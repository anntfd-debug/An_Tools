# -*- coding: utf-8 -*-
"""
ELBOW 45 LEFT - CONTINUOUS - ALL PIPE DIRECTIONS
================================================

Supports:
- Horizontal pipe
- Sloped pipe
- Vertical pipe

Workflow:
1. Run tool.
2. Pick near an OPEN end of a Pipe.
3. Tool creates a new Pipe + 45-degree elbow to the LEFT.
4. Tool immediately returns to Pick mode.
5. Press ESC to finish.

Direction rules:
- Horizontal pipe:
    Turn 45 degrees left in plan.
- Sloped pipe:
    Preserve the original Z component (therefore preserve slope) whenever
    mathematically possible, while correcting the plan-turn angle so the
    TRUE 3D angle between old/new pipe axes is 45 degrees.
    For extremely steep non-vertical pipes where that is impossible, use a
    true 3D 45-degree turn toward plan-left.
- Vertical pipe:
    "Left" is defined as the LEFT side of the Active View.
    The new pipe makes a true 45-degree 3D angle with the vertical pipe.

Safety:
- Only Pipe can be picked.
- Only an open end can be processed.
- Each pick uses its own Transaction.
- Failure rolls back only that pick and returns to continuous Pick mode.
- ESC exits silently.

pyRevit / Revit 2025-2026
"""

import math

from pyrevit import revit, DB, UI, forms
from Autodesk.Revit.Exceptions import OperationCanceledException


doc = revit.doc
uidoc = revit.uidoc


# ============================================================
# SETTINGS
# ============================================================

TURN_ANGLE_DEG = 45.0
TURN_ANGLE_RAD = math.radians(TURN_ANGLE_DEG)

# New straight segment length before the next user action.
# Revit internal unit = feet. 2.0 ft ~= 610 mm.
NEW_PIPE_LENGTH_FT = 2.0

# Classification tolerances.
HORIZONTAL_Z_TOL = 1e-8
VERTICAL_HORIZONTAL_TOL = 1e-6
VECTOR_TOL = 1e-9


# ============================================================
# SELECTION
# ============================================================

class PipeSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, DB.Plumbing.Pipe)

    def AllowReference(self, ref, pos):
        return True


# ============================================================
# VECTOR HELPERS
# ============================================================

def clamp(value, low, high):
    return max(low, min(high, value))


def xyz_length(vec):
    return math.sqrt(
        vec.X * vec.X +
        vec.Y * vec.Y +
        vec.Z * vec.Z
    )


def normalize_or_none(vec):
    if vec is None:
        return None

    if xyz_length(vec) <= VECTOR_TOL:
        return None

    return vec.Normalize()


def horizontal_component(vec):
    return DB.XYZ(vec.X, vec.Y, 0.0)


def get_view_left_direction():
    """
    Return a stable direction representing LEFT on the Active View.

    For a vertical source pipe, this direction is projected onto the XY plane
    because the outgoing segment needs a horizontal left/right component.
    """
    view = doc.ActiveView

    try:
        right = view.RightDirection
        left = right.Multiply(-1.0)
        left_xy = horizontal_component(left)
        left_xy = normalize_or_none(left_xy)
        if left_xy is not None:
            return left_xy
    except:
        pass

    # Safe fallback for unusual view types.
    return DB.XYZ(-1.0, 0.0, 0.0)


def get_pipe_outward_direction(pipe, end_connector):
    """
    Get the direction going OUT of the selected pipe end.

    Connector BasisZ is used as the primary axis, then its sign is validated
    against the Pipe LocationCurve so the direction points away from the pipe.
    """
    try:
        basis_z = end_connector.CoordinateSystem.BasisZ
        direction = normalize_or_none(basis_z)
    except:
        direction = None

    location = getattr(pipe, "Location", None)
    curve = getattr(location, "Curve", None) if location is not None else None

    if curve is not None:
        try:
            midpoint = curve.Evaluate(0.5, True)
            away_hint = end_connector.Origin - midpoint
            away_hint = normalize_or_none(away_hint)

            if away_hint is not None:
                if direction is None:
                    direction = away_hint
                elif direction.DotProduct(away_hint) < 0.0:
                    direction = direction.Multiply(-1.0)
        except:
            pass

    if direction is None:
        raise Exception("Không xác định được hướng đi ra khỏi đầu ống.")

    return direction.Normalize()


# ============================================================
# CONNECTOR / PIPE DATA
# ============================================================

