# -*- coding: utf-8 -*-
"""
Pipe Local Up/Down - pyRevit / IronPython 2.7

Che do 1 - Chon truc tiep dam:
- Chon mot hoac nhieu Pipe thang.
- Pick truc tiep mot dam trong model hien tai hoac Revit Link.
- Tool doc Solid cua dam, tim hai bien dam theo tung Pipe tren mat bang,
  cat Pipe va tao doan UP/DOWN.

Che do 2 - Pipe giua hai Union:
- Chon mot hoac nhieu doan Pipe nam giua hai Union.
- Tool xoa Union, thu ngan hai Pipe ngoai neu can va tao doan UP/DOWN.

Kieu hinh hoc:
- Ne cuc bo hai phia: nang/ha doan giua va tro ve cao do cu.
- Mot phia UP: phia ngoai mep dam giu nguyen, phia con lai duoc nang len.
- Mot phia DOWN: phia ngoai mep dam giu nguyen, phia con lai duoc ha xuong.

Goc:
- 90 do: hai doan noi dung.
- Nho hon 90 do: hai doan noi xien.
- Run ngang moi ben = abs(offset) / tan(angle).

Quy uoc:
- Gia tri duong: UP.
- Gia tri am: DOWN.
- Don vi chieu dai: mm.
"""

import clr
import math
import traceback

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List
import Autodesk.Revit.DB as DB
import Autodesk.Revit.DB.Plumbing as Plumbing

from pyrevit import revit, forms, script


doc = revit.doc
uidoc = revit.uidoc
app = doc.Application
output = script.get_output()
config = script.get_config()


# -----------------------------------------------------------------------------
# Constants / compatibility
# -----------------------------------------------------------------------------

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


GEOM_TOL = mm_to_internal(1.0)
POINT_TOL = mm_to_internal(2.0)
MIN_OFFSET = max(mm_to_internal(1.0), app.ShortCurveTolerance * 1.01)
MIN_END_CLEARANCE = max(mm_to_internal(10.0), app.ShortCurveTolerance * 2.1)
ANGLE_MIN = 5.0
ANGLE_MAX = 90.0
MODE_BEAM = u"beam"
MODE_UNION = u"union"

PROFILE_LOCAL = u"local"
PROFILE_ONE_SIDE_UP = u"one_side_up"
PROFILE_ONE_SIDE_DOWN = u"one_side_down"


def id_value(element_id):
    try:
        return element_id.Value
    except Exception:
        return element_id.IntegerValue


def same_id(id1, id2):
    if id1 is None or id2 is None:
        return False
    return id_value(id1) == id_value(id2)


def is_valid_element_id(element_id):
    return (
        element_id is not None
        and not same_id(element_id, DB.ElementId.InvalidElementId)
    )


def to_text(value):
    try:
        return unicode(value)
    except Exception:
        try:
            return str(value)
        except Exception:
            return u"Unknown error"


def parse_number(value):
    return float(to_text(value).strip().replace(",", "."))


def cfg_get(name, default_value):
    try:
        value = getattr(config, name)
        if value is None:
            return default_value
        return value
    except Exception:
        return default_value


def xyz_add_z(point, dz):
    return DB.XYZ(point.X, point.Y, point.Z + dz)


def distance_xy(point1, point2):
    dx = point1.X - point2.X
    dy = point1.Y - point2.Y
    return math.sqrt(dx * dx + dy * dy)


def xy_length(vector):
    return math.sqrt(vector.X * vector.X + vector.Y * vector.Y)


def normalized_xy(vector):
    length = xy_length(vector)
    if length <= GEOM_TOL:
        raise Exception(u"Không xác định được phương Pipe trên mặt bằng.")
    return DB.XYZ(vector.X / length, vector.Y / length, 0.0)


def evaluate_line(line, parameter):
    point1 = line.GetEndPoint(0)
    point2 = line.GetEndPoint(1)
    return point1 + (point2 - point1) * parameter


def line_xy_length(line):
    return distance_xy(line.GetEndPoint(0), line.GetEndPoint(1))


def parameter_on_line_xy(line, point):
    start = line.GetEndPoint(0)
    end = line.GetEndPoint(1)
    vector = end - start
    denominator = vector.X * vector.X + vector.Y * vector.Y
    if denominator <= GEOM_TOL * GEOM_TOL:
        raise Exception(u"Pipe đứng hoặc gần đứng, không thể xử lý trên mặt bằng.")
    return (
        (point.X - start.X) * vector.X
        + (point.Y - start.Y) * vector.Y
    ) / denominator


def angle_run(offset_z, angle_deg):
    if abs(angle_deg - 90.0) <= 1e-6:
        return 0.0
    angle_radians = math.radians(angle_deg)
    tangent = math.tan(angle_radians)
    if abs(tangent) <= 1e-9:
        raise Exception(u"Góc nhập quá nhỏ.")
    return abs(offset_z) / tangent


# -----------------------------------------------------------------------------
# Settings UI + persistent config
# -----------------------------------------------------------------------------

