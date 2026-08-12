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
from Autodesk.Revit.DB.Analysis import PathOfTravel
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType, ObjectSnapTypes
from Autodesk.Revit.Exceptions import OperationCanceledException
from System import Guid, String
from System.Collections.Generic import List
from Autodesk.Revit.DB.ExtensibleStorage import Schema, SchemaBuilder, Entity, AccessLevel

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
CABINET_SEARCH_RADIUS_MM = 5000.0

# Door portal settings. Doors are carved from the wall raster AFTER all wall
# segments are blocked. This is more reliable than asking whether a cell centre
# happens to fall inside a small door rectangle while the wall is rasterized.
DOOR_DEFAULT_WIDTH_MM = 900.0
DOOR_MIN_VALID_WIDTH_MM = 300.0
DOOR_MAX_VALID_WIDTH_MM = 5000.0
DOOR_PORTAL_WIDTH_EXTRA_MM = 25.0
DOOR_PORTAL_DEPTH_EXTRA_CELLS = 1.25
DOOR_FORCE_SAMPLE_STEP_FACTOR = 0.25
DOOR_NEAREST_WALL_SEARCH_MM = 1500.0
INCLUDE_WALL_OPENING_ELEMENTS = True

# Performance and output limits.
MAX_GRID_CELLS = 150000
MAX_UNCOVERED_CLUSTER_MARKERS = 500
MIN_SOLID_VOLUME_FT3 = 1.0e-7
MIN_BOOLEAN_INTERSECTION_VOLUME_FT3 = 1.0e-10

# Hybrid narrow-phase settings. The fast ray test handles obvious hits.
# Exact Boolean intersection is called only for ambiguous cells whose expanded
# footprint overlaps both the element and solid bounding boxes.
USE_FAST_RAY_NARROW_PHASE = True
USE_EXACT_BOOLEAN_NARROW_PHASE = True
CONSERVATIVE_ON_BOOLEAN_FAILURE = False
MAX_CELL_PRISM_CACHE = 5000

INCLUDE_REVIT_LINKS = True
DELETE_OLD_RESULTS = True

# Route-result settings. Every generated PathOfTravel and diagnostic
# DetailCurve receives an Extensible Storage entity, allowing the next run to
# delete only elements created by this tool. Comments are a readable fallback.
ROUTE_RESULT_SCHEMA_GUID = Guid('7d8ab5e5-3c9e-4f27-9f0d-82bb2e7d55d4')
ROUTE_RESULT_SCHEMA_NAME = 'PyRevitFireCabinetCoverageRouteResult'
ROUTE_RESULT_FIELD_NAME = 'Payload'
ROUTE_COMMENT_PREFIX = 'PYREVIT_FIRE_CABINET_ROUTE'
LEGACY_RESULT_COMMENT_PREFIXES = (
    'PYREVIT_FIRE_COVERAGE_RESULT',
    'PYREVIT_FIRE_COVERAGE_OPTIMIZED',
    'PYREVIT_FIRE_CABINET_DIAGNOSTIC',
)
DEFAULT_ENDPOINT_MARKER_MM = 300.0
DEFAULT_CREATE_UNCOVERED_MARKERS = True
DEFAULT_FORCE_DOOR = False
RUN_MODE_FULL_REBUILD = 'full_rebuild'
RUN_MODE_FORCE_DOOR_APPEND = 'force_door_append'
DEFAULT_RUN_MODE = RUN_MODE_FULL_REBUILD
CABINET_MARKER_HALF_SIZE_MM = 120.0
OWNER_TIE_TOLERANCE_MM = 0.1
FORCE_DOOR_SEARCH_LIMIT_FACTOR = 1.0

# Native Revit Path of Travel rejects control points that are too close. The
# Dijkstra route is therefore reduced to the endpoints of each straight raster
# run. A waypoint is retained only at a real direction change, at the route
# start/end, or at the selected Force Door. This removes the former periodic
# 1200 mm waypoints from long straight corridors while preserving every bend.
PATH_CONTROL_MIN_SPACING_MM = 500.0
PATH_GUIDE_MAX_SPACING_MM = 1200.0  # retained for backward-compatible reports
# Close turn points can still be merged when the replacement chord is proven
# walkable. Start, end and Force Door points remain mandatory.
PATH_WAYPOINT_REDUCE_THRESHOLD_MM = 1200.0
PATH_WAYPOINT_REDUCE_MAX_MERGED_MM = 2400.0
PATH_GUIDE_SEARCH_LIMIT_MM = 2600.0
PATH_GUIDE_SAMPLE_STEP_FACTOR = 0.20
PATH_GUIDE_MAX_CONTROLS = 160
# Visibility-based compression works on the complete Dijkstra parent chain,
# not only on exact 8-neighbour turn points. This removes 300/424 mm staircase
# turns while keeping the simplified chord close to the original route.
PATH_CORRIDOR_DEVIATION_FACTORS = (0.60, 0.90, 1.25)
PATH_CORRIDOR_STRETCH_RATIO = 1.30
PATH_CORRIDOR_EXTRA_LENGTH_CELLS = 0.75
FORCE_DOOR_CROSSING_TOLERANCE_MM = 125.0
FORCE_DOOR_CROSSING_MAX_SPAN_MM = 3500.0
MAX_FORCE_DOORS_PER_CABINET = 3  # legacy door-routing constant; no longer used by point constraints
MAX_FORCE_POINTS_PER_CABINET = 6
FORCE_POINT_MAX_SNAP_MM = 900.0
FORCE_NATIVE_SAMPLE_STEP_MM = 100.0
ROUTE_CHECKPOINT_DISTANCE_M = 35.0


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


class ForceDoorCrossingError(Exception):
    """Raised when a native Path of Travel does not cross the selected door."""
    def __init__(self, message, diagnostics=None):
        Exception.__init__(self, message)
        self.diagnostics = diagnostics or {}


# =============================================================================
# SELECTION FILTERS
# =============================================================================
class FilledRegionSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, DB.FilledRegion)

    def AllowReference(self, reference, point):
        return False


class PathOfTravelSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        try:
            return (
                isinstance(element, PathOfTravel) and
                element.OwnerViewId == view.Id
            )
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


class ModelElementSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        try:
            category = element.Category
            return category is not None and category.CategoryType == DB.CategoryType.Model
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


class LinkedElementSelectionFilter(ISelectionFilter):
    # Keep the linked-element selection filter deliberately permissive.
    # Revit can call AllowReference while the cursor is still resolving a link
    # reference. Reading LinkedElementId/category at that moment is unreliable
    # in some Revit/pyRevit combinations and can make every linked door appear
    # unpickable. Door-category validation is therefore performed only after
    # the user clicks Finish and stable Reference objects have been returned.
    def AllowElement(self, element):
        return isinstance(element, DB.RevitLinkInstance)

    def AllowReference(self, reference, point):
        return True


def is_door_element(element):
    try:
        if element is None or element.Category is None:
            return False
        return eid_int(element.Category.Id) == eid_int(DB.ElementId(DB.BuiltInCategory.OST_Doors))
    except Exception:
        return False


class RouteFailurePreprocessor(DB.IFailuresPreprocessor):
    """Remove route-analysis warnings posted by failed trial paths.

    Invalid trial paths are rolled back by the surrounding SubTransaction. The
    warning dialog would otherwise interrupt batch creation even though the
    script is already handling the failure and trying a safer alternative.
    Errors are never swallowed.
    """
    def PreprocessFailures(self, failures_accessor):
        try:
            messages = list(failures_accessor.GetFailureMessages())
        except Exception:
            messages = []
        for message in messages:
            try:
                if message.GetSeverity() == DB.FailureSeverity.Warning:
                    failures_accessor.DeleteWarning(message)
            except Exception:
                continue
        return DB.FailureProcessingResult.Continue


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

selected_compartment_id = eid_int(compartment.Id)
selected_compartment_view_id = eid_int(view.Id)


# Detail Line styles below are used only for cabinet/endpoint/uncovered markers.
# Main route geometry is created as PathOfTravel using the LineStyle selected in the UI.
def collect_line_style_rows():
    rows = []
    try:
        lines_category = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
        for subcategory in lines_category.SubCategories:
            try:
                graphics_style = subcategory.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
                if graphics_style is None:
                    continue
                rows.append((
                    u'{0}  [Id {1}]'.format(to_text(subcategory.Name), eid_int(graphics_style.Id)),
                    graphics_style
                ))
            except Exception:
                continue
    except Exception:
        rows = []
    rows.sort(key=lambda item: item[0].lower())
    return rows


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


line_style_rows = collect_line_style_rows()
if not line_style_rows:
    forms.alert('No Detail Line style exists for diagnostic markers.', exitscript=True)


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
        self.line_style_rows = rows

        self.max_distance_tb.Text = to_text(config_get('max_distance_m', DEFAULT_MAX_DISTANCE_M))
        self.grid_size_tb.Text = to_text(config_get('grid_mm', DEFAULT_GRID_MM))
        self.clearance_tb.Text = to_text(config_get('clearance_mm', DEFAULT_CLEARANCE_MM))
        self.endpoint_marker_tb.Text = to_text(config_get('endpoint_marker_mm', DEFAULT_ENDPOINT_MARKER_MM))
        self.uncovered_marker_cb.IsChecked = config_bool(
            'create_uncovered_markers',
            DEFAULT_CREATE_UNCOVERED_MARKERS
        )
        saved_run_mode = to_text(config_get('run_mode', DEFAULT_RUN_MODE))
        self.force_append_rb.IsChecked = (saved_run_mode == RUN_MODE_FORCE_DOOR_APPEND)
        self.full_rebuild_rb.IsChecked = not (self.force_append_rb.IsChecked == True)
        self.auto_scan_cb.IsChecked = config_bool('auto_scan', DEFAULT_AUTO_SCAN)
        self.keyword_tb.Text = to_text(config_get('cabinet_keywords', DEFAULT_CABINET_KEYWORDS))

        saved_line_style_id = -1
        try:
            saved_line_style_id = int(config_get('line_style_id', -1))
        except Exception:
            saved_line_style_id = -1

        selected_index = 0
        for index, row in enumerate(rows):
            self.line_style_cb.Items.Add(row[0])
            if eid_int(row[1].Id) == saved_line_style_id:
                selected_index = index
        self.line_style_cb.SelectedIndex = selected_index

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
                self.keyword_status.Text = 'Auto scan FHR: Find cabinets in the host model only.'
                self.keyword_status.Foreground = self.brush_from_hex('#8A5A00')
            else:
                self.keyword_panel.Background = self.brush_from_hex('#F1F3F5')
                self.keyword_panel.BorderBrush = self.brush_from_hex('#C8CDD2')
                self.keyword_status.Text = 'Manual Pick FHR: Select Run, then pick the cabinets.'
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
            endpoint_marker_mm_value = parse_float_text(
                self.endpoint_marker_tb.Text,
                '35 m checkpoint marker size',
                False
            )
            auto_scan_value = (self.auto_scan_cb.IsChecked == True)
            create_uncovered_markers_value = (self.uncovered_marker_cb.IsChecked == True)
            if self.force_append_rb.IsChecked == True:
                run_mode_value = RUN_MODE_FORCE_DOOR_APPEND
            else:
                run_mode_value = RUN_MODE_FULL_REBUILD
            # Point picking is intrinsic to Add New. Rebuild never asks for points.
            force_door_value = (run_mode_value == RUN_MODE_FORCE_DOOR_APPEND)
            raw_keywords = to_text(self.keyword_tb.Text).strip()
            parsed_keywords = split_keywords(raw_keywords)
            if auto_scan_value and not parsed_keywords:
                raise ValueError('Enter at least one cabinet keyword for Auto Scan.')

            selected_index = self.line_style_cb.SelectedIndex
            if selected_index < 0 or selected_index >= len(self.line_style_rows):
                raise ValueError('Select a Detail Line style for diagnostic markers.')

            self.values = {
                'max_distance_m': max_distance_m_value,
                'grid_mm': grid_mm_value,
                'clearance_mm': clearance_mm_value,
                'endpoint_marker_mm': endpoint_marker_mm_value,
                'create_uncovered_markers': create_uncovered_markers_value,
                'force_door': force_door_value,
                'run_mode': run_mode_value,
                'auto_scan': auto_scan_value,
                'raw_keywords': raw_keywords,
                'keywords': parsed_keywords,
                'line_style': self.line_style_rows[selected_index][1],
            }
            self.accepted = True
            self.Close()
        except Exception as error:
            self.show_error(error)

    def cancel_click(self, sender, args):
        self.accepted = False
        self.Close()


xaml_file = script.get_bundle_file('ui.xaml')
settings_window = CoverageSettingsWindow(xaml_file, line_style_rows)
settings_window.show_dialog()

if not settings_window.accepted or settings_window.values is None:
    script.exit()

ui_values = settings_window.values
max_distance_m = ui_values['max_distance_m']
grid_mm = ui_values['grid_mm']
clearance_mm = ui_values['clearance_mm']
endpoint_marker_mm = ui_values['endpoint_marker_mm']
create_uncovered_markers = ui_values['create_uncovered_markers']
force_door_enabled = ui_values['force_door']
run_mode = ui_values['run_mode']
auto_scan_enabled = ui_values['auto_scan']
cabinet_keywords_raw = ui_values['raw_keywords']
cabinet_keywords = ui_values['keywords']
selected_line_style = ui_values['line_style']

# No sample Path of Travel is required. A native PathOfTravel.Create call always
# performs Revit route analysis, so creating a disposable two-point sample would
# not be a true no-obstacle operation and would add unnecessary work. Apply the
# user-selected GraphicsStyle directly to every generated Path of Travel instead.
sample_path_id = None
sample_path_line_style_id = selected_line_style.Id

# Save settings immediately after the user confirms the window.
try:
    config.max_distance_m = max_distance_m
    config.grid_mm = grid_mm
    config.clearance_mm = clearance_mm
    config.endpoint_marker_mm = endpoint_marker_mm
    config.create_uncovered_markers = create_uncovered_markers
    config.force_door = force_door_enabled
    config.run_mode = run_mode
    config.auto_scan = auto_scan_enabled
    config.cabinet_keywords = cabinet_keywords_raw
    config.line_style_id = eid_int(selected_line_style.Id)
    script.save_config()
except Exception:
    pass

max_distance = m_to_ft(max_distance_m)
cell = mm_to_ft(grid_mm)
clearance = mm_to_ft(clearance_mm)
half_cell = cell * 0.5
half_diag = cell * math.sqrt(2.0) * 0.5
endpoint_marker_half_size = mm_to_ft(endpoint_marker_mm) * 0.5

# Cabinet sources are always host-model elements. Auto Scan and Manual Pick do
# not collect cabinets from Revit links. Linked models are still used for
# obstacles and Force Door selection.
cabinet_records = []
selected_host_cabinet_ids = set()
selected_link_cabinet_ids = {}
manual_cabinet_refs = []

if not auto_scan_enabled:
    try:
        manual_cabinet_refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            ModelElementSelectionFilter(),
            'Select fire cabinets, then click Finish.'
        )
    except OperationCanceledException:
        script.exit()

    if not manual_cabinet_refs or len(manual_cabinet_refs) == 0:
        forms.alert('No fire cabinet was selected.', exitscript=True)

    for reference in manual_cabinet_refs:
        cabinet = doc.GetElement(reference.ElementId)
        if cabinet is None:
            continue
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
        selected_host_cabinet_ids.add(eid_int(cabinet.Id))

# Mandatory route-point selection. Points are picked directly in the active view.
# Their cabinet ownership is resolved later, after the normal multi-source
# Dijkstra ownership map has been calculated. Press Esc to finish picking.
selected_force_points = []
selected_force_point_ignored_rows = []

if run_mode == RUN_MODE_FORCE_DOOR_APPEND:
    try:
        no_snap = getattr(ObjectSnapTypes, 'None')
    except Exception:
        no_snap = ObjectSnapTypes.Endpoints

    point_number = 1
    while True:
        try:
            picked_xyz = uidoc.Selection.PickPoint(
                no_snap,
                'Pick mandatory route point #{0}. Press Esc when finished.'.format(point_number)
            )
            selected_force_points.append({
                'number': point_number,
                'xyz': picked_xyz,
                'u': None,
                'v': None,
                'waypoint_index': None,
                'snap_distance': None,
                'assigned_cabinet_index': None,
                'assignment_status': 'Pending',
            })
            point_number += 1
        except OperationCanceledException:
            break

    if run_mode == RUN_MODE_FORCE_DOOR_APPEND and not selected_force_points:
        forms.alert(
            'No mandatory route point was picked.\n\n'
            'No existing route was changed.',
            exitscript=True
        )


# =============================================================================
# VIEW PLANE AND COMPARTMENT BOUNDARY
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


def get_link_family_instances(link_instance, use_visible_collector):
    if use_visible_collector:
        try:
            return list(
                DB.FilteredElementCollector(doc, view.Id, link_instance.Id)
                .OfClass(DB.FamilyInstance)
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
            .OfClass(DB.FamilyInstance)
            .WhereElementIsNotElementType()
        )
    except Exception:
        return []


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
    # Auto Scan intentionally searches the HOST model only. Linked models remain
    # available to obstacle collection and Force Door selection, but never become
    # cabinet route sources.
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

    return records, diagnostics


auto_scan_diagnostics = None
if auto_scan_enabled:
    cabinet_records, auto_scan_diagnostics = collect_auto_cabinet_records(
        cabinet_keywords
    )
    if not cabinet_records:
        forms.alert(
            'Auto Scan did not find any cabinet inside the selected compartment.\n\n'
            'Check the host-model keywords, active-view visibility and cabinet elevation.',
            exitscript=True
        )

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
    'door_portal_cells_forced': 0,
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
            if is_host:
                if element_key in selected_host_cabinet_ids:
                    continue
            else:
                link_key = eid_int(link_instance.Id) if link_instance is not None else -1
                if element_key in selected_link_cabinet_ids.get(link_key, set()):
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


def clear_wall_cell_if_in_portal(portal, i, j, forced=False):
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
    if forced:
        obstacle_stats['door_portal_cells_forced'] += 1
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

    # Force a continuous raster tunnel through the expanded wall band. The
    # samples are close enough that orthogonal neighbours are also cleared,
    # preventing the no-corner-cutting Dijkstra rule from breaking the portal.
    sample_step = max(cell * 0.10, cell * DOOR_FORCE_SAMPLE_STEP_FACTOR)
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
                        forced=True
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

def nearest_walkable_cell(source_u, source_v, halfspace_origin=None, halfspace_direction=None, excluded_indices=None):
    approximate_i = int(math.floor((source_u - min_u) / cell))
    approximate_j = int(math.floor((source_v - min_v) / cell))
    max_radius = max(3, int(math.ceil(mm_to_ft(CABINET_SEARCH_RADIUS_MM) / cell)))

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
                if excluded_indices is not None and index in excluded_indices:
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


