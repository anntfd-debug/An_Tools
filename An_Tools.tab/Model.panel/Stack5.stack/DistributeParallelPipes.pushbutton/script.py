# -*- coding: utf-8 -*-
from __future__ import division

import math

from pyrevit import revit, DB, UI, forms, script, EXEC_PARAMS
from Autodesk.Revit.Exceptions import OperationCanceledException


__title__ = "Distribute Pipes"
__author__ = "Nguyen Thien An"
__doc__ = u"""
Phân bố nhiều ống thẳng song song theo trục X hoặc Y.

Cách sử dụng:
1. Chạy lệnh.
2. Chọn nhiều ống song song.
3. Nhấn Finish để phân bố ống.

Nguyên tắc:
- Khoảng cách được tính từ tim ống đến tim ống.
- Trục X: giữ nguyên ống ngoài cùng bên trái.
- Trục Y: giữ nguyên ống thấp nhất.
- Lần chạy đầu tiên: yêu cầu nhập trục và khoảng cách.
- Shift + Click: thay đổi cài đặt.
- Click bình thường: sử dụng cài đặt đã lưu.
"""


doc = revit.doc
uidoc = revit.uidoc

# Dung sai kiểm tra các ống song song.
PARALLEL_ANGLE_TOLERANCE_DEG = 1.0

# Nếu trục phân bố gần song song với hướng chạy của ống thì dừng.
AXIS_PARALLEL_TOLERANCE_DEG = 5.0

# Dung sai vector di chuyển theo internal unit.
MOVE_TOLERANCE = 1.0e-9


# ============================================================
# SELECTION FILTER
# ============================================================

class PipeSelectionFilter(UI.Selection.ISelectionFilter):
    """Chỉ cho phép chọn Pipe trong model hiện hành."""

    def AllowElement(self, element):
        try:
            category = element.Category

            if category is None:
                return False

            is_pipe = (
                category.Id.IntegerValue
                == int(DB.BuiltInCategory.OST_PipeCurves)
            )

            return (
                is_pipe
                and isinstance(element.Location, DB.LocationCurve)
            )

        except Exception:
            return False

    def AllowReference(self, reference, position):
        return False


# ============================================================
# UNIT CONVERSION
# ============================================================

def mm_to_internal(value_mm):
    """
    Chuyển mm sang internal unit.

    Revit 2022 trở lên dùng UnitTypeId.
    Các phiên bản cũ hơn dùng DisplayUnitType.
    """

    try:
        return DB.UnitUtils.ConvertToInternalUnits(
            value_mm,
            DB.UnitTypeId.Millimeters
        )

    except AttributeError:
        return DB.UnitUtils.ConvertToInternalUnits(
            value_mm,
            DB.DisplayUnitType.DUT_MILLIMETERS
        )


# ============================================================
# CONFIGURATION
# ============================================================

def get_saved_settings():
    """
    Đọc cài đặt đã lưu.

    Returns:
        config
        axis
        spacing_mm
        has_valid_settings
    """

    config = script.get_config()

    try:
        axis = str(config.axis).upper()
    except Exception:
        axis = None

    try:
        spacing_text = str(config.spacing_mm).replace(",", ".")
        spacing_mm = float(spacing_text)
    except Exception:
        spacing_mm = None

    has_valid_settings = (
        axis in ("X", "Y")
        and spacing_mm is not None
        and spacing_mm > 0
    )

    return config, axis, spacing_mm, has_valid_settings


def ask_and_save_settings(
        config,
        current_axis=None,
        current_spacing_mm=None
):
    """Hiển thị giao diện nhập và lưu cài đặt."""

    axis_options = [
        u"Trục X",
        u"Trục Y"
    ]

    message = u"Chọn trục dùng để phân bố vị trí tim ống."

    if current_axis in ("X", "Y"):
        message += u"\n\nCài đặt hiện tại: trục {0}".format(
            current_axis
        )

    selected_axis = forms.CommandSwitchWindow.show(
        axis_options,
        message=message
    )

    if not selected_axis:
        script.exit()

    if selected_axis == u"Trục X":
        axis = "X"
    else:
        axis = "Y"

    if (
        current_spacing_mm is not None
        and current_spacing_mm > 0
    ):
        default_spacing = str(current_spacing_mm)
    else:
        default_spacing = "100"

    spacing_text = forms.ask_for_string(
        default=default_spacing,
        prompt=u"Nhập khoảng cách tim - tim giữa các ống (mm):",
        title=u"Cài đặt phân bố ống"
    )

    if spacing_text is None:
        script.exit()

    try:
        spacing_mm = float(
            spacing_text.strip().replace(",", ".")
        )

    except Exception:
        forms.alert(
            u"Khoảng cách nhập vào không hợp lệ.\n\n"
            u"Ví dụ:\n"
            u"100\n"
            u"150.5\n"
            u"200,5",
            title=u"Distribute Pipes",
            exitscript=True
        )

        return None, None

    if spacing_mm <= 0:
        forms.alert(
            u"Khoảng cách phải lớn hơn 0 mm.",
            title=u"Distribute Pipes",
            exitscript=True
        )

        return None, None

    # Lưu dưới dạng chuỗi để tương thích config parser.
    config.axis = axis
    config.spacing_mm = str(spacing_mm)

    script.save_config()

    return axis, spacing_mm


