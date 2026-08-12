# -*- coding: utf-8 -*-
__title__ = "Align Pipe AZN"
__doc__ = """
Align ống di chuyển lên phía trên ống chuẩn theo khe hở AZN.

- Click thường: align ống di chuyển đến đúng khe hở yêu cầu.
- Shift + Click: chỉ kiểm tra khe hở hiện tại, không thay đổi mô hình.
- Hỗ trợ hai ống có độ dốc khác nhau.
- Cao độ được tính tại điểm giao hoặc vị trí gần nhau nhất trên mặt bằng.
"""

import math
import traceback

from Autodesk.Revit.DB import (
    XYZ,
    ElementTransformUtils,
    BuiltInParameter,
)
from Autodesk.Revit.DB.Plumbing import Pipe
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException

from pyrevit import revit, forms, script, EXEC_PARAMS

try:
    from System.Windows.Input import Keyboard, Key
except Exception:
    Keyboard = None
    Key = None


# =============================================================================
# CẤU HÌNH
# =============================================================================

# Nếu OD ống chuẩn lớn hơn OD ống di chuyển thì yêu cầu khe hở 10 mm.
LARGE_BASE_REQUIRED_GAP_MM = 10.0

# Nếu OD ống chuẩn bằng hoặc nhỏ hơn OD ống di chuyển thì yêu cầu khe hở 0 mm.
DEFAULT_REQUIRED_GAP_MM = 0.0

# Sai số so sánh đường kính.
DIAMETER_COMPARE_TOLERANCE_MM = 0.1

# Sai số đánh giá khe hở đã đạt đúng yêu cầu.
CLEARANCE_TOLERANCE_MM = 0.5

# Nếu hai đường tim không giao nhau trên mặt bằng, tool dùng hai điểm gần nhau nhất.
# Giá trị này chỉ dùng để cảnh báo, không ngăn tool hoạt động.
PLAN_DISTANCE_WARNING_MM = 100.0

# Sai số hình học nội bộ.
GEOM_TOL_FT = 1.0e-9


output = script.get_output()


# =============================================================================
# ĐƠN VỊ
# =============================================================================

def mm_to_ft(value_mm):
    return float(value_mm) / 304.8


def ft_to_mm(value_ft):
    return float(value_ft) * 304.8


# =============================================================================
# SELECTION FILTER
# =============================================================================

class PipeSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, Pipe)

    def AllowReference(self, reference, position):
        return False


# =============================================================================
# HÀM VECTOR 2D
# =============================================================================

def vec2_sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def vec2_add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def vec2_mul(v, scalar):
    return (v[0] * scalar, v[1] * scalar)


def vec2_dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def vec2_cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def vec2_length_sq(v):
    return vec2_dot(v, v)


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def xyz_lerp(p0, p1, parameter):
    return XYZ(
        p0.X + (p1.X - p0.X) * parameter,
        p0.Y + (p1.Y - p0.Y) * parameter,
        p0.Z + (p1.Z - p0.Z) * parameter,
    )


def project_point_to_segment_2d(point, seg_start, seg_end):
    """
    Chiếu một điểm 2D lên đoạn thẳng 2D.

    Trả về:
        parameter: 0..1 trên đoạn thẳng
        projected_point: điểm chiếu 2D
    """
    segment = vec2_sub(seg_end, seg_start)
    length_sq = vec2_length_sq(segment)

    if length_sq <= GEOM_TOL_FT:
        return 0.0, seg_start

    parameter = vec2_dot(vec2_sub(point, seg_start), segment) / length_sq
    parameter = clamp(parameter, 0.0, 1.0)
    projected = vec2_add(seg_start, vec2_mul(segment, parameter))
    return parameter, projected