class SettingsWindow(forms.WPFWindow):
    def __init__(self):
        xaml_file = script.get_bundle_file("ui.xaml")
        forms.WPFWindow.__init__(self, xaml_file)

        saved_mode = to_text(cfg_get("mode", MODE_BEAM))
        saved_profile = to_text(cfg_get("profile_mode", PROFILE_LOCAL))
        saved_offset = to_text(cfg_get("offset_mm", u"300"))
        saved_angle = to_text(cfg_get("angle_deg", u"90"))
        saved_clearance = to_text(cfg_get("clearance_mm", u"50"))

        self.beam_mode_radio.IsChecked = saved_mode != MODE_UNION
        self.union_mode_radio.IsChecked = saved_mode == MODE_UNION

        self.local_profile_radio.IsChecked = saved_profile not in [
            PROFILE_ONE_SIDE_UP, PROFILE_ONE_SIDE_DOWN
        ]
        self.one_side_up_radio.IsChecked = saved_profile == PROFILE_ONE_SIDE_UP
        self.one_side_down_radio.IsChecked = saved_profile == PROFILE_ONE_SIDE_DOWN

        self.offset_text.Text = saved_offset
        self.angle_combo.Text = saved_angle
        self.clearance_text.Text = saved_clearance

        self.ok_button.Click += self.ok_click
        self.cancel_button.Click += self.cancel_click

        self.accepted = False
        self.mode = saved_mode
        self.profile_mode = saved_profile
        self.offset_mm = 0.0
        self.angle_deg = 90.0
        self.clearance_mm = 0.0

    def ok_click(self, sender, args):
        try:
            offset_mm = parse_number(self.offset_text.Text)
            angle_deg = parse_number(self.angle_combo.Text)
            clearance_mm = parse_number(self.clearance_text.Text)
        except Exception:
            forms.alert(
                u"Một hoặc nhiều giá trị nhập không hợp lệ.",
                title=u"Pipe Local Up/Down"
            )
            return

        if abs(mm_to_internal(offset_mm)) < MIN_OFFSET:
            forms.alert(
                u"Độ cao UP/DOWN quá nhỏ hoặc bằng 0.",
                title=u"Pipe Local Up/Down"
            )
            return

        if angle_deg < ANGLE_MIN or angle_deg > ANGLE_MAX:
            forms.alert(
                u"Góc phải nằm trong khoảng {0:.0f}° đến {1:.0f}°.".format(
                    ANGLE_MIN, ANGLE_MAX
                ),
                title=u"Pipe Local Up/Down"
            )
            return

        if clearance_mm < 0.0:
            forms.alert(
                u"Khoảng hở khỏi mép dầm không được âm.",
                title=u"Pipe Local Up/Down"
            )
            return

        self.mode = MODE_UNION if self.union_mode_radio.IsChecked else MODE_BEAM

        if self.one_side_up_radio.IsChecked:
            self.profile_mode = PROFILE_ONE_SIDE_UP
        elif self.one_side_down_radio.IsChecked:
            self.profile_mode = PROFILE_ONE_SIDE_DOWN
        else:
            self.profile_mode = PROFILE_LOCAL

        if self.mode == MODE_UNION and self.profile_mode != PROFILE_LOCAL:
            forms.alert(
                u"Hai chế độ một phía chỉ sử dụng với phương pháp chọn trực tiếp dầm.",
                title=u"Pipe Local Up/Down"
            )
            return

        self.offset_mm = offset_mm
        self.angle_deg = angle_deg
        self.clearance_mm = clearance_mm
        self.accepted = True

        config.mode = self.mode
        config.profile_mode = self.profile_mode
        config.offset_mm = to_text(offset_mm)
        config.angle_deg = to_text(angle_deg)
        config.clearance_mm = to_text(clearance_mm)
        script.save_config()

        self.Close()

    def cancel_click(self, sender, args):
        self.accepted = False
        self.Close()


def show_settings():
    window = SettingsWindow()
    window.ShowDialog()
    if not window.accepted:
        return None
    return {
        "mode": window.mode,
        "profile_mode": window.profile_mode,
        "offset_mm": window.offset_mm,
        "angle_deg": window.angle_deg,
        "clearance_mm": window.clearance_mm
    }


# -----------------------------------------------------------------------------
# Selection
# -----------------------------------------------------------------------------

class PipeSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, Plumbing.Pipe)

    def AllowReference(self, reference, position):
        return False


def get_selected_pipes():
    pipes = []

    try:
        selected_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        selected_ids = []

    for element_id in selected_ids:
        element = doc.GetElement(element_id)
        if isinstance(element, Plumbing.Pipe):
            pipes.append(element)

    if pipes:
        return pipes

    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            PipeSelectionFilter(),
            u"Chọn một hoặc nhiều Pipe, sau đó nhấn Finish"
        )
    except OperationCanceledException:
        return []

    for ref in refs:
        element = doc.GetElement(ref.ElementId)
        if isinstance(element, Plumbing.Pipe):
            pipes.append(element)

    return pipes


def category_is_structural_framing(element):
    try:
        return (
            element is not None
            and element.Category is not None
            and id_value(element.Category.Id)
            == int(DB.BuiltInCategory.OST_StructuralFraming)
        )
    except Exception:
        return False


class BeamSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        if category_is_structural_framing(element):
            return True
        return isinstance(element, DB.RevitLinkInstance)

    def AllowReference(self, reference, position):
        try:
            host_element = doc.GetElement(reference.ElementId)
            if category_is_structural_framing(host_element):
                return True

            if isinstance(host_element, DB.RevitLinkInstance):
                linked_id = reference.LinkedElementId
                if not is_valid_element_id(linked_id):
                    return False
                link_doc = host_element.GetLinkDocument()
                if link_doc is None:
                    return False
                return category_is_structural_framing(
                    link_doc.GetElement(linked_id)
                )
        except Exception:
            pass
        return False


def pick_beam_context():
    reference = uidoc.Selection.PickObject(
        ObjectType.PointOnElement,
        BeamSelectionFilter(),
        u"Pick trực tiếp vào dầm cần né"
    )

    host_element = doc.GetElement(reference.ElementId)
    if host_element is None:
        raise Exception(u"Không đọc được phần tử vừa pick.")

    if isinstance(host_element, DB.RevitLinkInstance):
        link_doc = host_element.GetLinkDocument()
        if link_doc is None:
            raise Exception(u"Revit Link chứa dầm chưa được load.")

        linked_id = reference.LinkedElementId
        if not is_valid_element_id(linked_id):
            raise Exception(u"Hãy pick trực tiếp vào dầm trong Revit Link.")

        beam = link_doc.GetElement(linked_id)
        if not category_is_structural_framing(beam):
            raise Exception(u"Phần tử vừa pick không phải Structural Framing.")

        try:
            transform = host_element.GetTotalTransform()
        except Exception:
            transform = host_element.GetTransform()

        return {
            "element": beam,
            "transform": transform,
            "is_linked": True,
            "link_instance": host_element,
            "display_id": u"{0} (Link: {1})".format(
                id_value(beam.Id), id_value(host_element.Id)
            )
        }

    if not category_is_structural_framing(host_element):
        raise Exception(u"Phần tử vừa pick không phải Structural Framing.")

    return {
        "element": host_element,
        "transform": None,
        "is_linked": False,
        "link_instance": None,
        "display_id": to_text(id_value(host_element.Id))
    }


def reference_matches_beam(reference, beam_context):
    try:
        host_element = doc.GetElement(reference.ElementId)
        if beam_context["is_linked"]:
            if not isinstance(host_element, DB.RevitLinkInstance):
                return False
            if not same_id(host_element.Id, beam_context["link_instance"].Id):
                return False
            return same_id(reference.LinkedElementId, beam_context["element"].Id)

        return same_id(reference.ElementId, beam_context["element"].Id)
    except Exception:
        return False


