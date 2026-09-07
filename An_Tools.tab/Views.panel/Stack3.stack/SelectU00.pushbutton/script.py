# -*- coding: utf-8 -*-
"""
SELECT SAME PIPING SYSTEM BELOW ACTIVE VIEW LEVEL 0.00
FAST START / FAST CANCEL / NATIVE SYSTEM FILTER

Workflow
--------
1. Preselect exactly 1 Pipe / Pipe Fitting / Pipe Accessory.
   If preselection is not exactly one supported element, tool immediately asks for one Pick.
2. ESC during Pick -> exit immediately and silently.
3. Only after a source exists:
      - read Active View Level
      - read source System Name
      - use a native Revit parameter filter for System Name
      - evaluate elevation only for elements already in that System Name
4. Select matching elements whose highest centerline/connector Z
   is <= 0.00 elevation of Active View Level.

Performance changes vs old version
----------------------------------
- Pick/Cancel happens BEFORE Level lookup / collectors / connector scanning.
- RBS_SYSTEM_NAME_PARAM is the primary source of System Name.
- FilteredElementCollector is filtered natively by System Name.
- Connector.MEPSystem is only a fallback for the ONE source element,
  or rare elements missing the built-in System Name parameter.
- One multi-category collector is attempted first; 3 category collectors are fallback.
- Success and Cancel are silent.

pyRevit / Revit 2025-2026
"""

# Reuse the Python engine between runs when pyRevit supports it.
# The script is stateless between executions, so this is safe here and
# reduces repeated interpreter/import overhead after the first run.
__persistentengine__ = True

from pyrevit import revit
from Autodesk.Revit import DB
from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List


# ============================================================
# SETTINGS
# ============================================================

Z_TOLERANCE_MM = 1.0
Z_TOLERANCE_FT = Z_TOLERANCE_MM / 304.8

# True: always keep the source in final selection.
# False: source is kept only if it also passes the elevation rule.
KEEP_SOURCE_ALWAYS = True

TARGET_BICS = (
    DB.BuiltInCategory.OST_PipeCurves,
    DB.BuiltInCategory.OST_PipeFitting,
    DB.BuiltInCategory.OST_PipeAccessory,
)

SYSTEM_NAME_BIP = DB.BuiltInParameter.RBS_SYSTEM_NAME_PARAM
PIPE_BIC = DB.BuiltInCategory.OST_PipeCurves


# ============================================================
# SMALL COMPATIBILITY HELPERS
# ============================================================

def eid_int(eid):
    if eid is None:
        return None

    try:
        return int(eid.Value)
    except:
        pass

    try:
        return int(eid.IntegerValue)
    except:
        return None


def bic_id_value(bic):
    try:
        return eid_int(DB.ElementId(bic))
    except:
        return None


TARGET_CAT_ID_VALUES = set()
for _bic in TARGET_BICS:
    _value = bic_id_value(_bic)
    if _value is not None:
        TARGET_CAT_ID_VALUES.add(_value)

PIPE_CAT_ID_VALUE = bic_id_value(PIPE_BIC)


# ============================================================
# CATEGORY
# ============================================================

def is_target_element(elem):
    if elem is None:
        return False

    try:
        cat = elem.Category
        if cat is None:
            return False
        return eid_int(cat.Id) in TARGET_CAT_ID_VALUES
    except:
        return False


def is_pipe(elem):
    if elem is None:
        return False

    try:
        cat = elem.Category
        return cat is not None and eid_int(cat.Id) == PIPE_CAT_ID_VALUE
    except:
        return False


# ============================================================
# CONNECTORS
# ============================================================

def get_connector_manager(elem):
    if elem is None:
        return None

    # Pipe / MEPCurve
    try:
        cm = elem.ConnectorManager
        if cm is not None:
            return cm
    except:
        pass

    # FamilyInstance: fitting / accessory
    try:
        mep_model = elem.MEPModel
        if mep_model is not None:
            cm = mep_model.ConnectorManager
            if cm is not None:
                return cm
    except:
        pass

    return None


# ============================================================
# SYSTEM NAME -- FAST FIRST
# ============================================================

def get_system_name_parameter(elem):
    """Cheap built-in parameter read. Returns one exact display string or None."""
    if elem is None:
        return None

    try:
        param = elem.get_Parameter(SYSTEM_NAME_BIP)
        if param is None:
            return None

        value = param.AsString()
        if value:
            value = value.strip()
            if value:
                return value
    except:
        pass

    return None


def get_source_system_names(source):
    """
    Resolve System Name for the ONE source element.

    Fast path:
        RBS_SYSTEM_NAME_PARAM

    Rare fallbacks:
        elem.MEPSystem
        connector.MEPSystem
    """
    names = set()

    # Fastest and most common path.
    value = get_system_name_parameter(source)
    if value:
        names.add(value)
        return names

    # Direct MEPSystem fallback.
    try:
        system = source.MEPSystem
        if system is not None:
            name = system.Name
            if name:
                names.add(name.strip())
    except:
        pass

    if names:
        return names

    # Connector fallback only for the source.
    cm = get_connector_manager(source)
    if cm is not None:
        try:
            for connector in cm.Connectors:
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

    return names


