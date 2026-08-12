# -*- coding: utf-8 -*-
"""
pyRevit - Replace one 90-degree pipe elbow with two 45-degree elbows.

Workflow
--------
1. Select one existing pipe elbow connected directly to exactly two pipes.
2. The tool calculates the theoretical intersection of the two pipe
   centerlines in full 3D.
3. Both pipes are trimmed by the same setback distance from that intersection.
4. A diagonal pipe is created between the two trimmed endpoints.
5. Revit creates one elbow at each end of the diagonal pipe.

The calculation does not project geometry to XY, so it supports:
- two horizontal pipes, including sloped pipes;
- one vertical pipe and one horizontal/sloped pipe;
- any orientation where the two original centerlines form a 90-degree turn.

Prerequisite
------------
The selected Pipe Type must have a suitable elbow in Routing Preferences.
The elbow family must support a 45-degree connection (fixed 45 or adjustable).
"""

import math
import traceback

from pyrevit import revit, DB, forms, script, EXEC_PARAMS
from Autodesk.Revit.DB.Plumbing import Pipe
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException


__title__ = "90 to 2x45"
__author__ = "OpenAI"
__doc__ = "Replace one 90-degree pipe elbow with two 45-degree elbows using full 3D geometry."


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
config = script.get_config()


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------
ANGLE_TOLERANCE_DEG = 3.0
CENTERLINE_GAP_TOLERANCE_MM = 1.0
CONNECTOR_ENDPOINT_TOLERANCE_MM = 5.0
MIN_REMAINING_PIPE_LENGTH_MM = 25.0
MIN_DIAGONAL_LENGTH_MM = 25.0


# -----------------------------------------------------------------------------
# Compatibility / units
# -----------------------------------------------------------------------------
def element_id_value(element_id):
    """Return ElementId numeric value for both old and new Revit APIs."""
    try:
        return element_id.Value
    except Exception:
        return element_id.IntegerValue


def is_invalid_id(element_id):
    if element_id is None:
        return True
    return element_id_value(element_id) == element_id_value(DB.ElementId.InvalidElementId)


