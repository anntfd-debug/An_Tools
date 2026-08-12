# -*- coding: utf-8 -*-
__title__ = "Find Orphaned Sleeve"
__doc__ = (
    "Tim Sleeve khong co Pipe/Pipe Fitting/Pipe Accessory/"
    "Plumbing Fixture di qua; BoundingBox lay tu Solid thuc. "
    "Cho phep chon Isolate hoac chi xuat bao cao; ID co lien ket Zoom."
)

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List
from pyrevit import forms, revit, script

uidoc = revit.uidoc
doc = revit.doc
active_view = doc.ActiveView
output = script.get_output()


# -----------------------------------------------------------------------------
# CAU HINH
# -----------------------------------------------------------------------------
# Chi dung dung sai nay de tim ung vien gan Sleeve.
# Khi ket luan co doi tuong di qua, code dung BoundingBox goc cua Sleeve.
BBOX_SEARCH_TOLERANCE_MM = 5.0

# The tich giao nhau nho hon gia tri nay duoc coi la tiep xuc so hoc,
# khong du de ket luan doi tuong di qua Sleeve.
MIN_INTERSECTION_VOLUME_MM3 = 1.0

# True: in bang ly do nhung Sleeve da co doi tuong di qua.
# Huu ich de tim doi tuong nao dang lam Sleeve bi nhan la da su dung.
SHOW_DEBUG_REPORT = True
MAX_DEBUG_ROWS = 300

MODE_ISOLATE = "Isolate Sleeve mo coi"
MODE_REPORT_ONLY = "Chi bao cao - khong isolate"

SOURCE_CATEGORIES = [
    BuiltInCategory.OST_PipeCurves,
    BuiltInCategory.OST_PipeFitting,
    BuiltInCategory.OST_PipeAccessory,
    BuiltInCategory.OST_PlumbingFixtures,
]


class FamilyInstanceSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, FamilyInstance)

    def AllowReference(self, ref, pos):
        return False


def mm_to_internal(value_mm):
    try:
        return UnitUtils.ConvertToInternalUnits(
            value_mm,
            UnitTypeId.Millimeters
        )
    except Exception:
        return UnitUtils.ConvertToInternalUnits(
            value_mm,
            DisplayUnitType.DUT_MILLIMETERS
        )


def mm3_to_internal(value_mm3):
    """1 ft = 304.8 mm; Revit Solid.Volume dung ft3."""
    return float(value_mm3) / (304.8 * 304.8 * 304.8)


def id_value(element_id):
    try:
        return element_id.Value
    except Exception:
        return element_id.IntegerValue


def get_category_name(elem):
    try:
        if elem.Category:
            return elem.Category.Name
    except Exception:
        pass
    return ""


def get_family_and_type_name(elem):
    family_name = ""
    type_name = ""

    try:
        if isinstance(elem, FamilyInstance) and elem.Symbol:
            type_name = elem.Symbol.Name
            if elem.Symbol.Family:
                family_name = elem.Symbol.Family.Name
    except Exception:
        pass

    if not type_name:
        try:
            elem_type = doc.GetElement(elem.GetTypeId())
            if elem_type:
                type_name = elem_type.Name
        except Exception:
            pass

    return family_name, type_name


def collect_solids_recursive(geometry_element, solids):
    if geometry_element is None:
        return

    for geom_obj in geometry_element:
        if isinstance(geom_obj, Solid):
            try:
                if geom_obj.Volume > 1e-9 and geom_obj.Faces.Size > 0:
                    solids.append(geom_obj)
            except Exception:
                pass

        elif isinstance(geom_obj, GeometryInstance):
            # GetInstanceGeometry tra geometry da transform ve he toa do model.
            try:
                collect_solids_recursive(
                    geom_obj.GetInstanceGeometry(),
                    solids
                )
            except Exception:
                pass


def get_element_solids(elem, options):
    solids = []
    try:
        collect_solids_recursive(
            elem.get_Geometry(options),
            solids
        )
    except Exception:
        pass
    return solids