# ============================================================
# PIPE SELECTION
# ============================================================

def collect_selected_pipes():
    """Yêu cầu người dùng chọn nhiều ống rồi nhấn Finish."""

    try:
        with forms.WarningBar(
            title=u"Chọn nhiều ống song song, sau đó nhấn Finish"
        ):
            references = uidoc.Selection.PickObjects(
                UI.Selection.ObjectType.Element,
                PipeSelectionFilter(),
                u"Chọn nhiều ống song song rồi nhấn Finish"
            )

    except OperationCanceledException:
        script.exit()
        return []

    pipes = []
    used_ids = set()

    for reference in references:
        element = doc.GetElement(reference.ElementId)

        if element is None:
            continue

        element_id = element.Id.IntegerValue

        # Tránh trường hợp chọn trùng một ống.
        if element_id in used_ids:
            continue

        used_ids.add(element_id)
        pipes.append(element)

    return pipes


# ============================================================
# GEOMETRY
# ============================================================

def get_pipe_geometry(pipe):
    """
    Lấy đường tim, phương và trung điểm của ống.

    Tool chỉ xử lý Pipe có LocationCurve dạng Line.
    """

    location = pipe.Location

    if not isinstance(location, DB.LocationCurve):
        return None

    curve = location.Curve

    if not isinstance(curve, DB.Line):
        return None

    start_point = curve.GetEndPoint(0)
    end_point = curve.GetEndPoint(1)

    midpoint = DB.XYZ(
        (start_point.X + end_point.X) * 0.5,
        (start_point.Y + end_point.Y) * 0.5,
        (start_point.Z + end_point.Z) * 0.5
    )

    return {
        "pipe": pipe,
        "curve": curve,
        "direction": curve.Direction.Normalize(),
        "midpoint": midpoint
    }


def validate_pipes(pipes, move_axis):
    """Kiểm tra số lượng, hình học, pin, group và phương ống."""

    if len(pipes) < 2:
        forms.alert(
            u"Cần chọn ít nhất 2 ống.",
            title=u"Distribute Pipes",
            exitscript=True
        )

    pipe_data = []

    invalid_curve_ids = []
    pinned_ids = []
    grouped_ids = []

    for pipe in pipes:
        data = get_pipe_geometry(pipe)

        if data is None:
            invalid_curve_ids.append(
                str(pipe.Id.IntegerValue)
            )
            continue

        if pipe.Pinned:
            pinned_ids.append(
                str(pipe.Id.IntegerValue)
            )

        try:
            if pipe.GroupId != DB.ElementId.InvalidElementId:
                grouped_ids.append(
                    str(pipe.Id.IntegerValue)
                )
        except Exception:
            pass

        pipe_data.append(data)

    if invalid_curve_ids:
        forms.alert(
            u"Tool chỉ xử lý các đoạn ống thẳng.\n\n"
            u"Pipe Id không hợp lệ:\n{0}".format(
                ", ".join(invalid_curve_ids)
            ),
            title=u"Distribute Pipes",
            exitscript=True
        )

    if pinned_ids:
        forms.alert(
            u"Có ống đang bị Pin.\n"
            u"Hãy Unpin các ống trước khi chạy.\n\n"
            u"Pipe Id:\n{0}".format(
                ", ".join(pinned_ids)
            ),
            title=u"Distribute Pipes",
            exitscript=True
        )

    if grouped_ids:
        forms.alert(
            u"Không thể di chuyển riêng các ống đang nằm trong Group.\n\n"
            u"Pipe Id:\n{0}".format(
                ", ".join(grouped_ids)
            ),
            title=u"Distribute Pipes",
            exitscript=True
        )

    # --------------------------------------------------------
    # Kiểm tra các ống song song
    # --------------------------------------------------------

    base_direction = pipe_data[0]["direction"]

    parallel_limit = math.cos(
        math.radians(PARALLEL_ANGLE_TOLERANCE_DEG)
    )

    non_parallel_ids = []

    for data in pipe_data[1:]:
        direction = data["direction"]

        # Dùng trị tuyệt đối vì hai đường song song có thể ngược hướng.
        dot_value = abs(
            base_direction.DotProduct(direction)
        )

        if dot_value < parallel_limit:
            non_parallel_ids.append(
                str(data["pipe"].Id.IntegerValue)
            )

    if non_parallel_ids:
        forms.alert(
            u"Các ống được chọn không song song với nhau.\n"
            u"Dung sai hiện tại: {0} độ.\n\n"
            u"Pipe Id không song song:\n{1}".format(
                PARALLEL_ANGLE_TOLERANCE_DEG,
                ", ".join(non_parallel_ids)
            ),
            title=u"Distribute Pipes",
            exitscript=True
        )

    # --------------------------------------------------------
    # Kiểm tra trục phân bố
    # --------------------------------------------------------

    axis_parallel_limit = math.cos(
        math.radians(AXIS_PARALLEL_TOLERANCE_DEG)
    )

    axis_dot = abs(
        base_direction.DotProduct(move_axis)
    )

    if axis_dot > axis_parallel_limit:
        forms.alert(
            u"Trục phân bố đang gần song song với hướng chạy của ống.\n\n"
            u"Hãy chọn trục vuông góc với hướng ống:\n\n"
            u"• Ống chạy theo trục Y → phân bố theo trục X.\n"
            u"• Ống chạy theo trục X → phân bố theo trục Y.",
            title=u"Distribute Pipes",
            exitscript=True
        )

    return pipe_data


