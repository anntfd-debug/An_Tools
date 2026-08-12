# -*- coding: utf-8 -*-
"""
Place Model Groups By Rooms In Revit Links
IronPython 2.7 / pyRevit / Revit 2019+

Workflow:
1. Pick one or more visible RevitLinkInstance objects.
2. Select a Model Group Type and one existing Group instance as the sample.
3. Filter linked Rooms by one or two arbitrary Room parameters using Contains.
4. When Start is pressed, Revit zooms to the selected sample Group and asks the
   user to pick a linked Door associated with that Group.
5. The tool records the sample Group-to-Door local offset and relative angle.
6. For every filtered Room, the tool finds a Door, preferring the same Family/Type
   as the sample Door, and places the Group using the same relative transform.

Notes:
- Multiple Contains texts inside one filter are separated by ';' and use OR logic.
- Both enabled Room filters use AND logic.
- Target Rooms are limited to the starting Active View's crop/view range.
- Direct Door FromRoom/ToRoom association is preferred. Room.IsPointInRoom is
  used as a fallback.
- Cancel during placement rolls back all Groups created by the current run.
"""

import math
import os
import traceback

from pyrevit import revit, DB, UI, forms, script
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

try:
    from System.Collections.Generic import List
except Exception:
    List = None


doc = revit.doc
uidoc = revit.uidoc
start_view = revit.active_view
output = script.get_output()

try:
    text_type = unicode
except NameError:
    text_type = str


# ============================================================
# GENERAL UTILITIES
# ============================================================

def safe_text(value):
    if value is None:
        return u""
    try:
        return text_type(value)
    except Exception:
        try:
            return text_type(str(value), "utf-8", "ignore")
        except Exception:
            return u""


