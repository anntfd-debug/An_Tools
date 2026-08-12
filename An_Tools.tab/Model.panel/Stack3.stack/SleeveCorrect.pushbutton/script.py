# -*- coding: utf-8 -*-
from __future__ import division

import os
import json
import traceback
import clr

clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')
clr.AddReference('System.Xaml')

from System import Windows
from System.Windows.Markup import XamlReader
from System.Windows.Controls import TextBlock
from System.Windows.Media import Brushes
from pyrevit import forms, script
from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document
logger = script.get_logger()
output = script.get_output()

HERE = os.path.dirname(__file__)
XAML = os.path.join(HERE, 'ui.xaml')
SETTINGS = os.path.join(HERE, 'settings.json')

MM = 304.8
FLOOR_Z = 3000.0 / MM
GENERAL_TOL = 100.0 / MM
EDGE_EXT = 25.0 / MM
MIN_T = 1.0 / MM
MAX_T = 3000.0 / MM
SCORE_EPS = 0.01 / MM


def txt(value):
    try:
        return unicode(value) if value is not None else u''
    except Exception:
        try:
            return str(value) if value is not None else ''
        except Exception:
            return ''


def iid(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        try:
            return int(element_id.IntegerValue)
        except Exception:
            return -1


def eid(value):
    try:
        return ElementId(int(value))
    except Exception:
        return None


def norm(vector):
    try:
        if vector and vector.GetLength() > 1e-9:
            return vector.Normalize()
    except Exception:
        pass
    return None


def catid(element):
    try:
        return iid(element.Category.Id)
    except Exception:
        return -1


def cname(element):
    try:
        return txt(element.Category.Name)
    except Exception:
        return ''


def doc_key():
    try:
        return txt(doc.PathName) or txt(doc.Title)
    except Exception:
        return txt(doc.Title)


def defaults():
    return {
        'document_key': doc_key(),
        'sleeve_ids': [],
        'link_ids': [],
        'scan_wall': True,
        'scan_floor': True,
        'scan_beam': True,
        'include_current': True,
        'parameter_text': (
            'NWCH_PEN_Thickness; Host Thickness; Thickness; '
            'Wall Thickness; Floor Thickness; Beam Thickness'
        ),
        'action_mode': 'both'
    }


def load_state():
    state = defaults()
    try:
        if os.path.exists(SETTINGS):
            with open(SETTINGS, 'r') as stream:
                state.update(json.load(stream))
    except Exception as ex:
        logger.warning(txt(ex))
    if state.get('document_key') != doc_key():
        state['document_key'] = doc_key()
        state['sleeve_ids'] = []
        state['link_ids'] = []
    return state


def save_state(state):
    data = defaults()
    data.update(state or {})
    data['document_key'] = doc_key()
    temp_path = SETTINGS + '.tmp'
    try:
        with open(temp_path, 'w') as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
        if os.path.exists(SETTINGS):
            os.remove(SETTINGS)
        os.rename(temp_path, SETTINGS)
    except Exception as ex:
        logger.warning(txt(ex))


class SleeveFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, FamilyInstance)

    def AllowReference(self, reference, point):
        return False


class LinkFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, RevitLinkInstance) and element.GetLinkDocument() is not None

    def AllowReference(self, reference, point):
        return False


def typename(family_instance):
    try:
        symbol = family_instance.Symbol
        parameter = symbol.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        type_name = txt(parameter.AsString()) if parameter else txt(symbol.Name)
        return txt(symbol.Family.Name) + ' : ' + type_name
    except Exception:
        return 'FamilyInstance'


# -----------------------------------------------------------------------------
# Geometry
# -----------------------------------------------------------------------------

def collect_solids(geometry, transform=None):
    transform = transform or Transform.Identity
    result = []
    if not geometry:
        return result
    for obj in geometry:
        if isinstance(obj, Solid):
            try:
                if obj.Volume > 1e-9 and obj.Faces.Size > 0:
                    result.append((obj, transform))
            except Exception:
                pass
        elif isinstance(obj, GeometryInstance):
            try:
                result.extend(collect_solids(
                    obj.GetSymbolGeometry(),
                    transform.Multiply(obj.Transform)
                ))
            except Exception:
                try:
                    result.extend(collect_solids(obj.GetInstanceGeometry(), transform))
                except Exception:
                    pass
    return result