def get_plan_reference_parameters(base_p0, base_p1, move_p0, move_p1):
    """
    Tìm vị trí dùng để so sánh hai ống trên mặt bằng XY.

    Ưu tiên:
    1. Nếu hai đoạn tim giao nhau trong phạm vi đoạn: dùng điểm giao.
    2. Nếu không giao: dùng cặp điểm gần nhau nhất giữa hai đoạn.

    Trả về dictionary:
        base_t          : tham số 0..1 trên ống chuẩn
        move_t          : tham số 0..1 trên ống di chuyển
        plan_distance   : khoảng cách XY giữa hai điểm tham chiếu, đơn vị feet
        method          : "intersection" hoặc "closest"
    """
    a0 = (base_p0.X, base_p0.Y)
    a1 = (base_p1.X, base_p1.Y)
    b0 = (move_p0.X, move_p0.Y)
    b1 = (move_p1.X, move_p1.Y)

    r = vec2_sub(a1, a0)
    s = vec2_sub(b1, b0)

    r_len_sq = vec2_length_sq(r)
    s_len_sq = vec2_length_sq(s)

    if r_len_sq <= GEOM_TOL_FT:
        raise ValueError("Ống chuẩn có hình chiếu trên mặt bằng quá ngắn hoặc gần như thẳng đứng.")

    if s_len_sq <= GEOM_TOL_FT:
        raise ValueError("Ống di chuyển có hình chiếu trên mặt bằng quá ngắn hoặc gần như thẳng đứng.")

    denominator = vec2_cross(r, s)
    a0_to_b0 = vec2_sub(b0, a0)

    # Hai đường không song song: thử lấy giao điểm của hai đoạn hữu hạn.
    if abs(denominator) > GEOM_TOL_FT:
        base_t = vec2_cross(a0_to_b0, s) / denominator
        move_t = vec2_cross(a0_to_b0, r) / denominator

        parameter_tolerance = 1.0e-7
        if (
            -parameter_tolerance <= base_t <= 1.0 + parameter_tolerance
            and -parameter_tolerance <= move_t <= 1.0 + parameter_tolerance
        ):
            return {
                "base_t": clamp(base_t, 0.0, 1.0),
                "move_t": clamp(move_t, 0.0, 1.0),
                "plan_distance": 0.0,
                "method": "intersection",
            }

    # Không giao nhau trong phạm vi hai đoạn: xét các cặp điểm gần nhất.
    candidates = []

    # Đầu A0 chiếu lên đoạn B.
    move_t, projected_b = project_point_to_segment_2d(a0, b0, b1)
    delta = vec2_sub(a0, projected_b)
    candidates.append((vec2_length_sq(delta), 0.0, move_t))

    # Đầu A1 chiếu lên đoạn B.
    move_t, projected_b = project_point_to_segment_2d(a1, b0, b1)
    delta = vec2_sub(a1, projected_b)
    candidates.append((vec2_length_sq(delta), 1.0, move_t))

    # Đầu B0 chiếu lên đoạn A.
    base_t, projected_a = project_point_to_segment_2d(b0, a0, a1)
    delta = vec2_sub(projected_a, b0)
    candidates.append((vec2_length_sq(delta), base_t, 0.0))

    # Đầu B1 chiếu lên đoạn A.
    base_t, projected_a = project_point_to_segment_2d(b1, a0, a1)
    delta = vec2_sub(projected_a, b1)
    candidates.append((vec2_length_sq(delta), base_t, 1.0))

    candidates.sort(key=lambda item: item[0])
    min_distance_sq, base_t, move_t = candidates[0]

    return {
        "base_t": base_t,
        "move_t": move_t,
        "plan_distance": math.sqrt(max(0.0, min_distance_sq)),
        "method": "closest",
    }


# =============================================================================
# HÌNH HỌC PIPE
# =============================================================================