def get_closest_end_connector(element, pick_point):
    """Lấy end connector gần điểm click nhất."""
    connector_manager = getattr(element, "ConnectorManager", None)
    if connector_manager is None:
        return None

    closest_conn = None
    min_dist = float("inf")

    for conn in connector_manager.Connectors:
        if conn.ConnectorType != DB.ConnectorType.End:
            continue

        dist = conn.Origin.DistanceTo(pick_point)
        if dist < min_dist:
            min_dist = dist
            closest_conn = conn

    return closest_conn


def get_pipe_data(pipe):
    """Đọc dữ liệu cần thiết để tạo đoạn ống mới."""
    system_param = pipe.get_Parameter(
        DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM
    )
    diameter_param = pipe.get_Parameter(
        DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM
    )

    if system_param is None:
        raise Exception("Không đọc được Piping System Type của ống gốc.")

    system_type_id = system_param.AsElementId()
    if system_type_id == DB.ElementId.InvalidElementId:
        raise Exception("Ống gốc chưa có Piping System Type hợp lệ.")

    reference_level = pipe.ReferenceLevel
    if reference_level is None:
        raise Exception("Không đọc được Reference Level của ống gốc.")

    diameter = diameter_param.AsDouble() if diameter_param else None

    return (
        system_type_id,
        pipe.GetTypeId(),
        reference_level.Id,
        diameter,
    )


# ============================================================
# DIRECTION LOGIC
# ============================================================

def classify_pipe_direction(v_out):
    """Return: 'horizontal', 'sloped', or 'vertical'."""
    h = horizontal_component(v_out)
    h_len = xyz_length(h)

    if h_len <= VERTICAL_HORIZONTAL_TOL:
        return "vertical"

    if abs(v_out.Z) <= HORIZONTAL_Z_TOL:
        return "horizontal"

    return "sloped"


def rotate_left_about_global_z(vector, angle_rad):
    transform = DB.Transform.CreateRotation(
        DB.XYZ.BasisZ,
        angle_rad
    )
    return transform.OfVector(vector).Normalize()


def get_horizontal_new_direction(v_out):
    """Horizontal source: simple 45-degree left turn in plan."""
    return rotate_left_about_global_z(v_out, TURN_ANGLE_RAD)


def get_sloped_new_direction(v_out):
    """
    Sloped source pipe.

    First choice:
        Keep v_new.Z == v_out.Z, preserving the original slope/elevation
        component. Because a simple 45-degree plan rotation would NOT be a
        true 45-degree 3D turn on a sloped pipe, solve the required plan angle:

            dot(v_old, v_new) = cos(TURN_ANGLE)

        with unchanged Z.

    Fallback for extremely steep pipes:
        Create a true 3D 45-degree vector toward plan-left. This keeps the
        fitting angle correct but cannot preserve the exact original slope.
    """
    z = v_out.Z
    h2 = max(0.0, 1.0 - z * z)

    if h2 > VECTOR_TOL:
        cos_turn = math.cos(TURN_ANGLE_RAD)
        cos_plan = (cos_turn - z * z) / h2

        # If cos_plan is inside [-1, 1], the exact source Z can be preserved.
        if cos_plan >= -1.0 - 1e-10 and cos_plan <= 1.0 + 1e-10:
            cos_plan = clamp(cos_plan, -1.0, 1.0)
            plan_angle = math.acos(cos_plan)
            return rotate_left_about_global_z(v_out, plan_angle)

    # Fallback: exact 3D angle toward the plan-left side.
    h = normalize_or_none(horizontal_component(v_out))
    if h is None:
        return get_vertical_new_direction(v_out)

    # Z x horizontal = left in plan and is perpendicular to v_out.
    left = DB.XYZ.BasisZ.CrossProduct(h)
    left = normalize_or_none(left)
    if left is None:
        return get_vertical_new_direction(v_out)

    v_new = (
        v_out.Multiply(math.cos(TURN_ANGLE_RAD)) +
        left.Multiply(math.sin(TURN_ANGLE_RAD))
    )
    return v_new.Normalize()