def get_id_value(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        return int(element_id.IntegerValue)


def invalid_id(element_id):
    if element_id is None:
        return True
    try:
        return get_id_value(element_id) == get_id_value(DB.ElementId.InvalidElementId)
    except Exception:
        return True


def get_element_name(element):
    if element is None:
        return u""
    try:
        value = element.Name
        if value:
            return safe_text(value)
    except Exception:
        pass

    for bip in (
        DB.BuiltInParameter.SYMBOL_NAME_PARAM,
        DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,
        DB.BuiltInParameter.ELEM_TYPE_PARAM
    ):
        try:
            parameter = element.get_Parameter(bip)
            if parameter is not None:
                value = parameter.AsString()
                if value:
                    return safe_text(value)
        except Exception:
            pass
    return u""


def mm_to_internal(value):
    try:
        return DB.UnitUtils.ConvertToInternalUnits(
            float(value), DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertToInternalUnits(
            float(value), DB.DisplayUnitType.DUT_MILLIMETERS
        )


def internal_to_mm(value):
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(
            float(value), DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertFromInternalUnits(
            float(value), DB.DisplayUnitType.DUT_MILLIMETERS
        )


def parse_number(raw_value, field_name, minimum=None):
    value_text = safe_text(raw_value).strip().replace(u",", u".")
    if not value_text:
        value = 0.0
    else:
        try:
            value = float(value_text)
        except Exception:
            raise ValueError(u"'{0}' phải là một số hợp lệ.".format(field_name))

    if minimum is not None and value < minimum:
        raise ValueError(
            u"'{0}' phải lớn hơn hoặc bằng {1}.".format(field_name, minimum)
        )
    return value


def parse_integer(raw_value, field_name, minimum=None, maximum=None):
    value_text = safe_text(raw_value).strip().replace(u",", u".")
    try:
        numeric_value = float(value_text)
        value = int(numeric_value)
        if abs(numeric_value - value) > 0.0000001:
            raise Exception()
    except Exception:
        raise ValueError(u"'{0}' phải là số nguyên hợp lệ.".format(field_name))

    if minimum is not None and value < minimum:
        raise ValueError(
            u"'{0}' phải lớn hơn hoặc bằng {1}.".format(field_name, minimum)
        )
    if maximum is not None and value > maximum:
        raise ValueError(
            u"'{0}' phải nhỏ hơn hoặc bằng {1}.".format(field_name, maximum)
        )
    return value


def normalize_xy(vector):
    if vector is None:
        return None
    vector_xy = DB.XYZ(vector.X, vector.Y, 0.0)
    if vector_xy.GetLength() < 0.0000001:
        return None
    return vector_xy.Normalize()


def perpendicular_xy(vector):
    direction = normalize_xy(vector)
    if direction is None:
        return DB.XYZ.BasisY
    return DB.XYZ(-direction.Y, direction.X, 0.0)


def vector_angle_xy(vector):
    direction = normalize_xy(vector)
    if direction is None:
        return 0.0
    return math.atan2(direction.Y, direction.X)


def normalize_angle(angle):
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle <= -math.pi:
        angle += 2.0 * math.pi
    return angle


def format_angle(angle):
    return u"{0:.2f}".format(math.degrees(normalize_angle(angle)))


def format_point_mm(point):
    if point is None:
        return u"-"
    return u"{0:.1f}; {1:.1f}; {2:.1f}".format(
        internal_to_mm(point.X),
        internal_to_mm(point.Y),
        internal_to_mm(point.Z)
    )


def points_distance(point_a, point_b):
    return (point_a - point_b).GetLength()


def category_is(element, built_in_category):
    try:
        return (
            element.Category is not None and
            get_id_value(element.Category.Id) == int(built_in_category)
        )
    except Exception:
        return False


def set_uidoc_selection(element_ids):
    try:
        if List is None:
            return
        values = List[DB.ElementId]()
        for element_id in element_ids:
            values.Add(element_id)
        uidoc.Selection.SetElementIds(values)
    except Exception:
        pass


# ============================================================
# OUTPUT REPORT
# ============================================================

def show_report(title, summary=None, notices=None, created=None, failed=None, traceback_text=None):
    summary = summary or []
    notices = notices or []
    created = created or []
    failed = failed or []

    try:
        output.set_title(u"Place Groups By Linked Rooms")
    except Exception:
        pass

    output.print_md(u"# Place Groups By Linked Rooms")
    output.print_md(u"## {0}".format(safe_text(title)))

    if summary:
        output.print_md(u"### Tóm tắt")
        for line in summary:
            output.print_md(u"- {0}".format(safe_text(line)))

    if notices:
        output.print_md(u"### Thông báo")
        for line in notices:
            output.print_md(u"- {0}".format(safe_text(line)))

    if created:
        output.print_md(u"### Group đã tạo")
        rows = []
        for index, item in enumerate(created):
            group_id = item.get("group_id")
            try:
                group_link = output.linkify(
                    group_id,
                    title=u"Group {0}".format(item.get("group_id_value", u"?"))
                )
            except Exception:
                group_link = u"Group {0}".format(item.get("group_id_value", u"?"))

            rows.append([
                index + 1,
                group_link,
                safe_text(item.get("room", u"")),
                safe_text(item.get("door", u"")),
                safe_text(item.get("link", u"")),
                safe_text(item.get("point", u"")),
                safe_text(item.get("angle", u"")),
                safe_text(item.get("door_match", u""))
            ])

        try:
            output.print_table(
                table_data=rows,
                columns=[
                    u"STT", u"Chọn / Zoom", u"Room", u"Door", u"Revit Link",
                    u"Điểm đặt X;Y;Z (mm)", u"Góc (°)", u"Chọn Door"
                ]
            )
        except Exception:
            for row in rows:
                output.print_md(
                    u"{0}. {1} | {2} | {3} | {4} | {5} | {6}° | {7}".format(*row)
                )

    if failed:
        output.print_md(u"### Room không xử lý được")
        for line in failed:
            output.print_md(u"- {0}".format(safe_text(line)))

    if traceback_text:
        output.print_md(u"### Chi tiết kỹ thuật")
        try:
            output.print_code(safe_text(traceback_text))
        except Exception:
            output.print_md(u"```\n{0}\n```".format(safe_text(traceback_text)))

    output.print_md(u"---")
    output.print_md(
        u"View nguồn khi mở tool: **{0}**".format(
            get_element_name(start_view) if start_view is not None else u"Không xác định"
        )
    )


# ============================================================
# LINK, GROUP AND PARAMETER ITEMS
# ============================================================

class LinkItem(object):
    def __init__(self, link_instance):
        self.element = link_instance
        self.link_doc = link_instance.GetLinkDocument()
        self.transform = link_instance.GetTotalTransform()
        self.uid = safe_text(link_instance.UniqueId)

        instance_name = get_element_name(link_instance)
        document_name = u""
        try:
            document_name = safe_text(self.link_doc.Title)
        except Exception:
            pass

        if document_name and document_name.lower() not in instance_name.lower():
            self.display_name = u"{0} | {1}".format(instance_name, document_name)
        else:
            self.display_name = instance_name or document_name or u"Revit Link"


class GroupTypeItem(object):
    def __init__(self, group_type):
        self.element = group_type
        self.uid = safe_text(group_type.UniqueId)
        self.display_name = get_element_name(group_type) or u"Unnamed Group Type"


class GroupInstanceItem(object):
    def __init__(self, group):
        self.element = group
        self.uid = safe_text(group.UniqueId)
        self.id_value = get_id_value(group.Id)
        point = get_group_location(group)
        level_text = get_element_level_name(group)
        self.display_name = u"ID {0} | {1} | {2}".format(
            self.id_value,
            level_text or u"Không rõ Level",
            format_point_mm(point)
        )


class RoomParameterItem(object):
    def __init__(self, name, is_shared=False, is_builtin=False):
        self.name = safe_text(name)
        self.is_shared = bool(is_shared)
        self.is_builtin = bool(is_builtin)
        suffix = u""
        if self.is_shared:
            suffix = u" [Shared]"
        elif self.is_builtin:
            suffix = u" [Built-in]"
        self.display_name = self.name + suffix


class RoomWorkItem(object):
    def __init__(self, link_item, room):
        self.link_item = link_item
        self.room = room
        self.room_id_value = get_id_value(room.Id)
        self.room_key = u"{0}|{1}".format(link_item.uid, self.room_id_value)
        self.host_point = get_room_host_point(room, link_item)


class DoorWorkItem(object):
    def __init__(self, link_item, door):
        self.link_item = link_item
        self.door = door
        self.door_id_value = get_id_value(door.Id)
        self.key = u"{0}|{1}".format(link_item.uid, self.door_id_value)
        self.semantic_key = get_door_semantic_key(door)
        self.link_point = get_element_location_point(door)
        self.host_point = None
        if self.link_point is not None:
            try:
                self.host_point = link_item.transform.OfPoint(self.link_point)
            except Exception:
                pass


# ============================================================
# SELECTION FILTERS
# ============================================================

class RevitLinkSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        try:
            return isinstance(element, DB.RevitLinkInstance)
        except Exception:
            return False

    def AllowReference(self, reference, position):
        return False


class LinkedDoorSelectionFilter(ISelectionFilter):
    def __init__(self, host_document, allowed_link_ids):
        self.host_document = host_document
        self.allowed_link_ids = set(allowed_link_ids or [])

    def AllowElement(self, element):
        try:
            return (
                isinstance(element, DB.RevitLinkInstance) and
                get_id_value(element.Id) in self.allowed_link_ids
            )
        except Exception:
            return False

    def AllowReference(self, reference, position):
        try:
            link_instance = self.host_document.GetElement(reference.ElementId)
            if not isinstance(link_instance, DB.RevitLinkInstance):
                return False
            if get_id_value(link_instance.Id) not in self.allowed_link_ids:
                return False
            link_doc = link_instance.GetLinkDocument()
            if link_doc is None or invalid_id(reference.LinkedElementId):
                return False
            linked_element = link_doc.GetElement(reference.LinkedElementId)
            return (
                isinstance(linked_element, DB.FamilyInstance) and
                category_is(linked_element, DB.BuiltInCategory.OST_Doors)
            )
        except Exception:
            return False


# ============================================================
# PICK AND COLLECT LINKS
# ============================================================

def pick_link_items():
    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element,
            RevitLinkSelectionFilter(),
            u"Chọn một hoặc nhiều Revit Link chứa Room, sau đó bấm Finish"
        )
    except UI.Exceptions.OperationCanceledException:
        return []

    results = []
    used_ids = set()
    for reference in references:
        try:
            link_instance = doc.GetElement(reference.ElementId)
            if not isinstance(link_instance, DB.RevitLinkInstance):
                continue
            if link_instance.GetLinkDocument() is None:
                continue
            id_value = get_id_value(link_instance.Id)
            if id_value in used_ids:
                continue
            used_ids.add(id_value)
            results.append(LinkItem(link_instance))
        except Exception:
            pass
    return results


def get_link_ids_visible_in_view(view):
    results = set()
    try:
        collector = (
            DB.FilteredElementCollector(doc, view.Id)
            .OfClass(DB.RevitLinkInstance)
            .WhereElementIsNotElementType()
        )
        for link_instance in collector:
            results.add(get_id_value(link_instance.Id))
    except Exception:
        pass
    return results


def keep_visible_links(link_items, view):
    visible_ids = get_link_ids_visible_in_view(view)
    visible = []
    hidden = []
    for item in link_items:
        if get_id_value(item.element.Id) in visible_ids:
            visible.append(item)
        else:
            hidden.append(item)
    return visible, hidden


# ============================================================
# GROUP FUNCTIONS
# ============================================================

def is_model_group_type(group_type):
    try:
        return (
            group_type.Category is not None and
            get_id_value(group_type.Category.Id) == int(DB.BuiltInCategory.OST_IOSModelGroups)
        )
    except Exception:
        return False


def collect_model_group_types():
    results = []
    for group_type in DB.FilteredElementCollector(doc).OfClass(DB.GroupType):
        if is_model_group_type(group_type):
            results.append(GroupTypeItem(group_type))
    results.sort(key=lambda item: item.display_name.lower())
    return results


def get_element_level_name(element):
    built_in_parameters = []
    for parameter_name in (
        "GROUP_LEVEL",
        "FAMILY_LEVEL_PARAM",
        "SCHEDULE_LEVEL_PARAM",
        "INSTANCE_REFERENCE_LEVEL_PARAM"
    ):
        try:
            built_in_parameters.append(getattr(DB.BuiltInParameter, parameter_name))
        except Exception:
            pass

    for bip in built_in_parameters:
        try:
            parameter = element.get_Parameter(bip)
            if parameter is None:
                continue
            level_id = parameter.AsElementId()
            if invalid_id(level_id):
                continue
            level = doc.GetElement(level_id)
            name = get_element_name(level)
            if name:
                return name
        except Exception:
            pass
    return u""


def collect_group_instances(group_type):
    results = []
    target_id = get_id_value(group_type.Id)
    collector = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.Group)
        .WhereElementIsNotElementType()
    )
    for group in collector:
        try:
            if get_id_value(group.GroupType.Id) != target_id:
                continue
            results.append(GroupInstanceItem(group))
        except Exception:
            pass
    results.sort(key=lambda item: item.id_value)
    return results


def get_group_location(group):
    try:
        location = group.Location
        if isinstance(location, DB.LocationPoint):
            return location.Point
    except Exception:
        pass
    try:
        bbox = group.get_BoundingBox(None)
        if bbox is not None:
            transform = bbox.Transform
            min_pt = transform.OfPoint(bbox.Min)
            max_pt = transform.OfPoint(bbox.Max)
            return DB.XYZ(
                (min_pt.X + max_pt.X) * 0.5,
                (min_pt.Y + max_pt.Y) * 0.5,
                (min_pt.Z + max_pt.Z) * 0.5
            )
    except Exception:
        pass
    return None


def get_group_basis_x(group):
    try:
        transform = group.GetTransform()
        direction = normalize_xy(transform.BasisX)
        if direction is not None:
            return direction
    except Exception:
        pass
    try:
        location = group.Location
        if isinstance(location, DB.LocationPoint):
            angle = float(location.Rotation)
            return DB.XYZ(math.cos(angle), math.sin(angle), 0.0)
    except Exception:
        pass
    return DB.XYZ.BasisX


def collect_existing_group_points(group_type):
    points = []
    for item in collect_group_instances(group_type):
        point = get_group_location(item.element)
        if point is not None:
            points.append(point)
    return points