def get_axis_coordinate(point, axis):
    """Lấy tọa độ X hoặc Y từ XYZ."""

    if axis == "X":
        return point.X

    return point.Y


# ============================================================
# DISTRIBUTE
# ============================================================

def distribute_pipes(pipe_data, axis, spacing_internal):
    """
    Phân bố ống theo tọa độ X hoặc Y.

    Trục X:
        Giữ ống có tọa độ X nhỏ nhất.

    Trục Y:
        Giữ ống có tọa độ Y nhỏ nhất.
    """

    if axis == "X":
        move_axis = DB.XYZ.BasisX
    else:
        move_axis = DB.XYZ.BasisY

    # Sắp xếp từ tọa độ nhỏ đến lớn.
    sorted_data = sorted(
        pipe_data,
        key=lambda item: get_axis_coordinate(
            item["midpoint"],
            axis
        )
    )

    # Ống đầu tiên là ống neo, không di chuyển.
    anchor_coordinate = get_axis_coordinate(
        sorted_data[0]["midpoint"],
        axis
    )

    transaction = DB.Transaction(
        doc,
        "Distribute Parallel Pipes"
    )

    moved_count = 0

    try:
        transaction.Start()

        for index, data in enumerate(sorted_data):

            current_coordinate = get_axis_coordinate(
                data["midpoint"],
                axis
            )

            target_coordinate = (
                anchor_coordinate
                + index * spacing_internal
            )

            delta = target_coordinate - current_coordinate

            if abs(delta) <= MOVE_TOLERANCE:
                continue

            movement_vector = move_axis.Multiply(delta)

            DB.ElementTransformUtils.MoveElement(
                doc,
                data["pipe"].Id,
                movement_vector
            )

            moved_count += 1

        status = transaction.Commit()

        if status != DB.TransactionStatus.Committed:
            raise Exception(
                "Transaction was not committed."
            )

    except Exception as error:

        if (
            transaction.GetStatus()
            == DB.TransactionStatus.Started
        ):
            transaction.RollBack()

        forms.alert(
            u"Không thể phân bố các ống.\n\n"
            u"Nguyên nhân có thể:\n"
            u"• Ống đang kết nối với fitting hoặc thiết bị.\n"
            u"• Ống đang bị ràng buộc bằng dimension.\n"
            u"• Ống thuộc hệ thống không cho phép di chuyển.\n"
            u"• Không có quyền chỉnh sửa Worksharing.\n\n"
            u"Chi tiết lỗi:\n{0}".format(error),
            title=u"Distribute Pipes",
            exitscript=True
        )

    return moved_count


# ============================================================
# MAIN
# ============================================================

def main():

    config, axis, spacing_mm, has_settings = (
        get_saved_settings()
    )

    # Hiện giao diện khi:
    # 1. Chưa từng lưu cài đặt.
    # 2. Người dùng Shift + Click.
    need_settings = (
        EXEC_PARAMS.config_mode
        or not has_settings
    )

    if need_settings:

        axis, spacing_mm = ask_and_save_settings(
            config,
            current_axis=axis,
            current_spacing_mm=spacing_mm
        )

        # Shift + Click chỉ cập nhật cấu hình,
        # không thực hiện chọn và di chuyển ống.
        if EXEC_PARAMS.config_mode:

            forms.alert(
                u"Đã lưu cài đặt:\n\n"
                u"Trục phân bố: {0}\n"
                u"Khoảng cách tim - tim: {1:g} mm".format(
                    axis,
                    spacing_mm
                ),
                title=u"Distribute Pipes"
            )

            script.exit()

    # Click bình thường hoặc lần chạy đầu tiên.
    pipes = collect_selected_pipes()

    if axis == "X":
        move_axis = DB.XYZ.BasisX
    else:
        move_axis = DB.XYZ.BasisY

    pipe_data = validate_pipes(
        pipes,
        move_axis
    )

    spacing_internal = mm_to_internal(
        spacing_mm
    )

    moved_count = distribute_pipes(
        pipe_data,
        axis,
        spacing_internal
    )

    if axis == "X":
        anchor_description = u"ống ngoài cùng bên trái"
    else:
        anchor_description = u"ống thấp nhất"


if __name__ == "__main__":
    main()