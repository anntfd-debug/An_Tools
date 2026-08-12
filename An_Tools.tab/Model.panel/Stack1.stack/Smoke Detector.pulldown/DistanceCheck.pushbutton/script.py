# -*- coding: utf-8 -*-
"""pyRevit - Split Room Filled Region creation and coverage checking.

Workflow
--------
1. CREATE mode: read linked Rooms, create/reuse Room Filled Regions, then stop.
2. CHECK mode: use previously created Room Filled Regions or pick one/many existing Filled Regions.
3. Pick one sample FamilyInstance and scan Same Family or Same Type in Active View.
4. Verify that every Room/Region is fully covered by at least one radius.
5. Create Filled Regions ONLY on the actual UNCOVERED portions of failed Rooms/Regions.
6. Preserve the boundary-facing Family rule: first-layer Families must be within
   a user-defined maximum distance from their NEAREST Room boundary; ONLY failures receive a 300 mm marker.

Coverage is checked with Revit solid Boolean subtraction. The remaining solid is
converted back into Filled Region boundary loops so only the uncovered area is
drawn. A 100 mm XY sampling fallback is used only if Boolean geometry fails.

Compatible with pyRevit IronPython 2.7 and Revit 2020-2025.
"""
from __future__ import print_function

import math
import os

from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, Color, CurveLoop, Element, ElementId,
    ElementReferenceType,
    Arc, BooleanOperationsType, BooleanOperationsUtils, FamilyInstance, FilledRegion,
    FilledRegionType, FillPatternElement, FilteredElementCollector,
    GeometryCreationUtilities, Line, LocationCurve, LocationPoint, PlanarFace,
    OverrideGraphicSettings, RevitLinkInstance, SpatialElementBoundaryOptions,
    Transaction, TransactionStatus, Transform, Wall, XYZ
)
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from pyrevit import forms, revit, script
from System.Collections.Generic import List

try:
    from Autodesk.Revit.DB import BoundaryValidation
except Exception:
    BoundaryValidation = None


doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView
output = script.get_output()
tool_config = script.get_config()


def safe_element_name(element, fallback="Unnamed"):
    try:
        value = Element.Name.GetValue(element)
        if value:
            return value
    except Exception:
        pass
    try:
        parameter = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        value = parameter.AsString() if parameter else None
        if value:
            return value
    except Exception:
        pass
    return fallback


class RegionTypeOption(object):
    def __init__(self, region_type):
        self.region_type = region_type
        try:
            masking_text = "Masking" if region_type.IsMasking else "Filled"
        except Exception:
            masking_text = "Filled Region"
        self.label = "{0}  [{1}]  [Id {2}]".format(
            safe_element_name(region_type, "Filled Region Type"),
            masking_text, region_type.Id.IntegerValue)

    def __str__(self):
        return self.label


class ModeWindow(forms.WPFWindow):
    def __init__(self, xaml_path, region_type_options,
                 last_mode=None, last_room_type_id=None,
                 last_coverage_type_id=None, last_result_type_id=None,
                 last_radius_mm=None, last_wall_distance_mm=None):
        forms.WPFWindow.__init__(self, xaml_path)
        self.mode = None
        self.room_region_type = None
        self.coverage_region_type = None
        self.result_region_type = None
        self.coverage_radius_mm = None
        self.wall_distance_mm = None
        self.cmbRoomRegionType.ItemsSource = region_type_options
        self.cmbCoverageRegionType.ItemsSource = region_type_options
        self.cmbResultRegionType.ItemsSource = region_type_options

        def _as_int(value):
            try:
                return int(value)
            except Exception:
                return None

        room_id = _as_int(last_room_type_id)
        coverage_id = _as_int(last_coverage_type_id)
        result_id = _as_int(last_result_type_id)
        if region_type_options:
            self.cmbRoomRegionType.SelectedIndex = 0
            self.cmbCoverageRegionType.SelectedIndex = 0
            self.cmbResultRegionType.SelectedIndex = 0
            for index, option in enumerate(region_type_options):
                option_id = option.region_type.Id.IntegerValue
                if option_id == room_id:
                    self.cmbRoomRegionType.SelectedIndex = index
                if option_id == coverage_id:
                    self.cmbCoverageRegionType.SelectedIndex = index
                if option_id == result_id:
                    self.cmbResultRegionType.SelectedIndex = index

        if last_mode in ("create_rooms", "auto_link"):
            self.rbCreateRooms.IsChecked = True
        elif last_mode == "check_auto_rooms":
            self.rbCheckAutoRooms.IsChecked = True
        else:
            self.rbCheckExistingRegion.IsChecked = True

        try:
            radius_value = float(last_radius_mm)
            if radius_value <= 0:
                raise Exception()
        except Exception:
            radius_value = 10000.0
        self.txtCoverageRadius.Text = ("{0:.0f}".format(radius_value))

        try:
            wall_distance_value = float(last_wall_distance_mm)
            if wall_distance_value <= 0:
                raise Exception()
        except Exception:
            wall_distance_value = 5000.0
        self.txtWallDistance.Text = ("{0:.0f}".format(wall_distance_value))

    def btnRun_Click(self, sender, args):
        room_option = self.cmbRoomRegionType.SelectedItem
        coverage_option = self.cmbCoverageRegionType.SelectedItem
        result_option = self.cmbResultRegionType.SelectedItem

        if self.rbCreateRooms.IsChecked:
            self.mode = "create_rooms"
            if room_option is None:
                forms.alert("Model khong co Filled Region Type cho Room de chon.")
                return
        elif self.rbCheckAutoRooms.IsChecked:
            self.mode = "check_auto_rooms"
        else:
            self.mode = "check_existing_region"

        # Coverage settings are required only for the two CHECK modes.
        if self.mode != "create_rooms":
            if coverage_option is None or result_option is None:
                forms.alert("Model khong co du Filled Region Type de kiem tra.")
                return
            radius_text = (self.txtCoverageRadius.Text or "").strip().replace(",", ".")
            try:
                radius_mm = float(radius_text)
            except Exception:
                forms.alert("Ban kinh phu phai la mot so, don vi mm.")
                return
            if radius_mm <= 0:
                forms.alert("Ban kinh phu phai lon hon 0 mm.")
                return

            wall_text = (self.txtWallDistance.Text or "").strip().replace(",", ".")
            try:
                wall_distance_mm = float(wall_text)
            except Exception:
                forms.alert("Khoang cach toi tuong/boundary phai la mot so, don vi mm.")
                return
            if wall_distance_mm <= 0:
                forms.alert("Khoang cach toi tuong/boundary phai lon hon 0 mm.")
                return
        else:
            try:
                radius_mm = float((self.txtCoverageRadius.Text or "10000").strip().replace(",", "."))
            except Exception:
                radius_mm = 10000.0
            try:
                wall_distance_mm = float((self.txtWallDistance.Text or "5000").strip().replace(",", "."))
            except Exception:
                wall_distance_mm = 5000.0

        # Keep current selections in config even when a field is not used by the
        # selected mode. This preserves the user's previous settings.
        self.room_region_type = room_option.region_type if room_option else None
        self.coverage_region_type = coverage_option.region_type if coverage_option else None
        self.result_region_type = result_option.region_type if result_option else None
        self.coverage_radius_mm = radius_mm
        self.wall_distance_mm = wall_distance_mm
        self.Close()

    def btnCancel_Click(self, sender, args):
        self.mode = None
        self.Close()


def choose_run_mode():
    try:
        xaml_path = script.get_bundle_file("ui.xaml")
    except Exception:
        xaml_path = None
    if not xaml_path:
        xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
    region_types = list(FilteredElementCollector(doc).OfClass(FilledRegionType))
    options = [RegionTypeOption(item) for item in region_types]
    options.sort(key=lambda item: item.label.lower())
    window = ModeWindow(
        xaml_path, options,
        getattr(tool_config, "last_mode", None),
        getattr(tool_config, "last_room_type_id", None),
        getattr(tool_config, "last_coverage_type_id", None),
        getattr(tool_config, "last_result_type_id", None),
        getattr(tool_config, "last_coverage_radius_mm", 10000.0),
        getattr(tool_config, "last_wall_distance_mm", 5000.0))
    window.ShowDialog()
    if not window.mode:
        script.exit()

    tool_config.last_mode = window.mode
    if window.room_region_type is not None:
        tool_config.last_room_type_id = window.room_region_type.Id.IntegerValue
    if window.coverage_region_type is not None:
        tool_config.last_coverage_type_id = window.coverage_region_type.Id.IntegerValue
    if window.result_region_type is not None:
        tool_config.last_result_type_id = window.result_region_type.Id.IntegerValue
    tool_config.last_coverage_radius_mm = window.coverage_radius_mm
    tool_config.last_wall_distance_mm = window.wall_distance_mm
    tool_config.last_view_id = view.Id.IntegerValue
    tool_config.last_view_name = safe_element_name(view, "Active View")
    script.save_config()
    return (window.mode, window.room_region_type,
            window.coverage_region_type, window.result_region_type,
            window.coverage_radius_mm, window.wall_distance_mm)

MM_PER_FOOT = 304.8
REGION_WIDTH = 300.0 / MM_PER_FOOT
BOUNDARY_TOLERANCE = 50.0 / MM_PER_FOOT
MAX_AXIS_OFFSET = 200.0 / MM_PER_FOOT
MAX_FRAGMENT_GAP = 3000.0 / MM_PER_FOOT
MAX_ELEVATION_DIFFERENCE = 300.0 / MM_PER_FOOT
CORNER_OVERLAP_TOLERANCE = 500.0 / MM_PER_FOOT
PREVIEW_HALF_LENGTH = 500000.0 / MM_PER_FOOT
MIN_PARALLEL_DOT = math.cos(math.radians(2.0))

# Boundary sampling controls. Smaller values are more accurate but slower.
WALL_SAMPLE_STEP = 500.0 / MM_PER_FOOT
TOL = 1.0e-8


class FamilyFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, FamilyInstance)

    def AllowReference(self, reference, point):
        return False


class FilledRegionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, FilledRegion)

    def AllowReference(self, reference, point):
        return False


class RevitLinkFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, RevitLinkInstance)

    def AllowReference(self, reference, point):
        return False


class LinkedWallFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, RevitLinkInstance)

    def AllowReference(self, reference, point):
        try:
            if (reference.ElementReferenceType !=
                    ElementReferenceType.REFERENCE_TYPE_SURFACE):
                return False
            link_instance = doc.GetElement(reference.ElementId)
            link_doc = link_instance.GetLinkDocument()
            linked_element = link_doc.GetElement(reference.LinkedElementId)
            return isinstance(linked_element, Wall)
        except Exception:
            return False


class LinkedWallInfo(object):
    """A linked Wall represented completely in host-model coordinates."""
    def __init__(self, link_instance, linked_wall):
        self.link_instance = link_instance
        self.linked_wall = linked_wall
        self.link_id = link_instance.Id.IntegerValue
        self.wall_id = linked_wall.Id.IntegerValue
        self.key = (self.link_id, self.wall_id)
        self.width = linked_wall.Width
        location = linked_wall.Location
        self.curve = None
        if isinstance(location, LocationCurve) and location.Curve:
            self.curve = location.Curve.CreateTransformed(
                link_instance.GetTotalTransform())


class LinkedWallFaceInfo(LinkedWallInfo):
    """A picked linked Wall face represented as a straight host-coordinate line."""
    def __init__(self, link_instance, linked_wall, picked_reference):
        LinkedWallInfo.__init__(self, link_instance, linked_wall)
        centre_curve = self.curve
        self.width = 0.0  # The curve below is already on the actual wall face.
        if centre_curve is None or not isinstance(centre_curve, Line):
            self.curve = None
            return
        start3 = centre_curve.GetEndPoint(0)
        end3 = centre_curve.GetEndPoint(1)
        start = xy(start3)
        end = xy(end3)
        direction = unit(sub(end, start))
        picked = picked_reference.GlobalPoint
        if direction is None or picked is None:
            self.curve = None
            return
        # Reject a horizontal top/bottom face or an end face. When the linked
        # geometry reference is unavailable in an older API build, the picked
        # surface point plus Wall direction remains the compatibility fallback.
        try:
            linked_reference = picked_reference.CreateReferenceInLink()
            geometry_object = linked_wall.GetGeometryObjectFromReference(
                linked_reference)
            if isinstance(geometry_object, PlanarFace):
                host_normal = link_instance.GetTotalTransform().OfVector(
                    geometry_object.FaceNormal).Normalize()
                horizontal_normal = unit(xy(host_normal))
                if (abs(host_normal.Z) > 0.20 or
                        horizontal_normal is None or
                        abs(dot(horizontal_normal, direction)) > 0.20):
                    self.curve = None
                    return
        except Exception:
            pass
        face_point = xy(picked)
        start_t = dot(sub(start, face_point), direction)
        end_t = dot(sub(end, face_point), direction)
        face_start = add(face_point, mul(direction, start_t))
        face_end = add(face_point, mul(direction, end_t))
        z_value = (start3.Z + end3.Z) * 0.5
        self.curve = Line.CreateBound(
            XYZ(face_start.X, face_start.Y, z_value),
            XYZ(face_end.X, face_end.Y, z_value))


class VirtualWallInfo(object):
    """Continuous checking boundary derived from one picked linked Wall."""
    def __init__(self, seed, curve, fragment_count):
        self.link_instance = seed.link_instance
        self.linked_wall = seed.linked_wall
        self.link_id = seed.link_id
        self.wall_id = seed.wall_id
        self.key = seed.key
        self.width = seed.width
        self.curve = curve
        self.fragment_count = fragment_count


class FilledRegionEdgeInfo(object):
    """One Filled Region boundary curve used as a virtual inside wall face."""
    def __init__(self, filled_region, curve, loop_index, edge_index):
        self.filled_region = filled_region
        self.region_id = filled_region.Id.IntegerValue
        self.loop_index = loop_index
        self.edge_index = edge_index
        self.key = ("FR", self.region_id, loop_index, edge_index)
        self.width = 0.0
        self.curve = curve


def v2(x, y):
    return XYZ(x, y, 0.0)


def xy(point):
    return v2(point.X, point.Y)


def add(a, b):
    return v2(a.X + b.X, a.Y + b.Y)


def sub(a, b):
    return v2(a.X - b.X, a.Y - b.Y)


def mul(vector, value):
    return v2(vector.X * value, vector.Y * value)


def dot(a, b):
    return a.X * b.X + a.Y * b.Y


def cross2(a, b):
    return a.X * b.Y - a.Y * b.X


def length(vector):
    return math.sqrt(vector.X * vector.X + vector.Y * vector.Y)


def distance(a, b):
    return length(sub(a, b))


def unit(vector):
    size = length(vector)
    if size <= TOL:
        return None
    return v2(vector.X / size, vector.Y / size)


def line_data(wall):
    """Return host XY origin/direction/length/Z for a straight linked Wall."""
    if wall.curve is None or not isinstance(wall.curve, Line):
        return None
    start3 = wall.curve.GetEndPoint(0)
    end3 = wall.curve.GetEndPoint(1)
    start = xy(start3)
    end = xy(end3)
    direction = unit(sub(end, start))
    if direction is None:
        return None
    z_value = (start3.Z + end3.Z) * 0.5
    return start, direction, distance(start, end), z_value


def infinite_line_intersection(origin_a, direction_a,
                               origin_b, direction_b):
    denominator = cross2(direction_a, direction_b)
    if abs(denominator) <= TOL:
        return None
    return cross2(sub(origin_b, origin_a), direction_b) / denominator


def build_virtual_walls(seed_walls):
    """Merge collinear linked fragments and extend seeds to adjacent sides."""
    seed_data = []
    for seed in seed_walls:
        data = line_data(seed)
        if data is None:
            forms.alert(
                "Linked Wall Id {0} co mat cong hoac khong doc duoc Seed Line. "
                "Che do pick mat trong hien chi ho tro Wall thang."
                .format(seed.wall_id),
                exitscript=True)
        seed_data.append((seed, data))

    virtual_walls = []
    open_ends = []
    for seed_index, seed_pair in enumerate(seed_data):
        seed, data = seed_pair
        origin, direction, seed_length, seed_z = data
        interval_min = 0.0
        interval_max = seed_length
        accepted_ids = set([seed.wall_id])

        # Search all Walls in the same linked document/instance. Only fragments
        # on almost the same infinite axis and elevation are candidates.
        link_doc = seed.link_instance.GetLinkDocument()
        candidates = []
        if link_doc is not None:
            for linked_wall in (FilteredElementCollector(link_doc)
                                .OfClass(Wall)
                                .WhereElementIsNotElementType()):
                candidate = LinkedWallInfo(seed.link_instance, linked_wall)
                candidate_data = line_data(candidate)
                if candidate_data is None:
                    continue
                candidate_origin, candidate_direction, candidate_length, candidate_z = candidate_data
                if abs(dot(direction, candidate_direction)) < MIN_PARALLEL_DOT:
                    continue
                if abs(candidate_z - seed_z) > MAX_ELEVATION_DIFFERENCE:
                    continue
                axis_offset = abs(cross2(sub(candidate_origin, origin), direction))
                if axis_offset > MAX_AXIS_OFFSET:
                    continue
                start_t = dot(sub(candidate_origin, origin), direction)
                candidate_end = add(candidate_origin,
                                    mul(candidate_direction, candidate_length))
                end_t = dot(sub(candidate_end, origin), direction)
                candidates.append((min(start_t, end_t), max(start_t, end_t),
                                   candidate.wall_id))

        # Grow transitively across door/opening gaps no larger than 3,000 mm.
        changed = True
        while changed:
            changed = False
            for start_t, end_t, candidate_id in candidates:
                if candidate_id in accepted_ids:
                    continue
                gap = max(interval_min - end_t, start_t - interval_max, 0.0)
                if gap <= MAX_FRAGMENT_GAP:
                    accepted_ids.add(candidate_id)
                    interval_min = min(interval_min, start_t)
                    interval_max = max(interval_max, end_t)
                    changed = True

        # Intersect this seed axis with every other picked face. A correct
        # corner must lie beyond (or only slightly inside) an original seed
        # endpoint. This remains stable for short fragments near doors and for
        # concave rooms with more than four faces.
        lower = []
        upper = []
        for other_index, other_pair in enumerate(seed_data):
            if other_index == seed_index:
                continue
            unused_other, other_data = other_pair
            other_origin, other_direction, unused_length, unused_z = other_data
            parameter = infinite_line_intersection(
                origin, direction, other_origin, other_direction)
            if parameter is None:
                continue
            is_lower = parameter <= CORNER_OVERLAP_TOLERANCE
            is_upper = parameter >= seed_length - CORNER_OVERLAP_TOLERANCE
            if is_lower and is_upper:
                if parameter < seed_length * 0.5:
                    lower.append(parameter)
                else:
                    upper.append(parameter)
            elif is_lower:
                lower.append(parameter)
            elif is_upper:
                upper.append(parameter)
        if lower:
            final_min = max(lower)
        else:
            final_min = interval_min
            open_ends.append((seed.link_id, seed.wall_id, "start"))
        if upper:
            final_max = min(upper)
        else:
            final_max = interval_max
            open_ends.append((seed.link_id, seed.wall_id, "end"))
        if final_max - final_min <= TOL:
            forms.alert(
                "Khong tao duoc mat tuong ao tu Linked Wall Id {0}. "
                "Hay kiem tra cac seed Wall da pick.".format(seed.wall_id),
                exitscript=True)

        start_xy = add(origin, mul(direction, final_min))
        end_xy = add(origin, mul(direction, final_max))
        virtual_curve = Line.CreateBound(
            XYZ(start_xy.X, start_xy.Y, seed_z),
            XYZ(end_xy.X, end_xy.Y, seed_z))
        virtual_walls.append(VirtualWallInfo(
            seed, virtual_curve, len(accepted_ids)))
    return virtual_walls, open_ends


