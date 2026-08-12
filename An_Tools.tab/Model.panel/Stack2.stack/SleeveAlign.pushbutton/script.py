# -*- coding: utf-8 -*-
"""
Align Sleeve To Pipe Center
---------------------------

Khi chay lenh, tool yeu cau chon 1 trong 2 che do:

1. SLEEVE DUNG / PIPE DUNG (che do cu)
- CLICK THUONG: chon Family sleeve mau, sau do quet chon Pipe dung.
- SHIFT + CLICK: chon Family sleeve mau, tu dong lay tat ca Pipe dung
  trong Active View.
- Chi dich sleeve theo X-Y, giu nguyen Z.

2. SLEEVE NGANG / PIPE KHONG DUNG (che do moi)
- CLICK THUONG: chon Family sleeve mau, sau do quet chon Pipe ngang
  hoac Pipe co do doc.
- SHIFT + CLICK: chon Family sleeve mau, tu dong lay tat ca Pipe
  khong dung trong Active View.
- Chi xu ly sleeve co truc nam ngang.
- Tim Pipe bao gom Pipe ngang va Pipe co do doc.
- Tim sleeve duoc chieu len mat bang tim Pipe de xac dinh tham so
  tren doan Pipe; cao do Z duoc noi suy dung tai vi tri do.
- Dich sleeve theo X-Y-Z de dua tim sleeve vao dung tim Pipe.

Ca hai che do:
- Chi tim sleeve thuoc Family da chon.
- Tim ung vien cuc bo quanh Pipe, khong quet toan bo Family trong View.
- Pipe phai thuc su di qua hoac rat gan BoundingBox sleeve.
- Khong cho phep sleeve bi keo qua khoang cach toi da.
- Co Progress Bar va Cancel.
- Cancel luc dich chuyen se rollback toan bo.
"""

import math

from pyrevit import revit, DB, forms, script

from Autodesk.Revit.UI.Selection import (
    ObjectType,
    ISelectionFilter
)

from Autodesk.Revit.Exceptions import OperationCanceledException


# ============================================================
# CHE DO ALIGN
# ============================================================

MODE_VERTICAL = "VERTICAL"
MODE_NONVERTICAL = "NONVERTICAL"

OPTION_VERTICAL = "Sleeve dung / Pipe dung - Align X-Y (che do cu)"
OPTION_NONVERTICAL = (
    "Sleeve ngang / Pipe ngang hoac doc - Align tim 3D (che do moi)"
)


# ============================================================
# CAU HINH
# ============================================================

# Sai so goc de xac dinh Pipe dung.
VERTICAL_ANGLE_TOLERANCE_DEG = 1.0

# Sai so goc de xac dinh truc sleeve nam ngang.
HORIZONTAL_SLEEVE_ANGLE_TOLERANCE_DEG = 5.0

# Vung tim sleeve cuc bo quanh Pipe.
LOCAL_PIPE_SEARCH_RADIUS_MM = 100.0

# Mo rong pham vi Z khi thu thap sleeve quanh Pipe.
LOCAL_SEARCH_Z_MARGIN_MM = 100.0

# Pipe phai di qua hoac rat gan BoundingBox sleeve.
PIPE_INSIDE_SLEEVE_MARGIN_MM = 15.0

# Sai so giao nhau theo cao do cho che do Pipe dung.
Z_OVERLAP_MARGIN_MM = 50.0

# Khong cho phep mot sleeve bi keo qua xa.
MAX_SLEEVE_MOVE_MM = 100.0

# Sai so xem nhu sleeve da dung tim Pipe.
MIN_MOVE_DISTANCE_MM = 0.5

# Che do cu giu nguyen cach lay diem tham chieu.
# LOCATION hoac BBOX_CENTER.
ALIGN_REFERENCE_MODE = "LOCATION"


# ============================================================
# REVIT CONTEXT
# ============================================================

doc = revit.doc
uidoc = revit.uidoc
active_view = doc.ActiveView

output = script.get_output()
output.close_others()
output.set_title("Align Sleeve To Pipe Center")


# ============================================================
# DON VI
# ============================================================