def has_nearby_point(target_point, existing_points, tolerance):
    if tolerance <= 0.0:
        return False
    for point in existing_points:
        try:
            if points_distance(target_point, point) <= tolerance:
                return True
        except Exception:
            pass
    return False


# ============================================================
# ACTIVE VIEW GEOMETRY FILTERING
# ============================================================

def bbox_local_corners(bbox):
    if bbox is None:
        return []
    min_pt = bbox.Min
    max_pt = bbox.Max
    return [
        DB.XYZ(min_pt.X, min_pt.Y, min_pt.Z),
        DB.XYZ(max_pt.X, min_pt.Y, min_pt.Z),
        DB.XYZ(min_pt.X, max_pt.Y, min_pt.Z),
        DB.XYZ(max_pt.X, max_pt.Y, min_pt.Z),
        DB.XYZ(min_pt.X, min_pt.Y, max_pt.Z),
        DB.XYZ(max_pt.X, min_pt.Y, max_pt.Z),
        DB.XYZ(min_pt.X, max_pt.Y, max_pt.Z),
        DB.XYZ(max_pt.X, max_pt.Y, max_pt.Z)
    ]


def bbox_model_corners(bbox):
    if bbox is None:
        return []
    try:
        transform = bbox.Transform
    except Exception:
        transform = DB.Transform.Identity
    results = []
    for point in bbox_local_corners(bbox):
        try:
            results.append(transform.OfPoint(point))
        except Exception:
            results.append(point)
    return results


def ranges_overlap(min_a, max_a, min_b, max_b, tolerance=0.000001):
    return not (
        max_a < min_b - tolerance or
        min_a > max_b + tolerance
    )


def get_plan_view_z_range(view_plan):
    try:
        view_range = view_plan.GetViewRange()
    except Exception:
        return None

    elevations = []
    for plane in (
        DB.PlanViewPlane.TopClipPlane,
        DB.PlanViewPlane.CutPlane,
        DB.PlanViewPlane.BottomClipPlane,
        DB.PlanViewPlane.ViewDepthPlane
    ):
        try:
            level_id = view_range.GetLevelId(plane)
            offset = view_range.GetOffset(plane)
            if invalid_id(level_id):
                continue
            level = doc.GetElement(level_id)
            if isinstance(level, DB.Level):
                elevations.append(level.Elevation + offset)
        except Exception:
            pass
    if not elevations:
        return None
    return min(elevations), max(elevations)


def get_active_clip_box(view):
    try:
        if isinstance(view, DB.View3D) and view.IsSectionBoxActive:
            return view.GetSectionBox(), True
    except Exception:
        pass
    try:
        if view.CropBoxActive:
            return view.CropBox, not isinstance(view, DB.ViewPlan)
    except Exception:
        pass
    return None, False


def points_intersect_oriented_box(host_points, oriented_box, check_z):
    if not host_points or oriented_box is None:
        return True
    try:
        inverse = oriented_box.Transform.Inverse
    except Exception:
        inverse = DB.Transform.Identity

    local_points = []
    for point in host_points:
        try:
            local_points.append(inverse.OfPoint(point))
        except Exception:
            local_points.append(point)

    xs = [point.X for point in local_points]
    ys = [point.Y for point in local_points]
    zs = [point.Z for point in local_points]
    min_pt = oriented_box.Min
    max_pt = oriented_box.Max

    if not ranges_overlap(min(xs), max(xs), min_pt.X, max_pt.X):
        return False
    if not ranges_overlap(min(ys), max(ys), min_pt.Y, max_pt.Y):
        return False
    if check_z and not ranges_overlap(min(zs), max(zs), min_pt.Z, max_pt.Z):
        return False
    return True


def get_room_host_bbox_points(room, link_item):
    try:
        bbox = room.get_BoundingBox(None)
    except Exception:
        bbox = None
    if bbox is None:
        return []

    results = []
    for link_point in bbox_model_corners(bbox):
        try:
            results.append(link_item.transform.OfPoint(link_point))
        except Exception:
            pass
    return results


def room_intersects_start_view(room, link_item, view):
    host_points = get_room_host_bbox_points(room, link_item)
    host_location = get_room_host_point(room, link_item)

    if not host_points and host_location is not None:
        host_points = [host_location]
    if not host_points:
        return False

    clip_box, check_z = get_active_clip_box(view)
    if clip_box is not None and not points_intersect_oriented_box(
        host_points, clip_box, check_z
    ):
        return False

    if isinstance(view, DB.ViewPlan):
        z_range = get_plan_view_z_range(view)
        if z_range is not None:
            z_values = [point.Z for point in host_points]
            if not ranges_overlap(
                min(z_values), max(z_values), z_range[0], z_range[1]
            ):
                return False
    return True


# ============================================================
# ROOM COLLECTION AND FILTERS
# ============================================================

def get_link_graphics_settings(view, link_instance):
    try:
        settings = view.GetLinkOverrides(link_instance.Id)
        if settings is not None:
            return settings
    except Exception:
        pass
    try:
        settings = view.GetLinkOverrides(link_instance.GetTypeId())
        if settings is not None:
            return settings
    except Exception:
        pass
    return None


def get_linked_view(link_item, host_view):
    settings = get_link_graphics_settings(host_view, link_item.element)
    if settings is None:
        return None
    try:
        visibility_type = safe_text(settings.LinkVisibilityType).lower()
        linked_view_id = settings.LinkedViewId
        if "bylinkview" not in visibility_type and "by link view" not in visibility_type:
            return None
        if invalid_id(linked_view_id):
            return None
        linked_view = link_item.link_doc.GetElement(linked_view_id)
        if isinstance(linked_view, DB.View) and not linked_view.IsTemplate:
            return linked_view
    except Exception:
        pass
    return None


def collect_room_elements(link_item, host_view):
    linked_view = get_linked_view(link_item, host_view)
    try:
        if linked_view is not None:
            collector = (
                DB.FilteredElementCollector(link_item.link_doc, linked_view.Id)
                .OfCategory(DB.BuiltInCategory.OST_Rooms)
                .WhereElementIsNotElementType()
            )
            return list(collector)
    except Exception:
        pass

    collector = (
        DB.FilteredElementCollector(link_item.link_doc)
        .OfCategory(DB.BuiltInCategory.OST_Rooms)
        .WhereElementIsNotElementType()
    )
    return list(collector)


def room_is_valid(room):
    try:
        if room.Area <= 0.0000001:
            return False
    except Exception:
        return False
    return get_room_link_point(room) is not None


def get_room_link_point(room):
    try:
        location = room.Location
        if isinstance(location, DB.LocationPoint):
            return location.Point
    except Exception:
        pass
    try:
        bbox = room.get_BoundingBox(None)
        if bbox is not None:
            points = bbox_model_corners(bbox)
            if points:
                count = float(len(points))
                return DB.XYZ(
                    sum(point.X for point in points) / count,
                    sum(point.Y for point in points) / count,
                    sum(point.Z for point in points) / count
                )
    except Exception:
        pass
    return None


def get_room_host_point(room, link_item):
    link_point = get_room_link_point(room)
    if link_point is None:
        return None
    try:
        return link_item.transform.OfPoint(link_point)
    except Exception:
        return None


def describe_room(room):
    number = u""
    name = u""
    try:
        number = safe_text(room.Number)
    except Exception:
        pass
    try:
        name = safe_text(room.Name)
    except Exception:
        pass
    if not name:
        try:
            parameter = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
            if parameter is not None:
                name = safe_text(parameter.AsString())
        except Exception:
            pass
    return u"{0} - {1} [ID {2}]".format(
        number or u"?", name or u"Unnamed Room", get_id_value(room.Id)
    )


def collect_visible_room_work_items(link_items, host_view):
    results = []
    for link_item in link_items:
        for room in collect_room_elements(link_item, host_view):
            try:
                if not room_is_valid(room):
                    continue
                if not room_intersects_start_view(room, link_item, host_view):
                    continue
                results.append(RoomWorkItem(link_item, room))
            except Exception:
                pass

    results.sort(
        key=lambda item: (
            item.link_item.display_name.lower(),
            safe_text(getattr(item.room, "Number", u"")).lower(),
            safe_text(getattr(item.room, "Name", u"")).lower(),
            item.room_id_value
        )
    )
    return results


