# -*- coding: utf-8 -*-
from __future__ import division

import heapq
import math
import traceback
import unicodedata

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit import DB
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List

from pyrevit import revit, forms, script


doc = revit.doc
uidoc = revit.uidoc
view = revit.active_view
output = script.get_output()


# =============================================================================
# USER SETTINGS
# =============================================================================
DEFAULT_MAX_DISTANCE_M = 40.0
DEFAULT_GRID_MM = 400.0
DEFAULT_CLEARANCE_MM = 100.0
DEFAULT_AUTO_SCAN = False
DEFAULT_CABINET_KEYWORDS = 'FHR; Fire Hose Reel; Hose Reel; Fire Cabinet; Tu Chua Chay'

# Walking obstacle height band, relative to the active view level.
ANALYSIS_BOTTOM_MM = 0.0
ANALYSIS_TOP_MM = 2000.0

# Cabinet source-point settings. The source point is calculated from the
# centre of the cabinet's physical geometry. FacingOrientation is used only
# to prefer a walkable cell on the front side of a wall-hosted cabinet.
CABINET_SEARCH_RADIUS_MM = 1500.0

# Door portal settings. Doors are carved from the wall raster AFTER all wall
# segments are blocked. This is more reliable than asking whether a cell centre
# happens to fall inside a small door rectangle while the wall is rasterized.
DOOR_DEFAULT_WIDTH_MM = 900.0
DOOR_MIN_VALID_WIDTH_MM = 300.0
DOOR_MAX_VALID_WIDTH_MM = 5000.0
DOOR_PORTAL_WIDTH_EXTRA_MM = 25.0
DOOR_PORTAL_DEPTH_EXTRA_CELLS = 1.25
DOOR_RASTER_SAMPLE_STEP_FACTOR = 0.25
DOOR_NEAREST_WALL_SEARCH_MM = 1500.0
INCLUDE_WALL_OPENING_ELEMENTS = True

# Performance and output limits.
MAX_GRID_CELLS = 150000
MAX_RESULT_REGIONS = 4000
MIN_SOLID_VOLUME_FT3 = 1.0e-7
MIN_BOOLEAN_INTERSECTION_VOLUME_FT3 = 1.0e-10
EXTRUSION_DEPTH_FT = 1.0

# Hybrid narrow-phase settings. The fast ray test handles obvious hits.
# Exact Boolean intersection is called only for ambiguous cells whose expanded
# footprint overlaps both the element and solid bounding boxes.
USE_FAST_RAY_NARROW_PHASE = True
USE_EXACT_BOOLEAN_NARROW_PHASE = True
CONSERVATIVE_ON_BOOLEAN_FAILURE = False
MAX_CELL_PRISM_CACHE = 5000

INCLUDE_REVIT_LINKS = True
DELETE_OLD_RESULTS = True
RESULT_COMMENT_PREFIX = 'PYREVIT_FIRE_COVERAGE_RESULT'
RESULT_COMMENT = 'PYREVIT_FIRE_COVERAGE_OPTIMIZED'

# Cabinet diagnostic markers are view-specific Detail Lines used only so the
# pyRevit output can zoom to the exact host-model cabinet check point.
# Existing markers are deleted and rebuilt every time the tool runs.
DIAGNOSTIC_MARKER_COMMENT_PREFIX = 'PYREVIT_FIRE_CABINET_DIAGNOSTIC'
DIAGNOSTIC_MARKER_HALF_SIZE_MM = 150.0


# =============================================================================
# BASIC HELPERS
# =============================================================================
def mm_to_ft(value_mm):
    return float(value_mm) / 304.8


def m_to_ft(value_m):
    return float(value_m) / 0.3048


def ft2_to_m2(value_ft2):
    return float(value_ft2) * 0.09290304


def ft_to_mm(value_ft):
    return float(value_ft) * 304.8


def markdown_cell(value):
    # Keep diagnostic tables valid when Revit names contain pipe or newlines.
    value = to_text(value) if 'to_text' in globals() else str(value or '')
    return value.replace(u'|', u'\\|').replace(u'\r', u' ').replace(u'\n', u' ')


def eid_int(element_id):
    try:
        return element_id.IntegerValue
    except Exception:
        try:
            return int(element_id.Value)
        except Exception:
            return int(str(element_id))


def app_version_major():
    try:
        return int(str(doc.Application.VersionNumber).split('.')[0])
    except Exception:
        return 0


def ask_positive_number(prompt, default_value, title):
    value = forms.ask_for_string(
        default=str(default_value),
        prompt=prompt,
        title=title
    )
    if value is None:
        script.exit()
    try:
        number = float(value.strip().replace(',', '.'))
    except Exception:
        forms.alert('Invalid numeric value.', exitscript=True)
    if number <= 0.0:
        forms.alert('Value must be greater than zero.', exitscript=True)
    return number


def ask_nonnegative_number(prompt, default_value, title):
    value = forms.ask_for_string(
        default=str(default_value),
        prompt=prompt,
        title=title
    )
    if value is None:
        script.exit()
    try:
        number = float(value.strip().replace(',', '.'))
    except Exception:
        forms.alert('Invalid numeric value.', exitscript=True)
    if number < 0.0:
        forms.alert('Value cannot be negative.', exitscript=True)
    return number


def normalize_2d(x, y):
    length = math.sqrt(x * x + y * y)
    if length < 1.0e-12:
        return None
    return (x / length, y / length)


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def point_segment_distance_sq(px, py, ax, ay, bx, by):
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    vv = vx * vx + vy * vy
    if vv < 1.0e-16:
        dx = px - ax
        dy = py - ay
        return dx * dx + dy * dy
    t = (wx * vx + wy * vy) / vv
    t = clamp(t, 0.0, 1.0)
    qx = ax + t * vx
    qy = ay + t * vy
    dx = px - qx
    dy = py - qy
    return dx * dx + dy * dy


def point_on_segment(px, py, ax, ay, bx, by, tol=1.0e-8):
    if point_segment_distance_sq(px, py, ax, ay, bx, by) > tol * tol:
        return False
    return (
        px >= min(ax, bx) - tol and px <= max(ax, bx) + tol and
        py >= min(ay, by) - tol and py <= max(ay, by) + tol
    )


def polygon_contains(poly, x, y):
    count = len(poly)
    if count < 3:
        return False, False

    inside = False
    j = count - 1
    for i in range(count):
        xi, yi = poly[i]
        xj, yj = poly[j]

        if point_on_segment(x, y, xi, yi, xj, yj):
            return True, True

        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i

    return inside, False


def region_contains(polygons, x, y):
    # Odd-even rule supports an outer boundary plus internal holes.
    inside = False
    for poly in polygons:
        loop_inside, on_boundary = polygon_contains(poly, x, y)
        if on_boundary:
            return True
        if loop_inside:
            inside = not inside
    return inside


def category_value(name):
    try:
        return getattr(DB.BuiltInCategory, name)
    except Exception:
        return None


def make_id_list(ids):
    result = List[DB.ElementId]()
    for item in ids:
        result.Add(item)
    return result


# =============================================================================
# SELECTION FILTERS
# =============================================================================
class FilledRegionSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, DB.FilledRegion)

    def AllowReference(self, reference, point):
        return False


class CabinetSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, DB.FamilyInstance)

    def AllowReference(self, reference, point):
        return False



# =============================================================================
# VALIDATE VIEW, PICK COMPARTMENT AND SHOW SETTINGS UI
# =============================================================================
if not isinstance(view, DB.ViewPlan):
    forms.alert('Run this tool in a plan view.', exitscript=True)

if view.IsTemplate:
    forms.alert('The active view cannot be a view template.', exitscript=True)

try:
    if abs(view.ViewDirection.Normalize().Z) < 0.99:
        forms.alert('The active plan view must be horizontal.', exitscript=True)
except Exception:
    pass

try:
    compartment_ref = uidoc.Selection.PickObject(
        ObjectType.Element,
        FilledRegionSelectionFilter(),
        'Pick the compartment Filled Region.'
    )
    compartment = doc.GetElement(compartment_ref.ElementId)
except OperationCanceledException:
    script.exit()

if compartment.OwnerViewId != view.Id:
    forms.alert('The compartment Filled Region must belong to the active view.', exitscript=True)


# Result Filled Region types are selected directly in the settings window.
region_types = list(DB.FilteredElementCollector(doc).OfClass(DB.FilledRegionType))
if not region_types:
    forms.alert('No Filled Region type exists in this project.', exitscript=True)

region_type_rows = []
for region_type in region_types:
    try:
        region_name = region_type.Name
    except Exception:
        region_name = DB.Element.Name.GetValue(region_type)
    region_type_rows.append((
        '{0}  [Id {1}]'.format(region_name, eid_int(region_type.Id)),
        region_type
    ))
region_type_rows.sort(key=lambda item: item[0].lower())


config = script.get_config()


def config_get(name, default_value):
    try:
        return getattr(config, name)
    except Exception:
        return default_value


def config_bool(name, default_value):
    value = config_get(name, default_value)
    if isinstance(value, bool):
        return value
    try:
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
    except Exception:
        return bool(default_value)


try:
    text_type = unicode
except NameError:
    text_type = str


def to_text(value):
    if value is None:
        return u''
    try:
        return text_type(value)
    except Exception:
        try:
            return text_type(str(value), 'utf-8', 'ignore')
        except Exception:
            return u''


def fold_text(value):
    value = to_text(value).strip().lower()
    try:
        normalized = unicodedata.normalize('NFD', value)
        return u''.join(
            character for character in normalized
            if unicodedata.category(character) != 'Mn'
        )
    except Exception:
        return value


def split_keywords(raw_value):
    raw_value = to_text(raw_value)
    for separator in (u'\r\n', u'\n', u'\r', u',', u'|'):
        raw_value = raw_value.replace(separator, u';')
    keywords = []
    seen = set()
    for item in raw_value.split(u';'):
        folded = fold_text(item)
        if folded and folded not in seen:
            seen.add(folded)
            keywords.append(folded)
    return keywords


def parse_float_text(value, field_name, allow_zero=False):
    try:
        number = float(to_text(value).strip().replace(',', '.'))
    except Exception:
        raise ValueError('{0}: invalid number.'.format(field_name))
    if allow_zero:
        if number < 0.0:
            raise ValueError('{0}: value cannot be negative.'.format(field_name))
    elif number <= 0.0:
        raise ValueError('{0}: value must be greater than zero.'.format(field_name))
    return number


