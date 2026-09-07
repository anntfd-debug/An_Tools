# -*- coding: utf-8 -*-
"""
CREATE 90-DEG RIGHT ELBOW - EXACT SLOPE PRESERVATION
=====================================================

Supports:
- Horizontal Pipe
- Sloped Pipe
- Vertical Pipe

Key rule for SLOPED pipe
------------------------
The new pipe keeps EXACTLY the same geometric slope magnitude/direction
(as seen from the selected open end) as the existing pipe, while the two
3D centerline axes remain exactly perpendicular for a true 90-degree elbow.

Because of 3D geometry, a sloped pipe cannot generally turn exactly 90.000°
in PLAN and also keep both:
    1) identical slope, and
    2) a true 90.000° 3D elbow.

Therefore the tool solves the required PLAN turn angle mathematically.
For example, at 2.000% slope the required plan turn is about 90.022918°.

pyRevit / Revit 2025-2026
"""

__persistentengine__ = True

import math

from pyrevit import revit, DB, UI
from Autodesk.Revit.Exceptions import OperationCanceledException


doc = revit.doc
uidoc = revit.uidoc


# ============================================================
# SETTINGS
# ============================================================

NEW_PIPE_LENGTH_FT = 2.0       # ~610 mm

VECTOR_TOL = 1e-9
HORIZONTAL_Z_TOL = 1e-6
VERTICAL_XY_TOL = 1e-6

# Geometry validation tolerances.
PERP_DOT_TOL = 1e-9
SLOPE_RATIO_TOL = 1e-9


# ============================================================
# LIGHTWEIGHT ERROR UI
# ============================================================

def show_error(message):
    """Only show UI for real errors. Success/Cancel stay silent."""
    try:
        UI.TaskDialog.Show("Create 90 Right Elbow", str(message))
    except Exception:
        pass


# ============================================================
# SELECTION
# ============================================================

class PipeSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, DB.Plumbing.Pipe)

    def AllowReference(self, ref, pos):
        return True


# ============================================================
# VECTOR / GEOMETRY HELPERS
# ============================================================

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


def xy_length(v):
    return math.sqrt(v.X * v.X + v.Y * v.Y)


def signed_slope_ratio_from_direction(v):
    """
    Geometric signed slope = dZ / horizontal run.

    0.02 = 2%.
    """
    h = xy_length(v)
    if h <= VECTOR_TOL:
        return None
    return v.Z / h


def slope_magnitude_from_pipe(pipe):
    """Return absolute geometric slope ratio from the Pipe centerline."""
    loc = getattr(pipe, "Location", None)
    curve = getattr(loc, "Curve", None) if loc is not None else None
    if curve is None:
        return None

    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    direction = safe_normalize(p1 - p0)
    if direction is None:
        return None

    slope = signed_slope_ratio_from_direction(direction)
    if slope is None:
        return None
    return abs(slope)


def get_pipe_orientation(v_unit):
    """Return HORIZONTAL / SLOPED / VERTICAL."""
    h = xy_length(v_unit)

    if h <= VERTICAL_XY_TOL:
        return "VERTICAL"

    if abs(v_unit.Z) <= HORIZONTAL_Z_TOL:
        return "HORIZONTAL"

    return "SLOPED"


def get_pipe_outward_direction(pipe, end_connector):
    """
    Read the direction from the Pipe centerline itself, not from connector
    BasisZ, then orient it OUTWARD from the selected end.

    This is important for exact slope preservation because LocationCurve is
    the actual pipe geometry that Revit displays/calculates slope from.
    """
    loc = getattr(pipe, "Location", None)
    curve = getattr(loc, "Curve", None) if loc is not None else None
    if curve is None:
        raise Exception("Không đọc được centerline của Pipe hiện hữu.")

    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    c = end_connector.Origin

    d0 = c.DistanceTo(p0)
    d1 = c.DistanceTo(p1)

    if d0 <= d1:
        # Selected end is p0. Outward points away from the pipe body.
        v_out = p0 - p1
    else:
        # Selected end is p1.
        v_out = p1 - p0

    v_out = safe_normalize(v_out)
    if v_out is None:
        raise Exception("Hướng centerline của Pipe hiện hữu không hợp lệ.")

    return v_out


def get_active_view_right_perpendicular(v_out):
    """
    For a vertical pipe, define 'right' using Active View.
    Returned vector is perpendicular to v_out.
    """
    active_view = doc.ActiveView
    candidates = []

    try:
        candidates.append(active_view.RightDirection)
    except Exception:
        pass

    try:
        candidates.append(active_view.UpDirection)
    except Exception:
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


