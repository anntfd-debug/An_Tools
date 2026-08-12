# -*- coding: utf-8 -*-
__title__ = "Pipe Missing\nSleeve"
__doc__ = """Kiem tra Sleeve cho Pipe/Pipe Accessory/Pipe Fitting.
Ho tro quet nhanh MEP-Sleeve hoac quet day du tung vi tri xuyen Wall/Floor.
Chi quet hinh hoc MEP nam trong Active View. Sleeve chi dung Solid 3D cua Family; khong dung Room Calculation Point, symbolic line hoac BoundingBox element.
"""

import clr
import math
import traceback

clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from System.Collections.Generic import List
from System.Drawing import Point, Size, Font, FontStyle
from System.Windows.Forms import (
    Form, Label, CheckBox, RadioButton, Button, NumericUpDown, GroupBox,
    DialogResult, FormStartPosition, FormBorderStyle, MessageBox,
    MessageBoxButtons, MessageBoxIcon
)

from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, BooleanOperationsType,
    BooleanOperationsUtils, BoundingBoxIntersectsFilter,
    ElementId, ElementMulticategoryFilter, FamilyInstance,
    FilteredElementCollector, GeometryInstance, LocationCurve,
    LocationPoint, Options, Outline, RevitLinkInstance, Solid,
    SolidCurveIntersectionOptions, SolidUtils, TemporaryViewMode,
    Transform, View3D, ViewPlan, PlanViewPlane, ViewDetailLevel,
    Wall, WallKind, XYZ
)
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

from pyrevit import forms, revit, script


uidoc = revit.uidoc
doc = revit.doc
active_view = doc.ActiveView
output = script.get_output()
config = script.get_config()

FT_PER_MM = 1.0 / 304.8
MM_PER_FT = 304.8
GEOM_EPS = 1.0e-9
MAX_REPORT_ROWS = 200
MAX_AUTO_VIEW_IDS = 500


def md_cell(value):
    """Lam sach noi dung de khong lam vo bang Markdown cua pyRevit Output."""
    try:
        text = unicode(value)
    except NameError:
        text = str(value)
    except Exception:
        text = u""
    return text.replace(u"|", u"\\|").replace(u"\r", u" ").replace(u"\n", u" ")


def get_cached_output_link(element, link_cache):
    """Tao link pyRevit mot lan cho moi ElementId de giam thoi gian render."""
    key = id_value(element.Id)
    if key in link_cache:
        return link_cache[key]
    try:
        value = output.linkify(element.Id)
    except Exception:
        value = str(key)
    link_cache[key] = value
    return value


def mm_to_ft(value):
    return float(value) * FT_PER_MM


def ft_to_mm(value):
    return float(value) * MM_PER_FT