def element_point(element):
    location = element.Location
    if isinstance(location, LocationPoint):
        return xy(location.Point)
    if isinstance(location, LocationCurve) and location.Curve:
        return xy(location.Curve.Evaluate(0.5, True))
    box = element.get_BoundingBox(view) or element.get_BoundingBox(None)
    if box:
        return v2((box.Min.X + box.Max.X) * 0.5,
                  (box.Min.Y + box.Max.Y) * 0.5)
    return None


def wall_polyline(wall):
    """Tessellate a straight or curved Wall location curve into XY segments."""
    if wall.curve is None:
        return []
    points = [xy(point) for point in wall.curve.Tessellate()]
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)
            if distance(points[i], points[i + 1]) > TOL]


def proper_segment_intersection(a, b, c, d):
    """True only for an interior crossing; shared endpoints do not block."""
    r = sub(b, a)
    s = sub(d, c)
    denominator = r.X * s.Y - r.Y * s.X
    if abs(denominator) <= TOL:
        return False
    ca = sub(c, a)
    t = (ca.X * s.Y - ca.Y * s.X) / denominator
    u = (ca.X * r.Y - ca.Y * r.X) / denominator
    epsilon = 1.0e-6
    return epsilon < t < 1.0 - epsilon and epsilon < u < 1.0 - epsilon


def is_visible(start, end, boundary_segments, ignored_wall_id=None):
    """Line of sight is blocked when it crosses another selected Wall."""
    for wall_id, seg_start, seg_end in boundary_segments:
        if ignored_wall_id is not None and wall_id == ignored_wall_id:
            continue
        if proper_segment_intersection(start, end, seg_start, seg_end):
            return False
    return True


def wall_sample_points(wall):
    """Sample the complete bounded curve, including ends and concave corners."""
    if wall.curve is None:
        return []
    curve = wall.curve
    try:
        curve_length = curve.Length
    except Exception:
        curve_length = distance(xy(curve.GetEndPoint(0)), xy(curve.GetEndPoint(1)))
    divisions = max(1, int(math.ceil(curve_length / WALL_SAMPLE_STEP)))
    return [xy(curve.Evaluate(float(i) / divisions, True))
            for i in range(divisions + 1)]


def point_in_boundary(point, boundary_segments):
    """Even-odd containment for unordered wall segments, including holes.

    The segments must form closed loops. Curved walls are already tessellated.
    A point within 50 mm of a wall centreline is considered inside.
    """
    crossings = 0
    for unused_wall_id, start, end in boundary_segments:
        segment = sub(end, start)
        segment_size2 = dot(segment, segment)
        if segment_size2 > TOL:
            factor = max(0.0, min(1.0,
                dot(sub(point, start), segment) / segment_size2))
            projected = add(start, mul(segment, factor))
            if distance(point, projected) <= BOUNDARY_TOLERANCE:
                return True
        if (start.Y > point.Y) != (end.Y > point.Y):
            x_at_y = (start.X + (point.Y - start.Y) *
                      (end.X - start.X) / (end.Y - start.Y))
            if x_at_y > point.X:
                crossings += 1
    return crossings % 2 == 1


def wall_axis_point(wall, source):
    if wall.curve is None:
        return None
    curve = wall.curve
    source3 = XYZ(source.X, source.Y, curve.GetEndPoint(0).Z)
    result = curve.Project(source3)
    if result:
        return xy(result.XYZPoint)
    p0 = xy(curve.GetEndPoint(0))
    p1 = xy(curve.GetEndPoint(1))
    return p0 if distance(source, p0) <= distance(source, p1) else p1


def wall_face_point(wall, source):
    axis_point = wall_axis_point(wall, source)
    if axis_point is None:
        return None
    toward_source = unit(sub(source, axis_point))
    if toward_source is None:
        return axis_point
    half_width = max(0.0, wall.width * 0.5)
    if distance(axis_point, source) <= half_width:
        return source
    return add(axis_point, mul(toward_source, half_width))


def relative_neighbour_pairs(items, boundary_segments):
    """Relative Neighbourhood Graph: sparse neighbours without grid diagonals.

    Pair A-B is kept only if there is no C that is closer to both A and B than
    A and B are to each other. This works for rows, grids and irregular layouts.
    """
    count = len(items)
    pairs = []
    distances = [[0.0 for unused in range(count)] for unused in range(count)]
    visible = [[True for unused in range(count)] for unused in range(count)]
    for i in range(count):
        for j in range(i + 1, count):
            distances[i][j] = distances[j][i] = distance(
                items[i]["point"], items[j]["point"])
            visible[i][j] = visible[j][i] = is_visible(
                items[i]["point"], items[j]["point"], boundary_segments)
    for i in range(count):
        for j in range(i + 1, count):
            if not visible[i][j]:
                continue
            dij = distances[i][j]
            blocked = False
            for k in range(count):
                if k == i or k == j:
                    continue
                if (distances[i][k] < dij - TOL and
                        distances[j][k] < dij - TOL and
                        visible[i][k] and visible[j][k]):
                    blocked = True
                    break
            if not blocked:
                pairs.append((items[i], items[j], dij))
    return pairs


def view_plane_z():
    """Return the active plan level in HOST project/internal elevation.

    Level.Elevation can be based on Shared coordinates. ProjectElevation is
    stable for host geometry and linked-room comparisons in Revit 2025.
    """
    try:
        if view.GenLevel:
            try:
                return view.GenLevel.ProjectElevation
            except Exception:
                return view.GenLevel.Elevation
    except Exception:
        pass
    try:
        return view.Origin.Z
    except Exception:
        return 0.0


def region_loop(start, end, width, elevation):
    """Rectangle with one corner exactly at start (the Family centre)."""
    direction = unit(sub(end, start))
    if direction is None:
        return None
    side = mul(v2(-direction.Y, direction.X), width)
    planar = [start, end, add(end, side), add(start, side)]
    points = [XYZ(p.X, p.Y, elevation) for p in planar]
    loop = CurveLoop()
    for index in range(4):
        loop.Append(Line.CreateBound(points[index], points[(index + 1) % 4]))
    return loop


def create_preview_line(wall_face):
    """Create a bright temporary 1 km Detail Line on the active view."""
    data = line_data(wall_face)
    if data is None:
        return None
    origin, direction, seed_length, unused_z = data
    centre = add(origin, mul(direction, seed_length * 0.5))
    start = add(centre, mul(direction, -PREVIEW_HALF_LENGTH))
    end = add(centre, mul(direction, PREVIEW_HALF_LENGTH))
    elevation = view_plane_z()
    curve = Line.CreateBound(
        XYZ(start.X, start.Y, elevation),
        XYZ(end.X, end.Y, elevation))
    transaction = Transaction(doc, "Show picked wall face")
    transaction.Start()
    try:
        detail_curve = doc.Create.NewDetailCurve(view, curve)
        overrides = OverrideGraphicSettings()
        overrides.SetProjectionLineColor(Color(255, 0, 255))
        overrides.SetProjectionLineWeight(8)
        view.SetElementOverrides(detail_curve.Id, overrides)
        transaction.Commit()
        uidoc.RefreshActiveView()
        return detail_curve.Id
    except Exception:
        transaction.RollBack()
        return None


def delete_preview_lines(element_ids):
    if not element_ids:
        return
    transaction = Transaction(doc, "Remove picked wall previews")
    transaction.Start()
    try:
        for element_id in element_ids:
            if doc.GetElement(element_id) is not None:
                doc.Delete(element_id)
        transaction.Commit()
    except Exception:
        transaction.RollBack()


def pick_filled_regions_for_check():
    """Pick one or many Filled Regions and return unique valid host regions.

    PickObjects gives the standard Revit multi-selection workflow: keep picking
    regions, then click Finish. Cancel/Escape is handled by the caller.
    """
    references = uidoc.Selection.PickObjects(
        ObjectType.Element, FilledRegionFilter(),
        "Chon 1 hoac nhieu Filled Region de kiem tra, sau do nhan Finish")
    regions = []
    seen_ids = set()
    for reference in references:
        try:
            region = doc.GetElement(reference.ElementId)
            if not isinstance(region, FilledRegion):
                continue
            region_id = region.Id.IntegerValue
            if region_id in seen_ids:
                continue
            # Validate that the boundary can actually be read before accepting it.
            loops = region.GetBoundaries()
            if not loops:
                continue
            seen_ids.add(region_id)
            regions.append(region)
        except Exception:
            continue
    if not regions:
        forms.alert(
            "Khong co Filled Region hop le nao duoc chon.",
            title="Khong co vung kiem tra",
            exitscript=True)
    return regions


def collect_walls():
    walls = []
    preview_ids = []
    picked_keys = set()
    try:
        while True:
            try:
                reference = uidoc.Selection.PickObject(
                    ObjectType.PointOnElement, LinkedWallFilter(),
                    "Da pick {0} mat. Pick MAT TRONG tiep theo; ESC de ket thuc"
                    .format(len(walls)))
            except OperationCanceledException:
                break
            link_instance = doc.GetElement(reference.ElementId)
            link_doc = link_instance.GetLinkDocument()
            if link_doc is None:
                continue
            linked_wall = link_doc.GetElement(reference.LinkedElementId)
            if not isinstance(linked_wall, Wall):
                continue
            wall_face = LinkedWallFaceInfo(
                link_instance, linked_wall, reference)
            if wall_face.curve is None:
                forms.alert(
                    "Mat da pick khong phai mat dung cua Wall thang. "
                    "Hay pick lai mat trong.")
                continue
            if wall_face.key in picked_keys:
                continue
            picked_keys.add(wall_face.key)
            walls.append(wall_face)
            preview_id = create_preview_line(wall_face)
            if preview_id is not None:
                preview_ids.append(preview_id)
    finally:
        delete_preview_lines(preview_ids)
    if len(walls) < 3:
        forms.alert("Can pick it nhat 3 mat trong cua Linked Wall.",
                    exitscript=True)
    return walls


def make_boundary_segments(walls):
    result = []
    for wall in walls:
        for start, end in wall_polyline(wall):
            result.append((wall.key, start, end))
    return result