class CoverageSettingsWindow(forms.WPFWindow):
    def __init__(self, xaml_file, rows):
        forms.WPFWindow.__init__(self, xaml_file)
        self.accepted = False
        self.values = None
        self.region_rows = rows

        self.max_distance_tb.Text = to_text(config_get('max_distance_m', DEFAULT_MAX_DISTANCE_M))
        self.grid_size_tb.Text = to_text(config_get('grid_mm', DEFAULT_GRID_MM))
        self.clearance_tb.Text = to_text(config_get('clearance_mm', DEFAULT_CLEARANCE_MM))
        self.auto_scan_cb.IsChecked = config_bool('auto_scan', DEFAULT_AUTO_SCAN)
        self.keyword_tb.Text = to_text(config_get('cabinet_keywords', DEFAULT_CABINET_KEYWORDS))

        saved_region_type_id = -1
        try:
            saved_region_type_id = int(config_get('region_type_id', -1))
        except Exception:
            saved_region_type_id = -1

        selected_index = 0
        for index, row in enumerate(rows):
            self.region_type_cb.Items.Add(row[0])
            if eid_int(row[1].Id) == saved_region_type_id:
                selected_index = index
        self.region_type_cb.SelectedIndex = selected_index

        self.auto_scan_cb.Checked += self.auto_scan_changed
        self.auto_scan_cb.Unchecked += self.auto_scan_changed
        self.run_btn.Click += self.run_click
        self.cancel_btn.Click += self.cancel_click
        self.update_keyword_state()

    def update_keyword_state(self):
        enabled = (self.auto_scan_cb.IsChecked == True)
        self.keyword_tb.IsEnabled = enabled
        self.keyword_hint.IsEnabled = enabled
        try:
            if enabled:
                self.keyword_panel.Background = self.brush_from_hex('#FFF4C2')
                self.keyword_panel.BorderBrush = self.brush_from_hex('#E0A800')
                self.keyword_status.Text = 'Auto scan FHR: Automatic find FHR inside compartment.'
                self.keyword_status.Foreground = self.brush_from_hex('#8A5A00')
            else:
                self.keyword_panel.Background = self.brush_from_hex('#F1F3F5')
                self.keyword_panel.BorderBrush = self.brush_from_hex('#C8CDD2')
                self.keyword_status.Text = 'Manual Pick FHR: click Run, then select cabinet Family instances.'
                self.keyword_status.Foreground = self.brush_from_hex('#687078')
        except Exception:
            pass

    def brush_from_hex(self, hex_value):
        try:
            from System.Windows.Media import BrushConverter
            return BrushConverter().ConvertFromString(hex_value)
        except Exception:
            return None

    def auto_scan_changed(self, sender, args):
        self.update_keyword_state()


    def show_error(self, message):
        self.error_text.Text = to_text(message)
        from System.Windows import Visibility
        self.error_text.Visibility = Visibility.Visible

    def run_click(self, sender, args):
        try:
            max_distance_m_value = parse_float_text(
                self.max_distance_tb.Text,
                'Maximum distance',
                False
            )
            grid_mm_value = parse_float_text(
                self.grid_size_tb.Text,
                'Grid size',
                False
            )
            clearance_mm_value = parse_float_text(
                self.clearance_tb.Text,
                'Obstacle clearance',
                True
            )
            auto_scan_value = (self.auto_scan_cb.IsChecked == True)
            raw_keywords = to_text(self.keyword_tb.Text).strip()
            parsed_keywords = split_keywords(raw_keywords)
            if auto_scan_value and not parsed_keywords:
                raise ValueError('Enter at least one cabinet keyword for Auto Scan.')

            selected_index = self.region_type_cb.SelectedIndex
            if selected_index < 0 or selected_index >= len(self.region_rows):
                raise ValueError('Select a Filled Region type for the result.')

            self.values = {
                'max_distance_m': max_distance_m_value,
                'grid_mm': grid_mm_value,
                'clearance_mm': clearance_mm_value,
                'auto_scan': auto_scan_value,
                'raw_keywords': raw_keywords,
                'keywords': parsed_keywords,
                'region_type': self.region_rows[selected_index][1],
            }
            self.accepted = True
            self.Close()
        except Exception as error:
            self.show_error(error)

    def cancel_click(self, sender, args):
        self.accepted = False
        self.Close()


xaml_file = script.get_bundle_file('ui.xaml')
settings_window = CoverageSettingsWindow(xaml_file, region_type_rows)
settings_window.show_dialog()

if not settings_window.accepted or settings_window.values is None:
    script.exit()

ui_values = settings_window.values
max_distance_m = ui_values['max_distance_m']
grid_mm = ui_values['grid_mm']
clearance_mm = ui_values['clearance_mm']
auto_scan_enabled = ui_values['auto_scan']
cabinet_keywords_raw = ui_values['raw_keywords']
cabinet_keywords = ui_values['keywords']
selected_region_type = ui_values['region_type']

# Save settings immediately after the user confirms the window.
try:
    config.max_distance_m = max_distance_m
    config.grid_mm = grid_mm
    config.clearance_mm = clearance_mm
    config.auto_scan = auto_scan_enabled
    config.cabinet_keywords = cabinet_keywords_raw
    config.region_type_id = eid_int(selected_region_type.Id)
    script.save_config()
except Exception:
    pass

max_distance = m_to_ft(max_distance_m)
cell = mm_to_ft(grid_mm)
clearance = mm_to_ft(clearance_mm)
half_cell = cell * 0.5
half_diag = cell * math.sqrt(2.0) * 0.5

# Cabinet sources are always host-model elements. Auto Scan intentionally
# excludes all FamilyInstances inside Revit links. Manual selection is host only.
cabinet_records = []
selected_host_cabinet_ids = set()
manual_cabinet_refs = []

if not auto_scan_enabled:
    try:
        manual_cabinet_refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            CabinetSelectionFilter(),
            'Select fire cabinet Family instances, then click Finish.'
        )
    except OperationCanceledException:
        script.exit()

    if not manual_cabinet_refs or len(manual_cabinet_refs) == 0:
        forms.alert('No fire cabinet was selected.', exitscript=True)

    seen_manual_cabinet_ids = set()
    for reference in manual_cabinet_refs:
        cabinet = doc.GetElement(reference.ElementId)
        if not isinstance(cabinet, DB.FamilyInstance):
            continue
        cabinet_id = eid_int(cabinet.Id)
        if cabinet_id in seen_manual_cabinet_ids:
            continue
        seen_manual_cabinet_ids.add(cabinet_id)
        cabinet_records.append({
            'element': cabinet,
            'source_view': view,
            'external_transform': None,
            'is_host': True,
            'link_instance_id': None,
            'source_label': 'Host',
            'geometry_center': None,
            'center_method': None,
            'matched_keywords': [],
            'display_name': u'',
        })
        selected_host_cabinet_ids.add(cabinet_id)


# =============================================================================
# VIEW PLANE AND COMPARTMENT BOUNDARY
# =============================================================================
right = view.RightDirection.Normalize()
up = view.UpDirection.Normalize()
view_direction = view.ViewDirection.Normalize()

boundaries = compartment.GetBoundaries()
if boundaries is None or boundaries.Count == 0:
    forms.alert('The selected Filled Region has no valid boundary.', exitscript=True)

base_point = None
for boundary_loop in boundaries:
    for boundary_curve in boundary_loop:
        base_point = boundary_curve.GetEndPoint(0)
        break
    if base_point is not None:
        break

if base_point is None:
    forms.alert('Cannot read the compartment boundary.', exitscript=True)


def xyz_to_uv(point):
    vector = point - base_point
    return (vector.DotProduct(right), vector.DotProduct(up))


def uv_to_xyz(u, v):
    return base_point + right.Multiply(u) + up.Multiply(v)


def uvz_to_xyz(u, v, z):
    point = uv_to_xyz(u, v)
    return DB.XYZ(point.X, point.Y, z)


compartment_polygons = []
for boundary_loop in boundaries:
    polygon = []
    for boundary_curve in boundary_loop:
        tess_points = list(boundary_curve.Tessellate())
        for point in tess_points:
            uv = xyz_to_uv(point)
            if not polygon:
                polygon.append(uv)
            else:
                last = polygon[-1]
                if abs(last[0] - uv[0]) > 1.0e-9 or abs(last[1] - uv[1]) > 1.0e-9:
                    polygon.append(uv)

    if len(polygon) > 1:
        first = polygon[0]
        last = polygon[-1]
        if abs(first[0] - last[0]) < 1.0e-9 and abs(first[1] - last[1]) < 1.0e-9:
            polygon.pop()

    if len(polygon) >= 3:
        compartment_polygons.append(polygon)

if not compartment_polygons:
    forms.alert('Cannot tessellate the compartment boundary.', exitscript=True)

all_u = []
all_v = []
for polygon in compartment_polygons:
    for u_value, v_value in polygon:
        all_u.append(u_value)
        all_v.append(v_value)

min_u = min(all_u)
max_u = max(all_u)
min_v = min(all_v)
max_v = max(all_v)

nu = int(math.ceil((max_u - min_u) / cell))
nv = int(math.ceil((max_v - min_v) / cell))

if nu <= 0 or nv <= 0:
    forms.alert('The compartment is too small for the selected grid size.', exitscript=True)

cell_count = nu * nv
if cell_count > MAX_GRID_CELLS:
    suggested_grid = grid_mm * math.sqrt(cell_count / float(MAX_GRID_CELLS))
    forms.alert(
        'The grid contains {0:,} cells. Increase grid size to about {1:.0f} mm or more.'.format(
            cell_count,
            suggested_grid
        ),
        exitscript=True
    )

try:
    level_elevation = view.GenLevel.Elevation
except Exception:
    level_elevation = base_point.Z

analysis_z_min = level_elevation + mm_to_ft(ANALYSIS_BOTTOM_MM)
analysis_z_max = level_elevation + mm_to_ft(ANALYSIS_TOP_MM)
if analysis_z_max < analysis_z_min:
    analysis_z_min, analysis_z_max = analysis_z_max, analysis_z_min


# =============================================================================
# BUILD INSIDE GRID
# =============================================================================
inside = bytearray(cell_count)
inside_indices = []

with forms.ProgressBar(
    title='Building compartment grid: {value} of {max_value}',
    cancellable=True
) as progress:
    for j in range(nv):
        center_v = min_v + (j + 0.5) * cell
        row_start = j * nu
        for i in range(nu):
            center_u = min_u + (i + 0.5) * cell
            if region_contains(compartment_polygons, center_u, center_v):
                index = row_start + i
                inside[index] = 1
                inside_indices.append(index)

        progress.update_progress(j + 1, nv)
        if progress.cancelled:
            script.exit()

if not inside_indices:
    forms.alert('No grid cell centre was found inside the compartment.', exitscript=True)


# =============================================================================
# GEOMETRY AND COLLECTOR HELPERS
# =============================================================================
def transform_point(point, external_transform):
    if external_transform is None:
        return point
    return external_transform.OfPoint(point)


def transform_vector(vector, external_transform):
    if external_transform is None:
        return vector
    return external_transform.OfVector(vector)


def transformed_bbox_corners(bbox, external_transform):
    if bbox is None:
        return []

    try:
        local_transform = bbox.Transform
    except Exception:
        local_transform = DB.Transform.Identity

    corners = []
    for x_value in (bbox.Min.X, bbox.Max.X):
        for y_value in (bbox.Min.Y, bbox.Max.Y):
            for z_value in (bbox.Min.Z, bbox.Max.Z):
                point = DB.XYZ(x_value, y_value, z_value)
                point = local_transform.OfPoint(point)
                if external_transform is not None:
                    point = external_transform.OfPoint(point)
                corners.append(point)
    return corners


def points_to_uvz_bbox(points):
    if not points:
        return None

    us = []
    vs = []
    zs = []
    for point in points:
        u_value, v_value = xyz_to_uv(point)
        us.append(u_value)
        vs.append(v_value)
        zs.append(point.Z)

    return (min(us), max(us), min(vs), max(vs), min(zs), max(zs))


def element_bbox_data(element, source_view, external_transform):
    bbox = None
    if source_view is not None:
        try:
            bbox = element.get_BoundingBox(source_view)
        except Exception:
            bbox = None
    if bbox is None:
        try:
            bbox = element.get_BoundingBox(None)
        except Exception:
            bbox = None
    return points_to_uvz_bbox(transformed_bbox_corners(bbox, external_transform))


def solid_bbox_data(solid):
    try:
        bbox = solid.GetBoundingBox()
    except Exception:
        return None
    return points_to_uvz_bbox(transformed_bbox_corners(bbox, None))


def bbox_overlaps_analysis_band(data):
    if data is None:
        return False
    return not (data[5] < analysis_z_min or data[4] > analysis_z_max)


def bbox_overlaps_compartment(data, margin=0.0):
    if data is None:
        return False
    return not (
        data[1] < min_u - margin or data[0] > max_u + margin or
        data[3] < min_v - margin or data[2] > max_v + margin
    )


def element_local_point(element):
    try:
        location = element.Location
    except Exception:
        location = None

    if isinstance(location, DB.LocationPoint):
        return location.Point
    if isinstance(location, DB.LocationCurve):
        return location.Curve.Evaluate(0.5, True)

    try:
        bbox = element.get_BoundingBox(None)
    except Exception:
        bbox = None
    if bbox is None:
        return None

    local_center = DB.XYZ(
        (bbox.Min.X + bbox.Max.X) * 0.5,
        (bbox.Min.Y + bbox.Max.Y) * 0.5,
        (bbox.Min.Z + bbox.Max.Z) * 0.5
    )
    return bbox.Transform.OfPoint(local_center)


def get_element_center(element, source_view, external_transform):
    try:
        location = element.Location
    except Exception:
        location = None

    if isinstance(location, DB.LocationPoint):
        return transform_point(location.Point, external_transform)
    if isinstance(location, DB.LocationCurve):
        return transform_point(location.Curve.Evaluate(0.5, True), external_transform)

    bbox = None
    if source_view is not None:
        try:
            bbox = element.get_BoundingBox(source_view)
        except Exception:
            bbox = None
    if bbox is None:
        try:
            bbox = element.get_BoundingBox(None)
        except Exception:
            bbox = None
    if bbox is None:
        return None

    local_center = DB.XYZ(
        (bbox.Min.X + bbox.Max.X) * 0.5,
        (bbox.Min.Y + bbox.Max.Y) * 0.5,
        (bbox.Min.Z + bbox.Max.Z) * 0.5
    )
    local_center = bbox.Transform.OfPoint(local_center)
    return transform_point(local_center, external_transform)