def get_element_solids(element):
    options = Options()
    options.DetailLevel = ViewDetailLevel.Fine
    options.IncludeNonVisibleObjects = False
    try:
        return collect_solids(element.get_Geometry(options))
    except Exception:
        return []


def transformed_bbox_points(solid, transform):
    points = []
    try:
        bbox = solid.GetBoundingBox()
        full_transform = transform.Multiply(bbox.Transform)
        for x in (bbox.Min.X, bbox.Max.X):
            for y in (bbox.Min.Y, bbox.Max.Y):
                for z in (bbox.Min.Z, bbox.Max.Z):
                    points.append(full_transform.OfPoint(XYZ(x, y, z)))
    except Exception:
        pass
    return points


def sleeve_solid_data(family_instance):
    """Return data based only on physical sleeve solids.

    Location.Point and Room Calculation Point are intentionally never used.
    """
    weighted = XYZ.Zero
    total_volume = 0.0
    xmin = xmax = ymin = ymax = zmin = zmax = None

    for solid, transform in get_element_solids(family_instance):
        try:
            volume = solid.Volume
            if volume <= 1e-9:
                continue
            center = transform.OfPoint(solid.ComputeCentroid())
            weighted = weighted + center * volume
            total_volume += volume
            for point in transformed_bbox_points(solid, transform):
                xmin = point.X if xmin is None or point.X < xmin else xmin
                xmax = point.X if xmax is None or point.X > xmax else xmax
                ymin = point.Y if ymin is None or point.Y < ymin else ymin
                ymax = point.Y if ymax is None or point.Y > ymax else ymax
                zmin = point.Z if zmin is None or point.Z < zmin else zmin
                zmax = point.Z if zmax is None or point.Z > zmax else zmax
        except Exception:
            pass

    if total_volume <= 1e-9 or None in (xmin, xmax, ymin, ymax, zmin, zmax):
        return None

    return {
        'center': weighted * (1.0 / total_volume),
        'xmin': xmin,
        'xmax': xmax,
        'ymin': ymin,
        'ymax': ymax,
        'zmin': zmin,
        'zmax': zmax
    }


def bbox_xy_overlap(element, xmin, xmax, ymin, ymax, tolerance):
    try:
        bbox = element.get_BoundingBox(None)
        if not bbox:
            return True
        return not (
            bbox.Max.X + tolerance < xmin or
            bbox.Min.X - tolerance > xmax or
            bbox.Max.Y + tolerance < ymin or
            bbox.Min.Y - tolerance > ymax
        )
    except Exception:
        return True


def bbox_xyz(element, point, tolerance):
    try:
        bbox = element.get_BoundingBox(None)
        return not bbox or (
            bbox.Min.X - tolerance <= point.X <= bbox.Max.X + tolerance and
            bbox.Min.Y - tolerance <= point.Y <= bbox.Max.Y + tolerance and
            bbox.Min.Z - tolerance <= point.Z <= bbox.Max.Z + tolerance
        )
    except Exception:
        return True


def axes(element):
    category_id = catid(element)
    result = []
    if category_id == int(BuiltInCategory.OST_Floors):
        result = [XYZ.BasisZ]
    elif category_id == int(BuiltInCategory.OST_Walls):
        try:
            result.append(element.Orientation)
        except Exception:
            pass
    else:
        try:
            transform = element.GetTransform()
            result.extend([transform.BasisY, transform.BasisZ])
        except Exception:
            pass
        result.extend([XYZ.BasisZ, XYZ.BasisX, XYZ.BasisY])
    return [norm(value) for value in result if norm(value)]


def floor_probe_points(center, xmin, xmax, ymin, ymax):
    """Probe across the physical sleeve footprint and 25 mm around its edge."""
    x0 = xmin - EDGE_EXT
    x1 = xmax + EDGE_EXT
    y0 = ymin - EDGE_EXT
    y1 = ymax + EDGE_EXT
    xs = [x0, x0 + (x1 - x0) * 0.25, (x0 + x1) * 0.5,
          x0 + (x1 - x0) * 0.75, x1]
    ys = [y0, y0 + (y1 - y0) * 0.25, (y0 + y1) * 0.5,
          y0 + (y1 - y0) * 0.75, y1]
    result = []
    for x in xs:
        for y in ys:
            probe = XYZ(x, y, center.Z)
            result.append((probe, probe.DistanceTo(center)))
    return result