# Sort Auto Scan results so ownership tie-breaking is deterministic between runs.
def cabinet_stable_sort_key(record):
    try:
        element_id = eid_int(record['element'].Id)
    except Exception:
        element_id = 2147483647
    if record.get('is_host'):
        return (0, 0, element_id)
    return (1, int(record.get('link_instance_id') or 0), element_id)


if auto_scan_enabled:
    cabinet_records.sort(key=cabinet_stable_sort_key)

cabinet_sources = []
reserved_seed_indices = set()
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
    cabinet_record['route_status'] = 'Not processed'
    cabinet_record['snap_distance'] = None
    cabinet_record['used_unconstrained_facing'] = False
    cabinet_record['seed_index'] = None
    cabinet_record['center_u'] = None
    cabinet_record['center_v'] = None
    cabinet_record['center_xyz'] = cabinet_record.get('geometry_center')
    cabinet_record['cabinet_marker_ids'] = []
    cabinet_record['endpoint_marker_ids'] = []
    cabinet_record['route_ids'] = []
    cabinet_record['route_distance'] = None
    cabinet_record['owned_covered_cells'] = 0
    cabinet_record['path_grid_steps'] = 0
    cabinet_record['route_segment_count'] = 0

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
        cabinet_record['route_status'] = 'No route'
        failed_cabinets.append(cabinet_record)
        continue

    center_u, center_v, facing_uv, center_method = data
    cabinet_record['center_u'] = center_u
    cabinet_record['center_v'] = center_v
    cabinet_record['center_method'] = center_method
    if cabinet_record.get('center_xyz') is None:
        cabinet_record['center_xyz'] = uv_to_xyz(center_u, center_v)
    if center_method in cabinet_center_method_counts:
        cabinet_center_method_counts[center_method] += 1

    halfspace_origin = (center_u, center_v) if facing_uv is not None else None
    seed_index, snap_distance = nearest_walkable_cell(
        center_u,
        center_v,
        halfspace_origin,
        facing_uv,
        reserved_seed_indices
    )

    # Retry without FacingOrientation, but still require a unique grid seed.
    if seed_index is None and facing_uv is not None:
        seed_index, snap_distance = nearest_walkable_cell(
            center_u,
            center_v,
            None,
            None,
            reserved_seed_indices
        )
        if seed_index is not None:
            cabinet_unconstrained_snap_fallbacks += 1
            cabinet_record['used_unconstrained_facing'] = True

    if seed_index is None:
        cabinet_record['connection_status'] = 'Failed: no unique walkable grid cell'
        cabinet_record['route_status'] = 'No route'
        failed_cabinets.append(cabinet_record)
        continue

    reserved_seed_indices.add(seed_index)
    cabinet_record['connection_status'] = 'Connected'
    cabinet_record['route_status'] = 'Pending'
    cabinet_record['snap_distance'] = snap_distance
    cabinet_record['seed_index'] = seed_index

    source = {
        'cabinet_index': cabinet_index,
        'seed_index': seed_index,
        'seed_cost': snap_distance,
    }
    cabinet_sources.append(source)
    connected_cabinet_count += 1

if not cabinet_sources:
    forms.alert(
        'None of the cabinet sources could be connected to a unique walkable grid cell.\n'
        'Check cabinet geometry, duplicate cabinets, compartment boundary and obstacle clearance.',
        exitscript=True
    )

# Nearest-neighbour diagnostics help identify duplicate linked instances or
# several keyword matches at effectively the same physical point.
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
# FORCE DOOR WAYPOINT ASSIGNMENT AND OWNED MULTI-SOURCE DIJKSTRA
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

    for factor in (0.25, 0.50, 0.75):
        test_u = u0 + (u1 - u0) * factor
        test_v = v0 + (v1 - v0) * factor
        if not region_contains(compartment_polygons, test_u, test_v):
            return False
    return True


def iter_walkable_neighbors(current_index):
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

        # No diagonal corner cutting.
        if di != 0 and dj != 0:
            side_index_1 = current_j * nu + next_i
            side_index_2 = next_j * nu + current_i
            if not walkable[side_index_1] or not walkable[side_index_2]:
                continue

        if not edge_stays_inside(current_i, current_j, next_i, next_j):
            continue

        yield next_index, step_cost


def run_temporary_dijkstra(start_index, target_indices, maximum_cost, keep_parents=False):
    """Shortest grid distances from one door waypoint to cabinet seed cells.

    This temporary search is used only to assign selected doors to the nearest
    cabinet by actual walkable distance. Coverage ownership is still calculated
    later by one multi-source Dijkstra pass.
    """
    target_set = set(target_indices)
    found = {}
    local_distances = {start_index: 0.0}
    local_parents = {} if keep_parents else None
    local_queue = [(0.0, start_index)]
    settled_local = set()

    while local_queue and len(found) < len(target_set):
        current_distance, current_index = heapq.heappop(local_queue)
        if current_index in settled_local:
            continue
        known_distance = local_distances.get(current_index, infinity)
        if current_distance > known_distance + 1.0e-12:
            continue
        if current_distance > maximum_cost + 1.0e-9:
            break

        settled_local.add(current_index)
        if current_index in target_set:
            found[current_index] = current_distance
            if len(found) >= len(target_set):
                break

        for next_index, step_cost in iter_walkable_neighbors(current_index):
            if next_index in settled_local:
                continue
            new_distance = current_distance + step_cost
            if new_distance > maximum_cost + 1.0e-9:
                continue
            old_distance = local_distances.get(next_index, infinity)
            if new_distance < old_distance - 1.0e-12:
                local_distances[next_index] = new_distance
                if keep_parents:
                    local_parents[next_index] = current_index
                heapq.heappush(local_queue, (new_distance, next_index))

    return found, local_parents



def run_baseline_owned_dijkstra():
    """Compute the normal cabinet ownership before any Force Door constraint.

    This immutable ownership map defines each cabinet's territory. Every forced
    route is later restricted to cells owned by the same cabinet, so a selected
    door can never pull a route through another cabinet's coverage territory.
    """
    local_distances = [infinity] * cell_count
    local_owners = [-1] * cell_count
    local_parents = [-1] * cell_count
    local_queue = []

    for source in cabinet_sources:
        source_index = source['seed_index']
        source_cost = source['seed_cost']
        cabinet_index = source['cabinet_index']
        old_distance = local_distances[source_index]
        old_owner = local_owners[source_index]
        if (
            source_cost < old_distance - 1.0e-12 or
            (
                abs(source_cost - old_distance) <= 1.0e-12 and
                (old_owner < 0 or cabinet_index < old_owner)
            )
        ):
            local_distances[source_index] = source_cost
            local_owners[source_index] = cabinet_index
            local_parents[source_index] = -1
            heapq.heappush(
                local_queue,
                (source_cost, cabinet_index, source_index)
            )

    tie_tolerance = mm_to_ft(OWNER_TIE_TOLERANCE_MM)
    settled_local = bytearray(cell_count)
    while local_queue:
        current_distance, current_owner, current_index = heapq.heappop(local_queue)
        if current_distance > local_distances[current_index] + 1.0e-12:
            continue
        if current_owner != local_owners[current_index]:
            continue
        if settled_local[current_index]:
            continue
        settled_local[current_index] = 1
        if current_distance > max_distance + 1.0e-9:
            break

        for next_index, step_cost in iter_walkable_neighbors(current_index):
            if settled_local[next_index]:
                continue
            new_distance = current_distance + step_cost
            if new_distance > max_distance + 1.0e-9:
                continue
            old_distance = local_distances[next_index]
            old_owner = local_owners[next_index]
            strictly_better = new_distance < old_distance - 1.0e-12
            deterministic_tie = (
                abs(new_distance - old_distance) <= tie_tolerance and
                (old_owner < 0 or current_owner < old_owner)
            )
            if strictly_better or deterministic_tie:
                local_distances[next_index] = new_distance
                local_owners[next_index] = current_owner
                local_parents[next_index] = current_index
                heapq.heappush(
                    local_queue,
                    (new_distance, current_owner, next_index)
                )

    return local_distances, local_owners, local_parents


def force_grid_index_to_uv(index):
    i = index % nu
    j = index // nu
    return (min_u + (i + 0.5) * cell, min_v + (j + 0.5) * cell)


def advance_force_door_state(
    index_a,
    index_b,
    portals,
    crossed_mask,
    pending_negative_mask,
    pending_positive_mask
):
    """Advance multi-door crossing state across one adjacent raster edge.

    A door is marked crossed only after the route moves from one clear side of
    its wall centre plane to the other inside the selected doorway width. A
    centre-plane cell is represented as a pending state, so touching the door
    centre and returning to the same side does not count as a crossing.
    """
    point_a = force_grid_index_to_uv(index_a)
    point_b = force_grid_index_to_uv(index_b)
    new_crossed = crossed_mask
    new_pending_negative = pending_negative_mask
    new_pending_positive = pending_positive_mask
    newly_crossed_mask = 0
    plane_tolerance = min(cell * 0.25, mm_to_ft(100.0))

    for portal_index, portal in enumerate(portals):
        bit = 1 << portal_index
        if new_crossed & bit:
            continue

        du_a = point_a[0] - portal['center_u']
        dv_a = point_a[1] - portal['center_v']
        du_b = point_b[0] - portal['center_u']
        dv_b = point_b[1] - portal['center_v']
        normal_a = du_a * portal['normal_u'] + dv_a * portal['normal_v']
        normal_b = du_b * portal['normal_u'] + dv_b * portal['normal_v']
        inside_a = portal_contains_point(
            portal, point_a[0], point_a[1], cell * 0.25
        )
        inside_b = portal_contains_point(
            portal, point_b[0], point_b[1], cell * 0.25
        )

        negative_a = normal_a < -plane_tolerance
        positive_a = normal_a > plane_tolerance
        negative_b = normal_b < -plane_tolerance
        positive_b = normal_b > plane_tolerance
        near_a = not negative_a and not positive_a
        near_b = not negative_b and not positive_b

        direct_transition = (
            (negative_a and positive_b) or
            (positive_a and negative_b)
        )
        direct_inside_width = False
        if direct_transition:
            denominator = normal_b - normal_a
            factor = 0.5 if abs(denominator) < 1.0e-12 else clamp(
                -normal_a / denominator, 0.0, 1.0
            )
            cross_u = point_a[0] + (point_b[0] - point_a[0]) * factor
            cross_v = point_a[1] + (point_b[1] - point_a[1]) * factor
            du = cross_u - portal['center_u']
            dv = cross_v - portal['center_v']
            along = du * portal['tangent_u'] + dv * portal['tangent_v']
            allowed_half_width = max(
                portal.get('physical_width', 0.0) * 0.5 +
                mm_to_ft(FORCE_DOOR_CROSSING_TOLERANCE_MM),
                cell * 0.75
            )
            direct_inside_width = abs(along) <= allowed_half_width

        crossed_now = direct_transition and direct_inside_width
        if not crossed_now and (new_pending_negative & bit):
            if positive_b and (inside_a or inside_b):
                crossed_now = True
            elif negative_b and not near_b:
                new_pending_negative &= ~bit
        if not crossed_now and (new_pending_positive & bit):
            if negative_b and (inside_a or inside_b):
                crossed_now = True
            elif positive_b and not near_b:
                new_pending_positive &= ~bit

        if crossed_now:
            new_crossed |= bit
            newly_crossed_mask |= bit
            new_pending_negative &= ~bit
            new_pending_positive &= ~bit
            continue

        if negative_a and near_b and inside_b:
            new_pending_negative |= bit
            new_pending_positive &= ~bit
        elif positive_a and near_b and inside_b:
            new_pending_positive |= bit
            new_pending_negative &= ~bit
        elif near_a and near_b and (inside_a or inside_b):
            # Preserve whichever approach side is already pending while moving
            # along the doorway centre band.
            pass
        elif not inside_a and not inside_b:
            new_pending_negative &= ~bit
            new_pending_positive &= ~bit

    return (
        new_crossed,
        new_pending_negative,
        new_pending_positive,
        newly_crossed_mask
    )


def run_force_route_inside_owner(cabinet_index, portals, baseline_owners):
    """Find the farthest <= max-distance route that crosses every selected door.

    Search state stores crossed doors plus pending centre-plane approaches. All
    visited cells must remain in the cabinet's immutable baseline territory.
    """
    if not portals:
        return None
    if len(portals) > MAX_FORCE_DOORS_PER_CABINET:
        return {
            'error': 'A maximum of {0} Force Doors is supported for one cabinet.'.format(
                MAX_FORCE_DOORS_PER_CABINET
            )
        }

    source = source_by_cabinet.get(cabinet_index)
    if source is None:
        return {'error': 'Cabinet source was not found.'}
    start_index = source['seed_index']
    if baseline_owners[start_index] != cabinet_index:
        return {'error': 'Cabinet seed is outside its baseline ownership territory.'}

    full_mask = (1 << len(portals)) - 1
    start_state = (start_index, 0, 0, 0)
    state_distances = {start_state: source['seed_cost']}
    state_parents = {}
    queue = [(source['seed_cost'], 0, 0, 0, start_index)]
    settled_states = set()
    best_state = None
    best_distance = -1.0

    while queue:
        (
            current_distance,
            current_crossed,
            current_pending_negative,
            current_pending_positive,
            current_index
        ) = heapq.heappop(queue)
        state = (
            current_index,
            current_crossed,
            current_pending_negative,
            current_pending_positive
        )
        if state in settled_states:
            continue
        known_distance = state_distances.get(state, infinity)
        if current_distance > known_distance + 1.0e-12:
            continue
        if current_distance > max_distance + 1.0e-9:
            break
        settled_states.add(state)

        if current_crossed == full_mask and current_distance > best_distance:
            best_distance = current_distance
            best_state = state

        for next_index, step_cost in iter_walkable_neighbors(current_index):
            if baseline_owners[next_index] != cabinet_index:
                continue
            new_distance = current_distance + step_cost
            if new_distance > max_distance + 1.0e-9:
                continue
            (
                next_crossed,
                next_pending_negative,
                next_pending_positive,
                newly_crossed_mask
            ) = advance_force_door_state(
                current_index,
                next_index,
                portals,
                current_crossed,
                current_pending_negative,
                current_pending_positive
            )
            next_state = (
                next_index,
                next_crossed,
                next_pending_negative,
                next_pending_positive
            )
            old_distance = state_distances.get(next_state, infinity)
            if new_distance < old_distance - 1.0e-12:
                state_distances[next_state] = new_distance
                state_parents[next_state] = (state, newly_crossed_mask)
                heapq.heappush(
                    queue,
                    (
                        new_distance,
                        next_crossed,
                        next_pending_negative,
                        next_pending_positive,
                        next_index
                    )
                )

    if best_state is None:
        return {
            'error': (
                'No route inside this cabinet\'s normal ownership territory could '
                'cross all selected doors within {0:.1f} m.'
            ).format(max_distance_m)
        }

    state_chain = []
    transition_masks = []
    current_state = best_state
    guard = 0
    while True:
        state_chain.append(current_state)
        if current_state == start_state:
            break
        parent_record = state_parents.get(current_state)
        if parent_record is None:
            return {'error': 'Forced state-parent reconstruction failed.'}
        parent_state, newly_crossed_mask = parent_record
        transition_masks.append(newly_crossed_mask)
        current_state = parent_state
        guard += 1
        if guard > cell_count * (4 ** len(portals)):
            return {'error': 'Forced state-parent chain contains a cycle.'}
    state_chain.reverse()
    transition_masks.reverse()

    path_indices = [state[0] for state in state_chain]
    ordered_portals = []
    ordered_far_side_signs = []
    seen_bits = set()
    for chain_position in range(1, len(state_chain)):
        added_mask = transition_masks[chain_position - 1]
        if not added_mask:
            continue
        previous_uv = force_grid_index_to_uv(state_chain[chain_position - 1][0])
        current_uv = force_grid_index_to_uv(state_chain[chain_position][0])
        for portal_index, portal in enumerate(portals):
            bit = 1 << portal_index
            if not (added_mask & bit) or portal_index in seen_bits:
                continue
            seen_bits.add(portal_index)
            current_normal = (
                (current_uv[0] - portal['center_u']) * portal['normal_u'] +
                (current_uv[1] - portal['center_v']) * portal['normal_v']
            )
            previous_normal = (
                (previous_uv[0] - portal['center_u']) * portal['normal_u'] +
                (previous_uv[1] - portal['center_v']) * portal['normal_v']
            )
            if abs(current_normal) > mm_to_ft(5.0):
                far_side_sign = 1.0 if current_normal > 0.0 else -1.0
            else:
                far_side_sign = 1.0 if current_normal >= previous_normal else -1.0
            ordered_portals.append(portal)
            ordered_far_side_signs.append(far_side_sign)

    if len(ordered_portals) != len(portals):
        return {'error': 'Not every selected Force Door could be ordered on the reconstructed route.'}

    return {
        'path_indices': path_indices,
        'distance': best_distance,
        'end_index': best_state[0],
        'portals': ordered_portals,
        'far_side_signs': ordered_far_side_signs,
        'crossed_count': len(ordered_portals),
    }