def points_xyz_center(points):
    if not points:
        return None
    return DB.XYZ(
        (min([point.X for point in points]) + max([point.X for point in points])) * 0.5,
        (min([point.Y for point in points]) + max([point.Y for point in points])) * 0.5,
        (min([point.Z for point in points]) + max([point.Z for point in points])) * 0.5
    )


def get_element_bbox_center(element, source_view, external_transform):
    bbox = None
    if source_view is not None:
        try:
            bbox = element.get_BoundingBox(source_view)
        except Exception:
            bbox = None
    if bbox is None:
        try:
            bbox = element.get_BoundingBox(None)
        except Exception:
            bbox = None
    return points_xyz_center(transformed_bbox_corners(bbox, external_transform))


def get_cabinet_geometry_center(element, source_view, external_transform):
    # Primary source point: centre of the combined extents of valid physical
    # Solids. This follows the actual family geometry instead of LocationPoint.
    solid_points = []
    try:
        solids = get_solid_geometry(element, external_transform, source_view)
    except Exception:
        solids = []

    for solid in solids:
        try:
            solid_points.extend(
                transformed_bbox_corners(solid.GetBoundingBox(), None)
            )
        except Exception:
            continue

    solid_center = points_xyz_center(solid_points)
    if solid_center is not None:
        return solid_center, 'solid_geometry'

    # Families made from meshes, imports, curves or hidden geometry may not
    # return a valid Solid. Use the complete element BoundingBox in that case.
    bbox_center = get_element_bbox_center(
        element,
        source_view,
        external_transform
    )
    if bbox_center is not None:
        return bbox_center, 'element_bbox'

    # Last-resort compatibility fallback only.
    legacy_center = get_element_center(
        element,
        source_view,
        external_transform
    )
    if legacy_center is not None:
        return legacy_center, 'location_fallback'

    return None, 'none'


def get_length_parameter_value(element, built_in_parameter=None, names=None):
    parameter = None
    if built_in_parameter is not None:
        try:
            parameter = element.get_Parameter(built_in_parameter)
        except Exception:
            parameter = None
        if parameter is not None and parameter.HasValue:
            try:
                value = parameter.AsDouble()
                if value > 1.0e-9:
                    return value
            except Exception:
                pass

    if names:
        lowered_names = set([str(name).strip().lower() for name in names])
        try:
            for parameter in element.Parameters:
                try:
                    definition = parameter.Definition
                    name = definition.Name.strip().lower() if definition is not None else ''
                    if name not in lowered_names or not parameter.HasValue:
                        continue
                    value = parameter.AsDouble()
                    if value > 1.0e-9:
                        return value
                except Exception:
                    continue
        except Exception:
            pass

    try:
        symbol = element.Symbol
    except Exception:
        symbol = None

    if symbol is not None:
        if built_in_parameter is not None:
            try:
                parameter = symbol.get_Parameter(built_in_parameter)
            except Exception:
                parameter = None
            if parameter is not None and parameter.HasValue:
                try:
                    value = parameter.AsDouble()
                    if value > 1.0e-9:
                        return value
                except Exception:
                    pass

        if names:
            try:
                for parameter in symbol.Parameters:
                    try:
                        definition = parameter.Definition
                        name = definition.Name.strip().lower() if definition is not None else ''
                        if name not in lowered_names or not parameter.HasValue:
                            continue
                        value = parameter.AsDouble()
                        if value > 1.0e-9:
                            return value
                    except Exception:
                        continue
            except Exception:
                pass

    return None


def get_bbox_projected_width(element, source_view, external_transform, tangent_uv):
    # Use model BoundingBox first so 2D swing-symbol geometry does not enlarge
    # the apparent opening. View BoundingBox is only a fallback.
    bbox = None
    try:
        bbox = element.get_BoundingBox(None)
    except Exception:
        bbox = None
    if bbox is None and source_view is not None:
        try:
            bbox = element.get_BoundingBox(source_view)
        except Exception:
            bbox = None
    corners = transformed_bbox_corners(bbox, external_transform)
    if not corners:
        return None

    values = []
    for corner in corners:
        u_value, v_value = xyz_to_uv(corner)
        values.append(u_value * tangent_uv[0] + v_value * tangent_uv[1])
    if not values:
        return None
    width = max(values) - min(values)
    if width <= 1.0e-9:
        return None
    return width


def get_door_width(door, source_view, external_transform, tangent_uv):
    width = get_length_parameter_value(
        door,
        DB.BuiltInParameter.DOOR_WIDTH,
        ['width', 'door width', 'opening width', 'rough width']
    )

    min_width = mm_to_ft(DOOR_MIN_VALID_WIDTH_MM)
    max_width = mm_to_ft(DOOR_MAX_VALID_WIDTH_MM)
    if width is not None and width >= min_width and width <= max_width:
        return width, 'parameter'

    bbox_width = get_bbox_projected_width(
        door,
        source_view,
        external_transform,
        tangent_uv
    )
    if bbox_width is not None and bbox_width >= min_width and bbox_width <= max_width:
        return bbox_width, 'bbox'

    return mm_to_ft(DOOR_DEFAULT_WIDTH_MM), 'default'


def get_wall_projection_data(wall, local_point, external_transform):
    try:
        location = wall.Location
        if not isinstance(location, DB.LocationCurve):
            return None
        curve = location.Curve
        projection = curve.Project(local_point)
        if projection is not None:
            projected_local_point = projection.XYZPoint
            derivatives = curve.ComputeDerivatives(projection.Parameter, False)
            tangent = derivatives.BasisX.Normalize()
        else:
            projected_local_point = curve.Evaluate(0.5, True)
            tangent = (curve.GetEndPoint(1) - curve.GetEndPoint(0)).Normalize()

        projected_host_point = transform_point(projected_local_point, external_transform)
        tangent = transform_vector(tangent, external_transform)
        tangent_uv = normalize_2d(
            tangent.DotProduct(right),
            tangent.DotProduct(up)
        )
        if tangent_uv is None:
            return None
        center_u, center_v = xyz_to_uv(projected_host_point)
        return center_u, center_v, tangent_uv
    except Exception:
        return None


def get_wall_tangent_at_point(wall, local_point, external_transform):
    data = get_wall_projection_data(wall, local_point, external_transform)
    if data is None:
        return None
    return data[2]


def nearest_wall_for_insert(insert, wall_elements):
    local_point = element_local_point(insert)
    if local_point is None:
        return None

    max_distance = mm_to_ft(DOOR_NEAREST_WALL_SEARCH_MM)
    best_wall = None
    best_distance = float('inf')
    for wall in wall_elements:
        try:
            location = wall.Location
            if not isinstance(location, DB.LocationCurve):
                continue
            projection = location.Curve.Project(local_point)
            if projection is None:
                continue
            distance = local_point.DistanceTo(projection.XYZPoint)
            if distance <= max_distance and distance < best_distance:
                best_distance = distance
                best_wall = wall
        except Exception:
            continue
    return best_wall


def create_door_portal_record(insert, host_wall, source_view, external_transform, source_key):
    local_center = element_local_point(insert)
    if local_center is None:
        return None

    projection_data = get_wall_projection_data(
        host_wall,
        local_center,
        external_transform
    )
    if projection_data is None:
        return None

    center_u, center_v, tangent_uv = projection_data
    width, width_source = get_door_width(
        insert,
        source_view,
        external_transform,
        tangent_uv
    )

    try:
        wall_width = host_wall.Width
    except Exception:
        wall_width = mm_to_ft(200.0)

    # Physical clear width is narrowed by requested person/route clearance.
    # Half a grid cell is then added only for raster quantisation, so a normal
    # 800-900 mm door is not lost when its centre falls between grid columns.
    physical_usable_half_width = max(0.0, width * 0.5 - clearance)
    portal_half_width = max(
        half_cell * 0.55,
        physical_usable_half_width + half_cell + mm_to_ft(DOOR_PORTAL_WIDTH_EXTRA_MM)
    )

    # The portal must cross the entire inflated wall band and reach at least
    # one row of cells on both sides of the wall.
    wall_block_radius = wall_width * 0.5 + clearance + half_diag
    portal_half_depth = (
        wall_block_radius +
        cell * DOOR_PORTAL_DEPTH_EXTRA_CELLS
    )

    normal_u = -tangent_uv[1]
    normal_v = tangent_uv[0]
    return {
        'center_u': center_u,
        'center_v': center_v,
        'tangent_u': tangent_uv[0],
        'tangent_v': tangent_uv[1],
        'normal_u': normal_u,
        'normal_v': normal_v,
        'half_width': portal_half_width,
        'half_depth': portal_half_depth,
        'physical_width': width,
        'width_source': width_source,
        'insert_id': eid_int(insert.Id),
        'wall_id': eid_int(host_wall.Id),
        'source_key': source_key,
    }


def get_solid_geometry(element, external_transform, source_view):
    options = DB.Options()
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = False
    if source_view is not None:
        try:
            options.View = source_view
        except Exception:
            pass
    else:
        try:
            options.DetailLevel = DB.ViewDetailLevel.Fine
        except Exception:
            pass

    try:
        geometry = element.get_Geometry(options)
    except Exception:
        geometry = None

    solids = []

    def parse_geometry(geometry_element):
        if geometry_element is None:
            return
        for geometry_object in geometry_element:
            if isinstance(geometry_object, DB.Solid):
                try:
                    if geometry_object.Faces.Size == 0:
                        continue
                    if geometry_object.Volume <= MIN_SOLID_VOLUME_FT3:
                        continue
                    solid = geometry_object
                    if external_transform is not None:
                        solid = DB.SolidUtils.CreateTransformed(solid, external_transform)
                    solids.append(solid)
                except Exception:
                    continue
            elif isinstance(geometry_object, DB.GeometryInstance):
                try:
                    parse_geometry(geometry_object.GetInstanceGeometry())
                except Exception:
                    continue

    parse_geometry(geometry)
    return solids


def get_host_visible_elements(bic):
    if bic is None:
        return []
    try:
        return list(
            DB.FilteredElementCollector(doc, view.Id)
            .OfCategory(bic)
            .WhereElementIsNotElementType()
        )
    except Exception:
        return []


def get_link_elements(link_instance, bic, use_visible_collector):
    if bic is None:
        return []

    if use_visible_collector:
        try:
            return list(
                DB.FilteredElementCollector(doc, view.Id, link_instance.Id)
                .OfCategory(bic)
                .WhereElementIsNotElementType()
            )
        except Exception:
            pass

    try:
        link_doc = link_instance.GetLinkDocument()
        if link_doc is None:
            return []
        return list(
            DB.FilteredElementCollector(link_doc)
            .OfCategory(bic)
            .WhereElementIsNotElementType()
        )
    except Exception:
        return []



# =============================================================================
# AUTO-SCAN FIRE CABINETS BY KEYWORDS
# =============================================================================
def safe_element_name(element):
    try:
        return to_text(element.Name)
    except Exception:
        try:
            return to_text(DB.Element.Name.GetValue(element))
        except Exception:
            return u''


def parameter_text(element, built_in_name):
    try:
        built_in_parameter = getattr(DB.BuiltInParameter, built_in_name)
        parameter = element.get_Parameter(built_in_parameter)
        if parameter is None or not parameter.HasValue:
            return u''
        value = parameter.AsString()
        if value:
            return to_text(value)
        value = parameter.AsValueString()
        if value:
            return to_text(value)
    except Exception:
        pass
    return u''


def cabinet_search_text(element):
    values = []
    values.append(safe_element_name(element))

    try:
        if element.Category is not None:
            values.append(to_text(element.Category.Name))
    except Exception:
        pass

    try:
        symbol = element.Symbol
    except Exception:
        symbol = None

    if symbol is not None:
        values.append(safe_element_name(symbol))
        try:
            if symbol.Family is not None:
                values.append(to_text(symbol.Family.Name))
        except Exception:
            pass
        for parameter_name in (
            'ALL_MODEL_TYPE_MARK',
            'ALL_MODEL_DESCRIPTION',
            'ALL_MODEL_TYPE_COMMENTS'
        ):
            values.append(parameter_text(symbol, parameter_name))

    for parameter_name in (
        'ALL_MODEL_MARK',
        'ALL_MODEL_INSTANCE_COMMENTS',
        'ALL_MODEL_DESCRIPTION'
    ):
        values.append(parameter_text(element, parameter_name))

    return fold_text(u' | '.join([value for value in values if value]))