def id_value(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        return int(element_id.IntegerValue)


def format_unique_element_ids(element_ids):
    """Tra ve toan bo ElementId duy nhat, cach nhau chi boi dau phay.

    Giu nguyen thu tu phat hien de nguoi dung co the copy truc tiep danh sach
    vao Select by ID. Ham khong gioi han so luong ID va khong phu thuoc vao
    MAX_REPORT_ROWS cua bang chi tiet.
    """
    values = []
    seen = set()
    for element_id in element_ids:
        try:
            value = id_value(element_id)
        except Exception:
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(u"{}".format(value))
    return u",".join(values)


def get_cfg(name, default_value):
    try:
        return getattr(config, name)
    except Exception:
        return default_value


def set_cfg(name, value):
    try:
        setattr(config, name, value)
    except Exception:
        pass


def make_bic_list(categories):
    result = List[BuiltInCategory]()
    for category in categories:
        result.Add(category)
    return result


def make_element_id_list(ids):
    result = List[ElementId]()
    for element_id in ids:
        result.Add(element_id)
    return result


def xyz_min(a, b):
    return XYZ(min(a.X, b.X), min(a.Y, b.Y), min(a.Z, b.Z))


def xyz_max(a, b):
    return XYZ(max(a.X, b.X), max(a.Y, b.Y), max(a.Z, b.Z))


def expand_bounds(min_pt, max_pt, distance):
    delta = XYZ(distance, distance, distance)
    return min_pt - delta, max_pt + delta


def bounds_overlap(min_a, max_a, min_b, max_b, tolerance=0.0):
    return not (
        max_a.X < min_b.X - tolerance or min_a.X > max_b.X + tolerance or
        max_a.Y < min_b.Y - tolerance or min_a.Y > max_b.Y + tolerance or
        max_a.Z < min_b.Z - tolerance or min_a.Z > max_b.Z + tolerance
    )


def point_in_bounds(point, min_pt, max_pt, tolerance=0.0):
    return (
        min_pt.X - tolerance <= point.X <= max_pt.X + tolerance and
        min_pt.Y - tolerance <= point.Y <= max_pt.Y + tolerance and
        min_pt.Z - tolerance <= point.Z <= max_pt.Z + tolerance
    )


def bbox_corners(min_pt, max_pt):
    return [
        XYZ(x, y, z)
        for x in (min_pt.X, max_pt.X)
        for y in (min_pt.Y, max_pt.Y)
        for z in (min_pt.Z, max_pt.Z)
    ]


def transform_bounds(min_pt, max_pt, transform):
    new_min = None
    new_max = None
    for point in bbox_corners(min_pt, max_pt):
        transformed = transform.OfPoint(point)
        if new_min is None:
            new_min = transformed
            new_max = transformed
        else:
            new_min = xyz_min(new_min, transformed)
            new_max = xyz_max(new_max, transformed)
    return new_min, new_max


def bounding_box_to_model_bounds(bbox):
    """Chuyen BoundingBoxXYZ sang AABB trong toa do model."""
    if bbox is None:
        return None

    bbox_transform = None
    try:
        bbox_transform = bbox.Transform
    except Exception:
        bbox_transform = None

    min_pt = None
    max_pt = None
    for point in bbox_corners(bbox.Min, bbox.Max):
        model_point = point
        if bbox_transform is not None:
            try:
                model_point = bbox_transform.OfPoint(point)
            except Exception:
                model_point = point

        if min_pt is None:
            min_pt = model_point
            max_pt = model_point
        else:
            min_pt = xyz_min(min_pt, model_point)
            max_pt = xyz_max(max_pt, model_point)

    if min_pt is None:
        return None
    return min_pt, max_pt


def get_element_bounds(element, transform=None, view=None, allow_model_fallback=True):
    """Lay bounds; co the cam fallback sang BoundingBox toan model."""
    bbox = None
    try:
        bbox = element.get_BoundingBox(view)
    except Exception:
        bbox = None

    if bbox is None and view is not None and allow_model_fallback:
        try:
            bbox = element.get_BoundingBox(None)
        except Exception:
            bbox = None

    bounds = bounding_box_to_model_bounds(bbox)
    if bounds is None:
        return None

    min_pt, max_pt = bounds
    if transform is not None:
        return transform_bounds(min_pt, max_pt, transform)
    return min_pt, max_pt


def is_element_hidden_by_view(element, view):
    try:
        if element.IsHidden(view):
            return True
    except Exception:
        pass

    try:
        category = element.Category
        if category is not None and view.GetCategoryHidden(category.Id):
            return True
    except Exception:
        pass

    try:
        if view.IsTemporaryHideIsolateActive():
            visible_method = getattr(view, "IsElementVisibleInTemporaryViewMode", None)
            if visible_method is not None:
                if not visible_method(
                    TemporaryViewMode.TemporaryHideIsolate,
                    element.Id
                ):
                    return True
    except Exception:
        pass

    return False


def bounds_overlap_2d(min_a, max_a, min_b, max_b, tolerance=0.0):
    """Kiem tra giao nhau tren mat phang X-Y, khong dung cao do Z."""
    return not (
        max_a.X < min_b.X - tolerance or min_a.X > max_b.X + tolerance or
        max_a.Y < min_b.Y - tolerance or min_a.Y > max_b.Y + tolerance
    )


def get_plan_view_z_range(view):
    """Lay khoang Z thuc cua Plan View tu View Range.

    Dung Top, Bottom va View Depth de mot Pipe dung chi duoc quet tai phan
    hinh hoc dang nam trong pham vi hien thi cua mat bang hien hanh.
    """
    try:
        if not isinstance(view, ViewPlan):
            return None
    except Exception:
        return None

    try:
        view_range = view.GetViewRange()
    except Exception:
        return None

    elevations = []
    planes = (
        PlanViewPlane.TopClipPlane,
        PlanViewPlane.BottomClipPlane,
        PlanViewPlane.ViewDepthPlane
    )

    for plane in planes:
        try:
            level_id = view_range.GetLevelId(plane)
            offset = view_range.GetOffset(plane)
            level = doc.GetElement(level_id)
            if level is not None:
                elevations.append(float(level.Elevation) + float(offset))
                continue
        except Exception:
            pass

        # Fallback cho mot so template/view co LevelId dac biet.
        try:
            level = view.GenLevel
            if level is not None:
                offset = view_range.GetOffset(plane)
                elevations.append(float(level.Elevation) + float(offset))
        except Exception:
            pass

    if not elevations:
        return None
    return min(elevations), max(elevations)


def get_active_view_box(view):
    """Tra ve (box, compare_xyz).

    - 3D co Section Box: cat theo X-Y-Z cua Section Box.
    - Plan: CropBox chi cat X-Y; Z duoc cat rieng bang View Range.
    - Section/Elevation: CropBox cat X-Y-Z, trong do Z la chieu sau view.
    """
    try:
        if isinstance(view, View3D):
            if not view.IsSectionBoxActive:
                return None, False
            return view.GetSectionBox(), True
    except Exception:
        return None, False

    try:
        if not view.CropBoxActive:
            return None, False
        crop_box = view.CropBox
    except Exception:
        return None, False

    try:
        compare_xyz = not isinstance(view, ViewPlan)
    except Exception:
        compare_xyz = True
    return crop_box, compare_xyz


def point_in_active_view_geometry(view, point, tolerance=1.0e-6):
    """Kiem tra mot diem hinh hoc co thuc su nam trong Active View."""
    if point is None:
        return False

    view_box, compare_xyz = get_active_view_box(view)
    if view_box is not None:
        try:
            local_point = view_box.Transform.Inverse.OfPoint(point)
            if compare_xyz:
                if not point_in_bounds(
                    local_point,
                    view_box.Min,
                    view_box.Max,
                    tolerance
                ):
                    return False
            else:
                if not (
                    view_box.Min.X - tolerance <= local_point.X <= view_box.Max.X + tolerance and
                    view_box.Min.Y - tolerance <= local_point.Y <= view_box.Max.Y + tolerance
                ):
                    return False
        except Exception:
            # Neu khong doc duoc transform thi de collector(view.Id) lam fallback.
            pass

    z_range = get_plan_view_z_range(view)
    if z_range is not None:
        if point.Z < z_range[0] - tolerance or point.Z > z_range[1] + tolerance:
            return False

    return True


def clip_bounds_to_active_view(view, min_pt, max_pt, tolerance=1.0e-6):
    """Cat AABB hinh hoc theo Crop/Section Box va View Range cua Active View.

    Ket qua duoc dung cho SpatialIndex va candidate search. Moi diem giao cat
    van duoc kiem tra lai bang point_in_active_view_geometry() de tranh sai so
    do AABB khi CropBox bi xoay.
    """
    clipped_min = min_pt
    clipped_max = max_pt

    view_box, compare_xyz = get_active_view_box(view)
    if view_box is not None:
        try:
            inverse = view_box.Transform.Inverse
            local_min = None
            local_max = None
            for point in bbox_corners(min_pt, max_pt):
                local_point = inverse.OfPoint(point)
                if local_min is None:
                    local_min = local_point
                    local_max = local_point
                else:
                    local_min = xyz_min(local_min, local_point)
                    local_max = xyz_max(local_max, local_point)

            x_min = max(local_min.X, view_box.Min.X)
            x_max = min(local_max.X, view_box.Max.X)
            y_min = max(local_min.Y, view_box.Min.Y)
            y_max = min(local_max.Y, view_box.Max.Y)
            if x_min > x_max + tolerance or y_min > y_max + tolerance:
                return None

            if compare_xyz:
                z_min = max(local_min.Z, view_box.Min.Z)
                z_max = min(local_max.Z, view_box.Max.Z)
                if z_min > z_max + tolerance:
                    return None
            else:
                z_min = local_min.Z
                z_max = local_max.Z

            model_min = None
            model_max = None
            for local_point in bbox_corners(
                XYZ(x_min, y_min, z_min),
                XYZ(x_max, y_max, z_max)
            ):
                model_point = view_box.Transform.OfPoint(local_point)
                if model_min is None:
                    model_min = model_point
                    model_max = model_point
                else:
                    model_min = xyz_min(model_min, model_point)
                    model_max = xyz_max(model_max, model_point)

            clipped_min = model_min
            clipped_max = model_max
        except Exception:
            clipped_min = min_pt
            clipped_max = max_pt

    z_range = get_plan_view_z_range(view)
    if z_range is not None:
        z_min = max(clipped_min.Z, z_range[0])
        z_max = min(clipped_max.Z, z_range[1])
        if z_min > z_max + tolerance:
            return None
        clipped_min = XYZ(clipped_min.X, clipped_min.Y, z_min)
        clipped_max = XYZ(clipped_max.X, clipped_max.Y, z_max)

    return clipped_min, clipped_max


def bounds_overlap_active_crop(view, min_pt, max_pt, tolerance=1.0e-6):
    return clip_bounds_to_active_view(view, min_pt, max_pt, tolerance) is not None


def curve_geometry_bounds(curve):
    if curve is None:
        return None
    try:
        points = list(curve.Tessellate())
    except Exception:
        points = []
    if not points:
        try:
            points = [curve.GetEndPoint(0), curve.GetEndPoint(1)]
        except Exception:
            return None

    min_pt = points[0]
    max_pt = points[0]
    for point in points[1:]:
        min_pt = xyz_min(min_pt, point)
        max_pt = xyz_max(max_pt, point)
    return min_pt, max_pt


def get_active_view_member_bounds(element):
    """Lay bounds tin cay cho element DA DUOC collector theo Active View lay ra.

    Fallback BoundingBox(None) o day la an toan: no chi cung cap hinh hoc de tinh
    giao cat/crop, khong the dua them element ngoai view vao danh sach vi ID cua
    element da den tu FilteredElementCollector(doc, active_view.Id).
    """
    bounds = get_element_bounds(
        element,
        view=active_view,
        allow_model_fallback=True
    )
    if bounds is not None:
        return bounds

    try:
        location = element.Location
        if isinstance(location, LocationCurve):
            bounds = curve_geometry_bounds(location.Curve)
            if bounds is not None:
                return expand_bounds(bounds[0], bounds[1], mm_to_ft(1.0))
    except Exception:
        pass

    solids = get_element_solids(element, include_non_visible=False)
    bounds = solid_bounds(solids)
    if bounds is not None:
        return bounds

    point = get_location_point(element)
    if point is not None:
        return expand_bounds(point, point, mm_to_ft(1.0))

    return None


def is_strictly_in_active_view(element, bounds=None):
    """Xac nhan element thuoc pham vi hien thi cua Active View.

    Khong bat buoc BoundingBox(view) phai ton tai, vi mot so MEP van hien thi
    nhung Revit tra ve None. Chieu Z cua CropBox 2D cung khong duoc dung de loc.
    """
    if element is None:
        return False

    if is_element_hidden_by_view(element, active_view):
        return False

    if bounds is None:
        bounds = get_active_view_member_bounds(element)
    if bounds is None:
        return False

    min_pt, max_pt = bounds
    return bounds_overlap_active_crop(active_view, min_pt, max_pt)


def collect_geometry_solids(geometry_element, result):
    if geometry_element is None:
        return

    for geometry_object in geometry_element:
        if isinstance(geometry_object, Solid):
            try:
                if geometry_object.Faces.Size > 0 and geometry_object.Volume > GEOM_EPS:
                    result.append(geometry_object)
            except Exception:
                pass
        elif isinstance(geometry_object, GeometryInstance):
            try:
                collect_geometry_solids(geometry_object.GetInstanceGeometry(), result)
            except Exception:
                try:
                    collect_geometry_solids(geometry_object.GetSymbolGeometry(), result)
                except Exception:
                    pass


def get_element_solids(element, transform=None, include_non_visible=False, view=None):
    """Chi lay Solid 3D co Volume.

    Khong lay BoundingBox cua element, symbolic line, reference geometry hay
    Room Calculation Point. Khi view duoc truyen vao, uu tien geometry theo
    chinh view do.
    """
    options = Options()
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = include_non_visible

    if view is not None:
        try:
            options.View = view
        except Exception:
            try:
                options.DetailLevel = ViewDetailLevel.Fine
            except Exception:
                pass
    else:
        try:
            options.DetailLevel = ViewDetailLevel.Fine
        except Exception:
            pass

    raw_solids = []
    try:
        geometry = element.get_Geometry(options)
        collect_geometry_solids(geometry, raw_solids)
    except Exception:
        return []

    if transform is None:
        return raw_solids

    transformed_solids = []
    for solid in raw_solids:
        try:
            transformed_solids.append(SolidUtils.CreateTransformed(solid, transform))
        except Exception:
            # Link mirrored/non-conformal hiem gap co the khong transform duoc solid.
            pass
    return transformed_solids


def solid_bounds(solids):
    min_pt = None
    max_pt = None

    for solid in solids:
        # Tessellate Edge de lay BoundingBox cua Solid that, bo qua symbolic line,
        # Room Calculation Point va cac duong tham chieu trong Family.
        try:
            for edge in solid.Edges:
                for point in edge.Tessellate():
                    if min_pt is None:
                        min_pt = point
                        max_pt = point
                    else:
                        min_pt = xyz_min(min_pt, point)
                        max_pt = xyz_max(max_pt, point)
        except Exception:
            pass

    if min_pt is None:
        return None
    return min_pt, max_pt


def get_location_point(element):
    try:
        location = element.Location
        if isinstance(location, LocationPoint):
            return location.Point
        if isinstance(location, LocationCurve):
            return location.Curve.Evaluate(0.5, True)
    except Exception:
        pass
    return None


def get_mark(element):
    try:
        parameter = element.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
        if parameter and parameter.HasValue:
            value = parameter.AsString()
            if value:
                return value
    except Exception:
        pass
    return u""


def get_type_name_from_element_type(element_type):
    """Doc ten Type on dinh tren cac phien ban Revit/pyRevit.

    Tranh doc truc tiep FamilySymbol.Name vi mot so bo Revit + IronPython
    co the nem exception khi truy cap .NET property nay.
    """
    if element_type is None:
        return u""

    try:
        parameter = element_type.get_Parameter(
            BuiltInParameter.SYMBOL_NAME_PARAM
        )
        if parameter is not None:
            value = parameter.AsString()
            if value:
                return value
    except Exception:
        pass

    try:
        parameter = element_type.LookupParameter(u"Type Name")
        if parameter is not None:
            value = parameter.AsString()
            if value:
                return value
    except Exception:
        pass

    try:
        return element_type.Name
    except Exception:
        return u""


def get_family_name_from_symbol(symbol):
    if symbol is None:
        return u""

    try:
        family = symbol.Family
        if family is not None:
            return family.Name
    except Exception:
        pass

    try:
        parameter = symbol.get_Parameter(
            BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM
        )
        if parameter is not None:
            value = parameter.AsString()
            if value:
                return value
    except Exception:
        pass

    return u""


def get_family_type_name(element):
    try:
        element_type = doc.GetElement(element.GetTypeId())
    except Exception:
        element_type = None

    family_name = get_family_name_from_symbol(element_type)
    type_name = get_type_name_from_element_type(element_type)

    if family_name and type_name:
        return u"{} : {}".format(family_name, type_name)
    if type_name:
        return type_name
    if family_name:
        return family_name

    try:
        return element.Name
    except Exception:
        return u""


class ScanOptionsForm(Form):
    def __init__(self):
        self.Text = u"Kiểm tra Sleeve cho Pipe / PA / PF"
        self.Size = Size(540, 770)
        self.MinimumSize = Size(540, 770)
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.StartPosition = FormStartPosition.CenterScreen
        self.TopMost = True
        self.Font = Font(u"Segoe UI", 9)

        title = Label()
        title.Text = u"PHẠM VI VÀ ĐỘ CHÍNH XÁC QUÉT"
        title.Font = Font(u"Segoe UI", 11, FontStyle.Bold)
        title.AutoSize = True
        title.Location = Point(22, 16)
        self.Controls.Add(title)

        structure_group = GroupBox()
        structure_group.Text = u"1. Loại kết cấu cần kiểm tra"
        structure_group.Location = Point(20, 50)
        structure_group.Size = Size(485, 82)
        self.Controls.Add(structure_group)
        self.structure_group = structure_group

        self.cb_wall = CheckBox()
        self.cb_wall.Text = u"Wall"
        self.cb_wall.Checked = bool(get_cfg("scan_wall", True))
        self.cb_wall.AutoSize = True
        self.cb_wall.Location = Point(24, 35)
        structure_group.Controls.Add(self.cb_wall)

        self.cb_floor = CheckBox()
        self.cb_floor.Text = u"Floor"
        self.cb_floor.Checked = bool(get_cfg("scan_floor", True))
        self.cb_floor.AutoSize = True
        self.cb_floor.Location = Point(150, 35)
        structure_group.Controls.Add(self.cb_floor)

        quick_hint = Label()
        quick_hint.Text = u"Bỏ chọn cả hai = Quét nhanh MEP ↔ Sleeve (không đọc kết cấu)."
        quick_hint.AutoSize = True
        quick_hint.Location = Point(24, 57)
        structure_group.Controls.Add(quick_hint)
        self.quick_hint = quick_hint

        source_group = GroupBox()
        source_group.Text = u"2. Nguồn Wall/Floor"
        source_group.Location = Point(20, 143)
        source_group.Size = Size(485, 85)
        self.Controls.Add(source_group)
        self.source_group = source_group

        self.cb_host = CheckBox()
        self.cb_host.Text = u"Model hiện tại"
        self.cb_host.Checked = bool(get_cfg("scan_host", True))
        self.cb_host.AutoSize = True
        self.cb_host.Location = Point(24, 35)
        source_group.Controls.Add(self.cb_host)

        self.cb_links = CheckBox()
        self.cb_links.Text = u"Tất cả Revit Link đang load"
        self.cb_links.Checked = bool(get_cfg("scan_links", True))
        self.cb_links.AutoSize = True
        self.cb_links.Location = Point(180, 35)
        source_group.Controls.Add(self.cb_links)

        target_group = GroupBox()
        target_group.Text = u"3. Đối tượng MEP cần kiểm tra (toàn bộ Active View)"
        target_group.Location = Point(20, 239)
        target_group.Size = Size(485, 88)
        self.Controls.Add(target_group)

        self.cb_pipe = CheckBox()
        self.cb_pipe.Text = u"Pipe"
        self.cb_pipe.Checked = bool(get_cfg("scan_pipe", True))
        self.cb_pipe.AutoSize = True
        self.cb_pipe.Location = Point(24, 36)
        target_group.Controls.Add(self.cb_pipe)

        self.cb_accessory = CheckBox()
        self.cb_accessory.Text = u"Pipe Accessory"
        self.cb_accessory.Checked = bool(get_cfg("scan_accessory", True))
        self.cb_accessory.AutoSize = True
        self.cb_accessory.Location = Point(130, 36)
        target_group.Controls.Add(self.cb_accessory)

        self.cb_fitting = CheckBox()
        self.cb_fitting.Text = u"Pipe Fitting"
        self.cb_fitting.Checked = bool(get_cfg("scan_fitting", True))
        self.cb_fitting.AutoSize = True
        self.cb_fitting.Location = Point(300, 36)
        target_group.Controls.Add(self.cb_fitting)

        sleeve_scope_group = GroupBox()
        sleeve_scope_group.Text = u"4. Phạm vi Sleeve dùng để đối chiếu"
        sleeve_scope_group.Location = Point(20, 338)
        sleeve_scope_group.Size = Size(485, 86)
        self.Controls.Add(sleeve_scope_group)
        self.sleeve_scope_group = sleeve_scope_group

        saved_scope = get_cfg("sleeve_scope", "active_view")

        self.rb_sleeve_view = RadioButton()
        self.rb_sleeve_view.Text = u"Chỉ Sleeve trong Active View"
        self.rb_sleeve_view.Checked = saved_scope != "all_model"
        self.rb_sleeve_view.AutoSize = True
        self.rb_sleeve_view.Location = Point(24, 31)
        sleeve_scope_group.Controls.Add(self.rb_sleeve_view)

        self.rb_sleeve_model = RadioButton()
        self.rb_sleeve_model.Text = u"Tất cả Sleeve trong Current Model"
        self.rb_sleeve_model.Checked = saved_scope == "all_model"
        self.rb_sleeve_model.AutoSize = True
        self.rb_sleeve_model.Location = Point(250, 31)
        sleeve_scope_group.Controls.Add(self.rb_sleeve_model)

        sleeve_scope_hint = Label()
        sleeve_scope_hint.Text = (
            u"Active View: lấy theo tập phần tử của view và giao crop 2D; không lọc theo Z. "
            u"All Model: MEP vẫn ở Active View, Sleeve lấy toàn model."
        )
        sleeve_scope_hint.AutoSize = False
        sleeve_scope_hint.Size = Size(440, 34)
        sleeve_scope_hint.Location = Point(24, 52)
        sleeve_scope_group.Controls.Add(sleeve_scope_hint)

        tolerance_group = GroupBox()
        tolerance_group.Text = u"5. Dung sai"
        tolerance_group.Location = Point(20, 435)
        tolerance_group.Size = Size(485, 135)
        self.Controls.Add(tolerance_group)

        label_match = Label()
        label_match.Text = u"Dung sai nhận sleeve quanh điểm xuyên (mm):"
        label_match.AutoSize = True
        label_match.Location = Point(22, 31)
        tolerance_group.Controls.Add(label_match)

        self.num_match = NumericUpDown()
        self.num_match.Minimum = 0
        self.num_match.Maximum = 1000
        self.num_match.DecimalPlaces = 0
        self.num_match.Increment = 5
        self.num_match.Value = int(get_cfg("sleeve_tolerance_mm", 50))
        self.num_match.Size = Size(90, 24)
        self.num_match.Location = Point(345, 27)
        tolerance_group.Controls.Add(self.num_match)

        label_merge = Label()
        label_merge.Text = u"Gộp các điểm xuyên trùng/gần nhau (mm):"
        label_merge.AutoSize = True
        label_merge.Location = Point(22, 66)
        tolerance_group.Controls.Add(label_merge)

        self.num_merge = NumericUpDown()
        self.num_merge.Minimum = 0
        self.num_merge.Maximum = 500
        self.num_merge.DecimalPlaces = 0
        self.num_merge.Increment = 5
        self.num_merge.Value = int(get_cfg("merge_tolerance_mm", 30))
        self.num_merge.Size = Size(90, 24)
        self.num_merge.Location = Point(345, 62)
        tolerance_group.Controls.Add(self.num_merge)

        label_length = Label()
        label_length.Text = u"Chiều dài tim ống tối thiểu trong kết cấu (mm):"
        label_length.AutoSize = True
        label_length.Location = Point(22, 101)
        tolerance_group.Controls.Add(label_length)

        self.num_min_length = NumericUpDown()
        self.num_min_length.Minimum = 0
        self.num_min_length.Maximum = 500
        self.num_min_length.DecimalPlaces = 0
        self.num_min_length.Increment = 1
        self.num_min_length.Value = int(get_cfg("min_penetration_mm", 2))
        self.num_min_length.Size = Size(90, 24)
        self.num_min_length.Location = Point(345, 97)
        tolerance_group.Controls.Add(self.num_min_length)

        self.cb_one_to_one = CheckBox()
        self.cb_one_to_one.Text = u"Một sleeve chỉ khớp với một vị trí xuyên"
        self.cb_one_to_one.Checked = bool(get_cfg("one_to_one", True))
        self.cb_one_to_one.AutoSize = True
        self.cb_one_to_one.Location = Point(24, 588)
        self.Controls.Add(self.cb_one_to_one)

        self.cb_isolate = CheckBox()
        self.cb_isolate.Text = u"Temporary Isolate các đối tượng còn thiếu sleeve"
        self.cb_isolate.Checked = bool(get_cfg("isolate_result", True))
        self.cb_isolate.AutoSize = True
        self.cb_isolate.Location = Point(24, 621)
        self.Controls.Add(self.cb_isolate)

        self.btn_ok = Button()
        self.btn_ok.Text = u"Bắt đầu quét"
        self.btn_ok.Size = Size(130, 34)
        self.btn_ok.Location = Point(245, 665)
        self.btn_ok.DialogResult = DialogResult.OK
        self.Controls.Add(self.btn_ok)

        self.btn_cancel = Button()
        self.btn_cancel.Text = u"Hủy"
        self.btn_cancel.Size = Size(100, 34)
        self.btn_cancel.Location = Point(385, 665)
        self.btn_cancel.DialogResult = DialogResult.Cancel
        self.Controls.Add(self.btn_cancel)

        self.AcceptButton = self.btn_ok
        self.CancelButton = self.btn_cancel
        self.FormClosing += self._form_closing
        self.cb_wall.CheckedChanged += self._structure_mode_changed
        self.cb_floor.CheckedChanged += self._structure_mode_changed
        self._update_mode_state()

    def _structure_mode_changed(self, sender, event_args):
        self._update_mode_state()

    def _update_mode_state(self):
        full_scan = self.cb_wall.Checked or self.cb_floor.Checked

        # Khi bỏ chọn cả Wall và Floor, các lựa chọn liên quan tới kết cấu
        # không còn được sử dụng. Giữ nguyên trạng thái Checked để lần sau
        # bật lại Wall/Floor không phải thiết lập lại từ đầu.
        self.cb_host.Enabled = full_scan
        self.cb_links.Enabled = full_scan
        self.num_merge.Enabled = full_scan
        self.num_min_length.Enabled = full_scan
        self.cb_one_to_one.Enabled = full_scan

        if full_scan:
            self.btn_ok.Text = u"Bắt đầu quét"
        else:
            self.btn_ok.Text = u"Quét nhanh"

    def validate_options(self):
        full_scan = self.cb_wall.Checked or self.cb_floor.Checked

        if full_scan and not self.cb_host.Checked and not self.cb_links.Checked:
            MessageBox.Show(
                u"Phải tích Model hiện tại hoặc Revit Link.",
                u"Thiếu nguồn kết cấu",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning
            )
            return False

        if not self.cb_pipe.Checked and not self.cb_accessory.Checked and not self.cb_fitting.Checked:
            MessageBox.Show(
                u"Phải tích ít nhất một loại đối tượng MEP.",
                u"Thiếu đối tượng",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning
            )
            return False

        return True

    def _form_closing(self, sender, event_args):
        if self.DialogResult == DialogResult.OK and not self.validate_options():
            event_args.Cancel = True

    def values(self):
        return {
            "scan_wall": self.cb_wall.Checked,
            "scan_floor": self.cb_floor.Checked,
            "quick_mode": not self.cb_wall.Checked and not self.cb_floor.Checked,
            "scan_host": self.cb_host.Checked,
            "scan_links": self.cb_links.Checked,
            "scan_pipe": self.cb_pipe.Checked,
            "scan_accessory": self.cb_accessory.Checked,
            "scan_fitting": self.cb_fitting.Checked,
            "sleeve_scope": "all_model" if self.rb_sleeve_model.Checked else "active_view",
            "sleeve_tolerance": mm_to_ft(float(self.num_match.Value)),
            "merge_tolerance": mm_to_ft(float(self.num_merge.Value)),
            "min_penetration": mm_to_ft(float(self.num_min_length.Value)),
            "one_to_one": self.cb_one_to_one.Checked,
            "isolate_result": self.cb_isolate.Checked,
            "sleeve_tolerance_mm": int(self.num_match.Value),
            "merge_tolerance_mm": int(self.num_merge.Value),
            "min_penetration_mm": int(self.num_min_length.Value)
        }


class TargetRecord(object):
    def __init__(self, element, min_pt, max_pt, curve=None, solids=None):
        self.element = element
        self.id = element.Id
        self.key = id_value(element.Id)
        # Bounds nay da duoc cat theo Active View.
        self.min_pt = min_pt
        self.max_pt = max_pt
        self.curve = curve
        self.solids = solids

    def get_solids(self):
        if self.solids is None:
            self.solids = get_element_solids(
                self.element,
                include_non_visible=False,
                view=active_view
            )
        return self.solids


class HostRecord(object):
    def __init__(self, element, source_doc, transform, source_name, kind, min_pt, max_pt):
        self.element = element
        self.source_doc = source_doc
        self.transform = transform
        self.source_name = source_name
        self.kind = kind
        self.min_pt = min_pt
        self.max_pt = max_pt
        self.element_id = element.Id
        self.key = u"{}|{}|{}".format(source_name, kind, id_value(element.Id))
        self.solids = None

    def get_solids(self):
        if self.solids is None:
            self.solids = get_element_solids(self.element, self.transform)
        return self.solids


class SleeveRecord(object):
    def __init__(self, element, min_pt, max_pt, center):
        self.element = element
        self.id = element.Id
        self.key = id_value(element.Id)
        self.min_pt = min_pt
        self.max_pt = max_pt
        self.center = center


class PenetrationRecord(object):
    def __init__(self, target, host, point):
        self.target = target
        self.host = host
        self.point = point
        self.sleeve = None
        self.candidate_pairs = []


class SpatialIndex(object):
    def __init__(self, cell_size=20.0, max_cells_per_item=500):
        self.cell_size = float(cell_size)
        self.max_cells_per_item = int(max_cells_per_item)
        self.cells = {}
        self.large_items = []
        self.items = []

    def _index(self, value):
        return int(math.floor(value / self.cell_size))

    def _ranges(self, min_pt, max_pt):
        return (
            range(self._index(min_pt.X), self._index(max_pt.X) + 1),
            range(self._index(min_pt.Y), self._index(max_pt.Y) + 1),
            range(self._index(min_pt.Z), self._index(max_pt.Z) + 1)
        )

    def add(self, item, min_pt, max_pt):
        item_index = len(self.items)
        self.items.append(item)

        xr, yr, zr = self._ranges(min_pt, max_pt)
        cell_count = len(xr) * len(yr) * len(zr)
        if cell_count > self.max_cells_per_item:
            self.large_items.append(item_index)
            return

        for ix in xr:
            for iy in yr:
                for iz in zr:
                    key = (ix, iy, iz)
                    if key not in self.cells:
                        self.cells[key] = []
                    self.cells[key].append(item_index)

    def query(self, min_pt, max_pt):
        xr, yr, zr = self._ranges(min_pt, max_pt)
        found = set(self.large_items)

        # Bounding box truy van qua lon: duyet toan bo index de tranh tao hang van cell.
        query_cell_count = len(xr) * len(yr) * len(zr)
        if query_cell_count > self.max_cells_per_item * 4:
            found.update(range(len(self.items)))
        else:
            for ix in xr:
                for iy in yr:
                    for iz in zr:
                        for item_index in self.cells.get((ix, iy, iz), []):
                            found.add(item_index)

        result = []
        for item_index in found:
            item = self.items[item_index]
            if bounds_overlap(min_pt, max_pt, item.min_pt, item.max_pt):
                result.append(item)
        return result


def save_options(options):
    set_cfg("scan_wall", options["scan_wall"])
    set_cfg("scan_floor", options["scan_floor"])
    set_cfg("scan_host", options["scan_host"])
    set_cfg("scan_links", options["scan_links"])
    set_cfg("scan_pipe", options["scan_pipe"])
    set_cfg("scan_accessory", options["scan_accessory"])
    set_cfg("scan_fitting", options["scan_fitting"])
    set_cfg("sleeve_scope", options["sleeve_scope"])
    set_cfg("sleeve_tolerance_mm", options["sleeve_tolerance_mm"])
    set_cfg("merge_tolerance_mm", options["merge_tolerance_mm"])
    set_cfg("min_penetration_mm", options["min_penetration_mm"])
    set_cfg("one_to_one", options["one_to_one"])
    set_cfg("isolate_result", options["isolate_result"])
    try:
        script.save_config()
    except Exception:
        pass


def choose_sample_sleeve():
    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            u"Chọn một Family Sleeve mẫu"
        )
    except OperationCanceledException:
        return None, None

    sample = doc.GetElement(reference.ElementId)
    if not isinstance(sample, FamilyInstance):
        forms.alert(
            u"Đối tượng được chọn không phải FamilyInstance.\n\n"
            u"Hãy chọn một Generic Model được tạo từ Loadable Family, "
            u"không chọn DirectShape, Import hoặc phần tử của Revit Link."
        )
        return None, None

    # Doc Symbol qua GetTypeId thay cho sample.Symbol.Name. Cach nay on dinh
    # hon tren Revit 2025 va cac engine IronPython cua pyRevit.
    try:
        symbol = doc.GetElement(sample.GetTypeId())
    except Exception:
        symbol = None

    if symbol is None:
        forms.alert(u"Không thể đọc FamilySymbol từ đối tượng đã chọn.")
        return None, None

    try:
        family = symbol.Family
    except Exception:
        family = None

    family_name = get_family_name_from_symbol(symbol)
    type_name = get_type_name_from_element_type(symbol)

    if family is None or not family_name:
        forms.alert(
            u"Không thể xác định Family của đối tượng đã chọn.\n\n"
            u"Element Id: {}\nCategory: {}".format(
                id_value(sample.Id),
                sample.Category.Name if sample.Category else u""
            )
        )
        return None, None

    if not type_name:
        type_name = u"(Không đọc được tên Type)"

    confirm_result = MessageBox.Show(
        u"Bạn đang chọn Family:\n\n{}\n\nType được pick: {}\n\n"
        u"Tool sẽ dùng tất cả Type thuộc Family này để quét Sleeve.\n"
        u"Chọn Yes để tiếp tục hoặc No để hủy lệnh.".format(
            family_name,
            type_name
        ),
        u"Xác nhận Family Sleeve",
        MessageBoxButtons.YesNo,
        MessageBoxIcon.Question
    )
    if confirm_result != DialogResult.Yes:
        return None, None

    try:
        type_ids = set(
            id_value(type_id)
            for type_id in family.GetFamilySymbolIds()
        )
    except Exception:
        forms.alert(u"Không thể đọc danh sách Type của Family Sleeve đã chọn.")
        return None, None

    if not type_ids:
        forms.alert(u"Family đã chọn không có Family Type nào để quét.")
        return None, None

    return sample, type_ids