def general_probe_points(point, axis):
    helper = XYZ.BasisX if abs(axis.DotProduct(XYZ.BasisZ)) > 0.9 else XYZ.BasisZ
    x_axis = norm(axis.CrossProduct(helper))
    y_axis = norm(axis.CrossProduct(x_axis)) if x_axis else None
    if not x_axis or not y_axis:
        return [(point, 0.0)]
    directions = [x_axis, x_axis.Negate(), y_axis, y_axis.Negate()]
    for vector in [x_axis + y_axis, x_axis - y_axis,
                   x_axis.Negate() + y_axis, x_axis.Negate() - y_axis]:
        vector = norm(vector)
        if vector:
            directions.append(vector)
    result = [(point, 0.0)]
    for distance_mm in (15, 30, 60, 100):
        distance = distance_mm / MM
        for direction in directions:
            result.append((point + direction * distance, distance))
    return result


def ray_segments(solid, transform, point, axis, half_length):
    try:
        inverse = transform.Inverse
        local_point = inverse.OfPoint(point)
        local_axis = norm(inverse.OfVector(axis))
        if not local_axis:
            return []
        line = Line.CreateBound(
            local_point - local_axis * half_length,
            local_point + local_axis * half_length
        )
        result = solid.IntersectWithCurve(line, SolidCurveIntersectionOptions())
        segments = []
        for index in range(result.SegmentCount):
            curve = result.GetCurveSegment(index)
            p0 = transform.OfPoint(curve.GetEndPoint(0))
            p1 = transform.OfPoint(curve.GetEndPoint(1))
            length = p0.DistanceTo(p1)
            if MIN_T <= length <= MAX_T:
                segments.append((length, (p0 + p1) * 0.5, p0, p1))
        return segments
    except Exception:
        return []


def floor_type_thickness(floor):
    try:
        floor_type = floor.Document.GetElement(floor.GetTypeId())
        parameter = floor_type.get_Parameter(BuiltInParameter.FLOOR_ATTR_THICKNESS_PARAM)
        value = parameter.AsDouble()
        if MIN_T <= value <= MAX_T:
            return value
    except Exception:
        pass
    return None


def level_of_host(host):
    try:
        level = host.Document.GetElement(host.LevelId)
        if isinstance(level, Level):
            return level
    except Exception:
        pass
    for name in ('LEVEL_PARAM', 'FAMILY_LEVEL_PARAM',
                 'INSTANCE_REFERENCE_LEVEL_PARAM', 'SCHEDULE_LEVEL_PARAM'):
        try:
            parameter = host.get_Parameter(getattr(BuiltInParameter, name))
            if parameter and parameter.StorageType == StorageType.ElementId:
                level = host.Document.GetElement(parameter.AsElementId())
                if isinstance(level, Level):
                    return level
        except Exception:
            pass
    return None


def direct_host_height_offset(host):
    if catid(host) == int(BuiltInCategory.OST_Floors):
        names = ('FLOOR_HEIGHTABOVELEVEL_PARAM',)
    else:
        names = ('INSTANCE_ELEVATION_PARAM',
                 'STRUCTURAL_BEAM_END0_ELEVATION', 'Z_OFFSET_VALUE')
    for name in names:
        try:
            parameter = host.get_Parameter(getattr(BuiltInParameter, name))
            if parameter and parameter.HasValue and parameter.StorageType == StorageType.Double:
                return parameter.AsDouble(), name
        except Exception:
            pass
    for name in ('Height Offset From Level', 'Z Offset Value', 'Start Level Offset'):
        try:
            parameter = host.LookupParameter(name)
            if parameter and parameter.HasValue and parameter.StorageType == StorageType.Double:
                return parameter.AsDouble(), name
        except Exception:
            pass
    return None, None


def host_offset_at_location(host, source_center, thickness):
    level = level_of_host(host)
    if catid(host) == int(BuiltInCategory.OST_Floors) and level and source_center:
        try:
            return ((source_center.Z + thickness * 0.5) - level.Elevation,
                    'Geometry at sleeve XY')
        except Exception:
            pass
    value, method = direct_host_height_offset(host)
    if value is not None:
        return value, method
    if level and source_center:
        try:
            return source_center.Z - level.Elevation, 'Geometry center vs Level'
        except Exception:
            pass
    return 0.0, 'Fallback zero'