def cabinet_matching_keywords(element, keywords):
    search_text = cabinet_search_text(element)
    if not search_text:
        return []
    return [keyword for keyword in keywords if keyword in search_text]


def element_matches_cabinet_keywords(element, keywords):
    return bool(cabinet_matching_keywords(element, keywords))


def cabinet_display_name(element):
    family_name = u''
    type_name = safe_element_name(element)
    try:
        symbol = element.Symbol
        if symbol is not None:
            type_name = safe_element_name(symbol) or type_name
            if symbol.Family is not None:
                family_name = to_text(symbol.Family.Name)
    except Exception:
        pass
    if family_name and type_name:
        return u'{0} : {1}'.format(family_name, type_name)
    return family_name or type_name or u'Unnamed cabinet'



def cabinet_center_if_inside_compartment(element, source_view, external_transform):
    center, center_method = get_cabinet_geometry_center(
        element,
        source_view,
        external_transform
    )
    if center is None:
        return None, center_method

    center_u, center_v = xyz_to_uv(center)
    if not region_contains(compartment_polygons, center_u, center_v):
        return None, center_method

    element_box = element_bbox_data(element, source_view, external_transform)
    if element_box is not None and not bbox_overlaps_analysis_band(element_box):
        return None, center_method
    return center, center_method


def collect_auto_cabinet_records(keywords):
    records = []
    seen_host = set()
    diagnostics = {
        'host_candidates': 0,
        'keyword_matches': 0,
        'inside_matches': 0,
        'solid_geometry_centres': 0,
        'element_bbox_centres': 0,
        'location_fallback_centres': 0,
    }

    try:
        host_instances = list(
            DB.FilteredElementCollector(doc, view.Id)
            .OfClass(DB.FamilyInstance)
            .WhereElementIsNotElementType()
        )
    except Exception:
        host_instances = []

    diagnostics['host_candidates'] = len(host_instances)
    for element in host_instances:
        element_id = eid_int(element.Id)
        if element_id in seen_host:
            continue
        matched_keywords = cabinet_matching_keywords(element, keywords)
        if not matched_keywords:
            continue
        diagnostics['keyword_matches'] += 1
        geometry_center, center_method = cabinet_center_if_inside_compartment(
            element,
            view,
            None
        )
        if geometry_center is None:
            continue

        seen_host.add(element_id)
        diagnostics['inside_matches'] += 1
        if center_method == 'solid_geometry':
            diagnostics['solid_geometry_centres'] += 1
        elif center_method == 'element_bbox':
            diagnostics['element_bbox_centres'] += 1
        elif center_method == 'location_fallback':
            diagnostics['location_fallback_centres'] += 1
        records.append({
            'element': element,
            'source_view': view,
            'external_transform': None,
            'is_host': True,
            'link_instance_id': None,
            'source_label': 'Host',
            'display_name': cabinet_display_name(element),
            'geometry_center': geometry_center,
            'center_method': center_method,
            'matched_keywords': matched_keywords,
        })
        selected_host_cabinet_ids.add(element_id)

    # Deliberately stop after the host-view collector. Revit-link FamilyInstances
    # are never candidates for automatic cabinet detection.

    return records, diagnostics


auto_scan_diagnostics = None
if auto_scan_enabled:
    cabinet_records, auto_scan_diagnostics = collect_auto_cabinet_records(
        cabinet_keywords
    )
    if not cabinet_records:
        forms.alert(
            'Auto Scan did not find any cabinet inside the selected compartment.\n\n'
            'Check the keywords, active-view visibility and cabinet elevation.',
            exitscript=True
        )

# Cache source centres before obstacle collection. Only cabinets that pass the
# same inside-compartment and analysis-height rules are excluded from equipment
# obstacles. Invalid manual picks remain obstacles and are reported later.
selected_host_cabinet_ids = set()
for cabinet_record in cabinet_records:
    cabinet = cabinet_record.get('element')
    if cabinet is None:
        continue
    geometry_center, center_method = cabinet_center_if_inside_compartment(
        cabinet,
        cabinet_record.get('source_view'),
        cabinet_record.get('external_transform')
    )
    if geometry_center is None:
        continue
    cabinet_record['geometry_center'] = geometry_center
    cabinet_record['center_method'] = center_method
    if cabinet_record.get('is_host'):
        selected_host_cabinet_ids.add(eid_int(cabinet.Id))

# =============================================================================
# COLLECT WALLS, DOOR OPENINGS, COLUMNS AND EQUIPMENT
# Broad phase: element BoundingBox.
# Narrow phase: solid BoundingBox -> fast rays -> exact Boolean when ambiguous.
# =============================================================================
wall_segments = []
door_portals = []
solid_element_obstacles = []
fallback_boxes = []

obstacle_stats = {
    'walls': 0,
    'wall_segments': 0,
    'doors': 0,
    'door_parameter_widths': 0,
    'door_bbox_widths': 0,
    'door_default_widths': 0,
    'door_wall_inserts': 0,
    'door_host_fallbacks': 0,
    'door_portal_cells_carved': 0,
    'door_portal_tunnel_cells': 0,
    'door_portals_without_inside_cells': 0,
    'columns': 0,
    'equipment': 0,
    'solid_elements': 0,
    'solids': 0,
    'fallback_boxes': 0,
    'links': 0,
    'link_visibility_fallbacks': 0,
    'candidate_cells': 0,
    'element_bbox_rejects': 0,
    'solid_bbox_rejects': 0,
    'fast_ray_tests': 0,
    'fast_ray_hits': 0,
    'boolean_tests': 0,
    'boolean_hits': 0,
    'boolean_failures': 0,
}


def union_bbox_data(boxes):
    valid = [box for box in boxes if box is not None]
    if not valid:
        return None
    return (
        min([box[0] for box in valid]),
        max([box[1] for box in valid]),
        min([box[2] for box in valid]),
        max([box[3] for box in valid]),
        min([box[4] for box in valid]),
        max([box[5] for box in valid]),
    )


def collect_source_obstacles(source_doc, external_transform, is_host, link_instance, use_visible_link_collector):
    if is_host:
        source_view = view

        def collect(bic):
            return get_host_visible_elements(bic)
    else:
        source_view = None

        def collect(bic):
            return get_link_elements(link_instance, bic, use_visible_link_collector)

    # -------------------------------------------------------------------------
    # Walls are blocked first. Door and wall-opening portals are collected and
    # carved from the finished wall raster in a separate pass. This avoids the
    # common coarse-grid failure where no cell centre falls inside a door while
    # the expanded wall band still blocks every cell through the opening.
    # -------------------------------------------------------------------------
    wall_elements = collect(category_value('OST_Walls'))
    explicit_door_elements = collect(category_value('OST_Doors'))

    explicit_doors_by_wall = {}
    hostless_doors = []
    for door in explicit_door_elements:
        try:
            host_wall = door.Host
        except Exception:
            host_wall = None
        if isinstance(host_wall, DB.Wall):
            explicit_doors_by_wall.setdefault(eid_int(host_wall.Id), []).append(door)
        else:
            hostless_doors.append(door)

    # Some curtain-wall or unusual hosted door families do not return a Wall
    # through FamilyInstance.Host. Associate only those exceptional cases with
    # the nearest visible wall, using a strict search radius.
    for door in hostless_doors:
        nearest_wall = nearest_wall_for_insert(door, wall_elements)
        if nearest_wall is not None:
            explicit_doors_by_wall.setdefault(eid_int(nearest_wall.Id), []).append(door)
            obstacle_stats['door_host_fallbacks'] += 1

    source_key = 'HOST' if is_host else 'LINK_{0}'.format(eid_int(link_instance.Id))
    seen_portal_keys = set()

    for wall in wall_elements:
        try:
            data = element_bbox_data(wall, source_view, external_transform)
            if not bbox_overlaps_analysis_band(data):
                continue
            if not bbox_overlaps_compartment(data, half_diag + clearance):
                continue

            location = wall.Location
            if not isinstance(location, DB.LocationCurve):
                continue

            tess_points = list(location.Curve.Tessellate())
            if len(tess_points) < 2:
                continue

            try:
                wall_width = wall.Width
            except Exception:
                wall_width = mm_to_ft(200.0)

            radius = wall_width * 0.5 + clearance + half_diag
            radius_sq = radius * radius
            previous_uv = xyz_to_uv(transform_point(tess_points[0], external_transform))
            added = False

            for point in tess_points[1:]:
                current_uv = xyz_to_uv(transform_point(point, external_transform))
                ax, ay = previous_uv
                bx, by = current_uv
                if abs(ax - bx) + abs(ay - by) > 1.0e-9:
                    seg_min_u = min(ax, bx) - radius
                    seg_max_u = max(ax, bx) + radius
                    seg_min_v = min(ay, by) - radius
                    seg_max_v = max(ay, by) + radius
                    if not (
                        seg_max_u < min_u or seg_min_u > max_u or
                        seg_max_v < min_v or seg_min_v > max_v
                    ):
                        wall_segments.append((
                            ax, ay, bx, by,
                            radius_sq,
                            seg_min_u, seg_max_u,
                            seg_min_v, seg_max_v
                        ))
                        obstacle_stats['wall_segments'] += 1
                        added = True
                previous_uv = current_uv

            if added:
                obstacle_stats['walls'] += 1

            insert_candidates = list(
                explicit_doors_by_wall.get(eid_int(wall.Id), [])
            )

            if INCLUDE_WALL_OPENING_ELEMENTS:
                try:
                    insert_ids = wall.FindInserts(True, False, True, True)
                except Exception:
                    insert_ids = []

                for insert_id in insert_ids:
                    try:
                        insert = source_doc.GetElement(insert_id)
                    except Exception:
                        insert = None
                    if insert is None:
                        continue

                    is_door = False
                    try:
                        is_door = (
                            insert.Category is not None and
                            insert.Category.Id == DB.ElementId(category_value('OST_Doors'))
                        )
                    except Exception:
                        is_door = False

                    is_opening = isinstance(insert, DB.Opening)
                    if not is_door and not is_opening:
                        continue
                    insert_candidates.append(insert)
                    obstacle_stats['door_wall_inserts'] += 1

            for insert in insert_candidates:
                portal_key = (
                    source_key,
                    eid_int(wall.Id),
                    eid_int(insert.Id)
                )
                if portal_key in seen_portal_keys:
                    continue
                seen_portal_keys.add(portal_key)

                portal = create_door_portal_record(
                    insert,
                    wall,
                    source_view,
                    external_transform,
                    source_key
                )
                if portal is None:
                    continue
                door_portals.append(portal)
                obstacle_stats['doors'] += 1
                width_source = portal['width_source']
                if width_source == 'parameter':
                    obstacle_stats['door_parameter_widths'] += 1
                elif width_source == 'bbox':
                    obstacle_stats['door_bbox_widths'] += 1
                else:
                    obstacle_stats['door_default_widths'] += 1
        except Exception:
            continue

    # Add more categories here when required. The broad/narrow phase pipeline
    # will work without other changes.
    category_specs = [
        ('columns', category_value('OST_Columns')),
        ('columns', category_value('OST_StructuralColumns')),
        ('equipment', category_value('OST_MechanicalEquipment')),
        ('equipment', category_value('OST_SpecialityEquipment')),
    ]

    seen_ids = set()
    for stat_name, bic in category_specs:
        for element in collect(bic):
            element_key = eid_int(element.Id)
            unique_key = (stat_name, element_key)
            if unique_key in seen_ids:
                continue
            seen_ids.add(unique_key)

            # Selected or auto-scanned cabinets are route sources, not obstacles.
            if is_host and element_key in selected_host_cabinet_ids:
                continue

            try:
                # Broad phase 0: do not even request Solid geometry when the
                # element BoundingBox misses the compartment or analysis band.
                element_box = element_bbox_data(element, source_view, external_transform)
                if not bbox_overlaps_analysis_band(element_box):
                    continue
                if not bbox_overlaps_compartment(element_box, half_diag + clearance):
                    continue

                solid_records = []
                for solid in get_solid_geometry(element, external_transform, source_view):
                    solid_box = solid_bbox_data(solid)
                    if not bbox_overlaps_analysis_band(solid_box):
                        continue
                    if not bbox_overlaps_compartment(solid_box, half_diag + clearance):
                        continue
                    solid_records.append((solid, solid_box))

                if solid_records:
                    if element_box is None:
                        element_box = union_bbox_data([record[1] for record in solid_records])
                    solid_element_obstacles.append({
                        'element_box': element_box,
                        'solid_records': solid_records,
                        'category_group': stat_name,
                        'element_id': element_key,
                    })
                    obstacle_stats['solid_elements'] += 1
                    obstacle_stats['solids'] += len(solid_records)
                elif element_box is not None:
                    # Only families without a valid physical Solid use this
                    # conservative fallback.
                    fallback_boxes.append((
                        element_box[0], element_box[1],
                        element_box[2], element_box[3]
                    ))
                    obstacle_stats['fallback_boxes'] += 1

                if solid_records or element_box is not None:
                    obstacle_stats[stat_name] += 1
            except Exception:
                continue