def get_bbox_corners(bbox):
    """
    Tra 8 goc BoundingBoxXYZ ve he toa do model.

    Solid.GetBoundingBox() co the tra Min/Max trong he toa do cuc bo,
    vi vay phai ap dung bbox.Transform thay vi dung truc tiep Min/Max.
    """
    if bbox is None:
        return []

    local_corners = [
        XYZ(bbox.Min.X, bbox.Min.Y, bbox.Min.Z),
        XYZ(bbox.Min.X, bbox.Min.Y, bbox.Max.Z),
        XYZ(bbox.Min.X, bbox.Max.Y, bbox.Min.Z),
        XYZ(bbox.Min.X, bbox.Max.Y, bbox.Max.Z),
        XYZ(bbox.Max.X, bbox.Min.Y, bbox.Min.Z),
        XYZ(bbox.Max.X, bbox.Min.Y, bbox.Max.Z),
        XYZ(bbox.Max.X, bbox.Max.Y, bbox.Min.Z),
        XYZ(bbox.Max.X, bbox.Max.Y, bbox.Max.Z),
    ]

    try:
        transform = bbox.Transform
    except Exception:
        transform = None

    if transform is None:
        return local_corners

    model_corners = []
    for point in local_corners:
        try:
            model_corners.append(transform.OfPoint(point))
        except Exception:
            model_corners.append(point)

    return model_corners


def get_bounds_from_solids(solids):
    """
    Tao Axis-Aligned BoundingBox chi tu Solid 3D that cua Sleeve.

    Ham nay bo qua:
    - Room Calculation Point
    - symbolic line
    - model line khong co the tich
    - flip control va cac control geometry
    - annotation geometry

    Tra ve (box_min, box_max), hoac (None, None) neu khong co Solid hop le.
    """
    all_points = []

    for solid in solids:
        try:
            solid_bbox = solid.GetBoundingBox()
        except Exception:
            solid_bbox = None

        if solid_bbox is None:
            continue

        all_points.extend(
            get_bbox_corners(solid_bbox)
        )

    if not all_points:
        return None, None

    min_x = min(point.X for point in all_points)
    min_y = min(point.Y for point in all_points)
    min_z = min(point.Z for point in all_points)

    max_x = max(point.X for point in all_points)
    max_y = max(point.Y for point in all_points)
    max_z = max(point.Z for point in all_points)

    return (
        XYZ(min_x, min_y, min_z),
        XYZ(max_x, max_y, max_z)
    )


def get_sleeve_core_bounds(sleeve, geometry_options):
    """
    Uu tien BoundingBox tao tu Solid thuc.

    Chi fallback ve FamilyInstance.get_BoundingBox(None) khi Family
    khong co bat ky Solid co the tich nao. Fallback co the van bi anh
    huong boi calculation point, nen duoc ghi ro de debug.
    """
    sleeve_solids = get_element_solids(
        sleeve,
        geometry_options
    )

    solid_min, solid_max = get_bounds_from_solids(
        sleeve_solids
    )

    if solid_min is not None and solid_max is not None:
        return solid_min, solid_max, sleeve_solids, "Solid geometry"

    try:
        fallback_bbox = sleeve.get_BoundingBox(None)
    except Exception:
        fallback_bbox = None

    if fallback_bbox is None:
        return None, None, sleeve_solids, "No bounding box"

    return (
        XYZ(
            fallback_bbox.Min.X,
            fallback_bbox.Min.Y,
            fallback_bbox.Min.Z
        ),
        XYZ(
            fallback_bbox.Max.X,
            fallback_bbox.Max.Y,
            fallback_bbox.Max.Z
        ),
        sleeve_solids,
        "Family fallback"
    )


def point_in_box(point, box_min, box_max):
    if point is None:
        return False

    return (
        box_min.X <= point.X <= box_max.X
        and box_min.Y <= point.Y <= box_max.Y
        and box_min.Z <= point.Z <= box_max.Z
    )


