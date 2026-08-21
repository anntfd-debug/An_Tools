# -*- coding: utf-8 -*-
"""
Select Same Piping System Below Active View Level 0.00

Workflow:
1. Preselect 1 Pipe / Pipe Fitting / Pipe Accessory
2. Run command
3. Read its piping System Name
4. Find Pipe / Pipe Fitting / Pipe Accessory in same System Name
5. Keep only elements whose highest centerline/connector Z
   is <= 0.00 elevation of Active View Level
6. Select matching elements

Compatible conceptually with Revit 2025 / 2026 + pyRevit.
"""

from pyrevit import revit
from Autodesk.Revit import DB
from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

from System.Collections.Generic import List


# ============================================================
# SETTINGS
# ============================================================

# Tolerance cao độ để tránh lỗi số thực.
# Revit internal unit = feet.
Z_TOLERANCE_MM = 1.0
Z_TOLERANCE_FT = Z_TOLERANCE_MM / 304.8

# True:
# phần tử dùng làm nguồn luôn được giữ trong Selection,
# ngay cả khi chính nó nằm trên Level 0.00.
#
# False:
# kết quả cuối cùng tuyệt đối chỉ chứa element <= Level 0.00.
KEEP_SOURCE_ALWAYS = True


# ============================================================
# REVIT CONTEXT
# ============================================================

uidoc = revit.uidoc
doc = revit.doc
view = doc.ActiveView


# ============================================================
# TARGET CATEGORIES
# ============================================================

TARGET_BICS = (
    DB.BuiltInCategory.OST_PipeCurves,
    DB.BuiltInCategory.OST_PipeFitting,
    DB.BuiltInCategory.OST_PipeAccessory,
)

TARGET_CAT_IDS = set()

for bic in TARGET_BICS:
    try:
        TARGET_CAT_IDS.add(
            DB.ElementId(bic).IntegerValue
        )
    except:
        pass


# ============================================================
# HELPERS
# ============================================================

def is_target_element(elem):
    """Pipe / Pipe Fitting / Pipe Accessory only."""

    if elem is None:
        return False

    try:
        cat = elem.Category
        if cat is None:
            return False

        return cat.Id.IntegerValue in TARGET_CAT_IDS

    except:
        return False


def get_connector_manager(elem):
    """
    Return ConnectorManager for:
    - Pipe / MEPCurve
    - FamilyInstance such as Pipe Fitting / Pipe Accessory
    """

    if elem is None:
        return None

    # --------------------------------------------------------
    # MEPCurve, e.g. Pipe
    # --------------------------------------------------------

    try:
        cm = elem.ConnectorManager
        if cm is not None:
            return cm
    except:
        pass

    # --------------------------------------------------------
    # FamilyInstance
    # --------------------------------------------------------

    try:
        mep_model = elem.MEPModel

        if mep_model is not None:
            cm = mep_model.ConnectorManager

            if cm is not None:
                return cm
    except:
        pass

    return None


def get_system_names(elem):
    """
    Return a set of actual MEP System names.

    Priority:
    1. Element.MEPSystem
    2. Connector.MEPSystem
    3. Built-in parameter RBS_SYSTEM_NAME_PARAM

    Using BuiltInParameter avoids depending on UI language.
    """

    names = set()

    # --------------------------------------------------------
    # Direct MEPSystem
    # Typical for Pipe
    # --------------------------------------------------------

    try:
        system = elem.MEPSystem

        if system is not None:
            name = system.Name

            if name:
                names.add(name.strip())
    except:
        pass

    # --------------------------------------------------------
    # Connector systems
    # Useful for fittings / accessories
    # --------------------------------------------------------

    cm = get_connector_manager(elem)

    if cm is not None:
        try:
            connectors = cm.Connectors

            for connector in connectors:
                try:
                    system = connector.MEPSystem

                    if system is None:
                        continue

                    name = system.Name

                    if name:
                        names.add(name.strip())

                except:
                    pass

        except:
            pass

    # --------------------------------------------------------
    # Built-in System Name fallback
    # --------------------------------------------------------

    if not names:
        try:
            param = elem.get_Parameter(
                DB.BuiltInParameter.RBS_SYSTEM_NAME_PARAM
            )

            if param is not None:
                value = param.AsString()

                if value:
                    names.add(value.strip())

        except:
            pass

    return names