def get_pipe_data(pipe):
    """Đọc curve, endpoint, OD, bán kính và độ dốc hình học của Pipe."""
    if not isinstance(pipe, Pipe):
        raise ValueError("Đối tượng được chọn không phải Pipe.")

    location = pipe.Location
    if location is None or not hasattr(location, "Curve") or location.Curve is None:
        raise ValueError("Không đọc được đường tim của Pipe ID {}.".format(pipe.Id.IntegerValue))

    curve = location.Curve
    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)

    axis_vector = p1 - p0
    axis_length = axis_vector.GetLength()
    if axis_length <= GEOM_TOL_FT:
        raise ValueError("Pipe ID {} có chiều dài không hợp lệ.".format(pipe.Id.IntegerValue))

    axis_direction = axis_vector.Normalize()
    horizontal_factor = math.sqrt(
        axis_direction.X * axis_direction.X
        + axis_direction.Y * axis_direction.Y
    )

    if horizontal_factor <= 1.0e-6:
        raise ValueError(
            "Pipe ID {} gần như thẳng đứng nên không thể kiểm tra khe hở trên mặt bằng.".format(
                pipe.Id.IntegerValue
            )
        )

    outer_param = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_OUTER_DIAMETER)
    if outer_param is None or not outer_param.HasValue:
        raise ValueError(
            "Không đọc được Outer Diameter của Pipe ID {}.".format(pipe.Id.IntegerValue)
        )

    outer_diameter = outer_param.AsDouble()
    if outer_diameter <= GEOM_TOL_FT:
        raise ValueError(
            "Outer Diameter của Pipe ID {} không hợp lệ.".format(pipe.Id.IntegerValue)
        )

    radius = outer_diameter / 2.0

    # Với ống dốc, giao tuyến của một đường thẳng đứng qua tim ống với mặt trụ
    # có nửa chiều cao theo Z bằng R / độ lớn thành phần ngang của vector trục.
    # Khi ống nằm ngang, horizontal_factor = 1 và giá trị này đúng bằng R.
    vertical_surface_half_height = radius / horizontal_factor

    slope_percent = 0.0
    horizontal_length = math.sqrt(
        (p1.X - p0.X) * (p1.X - p0.X)
        + (p1.Y - p0.Y) * (p1.Y - p0.Y)
    )
    if horizontal_length > GEOM_TOL_FT:
        slope_percent = ((p1.Z - p0.Z) / horizontal_length) * 100.0

    return {
        "pipe": pipe,
        "curve": curve,
        "p0": p0,
        "p1": p1,
        "outer_diameter": outer_diameter,
        "radius": radius,
        "axis_direction": axis_direction,
        "horizontal_factor": horizontal_factor,
        "vertical_half_height": vertical_surface_half_height,
        "slope_percent": slope_percent,
    }


def get_required_gap_ft(base_outer_diameter, move_outer_diameter):
    diameter_tolerance_ft = mm_to_ft(DIAMETER_COMPARE_TOLERANCE_MM)

    if base_outer_diameter > move_outer_diameter + diameter_tolerance_ft:
        return mm_to_ft(LARGE_BASE_REQUIRED_GAP_MM)

    return mm_to_ft(DEFAULT_REQUIRED_GAP_MM)


def calculate_clearance(base_data, move_data):
    """
    Tính khe hở theo phương Z tại điểm giao hoặc điểm gần nhau nhất trên mặt bằng.

    actual_gap = đáy ống di chuyển - đỉnh ống chuẩn

    Kết quả âm nghĩa là vùng bao theo phương đứng của hai ống đang chồng lên nhau.
    """
    ref_data = get_plan_reference_parameters(
        base_data["p0"],
        base_data["p1"],
        move_data["p0"],
        move_data["p1"],
    )

    base_center = xyz_lerp(
        base_data["p0"],
        base_data["p1"],
        ref_data["base_t"],
    )
    move_center = xyz_lerp(
        move_data["p0"],
        move_data["p1"],
        ref_data["move_t"],
    )

    base_top_z = base_center.Z + base_data["vertical_half_height"]
    move_bottom_z = move_center.Z - move_data["vertical_half_height"]

    actual_gap = move_bottom_z - base_top_z
    required_gap = get_required_gap_ft(
        base_data["outer_diameter"],
        move_data["outer_diameter"],
    )

    return {
        "reference": ref_data,
        "base_center": base_center,
        "move_center": move_center,
        "base_top_z": base_top_z,
        "move_bottom_z": move_bottom_z,
        "actual_gap": actual_gap,
        "required_gap": required_gap,
        "difference": actual_gap - required_gap,
    }


# =============================================================================
# THÔNG BÁO
# =============================================================================