def parameter_definition_name(parameter):
    try:
        return safe_text(parameter.Definition.Name).strip()
    except Exception:
        return u""


def collect_room_parameter_items(room_work_items):
    catalog = {}
    for work_item in room_work_items:
        try:
            parameters = work_item.room.Parameters
        except Exception:
            parameters = []
        for parameter in parameters:
            try:
                name = parameter_definition_name(parameter)
                if not name:
                    continue
                key = name.lower()
                if key not in catalog:
                    is_shared = False
                    is_builtin = False
                    try:
                        is_shared = parameter.IsShared is True
                    except Exception:
                        pass
                    try:
                        is_builtin = get_id_value(parameter.Id) < 0
                    except Exception:
                        pass
                    catalog[key] = RoomParameterItem(name, is_shared, is_builtin)
            except Exception:
                pass

    results = list(catalog.values())
    results.sort(key=lambda item: item.name.lower())
    return results


def get_room_parameter(room, parameter_name):
    target = safe_text(parameter_name).strip().lower()
    if not target:
        return None

    try:
        parameter = room.LookupParameter(parameter_name)
        if parameter is not None:
            return parameter
    except Exception:
        pass

    try:
        for parameter in room.Parameters:
            if parameter_definition_name(parameter).lower() == target:
                return parameter
    except Exception:
        pass
    return None


def parameter_value_text(parameter, element_document):
    if parameter is None:
        return u""

    values = []
    try:
        value = parameter.AsString()
        if value:
            values.append(safe_text(value))
    except Exception:
        pass

    try:
        value = parameter.AsValueString()
        if value:
            values.append(safe_text(value))
    except Exception:
        pass

    try:
        storage_type = parameter.StorageType
        if storage_type == DB.StorageType.Integer:
            values.append(safe_text(parameter.AsInteger()))
        elif storage_type == DB.StorageType.Double:
            values.append(safe_text(parameter.AsDouble()))
        elif storage_type == DB.StorageType.ElementId:
            element_id = parameter.AsElementId()
            values.append(safe_text(get_id_value(element_id)))
            if not invalid_id(element_id):
                referenced = element_document.GetElement(element_id)
                name = get_element_name(referenced)
                if name:
                    values.append(name)
    except Exception:
        pass

    unique = []
    for value in values:
        text = safe_text(value).strip()
        if text and text not in unique:
            unique.append(text)
    return u" | ".join(unique)


def split_contains_keywords(raw_text):
    """Split a Contains textbox by ';'. Values inside one filter use OR logic."""
    text = safe_text(raw_text).strip().lower()
    if not text:
        return []

    keywords = []
    for part in text.split(u";"):
        keyword = safe_text(part).strip()
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords


def room_matches_filter(room_work_item, filter_definition):
    room = room_work_item.room
    parameter = get_room_parameter(room, filter_definition["parameter_name"])
    value_text = parameter_value_text(parameter, room.Document).lower()

    keywords = filter_definition.get("contains_keywords")
    if keywords is None:
        keywords = split_contains_keywords(filter_definition.get("contains", u""))

    if not keywords:
        return False

    # OR inside one filter: matching any text separated by ';' is accepted.
    for keyword in keywords:
        if keyword in value_text:
            return True
    return False


def filter_room_work_items(room_work_items, filters):
    results = []
    for work_item in room_work_items:
        matched = True
        for filter_definition in filters:
            if not room_matches_filter(work_item, filter_definition):
                matched = False
                break
        if matched:
            results.append(work_item)
    return results


# ============================================================
# DOOR FUNCTIONS
# ============================================================

def get_element_location_point(element):
    try:
        location = element.Location
        if isinstance(location, DB.LocationPoint):
            return location.Point
        if isinstance(location, DB.LocationCurve):
            curve = location.Curve
            if curve is not None:
                return curve.Evaluate(0.5, True)
    except Exception:
        pass
    try:
        transform = element.GetTransform()
        if transform is not None:
            return transform.Origin
    except Exception:
        pass
    return None


def get_door_semantic_key(door):
    family_name = u""
    type_name = u""
    try:
        family_name = safe_text(door.Symbol.Family.Name).strip().lower()
    except Exception:
        pass
    try:
        type_name = get_element_name(door.Symbol).strip().lower()
    except Exception:
        pass
    return u"{0}|{1}".format(family_name, type_name)


def describe_door(door):
    family_name = u"Unknown Family"
    type_name = u"Unknown Type"
    try:
        family_name = safe_text(door.Symbol.Family.Name)
    except Exception:
        pass
    try:
        type_name = get_element_name(door.Symbol)
    except Exception:
        pass
    return u"{0} : {1} [ID {2}]".format(
        family_name, type_name, get_id_value(door.Id)
    )


def get_door_link_axes(door):
    local_x = None
    local_y = None
    try:
        local_x = normalize_xy(door.HandOrientation)
    except Exception:
        pass
    try:
        local_y = normalize_xy(door.FacingOrientation)
    except Exception:
        pass

    try:
        transform = door.GetTransform()
    except Exception:
        transform = None

    if local_x is None and transform is not None:
        local_x = normalize_xy(transform.BasisX)
    if local_y is None and transform is not None:
        local_y = normalize_xy(transform.BasisY)
    if local_x is None:
        local_x = DB.XYZ.BasisX
    if local_y is None or abs(local_x.DotProduct(local_y)) > 0.999:
        local_y = perpendicular_xy(local_x)
    return local_x, local_y


def get_door_host_axes(door, link_item):
    link_x, link_y = get_door_link_axes(door)
    try:
        host_x = normalize_xy(link_item.transform.OfVector(link_x))
    except Exception:
        host_x = link_x
    try:
        host_y = normalize_xy(link_item.transform.OfVector(link_y))
    except Exception:
        host_y = link_y

    if host_x is None:
        host_x = DB.XYZ.BasisX
    if host_y is None or abs(host_x.DotProduct(host_y)) > 0.999:
        host_y = perpendicular_xy(host_x)
    return host_x, host_y


def collect_door_work_items(link_items):
    results = []
    for link_item in link_items:
        collector = (
            DB.FilteredElementCollector(link_item.link_doc)
            .OfCategory(DB.BuiltInCategory.OST_Doors)
            .WhereElementIsNotElementType()
        )
        for door in collector:
            try:
                if not isinstance(door, DB.FamilyInstance):
                    continue
                work_item = DoorWorkItem(link_item, door)
                if work_item.link_point is None or work_item.host_point is None:
                    continue
                results.append(work_item)
            except Exception:
                pass
    return results


def get_document_phases(element_document):
    phases = []
    try:
        for phase in DB.FilteredElementCollector(element_document).OfClass(DB.Phase):
            phases.append(phase)
    except Exception:
        pass
    return phases


def append_room_id(room_ids, room):
    if room is None:
        return
    try:
        room_ids.add(get_id_value(room.Id))
    except Exception:
        pass


def get_door_associated_room_ids(door, phases):
    room_ids = set()

    try:
        append_room_id(room_ids, door.FromRoom)
    except Exception:
        pass
    try:
        append_room_id(room_ids, door.ToRoom)
    except Exception:
        pass

    for phase in phases:
        try:
            append_room_id(room_ids, door.get_FromRoom(phase))
        except Exception:
            pass
        try:
            append_room_id(room_ids, door.get_ToRoom(phase))
        except Exception:
            pass
    return room_ids


def build_direct_room_door_map(link_items, door_items):
    result = {}
    phases_by_link = {}
    for link_item in link_items:
        phases_by_link[link_item.uid] = get_document_phases(link_item.link_doc)

    for door_item in door_items:
        phases = phases_by_link.get(door_item.link_item.uid, [])
        for room_id_value in get_door_associated_room_ids(door_item.door, phases):
            key = u"{0}|{1}".format(door_item.link_item.uid, room_id_value)
            result.setdefault(key, []).append(door_item)
    return result