def get_reference_pick_point(reference):
    try:
        point = reference.GlobalPoint
        if point is not None:
            return point
    except Exception:
        pass

    try:
        geometry_object = doc.GetElement(reference.ElementId).GetGeometryObjectFromReference(reference)
        if isinstance(geometry_object, DB.Edge):
            curve = geometry_object.AsCurve()
            return curve.Evaluate(0.5, True)
    except Exception:
        pass

    raise Exception(u"Không đọc được vị trí mép dầm vừa pick.")


def pick_beam_edge_point(beam_context):
    reference = uidoc.Selection.PickObject(
        ObjectType.PointOnElement,
        BeamSelectionFilter(),
        u"Pick vào mép dầm: phía ngoài mép này giữ nguyên cao độ"
    )

    if not reference_matches_beam(reference, beam_context):
        raise Exception(u"Mép vừa pick không thuộc đúng dầm đã chọn trước đó.")

    return get_reference_pick_point(reference)


# -----------------------------------------------------------------------------
# Connector helpers
# -----------------------------------------------------------------------------


def get_connector_manager(element):
    try:
        manager = element.ConnectorManager
        if manager is not None:
            return manager
    except Exception:
        pass

    try:
        mep_model = element.MEPModel
        if mep_model is not None:
            manager = mep_model.ConnectorManager
            if manager is not None:
                return manager
    except Exception:
        pass

    return None


def get_connectors(element, end_only=False):
    result = []
    manager = get_connector_manager(element)
    if manager is None:
        return result

    try:
        for connector in manager.Connectors:
            if end_only:
                try:
                    if connector.ConnectorType != DB.ConnectorType.End:
                        continue
                except Exception:
                    continue
            result.append(connector)
    except Exception:
        pass

    return result


def nearest_connector(element, point, end_only=True):
    connectors = get_connectors(element, end_only)
    if not connectors:
        raise Exception(u"Không tìm thấy connector trên element ID {0}.".format(
            id_value(element.Id)
        ))

    best = None
    best_distance = None

    for connector in connectors:
        distance = connector.Origin.DistanceTo(point)
        if best is None or distance < best_distance:
            best = connector
            best_distance = distance

    maximum_distance = max(POINT_TOL * 10.0, mm_to_internal(10.0))
    if best is None or best_distance > maximum_distance:
        raise Exception(
            u"Không tìm thấy connector gần điểm xử lý; sai lệch {0:.1f} mm.".format(
                internal_to_mm(best_distance) if best_distance else -1.0
            )
        )

    return best


def connected_references(connector):
    result = []
    try:
        for ref_connector in connector.AllRefs:
            try:
                if ref_connector.Owner is None:
                    continue
                if same_id(ref_connector.Owner.Id, connector.Owner.Id):
                    continue
                result.append(ref_connector)
            except Exception:
                continue
    except Exception:
        pass
    return result


# -----------------------------------------------------------------------------
# Pipe metadata / creation
# -----------------------------------------------------------------------------


def get_pipe_system_type_id(pipe):
    try:
        parameter = pipe.get_Parameter(
            DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM
        )
        if parameter is not None:
            system_type_id = parameter.AsElementId()
            if is_valid_element_id(system_type_id):
                return system_type_id
    except Exception:
        pass

    try:
        if pipe.MEPSystem is not None:
            system_type_id = pipe.MEPSystem.GetTypeId()
            if is_valid_element_id(system_type_id):
                return system_type_id
    except Exception:
        pass

    raise Exception(u"Không xác định được Piping System Type của Pipe.")


def get_pipe_level_id(pipe):
    try:
        if pipe.ReferenceLevel is not None:
            return pipe.ReferenceLevel.Id
    except Exception:
        pass

    try:
        parameter = pipe.get_Parameter(DB.BuiltInParameter.RBS_START_LEVEL_PARAM)
        if parameter is not None:
            level_id = parameter.AsElementId()
            if is_valid_element_id(level_id):
                return level_id
    except Exception:
        pass

    raise Exception(u"Không xác định được Reference Level của Pipe.")


def get_pipe_diameter(pipe):
    parameter = pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    if parameter is None:
        raise Exception(u"Không đọc được đường kính Pipe.")
    return parameter.AsDouble()


def set_pipe_diameter(pipe, diameter):
    parameter = pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    if parameter is not None and not parameter.IsReadOnly:
        parameter.Set(diameter)


def create_pipe_like(source_pipe, point1, point2):
    if point1.DistanceTo(point2) <= app.ShortCurveTolerance:
        raise Exception(u"Đoạn Pipe mới ngắn hơn ShortCurveTolerance của Revit.")

    new_pipe = Plumbing.Pipe.Create(
        doc,
        get_pipe_system_type_id(source_pipe),
        source_pipe.GetTypeId(),
        get_pipe_level_id(source_pipe),
        point1,
        point2
    )

    set_pipe_diameter(new_pipe, get_pipe_diameter(source_pipe))
    return new_pipe


def get_pipe_line(pipe):
    try:
        curve = pipe.Location.Curve
        if isinstance(curve, DB.Line):
            return curve
    except Exception:
        pass
    return None


def set_pipe_curve(pipe, point1, point2):
    if point1.DistanceTo(point2) <= app.ShortCurveTolerance:
        raise Exception(u"Đoạn Pipe giữa quá ngắn.")

    location = pipe.Location
    if not isinstance(location, DB.LocationCurve):
        raise Exception(u"Pipe không có LocationCurve hợp lệ.")

    location.Curve = DB.Line.CreateBound(point1, point2)