def count_unmatched_virtual_ends(walls):
    endpoints = []
    for index, wall in enumerate(walls):
        endpoints.append((index, xy(wall.curve.GetEndPoint(0))))
        endpoints.append((index, xy(wall.curve.GetEndPoint(1))))
    unmatched = 0
    join_tolerance = 100.0 / MM_PER_FOOT
    for index, point in endpoints:
        matched = False
        for other_index, other_wall in enumerate(walls):
            if other_index == index:
                continue
            start = xy(other_wall.curve.GetEndPoint(0))
            end = xy(other_wall.curve.GetEndPoint(1))
            segment = sub(end, start)
            size2 = dot(segment, segment)
            if size2 <= TOL:
                continue
            factor = max(0.0, min(1.0,
                dot(sub(point, start), segment) / size2))
            projected = add(start, mul(segment, factor))
            if distance(point, projected) <= join_tolerance:
                matched = True
                break
        if not matched:
            unmatched += 1
    return unmatched


def pick_sample_family():
    reference = uidoc.Selection.PickObject(
        ObjectType.Element, FamilyFilter(),
        "Chon 1 Family mau de tu dong quet")
    return doc.GetElement(reference.ElementId)


def choose_auto_scan_mode():
    selected = forms.alert(
        "Chon dieu kien tu dong quet Family nam trong Filled Region.",
        title="Nguon Family tu dong",
        options=[
            "Cung Family - Active View",
            "Cung Type - Active View"
        ],
        cancel=True)
    if not selected:
        script.exit()
    same_type = selected.startswith("Cung Type")
    return same_type, selected


def auto_collect_families(sample, boundary_segments, same_type):
    # Intentionally host Active View only. Linked FamilyInstances and elements
    # on other levels/views are never included.
    collector = FilteredElementCollector(doc, view.Id)
    candidates = (collector.OfClass(FamilyInstance)
                  .WhereElementIsNotElementType())
    sample_symbol = sample.Symbol
    sample_type_id = sample_symbol.Id.IntegerValue
    sample_family_id = sample_symbol.Family.Id.IntegerValue
    result = []
    for candidate in candidates:
        try:
            symbol = candidate.Symbol
            if same_type:
                matched = symbol.Id.IntegerValue == sample_type_id
            else:
                matched = symbol.Family.Id.IntegerValue == sample_family_id
            if not matched:
                continue
            point = element_point(candidate)
            if point and point_in_boundary(point, boundary_segments):
                result.append(candidate)
        except Exception:
            continue
    return result


def choose_region_type(title="Chon Filled Region Type",
                       button_name="Kiem tra"):
    types = list(FilteredElementCollector(doc).OfClass(FilledRegionType))
    if not types:
        forms.alert("Model khong co Filled Region Type.", exitscript=True)
    lookup = {}
    for item in types:
        type_name = None
        try:
            type_name = Element.Name.GetValue(item)
        except Exception:
            pass
        if not type_name:
            try:
                parameter = item.get_Parameter(
                    BuiltInParameter.SYMBOL_NAME_PARAM)
                if parameter:
                    type_name = parameter.AsString()
            except Exception:
                pass
        if not type_name:
            type_name = "Filled Region Type"
        label = "{0}  [Id {1}]".format(
            type_name, item.Id.IntegerValue)
        lookup[label] = item
    selected = forms.SelectFromList.show(
        sorted(lookup.keys()), title=title,
        multiselect=False, button_name=button_name)
    if not selected:
        script.exit()
    return lookup[selected]


def pick_revit_links():
    references = uidoc.Selection.PickObjects(
        ObjectType.Element, RevitLinkFilter(),
        "Chon cac Revit Link, sau do nhan Finish")
    links = []
    seen_ids = set()
    for reference in references:
        link_instance = doc.GetElement(reference.ElementId)
        link_id = link_instance.Id.IntegerValue
        if link_id in seen_ids:
            continue
        seen_ids.add(link_id)
        if link_instance.GetLinkDocument() is not None:
            links.append(link_instance)
    if not links:
        forms.alert("Khong co Revit Link da load nao duoc chon.",
                    exitscript=True)
    tool_config.last_link_ids = ",".join(
        str(item.Id.IntegerValue) for item in links)
    tool_config.last_link_count = len(links)
    script.save_config()
    return links


def room_level_in_host_z(room, link_instance, link_doc):
    """Return linked Room base elevation in HOST internal coordinates.

    Important: use the Room location point first. Level.Elevation can be based
    on Shared coordinates and can therefore be numerically different from the
    linked model's internal geometry elevation.
    """
    transform = link_instance.GetTotalTransform()
    try:
        location = room.Location
        if location is not None and isinstance(location, LocationPoint):
            return transform.OfPoint(location.Point).Z
    except Exception:
        pass
    try:
        level = link_doc.GetElement(room.LevelId)
        try:
            source_z = level.ProjectElevation
        except Exception:
            source_z = level.Elevation
        return transform.OfPoint(XYZ(0.0, 0.0, source_z)).Z
    except Exception:
        return None


def room_curve_loops_exact(room, transform):
    """Preserve the native Room boundary curves and transform to HOST.

    This is the preferred path. Tessellating arcs into many short line segments
    can create slivers/self-intersections that FilledRegion.Create rejects.
    """
    options = SpatialElementBoundaryOptions()
    boundaries = room.GetBoundarySegments(options)
    if not boundaries:
        return []
    loops = []
    for boundary in boundaries:
        curve_loop = CurveLoop()
        curve_count = 0
        failed = False
        for segment in boundary:
            try:
                source_curve = segment.GetCurve()
                transformed = source_curve.CreateTransformed(transform)
                curve_loop.Append(transformed)
                curve_count += 1
            except Exception:
                failed = True
                break
        if failed or curve_count < 1:
            continue
        try:
            if curve_loop.IsOpen():
                continue
        except Exception:
            pass
        loops.append(curve_loop)
    return loops


def room_curve_loops_tessellated(room, transform, elevation):
    """Fallback: rebuild Room boundaries as planar host-XY line loops."""
    options = SpatialElementBoundaryOptions()
    boundaries = room.GetBoundarySegments(options)
    if not boundaries:
        return []
    loops = []
    short_tolerance = doc.Application.ShortCurveTolerance
    for boundary in boundaries:
        vertices = []
        for segment in boundary:
            source_curve = segment.GetCurve()
            transformed = source_curve.CreateTransformed(transform)
            points = list(transformed.Tessellate())
            for point in points:
                flattened = XYZ(point.X, point.Y, elevation)
                if (not vertices or
                        vertices[-1].DistanceTo(flattened) > short_tolerance):
                    vertices.append(flattened)
        if (len(vertices) > 1 and
                vertices[-1].DistanceTo(vertices[0]) <= short_tolerance):
            vertices.pop()
        if len(vertices) < 3:
            continue
        curve_loop = CurveLoop()
        valid = True
        for index in range(len(vertices)):
            start = vertices[index]
            end = vertices[(index + 1) % len(vertices)]
            if start.DistanceTo(end) <= short_tolerance:
                valid = False
                break
            curve_loop.Append(Line.CreateBound(start, end))
        if valid:
            loops.append(curve_loop)
    return loops


def room_boundary_variants(room, transform, elevation):
    """Return progressively more forgiving boundary representations."""
    variants = []
    exact = room_curve_loops_exact(room, transform)
    if exact:
        variants.append(("native transformed curves", exact))
    tessellated = room_curve_loops_tessellated(room, transform, elevation)
    if tessellated:
        variants.append(("tessellated XY curves", tessellated))
    return variants


def to_curve_loop_list(loops):
    """Build a real .NET IList[CurveLoop]; safer in IronPython than List[T](py_list)."""
    result = List[CurveLoop]()
    for loop in loops:
        result.Add(loop)
    return result


def curve_loop_area_xy(curve_loop):
    """Approximate signed XY area from curve start points (loops are line-based here)."""
    points = []
    try:
        for curve in curve_loop:
            point = curve.GetEndPoint(0)
            points.append(point)
    except Exception:
        return 0.0
    if len(points) < 3:
        return 0.0
    area2 = 0.0
    for index in range(len(points)):
        current = points[index]
        following = points[(index + 1) % len(points)]
        area2 += current.X * following.Y - following.X * current.Y
    return abs(area2) * 0.5


def boundary_validation_text(loops):
    """Return (is_valid_or_unknown, diagnostic_text)."""
    if not loops:
        return False, "No CurveLoop"
    net_loops = to_curve_loop_list(loops)
    if BoundaryValidation is None:
        return None, "BoundaryValidation API unavailable; Create will validate"
    try:
        valid = BoundaryValidation.IsValidHorizontalBoundary(net_loops)
        return valid, ("Valid horizontal boundary" if valid else
                       "BoundaryValidation.IsValidHorizontalBoundary = False")
    except Exception as error:
        return None, "BoundaryValidation error: {0}".format(error)


def create_region_with_room_variants(region_type, variants):
    """Create FilledRegion using exact Room curves first, then fallbacks."""
    if not variants:
        raise Exception("Room has no usable boundary representation")

    errors = []
    for variant_name, loops in variants:
        if not loops:
            continue
        # Preserve Room API loop order for the first attempt. It normally gives
        # outer boundary first and inner openings after it. Only the fallback
        # needs area ordering to identify an outer loop.
        attempts = [("all loops", loops)]
        ordered = sorted(loops, key=curve_loop_area_xy, reverse=True)
        if len(ordered) > 1:
            attempts.append(("outer loop only", [ordered[0]]))

        for loop_mode, attempt_loops in attempts:
            valid, validation_text = boundary_validation_text(attempt_loops)
            if valid is False:
                errors.append("{0} / {1}: {2}".format(
                    variant_name, loop_mode, validation_text))
                continue
            try:
                region = FilledRegion.Create(
                    doc, region_type.Id, view.Id,
                    to_curve_loop_list(attempt_loops))
                return (region,
                        "{0} / {1}".format(variant_name, loop_mode),
                        validation_text)
            except Exception as error:
                errors.append("{0} / {1}: {2}; {3}".format(
                    variant_name, loop_mode, validation_text, str(error)))

    raise Exception(" | ".join(errors))