def run_force_point_route_inside_owner(
        cabinet_index, point_records, baseline_owners, stop_at_picked_point=False):
    """Build the shortest route through all picked points assigned to one cabinet.

    Pick order is intentionally ignored. Each point has already been assigned to
    its nearest reachable cabinet by the baseline multi-source Dijkstra ownership
    map. This state-space Dijkstra then chooses the most efficient visit order for
    all points belonging to that cabinet.

    In Add New mode, the route ends at whichever picked point completes the
    shortest all-points route. It never continues toward the normal farthest
    coverage endpoint and never enters another cabinet's ownership territory.
    """
    if not point_records:
        return None
    if len(point_records) > MAX_FORCE_POINTS_PER_CABINET:
        return {
            'error': 'A maximum of {0} mandatory points is supported for one cabinet.'.format(
                MAX_FORCE_POINTS_PER_CABINET
            )
        }

    source = source_by_cabinet.get(cabinet_index)
    if source is None:
        return {'error': 'Cabinet source was not found.'}

    start_index = source['seed_index']
    if baseline_owners[start_index] != cabinet_index:
        return {'error': 'Cabinet seed is outside its baseline ownership territory.'}

    target_bits_by_index = {}
    for point_position, point_record in enumerate(point_records):
        waypoint_index = point_record.get('waypoint_index')
        if waypoint_index is None:
            return {'error': 'A picked point has no valid walkable waypoint cell.'}
        target_bits_by_index[waypoint_index] = (
            target_bits_by_index.get(waypoint_index, 0) | (1 << point_position)
        )

    full_mask = (1 << len(point_records)) - 1
    start_mask = target_bits_by_index.get(start_index, 0)
    start_state = (start_index, start_mask)
    state_distances = {start_state: source['seed_cost']}
    state_parents = {}
    queue = [(source['seed_cost'], start_mask, start_index)]
    settled_states = set()
    best_state = None
    best_distance = infinity if stop_at_picked_point else -1.0

    while queue:
        current_distance, current_mask, current_index = heapq.heappop(queue)
        state = (current_index, current_mask)
        if state in settled_states:
            continue

        known_distance = state_distances.get(state, infinity)
        if current_distance > known_distance + 1.0e-12:
            continue
        if current_distance > max_distance + 1.0e-9:
            break
        settled_states.add(state)

        if current_mask == full_mask:
            if stop_at_picked_point:
                # The first complete state settled by Dijkstra is the shortest
                # route from the cabinet that visits every assigned picked point.
                best_distance = current_distance
                best_state = state
                break
            elif current_distance > best_distance:
                best_distance = current_distance
                best_state = state

        for next_index, step_cost in iter_walkable_neighbors(current_index):
            if baseline_owners[next_index] != cabinet_index:
                continue

            new_distance = current_distance + step_cost
            if new_distance > max_distance + 1.0e-9:
                continue

            next_mask = current_mask | target_bits_by_index.get(next_index, 0)
            next_state = (next_index, next_mask)
            old_distance = state_distances.get(next_state, infinity)
            if new_distance < old_distance - 1.0e-12:
                state_distances[next_state] = new_distance
                state_parents[next_state] = state
                heapq.heappush(queue, (new_distance, next_mask, next_index))

    if best_state is None:
        return {
            'error': (
                'No route inside this cabinet\'s normal ownership territory could '
                'visit all assigned picked points within {0:.1f} m.'
            ).format(max_distance_m)
        }

    state_chain = []
    current_state = best_state
    guard = 0
    maximum_states = cell_count * (2 ** len(point_records))
    while True:
        state_chain.append(current_state)
        if current_state == start_state:
            break
        current_state = state_parents.get(current_state)
        if current_state is None:
            return {'error': 'Picked-point state-parent reconstruction failed.'}
        guard += 1
        if guard > maximum_states:
            return {'error': 'Picked-point state-parent chain contains a cycle.'}
    state_chain.reverse()

    path_indices = [state[0] for state in state_chain]
    ordered_points = []
    seen_positions = set()
    for grid_index in path_indices:
        bits = target_bits_by_index.get(grid_index, 0)
        if not bits:
            continue
        for point_position, point_record in enumerate(point_records):
            if point_position in seen_positions:
                continue
            if bits & (1 << point_position):
                seen_positions.add(point_position)
                ordered_points.append(point_record)

    if len(ordered_points) != len(point_records):
        return {'error': 'Not every picked point could be ordered on the reconstructed route.'}

    return {
        'path_indices': path_indices,
        'distance': best_distance,
        'end_index': best_state[0],
        'points': ordered_points,
        'visited_count': len(ordered_points),
        'ends_at_picked_point': bool(stop_at_picked_point),
        'optimized_visit_order': True,
    }


def nearest_walkable_cell_inside_portal(portal, excluded_indices=None):
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

    best_index = None
    best_score = None
    for j in range(j0, j1 + 1):
        for i in range(i0, i1 + 1):
            index = j * nu + i
            if not walkable[index]:
                continue
            if excluded_indices is not None and index in excluded_indices:
                continue
            center_u = min_u + (i + 0.5) * cell
            center_v = min_v + (j + 0.5) * cell
            if not portal_contains_point(portal, center_u, center_v, half_cell * 0.10):
                continue

            du = center_u - portal['center_u']
            dv = center_v - portal['center_v']
            along = abs(du * tangent_u + dv * tangent_v)
            normal = abs(du * normal_u + dv * normal_v)
            # Prefer the centreline through the wall, then the door centre.
            score = (normal, along, du * du + dv * dv, index)
            if best_score is None or score < best_score:
                best_score = score
                best_index = index

    if best_index is not None:
        return best_index

    # Defensive fallback for coarse grids: retain the selected-door behaviour
    # only if a nearby walkable cell can still be found.
    fallback_index, fallback_distance = nearest_walkable_cell(
        portal['center_u'],
        portal['center_v'],
        None,
        None,
        excluded_indices
    )
    if fallback_index is not None and fallback_distance <= cell * 1.5:
        return fallback_index
    return None


baseline_distances, baseline_owners, baseline_parents = run_baseline_owned_dijkstra()

force_door_diagnostics = {
    'enabled': bool(force_door_enabled),
    'selected': len(selected_force_points),
    'inside_compartment': 0,
    'snapped_walkable': 0,
    'assigned': 0,
    'unassigned': 0,
    'cabinet_groups': 0,
    'successful_routes': 0,
}
force_door_rows = []

source_by_cabinet = {}
for source in cabinet_sources:
    cabinet_index = source['cabinet_index']
    source['source_index'] = source['seed_index']
    source['source_cost'] = source['seed_cost']
    source['forced_prepath_indices'] = []
    source['force_point_records_ordered'] = []
    source_by_cabinet[cabinet_index] = source

for cabinet_record in cabinet_records:
    cabinet_record['force_door_status'] = 'Not constrained by picked points'
    cabinet_record['force_door_source'] = '-'
    cabinet_record['force_door_element_id'] = None
    cabinet_record['force_door_element_ids'] = []
    cabinet_record['force_door_grid_distance'] = None
    cabinet_record['force_door_total_to_waypoint'] = None
    cabinet_record['force_door_route_error'] = None
    cabinet_record['force_point_numbers'] = []

points_by_cabinet = {}
used_point_grid_indices = set()
if force_door_enabled and selected_force_points and cabinet_sources:
    max_point_snap = max(cell * 1.50, mm_to_ft(FORCE_POINT_MAX_SNAP_MM))
    for point_record in selected_force_points:
        picked_xyz = point_record['xyz']
        point_u, point_v = xyz_to_uv(picked_xyz)
        point_record['u'] = point_u
        point_record['v'] = point_v

        if not region_contains(compartment_polygons, point_u, point_v):
            point_record['assignment_status'] = 'Ignored: point is outside selected compartment'
            force_door_rows.append(point_record)
            continue
        force_door_diagnostics['inside_compartment'] += 1

        waypoint_index, snap_distance = nearest_walkable_cell(
            point_u,
            point_v,
            None,
            None,
            None
        )
        point_record['waypoint_index'] = waypoint_index
        point_record['snap_distance'] = snap_distance
        if waypoint_index is None or snap_distance > max_point_snap:
            point_record['assignment_status'] = (
                'Ignored: no nearby walkable grid cell within {0:.0f} mm'.format(
                    ft_to_mm(max_point_snap)
                )
            )
            force_door_rows.append(point_record)
            continue
        if waypoint_index in used_point_grid_indices:
            point_record['assignment_status'] = 'Ignored: duplicates another picked point grid cell'
            force_door_rows.append(point_record)
            continue
        used_point_grid_indices.add(waypoint_index)
        force_door_diagnostics['snapped_walkable'] += 1

        cabinet_index = baseline_owners[waypoint_index]
        if cabinet_index < 0:
            point_record['assignment_status'] = 'Unassigned: point is outside every cabinet coverage territory'
            force_door_rows.append(point_record)
            continue
        total_distance = baseline_distances[waypoint_index]
        if total_distance > max_distance + 1.0e-9:
            point_record['assignment_status'] = 'Unassigned: point is beyond the cabinet coverage limit'
            force_door_rows.append(point_record)
            continue

        point_record['assigned_cabinet_index'] = cabinet_index
        point_record['assigned_grid_distance'] = max(
            0.0,
            total_distance - source_by_cabinet[cabinet_index]['seed_cost']
        )
        point_record['assignment_status'] = 'Assigned to baseline owner cabinet'
        points_by_cabinet.setdefault(cabinet_index, []).append(point_record)
        force_door_diagnostics['assigned'] += 1
        force_door_rows.append(point_record)

force_door_diagnostics['cabinet_groups'] = len(points_by_cabinet)
force_door_diagnostics['unassigned'] = max(
    0,
    len(selected_force_points) - force_door_diagnostics['assigned']
)

forced_route_override_by_cabinet = {}
for cabinet_index, assigned_points in points_by_cabinet.items():
    cabinet_record = cabinet_records[cabinet_index]
    source = source_by_cabinet[cabinet_index]
    if len(assigned_points) > MAX_FORCE_POINTS_PER_CABINET:
        error_text = (
            'Cabinet has {0} picked points; the supported maximum is {1}.'
        ).format(len(assigned_points), MAX_FORCE_POINTS_PER_CABINET)
        cabinet_record['force_door_status'] = 'Picked-point route unavailable'
        cabinet_record['force_door_route_error'] = error_text
        for point_record in assigned_points:
            point_record['assignment_status'] = error_text
        continue

    forced_result = run_force_point_route_inside_owner(
        cabinet_index,
        assigned_points,
        baseline_owners,
        stop_at_picked_point=(run_mode == RUN_MODE_FORCE_DOOR_APPEND)
    )
    if not forced_result or forced_result.get('error'):
        error_text = (
            forced_result.get('error')
            if forced_result else
            'No constrained picked-point route was returned.'
        )
        cabinet_record['force_door_status'] = 'Picked-point route unavailable'
        cabinet_record['force_door_route_error'] = error_text
        for point_record in assigned_points:
            point_record['assignment_status'] = 'Assigned, but route failed: {0}'.format(error_text)
        continue

    forced_route_override_by_cabinet[cabinet_index] = forced_result
    ordered_points = forced_result['points']
    source['forced_prepath_indices'] = forced_result['path_indices']
    source['force_point_records_ordered'] = ordered_points

    point_numbers = [point_record.get('number') for point_record in ordered_points]
    if run_mode == RUN_MODE_FORCE_DOOR_APPEND:
        cabinet_record['force_door_status'] = (
            'Additional shortest route through {0} picked point(s), ending at the last point reached'.format(
                len(ordered_points)
            )
        )
    else:
        cabinet_record['force_door_status'] = (
            'Forced through {0} picked point(s), then continued to the farthest reachable endpoint'.format(
                len(ordered_points)
            )
        )
    cabinet_record['force_point_numbers'] = point_numbers
    cabinet_record['force_door_grid_distance'] = forced_result['distance']
    cabinet_record['force_door_total_to_waypoint'] = forced_result['distance']
    force_door_diagnostics['successful_routes'] += 1

    for order_number, point_record in enumerate(ordered_points, 1):
        point_record['assignment_status'] = 'Assigned; route order {0}'.format(order_number)
        point_record['route_order'] = order_number

forced_cabinet_indices = set(forced_route_override_by_cabinet.keys())
forced_cabinet_element_ids = set()
forced_cabinet_numbers = set()
for cabinet_index in forced_cabinet_indices:
    cabinet_record = cabinet_records[cabinet_index]
    forced_cabinet_numbers.add(int(cabinet_record.get('diagnostic_number') or 0))
    try:
        forced_cabinet_element_ids.add(eid_int(cabinet_record['element'].Id))
    except Exception:
        pass

if run_mode == RUN_MODE_FORCE_DOOR_APPEND and not forced_cabinet_indices:
    local_mode_name = 'Picked-point route append'
    failure_lines = []
    for cabinet_index, assigned_points in sorted(points_by_cabinet.items()):
        cabinet_record = cabinet_records[cabinet_index]
        failure_lines.append(
            'Cabinet {0} [Id {1}]: {2}'.format(
                cabinet_record.get('diagnostic_number') or cabinet_index + 1,
                eid_int(cabinet_record['element'].Id),
                cabinet_record.get('force_door_route_error') or 'No feasible route'
            )
        )
    forms.alert(
        '{0} did not produce a route through all mandatory picked points.\n\n'
        '{1}\n\nNo existing route was changed.'.format(
            local_mode_name,
            '\n'.join(failure_lines[:8]) or 'No picked point was assigned.'
        ),
        exitscript=True
    )

# Coverage and ownership always remain the normal multi-source result. Forced
# Coverage and ownership always remain the normal multi-source result. Forced
# routes are overlays constrained to this immutable ownership map.
distances = baseline_distances
owners = baseline_owners
parents = baseline_parents


# =============================================================================
# COVERAGE, OWNERSHIP AND UNCOVERED CLUSTERS
# =============================================================================
uncovered = bytearray(cell_count)
uncovered_count = 0
covered_count = 0
farthest_index_by_cabinet = [None] * len(cabinet_records)
farthest_distance_by_cabinet = [-1.0] * len(cabinet_records)
owned_covered_cells = [0] * len(cabinet_records)

for index in walkable_indices:
    owner = owners[index]
    distance = distances[index]
    if owner >= 0 and distance <= max_distance + 1.0e-9:
        covered_count += 1
        owned_covered_cells[owner] += 1

        if distance > farthest_distance_by_cabinet[owner]:
            farthest_distance_by_cabinet[owner] = distance
            farthest_index_by_cabinet[owner] = index
    else:
        uncovered[index] = 1
        uncovered_count += 1

for cabinet_index, cabinet_record in enumerate(cabinet_records):
    cabinet_record['owned_covered_cells'] = owned_covered_cells[cabinet_index]