def segment_intersects_box(point_0, point_1, box_min, box_max):
    """Slab algorithm cho doan thang va hop truc X/Y/Z."""
    t_min = 0.0
    t_max = 1.0

    values = (
        (point_0.X, point_1.X, box_min.X, box_max.X),
        (point_0.Y, point_1.Y, box_min.Y, box_max.Y),
        (point_0.Z, point_1.Z, box_min.Z, box_max.Z),
    )

    for start_value, end_value, min_value, max_value in values:
        direction = end_value - start_value

        if abs(direction) < 1e-12:
            if start_value < min_value or start_value > max_value:
                return False
            continue

        t_1 = (min_value - start_value) / direction
        t_2 = (max_value - start_value) / direction

        if t_1 > t_2:
            t_1, t_2 = t_2, t_1

        if t_1 > t_min:
            t_min = t_1
        if t_2 < t_max:
            t_max = t_2

        if t_min > t_max:
            return False

    return True


def curve_intersects_box(curve, box_min, box_max):
    try:
        points = list(curve.Tessellate())
    except Exception:
        points = []

    if len(points) < 2:
        try:
            points = [
                curve.GetEndPoint(0),
                curve.GetEndPoint(1)
            ]
        except Exception:
            return False

    for index in range(len(points) - 1):
        if segment_intersects_box(
            points[index],
            points[index + 1],
            box_min,
            box_max
        ):
            return True

    return False


def is_pipe(elem):
    try:
        return (
            elem.Category is not None
            and elem.Category.Id == ElementId(
                BuiltInCategory.OST_PipeCurves
            )
        )
    except Exception:
        return False


def pipe_centerline_intersects_box(pipe, box_min, box_max):
    try:
        location = pipe.Location
        if not isinstance(location, LocationCurve):
            return False

        return curve_intersects_box(
            location.Curve,
            box_min,
            box_max
        )
    except Exception:
        return False


def get_connector_origins(elem):
    origins = []

    connector_manager = None

    try:
        # MEPCurve
        connector_manager = elem.ConnectorManager
    except Exception:
        connector_manager = None

    if connector_manager is None:
        try:
            # FamilyInstance: Pipe Fitting, Pipe Accessory, Plumbing Fixture
            if elem.MEPModel:
                connector_manager = elem.MEPModel.ConnectorManager
        except Exception:
            connector_manager = None

    if connector_manager is None:
        return origins

    try:
        for connector in connector_manager.Connectors:
            try:
                origins.append(connector.Origin)
            except Exception:
                pass
    except Exception:
        pass

    return origins


def connector_inside_box(elem, box_min, box_max):
    for origin in get_connector_origins(elem):
        if point_in_box(origin, box_min, box_max):
            return True
    return False


def location_point_inside_box(elem, box_min, box_max):
    try:
        location = elem.Location
        if isinstance(location, LocationPoint):
            return point_in_box(
                location.Point,
                box_min,
                box_max
            )
    except Exception:
        pass

    return False


def bbox_center_inside_box(elem, box_min, box_max):
    try:
        bbox = elem.get_BoundingBox(None)
        if bbox is None:
            return False

        center = (bbox.Min + bbox.Max) * 0.5
        return point_in_box(center, box_min, box_max)
    except Exception:
        return False


def has_positive_solid_intersection(
    sleeve_solids,
    candidate,
    geometry_options,
    min_volume
):
    """
    Khong dung ElementIntersectsSolidFilter de ket luan cuoi cung.
    Filter do co the tra hit khi chi cham bien hoac khi geometry rat gan nhau.

    O day tinh Boolean intersection va chi chap nhan khi co the tich duong
    lon hon nguong cau hinh.
    """
    candidate_solids = get_element_solids(
        candidate,
        geometry_options
    )

    if not candidate_solids:
        return False

    for sleeve_solid in sleeve_solids:
        for candidate_solid in candidate_solids:
            try:
                intersection = BooleanOperationsUtils.ExecuteBooleanOperation(
                    sleeve_solid,
                    candidate_solid,
                    BooleanOperationsType.Intersect
                )

                if (
                    intersection is not None
                    and intersection.Volume > min_volume
                ):
                    return True
            except Exception:
                # Geometry loi Boolean khong duoc xem la bang chung co va cham.
                continue

    return False