def collect_targets(options):
    """Thu thap chi hinh hoc MEP dang nam trong Active View.

    Pipe dung LocationCurve; Pipe Accessory/Fitting chi dung Solid 3D co Volume.
    Bounds duoc cat theo Crop/Section Box va Plan View Range truoc khi dua vao
    SpatialIndex. Khong dung BoundingBox element de tranh symbolic/reference
    geometry lam rong vung quet.
    """
    target_categories = []
    if options["scan_pipe"]:
        target_categories.append(BuiltInCategory.OST_PipeCurves)
    if options["scan_accessory"]:
        target_categories.append(BuiltInCategory.OST_PipeAccessory)
    if options["scan_fitting"]:
        target_categories.append(BuiltInCategory.OST_PipeFitting)

    category_filter = ElementMulticategoryFilter(make_bic_list(target_categories))
    elements = (
        FilteredElementCollector(doc, active_view.Id)
        .WherePasses(category_filter)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    records = []
    global_min = None
    global_max = None
    pipe_category_id = int(BuiltInCategory.OST_PipeCurves)

    for element in elements:
        if is_element_hidden_by_view(element, active_view):
            continue

        try:
            category_id = id_value(element.Category.Id) if element.Category else 0
        except Exception:
            category_id = 0

        curve = None
        solids = None
        raw_bounds = None

        if category_id == pipe_category_id:
            try:
                location = element.Location
                if isinstance(location, LocationCurve):
                    curve = location.Curve
                    raw_bounds = curve_geometry_bounds(curve)
                    if raw_bounds is not None:
                        # Mo rong rat nho de Pipe song song truc van co the query index.
                        raw_bounds = expand_bounds(
                            raw_bounds[0],
                            raw_bounds[1],
                            mm_to_ft(1.0)
                        )
            except Exception:
                curve = None
                raw_bounds = None
        else:
            solids = get_element_solids(
                element,
                include_non_visible=False,
                view=active_view
            )
            raw_bounds = solid_bounds(solids)

        if raw_bounds is None:
            continue

        clipped_bounds = clip_bounds_to_active_view(
            active_view,
            raw_bounds[0],
            raw_bounds[1]
        )
        if clipped_bounds is None:
            continue

        min_pt, max_pt = clipped_bounds
        record = TargetRecord(
            element,
            min_pt,
            max_pt,
            curve=curve,
            solids=solids
        )
        records.append(record)

        if global_min is None:
            global_min = min_pt
            global_max = max_pt
        else:
            global_min = xyz_min(global_min, min_pt)
            global_max = xyz_max(global_max, max_pt)

    return records, global_min, global_max



def collect_hosts_in_document(source_doc, transform, source_name, global_min, global_max, options):
    categories = []
    if options["scan_wall"]:
        categories.append(BuiltInCategory.OST_Walls)
    if options["scan_floor"]:
        categories.append(BuiltInCategory.OST_Floors)

    query_min = global_min
    query_max = global_max
    if transform is not None:
        try:
            query_min, query_max = transform_bounds(global_min, global_max, transform.Inverse)
        except Exception:
            return []

    query_min, query_max = expand_bounds(query_min, query_max, mm_to_ft(100.0))
    outline = Outline(query_min, query_max)
    bbox_filter = BoundingBoxIntersectsFilter(outline)
    category_filter = ElementMulticategoryFilter(make_bic_list(categories))

    try:
        elements = (
            FilteredElementCollector(source_doc)
            .WherePasses(category_filter)
            .WherePasses(bbox_filter)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        return []

    records = []
    for element in elements:
        if is_curtain_wall(element):
            continue

        bounds = get_element_bounds(element, transform=transform)
        if bounds is None:
            continue
        min_pt, max_pt = bounds
        if not bounds_overlap(global_min, global_max, min_pt, max_pt):
            continue

        category_id = id_value(element.Category.Id) if element.Category else 0
        wall_id = int(BuiltInCategory.OST_Walls)
        kind = u"Wall" if category_id == wall_id else u"Floor"
        records.append(HostRecord(
            element, source_doc, transform, source_name, kind, min_pt, max_pt
        ))

    return records


def collect_hosts(global_min, global_max, options):
    hosts = []

    if options["scan_host"]:
        hosts.extend(collect_hosts_in_document(
            doc, None, u"Current Model", global_min, global_max, options
        ))

    unloaded_links = []
    if options["scan_links"]:
        link_instances = (
            FilteredElementCollector(doc)
            .OfClass(RevitLinkInstance)
            .WhereElementIsNotElementType()
            .ToElements()
        )

        for link_instance in link_instances:
            link_doc = link_instance.GetLinkDocument()
            if link_doc is None:
                unloaded_links.append(link_instance.Name)
                continue

            try:
                transform = link_instance.GetTotalTransform()
            except Exception:
                transform = link_instance.GetTransform()

            source_name = u"Link: {}".format(link_instance.Name)
            hosts.extend(collect_hosts_in_document(
                link_doc, transform, source_name, global_min, global_max, options
            ))

    return hosts, unloaded_links


def collect_sleeves(type_id_values, global_min, global_max, fallback_tolerance, sleeve_scope):
    """Thu thap Sleeve chi bang Solid 3D cua Family da chon.

    Tuyet doi khong fallback sang element.get_BoundingBox(), LocationPoint hay
    Room Calculation Point. Instance khong doc duoc Solid co Volume se bi bo qua.
    """
    strict_active_scope = sleeve_scope == "active_view"
    if strict_active_scope:
        collector = FilteredElementCollector(doc, active_view.Id)
    else:
        collector = FilteredElementCollector(doc)

    instances = (
        collector
        .OfClass(FamilyInstance)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    sleeves = []
    for instance in instances:
        if id_value(instance.GetTypeId()) not in type_id_values:
            continue

        if strict_active_scope and is_element_hidden_by_view(instance, active_view):
            continue

        solids = get_element_solids(
            instance,
            include_non_visible=False,
            view=active_view if strict_active_scope else None
        )
        bounds = solid_bounds(solids)
        if bounds is None:
            # Khong dung LocationPoint/BBox lam fallback vi co the bi Room
            # Calculation Point hoac symbolic geometry lam sai vung quet.
            continue

        min_pt, max_pt = bounds
        if strict_active_scope:
            clipped_bounds = clip_bounds_to_active_view(
                active_view,
                min_pt,
                max_pt
            )
            if clipped_bounds is None:
                continue
            min_pt, max_pt = clipped_bounds

        if not bounds_overlap(
            global_min,
            global_max,
            min_pt,
            max_pt,
            fallback_tolerance
        ):
            continue

        # Center cung lay tu bounds cua Solid, khong lay insertion/calculation point.
        center = XYZ(
            (min_pt.X + max_pt.X) * 0.5,
            (min_pt.Y + max_pt.Y) * 0.5,
            (min_pt.Z + max_pt.Z) * 0.5
        )
        sleeves.append(SleeveRecord(instance, min_pt, max_pt, center))

    return sleeves



def merge_close_points(points, tolerance):
    if tolerance <= 0.0:
        return points

    merged = []
    for point in points:
        found_index = None
        for index, existing in enumerate(merged):
            if point.DistanceTo(existing) <= tolerance:
                found_index = index
                break
        if found_index is None:
            merged.append(point)
        else:
            existing = merged[found_index]
            merged[found_index] = XYZ(
                (existing.X + point.X) * 0.5,
                (existing.Y + point.Y) * 0.5,
                (existing.Z + point.Z) * 0.5
            )
    return merged


def average_points(points):
    if not points:
        return None
    count = float(len(points))
    return XYZ(
        sum(point.X for point in points) / count,
        sum(point.Y for point in points) / count,
        sum(point.Z for point in points) / count
    )


def curve_segment_point_in_view(segment, view):
    """Lay diem dai dien cua phan curve segment nam trong Active View."""
    inside_points = []

    # Lay nhieu mau de khong bo sot segment cat qua bien crop/view range.
    for index in range(11):
        try:
            point = segment.Evaluate(float(index) / 10.0, True)
        except Exception:
            continue
        if point_in_active_view_geometry(view, point, mm_to_ft(1.0)):
            inside_points.append(point)

    if not inside_points:
        try:
            for point in segment.Tessellate():
                if point_in_active_view_geometry(view, point, mm_to_ft(1.0)):
                    inside_points.append(point)
        except Exception:
            pass

    return average_points(inside_points)


def solid_point_in_view(solid, view):
    """Lay mot diem dai dien cua phan Solid nam trong Active View.

    Uu tien centroid, sau do kiem tra edge/mesh vertices. Cach nay tranh bo sot
    Pipe Accessory/Fitting nam cat ngang bien Crop/Section Box.
    """
    inside_points = []

    try:
        centroid = solid.ComputeCentroid()
        if point_in_active_view_geometry(view, centroid, mm_to_ft(1.0)):
            inside_points.append(centroid)
    except Exception:
        pass

    try:
        for edge in solid.Edges:
            for point in edge.Tessellate():
                if point_in_active_view_geometry(view, point, mm_to_ft(1.0)):
                    inside_points.append(point)
                    if len(inside_points) >= 40:
                        return average_points(inside_points)
    except Exception:
        pass

    try:
        for face in solid.Faces:
            mesh = face.Triangulate()
            for vertex_index in range(mesh.NumVertices):
                point = mesh.get_Vertex(vertex_index)
                if point_in_active_view_geometry(view, point, mm_to_ft(1.0)):
                    inside_points.append(point)
                    if len(inside_points) >= 80:
                        return average_points(inside_points)
    except Exception:
        pass

    return average_points(inside_points)


def intersect_curve_with_solids(curve, solids, min_penetration,
                                merge_tolerance, view=None):
    points = []
    intersection_options = SolidCurveIntersectionOptions()
    try:
        for solid in solids:
            try:
                result = solid.IntersectWithCurve(curve, intersection_options)
                for index in range(result.SegmentCount):
                    segment = result.GetCurveSegment(index)
                    try:
                        length = segment.Length
                    except Exception:
                        length = 0.0
                    if length + GEOM_EPS < min_penetration:
                        continue

                    if view is None:
                        point = segment.Evaluate(0.5, True)
                    else:
                        point = curve_segment_point_in_view(segment, view)
                    if point is not None:
                        points.append(point)
            except Exception:
                continue
    finally:
        try:
            intersection_options.Dispose()
        except Exception:
            pass

    return merge_close_points(points, merge_tolerance)


def intersect_solids(target_solids, host_solids, merge_tolerance, view=None):
    points = []
    for target_solid in target_solids:
        for host_solid in host_solids:
            try:
                intersection = BooleanOperationsUtils.ExecuteBooleanOperation(
                    target_solid,
                    host_solid,
                    BooleanOperationsType.Intersect
                )
                if intersection is None:
                    continue
                if intersection.Volume <= GEOM_EPS:
                    continue

                if view is None:
                    point = intersection.ComputeCentroid()
                else:
                    point = solid_point_in_view(intersection, view)
                if point is not None:
                    points.append(point)
            except Exception:
                continue
    return merge_close_points(points, merge_tolerance)


def find_penetrations(targets, host_index, options):
    penetrations = []
    unsupported_targets = []

    with forms.ProgressBar(
        title=u"Kiểm tra giao cắt {value}/{max_value}",
        cancellable=True
    ) as progress:
        total = len(targets)
        for index, target in enumerate(targets):
            if progress.cancelled:
                return None, unsupported_targets

            candidates = host_index.query(target.min_pt, target.max_pt)
            target_supported = target.curve is not None

            for host in candidates:
                if not bounds_overlap(target.min_pt, target.max_pt, host.min_pt, host.max_pt):
                    continue

                host_solids = host.get_solids()
                if not host_solids:
                    continue

                if target.curve is not None:
                    points = intersect_curve_with_solids(
                        target.curve,
                        host_solids,
                        options["min_penetration"],
                        options["merge_tolerance"],
                        view=active_view
                    )
                else:
                    target_solids = target.get_solids()
                    if not target_solids:
                        continue
                    target_supported = True
                    points = intersect_solids(
                        target_solids,
                        host_solids,
                        options["merge_tolerance"],
                        view=active_view
                    )

                for point in points:
                    # Curve/Solid cua element co the keo dai ra ngoai crop/view
                    # range. Chi ghi nhan diem giao nam trong Active View.
                    if not point_in_active_view_geometry(
                        active_view,
                        point,
                        mm_to_ft(1.0)
                    ):
                        continue
                    penetrations.append(PenetrationRecord(target, host, point))

            if not target_supported:
                unsupported_targets.append(target)

            progress.update_progress(index + 1, total)

    return penetrations, unsupported_targets


def merge_duplicate_penetrations(penetrations, tolerance):
    # Gop theo tung target. Huu ich khi Wall/Floor co nhieu solid/lop trung nhau
    # hoac 2 host element bi duplicate tai cung mot vi tri.
    if tolerance <= 0.0:
        return penetrations

    grouped = {}
    for penetration in penetrations:
        grouped.setdefault(penetration.target.key, []).append(penetration)

    result = []
    for target_key, records in grouped.items():
        kept = []
        for record in records:
            duplicate = None
            for existing in kept:
                if record.point.DistanceTo(existing.point) <= tolerance:
                    duplicate = existing
                    break
            if duplicate is None:
                kept.append(record)
            else:
                # Uu tien giu thong tin Wall neu trung Wall/Floor tai mep giao nhau.
                if duplicate.host.kind != u"Wall" and record.host.kind == u"Wall":
                    duplicate.host = record.host
        result.extend(kept)
    return result


def build_sleeve_candidates(penetrations, sleeve_index, tolerance):
    all_pairs = []
    for pen_index, penetration in enumerate(penetrations):
        query_min, query_max = expand_bounds(penetration.point, penetration.point, tolerance)
        sleeves = sleeve_index.query(query_min, query_max)

        for sleeve in sleeves:
            if not point_in_bounds(penetration.point, sleeve.min_pt, sleeve.max_pt, tolerance):
                continue
            distance = penetration.point.DistanceTo(sleeve.center)
            penetration.candidate_pairs.append((distance, sleeve))
            all_pairs.append((distance, pen_index, sleeve))

        penetration.candidate_pairs.sort(key=lambda item: item[0])

    all_pairs.sort(key=lambda item: item[0])
    return all_pairs


def match_sleeves(penetrations, sleeves, tolerance, one_to_one):
    sleeve_index = SpatialIndex(cell_size=max(mm_to_ft(500.0), tolerance * 4.0), max_cells_per_item=100)
    for sleeve in sleeves:
        min_pt, max_pt = expand_bounds(sleeve.min_pt, sleeve.max_pt, tolerance)
        sleeve_index.add(sleeve, min_pt, max_pt)

    all_pairs = build_sleeve_candidates(penetrations, sleeve_index, tolerance)

    if not one_to_one:
        for penetration in penetrations:
            if penetration.candidate_pairs:
                penetration.sleeve = penetration.candidate_pairs[0][1]
        return

    used_penetrations = set()
    used_sleeves = set()
    for distance, pen_index, sleeve in all_pairs:
        if pen_index in used_penetrations:
            continue
        if sleeve.key in used_sleeves:
            continue
        penetrations[pen_index].sleeve = sleeve
        used_penetrations.add(pen_index)
        used_sleeves.add(sleeve.key)



def get_record_center(record):
    # Dung center cua phan bounds da cat theo Active View. Khong dung
    # LocationPoint vi insertion point co the nam ngoai crop/view range.
    return XYZ(
        (record.min_pt.X + record.max_pt.X) * 0.5,
        (record.min_pt.Y + record.max_pt.Y) * 0.5,
        (record.min_pt.Z + record.max_pt.Z) * 0.5
    )


def bounds_contact_point(min_a, max_a, min_b, max_b):
    """Lay diem dai dien cua vung giao/gan nhau giua hai AABB."""
    coordinates = []
    for a_min, a_max, b_min, b_max in (
        (min_a.X, max_a.X, min_b.X, max_b.X),
        (min_a.Y, max_a.Y, min_b.Y, max_b.Y),
        (min_a.Z, max_a.Z, min_b.Z, max_b.Z)
    ):
        overlap_min = max(a_min, b_min)
        overlap_max = min(a_max, b_max)
        coordinates.append((overlap_min + overlap_max) * 0.5)
    return XYZ(coordinates[0], coordinates[1], coordinates[2])


def quick_check_targets_against_sleeves(targets, sleeves, tolerance):
    """Quet nhanh MEP <-> Sleeve, khong can Wall/Floor.

    BoundingBox cua Sleeve da duoc tao tu Solid that trong collect_sleeves(),
    nen symbolic line, reference line va Room Calculation Point khong lam rong
    vung nhan dien. Tolerance chi duoc dung de mo rong vung tim kiem.
    """
    sleeve_index = SpatialIndex(
        cell_size=max(mm_to_ft(500.0), tolerance * 4.0),
        max_cells_per_item=100
    )
    for sleeve in sleeves:
        sleeve_index.add(sleeve, sleeve.min_pt, sleeve.max_pt)

    matched = {}
    missing = []

    with forms.ProgressBar(
        title=u"Quét nhanh MEP và Sleeve {value}/{max_value}",
        cancellable=True
    ) as progress:
        total = len(targets)
        for index, target in enumerate(targets):
            if progress.cancelled:
                return None, None

            query_min, query_max = expand_bounds(
                target.min_pt,
                target.max_pt,
                tolerance
            )
            candidates = sleeve_index.query(query_min, query_max)

            best_sleeve = None
            best_distance = None
            target_center = get_record_center(target)

            for sleeve in candidates:
                if not bounds_overlap(
                    target.min_pt,
                    target.max_pt,
                    sleeve.min_pt,
                    sleeve.max_pt,
                    tolerance
                ):
                    continue

                contact_point = bounds_contact_point(
                    target.min_pt,
                    target.max_pt,
                    sleeve.min_pt,
                    sleeve.max_pt
                )
                if not point_in_active_view_geometry(
                    active_view,
                    contact_point,
                    tolerance
                ):
                    continue

                try:
                    distance = target_center.DistanceTo(sleeve.center)
                except Exception:
                    distance = 0.0

                if best_sleeve is None or distance < best_distance:
                    best_sleeve = sleeve
                    best_distance = distance

            if best_sleeve is None:
                missing.append(target)
            else:
                matched[target.key] = best_sleeve

            progress.update_progress(index + 1, total)

    return matched, missing


def print_quick_report(sample, options, targets, sleeves, matched, missing):
    output.set_title(u"Quick Sleeve Check")

    lines = [
        u"# KẾT QUẢ QUÉT NHANH MEP ↔ SLEEVE",
        u"**Sleeve family:** `{}`  \n"
        u"**Đối tượng MEP trong Active View:** {}  \n"
        u"**Sleeve instance trong vùng quét:** {}  \n"
        u"**Đối tượng có chạm/gần Sleeve:** {}  \n"
        u"**Đối tượng không tìm thấy Sleeve:** {}".format(
            get_family_type_name(sample),
            len(targets),
            len(sleeves),
            len(matched),
            len(missing)
        ),
        u"**Chế độ:** Quét nhanh, bỏ qua Wall/Floor và Revit Link | "
        u"MEP: Hình học trong Active View | Sleeve: {} | Dung sai nhận Sleeve: {} mm".format(
            u"Active View" if options["sleeve_scope"] == "active_view" else u"Current Model",
            options["sleeve_tolerance_mm"]
        ),
        u"> Chế độ này chỉ xác định đối tượng MEP có giao hoặc nằm gần "
        u"BoundingBox được dựng chỉ từ **Solid 3D thật** của Sleeve. Nó không xác định "
        u"đối tượng có thực sự xuyên kết cấu hay không."
    ]

    if not sleeves:
        lines.append(
            u"> Không tìm thấy Sleeve thuộc family đã chọn trong vùng quét; "
            u"toàn bộ đối tượng MEP được xếp vào danh sách không có Sleeve."
        )

    if not missing:
        lines.append(u"## Không phát hiện đối tượng nào thiếu Sleeve trong chế độ quét nhanh.")
        output.print_md(u"\n\n".join(lines))
        return

    missing_ids_text = format_unique_element_ids(
        [target.id for target in missing]
    )
    lines.extend([
        u"## ID tất cả đối tượng không có Sleeve",
        u"{}".format(missing_ids_text),
        u"> Danh sách trên chứa đầy đủ {} ID duy nhất, không bị giới hạn theo số dòng của bảng bên dưới.".format(
            len(missing_ids_text.split(u",")) if missing_ids_text else 0
        ),
        u"## Danh sách đối tượng không tìm thấy Sleeve",
        u"| # | Target | Category | Mark | Family/Type | X (mm) | Y (mm) | Z (mm) |",
        u"|---:|:---|:---|:---|:---|---:|---:|---:|"
    ])

    link_cache = {}
    for index, target in enumerate(missing[:MAX_REPORT_ROWS]):
        element = target.element
        target_link = get_cached_output_link(element, link_cache)
        category_name = element.Category.Name if element.Category else u""
        point = get_record_center(target)
        lines.append(
            u"| {} | {} | {} | {} | {} | {:.1f} | {:.1f} | {:.1f} |".format(
                index + 1,
                target_link,
                md_cell(category_name),
                md_cell(get_mark(element)),
                md_cell(get_family_type_name(element)),
                ft_to_mm(point.X),
                ft_to_mm(point.Y),
                ft_to_mm(point.Z)
            )
        )

    if len(missing) > MAX_REPORT_ROWS:
        lines.append(
            u"> Báo cáo chỉ hiển thị {} / {} đối tượng để tránh làm chậm "
            u"cửa sổ output. Toàn bộ đối tượng thiếu vẫn được xử lý trong model.".format(
                MAX_REPORT_ROWS,
                len(missing)
            )
        )

    # Chi mot lan render Markdown thay vi goi output.print_md cho tung dong.
    output.print_md(u"\n".join(lines))

def isolate_and_select(element_ids, do_isolate):
    # Loai ID trung truoc khi gui mot danh sach lon sang Revit UI.
    unique_ids = []
    seen = set()
    for element_id in element_ids:
        key = id_value(element_id)
        if key in seen:
            continue
        seen.add(key)
        unique_ids.append(element_id)

    if not unique_ids:
        uidoc.Selection.SetElementIds(List[ElementId]())
        return False

    if len(unique_ids) > MAX_AUTO_VIEW_IDS:
        action_text = (
            u"Select và Temporary Isolate"
            if do_isolate else
            u"Select"
        )
        decision = MessageBox.Show(
            u"Kết quả có {} đối tượng. Thao tác {} toàn bộ có thể làm Revit "
            u"đứng lâu.\n\nYes: tiếp tục xử lý toàn bộ.\n"
            u"No: bỏ qua Select/Isolate và chỉ xem danh sách trong pyRevit Output.".format(
                len(unique_ids),
                action_text
            ),
            u"Kết quả có quá nhiều đối tượng",
            MessageBoxButtons.YesNo,
            MessageBoxIcon.Warning
        )
        if decision != DialogResult.Yes:
            uidoc.Selection.SetElementIds(List[ElementId]())
            return False

    id_list = make_element_id_list(unique_ids)
    uidoc.Selection.SetElementIds(id_list)

    if not do_isolate:
        return True

    with revit.Transaction(u"Isolate đối tượng thiếu Sleeve"):
        try:
            if active_view.IsTemporaryHideIsolateActive():
                active_view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
        except Exception:
            pass
        active_view.IsolateElementsTemporary(id_list)

    uidoc.RefreshActiveView()
    return True


def print_report(sample, options, targets, hosts, sleeves, penetrations,
                 missing, unsupported_targets, unloaded_links):
    output.set_title(u"Pipe Penetration - Missing Sleeve")

    missing_target_keys = set(record.target.key for record in missing)
    covered_count = len(penetrations) - len(missing)

    scan_types = []
    if options["scan_wall"]:
        scan_types.append(u"Wall")
    if options["scan_floor"]:
        scan_types.append(u"Floor")

    sources = []
    if options["scan_host"]:
        sources.append(u"Current Model")
    if options["scan_links"]:
        sources.append(u"Revit Link")

    lines = [
        u"# KẾT QUẢ KIỂM TRA PIPE XUYÊN WALL/FLOOR",
        u"**Sleeve family:** `{}`  \n"
        u"**Target có hình học trong Active View:** {}  \n"
        u"**Wall/Floor candidate:** {}  \n"
        u"**Sleeve instance:** {}  \n"
        u"**Tổng vị trí xuyên:** {}  \n"
        u"**Đã có sleeve:** {}  \n"
        u"**Thiếu sleeve:** {}  \n"
        u"**Đối tượng có ít nhất một vị trí thiếu:** {}".format(
            get_family_type_name(sample),
            len(targets),
            len(hosts),
            len(sleeves),
            len(penetrations),
            covered_count,
            len(missing),
            len(missing_target_keys)
        ),
        u"**Chế độ:** {} | {} | MEP: Hình học trong Active View | Sleeve: {} | "
        u"Sleeve tolerance {} mm | Merge {} mm | Min penetration {} mm".format(
            u" + ".join(scan_types),
            u" + ".join(sources),
            u"Active View" if options["sleeve_scope"] == "active_view" else u"Current Model",
            options["sleeve_tolerance_mm"],
            options["merge_tolerance_mm"],
            options["min_penetration_mm"]
        )
    ]

    if unloaded_links:
        lines.append(u"## Revit Link chưa load")
        for link_name in unloaded_links:
            lines.append(u"- {}".format(md_cell(link_name)))

    if unsupported_targets:
        lines.append(
            u"> Có {} đối tượng không có LocationCurve và cũng không đọc được Solid; "
            u"các đối tượng này đã bị bỏ qua.".format(len(unsupported_targets))
        )

    if not missing:
        lines.append(u"## Không phát hiện vị trí xuyên nào thiếu sleeve.")
        output.print_md(u"\n\n".join(lines))
        return

    missing_ids_text = format_unique_element_ids(
        [record.target.id for record in missing]
    )
    lines.extend([
        u"## ID tất cả đối tượng không có Sleeve",
        u"{}".format(missing_ids_text),
        u"> Danh sách trên chứa đầy đủ {} ID duy nhất, không bị giới hạn theo số dòng của bảng bên dưới.".format(
            len(missing_ids_text.split(u",")) if missing_ids_text else 0
        ),
        u"## Danh sách vị trí thiếu sleeve",
        u"| # | Target | Category | Mark | Host | Source | Host Id | X (mm) | Y (mm) | Z (mm) |",
        u"|---:|:---|:---|:---|:---|:---|---:|---:|---:|---:|"
    ])

    link_cache = {}
    for index, record in enumerate(missing[:MAX_REPORT_ROWS]):
        target = record.target.element
        target_link = get_cached_output_link(target, link_cache)
        category_name = target.Category.Name if target.Category else u""
        point = record.point
        lines.append(
            u"| {} | {} | {} | {} | {} | {} | {} | {:.1f} | {:.1f} | {:.1f} |".format(
                index + 1,
                target_link,
                md_cell(category_name),
                md_cell(get_mark(target)),
                md_cell(record.host.kind),
                md_cell(record.host.source_name),
                id_value(record.host.element_id),
                ft_to_mm(point.X),
                ft_to_mm(point.Y),
                ft_to_mm(point.Z)
            )
        )

    if len(missing) > MAX_REPORT_ROWS:
        lines.append(
            u"> Báo cáo chỉ hiển thị {} / {} vị trí để tránh làm chậm cửa sổ output. "
            u"Toàn bộ kết quả vẫn được dùng để select/isolate trong model.".format(
                MAX_REPORT_ROWS,
                len(missing)
            )
        )

    # Chi mot lan render Markdown; cache link khi mot target co nhieu vi tri xuyen.
    output.print_md(u"\n".join(lines))


def main():
    sample, sleeve_type_ids = choose_sample_sleeve()
    if sample is None:
        return

    dialog = ScanOptionsForm()
    if dialog.ShowDialog() != DialogResult.OK:
        return

    options = dialog.values()
    save_options(options)

    targets, global_min, global_max = collect_targets(options)
    if not targets or global_min is None:
        forms.alert(u"Không tìm thấy đối tượng MEP phù hợp trong Active View.")
        return

    # Mo rong vung tong de khong bo sot sleeve/host sat dau target.
    global_min, global_max = expand_bounds(global_min, global_max, mm_to_ft(100.0))

    if options["quick_mode"]:
        forms.toast(u"Đang quét nhanh đối tượng MEP và Sleeve...")
    else:
        forms.toast(u"Đang thu thập Wall/Floor và Sleeve...")

    sleeves = collect_sleeves(
        sleeve_type_ids,
        global_min,
        global_max,
        options["sleeve_tolerance"],
        options["sleeve_scope"]
    )

    if not sleeves:
        forms.toast(
            u"Không tìm thấy instance có Solid 3D hợp lệ của Family Sleeve đã chọn. "
            u"Room Calculation Point và BoundingBox element không được dùng để quét."
        )

    # Bỏ chọn đồng thời Wall và Floor: chỉ kiểm tra nhanh MEP có chạm/gần
    # Solid BoundingBox của Sleeve hay không. Không đọc host và Revit Link.
    if options["quick_mode"]:
        matched, quick_missing = quick_check_targets_against_sleeves(
            targets,
            sleeves,
            options["sleeve_tolerance"]
        )
        if matched is None:
            forms.alert(u"Đã hủy quá trình quét nhanh.")
            return

        print_quick_report(
            sample,
            options,
            targets,
            sleeves,
            matched,
            quick_missing
        )

        missing_ids = [target.id for target in quick_missing]
        if missing_ids:
            isolate_and_select(missing_ids, options["isolate_result"])
            forms.toast(
                u"Quét nhanh: phát hiện {} đối tượng không tìm thấy Sleeve. Toàn bộ ID đã được ghi trong pyRevit Output.".format(
                    len(missing_ids)
                )
            )
        else:
            uidoc.Selection.SetElementIds(List[ElementId]())
            forms.alert(
                u"Hoàn tất quét nhanh. Tất cả đối tượng MEP đã chạm hoặc nằm "
                u"trong dung sai của ít nhất một Sleeve."
            )
        return

    hosts, unloaded_links = collect_hosts(global_min, global_max, options)
    if not hosts:
        forms.alert(
            u"Không tìm thấy Wall/Floor phù hợp trong phạm vi các đối tượng MEP.\n"
            u"Kiểm tra lại lựa chọn Current Model/Revit Link và Wall/Floor."
        )
        return

    host_index = SpatialIndex(cell_size=20.0, max_cells_per_item=500)
    for host in hosts:
        host_index.add(host, host.min_pt, host.max_pt)

    penetrations, unsupported_targets = find_penetrations(targets, host_index, options)
    if penetrations is None:
        forms.alert(u"Đã hủy quá trình quét.")
        return

    penetrations = merge_duplicate_penetrations(
        penetrations,
        options["merge_tolerance"]
    )

    match_sleeves(
        penetrations,
        sleeves,
        options["sleeve_tolerance"],
        options["one_to_one"]
    )

    missing = [record for record in penetrations if record.sleeve is None]
    missing_ids_by_key = {}
    for record in missing:
        missing_ids_by_key[record.target.key] = record.target.id
    missing_ids = list(missing_ids_by_key.values())

    print_report(
        sample,
        options,
        targets,
        hosts,
        sleeves,
        penetrations,
        missing,
        unsupported_targets,
        unloaded_links
    )

    if missing_ids:
        isolate_and_select(missing_ids, options["isolate_result"])
        forms.toast(
            u"Đã phát hiện {} vị trí thiếu sleeve trên {} đối tượng. Toàn bộ ID đã được ghi trong pyRevit Output.".format(
                len(missing), len(missing_ids)
            )
        )
    else:
        uidoc.Selection.SetElementIds(List[ElementId]())
        forms.alert(
            u"Hoàn tất. Không phát hiện vị trí Pipe/PA/PF xuyên Wall/Floor thiếu Sleeve."
        )


if __name__ == "__main__":
    try:
        main()
    except OperationCanceledException:
        pass
    except Exception:
        output.print_md(u"# LỖI CHẠY TOOL")
        output.print_md(u"```\n{}\n```".format(traceback.format_exc()))
        forms.alert(
            u"Tool gặp lỗi. Chi tiết đã được in trong cửa sổ pyRevit Output."
        )
