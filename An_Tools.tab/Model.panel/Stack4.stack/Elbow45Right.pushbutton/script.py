# -*- coding: utf-8 -*-
"""
CREATE 45-DEG RIGHT ELBOW FOR HORIZONTAL / SLOPED / VERTICAL PIPE
=================================================================

Workflow
--------
1. Run tool once.
2. Pick near an OPEN end of a Pipe.
3. Detect the nearest end connector.
4. Detect pipe orientation automatically:
      - Horizontal
      - Sloped
      - Vertical
5. Build a true 45-degree direction to the RIGHT.
6. Create a short new Pipe using the same:
      - Piping System Type
      - Pipe Type
      - Reference Level
      - Diameter
7. Create elbow with Document.Create.NewElbowFitting().
8. Continue picking until ESC is pressed.

Direction rule
--------------
HORIZONTAL:
    Turn 45 degrees to the RIGHT in plan.

SLOPED:
    First try to preserve the original slope exactly. The tool calculates
    the required plan rotation so the REAL 3D angle between old/new Pipe
    axes is exactly 45 degrees.

    If the source Pipe is so steep that a same-slope 45-degree solution is
    mathematically impossible, the tool falls back to a true 45-degree 3D
    direction on the RIGHT side. In that fallback, exact elbow geometry is
    prioritized over preserving slope.

VERTICAL:
    A vertical Pipe has no intrinsic RIGHT direction in plan. RIGHT is
    therefore defined by ActiveView.RightDirection. The new Pipe is tilted
    45 degrees from the vertical Pipe toward the right side of Active View.

Runtime behavior
----------------
- Success: silent, immediately ready for next pick.
- Invalid pick / connected end / fitting failure: one small error popup,
  then continue picking.
- ESC: exit tool normally.

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
NEW_PIPE_LENGTH_FT = 2.0       # ~610 mm

VECTOR_TOL = 1e-9
HORIZONTAL_Z_TOL = 1e-6
VERTICAL_XY_TOL = 1e-6

# Validate the real 3D angle after direction construction.
ANGLE_VALIDATION_TOL_DEG = 0.01


# ============================================================
# SELECTION
# ============================================================

class PipeSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, DB.Plumbing.Pipe)

    def AllowReference(self, ref, pos):
        return True


# ============================================================
# UI
# ============================================================

def show_error(message):
    """Small blocking popup; after closing it the continuous pick resumes."""
    try:
        forms.alert(
            message,
            title="Elbow 45 Right",
            warn_icon=True
        )
    except:
        try:
            UI.TaskDialog.Show("Elbow 45 Right", message)
        except:
            pass


# ============================================================
# VECTOR HELPERS
# ============================================================

def clamp(value, low, high):
    return max(low, min(high, value))


def vector_length(v):
    if v is None:
        return 0.0
    return math.sqrt(v.X * v.X + v.Y * v.Y + v.Z * v.Z)


def safe_normalize(v):
    length = vector_length(v)
    if length <= VECTOR_TOL:
        return None
    return DB.XYZ(v.X / length, v.Y / length, v.Z / length)


def subtract_projection(vector, axis_unit):
    """Project vector onto the plane perpendicular to axis_unit."""
    dot = vector.DotProduct(axis_unit)
    return vector - axis_unit.Multiply(dot)


def angle_between_deg(a, b):
    a = safe_normalize(a)
    b = safe_normalize(b)
    if a is None or b is None:
        return None

    dot = clamp(a.DotProduct(b), -1.0, 1.0)
    return math.degrees(math.acos(dot))


def get_pipe_orientation(v_unit):
    """Return HORIZONTAL / SLOPED / VERTICAL."""
    xy_len = math.sqrt(v_unit.X * v_unit.X + v_unit.Y * v_unit.Y)

    if xy_len <= VERTICAL_XY_TOL:
        return "VERTICAL"

    if abs(v_unit.Z) <= HORIZONTAL_Z_TOL:
        return "HORIZONTAL"

    return "SLOPED"


def get_active_view_right_perpendicular(v_out):
    """
    Return a unit vector on the RIGHT side of Active View and perpendicular
    to v_out. Used mainly for vertical / near-vertical Pipe.
    """
    active_view = doc.ActiveView
    candidates = []

    try:
        candidates.append(active_view.RightDirection)
    except:
        pass

    try:
        candidates.append(active_view.UpDirection)
    except:
        pass

    candidates.extend([
        DB.XYZ.BasisX,
        DB.XYZ.BasisY,
        DB.XYZ.BasisZ,
    ])

    for candidate in candidates:
        if candidate is None:
            continue

        perp = subtract_projection(candidate, v_out)
        perp_unit = safe_normalize(perp)

        if perp_unit is not None:
            return perp_unit

    return None


def get_horizontal_right_side(v_out):
    """
    Horizontal unit vector pointing to the RIGHT of the Pipe's XY projection.

    For plan vector (x, y), clockwise/right perpendicular is (y, -x).
    This vector is also exactly perpendicular to the full 3D v_out because
    its Z component is zero.
    """
    xy_len = math.sqrt(v_out.X * v_out.X + v_out.Y * v_out.Y)
    if xy_len <= VERTICAL_XY_TOL:
        return None

    return DB.XYZ(
        v_out.Y / xy_len,
        -v_out.X / xy_len,
        0.0
    )


def build_exact_angle_from_side(v_out, side_unit, angle_deg):
    """
    Build v_new = cos(a)*v_out + sin(a)*side, where side is perpendicular
    to v_out. This guarantees the true 3D angle is exactly angle_deg.
    """
    angle_rad = math.radians(angle_deg)

    v_new = (
        v_out.Multiply(math.cos(angle_rad))
        + side_unit.Multiply(math.sin(angle_rad))
    )

    return safe_normalize(v_new)


def get_sloped_same_slope_right_direction(v_out, angle_deg):
    """
    Try to preserve the exact source slope while making a true 3D bend angle.

    Let z = vertical component of the source unit vector and h^2 = 1-z^2.
    If the new vector keeps the same z and same horizontal magnitude h, then:

        dot(old, new) = h^2 * cos(plan_turn) + z^2

    For target 3D angle theta:

        cos(plan_turn) = (cos(theta) - z^2) / h^2

    A negative plan rotation is used for RIGHT/clockwise.
    Return None when no same-slope solution exists.
    """
    z2 = v_out.Z * v_out.Z
    h2 = max(0.0, 1.0 - z2)

    if h2 <= VECTOR_TOL:
        return None

    target_cos = math.cos(math.radians(angle_deg))
    cos_plan_turn = (target_cos - z2) / h2

    # No real plan angle can satisfy same-slope + requested 3D angle.
    if cos_plan_turn < -1.0 - 1e-10 or cos_plan_turn > 1.0 + 1e-10:
        return None

    cos_plan_turn = clamp(cos_plan_turn, -1.0, 1.0)
    plan_turn_rad = math.acos(cos_plan_turn)

    rot_transform = DB.Transform.CreateRotation(
        DB.XYZ.BasisZ,
        -plan_turn_rad
    )

    return safe_normalize(rot_transform.OfVector(v_out))


def get_right_turn_direction(v_out):
    """
    Return (new_direction, orientation, mode).

    The returned new_direction forms a TRUE 45-degree 3D angle with v_out.
    """
    v_out = safe_normalize(v_out)
    if v_out is None:
        raise Exception("Hướng Pipe gốc không hợp lệ.")

    orientation = get_pipe_orientation(v_out)

    # --------------------------------------------------------
    # HORIZONTAL PIPE
    # --------------------------------------------------------
    if orientation == "HORIZONTAL":
        rot_transform = DB.Transform.CreateRotation(
            DB.XYZ.BasisZ,
            -math.radians(TURN_ANGLE_DEG)
        )
        v_new = safe_normalize(rot_transform.OfVector(v_out))
        mode = "plan-right"

    # --------------------------------------------------------
    # SLOPED PIPE
    # --------------------------------------------------------
    elif orientation == "SLOPED":
        # Preferred solution: preserve exact source slope and solve the plan
        # turn required for a real 45-degree angle in 3D.
        v_new = get_sloped_same_slope_right_direction(
            v_out,
            TURN_ANGLE_DEG
        )

        if v_new is not None:
            mode = "same-slope"
        else:
            # Steep-pipe fallback: exact 45 degrees takes priority.
            side = get_horizontal_right_side(v_out)
            if side is None:
                side = get_active_view_right_perpendicular(v_out)

            if side is None:
                raise Exception(
                    "Không xác định được hướng sang phải cho Pipe có độ dốc."
                )

            # Ensure the side vector is truly perpendicular before combining.
            side = safe_normalize(subtract_projection(side, v_out))
            if side is None:
                raise Exception(
                    "Không tính được vector bên phải vuông góc với Pipe gốc."
                )

            v_new = build_exact_angle_from_side(
                v_out,
                side,
                TURN_ANGLE_DEG
            )
            mode = "true-3d-fallback"

    # --------------------------------------------------------
    # VERTICAL PIPE
    # --------------------------------------------------------
    else:
        side = get_active_view_right_perpendicular(v_out)
        if side is None:
            raise Exception(
                "Không xác định được hướng sang phải cho ống đứng trong Active View."
            )

        v_new = build_exact_angle_from_side(
            v_out,
            side,
            TURN_ANGLE_DEG
        )
        mode = "active-view-right"

    if v_new is None:
        raise Exception("Không tính được hướng Pipe mới.")

    actual_angle = angle_between_deg(v_out, v_new)
    if actual_angle is None:
        raise Exception("Không kiểm tra được góc Pipe mới.")

    if abs(actual_angle - TURN_ANGLE_DEG) > ANGLE_VALIDATION_TOL_DEG:
        raise Exception(
            "Góc 3D tính được không đúng 45 độ. Actual = {:.6f}°".format(
                actual_angle
            )
        )

    return v_new, orientation, mode


# ============================================================
# PIPE / CONNECTOR HELPERS
# ============================================================

def get_closest_end_connector(element, pick_point):
    """Get the End connector nearest the click point."""
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


def get_pipe_outward_direction(pipe, end_connector):
    """
    Get a stable outward direction from the actual Pipe LocationCurve.

    At curve endpoint 0: outward = endpoint0 - endpoint1
    At curve endpoint 1: outward = endpoint1 - endpoint0

    Fall back to connector BasisZ if LocationCurve is unavailable.
    """
    try:
        location = pipe.Location
        curve = location.Curve if location is not None else None

        if curve is not None:
            p0 = curve.GetEndPoint(0)
            p1 = curve.GetEndPoint(1)
            origin = end_connector.Origin

            d0 = origin.DistanceTo(p0)
            d1 = origin.DistanceTo(p1)

            if d0 <= d1:
                direction = p0 - p1
            else:
                direction = p1 - p0

            direction = safe_normalize(direction)
            if direction is not None:
                return direction
    except:
        pass

    try:
        direction = safe_normalize(
            end_connector.CoordinateSystem.BasisZ
        )
        if direction is not None:
            return direction
    except:
        pass

    return None


def get_pipe_data(pipe):
    """Read data required to create a matching Pipe segment."""
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


def orientation_text(orientation):
    if orientation == "HORIZONTAL":
        return "ống ngang"
    if orientation == "SLOPED":
        return "ống có độ dốc"
    if orientation == "VERTICAL":
        return "ống đứng"
    return orientation


# ============================================================
# ONE PICK / ONE TRANSACTION
# ============================================================

def process_pick(ref):
    """
    Process one selected Pipe end.

    Any failure is contained to this pick. The caller keeps the command alive.
    """
    pipe = doc.GetElement(ref.ElementId)
    pick_point = ref.GlobalPoint

    if pipe is None or pick_point is None:
        raise Exception("Không đọc được Pipe hoặc vị trí click.")

    conn1 = get_closest_end_connector(pipe, pick_point)
    if conn1 is None:
        raise Exception("Không tìm thấy End Connector trên Pipe này.")

    if conn1.IsConnected:
        raise Exception(
            "Đầu Pipe này đã được kết nối. Vui lòng chọn một đầu Pipe hở."
        )

    system_type_id, pipe_type_id, level_id, diameter = get_pipe_data(pipe)

    v_out = get_pipe_outward_direction(pipe, conn1)
    if v_out is None:
        raise Exception("Không xác định được hướng đi ra khỏi đầu Pipe.")

    v_new, orientation, direction_mode = get_right_turn_direction(v_out)

    p_start = conn1.Origin
    p_end = p_start + v_new.Multiply(NEW_PIPE_LENGTH_FT)

    transaction = DB.Transaction(
        doc,
        "Tạo Elbow 45 - {}".format(orientation_text(orientation))
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
            raise Exception("Không tìm thấy connector đầu của đoạn Pipe mới.")

        # The centerline axes were constructed at a true 45-degree 3D angle.
        # Revit chooses the elbow family/type through Pipe Type routing rules.
        doc.Create.NewElbowFitting(conn1, conn2)

        transaction.Commit()

    except Exception:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        raise

    # Returned only for optional diagnostics; success remains silent.
    return orientation, direction_mode


# ============================================================
# MAIN - CONTINUOUS PICK UNTIL ESC
# ============================================================

def main():
    selection_filter = PipeSelectionFilter()

    while True:
        try:
            ref = uidoc.Selection.PickObject(
                UI.Selection.ObjectType.Element,
                selection_filter,
                "Chọn gần đầu Pipe hở để tạo Elbow 45° sang phải | ESC để thoát"
            )

        except OperationCanceledException:
            # ESC / Cancel = normal silent exit.
            break

        except Exception as ex:
            # Unexpected selection-layer error: show once and stop because the
            # Revit pick context may no longer be valid.
            show_error(
                "Không thể tiếp tục chế độ chọn Pipe.\n\n{}".format(ex)
            )
            break

        try:
            process_pick(ref)

        except Exception as ex:
            # Failure of one Pipe must NOT end the command.
            show_error(
                "Không thể tạo Elbow 45° sang phải.\n\n"
                "Hãy kiểm tra:\n"
                "- Đầu Pipe đang chọn phải là đầu hở;\n"
                "- Routing Preferences của Pipe Type;\n"
                "- Family Elbow có hỗ trợ 45°;\n"
                "- Connector / family cho phép hình học slope cần thiết;\n"
                "- Đường kính và khoảng hình học hợp lệ.\n\n"
                "Lần chọn này đã rollback, tool vẫn tiếp tục.\n\n"
                "Chi tiết lỗi:\n{}".format(ex)
            )
            continue

        # Success is intentionally silent. Loop returns directly to PickObject.


if __name__ == "__main__":
    main()