# -----------------------------------------------------------------------------
# Cached host analysis
# -----------------------------------------------------------------------------

def collect_hosts(source_doc, scan_wall, scan_floor, scan_beam):
    result = []
    options = (
        (scan_wall, BuiltInCategory.OST_Walls),
        (scan_floor, BuiltInCategory.OST_Floors),
        (scan_beam, BuiltInCategory.OST_StructuralFraming)
    )
    for enabled, category in options:
        if enabled:
            try:
                result.extend(list(
                    FilteredElementCollector(source_doc)
                    .OfCategory(category)
                    .WhereElementIsNotElementType()
                    .ToElements()
                ))
            except Exception:
                pass
    return result


def build_host_cache(sources, scan_wall, scan_floor, scan_beam):
    """Collect hosts and geometry once, before any sleeve is modified."""
    cache = []
    for source_doc, source_transform, source_name in sources:
        for host in collect_hosts(source_doc, scan_wall, scan_floor, scan_beam):
            solids = get_element_solids(host)
            if not solids:
                continue
            cache.append({
                'host': host,
                'transform': source_transform,
                'source': source_name,
                'solids': solids,
                'host_id': iid(host.Id)
            })
    return cache


def source_footprint(sleeve_data, inverse):
    corners = []
    z = sleeve_data['center'].Z
    for x in (sleeve_data['xmin'], sleeve_data['xmax']):
        for y in (sleeve_data['ymin'], sleeve_data['ymax']):
            corners.append(inverse.OfPoint(XYZ(x, y, z)))
    return (
        min(point.X for point in corners),
        max(point.X for point in corners),
        min(point.Y for point in corners),
        max(point.Y for point in corners)
    )


def best_hit(cache_item, sleeve_data):
    host = cache_item['host']
    source_transform = cache_item['transform']
    try:
        inverse = source_transform.Inverse
        point = inverse.OfPoint(sleeve_data['center'])
        sxmin, sxmax, symin, symax = source_footprint(sleeve_data, inverse)
    except Exception:
        return None

    is_floor = catid(host) == int(BuiltInCategory.OST_Floors)
    if is_floor:
        if not bbox_xy_overlap(host, sxmin, sxmax, symin, symax, EDGE_EXT):
            return None
    elif not bbox_xyz(host, point, GENERAL_TOL):
        return None

    best = None
    for solid, solid_transform in cache_item['solids']:
        for axis in axes(host):
            probes = (floor_probe_points(point, sxmin, sxmax, symin, symax)
                      if is_floor else general_probe_points(point, axis))
            for probe, probe_distance in probes:
                half_length = FLOOR_Z if is_floor else 10.0
                for thickness, center, p0, p1 in ray_segments(
                        solid, solid_transform, probe, axis, half_length):
                    along = (center - point).DotProduct(axis)
                    source_center = point + axis * along
                    host_center = source_transform.OfPoint(source_center)
                    overlap = 0.0
                    if is_floor:
                        hz0 = source_transform.OfPoint(p0).Z
                        hz1 = source_transform.OfPoint(p1).Z
                        overlap = max(
                            0.0,
                            min(max(hz0, hz1), sleeve_data['zmax']) -
                            max(min(hz0, hz1), sleeve_data['zmin'])
                        )
                    physical_rank = 0 if (not is_floor or overlap > 1e-6) else 1
                    # Deterministic ordering: physical intersection, larger Z overlap,
                    # shorter vertical distance, shorter probe, then host ElementId.
                    rank = (
                        physical_rank,
                        round(-overlap, 9),
                        round(abs(along), 9),
                        round(probe_distance, 9),
                        cache_item['host_id']
                    )
                    if best is None or rank < best['rank']:
                        best = {
                            'rank': rank,
                            'distance': abs(along),
                            'thickness': thickness,
                            'center': host_center,
                            'source_center': source_center,
                            'probe': probe_distance,
                            'overlap': overlap,
                            'fallback': False
                        }

    # Fallback is intentionally weak and is used only if geometry rays found nothing.
    if best is None and is_floor:
        thickness = floor_type_thickness(host)
        try:
            bbox = host.get_BoundingBox(None)
        except Exception:
            bbox = None
        if thickness and bbox:
            z = (bbox.Min.Z + bbox.Max.Z) * 0.5
            if abs(z - point.Z) <= FLOOR_Z:
                source_center = XYZ(point.X, point.Y, z)
                best = {
                    'rank': (2, 0.0, round(abs(z - point.Z), 9), 0.0,
                             cache_item['host_id']),
                    'distance': abs(z - point.Z),
                    'thickness': thickness,
                    'center': source_transform.OfPoint(source_center),
                    'source_center': source_center,
                    'probe': 0.0,
                    'overlap': 0.0,
                    'fallback': True
                }

    if not best:
        return None

    offset, method = host_offset_at_location(
        host, best['source_center'], best['thickness']
    )
    best['host_level_offset'] = offset
    best['offset_method'] = method
    return best