def get_reference_method_text(method):
    if method == "intersection":
        return "Giao điểm hai đường tim trên mặt bằng"
    return "Cặp điểm gần nhau nhất trên mặt bằng"


def build_check_message(base_data, move_data, result):
    actual_mm = ft_to_mm(result["actual_gap"])
    required_mm = ft_to_mm(result["required_gap"])
    difference_mm = ft_to_mm(result["difference"])
    plan_distance_mm = ft_to_mm(result["reference"]["plan_distance"])

    tolerance = CLEARANCE_TOLERANCE_MM

    if actual_mm < required_mm - tolerance:
        status = "CHƯA ĐẠT"
        status_detail = "Thiếu {:.2f} mm so với yêu cầu.".format(abs(difference_mm))
    elif actual_mm > required_mm + tolerance:
        status = "LỚN HƠN YÊU CẦU"
        status_detail = "Dư {:.2f} mm so với khe hở yêu cầu.".format(difference_mm)
    else:
        status = "ĐẠT ĐÚNG YÊU CẦU"
        status_detail = "Sai lệch nằm trong dung sai ±{:.2f} mm.".format(tolerance)

    if actual_mm < 0.0:
        overlap_text = (
            "\nCẢNH BÁO: Đáy ống di chuyển đang thấp hơn đỉnh ống chuẩn "
            "{:.2f} mm.".format(abs(actual_mm))
        )
    else:
        overlap_text = ""

    if plan_distance_mm > PLAN_DISTANCE_WARNING_MM:
        plan_warning = (
            "\nCẢNH BÁO: Hai đường tim không giao nhau trong phạm vi đoạn ống; "
            "khoảng cách gần nhất trên mặt bằng là {:.2f} mm.".format(plan_distance_mm)
        )
    else:
        plan_warning = ""

    message = (
        "KẾT QUẢ KIỂM TRA KHE HỞ\n\n"
        "Trạng thái: {status}\n"
        "{status_detail}\n\n"
        "Khe hở thực tế: {actual:.2f} mm\n"
        "Khe hở yêu cầu: {required:.2f} mm\n"
        "Sai lệch thực tế - yêu cầu: {difference:+.2f} mm\n\n"
        "Phương pháp xác định vị trí: {method}\n"
        "Khoảng cách hai điểm tham chiếu trên mặt bằng: {plan_distance:.2f} mm\n\n"
        "Ống chuẩn ID {base_id}: OD = {base_od:.2f} mm, slope = {base_slope:+.4f}%\n"
        "Ống di chuyển ID {move_id}: OD = {move_od:.2f} mm, slope = {move_slope:+.4f}%"
        "{overlap}"
        "{plan_warning}"
    ).format(
        status=status,
        status_detail=status_detail,
        actual=actual_mm,
        required=required_mm,
        difference=difference_mm,
        method=get_reference_method_text(result["reference"]["method"]),
        plan_distance=plan_distance_mm,
        base_id=base_data["pipe"].Id.IntegerValue,
        move_id=move_data["pipe"].Id.IntegerValue,
        base_od=ft_to_mm(base_data["outer_diameter"]),
        move_od=ft_to_mm(move_data["outer_diameter"]),
        base_slope=base_data["slope_percent"],
        move_slope=move_data["slope_percent"],
        overlap=overlap_text,
        plan_warning=plan_warning,
    )

    return message


# =============================================================================
# MAIN
# =============================================================================

def pick_pipe(uidoc, prompt):
    reference = uidoc.Selection.PickObject(
        ObjectType.Element,
        PipeSelectionFilter(),
        prompt,
    )
    return revit.doc.GetElement(reference.ElementId)