def set_pipe_endpoint(pipe, old_connection_point, new_connection_point):
    line = get_pipe_line(pipe)
    if line is None:
        raise Exception(
            u"Pipe ngoài ID {0} không phải Pipe thẳng.".format(
                id_value(pipe.Id)
            )
        )

    end0 = line.GetEndPoint(0)
    end1 = line.GetEndPoint(1)

    if end0.DistanceTo(old_connection_point) <= end1.DistanceTo(old_connection_point):
        old_end = end0
        far_end = end1
        replace_first = True
    else:
        old_end = end1
        far_end = end0
        replace_first = False

    original_length = old_end.DistanceTo(far_end)
    trim_distance = old_end.DistanceTo(new_connection_point)
    new_length = new_connection_point.DistanceTo(far_end)

    collinear_check = abs((trim_distance + new_length) - original_length)
    if collinear_check > max(POINT_TOL * 5.0, mm_to_internal(5.0)):
        raise Exception(
            u"Không thể tạo góc trên Pipe ngoài ID {0}: Pipe ngoài không thẳng hàng với đoạn giữa.".format(
                id_value(pipe.Id)
            )
        )

    if new_length <= MIN_END_CLEARANCE:
        raise Exception(
            u"Pipe ngoài ID {0} không đủ chiều dài thẳng. Cần thêm tối thiểu khoảng {1:.1f} mm.".format(
                id_value(pipe.Id),
                internal_to_mm(trim_distance + MIN_END_CLEARANCE - original_length)
            )
        )

    if replace_first:
        set_pipe_curve(pipe, new_connection_point, far_end)
    else:
        set_pipe_curve(pipe, far_end, new_connection_point)


# -----------------------------------------------------------------------------
# Union mode
# -----------------------------------------------------------------------------


def is_union_fitting(element):
    if element is None:
        return False

    try:
        if element.Category is None:
            return False
        if id_value(element.Category.Id) != int(DB.BuiltInCategory.OST_PipeFitting):
            return False
    except Exception:
        return False

    try:
        if element.MEPModel.PartType == DB.PartType.Union:
            return True
    except Exception:
        pass

    names = []
    try:
        names.append(element.Name)
    except Exception:
        pass

    try:
        symbol = doc.GetElement(element.GetTypeId())
        if symbol is not None:
            names.append(symbol.FamilyName)
            names.append(symbol.Name)
    except Exception:
        pass

    joined = u" ".join([to_text(item).lower() for item in names if item])
    keywords = [u"union", u"coupling", u"socket", u"măng sông", u"mang song"]
    for keyword in keywords:
        if keyword in joined:
            return True

    return False


def find_union_at_pipe_connector(pipe, pipe_connector):
    for ref_connector in connected_references(pipe_connector):
        owner = ref_connector.Owner
        if is_union_fitting(owner):
            return owner

    raise Exception(
        u"Một đầu Pipe không nối trực tiếp với Union."
    )


def find_other_side_of_union(union, selected_pipe):
    for union_connector in get_connectors(union, end_only=True):
        for ref_connector in connected_references(union_connector):
            owner = ref_connector.Owner
            if owner is None:
                continue
            if same_id(owner.Id, selected_pipe.Id):
                continue
            if same_id(owner.Id, union.Id):
                continue

            return {
                "owner_id": owner.Id,
                "point": ref_connector.Origin
            }

    raise Exception(
        u"Union ID {0} không tìm thấy phần tử nối ở phía ngoài.".format(
            id_value(union.Id)
        )
    )


def get_union_boundaries(pipe):
    end_connectors = get_connectors(pipe, end_only=True)
    if len(end_connectors) != 2:
        raise Exception(u"Pipe phải có đúng 2 end connector.")

    boundaries = []
    union_ids = []

    for pipe_connector in end_connectors:
        union = find_union_at_pipe_connector(pipe, pipe_connector)
        outside = find_other_side_of_union(union, pipe)
        outside["union_id"] = union.Id
        boundaries.append(outside)
        union_ids.append(union.Id)

    if same_id(union_ids[0], union_ids[1]):
        raise Exception(u"Hai đầu Pipe đang tham chiếu cùng một Union.")

    line = get_pipe_line(pipe)
    if line is None:
        raise Exception(u"Đoạn Pipe giữa hai Union phải là Pipe thẳng.")

    line_start = line.GetEndPoint(0)
    boundaries.sort(key=lambda item: item["point"].DistanceTo(line_start))
    return boundaries


# -----------------------------------------------------------------------------
# Beam solid extraction and plan intersection
# -----------------------------------------------------------------------------


def collect_solids(geometry_element, result):
    if geometry_element is None:
        return

    for geometry_object in geometry_element:
        try:
            if isinstance(geometry_object, DB.Solid):
                if geometry_object.Faces.Size > 0 and geometry_object.Edges.Size > 0:
                    try:
                        if geometry_object.Volume > 1e-9:
                            result.append(geometry_object)
                    except Exception:
                        result.append(geometry_object)

            elif isinstance(geometry_object, DB.GeometryInstance):
                try:
                    collect_solids(
                        geometry_object.GetInstanceGeometry(),
                        result
                    )
                except Exception:
                    try:
                        collect_solids(
                            geometry_object.GetSymbolGeometry(),
                            result
                        )
                    except Exception:
                        pass
        except Exception:
            continue


def transform_solid(solid, transform):
    if transform is None:
        return solid
    try:
        return DB.SolidUtils.CreateTransformed(solid, transform)
    except Exception as ex:
        raise Exception(
            u"Không chuyển được hình học dầm từ Revit Link: {0}".format(
                to_text(ex)
            )
        )


def bbox_points(bbox):
    if bbox is None:
        return []

    transform = bbox.Transform
    points = []
    for x in [bbox.Min.X, bbox.Max.X]:
        for y in [bbox.Min.Y, bbox.Max.Y]:
            for z in [bbox.Min.Z, bbox.Max.Z]:
                point = DB.XYZ(x, y, z)
                try:
                    point = transform.OfPoint(point)
                except Exception:
                    pass
                points.append(point)
    return points


def transform_points(points, transform):
    if transform is None:
        return points
    return [transform.OfPoint(point) for point in points]


def points_bounds(points):
    if not points:
        return None
    return {
        "min_x": min(point.X for point in points),
        "max_x": max(point.X for point in points),
        "min_y": min(point.Y for point in points),
        "max_y": max(point.Y for point in points),
        "min_z": min(point.Z for point in points),
        "max_z": max(point.Z for point in points)
    }