# ============================================================
# NATIVE REVIT SYSTEM NAME FILTER
# ============================================================

def make_string_equals_rule(param_id, text):
    """Revit 2025/2026 compatible string equals rule."""
    provider = DB.ParameterValueProvider(param_id)
    evaluator = DB.FilterStringEquals()

    # Current API overload.
    try:
        return DB.FilterStringRule(provider, evaluator, text)
    except:
        pass

    # Compatibility fallback for APIs exposing caseSensitive overload.
    try:
        return DB.FilterStringRule(provider, evaluator, text, False)
    except:
        return None


def build_system_name_element_filter(system_names):
    """Build native ElementParameterFilter / LogicalOrFilter."""
    if not system_names:
        return None

    try:
        param_id = DB.ElementId(SYSTEM_NAME_BIP)
    except:
        return None

    filters = List[DB.ElementFilter]()

    for name in sorted(system_names):
        if not name:
            continue

        rule = make_string_equals_rule(param_id, name)
        if rule is None:
            continue

        try:
            filters.Add(DB.ElementParameterFilter(rule))
        except:
            pass

    if filters.Count == 0:
        return None

    if filters.Count == 1:
        return filters[0]

    try:
        return DB.LogicalOrFilter(filters)
    except:
        return None


def collect_same_system_elements_native(doc, system_names):
    """
    Fast path: let Revit's collector filter System Name natively.
    Returns None only if native filter construction/application fails.
    """
    system_filter = build_system_name_element_filter(system_names)
    if system_filter is None:
        return None

    # --------------------------------------------------------
    # Preferred: one multi-category collector
    # --------------------------------------------------------
    try:
        bic_list = List[DB.BuiltInCategory]()
        for bic in TARGET_BICS:
            bic_list.Add(bic)

        category_filter = DB.ElementMulticategoryFilter(bic_list)

        collector = (
            DB.FilteredElementCollector(doc)
            .WhereElementIsNotElementType()
            .WherePasses(category_filter)
            .WherePasses(system_filter)
        )

        return list(collector)

    except:
        pass

    # --------------------------------------------------------
    # Fallback: 3 category collectors, still native system filter
    # --------------------------------------------------------
    result = []
    seen = set()
    any_collector_succeeded = False

    for bic in TARGET_BICS:
        try:
            collector = (
                DB.FilteredElementCollector(doc)
                .OfCategory(bic)
                .WhereElementIsNotElementType()
                .WherePasses(system_filter)
            )

            any_collector_succeeded = True

            for elem in collector:
                value = eid_int(elem.Id)
                if value is None or value in seen:
                    continue
                seen.add(value)
                result.append(elem)
        except:
            pass

    if any_collector_succeeded:
        return result

    return None