def get_occupancy_reason(
    candidate,
    sleeve_solids,
    core_min,
    core_max,
    geometry_options,
    min_intersection_volume
):
    """
    Tra ve ly do neu candidate thuc su duoc xem la di qua Sleeve.
    Tra ve None neu candidate chi nam gan Sleeve.
    """

    # Pipe chi duoc cong nhan khi tim ong di qua BoundingBox goc.
    # Khong dung Solid clash cho Pipe de tranh ong nam sat ngoai Sleeve
    # lam Sleeve bi nhan nham la da su dung.
    if is_pipe(candidate):
        if pipe_centerline_intersects_box(
            candidate,
            core_min,
            core_max
        ):
            return "Pipe centerline"
        return None

    # Fitting/Accessory/Fixture: uu tien connector hoac tam dat Family.
    if connector_inside_box(candidate, core_min, core_max):
        return "Connector inside"

    if location_point_inside_box(candidate, core_min, core_max):
        return "Location point inside"

    if bbox_center_inside_box(candidate, core_min, core_max):
        return "Element center inside"

    # Solid overlap chi la fallback cuoi, va phai co the tich giao duong.
    if sleeve_solids and has_positive_solid_intersection(
        sleeve_solids,
        candidate,
        geometry_options,
        min_intersection_volume
    ):
        return "Positive solid overlap"

    return None


def choose_result_mode():
    """
    Tra ve:
    - True: isolate ket qua sau khi quet.
    - False: chi xuat pyRevit Output, khong thay doi hien thi.
    - None: nguoi dung huy.
    """
    selected_mode = forms.CommandSwitchWindow.show(
        [
            MODE_ISOLATE,
            MODE_REPORT_ONLY
        ],
        message="Chon cach xu ly Sleeve mo coi"
    )

    if not selected_mode:
        return None

    return selected_mode == MODE_ISOLATE


def print_orphan_report(orphaned_ids, should_isolate):
    """
    In tung Sleeve mo coi ra pyRevit Output.

    output.linkify tao link Revit cho ElementId:
    - bam vao ID: select element;
    - bam bieu tu kinh lup ben canh: show/zoom element.
    """
    if not orphaned_ids:
        return

    output.print_md("## Sleeve mo coi")
    output.print_md(
        "**Tong so:** {}  \n"
        "**Che do:** {}"
        .format(
            len(orphaned_ids),
            MODE_ISOLATE if should_isolate else MODE_REPORT_ONLY
        )
    )
    output.print_md(
        "_Bam vao ID de select; bam bieu tu kinh lup ben canh ID "
        "de zoom den vi tri Sleeve._"
    )

    for index, element_id in enumerate(orphaned_ids):
        sleeve = doc.GetElement(element_id)
        family_name, type_name = get_family_and_type_name(sleeve)

        if not family_name:
            family_name = "-"
        if not type_name:
            type_name = "-"

        element_id_text = str(id_value(element_id))
        element_link = output.linkify(
            element_id,
            title=element_id_text
        )

        print(
            "{0}. ID {1} | Family: {2} | Type: {3}"
            .format(
                index + 1,
                element_link,
                family_name,
                type_name
            )
        )


def isolate_results(element_ids):
    """
    Isolate tam thoi cac Sleeve mo coi.

    Tra ve True neu isolate thanh cong.
    Tra ve False neu view khong ho tro hoac xay ra loi. Moi thong bao
    deu duoc ghi vao pyRevit Output, khong dung alert/toast.
    """
    if not element_ids:
        return False

    try:
        if not active_view.CanUseTemporaryVisibilityModes():
            uidoc.Selection.SetElementIds(
                List[ElementId](element_ids)
            )
            output.print_md(
                "**Khong the isolate:** View hien tai khong ho tro "
                "Temporary Hide/Isolate. Da select {} Sleeve mo coi."
                .format(len(element_ids))
            )
            return False
    except Exception:
        # Neu API khong doc duoc kha nang isolate, van thu isolate ben duoi.
        pass

    try:
        with revit.Transaction("Isolate orphaned sleeves"):
            active_view.IsolateElementsTemporary(
                List[ElementId](element_ids)
            )

        uidoc.RefreshActiveView()
        return True

    except Exception as ex:
        output.print_md(
            "**Loi isolate:** `{}`"
            .format(ex)
        )
        return False