def room_contains_door_by_points(room, door_item):
    if door_item.link_point is None:
        return False
    link_x, link_y = get_door_link_axes(door_item.door)
    point = door_item.link_point
    test_points = []
    for offset_mm in (100.0, 250.0, 500.0, 800.0):
        offset = mm_to_internal(offset_mm)
        test_points.extend([
            point + link_y.Multiply(offset),
            point - link_y.Multiply(offset),
            point + link_x.Multiply(offset),
            point - link_x.Multiply(offset)
        ])
    for test_point in test_points:
        try:
            if room.IsPointInRoom(test_point):
                return True
        except Exception:
            pass
    return False


def get_room_candidate_doors(room_work_item, direct_map, doors_by_link):
    direct = list(direct_map.get(room_work_item.room_key, []))
    if direct:
        return direct, u"FromRoom/ToRoom"

    fallback = []
    for door_item in doors_by_link.get(room_work_item.link_item.uid, []):
        try:
            if room_contains_door_by_points(room_work_item.room, door_item):
                fallback.append(door_item)
        except Exception:
            pass
    return fallback, u"IsPointInRoom"


def choose_target_door(
        room_work_item,
        direct_map,
        doors_by_link,
        sample_door_key,
        used_door_keys):
    candidates, association_method = get_room_candidate_doors(
        room_work_item, direct_map, doors_by_link
    )
    if not candidates:
        return None, association_method, False

    room_point = room_work_item.host_point

    def score(door_item):
        used_penalty = 1 if door_item.key in used_door_keys else 0
        same_type_penalty = 0 if door_item.semantic_key == sample_door_key else 1
        distance = 0.0
        if room_point is not None and door_item.host_point is not None:
            try:
                distance = points_distance(room_point, door_item.host_point)
            except Exception:
                pass
        return (
            used_penalty,
            same_type_penalty,
            distance,
            door_item.door_id_value
        )

    candidates.sort(key=score)
    selected = candidates[0]
    return selected, association_method, selected.semantic_key == sample_door_key


# ============================================================
# SAMPLE GROUP / DOOR CALIBRATION
# ============================================================

def find_link_item_by_id(link_items, link_id):
    link_id_value = get_id_value(link_id)
    for item in link_items:
        if get_id_value(item.element.Id) == link_id_value:
            return item
    return None


def zoom_to_sample_group(group):
    try:
        set_uidoc_selection([group.Id])
        uidoc.ShowElements(group.Id)
        uidoc.RefreshActiveView()
    except Exception:
        try:
            uidoc.ShowElements(group)
            uidoc.RefreshActiveView()
        except Exception:
            pass


def pick_sample_linked_door(link_items):
    allowed_ids = [get_id_value(item.element.Id) for item in link_items]
    selection_filter = LinkedDoorSelectionFilter(doc, allowed_ids)
    reference = uidoc.Selection.PickObject(
        ObjectType.LinkedElement,
        selection_filter,
        u"Chọn Door trong Revit Link tương ứng với Group mẫu"
    )
    link_item = find_link_item_by_id(link_items, reference.ElementId)
    if link_item is None:
        raise Exception(u"Door được chọn không thuộc Revit Link đang xử lý.")
    door = link_item.link_doc.GetElement(reference.LinkedElementId)
    if not isinstance(door, DB.FamilyInstance) or not category_is(
        door, DB.BuiltInCategory.OST_Doors
    ):
        raise Exception(u"Đối tượng được chọn không phải Door trong Revit Link.")
    return DoorWorkItem(link_item, door)


def restore_start_view():
    try:
        if start_view is not None and uidoc.ActiveView.Id != start_view.Id:
            uidoc.ActiveView = start_view
    except Exception:
        pass


def calibrate_group_to_door(sample_group_item, link_items):
    sample_group = sample_group_item.element
    group_point = get_group_location(sample_group)
    if group_point is None:
        raise Exception(u"Không xác định được điểm chèn của Group mẫu.")

    zoom_to_sample_group(sample_group)

    try:
        sample_door_item = pick_sample_linked_door(link_items)
    except UI.Exceptions.OperationCanceledException:
        restore_start_view()
        return None

    restore_start_view()

    door_point = sample_door_item.host_point
    if door_point is None:
        raise Exception(u"Không xác định được điểm đặt của Door mẫu.")

    door_x, door_y = get_door_host_axes(
        sample_door_item.door, sample_door_item.link_item
    )
    group_x = get_group_basis_x(sample_group)
    delta = group_point - door_point

    local_offset_x = delta.DotProduct(door_x)
    local_offset_y = delta.DotProduct(door_y)
    local_offset_z = delta.Z
    relative_angle = normalize_angle(
        vector_angle_xy(group_x) - vector_angle_xy(door_x)
    )

    return {
        "sample_group": sample_group,
        "sample_door_item": sample_door_item,
        "sample_door_key": sample_door_item.semantic_key,
        "offset_x": local_offset_x,
        "offset_y": local_offset_y,
        "offset_z": local_offset_z,
        "relative_angle": relative_angle
    }


# ============================================================
# USER INTERFACE
# ============================================================