def simple_region_probe(region_type):
    """Prove whether the selected View + FilledRegionType can create ANY region.

    A small square is committed and verified, then deleted. If this fails, Room
    geometry is not the problem and the exact Revit API exception is reported.
    """
    size = 1000.0 / MM_PER_FOOT
    try:
        box = view.CropBox
        centre = XYZ((box.Min.X + box.Max.X) * 0.5,
                     (box.Min.Y + box.Max.Y) * 0.5,
                     view_plane_z())
    except Exception:
        try:
            origin = view.Origin
            centre = XYZ(origin.X, origin.Y, view_plane_z())
        except Exception:
            centre = XYZ(0.0, 0.0, view_plane_z())

    half = size * 0.5
    points = [
        XYZ(centre.X - half, centre.Y - half, centre.Z),
        XYZ(centre.X + half, centre.Y - half, centre.Z),
        XYZ(centre.X + half, centre.Y + half, centre.Z),
        XYZ(centre.X - half, centre.Y + half, centre.Z)
    ]
    loop = CurveLoop()
    for index in range(4):
        loop.Append(Line.CreateBound(points[index], points[(index + 1) % 4]))

    transaction = Transaction(doc, "Filled Region API probe")
    probe_id = None
    try:
        transaction.Start()
        probe = FilledRegion.Create(
            doc, region_type.Id, view.Id, to_curve_loop_list([loop]))
        probe_id = probe.Id
        status = transaction.Commit()
        if status != TransactionStatus.Committed:
            return False, "Probe transaction status: {0}".format(status)
        committed = doc.GetElement(probe_id)
        if committed is None or not committed.IsValidObject:
            return False, "Probe commit succeeded but element cannot be resolved"
    except Exception as error:
        try:
            if transaction.GetStatus() == TransactionStatus.Started:
                transaction.RollBack()
        except Exception:
            pass
        return False, str(error)

    cleanup = Transaction(doc, "Remove Filled Region API probe")
    try:
        cleanup.Start()
        if doc.GetElement(probe_id) is not None:
            doc.Delete(probe_id)
        cleanup.Commit()
    except Exception:
        try:
            if cleanup.GetStatus() == TransactionStatus.Started:
                cleanup.RollBack()
        except Exception:
            pass
    return True, "PASS"