def get_pipe_top_centerline_z(elem):
    """
    For Pipe:
    return highest Z of its LocationCurve endpoints.

    This means:
        Horizontal pipe:
            Z = centerline elevation

        Sloped pipe:
            Z = higher endpoint

        Vertical pipe:
            Z = upper endpoint
    """

    try:
        location = elem.Location

        if isinstance(location, DB.LocationCurve):
            curve = location.Curve

            if curve is not None:
                p0 = curve.GetEndPoint(0)
                p1 = curve.GetEndPoint(1)

                return max(p0.Z, p1.Z)

    except:
        pass

    return None


def get_connector_top_z(elem):
    """
    For fitting/accessory:
    use highest connector origin.
    """

    cm = get_connector_manager(elem)

    if cm is None:
        return None

    values = []

    try:
        for connector in cm.Connectors:
            try:
                origin = connector.Origin

                if origin is not None:
                    values.append(origin.Z)

            except:
                pass
    except:
        pass

    if not values:
        return None

    return max(values)


def get_location_point_z(elem):
    """Fallback for point-based elements."""

    try:
        location = elem.Location

        if isinstance(location, DB.LocationPoint):
            point = location.Point

            if point is not None:
                return point.Z

    except:
        pass

    return None


def get_bbox_center_z(elem):
    """
    Last-resort fallback.
    Uses bounding-box center, NOT top of geometry.
    """

    try:
        bbox = elem.get_BoundingBox(None)

        if bbox is None:
            return None

        return (bbox.Min.Z + bbox.Max.Z) * 0.5

    except:
        return None


def get_reference_z(elem):
    """
    Get a meaningful MEP elevation.

    Pipe:
        highest centerline endpoint

    Fitting / Accessory:
        highest connector elevation

    Fallback:
        LocationPoint
        BoundingBox center
    """

    # --------------------------------------------------------
    # Pipe
    # --------------------------------------------------------

    try:
        if elem.Category.Id.IntegerValue == DB.ElementId(
            DB.BuiltInCategory.OST_PipeCurves
        ).IntegerValue:

            value = get_pipe_top_centerline_z(elem)

            if value is not None:
                return value

    except:
        pass

    # --------------------------------------------------------
    # Fitting / Accessory
    # --------------------------------------------------------

    value = get_connector_top_z(elem)

    if value is not None:
        return value

    # --------------------------------------------------------
    # Fallback location
    # --------------------------------------------------------

    value = get_location_point_z(elem)

    if value is not None:
        return value

    # --------------------------------------------------------
    # Last fallback
    # --------------------------------------------------------

    return get_bbox_center_z(elem)


def get_active_view_level(view):
    """
    Get Level associated with Active View.

    Normally works for:
    - Floor Plan
    - Ceiling Plan
    - Engineering Plan
    """

    try:
        level = view.GenLevel

        if level is not None:
            return level
    except:
        pass

    return None


def get_level_project_z(level):
    """
    ProjectElevation is preferred because it is relative
    to Project Origin regardless of Level Elevation Base.
    """

    try:
        return level.ProjectElevation
    except:
        pass

    try:
        return level.Elevation
    except:
        return None


def collect_target_elements():
    """
    Collect all:
    - Pipes
    - Pipe Fittings
    - Pipe Accessories

    from HOST document.
    """

    result = []
    seen = set()

    for bic in TARGET_BICS:

        try:
            collector = (
                DB.FilteredElementCollector(doc)
                .OfCategory(bic)
                .WhereElementIsNotElementType()
            )

            for elem in collector:

                try:
                    eid = elem.Id.IntegerValue
                except:
                    continue

                if eid in seen:
                    continue

                seen.add(eid)
                result.append(elem)

        except:
            pass

    return result