def build_uncovered_clusters():
    clusters = []
    visited = bytearray(cell_count)
    # Four-connected clustering prevents uncovered cells on opposite sides of
    # a blocked diagonal corner from being merged into one diagnostic cluster.
    neighbor_offsets = (
        (-1, 0), (1, 0), (0, -1), (0, 1),
    )

    for start_index in walkable_indices:
        if not uncovered[start_index] or visited[start_index]:
            continue

        queue = [start_index]
        visited[start_index] = 1
        head = 0
        indices = []
        sum_u = 0.0
        sum_v = 0.0

        while head < len(queue):
            index = queue[head]
            head += 1
            indices.append(index)
            i = index % nu
            j = index // nu
            u_value = min_u + (i + 0.5) * cell
            v_value = min_v + (j + 0.5) * cell
            sum_u += u_value
            sum_v += v_value

            for di, dj in neighbor_offsets:
                next_i = i + di
                next_j = j + dj
                if next_i < 0 or next_i >= nu or next_j < 0 or next_j >= nv:
                    continue
                next_index = next_j * nu + next_i
                if uncovered[next_index] and not visited[next_index]:
                    visited[next_index] = 1
                    queue.append(next_index)

        centroid_u = sum_u / float(len(indices))
        centroid_v = sum_v / float(len(indices))
        marker_index = min(
            indices,
            key=lambda item: (
                (min_u + ((item % nu) + 0.5) * cell - centroid_u) ** 2 +
                (min_v + ((item // nu) + 0.5) * cell - centroid_v) ** 2
            )
        )
        marker_i = marker_index % nu
        marker_j = marker_index // nu
        clusters.append({
            'indices': indices,
            'cell_count': len(indices),
            'marker_index': marker_index,
            'marker_u': min_u + (marker_i + 0.5) * cell,
            'marker_v': min_v + (marker_j + 0.5) * cell,
            'marker_ids': [],
        })

    clusters.sort(key=lambda cluster: cluster['cell_count'], reverse=True)
    for cluster_index, cluster in enumerate(clusters):
        cluster['number'] = cluster_index + 1
    return clusters


uncovered_clusters = build_uncovered_clusters()


# =============================================================================
# RECONSTRUCT THE LONGEST OWNED ROUTE AND PREPARE NATIVE CONTROL POINTS
# =============================================================================
def grid_index_to_uv(index):
    i = index % nu
    j = index // nu
    return (
        min_u + (i + 0.5) * cell,
        min_v + (j + 0.5) * cell,
    )


def uv_distance(point_a, point_b):
    du = point_b[0] - point_a[0]
    dv = point_b[1] - point_a[1]
    return math.sqrt(du * du + dv * dv)


def reconstruct_path_indices(end_index, expected_owner):
    result = []
    current = end_index
    guard = 0
    while current is not None and current >= 0:
        if owners[current] != expected_owner:
            raise Exception('Route parent chain changed owner unexpectedly.')
        result.append(current)
        current = parents[current]
        guard += 1
        if guard > cell_count:
            raise Exception('Route parent chain contains a cycle.')
    result.reverse()
    return result


def clean_route_path_indices(path_indices):
    result = []
    for index in path_indices or []:
        if index is None or index < 0 or index >= cell_count:
            continue
        if result and result[-1] == index:
            continue
        result.append(index)
    return result


def raster_index_is_walkable(index):
    return (
        index is not None and
        index >= 0 and
        index < cell_count and
        bool(walkable[index])
    )


def raster_segment_is_walkable(index_a, index_b):
    """Check a straight guide chord against the custom obstacle raster.

    The test samples more densely than the grid size and also applies the same
    no-corner-cutting rule used by Dijkstra whenever the sampled cell changes
    diagonally. A native PathOfTravel control chord is accepted only when every
    crossed cell remains walkable.
    """
    if not raster_index_is_walkable(index_a) or not raster_index_is_walkable(index_b):
        return False

    point_a = grid_index_to_uv(index_a)
    point_b = grid_index_to_uv(index_b)
    distance = uv_distance(point_a, point_b)
    if distance < 1.0e-12:
        return True

    sample_step = max(
        mm_to_ft(25.0),
        cell * PATH_GUIDE_SAMPLE_STEP_FACTOR
    )
    sample_count = max(1, int(math.ceil(distance / sample_step)))
    previous_i = None
    previous_j = None

    for sample_number in range(sample_count + 1):
        factor = sample_number / float(sample_count)
        u_value = point_a[0] + (point_b[0] - point_a[0]) * factor
        v_value = point_a[1] + (point_b[1] - point_a[1]) * factor
        i = int(math.floor((u_value - min_u) / cell))
        j = int(math.floor((v_value - min_v) / cell))
        if i < 0 or i >= nu or j < 0 or j >= nv:
            return False
        index = j * nu + i
        if not walkable[index]:
            return False

        if previous_i is not None and i != previous_i and j != previous_j:
            # A diagonal transition is legal only when both orthogonal side
            # cells are also walkable. This prevents a guide chord from slicing
            # through the corner of an inflated wall or equipment obstacle.
            side_a = previous_j * nu + i
            side_b = j * nu + previous_i
            if not walkable[side_a] or not walkable[side_b]:
                return False

        previous_i = i
        previous_j = j

    return True


def route_cumulative_lengths(path_indices):
    cumulative = [0.0]
    for position in range(1, len(path_indices)):
        point_a = grid_index_to_uv(path_indices[position - 1])
        point_b = grid_index_to_uv(path_indices[position])
        cumulative.append(cumulative[-1] + uv_distance(point_a, point_b))
    return cumulative


def build_guided_span_positions(path_indices, cumulative, start_position, end_position):
    """Build raster-safe controls for one mandatory-to-mandatory path span."""
    if end_position <= start_position:
        return [start_position]

    minimum_spacing = mm_to_ft(PATH_CONTROL_MIN_SPACING_MM)
    maximum_spacing = mm_to_ft(PATH_GUIDE_MAX_SPACING_MM)
    search_limit = max(
        maximum_spacing,
        mm_to_ft(PATH_GUIDE_SEARCH_LIMIT_MM)
    )

    controls = [start_position]
    current_position = start_position

    while current_position < end_position:
        current_index = path_indices[current_position]
        current_uv = grid_index_to_uv(current_index)

        # The mandatory end of this span is preferred whenever it is not too
        # far away and its direct chord stays within the walkable raster.
        end_index = path_indices[end_position]
        end_uv = grid_index_to_uv(end_index)
        direct_to_end = uv_distance(current_uv, end_uv)
        if (
            direct_to_end >= minimum_spacing and
            direct_to_end <= maximum_spacing * 1.15 and
            raster_segment_is_walkable(current_index, end_index)
        ):
            controls.append(end_position)
            break

        best_position = None
        position = current_position + 1
        while position <= end_position:
            path_distance = cumulative[position] - cumulative[current_position]
            if path_distance > search_limit:
                break

            candidate_index = path_indices[position]
            candidate_uv = grid_index_to_uv(candidate_index)
            chord_distance = uv_distance(current_uv, candidate_uv)
            if chord_distance < minimum_spacing:
                position += 1
                continue
            if not raster_segment_is_walkable(current_index, candidate_index):
                position += 1
                continue

            # Prefer the farthest clear point up to the normal maximum. A point
            # beyond that distance is kept only as a defensive escape from a
            # tight 300 mm-grid corner where no nearer Revit-valid point exists.
            if path_distance <= maximum_spacing * 1.10:
                best_position = position
            elif best_position is None:
                best_position = position
                break
            else:
                break
            position += 1

        if best_position is None:
            raise Exception(
                'Cannot convert the Dijkstra route into raster-safe native controls '
                'near grid path position {0}.'.format(current_position)
            )

        # Avoid leaving a final sub-minimum segment to the mandatory span end.
        remaining_chord = uv_distance(
            grid_index_to_uv(path_indices[best_position]),
            end_uv
        )
        if best_position < end_position and remaining_chord < minimum_spacing:
            if (
                direct_to_end >= minimum_spacing and
                raster_segment_is_walkable(current_index, end_index)
            ):
                best_position = end_position
            else:
                earlier_position = best_position - 1
                replacement = None
                while earlier_position > current_position:
                    earlier_index = path_indices[earlier_position]
                    if (
                        uv_distance(current_uv, grid_index_to_uv(earlier_index)) >= minimum_spacing and
                        uv_distance(grid_index_to_uv(earlier_index), end_uv) >= minimum_spacing and
                        raster_segment_is_walkable(current_index, earlier_index)
                    ):
                        replacement = earlier_position
                        break
                    earlier_position -= 1
                if replacement is not None:
                    best_position = replacement

        if best_position <= current_position:
            raise Exception('Guided Path of Travel control generation did not advance.')
        controls.append(best_position)
        current_position = best_position

        if len(controls) > PATH_GUIDE_MAX_CONTROLS:
            raise Exception(
                'The route requires more than {0} native controls. Increase Grid Size '
                'or PATH_GUIDE_MAX_SPACING_MM.'.format(PATH_GUIDE_MAX_CONTROLS)
            )

    # Final spacing cleanup. Removing the penultimate point is allowed only if
    # the replacement chord is itself raster-safe.
    if len(controls) >= 3:
        last_distance = uv_distance(
            grid_index_to_uv(path_indices[controls[-2]]),
            grid_index_to_uv(path_indices[controls[-1]])
        )
        if last_distance < minimum_spacing:
            previous_position = controls[-3]
            if (
                uv_distance(
                    grid_index_to_uv(path_indices[previous_position]),
                    grid_index_to_uv(path_indices[controls[-1]])
                ) >= minimum_spacing and
                raster_segment_is_walkable(
                    path_indices[previous_position],
                    path_indices[controls[-1]]
                )
            ):
                controls.pop(-2)
            else:
                raise Exception(
                    'The final guided Path of Travel controls are too close for Revit.'
                )

    return controls



def reduce_close_control_positions(path_indices, control_positions, mandatory_positions):
    """Reduce only locally close non-mandatory PathOfTravel controls.

    A middle control B may be removed from A-B-C only when at least one of the
    adjacent pairs A-B or B-C is shorter than 1200 mm. Start, end and Force Door
    positions are mandatory and are never removed. The replacement A-C chord
    must remain walkable in the custom raster and is capped at 2400 mm so this
    cleanup cannot reintroduce a long bird-flight shortcut.
    """
    if not control_positions or len(control_positions) <= 2:
        return list(control_positions or []), 0

    threshold = mm_to_ft(PATH_WAYPOINT_REDUCE_THRESHOLD_MM)
    maximum_merged = mm_to_ft(PATH_WAYPOINT_REDUCE_MAX_MERGED_MM)
    minimum_spacing = mm_to_ft(PATH_CONTROL_MIN_SPACING_MM)
    mandatory = set(mandatory_positions or [])
    reduced = list(control_positions)
    removed_count = 0

    changed = True
    while changed and len(reduced) > 2:
        changed = False
        for position in range(1, len(reduced) - 1):
            previous_path_position = reduced[position - 1]
            current_path_position = reduced[position]
            next_path_position = reduced[position + 1]

            if current_path_position in mandatory:
                continue

            previous_index = path_indices[previous_path_position]
            current_index = path_indices[current_path_position]
            next_index = path_indices[next_path_position]

            previous_uv = grid_index_to_uv(previous_index)
            current_uv = grid_index_to_uv(current_index)
            next_uv = grid_index_to_uv(next_index)

            distance_before = uv_distance(previous_uv, current_uv)
            distance_after = uv_distance(current_uv, next_uv)

            # The user-requested reduction applies only at a close pair.
            if (
                distance_before >= threshold - 1.0e-9 and
                distance_after >= threshold - 1.0e-9
            ):
                continue

            merged_distance = uv_distance(previous_uv, next_uv)
            if merged_distance < minimum_spacing - 1.0e-9:
                continue
            if merged_distance > maximum_merged + 1.0e-9:
                continue
            if not raster_segment_is_walkable(previous_index, next_index):
                continue

            reduced.pop(position)
            removed_count += 1
            changed = True
            break

    return reduced, removed_count


def grid_step_direction(index_a, index_b):
    """Return the normalized 8-neighbour raster direction between two cells."""
    i_a = index_a % nu
    j_a = index_a // nu
    i_b = index_b % nu
    j_b = index_b // nu
    di = i_b - i_a
    dj = j_b - j_a
    if di > 0:
        di = 1
    elif di < 0:
        di = -1
    if dj > 0:
        dj = 1
    elif dj < 0:
        dj = -1
    return (di, dj)


def build_straight_run_control_positions(path_indices, mandatory_positions):
    """Keep only both ends of every straight Dijkstra raster run.

    For a sequence A-B-C, B is retained only when the normalized grid direction
    A->B differs from B->C. Mandatory positions (start, end and Force Door) are
    inserted even when they lie inside an otherwise straight run.
    """
    count = len(path_indices)
    if count <= 2:
        return list(range(count))

    positions = set(mandatory_positions or [])
    positions.add(0)
    positions.add(count - 1)

    for position in range(1, count - 1):
        direction_before = grid_step_direction(
            path_indices[position - 1],
            path_indices[position]
        )
        direction_after = grid_step_direction(
            path_indices[position],
            path_indices[position + 1]
        )
        if direction_before != direction_after:
            positions.add(position)

    return sorted(positions)


def path_span_metrics(path_indices, start_position, end_position, metrics_cache):
    """Return chord, walked length and maximum deviation for one path span."""
    key = (start_position, end_position)
    cached = metrics_cache.get(key)
    if cached is not None:
        return cached

    start_uv = grid_index_to_uv(path_indices[start_position])
    end_uv = grid_index_to_uv(path_indices[end_position])
    chord_distance = uv_distance(start_uv, end_uv)
    walked_distance = 0.0
    maximum_deviation_sq = 0.0

    previous_uv = start_uv
    for position in range(start_position + 1, end_position + 1):
        current_uv = grid_index_to_uv(path_indices[position])
        walked_distance += uv_distance(previous_uv, current_uv)
        previous_uv = current_uv
        if position < end_position:
            deviation_sq = point_segment_distance_sq(
                current_uv[0], current_uv[1],
                start_uv[0], start_uv[1],
                end_uv[0], end_uv[1]
            )
            if deviation_sq > maximum_deviation_sq:
                maximum_deviation_sq = deviation_sq

    result = (
        chord_distance,
        walked_distance,
        math.sqrt(maximum_deviation_sq)
    )
    metrics_cache[key] = result
    return result


def native_chord_is_acceptable(
    path_indices,
    start_position,
    end_position,
    maximum_deviation,
    metrics_cache
):
    """Check whether one native control chord still represents the Dijkstra path."""
    if end_position <= start_position:
        return False, None

    minimum_spacing = mm_to_ft(PATH_CONTROL_MIN_SPACING_MM)
    chord_distance, walked_distance, deviation = path_span_metrics(
        path_indices,
        start_position,
        end_position,
        metrics_cache
    )
    if chord_distance < minimum_spacing - 1.0e-9:
        return False, None

    if not raster_segment_is_walkable(
        path_indices[start_position],
        path_indices[end_position]
    ):
        return False, None

    # The deviation limit collapses a 300/400 mm staircase approximation into
    # one diagonal/native segment, while retaining real corridor bends.
    if deviation > maximum_deviation + 1.0e-9:
        return False, None

    allowed_walked_distance = (
        chord_distance * PATH_CORRIDOR_STRETCH_RATIO +
        cell * PATH_CORRIDOR_EXTRA_LENGTH_CELLS
    )
    if walked_distance > allowed_walked_distance + 1.0e-9:
        return False, None

    return True, (chord_distance, walked_distance, deviation)


def simplify_path_span_with_visibility(
    path_indices,
    start_position,
    end_position,
    maximum_deviation
):
    """Find the minimum-control raster-safe representation of one mandatory span.

    All Dijkstra cells are available as candidate controls. Therefore two close
    turn points, for example 424 mm apart on a 300 mm grid, can be replaced by a
    single better-positioned control instead of causing PathOfTravel.Create to
    fail. The selected chords must remain walkable and close to the parent chain.
    """
    if end_position <= start_position:
        return [start_position]

    metrics_cache = {}
    infinity_cost = 10 ** 9
    best_edge_count = {}
    best_deviation_sum = {}
    next_position = {}
    best_edge_count[end_position] = 0
    best_deviation_sum[end_position] = 0.0

    for current_position in range(end_position - 1, start_position - 1, -1):
        current_best_count = infinity_cost
        current_best_deviation = float('inf')
        current_best_next = None

        for candidate_position in range(current_position + 1, end_position + 1):
            if candidate_position not in best_edge_count:
                continue

            acceptable, metrics = native_chord_is_acceptable(
                path_indices,
                current_position,
                candidate_position,
                maximum_deviation,
                metrics_cache
            )
            if not acceptable:
                continue

            candidate_count = 1 + best_edge_count[candidate_position]
            candidate_deviation = metrics[2] + best_deviation_sum[candidate_position]
            choose_candidate = False
            if candidate_count < current_best_count:
                choose_candidate = True
            elif candidate_count == current_best_count:
                if candidate_deviation < current_best_deviation - 1.0e-9:
                    choose_candidate = True
                elif abs(candidate_deviation - current_best_deviation) <= 1.0e-9:
                    # Deterministic tie: prefer the farther control, producing
                    # fewer visually redundant waypoints on open straight runs.
                    if current_best_next is None or candidate_position > current_best_next:
                        choose_candidate = True

            if choose_candidate:
                current_best_count = candidate_count
                current_best_deviation = candidate_deviation
                current_best_next = candidate_position

        if current_best_next is not None:
            best_edge_count[current_position] = current_best_count
            best_deviation_sum[current_position] = current_best_deviation
            next_position[current_position] = current_best_next

    if start_position not in next_position:
        return None

    result = [start_position]
    current_position = start_position
    guard = 0
    while current_position != end_position:
        current_position = next_position.get(current_position)
        if current_position is None:
            return None
        result.append(current_position)
        guard += 1
        if guard > (end_position - start_position + 2):
            return None
    return result


def choose_force_portal_positions(path_indices, force_portals):
    """Choose one ordered mandatory path position inside each selected portal."""
    if not force_portals:
        return []
    result = []
    minimum_position = 0
    for portal in force_portals:
        candidates = []
        exact_index = portal.get('waypoint_index')
        for position in range(minimum_position, len(path_indices)):
            index = path_indices[position]
            point_uv = grid_index_to_uv(index)
            if not portal_contains_point(
                portal,
                point_uv[0],
                point_uv[1],
                cell * 0.20
            ):
                continue
            centre_distance = math.sqrt(
                (point_uv[0] - portal['center_u']) ** 2 +
                (point_uv[1] - portal['center_v']) ** 2
            )
            candidates.append((
                0 if index == exact_index else 1,
                centre_distance,
                position
            ))
        if not candidates:
            return None
        candidates.sort()
        chosen_position = candidates[0][2]
        result.append(chosen_position)
        minimum_position = chosen_position + 1
    return result


def build_visibility_control_positions(path_indices, mandatory_positions):
    """Compress all mandatory spans and automatically repair close controls."""
    mandatory_positions = sorted(set(mandatory_positions or []))
    if not mandatory_positions:
        mandatory_positions = [0, len(path_indices) - 1]

    last_error = None
    for deviation_factor in PATH_CORRIDOR_DEVIATION_FACTORS:
        maximum_deviation = cell * deviation_factor
        combined = []
        failed = False
        for span_number in range(len(mandatory_positions) - 1):
            start_position = mandatory_positions[span_number]
            end_position = mandatory_positions[span_number + 1]
            span_positions = simplify_path_span_with_visibility(
                path_indices,
                start_position,
                end_position,
                maximum_deviation
            )
            if not span_positions:
                failed = True
                last_error = (
                    'No raster-safe native controls could be generated between '
                    'mandatory path positions {0} and {1} with deviation {2:.0f} mm.'
                ).format(
                    start_position,
                    end_position,
                    ft_to_mm(maximum_deviation)
                )
                break
            if combined:
                combined.extend(span_positions[1:])
            else:
                combined.extend(span_positions)

        if not failed:
            return combined, deviation_factor

    raise Exception(last_error or 'Unable to generate raster-safe native controls.')


def choose_force_point_positions(path_indices, point_records):
    """Return ordered path positions for mandatory picked-point grid cells."""
    if not point_records:
        return []
    result = []
    minimum_position = 0
    for point_record in point_records:
        target_index = point_record.get('waypoint_index')
        chosen_position = None
        for position in range(minimum_position, len(path_indices)):
            if path_indices[position] == target_index:
                chosen_position = position
                break
        if chosen_position is None:
            return None
        result.append(chosen_position)
        minimum_position = chosen_position + 1
    return result


def prepare_actual_route_control_points(path_indices, source):
    """Create a minimum-control representation of the Dijkstra parent chain.

    Long straight runs and grid staircases use only their two necessary ends.
    Real bends, route endpoints and the selected Force Door remain constrained.
    Consecutive native controls are always at least 500 mm apart.
    """
    cleaned = clean_route_path_indices(path_indices)
    if len(cleaned) < 2:
        raise Exception('The Dijkstra route contains fewer than two valid cells.')

    mandatory_positions = [0, len(cleaned) - 1]
    force_points = list(source.get('force_point_records_ordered') or []) if source else []
    point_positions = choose_force_point_positions(cleaned, force_points)
    if point_positions is None:
        raise Exception('One or more mandatory picked points have no ordered Dijkstra cell on the route.')
    mandatory_positions.extend(point_positions or [])

    mandatory_positions = sorted(set(mandatory_positions))
    point_position_set = set(point_positions or [])
    control_positions, deviation_factor = build_visibility_control_positions(
        cleaned,
        mandatory_positions
    )

    if len(control_positions) < 2:
        raise Exception('Waypoint compression removed too many native controls.')
    if len(control_positions) > PATH_GUIDE_MAX_CONTROLS:
        raise Exception(
            'The route still requires more than {0} controls after visibility '
            'compression.'.format(PATH_GUIDE_MAX_CONTROLS)
        )

    minimum_spacing = mm_to_ft(PATH_CONTROL_MIN_SPACING_MM)
    points_uv = []
    roles = []
    control_indices = []
    for position_number, path_position in enumerate(control_positions):
        index = cleaned[path_position]
        point_uv = grid_index_to_uv(index)
        if points_uv:
            spacing = uv_distance(points_uv[-1], point_uv)
            if spacing < minimum_spacing - 1.0e-9:
                raise Exception(
                    'Automatic close-control repair could not resolve positions '
                    '{0} and {1} ({2:.0f} mm).'.format(
                        position_number,
                        position_number + 1,
                        ft_to_mm(spacing)
                    )
                )

        if path_position == 0:
            role = 'start'
        elif path_position == len(cleaned) - 1:
            role = 'end'
        elif path_position in point_position_set:
            role = 'force_point'
        else:
            role = 'dijkstra_guide'

        points_uv.append(point_uv)
        roles.append(role)
        control_indices.append(index)

    for control_number in range(len(control_indices) - 1):
        if not raster_segment_is_walkable(
            control_indices[control_number],
            control_indices[control_number + 1]
        ):
            raise Exception(
                'A compressed native control chord leaves the Dijkstra walkable corridor.'
            )

    initial_turn_positions = build_straight_run_control_positions(
        cleaned,
        mandatory_positions
    )
    straight_run_removed_count = max(0, len(cleaned) - len(initial_turn_positions))
    total_removed_count = max(0, len(cleaned) - len(control_positions))
    local_removed_count = max(0, total_removed_count - straight_run_removed_count)

    # Stored for the report without altering the existing return signature.
    if source is not None:
        source['native_deviation_factor'] = deviation_factor
        source['force_point_control_positions'] = list(point_positions or [])

    return (
        points_uv,
        roles,
        control_indices,
        total_removed_count,
        straight_run_removed_count,
        local_removed_count
    )


route_records = []
for cabinet_index, cabinet_record in enumerate(cabinet_records):
    baseline_farthest_index = farthest_index_by_cabinet[cabinet_index]
    forced_override = forced_route_override_by_cabinet.get(cabinet_index)
    if cabinet_record.get('seed_index') is None:
        continue
    if forced_override is None and baseline_farthest_index is None:
        cabinet_record['route_status'] = 'No exclusively owned covered cell'
        continue

    try:
        source = source_by_cabinet.get(cabinet_index)
        if forced_override is not None:
            combined_path_indices = list(forced_override['path_indices'])
            route_distance = forced_override['distance']
            farthest_index = forced_override['end_index']
            force_points = list(forced_override.get('points') or [])
            force_portals = []
            force_far_side_signs = []
            route_is_forced = True
        else:
            farthest_index = baseline_farthest_index
            combined_path_indices = reconstruct_path_indices(
                farthest_index,
                cabinet_index
            )
            route_distance = distances[farthest_index]
            force_points = []
            force_portals = []
            force_far_side_signs = []
            route_is_forced = False

        (
            native_points_uv,
            native_roles,
            native_control_indices,
            reduced_control_count,
            straight_run_removed_count,
            local_removed_count
        ) = prepare_actual_route_control_points(
            combined_path_indices,
            source
        )
        end_uv = grid_index_to_uv(combined_path_indices[-1])

        cabinet_record['route_status'] = (
            'Prepared ({0} mandatory picked points)'.format(len(force_points))
            if route_is_forced else
            'Prepared'
        )
        cabinet_record['route_distance'] = route_distance
        cabinet_record['path_grid_steps'] = max(0, len(combined_path_indices) - 1)
        cabinet_record['route_segment_count'] = 0
        cabinet_record['endpoint_u'] = end_uv[0]
        cabinet_record['endpoint_v'] = end_uv[1]
        route_records.append({
            'cabinet_index': cabinet_index,
            'cabinet_record': cabinet_record,
            'farthest_index': farthest_index,
            'path_indices': combined_path_indices,
            'points_uv': native_points_uv,
            'control_roles': native_roles,
            'control_indices': native_control_indices,
            'reduced_control_count': reduced_control_count,
            'straight_run_removed_count': straight_run_removed_count,
            'local_removed_count': local_removed_count,
            'native_control_count': len(native_points_uv),
            'force_points': force_points,
            'force_portals': force_portals,
            'force_far_side_signs': force_far_side_signs,
            'force_portal': force_portals[0] if force_portals else None,
            'force_far_side_sign': force_far_side_signs[0] if force_far_side_signs else None,
        })
    except Exception as error:
        cabinet_record['route_status'] = 'Failed to prepare native route: {0}'.format(error)


if run_mode == RUN_MODE_FORCE_DOOR_APPEND:
    route_records_to_create = [
        route_record for route_record in route_records
        if route_record['cabinet_index'] in forced_cabinet_indices
    ]
    for route_record in route_records:
        if route_record['cabinet_index'] not in forced_cabinet_indices:
            route_record['cabinet_record']['route_status'] = 'Preserved existing result'
    if not route_records_to_create:
        failure_lines = []
        for cabinet_index in sorted(forced_cabinet_indices):
            cabinet_record = cabinet_records[cabinet_index]
            try:
                cabinet_id_text = str(eid_int(cabinet_record['element'].Id))
            except Exception:
                cabinet_id_text = '-'
            failure_lines.append(
                'Cabinet {0} [Id {1}]: {2}'.format(
                    cabinet_record.get('diagnostic_number') or cabinet_index + 1,
                    cabinet_id_text,
                    cabinet_record.get('route_status') or 'No prepared route'
                )
            )
        detail_text = '\n'.join(failure_lines[:8])
        action_text = (
            'No additional picked-point route was added.'
            if run_mode == RUN_MODE_FORCE_DOOR_APPEND
            else 'No existing route was changed.'
        )
        forms.alert(
            'The cabinets assigned mandatory picked points did not produce a route through every selected point inside their normal coverage territory.\n\n'
            '{0}\n\n{1}'.format(detail_text, action_text),
            exitscript=True
        )
else:
    route_records_to_create = list(route_records)


# =============================================================================
# EXTENSIBLE STORAGE, PATH OF TRAVEL AND MARKER CREATION
# =============================================================================
def get_route_result_schema():
    existing_schema = Schema.Lookup(ROUTE_RESULT_SCHEMA_GUID)
    if existing_schema is not None:
        if existing_schema.GetField(ROUTE_RESULT_FIELD_NAME) is None:
            raise Exception(
                'Extensible Storage schema GUID conflict: {0}'.format(
                    ROUTE_RESULT_SCHEMA_GUID
                )
            )
        return existing_schema

    builder = SchemaBuilder(ROUTE_RESULT_SCHEMA_GUID)
    builder.SetSchemaName(ROUTE_RESULT_SCHEMA_NAME)
    builder.SetDocumentation(
        'Identifies PathOfTravel and diagnostic DetailCurve results created by the pyRevit fire cabinet coverage route tool.'
    )
    builder.SetReadAccessLevel(AccessLevel.Public)
    builder.SetWriteAccessLevel(AccessLevel.Public)
    builder.AddSimpleField(ROUTE_RESULT_FIELD_NAME, String)
    return builder.Finish()


try:
    route_result_schema = get_route_result_schema()
    route_result_field = route_result_schema.GetField(ROUTE_RESULT_FIELD_NAME)
except Exception:
    forms.alert(
        'Cannot prepare Extensible Storage for route results.\n\n{0}'.format(
            traceback.format_exc()
        ),
        exitscript=True
    )


def element_has_route_result_entity(element):
    try:
        entity = element.GetEntity(route_result_schema)
        return entity is not None and entity.IsValid()
    except Exception:
        return False


def get_route_result_payload(element):
    try:
        entity = element.GetEntity(route_result_schema)
        if entity is None or not entity.IsValid():
            return u''
        return to_text(entity.Get[String](route_result_field))
    except Exception:
        return u''


def parse_route_result_payload(payload):
    values = {}
    for token in to_text(payload).split(u';'):
        token = token.strip()
        if not token or u'=' not in token:
            continue
        key, value = token.split(u'=', 1)
        values[key.strip().lower()] = value.strip()
    return values


def safe_int_value(value):
    try:
        return int(to_text(value).strip())
    except Exception:
        return None


def forced_cabinet_index_from_payload(payload):
    data = parse_route_result_payload(payload)
    role = data.get('role', '').lower()
    # Never treat uncovered-cluster markers as cabinet-owned results.
    if role not in ('route', 'force_point_append_route', 'cabinet_marker', 'endpoint_marker', 'force_point_append_endpoint_marker', 'distance_checkpoint_35m_marker', 'force_point_append_35m_marker'):
        return None

    stable_id = safe_int_value(data.get('cabinet_id'))
    if stable_id is None:
        stable_id = safe_int_value(data.get('owner_id'))
    if stable_id is not None:
        for cabinet_index in forced_cabinet_indices:
            try:
                if eid_int(cabinet_records[cabinet_index]['element'].Id) == stable_id:
                    return cabinet_index
            except Exception:
                continue

    legacy_number = safe_int_value(data.get('cabinet'))
    if legacy_number is None:
        legacy_number = safe_int_value(data.get('owner'))
    if legacy_number is not None:
        for cabinet_index in forced_cabinet_indices:
            if int(cabinet_records[cabinet_index].get('diagnostic_number') or 0) == legacy_number:
                return cabinet_index
    return None


def attach_route_result_entity(element, payload):
    entity = Entity(ROUTE_RESULT_SCHEMA_GUID)
    entity.Set[String](route_result_field, to_text(payload))
    element.SetEntity(entity)

    # Human-readable fallback only; Extensible Storage remains authoritative.
    try:
        parameter = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if parameter is not None and not parameter.IsReadOnly:
            parameter.Set('{0}; {1}'.format(ROUTE_COMMENT_PREFIX, payload))
    except Exception:
        pass


def apply_sample_path_line_style(path_element):
    # Kept as a compatibility function name; the style now comes directly
    # from the UI selection rather than from a picked sample path.
    try:
        path_element.LineStyle = sample_path_line_style_id
    except Exception as error:
        raise Exception('Cannot apply the selected Path of Travel LineStyle: {0}'.format(error))


def create_native_path_of_travel(point_a, point_b, payload):
    minimum_spacing = mm_to_ft(PATH_CONTROL_MIN_SPACING_MM)
    if point_a.DistanceTo(point_b) < minimum_spacing:
        raise Exception(
            'Path of Travel points are only {0:.0f} mm apart; minimum used by this tool is {1:.0f} mm.'.format(
                ft_to_mm(point_a.DistanceTo(point_b)),
                PATH_CONTROL_MIN_SPACING_MM
            )
        )
    path_element = PathOfTravel.Create(view, point_a, point_b)
    if path_element is None:
        raise Exception('Revit did not find a native Path of Travel between two points.')
    apply_sample_path_line_style(path_element)
    attach_route_result_entity(path_element, payload)
    return path_element


def get_path_curves(path_element):
    try:
        return list(path_element.GetCurves() or [])
    except Exception:
        return []


def count_path_curves(path_element):
    return len(get_path_curves(path_element))


def update_path_elements(path_ids):
    valid_ids = []
    for path_id in path_ids:
        try:
            path_element = doc.GetElement(path_id)
            if not isinstance(path_element, PathOfTravel):
                continue
            valid_ids.append(path_id)
            try:
                path_element.Update()
            except Exception:
                pass
        except Exception:
            continue
    if valid_ids:
        try:
            PathOfTravel.UpdateMultiple(doc, make_id_list(valid_ids))
        except Exception:
            pass
    return valid_ids


def path_polyline_points(path_ids):
    """Return a densely sampled, ordered native Path of Travel polyline."""
    ordered = []
    previous = None
    target_step = mm_to_ft(FORCE_NATIVE_SAMPLE_STEP_MM)
    for path_id in path_ids:
        path_element = doc.GetElement(path_id)
        if not isinstance(path_element, PathOfTravel):
            continue
        curves = get_path_curves(path_element)
        for curve in curves:
            try:
                curve_length = float(curve.Length)
                sample_count = max(1, int(math.ceil(curve_length / target_step)))
                points = [
                    curve.Evaluate(sample_number / float(sample_count), True)
                    for sample_number in range(sample_count + 1)
                ]
            except Exception:
                try:
                    points = list(curve.Tessellate())
                except Exception:
                    try:
                        points = [curve.GetEndPoint(0), curve.GetEndPoint(1)]
                    except Exception:
                        points = []
            if len(points) < 2:
                continue
            if previous is not None:
                try:
                    if previous.DistanceTo(points[-1]) < previous.DistanceTo(points[0]):
                        points.reverse()
                except Exception:
                    pass
            if ordered and ordered[-1].DistanceTo(points[0]) <= mm_to_ft(1.0):
                points = points[1:]
            ordered.extend(points)
            if ordered:
                previous = ordered[-1]
    return ordered


def point_at_distance_along_path(path_ids, target_distance):
    """Return (XYZ at target distance, total native route length).

    The point is measured along the actual native Path of Travel geometry, not
    along the Dijkstra estimate. For chained PathOfTravel results, path_ids are
    already stored in route order and path_polyline_points joins them in that
    same order.
    """
    points = path_polyline_points(path_ids)
    if len(points) < 2:
        return None, 0.0

    segment_lengths = []
    total_length = 0.0
    for index in range(len(points) - 1):
        try:
            segment_length = points[index].DistanceTo(points[index + 1])
        except Exception:
            segment_length = 0.0
        segment_lengths.append(segment_length)
        total_length += segment_length

    if total_length <= target_distance + mm_to_ft(1.0):
        return None, total_length

    travelled = 0.0
    for index, segment_length in enumerate(segment_lengths):
        if segment_length <= 1.0e-12:
            continue
        if travelled + segment_length >= target_distance:
            factor = clamp(
                (target_distance - travelled) / segment_length,
                0.0,
                1.0
            )
            point_a = points[index]
            point_b = points[index + 1]
            point = DB.XYZ(
                point_a.X + (point_b.X - point_a.X) * factor,
                point_a.Y + (point_b.Y - point_a.Y) * factor,
                point_a.Z + (point_b.Z - point_a.Z) * factor
            )
            return point, total_length
        travelled += segment_length

    return None, total_length


def analyze_force_portal_crossing(path_ids, portal, far_side_sign):
    """Return detailed evidence about whether native geometry crossed the door.

    Signed normal values are oriented so the cabinet side is negative and the
    required far side is positive. The diagnostic is intentionally verbose so
    a failed Force Door run can distinguish a linked-wall routing limitation
    from an incorrect portal width, normal, or local-span test.
    """
    diagnostics = {
        'passed': False,
        'failure_code': 'unknown',
        'failure_reason': 'Force Door crossing could not be evaluated.',
        'door_id': portal.get('insert_id') if portal else None,
        'source_key': portal.get('source_key') if portal else None,
        'wall_id': portal.get('wall_id') if portal else None,
        'path_element_count': len(path_ids or []),
        'path_point_count': 0,
        'path_length_m': 0.0,
        'physical_width_mm': ft_to_mm(portal.get('physical_width', 0.0)) if portal else 0.0,
        'allowed_half_width_mm': 0.0,
        'crossing_tolerance_mm': FORCE_DOOR_CROSSING_TOLERANCE_MM,
        'maximum_local_span_mm': FORCE_DOOR_CROSSING_MAX_SPAN_MM,
        'portal_center_u_mm': ft_to_mm(portal.get('center_u', 0.0)) if portal else 0.0,
        'portal_center_v_mm': ft_to_mm(portal.get('center_v', 0.0)) if portal else 0.0,
        'portal_normal_u': portal.get('normal_u', 0.0) if portal else 0.0,
        'portal_normal_v': portal.get('normal_v', 0.0) if portal else 0.0,
        'portal_tangent_u': portal.get('tangent_u', 0.0) if portal else 0.0,
        'portal_tangent_v': portal.get('tangent_v', 0.0) if portal else 0.0,
        'far_side_sign': far_side_sign,
        'minimum_signed_normal_mm': None,
        'maximum_signed_normal_mm': None,
        'closest_plane_distance_mm': None,
        'plane_crossings': 0,
        'crossings_inside_door_width': 0,
        'crossings_with_both_sides': 0,
        'valid_crossings': 0,
        'nearest_crossing_offset_mm': None,
        'nearest_crossing_cabinet_span_mm': None,
        'nearest_crossing_far_span_mm': None,
    }

    if portal is None or far_side_sign is None:
        diagnostics['passed'] = True
        diagnostics['failure_code'] = 'not_required'
        diagnostics['failure_reason'] = 'No Force Door validation was required.'
        return diagnostics

    points = path_polyline_points(path_ids)
    diagnostics['path_point_count'] = len(points)
    if len(points) < 2:
        diagnostics['failure_code'] = 'no_native_geometry'
        diagnostics['failure_reason'] = (
            'The native Path of Travel returned fewer than two route points.'
        )
        return diagnostics

    far_side_sign = 1.0 if far_side_sign >= 0.0 else -1.0
    diagnostics['far_side_sign'] = far_side_sign
    tolerance = mm_to_ft(FORCE_DOOR_CROSSING_TOLERANCE_MM)
    maximum_local_span = max(
        mm_to_ft(FORCE_DOOR_CROSSING_MAX_SPAN_MM),
        portal['half_depth'] * 2.0 + mm_to_ft(1000.0)
    )
    allowed_half_width = max(
        portal.get('physical_width', 0.0) * 0.5 + tolerance,
        cell * 0.75
    )
    diagnostics['allowed_half_width_mm'] = ft_to_mm(allowed_half_width)
    diagnostics['maximum_local_span_mm'] = ft_to_mm(maximum_local_span)

    uv_points = []
    cumulative = [0.0]
    for point in points:
        u_value, v_value = xyz_to_uv(point)
        du = u_value - portal['center_u']
        dv = v_value - portal['center_v']
        normal_value = (
            du * portal['normal_u'] + dv * portal['normal_v']
        ) * far_side_sign
        along_value = du * portal['tangent_u'] + dv * portal['tangent_v']
        uv_points.append((u_value, v_value, normal_value, along_value))
        if len(uv_points) > 1:
            cumulative.append(
                cumulative[-1] + math.sqrt(
                    (uv_points[-1][0] - uv_points[-2][0]) ** 2 +
                    (uv_points[-1][1] - uv_points[-2][1]) ** 2
                )
            )

    diagnostics['path_length_m'] = cumulative[-1] * 0.3048
    normal_values = [item[2] for item in uv_points]
    diagnostics['minimum_signed_normal_mm'] = ft_to_mm(min(normal_values))
    diagnostics['maximum_signed_normal_mm'] = ft_to_mm(max(normal_values))
    diagnostics['closest_plane_distance_mm'] = ft_to_mm(
        min([abs(value) for value in normal_values])
    )

    crossing_records = []
    for index in range(len(uv_points) - 1):
        point_a = uv_points[index]
        point_b = uv_points[index + 1]
        normal_a = point_a[2]
        normal_b = point_b[2]

        # Ignore segments that remain clearly on only one side of the wall.
        if normal_a > tolerance and normal_b > tolerance:
            continue
        if normal_a < -tolerance and normal_b < -tolerance:
            continue

        denominator = normal_b - normal_a
        if abs(denominator) < 1.0e-12:
            factor = 0.5
        else:
            factor = clamp(-normal_a / denominator, 0.0, 1.0)

        along_at_crossing = point_a[3] + (
            point_b[3] - point_a[3]
        ) * factor
        crossing_distance = cumulative[index] + (
            cumulative[index + 1] - cumulative[index]
        ) * factor

        nearest_cabinet_side = None
        for previous_index in range(index, -1, -1):
            if uv_points[previous_index][2] <= -tolerance:
                nearest_cabinet_side = cumulative[previous_index]
                break

        nearest_far_side = None
        for following_index in range(index + 1, len(uv_points)):
            if uv_points[following_index][2] >= tolerance:
                nearest_far_side = cumulative[following_index]
                break

        cabinet_span = None
        far_span = None
        if nearest_cabinet_side is not None:
            cabinet_span = crossing_distance - nearest_cabinet_side
        if nearest_far_side is not None:
            far_span = nearest_far_side - crossing_distance

        record = {
            'segment_index': index,
            'along_offset': along_at_crossing,
            'inside_width': abs(along_at_crossing) <= allowed_half_width,
            'has_cabinet_side': nearest_cabinet_side is not None,
            'has_far_side': nearest_far_side is not None,
            'cabinet_span': cabinet_span,
            'far_span': far_span,
            'local_span_ok': (
                cabinet_span is not None and far_span is not None and
                cabinet_span <= maximum_local_span and
                far_span <= maximum_local_span
            ),
        }
        crossing_records.append(record)

    diagnostics['plane_crossings'] = len(crossing_records)
    diagnostics['crossings_inside_door_width'] = len([
        item for item in crossing_records if item['inside_width']
    ])
    diagnostics['crossings_with_both_sides'] = len([
        item for item in crossing_records
        if item['has_cabinet_side'] and item['has_far_side']
    ])
    diagnostics['valid_crossings'] = len([
        item for item in crossing_records
        if item['inside_width'] and item['has_cabinet_side'] and
        item['has_far_side'] and item['local_span_ok']
    ])

    if crossing_records:
        nearest = min(crossing_records, key=lambda item: abs(item['along_offset']))
        diagnostics['nearest_crossing_offset_mm'] = ft_to_mm(
            nearest['along_offset']
        )
        if nearest['cabinet_span'] is not None:
            diagnostics['nearest_crossing_cabinet_span_mm'] = ft_to_mm(
                nearest['cabinet_span']
            )
        if nearest['far_span'] is not None:
            diagnostics['nearest_crossing_far_span_mm'] = ft_to_mm(
                nearest['far_span']
            )

    if diagnostics['valid_crossings'] > 0:
        diagnostics['passed'] = True
        diagnostics['failure_code'] = 'passed'
        diagnostics['failure_reason'] = (
            'The native Path of Travel crossed the selected doorway.'
        )
        return diagnostics

    minimum_normal = min(normal_values)
    maximum_normal = max(normal_values)
    if maximum_normal < tolerance:
        diagnostics['failure_code'] = 'remained_on_cabinet_side'
        diagnostics['failure_reason'] = (
            'The route remained on the cabinet side of the selected linked wall. '
            'Revit Route Analysis may be treating the linked wall at this door as '
            'a continuous obstacle.'
        )
    elif minimum_normal > -tolerance:
        diagnostics['failure_code'] = 'missing_cabinet_side'
        diagnostics['failure_reason'] = (
            'The route did not contain a clear point on the cabinet side of the '
            'selected doorway. The portal normal or cabinet-side sign may be wrong.'
        )
    elif not crossing_records:
        diagnostics['failure_code'] = 'no_wall_plane_crossing'
        diagnostics['failure_reason'] = (
            'The route reached both signed sides numerically, but no continuous '
            'native segment crossed the selected wall centre plane.'
        )
    elif diagnostics['crossings_inside_door_width'] == 0:
        diagnostics['failure_code'] = 'crossed_outside_door_width'
        diagnostics['failure_reason'] = (
            'The route crossed the selected wall centre plane outside the allowed '
            'width of the chosen doorway.'
        )
    elif diagnostics['crossings_with_both_sides'] == 0:
        diagnostics['failure_code'] = 'touched_without_transition'
        diagnostics['failure_reason'] = (
            'The route touched or crossed near the doorway centre plane but did not '
            'establish a clear cabinet-side to far-side transition.'
        )
    else:
        diagnostics['failure_code'] = 'local_transition_too_long'
        diagnostics['failure_reason'] = (
            'A crossing was found near the selected doorway, but the local approach '
            'or departure span was too long, indicating that the route probably '
            'detoured through another opening.'
        )
    return diagnostics


def path_crosses_force_portal(path_ids, portal, far_side_sign):
    return analyze_force_portal_crossing(
        path_ids,
        portal,
        far_side_sign
    ).get('passed', False)


def format_force_portal_diagnostics(diagnostics, compact=False):
    if not diagnostics:
        return 'No Force Door crossing diagnostic was recorded.'

    def number_text(name, digits=0, suffix=''):
        value = diagnostics.get(name)
        if value is None:
            return '-'
        try:
            pattern = '{0:.' + str(digits) + 'f}'
            return pattern.format(float(value)) + suffix
        except Exception:
            return to_text(value) + suffix

    if compact:
        return (
            '{0} Door {1}, source {2}; path {3:.2f} m; normal range {4} to {5}; '
            'plane crossings {6}, inside width {7}; nearest offset {8} '
            '(allowed +/-{9}).'
        ).format(
            diagnostics.get('failure_reason') or '-',
            diagnostics.get('door_id') or '-',
            diagnostics.get('source_key') or '-',
            float(diagnostics.get('path_length_m') or 0.0),
            number_text('minimum_signed_normal_mm', 0, ' mm'),
            number_text('maximum_signed_normal_mm', 0, ' mm'),
            diagnostics.get('plane_crossings') or 0,
            diagnostics.get('crossings_inside_door_width') or 0,
            number_text('nearest_crossing_offset_mm', 0, ' mm'),
            number_text('allowed_half_width_mm', 0, ' mm')
        )

    lines = [
        'Failure classification: {0}'.format(
            diagnostics.get('failure_code') or 'unknown'
        ),
        'Reason: {0}'.format(diagnostics.get('failure_reason') or '-'),
        'Door: {0} | Source: {1} | Wall: {2}'.format(
            diagnostics.get('door_id') or '-',
            diagnostics.get('source_key') or '-',
            diagnostics.get('wall_id') or '-'
        ),
        'Door physical width: {0} | Allowed crossing half-width: +/-{1}'.format(
            number_text('physical_width_mm', 0, ' mm'),
            number_text('allowed_half_width_mm', 0, ' mm')
        ),
        'Portal centre U/V: {0} / {1}'.format(
            number_text('portal_center_u_mm', 0, ' mm'),
            number_text('portal_center_v_mm', 0, ' mm')
        ),
        'Portal normal U/V: {0:.4f} / {1:.4f} | Far-side sign: {2}'.format(
            float(diagnostics.get('portal_normal_u') or 0.0),
            float(diagnostics.get('portal_normal_v') or 0.0),
            diagnostics.get('far_side_sign')
        ),
        'Native path: {0} element(s), {1} sampled point(s), {2:.2f} m'.format(
            diagnostics.get('path_element_count') or 0,
            diagnostics.get('path_point_count') or 0,
            float(diagnostics.get('path_length_m') or 0.0)
        ),
        'Signed normal range: {0} to {1} | Closest to wall plane: {2}'.format(
            number_text('minimum_signed_normal_mm', 0, ' mm'),
            number_text('maximum_signed_normal_mm', 0, ' mm'),
            number_text('closest_plane_distance_mm', 0, ' mm')
        ),
        'Crossings: {0} wall-plane, {1} inside selected door width, {2} with both sides, {3} valid'.format(
            diagnostics.get('plane_crossings') or 0,
            diagnostics.get('crossings_inside_door_width') or 0,
            diagnostics.get('crossings_with_both_sides') or 0,
            diagnostics.get('valid_crossings') or 0
        ),
        'Nearest crossing: offset {0}; cabinet-side local span {1}; far-side local span {2}; maximum local span {3}'.format(
            number_text('nearest_crossing_offset_mm', 0, ' mm'),
            number_text('nearest_crossing_cabinet_span_mm', 0, ' mm'),
            number_text('nearest_crossing_far_span_mm', 0, ' mm'),
            number_text('maximum_local_span_mm', 0, ' mm')
        ),
    ]
    return '\n'.join(lines)


def native_path_stays_in_walkable_raster(path_ids):
    """Reject a native result that shortcuts through the custom obstacle grid."""
    points = path_polyline_points(path_ids)
    if len(points) < 2:
        return False

    sample_step = max(mm_to_ft(25.0), cell * PATH_GUIDE_SAMPLE_STEP_FACTOR)
    for point_index in range(len(points) - 1):
        point_a = points[point_index]
        point_b = points[point_index + 1]
        distance = point_a.DistanceTo(point_b)
        sample_count = max(1, int(math.ceil(distance / sample_step)))
        previous_i = None
        previous_j = None
        for sample_number in range(sample_count + 1):
            factor = sample_number / float(sample_count)
            point = point_a + (point_b - point_a).Multiply(factor)
            u_value, v_value = xyz_to_uv(point)
            i = int(math.floor((u_value - min_u) / cell))
            j = int(math.floor((v_value - min_v) / cell))
            if i < 0 or i >= nu or j < 0 or j >= nv:
                return False
            index = j * nu + i
            if not walkable[index]:
                return False
            if previous_i is not None and i != previous_i and j != previous_j:
                side_a = previous_j * nu + i
                side_b = j * nu + previous_i
                if not walkable[side_a] or not walkable[side_b]:
                    return False
            previous_i = i
            previous_j = j
    return True


def validate_created_path_ids(path_ids, force_portals=None, force_far_side_signs=None):
    doc.Regenerate()
    update_path_elements(path_ids)
    doc.Regenerate()

    total_curves = 0
    for path_id in path_ids:
        path_element = doc.GetElement(path_id)
        if not isinstance(path_element, PathOfTravel):
            raise Exception('Created route element is no longer a Path of Travel.')
        curves = get_path_curves(path_element)
        if not curves:
            raise Exception('Revit created endpoint dots but no Path of Travel curve.')
        total_curves += len(curves)

    if not native_path_stays_in_walkable_raster(path_ids):
        raise Exception(
            'Native Path of Travel left the Dijkstra walkable corridor or cut through an obstacle.'
        )

    portals = list(force_portals or [])
    signs = list(force_far_side_signs or [])
    for portal_index, portal in enumerate(portals):
        far_side_sign = signs[portal_index] if portal_index < len(signs) else None
        crossing_diagnostics = analyze_force_portal_crossing(
            path_ids,
            portal,
            far_side_sign
        )
        crossing_diagnostics['door_sequence_number'] = portal_index + 1
        crossing_diagnostics['door_sequence_total'] = len(portals)
        if not crossing_diagnostics.get('passed'):
            raise ForceDoorCrossingError(
                'Native Path of Travel did not cross Force Door {0} of {1}. {2}'.format(
                    portal_index + 1,
                    len(portals),
                    crossing_diagnostics.get('failure_reason') or ''
                ),
                crossing_diagnostics
            )
    return total_curves


def create_route_path_of_travel(
    points_uv,
    cabinet_number,
    cabinet_element_id,
    distance_m,
    force_portals=None,
    force_far_side_signs=None,
    result_role='route',
    result_variant='base'
):
    """Create a native route through raster-safe Dijkstra guide controls.

    Each neighbouring control chord has already been proved walkable against
    the custom host/link obstacle raster. Revit may refine the short local
    segment, but it cannot replace the complete route with one bird-flight line.
    """
    if len(points_uv) < 2:
        raise Exception('At least two Path of Travel control points are required.')

    force_portals = list(force_portals or [])
    force_far_side_signs = list(force_far_side_signs or [])
    force_door_ids = ','.join([to_text(portal.get('insert_id')) for portal in force_portals])

    points_xyz = [uv_to_xyz(point[0], point[1]) for point in points_uv]
    minimum_spacing = mm_to_ft(PATH_CONTROL_MIN_SPACING_MM)
    for index in range(len(points_xyz) - 1):
        spacing = points_xyz[index].DistanceTo(points_xyz[index + 1])
        if spacing < minimum_spacing:
            raise Exception(
                'Native control points {0} and {1} are too close ({2:.0f} mm).'.format(
                    index + 1,
                    index + 2,
                    ft_to_mm(spacing)
                )
            )

    attempt = DB.SubTransaction(doc)
    attempt.Start()
    try:
        path_element = create_native_path_of_travel(
            points_xyz[0],
            points_xyz[-1],
            'role={0}; cabinet={1}; cabinet_id={2}; distance_m={3:.3f}; mode=waypoints; variant={4}; door_id={5}; compartment_id={6}'.format(
                result_role,
                cabinet_number,
                cabinet_element_id,
                distance_m,
                result_variant,
                force_door_ids,
                selected_compartment_id
            )
        )
        for waypoint_index, waypoint in enumerate(points_xyz[1:-1]):
            path_element.InsertWaypoint(waypoint, waypoint_index)

        apply_sample_path_line_style(path_element)
        curve_count = validate_created_path_ids(
            [path_element.Id],
            force_portals,
            force_far_side_signs
        )
        status = attempt.Commit()
        if status != DB.TransactionStatus.Committed:
            raise Exception('Waypoint PathOfTravel SubTransaction was not committed.')
        return [path_element.Id], curve_count, 'Validated Dijkstra-guided waypoints'
    except Exception as waypoint_error:
        waypoint_error_text = to_text(waypoint_error)
        waypoint_crossing_diagnostics = getattr(
            waypoint_error,
            'diagnostics',
            None
        )
        try:
            if attempt.GetStatus() == DB.TransactionStatus.Started:
                attempt.RollBack()
        except Exception:
            pass

        # Native fallback: one PathOfTravel between each raster-safe guide pair.
        # Every pair remains inside the same Dijkstra walkable corridor.
        path_ids = []
        for segment_index in range(len(points_xyz) - 1):
            point_a = points_xyz[segment_index]
            point_b = points_xyz[segment_index + 1]
            if point_a.DistanceTo(point_b) < minimum_spacing:
                continue
            path_element = create_native_path_of_travel(
                point_a,
                point_b,
                'role={0}; cabinet={1}; cabinet_id={2}; segment={3}; distance_m={4:.3f}; mode=chained; variant={5}; door_id={6}; compartment_id={7}'.format(
                    result_role,
                    cabinet_number,
                    cabinet_element_id,
                    segment_index,
                    distance_m,
                    result_variant,
                    force_door_ids,
                    selected_compartment_id
                )
            )
            path_ids.append(path_element.Id)

        if not path_ids:
            raise Exception(
                'Path of Travel creation failed. Waypoint error: {0}'.format(
                    waypoint_error_text
                )
            )
        try:
            curve_count = validate_created_path_ids(
                path_ids,
                force_portals,
                force_far_side_signs
            )
        except ForceDoorCrossingError as chained_crossing_error:
            diagnostics = dict(chained_crossing_error.diagnostics or {})
            if waypoint_crossing_diagnostics:
                diagnostics['waypoint_attempt_failure_code'] = (
                    waypoint_crossing_diagnostics.get('failure_code')
                )
                diagnostics['waypoint_attempt_failure_reason'] = (
                    waypoint_crossing_diagnostics.get('failure_reason')
                )
            raise ForceDoorCrossingError(
                to_text(chained_crossing_error),
                diagnostics
            )
        return path_ids, curve_count, 'Validated chained Dijkstra-guided paths'



def set_route_failure_preprocessor(transaction_object):
    try:
        options = transaction_object.GetFailureHandlingOptions()
        options.SetFailuresPreprocessor(RouteFailurePreprocessor())
        options.SetClearAfterRollback(True)
        transaction_object.SetFailureHandlingOptions(options)
    except Exception:
        pass


def force_path_graphics_refresh(path_ids):
    """Pulse each path by a tiny move, then restore it exactly.

    Revit 2025 can calculate GetCurves() correctly while initially showing only
    endpoint/waypoint dots. A manual Move makes the line appear. This reproduces
    that graphics invalidation without adding another close waypoint.
    """
    result = {
        'requested': len(path_ids),
        'updated': 0,
        'pulsed': 0,
        'failed': 0,
        'message': 'Not required',
    }
    if not path_ids:
        return result

    valid_ids = []
    for path_id in path_ids:
        try:
            if isinstance(doc.GetElement(path_id), PathOfTravel):
                valid_ids.append(path_id)
        except Exception:
            continue
    if not valid_ids:
        return result

    tiny_shift = view.RightDirection.Normalize().Multiply(mm_to_ft(0.5))
    pulse_transaction = DB.Transaction(doc, 'Refresh Path of Travel graphics - pulse')
    try:
        status = pulse_transaction.Start()
        if status != DB.TransactionStatus.Started:
            raise Exception('Cannot start Path graphics pulse Transaction.')
        set_route_failure_preprocessor(pulse_transaction)
        for path_id in valid_ids:
            try:
                DB.ElementTransformUtils.MoveElement(doc, path_id, tiny_shift)
                result['pulsed'] += 1
            except Exception:
                result['failed'] += 1
        doc.Regenerate()
        result['updated'] = len(update_path_elements(valid_ids))
        doc.Regenerate()
        commit_status = pulse_transaction.Commit()
        if commit_status != DB.TransactionStatus.Committed:
            raise Exception('Path graphics pulse Transaction was not committed.')
    except Exception as error:
        try:
            if pulse_transaction.GetStatus() == DB.TransactionStatus.Started:
                pulse_transaction.RollBack()
        except Exception:
            pass
        result['message'] = 'Pulse failed: {0}'.format(to_text(error))
        return result

    restore_transaction = DB.Transaction(doc, 'Refresh Path of Travel graphics - restore')
    try:
        status = restore_transaction.Start()
        if status != DB.TransactionStatus.Started:
            raise Exception('Cannot start Path graphics restore Transaction.')
        set_route_failure_preprocessor(restore_transaction)
        reverse_shift = tiny_shift.Multiply(-1.0)
        for path_id in valid_ids:
            try:
                DB.ElementTransformUtils.MoveElement(doc, path_id, reverse_shift)
                path_element = doc.GetElement(path_id)
                if isinstance(path_element, PathOfTravel):
                    apply_sample_path_line_style(path_element)
            except Exception:
                result['failed'] += 1
        doc.Regenerate()
        update_path_elements(valid_ids)
        doc.Regenerate()
        commit_status = restore_transaction.Commit()
        if commit_status != DB.TransactionStatus.Committed:
            raise Exception('Path graphics restore Transaction was not committed.')
        result['message'] = 'Updated, moved 0.5 mm and restored'
    except Exception as error:
        try:
            if restore_transaction.GetStatus() == DB.TransactionStatus.Started:
                restore_transaction.RollBack()
        except Exception:
            pass
        result['message'] = 'Restore failed: {0}'.format(to_text(error))

    try:
        uidoc.RefreshActiveView()
    except Exception:
        pass
    return result


def apply_selected_line_style(detail_curve):
    try:
        detail_curve.LineStyle = selected_line_style
    except Exception as error:
        raise Exception('Cannot apply selected Detail Line style: {0}'.format(error))


def create_tagged_detail_line(point_a, point_b, payload):
    if point_a.DistanceTo(point_b) <= doc.Application.ShortCurveTolerance * 1.05:
        return None
    model_line = DB.Line.CreateBound(point_a, point_b)
    detail_curve = doc.Create.NewDetailCurve(view, model_line)
    apply_selected_line_style(detail_curve)
    attach_route_result_entity(detail_curve, payload)
    return detail_curve


def marker_uv_segments(center_u, center_v, half_size, marker_shape):
    if marker_shape == 'plus':
        return [
            ((center_u - half_size, center_v), (center_u + half_size, center_v)),
            ((center_u, center_v - half_size), (center_u, center_v + half_size)),
        ]
    if marker_shape == 'diamond':
        return [
            ((center_u, center_v + half_size), (center_u + half_size, center_v)),
            ((center_u + half_size, center_v), (center_u, center_v - half_size)),
            ((center_u, center_v - half_size), (center_u - half_size, center_v)),
            ((center_u - half_size, center_v), (center_u, center_v + half_size)),
        ]
    return [
        ((center_u - half_size, center_v - half_size), (center_u + half_size, center_v + half_size)),
        ((center_u - half_size, center_v + half_size), (center_u + half_size, center_v - half_size)),
    ]


def create_marker_lines(
    center_u,
    center_v,
    half_size,
    marker_shape,
    role,
    owner_number,
    owner_element_id=None,
    extra_payload=u''
):
    ids = []
    try:
        for segment_number, segment in enumerate(marker_uv_segments(
            center_u,
            center_v,
            half_size,
            marker_shape
        )):
            point_a = uv_to_xyz(segment[0][0], segment[0][1])
            point_b = uv_to_xyz(segment[1][0], segment[1][1])
            payload = 'role={0}; owner={1}; owner_id={2}; segment={3}; compartment_id={4}'.format(
                role,
                owner_number,
                owner_element_id if owner_element_id is not None else '',
                segment_number,
                selected_compartment_id
            )
            if extra_payload:
                payload += '; {0}'.format(to_text(extra_payload).strip(' ;'))
            curve = create_tagged_detail_line(
                point_a,
                point_b,
                payload
            )
            if curve is not None:
                ids.append(curve.Id)
        return ids
    except Exception:
        if ids:
            try:
                doc.Delete(make_id_list(ids))
            except Exception:
                pass
        raise


old_route_result_ids = []
legacy_result_ids = []
old_filled_region_ids = []
partial_old_ids_by_cabinet = dict(
    (cabinet_index, set()) for cabinet_index in forced_cabinet_indices
)


def readable_result_comment(element):
    try:
        parameter = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        return to_text(parameter.AsString()) if parameter is not None else u''
    except Exception:
        return u''


def payload_compartment_id(payload):
    data = parse_route_result_payload(payload)
    return safe_int_value(data.get('compartment_id'))


def element_representative_points(element):
    points = []
    try:
        if isinstance(element, PathOfTravel):
            for curve in get_path_curves(element):
                points.append(curve.GetEndPoint(0))
                points.append(curve.GetEndPoint(1))
        elif isinstance(element, DB.CurveElement):
            curve = element.GeometryCurve
            if curve is not None:
                points.append(curve.GetEndPoint(0))
                points.append(curve.GetEndPoint(1))
        elif isinstance(element, DB.FilledRegion):
            for loop in element.GetBoundaries() or []:
                for curve in loop:
                    points.append(curve.GetEndPoint(0))
                    if len(points) >= 12:
                        break
                if len(points) >= 12:
                    break
    except Exception:
        pass
    if not points:
        try:
            bbox = element.get_BoundingBox(view)
            if bbox is not None:
                points.append(DB.XYZ(
                    (bbox.Min.X + bbox.Max.X) * 0.5,
                    (bbox.Min.Y + bbox.Max.Y) * 0.5,
                    (bbox.Min.Z + bbox.Max.Z) * 0.5
                ))
        except Exception:
            pass
    return points


def existing_result_belongs_to_selected_compartment(element, payloads):
    explicit_ids = []
    for payload in payloads:
        compartment_id = payload_compartment_id(payload)
        if compartment_id is not None:
            explicit_ids.append(compartment_id)
    if explicit_ids:
        return selected_compartment_id in explicit_ids

    # Migration fallback for results created by earlier tool versions that did
    # not store a compartment ID. Routes produced by this tool stay inside one
    # selected Filled Region, so representative geometry inside the current
    # boundary is a safe one-time attribution method.
    for point in element_representative_points(element):
        try:
            u_value, v_value = xyz_to_uv(point)
            if region_contains(compartment_polygons, u_value, v_value):
                return True
        except Exception:
            continue
    return False


def register_existing_result(element):
    entity_payload = get_route_result_payload(element)
    comment_text = readable_result_comment(element)
    candidate_scope_payloads = [value for value in (entity_payload, comment_text) if value]
    if not existing_result_belongs_to_selected_compartment(element, candidate_scope_payloads):
        return

    # Append mode never deletes or replaces any existing result. The newly
    # generated picked-point route is stored as an additional independent result.
    if run_mode == RUN_MODE_FORCE_DOOR_APPEND:
        return

    if run_mode == RUN_MODE_FULL_REBUILD:
        if entity_payload:
            old_route_result_ids.append(element.Id)
            return
        if comment_text and any(
            comment_text.startswith(prefix) for prefix in LEGACY_RESULT_COMMENT_PREFIXES
        ):
            legacy_result_ids.append(element.Id)
        return

    # Picked-point-only mode is deliberately conservative: delete only a route,
    # cabinet marker or route-distance marker that can be attributed to one of the
    # cabinets assigned a picked point in this run. Unknown/legacy global
    # results and uncovered markers remain untouched.
    candidate_payloads = []
    if entity_payload:
        candidate_payloads.append(entity_payload)
    if comment_text:
        candidate_payloads.append(comment_text)
    for payload in candidate_payloads:
        cabinet_index = forced_cabinet_index_from_payload(payload)
        if cabinet_index is not None:
            partial_old_ids_by_cabinet.setdefault(cabinet_index, set()).add(element.Id)
            return


if DELETE_OLD_RESULTS:
    try:
        for path_element in DB.FilteredElementCollector(doc, view.Id).OfClass(PathOfTravel):
            try:
                register_existing_result(path_element)
            except Exception:
                continue
    except Exception:
        pass

    try:
        for curve_element in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.CurveElement):
            try:
                register_existing_result(curve_element)
            except Exception:
                continue
    except Exception:
        pass

    # Filled Regions belong to legacy full-run output and cannot be attributed
    # safely to a single cabinet. Delete them only during a full rebuild.
    if run_mode == RUN_MODE_FULL_REBUILD:
        try:
            for region in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.FilledRegion):
                try:
                    text_value = readable_result_comment(region)
                    if (
                        text_value and any(
                            text_value.startswith(prefix) for prefix in LEGACY_RESULT_COMMENT_PREFIXES
                        ) and existing_result_belongs_to_selected_compartment(region, [text_value])
                    ):
                        old_filled_region_ids.append(region.Id)
                except Exception:
                    continue
        except Exception:
            old_filled_region_ids = []

# Large obstacle solids are no longer needed. Release references before opening
# a document-modifying Transaction.
cell_prism_cache.clear()
solid_element_obstacles[:] = []
fallback_boxes[:] = []
wall_segments[:] = []

created_route_ids = []
created_marker_ids = []
created_35m_marker_ids = []
created_uncovered_marker_ids = []
distance_35m_marker_records = []
failed_route_creations = []
failed_35m_marker_creations = []
failed_uncovered_marker_creations = []

transaction_title = 'Rebuild fire cabinet routes in selected compartment'
if run_mode == RUN_MODE_FORCE_DOOR_APPEND:
    transaction_title = 'Add picked-point cabinet routes'

transaction = DB.Transaction(doc, transaction_title)
try:
    start_status = transaction.Start()
    if start_status != DB.TransactionStatus.Started:
        raise Exception('Cannot start Transaction. Status: {0}'.format(start_status))

    # Failed trial routes are handled in SubTransactions. Suppress their warning
    # dialogs so a 300 mm grid cannot interrupt the batch with a modal message.
    try:
        failure_options = transaction.GetFailureHandlingOptions()
        failure_options.SetFailuresPreprocessor(RouteFailurePreprocessor())
        failure_options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(failure_options)
    except Exception:
        pass

    if run_mode == RUN_MODE_FULL_REBUILD:
        ids_to_delete = (
            list(old_route_result_ids) +
            list(legacy_result_ids) +
            list(old_filled_region_ids)
        )
        if ids_to_delete:
            doc.Delete(make_id_list(ids_to_delete))

    for route_record in route_records_to_create:
        cabinet_record = route_record['cabinet_record']
        cabinet_number = cabinet_record['diagnostic_number']
        subtransaction = DB.SubTransaction(doc)
        subtransaction.Start()
        try:
            points_uv = route_record['points_uv']
            cabinet_element_id = eid_int(cabinet_record['element'].Id)
            is_append_mode = (run_mode == RUN_MODE_FORCE_DOOR_APPEND)
            result_role = 'force_point_append_route' if is_append_mode else 'route'
            result_variant = 'force_point_append' if is_append_mode else 'base'
            route_ids, native_curve_count, path_creation_mode = create_route_path_of_travel(
                points_uv,
                cabinet_number,
                cabinet_element_id,
                cabinet_record['route_distance'] * 0.3048,
                route_record.get('force_portals'),
                route_record.get('force_far_side_signs'),
                result_role,
                result_variant
            )

            if not route_ids:
                raise Exception('No route Path of Travel was created.')

            # Append mode keeps the original cabinet marker. No marker is
            # created at the route endpoint in any mode. A plus marker is created
            # only at exactly 35 m along native routes whose actual length exceeds
            # 35 m.
            if is_append_mode:
                cabinet_marker_ids = []
            else:
                cabinet_marker_ids = create_marker_lines(
                    cabinet_record['center_u'],
                    cabinet_record['center_v'],
                    mm_to_ft(CABINET_MARKER_HALF_SIZE_MM),
                    'x',
                    'cabinet_marker',
                    cabinet_number,
                    cabinet_element_id
                )

            checkpoint_marker_ids = []
            checkpoint_marker_error = None
            checkpoint_point, native_route_length = point_at_distance_along_path(
                route_ids,
                m_to_ft(ROUTE_CHECKPOINT_DISTANCE_M)
            )
            cabinet_record['native_route_length'] = native_route_length
            cabinet_record['checkpoint_35m_marker_ids'] = []

            if checkpoint_point is not None:
                try:
                    checkpoint_u, checkpoint_v = xyz_to_uv(checkpoint_point)
                    checkpoint_role = (
                        'force_point_append_35m_marker'
                        if is_append_mode else
                        'distance_checkpoint_35m_marker'
                    )
                    checkpoint_marker_ids = create_marker_lines(
                        checkpoint_u,
                        checkpoint_v,
                        endpoint_marker_half_size,
                        'plus',
                        checkpoint_role,
                        cabinet_number,
                        cabinet_element_id,
                        'checkpoint_m={0:.3f}; native_route_length_m={1:.3f}'.format(
                            ROUTE_CHECKPOINT_DISTANCE_M,
                            native_route_length * 0.3048
                        )
                    )
                    if not checkpoint_marker_ids:
                        raise Exception('No 35 m checkpoint Detail Line was created.')
                except Exception as checkpoint_error:
                    checkpoint_marker_error = to_text(checkpoint_error)
                    checkpoint_marker_ids = []

            sub_status = subtransaction.Commit()
            if sub_status != DB.TransactionStatus.Committed:
                raise Exception('Cabinet SubTransaction was not committed.')

            cabinet_record['route_ids'] = route_ids
            cabinet_record['cabinet_marker_ids'] = cabinet_marker_ids
            cabinet_record['endpoint_marker_ids'] = []
            cabinet_record['checkpoint_35m_marker_ids'] = checkpoint_marker_ids
            cabinet_record['route_status'] = (
                'Added picked-point route'
                if run_mode == RUN_MODE_FORCE_DOOR_APPEND
                else 'Created'
            )
            cabinet_record['path_creation_mode'] = path_creation_mode
            cabinet_record['route_segment_count'] = native_curve_count
            created_route_ids.extend(route_ids)
            created_marker_ids.extend(cabinet_marker_ids)
            created_35m_marker_ids.extend(checkpoint_marker_ids)
            if checkpoint_marker_ids:
                distance_35m_marker_records.append({
                    'number': len(distance_35m_marker_records) + 1,
                    'marker_ids': list(checkpoint_marker_ids),
                    'route_ids': list(route_ids),
                    'cabinet_number': cabinet_number,
                    'cabinet_element_id': cabinet_element_id,
                    'native_route_length_m': native_route_length * 0.3048,
                    'run_mode': run_mode,
                })
            elif checkpoint_marker_error:
                failed_35m_marker_creations.append({
                    'cabinet_number': cabinet_number,
                    'cabinet_element_id': cabinet_element_id,
                    'reason': checkpoint_marker_error,
                })
        except Exception as error:
            try:
                if subtransaction.GetStatus() == DB.TransactionStatus.Started:
                    subtransaction.RollBack()
            except Exception:
                pass
            crossing_diagnostics = getattr(error, 'diagnostics', None)
            if crossing_diagnostics:
                cabinet_record['force_crossing_diagnostic'] = crossing_diagnostics
                cabinet_record['route_status'] = (
                    'Creation failed: Force Door crossing validation failed ({0}).'.format(
                        crossing_diagnostics.get('failure_code') or 'unknown'
                    )
                )
            else:
                cabinet_record['route_status'] = 'Creation failed: {0}'.format(error)
            failed_route_creations.append(cabinet_record)

    if create_uncovered_markers and run_mode == RUN_MODE_FULL_REBUILD:
        marker_clusters = uncovered_clusters[:MAX_UNCOVERED_CLUSTER_MARKERS]
        for cluster in marker_clusters:
            subtransaction = DB.SubTransaction(doc)
            subtransaction.Start()
            try:
                marker_ids = create_marker_lines(
                    cluster['marker_u'],
                    cluster['marker_v'],
                    endpoint_marker_half_size,
                    'diamond',
                    'uncovered_cluster',
                    cluster['number']
                )
                if not marker_ids:
                    raise Exception('No cluster marker Detail Line was created.')
                sub_status = subtransaction.Commit()
                if sub_status != DB.TransactionStatus.Committed:
                    raise Exception('Cluster SubTransaction was not committed.')
                cluster['marker_ids'] = marker_ids
                created_uncovered_marker_ids.extend(marker_ids)
            except Exception as error:
                try:
                    if subtransaction.GetStatus() == DB.TransactionStatus.Started:
                        subtransaction.RollBack()
                except Exception:
                    pass
                failed_uncovered_marker_creations.append((cluster, to_text(error)))

    if run_mode == RUN_MODE_FULL_REBUILD and failed_route_creations:
        failed_labels = []
        for failed_record in failed_route_creations[:8]:
            try:
                failed_labels.append(
                    'Cabinet {0} [Id {1}]: {2}'.format(
                        failed_record.get('diagnostic_number') or '-',
                        eid_int(failed_record['element'].Id),
                        failed_record.get('route_status') or 'Route creation failed'
                    )
                )
            except Exception:
                failed_labels.append(
                    'Cabinet {0}: {1}'.format(
                        failed_record.get('diagnostic_number') or '-',
                        failed_record.get('route_status') or 'Route creation failed'
                    )
                )
        raise Exception(
            'Full Rebuild is atomic. {0} cabinet route(s) failed, so the complete '
            'Transaction was rolled back and every existing route was preserved.\n\n{1}'.format(
                len(failed_route_creations),
                '\n'.join(failed_labels)
            )
        )

    if (
        run_mode == RUN_MODE_FULL_REBUILD and
        route_records_to_create and
        not created_route_ids
    ):
        raise Exception(
            'No Path of Travel route was created. The complete Transaction will be rolled back '
            'and every existing route will be preserved.'
        )

    commit_status = transaction.Commit()
    if commit_status != DB.TransactionStatus.Committed:
        raise Exception('Transaction was not committed. Status: {0}'.format(commit_status))
except Exception:
    try:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
    except Exception:
        pass
    forms.alert(
        'Failed to create fire cabinet Path of Travel routes.\n\n{0}'.format(
            traceback.format_exc()
        ),
        exitscript=True
    )

local_mode_created_nothing = (
    run_mode == RUN_MODE_FORCE_DOOR_APPEND and
    bool(route_records_to_create) and
    not bool(created_route_ids)
)
if local_mode_created_nothing:
    failure_lines = []
    for failed_record in failed_route_creations[:8]:
        try:
            cabinet_id_text = str(eid_int(failed_record['element'].Id))
        except Exception:
            cabinet_id_text = '-'
        failure_lines.append(
            'Cabinet {0} [Id {1}]: {2}'.format(
                failed_record.get('diagnostic_number') or '-',
                cabinet_id_text,
                failed_record.get('route_status') or 'Creation failed'
            )
        )
        crossing_diagnostics = failed_record.get('force_crossing_diagnostic')
        if crossing_diagnostics:
            failure_lines.append(
                format_force_portal_diagnostics(
                    crossing_diagnostics,
                    compact=False
                )
            )
    preserve_text = (
        'All existing routes were preserved; no additional route was added.'
        if run_mode == RUN_MODE_FORCE_DOOR_APPEND
        else 'The old routes were preserved because each failed replacement was rolled back.'
    )
    forms.alert(
        'No picked-point Path of Travel route was created in this run.\n\n{0}\n\n{1}'.format(
            '\n'.join(failure_lines),
            preserve_text
        )
    )

path_graphics_refresh = force_path_graphics_refresh(created_route_ids)

if created_route_ids:
    try:
        uidoc.Selection.SetElementIds(make_id_list(created_route_ids))
    except Exception:
        pass
    try:
        uidoc.RefreshActiveView()
    except Exception:
        pass


# =============================================================================
# REPORT
# =============================================================================
uncovered_area_m2 = ft2_to_m2(uncovered_count * cell * cell)
blocked_count = sum([1 for index in inside_indices if blocked[index]])
created_route_count = sum([
    1 for cabinet_record in cabinet_records
    if cabinet_record.get('route_status') in ('Created', 'Added picked-point route')
])

output.print_md('## Fire cabinet Path of Travel routes - Owned multi-source Dijkstra')
output.print_md('- Revit version: **{0}**'.format(revit_major))
output.print_md('- Cabinet source mode: **{0}**'.format('Auto Scan' if auto_scan_enabled else 'Manual Pick'))
if run_mode == RUN_MODE_FULL_REBUILD:
    run_mode_report = 'Rebuild routes only in the selected compartment; no point picking'
else:
    run_mode_report = (
        'Assign each point to its nearest reachable cabinet, optimize the visit order, and preserve every existing route'
    )
output.print_md('- Update mode: **{0}**'.format(run_mode_report))
output.print_md('- Cabinets connected with unique seeds: **{0} / {1}**'.format(
    connected_cabinet_count,
    len(cabinet_records)
))
output.print_md('- Longest owned Path of Travel routes created this run: **{0} / {1}**'.format(
    created_route_count,
    len(route_records_to_create)
))
if run_mode == RUN_MODE_FORCE_DOOR_APPEND:
    output.print_md(
        '- Existing routes preserved: **all existing base and previously added routes**'
    )
    output.print_md(
        '- Additional constrained endpoint rule: **optimize the visit order of all points assigned to the nearest reachable cabinet and stop at the last point reached; do not continue toward 40 m**'
    )
output.print_md('- Cabinet check point: **centre of physical geometry**')
output.print_md('- Native Path of Travel start: **nearest unique walkable cabinet seed**')
output.print_md(
    '- Cabinet centre methods: **{0} Solid geometry**, **{1} element BoundingBox**, '
    '**{2} Location fallback**'.format(
        cabinet_center_method_counts['solid_geometry'],
        cabinet_center_method_counts['element_bbox'],
        cabinet_center_method_counts['location_fallback']
    )
)
output.print_md('- Cabinets snapped without FacingOrientation constraint: **{0}**'.format(
    cabinet_unconstrained_snap_fallbacks
))
if auto_scan_enabled:
    output.print_md('- Auto Scan scope: **host model only; linked cabinets are excluded**')
    output.print_md('- Auto Scan keywords: **{0}**'.format(cabinet_keywords_raw))
output.print_md('- Picked-point workflow: **{0}**'.format('Add New mode - point picking required' if run_mode == RUN_MODE_FORCE_DOOR_APPEND else 'Rebuild mode - no point picking'))
if run_mode == RUN_MODE_FORCE_DOOR_APPEND:
    output.print_md(
        '- Picked-point diagnostics: **{0} selected**, **{1} inside compartment**, '
        '**{2} snapped to walkable cells**, **{3} assigned to baseline-owner cabinets**, '
        '**{4} unassigned/ignored**, **{5} cabinet groups**, **{6} successful constrained routes**'.format(
            force_door_diagnostics['selected'],
            force_door_diagnostics['inside_compartment'],
            force_door_diagnostics['snapped_walkable'],
            force_door_diagnostics['assigned'],
            force_door_diagnostics['unassigned'],
            force_door_diagnostics.get('cabinet_groups', 0),
            force_door_diagnostics.get('successful_routes', 0)
        )
    )
output.print_md('- Maximum travel distance: **{0:g} m**'.format(max_distance_m))
output.print_md('- Grid size: **{0:g} mm**'.format(grid_mm))
output.print_md('- Obstacle clearance: **{0:g} mm**'.format(clearance_mm))
output.print_md('- Path of Travel sample: **not required; the tool can run in a project that has never contained a Path of Travel**')
output.print_md('- Route endpoint plus markers: **not created**')
output.print_md('- 35 m checkpoint rule: **one plus marker at 35 m for each native route longer than 35 m**')
output.print_md('- Path of Travel and diagnostic marker LineStyle: **{0}**'.format(
    markdown_cell(safe_element_name(selected_line_style))
))
output.print_md('- Analysis height band: **{0:g} to {1:g} mm above active level**'.format(
    ANALYSIS_BOTTOM_MM,
    ANALYSIS_TOP_MM
))
output.print_md('- Selected compartment Filled Region ID: **{0}**'.format(selected_compartment_id))
output.print_md('- Grid cells inside compartment: **{0:,}**'.format(len(inside_indices)))
output.print_md('- Blocked cells: **{0:,}**'.format(blocked_count))
output.print_md('- Walkable cells: **{0:,}**'.format(walkable_count))
output.print_md('- Covered cells: **{0:,}**'.format(covered_count))
output.print_md('- Uncovered cells: **{0:,}**'.format(uncovered_count))
output.print_md('- Approximate uncovered area: **{0:.2f} m2**'.format(uncovered_area_m2))
output.print_md('- Uncovered clusters: **{0}**'.format(len(uncovered_clusters)))
output.print_md('- Native Path of Travel elements created: **{0}**'.format(len(created_route_ids)))
output.print_md('- Grid points removed from straight runs: **{0}**'.format(
    sum([int(route_record.get('straight_run_removed_count') or 0) for route_record in route_records_to_create])
))
output.print_md('- Additional close turn waypoints removed: **{0}**'.format(
    sum([int(route_record.get('local_removed_count') or 0) for route_record in route_records_to_create])
))
output.print_md('- Native turn controls prepared: **{0}**'.format(
    sum([int(route_record.get('native_control_count') or 0) for route_record in route_records_to_create])
))
output.print_md(
    '- Path of Travel control rule: every straight Dijkstra run uses only its two ends; '
    'waypoints are retained at actual direction changes, route start/end and mandatory picked points. '
    'Close non-mandatory turns below **{0:.0f} mm** may be merged only when the replacement '
    'chord remains raster-safe and is no longer than **{1:.0f} mm**.'.format(
        PATH_WAYPOINT_REDUCE_THRESHOLD_MM,
        PATH_WAYPOINT_REDUCE_MAX_MERGED_MM
    )
)
output.print_md(
    '- Path graphics refresh: **{0}**, **{1} updated**, **{2} pulsed**, **{3} failures**'.format(
        path_graphics_refresh.get('message'),
        path_graphics_refresh.get('updated'),
        path_graphics_refresh.get('pulsed'),
        path_graphics_refresh.get('failed')
    )
)
output.print_md('- Cabinet marker Detail Lines created: **{0}**'.format(len(created_marker_ids)))
output.print_md('- 35 m checkpoint points created: **{0}**'.format(len(distance_35m_marker_records)))
output.print_md('- 35 m checkpoint Detail Lines created: **{0}**'.format(len(created_35m_marker_ids)))
output.print_md('- Uncovered marker lines created: **{0}**'.format(len(created_uncovered_marker_ids)))
if run_mode == RUN_MODE_FORCE_DOOR_APPEND:
    output.print_md('- Uncovered-cluster markers: **preserved unchanged**')

if failed_cabinets:
    output.print_md('- Cabinets that could not be connected: **{0}**'.format(len(failed_cabinets)))
if failed_route_creations:
    output.print_md('- Cabinet routes that failed during creation: **{0}**'.format(
        len(failed_route_creations)
    ))
if failed_35m_marker_creations:
    output.print_md('- 35 m checkpoint markers that failed: **{0}**'.format(
        len(failed_35m_marker_creations)
    ))
if failed_uncovered_marker_creations:
    output.print_md('- Uncovered cluster markers that failed: **{0}**'.format(
        len(failed_uncovered_marker_creations)
    ))
if len(uncovered_clusters) > MAX_UNCOVERED_CLUSTER_MARKERS and create_uncovered_markers:
    output.print_md(
        '- Uncovered marker safety limit: only the largest **{0}** clusters were marked.'.format(
            MAX_UNCOVERED_CLUSTER_MARKERS
        )
    )

if auto_scan_diagnostics is not None:
    output.print_md(
        '- Auto Scan diagnostics: **{0} host candidates**, **{1} keyword matches**, '
        '**{2} matches inside compartment**, centre detection: **{3} Solid**, '
        '**{4} BoundingBox**, **{5} Location fallback**'.format(
            auto_scan_diagnostics['host_candidates'],
            auto_scan_diagnostics['keyword_matches'],
            auto_scan_diagnostics['inside_matches'],
            auto_scan_diagnostics['solid_geometry_centres'],
            auto_scan_diagnostics['element_bbox_centres'],
            auto_scan_diagnostics['location_fallback_centres']
        )
    )

# -----------------------------------------------------------------------------
# CABINET ROUTE AND DIAGNOSTIC TABLE
# -----------------------------------------------------------------------------
output.print_md('### Cabinet route diagnostics')
output.print_md(
    '> **Cabinet** zooms to the physical check point, **35 m** zooms to the '
    'checkpoint at 35 m along the actual native route, and **Route** selects the complete real '
    'Dijkstra path. All cabinet route sources are host-model elements.'
)
output.print_md(
    '| No. | Cabinet | 35 m | Route | Status | Picked points | Longest owned route (m) | Owned cells | Segments | Source | Cabinet Element ID | Family : Type | Match | Centre method | Snap (mm) | Nearest cabinet |'
)
output.print_md(
    '|---:|:---:|:---:|:---:|---|---|---:|---:|---:|---|---:|---|---|---|---:|---|'
)

for cabinet_record in cabinet_records:
    cabinet = cabinet_record.get('element')
    number = cabinet_record.get('diagnostic_number', '')

    cabinet_marker_ids = cabinet_record.get('cabinet_marker_ids') or []
    checkpoint_35m_marker_ids = cabinet_record.get('checkpoint_35m_marker_ids') or []
    route_ids = cabinet_record.get('route_ids') or []

    try:
        cabinet_zoom = output.linkify(cabinet_marker_ids, title='Zoom') if cabinet_marker_ids else '-'
    except Exception:
        cabinet_zoom = '-'
    try:
        checkpoint_35m_zoom = output.linkify(
            checkpoint_35m_marker_ids,
            title='Zoom'
        ) if checkpoint_35m_marker_ids else '-'
    except Exception:
        checkpoint_35m_zoom = '-'
    try:
        route_link = output.linkify(route_ids, title='Select') if route_ids else '-'
    except Exception:
        route_link = '-'

    source_text = 'Host'
    try:
        element_text = output.linkify(cabinet.Id, title=str(eid_int(cabinet.Id)))
    except Exception:
        element_text = str(eid_int(cabinet.Id)) if cabinet is not None else '-'

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

    keyword_text = '; '.join(cabinet_record.get('matched_keywords') or []) or '-'
    route_distance = cabinet_record.get('route_distance')
    route_distance_text = '{0:.2f}'.format(route_distance * 0.3048) if route_distance is not None else '-'
    snap_distance = cabinet_record.get('snap_distance')
    snap_text = '{0:.0f}'.format(ft_to_mm(snap_distance)) if snap_distance is not None else '-'

    nearest_number = cabinet_record.get('nearest_cabinet_number')
    nearest_distance = cabinet_record.get('nearest_cabinet_distance')
    if nearest_number is not None and nearest_distance is not None:
        nearest_text = '#{0} / {1:.0f} mm'.format(nearest_number, ft_to_mm(nearest_distance))
    else:
        nearest_text = '-'

    status_text = '{0}; {1}'.format(
        cabinet_record.get('connection_status') or '-',
        cabinet_record.get('route_status') or '-'
    )
    if cabinet_record.get('path_creation_mode'):
        status_text += ' / {0}'.format(cabinet_record['path_creation_mode'])
    if cabinet_record.get('used_unconstrained_facing'):
        status_text += ' (Facing ignored)'

    force_status = cabinet_record.get('force_door_status') or 'Not constrained'
    force_point_numbers = list(cabinet_record.get('force_point_numbers') or [])
    if force_point_numbers:
        force_status += ' / Points {0}'.format(', '.join([to_text(value) for value in force_point_numbers]))

    output.print_md(
        '| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7:,} | {8} | {9} | {10} | {11} | {12} | {13} | {14} | {15} |'.format(
            number,
            cabinet_zoom,
            checkpoint_35m_zoom,
            route_link,
            markdown_cell(status_text),
            markdown_cell(force_status),
            route_distance_text,
            cabinet_record.get('owned_covered_cells') or 0,
            cabinet_record.get('route_segment_count') or 0,
            markdown_cell(source_text),
            element_text,
            markdown_cell(family_type_text),
            markdown_cell(keyword_text),
            markdown_cell(cabinet_record.get('center_method') or '-'),
            snap_text,
            markdown_cell(nearest_text)
        )
    )

if distance_35m_marker_records:
    output.print_md('### 35 m checkpoint diagnostics')
    output.print_md(
        '> A plus marker is placed at exactly **{0:.0f} m** along the actual native '
        'Path of Travel geometry. Routes whose native length is not greater than '
        '{0:.0f} m do not receive a marker.'.format(ROUTE_CHECKPOINT_DISTANCE_M)
    )
    output.print_md(
        '| Point | Zoom | Route | Cabinet | Cabinet Element ID | Native route length (m) | Result mode |'
    )
    output.print_md('|---:|:---:|:---:|---:|---:|---:|---|')
    for checkpoint_record in distance_35m_marker_records:
        marker_ids = checkpoint_record.get('marker_ids') or []
        route_ids = checkpoint_record.get('route_ids') or []
        try:
            zoom_link = output.linkify(marker_ids, title='Zoom') if marker_ids else '-'
        except Exception:
            zoom_link = '-'
        try:
            route_link = output.linkify(route_ids, title='Select') if route_ids else '-'
        except Exception:
            route_link = '-'
        output.print_md(
            '| {0} | {1} | {2} | {3} | {4} | {5:.2f} | {6} |'.format(
                checkpoint_record.get('number') or '-',
                zoom_link,
                route_link,
                checkpoint_record.get('cabinet_number') or '-',
                checkpoint_record.get('cabinet_element_id') or '-',
                checkpoint_record.get('native_route_length_m') or 0.0,
                markdown_cell(checkpoint_record.get('run_mode') or '-')
            )
        )

if failed_35m_marker_creations:
    output.print_md('### Failed 35 m checkpoint markers')
    output.print_md('| Cabinet | Cabinet Element ID | Reason |')
    output.print_md('|---:|---:|---|')
    for failure in failed_35m_marker_creations:
        output.print_md(
            '| {0} | {1} | {2} |'.format(
                failure.get('cabinet_number') or '-',
                failure.get('cabinet_element_id') or '-',
                markdown_cell(failure.get('reason') or '-')
            )
        )


if force_door_enabled:
    output.print_md('### Mandatory picked-point diagnostics')
    output.print_md(
        "> Each picked point is snapped to a walkable cell and assigned to the cabinet "
        "that is nearest by walkable route distance in the normal multi-source coverage map. "
        "Add New automatically optimizes the visit order of all points assigned to that "
        "cabinet and stops at the last point reached without entering another cabinet's territory."
    )
    output.print_md('| Point | Status | Assigned cabinet | Snap (mm) | Cabinet-to-point grid distance (m) | U (mm) | V (mm) |')
    output.print_md('|---:|---|---:|---:|---:|---:|---:|')
    for point_record in sorted(force_door_rows, key=lambda item: int(item.get('number') or 0)):
        cabinet_index = point_record.get('assigned_cabinet_index')
        cabinet_number = '-'
        if cabinet_index is not None and cabinet_index >= 0 and cabinet_index < len(cabinet_records):
            cabinet_number = cabinet_records[cabinet_index].get('diagnostic_number') or '-'
        grid_distance = point_record.get('assigned_grid_distance')
        grid_distance_text = '{0:.2f}'.format(grid_distance * 0.3048) if grid_distance is not None else '-'
        snap_distance = point_record.get('snap_distance')
        snap_text = '{0:.0f}'.format(ft_to_mm(snap_distance)) if snap_distance is not None else '-'
        point_u = point_record.get('u')
        point_v = point_record.get('v')
        output.print_md(
            '| {0} | {1} | {2} | {3} | {4} | {5} | {6} |'.format(
                point_record.get('number') or '-',
                markdown_cell(point_record.get('assignment_status') or '-'),
                cabinet_number,
                snap_text,
                grid_distance_text,
                '{0:.0f}'.format(ft_to_mm(point_u)) if point_u is not None else '-',
                '{0:.0f}'.format(ft_to_mm(point_v)) if point_v is not None else '-'
            )
        )



if uncovered_clusters:
    output.print_md('### Uncovered cluster diagnostics')
    output.print_md(
        '| Cluster | Zoom | Cells | Approximate area (m2) | Marker status |'
    )
    output.print_md('|---:|:---:|---:|---:|---|')
    for cluster in uncovered_clusters:
        marker_ids = cluster.get('marker_ids') or []
        try:
            zoom_link = output.linkify(marker_ids, title='Zoom') if marker_ids else '-'
        except Exception:
            zoom_link = '-'
        status_text = 'Created' if marker_ids else (
            'Not requested' if not create_uncovered_markers else 'Not created'
        )
        output.print_md(
            '| {0} | {1} | {2:,} | {3:.2f} | {4} |'.format(
                cluster['number'],
                zoom_link,
                cluster['cell_count'],
                ft2_to_m2(cluster['cell_count'] * cell * cell),
                status_text
            )
        )

all_created_ids = created_route_ids + created_marker_ids + created_uncovered_marker_ids
if all_created_ids:
    try:
        all_result_link = output.linkify(all_created_ids, title='Select all generated route results')
        output.print_md('- {0}'.format(all_result_link))
    except Exception:
        pass

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
    '**{5} normal carved cells**, **{6} forced tunnel cells**, **{7} portals outside the compartment grid**'.format(
        obstacle_stats['door_parameter_widths'],
        obstacle_stats['door_bbox_widths'],
        obstacle_stats['door_default_widths'],
        obstacle_stats['door_wall_inserts'],
        obstacle_stats['door_host_fallbacks'],
        obstacle_stats['door_portal_cells_carved'],
        obstacle_stats['door_portal_cells_forced'],
        obstacle_stats['door_portals_without_inside_cells']
    )
)
output.print_md(
    '> Door openings are carved after the complete wall raster is built. Each door '
    'portal also receives a forced orthogonal raster tunnel across the inflated wall '
    'band, so a coarse or diagonal grid cannot accidentally close a valid doorway.'
)