# Host model.
collect_source_obstacles(doc, None, True, None, False)

# Revit links represented in the active host view.
revit_major = app_version_major()
link_visible_collector_supported = revit_major >= 2024

if INCLUDE_REVIT_LINKS:
    try:
        link_instances = list(
            DB.FilteredElementCollector(doc, view.Id)
            .OfClass(DB.RevitLinkInstance)
            .WhereElementIsNotElementType()
        )
    except Exception:
        link_instances = []

    for link_instance in link_instances:
        try:
            link_doc = link_instance.GetLinkDocument()
            if link_doc is None:
                continue

            use_visible_link_collector = link_visible_collector_supported
            collect_source_obstacles(
                link_doc,
                link_instance.GetTotalTransform(),
                False,
                link_instance,
                use_visible_link_collector
            )
            obstacle_stats['links'] += 1
            if not use_visible_link_collector:
                obstacle_stats['link_visibility_fallbacks'] += 1
        except Exception:
            continue


# =============================================================================
# RASTERIZE OBSTACLES INTO THE GRID
# =============================================================================
# Keep wall raster separate so door portals clear only wall cells. They must
# never erase columns or equipment that happen to stand inside an opening.
wall_blocked = bytearray(cell_count)
blocked = bytearray(cell_count)


def grid_range_from_bbox(u0, u1, v0, v1):
    i0 = int(math.floor((u0 - min_u) / cell))
    i1 = int(math.floor((u1 - min_u) / cell))
    j0 = int(math.floor((v0 - min_v) / cell))
    j1 = int(math.floor((v1 - min_v) / cell))

    i0 = max(0, min(nu - 1, i0))
    i1 = max(0, min(nu - 1, i1))
    j0 = max(0, min(nv - 1, j0))
    j1 = max(0, min(nv - 1, j1))
    return i0, i1, j0, j1


def boxes_overlap_2d(a0, a1, a2, a3, b0, b1, b2, b3):
    return not (a1 < b0 or a0 > b1 or a3 < b2 or a2 > b3)


def expanded_cell_box(i, j):
    # Expanding the cell footprint by clearance is equivalent to keeping the
    # route away from the physical solid without constructing an offset solid.
    u0 = min_u + i * cell - clearance
    u1 = min_u + (i + 1) * cell + clearance
    v0 = min_v + j * cell - clearance
    v1 = min_v + (j + 1) * cell + clearance
    return u0, u1, v0, v1


def rasterize_wall_segment(segment):
    ax, ay, bx, by = segment[0], segment[1], segment[2], segment[3]
    radius_sq = segment[4]
    i0, i1, j0, j1 = grid_range_from_bbox(
        segment[5], segment[6], segment[7], segment[8]
    )

    for j in range(j0, j1 + 1):
        center_v = min_v + (j + 0.5) * cell
        row_start = j * nu
        for i in range(i0, i1 + 1):
            index = row_start + i
            if not inside[index] or wall_blocked[index]:
                continue
            center_u = min_u + (i + 0.5) * cell
            if point_segment_distance_sq(center_u, center_v, ax, ay, bx, by) <= radius_sq:
                wall_blocked[index] = 1


def portal_contains_point(portal, u_value, v_value, margin=0.0):
    du = u_value - portal['center_u']
    dv = v_value - portal['center_v']
    along = du * portal['tangent_u'] + dv * portal['tangent_v']
    normal = du * portal['normal_u'] + dv * portal['normal_v']
    return (
        abs(along) <= portal['half_width'] + margin and
        abs(normal) <= portal['half_depth'] + margin
    )


def clear_wall_cell_if_in_portal(portal, i, j, tunnel=False):
    if i < 0 or i >= nu or j < 0 or j >= nv:
        return 0
    index = j * nu + i
    if not inside[index] or not wall_blocked[index]:
        return 0

    center_u = min_u + (i + 0.5) * cell
    center_v = min_v + (j + 0.5) * cell
    if not portal_contains_point(portal, center_u, center_v, half_cell * 0.10):
        return 0

    wall_blocked[index] = 0
    if tunnel:
        obstacle_stats['door_portal_tunnel_cells'] += 1
    else:
        obstacle_stats['door_portal_cells_carved'] += 1
    return 1


def carve_door_portal(portal):
    tangent_u = portal['tangent_u']
    tangent_v = portal['tangent_v']
    normal_u = portal['normal_u']
    normal_v = portal['normal_v']
    half_width = portal['half_width']
    half_depth = portal['half_depth']

    extent_u = abs(tangent_u) * half_width + abs(normal_u) * half_depth
    extent_v = abs(tangent_v) * half_width + abs(normal_v) * half_depth
    i0, i1, j0, j1 = grid_range_from_bbox(
        portal['center_u'] - extent_u - half_cell,
        portal['center_u'] + extent_u + half_cell,
        portal['center_v'] - extent_v - half_cell,
        portal['center_v'] + extent_v + half_cell
    )

    carved = 0
    inside_candidate_count = 0
    for j in range(j0, j1 + 1):
        center_v = min_v + (j + 0.5) * cell
        for i in range(i0, i1 + 1):
            index = j * nu + i
            if not inside[index]:
                continue
            center_u = min_u + (i + 0.5) * cell
            if not portal_contains_point(portal, center_u, center_v):
                continue
            inside_candidate_count += 1
            if wall_blocked[index]:
                wall_blocked[index] = 0
                carved += 1
                obstacle_stats['door_portal_cells_carved'] += 1

    if inside_candidate_count == 0:
        obstacle_stats['door_portals_without_inside_cells'] += 1
        return 0

    # Create a continuous raster tunnel through the expanded wall band. The
    # samples are close enough that orthogonal neighbours are also cleared,
    # preventing the no-corner-cutting Dijkstra rule from breaking the portal.
    sample_step = max(cell * 0.10, cell * DOOR_RASTER_SAMPLE_STEP_FACTOR)
    sample_count = max(2, int(math.ceil((2.0 * half_depth) / sample_step)))

    lane_offsets = [0.0]
    if half_width >= cell * 0.90:
        lane = min(half_width * 0.45, cell * 0.45)
        lane_offsets.extend([-lane, lane])

    for sample_index in range(sample_count + 1):
        factor = sample_index / float(sample_count)
        normal_offset = -half_depth + 2.0 * half_depth * factor
        for lane_offset in lane_offsets:
            sample_u = (
                portal['center_u'] +
                tangent_u * lane_offset +
                normal_u * normal_offset
            )
            sample_v = (
                portal['center_v'] +
                tangent_v * lane_offset +
                normal_v * normal_offset
            )
            approximate_i = int(math.floor((sample_u - min_u) / cell))
            approximate_j = int(math.floor((sample_v - min_v) / cell))

            # Clear a constrained 3 x 3 neighbourhood. This guarantees a
            # connected orthogonal corridor even when the wall or door is
            # diagonal relative to the grid.
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    carved += clear_wall_cell_if_in_portal(
                        portal,
                        approximate_i + di,
                        approximate_j + dj,
                        tunnel=True
                    )

    return carved


def rasterize_fallback_box(box):
    # Fallback is intentionally conservative because no valid Solid was found.
    u0 = box[0] - clearance
    u1 = box[1] + clearance
    v0 = box[2] - clearance
    v1 = box[3] + clearance
    i0, i1, j0, j1 = grid_range_from_bbox(
        u0 - half_cell, u1 + half_cell,
        v0 - half_cell, v1 + half_cell
    )

    for j in range(j0, j1 + 1):
        row_start = j * nu
        for i in range(i0, i1 + 1):
            index = row_start + i
            if not inside[index] or blocked[index]:
                continue
            cu0, cu1, cv0, cv1 = expanded_cell_box(i, j)
            if boxes_overlap_2d(cu0, cu1, cv0, cv1, u0, u1, v0, v1):
                blocked[index] = 1


try:
    solid_curve_options = DB.SolidCurveIntersectionOptions()
    try:
        solid_curve_options.ResultType = DB.SolidCurveIntersectionMode.CurveSegmentsInside
    except Exception:
        pass
except Exception:
    solid_curve_options = None

line_z0 = analysis_z_min - mm_to_ft(5.0)
line_z1 = analysis_z_max + mm_to_ft(5.0)


def vertical_line_intersects_solid(solid, u_value, v_value):
    try:
        line = DB.Line.CreateBound(
            uvz_to_xyz(u_value, v_value, line_z0),
            uvz_to_xyz(u_value, v_value, line_z1)
        )
        if solid_curve_options is None:
            return False
        result = solid.IntersectWithCurve(line, solid_curve_options)
        return result is not None and result.SegmentCount > 0
    except Exception:
        return False


def fast_ray_blocks_cell(solid, solid_box, i, j):
    # Nine samples catch obvious interior hits cheaply. A missed edge hit is
    # handled by the exact Boolean phase below.
    u0, u1, v0, v1 = expanded_cell_box(i, j)
    center_u = (u0 + u1) * 0.5
    center_v = (v0 + v1) * 0.5
    sample_points = (
        (center_u, center_v),
        (u0, v0), (u1, v0), (u1, v1), (u0, v1),
        (center_u, v0), (u1, center_v),
        (center_u, v1), (u0, center_v),
    )

    for sample_u, sample_v in sample_points:
        if sample_u < solid_box[0] or sample_u > solid_box[1]:
            continue
        if sample_v < solid_box[2] or sample_v > solid_box[3]:
            continue
        obstacle_stats['fast_ray_tests'] += 1
        if vertical_line_intersects_solid(solid, sample_u, sample_v):
            obstacle_stats['fast_ray_hits'] += 1
            return True
    return False


cell_prism_cache = {}


def make_cell_prism(i, j):
    cache_key = j * nu + i
    cached = cell_prism_cache.get(cache_key)
    if cached is not None:
        return cached

    u0, u1, v0, v1 = expanded_cell_box(i, j)
    p0 = uvz_to_xyz(u0, v0, analysis_z_min)
    p1 = uvz_to_xyz(u1, v0, analysis_z_min)
    p2 = uvz_to_xyz(u1, v1, analysis_z_min)
    p3 = uvz_to_xyz(u0, v1, analysis_z_min)

    loop = DB.CurveLoop()
    loop.Append(DB.Line.CreateBound(p0, p1))
    loop.Append(DB.Line.CreateBound(p1, p2))
    loop.Append(DB.Line.CreateBound(p2, p3))
    loop.Append(DB.Line.CreateBound(p3, p0))

    loops = List[DB.CurveLoop]()
    loops.Add(loop)
    depth = max(mm_to_ft(1.0), analysis_z_max - analysis_z_min)
    prism = DB.GeometryCreationUtilities.CreateExtrusionGeometry(
        loops,
        DB.XYZ.BasisZ,
        depth
    )

    if len(cell_prism_cache) >= MAX_CELL_PRISM_CACHE:
        cell_prism_cache.clear()
    cell_prism_cache[cache_key] = prism
    return prism