def get_source_element():
    """
    Primary workflow:
        preselect exactly one supported element before running.

    Fallback:
        if preselection is missing/invalid,
        ask user to pick one element.
    """

    supported = []

    try:
        selected_ids = uidoc.Selection.GetElementIds()

        for eid in selected_ids:
            elem = doc.GetElement(eid)

            if is_target_element(elem):
                supported.append(elem)

    except:
        pass

    # Exactly one supported element
    if len(supported) == 1:
        return supported[0]

    # More than one valid source -> require one explicit source
    if len(supported) > 1:
        TaskDialog.Show(
            "Select Same Pipe System",
            "Bạn đang chọn nhiều hơn 1 Pipe / Pipe Fitting / "
            "Pipe Accessory.\n\n"
            "Hãy pick đúng 1 phần tử làm nguồn."
        )

    # Interactive fallback
    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            "Pick 1 Pipe / Pipe Fitting / Pipe Accessory"
        )

        elem = doc.GetElement(reference.ElementId)

        if not is_target_element(elem):
            TaskDialog.Show(
                "Select Same Pipe System",
                "Phần tử được pick không phải:\n\n"
                "- Pipe\n"
                "- Pipe Fitting\n"
                "- Pipe Accessory"
            )
            return None

        return elem

    except OperationCanceledException:
        return None

    except:
        return None


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # 1. Active View Level
    # --------------------------------------------------------

    level = get_active_view_level(view)

    if level is None:
        TaskDialog.Show(
            "Select Same Pipe System",
            "Active View không có Level hợp lệ.\n\n"
            "Hãy chạy tool trong Floor Plan / "
            "Engineering Plan có Level."
        )
        return

    level_z = get_level_project_z(level)

    if level_z is None:
        TaskDialog.Show(
            "Select Same Pipe System",
            "Không đọc được cao độ Level của Active View."
        )
        return

    # --------------------------------------------------------
    # 2. Source
    # --------------------------------------------------------

    source = get_source_element()

    if source is None:
        return

    # --------------------------------------------------------
    # 3. Source System Name
    # --------------------------------------------------------

    source_system_names = get_system_names(source)

    if not source_system_names:
        TaskDialog.Show(
            "Select Same Pipe System",
            "Không đọc được System Name của phần tử đã chọn.\n\n"
            "ElementId: {}".format(
                source.Id.IntegerValue
            )
        )
        return

    # --------------------------------------------------------
    # 4. Collect target elements
    # --------------------------------------------------------

    candidates = collect_target_elements()

    result_ids = []
    result_seen = set()

    # --------------------------------------------------------
    # 5. Filter by:
    #    same system
    #    Z <= active Level 0.00
    # --------------------------------------------------------

    for elem in candidates:

        # System check
        elem_system_names = get_system_names(elem)

        if not elem_system_names:
            continue

        # Exact System Name intersection
        if source_system_names.isdisjoint(elem_system_names):
            continue

        # Elevation
        z = get_reference_z(elem)

        if z is None:
            continue

        if z > level_z + Z_TOLERANCE_FT:
            continue

        # Passed
        eid_int = elem.Id.IntegerValue

        if eid_int not in result_seen:
            result_seen.add(eid_int)
            result_ids.append(elem.Id)

    # --------------------------------------------------------
    # 6. Preserve picked source if desired
    # --------------------------------------------------------

    if KEEP_SOURCE_ALWAYS:

        source_id_int = source.Id.IntegerValue

        if source_id_int not in result_seen:
            result_seen.add(source_id_int)
            result_ids.append(source.Id)

    # --------------------------------------------------------
    # 7. Set Revit Selection
    # --------------------------------------------------------

    if not result_ids:
        TaskDialog.Show(
            "Select Same Pipe System",
            "Không tìm thấy phần tử phù hợp."
        )
        return

    ids_to_select = List[DB.ElementId]()

    for eid in result_ids:
        ids_to_select.Add(eid)

    uidoc.Selection.SetElementIds(ids_to_select)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()