class SettingsWindow(forms.WPFWindow):
    def __init__(
            self,
            xaml_file,
            link_items,
            group_type_items,
            room_work_items,
            room_parameter_items,
            config,
            host_view):
        forms.WPFWindow.__init__(self, xaml_file)

        self.link_items = link_items
        self.group_type_items = group_type_items
        self.room_work_items = room_work_items
        self.room_parameter_items = room_parameter_items
        self.config = config
        self.host_view = host_view
        self.group_instance_items = []
        self.cached_filtered_rooms = None
        self.cached_signature = None
        self.result = None

        self.active_view_text.Text = u"Active View: {0}".format(
            get_element_name(host_view) or safe_text(host_view.ViewType)
        )

        for item in link_items:
            self.links_list.Items.Add(item.display_name)
        for item in group_type_items:
            self.group_combo.Items.Add(item.display_name)
        for item in room_parameter_items:
            self.parameter_1_combo.Items.Add(item.display_name)
            self.parameter_2_combo.Items.Add(item.display_name)

        self.restore_config()
        self.group_combo.SelectionChanged += self.group_type_changed
        self.filter_1_checkbox.Checked += self.filter_setting_changed
        self.filter_1_checkbox.Unchecked += self.filter_setting_changed
        self.filter_2_checkbox.Checked += self.filter_setting_changed
        self.filter_2_checkbox.Unchecked += self.filter_setting_changed
        self.parameter_1_combo.SelectionChanged += self.filter_setting_changed
        self.parameter_2_combo.SelectionChanged += self.filter_setting_changed
        self.contains_1_text.TextChanged += self.filter_setting_changed
        self.contains_2_text.TextChanged += self.filter_setting_changed
        self.scan_button.Click += self.scan_clicked
        self.test_one_checkbox.Checked += self.test_state_changed
        self.test_one_checkbox.Unchecked += self.test_state_changed
        self.run_button.Click += self.run_clicked
        self.cancel_button.Click += self.cancel_clicked

        self.populate_group_instances(
            safe_text(getattr(self.config, "last_sample_group_uid", u""))
        )
        self.update_filter_controls()
        self.update_test_controls()
        self.update_scan_status()

    def restore_config(self):
        last_group_uid = safe_text(getattr(self.config, "last_group_uid", u""))
        self.group_combo.SelectedIndex = self.find_uid_index(
            self.group_type_items, last_group_uid
        )

        self.filter_1_checkbox.IsChecked = bool(
            getattr(self.config, "filter_1_enabled", True)
        )
        self.filter_2_checkbox.IsChecked = bool(
            getattr(self.config, "filter_2_enabled", False)
        )

        parameter_1_name = safe_text(
            getattr(self.config, "filter_1_parameter", u"")
        )
        parameter_2_name = safe_text(
            getattr(self.config, "filter_2_parameter", u"")
        )
        self.parameter_1_combo.SelectedIndex = self.find_parameter_index(parameter_1_name)
        self.parameter_2_combo.SelectedIndex = self.find_parameter_index(parameter_2_name)

        self.contains_1_text.Text = safe_text(
            getattr(self.config, "filter_1_contains", u"")
        )
        self.contains_2_text.Text = safe_text(
            getattr(self.config, "filter_2_contains", u"")
        )
        self.extra_offset_x_text.Text = safe_text(
            getattr(self.config, "extra_offset_x_mm", u"0")
        )
        self.extra_offset_y_text.Text = safe_text(
            getattr(self.config, "extra_offset_y_mm", u"0")
        )
        self.extra_rotation_text.Text = safe_text(
            getattr(self.config, "extra_rotation_deg", u"0")
        )
        self.duplicate_tolerance_text.Text = safe_text(
            getattr(self.config, "duplicate_tolerance_mm", u"10")
        )
        self.skip_duplicate_checkbox.IsChecked = bool(
            getattr(self.config, "skip_duplicate", True)
        )
        self.test_one_checkbox.IsChecked = bool(
            getattr(self.config, "test_one", True)
        )
        self.test_index_text.Text = safe_text(
            getattr(self.config, "test_room_index", u"1")
        )

    def find_uid_index(self, items, uid):
        if uid:
            for index, item in enumerate(items):
                if item.uid == uid:
                    return index
        return 0 if items else -1

    def find_parameter_index(self, parameter_name):
        target = safe_text(parameter_name).strip().lower()
        if target:
            for index, item in enumerate(self.room_parameter_items):
                if item.name.lower() == target:
                    return index
        return 0 if self.room_parameter_items else -1

    def get_selected_group_type_item(self):
        index = self.group_combo.SelectedIndex
        if index < 0 or index >= len(self.group_type_items):
            return None
        return self.group_type_items[index]

    def get_selected_group_instance_item(self):
        index = self.sample_group_combo.SelectedIndex
        if index < 0 or index >= len(self.group_instance_items):
            return None
        return self.group_instance_items[index]

    def get_parameter_item(self, combo):
        index = combo.SelectedIndex
        if index < 0 or index >= len(self.room_parameter_items):
            return None
        return self.room_parameter_items[index]

    def populate_group_instances(self, preferred_uid=None):
        self.sample_group_combo.Items.Clear()
        self.group_instance_items = []
        group_type_item = self.get_selected_group_type_item()
        if group_type_item is None:
            self.sample_group_status.Text = u"Chưa chọn Model Group Type."
            return

        self.group_instance_items = collect_group_instances(group_type_item.element)
        for item in self.group_instance_items:
            self.sample_group_combo.Items.Add(item.display_name)

        selected_index = self.find_uid_index(
            self.group_instance_items, safe_text(preferred_uid)
        )
        self.sample_group_combo.SelectedIndex = selected_index

        if self.group_instance_items:
            self.sample_group_status.Text = (
                u"Có {0} Group instance. Khi chạy, tool sẽ zoom tới Group mẫu rồi yêu cầu chọn Door."
            ).format(len(self.group_instance_items))
        else:
            self.sample_group_status.Text = (
                u"Group Type này chưa có instance mẫu. Hãy đặt thủ công 1 Group tại một Door trước."
            )

    def group_type_changed(self, sender, args):
        self.populate_group_instances(None)

    def filter_setting_changed(self, sender, args):
        self.cached_filtered_rooms = None
        self.cached_signature = None
        self.update_filter_controls()
        self.update_scan_status()

    def update_filter_controls(self):
        enabled_1 = self.filter_1_checkbox.IsChecked is True
        enabled_2 = self.filter_2_checkbox.IsChecked is True
        self.parameter_1_combo.IsEnabled = enabled_1
        self.contains_1_text.IsEnabled = enabled_1
        self.parameter_2_combo.IsEnabled = enabled_2
        self.contains_2_text.IsEnabled = enabled_2

    def current_filter_signature(self):
        values = []
        for checkbox, combo, textbox in (
            (self.filter_1_checkbox, self.parameter_1_combo, self.contains_1_text),
            (self.filter_2_checkbox, self.parameter_2_combo, self.contains_2_text)
        ):
            item = self.get_parameter_item(combo)
            values.append(u"{0}|{1}|{2}".format(
                checkbox.IsChecked is True,
                item.name if item is not None else u"",
                u";".join(split_contains_keywords(textbox.Text))
            ))
        return u"||".join(values)

    def build_filters(self):
        filters = []
        for index, checkbox, combo, textbox in (
            (1, self.filter_1_checkbox, self.parameter_1_combo, self.contains_1_text),
            (2, self.filter_2_checkbox, self.parameter_2_combo, self.contains_2_text)
        ):
            if checkbox.IsChecked is not True:
                continue
            parameter_item = self.get_parameter_item(combo)
            if parameter_item is None:
                raise ValueError(u"Hãy chọn Parameter cho bộ lọc {0}.".format(index))
            contains_text = safe_text(textbox.Text).strip()
            contains_keywords = split_contains_keywords(contains_text)
            if not contains_keywords:
                raise ValueError(
                    u"Hãy nhập ít nhất một nội dung Contains hợp lệ cho bộ lọc {0}.".format(index)
                )
            filters.append({
                "parameter_name": parameter_item.name,
                "contains": contains_text,
                "contains_keywords": contains_keywords
            })

        if not filters:
            raise ValueError(u"Phải tick ít nhất một trong hai bộ lọc Room.")
        return filters

    def get_filtered_rooms(self):
        filters = self.build_filters()
        signature = self.current_filter_signature()
        if (
            self.cached_filtered_rooms is not None and
            self.cached_signature == signature
        ):
            return list(self.cached_filtered_rooms), filters

        filtered = filter_room_work_items(self.room_work_items, filters)
        self.cached_filtered_rooms = list(filtered)
        self.cached_signature = signature
        return filtered, filters

    def update_scan_status(self):
        if self.cached_filtered_rooms is None:
            self.match_count_text.Text = (
                u"Có {0} Room hợp lệ trong Active View. Chưa áp dụng bộ lọc."
            ).format(len(self.room_work_items))
        else:
            self.match_count_text.Text = u"Tìm thấy {0} Room khớp.".format(
                len(self.cached_filtered_rooms)
            )
        self.update_test_hint()

    def scan_clicked(self, sender, args):
        try:
            filtered, unused_filters = self.get_filtered_rooms()
            self.match_count_text.Text = u"Tìm thấy {0} Room khớp.".format(len(filtered))
            self.update_test_hint()
        except ValueError as ex:
            self.match_count_text.Text = safe_text(ex)

    def test_state_changed(self, sender, args):
        self.update_test_controls()

    def update_test_controls(self):
        enabled = self.test_one_checkbox.IsChecked is True
        self.test_index_label.IsEnabled = enabled
        self.test_index_text.IsEnabled = enabled
        self.test_index_hint.IsEnabled = enabled
        self.update_test_hint()

    def update_test_hint(self):
        if self.cached_filtered_rooms is not None:
            self.test_index_hint.Text = u"Nhập từ 1 đến {0}".format(
                len(self.cached_filtered_rooms)
            )
        else:
            self.test_index_hint.Text = u"Nhập theo thứ tự danh sách Room sau khi lọc"

    def run_clicked(self, sender, args):
        group_type_item = self.get_selected_group_type_item()
        sample_group_item = self.get_selected_group_instance_item()
        if group_type_item is None:
            self.match_count_text.Text = u"Hãy chọn Model Group Type."
            return
        if sample_group_item is None:
            self.match_count_text.Text = (
                u"Hãy chọn Group mẫu. Nếu chưa có, đặt thủ công một Group tại Door mẫu."
            )
            return

        try:
            filtered_rooms, filters = self.get_filtered_rooms()
            extra_offset_x_mm = parse_number(
                self.extra_offset_x_text.Text, u"Hiệu chỉnh Offset X"
            )
            extra_offset_y_mm = parse_number(
                self.extra_offset_y_text.Text, u"Hiệu chỉnh Offset Y"
            )
            extra_rotation_deg = parse_number(
                self.extra_rotation_text.Text, u"Hiệu chỉnh góc"
            )
            duplicate_tolerance_mm = parse_number(
                self.duplicate_tolerance_text.Text,
                u"Dung sai Group trùng",
                0.0
            )

            test_one = self.test_one_checkbox.IsChecked is True
            test_room_index = 1
            if test_one:
                test_room_index = parse_integer(
                    self.test_index_text.Text,
                    u"Room thứ",
                    1,
                    len(filtered_rooms) if filtered_rooms else None
                )
        except ValueError as ex:
            self.match_count_text.Text = safe_text(ex)
            return

        filter_slots = {
            1: {
                "enabled": self.filter_1_checkbox.IsChecked is True,
                "parameter_name": (
                    self.get_parameter_item(self.parameter_1_combo).name
                    if self.get_parameter_item(self.parameter_1_combo) is not None else u""
                ),
                "contains": safe_text(self.contains_1_text.Text).strip()
            },
            2: {
                "enabled": self.filter_2_checkbox.IsChecked is True,
                "parameter_name": (
                    self.get_parameter_item(self.parameter_2_combo).name
                    if self.get_parameter_item(self.parameter_2_combo) is not None else u""
                ),
                "contains": safe_text(self.contains_2_text.Text).strip()
            }
        }

        self.result = {
            "config": self.config,
            "link_items": self.link_items,
            "group_type_item": group_type_item,
            "sample_group_item": sample_group_item,
            "filtered_rooms": list(filtered_rooms),
            "filters": filters,
            "filter_slots": filter_slots,
            "extra_offset_x_mm": extra_offset_x_mm,
            "extra_offset_y_mm": extra_offset_y_mm,
            "extra_rotation_deg": extra_rotation_deg,
            "skip_duplicate": self.skip_duplicate_checkbox.IsChecked is True,
            "duplicate_tolerance_mm": duplicate_tolerance_mm,
            "test_one": test_one,
            "test_room_index": test_room_index
        }
        self.Close()

    def cancel_clicked(self, sender, args):
        self.result = None
        self.Close()