def freeze_match(cache_item, hit):
    """Copy all numeric results so the update phase never re-runs geometry."""
    center = hit['center']
    return {
        'host': cache_item['host'],
        'host_id': cache_item['host_id'],
        'source': cache_item['source'],
        'data': {
            'rank': hit['rank'],
            'distance': float(hit['distance']),
            'thickness': float(hit['thickness']),
            'center': XYZ(center.X, center.Y, center.Z),
            'probe': float(hit['probe']),
            'overlap': float(hit['overlap']),
            'fallback': bool(hit['fallback']),
            'host_level_offset': float(hit['host_level_offset']),
            'offset_method': txt(hit['offset_method'])
        }
    }


def find_host(family_instance, host_cache):
    sleeve_data = sleeve_solid_data(family_instance)
    if not sleeve_data:
        return None, 'Sleeve has no valid physical Solid'

    best = None
    for cache_item in host_cache:
        hit = best_hit(cache_item, sleeve_data)
        if not hit:
            continue
        rank = hit['rank']
        if best is None or rank < best[0]:
            best = (rank, cache_item, hit)

    if not best:
        return None, 'No intersecting host'
    return freeze_match(best[1], best[2]), 'OK'


# -----------------------------------------------------------------------------
# Parameters and update
# -----------------------------------------------------------------------------

def writable_thickness_parameter(family_instance, names):
    for name in names:
        try:
            parameter = family_instance.LookupParameter(name)
            if (parameter and not parameter.IsReadOnly and
                    parameter.StorageType == StorageType.Double):
                return parameter, name
        except Exception:
            pass
    return None, None


def offset_parameter(family_instance):
    candidates = []
    try:
        candidates.append(family_instance.get_Parameter(
            BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM
        ))
    except Exception:
        pass
    for name in ('Offset from Host', 'Host Offset', 'Offset'):
        try:
            candidates.append(family_instance.LookupParameter(name))
        except Exception:
            pass
    for parameter in candidates:
        try:
            if (parameter and not parameter.IsReadOnly and
                    parameter.StorageType == StorageType.Double):
                return parameter
        except Exception:
            pass
    return None


def update_sleeve(family_instance, match, parameter_names,
                  update_thickness, update_center):
    used_name = ''
    data = match['data']
    if update_thickness:
        parameter, used_name = writable_thickness_parameter(
            family_instance, parameter_names
        )
        if not parameter:
            return False, 'No writable thickness parameter', used_name
        parameter.Set(data['thickness'])
    if update_center:
        parameter = offset_parameter(family_instance)
        if not parameter:
            return False, 'No writable Offset from Host parameter', used_name
        parameter.Set(data['host_level_offset'])
    return True, 'OK', used_name


# -----------------------------------------------------------------------------
# Window
# -----------------------------------------------------------------------------

