# -*- coding: utf-8 -*-
"""
PLACE MODEL GROUPS BY DETAIL GROUPS IN ACTIVE VIEW
==================================================

IronPython 2.7 / pyRevit / Revit 2019+

PURPOSE
-------
Replace the old workflow:

    Linked Room -> Door -> place Model Group

with:

    Detail Group in Active View -> place Model Group

TARGET DETAIL GROUP SELECTION
-----------------------------
Option 1 - NAME CONTAINS
    - Enter one or more text fragments separated by ';'.
    - The tool finds Detail Group instances in Active View whose Detail Group
      Type name contains any fragment.
    - The matching Detail Group nearest to the selected Model Group sample is
      used as the calibration Detail Group.

Option 2 - PICK + GEOMETRY SCAN
    - Pick one Detail Group in Active View.
    - The tool fingerprints the picked Detail Group geometry.
    - It scans Detail Groups in Active View and keeps geometrically similar
      groups, even when their Group Type names are different.

CALIBRATION
-----------
The tool learns the relative position/rotation between:
    Model Group sample <-> calibration Detail Group

Then it applies the same relation to every target Detail Group.

NOTES
-----
- Output is still a MODEL GROUP. Detail Groups are only placement anchors.
- Detail Group geometry matching is based on member category + local bounding
  geometry + curve geometry. It is translation/instance-rotation independent.
- Cancel during placement rolls back the entire run.
- Duplicate checking uses existing Model Groups of the selected Type.
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
    if element_id is None:
        return None
    try:
        return int(element_id.Value)
    except Exception:
        try:
            return int(element_id.IntegerValue)
        except Exception:
            return None


def invalid_id(element_id):
    if element_id is None:
        return True
    value = get_id_value(element_id)
    invalid_value = get_id_value(DB.ElementId.InvalidElementId)
    return value is None or value == invalid_value


def same_id(id_a, id_b):
    a = get_id_value(id_a)
    b = get_id_value(id_b)
    return a is not None and b is not None and a == b


def get_element_name(element):
    if element is None:
        return u""
    try:
        value = element.Name
        if value:
            return safe_text(value)
    except Exception:
        pass

    for bip_name in (
        "SYMBOL_NAME_PARAM",
        "ALL_MODEL_TYPE_NAME",
        "ELEM_TYPE_PARAM"
    ):
        try:
            bip = getattr(DB.BuiltInParameter, bip_name)
            parameter = element.get_Parameter(bip)
            if parameter is not None:
                value = parameter.AsString()
                if value:
                    return safe_text(value)
        except Exception:
            pass
    return u""


def category_is(element, built_in_category):
    try:
        return (
            element is not None and
            element.Category is not None and
            get_id_value(element.Category.Id) == int(built_in_category)
        )
    except Exception:
        return False


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
        numeric = float(value_text)
        value = int(numeric)
        if abs(numeric - value) > 0.0000001:
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
    try:
        vector_xy = DB.XYZ(vector.X, vector.Y, 0.0)
        if vector_xy.GetLength() < 0.0000001:
            return None
        return vector_xy.Normalize()
    except Exception:
        return None


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


def points_distance(point_a, point_b):
    return (point_a - point_b).GetLength()


def format_point_mm(point):
    if point is None:
        return u"-"
    return u"{0:.1f}; {1:.1f}; {2:.1f}".format(
        internal_to_mm(point.X),
        internal_to_mm(point.Y),
        internal_to_mm(point.Z)
    )


def split_keywords(raw_text):
    text = safe_text(raw_text).strip().lower()
    if not text:
        return []
    results = []
    for part in text.split(u";"):
        item = safe_text(part).strip()
        if item and item not in results:
            results.append(item)
    return results


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

def show_report(title, summary=None, notices=None, created=None, failed=None,
                traceback_text=None):
    summary = summary or []
    notices = notices or []
    created = created or []
    failed = failed or []

    try:
        output.set_title(u"Place Model Groups By Detail Groups")
    except Exception:
        pass

    output.print_md(u"# Place Model Groups By Detail Groups")
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
        output.print_md(u"### Model Group đã tạo")
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
                safe_text(item.get("detail_group", u"")),
                safe_text(item.get("detail_type", u"")),
                safe_text(item.get("point", u"")),
                safe_text(item.get("angle", u"")),
                safe_text(item.get("mode", u""))
            ])

        try:
            output.print_table(
                table_data=rows,
                columns=[
                    u"STT", u"Chọn / Zoom", u"Detail Group đích",
                    u"Detail Group Type", u"Điểm đặt X;Y;Z (mm)",
                    u"Góc (°)", u"Cách chọn"
                ]
            )
        except Exception:
            for row in rows:
                output.print_md(
                    u"{0}. {1} | {2} | {3} | {4} | {5}° | {6}".format(*row)
                )

    if failed:
        output.print_md(u"### Detail Group không xử lý được")
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
        u"Active View khi mở tool: **{0}**".format(
            get_element_name(start_view) if start_view is not None else u"Không xác định"
        )
    )


# ============================================================
# GROUP TYPES / INSTANCES
# ============================================================

def is_model_group_type(group_type):
    return category_is(group_type, DB.BuiltInCategory.OST_IOSModelGroups)


def is_detail_group(group):
    return isinstance(group, DB.Group) and category_is(
        group, DB.BuiltInCategory.OST_IOSDetailGroups
    )


class GroupTypeItem(object):
    def __init__(self, group_type):
        self.element = group_type
        self.uid = safe_text(group_type.UniqueId)
        self.display_name = get_element_name(group_type) or u"Unnamed Model Group Type"


class GroupInstanceItem(object):
    def __init__(self, group):
        self.element = group
        self.uid = safe_text(group.UniqueId)
        self.id_value = get_id_value(group.Id)
        point = get_group_location(group)
        self.display_name = u"ID {0} | {1}".format(
            self.id_value,
            format_point_mm(point)
        )


class DetailGroupItem(object):
    def __init__(self, group, view):
        self.element = group
        self.uid = safe_text(group.UniqueId)
        self.id_value = get_id_value(group.Id)
        self.type_id_value = None
        self.type_name = u"Unnamed Detail Group"
        try:
            group_type = group.GroupType
            self.type_id_value = get_id_value(group_type.Id)
            self.type_name = get_element_name(group_type) or self.type_name
        except Exception:
            pass
        self.anchor = get_detail_group_anchor(group, view)
        self.display_name = u"{0} | ID {1} | {2}".format(
            self.type_name,
            self.id_value,
            format_point_mm(self.anchor)
        )


def collect_model_group_types():
    results = []
    for group_type in DB.FilteredElementCollector(doc).OfClass(DB.GroupType):
        try:
            if is_model_group_type(group_type):
                results.append(GroupTypeItem(group_type))
        except Exception:
            pass
    results.sort(key=lambda item: item.display_name.lower())
    return results


def collect_model_group_instances(group_type, view=None, preferred_uid=None):
    """Collect ALL model-group instances of the selected type in the project.

    IMPORTANT:
    - Model Group SAMPLE is a calibration/reference object, so it must NOT be
      restricted to the Active View.
    - Only target Detail Groups are scoped to the Active View.
    - ``view`` is intentionally kept in the function signature for backwards
      compatibility with existing UI calls, but is not used for filtering.
    - The previously used sample is kept at the top when it still exists.
    """
    results = []
    used_ids = set()
    target_id = get_id_value(group_type.Id)
    preferred_uid = safe_text(preferred_uid).strip()

    # ALWAYS search the whole project for Model Group instances.
    collector = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.Group)
        .WhereElementIsNotElementType()
    )

    for group in collector:
        try:
            if not category_is(group, DB.BuiltInCategory.OST_IOSModelGroups):
                continue
            if get_id_value(group.GroupType.Id) != target_id:
                continue
            group_id_value = get_id_value(group.Id)
            if group_id_value in used_ids:
                continue
            used_ids.add(group_id_value)
            results.append(GroupInstanceItem(group))
        except Exception:
            pass

    # Extra safety: resolve the saved sample directly by UniqueId. Normally it
    # is already present in the project-wide collector, but this preserves the
    # sample even if collector behaviour changes in a future Revit version.
    if preferred_uid:
        try:
            preferred_group = doc.GetElement(preferred_uid)
        except Exception:
            preferred_group = None

        try:
            if (
                isinstance(preferred_group, DB.Group) and
                category_is(preferred_group, DB.BuiltInCategory.OST_IOSModelGroups) and
                get_id_value(preferred_group.GroupType.Id) == target_id
            ):
                preferred_id_value = get_id_value(preferred_group.Id)
                if preferred_id_value not in used_ids:
                    used_ids.add(preferred_id_value)
                    results.append(GroupInstanceItem(preferred_group))
        except Exception:
            pass

    # Keep previous sample at the top. Remaining items are sorted by ID.
    results.sort(key=lambda item: (
        0 if preferred_uid and item.uid == preferred_uid else 1,
        item.id_value
    ))
    return results


def collect_detail_group_items(view):
    results = []
    collector = (
        DB.FilteredElementCollector(doc, view.Id)
        .OfClass(DB.Group)
        .WhereElementIsNotElementType()
    )
    for group in collector:
        try:
            if not is_detail_group(group):
                continue
            try:
                owner_view_id = group.OwnerViewId
                if not invalid_id(owner_view_id) and not same_id(owner_view_id, view.Id):
                    continue
            except Exception:
                pass
            results.append(DetailGroupItem(group, view))
        except Exception:
            pass

    results.sort(key=lambda item: (item.type_name.lower(), item.id_value))
    return results


def get_group_location(group):
    try:
        location = group.Location
        if isinstance(location, DB.LocationPoint):
            return location.Point
    except Exception:
        pass
    try:
        transform = group.GetTransform()
        if transform is not None:
            return transform.Origin
    except Exception:
        pass
    try:
        bbox = group.get_BoundingBox(None)
        if bbox is not None:
            corners = bbox_world_corners(bbox)
            if corners:
                return average_points(corners)
    except Exception:
        pass
    return None


def get_group_axes(group):
    x_axis = None
    y_axis = None
    try:
        transform = group.GetTransform()
        x_axis = normalize_xy(transform.BasisX)
        y_axis = normalize_xy(transform.BasisY)
    except Exception:
        pass

    if x_axis is None:
        try:
            location = group.Location
            if isinstance(location, DB.LocationPoint):
                angle = float(location.Rotation)
                x_axis = DB.XYZ(math.cos(angle), math.sin(angle), 0.0)
        except Exception:
            pass

    if x_axis is None:
        x_axis = DB.XYZ.BasisX
    if y_axis is None or abs(x_axis.DotProduct(y_axis)) > 0.999:
        y_axis = perpendicular_xy(x_axis)
    return x_axis, y_axis


def collect_existing_model_group_points(group_type):
    points = []
    for item in collect_model_group_instances(group_type, None):
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
# DETAIL GROUP GEOMETRY
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


def bbox_world_corners(bbox):
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


def average_points(points):
    if not points:
        return None
    count = float(len(points))
    return DB.XYZ(
        sum(point.X for point in points) / count,
        sum(point.Y for point in points) / count,
        sum(point.Z for point in points) / count
    )


def get_group_inverse_transform(group):
    try:
        return group.GetTransform().Inverse
    except Exception:
        return DB.Transform.Identity


def transform_points(transform, points):
    results = []
    for point in points or []:
        try:
            results.append(transform.OfPoint(point))
        except Exception:
            results.append(point)
    return results


def element_bbox_world_points(element, view):
    bbox = None
    try:
        bbox = element.get_BoundingBox(view)
    except Exception:
        pass
    if bbox is None:
        try:
            bbox = element.get_BoundingBox(None)
        except Exception:
            pass
    return bbox_world_corners(bbox)


def get_element_curve(element):
    try:
        location = element.Location
        if isinstance(location, DB.LocationCurve):
            curve = location.Curve
            if curve is not None:
                return curve
    except Exception:
        pass
    try:
        curve = element.GeometryCurve
        if curve is not None:
            return curve
    except Exception:
        pass
    try:
        curve = element.Curve
        if curve is not None:
            return curve
    except Exception:
        pass
    return None


def curve_sample_points(curve):
    if curve is None:
        return []
    points = []
    try:
        points.append(curve.GetEndPoint(0))
        points.append(curve.GetEndPoint(1))
    except Exception:
        pass
    try:
        points.append(curve.Evaluate(0.5, True))
    except Exception:
        pass
    return points


def element_class_name(element):
    try:
        return safe_text(element.GetType().Name)
    except Exception:
        return safe_text(type(element).__name__)


def get_member_local_data(group, view):
    inverse = get_group_inverse_transform(group)
    raw_items = []
    all_points = []

    try:
        member_ids = list(group.GetMemberIds())
    except Exception:
        member_ids = []

    for member_id in member_ids:
        member = doc.GetElement(member_id)
        if member is None:
            continue

        category_id = 0
        try:
            if member.Category is not None:
                category_id = get_id_value(member.Category.Id) or 0
        except Exception:
            pass

        local_bbox_points = transform_points(
            inverse, element_bbox_world_points(member, view)
        )

        curve = get_element_curve(member)
        local_curve_points = transform_points(
            inverse, curve_sample_points(curve)
        )

        local_location_point = None
        try:
            location = member.Location
            if isinstance(location, DB.LocationPoint):
                local_location_point = inverse.OfPoint(location.Point)
        except Exception:
            pass

        points = []
        points.extend(local_bbox_points)
        points.extend(local_curve_points)
        if local_location_point is not None:
            points.append(local_location_point)

        if points:
            all_points.extend(points)

        raw_items.append({
            "category_id": category_id,
            "class_name": element_class_name(member),
            "bbox_points": local_bbox_points,
            "curve_points": local_curve_points,
            "location_point": local_location_point,
            "all_points": points
        })

    # Fallback for unusual groups whose members return no usable geometry.
    if not all_points:
        group_points = transform_points(
            inverse, element_bbox_world_points(group, view)
        )
        all_points.extend(group_points)

    return raw_items, all_points


def points_bounds(points):
    if not points:
        return None
    xs = [p.X for p in points]
    ys = [p.Y for p in points]
    zs = [p.Z for p in points]
    return (
        min(xs), max(xs),
        min(ys), max(ys),
        min(zs), max(zs)
    )


def get_detail_group_anchor(group, view):
    raw_items, all_local_points = get_member_local_data(group, view)
    bounds = points_bounds(all_local_points)
    if bounds is None:
        return get_group_location(group)

    local_center = DB.XYZ(
        (bounds[0] + bounds[1]) * 0.5,
        (bounds[2] + bounds[3]) * 0.5,
        (bounds[4] + bounds[5]) * 0.5
    )
    try:
        return group.GetTransform().OfPoint(local_center)
    except Exception:
        return local_center


def quantize(value, tolerance):
    tol = max(abs(float(tolerance)), 0.0000001)
    return int(round(float(value) / tol))


def local_item_descriptor(raw_item, center, tolerance):
    points = raw_item.get("all_points") or []
    bounds = points_bounds(points)

    if bounds is None:
        cx = cy = cz = sx = sy = sz = 0.0
    else:
        cx = (bounds[0] + bounds[1]) * 0.5 - center.X
        cy = (bounds[2] + bounds[3]) * 0.5 - center.Y
        cz = (bounds[4] + bounds[5]) * 0.5 - center.Z
        sx = bounds[1] - bounds[0]
        sy = bounds[3] - bounds[2]
        sz = bounds[5] - bounds[4]

    curve_points = raw_item.get("curve_points") or []
    curve_descriptor = []
    for point in curve_points:
        curve_descriptor.append((
            quantize(point.X - center.X, tolerance),
            quantize(point.Y - center.Y, tolerance),
            quantize(point.Z - center.Z, tolerance)
        ))
    curve_descriptor.sort()

    return (
        int(raw_item.get("category_id") or 0),
        safe_text(raw_item.get("class_name", u"")),
        quantize(cx, tolerance),
        quantize(cy, tolerance),
        quantize(cz, tolerance),
        quantize(sx, tolerance),
        quantize(sy, tolerance),
        quantize(sz, tolerance),
        tuple(curve_descriptor)
    )


def invariant_item_descriptor(raw_item, center, tolerance):
    points = raw_item.get("all_points") or []
    bounds = points_bounds(points)

    if bounds is None:
        cx = cy = cz = sx = sy = sz = 0.0
    else:
        cx = (bounds[0] + bounds[1]) * 0.5 - center.X
        cy = (bounds[2] + bounds[3]) * 0.5 - center.Y
        cz = (bounds[4] + bounds[5]) * 0.5 - center.Z
        sx = bounds[1] - bounds[0]
        sy = bounds[3] - bounds[2]
        sz = bounds[5] - bounds[4]

    radial = math.sqrt(cx * cx + cy * cy + cz * cz)
    planar_sizes = sorted([abs(sx), abs(sy)])

    curve_points = raw_item.get("curve_points") or []
    curve_distances = []
    for i in range(len(curve_points)):
        for j in range(i + 1, len(curve_points)):
            try:
                curve_distances.append(
                    quantize(points_distance(curve_points[i], curve_points[j]), tolerance)
                )
            except Exception:
                pass
    curve_distances.sort()

    return (
        int(raw_item.get("category_id") or 0),
        safe_text(raw_item.get("class_name", u"")),
        quantize(radial, tolerance),
        quantize(planar_sizes[0], tolerance),
        quantize(planar_sizes[1], tolerance),
        quantize(abs(sz), tolerance),
        tuple(curve_distances)
    )


def build_detail_group_geometry_signature(group, view, tolerance):
    raw_items, all_points = get_member_local_data(group, view)
    bounds = points_bounds(all_points)
    if bounds is None:
        return None

    center = DB.XYZ(
        (bounds[0] + bounds[1]) * 0.5,
        (bounds[2] + bounds[3]) * 0.5,
        (bounds[4] + bounds[5]) * 0.5
    )

    exact_items = []
    invariant_items = []
    centers = []

    for raw_item in raw_items:
        exact_items.append(local_item_descriptor(raw_item, center, tolerance))
        invariant_items.append(invariant_item_descriptor(raw_item, center, tolerance))

        item_bounds = points_bounds(raw_item.get("all_points") or [])
        if item_bounds is not None:
            centers.append(DB.XYZ(
                (item_bounds[0] + item_bounds[1]) * 0.5,
                (item_bounds[2] + item_bounds[3]) * 0.5,
                (item_bounds[4] + item_bounds[5]) * 0.5
            ))

    exact_items.sort()
    invariant_items.sort()

    pair_distances = []
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            try:
                pair_distances.append(
                    quantize(points_distance(centers[i], centers[j]), tolerance)
                )
            except Exception:
                pass
    pair_distances.sort()

    width = bounds[1] - bounds[0]
    height = bounds[3] - bounds[2]
    depth = bounds[5] - bounds[4]
    planar_dims = sorted([abs(width), abs(height)])

    exact_signature = (
        len(raw_items),
        quantize(width, tolerance),
        quantize(height, tolerance),
        quantize(depth, tolerance),
        tuple(exact_items)
    )

    invariant_signature = (
        len(raw_items),
        quantize(planar_dims[0], tolerance),
        quantize(planar_dims[1], tolerance),
        quantize(abs(depth), tolerance),
        tuple(invariant_items),
        tuple(pair_distances)
    )

    return {
        "exact": exact_signature,
        "invariant": invariant_signature,
        "member_count": len(raw_items)
    }


def geometry_signatures_match(signature_a, signature_b):
    if signature_a is None or signature_b is None:
        return False
    if signature_a.get("exact") == signature_b.get("exact"):
        return True
    return signature_a.get("invariant") == signature_b.get("invariant")


def find_geometry_similar_detail_groups(sample_item, detail_items, view,
                                        tolerance_internal):
    sample_signature = build_detail_group_geometry_signature(
        sample_item.element, view, tolerance_internal
    )
    if sample_signature is None:
        return [], None

    results = []
    for item in detail_items:
        try:
            signature = build_detail_group_geometry_signature(
                item.element, view, tolerance_internal
            )
            if geometry_signatures_match(sample_signature, signature):
                results.append(item)
        except Exception:
            pass

    results.sort(key=lambda item: (item.type_name.lower(), item.id_value))
    return results, sample_signature


# ============================================================
# TARGET FILTERS / CALIBRATION
# ============================================================

def filter_detail_groups_by_name(detail_items, raw_contains):
    keywords = split_keywords(raw_contains)
    if not keywords:
        return []

    results = []
    for item in detail_items:
        name = safe_text(item.type_name).lower()
        if any(keyword in name for keyword in keywords):
            results.append(item)
    return results


def nearest_detail_group_to_model_group(sample_model_group_item, target_items):
    sample_point = get_group_location(sample_model_group_item.element)
    if sample_point is None or not target_items:
        return None

    best_item = None
    best_distance = None
    for item in target_items:
        point = item.anchor
        if point is None:
            continue
        try:
            distance = points_distance(sample_point, point)
        except Exception:
            continue
        if best_distance is None or distance < best_distance:
            best_item = item
            best_distance = distance
    return best_item


def calibrate_model_group_to_detail_group(sample_model_group_item,
                                           sample_detail_group_item):
    model_group = sample_model_group_item.element
    detail_group = sample_detail_group_item.element

    model_point = get_group_location(model_group)
    detail_point = sample_detail_group_item.anchor
    if model_point is None:
        raise Exception(u"Không xác định được điểm chèn của Model Group mẫu.")
    if detail_point is None:
        raise Exception(u"Không xác định được tâm hình học của Detail Group mẫu.")

    detail_x, detail_y = get_group_axes(detail_group)
    model_x, unused_model_y = get_group_axes(model_group)

    delta = model_point - detail_point
    local_offset_x = delta.DotProduct(detail_x)
    local_offset_y = delta.DotProduct(detail_y)
    local_offset_z = delta.Z
    relative_angle = normalize_angle(
        vector_angle_xy(model_x) - vector_angle_xy(detail_x)
    )

    return {
        "sample_detail_group_item": sample_detail_group_item,
        "offset_x": local_offset_x,
        "offset_y": local_offset_y,
        "offset_z": local_offset_z,
        "relative_angle": relative_angle
    }


# ============================================================
# MODEL GROUP SAMPLE PICK FILTER
# ============================================================

class ModelGroupSelectionFilter(ISelectionFilter):
    def __init__(self, group_type_id):
        self.group_type_id = group_type_id

    def AllowElement(self, element):
        try:
            if not isinstance(element, DB.Group):
                return False
            if not category_is(element, DB.BuiltInCategory.OST_IOSModelGroups):
                return False
            return same_id(element.GroupType.Id, self.group_type_id)
        except Exception:
            return False

    def AllowReference(self, reference, position):
        return False


def pick_model_group_sample(window, group_type_item):
    """Pick one visible Model Group instance of the selected Group Type."""
    if group_type_item is None:
        return None

    reference = None
    selection_filter = ModelGroupSelectionFilter(group_type_item.element.Id)
    prompt = u"Chọn Model Group mẫu đúng Type: {0}".format(
        group_type_item.display_name
    )

    try:
        with forms.WindowToggler(window):
            reference = uidoc.Selection.PickObject(
                ObjectType.Element,
                selection_filter,
                prompt
            )
    except AttributeError:
        try:
            window.Hide()
            reference = uidoc.Selection.PickObject(
                ObjectType.Element,
                selection_filter,
                prompt
            )
        finally:
            try:
                window.Show()
            except Exception:
                pass

    if reference is None:
        return None

    group = doc.GetElement(reference.ElementId)
    if group is None:
        return None
    if not isinstance(group, DB.Group):
        return None
    if not category_is(group, DB.BuiltInCategory.OST_IOSModelGroups):
        return None
    if not same_id(group.GroupType.Id, group_type_item.element.Id):
        return None
    return GroupInstanceItem(group)


# ============================================================
# DETAIL GROUP PICK FILTER
# ============================================================

class DetailGroupSelectionFilter(ISelectionFilter):
    def __init__(self, view_id):
        self.view_id = view_id

    def AllowElement(self, element):
        try:
            if not is_detail_group(element):
                return False
            try:
                owner_view_id = element.OwnerViewId
                if not invalid_id(owner_view_id) and not same_id(owner_view_id, self.view_id):
                    return False
            except Exception:
                pass
            return True
        except Exception:
            return False

    def AllowReference(self, reference, position):
        return False


def pick_detail_group_in_active_view(window, detail_items):
    reference = None
    selection_filter = DetailGroupSelectionFilter(start_view.Id)

    try:
        with forms.WindowToggler(window):
            reference = uidoc.Selection.PickObject(
                ObjectType.Element,
                selection_filter,
                u"Chọn một Detail Group trong Active View để quét hình học tương tự"
            )
    except AttributeError:
        # Compatibility fallback for older pyRevit builds.
        try:
            window.Hide()
            reference = uidoc.Selection.PickObject(
                ObjectType.Element,
                selection_filter,
                u"Chọn một Detail Group trong Active View để quét hình học tương tự"
            )
        finally:
            try:
                window.Show()
            except Exception:
                pass

    if reference is None:
        return None

    selected_id = get_id_value(reference.ElementId)
    for item in detail_items:
        if item.id_value == selected_id:
            return item

    element = doc.GetElement(reference.ElementId)
    if element is not None and is_detail_group(element):
        return DetailGroupItem(element, start_view)
    return None


# ============================================================
# USER INTERFACE
# ============================================================

class SettingsWindow(forms.WPFWindow):
    def __init__(self, xaml_file, group_type_items, detail_items, config, host_view):
        forms.WPFWindow.__init__(self, xaml_file)

        self.group_type_items = group_type_items
        self.detail_items = detail_items
        self.config = config
        self.host_view = host_view
        self.sample_group_item = None
        self.picked_detail_item = None
        self.cached_targets = None
        self.cached_signature = None
        self.result = None

        self.active_view_text.Text = u"Active View: {0}".format(
            get_element_name(host_view) or safe_text(host_view.ViewType)
        )
        self.detail_group_total_text.Text = u"Có {0} Detail Group trong Active View.".format(
            len(detail_items)
        )

        for item in group_type_items:
            self.group_combo.Items.Add(item.display_name)

        self.restore_config()

        self.group_combo.SelectionChanged += self.group_type_changed
        self.pick_sample_group_button.Click += self.pick_sample_group_clicked
        self.mode_name_radio.Checked += self.mode_changed
        self.mode_geometry_radio.Checked += self.mode_changed
        self.detail_name_contains_text.TextChanged += self.target_setting_changed
        self.geometry_tolerance_text.TextChanged += self.target_setting_changed
        self.pick_detail_button.Click += self.pick_detail_clicked
        self.scan_button.Click += self.scan_clicked
        self.test_one_checkbox.Checked += self.test_state_changed
        self.test_one_checkbox.Unchecked += self.test_state_changed
        self.run_button.Click += self.run_clicked
        self.cancel_button.Click += self.cancel_clicked

        self.restore_sample_group(
            safe_text(getattr(self.config, "last_sample_group_uid", u""))
        )
        self.restore_picked_detail_group()
        self.update_mode_controls()
        self.update_test_controls()
        self.update_scan_status()

    def restore_config(self):
        last_group_uid = safe_text(getattr(self.config, "last_group_uid", u""))
        self.group_combo.SelectedIndex = self.find_uid_index(
            self.group_type_items, last_group_uid
        )

        target_mode = safe_text(
            getattr(self.config, "detail_target_mode", u"NAME")
        ).upper()
        self.mode_geometry_radio.IsChecked = target_mode == u"GEOMETRY"
        self.mode_name_radio.IsChecked = target_mode != u"GEOMETRY"

        self.detail_name_contains_text.Text = safe_text(
            getattr(self.config, "detail_name_contains", u"")
        )
        self.geometry_tolerance_text.Text = safe_text(
            getattr(self.config, "geometry_tolerance_mm", u"2")
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
            getattr(self.config, "test_target_index", u"1")
        )

    def restore_picked_detail_group(self):
        uid = safe_text(getattr(self.config, "picked_detail_group_uid", u""))
        self.picked_detail_item = None
        if uid:
            for item in self.detail_items:
                if item.uid == uid:
                    self.picked_detail_item = item
                    break
        self.update_picked_detail_status()

    def find_uid_index(self, items, uid):
        if uid:
            for index, item in enumerate(items):
                if item.uid == uid:
                    return index
        return 0 if items else -1

    def get_selected_group_type_item(self):
        index = self.group_combo.SelectedIndex
        if index < 0 or index >= len(self.group_type_items):
            return None
        return self.group_type_items[index]

    def get_selected_group_instance_item(self):
        return self.sample_group_item

    def set_sample_group_item(self, item, source_text=None):
        self.sample_group_item = item
        if item is None:
            self.sample_group_text.Text = u"Chưa chọn Model Group mẫu"
            return

        suffix = u""
        if source_text:
            suffix = u" | {0}".format(source_text)
        self.sample_group_text.Text = u"{0}{1}".format(
            item.display_name,
            suffix
        )

    def restore_sample_group(self, preferred_uid=None):
        """Restore one designated sample; do not expose all instances in a dropdown.

        Rules:
        - Saved sample exists + matches selected type -> restore it.
        - Otherwise, if the selected type has exactly one project instance -> auto-use it.
        - If there are multiple instances -> require direct Pick so generated instances do
          not flood/ambiguate the sample selector.
        """
        group_type_item = self.get_selected_group_type_item()
        self.sample_group_item = None

        if group_type_item is None:
            self.set_sample_group_item(None)
            self.sample_group_status.Text = u"Chưa chọn Model Group Type."
            return

        preferred_uid = safe_text(preferred_uid).strip()
        if preferred_uid:
            try:
                preferred_group = doc.GetElement(preferred_uid)
            except Exception:
                preferred_group = None

            try:
                if (
                    isinstance(preferred_group, DB.Group) and
                    category_is(preferred_group, DB.BuiltInCategory.OST_IOSModelGroups) and
                    same_id(preferred_group.GroupType.Id, group_type_item.element.Id)
                ):
                    self.set_sample_group_item(
                        GroupInstanceItem(preferred_group),
                        u"MẪU LẦN TRƯỚC"
                    )
                    count = len(collect_model_group_instances(group_type_item.element))
                    self.sample_group_status.Text = (
                        u"Đã khôi phục Model Group mẫu lần trước. "
                        u"Type này hiện có {0} instance trong project; "
                        u"các instance khác không được đưa vào dropdown."
                    ).format(count)
                    return
            except Exception:
                pass

        instances = collect_model_group_instances(group_type_item.element)
        if len(instances) == 1:
            self.set_sample_group_item(instances[0], u"TỰ ĐỘNG - CHỈ CÓ 1 INSTANCE")
            self.sample_group_status.Text = (
                u"Type này chỉ có 1 Model Group instance trong project nên tool tự dùng làm mẫu."
            )
        elif len(instances) > 1:
            self.set_sample_group_item(None)
            self.sample_group_status.Text = (
                u"Type này có {0} Model Group instance. "
                u"Bấm 'Pick Model Group mẫu' và chọn đúng instance mẫu trong model."
            ).format(len(instances))
        else:
            self.set_sample_group_item(None)
            self.sample_group_status.Text = (
                u"Type này chưa có Model Group instance nào trong project. "
                u"Hãy đặt thủ công một instance mẫu trước."
            )

    def pick_sample_group_clicked(self, sender, args):
        group_type_item = self.get_selected_group_type_item()
        if group_type_item is None:
            self.sample_group_status.Text = u"Hãy chọn Model Group Type trước."
            return

        try:
            item = pick_model_group_sample(self, group_type_item)
        except UI.Exceptions.OperationCanceledException:
            return
        except Exception as ex:
            self.sample_group_status.Text = u"Không pick được Model Group mẫu: {0}".format(
                safe_text(ex)
            )
            return

        if item is None:
            self.sample_group_status.Text = (
                u"Đối tượng chọn không phải Model Group đúng Type đang chọn."
            )
            return

        self.set_sample_group_item(item, u"ĐÃ PICK")
        self.sample_group_status.Text = (
            u"Đã chọn Model Group mẫu ID {0}. Tool sẽ dùng duy nhất instance này để calibration."
        ).format(item.id_value)

    def group_type_changed(self, sender, args):
        # A sample is valid only for its own Group Type. Try the saved sample first;
        # otherwise auto-use only when this type has exactly one instance.
        saved_uid = safe_text(getattr(self.config, "last_sample_group_uid", u""))
        self.restore_sample_group(saved_uid)
        self.invalidate_targets()

    def mode_changed(self, sender, args):
        self.invalidate_targets()
        self.update_mode_controls()
        self.update_scan_status()

    def target_setting_changed(self, sender, args):
        self.invalidate_targets()
        self.update_scan_status()

    def invalidate_targets(self):
        self.cached_targets = None
        self.cached_signature = None

    def update_mode_controls(self):
        name_mode = self.mode_name_radio.IsChecked is True
        geometry_mode = self.mode_geometry_radio.IsChecked is True

        self.detail_name_contains_text.IsEnabled = name_mode
        self.pick_detail_button.IsEnabled = geometry_mode
        self.geometry_tolerance_text.IsEnabled = geometry_mode
        self.picked_detail_status.IsEnabled = geometry_mode

    def update_picked_detail_status(self):
        if self.picked_detail_item is None:
            self.picked_detail_status.Text = u"Chưa chọn Detail Group mẫu hình học."
        else:
            self.picked_detail_status.Text = u"Đã chọn: {0}".format(
                self.picked_detail_item.display_name
            )

    def pick_detail_clicked(self, sender, args):
        try:
            item = pick_detail_group_in_active_view(self, self.detail_items)
        except UI.Exceptions.OperationCanceledException:
            return
        except Exception as ex:
            self.match_count_text.Text = u"Không pick được Detail Group: {0}".format(
                safe_text(ex)
            )
            return

        if item is None:
            self.match_count_text.Text = u"Đối tượng chọn không phải Detail Group hợp lệ."
            return

        self.picked_detail_item = item
        self.invalidate_targets()
        self.update_picked_detail_status()
        self.update_scan_status()

    def current_target_signature(self):
        if self.mode_name_radio.IsChecked is True:
            return u"NAME|{0}".format(
                u";".join(split_keywords(self.detail_name_contains_text.Text))
            )

        picked_uid = self.picked_detail_item.uid if self.picked_detail_item else u""
        return u"GEOMETRY|{0}|{1}".format(
            picked_uid,
            safe_text(self.geometry_tolerance_text.Text).strip()
        )

    def get_targets(self):
        signature = self.current_target_signature()
        if self.cached_targets is not None and signature == self.cached_signature:
            return list(self.cached_targets)

        if self.mode_name_radio.IsChecked is True:
            contains_text = safe_text(self.detail_name_contains_text.Text).strip()
            if not split_keywords(contains_text):
                raise ValueError(u"Option 1: hãy nhập Contains của tên Detail Group.")
            targets = filter_detail_groups_by_name(self.detail_items, contains_text)
        else:
            if self.picked_detail_item is None:
                raise ValueError(u"Option 2: hãy Pick một Detail Group mẫu trước.")
            tolerance_mm = parse_number(
                self.geometry_tolerance_text.Text,
                u"Dung sai quét hình học",
                0.01
            )
            tolerance_internal = mm_to_internal(tolerance_mm)
            targets, unused_signature = find_geometry_similar_detail_groups(
                self.picked_detail_item,
                self.detail_items,
                self.host_view,
                tolerance_internal
            )

        self.cached_targets = list(targets)
        self.cached_signature = signature
        return targets

    def get_calibration_detail_item(self, targets):
        if self.mode_geometry_radio.IsChecked is True:
            return self.picked_detail_item

        sample_group_item = self.get_selected_group_instance_item()
        if sample_group_item is None:
            return None
        return nearest_detail_group_to_model_group(sample_group_item, targets)

    def update_scan_status(self):
        if self.cached_targets is None:
            self.match_count_text.Text = u"Chưa quét Detail Group đích."
        else:
            self.match_count_text.Text = u"Tìm thấy {0} Detail Group đích.".format(
                len(self.cached_targets)
            )
        self.update_test_hint()

    def scan_clicked(self, sender, args):
        try:
            targets = self.get_targets()
            calibration_item = self.get_calibration_detail_item(targets)
            if not targets:
                self.match_count_text.Text = u"Không tìm thấy Detail Group phù hợp."
            elif calibration_item is None:
                self.match_count_text.Text = (
                    u"Tìm thấy {0} Detail Group nhưng chưa xác định được Detail Group mẫu."
                ).format(len(targets))
            else:
                self.match_count_text.Text = (
                    u"Tìm thấy {0} Detail Group. Detail Group mẫu: {1}"
                ).format(len(targets), calibration_item.display_name)
            self.update_test_hint()
        except ValueError as ex:
            self.match_count_text.Text = safe_text(ex)
        except Exception:
            self.match_count_text.Text = u"Lỗi khi quét hình học. Xem pyRevit output sau khi chạy."

    def test_state_changed(self, sender, args):
        self.update_test_controls()

    def update_test_controls(self):
        enabled = self.test_one_checkbox.IsChecked is True
        self.test_index_label.IsEnabled = enabled
        self.test_index_text.IsEnabled = enabled
        self.test_index_hint.IsEnabled = enabled
        self.update_test_hint()

    def update_test_hint(self):
        if self.cached_targets is not None:
            self.test_index_hint.Text = u"Nhập từ 1 đến {0}".format(
                len(self.cached_targets)
            )
        else:
            self.test_index_hint.Text = u"Nhập theo thứ tự danh sách Detail Group sau khi quét"

    def run_clicked(self, sender, args):
        group_type_item = self.get_selected_group_type_item()
        sample_group_item = self.get_selected_group_instance_item()

        if group_type_item is None:
            self.match_count_text.Text = u"Hãy chọn Model Group Type cần đặt."
            return
        if sample_group_item is None:
            self.match_count_text.Text = (
                u"Hãy chọn Model Group mẫu. Nếu Type có nhiều instance, bấm 'Pick Model Group mẫu'."
            )
            return

        try:
            targets = self.get_targets()
            if not targets:
                raise ValueError(u"Không tìm thấy Detail Group đích phù hợp.")

            calibration_detail_item = self.get_calibration_detail_item(targets)
            if calibration_detail_item is None:
                raise ValueError(u"Không xác định được Detail Group mẫu để học vị trí/góc.")

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
                u"Dung sai Model Group trùng",
                0.0
            )

            geometry_tolerance_mm = parse_number(
                self.geometry_tolerance_text.Text,
                u"Dung sai quét hình học",
                0.01
            )

            test_one = self.test_one_checkbox.IsChecked is True
            test_target_index = 1
            if test_one:
                test_target_index = parse_integer(
                    self.test_index_text.Text,
                    u"Detail Group thứ",
                    1,
                    len(targets)
                )
        except ValueError as ex:
            self.match_count_text.Text = safe_text(ex)
            return

        target_mode = u"NAME" if self.mode_name_radio.IsChecked is True else u"GEOMETRY"

        self.result = {
            "config": self.config,
            "group_type_item": group_type_item,
            "sample_group_item": sample_group_item,
            "detail_items": self.detail_items,
            "target_items": list(targets),
            "calibration_detail_item": calibration_detail_item,
            "target_mode": target_mode,
            "detail_name_contains": safe_text(self.detail_name_contains_text.Text).strip(),
            "picked_detail_item": self.picked_detail_item,
            "geometry_tolerance_mm": geometry_tolerance_mm,
            "extra_offset_x_mm": extra_offset_x_mm,
            "extra_offset_y_mm": extra_offset_y_mm,
            "extra_rotation_deg": extra_rotation_deg,
            "skip_duplicate": self.skip_duplicate_checkbox.IsChecked is True,
            "duplicate_tolerance_mm": duplicate_tolerance_mm,
            "test_one": test_one,
            "test_target_index": test_target_index
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
    config.detail_target_mode = settings["target_mode"]
    config.detail_name_contains = safe_text(settings["detail_name_contains"])
    config.geometry_tolerance_mm = safe_text(settings["geometry_tolerance_mm"])

    picked_item = settings.get("picked_detail_item")
    config.picked_detail_group_uid = picked_item.uid if picked_item is not None else u""

    config.extra_offset_x_mm = safe_text(settings["extra_offset_x_mm"])
    config.extra_offset_y_mm = safe_text(settings["extra_offset_y_mm"])
    config.extra_rotation_deg = safe_text(settings["extra_rotation_deg"])
    config.skip_duplicate = settings["skip_duplicate"]
    config.duplicate_tolerance_mm = safe_text(settings["duplicate_tolerance_mm"])
    config.test_one = settings["test_one"]
    config.test_target_index = safe_text(settings["test_target_index"])
    script.save_config()


# ============================================================
# MAIN PLACEMENT
# ============================================================

def build_context_lines(settings, calibration=None):
    mode = settings["target_mode"]
    if mode == u"NAME":
        mode_text = u"Option 1 - Name Contains: [{0}]".format(
            u"; ".join(split_keywords(settings["detail_name_contains"]))
        )
    else:
        picked = settings.get("picked_detail_item")
        mode_text = u"Option 2 - Pick + Geometry Scan"
        if picked is not None:
            mode_text += u" | mẫu: {0}".format(picked.display_name)
        mode_text += u" | tolerance {0:.2f} mm".format(
            settings["geometry_tolerance_mm"]
        )

    lines = [
        u"Active View: {0}".format(
            get_element_name(start_view) or safe_text(start_view.ViewType)
        ),
        u"Model Group Type cần đặt: {0}".format(
            settings["group_type_item"].display_name
        ),
        u"Model Group mẫu: ID {0}".format(
            settings["sample_group_item"].id_value
        ),
        u"Cách chọn Detail Group đích: {0}".format(mode_text),
        u"Detail Group đích: {0}".format(len(settings["target_items"])),
        u"Hiệu chỉnh thêm X / Y: {0:+.1f} / {1:+.1f} mm".format(
            settings["extra_offset_x_mm"], settings["extra_offset_y_mm"]
        ),
        u"Hiệu chỉnh thêm góc: {0:+.2f}°".format(
            settings["extra_rotation_deg"]
        )
    ]

    if calibration is not None:
        detail_item = calibration["sample_detail_group_item"]
        lines.extend([
            u"Detail Group mẫu để calibration: {0}".format(detail_item.display_name),
            u"Offset Model Group mẫu so với tâm hình học Detail Group: X={0:+.1f}; Y={1:+.1f}; Z={2:+.1f} mm".format(
                internal_to_mm(calibration["offset_x"]),
                internal_to_mm(calibration["offset_y"]),
                internal_to_mm(calibration["offset_z"])
            ),
            u"Góc Model Group mẫu so với trục X Detail Group: {0}°".format(
                format_angle(calibration["relative_angle"])
            )
        ])
    return lines


def run_tool(settings, startup_notices=None):
    startup_notices = list(startup_notices or [])
    target_items = list(settings["target_items"])

    if not target_items:
        show_report(
            u"Không tìm thấy Detail Group phù hợp",
            summary=build_context_lines(settings),
            notices=startup_notices
        )
        return

    targets_to_process = list(target_items)
    if settings["test_one"]:
        index = settings["test_target_index"]
        if index < 1 or index > len(target_items):
            show_report(
                u"Detail Group thứ không hợp lệ",
                summary=build_context_lines(settings) + [
                    u"Hãy nhập từ 1 đến {0}.".format(len(target_items))
                ],
                notices=startup_notices
            )
            return
        targets_to_process = [target_items[index - 1]]
        startup_notices.append(
            u"Chế độ thử: Detail Group thứ {0}/{1} = {2}.".format(
                index, len(target_items), target_items[index - 1].display_name
            )
        )

    try:
        calibration = calibrate_model_group_to_detail_group(
            settings["sample_group_item"],
            settings["calibration_detail_item"]
        )
    except Exception:
        show_report(
            u"Không calibration được Model Group và Detail Group mẫu",
            summary=build_context_lines(settings),
            notices=startup_notices,
            traceback_text=traceback.format_exc()
        )
        return

    group_type = settings["group_type_item"].element
    extra_offset_x = mm_to_internal(settings["extra_offset_x_mm"])
    extra_offset_y = mm_to_internal(settings["extra_offset_y_mm"])
    extra_rotation = math.radians(settings["extra_rotation_deg"])
    duplicate_tolerance = mm_to_internal(settings["duplicate_tolerance_mm"])

    existing_points = []
    if settings["skip_duplicate"]:
        existing_points = collect_existing_model_group_points(group_type)

    created_records = []
    failed_items = []
    duplicate_count = 0
    cancelled = False

    transaction_name = u"Test Place Model Group By Detail Group"
    if not settings["test_one"]:
        transaction_name = u"Place Model Groups By Detail Groups"

    transaction = DB.Transaction(doc, transaction_name)

    try:
        transaction.Start()
        total = len(targets_to_process)

        with forms.ProgressBar(
            title=u"Đang đặt Model Group theo Detail Group... {value} / {max_value}",
            cancellable=True,
            step=1
        ) as progress:
            for index, detail_item in enumerate(targets_to_process):
                if progress.cancelled:
                    cancelled = True
                    break

                sub_transaction = DB.SubTransaction(doc)
                sub_transaction.Start()

                try:
                    detail_point = get_detail_group_anchor(
                        detail_item.element, start_view
                    )
                    if detail_point is None:
                        raise Exception(u"Không xác định được tâm hình học Detail Group.")

                    detail_x, detail_y = get_group_axes(detail_item.element)

                    total_offset_x = calibration["offset_x"] + extra_offset_x
                    total_offset_y = calibration["offset_y"] + extra_offset_y

                    host_anchor = (
                        detail_point
                        + detail_x.Multiply(total_offset_x)
                        + detail_y.Multiply(total_offset_y)
                        + DB.XYZ.BasisZ.Multiply(calibration["offset_z"])
                    )

                    if settings["skip_duplicate"] and has_nearby_point(
                        host_anchor, existing_points, duplicate_tolerance
                    ):
                        duplicate_count += 1
                        sub_transaction.RollBack()
                    else:
                        target_angle = (
                            vector_angle_xy(detail_x)
                            + calibration["relative_angle"]
                            + extra_rotation
                        )

                        new_group = doc.Create.PlaceGroup(host_anchor, group_type)
                        if new_group is None:
                            raise Exception(u"Revit không tạo được Model Group.")

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
                        created_records.append({
                            "group_id": group_id,
                            "group_id_value": group_id_value,
                            "detail_group": u"ID {0}".format(detail_item.id_value),
                            "detail_type": detail_item.type_name,
                            "point": format_point_mm(host_anchor),
                            "angle": format_angle(target_angle),
                            "mode": (
                                u"Name Contains"
                                if settings["target_mode"] == u"NAME"
                                else u"Geometry Similar"
                            )
                        })

                except Exception as item_error:
                    try:
                        if sub_transaction.GetStatus() == DB.TransactionStatus.Started:
                            sub_transaction.RollBack()
                    except Exception:
                        pass
                    failed_items.append(
                        u"{0}: {1}".format(
                            detail_item.display_name,
                            safe_text(item_error)
                        )
                    )

                progress.update_progress(index + 1, total)

            if progress.cancelled:
                cancelled = True

        if cancelled:
            transaction.RollBack()
            show_report(
                u"Đã hủy đặt Model Group",
                summary=build_context_lines(settings, calibration) + [
                    u"Toàn bộ Model Group của lần chạy đã được rollback."
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
        u"Detail Group đã xử lý trong lần chạy: {0}".format(len(targets_to_process)),
        u"Model Group đã tạo: {0}".format(len(created_records)),
        u"Bỏ qua vì Model Group cùng Type đã có gần vị trí: {0}".format(duplicate_count),
        u"Detail Group không xử lý được: {0}".format(len(failed_items))
    ]

    if settings["test_one"]:
        if created_records:
            title = u"Đã tạo 1 Model Group kiểm tra"
            startup_notices.extend([
                u"Kiểm tra vị trí và góc bằng liên kết Group trong bảng.",
                u"Khi đúng, chạy lại và bỏ tick chế độ chỉ tạo 1 Detail Group."
            ])
        elif duplicate_count:
            title = u"Model Group kiểm tra đã tồn tại gần vị trí"
        else:
            title = u"Không tạo được Model Group kiểm tra"
    else:
        title = u"Đã hoàn thành đặt Model Group theo Detail Group"

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

# The placement/rotation logic is XY + global Z, matching the original tool.
# Warn instead of hard-blocking non-plan views, because some users intentionally
# use model-compatible custom views.
startup_notices = []
try:
    view_type_text = safe_text(start_view.ViewType)
    plan_view_types = (
        "FloorPlan", "CeilingPlan", "EngineeringPlan", "AreaPlan"
    )
    if view_type_text not in plan_view_types:
        startup_notices.append(
            u"Active View không phải plan view tiêu chuẩn. Tool vẫn chạy nhưng "
            u"rotation của Model Group dùng trục Z toàn cục như bản cũ."
        )
except Exception:
    pass


detail_items = collect_detail_group_items(start_view)
if not detail_items:
    show_report(
        u"Không có Detail Group trong Active View",
        summary=[u"Không tìm thấy Detail Group instance hiển thị trong Active View."],
        notices=[u"Hãy mở đúng view hoặc đặt/hiển thị Detail Group trước khi chạy tool."]
    )
    script.exit()


group_type_items = collect_model_group_types()
if not group_type_items:
    show_report(
        u"Không có Model Group Type",
        summary=[u"Host model chưa có Model Group Type."],
        notices=[u"Tạo hoặc load Model Group trước khi chạy tool."]
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
    group_type_items,
    detail_items,
    config,
    start_view
)
window.ShowDialog()

if window.result is None:
    script.exit()

save_config(config, window.result)
run_tool(window.result, startup_notices)