# ============================================================
# SAVE CONFIG
# ============================================================

def save_config(config, settings):
    config.last_group_uid = settings["group_type_item"].uid
    config.last_sample_group_uid = settings["sample_group_item"].uid

    filter_slots = settings.get("filter_slots", {})
    slot_1 = filter_slots.get(1, {})
    slot_2 = filter_slots.get(2, {})

    config.filter_1_enabled = bool(slot_1.get("enabled", False))
    config.filter_2_enabled = bool(slot_2.get("enabled", False))
    config.filter_1_parameter = safe_text(slot_1.get("parameter_name", u""))
    config.filter_2_parameter = safe_text(slot_2.get("parameter_name", u""))
    config.filter_1_contains = safe_text(slot_1.get("contains", u""))
    config.filter_2_contains = safe_text(slot_2.get("contains", u""))

    config.extra_offset_x_mm = safe_text(settings["extra_offset_x_mm"])
    config.extra_offset_y_mm = safe_text(settings["extra_offset_y_mm"])
    config.extra_rotation_deg = safe_text(settings["extra_rotation_deg"])
    config.skip_duplicate = settings["skip_duplicate"]
    config.duplicate_tolerance_mm = safe_text(settings["duplicate_tolerance_mm"])
    config.test_one = settings["test_one"]
    config.test_room_index = safe_text(settings["test_room_index"])
    script.save_config()


# ============================================================
# MAIN PLACEMENT
# ============================================================

def build_context_lines(settings, calibration=None):
    filters_text = []
    for filter_definition in settings["filters"]:
        keywords = filter_definition.get("contains_keywords") or split_contains_keywords(
            filter_definition.get("contains", u"")
        )
        filters_text.append(u"{0} CONTAINS ANY OF [{1}]".format(
            filter_definition["parameter_name"],
            u"; ".join(keywords)
        ))

    lines = [
        u"Active View nguồn: {0}".format(
            get_element_name(start_view) or safe_text(start_view.ViewType)
        ),
        u"Revit Link: {0}".format(len(settings["link_items"])),
        u"Model Group Type: {0}".format(
            settings["group_type_item"].display_name
        ),
        u"Group mẫu: ID {0}".format(
            settings["sample_group_item"].id_value
        ),
        u"Bộ lọc Room (AND): {0}".format(u"; ".join(filters_text)),
        u"Room khớp: {0}".format(len(settings["filtered_rooms"])),
        u"Hiệu chỉnh thêm X / Y: {0:+.1f} / {1:+.1f} mm".format(
            settings["extra_offset_x_mm"], settings["extra_offset_y_mm"]
        ),
        u"Hiệu chỉnh thêm góc: {0:+.2f}°".format(
            settings["extra_rotation_deg"]
        )
    ]

    if calibration is not None:
        sample_door_item = calibration["sample_door_item"]
        lines.extend([
            u"Door mẫu: {0} | {1}".format(
                sample_door_item.link_item.display_name,
                describe_door(sample_door_item.door)
            ),
            u"Offset Group mẫu so với Door: X={0:+.1f}; Y={1:+.1f}; Z={2:+.1f} mm".format(
                internal_to_mm(calibration["offset_x"]),
                internal_to_mm(calibration["offset_y"]),
                internal_to_mm(calibration["offset_z"])
            ),
            u"Góc Group mẫu so với trục X của Door: {0}°".format(
                format_angle(calibration["relative_angle"])
            )
        ])
    return lines