def mm_to_internal(value_mm):
    try:
        return DB.UnitUtils.ConvertToInternalUnits(
            float(value_mm), DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertToInternalUnits(
            float(value_mm), DB.DisplayUnitType.DUT_MILLIMETERS
        )


def internal_to_mm(value_internal):
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(
            float(value_internal), DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertFromInternalUnits(
            float(value_internal), DB.DisplayUnitType.DUT_MILLIMETERS
        )


CENTERLINE_GAP_TOLERANCE = mm_to_internal(CENTERLINE_GAP_TOLERANCE_MM)
CONNECTOR_ENDPOINT_TOLERANCE = mm_to_internal(CONNECTOR_ENDPOINT_TOLERANCE_MM)
MIN_REMAINING_PIPE_LENGTH = mm_to_internal(MIN_REMAINING_PIPE_LENGTH_MM)
MIN_DIAGONAL_LENGTH = mm_to_internal(MIN_DIAGONAL_LENGTH_MM)


# -----------------------------------------------------------------------------
# Selection
# -----------------------------------------------------------------------------
class PipeFittingSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        try:
            if element.Category is None:
                return False
            category_value = element_id_value(element.Category.Id)
            return category_value == int(DB.BuiltInCategory.OST_PipeFitting)
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


def is_pipe_fitting(element):
    try:
        return (
            element is not None
            and element.Category is not None
            and element_id_value(element.Category.Id)
            == int(DB.BuiltInCategory.OST_PipeFitting)
        )
    except Exception:
        return False


def get_selected_or_pick_fitting():
    preselected = []
    try:
        for element_id in uidoc.Selection.GetElementIds():
            element = doc.GetElement(element_id)
            if is_pipe_fitting(element):
                preselected.append(element)
    except Exception:
        preselected = []

    if len(preselected) == 1:
        return preselected[0]

    reference = uidoc.Selection.PickObject(
        ObjectType.Element,
        PipeFittingSelectionFilter(),
        "Chọn elbow 90 đang kết nối trực tiếp hai ống"
    )
    return doc.GetElement(reference.ElementId)


# -----------------------------------------------------------------------------
# Connector helpers
# -----------------------------------------------------------------------------
def iter_connectors(connector_manager):
    if connector_manager is None:
        return []
    return [connector for connector in connector_manager.Connectors]


def get_fitting_connector_manager(fitting):
    try:
        if fitting.MEPModel is not None:
            return fitting.MEPModel.ConnectorManager
    except Exception:
        pass
    return None


def get_two_connected_pipes(fitting):
    """
    Return two dictionaries containing:
      fitting_connector, pipe, pipe_connector
    """
    manager = get_fitting_connector_manager(fitting)
    if manager is None:
        raise Exception("Family được chọn không có ConnectorManager MEP.")

    found = []
    used_pipe_ids = set()

    for fitting_connector in iter_connectors(manager):
        try:
            if fitting_connector.ConnectorType != DB.ConnectorType.End:
                continue
            if not fitting_connector.IsConnected:
                continue
        except Exception:
            continue

        for referenced_connector in fitting_connector.AllRefs:
            try:
                owner = referenced_connector.Owner
                if owner is None or owner.Id == fitting.Id:
                    continue
                if not isinstance(owner, Pipe):
                    continue
                if referenced_connector.ConnectorType != DB.ConnectorType.End:
                    continue

                pipe_key = element_id_value(owner.Id)
                if pipe_key in used_pipe_ids:
                    continue

                found.append({
                    "fitting_connector": fitting_connector,
                    "pipe": owner,
                    "pipe_connector": referenced_connector,
                })
                used_pipe_ids.add(pipe_key)
                break
            except Exception:
                continue

    if len(found) != 2:
        raise Exception(
            "Fitting phải kết nối trực tiếp với đúng 2 ống. "
            "Hiện tại tìm thấy {} ống.".format(len(found))
        )

    return found


def get_end_connector_near(pipe, point, maximum_distance=None):
    candidates = []
    for connector in iter_connectors(pipe.ConnectorManager):
        try:
            if connector.ConnectorType == DB.ConnectorType.End:
                distance = connector.Origin.DistanceTo(point)
                candidates.append((distance, connector))
        except Exception:
            continue

    if not candidates:
        raise Exception("Không tìm thấy end connector của Pipe ID {}.".format(pipe.Id))

    candidates.sort(key=lambda item: item[0])
    nearest_distance, nearest_connector = candidates[0]

    if maximum_distance is not None and nearest_distance > maximum_distance:
        raise Exception(
            "Không tìm thấy connector đủ gần điểm cần nối trên Pipe ID {}. "
            "Sai lệch: {:.2f} mm.".format(
                pipe.Id, internal_to_mm(nearest_distance)
            )
        )

    return nearest_connector


# -----------------------------------------------------------------------------
# Pipe and geometry helpers
# -----------------------------------------------------------------------------
def get_pipe_diameter(pipe):
    parameter = pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    if parameter is None:
        raise Exception("Không đọc được đường kính Pipe ID {}.".format(pipe.Id))
    return parameter.AsDouble()


def get_pipe_system_type_id(pipe):
    try:
        if pipe.MEPSystem is not None:
            system_type_id = pipe.MEPSystem.GetTypeId()
            if not is_invalid_id(system_type_id):
                return system_type_id
    except Exception:
        pass

    try:
        parameter = pipe.get_Parameter(
            DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM
        )
        if parameter is not None:
            system_type_id = parameter.AsElementId()
            if not is_invalid_id(system_type_id):
                return system_type_id
    except Exception:
        pass

    raise Exception(
        "Pipe ID {} chưa có Piping System Type hợp lệ.".format(pipe.Id)
    )


def get_pipe_level_id(pipe):
    try:
        if pipe.ReferenceLevel is not None:
            return pipe.ReferenceLevel.Id
    except Exception:
        pass

    fallback_parameters = []
    for parameter_name in (
        "RBS_START_LEVEL_PARAM",
        "RBS_REFERENCE_LEVEL_PARAM",
    ):
        try:
            fallback_parameters.append(
                getattr(DB.BuiltInParameter, parameter_name)
            )
        except Exception:
            pass

    for built_in_parameter in fallback_parameters:
        try:
            parameter = pipe.get_Parameter(built_in_parameter)
            if parameter is not None:
                level_id = parameter.AsElementId()
                if not is_invalid_id(level_id):
                    return level_id
        except Exception:
            pass

    raise Exception("Không đọc được Reference Level của Pipe ID {}.".format(pipe.Id))


def get_pipe_end_data(pipe, connected_connector):
    location = pipe.Location
    if location is None or not isinstance(location, DB.LocationCurve):
        raise Exception("Pipe ID {} không có LocationCurve.".format(pipe.Id))

    curve = location.Curve
    if curve is None or not isinstance(curve, DB.Line):
        raise Exception("Tool chỉ hỗ trợ ống thẳng. Pipe ID {} không phải Line.".format(pipe.Id))

    point_0 = curve.GetEndPoint(0)
    point_1 = curve.GetEndPoint(1)
    connector_point = connected_connector.Origin

    distance_0 = connector_point.DistanceTo(point_0)
    distance_1 = connector_point.DistanceTo(point_1)

    if min(distance_0, distance_1) > CONNECTOR_ENDPOINT_TOLERANCE:
        raise Exception(
            "Connector của Pipe ID {} không trùng đầu ống. Sai lệch nhỏ nhất {:.2f} mm.".format(
                pipe.Id, internal_to_mm(min(distance_0, distance_1))
            )
        )

    if distance_0 <= distance_1:
        connected_index = 0
        near_point = point_0
        far_point = point_1
    else:
        connected_index = 1
        near_point = point_1
        far_point = point_0

    vector = far_point - near_point
    if vector.GetLength() < MIN_REMAINING_PIPE_LENGTH:
        raise Exception("Pipe ID {} quá ngắn để xử lý.".format(pipe.Id))

    return {
        "pipe": pipe,
        "connected_index": connected_index,
        "near": near_point,
        "far": far_point,
        "outward": vector.Normalize(),
    }


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def closest_points_on_infinite_lines(point_1, direction_1, point_2, direction_2):
    """Return closest points on two infinite 3D lines."""
    dot_12 = direction_1.DotProduct(direction_2)
    denominator = 1.0 - dot_12 * dot_12

    if abs(denominator) < 1.0e-9:
        raise Exception("Hai đường tâm gần song song, không thể là elbow 90.")

    delta = point_1 - point_2
    d = direction_1.DotProduct(delta)
    e = direction_2.DotProduct(delta)

    parameter_1 = (dot_12 * e - d) / denominator
    parameter_2 = (e - dot_12 * d) / denominator

    closest_1 = point_1 + direction_1.Multiply(parameter_1)
    closest_2 = point_2 + direction_2.Multiply(parameter_2)
    return closest_1, closest_2


def midpoint(point_1, point_2):
    return (point_1 + point_2).Multiply(0.5)


def analyze_corner(pipe_data_1, pipe_data_2):
    direction_1 = pipe_data_1["outward"]
    direction_2 = pipe_data_2["outward"]

    closest_1, closest_2 = closest_points_on_infinite_lines(
        pipe_data_1["near"], direction_1,
        pipe_data_2["near"], direction_2
    )

    centerline_gap = closest_1.DistanceTo(closest_2)
    if centerline_gap > CENTERLINE_GAP_TOLERANCE:
        raise Exception(
            "Hai đường tâm là hai đường chéo nhau trong không gian, không giao nhau. "
            "Khoảng cách gần nhất: {:.2f} mm (giới hạn {:.2f} mm).".format(
                internal_to_mm(centerline_gap), CENTERLINE_GAP_TOLERANCE_MM
            )
        )

    intersection = midpoint(closest_1, closest_2)

    dot_value = clamp(direction_1.DotProduct(direction_2), -1.0, 1.0)
    ray_angle_rad = math.acos(dot_value)
    turn_angle_rad = math.pi - ray_angle_rad
    turn_angle_deg = math.degrees(turn_angle_rad)

    if abs(turn_angle_deg - 90.0) > ANGLE_TOLERANCE_DEG:
        raise Exception(
            "Góc đổi hướng 3D hiện tại là {:.3f}°, không nằm trong khoảng "
            "90° ± {:.1f}°. Tool chỉ chuyển elbow 90 thành hai góc xấp xỉ 45°.".format(
                turn_angle_deg, ANGLE_TOLERANCE_DEG
            )
        )

    # Use the closest point belonging to each pipe line. When the model has a
    # tiny numerical skew, this keeps each trimmed endpoint exactly on its
    # original 3D centerline.
    old_setback_1 = (pipe_data_1["near"] - closest_1).DotProduct(direction_1)
    old_setback_2 = (pipe_data_2["near"] - closest_2).DotProduct(direction_2)

    if old_setback_1 < -CENTERLINE_GAP_TOLERANCE or old_setback_2 < -CENTERLINE_GAP_TOLERANCE:
        raise Exception(
            "Không xác định đúng phía bên ngoài của elbow. "
            "Hãy kiểm tra hình học hoặc connector của family elbow."
        )

    available_1 = (pipe_data_1["far"] - closest_1).DotProduct(direction_1)
    available_2 = (pipe_data_2["far"] - closest_2).DotProduct(direction_2)

    return {
        "intersection": intersection,
        "intersection_1": closest_1,
        "intersection_2": closest_2,
        "turn_angle_deg": turn_angle_deg,
        "new_angle_deg": turn_angle_deg * 0.5,
        "centerline_gap": centerline_gap,
        "old_setback_1": max(0.0, old_setback_1),
        "old_setback_2": max(0.0, old_setback_2),
        "available_1": available_1,
        "available_2": available_2,
    }


def set_pipe_connected_end(pipe_data, new_point):
    pipe = pipe_data["pipe"]
    curve = pipe.Location.Curve
    point_0 = curve.GetEndPoint(0)
    point_1 = curve.GetEndPoint(1)

    if pipe_data["connected_index"] == 0:
        new_curve = DB.Line.CreateBound(new_point, point_1)
    else:
        new_curve = DB.Line.CreateBound(point_0, new_point)

    pipe.Location.Curve = new_curve


# -----------------------------------------------------------------------------
# Validation and UI
# -----------------------------------------------------------------------------
def validate_editable(element, label):
    if element.Pinned:
        raise Exception("{} ID {} đang bị Pin.".format(label, element.Id))

    try:
        if not is_invalid_id(element.GroupId):
            raise Exception("{} ID {} đang nằm trong Group.".format(label, element.Id))
    except AttributeError:
        pass


def is_shift_click():
    """Detect Shift+Click in pyRevit, including buttons inside a pulldown."""
    try:
        if bool(EXEC_PARAMS.config_mode):
            return True
    except Exception:
        pass

    try:
        return bool(globals().get("__shiftclick__", False))
    except Exception:
        return False


def get_saved_setback_mm():
    try:
        value = float(config.setback_mm)
        if value > 0.0:
            return value
    except Exception:
        pass
    return None


def save_setback_mm(value_mm):
    config.setback_mm = float(value_mm)
    script.save_config()


def suggest_setback_mm(corner_data, diameter_internal):
    diameter_mm = internal_to_mm(diameter_internal)
    old_max_mm = internal_to_mm(
        max(corner_data["old_setback_1"], corner_data["old_setback_2"])
    )

    suggested_mm = max(300.0, old_max_mm + max(100.0, diameter_mm))
    return math.ceil(suggested_mm / 10.0) * 10.0


def ask_setback_mm(default_mm):
    prompt = (
        "Nhập khoảng lùi từ giao điểm lý thuyết đến đầu mỗi ống (mm).\n\n"
        "- Góc được tính trong không gian 3D, có xét độ dốc.\n"
        "- Giá trị lớn hơn sẽ tạo đoạn ống chéo dài hơn.\n"
        "- Giá trị này sẽ được lưu cho những lần Click tiếp theo."
    )

    default_text = "{:.3f}".format(default_mm).rstrip("0").rstrip(".")
    text = forms.ask_for_string(
        default=default_text,
        prompt=prompt,
        title="Cài đặt 90° → 2 elbow 45°"
    )

    if text is None:
        script.exit()

    try:
        value_mm = float(text.strip().replace(",", "."))
    except Exception:
        raise Exception("Khoảng lùi phải là một số hợp lệ.")

    if value_mm <= 0.0:
        raise Exception("Khoảng lùi phải lớn hơn 0 mm.")

    save_setback_mm(value_mm)
    return value_mm


def configure_setback_only():
    """Shift+Click configuration mode. Save the value and do not edit model."""
    saved_mm = get_saved_setback_mm()
    if saved_mm is None:
        saved_mm = 300.0
    ask_setback_mm(saved_mm)


def get_setback_mm(corner_data, diameter_internal):
    """
    Normal Click: reuse the last saved value without showing UI.
    First run: show UI once because no saved value exists yet.
    """
    saved_mm = get_saved_setback_mm()
    if saved_mm is not None:
        return saved_mm

    suggested_mm = suggest_setback_mm(corner_data, diameter_internal)
    return ask_setback_mm(suggested_mm)


def validate_compatibility(pipe_1, pipe_2, diameter_1, diameter_2):
    diameter_difference = abs(diameter_1 - diameter_2)
    diameter_tolerance = mm_to_internal(0.1)
    if diameter_difference > diameter_tolerance:
        raise Exception(
            "Hai ống khác đường kính: {:.2f} mm và {:.2f} mm. "
            "Hai elbow 45 trực tiếp không thể thay thế reducer.".format(
                internal_to_mm(diameter_1), internal_to_mm(diameter_2)
            )
        )

    system_1 = get_pipe_system_type_id(pipe_1)
    system_2 = get_pipe_system_type_id(pipe_2)
    if element_id_value(system_1) != element_id_value(system_2):
        raise Exception("Hai ống không cùng Piping System Type.")

    return system_1


# -----------------------------------------------------------------------------
# Main operation
# -----------------------------------------------------------------------------
def main():
    # Shift+Click is configuration-only: save the length and exit silently.
    if is_shift_click():
        configure_setback_only()
        return

    fitting = get_selected_or_pick_fitting()
    validate_editable(fitting, "Elbow")

    connected = get_two_connected_pipes(fitting)
    pipe_1 = connected[0]["pipe"]
    pipe_2 = connected[1]["pipe"]

    validate_editable(pipe_1, "Pipe")
    validate_editable(pipe_2, "Pipe")

    diameter_1 = get_pipe_diameter(pipe_1)
    diameter_2 = get_pipe_diameter(pipe_2)
    system_type_id = validate_compatibility(
        pipe_1, pipe_2, diameter_1, diameter_2
    )

    pipe_data_1 = get_pipe_end_data(pipe_1, connected[0]["pipe_connector"])
    pipe_data_2 = get_pipe_end_data(pipe_2, connected[1]["pipe_connector"])
    corner_data = analyze_corner(pipe_data_1, pipe_data_2)

    setback_mm = get_setback_mm(corner_data, diameter_1)
    setback = mm_to_internal(setback_mm)

    if corner_data["available_1"] - setback < MIN_REMAINING_PIPE_LENGTH:
        raise Exception(
            "Pipe ID {} không đủ chiều dài sau khi lùi {:.1f} mm.".format(
                pipe_1.Id, setback_mm
            )
        )

    if corner_data["available_2"] - setback < MIN_REMAINING_PIPE_LENGTH:
        raise Exception(
            "Pipe ID {} không đủ chiều dài sau khi lùi {:.1f} mm.".format(
                pipe_2.Id, setback_mm
            )
        )

    trim_point_1 = (
        corner_data["intersection_1"]
        + pipe_data_1["outward"].Multiply(setback)
    )
    trim_point_2 = (
        corner_data["intersection_2"]
        + pipe_data_2["outward"].Multiply(setback)
    )

    diagonal_length = trim_point_1.DistanceTo(trim_point_2)
    if diagonal_length < MIN_DIAGONAL_LENGTH:
        raise Exception(
            "Đoạn ống chéo chỉ dài {:.2f} mm, quá ngắn để tạo fitting.".format(
                internal_to_mm(diagonal_length)
            )
        )

    pipe_type_id = pipe_1.GetTypeId()
    level_id = get_pipe_level_id(pipe_1)

    transaction = DB.Transaction(doc, "Replace 90 elbow with two 45 elbows")
    transaction.Start()

    try:
        # Remove the existing elbow first, leaving both original pipe ends free.
        doc.Delete(fitting.Id)
        doc.Regenerate()

        # Trim the two original pipes in their own 3D centerline directions.
        set_pipe_connected_end(pipe_data_1, trim_point_1)
        set_pipe_connected_end(pipe_data_2, trim_point_2)
        doc.Regenerate()

        # Create the diagonal pipe. Its Z values come directly from the 3D points,
        # therefore its slope is created geometrically rather than copied as 2D data.
        middle_pipe = Pipe.Create(
            doc,
            system_type_id,
            pipe_type_id,
            level_id,
            trim_point_1,
            trim_point_2
        )

        middle_diameter_parameter = middle_pipe.get_Parameter(
            DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM
        )
        if middle_diameter_parameter is None or middle_diameter_parameter.IsReadOnly:
            raise Exception("Không thể gán đường kính cho đoạn ống chéo mới.")
        middle_diameter_parameter.Set(diameter_1)
        doc.Regenerate()

        # First 45 elbow.
        connector_pipe_1 = get_end_connector_near(
            pipe_1, trim_point_1, mm_to_internal(20.0)
        )
        connector_middle_1 = get_end_connector_near(
            middle_pipe, trim_point_1, mm_to_internal(20.0)
        )
        elbow_1 = doc.Create.NewElbowFitting(
            connector_pipe_1, connector_middle_1
        )
        doc.Regenerate()

        # Re-read connectors because creation of the first fitting can trim curves.
        connector_pipe_2 = get_end_connector_near(
            pipe_2, trim_point_2, mm_to_internal(50.0)
        )
        connector_middle_2 = get_end_connector_near(
            middle_pipe, trim_point_2, mm_to_internal(50.0)
        )
        elbow_2 = doc.Create.NewElbowFitting(
            connector_middle_2, connector_pipe_2
        )
        doc.Regenerate()

        if elbow_1 is None or elbow_2 is None:
            raise Exception("Revit không trả về đủ hai elbow mới.")

        commit_status = transaction.Commit()
        if commit_status != DB.TransactionStatus.Committed:
            raise Exception(
                "Transaction không Commit thành công. Trạng thái: {}".format(
                    commit_status
                )
            )

    except Exception:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        raise



try:
    main()
except OperationCanceledException:
    pass
except SystemExit:
    pass
except Exception as error:
    output.print_md("## 90° → 2 elbow 45° - Chi tiết lỗi")
    output.print_md("**{}**".format(str(error)))
    output.print_code(traceback.format_exc())
    forms.alert(
        "Không thể chuyển kết nối.\n\n{}\n\n"
        "Mô hình đã được hoàn tác nếu lỗi xảy ra trong Transaction.".format(error),
        title="90° → 2 elbow 45°",
        warn_icon=True
    )