def mm_to_internal(value_mm):
    try:
        return DB.UnitUtils.ConvertToInternalUnits(
            value_mm,
            DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertToInternalUnits(
            value_mm,
            DB.DisplayUnitType.DUT_MILLIMETERS
        )


def internal_to_mm(value_internal):
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(
            value_internal,
            DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertFromInternalUnits(
            value_internal,
            DB.DisplayUnitType.DUT_MILLIMETERS
        )


LOCAL_PIPE_SEARCH_RADIUS = mm_to_internal(
    LOCAL_PIPE_SEARCH_RADIUS_MM
)

LOCAL_SEARCH_Z_MARGIN = mm_to_internal(
    LOCAL_SEARCH_Z_MARGIN_MM
)

PIPE_INSIDE_SLEEVE_MARGIN = mm_to_internal(
    PIPE_INSIDE_SLEEVE_MARGIN_MM
)

Z_OVERLAP_MARGIN = mm_to_internal(
    Z_OVERLAP_MARGIN_MM
)

MAX_SLEEVE_MOVE = mm_to_internal(
    MAX_SLEEVE_MOVE_MM
)

MIN_MOVE_DISTANCE = mm_to_internal(
    MIN_MOVE_DISTANCE_MM
)


# ============================================================
# SHIFT + CLICK
# ============================================================

def get_shift_click_state():
    try:
        return bool(script.get_shiftclick_state())
    except Exception:
        try:
            return bool(__shiftclick__)
        except Exception:
            return False


shift_clicked = get_shift_click_state()


# ============================================================
# HAM CHUNG
# ============================================================

def get_element_id_value(element_id):
    try:
        return element_id.Value
    except Exception:
        try:
            return element_id.IntegerValue
        except Exception:
            return str(element_id)


def sanitize_message(message):
    if message is None:
        return ""

    text = str(message)
    text = text.replace("\r", " ")
    text = text.replace("\n", " ")
    text = text.replace("|", "/")

    if len(text) > 180:
        text = text[:177] + "..."

    return text


def get_type_name(instance):
    try:
        symbol = instance.Symbol

        try:
            return symbol.Name
        except Exception:
            pass

        parameter = symbol.get_Parameter(
            DB.BuiltInParameter.SYMBOL_NAME_PARAM
        )

        if parameter:
            return parameter.AsString()

    except Exception:
        pass

    return "<Khong xac dinh>"


def get_family_name(instance):
    try:
        return instance.Symbol.Family.Name
    except Exception:
        return "<Khong xac dinh>"


def vector_length(vector):
    if vector is None:
        return 0.0

    try:
        return vector.GetLength()
    except Exception:
        return math.sqrt(
            vector.X * vector.X +
            vector.Y * vector.Y +
            vector.Z * vector.Z
        )


def normalize_vector(vector):
    length = vector_length(vector)

    if length <= 1e-9:
        return None

    return DB.XYZ(
        vector.X / length,
        vector.Y / length,
        vector.Z / length
    )


def distance_3d(point_a, point_b):
    if point_a is None or point_b is None:
        return 0.0

    delta = point_b.Subtract(point_a)
    return vector_length(delta)


def is_pipe(element):
    if element is None:
        return False

    try:
        category = element.Category

        if category is None:
            return False

        category_id = get_element_id_value(category.Id)
        pipe_category_id = int(DB.BuiltInCategory.OST_PipeCurves)

        return category_id == pipe_category_id

    except Exception:
        return False


# ============================================================
# CHON CHE DO
# ============================================================

def select_align_mode():
    options = [
        OPTION_VERTICAL,
        OPTION_NONVERTICAL
    ]

    selected = None

    try:
        selected = forms.CommandSwitchWindow.show(
            options,
            message=(
                "Chon cach align sleeve.\n\n"
                "Click thuong: quet chon Pipe.\n"
                "Shift+Click: tu dong lay Pipe trong Active View."
            )
        )
    except Exception:
        try:
            selected = forms.SelectFromList.show(
                options,
                title="Chon che do Align Sleeve",
                multiselect=False,
                button_name="Chay"
            )
        except Exception:
            selected = None

    if selected == OPTION_VERTICAL:
        return MODE_VERTICAL

    if selected == OPTION_NONVERTICAL:
        return MODE_NONVERTICAL

    return None


# ============================================================
# PIPE GEOMETRY
# ============================================================

def get_pipe_curve(pipe):
    try:
        location = pipe.Location

        if isinstance(location, DB.LocationCurve):
            return location.Curve

    except Exception:
        pass

    return None


def get_pipe_endpoints(pipe):
    curve = get_pipe_curve(pipe)

    if curve is None:
        return None, None

    try:
        return curve.GetEndPoint(0), curve.GetEndPoint(1)
    except Exception:
        return None, None


def is_vertical_pipe(pipe):
    if not is_pipe(pipe):
        return False

    start_point, end_point = get_pipe_endpoints(pipe)

    if start_point is None or end_point is None:
        return False

    direction = end_point.Subtract(start_point)
    pipe_length = vector_length(direction)

    if pipe_length <= 1e-9:
        return False

    vertical_ratio = abs(direction.Z) / pipe_length

    minimum_vertical_ratio = math.cos(
        math.radians(VERTICAL_ANGLE_TOLERANCE_DEG)
    )

    return vertical_ratio >= minimum_vertical_ratio


def is_nonvertical_pipe(pipe):
    if not is_pipe(pipe):
        return False

    if is_vertical_pipe(pipe):
        return False

    start_point, end_point = get_pipe_endpoints(pipe)

    if start_point is None or end_point is None:
        return False

    delta_x = end_point.X - start_point.X
    delta_y = end_point.Y - start_point.Y

    horizontal_length = math.sqrt(
        delta_x * delta_x +
        delta_y * delta_y
    )

    return horizontal_length > 1e-9


def pipe_matches_mode(pipe, align_mode):
    if align_mode == MODE_VERTICAL:
        return is_vertical_pipe(pipe)

    return is_nonvertical_pipe(pipe)


def get_pipe_data(pipe):
    start_point, end_point = get_pipe_endpoints(pipe)

    if start_point is None or end_point is None:
        return None

    direction = end_point.Subtract(start_point)
    length_3d = vector_length(direction)

    if length_3d <= 1e-9:
        return None

    delta_x = end_point.X - start_point.X
    delta_y = end_point.Y - start_point.Y

    horizontal_length = math.sqrt(
        delta_x * delta_x +
        delta_y * delta_y
    )

    return {
        "start": start_point,
        "end": end_point,
        "direction": direction,
        "length": length_3d,
        "horizontal_length": horizontal_length,
        "min_x": min(start_point.X, end_point.X),
        "max_x": max(start_point.X, end_point.X),
        "min_y": min(start_point.Y, end_point.Y),
        "max_y": max(start_point.Y, end_point.Y),
        "min_z": min(start_point.Z, end_point.Z),
        "max_z": max(start_point.Z, end_point.Z),
        "center_x": (start_point.X + end_point.X) / 2.0,
        "center_y": (start_point.Y + end_point.Y) / 2.0
    }


def project_point_to_pipe_by_vertical_axis(point, pipe_data):
    """
    Lay tham so tren Pipe bang phep chieu tren mat bang XY.

    Day khong phai phep Project 3D cua Revit. Voi Pipe co do doc,
    cach nay dam bao Z duoc noi suy tai dung vi tri mat bang cua sleeve.
    """

    if point is None or pipe_data is None:
        return None, None, "Khong du du lieu de chieu tim sleeve"

    start_point = pipe_data["start"]
    end_point = pipe_data["end"]

    delta_x = end_point.X - start_point.X
    delta_y = end_point.Y - start_point.Y

    denominator = (
        delta_x * delta_x +
        delta_y * delta_y
    )

    if denominator <= 1e-12:
        return None, None, "Pipe khong co chieu dai chieu bang XY"

    raw_parameter = (
        (point.X - start_point.X) * delta_x +
        (point.Y - start_point.Y) * delta_y
    ) / denominator

    if raw_parameter < 0.0 or raw_parameter > 1.0:
        return (
            None,
            raw_parameter,
            "Hinh chieu tim sleeve nam ngoai doan Pipe"
        )

    target_point = DB.XYZ(
        start_point.X + raw_parameter * (
            end_point.X - start_point.X
        ),
        start_point.Y + raw_parameter * (
            end_point.Y - start_point.Y
        ),
        start_point.Z + raw_parameter * (
            end_point.Z - start_point.Z
        )
    )

    return target_point, raw_parameter, None


# ============================================================
# FAMILY / SLEEVE GEOMETRY
# ============================================================

def get_bounding_box(element):
    bbox = None

    try:
        bbox = element.get_BoundingBox(active_view)
    except Exception:
        bbox = None

    if bbox is None:
        try:
            bbox = element.get_BoundingBox(None)
        except Exception:
            bbox = None

    return bbox


def get_bbox_center(bbox):
    if bbox is None:
        return None

    return DB.XYZ(
        (bbox.Min.X + bbox.Max.X) / 2.0,
        (bbox.Min.Y + bbox.Max.Y) / 2.0,
        (bbox.Min.Z + bbox.Max.Z) / 2.0
    )


def get_location_point(element):
    try:
        location = element.Location
    except Exception:
        location = None

    if isinstance(location, DB.LocationPoint):
        try:
            return location.Point
        except Exception:
            pass

    if isinstance(location, DB.LocationCurve):
        try:
            return location.Curve.Evaluate(0.5, True)
        except Exception:
            pass

    return get_bbox_center(get_bounding_box(element))


def get_vertical_family_reference_point(instance, bbox):
    if ALIGN_REFERENCE_MODE.upper() == "BBOX_CENTER":
        bbox_center = get_bbox_center(bbox)

        if bbox_center is not None:
            return bbox_center

    return get_location_point(instance)


def points_are_same(point_a, point_b, tolerance=1e-7):
    if point_a is None or point_b is None:
        return False

    return distance_3d(point_a, point_b) <= tolerance


def get_connector_origins(instance):
    origins = []
    connector_manager = None

    try:
        mep_model = instance.MEPModel

        if mep_model is not None:
            connector_manager = mep_model.ConnectorManager
    except Exception:
        connector_manager = None

    if connector_manager is None:
        try:
            connector_manager = instance.ConnectorManager
        except Exception:
            connector_manager = None

    if connector_manager is None:
        return origins

    try:
        connectors = connector_manager.Connectors
    except Exception:
        return origins

    try:
        for connector in connectors:
            try:
                origin = connector.Origin
            except Exception:
                continue

            duplicated = False

            for existing_origin in origins:
                if points_are_same(origin, existing_origin):
                    duplicated = True
                    break

            if not duplicated:
                origins.append(origin)

    except Exception:
        pass

    return origins


def get_farthest_point_pair(points):
    if points is None or len(points) < 2:
        return None, None

    best_point_a = None
    best_point_b = None
    best_distance = -1.0

    for first_index in range(len(points)):
        for second_index in range(first_index + 1, len(points)):
            point_a = points[first_index]
            point_b = points[second_index]
            current_distance = distance_3d(point_a, point_b)

            if current_distance > best_distance:
                best_distance = current_distance
                best_point_a = point_a
                best_point_b = point_b

    return best_point_a, best_point_b


def get_horizontal_sleeve_geometry(instance, bbox):
    """
    Uu tien dung 2 connector xa nhau nhat de lay dung truc va tim sleeve.

    Neu Family khong co connector:
    - Tim sleeve: dung tam BoundingBox.
    - Truc sleeve: uu tien FacingOrientation; sau do BasisX cua Transform.

    Neu khong xac dinh duoc truc nam ngang, sleeve se bi bo qua de tranh
    move nham sleeve dung.
    """

    connector_origins = get_connector_origins(instance)

    point_a, point_b = get_farthest_point_pair(
        connector_origins
    )

    if point_a is not None and point_b is not None:
        axis_vector = point_b.Subtract(point_a)
        axis_direction = normalize_vector(axis_vector)

        if axis_direction is not None:
            center_point = DB.XYZ(
                (point_a.X + point_b.X) / 2.0,
                (point_a.Y + point_b.Y) / 2.0,
                (point_a.Z + point_b.Z) / 2.0
            )

            return center_point, axis_direction, "CONNECTORS"

    center_point = get_bbox_center(bbox)

    if center_point is None:
        center_point = get_location_point(instance)

    axis_direction = None

    try:
        facing_orientation = instance.FacingOrientation
        axis_direction = normalize_vector(facing_orientation)
    except Exception:
        axis_direction = None

    if axis_direction is None:
        try:
            transform = instance.GetTransform()
            axis_direction = normalize_vector(transform.BasisX)
        except Exception:
            axis_direction = None

    if center_point is None or axis_direction is None:
        return None, None, None

    return center_point, axis_direction, "FAMILY_ORIENTATION"


def is_horizontal_axis(axis_direction):
    if axis_direction is None:
        return False

    maximum_vertical_ratio = math.sin(
        math.radians(HORIZONTAL_SLEEVE_ANGLE_TOLERANCE_DEG)
    )

    return abs(axis_direction.Z) <= maximum_vertical_ratio


def pipe_xy_inside_sleeve_bbox(pipe_data, sleeve_bbox):
    if pipe_data is None or sleeve_bbox is None:
        return False

    pipe_x = pipe_data["center_x"]
    pipe_y = pipe_data["center_y"]

    return (
        pipe_x >= sleeve_bbox.Min.X - PIPE_INSIDE_SLEEVE_MARGIN and
        pipe_x <= sleeve_bbox.Max.X + PIPE_INSIDE_SLEEVE_MARGIN and
        pipe_y >= sleeve_bbox.Min.Y - PIPE_INSIDE_SLEEVE_MARGIN and
        pipe_y <= sleeve_bbox.Max.Y + PIPE_INSIDE_SLEEVE_MARGIN
    )


def pipe_z_overlaps_sleeve(pipe_data, sleeve_bbox, reference_point):
    if pipe_data is None:
        return False

    pipe_min_z = pipe_data["min_z"]
    pipe_max_z = pipe_data["max_z"]

    if sleeve_bbox is not None:
        sleeve_min_z = sleeve_bbox.Min.Z - Z_OVERLAP_MARGIN
        sleeve_max_z = sleeve_bbox.Max.Z + Z_OVERLAP_MARGIN

    elif reference_point is not None:
        sleeve_min_z = reference_point.Z - Z_OVERLAP_MARGIN
        sleeve_max_z = reference_point.Z + Z_OVERLAP_MARGIN

    else:
        return False

    if pipe_max_z < sleeve_min_z:
        return False

    if pipe_min_z > sleeve_max_z:
        return False

    return True


def segment_intersects_expanded_bbox(
    start_point,
    end_point,
    bbox,
    margin
):
    """
    Kiem tra doan thang tim Pipe co cat BoundingBox sleeve da mo rong
    hay khong bang thuat toan slab 3D.
    """

    if start_point is None or end_point is None or bbox is None:
        return False

    minimum_values = [
        bbox.Min.X - margin,
        bbox.Min.Y - margin,
        bbox.Min.Z - margin
    ]

    maximum_values = [
        bbox.Max.X + margin,
        bbox.Max.Y + margin,
        bbox.Max.Z + margin
    ]

    start_values = [
        start_point.X,
        start_point.Y,
        start_point.Z
    ]

    end_values = [
        end_point.X,
        end_point.Y,
        end_point.Z
    ]

    minimum_parameter = 0.0
    maximum_parameter = 1.0

    for axis_index in range(3):
        start_value = start_values[axis_index]
        direction_value = (
            end_values[axis_index] -
            start_values[axis_index]
        )

        minimum_value = minimum_values[axis_index]
        maximum_value = maximum_values[axis_index]

        if abs(direction_value) <= 1e-12:
            if (
                start_value < minimum_value or
                start_value > maximum_value
            ):
                return False

            continue

        first_parameter = (
            minimum_value - start_value
        ) / direction_value

        second_parameter = (
            maximum_value - start_value
        ) / direction_value

        if first_parameter > second_parameter:
            temporary = first_parameter
            first_parameter = second_parameter
            second_parameter = temporary

        minimum_parameter = max(
            minimum_parameter,
            first_parameter
        )

        maximum_parameter = min(
            maximum_parameter,
            second_parameter
        )

        if minimum_parameter > maximum_parameter:
            return False

    return True


# ============================================================
# SELECTION FILTER
# ============================================================

class FamilyInstanceSelectionFilter(ISelectionFilter):

    def AllowElement(self, element):
        return isinstance(element, DB.FamilyInstance)

    def AllowReference(self, reference, position):
        return False


class PipeModeSelectionFilter(ISelectionFilter):

    def __init__(self, align_mode):
        self.align_mode = align_mode

    def AllowElement(self, element):
        return pipe_matches_mode(element, self.align_mode)

    def AllowReference(self, reference, position):
        return False


# ============================================================
# CHON FAMILY MAU
# ============================================================

def get_sample_family_instance():
    selected_instances = []

    try:
        selected_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        selected_ids = []

    for element_id in selected_ids:
        element = doc.GetElement(element_id)

        if isinstance(element, DB.FamilyInstance):
            selected_instances.append(element)

    if selected_instances:
        family_ids = set()

        for instance in selected_instances:
            try:
                family_ids.add(
                    get_element_id_value(
                        instance.Symbol.Family.Id
                    )
                )
            except Exception:
                pass

        if len(family_ids) == 1:
            return selected_instances[0]

    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            FamilyInstanceSelectionFilter(),
            "Chon mot sleeve thuoc Family can xu ly"
        )

        return doc.GetElement(reference.ElementId)

    except OperationCanceledException:
        return None

    except Exception:
        return None


# ============================================================
# CHON / THU THAP PIPE
# ============================================================

def get_pipe_selection_prompt(align_mode):
    if align_mode == MODE_VERTICAL:
        return "Quet chon cac Pipe dung, sau do nhan Finish"

    return (
        "Quet chon cac Pipe ngang hoac Pipe co do doc, "
        "sau do nhan Finish"
    )


def pick_pipes(align_mode):
    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element,
            PipeModeSelectionFilter(align_mode),
            get_pipe_selection_prompt(align_mode)
        )

    except OperationCanceledException:
        return []

    except Exception:
        return []

    selected_pipes = []
    used_ids = set()

    for reference in references:
        pipe = doc.GetElement(reference.ElementId)

        if not pipe_matches_mode(pipe, align_mode):
            continue

        pipe_id = get_element_id_value(pipe.Id)

        if pipe_id in used_ids:
            continue

        used_ids.add(pipe_id)
        selected_pipes.append(pipe)

    return selected_pipes