def existing_auto_room_regions():
    regions_by_key = {}
    for region in (FilteredElementCollector(doc, view.Id)
                   .OfClass(FilledRegion)
                   .WhereElementIsNotElementType()):
        try:
            parameter = region.get_Parameter(
                BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            value = parameter.AsString() if parameter else None
            if value and value.startswith("AUTO_ROOM_FR|"):
                regions_by_key[value] = region
        except Exception:
            continue
    return regions_by_key


def get_solid_fill_pattern_id():
    for pattern_element in (FilteredElementCollector(doc)
                            .OfClass(FillPatternElement)):
        try:
            if pattern_element.GetFillPattern().IsSolidFill:
                return pattern_element.Id
        except Exception:
            continue
    return ElementId.InvalidElementId


def force_room_regions_visible(room_region_pairs):
    """Apply a bright per-element override and unhide the category."""
    if not room_region_pairs:
        return False, False
    category_unhidden = False
    override_failed = False
    transaction = Transaction(doc, "Make Room Filled Regions visible")
    try:
        transaction.Start()
        first_region = room_region_pairs[0][1]
        try:
            if view.GetCategoryHidden(first_region.Category.Id):
                view.SetCategoryHidden(first_region.Category.Id, False)
                category_unhidden = True
        except Exception:
            override_failed = True
        solid_fill_id = get_solid_fill_pattern_id()
        for unused_room, region in room_region_pairs:
            try:
                overrides = OverrideGraphicSettings()
                overrides.SetProjectionLineColor(Color(255, 0, 255))
                overrides.SetProjectionLineWeight(8)
                if solid_fill_id != ElementId.InvalidElementId:
                    overrides.SetSurfaceForegroundPatternId(solid_fill_id)
                    overrides.SetSurfaceForegroundPatternColor(
                        Color(0, 255, 255))
                    overrides.SetSurfaceTransparency(65)
                view.SetElementOverrides(region.Id, overrides)
            except Exception:
                override_failed = True
        status = transaction.Commit()
        if status != TransactionStatus.Committed:
            override_failed = True
    except Exception:
        try:
            if transaction.GetStatus() == TransactionStatus.Started:
                transaction.RollBack()
        except Exception:
            pass
        override_failed = True
    return category_unhidden, override_failed


def create_filled_regions_from_link_rooms(link_instance, region_type):
    link_doc = link_instance.GetLinkDocument()
    if view.GenLevel is None:
        forms.alert("Active View khong co GenLevel. Hay chay tren plan view.",
                    exitscript=True)

    probe_ok, probe_text = simple_region_probe(region_type)
    if not probe_ok:
        output.print_md("# FILLED REGION API PROBE: FAILED")
        output.print_md("- Active View: **{0}** [Id {1}]".format(
            safe_element_name(view, "Active View"), view.Id.IntegerValue))
        output.print_md("- Filled Region Type: **{0}** [Id {1}]".format(
            safe_element_name(region_type, "Filled Region Type"),
            region_type.Id.IntegerValue))
        output.print_md("- API error: **{0}**".format(probe_text))
        forms.alert(
            "Revit khong tao duoc ngay ca Filled Region hinh vuong test.\n\n"
            "Loi khong nam o Room boundary. Xem pyRevit report de biet "
            "chinh xac View/Type/API error.",
            title="Filled Region API Probe Failed")
        return {"created": 0, "reused": 0, "failed": 1,
                "host_ids": [], "regions": []}

    active_z = view_plane_z()
    level_tolerance = 1500.0 / MM_PER_FOOT
    transform = link_instance.GetTotalTransform()
    existing_regions = existing_auto_room_regions()
    candidates = []
    reused = []
    skipped_other_level = 0
    skipped_invalid = 0
    for room in (FilteredElementCollector(link_doc)
                 .OfCategory(BuiltInCategory.OST_Rooms)
                 .WhereElementIsNotElementType()):
        try:
            if room.Area <= TOL:
                skipped_invalid += 1
                continue
            room_z = room_level_in_host_z(room, link_instance, link_doc)
            if room_z is None or abs(room_z - active_z) > level_tolerance:
                skipped_other_level += 1
                continue
            key = "AUTO_ROOM_FR|{0}|{1}".format(
                link_instance.Id.IntegerValue, room.Id.IntegerValue)
            if key in existing_regions:
                reused.append((room, existing_regions[key]))
                continue
            variants = room_boundary_variants(
                room, transform, view_plane_z())
            if not variants:
                skipped_invalid += 1
                continue
            candidates.append((room, key, variants))
        except Exception:
            skipped_invalid += 1

    created = []
    failed = []
    create_notes = []
    category_was_unhidden = False
    category_unhide_failed = False
    for room, key, variants in candidates:
        transaction = Transaction(
            doc, "Create Filled Region for linked Room {0}".format(
                room.Id.IntegerValue))
        region_id = None
        try:
            transaction.Start()
            region, create_mode, validation_text = create_region_with_room_variants(
                region_type, variants)
            region_id = region.Id
            parameter = region.get_Parameter(
                BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if parameter and not parameter.IsReadOnly:
                parameter.Set(key)
            commit_status = transaction.Commit()
            if commit_status != TransactionStatus.Committed:
                failed.append((
                    room,
                    "Transaction commit status: {0}".format(commit_status)))
                continue
            committed_region = doc.GetElement(region_id)
            if committed_region is None or not committed_region.IsValidObject:
                failed.append((
                    room,
                    "Commit returned success but element does not exist"))
                continue
            created.append((room, committed_region))
            create_notes.append((room.Id.IntegerValue, create_mode, validation_text))
        except Exception as create_error:
            try:
                if transaction.GetStatus() == TransactionStatus.Started:
                    transaction.RollBack()
            except Exception:
                pass
            failed.append((room, str(create_error)))

    visible_regions = created + reused
    if visible_regions:
        category_was_unhidden, category_unhide_failed = (
            force_room_regions_visible(visible_regions))

        # Final proof: an ID is reported only when a fresh active-view
        # collector can resolve it after every transaction and override.
        active_region_ids = set(
            region.Id.IntegerValue
            for region in (FilteredElementCollector(doc, view.Id)
                           .OfClass(FilledRegion)
                           .WhereElementIsNotElementType()))
        verified_created = []
        for room, region in created:
            if region.Id.IntegerValue in active_region_ids:
                verified_created.append((room, region))
            else:
                failed.append((
                    room,
                    "Host Filled Region ID {0} is absent from Active View"
                    .format(region.Id.IntegerValue)))
        created = verified_created
        reused = [
            pair for pair in reused
            if pair[1].Id.IntegerValue in active_region_ids]
        visible_regions = created + reused
        created_ids = List[ElementId](
            [region.Id for unused_room, region in visible_regions])
        try:
            uidoc.Selection.SetElementIds(created_ids)
            uidoc.ShowElements(created_ids)
            uidoc.RefreshActiveView()
        except Exception:
            pass

    output.print_md("# TAO FILLED REGION TU LINKED ROOMS")
    output.print_md("- Revit Link: **{0}**".format(
        output.linkify(link_instance.Id)))
    active_view_name = safe_element_name(view, "Active View")
    output.print_md("- Tao tai Active View: **{0}** - View ID **{1}**".format(
        active_view_name, view.Id.IntegerValue))
    try:
        active_level_name = Element.Name.GetValue(view.GenLevel)
    except Exception:
        active_level_name = str(view.GenLevel.Id.IntegerValue)
    output.print_md("- Active View Level: **{0}**".format(
        active_level_name))
    output.print_md("- Filled Region API square probe: **PASS**")
    output.print_md("- Active host Z used: **{0:,.0f} mm**".format(
        millimetres(active_z)))
    output.print_md("- Filled Region da tao: **{0}**".format(len(created)))
    output.print_md("- Filled Region da ton tai, duoc hien lai: **{0}**".format(
        len(reused)))
    output.print_md("- Room Filled Region Type: **{0}**".format(
        safe_element_name(region_type, "Filled Region Type")))
    if category_was_unhidden:
        output.print_md("- Visibility: **Da bat lai category Filled Region**")
    if category_unhide_failed:
        output.print_md(
            "- Visibility: **Khong the doi category do View Template/setting**")
    output.print_md("- Room level khac da bo qua: **{0}**".format(
        skipped_other_level))
    output.print_md("- Room invalid/unbounded da bo qua: **{0}**".format(
        skipped_invalid))
    output.print_md("- Room tao that bai: **{0}**".format(len(failed)))
    fallback_count = len([note for note in create_notes
                          if "outer loop only" in note[1]])
    output.print_md("- Room phai fallback outer loop: **{0}**".format(
        fallback_count))
    host_ids = [str(region.Id.IntegerValue) for unused_room, region in created]
    output.print_md("\n## HOST FILLED REGION IDS")
    output.print_md(", ".join(host_ids) if host_ids else "Khong co")
    for room, region in created:
        try:
            owner_view_id = region.OwnerViewId
            owner_view = doc.GetElement(owner_view_id)
            owner_view_name = safe_element_name(owner_view, "Unknown View")
            owner_text = "{0} [View ID {1}]".format(
                owner_view_name, owner_view_id.IntegerValue)
        except Exception:
            owner_text = "Khong doc duoc Owner View"
        output.print_md(
            "- Linked Room Id {0} -> **HOST Filled Region ID {1}** {2}; "
            "Owner View: **{3}**"
            .format(room.Id.IntegerValue, region.Id.IntegerValue,
                    output.linkify(region.Id), owner_text))
    if create_notes:
        output.print_md("\n## BOUNDARY CREATE MODE")
        for room_id_value, create_mode, validation_text in create_notes:
            output.print_md(
                "- Linked Room Id {0}: **{1}** - {2}".format(
                    room_id_value, create_mode, validation_text))
    for room, error_text in failed:
        output.print_md(
            "- Linked Room Id {0}: **FAILED** - {1}".format(
                room.Id.IntegerValue, error_text))
    tool_config.last_auto_view_id = view.Id.IntegerValue
    tool_config.last_auto_view_name = active_view_name
    tool_config.last_auto_created_count = len(created)
    tool_config.last_auto_reused_count = len(reused)
    tool_config.last_auto_failed_count = len(failed)
    tool_config.last_auto_host_ids = ",".join(
        str(region.Id.IntegerValue) for unused_room, region in created)
    script.save_config()
    return {
        "created": len(created),
        "reused": len(reused),
        "failed": len(failed),
        "host_ids": [region.Id.IntegerValue
                     for unused_room, region in created],
        "regions": [region for unused_room, region in visible_regions]
    }


def millimetres(feet):
    return feet * MM_PER_FOOT


def filled_region_edges(filled_region):
    edges = []
    try:
        loops = filled_region.GetBoundaries()
    except Exception:
        loops = None
    if not loops:
        return edges
    for loop_index, curve_loop in enumerate(loops):
        for edge_index, curve in enumerate(curve_loop):
            edges.append(FilledRegionEdgeInfo(
                filled_region, curve, loop_index, edge_index))
    return edges


def region_geometry_info(filled_region):
    edges = filled_region_edges(filled_region)
    segments = make_boundary_segments(edges)
    return {
        "region": filled_region,
        "edges": edges,
        "segments": segments
    }


def auto_collect_families_for_regions(sample, region_infos, same_type):
    collector = FilteredElementCollector(doc, view.Id)
    candidates = (collector.OfClass(FamilyInstance)
                  .WhereElementIsNotElementType())
    sample_symbol = sample.Symbol
    sample_type_id = sample_symbol.Id.IntegerValue
    sample_family_id = sample_symbol.Family.Id.IntegerValue
    result = []
    for candidate in candidates:
        try:
            symbol = candidate.Symbol
            if same_type:
                matched = symbol.Id.IntegerValue == sample_type_id
            else:
                matched = symbol.Family.Id.IntegerValue == sample_family_id
            if not matched:
                continue
            point = element_point(candidate)
            if point is None:
                continue
            inside_any = False
            for info in region_infos:
                if info["segments"] and point_in_boundary(point, info["segments"]):
                    inside_any = True
                    break
            if inside_any:
                result.append(candidate)
        except Exception:
            continue
    return result


def circle_loop(center, radius, elevation):
    """Create a closed circle using four quarter arcs."""
    cx = center.X
    cy = center.Y
    z = elevation
    cardinal = [
        XYZ(cx + radius, cy, z),
        XYZ(cx, cy + radius, z),
        XYZ(cx - radius, cy, z),
        XYZ(cx, cy - radius, z)
    ]
    diagonal = radius / math.sqrt(2.0)
    mids = [
        XYZ(cx + diagonal, cy + diagonal, z),
        XYZ(cx - diagonal, cy + diagonal, z),
        XYZ(cx - diagonal, cy - diagonal, z),
        XYZ(cx + diagonal, cy - diagonal, z)
    ]
    loop = CurveLoop()
    for index in range(4):
        start = cardinal[index]
        end = cardinal[(index + 1) % 4]
        mid = mids[index]
        loop.Append(Arc.Create(start, end, mid))
    return loop


def set_comments(element, value):
    try:
        parameter = element.get_Parameter(
            BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if parameter and not parameter.IsReadOnly:
            parameter.Set(value)
    except Exception:
        pass


def delete_old_generated_results():
    ids = []
    for region in (FilteredElementCollector(doc, view.Id)
                   .OfClass(FilledRegion)
                   .WhereElementIsNotElementType()):
        try:
            parameter = region.get_Parameter(
                BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            value = parameter.AsString() if parameter else None
            if value and (value.startswith("AUTO_RADIUS_FR|") or
                          value.startswith("AUTO_UNCOVERED_FR|") or
                          (value.startswith("AUTO_WALL5M_FR|") or
                           value.startswith("AUTO_WALLDIST_FR|"))):
                ids.append(region.Id)
        except Exception:
            continue
    if not ids:
        return 0
    transaction = Transaction(doc, "Clear previous coverage results")
    transaction.Start()
    try:
        for element_id in ids:
            if doc.GetElement(element_id) is not None:
                doc.Delete(element_id)
        transaction.Commit()
        return len(ids)
    except Exception:
        transaction.RollBack()
        return 0


def create_radius_regions(items, region_type, radius):
    created = []
    errors = []
    if not items:
        return created, errors
    transaction = Transaction(doc, "Draw Family coverage radius")
    transaction.Start()
    try:
        elevation = view_plane_z()
        for item in items:
            family = item["element"]
            try:
                loop = circle_loop(item["point"], radius, elevation)
                region = FilledRegion.Create(
                    doc, region_type.Id, view.Id, to_curve_loop_list([loop]))
                set_comments(region, "AUTO_RADIUS_FR|{0}|{1:.3f}".format(
                    family.Id.IntegerValue, millimetres(radius)))
                created.append((family, region))
            except Exception as error:
                errors.append((family.Id.IntegerValue, str(error)))
        transaction.Commit()
    except Exception:
        transaction.RollBack()
        raise
    return created, errors


def region_curve_loops(region):
    try:
        return [loop for loop in region.GetBoundaries()]
    except Exception:
        return []


def create_extrusion_from_loops(loops, height):
    if not loops:
        return None
    return GeometryCreationUtilities.CreateExtrusionGeometry(
        to_curve_loop_list(loops), XYZ.BasisZ, height)


def _translated_curve_loop(source_loop, vector):
    """Copy one CurveLoop by translating every curve."""
    transform = Transform.CreateTranslation(vector)
    target = CurveLoop()
    for curve in source_loop:
        target.Append(curve.CreateTransformed(transform))
    return target


def uncovered_top_face_loop_sets(uncovered_solid, target_elevation):
    """Return one CurveLoop set per horizontal top face of an uncovered solid.

    Boolean subtraction can split one Room into several disconnected uncovered
    islands. Each horizontal top PlanarFace represents one drawable island; its
    inner loops represent covered holes. Loops are shifted onto the Active View
    sketch plane before FilledRegion.Create is called.
    """
    result = []
    if uncovered_solid is None:
        return result
    for face in uncovered_solid.Faces:
        if not isinstance(face, PlanarFace):
            continue
        try:
            normal = face.FaceNormal
            if normal.Z < 0.999:
                continue
            origin_z = face.Origin.Z
            shift = target_elevation - origin_z
            loops = []
            for source_loop in face.GetEdgesAsCurveLoops():
                loops.append(_translated_curve_loop(
                    source_loop, XYZ(0.0, 0.0, shift)))
            if loops:
                result.append(loops)
        except Exception:
            continue
    return result


def coverage_boolean_check(region, room_items, radius):
    """Exact coverage check.

    Extrude the Room Filled Region and subtract one cylinder for every Family
    radius. The remaining solid is exactly the uncovered area. Returns:
      (covered, uncovered_area_ft2, note, uncovered_loop_sets)
    where uncovered_loop_sets can be passed to FilledRegion.Create.
    """
    loops = region_curve_loops(region)
    if not loops:
        return False, None, "Filled Region has no readable boundaries", []
    height = 1.0
    try:
        uncovered = create_extrusion_from_loops(loops, height)
        if uncovered is None:
            return False, None, "Cannot create room extrusion", []
        elevation = view_plane_z()
        for item in room_items:
            circle = circle_loop(item["point"], radius, elevation)
            circle_solid = create_extrusion_from_loops([circle], height)
            uncovered = BooleanOperationsUtils.ExecuteBooleanOperation(
                uncovered, circle_solid, BooleanOperationsType.Difference)
            try:
                if uncovered.Volume <= 1.0e-8:
                    return True, 0.0, "Boolean solid coverage", []
            except Exception:
                pass
        volume = uncovered.Volume
        area_ft2 = max(0.0, volume / height)
        covered = area_ft2 <= 1.0e-6
        if covered:
            return True, area_ft2, "Boolean solid coverage", []
        loop_sets = uncovered_top_face_loop_sets(uncovered, elevation)
        if not loop_sets:
            return None, None, (
                "Boolean area found but uncovered top-face boundary extraction failed"), []
        return False, area_ft2, "Boolean solid coverage + exact uncovered boundary", loop_sets
    except Exception as error:
        return None, None, "Boolean coverage failed: {0}".format(error), []


def sample_region_bbox(region):
    try:
        box = region.get_BoundingBox(view) or region.get_BoundingBox(None)
        return box
    except Exception:
        return None


def coverage_sampling_check(info, room_items, radius):
    """Fallback coverage check and approximate uncovered cells.

    Returns (covered, uncovered_count, note, uncovered_points). The points are
    converted into 100 mm square Filled Regions only when Boolean geometry cannot
    provide exact uncovered boundaries.
    """
    region = info["region"]
    segments = info["segments"]
    if not segments:
        return False, None, "No boundary segments", []
    box = sample_region_bbox(region)
    if box is None:
        return False, None, "No Filled Region bounding box", []
    step = 100.0 / MM_PER_FOOT
    uncovered_points = []
    total_count = 0
    x = box.Min.X + step * 0.5
    while x <= box.Max.X + TOL:
        y = box.Min.Y + step * 0.5
        while y <= box.Max.Y + TOL:
            point = v2(x, y)
            if point_in_boundary(point, segments):
                total_count += 1
                if not any(distance(point, item["point"]) <= radius + TOL
                           for item in room_items):
                    uncovered_points.append(point)
            y += step
        x += step
    covered = len(uncovered_points) == 0
    return covered, len(uncovered_points), (
        "100 mm sampling fallback ({0} interior points)".format(total_count)), uncovered_points


def check_region_coverage(info, room_items, radius):
    # A Room with no matching Family is fully uncovered. Boolean geometry is still
    # useful because it gives us the exact Room boundary for the fail Filled Region.
    covered, uncovered_area_ft2, note, loop_sets = coverage_boolean_check(
        info["region"], room_items, radius)
    if covered is not None:
        return covered, uncovered_area_ft2, note, loop_sets, []
    fallback_covered, uncovered_count, fallback_note, uncovered_points = (
        coverage_sampling_check(info, room_items, radius))
    return (fallback_covered, None, "{0}; {1}".format(note, fallback_note),
            [], uncovered_points)


def square_loop(center, half_size, elevation):
    points = [
        XYZ(center.X - half_size, center.Y - half_size, elevation),
        XYZ(center.X + half_size, center.Y - half_size, elevation),
        XYZ(center.X + half_size, center.Y + half_size, elevation),
        XYZ(center.X - half_size, center.Y + half_size, elevation)
    ]
    loop = CurveLoop()
    for index in range(4):
        loop.Append(Line.CreateBound(points[index], points[(index + 1) % 4]))
    return loop



def fallback_uncovered_strip_loops(points, step, elevation):
    """Merge 100 mm fallback cells into horizontal strips to avoid thousands
    of individual Filled Regions when Boolean geometry is unavailable.
    """
    if not points:
        return []
    rows = {}
    for point in points:
        key = int(round(point.Y / step))
        rows.setdefault(key, []).append(point)
    loops = []
    half = step * 0.5
    for row_points in rows.values():
        ordered = sorted(row_points, key=lambda p: p.X)
        run_start = ordered[0]
        run_end = ordered[0]
        for point in ordered[1:]:
            if point.X - run_end.X <= step * 1.5:
                run_end = point
                continue
            centre = v2((run_start.X + run_end.X) * 0.5, run_start.Y)
            half_x = (run_end.X - run_start.X) * 0.5 + half
            corners = [
                XYZ(centre.X - half_x, centre.Y - half, elevation),
                XYZ(centre.X + half_x, centre.Y - half, elevation),
                XYZ(centre.X + half_x, centre.Y + half, elevation),
                XYZ(centre.X - half_x, centre.Y + half, elevation)
            ]
            loop = CurveLoop()
            for i in range(4):
                loop.Append(Line.CreateBound(corners[i], corners[(i + 1) % 4]))
            loops.append(loop)
            run_start = point
            run_end = point
        centre = v2((run_start.X + run_end.X) * 0.5, run_start.Y)
        half_x = (run_end.X - run_start.X) * 0.5 + half
        corners = [
            XYZ(centre.X - half_x, centre.Y - half, elevation),
            XYZ(centre.X + half_x, centre.Y - half, elevation),
            XYZ(centre.X + half_x, centre.Y + half, elevation),
            XYZ(centre.X - half_x, centre.Y + half, elevation)
        ]
        loop = CurveLoop()
        for i in range(4):
            loop.Append(Line.CreateBound(corners[i], corners[(i + 1) % 4]))
        loops.append(loop)
    return loops

def create_uncovered_regions(coverage_results, region_type):
    """Draw ONLY actual uncovered portions. No Family radius circles are drawn."""
    created = []
    errors = []
    if not coverage_results:
        return created, errors
    transaction = Transaction(doc, "Draw uncovered coverage areas")
    transaction.Start()
    try:
        elevation = view_plane_z()
        for result in coverage_results:
            if result["covered"]:
                continue
            source_region = result["region"]
            loop_sets = result.get("uncovered_loop_sets") or []
            points = result.get("fallback_uncovered_points") or []
            if loop_sets:
                for island_index, loops in enumerate(loop_sets):
                    try:
                        marker = FilledRegion.Create(
                            doc, region_type.Id, view.Id,
                            to_curve_loop_list(loops))
                        set_comments(marker,
                            "AUTO_UNCOVERED_FR|{0}|{1}".format(
                                source_region.Id.IntegerValue, island_index + 1))
                        created.append(marker)
                    except Exception as error:
                        errors.append(
                            "Region {0}, uncovered island {1}: {2}".format(
                                source_region.Id.IntegerValue, island_index + 1, error))
            elif points:
                # Boolean failed: merge 100 mm uncovered cells into horizontal
                # strips. This remains an approximate fallback but avoids creating
                # thousands of tiny Filled Region elements.
                step = 100.0 / MM_PER_FOOT
                strip_loops = fallback_uncovered_strip_loops(
                    points, step, elevation)
                for strip_index, loop in enumerate(strip_loops):
                    try:
                        marker = FilledRegion.Create(
                            doc, region_type.Id, view.Id,
                            to_curve_loop_list([loop]))
                        set_comments(marker,
                            "AUTO_UNCOVERED_FR|{0}|S{1}".format(
                                source_region.Id.IntegerValue, strip_index + 1))
                        created.append(marker)
                    except Exception as error:
                        errors.append(
                            "Region {0}, fallback strip {1}: {2}".format(
                                source_region.Id.IntegerValue, strip_index + 1, error))
        transaction.Commit()
    except Exception:
        transaction.RollBack()
        raise
    return created, errors


def create_wall_clearance_failures(region_infos, items, result_region_type, wall_distance_mm):
    """Check each boundary-layer Family only once, to its NEAREST Room boundary.

    The boundary-facing Family layer is still detected with the existing
    boundary sampling/Voronoi logic. However, after a Family has been identified
    as belonging to that outer layer, the distance rule is NOT checked separately
    against every edge that selected it. Instead, for that Family and that Room,
    the nearest point on ALL Room/Filled Region boundary edges is found and only
    that minimum distance is compared with the user-defined maximum distance.
    """
    wall_limit = wall_distance_mm / MM_PER_FOOT
    failures = []
    outer_ids = set()
    items_by_id = dict((item["element"].Id.IntegerValue, item) for item in items)

    for info in region_infos:
        room_items = [item for item in items
                      if point_in_boundary(item["point"], info["segments"])]
        if not room_items:
            continue

        # STEP 1: Preserve the original logic that decides which Families belong
        # to the boundary-facing (outer) layer. Multiple edges may nominate the
        # same Family, but we only keep the Family Id from this step.
        outer_family_ids = set()
        for edge in info["edges"]:
            wall_key = edge.key
            for sample in wall_sample_points(edge):
                nearest = None
                nearest_distance = None
                for item in room_items:
                    if not is_visible(sample, item["point"], info["segments"],
                                      ignored_wall_id=wall_key):
                        continue
                    current = distance(sample, item["point"])
                    if nearest_distance is None or current < nearest_distance:
                        nearest = item
                        nearest_distance = current
                if nearest is not None:
                    outer_family_ids.add(
                        nearest["element"].Id.IntegerValue)

        # STEP 2: Check each outer Family ONCE. Find its minimum distance to the
        # entire Room/Filled Region boundary, including inner loops when present.
        for family_id_value in outer_family_ids:
            item = items_by_id.get(family_id_value)
            if item is None:
                continue

            nearest_edge = None
            nearest_face = None
            nearest_gap = None
            for edge in info["edges"]:
                face = wall_face_point(edge, item["point"])
                if face is None:
                    continue
                gap = distance(item["point"], face)
                if nearest_gap is None or gap < nearest_gap:
                    nearest_gap = gap
                    nearest_edge = edge
                    nearest_face = face

            if nearest_gap is None or nearest_edge is None or nearest_face is None:
                continue

            outer_ids.add(family_id_value)
            if nearest_gap > wall_limit:
                failures.append({
                    "family": item["element"],
                    "region": info["region"],
                    "edge": nearest_edge,
                    "start": item["point"],
                    "end": nearest_face,
                    "distance": nearest_gap,
                    "limit": wall_limit
                })

    created = []
    errors = []
    if failures:
        transaction = Transaction(doc, "Mark boundary clearance failure")
        transaction.Start()
        try:
            elevation = view_plane_z()
            for failure in failures:
                try:
                    loop = region_loop(failure["start"], failure["end"],
                                       REGION_WIDTH, elevation)
                    if loop is None:
                        continue
                    marker = FilledRegion.Create(
                        doc, result_region_type.Id, view.Id,
                        to_curve_loop_list([loop]))
                    set_comments(marker, "AUTO_WALLDIST_FR|{0}|{1}".format(
                        failure["family"].Id.IntegerValue,
                        failure["region"].Id.IntegerValue))
                    created.append(marker)
                except Exception as error:
                    errors.append(str(error))
            transaction.Commit()
        except Exception:
            transaction.RollBack()
            raise
    return failures, outer_ids, created, errors


def square_metres_from_ft2(area_ft2):
    if area_ft2 is None:
        return None
    return area_ft2 * 0.09290304


def run_coverage_check(source_regions, coverage_region_type,
                       result_region_type, coverage_radius_mm, wall_distance_mm):
    region_infos = [region_geometry_info(region) for region in source_regions]
    region_infos = [info for info in region_infos if info["segments"]]
    if not region_infos:
        forms.alert("Khong doc duoc boundary cua Filled Region Room.",
                    exitscript=True)

    sample_family = pick_sample_family()
    same_type, scan_mode = choose_auto_scan_mode()
    families = auto_collect_families_for_regions(
        sample_family, region_infos, same_type)
    items = []
    for family in families:
        point = element_point(family)
        if point is not None:
            items.append({"element": family, "point": point})
    # Do not abort when no matching Family lies inside the selected regions.
    # In that case every selected Room/Region is correctly treated as 100%
    # uncovered and will receive a fail Filled Region over its whole area.

    radius = coverage_radius_mm / MM_PER_FOOT
    cleared_old = delete_old_generated_results()

    # Check coverage FIRST. We no longer draw Family-radius circles. For every
    # failed Room/Region, retain only the actual uncovered geometry.
    coverage_results = []
    for info in region_infos:
        room_items = [item for item in items
                      if point_in_boundary(item["point"], info["segments"])]
        (covered, uncovered_area_ft2, method, uncovered_loop_sets,
         fallback_uncovered_points) = check_region_coverage(
            info, room_items, radius)
        coverage_results.append({
            "region": info["region"],
            "family_count": len(room_items),
            "covered": covered,
            "uncovered_area_ft2": uncovered_area_ft2,
            "method": method,
            "uncovered_loop_sets": uncovered_loop_sets,
            "fallback_uncovered_points": fallback_uncovered_points
        })

    # Create Filled Regions ONLY where coverage is missing.
    uncovered_regions, uncovered_errors = create_uncovered_regions(
        coverage_results, coverage_region_type)

    # Boundary-distance markers are created only for actual failures.
    wall_failures, outer_ids, wall_markers, wall_errors = (
        create_wall_clearance_failures(
            region_infos, items, result_region_type, wall_distance_mm))

    output.print_md("# KIEM TRA DO PHU FAMILY THEO ROOM")
    output.print_md("- Che do Family: **{0}**".format(scan_mode))
    output.print_md("- Family mau: **{0}**".format(
        output.linkify(sample_family.Id)))
    output.print_md("- Family trong vung kiem tra: **{0}**".format(len(items)))
    output.print_md("- Ban kinh phu: **{0:,.0f} mm**".format(
        coverage_radius_mm))
    output.print_md("- Filled Region VUNG KHONG PHU da tao: **{0}**".format(
        len(uncovered_regions)))
    output.print_md("- Tool khong ve vong tron ban kinh quanh Family; chi ve phan dien tich FAIL.")
    output.print_md("- Filled Region khoang cach boundary FAIL da tao: **{0}**".format(
        len(wall_markers)))
    output.print_md("- Ket qua cu tu lan chay truoc da xoa: **{0}**".format(
        cleared_old))
    output.print_md("- Family thuoc lop gan boundary: **{0}**".format(
        len(outer_ids)))
    output.print_md("- Family lop ngoai chi check **khoang cach GAN NHAT** toi boundary Room; gioi han: **{0:,.0f} mm**".format(wall_distance_mm))

    output.print_md("\n## DO PHU TUNG ROOM / FILLED REGION")
    covered_count = 0
    for index, result in enumerate(coverage_results, 1):
        region = result["region"]
        status = "PASS - PHU KIN" if result["covered"] else "FAIL - CON VUNG KHONG PHU"
        if result["covered"]:
            covered_count += 1
        area_m2 = square_metres_from_ft2(result["uncovered_area_ft2"])
        area_text = ("; dien tich chua phu ~ **{0:,.3f} m2**".format(area_m2)
                     if area_m2 is not None else "")
        output.print_md(
            "{0}. Region {1}: **{2}**; Family = **{3}**{4}; `{5}`".format(
                index, output.linkify(region.Id), status,
                result["family_count"], area_text, result["method"]))

    output.print_md("\n## KIEM TRA KHOANG CACH GAN NHAT TOI BOUNDARY")
    if not wall_failures:
        output.print_md("**PASS - Tat ca Family lop ngoai co boundary gan nhat <= {0:,.0f} mm.**".format(wall_distance_mm))
    else:
        output.print_md("**FAIL - {0} Family lop ngoai co boundary gan nhat > {1:,.0f} mm**".format(
            len(wall_failures), wall_distance_mm))
        for index, failure in enumerate(wall_failures, 1):
            output.print_md(
                "{0}. Family {1} -> boundary GAN NHAT cua Room Region {2}, Loop {3}, Edge {4}: "
                "**{5:,.0f} mm**".format(
                    index, output.linkify(failure["family"].Id),
                    output.linkify(failure["region"].Id),
                    failure["edge"].loop_index + 1,
                    failure["edge"].edge_index + 1,
                    millimetres(failure["distance"])))

    if uncovered_errors:
        output.print_md("\n## LOI TAO FILLED REGION VUNG KHONG PHU")
        for error_text in uncovered_errors[:20]:
            output.print_md("- `{0}`".format(error_text))
    if wall_errors:
        output.print_md("\n## LOI TAO FILLED REGION KHOANG CACH BOUNDARY")
        for error_text in wall_errors[:20]:
            output.print_md("- `{0}`".format(error_text))

    output.print_md("\n## TONG KET")
    output.print_md("- Room/Region phu kin: **{0}/{1}**".format(
        covered_count, len(coverage_results)))
    output.print_md("- Room/Region con vung khong phu: **{0}**".format(
        len(coverage_results) - covered_count))
    output.print_md("- Filled Region vung khong phu da ve: **{0}**".format(
        len(uncovered_regions)))
    output.print_md("- Vi tri Family bien > {0:,.0f} mm: **{1}**".format(
        wall_distance_mm, len(wall_failures)))

    tool_config.last_coverage_radius_mm = coverage_radius_mm
    tool_config.last_coverage_room_count = len(coverage_results)
    tool_config.last_coverage_pass_count = covered_count
    tool_config.last_wall_distance_mm = wall_distance_mm
    tool_config.last_wall_distance_fail_count = len(wall_failures)
    script.save_config()

    try:
        output.set_title("Room Coverage + Boundary Distance Report")
        output.show()
        output.center()
    except Exception:
        pass


run_mode, room_region_type, coverage_region_type, result_region_type, coverage_radius_mm, wall_distance_mm = choose_run_mode()


def show_create_rooms_summary(link_count, total_created, total_reused, total_failed):
    output.print_md("\n# TAO FILLED REGION THEO ROOM - HOAN TAT")
    output.print_md("- Revit Link da xu ly: **{0}**".format(link_count))
    output.print_md("- Room Filled Region moi: **{0}**".format(total_created))
    output.print_md("- Room Filled Region da ton tai/tai su dung: **{0}**".format(total_reused))
    output.print_md("- Room tao that bai: **{0}**".format(total_failed))
    output.print_md("- View: **{0}** [View ID {1}]".format(
        safe_element_name(view, "Active View"), view.Id.IntegerValue))
    output.print_md("\n> Buoc tao Room Filled Region da ket thuc. Tool KHONG chay Coverage trong che do nay.")
    try:
        output.set_title("Create Room Filled Regions")
        output.show()
        output.center()
    except Exception:
        pass


# MODE 1 - CREATE ONLY. This branch intentionally exits before any Family scan
# or coverage function can run.
if run_mode == "create_rooms":
    try:
        selected_links = pick_revit_links()
        total_created = 0
        total_reused = 0
        total_failed = 0
        for selected_link in selected_links:
            link_result = create_filled_regions_from_link_rooms(
                selected_link, room_region_type)
            total_created += link_result["created"]
            total_reused += link_result["reused"]
            total_failed += link_result["failed"]

        show_create_rooms_summary(
            len(selected_links), total_created, total_reused, total_failed)
        forms.alert(
            "Da ket thuc buoc TAO ROOM FILLED REGION.\n"
            "Tao moi: {0}\nDa ton tai: {1}\nThat bai: {2}\n\n"
            "Tool chua chay kiem tra Coverage.".format(
                total_created, total_reused, total_failed),
            title="Create Room Filled Region")
    except Exception as error:
        text = str(error).lower()
        if "cancel" in text or "operation" in text:
            script.exit()
        raise
    script.exit()


# MODE 2/3 - CHECK ONLY. No Revit Link Room creation is called here.
source_regions = []

if run_mode == "check_auto_rooms":
    auto_regions = existing_auto_room_regions()
    source_regions = [region for region in auto_regions.values()
                      if region is not None and region.IsValidObject]
    # Defensive de-duplication by host ElementId.
    unique = {}
    for region in source_regions:
        unique[region.Id.IntegerValue] = region
    source_regions = list(unique.values())
    if not source_regions:
        forms.alert(
            "Active View khong co Filled Region Room do tool tao truoc do.\n"
            "Hay chay che do 'Tao Filled Region theo Room tu Revit Link' truoc.",
            title="Khong co Room Filled Region",
            exitscript=True)
    output.print_md("# NGUON KIEM TRA COVERAGE")
    output.print_md("- Tu dong lay Room Filled Region da tao tren Active View: **{0}**".format(
        len(source_regions)))

elif run_mode == "check_existing_region":
    try:
        source_regions = pick_filled_regions_for_check()
        output.print_md("# NGUON KIEM TRA COVERAGE")
        output.print_md(
            "- Filled Region pick thu cong: **{0}**".format(len(source_regions)))
    except Exception as error:
        text = str(error).lower()
        if "cancel" in text or "operation" in text:
            script.exit()
        raise

run_coverage_check(source_regions, coverage_region_type,
                   result_region_type, coverage_radius_mm, wall_distance_mm)