class Win(object):
    def __init__(self, state):
        with open(XAML, 'r') as stream:
            self.w = XamlReader.Parse(stream.read())
        self.s = defaults()
        self.s.update(state)
        self.req = None
        self.action = None
        self.si = list(self.s.get('sleeve_ids', []))
        self.li = list(self.s.get('link_ids', []))
        self.ss = self.w.FindName('TxtSleeveSummary')
        self.sc = self.w.FindName('SleeveContainer')
        self.ls = self.w.FindName('TxtLinkSummary')
        self.lc = self.w.FindName('LinkContainer')
        self.val = self.w.FindName('TxtValidation')
        handlers = (
            ('BtnPickSleeves', self.pick_sleeves),
            ('BtnClearSleeves', self.clear_sleeves),
            ('BtnPickLinks', self.pick_links),
            ('BtnClearLinks', self.clear_links),
            ('BtnRun', self.run),
            ('BtnCancel', self.cancel)
        )
        for name, handler in handlers:
            self.w.FindName(name).Click += handler
        self.apply()
        self.refresh()

    def apply(self):
        mapping = (
            ('ChkWall', 'scan_wall'),
            ('ChkFloor', 'scan_floor'),
            ('ChkBeam', 'scan_beam'),
            ('ChkIncludeCurrent', 'include_current')
        )
        for control, key in mapping:
            self.w.FindName(control).IsChecked = bool(self.s.get(key, True))
        self.w.FindName('TxtThicknessParameters').Text = txt(
            self.s.get('parameter_text', '')
        )
        mode = self.s.get('action_mode', 'both')
        self.w.FindName('RbBoth').IsChecked = mode == 'both'
        self.w.FindName('RbThicknessOnly').IsChecked = mode == 'thickness_only'
        self.w.FindName('RbCenterOnly').IsChecked = mode == 'center_only'

    def state(self):
        if self.w.FindName('RbThicknessOnly').IsChecked:
            mode = 'thickness_only'
        elif self.w.FindName('RbCenterOnly').IsChecked:
            mode = 'center_only'
        else:
            mode = 'both'
        return {
            'document_key': doc_key(),
            'sleeve_ids': self.si,
            'link_ids': self.li,
            'scan_wall': bool(self.w.FindName('ChkWall').IsChecked),
            'scan_floor': bool(self.w.FindName('ChkFloor').IsChecked),
            'scan_beam': bool(self.w.FindName('ChkBeam').IsChecked),
            'include_current': bool(self.w.FindName('ChkIncludeCurrent').IsChecked),
            'parameter_text': txt(self.w.FindName('TxtThicknessParameters').Text),
            'action_mode': mode
        }

    def add_line(self, container, value):
        line = TextBlock()
        line.Text = value
        line.Foreground = Brushes.DarkSlateGray
        line.Margin = Windows.Thickness(2)
        container.Children.Add(line)

    def refresh(self):
        self.sc.Children.Clear()
        valid = []
        for value in self.si:
            element = doc.GetElement(eid(value))
            if isinstance(element, FamilyInstance):
                valid.append(value)
                self.add_line(self.sc, 'ID {0} | {1}'.format(value, typename(element)))
        self.si = valid
        self.ss.Text = '{0} sleeves selected'.format(len(valid))
        if not valid:
            self.add_line(self.sc, 'No sleeves selected.')

        self.lc.Children.Clear()
        valid = []
        for value in self.li:
            element = doc.GetElement(eid(value))
            if isinstance(element, RevitLinkInstance) and element.GetLinkDocument():
                valid.append(value)
                self.add_line(self.lc, 'ID {0} | {1}'.format(value, txt(element.Name)))
        self.li = valid
        self.ls.Text = 'Current model | {0} Revit Links'.format(len(valid))
        if not valid:
            self.add_line(self.lc, 'No links selected. Current model only.')

    def close_request(self, request):
        save_state(self.state())
        self.req = request
        self.w.DialogResult = False
        self.w.Close()

    def pick_sleeves(self, sender, args):
        self.close_request('sleeves')

    def pick_links(self, sender, args):
        self.close_request('links')

    def clear_sleeves(self, sender, args):
        self.si = []
        self.refresh()
        save_state(self.state())

    def clear_links(self, sender, args):
        self.li = []
        self.refresh()
        save_state(self.state())

    def run(self, sender, args):
        state = self.state()
        names = [value.strip() for value in
                 state['parameter_text'].split(';') if value.strip()]
        if not self.si:
            self.val.Text = 'Select at least one sleeve.'
            return
        if not names and state['action_mode'] != 'center_only':
            self.val.Text = 'Enter a thickness parameter name.'
            return
        state['parameter_names'] = names
        state['do_thickness'] = state['action_mode'] != 'center_only'
        state['do_center'] = state['action_mode'] != 'thickness_only'
        self.action = state
        save_state(state)
        self.w.DialogResult = True
        self.w.Close()

    def cancel(self, sender, args):
        save_state(self.state())
        self.w.DialogResult = False
        self.w.Close()


def pick(selection_filter, prompt):
    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element, selection_filter, prompt
        )
        return [iid(reference.ElementId) for reference in references]
    except OperationCanceledException:
        return None