def dense_ray_blocks_cell(solid, solid_box, i, j):
    # Used only when a Boolean operation fails. A 5 x 5 pattern is still much
    # cheaper than treating the whole BoundingBox as blocked.
    u0, u1, v0, v1 = expanded_cell_box(i, j)
    for row in range(5):
        factor_v = row / 4.0
        sample_v = v0 + (v1 - v0) * factor_v
        for column in range(5):
            factor_u = column / 4.0
            sample_u = u0 + (u1 - u0) * factor_u
            if sample_u < solid_box[0] or sample_u > solid_box[1]:
                continue
            if sample_v < solid_box[2] or sample_v > solid_box[3]:
                continue
            obstacle_stats['fast_ray_tests'] += 1
            if vertical_line_intersects_solid(solid, sample_u, sample_v):
                obstacle_stats['fast_ray_hits'] += 1
                return True
    return False


def exact_boolean_blocks_cell(solid, solid_box, i, j):
    obstacle_stats['boolean_tests'] += 1
    try:
        cell_prism = make_cell_prism(i, j)
        intersection = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
            cell_prism,
            solid,
            DB.BooleanOperationsType.Intersect
        )
        if intersection is not None and intersection.Volume > MIN_BOOLEAN_INTERSECTION_VOLUME_FT3:
            obstacle_stats['boolean_hits'] += 1
            return True
        return False
    except Exception:
        obstacle_stats['boolean_failures'] += 1
        if dense_ray_blocks_cell(solid, solid_box, i, j):
            return True
        return bool(CONSERVATIVE_ON_BOOLEAN_FAILURE)


def rasterize_solid_element(obstacle):
    element_box = obstacle['element_box']
    solid_records = obstacle['solid_records']
    if element_box is None or not solid_records:
        return

    # Candidate range is derived once from the element BoundingBox. This avoids
    # comparing every obstacle against every grid cell.
    candidate_u0 = element_box[0] - clearance - half_cell
    candidate_u1 = element_box[1] + clearance + half_cell
    candidate_v0 = element_box[2] - clearance - half_cell
    candidate_v1 = element_box[3] + clearance + half_cell
    i0, i1, j0, j1 = grid_range_from_bbox(
        candidate_u0, candidate_u1, candidate_v0, candidate_v1
    )

    for j in range(j0, j1 + 1):
        row_start = j * nu
        for i in range(i0, i1 + 1):
            index = row_start + i
            if not inside[index] or blocked[index]:
                continue

            obstacle_stats['candidate_cells'] += 1
            cu0, cu1, cv0, cv1 = expanded_cell_box(i, j)

            # Broad phase 1: expanded cell against element BoundingBox.
            if not boxes_overlap_2d(
                cu0, cu1, cv0, cv1,
                element_box[0], element_box[1],
                element_box[2], element_box[3]
            ):
                obstacle_stats['element_bbox_rejects'] += 1
                continue

            cell_is_blocked = False
            for solid, solid_box in solid_records:
                # Broad phase 2: expanded cell against each Solid BoundingBox.
                if not boxes_overlap_2d(
                    cu0, cu1, cv0, cv1,
                    solid_box[0], solid_box[1],
                    solid_box[2], solid_box[3]
                ):
                    obstacle_stats['solid_bbox_rejects'] += 1
                    continue

                # Narrow phase 1: cheap multi-ray test.
                if USE_FAST_RAY_NARROW_PHASE:
                    if fast_ray_blocks_cell(solid, solid_box, i, j):
                        cell_is_blocked = True
                        break

                # Narrow phase 2: exact Solid-vs-expanded-cell-prism Boolean.
                # This is called only for ambiguous boundary cells.
                if USE_EXACT_BOOLEAN_NARROW_PHASE:
                    if exact_boolean_blocks_cell(solid, solid_box, i, j):
                        cell_is_blocked = True
                        break

            if cell_is_blocked:
                blocked[index] = 1


total_obstacle_items = (
    len(wall_segments) +
    len(door_portals) +
    len(fallback_boxes) +
    len(solid_element_obstacles)
)
processed_obstacles = 0

if total_obstacle_items > 0:
    with forms.ProgressBar(
        title='Rasterizing optimized obstacles: {value} of {max_value}',
        cancellable=True
    ) as progress:
        for wall_segment in wall_segments:
            rasterize_wall_segment(wall_segment)
            processed_obstacles += 1
            progress.update_progress(processed_obstacles, total_obstacle_items)
            if progress.cancelled:
                script.exit()

        # Carve doors only after all wall segments have been rasterized.
        for door_portal in door_portals:
            carve_door_portal(door_portal)
            processed_obstacles += 1
            progress.update_progress(processed_obstacles, total_obstacle_items)
            if progress.cancelled:
                script.exit()

        # Merge the final wall mask into the global obstacle mask.
        for index in inside_indices:
            if wall_blocked[index]:
                blocked[index] = 1

        for fallback_box in fallback_boxes:
            rasterize_fallback_box(fallback_box)
            processed_obstacles += 1
            progress.update_progress(processed_obstacles, total_obstacle_items)
            if progress.cancelled:
                script.exit()

        for solid_element in solid_element_obstacles:
            rasterize_solid_element(solid_element)
            processed_obstacles += 1
            progress.update_progress(processed_obstacles, total_obstacle_items)
            if progress.cancelled:
                script.exit()

# Release potentially large transient Solid cache before path calculation.
cell_prism_cache.clear()

walkable = bytearray(cell_count)
walkable_indices = []
for index in inside_indices:
    if not blocked[index]:
        walkable[index] = 1
        walkable_indices.append(index)

walkable_count = len(walkable_indices)
if walkable_count == 0:
    forms.alert('No walkable grid cell was found inside the compartment.', exitscript=True)


# =============================================================================
# SNAP SELECTED CABINETS TO WALKABLE GRID CELLS
# =============================================================================
def cabinet_geometry_center_and_facing(cabinet, source_view, external_transform, cached_center=None, cached_method=None):
    center = cached_center
    center_method = cached_method
    if center is None:
        center, center_method = get_cabinet_geometry_center(
            cabinet,
            source_view,
            external_transform
        )
    if center is None:
        return None

    center_u, center_v = xyz_to_uv(center)

    facing_uv = None
    try:
        facing = cabinet.FacingOrientation
        if external_transform is not None:
            facing = external_transform.OfVector(facing)
        facing_uv = normalize_2d(
            facing.DotProduct(right),
            facing.DotProduct(up)
        )
    except Exception:
        facing_uv = None

    return center_u, center_v, facing_uv, center_method

def nearest_walkable_cell(
        source_u,
        source_v,
        halfspace_origin=None,
        halfspace_direction=None,
        search_radius_mm=CABINET_SEARCH_RADIUS_MM
):
    approximate_i = int(math.floor((source_u - min_u) / cell))
    approximate_j = int(math.floor((source_v - min_v) / cell))
    max_radius = max(3, int(math.ceil(mm_to_ft(search_radius_mm) / cell)))

    best_index = None
    best_distance = float('inf')

    for radius in range(max_radius + 1):
        i0 = max(0, approximate_i - radius)
        i1 = min(nu - 1, approximate_i + radius)
        j0 = max(0, approximate_j - radius)
        j1 = min(nv - 1, approximate_j + radius)

        for j in range(j0, j1 + 1):
            row_start = j * nu
            for i in range(i0, i1 + 1):
                if radius > 0 and i > i0 and i < i1 and j > j0 and j < j1:
                    continue
                index = row_start + i
                if not walkable[index]:
                    continue

                center_u = min_u + (i + 0.5) * cell
                center_v = min_v + (j + 0.5) * cell

                if halfspace_origin is not None and halfspace_direction is not None:
                    projection = (
                        (center_u - halfspace_origin[0]) * halfspace_direction[0] +
                        (center_v - halfspace_origin[1]) * halfspace_direction[1]
                    )
                    if projection < -cell * 0.25:
                        continue

                du = center_u - source_u
                dv = center_v - source_v
                distance = math.sqrt(du * du + dv * dv)
                if distance < best_distance:
                    best_distance = distance
                    best_index = index

        if best_index is not None and best_distance <= (radius + 0.75) * cell:
            break

    return best_index, best_distance


seed_costs = {}
failed_cabinets = []
connected_cabinet_count = 0
cabinet_unconstrained_snap_fallbacks = 0
cabinet_center_method_counts = {
    'solid_geometry': 0,
    'element_bbox': 0,
    'location_fallback': 0,
}

for cabinet_index, cabinet_record in enumerate(cabinet_records):
    cabinet_record['diagnostic_number'] = cabinet_index + 1
    cabinet_record['connection_status'] = 'Not processed'
    cabinet_record['snap_distance'] = None
    cabinet_record['used_unconstrained_facing'] = False
    cabinet_record['seed_index'] = None
    cabinet_record['center_u'] = None
    cabinet_record['center_v'] = None
    cabinet_record['center_xyz'] = cabinet_record.get('geometry_center')
    cabinet_record['diagnostic_marker_ids'] = []

    cabinet = cabinet_record['element']
    data = cabinet_geometry_center_and_facing(
        cabinet,
        cabinet_record.get('source_view'),
        cabinet_record.get('external_transform'),
        cabinet_record.get('geometry_center'),
        cabinet_record.get('center_method')
    )
    if data is None:
        cabinet_record['connection_status'] = 'Failed: no geometry centre'
        failed_cabinets.append(cabinet_record)
        continue

    center_u, center_v, facing_uv, center_method = data

    # Manual Pick follows the same rule as Auto Scan: only a cabinet whose
    # physical geometry centre is inside the selected Fire Compartment may
    # become a Dijkstra source. This prevents an external or wrong cabinet
    # from snapping into the compartment through the 2D search radius.
    if not region_contains(compartment_polygons, center_u, center_v):
        cabinet_record['connection_status'] = 'Failed: centre outside selected compartment'
        failed_cabinets.append(cabinet_record)
        continue

    cabinet_box = element_bbox_data(
        cabinet,
        cabinet_record.get('source_view'),
        cabinet_record.get('external_transform')
    )
    if cabinet_box is not None and not bbox_overlaps_analysis_band(cabinet_box):
        cabinet_record['connection_status'] = 'Failed: outside analysis height band'
        failed_cabinets.append(cabinet_record)
        continue

    cabinet_record['center_u'] = center_u
    cabinet_record['center_v'] = center_v
    cabinet_record['center_method'] = center_method
    if cabinet_record.get('center_xyz') is None:
        cabinet_record['center_xyz'] = uv_to_xyz(center_u, center_v)
    if center_method in cabinet_center_method_counts:
        cabinet_center_method_counts[center_method] += 1

    # The route starts at the physical geometry centre. Dijkstra uses grid-cell
    # centres, so the only initial cost is the straight snap distance from the
    # geometry centre to the selected walkable cell.
    halfspace_origin = (center_u, center_v) if facing_uv is not None else None
    seed_index, snap_distance = nearest_walkable_cell(
        center_u,
        center_v,
        halfspace_origin,
        facing_uv
    )

    # Retry only the grid connection without a facing constraint when a family
    # has an incorrect or non-standard FacingOrientation. The source point does
    # not change and remains the geometry centre.
    if seed_index is None and facing_uv is not None:
        seed_index, snap_distance = nearest_walkable_cell(
            center_u,
            center_v,
            None,
            None
        )
        if seed_index is not None:
            cabinet_unconstrained_snap_fallbacks += 1
            cabinet_record['used_unconstrained_facing'] = True

    if seed_index is None:
        cabinet_record['connection_status'] = 'Failed: no walkable grid cell'
        failed_cabinets.append(cabinet_record)
        continue

    initial_cost = snap_distance
    cabinet_record['connection_status'] = 'Connected'
    cabinet_record['snap_distance'] = snap_distance
    cabinet_record['seed_index'] = seed_index

    connected_cabinet_count += 1
    old_cost = seed_costs.get(seed_index)
    if old_cost is None or initial_cost < old_cost:
        seed_costs[seed_index] = initial_cost

if not seed_costs:
    forms.alert(
        'No valid cabinet source could be connected to the walkable grid.\n\n'
        'Manual cabinets must be Family instances whose physical centre is inside '
        'the selected compartment and whose geometry overlaps the analysis height band.\n'
        'Also check the grid size and obstacle clearance.',
        exitscript=True
    )