def run_tool(settings, startup_notices=None):
    startup_notices = list(startup_notices or [])
    filtered_rooms = list(settings["filtered_rooms"])

    if not filtered_rooms:
        show_report(
            u"Không tìm thấy Room phù hợp",
            summary=build_context_lines(settings),
            notices=startup_notices + [
                u"Kiểm tra parameter, nội dung Contains, View Range, Crop Region và Room trong Link."
            ]
        )
        return

    rooms_to_process = list(filtered_rooms)
    if settings["test_one"]:
        index = settings["test_room_index"]
        if index < 1 or index > len(filtered_rooms):
            show_report(
                u"Room thứ không hợp lệ",
                summary=build_context_lines(settings) + [
                    u"Hãy nhập từ 1 đến {0}.".format(len(filtered_rooms))
                ],
                notices=startup_notices
            )
            return
        rooms_to_process = [filtered_rooms[index - 1]]
        startup_notices.append(
            u"Chế độ thử: Room thứ {0}/{1} = {2}.".format(
                index,
                len(filtered_rooms),
                describe_room(filtered_rooms[index - 1].room)
            )
        )

    try:
        calibration = calibrate_group_to_door(
            settings["sample_group_item"], settings["link_items"]
        )
    except Exception:
        restore_start_view()
        show_report(
            u"Không xác lập được Group mẫu và Door mẫu",
            summary=build_context_lines(settings),
            notices=startup_notices,
            traceback_text=traceback.format_exc()
        )
        return

    if calibration is None:
        show_report(
            u"Đã hủy chọn Door mẫu",
            summary=build_context_lines(settings),
            notices=startup_notices + [u"Chưa có Group nào được tạo."]
        )
        return

    door_items = collect_door_work_items(settings["link_items"])
    direct_map = build_direct_room_door_map(settings["link_items"], door_items)
    doors_by_link = {}
    for door_item in door_items:
        doors_by_link.setdefault(door_item.link_item.uid, []).append(door_item)

    group_type = settings["group_type_item"].element
    extra_offset_x = mm_to_internal(settings["extra_offset_x_mm"])
    extra_offset_y = mm_to_internal(settings["extra_offset_y_mm"])
    extra_rotation = math.radians(settings["extra_rotation_deg"])
    duplicate_tolerance = mm_to_internal(settings["duplicate_tolerance_mm"])

    existing_points = []
    if settings["skip_duplicate"]:
        existing_points = collect_existing_group_points(group_type)

    created_records = []
    failed_items = []
    used_door_keys = set()
    duplicate_count = 0
    fallback_association_count = 0
    fallback_door_type_count = 0
    cancelled = False

    transaction_name = u"Test Place Group By Linked Room"
    if not settings["test_one"]:
        transaction_name = u"Place Groups By Linked Rooms"
    transaction = DB.Transaction(doc, transaction_name)

    try:
        transaction.Start()
        total = len(rooms_to_process)

        with forms.ProgressBar(
            title=u"Đang đặt Group theo Room... {value} / {max_value}",
            cancellable=True,
            step=1
        ) as progress:
            for index, room_work_item in enumerate(rooms_to_process):
                if progress.cancelled:
                    cancelled = True
                    break

                sub_transaction = DB.SubTransaction(doc)
                sub_transaction.Start()
                try:
                    target_door_item, association_method, same_door_type = choose_target_door(
                        room_work_item,
                        direct_map,
                        doors_by_link,
                        calibration["sample_door_key"],
                        used_door_keys
                    )
                    if target_door_item is None:
                        raise Exception(
                            u"Không tìm thấy Door liên kết với Room bằng FromRoom/ToRoom hoặc IsPointInRoom."
                        )

                    if association_method == u"IsPointInRoom":
                        fallback_association_count += 1
                    if not same_door_type:
                        fallback_door_type_count += 1

                    door_x, door_y = get_door_host_axes(
                        target_door_item.door,
                        target_door_item.link_item
                    )
                    door_point = target_door_item.host_point

                    total_offset_x = calibration["offset_x"] + extra_offset_x
                    total_offset_y = calibration["offset_y"] + extra_offset_y
                    host_anchor = (
                        door_point
                        + door_x.Multiply(total_offset_x)
                        + door_y.Multiply(total_offset_y)
                        + DB.XYZ.BasisZ.Multiply(calibration["offset_z"])
                    )

                    if settings["skip_duplicate"] and has_nearby_point(
                        host_anchor, existing_points, duplicate_tolerance
                    ):
                        duplicate_count += 1
                        sub_transaction.RollBack()
                        used_door_keys.add(target_door_item.key)
                    else:
                        target_angle = (
                            vector_angle_xy(door_x)
                            + calibration["relative_angle"]
                            + extra_rotation
                        )

                        new_group = doc.Create.PlaceGroup(host_anchor, group_type)
                        if new_group is None:
                            raise Exception(u"Revit không tạo được Group.")

                        if abs(target_angle) > 0.0000001:
                            axis = DB.Line.CreateBound(
                                host_anchor,
                                host_anchor + DB.XYZ.BasisZ
                            )
                            DB.ElementTransformUtils.RotateElement(
                                doc, new_group.Id, axis, target_angle
                            )

                        group_id = new_group.Id
                        group_id_value = get_id_value(group_id)
                        sub_transaction.Commit()

                        existing_points.append(host_anchor)
                        used_door_keys.add(target_door_item.key)
                        created_records.append({
                            "group_id": group_id,
                            "group_id_value": group_id_value,
                            "room": describe_room(room_work_item.room),
                            "door": describe_door(target_door_item.door),
                            "link": room_work_item.link_item.display_name,
                            "point": format_point_mm(host_anchor),
                            "angle": format_angle(target_angle),
                            "door_match": (
                                u"Cùng Family/Type mẫu; {0}".format(association_method)
                                if same_door_type else
                                u"Door khác Type; {0}".format(association_method)
                            )
                        })

                except Exception as item_error:
                    try:
                        if sub_transaction.GetStatus() == DB.TransactionStatus.Started:
                            sub_transaction.RollBack()
                    except Exception:
                        pass
                    failed_items.append(
                        u"{0} | {1}: {2}".format(
                            room_work_item.link_item.display_name,
                            describe_room(room_work_item.room),
                            safe_text(item_error)
                        )
                    )

                progress.update_progress(index + 1, total)

            if progress.cancelled:
                cancelled = True

        if cancelled:
            transaction.RollBack()
            show_report(
                u"Đã hủy đặt Group",
                summary=build_context_lines(settings, calibration) + [
                    u"Toàn bộ Group của lần chạy đã được rollback."
                ],
                notices=startup_notices,
                failed=failed_items
            )
            return

        transaction.Commit()

    except Exception:
        try:
            if transaction.GetStatus() == DB.TransactionStatus.Started:
                transaction.RollBack()
        except Exception:
            pass
        show_report(
            u"Tool không thể hoàn thành",
            summary=build_context_lines(settings, calibration) + [
                u"Toàn bộ thay đổi của lần chạy đã được rollback."
            ],
            notices=startup_notices,
            failed=failed_items,
            traceback_text=traceback.format_exc()
        )
        return

    summary = build_context_lines(settings, calibration) + [
        u"Room đã xử lý trong lần chạy: {0}".format(len(rooms_to_process)),
        u"Group đã tạo: {0}".format(len(created_records)),
        u"Bỏ qua vì Group cùng Type đã có gần vị trí: {0}".format(duplicate_count),
        u"Room không xử lý được: {0}".format(len(failed_items))
    ]

    if fallback_association_count:
        startup_notices.append(
            u"Có {0} Room phải nhận diện Door bằng IsPointInRoom vì FromRoom/ToRoom không trả kết quả.".format(
                fallback_association_count
            )
        )
    if fallback_door_type_count:
        startup_notices.append(
            u"Có {0} Room không có Door cùng Family/Type với Door mẫu; tool đã dùng Door liên kết gần Room nhất.".format(
                fallback_door_type_count
            )
        )

    if settings["test_one"]:
        if created_records:
            title = u"Đã tạo 1 Group kiểm tra"
            startup_notices.extend([
                u"Kiểm tra vị trí và góc bằng liên kết Group trong bảng.",
                u"Khi đúng, chạy lại và bỏ tick chế độ chỉ tạo 1 Room."
            ])
        else:
            title = u"Không tạo được Group kiểm tra"
    else:
        title = u"Đã hoàn thành đặt Group theo Room"

    show_report(
        title,
        summary=summary,
        notices=startup_notices,
        created=created_records,
        failed=failed_items
    )


# ============================================================
# STARTUP
# ============================================================

if doc is None:
    show_report(u"Không thể chạy tool", summary=[u"Không có Revit document đang mở."])
    script.exit()

if doc.IsFamilyDocument:
    show_report(
        u"Không thể chạy tool",
        summary=[u"Tool chỉ chạy trong Project Document."]
    )
    script.exit()

if start_view is None or start_view.IsTemplate:
    show_report(
        u"Không thể chạy tool",
        summary=[u"Hãy mở một model view hợp lệ trước khi chạy tool."]
    )
    script.exit()

selected_links = pick_link_items()
if not selected_links:
    script.exit()

selected_links, hidden_links = keep_visible_links(selected_links, start_view)
startup_notices = []

if not selected_links:
    show_report(
        u"Không có Revit Link hợp lệ",
        summary=[u"Các Link đã chọn không hiển thị trong Active View."],
        notices=[u"Bật Link trong Visibility/Graphics hoặc chọn Link khác."]
    )
    script.exit()

if hidden_links:
    startup_notices.append(
        u"Các Link không hiển thị trong Active View đã bị bỏ qua: {0}".format(
            u"; ".join([item.display_name for item in hidden_links])
        )
    )

group_type_items = collect_model_group_types()
if not group_type_items:
    show_report(
        u"Không có Model Group Type",
        summary=[u"Host model chưa có Model Group Type."],
        notices=[u"Tạo hoặc load Model Group trước khi chạy tool."]
    )
    script.exit()

room_work_items = collect_visible_room_work_items(selected_links, start_view)
if not room_work_items:
    show_report(
        u"Không tìm thấy Room hợp lệ",
        summary=[
            u"Không có Room có Area > 0 thuộc các Link đã chọn trong Active View."
        ],
        notices=[
            u"Kiểm tra Crop Region, View Range, linked view, phase và trạng thái Room trong file Link."
        ]
    )
    script.exit()

room_parameter_items = collect_room_parameter_items(room_work_items)
if not room_parameter_items:
    show_report(
        u"Không đọc được Room Parameter",
        summary=[u"Không có parameter khả dụng trên các Room đã thu thập."]
    )
    script.exit()

config = script.get_config()
xaml_path = script.get_bundle_file("ui.xaml")
if not xaml_path or not os.path.exists(xaml_path):
    show_report(
        u"Thiếu file giao diện",
        summary=[u"Không tìm thấy ui.xaml trong thư mục pushbutton."]
    )
    script.exit()

window = SettingsWindow(
    xaml_path,
    selected_links,
    group_type_items,
    room_work_items,
    room_parameter_items,
    config,
    start_view
)
window.ShowDialog()

if window.result is None:
    script.exit()

save_config(config, window.result)
run_tool(window.result, startup_notices)