def build_beam_geometry(beam_context):
    beam = beam_context["element"]
    transform = beam_context["transform"]

    options = DB.Options()
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = False
    options.DetailLevel = DB.ViewDetailLevel.Fine

    source_solids = []
    try:
        collect_solids(beam.get_Geometry(options), source_solids)
    except Exception:
        source_solids = []

    solids = []
    for solid in source_solids:
        try:
            solids.append(transform_solid(solid, transform))
        except Exception:
            continue

    bbox = None
    try:
        bbox = beam.get_BoundingBox(None)
    except Exception:
        bbox = None

    bbox_host_points = transform_points(bbox_points(bbox), transform)
    bounds = points_bounds(bbox_host_points)

    if not solids and bounds is None:
        raise Exception(u"Không đọc được Solid hoặc BoundingBox của dầm đã chọn.")

    return {
        "solids": solids,
        "bounds": bounds,
        "display_id": beam_context["display_id"]
    }


def interval_from_solid(pipe_line, solid):
    start = pipe_line.GetEndPoint(0)
    end = pipe_line.GetEndPoint(1)
    if distance_xy(start, end) <= GEOM_TOL:
        return []

    try:
        center_z = solid.ComputeCentroid().Z
    except Exception:
        center_z = (start.Z + end.Z) * 0.5

    test_start = DB.XYZ(start.X, start.Y, center_z)
    test_end = DB.XYZ(end.X, end.Y, center_z)
    test_line = DB.Line.CreateBound(test_start, test_end)

    options = DB.SolidCurveIntersectionOptions()
    try:
        options.ResultType = DB.SolidCurveIntersectionMode.CurveSegmentsInside
    except Exception:
        pass

    intervals = []
    try:
        intersection = solid.IntersectWithCurve(test_line, options)
        count = intersection.SegmentCount
        for index in range(count):
            segment = intersection.GetCurveSegment(index)
            t0 = parameter_on_line_xy(pipe_line, segment.GetEndPoint(0))
            t1 = parameter_on_line_xy(pipe_line, segment.GetEndPoint(1))
            intervals.append((min(t0, t1), max(t0, t1)))
    except Exception:
        pass

    return intervals


def interval_from_axis_aligned_bounds(pipe_line, bounds):
    if bounds is None:
        return []

    start = pipe_line.GetEndPoint(0)
    end = pipe_line.GetEndPoint(1)
    dx = end.X - start.X
    dy = end.Y - start.Y

    t_min = 0.0
    t_max = 1.0

    axes = [
        (start.X, dx, bounds["min_x"], bounds["max_x"]),
        (start.Y, dy, bounds["min_y"], bounds["max_y"])
    ]

    for origin, direction, minimum, maximum in axes:
        if abs(direction) <= 1e-12:
            if origin < minimum or origin > maximum:
                return []
            continue

        t1 = (minimum - origin) / direction
        t2 = (maximum - origin) / direction
        entry = min(t1, t2)
        exit_value = max(t1, t2)
        t_min = max(t_min, entry)
        t_max = min(t_max, exit_value)

        if t_min >= t_max:
            return []

    return [(t_min, t_max)]