# Nearest-neighbour diagnostics are useful for spotting duplicate linked
# instances or several keyword matches located at effectively the same point.
for cabinet_record in cabinet_records:
    cabinet_record['nearest_cabinet_number'] = None
    cabinet_record['nearest_cabinet_distance'] = None
    source_u = cabinet_record.get('center_u')
    source_v = cabinet_record.get('center_v')
    if source_u is None or source_v is None:
        continue

    best_record = None
    best_distance = float('inf')
    for other_record in cabinet_records:
        if other_record is cabinet_record:
            continue
        other_u = other_record.get('center_u')
        other_v = other_record.get('center_v')
        if other_u is None or other_v is None:
            continue
        du = other_u - source_u
        dv = other_v - source_v
        distance = math.sqrt(du * du + dv * dv)
        if distance < best_distance:
            best_distance = distance
            best_record = other_record

    if best_record is not None:
        cabinet_record['nearest_cabinet_number'] = best_record.get('diagnostic_number')
        cabinet_record['nearest_cabinet_distance'] = best_distance


# =============================================================================
# MULTI-SOURCE DIJKSTRA FROM VALID CABINET SOURCES
# =============================================================================
infinity = float('inf')
sqrt2 = math.sqrt(2.0)
neighbor_steps = [
    (-1, 0, cell),
    (1, 0, cell),
    (0, -1, cell),
    (0, 1, cell),
    (-1, -1, cell * sqrt2),
    (1, -1, cell * sqrt2),
    (-1, 1, cell * sqrt2),
    (1, 1, cell * sqrt2),
]


def edge_stays_inside(i0, j0, i1, j1):
    u0 = min_u + (i0 + 0.5) * cell
    v0 = min_v + (j0 + 0.5) * cell
    u1 = min_u + (i1 + 0.5) * cell
    v1 = min_v + (j1 + 0.5) * cell

    # Sample the edge at quarter points. This prevents shortcuts across a
    # concave compartment edge or an internal hole without performing a costly
    # curve intersection for every Dijkstra step.
    for factor in (0.25, 0.50, 0.75):
        test_u = u0 + (u1 - u0) * factor
        test_v = v0 + (v1 - v0) * factor
        if not region_contains(compartment_polygons, test_u, test_v):
            return False
    return True


final_seed_costs = dict(seed_costs)

# Final coverage pass starts directly from every valid cabinet source.
distances = [infinity] * cell_count
priority_queue = []

for seed_index, seed_cost in final_seed_costs.items():
    distances[seed_index] = seed_cost
    heapq.heappush(priority_queue, (seed_cost, seed_index))

popped_count = 0
settled = bytearray(cell_count)
progress_update_interval = 200

with forms.ProgressBar(
    title='Running multi-source Dijkstra: {value} of {max_value}',
    cancellable=True
) as progress:
    while priority_queue:
        current_distance, current_index = heapq.heappop(priority_queue)
        if current_distance > distances[current_index] + 1.0e-12:
            continue
        if settled[current_index]:
            continue
        settled[current_index] = 1
        popped_count += 1

        if popped_count % progress_update_interval == 0:
            progress.update_progress(min(popped_count, walkable_count), walkable_count)
            if progress.cancelled:
                script.exit()

        # The heap is ordered. Once its smallest item exceeds the limit, all
        # remaining routes are also outside coverage and the search can stop.
        if current_distance > max_distance + 1.0e-9:
            break

        current_i = current_index % nu
        current_j = current_index // nu

        for di, dj, step_cost in neighbor_steps:
            next_i = current_i + di
            next_j = current_j + dj
            if next_i < 0 or next_i >= nu or next_j < 0 or next_j >= nv:
                continue

            next_index = next_j * nu + next_i
            if not walkable[next_index]:
                continue

            # Prevent diagonal movement through the corner of a wall or column.
            if di != 0 and dj != 0:
                side_index_1 = current_j * nu + next_i
                side_index_2 = next_j * nu + current_i
                if not walkable[side_index_1] or not walkable[side_index_2]:
                    continue

            if not edge_stays_inside(current_i, current_j, next_i, next_j):
                continue

            new_distance = current_distance + step_cost
            if new_distance > max_distance + 1.0e-9:
                continue

            if new_distance + 1.0e-12 < distances[next_index]:
                distances[next_index] = new_distance
                heapq.heappush(priority_queue, (new_distance, next_index))

    progress.update_progress(min(popped_count, walkable_count), walkable_count)


# =============================================================================
# UNCOVERED GRID AND RECTANGLE MERGING
# =============================================================================
uncovered = bytearray(cell_count)
uncovered_count = 0
covered_count = 0

for index in walkable_indices:
    if distances[index] <= max_distance + 1.0e-9:
        covered_count += 1
    else:
        uncovered[index] = 1
        uncovered_count += 1

if uncovered_count == 0:
    forms.alert(
        'All analysed walkable areas are within {0:g} m from the selected valid cabinets.'.format(
            max_distance_m
        )
    )
    script.exit()

visited = bytearray(cell_count)
rectangles = []

for j in range(nv):
    for i in range(nu):
        index = j * nu + i
        if not uncovered[index] or visited[index]:
            continue

        width = 1
        while i + width < nu:
            test_index = j * nu + i + width
            if not uncovered[test_index] or visited[test_index]:
                break
            width += 1

        height = 1
        while j + height < nv:
            row_ok = True
            row_start = (j + height) * nu + i
            for offset in range(width):
                test_index = row_start + offset
                if not uncovered[test_index] or visited[test_index]:
                    row_ok = False
                    break
            if not row_ok:
                break
            height += 1

        for row in range(j, j + height):
            row_start = row * nu + i
            for offset in range(width):
                visited[row_start + offset] = 1

        rectangles.append((i, j, width, height))

if len(rectangles) > MAX_RESULT_REGIONS:
    forms.alert(
        'The result requires {0:,} preliminary regions. Increase the grid size to reduce element count.'.format(
            len(rectangles)
        ),
        exitscript=True
    )


# =============================================================================
# EXACTLY CLIP OUTPUT TO THE ORIGINAL COMPARTMENT BOUNDARY
# =============================================================================
def clone_curve_loop(source_loop):
    new_loop = DB.CurveLoop()
    for curve in source_loop:
        try:
            new_loop.Append(curve.Clone())
        except Exception:
            new_loop.Append(curve.CreateTransformed(DB.Transform.Identity))
    return new_loop


def make_rectangle_loop(u0, v0, u1, v1):
    p0 = uv_to_xyz(u0, v0)
    p1 = uv_to_xyz(u1, v0)
    p2 = uv_to_xyz(u1, v1)
    p3 = uv_to_xyz(u0, v1)

    loop = DB.CurveLoop()
    loop.Append(DB.Line.CreateBound(p0, p1))
    loop.Append(DB.Line.CreateBound(p1, p2))
    loop.Append(DB.Line.CreateBound(p2, p3))
    loop.Append(DB.Line.CreateBound(p3, p0))
    return loop


def create_extrusion_from_loops(curve_loops):
    loops = List[DB.CurveLoop]()
    for curve_loop in curve_loops:
        loops.Add(curve_loop)
    return DB.GeometryCreationUtilities.CreateExtrusionGeometry(
        loops,
        view_direction,
        EXTRUSION_DEPTH_FT
    )


def get_base_face_loop_sets(solid):
    result = []
    plane_tolerance = 1.0e-5

    for face in solid.Faces:
        if not isinstance(face, DB.PlanarFace):
            continue
        try:
            normal = face.FaceNormal.Normalize()
            if abs(normal.DotProduct(view_direction)) < 0.999999:
                continue
            plane_distance = abs((face.Origin - base_point).DotProduct(view_direction))
            if plane_distance > plane_tolerance:
                continue
            loops = face.GetEdgesAsCurveLoops()
            if loops is not None and loops.Count > 0:
                result.append(loops)
        except Exception:
            continue

    return result


try:
    compartment_profile_loops = []
    for boundary_loop in boundaries:
        compartment_profile_loops.append(clone_curve_loop(boundary_loop))
    compartment_solid = create_extrusion_from_loops(compartment_profile_loops)
except Exception:
    forms.alert(
        'Cannot create the compartment clipping solid. Check that the Filled Region has valid closed loops.\n\n{0}'.format(
            traceback.format_exc()
        ),
        exitscript=True
    )

created_regions = []
failed_clip_count = 0
failed_region_count = 0
old_result_ids = []
old_diagnostic_marker_ids = []
all_diagnostic_marker_ids = []

if DELETE_OLD_RESULTS:
    try:
        existing_regions = list(
            DB.FilteredElementCollector(doc, view.Id)
            .OfClass(DB.FilledRegion)
            .WhereElementIsNotElementType()
        )
        for existing in existing_regions:
            try:
                parameter = existing.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                text = parameter.AsString() if parameter is not None else None
                if text and text.startswith(RESULT_COMMENT_PREFIX):
                    old_result_ids.append(existing.Id)
            except Exception:
                continue
    except Exception:
        old_result_ids = []

# Remove diagnostic Detail Lines from the previous run. They are recreated at
# the current host-model cabinet centres and provide exact zoom targets.
try:
    existing_curves = list(
        DB.FilteredElementCollector(doc, view.Id)
        .OfClass(DB.CurveElement)
        .WhereElementIsNotElementType()
    )
    for existing_curve in existing_curves:
        try:
            parameter = existing_curve.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            text_value = parameter.AsString() if parameter is not None else None
            if text_value and text_value.startswith(DIAGNOSTIC_MARKER_COMMENT_PREFIX):
                old_diagnostic_marker_ids.append(existing_curve.Id)
        except Exception:
            continue
except Exception:
    old_diagnostic_marker_ids = []

transaction = DB.Transaction(doc, 'Create fire cabinet uncovered regions - optimized')
transaction.Start()
try:
    ids_to_delete = list(old_result_ids) + list(old_diagnostic_marker_ids)
    if ids_to_delete:
        doc.Delete(make_id_list(ids_to_delete))

    for i, j, width, height in rectangles:
        u0 = min_u + i * cell
        v0 = min_v + j * cell
        u1 = min_u + (i + width) * cell
        v1 = min_v + (j + height) * cell

        try:
            rectangle_solid = create_extrusion_from_loops([
                make_rectangle_loop(u0, v0, u1, v1)
            ])
            clipped_solid = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                rectangle_solid,
                compartment_solid,
                DB.BooleanOperationsType.Intersect
            )
        except Exception:
            failed_clip_count += 1
            continue

        try:
            if clipped_solid is None or clipped_solid.Volume < 1.0e-10:
                continue
        except Exception:
            failed_clip_count += 1
            continue

        loop_sets = get_base_face_loop_sets(clipped_solid)
        if not loop_sets:
            failed_clip_count += 1
            continue

        for loop_set in loop_sets:
            try:
                region = DB.FilledRegion.Create(
                    doc,
                    selected_region_type.Id,
                    view.Id,
                    loop_set
                )
                created_regions.append(region)

                try:
                    parameter = region.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                    if parameter is not None and not parameter.IsReadOnly:
                        parameter.Set(
                            '{0}; limit={1:g}m; grid={2:g}mm; clearance={3:g}mm'.format(
                                RESULT_COMMENT,
                                max_distance_m,
                                grid_mm,
                                clearance_mm
                            )
                        )
                except Exception:
                    pass
            except Exception:
                failed_region_count += 1

    marker_half_size = mm_to_ft(DIAGNOSTIC_MARKER_HALF_SIZE_MM)
    for cabinet_record in cabinet_records:
        center_u = cabinet_record.get('center_u')
        center_v = cabinet_record.get('center_v')
        if center_u is None or center_v is None:
            continue
        try:
            p_left = uv_to_xyz(center_u - marker_half_size, center_v)
            p_right = uv_to_xyz(center_u + marker_half_size, center_v)
            p_bottom = uv_to_xyz(center_u, center_v - marker_half_size)
            p_top = uv_to_xyz(center_u, center_v + marker_half_size)

            marker_curves = [
                DB.Line.CreateBound(p_left, p_right),
                DB.Line.CreateBound(p_bottom, p_top),
            ]
            marker_ids = []
            for marker_curve in marker_curves:
                detail_curve = doc.Create.NewDetailCurve(view, marker_curve)
                marker_ids.append(detail_curve.Id)
                all_diagnostic_marker_ids.append(detail_curve.Id)
                try:
                    parameter = detail_curve.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                    if parameter is not None and not parameter.IsReadOnly:
                        parameter.Set(
                            '{0}; cabinet={1}'.format(
                                DIAGNOSTIC_MARKER_COMMENT_PREFIX,
                                cabinet_record.get('diagnostic_number')
                            )
                        )
                except Exception:
                    pass
            cabinet_record['diagnostic_marker_ids'] = marker_ids
        except Exception:
            cabinet_record['diagnostic_marker_ids'] = []

    transaction.Commit()