def show_results(rows, cancelled):
    output.print_md('## Sleeve Host Updater Results')
    if cancelled:
        output.print_md('**Cancelled. No sleeve was modified.**')
    headers = [
        'Sleeve ID', 'Status', 'Message', 'Host ID', 'Host Category',
        'Source', 'Thickness', 'Offset', 'Z overlap', 'Probe', 'Fallback'
    ]
    try:
        output.print_table(table_data=rows, columns=headers)
    except Exception:
        for row in rows:
            output.print_md(' | '.join([txt(value) for value in row]))


def main():
    state = load_state()
    preselected = [
        iid(value) for value in uidoc.Selection.GetElementIds()
        if isinstance(doc.GetElement(value), FamilyInstance)
    ]
    if preselected:
        state['sleeve_ids'] = preselected

    while True:
        window = Win(state)
        accepted = window.w.ShowDialog()
        state = window.state()
        if window.req == 'sleeves':
            values = pick(SleeveFilter(), 'Select sleeves, then Finish')
            if values is not None:
                state['sleeve_ids'] = values
                save_state(state)
            continue
        if window.req == 'links':
            values = pick(LinkFilter(), 'Select loaded Revit Links, then Finish')
            if values is not None:
                state['link_ids'] = values
                save_state(state)
            continue
        if accepted and window.action:
            action = window.action
            break
        return

    sources = []
    if action['include_current']:
        sources.append((doc, Transform.Identity, 'Current Model'))
    for value in action['link_ids']:
        link = doc.GetElement(eid(value))
        if isinstance(link, RevitLinkInstance) and link.GetLinkDocument():
            try:
                transform = link.GetTotalTransform()
            except Exception:
                transform = link.GetTransform()
            sources.append((link.GetLinkDocument(), transform, txt(link.Name)))

    # PHASE 1: cache hosts and analyze every sleeve before modifying the model.
    host_cache = build_host_cache(
        sources,
        action['scan_wall'], action['scan_floor'], action['scan_beam']
    )
    analysis = []
    rows = []
    cancelled = False
    total = len(action['sleeve_ids'])

    with forms.ProgressBar(
            title='Analyzing sleeve hosts: {value} of {max_value}',
            cancellable=True, step=1) as progress:
        for index, value in enumerate(action['sleeve_ids']):
            if progress.cancelled:
                cancelled = True
                break
            progress.update_progress(index + 1, total)
            family_instance = doc.GetElement(eid(value))
            if not isinstance(family_instance, FamilyInstance):
                analysis.append((value, None, None, 'Not a FamilyInstance'))
                continue
            match, message = find_host(family_instance, host_cache)
            analysis.append((value, family_instance, match, message))

    if cancelled:
        show_results(rows, True)
        return

    # PHASE 2: update only from frozen results. No geometry query occurs here.
    transaction = Transaction(doc, 'Update sleeve thickness and host offset')
    transaction.Start()
    try:
        with forms.ProgressBar(
                title='Updating sleeves: {value} of {max_value}',
                cancellable=True, step=1) as progress:
            for index, item in enumerate(analysis):
                if progress.cancelled:
                    cancelled = True
                    break
                progress.update_progress(index + 1, len(analysis))
                value, family_instance, match, message = item
                if family_instance is None or not match:
                    rows.append((value, 'Skipped', message, '', '', '', '', '', '', '', ''))
                    continue
                ok, update_message, parameter_name = update_sleeve(
                    family_instance, match, action['parameter_names'],
                    action['do_thickness'], action['do_center']
                )
                data = match['data']
                rows.append((
                    value,
                    'Success' if ok else 'Skipped',
                    update_message,
                    match['host_id'],
                    cname(match['host']),
                    match['source'],
                    '{0:.1f} mm'.format(data['thickness'] * MM),
                    '{0:.1f} mm'.format(data['host_level_offset'] * MM),
                    '{0:.1f} mm'.format(data['overlap'] * MM),
                    '{0:.1f} mm'.format(data['probe'] * MM),
                    txt(data['fallback'])
                ))

        if cancelled:
            transaction.RollBack()
        else:
            transaction.Commit()
    except Exception:
        try:
            if transaction.GetStatus() == TransactionStatus.Started:
                transaction.RollBack()
        except Exception:
            pass
        logger.error(traceback.format_exc())
        raise

    show_results(rows, cancelled)


if __name__ == '__main__':
    main()