def merge_intervals(intervals):
    cleaned = []
    for start, end in intervals:
        start = max(0.0, min(1.0, start))
        end = max(0.0, min(1.0, end))
        if end - start > 1e-9:
            cleaned.append((start, end))

    if not cleaned:
        return []

    cleaned.sort(key=lambda item: item[0])
    merged = [cleaned[0]]

    for start, end in cleaned[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1e-7:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def get_beam_interval(pipe, beam_geometry):
    line = get_pipe_line(pipe)
    if line is None:
        raise Exception(u"Chế độ chọn dầm chỉ hỗ trợ Pipe thẳng.")

    if line_xy_length(line) <= GEOM_TOL:
        raise Exception(u"Pipe đứng hoặc gần đứng, không thể xác định bề rộng dầm trên mặt bằng.")

    intervals = []
    for solid in beam_geometry["solids"]:
        intervals.extend(interval_from_solid(line, solid))

    intervals = merge_intervals(intervals)

    if not intervals:
        intervals = merge_intervals(
            interval_from_axis_aligned_bounds(line, beam_geometry["bounds"])
        )

    if not intervals:
        raise Exception(
            u"Tim Pipe không cắt qua hình chiếu của dầm ID {0}.".format(
                beam_geometry["display_id"]
            )
        )

    interval = max(intervals, key=lambda item: item[1] - item[0])
    t_start, t_end = interval

    beam_width = (t_end - t_start) * line_xy_length(line)
    if beam_width <= MIN_END_CLEARANCE:
        raise Exception(
            u"Bề rộng dầm đọc được chỉ {0:.1f} mm. Hãy kiểm tra Solid của family dầm.".format(
                internal_to_mm(beam_width)
            )
        )

    return interval


# -----------------------------------------------------------------------------
# Pipe break helpers
# -----------------------------------------------------------------------------


def point_on_bound_line(point, line, tolerance):
    point1 = line.GetEndPoint(0)
    point2 = line.GetEndPoint(1)
    total = point1.DistanceTo(point2)
    split = point1.DistanceTo(point) + point.DistanceTo(point2)
    return abs(split - total) <= tolerance


def segment_pair_score(pipe, point1, point2):
    line = get_pipe_line(pipe)
    if line is None:
        return 1e99

    end1 = line.GetEndPoint(0)
    end2 = line.GetEndPoint(1)

    score_forward = end1.DistanceTo(point1) + end2.DistanceTo(point2)
    score_reverse = end1.DistanceTo(point2) + end2.DistanceTo(point1)
    return min(score_forward, score_reverse)


def segment_has_endpoint(pipe, point):
    line = get_pipe_line(pipe)
    if line is None:
        return False
    return (
        line.GetEndPoint(0).DistanceTo(point) <= POINT_TOL * 3.0
        or line.GetEndPoint(1).DistanceTo(point) <= POINT_TOL * 3.0
    )


def break_pipe_between_points(pipe, point1, point2):
    original_id = pipe.Id

    new_id_1 = Plumbing.PlumbingUtils.BreakCurve(doc, original_id, point1)
    if not is_valid_element_id(new_id_1):
        raise Exception(u"BreakCurve thất bại tại điểm cắt thứ nhất.")

    doc.Regenerate()

    first_segments = [
        doc.GetElement(original_id),
        doc.GetElement(new_id_1)
    ]

    target_for_second_break = None
    for segment in first_segments:
        if segment is None:
            continue
        line = get_pipe_line(segment)
        if line is not None and point_on_bound_line(point2, line, POINT_TOL * 3.0):
            target_for_second_break = segment
            break

    if target_for_second_break is None:
        raise Exception(u"Không tìm thấy đoạn Pipe chứa điểm cắt thứ hai.")

    new_id_2 = Plumbing.PlumbingUtils.BreakCurve(
        doc,
        target_for_second_break.Id,
        point2
    )
    if not is_valid_element_id(new_id_2):
        raise Exception(u"BreakCurve thất bại tại điểm cắt thứ hai.")

    doc.Regenerate()

    segment_ids = []
    for element_id in [original_id, new_id_1, new_id_2]:
        if not any(same_id(element_id, existing_id) for existing_id in segment_ids):
            segment_ids.append(element_id)

    segments = []
    for element_id in segment_ids:
        element = doc.GetElement(element_id)
        if isinstance(element, Plumbing.Pipe):
            segments.append(element)

    if len(segments) != 3:
        raise Exception(u"Sau khi cắt không thu được đúng 3 đoạn Pipe.")

    middle_pipe = min(
        segments,
        key=lambda item: segment_pair_score(item, point1, point2)
    )

    if segment_pair_score(middle_pipe, point1, point2) > POINT_TOL * 10.0:
        raise Exception(u"Không nhận diện được đoạn Pipe nằm giữa hai điểm cắt.")

    outside1 = None
    outside2 = None

    for segment in segments:
        if same_id(segment.Id, middle_pipe.Id):
            continue
        if segment_has_endpoint(segment, point1):
            outside1 = segment
        if segment_has_endpoint(segment, point2):
            outside2 = segment

    if outside1 is None or outside2 is None:
        raise Exception(u"Không nhận diện được hai đoạn Pipe phía ngoài.")

    return middle_pipe, outside1.Id, outside2.Id


def break_pipe_at_point(pipe, point):
    original_id = pipe.Id
    new_id = Plumbing.PlumbingUtils.BreakCurve(doc, original_id, point)
    if not is_valid_element_id(new_id):
        raise Exception(u"BreakCurve thất bại tại mép dầm đã chọn.")

    doc.Regenerate()

    segment1 = doc.GetElement(original_id)
    segment2 = doc.GetElement(new_id)
    if not isinstance(segment1, Plumbing.Pipe) or not isinstance(segment2, Plumbing.Pipe):
        raise Exception(u"Sau khi cắt không thu được hai đoạn Pipe hợp lệ.")

    return segment1, segment2


def segment_mid_parameter(segment, source_line):
    line = get_pipe_line(segment)
    if line is None:
        raise Exception(u"Đoạn Pipe sau khi cắt không còn là Pipe thẳng.")
    midpoint = (line.GetEndPoint(0) + line.GetEndPoint(1)) * 0.5
    return parameter_on_line_xy(source_line, midpoint)


def classify_one_side_segments(segment1, segment2, source_line, break_t, changed_sign):
    t1 = segment_mid_parameter(segment1, source_line)
    t2 = segment_mid_parameter(segment2, source_line)
    score1 = changed_sign * (t1 - break_t)
    score2 = changed_sign * (t2 - break_t)

    if score1 > score2:
        changed = segment1
        unchanged = segment2
    else:
        changed = segment2
        unchanged = segment1

    if max(score1, score2) <= 0.0 or min(score1, score2) >= 0.0:
        raise Exception(u"Không xác định được phía Pipe cần thay đổi cao độ.")

    return unchanged, changed


def validate_one_side_parameters(line, outer_t, inner_t):
    length = line.Length
    if length <= app.ShortCurveTolerance:
        raise Exception(u"Pipe quá ngắn.")

    end_parameter = MIN_END_CLEARANCE / length
    minimum_t = min(outer_t, inner_t)
    maximum_t = max(outer_t, inner_t)

    if minimum_t <= end_parameter:
        missing = (end_parameter - minimum_t) * length
        raise Exception(
            u"Không đủ chiều dài Pipe ở phía đầu. Cần thêm khoảng {0:.1f} mm.".format(
                internal_to_mm(missing)
            )
        )

    if maximum_t >= 1.0 - end_parameter:
        missing = (maximum_t - (1.0 - end_parameter)) * length
        raise Exception(
            u"Không đủ chiều dài Pipe ở phía cuối. Cần thêm khoảng {0:.1f} mm.".format(
                internal_to_mm(missing)
            )
        )


# -----------------------------------------------------------------------------
# Offset geometry + fitting creation
# -----------------------------------------------------------------------------


def validate_break_parameters(line, outer_t1, outer_t2):
    length = line.Length
    if length <= app.ShortCurveTolerance:
        raise Exception(u"Pipe quá ngắn.")

    end_parameter = MIN_END_CLEARANCE / length
    if outer_t1 <= end_parameter:
        missing = (end_parameter - outer_t1) * length
        raise Exception(
            u"Không đủ đoạn thẳng trước dầm. Cần thêm khoảng {0:.1f} mm ở đầu Pipe.".format(
                internal_to_mm(missing)
            )
        )
    if outer_t2 >= 1.0 - end_parameter:
        missing = (outer_t2 - (1.0 - end_parameter)) * length
        raise Exception(
            u"Không đủ đoạn thẳng sau dầm. Cần thêm khoảng {0:.1f} mm ở cuối Pipe.".format(
                internal_to_mm(missing)
            )
        )
    if outer_t2 <= outer_t1:
        raise Exception(u"Hai điểm cắt Pipe không hợp lệ.")


def create_elbow(connector1, connector2, angle_deg):
    if connector1 is None or connector2 is None:
        raise Exception(u"Connector tạo elbow không hợp lệ.")
    try:
        return doc.Create.NewElbowFitting(connector1, connector2)
    except Exception as ex:
        raise Exception(
            u"Không tạo được elbow góc {0:g}°. Kiểm tra Routing Preferences, family elbow và khoảng takeoff. Revit: {1}".format(
                angle_deg, to_text(ex)
            )
        )


def build_offset(middle_pipe,
                 outside_owner_id1,
                 outside_owner_id2,
                 outer_point1,
                 top_point1,
                 top_point2,
                 outer_point2,
                 angle_deg):
    set_pipe_curve(middle_pipe, top_point1, top_point2)
    doc.Regenerate()

    connector_pipe1 = create_pipe_like(middle_pipe, outer_point1, top_point1)
    connector_pipe2 = create_pipe_like(middle_pipe, top_point2, outer_point2)
    doc.Regenerate()

    outside_owner1 = doc.GetElement(outside_owner_id1)
    outside_owner2 = doc.GetElement(outside_owner_id2)

    if outside_owner1 is None or outside_owner2 is None:
        raise Exception(u"Không đọc lại được hai Pipe phía ngoài.")

    outside_connector1 = nearest_connector(outside_owner1, outer_point1, True)
    outside_connector2 = nearest_connector(outside_owner2, outer_point2, True)

    connector_outer1 = nearest_connector(connector_pipe1, outer_point1, True)
    connector_top1 = nearest_connector(connector_pipe1, top_point1, True)
    connector_top2 = nearest_connector(connector_pipe2, top_point2, True)
    connector_outer2 = nearest_connector(connector_pipe2, outer_point2, True)

    middle_connector1 = nearest_connector(middle_pipe, top_point1, True)
    middle_connector2 = nearest_connector(middle_pipe, top_point2, True)

    create_elbow(outside_connector1, connector_outer1, angle_deg)
    create_elbow(connector_top1, middle_connector1, angle_deg)
    create_elbow(middle_connector2, connector_top2, angle_deg)
    create_elbow(connector_outer2, outside_connector2, angle_deg)


def process_beam_pipe(pipe, beam_interval, offset_z, angle_deg, clearance):
    line = get_pipe_line(pipe)
    if line is None:
        raise Exception(u"Chế độ chọn dầm chỉ hỗ trợ Pipe thẳng.")

    xy_len = line_xy_length(line)
    if xy_len <= GEOM_TOL:
        raise Exception(u"Pipe đứng hoặc gần đứng, không thể xử lý.")

    beam_t1, beam_t2 = beam_interval
    run = angle_run(offset_z, angle_deg)

    inner_shift_t = clearance / xy_len
    run_shift_t = run / xy_len

    inner_t1 = beam_t1 - inner_shift_t
    inner_t2 = beam_t2 + inner_shift_t
    outer_t1 = inner_t1 - run_shift_t
    outer_t2 = inner_t2 + run_shift_t

    validate_break_parameters(line, outer_t1, outer_t2)

    outer_point1 = evaluate_line(line, outer_t1)
    outer_point2 = evaluate_line(line, outer_t2)
    inner_base1 = evaluate_line(line, inner_t1)
    inner_base2 = evaluate_line(line, inner_t2)
    top_point1 = xyz_add_z(inner_base1, offset_z)
    top_point2 = xyz_add_z(inner_base2, offset_z)

    middle_pipe, outside_id1, outside_id2 = break_pipe_between_points(
        pipe,
        outer_point1,
        outer_point2
    )

    build_offset(
        middle_pipe,
        outside_id1,
        outside_id2,
        outer_point1,
        top_point1,
        top_point2,
        outer_point2,
        angle_deg
    )


def process_one_side_beam_pipe(pipe, beam_interval, edge_pick_point, offset_z, angle_deg, clearance):
    line = get_pipe_line(pipe)
    if line is None:
        raise Exception(u"Chế độ một phía chỉ hỗ trợ Pipe thẳng.")

    xy_len = line_xy_length(line)
    if xy_len <= GEOM_TOL:
        raise Exception(u"Pipe đứng hoặc gần đứng, không thể xử lý.")

    beam_t1, beam_t2 = beam_interval
    click_t = parameter_on_line_xy(line, edge_pick_point)

    if abs(click_t - beam_t1) <= abs(click_t - beam_t2):
        edge_t = beam_t1
        changed_sign = 1.0
    else:
        edge_t = beam_t2
        changed_sign = -1.0

    run = angle_run(offset_z, angle_deg)
    clearance_t = clearance / xy_len
    run_t = run / xy_len

    # Phia ngoai dam giu nguyen. Doan chuyen cao do nam ngoai mep dam,
    # va ket thuc truoc mep dam mot khoang clearance.
    inner_t = edge_t - changed_sign * clearance_t
    outer_t = inner_t - changed_sign * run_t

    validate_one_side_parameters(line, outer_t, inner_t)

    outer_point = evaluate_line(line, outer_t)
    inner_base = evaluate_line(line, inner_t)
    top_point = xyz_add_z(inner_base, offset_z)

    segment1, segment2 = break_pipe_at_point(pipe, outer_point)
    unchanged_pipe, changed_pipe = classify_one_side_segments(
        segment1, segment2, line, outer_t, changed_sign
    )

    # Thu ngan dau gan mep dam, sau do nang/ha toan bo phia con lai.
    set_pipe_endpoint(changed_pipe, outer_point, inner_base)
    doc.Regenerate()

    DB.ElementTransformUtils.MoveElement(
        doc, changed_pipe.Id, DB.XYZ(0.0, 0.0, offset_z)
    )
    doc.Regenerate()

    transition_pipe = create_pipe_like(changed_pipe, outer_point, top_point)
    doc.Regenerate()

    unchanged_connector = nearest_connector(unchanged_pipe, outer_point, True)
    transition_outer = nearest_connector(transition_pipe, outer_point, True)
    transition_top = nearest_connector(transition_pipe, top_point, True)
    changed_connector = nearest_connector(changed_pipe, top_point, True)

    create_elbow(unchanged_connector, transition_outer, angle_deg)
    create_elbow(transition_top, changed_connector, angle_deg)


def process_union_pipe(pipe, offset_z, angle_deg, clearance):
    boundaries = get_union_boundaries(pipe)
    line = get_pipe_line(pipe)
    if line is None:
        raise Exception(u"Đoạn Pipe giữa hai Union phải là Pipe thẳng.")

    xy_len = line_xy_length(line)
    if xy_len <= GEOM_TOL:
        raise Exception(u"Pipe đứng hoặc gần đứng, không thể tạo đoạn xiên.")

    run = angle_run(offset_z, angle_deg)
    total_outward = clearance + run

    boundary1 = boundaries[0]
    boundary2 = boundaries[1]

    outside1 = doc.GetElement(boundary1["owner_id"])
    outside2 = doc.GetElement(boundary2["owner_id"])

    if not isinstance(outside1, Plumbing.Pipe) or not isinstance(outside2, Plumbing.Pipe):
        raise Exception(
            u"Hai phía ngoài Union phải nối trực tiếp với Pipe để có thể tạo góc {0:g}°.".format(
                angle_deg
            )
        )

    p1 = boundary1["point"]
    p2 = boundary2["point"]

    t1 = parameter_on_line_xy(line, p1)
    t2 = parameter_on_line_xy(line, p2)
    if t1 > t2:
        t1, t2 = t2, t1
        boundary1, boundary2 = boundary2, boundary1
        outside1, outside2 = outside2, outside1
        p1, p2 = p2, p1

    clearance_t = clearance / xy_len
    run_t = run / xy_len

    inner_t1 = t1 - clearance_t
    inner_t2 = t2 + clearance_t
    outer_t1 = inner_t1 - run_t
    outer_t2 = inner_t2 + run_t

    outer_point1 = evaluate_line(line, outer_t1)
    outer_point2 = evaluate_line(line, outer_t2)
    inner_base1 = evaluate_line(line, inner_t1)
    inner_base2 = evaluate_line(line, inner_t2)
    top_point1 = xyz_add_z(inner_base1, offset_z)
    top_point2 = xyz_add_z(inner_base2, offset_z)

    delete_ids = List[DB.ElementId]()
    delete_ids.Add(boundary1["union_id"])
    delete_ids.Add(boundary2["union_id"])
    doc.Delete(delete_ids)
    doc.Regenerate()

    set_pipe_endpoint(outside1, p1, outer_point1)
    set_pipe_endpoint(outside2, p2, outer_point2)
    doc.Regenerate()

    build_offset(
        pipe,
        outside1.Id,
        outside2.Id,
        outer_point1,
        top_point1,
        top_point2,
        outer_point2,
        angle_deg
    )


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main():
    settings = show_settings()
    if settings is None:
        return

    pipes = get_selected_pipes()
    if not pipes:
        return

    offset_mm = settings["offset_mm"]
    profile_mode = settings["profile_mode"]

    if profile_mode == PROFILE_ONE_SIDE_UP:
        offset_mm = abs(offset_mm)
    elif profile_mode == PROFILE_ONE_SIDE_DOWN:
        offset_mm = -abs(offset_mm)

    offset_z = mm_to_internal(offset_mm)
    angle_deg = settings["angle_deg"]
    clearance = mm_to_internal(settings["clearance_mm"])
    mode = settings["mode"]

    beam_geometry = None
    beam_intervals = {}
    edge_pick_point = None

    if mode == MODE_BEAM:
        try:
            beam_context = pick_beam_context()
            beam_geometry = build_beam_geometry(beam_context)

            if profile_mode in [PROFILE_ONE_SIDE_UP, PROFILE_ONE_SIDE_DOWN]:
                edge_pick_point = pick_beam_edge_point(beam_context)
        except OperationCanceledException:
            return
        except Exception as ex:
            forms.alert(
                u"Không đọc được dầm đã chọn:\n{0}".format(to_text(ex)),
                title=u"Pipe Local Up/Down",
                exitscript=True
            )
            return

        for source_pipe in pipes:
            pipe_key = id_value(source_pipe.Id)
            try:
                beam_intervals[pipe_key] = {
                    "interval": get_beam_interval(source_pipe, beam_geometry),
                    "error": None
                }
            except Exception as ex:
                beam_intervals[pipe_key] = {
                    "interval": None,
                    "error": to_text(ex)
                }

    success_ids = []
    failed = []

    transaction_group = DB.TransactionGroup(doc, u"Pipe Local Up Down")
    transaction_group.Start()

    try:
        for source_pipe in pipes:
            pipe_id_text = to_text(id_value(source_pipe.Id))
            transaction = DB.Transaction(
                doc,
                u"Up Down Pipe {0}".format(pipe_id_text)
            )
            transaction.Start()

            try:
                current_pipe = doc.GetElement(source_pipe.Id)
                if current_pipe is None:
                    raise Exception(u"Pipe không còn tồn tại trong model.")

                if mode == MODE_BEAM:
                    interval_data = beam_intervals[id_value(source_pipe.Id)]
                    if interval_data["error"]:
                        raise Exception(interval_data["error"])

                    if profile_mode == PROFILE_LOCAL:
                        process_beam_pipe(
                            current_pipe,
                            interval_data["interval"],
                            offset_z,
                            angle_deg,
                            clearance
                        )
                    else:
                        process_one_side_beam_pipe(
                            current_pipe,
                            interval_data["interval"],
                            edge_pick_point,
                            offset_z,
                            angle_deg,
                            clearance
                        )
                else:
                    process_union_pipe(
                        current_pipe,
                        offset_z,
                        angle_deg,
                        clearance
                    )

                transaction.Commit()
                success_ids.append(pipe_id_text)

            except Exception as ex:
                if transaction.GetStatus() == DB.TransactionStatus.Started:
                    transaction.RollBack()

                failed.append({
                    "id": pipe_id_text,
                    "reason": to_text(ex),
                    "trace": traceback.format_exc()
                })

        transaction_group.Assimilate()

    except Exception:
        if transaction_group.GetStatus() == DB.TransactionStatus.Started:
            transaction_group.RollBack()
        raise

    if failed:
        output.print_md(u"## Pipe Local Up/Down - Chi tiết lỗi")
        for item in failed:
            output.print_md(
                u"**Pipe ID {0}:** {1}".format(item["id"], item["reason"])
            )
            output.print_code(item["trace"])

    direction = u"UP" if offset_mm > 0 else u"DOWN"
    mode_text = u"Chọn trực tiếp dầm" if mode == MODE_BEAM else u"Giữa hai Union"

    if profile_mode == PROFILE_ONE_SIDE_UP:
        profile_text = u"Một phía - UP"
    elif profile_mode == PROFILE_ONE_SIDE_DOWN:
        profile_text = u"Một phía - DOWN"
    else:
        profile_text = u"Né cục bộ hai phía"

    message = (
        u"Hoàn thành {0} {1:.1f} mm.\n"
        u"Góc: {2:g}°\n"
        u"Phương pháp: {3}\n"
        u"Kiểu hình học: {4}\n\n"
        u"Thành công: {5}/{6}\n"
        u"Không xử lý được: {7}"
    ).format(
        direction,
        abs(offset_mm),
        angle_deg,
        mode_text,
        profile_text,
        len(success_ids),
        len(pipes),
        len(failed)
    )

    if failed:
        message += u"\n\nChi tiết lỗi đã được ghi trong pyRevit Output."

    forms.alert(message, title=u"Pipe Local Up/Down")


if __name__ == "__main__":
    main()