def collect_same_system_elements_fallback(doc, system_names):
    """
    Compatibility fallback only if native parameter filtering fails.

    Still optimized vs old code:
    - parameter first for every element
    - connector system lookup only if the parameter is missing
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
                eid_value = eid_int(elem.Id)
                if eid_value is None or eid_value in seen:
                    continue

                param_name = get_system_name_parameter(elem)
                matched = param_name in system_names if param_name else False

                # Rare fallback only when the element has no System Name param value.
                if not matched and not param_name:
                    cm = get_connector_manager(elem)
                    if cm is not None:
                        try:
                            for connector in cm.Connectors:
                                try:
                                    system = connector.MEPSystem
                                    if system is None:
                                        continue
                                    name = system.Name
                                    if name and name.strip() in system_names:
                                        matched = True
                                        break
                                except:
                                    pass
                        except:
                            pass

                if not matched:
                    continue

                seen.add(eid_value)
                result.append(elem)

        except:
            pass

    return result


def collect_same_system_elements(doc, system_names):
    result = collect_same_system_elements_native(doc, system_names)
    if result is not None:
        return result

    return collect_same_system_elements_fallback(doc, system_names)


# ============================================================
# ELEVATION
# ============================================================

def get_pipe_top_centerline_z(elem):
    try:
        location = elem.Location
        if not isinstance(location, DB.LocationCurve):
            return None

        curve = location.Curve
        if curve is None:
            return None

        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
        return max(p0.Z, p1.Z)
    except:
        return None


def get_connector_top_z(elem):
    cm = get_connector_manager(elem)
    if cm is None:
        return None

    max_z = None

    try:
        for connector in cm.Connectors:
            try:
                origin = connector.Origin
                if origin is None:
                    continue

                z = origin.Z
                if max_z is None or z > max_z:
                    max_z = z
            except:
                pass
    except:
        pass

    return max_z


def get_location_point_z(elem):
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
    try:
        bbox = elem.get_BoundingBox(None)
        if bbox is not None:
            return (bbox.Min.Z + bbox.Max.Z) * 0.5
    except:
        pass

    return None


def get_reference_z(elem):
    # Pipe: highest centerline endpoint.
    if is_pipe(elem):
        value = get_pipe_top_centerline_z(elem)
        if value is not None:
            return value

    # Fitting / accessory: highest connector origin.
    value = get_connector_top_z(elem)
    if value is not None:
        return value

    # Fallbacks.
    value = get_location_point_z(elem)
    if value is not None:
        return value

    return get_bbox_center_z(elem)


# ============================================================
# ACTIVE VIEW LEVEL
# ============================================================

def get_active_view_level(view):
    try:
        level = view.GenLevel
        if level is not None:
            return level
    except:
        pass

    return None


def get_level_project_z(level):
    try:
        return level.ProjectElevation
    except:
        pass

    try:
        return level.Elevation
    except:
        return None


# ============================================================
# SOURCE -- CANCEL FIRST
# ============================================================

def get_source_element(uidoc, doc):
    """
    Fast-cancel workflow:
    - If exactly one supported preselected element exists -> use it.
    - Otherwise immediately enter PickObject.
    - ESC -> return None silently.

    No Level lookup, collector, System scan, or connector traversal happens
    before this function returns a real source element.
    """
    supported = []

    try:
        for eid in uidoc.Selection.GetElementIds():
            elem = doc.GetElement(eid)
            if is_target_element(elem):
                supported.append(elem)
                if len(supported) > 1:
                    break
    except:
        supported = []

    if len(supported) == 1:
        return supported[0]

    # No warning dialog here. Going straight into Pick makes both Run and
    # ESC feel closer to native Revit commands.
    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            "Pick 1 Pipe / Pipe Fitting / Pipe Accessory | ESC to cancel"
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
# SET SELECTION
# ============================================================

def set_selection(uidoc, element_ids):
    ids_to_select = List[DB.ElementId]()

    for eid in element_ids:
        try:
            ids_to_select.Add(eid)
        except:
            pass

    uidoc.Selection.SetElementIds(ids_to_select)


# ============================================================
# MAIN
# ============================================================

def main():
    uidoc = revit.uidoc
    doc = revit.doc

    if uidoc is None or doc is None:
        return

    # ========================================================
    # 1. SOURCE FIRST
    #    ESC happens before any expensive operation.
    # ========================================================
    source = get_source_element(uidoc, doc)
    if source is None:
        return

    # ========================================================
    # 2. ACTIVE VIEW LEVEL
    # ========================================================
    view = doc.ActiveView
    level = get_active_view_level(view)

    if level is None:
        TaskDialog.Show(
            "Select Same Pipe System",
            "Active View không có Level hợp lệ.\n\n"
            "Hãy chạy tool trong Floor Plan / Engineering Plan có Level."
        )
        return

    level_z = get_level_project_z(level)

    if level_z is None:
        TaskDialog.Show(
            "Select Same Pipe System",
            "Không đọc được cao độ Level của Active View."
        )
        return

    # ========================================================
    # 3. SOURCE SYSTEM NAME
    # ========================================================
    source_system_names = get_source_system_names(source)

    if not source_system_names:
        source_id = eid_int(source.Id)
        TaskDialog.Show(
            "Select Same Pipe System",
            "Không đọc được System Name của phần tử đã chọn.\n\n"
            "ElementId: {}".format(source_id if source_id is not None else "?")
        )
        return

    # ========================================================
    # 4. NATIVE SYSTEM FILTER
    #    Revit filters the project before Python receives elems.
    # ========================================================
    candidates = collect_same_system_elements(doc, source_system_names)

    result_ids = []
    result_seen = set()

    # ========================================================
    # 5. ELEVATION FILTER
    #    Connector Z is now evaluated only inside the target system.
    # ========================================================
    for elem in candidates:
        z = get_reference_z(elem)

        if z is None:
            continue

        if z > level_z + Z_TOLERANCE_FT:
            continue

        value = eid_int(elem.Id)
        if value is None or value in result_seen:
            continue

        result_seen.add(value)
        result_ids.append(elem.Id)

    # ========================================================
    # 6. KEEP SOURCE IF REQUESTED
    # ========================================================
    if KEEP_SOURCE_ALWAYS:
        source_value = eid_int(source.Id)

        if source_value is not None and source_value not in result_seen:
            result_seen.add(source_value)
            result_ids.append(source.Id)

    # ========================================================
    # 7. RESULT
    # ========================================================
    if not result_ids:
        TaskDialog.Show(
            "Select Same Pipe System",
            "Không tìm thấy phần tử phù hợp."
        )
        return

    set_selection(uidoc, result_ids)
    # Success = silent.


if __name__ == "__main__":
    main()