def get_right_turn_direction_exact_slope(v_out):
    """
    Return (new_direction, orientation, plan_turn_deg).

    HORIZONTAL
        90.000° right turn in plan.

    SLOPED
        Preserve the exact signed slope ratio of v_out and solve the plan
        turn angle so v_out dot v_new = 0 exactly.

        Let:
            h = horizontal component magnitude of the unit old direction
            z = vertical component of the unit old direction
            phi = plan turn angle

        For identical signed slope:
            dot(old, new) = h^2*cos(phi) + z^2 = 0

        therefore:
            cos(phi) = -z^2 / h^2 = -(slope_ratio)^2

        phi is slightly > 90° for ordinary positive/negative slopes.

    VERTICAL
        Use Active View right direction projected perpendicular to the pipe.
    """
    v_out = safe_normalize(v_out)
    if v_out is None:
        raise Exception("Hướng Pipe hiện hữu không hợp lệ.")

    orientation = get_pipe_orientation(v_out)

    # --------------------------------------------------------
    # VERTICAL
    # --------------------------------------------------------
    if orientation == "VERTICAL":
        v_new = get_active_view_right_perpendicular(v_out)
        if v_new is None:
            raise Exception(
                "Không xác định được hướng sang phải cho ống đứng trong Active View."
            )
        return v_new, orientation, 90.0

    h = xy_length(v_out)
    if h <= VECTOR_TOL:
        raise Exception("Không đọc được thành phần ngang của hướng Pipe.")

    # Horizontal unit direction of the existing pipe.
    u_plan = DB.XYZ(v_out.X / h, v_out.Y / h, 0.0)

    # Clockwise 90° in plan = RIGHT side when viewed from +Z.
    u_right = DB.XYZ(u_plan.Y, -u_plan.X, 0.0)

    # --------------------------------------------------------
    # HORIZONTAL
    # --------------------------------------------------------
    if orientation == "HORIZONTAL":
        v_new = safe_normalize(u_right)
        if v_new is None:
            raise Exception("Không tính được hướng 90° sang phải cho ống ngang.")
        return v_new, orientation, 90.0

    # --------------------------------------------------------
    # SLOPED - EXACT SAME SLOPE + TRUE 90° IN 3D
    # --------------------------------------------------------
    slope_ratio = v_out.Z / h

    # For same signed slope and true 90° 3D, a solution exists only when
    # |slope| <= 100%. Normal MEP pipe slopes are far below this.
    cos_phi = -(slope_ratio * slope_ratio)

    if cos_phi < -1.0 - SLOPE_RATIO_TOL or cos_phi > 1.0 + SLOPE_RATIO_TOL:
        raise Exception(
            "Slope hiện hữu quá lớn ({:.6f}%) nên không thể đồng thời giữ "
            "nguyên slope và tạo góc 90° thật trong 3D.".format(
                abs(slope_ratio) * 100.0
            )
        )

    # Clamp tiny floating-point overflow only.
    cos_phi = max(-1.0, min(1.0, cos_phi))
    sin_phi = math.sqrt(max(0.0, 1.0 - cos_phi * cos_phi))

    # Rotate from old plan direction toward RIGHT by phi.
    u_new_plan = (
        u_plan.Multiply(cos_phi)
        + u_right.Multiply(sin_phi)
    )
    u_new_plan = safe_normalize(u_new_plan)
    if u_new_plan is None:
        raise Exception("Không tính được hướng plan cho Pipe mới.")

    # Keep EXACTLY the same horizontal and vertical component magnitudes as
    # the normalized existing pipe direction. Therefore Z/XY is identical.
    v_new = (
        u_new_plan.Multiply(h)
        + DB.XYZ.BasisZ.Multiply(v_out.Z)
    )
    v_new = safe_normalize(v_new)

    if v_new is None:
        raise Exception("Không tính được vector Pipe mới giữ nguyên slope.")

    # --------------------------------------------------------
    # STRICT VALIDATION BEFORE CREATING REVIT GEOMETRY
    # --------------------------------------------------------
    dot = v_out.DotProduct(v_new)
    if abs(dot) > PERP_DOT_TOL:
        raise Exception(
            "Vector Pipe mới không đạt 90° 3D. Dot = {:.12f}".format(dot)
        )

    old_slope = signed_slope_ratio_from_direction(v_out)
    new_slope = signed_slope_ratio_from_direction(v_new)

    if old_slope is None or new_slope is None:
        raise Exception("Không kiểm tra được slope hình học.")

    if abs(old_slope - new_slope) > SLOPE_RATIO_TOL:
        raise Exception(
            "Slope vector không khớp: cũ = {:.9f}% | mới = {:.9f}%".format(
                old_slope * 100.0,
                new_slope * 100.0
            )
        )

    plan_turn_deg = math.degrees(math.acos(cos_phi))
    return v_new, orientation, plan_turn_deg


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
# MAIN
# ============================================================