except Exception:
    try:
        transaction.RollBack()
    except Exception:
        pass
    forms.alert(
        'Failed to create Filled Regions.\n\n{0}'.format(traceback.format_exc()),
        exitscript=True
    )

if created_regions:
    try:
        uidoc.Selection.SetElementIds(
            make_id_list([region.Id for region in created_regions])
        )
    except Exception:
        pass


# =============================================================================
# REPORT
# =============================================================================
uncovered_area_m2 = ft2_to_m2(uncovered_count * cell * cell)
blocked_count = sum([1 for index in inside_indices if blocked[index]])

output.print_md('## Fire cabinet coverage - Optimized multi-source Dijkstra')
output.print_md('- Revit version: **{0}**'.format(revit_major))
output.print_md('- Cabinet source mode: **{0}**'.format('Auto Scan (host model only)' if auto_scan_enabled else 'Manual Pick (host model only)'))
output.print_md('- Cabinets connected: **{0} / {1}**'.format(connected_cabinet_count, len(cabinet_records)))
output.print_md('- Cabinet source validity: **FamilyInstance + physical centre inside selected compartment + analysis height overlap**')
output.print_md('- Cabinet check point: **centre of physical geometry**')
output.print_md('- Cabinet centre methods: **{0} Solid geometry**, **{1} element BoundingBox**, **{2} Location fallback**'.format(
    cabinet_center_method_counts['solid_geometry'],
    cabinet_center_method_counts['element_bbox'],
    cabinet_center_method_counts['location_fallback']
))
if cabinet_unconstrained_snap_fallbacks:
    output.print_md('- Cabinets snapped without FacingOrientation constraint: **{0}**'.format(
        cabinet_unconstrained_snap_fallbacks
    ))
if auto_scan_enabled:
    output.print_md('- Auto Scan keywords: **{0}**'.format(cabinet_keywords_raw))
output.print_md('- Maximum travel distance: **{0:g} m, including cabinet-to-grid snap distance**'.format(max_distance_m))
output.print_md('- Grid size: **{0:g} mm**'.format(grid_mm))
output.print_md('- Obstacle clearance: **{0:g} mm**'.format(clearance_mm))
output.print_md('- Analysis height band: **{0:g} to {1:g} mm above active level**'.format(
    ANALYSIS_BOTTOM_MM,
    ANALYSIS_TOP_MM
))
output.print_md('- Grid cells inside compartment: **{0:,}**'.format(len(inside_indices)))
output.print_md('- Blocked cells: **{0:,}**'.format(blocked_count))
output.print_md('- Walkable cells: **{0:,}**'.format(walkable_count))
output.print_md('- Covered cells: **{0:,}**'.format(covered_count))
output.print_md('- Uncovered cells: **{0:,}**'.format(uncovered_count))
output.print_md('- Approximate uncovered area before exact compartment clipping: **{0:.2f} m2**'.format(
    uncovered_area_m2
))
output.print_md('- Filled Regions created: **{0:,}**'.format(len(created_regions)))

if failed_cabinets:
    output.print_md('- Cabinets that could not be connected: **{0}**'.format(len(failed_cabinets)))
if auto_scan_diagnostics is not None:
    output.print_md(
        '- Auto Scan diagnostics (host model only): **{0} candidates**, '
        '**{1} keyword matches**, **{2} matches inside compartment**, '
        'centre detection: **{3} Solid**, **{4} BoundingBox**, '
        '**{5} Location fallback**'.format(
            auto_scan_diagnostics['host_candidates'],
            auto_scan_diagnostics['keyword_matches'],
            auto_scan_diagnostics['inside_matches'],
            auto_scan_diagnostics['solid_geometry_centres'],
            auto_scan_diagnostics['element_bbox_centres'],
            auto_scan_diagnostics['location_fallback_centres']
        )
    )


# -----------------------------------------------------------------------------
# CABINET DIAGNOSTIC TABLE
# -----------------------------------------------------------------------------
output.print_md('### Cabinet diagnostic list')
output.print_md(
    '> Click **Zoom** to go to the exact host-model cabinet check point. '
    'The temporary Detail Line crosses are automatically deleted and rebuilt on the next run.'
)
output.print_md(
    '| No. | Zoom | Status | Source | Link Instance | Cabinet Element ID | Category | Family : Type | Matched keyword | Centre method | Centre X / Y / Z above level (mm) | Snap (mm) | Nearest cabinet |'
)
output.print_md(
    '|---:|:---:|---|---|---:|---:|---|---|---|---|---|---:|---|'
)

for cabinet_record in cabinet_records:
    cabinet = cabinet_record.get('element')
    number = cabinet_record.get('diagnostic_number', '')

    marker_ids = cabinet_record.get('diagnostic_marker_ids') or []
    if marker_ids:
        try:
            zoom_link = output.linkify(marker_ids, title='Zoom')
        except Exception:
            zoom_link = 'Zoom marker'
    elif cabinet_record.get('is_host') and cabinet is not None:
        try:
            zoom_link = output.linkify(cabinet.Id, title='Zoom')
        except Exception:
            zoom_link = '-'
    else:
        zoom_link = '-'

    is_host = bool(cabinet_record.get('is_host'))
    if is_host:
        source_text = 'Host'
        link_text = '-'
        try:
            element_text = output.linkify(cabinet.Id, title=str(eid_int(cabinet.Id)))
        except Exception:
            element_text = str(eid_int(cabinet.Id)) if cabinet is not None else '-'
    else:
        source_text = cabinet_record.get('source_label') or cabinet_record.get('link_document_title') or 'Link'
        link_instance_id = cabinet_record.get('link_instance_id')
        if link_instance_id is not None:
            try:
                link_text = output.linkify(DB.ElementId(int(link_instance_id)), title=str(link_instance_id))
            except Exception:
                link_text = str(link_instance_id)
        else:
            link_text = '-'
        element_text = str(eid_int(cabinet.Id)) if cabinet is not None else '-'

    category_name = '-'
    try:
        if cabinet is not None and cabinet.Category is not None:
            category_name = to_text(cabinet.Category.Name)
    except Exception:
        pass

    family_name = ''
    type_name = safe_element_name(cabinet) if cabinet is not None else ''
    try:
        symbol = cabinet.Symbol
        if symbol is not None:
            type_name = safe_element_name(symbol) or type_name
            if symbol.Family is not None:
                family_name = to_text(symbol.Family.Name)
    except Exception:
        pass
    family_type_text = family_name
    if family_name and type_name:
        family_type_text = '{0} : {1}'.format(family_name, type_name)
    elif type_name:
        family_type_text = type_name
    if not family_type_text:
        family_type_text = cabinet_record.get('display_name') or '-'

    matched_keywords = cabinet_record.get('matched_keywords') or []
    keyword_text = '; '.join(matched_keywords) if matched_keywords else '-'

    center_method = cabinet_record.get('center_method') or '-'
    center_xyz = cabinet_record.get('center_xyz')
    if center_xyz is not None:
        center_text = '{0:.0f} / {1:.0f} / {2:.0f}'.format(
            ft_to_mm(center_xyz.X),
            ft_to_mm(center_xyz.Y),
            ft_to_mm(center_xyz.Z - level_elevation)
        )
    else:
        center_text = '-'

    snap_distance = cabinet_record.get('snap_distance')
    snap_text = '{0:.0f}'.format(ft_to_mm(snap_distance)) if snap_distance is not None else '-'

    nearest_number = cabinet_record.get('nearest_cabinet_number')
    nearest_distance = cabinet_record.get('nearest_cabinet_distance')
    if nearest_number is not None and nearest_distance is not None:
        nearest_text = '#{0} / {1:.0f} mm'.format(nearest_number, ft_to_mm(nearest_distance))
    else:
        nearest_text = '-'

    status_text = cabinet_record.get('connection_status') or '-'
    if cabinet_record.get('used_unconstrained_facing'):
        status_text += ' (Facing ignored)'

    output.print_md(
        '| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} | {11} | {12} |'.format(
            number,
            zoom_link,
            markdown_cell(status_text),
            markdown_cell(source_text),
            link_text,
            element_text,
            markdown_cell(category_name),
            markdown_cell(family_type_text),
            markdown_cell(keyword_text),
            markdown_cell(center_method),
            markdown_cell(center_text),
            snap_text,
            markdown_cell(nearest_text)
        )
    )

if all_diagnostic_marker_ids:
    try:
        all_marker_link = output.linkify(all_diagnostic_marker_ids, title='Select all cabinet diagnostic markers')
        output.print_md(
            '- {0} — press **Delete** only when you no longer need the zoom targets. '
            'They will also be removed automatically on the next run.'.format(all_marker_link)
        )
    except Exception:
        pass

if failed_clip_count:
    output.print_md('- Rectangle Boolean clips failed: **{0:,}**'.format(failed_clip_count))
if failed_region_count:
    output.print_md('- Filled Region creations failed: **{0:,}**'.format(failed_region_count))

output.print_md(
    '- Obstacles: **{0} walls / {1} wall segments**, **{2} door openings**, '
    '**{3} columns**, **{4} equipment**, **{5} true solids in {6} solid elements**, '
    '**{7} bounding-box fallbacks**, **{8} Revit links**'.format(
        obstacle_stats['walls'],
        obstacle_stats['wall_segments'],
        obstacle_stats['doors'],
        obstacle_stats['columns'],
        obstacle_stats['equipment'],
        obstacle_stats['solids'],
        obstacle_stats['solid_elements'],
        obstacle_stats['fallback_boxes'],
        obstacle_stats['links']
    )
)
output.print_md(
    '- Door portal diagnostics: **{0} parameter widths**, **{1} BoundingBox widths**, '
    '**{2} default widths**, **{3} inserts found from walls**, **{4} nearest-wall fallbacks**, '
    '**{5} normal carved cells**, **{6} raster-tunnel cells**, **{7} portals outside the compartment grid**'.format(
        obstacle_stats['door_parameter_widths'],
        obstacle_stats['door_bbox_widths'],
        obstacle_stats['door_default_widths'],
        obstacle_stats['door_wall_inserts'],
        obstacle_stats['door_host_fallbacks'],
        obstacle_stats['door_portal_cells_carved'],
        obstacle_stats['door_portal_tunnel_cells'],
        obstacle_stats['door_portals_without_inside_cells']
    )
)
output.print_md(
    '> Door openings are carved after the complete wall raster is built. Each door portal '
    'also receives an orthogonal raster tunnel across the inflated wall band, so a '
    'coarse or diagonal grid cannot accidentally close an otherwise valid doorway.'
)

if obstacle_stats['link_visibility_fallbacks']:
    output.print_md(
        '> Warning: Revit 2023 or older cannot use the host-view linked-element collector. '
        'The script used a linked-document fallback for **{0}** link instance(s), so hidden linked '
        'elements may still be included after bounding-box and height filtering.'.format(
            obstacle_stats['link_visibility_fallbacks']
        )
    )
else:
    output.print_md(
        '> Linked elements were collected with host-view visibility filtering available in Revit 2024 or newer.'
    )

output.print_md(
    '> Final travel distance is calculated directly from all valid selected cabinets by one multi-source Dijkstra pass. PathOfTravel.FindShortestPaths is not called.'
)
output.print_md('- Hybrid geometry checks: **{0:,} candidate cells**, **{1:,} solid-BBox rejects**, '
                '**{2:,} ray tests / {3:,} ray hits**, **{4:,} Boolean tests / {5:,} Boolean hits**, '
                '**{6:,} Boolean failures**'.format(
                    obstacle_stats['candidate_cells'],
                    obstacle_stats['solid_bbox_rejects'],
                    obstacle_stats['fast_ray_tests'],
                    obstacle_stats['fast_ray_hits'],
                    obstacle_stats['boolean_tests'],
                    obstacle_stats['boolean_hits'],
                    obstacle_stats['boolean_failures']
                ))
output.print_md(
    '> Column and equipment checks use: element BoundingBox -> Solid BoundingBox -> '
    'nine fast vertical rays -> exact Solid/expanded-cell-prism Boolean only for ambiguous cells. '
    'A bounding box becomes the final obstacle only when the family returns no valid Solid.'
)
output.print_md(
    '> Result rectangles are Boolean-intersected with the original compartment Solid before Filled Region creation, so the output cannot extend beyond the selected compartment boundary.'
)