def get_vertical_new_direction(v_out):
    """
    Vertical source pipe.

    There is no intrinsic 'left' around a vertical pipe, so LEFT is defined by
    the Active View. The resulting vector is a true 45-degree 3D turn.
    """
    view_left = get_view_left_direction()

    # Project view-left onto the plane perpendicular to the source direction.
    projected = (
        view_left -
        v_out.Multiply(view_left.DotProduct(v_out))
    )
    projected = normalize_or_none(projected)

    if projected is None:
        # Extremely unusual view orientation: use a stable world-axis fallback.
        fallback = DB.XYZ.BasisX
        if abs(fallback.DotProduct(v_out)) > 0.99:
            fallback = DB.XYZ.BasisY

        projected = (
            fallback -
            v_out.Multiply(fallback.DotProduct(v_out))
        )
        projected = normalize_or_none(projected)

    if projected is None:
        raise Exception("Không xác định được hướng trái cho ống đứng.")

    v_new = (
        v_out.Multiply(math.cos(TURN_ANGLE_RAD)) +
        projected.Multiply(math.sin(TURN_ANGLE_RAD))
    )
    return v_new.Normalize()


def get_new_pipe_direction(pipe, conn1):
    v_out = get_pipe_outward_direction(pipe, conn1)
    pipe_kind = classify_pipe_direction(v_out)

    if pipe_kind == "horizontal":
        return get_horizontal_new_direction(v_out), pipe_kind

    if pipe_kind == "sloped":
        return get_sloped_new_direction(v_out), pipe_kind

    return get_vertical_new_direction(v_out), pipe_kind


# ============================================================
# CREATE ONE ELBOW
# ============================================================

def create_one_elbow(pipe, pick_point):
    conn1 = get_closest_end_connector(pipe, pick_point)

    if conn1 is None:
        raise Exception("Không tìm thấy end connector trên ống này.")

    if conn1.IsConnected:
        raise Exception(
            "Đầu ống này đã được kết nối.\n"
            "Vui lòng chọn một điểm gần đầu ống hở."
        )

    system_type_id, pipe_type_id, level_id, diameter = get_pipe_data(pipe)
    v_new, pipe_kind = get_new_pipe_direction(pipe, conn1)

    p_start = conn1.Origin
    p_end = p_start + v_new.Multiply(NEW_PIPE_LENGTH_FT)

    transaction = DB.Transaction(
        doc,
        "Tạo Elbow {} độ sang trái".format(int(TURN_ANGLE_DEG))
    )

    try:
        transaction.Start()

        new_pipe = DB.Plumbing.Pipe.Create(
            doc,
            system_type_id,
            pipe_type_id,
            level_id,
            p_start,
            p_end
        )

        if diameter is not None:
            new_diameter_param = new_pipe.get_Parameter(
                DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM
            )
            if new_diameter_param and not new_diameter_param.IsReadOnly:
                new_diameter_param.Set(diameter)

        doc.Regenerate()

        conn2 = get_closest_end_connector(new_pipe, p_start)
        if conn2 is None:
            raise Exception("Không tìm thấy connector đầu của đoạn ống mới.")

        doc.Create.NewElbowFitting(conn1, conn2)
        transaction.Commit()

        return pipe_kind

    except Exception:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        raise


# ============================================================
# CONTINUOUS COMMAND
# ============================================================

def main():
    while True:
        try:
            ref = uidoc.Selection.PickObject(
                UI.Selection.ObjectType.Element,
                PipeSelectionFilter(),
                "Chọn gần đầu ống hở để tạo co {} độ sang trái | ESC để thoát".format(
                    int(TURN_ANGLE_DEG)
                )
            )
        except OperationCanceledException:
            # ESC/Cancel = normal command finish.
            return

        try:
            pipe = doc.GetElement(ref.ElementId)
            pick_point = ref.GlobalPoint

            if pipe is None or pick_point is None:
                raise Exception("Không đọc được ống hoặc vị trí click.")

            create_one_elbow(pipe, pick_point)

            # Success = silent. Loop immediately returns to PickObject().

        except Exception as ex:
            # Failure of one pick does NOT terminate the continuous command.
            forms.alert(
                "Không thể tạo fitting {} độ.\n\n"
                "Tool vẫn đang chạy; đóng thông báo và pick lại, "
                "hoặc nhấn ESC để thoát.\n\n"
                "Hãy kiểm tra:\n"
                "- Routing Preferences của Pipe Type;\n"
                "- Family elbow có hỗ trợ góc {} độ;\n"
                "- Đầu ống đang chọn thực sự đang hở;\n"
                "- Đường kính và khoảng hình học hợp lệ.\n\n"
                "Chi tiết lỗi:\n{}".format(
                    int(TURN_ANGLE_DEG),
                    int(TURN_ANGLE_DEG),
                    ex
                )
            )
            continue


if __name__ == "__main__":
    main()