def main():
    # --------------------------------------------------------
    # 1. PICK FIRST
    #    ESC exits before geometry / transaction work.
    # --------------------------------------------------------
    try:
        ref = uidoc.Selection.PickObject(
            UI.Selection.ObjectType.Element,
            PipeSelectionFilter(),
            "Chọn gần đầu ống hở để tạo Elbow 90 độ sang phải"
        )
    except OperationCanceledException:
        return
    except Exception as ex:
        show_error("Không thể bắt đầu lệnh chọn Pipe.\n\n{}".format(ex))
        return

    pipe = doc.GetElement(ref.ElementId)
    pick_point = ref.GlobalPoint

    if pipe is None or pick_point is None:
        show_error("Không đọc được ống hoặc vị trí click.")
        return

    conn1 = get_closest_end_connector(pipe, pick_point)

    if conn1 is None:
        show_error("Không tìm thấy end connector trên ống này.")
        return

    if conn1.IsConnected:
        show_error("Đầu ống này đã được kết nối. Vui lòng chọn một đầu ống hở.")
        return

    # --------------------------------------------------------
    # 2. READ ONLY WHAT IS NEEDED AFTER A VALID PICK
    # --------------------------------------------------------
    try:
        system_type_id, pipe_type_id, level_id, diameter = get_pipe_data(pipe)

        # Use actual centerline geometry for exact slope.
        v_out = get_pipe_outward_direction(pipe, conn1)
        v_new, orientation, plan_turn_deg = get_right_turn_direction_exact_slope(v_out)

        # We already have the exact old centerline direction. Avoid reading the
        # LocationCurve a second time just to get the same slope value.
        if orientation == "SLOPED":
            old_signed_slope = signed_slope_ratio_from_direction(v_out)
            if old_signed_slope is None:
                raise Exception("Không đọc được slope hình học của Pipe hiện hữu.")
            old_slope_before = abs(old_signed_slope)
        else:
            old_slope_before = None

    except Exception as ex:
        show_error(str(ex))
        return

    p_start = conn1.Origin
    p_end = p_start + v_new.Multiply(NEW_PIPE_LENGTH_FT)

    transaction = DB.Transaction(
        doc,
        "Tạo Elbow 90 - {} - giữ slope".format(orientation_text(orientation))
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

        # ----------------------------------------------------
        # FAST PATH
        # ----------------------------------------------------
        # Pipe.Create already received the mathematically validated endpoints.
        # Do NOT regenerate just to re-check the same pre-fitting slope.
        # Try connector access immediately. Revit normally exposes it directly.
        conn2 = get_closest_end_connector(new_pipe, p_start)

        # Rare fallback: some model states may not expose fresh connectors until
        # regeneration. Pay this cost only when actually necessary.
        if conn2 is None:
            doc.Regenerate()
            conn2 = get_closest_end_connector(new_pipe, p_start)

        if conn2 is None:
            raise Exception("Không tìm thấy connector đầu của đoạn Pipe mới.")

        # Geometry is already true 90° in 3D. Revit only places the elbow from
        # the current Pipe Type Routing Preferences.
        doc.Create.NewElbowFitting(conn1, conn2)

        # ----------------------------------------------------
        # FINAL SLOPE PROTECTION - SLOPED PIPE ONLY
        # ----------------------------------------------------
        # Horizontal / vertical cases do not need an expensive explicit
        # regeneration + slope verification.
        if orientation == "SLOPED":
            doc.Regenerate()

            old_slope_after = slope_magnitude_from_pipe(pipe)
            new_slope_after = slope_magnitude_from_pipe(new_pipe)

            if old_slope_after is None or new_slope_after is None:
                raise Exception("Không kiểm tra được slope sau khi tạo elbow.")

            if abs(old_slope_after - old_slope_before) > SLOPE_RATIO_TOL:
                raise Exception(
                    "Revit đã làm thay đổi slope của Pipe hiện hữu.\n"
                    "Trước: {:.9f}%\nSau: {:.9f}%".format(
                        old_slope_before * 100.0,
                        old_slope_after * 100.0
                    )
                )

            if abs(new_slope_after - old_slope_before) > SLOPE_RATIO_TOL:
                raise Exception(
                    "Revit đã làm thay đổi slope của Pipe mới sau khi tạo elbow.\n"
                    "Slope yêu cầu: {:.9f}%\n"
                    "Slope thực tế: {:.9f}%\n"
                    "Plan turn đã dùng: {:.9f}°".format(
                        old_slope_before * 100.0,
                        new_slope_after * 100.0,
                        plan_turn_deg
                    )
                )

        transaction.Commit()

    except Exception as ex:
        try:
            if transaction.GetStatus() == DB.TransactionStatus.Started:
                transaction.RollBack()
        except Exception:
            pass

        show_error(
            "Không thể tạo Elbow 90 độ cho {} mà vẫn giữ chính xác slope.\n\n"
            "Chi tiết lỗi:\n{}".format(
                orientation_text(orientation),
                ex
            )
        )
        return


if __name__ == "__main__":
    main()
