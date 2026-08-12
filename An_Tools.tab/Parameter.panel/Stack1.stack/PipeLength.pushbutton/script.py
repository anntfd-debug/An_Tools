# -*- coding: utf-8 -*-

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException


doc = revit.doc
uidoc = revit.uidoc


# ============================================================
# CHUYỂN ĐỔI ĐƠN VỊ
# Tương thích Revit 2019 - 2025
# ============================================================

def internal_to_mm(value):
    """Chuyển feet nội bộ của Revit sang mm."""
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(
            value,
            DB.UnitTypeId.Millimeters
        )
    except AttributeError:
        return DB.UnitUtils.ConvertFromInternalUnits(
            value,
            DB.DisplayUnitType.DUT_MILLIMETERS
        )


def internal_to_meter(value):
    """Chuyển feet nội bộ của Revit sang mét."""
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(
            value,
            DB.UnitTypeId.Meters
        )
    except AttributeError:
        return DB.UnitUtils.ConvertFromInternalUnits(
            value,
            DB.DisplayUnitType.DUT_METERS
        )


# ============================================================
# LẤY CÁC PHẦN TỬ ĐƯỢC CHỌN
# ============================================================

def get_selected_elements():
    """
    Nếu người dùng đã chọn phần tử trước khi chạy lệnh,
    sử dụng selection hiện tại.

    Nếu chưa chọn phần tử, yêu cầu người dùng quét chọn.
    """

    selected_ids = list(uidoc.Selection.GetElementIds())

    if selected_ids:
        elements = []

        for element_id in selected_ids:
            element = doc.GetElement(element_id)

            if element is not None:
                elements.append(element)

        return elements

    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element,
            "Chọn các ống cần kiểm tra. "
            "Pipe Fitting và Pipe Accessory sẽ tự động được bỏ qua."
        )

        elements = []

        for reference in references:
            element = doc.GetElement(reference.ElementId)

            if element is not None:
                elements.append(element)

        return elements

    except OperationCanceledException:
        return []


# ============================================================
# KIỂM TRA PIPE
# ============================================================

def is_pipe(element):
    """
    Chỉ chấp nhận đối tượng Pipe.
    Pipe Fitting, Pipe Accessory và các category khác sẽ bị bỏ qua.
    """
    return isinstance(element, DB.Plumbing.Pipe)


# ============================================================
# LẤY ĐƯỜNG KÍNH PIPE
# ============================================================

def get_pipe_diameter(pipe):
    """
    Trả về đường kính ống theo đơn vị nội bộ của Revit.
    """

    parameter = pipe.get_Parameter(
        DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM
    )

    if parameter is not None and parameter.HasValue:
        return parameter.AsDouble()

    return None


# ============================================================
# LẤY CHIỀU DÀI PIPE
# ============================================================

def get_pipe_length(pipe):
    """
    Trả về chiều dài pipe theo đơn vị nội bộ của Revit.
    Ưu tiên parameter Length, sau đó dùng LocationCurve.
    """

    parameter = pipe.get_Parameter(
        DB.BuiltInParameter.CURVE_ELEM_LENGTH
    )

    if parameter is not None and parameter.HasValue:
        return parameter.AsDouble()

    location = pipe.Location

    if isinstance(location, DB.LocationCurve):
        curve = location.Curve

        if curve is not None:
            return curve.Length

    return 0.0


# ============================================================
# ĐỊNH DẠNG SIZE
# ============================================================

def format_number(value, decimal_places=2):
    """
    Xóa các số 0 không cần thiết phía sau phần thập phân.

    Ví dụ:
        25.00 -> 25
        21.30 -> 21.3
    """

    text = ("{0:." + str(decimal_places) + "f}").format(value)
    return text.rstrip("0").rstrip(".")


def format_pipe_size(diameter_mm):
    return u"Ø{0} mm".format(
        format_number(diameter_mm, 2)
    )


# ============================================================
# CHƯƠNG TRÌNH CHÍNH
# ============================================================

elements = get_selected_elements()

if not elements:
    forms.alert(
        u"Không có phần tử nào được chọn.",
        title=u"Tổng chiều dài ống",
        warn_icon=False
    )
    script.exit()


pipe_data = {}

total_pipe_count = 0
total_length_internal = 0.0
ignored_count = 0
invalid_pipe_count = 0


for element in elements:

    # Bỏ qua Pipe Fitting, Pipe Accessory và các phần tử khác
    if not is_pipe(element):
        ignored_count += 1
        continue

    diameter_internal = get_pipe_diameter(element)
    length_internal = get_pipe_length(element)

    if diameter_internal is None or length_internal <= 0:
        invalid_pipe_count += 1
        continue

    diameter_mm = internal_to_mm(diameter_internal)

    # Làm tròn để tránh sai số số thực khi gom nhóm
    size_key = round(diameter_mm, 3)

    if size_key not in pipe_data:
        pipe_data[size_key] = {
            "count": 0,
            "length": 0.0
        }

    pipe_data[size_key]["count"] += 1
    pipe_data[size_key]["length"] += length_internal

    total_pipe_count += 1
    total_length_internal += length_internal


# ============================================================
# KIỂM TRA KẾT QUẢ
# ============================================================

if not pipe_data:
    message = (
        u"Không tìm thấy Pipe hợp lệ trong các phần tử được chọn."
        u"\n\nPipe Fitting, Pipe Accessory và các phần tử khác "
        u"không được tính."
    )

    if ignored_count > 0:
        message += u"\n\nSố phần tử đã bỏ qua: {0}".format(
            ignored_count
        )

    forms.alert(
        message,
        title=u"Tổng chiều dài ống",
        warn_icon=True
    )

    script.exit()


# ============================================================
# TẠO NỘI DUNG THÔNG BÁO
# ============================================================

message_lines = []

message_lines.append(u"TỔNG CHIỀU DÀI ỐNG THEO SIZE")
message_lines.append(u"")

sorted_sizes = sorted(pipe_data.keys())


for size_key in sorted_sizes:
    data = pipe_data[size_key]

    pipe_count = data["count"]
    length_meter = internal_to_meter(data["length"])

    line = u"{0}:  {1} ống  |  {2:.3f} m".format(
        format_pipe_size(size_key),
        pipe_count,
        length_meter
    )

    message_lines.append(line)


message_lines.append(u"")
message_lines.append(u"----------------------------------------")
message_lines.append(
    u"Tổng số lượng Pipe: {0} ống".format(total_pipe_count)
)
message_lines.append(
    u"Tổng chiều dài: {0:.3f} m".format(
        internal_to_meter(total_length_internal)
    )
)


if ignored_count > 0:
    message_lines.append(
        u"Phần tử không phải Pipe đã bỏ qua: {0}".format(
            ignored_count
        )
    )


if invalid_pipe_count > 0:
    message_lines.append(
        u"Pipe không đọc được chiều dài/size: {0}".format(
            invalid_pipe_count
        )
    )


result_message = u"\n".join(message_lines)


# ============================================================
# HIỂN THỊ THÔNG BÁO
# ============================================================

forms.alert(
    result_message,
    title=u"Thống kê chiều dài Pipe",
    warn_icon=False
)