def collect_all_pipes(align_mode):
    pipe_elements = (
        DB.FilteredElementCollector(doc, active_view.Id)
        .OfCategory(DB.BuiltInCategory.OST_PipeCurves)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    result = []

    for pipe in pipe_elements:
        if pipe_matches_mode(pipe, align_mode):
            result.append(pipe)

    return result


# ============================================================
# THU THAP SLEEVE CUC BO QUANH PIPE
# ============================================================

def create_pipe_local_outline(pipe_data, align_mode):
    if pipe_data is None:
        return None

    if align_mode == MODE_VERTICAL:
        minimum_point = DB.XYZ(
            pipe_data["center_x"] - LOCAL_PIPE_SEARCH_RADIUS,
            pipe_data["center_y"] - LOCAL_PIPE_SEARCH_RADIUS,
            pipe_data["min_z"] - LOCAL_SEARCH_Z_MARGIN
        )

        maximum_point = DB.XYZ(
            pipe_data["center_x"] + LOCAL_PIPE_SEARCH_RADIUS,
            pipe_data["center_y"] + LOCAL_PIPE_SEARCH_RADIUS,
            pipe_data["max_z"] + LOCAL_SEARCH_Z_MARGIN
        )

    else:
        minimum_point = DB.XYZ(
            pipe_data["min_x"] - LOCAL_PIPE_SEARCH_RADIUS,
            pipe_data["min_y"] - LOCAL_PIPE_SEARCH_RADIUS,
            pipe_data["min_z"] - LOCAL_SEARCH_Z_MARGIN
        )

        maximum_point = DB.XYZ(
            pipe_data["max_x"] + LOCAL_PIPE_SEARCH_RADIUS,
            pipe_data["max_y"] + LOCAL_PIPE_SEARCH_RADIUS,
            pipe_data["max_z"] + LOCAL_SEARCH_Z_MARGIN
        )

    try:
        return DB.Outline(minimum_point, maximum_point)
    except Exception:
        return None


def collect_nearby_sleeves_for_pipe(
    pipe,
    family_id,
    align_mode
):
    pipe_data = get_pipe_data(pipe)

    if pipe_data is None:
        return [], None

    outline = create_pipe_local_outline(
        pipe_data,
        align_mode
    )

    if outline is None:
        return [], pipe_data

    try:
        bbox_filter = DB.BoundingBoxIntersectsFilter(outline)

        collector = (
            DB.FilteredElementCollector(doc, active_view.Id)
            .OfClass(DB.FamilyInstance)
            .WherePasses(bbox_filter)
            .WhereElementIsNotElementType()
        )

        nearby_sleeves = []
        selected_family_id_value = get_element_id_value(family_id)

        for instance in collector:
            try:
                instance_family_id_value = get_element_id_value(
                    instance.Symbol.Family.Id
                )
            except Exception:
                continue

            if instance_family_id_value == selected_family_id_value:
                nearby_sleeves.append(instance)

        return nearby_sleeves, pipe_data

    except Exception as collect_error:
        try:
            output.print_md(
                "> Loi khi thu thap sleeve gan Pipe ID "
                "**{0}**: {1}".format(
                    get_element_id_value(pipe.Id),
                    sanitize_message(collect_error)
                )
            )
        except Exception:
            pass

        return [], pipe_data


def build_local_candidate_map(
    pipes,
    family_id,
    align_mode
):
    candidate_map = {}
    cancelled = False
    total_pipes = len(pipes)

    with forms.ProgressBar(
        title=(
            "Dang dinh vi sleeve gan Pipe "
            "{value}/{max_value}"
        ),
        cancellable=True,
        step=1
    ) as progress_bar:

        for index, pipe in enumerate(pipes, 1):
            if progress_bar.cancelled:
                cancelled = True
                break

            progress_bar.update_progress(index, total_pipes)

            nearby_sleeves, pipe_data = (
                collect_nearby_sleeves_for_pipe(
                    pipe,
                    family_id,
                    align_mode
                )
            )

            if pipe_data is None:
                continue

            pipe_id_value = get_element_id_value(pipe.Id)

            for sleeve in nearby_sleeves:
                sleeve_id_value = get_element_id_value(sleeve.Id)

                if sleeve_id_value not in candidate_map:
                    candidate_map[sleeve_id_value] = {
                        "instance": sleeve,
                        "pipes": {}
                    }

                candidate_map[sleeve_id_value]["pipes"][pipe_id_value] = {
                    "pipe": pipe,
                    "pipe_data": pipe_data
                }

    return candidate_map, cancelled


# ============================================================
# TIM PIPE PHU HOP - CHE DO CU
# ============================================================

def find_best_vertical_pipe_for_sleeve(
    sleeve,
    nearby_pipe_entries
):
    sleeve_bbox = get_bounding_box(sleeve)

    reference_point = get_vertical_family_reference_point(
        sleeve,
        sleeve_bbox
    )

    if reference_point is None:
        return None, None, "Khong lay duoc vi tri sleeve"

    if sleeve_bbox is None:
        return None, reference_point, "Khong lay duoc BoundingBox sleeve"

    valid_candidates = []
    rejected_outside_bbox = 0
    rejected_z = 0
    rejected_distance = 0

    for pipe_entry in nearby_pipe_entries:
        pipe = pipe_entry["pipe"]
        pipe_data = pipe_entry["pipe_data"]

        if not pipe_xy_inside_sleeve_bbox(pipe_data, sleeve_bbox):
            rejected_outside_bbox += 1
            continue

        if not pipe_z_overlaps_sleeve(
            pipe_data,
            sleeve_bbox,
            reference_point
        ):
            rejected_z += 1
            continue

        delta_x = pipe_data["center_x"] - reference_point.X
        delta_y = pipe_data["center_y"] - reference_point.Y

        distance_xy = math.sqrt(
            delta_x * delta_x +
            delta_y * delta_y
        )

        if distance_xy > MAX_SLEEVE_MOVE:
            rejected_distance += 1
            continue

        valid_candidates.append({
            "pipe": pipe,
            "pipe_data": pipe_data,
            "target_point": DB.XYZ(
                pipe_data["center_x"],
                pipe_data["center_y"],
                reference_point.Z
            ),
            "delta_x": delta_x,
            "delta_y": delta_y,
            "delta_z": 0.0,
            "distance": distance_xy,
            "reference_source": ALIGN_REFERENCE_MODE
        })

    if not valid_candidates:
        reason_parts = []

        if rejected_outside_bbox > 0:
            reason_parts.append(
                "tim Pipe khong nam trong BoundingBox sleeve"
            )

        if rejected_z > 0:
            reason_parts.append(
                "Pipe khong giao sleeve theo cao do"
            )

        if rejected_distance > 0:
            reason_parts.append(
                "khoang dich vuot {0:.0f} mm".format(
                    MAX_SLEEVE_MOVE_MM
                )
            )

        if not reason_parts:
            reason_parts.append("khong co Pipe dung phu hop")

        return None, reference_point, "; ".join(reason_parts)

    valid_candidates.sort(
        key=lambda item: (
            item["distance"],
            get_element_id_value(item["pipe"].Id)
        )
    )

    return valid_candidates[0], reference_point, None


# ============================================================
# TIM PIPE PHU HOP - CHE DO MOI
# ============================================================

def find_best_nonvertical_pipe_for_sleeve(
    sleeve,
    nearby_pipe_entries
):
    sleeve_bbox = get_bounding_box(sleeve)

    if sleeve_bbox is None:
        return None, None, "Khong lay duoc BoundingBox sleeve"

    sleeve_center, sleeve_axis, reference_source = (
        get_horizontal_sleeve_geometry(
            sleeve,
            sleeve_bbox
        )
    )

    if sleeve_center is None or sleeve_axis is None:
        return (
            None,
            sleeve_center,
            "Khong xac dinh duoc tim hoac truc cua sleeve"
        )

    if not is_horizontal_axis(sleeve_axis):
        return (
            None,
            sleeve_center,
            "Sleeve khong nam ngang nen bi bo qua"
        )

    valid_candidates = []
    rejected_not_intersecting = 0
    rejected_projection = 0
    rejected_distance = 0

    for pipe_entry in nearby_pipe_entries:
        pipe = pipe_entry["pipe"]
        pipe_data = pipe_entry["pipe_data"]

        if not segment_intersects_expanded_bbox(
            pipe_data["start"],
            pipe_data["end"],
            sleeve_bbox,
            PIPE_INSIDE_SLEEVE_MARGIN
        ):
            rejected_not_intersecting += 1
            continue

        target_point, pipe_parameter, projection_error = (
            project_point_to_pipe_by_vertical_axis(
                sleeve_center,
                pipe_data
            )
        )

        if target_point is None:
            rejected_projection += 1
            continue

        delta_x = target_point.X - sleeve_center.X
        delta_y = target_point.Y - sleeve_center.Y
        delta_z = target_point.Z - sleeve_center.Z

        move_distance = math.sqrt(
            delta_x * delta_x +
            delta_y * delta_y +
            delta_z * delta_z
        )

        if move_distance > MAX_SLEEVE_MOVE:
            rejected_distance += 1
            continue

        valid_candidates.append({
            "pipe": pipe,
            "pipe_data": pipe_data,
            "target_point": target_point,
            "pipe_parameter": pipe_parameter,
            "delta_x": delta_x,
            "delta_y": delta_y,
            "delta_z": delta_z,
            "distance": move_distance,
            "reference_source": reference_source
        })

    if not valid_candidates:
        reason_parts = []

        if rejected_not_intersecting > 0:
            reason_parts.append(
                "tim Pipe khong di qua BoundingBox sleeve"
            )

        if rejected_projection > 0:
            reason_parts.append(
                "khong chieu duoc tim sleeve vao trong doan Pipe"
            )

        if rejected_distance > 0:
            reason_parts.append(
                "khoang dich 3D vuot {0:.0f} mm".format(
                    MAX_SLEEVE_MOVE_MM
                )
            )

        if not reason_parts:
            reason_parts.append(
                "khong co Pipe ngang hoac Pipe doc phu hop"
            )

        return None, sleeve_center, "; ".join(reason_parts)

    valid_candidates.sort(
        key=lambda item: (
            item["distance"],
            get_element_id_value(item["pipe"].Id)
        )
    )

    return valid_candidates[0], sleeve_center, None


def find_best_pipe_for_sleeve(
    sleeve,
    nearby_pipe_entries,
    align_mode
):
    if align_mode == MODE_VERTICAL:
        return find_best_vertical_pipe_for_sleeve(
            sleeve,
            nearby_pipe_entries
        )

    return find_best_nonvertical_pipe_for_sleeve(
        sleeve,
        nearby_pipe_entries
    )


# ============================================================
# THONG TIN CHE DO
# ============================================================

def get_mode_display_name(align_mode):
    if align_mode == MODE_VERTICAL:
        return "Sleeve dung / Pipe dung - Align X-Y"

    return "Sleeve ngang / Pipe ngang hoac doc - Align tim 3D"


def get_pipe_description(align_mode):
    if align_mode == MODE_VERTICAL:
        return "Pipe dung"

    return "Pipe ngang hoac Pipe co do doc"


def get_distance_description(align_mode):
    if align_mode == MODE_VERTICAL:
        return "Sai lech XY (mm)"

    return "Sai lech 3D (mm)"


# ============================================================
# MAIN - CHON CHE DO, FAMILY VA PIPE
# ============================================================

align_mode = select_align_mode()

if align_mode is None:
    script.exit()

sample_instance = get_sample_family_instance()

if sample_instance is None:
    script.exit()

try:
    selected_family = sample_instance.Symbol.Family
    selected_family_id = selected_family.Id

except Exception:
    forms.alert(
        "Khong xac dinh duoc Family tu sleeve da chon.",
        exitscript=True
    )

if shift_clicked:
    if align_mode == MODE_VERTICAL:
        run_mode = "Shift+Click - tat ca Pipe dung trong Active View"
    else:
        run_mode = (
            "Shift+Click - tat ca Pipe ngang hoac Pipe co do doc "
            "trong Active View"
        )

    selected_pipes = collect_all_pipes(align_mode)

else:
    if align_mode == MODE_VERTICAL:
        run_mode = "Click thuong - Pipe dung duoc quet chon"
    else:
        run_mode = (
            "Click thuong - Pipe ngang hoac Pipe co do doc "
            "duoc quet chon"
        )

    selected_pipes = pick_pipes(align_mode)

if not selected_pipes:
    if shift_clicked:
        forms.alert(
            "Khong tim thay {0} nao trong Active View.".format(
                get_pipe_description(align_mode)
            ),
            exitscript=True
        )
    else:
        if align_mode == MODE_VERTICAL:
            selection_message = (
                "Chua chon Pipe dung nao.\n\n"
                "Pipe ngang va Pipe co do doc khong duoc phep chon."
            )
        else:
            selection_message = (
                "Chua chon Pipe ngang hoac Pipe co do doc nao.\n\n"
                "Pipe dung khong duoc phep chon trong che do nay."
            )

        forms.alert(
            selection_message,
            exitscript=True
        )


# ============================================================
# DINH VI CUC BO CAC SLEEVE GAN PIPE
# ============================================================

candidate_map, location_cancelled = build_local_candidate_map(
    selected_pipes,
    selected_family_id,
    align_mode
)

if location_cancelled:
    output.print_md("# Da huy lenh")

    output.print_md(
        "> Da huy trong qua trinh dinh vi sleeve gan Pipe.  \n"
        "> Chua co doi tuong nao bi dich chuyen."
    )

    script.exit()

if not candidate_map:
    output.print_md("# Khong tim thay sleeve gan Pipe")

    output.print_md(
        "**Che do Align:** {0}  \n"
        "**Che do chay:** {1}  \n"
        "**Family sleeve:** {2}  \n"
        "**So Pipe:** {3}  \n"
        "**Ban kinh tim kiem cuc bo:** {4:.0f} mm".format(
            get_mode_display_name(align_mode),
            run_mode,
            get_family_name(sample_instance),
            len(selected_pipes),
            LOCAL_PIPE_SEARCH_RADIUS_MM
        )
    )

    output.print_md(
        "> Khong co sleeve thuoc Family da chon nam trong "
        "vung tim kiem cuc bo quanh cac Pipe."
    )

    script.exit()


# ============================================================
# PHAN TICH CAC SLEEVE UNG VIEN
# ============================================================

move_requests = []
already_aligned = []
skipped_items = []

analysis_cancelled = False
candidate_items = list(candidate_map.values())
total_candidates = len(candidate_items)

with forms.ProgressBar(
    title=(
        "Dang kiem tra sleeve cuc bo "
        "{value}/{max_value}"
    ),
    cancellable=True,
    step=1
) as progress_bar:

    for index, candidate_item in enumerate(candidate_items, 1):
        if progress_bar.cancelled:
            analysis_cancelled = True
            break

        progress_bar.update_progress(index, total_candidates)

        sleeve = candidate_item["instance"]
        nearby_pipe_entries = list(
            candidate_item["pipes"].values()
        )

        try:
            if sleeve.GroupId != DB.ElementId.InvalidElementId:
                skipped_items.append({
                    "instance": sleeve,
                    "pipe": None,
                    "reason": "Sleeve dang nam trong Model Group"
                })
                continue

        except Exception:
            pass

        candidate, reference_point, error_message = (
            find_best_pipe_for_sleeve(
                sleeve,
                nearby_pipe_entries,
                align_mode
            )
        )

        if candidate is None:
            skipped_items.append({
                "instance": sleeve,
                "pipe": None,
                "reason": error_message
            })
            continue

        pipe = candidate["pipe"]
        move_distance = candidate["distance"]

        if move_distance <= MIN_MOVE_DISTANCE:
            already_aligned.append({
                "instance": sleeve,
                "pipe": pipe,
                "distance": move_distance,
                "delta_x": candidate["delta_x"],
                "delta_y": candidate["delta_y"],
                "delta_z": candidate["delta_z"],
                "reference_source": candidate.get(
                    "reference_source",
                    "-"
                )
            })
            continue

        move_requests.append({
            "instance": sleeve,
            "pipe": pipe,
            "reference_point": reference_point,
            "target_point": candidate.get("target_point"),
            "delta_x": candidate["delta_x"],
            "delta_y": candidate["delta_y"],
            "delta_z": candidate["delta_z"],
            "distance": move_distance,
            "reference_source": candidate.get(
                "reference_source",
                "-"
            )
        })

if analysis_cancelled:
    output.print_md("# Da huy lenh")

    output.print_md(
        "> Da huy trong qua trinh kiem tra cac sleeve ung vien.  \n"
        "> Chua co doi tuong nao bi dich chuyen."
    )

    script.exit()


# ============================================================
# DICH CHUYEN SLEEVE
# ============================================================

moved_items = []
failed_items = []
move_cancelled = False
processed_move_count = 0

if move_requests:
    transaction_name = "Align Sleeve To Pipe Center"

    transaction = DB.Transaction(
        doc,
        transaction_name
    )

    try:
        transaction.Start()
        total_moves = len(move_requests)

        with forms.ProgressBar(
            title=(
                "Dang dich chuyen sleeve "
                "{value}/{max_value}"
            ),
            cancellable=True,
            step=1
        ) as progress_bar:

            for index, request in enumerate(move_requests, 1):
                if progress_bar.cancelled:
                    move_cancelled = True
                    break

                progress_bar.update_progress(index, total_moves)
                processed_move_count = index

                sleeve = request["instance"]
                subtransaction = DB.SubTransaction(doc)

                try:
                    subtransaction.Start()

                    try:
                        was_pinned = sleeve.Pinned
                    except Exception:
                        was_pinned = False

                    if was_pinned:
                        sleeve.Pinned = False

                    translation = DB.XYZ(
                        request["delta_x"],
                        request["delta_y"],
                        request["delta_z"]
                    )

                    DB.ElementTransformUtils.MoveElement(
                        doc,
                        sleeve.Id,
                        translation
                    )

                    if was_pinned:
                        sleeve.Pinned = True

                    subtransaction.Commit()
                    moved_items.append(request)

                except Exception as move_error:
                    try:
                        if (
                            subtransaction.GetStatus() ==
                            DB.TransactionStatus.Started
                        ):
                            subtransaction.RollBack()
                    except Exception:
                        pass

                    failed_items.append({
                        "instance": sleeve,
                        "pipe": request["pipe"],
                        "reason": sanitize_message(move_error)
                    })

        if move_cancelled:
            transaction.RollBack()
            moved_items = []
        else:
            transaction.Commit()

    except Exception as transaction_error:
        try:
            if (
                transaction.GetStatus() ==
                DB.TransactionStatus.Started
            ):
                transaction.RollBack()
        except Exception:
            pass

        forms.alert(
            "Khong the hoan thanh Transaction.\n\n{0}".format(
                sanitize_message(transaction_error)
            ),
            exitscript=True
        )

if move_cancelled:
    output.print_md("# Da huy dich chuyen")

    output.print_md(
        "> Da dung tai buoc **{0}/{1}**.  \n"
        "> Toan bo thay doi da duoc rollback.  \n"
        "> Khong co sleeve nao bi giu lai o vi tri da "
        "dich chuyen.".format(
            processed_move_count,
            len(move_requests)
        )
    )

    script.exit()


# ============================================================
# BAO CAO TONG QUAN
# ============================================================

family_name = get_family_name(sample_instance)

output.print_md("# Ket qua dua sleeve vao tim Pipe")

output.print_md(
    "**Che do Align:** {0}  \n"
    "**Che do chay:** {1}  \n"
    "**Active View:** {2}  \n"
    "**Family sleeve:** {3}  \n"
    "**So Pipe duoc kiem tra:** {4}  \n"
    "**Sleeve duoc dinh vi gan Pipe:** {5}  \n"
    "**Da dich chuyen:** {6}  \n"
    "**Da dung tim Pipe:** {7}  \n"
    "**Bo qua hoac loi:** {8}".format(
        get_mode_display_name(align_mode),
        run_mode,
        active_view.Name,
        family_name,
        len(selected_pipes),
        len(candidate_items),
        len(moved_items),
        len(already_aligned),
        len(skipped_items) + len(failed_items)
    )
)

output.print_md(
    "**Ban kinh dinh vi cuc bo:** {0:.0f} mm  \n"
    "**Sai so Pipe gan BoundingBox sleeve:** {1:.0f} mm  \n"
    "**Khoang dich toi da:** {2:.0f} mm".format(
        LOCAL_PIPE_SEARCH_RADIUS_MM,
        PIPE_INSIDE_SLEEVE_MARGIN_MM,
        MAX_SLEEVE_MOVE_MM
    )
)

if align_mode == MODE_NONVERTICAL:
    output.print_md(
        "> **Pipe doc:** Z duoc noi suy theo tham so cua hinh chieu "
        "tim sleeve tren mat bang XY; khong dung diem gan nhat 3D."
    )


# ============================================================
# BANG DA DICH CHUYEN
# ============================================================

if moved_items:
    output.print_md("## Cac sleeve da dich chuyen")

    moved_rows = []

    for index, item in enumerate(moved_items, 1):
        sleeve = item["instance"]
        pipe = item["pipe"]

        moved_rows.append([
            index,
            output.linkify(sleeve.Id, title="Zoom Sleeve"),
            get_element_id_value(sleeve.Id),
            get_type_name(sleeve),
            output.linkify(pipe.Id, title="Zoom Pipe"),
            get_element_id_value(pipe.Id),
            "{0:.1f}".format(
                internal_to_mm(item["delta_x"])
            ),
            "{0:.1f}".format(
                internal_to_mm(item["delta_y"])
            ),
            "{0:.1f}".format(
                internal_to_mm(item["delta_z"])
            ),
            "{0:.1f}".format(
                internal_to_mm(item["distance"])
            ),
            item.get("reference_source", "-")
        ])

    output.print_table(
        table_data=moved_rows,
        columns=[
            "STT",
            "Kiem tra",
            "Sleeve ID",
            "Family Type",
            "Pipe",
            "Pipe ID",
            "Dich X (mm)",
            "Dich Y (mm)",
            "Dich Z (mm)",
            "Tong dich (mm)",
            "Nguon tim sleeve"
        ]
    )


# ============================================================
# BANG DA DUNG TIM
# ============================================================

if already_aligned:
    output.print_md("## Cac sleeve da nam dung tim Pipe")

    aligned_rows = []

    for index, item in enumerate(already_aligned, 1):
        sleeve = item["instance"]
        pipe = item["pipe"]

        aligned_rows.append([
            index,
            output.linkify(sleeve.Id, title="Zoom Sleeve"),
            get_element_id_value(sleeve.Id),
            get_type_name(sleeve),
            output.linkify(pipe.Id, title="Zoom Pipe"),
            get_element_id_value(pipe.Id),
            "{0:.2f}".format(
                internal_to_mm(item["distance"])
            ),
            item.get("reference_source", "-")
        ])

    output.print_table(
        table_data=aligned_rows,
        columns=[
            "STT",
            "Kiem tra",
            "Sleeve ID",
            "Family Type",
            "Pipe",
            "Pipe ID",
            get_distance_description(align_mode),
            "Nguon tim sleeve"
        ]
    )


# ============================================================
# BANG BO QUA / LOI
# ============================================================

combined_skipped = []

for item in skipped_items:
    combined_skipped.append(item)

for item in failed_items:
    combined_skipped.append(item)

if combined_skipped:
    output.print_md("## Cac sleeve khong duoc dich chuyen")

    skipped_rows = []

    for index, item in enumerate(combined_skipped, 1):
        sleeve = item["instance"]
        pipe = item.get("pipe")
        pipe_link = "-"

        if pipe is not None:
            pipe_link = output.linkify(
                pipe.Id,
                title="Zoom Pipe"
            )

        skipped_rows.append([
            index,
            output.linkify(sleeve.Id, title="Zoom Sleeve"),
            get_element_id_value(sleeve.Id),
            get_type_name(sleeve),
            pipe_link,
            sanitize_message(item["reason"])
        ])

    output.print_table(
        table_data=skipped_rows,
        columns=[
            "STT",
            "Kiem tra",
            "Sleeve ID",
            "Family Type",
            "Pipe",
            "Nguyen nhan"
        ]
    )

if not moved_items:
    output.print_md("> Khong co sleeve nao can dich chuyen.")