def print_debug_report(debug_rows):
    if not SHOW_DEBUG_REPORT or not debug_rows:
        return

    rows = debug_rows[:MAX_DEBUG_ROWS]

    output.print_md("## Sleeve da co doi tuong di qua va ly do")
    output.print_table(
        table_data=rows,
        columns=[
            "Sleeve Id",
            "Source Id",
            "Category",
            "Family",
            "Type",
            "Reason",
            "Sleeve bbox"
        ]
    )

    if len(debug_rows) > MAX_DEBUG_ROWS:
        output.print_md(
            "_Chi hien thi {} / {} dong dau tien._"
            .format(MAX_DEBUG_ROWS, len(debug_rows))
        )


def main():
    # Chon che do truoc, sau do moi yeu cau pick Family Sleeve.
    should_isolate = choose_result_mode()
    if should_isolate is None:
        return

    try:
        picked_ref = uidoc.Selection.PickObject(
            ObjectType.Element,
            FamilyInstanceSelectionFilter(),
            "Chon mot instance thuoc Family Sleeve can kiem tra..."
        )
        sample_instance = doc.GetElement(
            picked_ref.ElementId
        )

    except OperationCanceledException:
        return
    except Exception as ex:
        output.print_md("## Loi chon Family Sleeve")
        output.print_md("`{}`".format(ex))
        return

    if (
        sample_instance is None
        or sample_instance.Symbol is None
    ):
        output.print_md("## Khong the kiem tra Sleeve")
        output.print_md(
            "Doi tuong duoc chon khong co FamilySymbol hop le."
        )
        return

    family_symbol_ids = (
        sample_instance.Symbol.Family.GetFamilySymbolIds()
    )
    family_symbol_keys = set(
        id_value(symbol_id)
        for symbol_id in family_symbol_ids
    )

    all_instances = (
        FilteredElementCollector(doc, active_view.Id)
        .OfClass(FamilyInstance)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    sleeve_instances = [
        instance
        for instance in all_instances
        if id_value(instance.GetTypeId()) in family_symbol_keys
    ]

    if not sleeve_instances:
        output.print_md("## Ket qua kiem tra Sleeve")
        output.print_md(
            "Khong tim thay instance nao cua Family da chon "
            "trong active view."
        )
        return

    source_filter = ElementMulticategoryFilter(
        List[BuiltInCategory](SOURCE_CATEGORIES)
    )

    all_source_ids = (
        FilteredElementCollector(doc, active_view.Id)
        .WhereElementIsNotElementType()
        .WherePasses(source_filter)
        .ToElementIds()
    )

    # Loai tru toan bo instance thuoc chinh Family Sleeve duoc chon.
    sleeve_id_keys = set(
        id_value(instance.Id)
        for instance in sleeve_instances
    )

    source_ids = List[ElementId]([
        element_id
        for element_id in all_source_ids
        if id_value(element_id) not in sleeve_id_keys
    ])

    geometry_options = Options()
    geometry_options.ComputeReferences = False
    geometry_options.DetailLevel = ViewDetailLevel.Fine

    # Doc ca geometry bi an de tranh mot so type Family khong lay duoc Solid.
    geometry_options.IncludeNonVisibleObjects = True

    search_tolerance = mm_to_internal(
        BBOX_SEARCH_TOLERANCE_MM
    )
    min_intersection_volume = mm3_to_internal(
        MIN_INTERSECTION_VOLUME_MM3
    )

    orphaned_ids = []
    debug_rows = []
    total_items = len(sleeve_instances)

    with forms.ProgressBar(
        title="Dang kiem tra Sleeve {value}/{max_value}",
        cancellable=True
    ) as progress_bar:

        for index, sleeve in enumerate(sleeve_instances):
            if progress_bar.cancelled:
                output.print_md("## Da huy kiem tra Sleeve")
                output.print_md(
                    "Qua trinh da dung tai {}/{} Sleeve."
                    .format(index, total_items)
                )
                return

            progress_bar.update_progress(
                index + 1,
                total_items
            )

            # Khong dung sleeve.get_BoundingBox(None) lam nguon chinh.
            # Family BoundingBox co the bi mo rong boi Room Calculation Point,
            # symbolic line, flip control hoac geometry dieu khien.
            (
                core_min,
                core_max,
                sleeve_solids,
                bbox_source
            ) = get_sleeve_core_bounds(
                sleeve,
                geometry_options
            )

            if core_min is None or core_max is None:
                # Khong co bang chung hinh hoc cho thay Sleeve dang duoc su dung.
                orphaned_ids.append(sleeve.Id)
                continue

            # Search box: chi dung de thu hep danh sach candidate.
            # Ket luan occupied van dung core_min/core_max tu Solid thuc.
            search_min = XYZ(
                core_min.X - search_tolerance,
                core_min.Y - search_tolerance,
                core_min.Z - search_tolerance
            )
            search_max = XYZ(
                core_max.X + search_tolerance,
                core_max.Y + search_tolerance,
                core_max.Z + search_tolerance
            )

            if source_ids.Count == 0:
                orphaned_ids.append(sleeve.Id)
                continue

            candidate_ids = (
                FilteredElementCollector(doc, source_ids)
                .WherePasses(
                    BoundingBoxIntersectsFilter(
                        Outline(search_min, search_max)
                    )
                )
                .ToElementIds()
            )

            if candidate_ids.Count == 0:
                orphaned_ids.append(sleeve.Id)
                continue

            # sleeve_solids da duoc lay cung luc voi Solid BoundingBox,
            # khong can doc geometry lan thu hai.
            occupied = False

            for candidate_id in candidate_ids:
                candidate = doc.GetElement(candidate_id)

                if candidate is None:
                    continue

                reason = get_occupancy_reason(
                    candidate,
                    sleeve_solids,
                    core_min,
                    core_max,
                    geometry_options,
                    min_intersection_volume
                )

                if reason:
                    occupied = True

                    if SHOW_DEBUG_REPORT:
                        family_name, type_name = (
                            get_family_and_type_name(candidate)
                        )
                        debug_rows.append([
                            str(id_value(sleeve.Id)),
                            str(id_value(candidate.Id)),
                            get_category_name(candidate),
                            family_name,
                            type_name,
                            reason,
                            bbox_source
                        ])
                    break

            if not occupied:
                orphaned_ids.append(sleeve.Id)

    if orphaned_ids:
        # Luon in ket qua de co the bam ID/kinh lup zoom tung vi tri.
        print_orphan_report(
            orphaned_ids,
            should_isolate
        )
        print_debug_report(debug_rows)

        if should_isolate:
            isolate_succeeded = isolate_results(orphaned_ids)

            if isolate_succeeded:
                output.print_md(
                    "**Trang thai:** Da isolate tam thoi {} Sleeve mo coi.  \n"
                    "Dung Reset Temporary Hide/Isolate de khoi phuc hien thi."
                    .format(len(orphaned_ids))
                )
        else:
            output.print_md(
                "**Trang thai:** Chi xuat bao cao; khong thay doi "
                "hien thi cua view."
            )
    else:
        output.print_md("## Ket qua kiem tra Sleeve")
        output.print_md(
            "**Khong tim thay Sleeve mo coi.** Tat ca {} Sleeve cua "
            "Family da chon deu co doi tuong di qua."
            .format(total_items)
        )
        print_debug_report(debug_rows)


if __name__ == "__main__":
    main()