if obstacle_stats['link_visibility_fallbacks']:
    output.print_md(
        '> Warning: linked-document visibility fallback was used for **{0}** link '
        'instance(s); hidden linked elements may still be included after geometry filtering.'.format(
            obstacle_stats['link_visibility_fallbacks']
        )
    )
else:
    output.print_md(
        '> Linked elements were collected with host-view visibility filtering available in Revit 2024 or newer.'
    )

output.print_md(
    "> Normal coverage distance, cabinet ownership and parent chains are calculated first by multi-source Dijkstra. Picked-point constraints do not alter that ownership. A state-space Dijkstra then searches only the assigned cabinet\'s original cells, visits all mandatory points in the best feasible order, and continues to the farthest endpoint whose total travel remains within the limit. PathOfTravel.FindShortestPaths is not called."
)
output.print_md(
    '> Each native Path of Travel is constrained by control points derived from the stored raster route. Replacement mode is cabinet seed -> all assigned picked points -> farthest reachable cell. Append mode is cabinet seed -> all assigned picked points in optimized order and stops at the last point reached. Every raster cell remains owned by that cabinet.'
)
output.print_md(
    '> Result Path of Travel elements and diagnostic marker Detail Lines are tagged with the selected Filled Region ID in Extensible Storage. The next run deletes '
    'only results generated by this tool for the selected compartment in the active view.'
)
output.print_md(
    '- Hybrid geometry checks: **{0:,} candidate cells**, **{1:,} solid-BBox rejects**, '
    '**{2:,} ray tests / {3:,} ray hits**, **{4:,} Boolean tests / {5:,} Boolean hits**, '
    '**{6:,} Boolean failures**'.format(
        obstacle_stats['candidate_cells'],
        obstacle_stats['solid_bbox_rejects'],
        obstacle_stats['fast_ray_tests'],
        obstacle_stats['fast_ray_hits'],
        obstacle_stats['boolean_tests'],
        obstacle_stats['boolean_hits'],
        obstacle_stats['boolean_failures']
    )
)