def align_or_check_pipe(check_only=False):
    doc = revit.doc
    uidoc = revit.uidoc

    try:
        try:
            base_pipe = pick_pipe(uidoc, "Chọn ống GỐC làm chuẩn...")
            move_pipe = pick_pipe(uidoc, "Chọn ống CẦN DI CHUYỂN...")
        except OperationCanceledException:
            return

        if base_pipe.Id == move_pipe.Id:
            forms.alert(
                "Không thể chọn cùng một Pipe làm ống chuẩn và ống di chuyển.",
                title="Align Pipe AZN",
            )
            return

        base_data = get_pipe_data(base_pipe)
        move_data = get_pipe_data(move_pipe)
        result = calculate_clearance(base_data, move_data)

        # Shift + Click: chỉ kiểm tra, tuyệt đối không thay đổi mô hình.
        if check_only:
            forms.alert(
                build_check_message(base_data, move_data, result),
                title="Align Pipe AZN - Kiểm tra khe hở",
                warn_icon=(result["difference"] < -mm_to_ft(CLEARANCE_TOLERANCE_MM)),
            )
            return

        if move_pipe.Pinned:
            forms.alert(
                "Ống di chuyển đang bị Pin. Hãy Unpin trước khi chạy tool.",
                title="Align Pipe AZN",
                warn_icon=True,
            )
            return

        # Muốn actual_gap mới bằng required_gap:
        # translation_z = required_gap - actual_gap
        translation_z = result["required_gap"] - result["actual_gap"]

        move_tolerance_ft = mm_to_ft(CLEARANCE_TOLERANCE_MM)
        if abs(translation_z) <= move_tolerance_ft:
            forms.toast(
                "Ống đã đạt khe hở yêu cầu trong dung sai ±{:.1f} mm.".format(
                    CLEARANCE_TOLERANCE_MM
                )
            )
            return

        with revit.Transaction("Align Pipe AZN - Vertical Clearance"):
            ElementTransformUtils.MoveElement(
                doc,
                move_pipe.Id,
                XYZ(0.0, 0.0, translation_z),
            )

        # Tính lại sau khi di chuyển để báo kết quả thực tế.
        move_data_after = get_pipe_data(move_pipe)
        result_after = calculate_clearance(base_data, move_data_after)

        moved_mm = ft_to_mm(translation_z)
        final_gap_mm = ft_to_mm(result_after["actual_gap"])
        required_gap_mm = ft_to_mm(result_after["required_gap"])

        direction_text = "lên" if moved_mm > 0.0 else "xuống"

        forms.toast(
            "Đã di chuyển ống {direction} {distance:.2f} mm. "
            "Khe hở mới: {actual:.2f} mm; yêu cầu: {required:.2f} mm.".format(
                direction=direction_text,
                distance=abs(moved_mm),
                actual=final_gap_mm,
                required=required_gap_mm,
            )
        )

    except Exception as error:
        output.show()
        print("Align Pipe AZN - Chi tiết lỗi")
        print("{}".format(str(error)))
        print("\n{}".format(traceback.format_exc()))

        forms.alert(
            "Tool không thể hoàn thành. Xem cửa sổ pyRevit Output để biết chi tiết.",
            title="Align Pipe AZN",
            warn_icon=True,
        )


def is_shift_click_mode():
    """
    Phát hiện Shift+Click theo nhiều lớp, ưu tiên cơ chế chính thức của pyRevit.

    1. EXEC_PARAMS.config_mode: cơ chế được pyRevit khuyến nghị.
    2. __shiftclick__: tương thích các phiên bản pyRevit cũ.
    3. Keyboard.IsKeyDown: dự phòng khi command nằm trong pulldown/split button.

    Lưu ý: nếu bundle có config.py, Shift+Click sẽ chạy config.py thay vì script.py.
    """
    try:
        if bool(EXEC_PARAMS.config_mode):
            return True
    except Exception:
        pass

    try:
        if bool(globals().get("__shiftclick__", False)):
            return True
    except Exception:
        pass

    try:
        if Keyboard is not None and Key is not None:
            return (
                Keyboard.IsKeyDown(Key.LeftShift)
                or Keyboard.IsKeyDown(Key.RightShift)
            )
    except Exception:
        pass

    return False


if __name__ == "__main__":
    check_mode = is_shift_click_mode()

    # Thông báo ngắn để xác nhận pulldown đã nhận đúng Shift+Click.
    if check_mode:
        forms.toast("Chế độ KIỂM TRA khe hở - mô hình sẽ không bị thay đổi.")

    align_or_check_pipe(check_only=check_mode)
