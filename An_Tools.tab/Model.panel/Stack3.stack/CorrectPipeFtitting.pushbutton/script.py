# -*- coding: utf-8 -*-
__title__ = "Correct Pipes & Fittings"
__doc__ = "V5.8.3 Universal / Revit 2025-2026: Transition uses native Revit ChangeTypeId without rotate/recreate; Option 2 uses only fitting types already loaded in the project."

import os
import traceback
import math
import itertools
import System
from pyrevit import revit, DB, forms, script

try:
    from System.Collections.Generic import List
except Exception:
    List = None

uidoc = revit.uidoc
doc = revit.doc

SUPPORTED_REVIT_VERSIONS = (2025, 2026)
REVIT_VERSION = 0


# ============================================================
# Helper functions
# ============================================================

def safe_text(value):
    try:
        if value is None:
            return u""
        return unicode(value)
    except Exception:
        try:
            return str(value)
        except Exception:
            return u""


def get_revit_major_version():
    """Return current Revit major version as int without hard-failing."""
    try:
        return int(safe_text(revit.app.VersionNumber))
    except Exception:
        try:
            return int(safe_text(doc.Application.VersionNumber))
        except Exception:
            return 0


REVIT_VERSION = get_revit_major_version()


# ============================================================
# Revit 2025/2026 universal compatibility - ElementId is 64-bit
# ============================================================

def element_id_value(eid):
    """Return a Python int from ElementId on Revit 2024-2026+.

    Revit 2025 and 2026 use the 64-bit ElementId.Value API. IntegerValue is
    retained only as a compatibility fallback for older Revit versions.
    """
    if eid is None:
        return None
    try:
        return int(eid.Value)
    except Exception:
        try:
            return int(eid.IntegerValue)
        except Exception:
            try:
                return int(safe_text(eid))
            except Exception:
                return None


def make_element_id(value):
    """Create ElementId using Int64 on Revit 2025/2026, with legacy fallback."""
    if isinstance(value, DB.ElementId):
        return value
    try:
        return DB.ElementId(System.Int64(int(value)))
    except Exception:
        try:
            return DB.ElementId(int(value))
        except Exception:
            return DB.ElementId.InvalidElementId


def same_element_id(a, b):
    av = element_id_value(a)
    bv = element_id_value(b)
    return av is not None and bv is not None and av == bv


def is_valid_element_id(eid):
    value = element_id_value(eid)
    invalid = element_id_value(DB.ElementId.InvalidElementId)
    return value is not None and value != invalid


def enum_int64(value):
    try:
        return int(System.Convert.ToInt64(value))
    except Exception:
        try:
            return int(value)
        except Exception:
            return None


def category_is(elem, built_in_category):
    try:
        if elem is None or elem.Category is None:
            return False
        return element_id_value(elem.Category.Id) == enum_int64(built_in_category)
    except Exception:
        return False


def get_real_name(elem):
    """Return a stable readable name for a Revit element/type."""
    if not elem:
        return u"Không xác định"

    for bip in [
        DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,
        DB.BuiltInParameter.SYMBOL_NAME_PARAM,
        DB.BuiltInParameter.RBS_SYSTEM_NAME_PARAM,
    ]:
        try:
            p = elem.get_Parameter(bip)
            if p and p.AsString():
                return p.AsString()
        except Exception:
            pass

    try:
        n = revit.query.get_name(elem)
        if n:
            return n
    except Exception:
        pass

    try:
        if hasattr(elem, "Name") and elem.Name:
            return elem.Name
    except Exception:
        pass

    try:
        return u"Đối tượng ID {}".format(element_id_value(elem.Id))
    except Exception:
        return u"Không xác định"


def get_real_family_name(elem):
    if not elem:
        return u"Family Không Xác Định"
    try:
        p = elem.get_Parameter(DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME)
        if p and p.AsString():
            return p.AsString()
    except Exception:
        pass
    try:
        if hasattr(elem, "FamilyName") and elem.FamilyName:
            return elem.FamilyName
    except Exception:
        pass
    try:
        if hasattr(elem, "Family") and elem.Family:
            return elem.Family.Name
    except Exception:
        pass
    return u"Family Không Xác Định"


def _is_piping_system_type_id(eid):
    """True only when eid resolves to Autodesk.Revit.DB.Plumbing.PipingSystemType."""
    if not is_valid_element_id(eid):
        return False
    try:
        return isinstance(doc.GetElement(eid), DB.Plumbing.PipingSystemType)
    except Exception:
        return False


def _system_type_id_from_mep_system(mep_system):
    try:
        if mep_system is None:
            return DB.ElementId.InvalidElementId
        tid = mep_system.GetTypeId()
        if _is_piping_system_type_id(tid):
            return tid
    except Exception:
        pass
    return DB.ElementId.InvalidElementId


def _system_type_id_from_owner_parameter(owner):
    if owner is None:
        return DB.ElementId.InvalidElementId
    try:
        p = owner.get_Parameter(DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM)
        if p and p.StorageType == DB.StorageType.ElementId:
            eid = p.AsElementId()
            if _is_piping_system_type_id(eid):
                return eid
    except Exception:
        pass
    return DB.ElementId.InvalidElementId


def get_system_type_id(elem):
    """Return the PipingSystemType ElementId for a Pipe or Pipe Fitting.

    Revit 2025/2026-safe order:
      1. Built-in System Type parameter (RBS_PIPING_SYSTEM_TYPE_PARAM).
      2. Element.MEPSystem.GetTypeId().
      3. Connector.MEPSystem.GetTypeId().
      4. System Type of physically connected owners (normally the pipes).

    RBS_SYSTEM_CLASSIFICATION_PARAM is deliberately NOT treated as a system
    type id; classification and PipingSystemType are different concepts.
    """
    if elem is None:
        return DB.ElementId.InvalidElementId

    # Primary path: Autodesk's built-in piping System Type parameter.
    eid = _system_type_id_from_owner_parameter(elem)
    if is_valid_element_id(eid):
        return eid

    # Pipe/MEP element may expose a direct MEPSystem.
    try:
        eid = _system_type_id_from_mep_system(elem.MEPSystem)
        if is_valid_element_id(eid):
            return eid
    except Exception:
        pass

    # Fittings frequently resolve their system through their connectors.
    try:
        for conn in get_connectors(elem):
            try:
                eid = _system_type_id_from_mep_system(conn.MEPSystem)
                if is_valid_element_id(eid):
                    return eid
            except Exception:
                pass

            # Last fallback: inspect owners physically referenced by this connector.
            try:
                refs = conn.AllRefs
            except Exception:
                refs = []
            for ref_conn in refs:
                try:
                    owner = ref_conn.Owner
                except Exception:
                    owner = None
                if owner is None or owner == elem:
                    continue
                eid = _system_type_id_from_owner_parameter(owner)
                if is_valid_element_id(eid):
                    return eid
                try:
                    eid = _system_type_id_from_mep_system(owner.MEPSystem)
                    if is_valid_element_id(eid):
                        return eid
                except Exception:
                    pass
    except Exception:
        pass

    return DB.ElementId.InvalidElementId


def get_system_type_name_by_id(system_type_id):
    try:
        st = doc.GetElement(system_type_id)
        if st:
            return get_real_name(st)
    except Exception:
        pass
    return u"Không xác định System Type"


def get_part_type(family_symbol):
    """Return DB.PartType integer for a loaded MEP FamilySymbol.

    V5.8 is deliberately defensive here.  Most Pipe Fitting content exposes
    FAMILY_CONTENT_PART_TYPE on the Family object, but project/custom content
    can expose the built-in parameter on the symbol instead.  Reading both
    prevents valid loaded fitting types from disappearing from the manual list.
    """
    if not family_symbol:
        return None

    owners = []
    try:
        owners.append(family_symbol)
    except Exception:
        pass
    try:
        fam = family_symbol.Family
        if fam is not None:
            owners.append(fam)
    except Exception:
        pass

    for owner in owners:
        try:
            p = owner.get_Parameter(DB.BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
            if p:
                try:
                    return int(p.AsInteger())
                except Exception:
                    pass
        except Exception:
            pass

    # Last-resort localized-name lookup.  This is not used when the built-in
    # parameter is available, but keeps unusual custom content visible.
    for owner in owners:
        try:
            p = owner.LookupParameter(u"Part Type")
            if p:
                try:
                    return int(p.AsInteger())
                except Exception:
                    pass
        except Exception:
            pass
    return None


def get_symbol_from_instance(elem):
    try:
        return doc.GetElement(elem.GetTypeId())
    except Exception:
        return None


def is_pipe(elem):
    try:
        if isinstance(elem, DB.Plumbing.Pipe):
            return True
    except Exception:
        pass
    return category_is(elem, DB.BuiltInCategory.OST_PipeCurves)


def is_pipe_fitting(elem):
    return category_is(elem, DB.BuiltInCategory.OST_PipeFitting)


def _category_matches_bic(category, built_in_category):
    try:
        return (category is not None and
                element_id_value(category.Id) == enum_int64(built_in_category))
    except Exception:
        return False


def _family_is_pipe_fitting_family(family):
    """True when a Family is authored in the Pipe Fittings category."""
    try:
        return _category_matches_bic(family.FamilyCategory,
                                     DB.BuiltInCategory.OST_PipeFitting)
    except Exception:
        return False


def collect_all_pipe_fitting_symbols(instance_fittings=None, routing_symbol_ids=None):
    """V5.8 robust project-wide scan for every loaded Pipe Fitting FamilySymbol.

    A single OfCategory(OST_PipeFitting) collector is normally sufficient, but
    some custom/project content can be missed when the symbol Category is not
    exposed consistently.  We therefore merge five independent sources and
    de-duplicate by 64-bit ElementId:
      1) direct OST_PipeFitting element-type collector;
      2) all FamilySymbol elements whose symbol Category is Pipe Fittings;
      3) all FamilySymbol elements whose parent FamilyCategory is Pipe Fittings;
      4) type IDs used by existing Pipe Fitting instances;
      5) FamilySymbols referenced by PipeType Routing Preferences.

    Returns (symbols, stats).
    """
    found = {}
    sources_by_id = {}
    stats = {
        'direct_category': 0,
        'symbol_category': 0,
        'family_category': 0,
        'instance_type': 0,
        'routing_reference': 0,
        'family_enumeration': 0,
        'all_element_types': 0,
    }

    def _add(symbol, source, force=False):
        try:
            if not isinstance(symbol, DB.FamilySymbol):
                return False
            valid_category = (category_is(symbol, DB.BuiltInCategory.OST_PipeFitting) or
                              _family_is_pipe_fitting_family(symbol.Family))
            if not valid_category and not force:
                return False
            sid = element_id_value(symbol.Id)
            if sid is None:
                return False
            if sid not in found:
                found[sid] = symbol
            sources_by_id.setdefault(sid, set()).add(source)
            stats[source] = stats.get(source, 0) + 1
            return True
        except Exception:
            return False

    # 1. Fast/direct category scan.
    try:
        direct = list(DB.FilteredElementCollector(doc)
                      .OfCategory(DB.BuiltInCategory.OST_PipeFitting)
                      .WhereElementIsElementType()
                      .ToElements())
        for symbol in direct:
            _add(symbol, 'direct_category', force=True)
    except Exception:
        pass

    # 2 + 3. Full FamilySymbol scan catches symbols whose own Category is null
    # but whose parent FamilyCategory is correctly Pipe Fittings.
    try:
        all_symbols = list(DB.FilteredElementCollector(doc)
                           .OfClass(DB.FamilySymbol)
                           .WhereElementIsElementType()
                           .ToElements())
        for symbol in all_symbols:
            try:
                if category_is(symbol, DB.BuiltInCategory.OST_PipeFitting):
                    _add(symbol, 'symbol_category', force=True)
                elif _family_is_pipe_fitting_family(symbol.Family):
                    _add(symbol, 'family_category', force=True)
            except Exception:
                pass
    except Exception:
        pass

    # 3b. Enumerate all symbols from every Pipe Fitting Family explicitly.
    try:
        families = list(DB.FilteredElementCollector(doc)
                        .OfClass(DB.Family)
                        .ToElements())
        for family in families:
            if not _family_is_pipe_fitting_family(family):
                continue
            try:
                for sid in family.GetFamilySymbolIds():
                    symbol = doc.GetElement(sid)
                    _add(symbol, 'family_enumeration', force=True)
            except Exception:
                pass
    except Exception:
        pass

    # 3c. Last-resort scan across every ElementType. This is intentionally
    # broader than OfClass(FamilySymbol) and is only accepted when the element
    # resolves to a FamilySymbol whose own Category OR parent FamilyCategory is
    # Pipe Fittings. It helps with unusual/custom content metadata.
    try:
        all_element_types = list(DB.FilteredElementCollector(doc)
                                 .WhereElementIsElementType()
                                 .ToElements())
        for et in all_element_types:
            try:
                if isinstance(et, DB.FamilySymbol):
                    if (category_is(et, DB.BuiltInCategory.OST_PipeFitting) or
                            _family_is_pipe_fitting_family(et.Family)):
                        _add(et, 'all_element_types', force=True)
            except Exception:
                pass
    except Exception:
        pass

    # 4. Any type actually used by a Pipe Fitting instance must be visible in
    # manual mode, even if one of the collectors above behaves unexpectedly.
    for fitting in list(instance_fittings or []):
        try:
            symbol = doc.GetElement(fitting.GetTypeId())
            _add(symbol, 'instance_type', force=True)
        except Exception:
            pass

    # 5. Routing Preferences are another authoritative reference to loaded MEP
    # fitting content.  Only keep FamilySymbols that really belong to the Pipe
    # Fittings category/family so Segments cannot leak into the manual list.
    for raw_id in list(routing_symbol_ids or []):
        try:
            symbol = doc.GetElement(make_element_id(raw_id))
            _add(symbol, 'routing_reference', force=False)
        except Exception:
            pass

    symbols = list(found.values())
    stats['unique_total'] = len(symbols)
    stats['sources_by_id'] = sources_by_id
    return symbols, stats


def get_pipe_diameter_ft(pipe):
    """Return the actual instance pipe diameter in Revit internal units (feet)."""
    if pipe is None or not is_pipe(pipe):
        return None
    try:
        p = pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
        if p and p.StorageType == DB.StorageType.Double:
            value = float(p.AsDouble())
            if value > 0.0:
                return value
    except Exception:
        pass
    try:
        value = float(pipe.Diameter)
        if value > 0.0:
            return value
    except Exception:
        pass
    return None


def pipe_size_mm(size_ft):
    try:
        return float(size_ft) * 304.8
    except Exception:
        return None


def format_pipe_size(size_ft):
    """Readable DN/diameter label. Values come from actual pipe instances."""
    mm = pipe_size_mm(size_ft)
    if mm is None:
        return u"?"
    nearest = round(mm)
    if abs(mm - nearest) <= 1.0e-6:
        return u"DN{:.0f} mm".format(nearest)
    text = u"{:.3f}".format(mm).rstrip(u"0").rstrip(u".")
    return u"DN{} mm".format(text)


def collect_model_pipe_sizes(pipes=None):
    """Return sorted unique diameters that actually occur on Pipe instances in model."""
    if pipes is None:
        try:
            pipes = list(DB.FilteredElementCollector(doc)\
                         .OfClass(DB.Plumbing.Pipe)\
                         .WhereElementIsNotElementType()\
                         .ToElements())
        except Exception:
            pipes = []
    values = []
    # Key in millimetres avoids duplicate float noise while preserving model sizes.
    seen = set()
    for pipe in pipes:
        value = get_pipe_diameter_ft(pipe)
        if value is None or value <= 0.0:
            continue
        try:
            key = round(float(value) * 304.8, 6)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        values.append(float(value))
    values.sort()
    return values


def pipe_size_in_range(pipe, min_size_ft, max_size_ft, tol=1.0e-7):
    value = get_pipe_diameter_ft(pipe)
    if value is None:
        return False
    return value >= (float(min_size_ft) - tol) and value <= (float(max_size_ft) + tol)


def fitting_port_diameters_ft(fitting):
    """Return diameters represented by all physical round ports of a fitting.

    This is intentionally based on every port, not only one 'primary' port. A
    reducing Tee crossing the selected size boundary is therefore skipped as a
    whole, preventing partial changes to a mixed-size junction.
    """
    result = []
    try:
        connectors = physical_connectors(fitting)
    except Exception:
        connectors = get_connectors(fitting)
    for conn in connectors:
        sig = connector_size_signature(conn)
        try:
            if sig and safe_text(sig[0]).lower() == u"round" and len(sig) >= 2:
                # connector_size_signature stores Radius for round connectors.
                diameter = float(sig[1]) * 2.0
                if diameter > 0.0:
                    result.append(diameter)
        except Exception:
            pass
    return result


def fitting_size_in_range(fitting, min_size_ft, max_size_ft, tol=1.0e-7):
    """A fitting is eligible only when ALL physical pipe-port sizes are in range."""
    sizes = fitting_port_diameters_ft(fitting)
    if not sizes:
        return False, []
    ok = all((value >= float(min_size_ft) - tol and
              value <= float(max_size_ft) + tol) for value in sizes)
    return ok, sizes


# Tolerance used by the forced reconnect fallback.
# Revit internal length unit is feet. 1/16 inch = 1 / (12 * 16) ft.
RECONNECT_TOLERANCE_FT = 1.0 / (12.0 * 16.0)
SIZE_TOLERANCE_FT = 1.0e-5
TRANSFORM_TOLERANCE = 1.0e-7
# Two physical MEP connectors should normally face each other.
# dot(BasisZ_fit, BasisZ_partner) ~= -1.0 is ideal.
CONNECTOR_OPPOSITE_DOT_LIMIT = -0.80
# V3.2: temporary pipe stubs are used to preserve OPEN ports on Tee/Wye/Cross
# while recreating a fitting through Revit Routing Preferences.
TEMP_STUB_LENGTH_FT = 1.0
# V4.8: connector origins of two different fitting families may have different
# center-to-end distances even when their angles and sizes are equivalent.
# Instead of requiring every origin to coincide, align connector AXES and the
# virtual junction center, then trim/extend only the disconnected pipe end.
AXIS_ALIGNMENT_DOT_MIN = 0.98
CENTERLINE_TOLERANCE_FT = 1.0 / (12.0 * 16.0)  # 1/16 inch
MIN_CURVE_LENGTH_FT = 1.0 / 120.0              # 0.1 inch


def get_connector_manager(elem):
    """Return ConnectorManager for FamilyInstance/MEPCurve when available."""
    if elem is None:
        return None
    try:
        cm = elem.ConnectorManager
        if cm:
            return cm
    except Exception:
        pass
    try:
        mep_model = elem.MEPModel
        if mep_model and mep_model.ConnectorManager:
            return mep_model.ConnectorManager
    except Exception:
        pass
    return None


def get_connectors(elem):
    cm = get_connector_manager(elem)
    if not cm:
        return []
    result = []
    try:
        for c in cm.Connectors:
            result.append(c)
    except Exception:
        pass
    return result


def copy_xyz(pt):
    try:
        return DB.XYZ(pt.X, pt.Y, pt.Z)
    except Exception:
        return None


def xyz_distance(a, b):
    if a is None or b is None:
        return 1.0e30
    try:
        return a.DistanceTo(b)
    except Exception:
        try:
            dx = a.X - b.X
            dy = a.Y - b.Y
            dz = a.Z - b.Z
            return (dx * dx + dy * dy + dz * dz) ** 0.5
        except Exception:
            return 1.0e30


def xyz_almost_equal(a, b, tol=TRANSFORM_TOLERANCE):
    return xyz_distance(a, b) <= tol


def connector_domain_name(conn):
    try:
        return safe_text(conn.Domain)
    except Exception:
        return u""


def connector_shape_name(conn):
    try:
        return safe_text(conn.Shape)
    except Exception:
        return u""


def connector_type_name(conn):
    try:
        return safe_text(conn.ConnectorType)
    except Exception:
        return u""


def connector_size_signature(conn):
    """Return connector dimensions in internal units, when available."""
    shape = connector_shape_name(conn).lower()
    try:
        if u"round" in shape:
            return (u"round", float(conn.Radius))
    except Exception:
        pass
    try:
        if u"rect" in shape or u"oval" in shape:
            return (shape, float(conn.Width), float(conn.Height))
    except Exception:
        pass
    return None


def size_signatures_match(sa, sb, tol=SIZE_TOLERANCE_FT):
    """Compare connector size signatures with tolerance (never raw float equality)."""
    if sa is None or sb is None:
        return True
    if sa[0] != sb[0] or len(sa) != len(sb):
        return False
    try:
        for va, vb in zip(sa[1:], sb[1:]):
            if abs(float(va) - float(vb)) > tol:
                return False
        return True
    except Exception:
        return False


def size_signature_error(sa, sb):
    if sa is None or sb is None:
        return 0.0
    if sa[0] != sb[0] or len(sa) != len(sb):
        return 1.0e30
    try:
        return max([abs(float(a) - float(b)) for a, b in zip(sa[1:], sb[1:])] or [0.0])
    except Exception:
        return 1.0e30


def connector_sizes_match(a, b, tol=SIZE_TOLERANCE_FT):
    return size_signatures_match(connector_size_signature(a), connector_size_signature(b), tol)


def is_logical_connector(conn):
    # Logical connectors can appear in AllRefs but must not be physically
    # disconnected/reconnected as pipe/fitting geometry.
    try:
        return conn.ConnectorType == DB.ConnectorType.Logical
    except Exception:
        return u"logical" in connector_type_name(conn).lower()


def connectors_are_connected(a, b):
    """Robust physical-pair check.

    V4.8 first asks Revit directly through Connector.IsConnectedTo when the
    running Revit version exposes it.  AllRefs remains a compatibility fallback
    for older API/content states.  This avoids false negatives caused by stale
    AllRefs handles immediately after Regenerate().
    """
    if a is None or b is None:
        return False

    for first, second in ((a, b), (b, a)):
        try:
            checker = getattr(first, 'IsConnectedTo', None)
            if checker is not None and checker(second):
                return True
        except Exception:
            pass

    b_owner_id = None
    try:
        b_owner_id = element_id_value(b.Owner.Id)
    except Exception:
        pass
    try:
        for ref in a.AllRefs:
            try:
                if b_owner_id is not None and element_id_value(ref.Owner.Id) != b_owner_id:
                    continue
                if xyz_distance(ref.Origin, b.Origin) <= RECONNECT_TOLERANCE_FT:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def snapshot_transform(elem):
    try:
        tr = elem.GetTransform()
        return {
            'origin': copy_xyz(tr.Origin),
            'x': copy_xyz(tr.BasisX),
            'y': copy_xyz(tr.BasisY),
            'z': copy_xyz(tr.BasisZ),
        }
    except Exception:
        pass

    try:
        loc = elem.Location
        if isinstance(loc, DB.LocationPoint):
            return {
                'point': copy_xyz(loc.Point),
                'rotation': float(loc.Rotation),
            }
    except Exception:
        pass
    return None


def transform_is_preserved(elem, snapshot):
    """Fail-safe check: do not accept a forced type change that rotates/moves the instance."""
    if not snapshot:
        return True
    if 'origin' in snapshot:
        try:
            tr = elem.GetTransform()
            return (xyz_almost_equal(tr.Origin, snapshot['origin']) and
                    xyz_almost_equal(tr.BasisX, snapshot['x']) and
                    xyz_almost_equal(tr.BasisY, snapshot['y']) and
                    xyz_almost_equal(tr.BasisZ, snapshot['z']))
        except Exception:
            return False
    if 'point' in snapshot:
        try:
            loc = elem.Location
            if not isinstance(loc, DB.LocationPoint):
                return False
            if not xyz_almost_equal(loc.Point, snapshot['point']):
                return False
            return abs(float(loc.Rotation) - float(snapshot['rotation'])) <= TRANSFORM_TOLERANCE
        except Exception:
            return False
    return True


def snapshot_instance_parameters(elem):
    """Keep writable instance values (including family angle/size parameters where present)."""
    values = []
    try:
        params = elem.Parameters
    except Exception:
        return values

    for p in params:
        try:
            if p is None or p.IsReadOnly or not p.HasValue:
                continue
            name = safe_text(p.Definition.Name)
            if not name:
                continue
            st = p.StorageType
            value = None
            if st == DB.StorageType.Double:
                value = p.AsDouble()
            elif st == DB.StorageType.Integer:
                value = p.AsInteger()
            elif st == DB.StorageType.String:
                value = p.AsString()
            elif st == DB.StorageType.ElementId:
                value = p.AsElementId()
            else:
                continue
            values.append((name, st, value))
        except Exception:
            pass
    return values


def _parameter_bip(param):
    """Return BuiltInParameter for a built-in parameter when available."""
    try:
        definition = param.Definition
        if isinstance(definition, DB.InternalDefinition):
            return definition.BuiltInParameter
    except Exception:
        pass
    return None


def _dangerous_identity_parameter(param, storage_type, value):
    """True for parameters that can change the instance type/family/system/host.

    V3.4 restored every writable instance parameter by display name. On some MEP
    fittings this includes an ElementId parameter that points to the *old*
    FamilySymbol. Setting it on the newly placed fitting is effectively another
    family/type swap and can post Revit 2025's non-ignorable
    'Changing the family for a MEP fitting...' failure.
    """
    # For cross-family recreation, ElementId values are intentionally not copied.
    # Size/angle geometry is normally Double/Integer; text metadata is String.
    if storage_type == DB.StorageType.ElementId:
        return True

    bip = _parameter_bip(param)
    dangerous_bip_names = [
        'ELEM_TYPE_PARAM',
        'SYMBOL_ID_PARAM',
        'ELEM_FAMILY_PARAM',
        'FAMILY_LEVEL_PARAM',
        'INSTANCE_REFERENCE_LEVEL_PARAM',
        'RBS_PIPING_SYSTEM_TYPE_PARAM',
        'RBS_SYSTEM_CLASSIFICATION_PARAM',
        'RBS_SYSTEM_NAME_PARAM',
    ]
    for attr in dangerous_bip_names:
        try:
            if hasattr(DB.BuiltInParameter, attr) and bip == getattr(DB.BuiltInParameter, attr):
                return True
        except Exception:
            pass

    try:
        name = safe_text(param.Definition.Name).strip().lower()
    except Exception:
        name = u''
    dangerous_names = set([
        u'family', u'family and type', u'type', u'type id', u'family type',
        u'system type', u'system classification', u'system name',
        u'level', u'reference level', u'schedule level', u'host',
        u'family và type', u'loại', u'kiểu', u'cấp', u'hệ thống',
    ])
    return name in dangerous_names


def restore_instance_parameters(elem, values, safe_cross_family=False, diagnostics=None):
    """Restore compatible instance values.

    When safe_cross_family=True, never copy ElementId/type-family/system identity
    parameters. This prevents an innocent-looking parameter restore from changing
    the newly created fitting back to the old MEP family/type.
    """
    restored = 0
    skipped = []
    for name, storage_type, value in values:
        try:
            candidates = elem.GetParameters(name)
        except Exception:
            candidates = []
        for p in candidates:
            try:
                if p.IsReadOnly or p.StorageType != storage_type:
                    continue
                if safe_cross_family and _dangerous_identity_parameter(p, storage_type, value):
                    skipped.append(name)
                    break
                if storage_type == DB.StorageType.String:
                    p.Set(value if value is not None else u"")
                else:
                    p.Set(value)
                restored += 1
                break
            except Exception:
                pass
    if diagnostics is not None:
        try:
            diagnostics.extend(skipped)
        except Exception:
            pass
    return restored


def snapshot_fitting_connections(fitting):
    """Snapshot every physical external connector relation of one fitting."""
    fit_id = element_id_value(fitting.Id)
    links = []
    seen = set()

    for fc in get_connectors(fitting):
        try:
            if not fc.IsConnected or is_logical_connector(fc):
                continue
        except Exception:
            continue

        try:
            refs = list(fc.AllRefs)
        except Exception:
            refs = []

        for ref in refs:
            try:
                if ref is None or is_logical_connector(ref):
                    continue
                owner = ref.Owner
                if owner is None:
                    continue
                owner_id = element_id_value(owner.Id)
                if owner_id is None or owner_id == fit_id:
                    continue
                # Skip MEPSystem/logical owners and keep only owners with physical connectors.
                if get_connector_manager(owner) is None:
                    continue
                if connector_domain_name(fc) and connector_domain_name(ref):
                    if connector_domain_name(fc) != connector_domain_name(ref):
                        continue

                fo = copy_xyz(fc.Origin)
                po = copy_xyz(ref.Origin)
                key = (owner_id,
                       round(po.X, 8) if po else None,
                       round(po.Y, 8) if po else None,
                       round(po.Z, 8) if po else None,
                       round(fo.X, 8) if fo else None,
                       round(fo.Y, 8) if fo else None,
                       round(fo.Z, 8) if fo else None)
                if key in seen:
                    continue
                seen.add(key)
                links.append({
                    'fit_connector': fc,
                    'partner_connector': ref,
                    'partner_owner_id': owner.Id,
                    'fit_origin': fo,
                    'partner_origin': po,
                    'domain': connector_domain_name(fc),
                    'shape': connector_shape_name(fc),
                    'partner_size': connector_size_signature(ref),
                })
            except Exception:
                pass
    return links



def physical_connectors(elem):
    """Return non-logical MEP connectors only in a deterministic order.

    ConnectorSet iteration order must not be used as a persistent port identity.
    V4.8 sorts by a stable connector key so repeated reads after Regenerate()
    are far less likely to permute Tee/Wye ports.
    """
    result = []
    for c in get_connectors(elem):
        try:
            if not is_logical_connector(c):
                result.append(c)
        except Exception:
            pass
    try:
        result.sort(key=lambda x: safe_text(connector_identity_key(x)))
    except Exception:
        pass
    return result



def fitting_port_size_profile(fitting):
    """Return physical connector size signatures for a fitting.

    V4.8 uses the ACTUAL instance connector sizes, not family/type names, to
    decide whether a fitting is a reducing/variable-size fitting. This catches
    reducing Tees, reducing elbows and other custom fittings whose PartType is
    identical to their equal-size counterpart.
    """
    result = []
    for c in physical_connectors(fitting):
        try:
            sig = connector_size_signature(c)
            if sig is not None:
                result.append(sig)
        except Exception:
            pass
    return result


def size_profile_is_variable(signatures):
    """True when at least two physical ports have different connector sizes."""
    sigs = list(signatures or [])
    if len(sigs) < 2:
        return False
    first = sigs[0]
    for sig in sigs[1:]:
        try:
            if not size_signatures_match(first, sig):
                return True
        except Exception:
            if safe_text(first) != safe_text(sig):
                return True
    return False


def fitting_has_variable_port_sizes(fitting):
    try:
        return size_profile_is_variable(fitting_port_size_profile(fitting))
    except Exception:
        return False


def fitting_size_profile_text(fitting):
    try:
        return safe_text(fitting_port_size_profile(fitting))
    except Exception:
        return u"[]"


def connector_identity_key(conn):
    """Stable-enough key while the current transaction state is valid.

    Connector.Id is not equally reliable across all Revit/content versions,
    therefore geometry + axis + domain/shape are kept as a fallback.
    """
    try:
        cid = conn.Id
        try:
            return (u'id', int(cid))
        except Exception:
            try:
                return (u'id', int(cid.Value))
            except Exception:
                pass
    except Exception:
        pass

    try:
        o = conn.Origin
        a = connector_axis(conn)
        return (
            u'geo',
            round(o.X, 7), round(o.Y, 7), round(o.Z, 7),
            round(a.X, 5) if a else None,
            round(a.Y, 5) if a else None,
            round(a.Z, 5) if a else None,
            connector_domain_name(conn), connector_shape_name(conn)
        )
    except Exception:
        return (u'obj', safe_text(conn))


def get_open_physical_fitting_connectors(fitting, links):
    """Return fitting ports that have no saved physical external connection.

    V3.1 only counted *connected* ports. A 3-port Wye/Tee with one open branch
    therefore looked like a 2-port elbow and NewElbowFitting() failed with the
    misleading 'angle too small or too large' error. V3.2 keeps topology count
    separate from physical connection count.
    """
    linked_keys = set()
    for link in links:
        try:
            linked_keys.add(connector_identity_key(link['fit_connector']))
        except Exception:
            pass

    result = []
    for c in physical_connectors(fitting):
        try:
            if connector_identity_key(c) not in linked_keys:
                result.append(c)
        except Exception:
            pass
    return result


def find_connector_near(owner, point):
    """Find the physical connector of owner nearest a saved point."""
    best = None
    best_dist = 1.0e30
    for c in physical_connectors(owner):
        try:
            d = xyz_distance(c.Origin, point)
            if d < best_dist:
                best_dist = d
                best = c
        except Exception:
            pass
    return best


def get_pipe_level_id(pipe):
    if pipe is None:
        return DB.ElementId.InvalidElementId
    try:
        lvl = pipe.ReferenceLevel
        if lvl:
            return lvl.Id
    except Exception:
        pass
    try:
        eid = pipe.LevelId
        if eid and eid != DB.ElementId.InvalidElementId:
            return eid
    except Exception:
        pass
    try:
        p = pipe.get_Parameter(DB.BuiltInParameter.RBS_START_LEVEL_PARAM)
        if p and p.StorageType == DB.StorageType.ElementId:
            eid = p.AsElementId()
            if eid and eid != DB.ElementId.InvalidElementId:
                return eid
    except Exception:
        pass
    return DB.ElementId.InvalidElementId


def nearest_level_id(z_value):
    best_id = DB.ElementId.InvalidElementId
    best_delta = 1.0e30
    try:
        levels = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
        for lvl in levels:
            try:
                d = abs(float(lvl.Elevation) - float(z_value))
                if d < best_delta:
                    best_delta = d
                    best_id = lvl.Id
            except Exception:
                pass
    except Exception:
        pass
    return best_id


def resolve_temp_pipe_ids(fitting, links, preferred_pipe_type_id):
    """Resolve SystemType/PipeType/Level for temporary branch stubs."""
    system_type_id = get_system_type_id(fitting)
    pipe_type_id = preferred_pipe_type_id
    level_id = DB.ElementId.InvalidElementId

    for link in links:
        try:
            owner = doc.GetElement(link['partner_owner_id'])
        except Exception:
            owner = None
        if owner is None or not is_pipe(owner):
            continue
        try:
            sid = get_system_type_id(owner)
            if (not system_type_id or system_type_id == DB.ElementId.InvalidElementId) and sid:
                system_type_id = sid
        except Exception:
            pass
        try:
            if pipe_type_id is None or pipe_type_id == DB.ElementId.InvalidElementId:
                pipe_type_id = owner.GetTypeId()
        except Exception:
            pass
        level_id = get_pipe_level_id(owner)
        if level_id and level_id != DB.ElementId.InvalidElementId:
            break

    if not level_id or level_id == DB.ElementId.InvalidElementId:
        try:
            eid = fitting.LevelId
            if eid and eid != DB.ElementId.InvalidElementId:
                level_id = eid
        except Exception:
            pass

    if not level_id or level_id == DB.ElementId.InvalidElementId:
        try:
            pcs = physical_connectors(fitting)
            z_value = pcs[0].Origin.Z if pcs else 0.0
            level_id = nearest_level_id(z_value)
        except Exception:
            pass

    if not system_type_id or system_type_id == DB.ElementId.InvalidElementId:
        raise Exception(u"Không xác định được Piping System Type để tạo ống tạm")
    if not pipe_type_id or pipe_type_id == DB.ElementId.InvalidElementId:
        raise Exception(u"Không xác định được Pipe Type để tạo ống tạm")
    if not level_id or level_id == DB.ElementId.InvalidElementId:
        raise Exception(u"Không xác định được Level để tạo ống tạm")

    return system_type_id, pipe_type_id, level_id


def create_temp_pipe_stub(open_connector, system_type_id, pipe_type_id, level_id):
    """Create a short pipe whose near endpoint occupies an OLD open fitting port.

    The stub is only a construction aid for NewTeeFitting/NewCrossFitting and is
    removed immediately after the replacement fitting has been created.
    """
    origin = copy_xyz(open_connector.Origin)
    axis = connector_axis(open_connector)
    if origin is None or axis is None:
        raise Exception(u"Connector hở không có Origin/Direction hợp lệ")

    diameter = None
    sig = connector_size_signature(open_connector)
    try:
        if sig and sig[0] == u'round':
            diameter = float(sig[1]) * 2.0
    except Exception:
        diameter = None

    length = TEMP_STUB_LENGTH_FT
    if diameter is not None:
        length = max(length, diameter * 6.0)

    # Connector BasisZ on a fitting normally points outward. A pipe created
    # from the fitting-port point toward +BasisZ has its near pipe connector
    # facing back toward the fitting, which is the desired relationship.
    end = origin + axis.Multiply(length)
    temp_pipe = DB.Plumbing.Pipe.Create(
        doc, system_type_id, pipe_type_id, level_id, origin, end)

    if temp_pipe is None:
        raise Exception(u"Revit không tạo được ống tạm cho connector hở")

    if diameter is not None:
        try:
            p = temp_pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
            if p and not p.IsReadOnly:
                p.Set(diameter)
        except Exception:
            pass

    doc.Regenerate()
    near = find_connector_near(temp_pipe, origin)
    if near is None:
        raise Exception(u"Không tìm được connector đầu ống tạm")

    return {
        'pipe_id': temp_pipe.Id,
        'near_origin': copy_xyz(near.Origin),
        'old_port_origin': origin,
        'old_port_axis': copy_xyz(axis),
    }


def resolve_temp_stub_connector(record):
    pipe = doc.GetElement(record['pipe_id'])
    if pipe is None:
        return None
    return find_connector_near(pipe, record['near_origin'])


def remove_temp_stubs(records, created_fitting_id):
    """Disconnect/delete construction stubs while leaving replacement ports open."""
    removed = 0
    for record in records:
        pipe = doc.GetElement(record['pipe_id'])
        if pipe is None:
            continue
        # Disconnect explicitly first so deleting a stub cannot cascade into
        # deleting/replacing the fitting created by routing.
        for pc in physical_connectors(pipe):
            try:
                refs = list(pc.AllRefs)
            except Exception:
                refs = []
            for ref in refs:
                try:
                    if element_id_value(ref.Owner.Id) == element_id_value(created_fitting_id):
                        pc.DisconnectFrom(ref)
                except Exception:
                    pass
        try:
            doc.Delete(pipe.Id)
            removed += 1
        except Exception:
            raise Exception(u"Không xóa được ống tạm ID {}".format(element_id_value(pipe.Id)))
    if records:
        doc.Regenerate()
    return removed


def connector_pair_angle_degrees(c1, c2):
    d = connector_dot(c1, c2)
    if d is None:
        return None
    try:
        d = max(-1.0, min(1.0, float(d)))
        return math.degrees(math.acos(d))
    except Exception:
        return None


def partner_geometry_description(connectors):
    if not connectors:
        return u"không có connector đối tác"
    parts = [u"{} connector".format(len(connectors))]
    # V3.3: always show all pairwise axis angles for 3/4-port junction diagnostics.
    if len(connectors) >= 2:
        angles = []
        for i in range(len(connectors)):
            for j in range(i + 1, len(connectors)):
                ang = connector_pair_angle_degrees(connectors[i], connectors[j])
                if ang is not None:
                    angles.append(u"{}-{}={:.2f}°".format(i + 1, j + 1, ang))
        if angles:
            parts.append(u"góc trục [" + u", ".join(angles) + u"]")
    try:
        sizes = [safe_text(connector_size_signature(c)) for c in connectors]
        if sizes:
            parts.append(u"size [" + u", ".join(sizes) + u"]")
    except Exception:
        pass
    return u", ".join(parts)


def snapshot_port_geometry(fitting):
    """Snapshot every physical fitting port before delete/recreate."""
    result = []
    for c in physical_connectors(fitting):
        try:
            result.append({
                'origin': copy_xyz(c.Origin),
                'axis': connector_axis(c),
                'size': connector_size_signature(c),
                'domain': connector_domain_name(c),
                'shape': connector_shape_name(c),
            })
        except Exception:
            pass
    return result


def xyz_centroid(points):
    pts = [p for p in points if p is not None]
    if not pts:
        return None
    return DB.XYZ(sum(p.X for p in pts) / len(pts),
                  sum(p.Y for p in pts) / len(pts),
                  sum(p.Z for p in pts) / len(pts))


def _port_pair_length(a, b):
    try:
        return xyz_distance(a, b)
    except Exception:
        return 1.0e30


def best_new_to_old_port_permutation(new_ports, old_records):
    """Match new fitting ports to old port geometry before 3D rigid alignment.

    The score is dominated by pairwise distance preservation, then connector
    size/domain/shape. This lets a custom Tee/Wye be placed directly even when
    NewTeeFitting rejects the existing branch angle.
    """
    n = len(old_records)
    if n == 0 or len(new_ports) != n or n > 6:
        return None
    old_pts = [r.get('origin') for r in old_records]
    best = None
    for perm in itertools.permutations(range(n)):
        score = 0.0
        compatible = True
        for oi in range(n):
            np = new_ports[perm[oi]]
            old = old_records[oi]
            try:
                if old.get('domain') and connector_domain_name(np):
                    if old.get('domain') != connector_domain_name(np):
                        compatible = False
                        score += 1000000.0
                if old.get('shape') and connector_shape_name(np):
                    if old.get('shape') != connector_shape_name(np):
                        compatible = False
                        score += 100000.0
                if old.get('size') is not None:
                    ns = connector_size_signature(np)
                    if ns != old.get('size'):
                        score += 5000.0
            except Exception:
                pass

        # A rigid transform preserves all inter-port distances.
        for i in range(n):
            for j in range(i + 1, n):
                try:
                    old_len = _port_pair_length(old_pts[i], old_pts[j])
                    new_len = _port_pair_length(new_ports[perm[i]].Origin,
                                                new_ports[perm[j]].Origin)
                    score += abs(old_len - new_len) / max(RECONNECT_TOLERANCE_FT, 1.0e-9)
                except Exception:
                    score += 100000.0
        item = {'perm': list(perm), 'score': score, 'compatible': compatible}
        if best is None or item['score'] < best['score']:
            best = item
    return best


def _safe_unit(v):
    try:
        if v is not None and v.GetLength() > 1.0e-10:
            return v.Normalize()
    except Exception:
        pass
    return None


def _frame_from_three_points(p0, p1, p2):
    u = _safe_unit(p1 - p0)
    if u is None:
        return None
    raw = p2 - p0
    w = _safe_unit(u.CrossProduct(raw))
    if w is None:
        return None
    v = _safe_unit(w.CrossProduct(u))
    if v is None:
        return None
    return (u, v, w)


def _best_noncollinear_triple(points):
    best = None
    best_area = 0.0
    for comb in itertools.combinations(range(len(points)), 3):
        i, j, k = comb
        try:
            a = points[j] - points[i]
            b = points[k] - points[i]
            area = a.CrossProduct(b).GetLength()
            if area > best_area:
                best_area = area
                best = comb
        except Exception:
            pass
    return best if best_area > 1.0e-9 else None


def _rotation_axis_angle(a, b):
    a = _safe_unit(a)
    b = _safe_unit(b)
    if a is None or b is None:
        return None, 0.0
    d = max(-1.0, min(1.0, a.DotProduct(b)))
    if d > 1.0 - 1.0e-10:
        return None, 0.0
    cr = a.CrossProduct(b)
    if cr.GetLength() > 1.0e-9:
        return cr.Normalize(), math.acos(d)
    # 180 degrees: choose any stable axis perpendicular to a.
    cand = a.CrossProduct(DB.XYZ.BasisX)
    if cand.GetLength() <= 1.0e-9:
        cand = a.CrossProduct(DB.XYZ.BasisY)
    return cand.Normalize(), math.pi


def _rotate_vector(v, axis, angle):
    """Rodrigues rotation used only for computing the second alignment turn."""
    if axis is None or abs(angle) <= 1.0e-12:
        return v
    k = axis.Normalize()
    return (v.Multiply(math.cos(angle)) +
            k.CrossProduct(v).Multiply(math.sin(angle)) +
            k.Multiply(k.DotProduct(v) * (1.0 - math.cos(angle))))


def align_element_by_three_port_points(elem, source_points, target_points):
    """Apply a proper 3D rigid transform using two rotations + one move.

    Returns an explanatory string. No reflection/scale is used.
    """
    if len(source_points) != len(target_points) or len(source_points) < 3:
        raise Exception(u"Cần ít nhất 3 điểm connector để căn fitting trực tiếp")
    triple = _best_noncollinear_triple(target_points)
    if triple is None:
        raise Exception(u"Ba connector mục tiêu gần thẳng hàng, không dựng được hệ trục 3D")
    i, j, k = triple
    sf = _frame_from_three_points(source_points[i], source_points[j], source_points[k])
    tf = _frame_from_three_points(target_points[i], target_points[j], target_points[k])
    if sf is None or tf is None:
        raise Exception(u"Không dựng được hệ trục từ connector cũ/mới")
    su, sv, sw = sf
    tu, tv, tw = tf
    pivot = copy_xyz(source_points[i])

    axis1, angle1 = _rotation_axis_angle(su, tu)
    if axis1 is not None and abs(angle1) > 1.0e-10:
        line1 = DB.Line.CreateBound(pivot, pivot + axis1)
        DB.ElementTransformUtils.RotateElement(doc, elem.Id, line1, angle1)
        doc.Regenerate()

    sw1 = _rotate_vector(sw, axis1, angle1)
    # Signed roll around the already-aligned primary axis.
    x = max(-1.0, min(1.0, sw1.DotProduct(tw)))
    y = tu.DotProduct(sw1.CrossProduct(tw))
    angle2 = math.atan2(y, x)
    if abs(angle2) > 1.0e-10:
        line2 = DB.Line.CreateBound(pivot, pivot + tu)
        DB.ElementTransformUtils.RotateElement(doc, elem.Id, line2, angle2)
        doc.Regenerate()

    move = target_points[i] - pivot
    if move.GetLength() > 1.0e-10:
        DB.ElementTransformUtils.MoveElement(doc, elem.Id, move)
        doc.Regenerate()
    return u"rigid-align 3D: {:.2f}° + {:.2f}°".format(
        math.degrees(angle1), math.degrees(angle2))


def nearest_port_assignment_to_old_geometry(new_ports, old_records):
    n = len(old_records)
    if len(new_ports) != n:
        return None
    best = None
    for perm in itertools.permutations(range(n)):
        max_gap = 0.0
        total = 0.0
        for oi in range(n):
            d = xyz_distance(new_ports[perm[oi]].Origin, old_records[oi]['origin'])
            total += d
            max_gap = max(max_gap, d)
        item = {'perm': list(perm), 'total': total, 'max_gap': max_gap}
        if best is None or item['total'] < best['total']:
            best = item
    return best


def direct_alignment_candidate_score(created, source_points, target_points, old_records, links):
    """Trial one rigid alignment and score BOTH connector origins and axes.

    V3.3 only matched the three connector origins.  For a symmetric Tee/Wye,
    swapping two equal-size run ports can preserve every point distance while
    rotating the family 180 degrees.  V3.4 resolves that ambiguity by requiring
    the new connector BasisZ to follow the OLD fitting connector BasisZ, and by
    requiring every connected partner to face the new fitting connector.
    """
    align_text = align_element_by_three_port_points(created, source_points, target_points)
    doc.Regenerate()

    new_ports = physical_connectors(created)
    final_map = nearest_port_assignment_to_old_geometry(new_ports, old_records)
    if final_map is None:
        return None

    max_gap = final_map.get('max_gap', 1.0e30)
    score = max_gap / max(RECONNECT_TOLERANCE_FT, 1.0e-9) * 1000.0
    compatible = True
    size_ok = True
    worst_old_axis_dot = 1.0
    old_axis_known = False

    # After the trial transform, each old record has one unique connector at its
    # saved origin.  Compare axes after that positional assignment.
    for oi in range(len(old_records)):
        ni = final_map['perm'][oi]
        nc = new_ports[ni]
        old = old_records[oi]
        try:
            if old.get('domain') and connector_domain_name(nc):
                if old.get('domain') != connector_domain_name(nc):
                    compatible = False
                    score += 1000000.0
            if old.get('shape') and connector_shape_name(nc):
                if old.get('shape') != connector_shape_name(nc):
                    compatible = False
                    score += 100000.0
        except Exception:
            pass

        try:
            if old.get('size') is not None and connector_size_signature(nc) != old.get('size'):
                size_ok = False
                score += 10000.0
        except Exception:
            pass

        na = connector_axis(nc)
        oa = old.get('axis')
        if na is not None and oa is not None:
            old_axis_known = True
            d = max(-1.0, min(1.0, na.DotProduct(oa)))
            if d < worst_old_axis_dot:
                worst_old_axis_dot = d
            # Ideal +1.  A 180-degree reversed connector (+ point geometry but
            # wrong axis) receives a very large penalty.
            score += (1.0 - d) * 5000.0

    worst_partner_dot = -1.0
    partner_axis_known = False
    partner_direction_ok = True

    # For each REAL connection, find the old port record at its fitting origin,
    # then verify the new port at that origin faces the external connector.
    for link in links or []:
        partner = find_partner_connector(link)
        if partner is None:
            compatible = False
            score += 1000000.0
            continue

        old_index = None
        old_gap = 1.0e30
        for oi, old in enumerate(old_records):
            try:
                g = xyz_distance(old.get('origin'), link.get('fit_origin'))
                if g < old_gap:
                    old_gap = g
                    old_index = oi
            except Exception:
                pass
        if old_index is None:
            compatible = False
            score += 1000000.0
            continue

        nc = new_ports[final_map['perm'][old_index]]
        if not connector_sizes_match(nc, partner):
            size_ok = False
            score += 10000.0

        d = connector_dot(nc, partner)
        if d is not None:
            partner_axis_known = True
            if d > worst_partner_dot:
                worst_partner_dot = d
            # Ideal is -1.0.  Same direction (+1.0) is unacceptable.
            score += (d + 1.0) * 10000.0
            if d > CONNECTOR_OPPOSITE_DOT_LIMIT:
                partner_direction_ok = False
                score += 100000.0

    return {
        'score': score,
        'max_gap': max_gap,
        'compatible': compatible,
        'size_ok': size_ok,
        'old_axis_known': old_axis_known,
        'worst_old_axis_dot': worst_old_axis_dot,
        'partner_axis_known': partner_axis_known,
        'worst_partner_dot': worst_partner_dot,
        'partner_direction_ok': partner_direction_ok,
        'align_text': align_text,
    }


def choose_direct_alignment_by_axes(created_id, old_records, links):
    """Exhaustively choose the port permutation that preserves connector axes.

    Every candidate is tested in a nested SubTransaction and rolled back.  The
    final returned source_points can then be applied once for real by the caller.
    This is deliberately limited to <= 6 ports; Tee/Wye/Cross are only 3/4.
    """
    created = doc.GetElement(created_id)
    if created is None:
        return None
    base_ports = physical_connectors(created)
    n = len(old_records)
    if n < 3 or len(base_ports) != n or n > 6:
        return None

    # Freeze the default-placement connector origins.  Using points rather than
    # Connector handles makes the winning candidate robust across Regenerate().
    base_points = [copy_xyz(c.Origin) for c in base_ports]
    target_points = [r.get('origin') for r in old_records]
    best = None

    for perm in itertools.permutations(range(n)):
        # Fast compatibility filter before touching model geometry.
        compatible = True
        for oi in range(n):
            c = base_ports[perm[oi]]
            old = old_records[oi]
            try:
                if old.get('domain') and connector_domain_name(c):
                    if old.get('domain') != connector_domain_name(c):
                        compatible = False
                        break
                if old.get('shape') and connector_shape_name(c):
                    if old.get('shape') != connector_shape_name(c):
                        compatible = False
                        break
                if old.get('size') is not None and connector_size_signature(c) != old.get('size'):
                    # Size mismatch is allowed to trial because some family
                    # parameters finish updating only after placement/regenerate.
                    pass
            except Exception:
                pass
        if not compatible:
            continue

        source_points = [base_points[perm[oi]] for oi in range(n)]
        trial = DB.SubTransaction(doc)
        try:
            trial.Start()
            created_trial = doc.GetElement(created_id)
            result = direct_alignment_candidate_score(
                created_trial, source_points, target_points, old_records, links)
            if result is not None:
                result['perm'] = list(perm)
                result['source_points'] = [copy_xyz(p) for p in source_points]
                if best is None or result['score'] < best['score']:
                    best = result
            trial.RollBack()
        except Exception:
            try:
                trial.RollBack()
            except Exception:
                pass

    return best


def direct_alignment_description(candidate):
    if candidate is None:
        return u"không có candidate orientation"
    old_dot = u"n/a"
    partner_dot = u"n/a"
    if candidate.get('old_axis_known', False):
        old_dot = u"{:.3f}".format(candidate.get('worst_old_axis_dot', 0.0))
    if candidate.get('partner_axis_known', False):
        partner_dot = u"{:.3f}".format(candidate.get('worst_partner_dot', 0.0))
    return (u"gap max {:.6f} ft | dot(new,old) xấu nhất {} | "
            u"dot(new,pipe) xấu nhất {} | size {} | direction {}"
            .format(candidate.get('max_gap', 0.0), old_dot, partner_dot,
                    u"OK" if candidate.get('size_ok', False) else u"SAI",
                    u"OK" if candidate.get('partner_direction_ok', False) else u"SAI"))


def disconnect_snapshot_links(links):
    count = 0
    for link in links:
        try:
            fc = link['fit_connector']
            pc = link['partner_connector']
            if connectors_are_connected(fc, pc):
                fc.DisconnectFrom(pc)
                count += 1
        except Exception:
            # If a saved physical relation cannot be disconnected, the caller
            # will rollback the fitting subtransaction.
            raise
    return count


def find_partner_connector(link):
    owner = doc.GetElement(link['partner_owner_id'])
    if owner is None:
        return None
    candidates = get_connectors(owner)
    if not candidates:
        return None

    best = None
    best_score = 1.0e30
    for c in candidates:
        try:
            if is_logical_connector(c):
                continue
            score = xyz_distance(c.Origin, link['partner_origin'])
            if link['domain'] and connector_domain_name(c) != link['domain']:
                score += 1000000.0
            if link['shape'] and connector_shape_name(c) != link['shape']:
                score += 1000.0
            if score < best_score:
                best_score = score
                best = c
        except Exception:
            pass
    return best


def find_new_fitting_connector(new_connectors, link, used_indices):
    best_index = None
    best = None
    best_score = 1.0e30
    target = link['partner_origin'] or link['fit_origin']

    for index, c in enumerate(new_connectors):
        if index in used_indices:
            continue
        try:
            if is_logical_connector(c):
                continue
            score = xyz_distance(c.Origin, target)
            if link['domain'] and connector_domain_name(c) != link['domain']:
                score += 1000000.0
            if link['shape'] and connector_shape_name(c) != link['shape']:
                score += 1000.0
            if score < best_score:
                best_score = score
                best_index = index
                best = c
        except Exception:
            pass
    return best_index, best



def same_family_for_type_ids(type_id_a, type_id_b):
    """True when both FamilySymbols belong to the same loaded Family."""
    try:
        a = doc.GetElement(type_id_a)
        b = doc.GetElement(type_id_b)
        if not isinstance(a, DB.FamilySymbol) or not isinstance(b, DB.FamilySymbol):
            return False
        return a.Family.Id == b.Family.Id
    except Exception:
        return False


def part_type_enum_name(family_symbol):
    """Return the Revit PartType enum name without depending on UI language."""
    value = get_part_type(family_symbol)
    if value is None:
        return u"Undefined"
    try:
        name = System.Enum.GetName(DB.PartType, value)
        if name:
            return safe_text(name)
    except Exception:
        pass
    return safe_text(value)


# V5.8: PartType is no longer hard-coded to Tee/Elbow/Transition/Cross.
# The actual roles are discovered from ALL FamilySymbol elements in the
# OST_PipeFitting category that are loaded in the current project.  This makes
# the tool future-safe for Revit 2025/2026 content such as Wye, LateralTee,
# Union, Cap, Offset, MultiPort, PipeFlange, PipeMechanicalCoupling, etc.
# These tuples are populated during setup_data() before the fitting lists are
# rendered.  Every role still allows at most ONE selected type.
NORMAL_FITTING_ROLES = tuple()
REDUCING_FITTING_ROLES = tuple()

# Preferred UI order for common pipe-fitting PartTypes.  Unknown/new PartTypes
# are appended automatically, so this list is only a display preference and
# never a functional allow-list.
PIPE_FITTING_PARTTYPE_PREFERRED_ORDER = (
    u"Elbow", u"Tee", u"Wye", u"LateralTee", u"Transition", u"Cross",
    u"LateralCross", u"Union", u"PipeMechanicalCoupling", u"PipeFlange",
    u"Cap", u"EndCap", u"Offset", u"TapPerpendicular", u"TapAdjustable",
    u"SpudPerpendicular", u"SpudAdjustable", u"Pants", u"MultiPort",
    u"ValveNormal", u"ValveBreaksInto", u"Normal", u"Undefined"
)


def fitting_role_from_part_type_value(value):
    """Return the exact Revit DB.PartType enum name for any Pipe Fitting type.

    V5.8 deliberately does not maintain a short allow-list.  If unusual custom
    content does not expose FAMILY_CONTENT_PART_TYPE, keep it visible under the
    deterministic 'Undefined' role instead of silently dropping it from UI.
    """
    if value is None:
        return u"Undefined"
    try:
        name = System.Enum.GetName(DB.PartType, value)
        if name:
            return safe_text(name)
    except Exception:
        pass
    # A numeric fallback keeps malformed/custom content visible instead of
    # silently discarding it.  It can still be matched deterministically.
    try:
        return u"PartType_{}".format(int(value))
    except Exception:
        return safe_text(value)


def _split_camel_case(text):
    """Human-readable enum label without depending on Revit UI language."""
    try:
        import re
        value = safe_text(text)
        value = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', value)
        value = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', value)
        return value
    except Exception:
        return safe_text(text)


def fitting_role_display_name(role):
    return _split_camel_case(role) if role else u"PartType không xác định"


def sort_pipe_fitting_roles(roles):
    """Stable order: common pipe roles first, then every discovered role."""
    unique = []
    seen = set()
    for role in list(roles or []):
        role = safe_text(role)
        if role and role not in seen:
            seen.add(role)
            unique.append(role)
    rank = dict((name, idx) for idx, name in enumerate(PIPE_FITTING_PARTTYPE_PREFERRED_ORDER))
    return tuple(sorted(unique, key=lambda r: (rank.get(r, 10000), fitting_role_display_name(r).lower())))



# ============================================================
# V5.8 - Automatic Routing Preferences resolver
# ============================================================

ROUTING_FITTING_GROUP_NAMES = (
    u"Elbows", u"Junctions", u"Crosses", u"Transitions",
    u"Unions", u"MechanicalJoints", u"Caps"
)

ROUTING_GROUP_DISPLAY = {
    u"Segments": u"Segments",
    u"Elbows": u"Elbow",
    u"Junctions": u"Junction",
    u"Crosses": u"Cross",
    u"Transitions": u"Transition",
    u"Unions": u"Union",
    u"MechanicalJoints": u"Mechanical Joint",
    u"Caps": u"Cap",
}


def _routing_group_enum(group_name):
    try:
        return getattr(DB.RoutingPreferenceRuleGroupType, safe_text(group_name))
    except Exception:
        return None


def _connector_nominal_diameter_ft(conn):
    """Round pipe nominal diameter in Revit internal feet."""
    try:
        sig = connector_size_signature(conn)
        if sig and sig[0] == u"round" and len(sig) >= 2:
            return float(sig[1]) * 2.0
    except Exception:
        pass
    return None


def _connector_axis(conn):
    try:
        return conn.CoordinateSystem.BasisZ.Normalize()
    except Exception:
        return None


def routing_primary_size_ft(fitting):
    """Best approximation of Revit Primary Size for a pipe fitting rule.

    For Tee/Wye/Cross the main run is the most-collinear connector pair, so its
    diameter drives the Routing Preference size range.  For one/two-port parts
    use the largest live diameter.  This also behaves predictably for reducing
    fittings, whose branch/secondary size may differ from the run.
    """
    conns = list(physical_connectors(fitting) or [])
    sized = []
    for c in conns:
        d = _connector_nominal_diameter_ft(c)
        if d is not None and d > 0:
            sized.append((c, d))
    if not sized:
        return None
    if len(sized) <= 2:
        return max([x[1] for x in sized])

    best_pair = None
    best_dot = -1.0
    for i in range(len(sized)):
        ai = _connector_axis(sized[i][0])
        if ai is None:
            continue
        for j in range(i + 1, len(sized)):
            aj = _connector_axis(sized[j][0])
            if aj is None:
                continue
            try:
                score = abs(float(ai.DotProduct(aj)))
            except Exception:
                continue
            if score > best_dot:
                best_dot = score
                best_pair = (sized[i][1], sized[j][1])
    if best_pair is not None:
        return max(best_pair)
    return max([x[1] for x in sized])


def routing_group_name_for_fitting(fitting):
    """Map an existing Pipe Fitting to the RoutingPreference group of a PipeType."""
    symbol = get_symbol_from_instance(fitting)
    ptn = part_type_enum_name(symbol).lower().replace(u" ", u"")

    # Explicit PartType semantics first.  This preserves a reducing elbow as an
    # Elbow instead of misclassifying it as a Transition just because sizes differ.
    if u"cross" in ptn:
        return u"Crosses"
    if u"transition" in ptn or u"reducer" in ptn:
        return u"Transitions"
    if u"elbow" in ptn or u"bend" in ptn:
        return u"Elbows"
    if u"union" in ptn:
        return u"Unions"
    if (u"mechanicaljoint" in ptn or u"mechanicalcoupling" in ptn or
            u"pipeflange" in ptn or u"flange" in ptn or u"coupling" in ptn):
        return u"MechanicalJoints"
    if u"cap" in ptn:
        return u"Caps"
    if (u"tee" in ptn or u"wye" in ptn or u"lateral" in ptn or
            u"junction" in ptn or u"tap" in ptn or u"spud" in ptn or
            u"pants" in ptn or u"multiport" in ptn):
        return u"Junctions"

    # Geometry fallback for custom/Undefined content.
    conns = list(physical_connectors(fitting) or [])
    count = len(conns)
    if count == 1:
        return u"Caps"
    if count == 3:
        return u"Junctions"
    if count == 4:
        return u"Crosses"
    if count == 2:
        a0 = _connector_axis(conns[0])
        a1 = _connector_axis(conns[1])
        dot_abs = None
        if a0 is not None and a1 is not None:
            try:
                dot_abs = abs(float(a0.DotProduct(a1)))
            except Exception:
                pass
        # Collinear two-port unequal-size fitting is normally a transition;
        # equal-size collinear fitting is normally a union/coupling.
        if dot_abs is not None and dot_abs > 0.985:
            if fitting_has_variable_port_sizes(fitting):
                return u"Transitions"
            return u"Unions"
        return u"Elbows"
    return None


def _routing_rule_criteria(rule):
    result = []
    count = None
    try:
        count = int(rule.GetNumberOfCriteria())
    except Exception:
        try:
            count = int(rule.NumberOfCriteria)
        except Exception:
            count = None
    if count is None:
        return result
    for i in range(max(0, count)):
        try:
            result.append(rule.GetCriterion(i))
        except Exception:
            pass
    return result


def _float_property(obj, names):
    for name in names:
        try:
            value = getattr(obj, name)
            if callable(value):
                value = value()
            return float(value)
        except Exception:
            pass
    return None


def _primary_size_bounds(criterion):
    """Read PrimarySizeCriterion range without binding to one Revit minor API."""
    if criterion is None:
        return (None, None, False)
    try:
        type_name = safe_text(criterion.GetType().Name).lower()
    except Exception:
        type_name = safe_text(type(criterion)).lower()
    if u"primarysize" not in type_name and u"sizecriterion" not in type_name:
        return (None, None, False)
    min_size = _float_property(criterion, (
        'MinimumSize', 'MinSize', 'Minimum', 'MinimumDiameter'))
    max_size = _float_property(criterion, (
        'MaximumSize', 'MaxSize', 'Maximum', 'MaximumDiameter'))
    return (min_size, max_size, True)


def routing_rule_matches_size(rule, primary_size_ft, tol=1.0e-9):
    """Evaluate PrimarySizeCriterion. Rules are tested in manager order."""
    criteria = _routing_rule_criteria(rule)
    if not criteria:
        return True
    for criterion in criteria:
        min_size, max_size, recognized = _primary_size_bounds(criterion)
        if not recognized:
            # Current Pipe routing rules use PrimarySizeCriterion. Unknown future
            # criteria are left to rule priority instead of rejecting valid content.
            continue
        if primary_size_ft is None:
            continue
        if min_size is not None and min_size > 0.0 and primary_size_ft < min_size - tol:
            return False
        if max_size is not None and max_size > 0.0 and primary_size_ft > max_size + tol:
            return False
        if min_size is not None and max_size is not None and min_size > max_size + tol:
            return False
    return True


def routing_rule_range_text(rule):
    criteria = _routing_rule_criteria(rule)
    ranges = []
    for criterion in criteria:
        mn, mx, recognized = _primary_size_bounds(criterion)
        if not recognized:
            continue
        try:
            mn_text = u"{:.1f}".format(float(mn) * 304.8) if mn is not None and mn > 0 else u"All"
        except Exception:
            mn_text = u"?"
        try:
            mx_text = u"{:.1f}".format(float(mx) * 304.8) if mx is not None and mx > 0 else u"All"
        except Exception:
            mx_text = u"?"
        ranges.append(u"{}–{} mm".format(mn_text, mx_text))
    return u", ".join(ranges) if ranges else u"All sizes"


def _routing_rule_part(rule):
    try:
        part_id = rule.MEPPartId
    except Exception:
        return None
    try:
        if not is_valid_element_id(part_id):
            return None
    except Exception:
        pass
    try:
        return doc.GetElement(part_id)
    except Exception:
        return None


def _routing_rule_family_symbol(rule, fitting=None):
    part = _routing_rule_part(rule)
    if isinstance(part, DB.FamilySymbol):
        return part
    if isinstance(part, DB.Family):
        wanted_role = u""
        try:
            wanted_role = fitting_role_from_symbol(get_symbol_from_instance(fitting))
        except Exception:
            pass
        candidates = []
        try:
            for sid in part.GetFamilySymbolIds():
                sym = doc.GetElement(sid)
                if isinstance(sym, DB.FamilySymbol) and category_is(sym, DB.BuiltInCategory.OST_PipeFitting):
                    candidates.append(sym)
        except Exception:
            pass
        if wanted_role:
            same = [s for s in candidates if fitting_role_from_symbol(s) == wanted_role]
            if same:
                return sorted(same, key=lambda s: get_real_name(s).lower())[0]
        if candidates:
            return sorted(candidates, key=lambda s: get_real_name(s).lower())[0]
    try:
        if part is not None and category_is(part, DB.BuiltInCategory.OST_PipeFitting):
            if isinstance(part, DB.ElementType):
                return part
    except Exception:
        pass
    return None


def resolve_routing_symbol_for_fitting(pipe_type, fitting):
    """Return (FamilySymbol, info dict) using selected PipeType Routing Preferences.

    Revit evaluates routing rules by group, size criterion, then priority/order.
    V5.8 mirrors that behavior and returns the first valid rule that satisfies the
    existing fitting's primary run size.
    """
    if pipe_type is None:
        raise Exception(u"Không có Pipe Type mới để đọc Routing Preferences")
    group_name = routing_group_name_for_fitting(fitting)
    if not group_name:
        raise Exception(u"Không xác định được Routing Preference group cho PartType '{}' ({} cổng)"
                        .format(part_type_enum_name(get_symbol_from_instance(fitting)),
                                len(physical_connectors(fitting))))
    group = _routing_group_enum(group_name)
    if group is None:
        raise Exception(u"Revit API không có RoutingPreference group '{}'".format(group_name))
    try:
        manager = pipe_type.RoutingPreferenceManager
        count = int(manager.GetNumberOfRules(group))
    except Exception as ex:
        raise Exception(u"Không đọc được Routing Preferences nhóm {}: {}"
                        .format(group_name, safe_text(ex)))
    if count <= 0:
        raise Exception(u"Pipe Type '{}' chưa cấu hình Routing Preferences nhóm {}"
                        .format(get_real_name(pipe_type), ROUTING_GROUP_DISPLAY.get(group_name, group_name)))

    primary_size = routing_primary_size_ft(fitting)
    checked = []
    for index in range(count):
        try:
            rule = manager.GetRule(group, index)
        except Exception as ex:
            checked.append(u"#{} không đọc được rule: {}".format(index + 1, safe_text(ex)))
            continue
        part = _routing_rule_part(rule)
        part_name = get_real_name(part) if part is not None else u"<None>"
        range_text = routing_rule_range_text(rule)
        if not routing_rule_matches_size(rule, primary_size):
            checked.append(u"#{} '{}' [{}] không khớp size".format(index + 1, part_name, range_text))
            continue
        symbol = _routing_rule_family_symbol(rule, fitting)
        if symbol is None:
            checked.append(u"#{} '{}' [{}] không phải Pipe Fitting FamilySymbol hợp lệ"
                           .format(index + 1, part_name, range_text))
            continue
        info = {
            'group_name': group_name,
            'group_display': ROUTING_GROUP_DISPLAY.get(group_name, group_name),
            'rule_index': index,
            'rule_number': index + 1,
            'range_text': range_text,
            'primary_size_ft': primary_size,
            'symbol': symbol,
            'checked': checked,
        }
        return symbol, info

    primary_text = u"?"
    if primary_size is not None:
        try:
            primary_text = u"{:.1f} mm".format(primary_size * 304.8)
        except Exception:
            pass
    detail = u" | ".join(checked[:8])
    raise Exception(u"Không có rule {} phù hợp Primary Size {} trong Pipe Type '{}'. {}"
                    .format(ROUTING_GROUP_DISPLAY.get(group_name, group_name), primary_text,
                            get_real_name(pipe_type), detail))


def routing_preferences_summary(pipe_type):
    """Human-readable summary shown in the V5.8 UI."""
    if pipe_type is None:
        return u"Chưa chọn Pipe Type."
    lines = [u"Pipe Type: {}".format(get_real_name(pipe_type))]
    try:
        manager = pipe_type.RoutingPreferenceManager
    except Exception as ex:
        return u"Không đọc được Routing Preferences: {}".format(safe_text(ex))
    group_names = (u"Segments",) + ROUTING_FITTING_GROUP_NAMES
    total_rules = 0
    for group_name in group_names:
        group = _routing_group_enum(group_name)
        if group is None:
            continue
        try:
            count = int(manager.GetNumberOfRules(group))
        except Exception:
            count = 0
        total_rules += count
        label = ROUTING_GROUP_DISPLAY.get(group_name, group_name)
        if count <= 0:
            lines.append(u"{}: chưa cấu hình".format(label))
            continue
        parts = []
        for i in range(count):
            try:
                rule = manager.GetRule(group, i)
                part = _routing_rule_part(rule)
                parts.append(u"#{} {} [{}]".format(
                    i + 1, get_real_name(part) if part is not None else u"<None>",
                    routing_rule_range_text(rule)))
            except Exception as ex:
                parts.append(u"#{} <lỗi: {}>".format(i + 1, safe_text(ex)))
        lines.append(u"{}: {}".format(label, u"; ".join(parts)))
    lines.append(u"Tổng rule đọc được: {}. Fitting sẽ tự lấy rule đầu tiên thỏa Primary Size."
                 .format(total_rules))
    return u"\n".join(lines)


def fitting_role_from_symbol(symbol):
    try:
        return fitting_role_from_part_type_value(get_part_type(symbol))
    except Exception:
        return u""


def connector_axis(conn):
    try:
        v = conn.CoordinateSystem.BasisZ
        if v and v.GetLength() > 1.0e-9:
            return v.Normalize()
    except Exception:
        pass
    return None


def connector_dot(a, b):
    va = connector_axis(a)
    vb = connector_axis(b)
    if va is None or vb is None:
        return None
    try:
        return va.DotProduct(vb)
    except Exception:
        return None


def connector_pair_direction_ok(fitting_connector, partner_connector, limit=CONNECTOR_OPPOSITE_DOT_LIMIT):
    """Return (ok, dot). Physical fitting/pipe connector axes should face each other."""
    d = connector_dot(fitting_connector, partner_connector)
    if d is None:
        # Some content/API states do not expose a stable connector frame.
        # Do not reject solely because direction cannot be read.
        return True, None
    return d <= limit, d



def _neg_xyz(v):
    try:
        return v.Multiply(-1.0)
    except Exception:
        return None


def _solve_3x3(matrix, vector):
    """Small Gaussian-elimination solver. Returns (x,y,z) or None."""
    try:
        m = []
        for i in range(3):
            m.append([
                float(matrix[i][0]), float(matrix[i][1]), float(matrix[i][2]),
                float(vector[i])
            ])

        for col in range(3):
            pivot = col
            pivot_abs = abs(m[pivot][col])
            for row in range(col + 1, 3):
                value = abs(m[row][col])
                if value > pivot_abs:
                    pivot = row
                    pivot_abs = value
            if pivot_abs <= 1.0e-12:
                return None
            if pivot != col:
                m[col], m[pivot] = m[pivot], m[col]

            div = m[col][col]
            for j in range(col, 4):
                m[col][j] /= div

            for row in range(3):
                if row == col:
                    continue
                factor = m[row][col]
                if abs(factor) <= 1.0e-15:
                    continue
                for j in range(col, 4):
                    m[row][j] -= factor * m[col][j]

        return (m[0][3], m[1][3], m[2][3])
    except Exception:
        return None


def virtual_junction_center(origins, axes):
    """Least-squares intersection of connector center lines.

    Each connector contributes its infinite line Origin + t*BasisZ.  This center
    is independent of the fitting's center-to-end dimensions, which is exactly
    what V4.8 needs when two families have different physical lengths.
    """
    valid = []
    for point, axis in zip(origins, axes):
        try:
            d = _safe_unit(axis)
            if point is not None and d is not None:
                valid.append((point, d))
        except Exception:
            pass

    if not valid:
        return xyz_centroid(origins)

    # Minimize sum ||(I - d*d^T)(x - p)||^2.
    a = [[0.0, 0.0, 0.0],
         [0.0, 0.0, 0.0],
         [0.0, 0.0, 0.0]]
    b = [0.0, 0.0, 0.0]

    for p, d in valid:
        dv = [d.X, d.Y, d.Z]
        pv = [p.X, p.Y, p.Z]
        for r in range(3):
            for c in range(3):
                q = (1.0 if r == c else 0.0) - dv[r] * dv[c]
                a[r][c] += q
                b[r] += q * pv[c]

    solved = _solve_3x3(a, b)
    if solved is not None:
        return DB.XYZ(solved[0], solved[1], solved[2])

    # Parallel/near-parallel connector lines do not define a unique center.
    return xyz_centroid([p for p, d in valid])


def point_to_axis_line_distance(point, line_point, line_axis):
    try:
        d = _safe_unit(line_axis)
        if point is None or line_point is None or d is None:
            return 1.0e30
        return (point - line_point).CrossProduct(d).GetLength()
    except Exception:
        return 1.0e30


def _frame_from_two_axes(a, b):
    """Build an orthonormal right-handed frame from two non-parallel vectors."""
    u = _safe_unit(a)
    bb = _safe_unit(b)
    if u is None or bb is None:
        return None
    try:
        raw_v = bb - u.Multiply(bb.DotProduct(u))
        v = _safe_unit(raw_v)
        if v is None:
            return None
        w = _safe_unit(u.CrossProduct(v))
        if w is None:
            return None
        return (u, v, w)
    except Exception:
        return None


def _best_axis_pair(source_axes, target_axes):
    """Choose the most non-parallel mapped axis pair for a stable 3D rotation."""
    best = None
    best_quality = 0.0
    n = min(len(source_axes), len(target_axes))
    for i in range(n):
        for j in range(i + 1, n):
            sa = _safe_unit(source_axes[i])
            sb = _safe_unit(source_axes[j])
            ta = _safe_unit(target_axes[i])
            tb = _safe_unit(target_axes[j])
            if sa is None or sb is None or ta is None or tb is None:
                continue
            try:
                q_source = sa.CrossProduct(sb).GetLength()
                q_target = ta.CrossProduct(tb).GetLength()
                quality = min(q_source, q_target)
                if quality > best_quality:
                    best_quality = quality
                    best = (i, j)
            except Exception:
                pass
    if best_quality <= 1.0e-5:
        return None
    return best


def align_element_by_connector_axes(elem, source_axes, target_axes, source_center, target_center):
    """Rigidly rotate fitting axes onto target axes, then move virtual centers."""
    pair = _best_axis_pair(source_axes, target_axes)
    if pair is None:
        raise Exception(u"Connector axes song song/gần song song; không xác định được orientation 3D duy nhất")

    i, j = pair
    sf = _frame_from_two_axes(source_axes[i], source_axes[j])
    tf = _frame_from_two_axes(target_axes[i], target_axes[j])
    if sf is None or tf is None:
        raise Exception(u"Không dựng được frame từ connector axes")

    su, sv, sw = sf
    tu, tv, tw = tf
    pivot = copy_xyz(source_center)
    if pivot is None:
        raise Exception(u"Không xác định được tâm fitting mới")

    axis1, angle1 = _rotation_axis_angle(su, tu)
    if axis1 is not None and abs(angle1) > 1.0e-10:
        line1 = DB.Line.CreateBound(pivot, pivot + axis1)
        DB.ElementTransformUtils.RotateElement(doc, elem.Id, line1, angle1)
        doc.Regenerate()

    sv1 = _rotate_vector(sv, axis1, angle1)
    x = max(-1.0, min(1.0, sv1.DotProduct(tv)))
    y = tu.DotProduct(sv1.CrossProduct(tv))
    angle2 = math.atan2(y, x)
    if abs(angle2) > 1.0e-10:
        line2 = DB.Line.CreateBound(pivot, pivot + tu)
        DB.ElementTransformUtils.RotateElement(doc, elem.Id, line2, angle2)
        doc.Regenerate()

    move = target_center - source_center
    if move.GetLength() > 1.0e-10:
        DB.ElementTransformUtils.MoveElement(doc, elem.Id, move)
        doc.Regenerate()

    return u"axis-center align: {:.2f}° + {:.2f}°".format(
        math.degrees(angle1), math.degrees(angle2))


def find_link_for_old_port(old_record, links):
    best = None
    best_dist = 1.0e30
    target = old_record.get('origin')
    for link in links:
        try:
            d = xyz_distance(target, link.get('fit_origin'))
            if d < best_dist:
                best_dist = d
                best = link
        except Exception:
            pass
    if best is not None and best_dist <= max(RECONNECT_TOLERANCE_FT * 4.0, 1.0e-5):
        return best
    return None


def _owner_curve_axis(owner, near_point=None):
    """Return the actual geometric centerline axis of a line-based MEP owner.

    V4.8 intentionally prefers LocationCurve over Connector.BasisZ.  A custom
    family can have connector arrows authored with an unexpected orientation,
    while the pipe LocationCurve is the authoritative centerline that the new
    fitting must land on.
    """
    if owner is None:
        return None
    try:
        curve = owner.Location.Curve
        p0 = copy_xyz(curve.GetEndPoint(0))
        p1 = copy_xyz(curve.GetEndPoint(1))
        axis = _safe_unit(p1 - p0)
        if axis is not None:
            return axis
    except Exception:
        pass
    return None


def _target_point_axis_for_old_port(old_record, links):
    """Resolve target point/axis from the CONNECTED PIPE whenever possible.

    Returns (point, axis, source_text).  For an open fitting port we fall back
    to the old fitting connector geometry because there is no external pipe
    centerline to preserve.
    """
    point = old_record.get('origin')
    axis = _safe_unit(old_record.get('axis'))
    source = u"old fitting"
    link = find_link_for_old_port(old_record, links)
    if link is None:
        return point, axis, source

    try:
        owner = doc.GetElement(link['partner_owner_id'])
    except Exception:
        owner = None
    try:
        partner = find_partner_connector(link)
    except Exception:
        partner = None

    # Use the live external connector origin if available.
    try:
        if partner is not None:
            point = copy_xyz(partner.Origin)
        elif link.get('partner_origin') is not None:
            point = copy_xyz(link.get('partner_origin'))
    except Exception:
        pass

    # Most important V4.8 change: use the real MEPCurve centerline axis first.
    curve_axis = _owner_curve_axis(owner, point)
    if curve_axis is not None:
        axis = curve_axis
        source = u"pipe LocationCurve"
    else:
        try:
            pa = connector_axis(partner) if partner is not None else None
            if pa is not None:
                axis = pa
                source = u"partner Connector.BasisZ"
        except Exception:
            pass

    return point, _safe_unit(axis), source


def target_geometry_for_old_ports(old_records, links):
    points, axes, sources = [], [], []
    for old in old_records:
        point, axis, source = _target_point_axis_for_old_port(old, links)
        points.append(point)
        axes.append(axis)
        sources.append(source)
    return points, axes, sources


def target_axes_for_old_ports(old_records, links):
    """V4.8 target axes: actual connected pipe centerlines are authoritative."""
    points, axes, sources = target_geometry_for_old_ports(old_records, links)
    return axes


def axes_have_nonparallel_pair(axes):
    axes = [_safe_unit(a) for a in axes]
    for i in range(len(axes)):
        for j in range(i + 1, len(axes)):
            if axes[i] is None or axes[j] is None:
                continue
            try:
                if axes[i].CrossProduct(axes[j]).GetLength() > 1.0e-5:
                    return True
            except Exception:
                pass
    return False


def port_axes_have_nonparallel_pair(old_records):
    return axes_have_nonparallel_pair([r.get('axis') for r in old_records])


def target_pipe_geometry_description(old_records, links):
    try:
        points, axes, sources = target_geometry_for_old_ports(old_records, links)
        center = virtual_junction_center(points, axes)
        residual = 0.0
        if center is not None:
            for p, a in zip(points, axes):
                residual = max(residual, point_to_axis_line_distance(center, p, a))
        old_vs_pipe = []
        for old, a in zip(old_records, axes):
            oa = _safe_unit(old.get('axis'))
            aa = _safe_unit(a)
            if oa is not None and aa is not None:
                try:
                    old_vs_pipe.append(abs(max(-1.0, min(1.0, oa.DotProduct(aa)))))
                except Exception:
                    pass
        worst = min(old_vs_pipe) if old_vs_pipe else None
        source_text = u"/".join(sorted(set(sources)))
        return (u"PIPE-center signature {} | giao tuyến residual {:.6f} ft | "
                u"|dot(old fitting axis, pipe axis)| xấu nhất {} | nguồn {}"
                .format(_signature_text(_undirected_axis_signature(axes)), residual,
                        u"{:.3f}".format(worst) if worst is not None else u"n/a",
                        source_text))
    except Exception as ex:
        return u"không đọc được pipe-center diagnostics: {}".format(safe_text(ex))


def _port_compatibility_penalty(conn, old):
    penalty = 0.0
    compatible = True
    size_ok = True
    try:
        if old.get('domain') and connector_domain_name(conn):
            if old.get('domain') != connector_domain_name(conn):
                penalty += 10000000.0
                compatible = False
        if old.get('shape') and connector_shape_name(conn):
            if old.get('shape') != connector_shape_name(conn):
                penalty += 1000000.0
                compatible = False
        if old.get('size') is not None:
            new_size = connector_size_signature(conn)
            if not size_signatures_match(new_size, old.get('size')):
                err = size_signature_error(new_size, old.get('size'))
                penalty += 10000.0 + min(err, 1.0) * 100000.0
                size_ok = False
    except Exception:
        pass
    return penalty, compatible, size_ok


def _radial_direction(point, center):
    try:
        return _safe_unit(point - center)
    except Exception:
        return None


def _radial_match_dot(new_point, new_center, old_point, old_center):
    """Signed direction match from virtual junction center to each port.

    This is the key Tee/Wye disambiguator in V4.8: the two run connectors share
    one infinite centerline, so line-distance alone cannot tell the left end
    from the right end.  Radial direction can.
    """
    nr = _radial_direction(new_point, new_center)
    orr = _radial_direction(old_point, old_center)
    if nr is None or orr is None:
        return None
    try:
        return max(-1.0, min(1.0, nr.DotProduct(orr)))
    except Exception:
        return None


def best_axis_line_assignment(new_ports, old_records, target_axes, target_points=None):
    """Match ports to old pipe centerlines and preserve WHICH SIDE of junction.

    V3.8 treated each connector axis as an infinite undirected line.  For a Tee
    that makes the two collinear run ports indistinguishable and can map the
    left connector to the right pipe.  V4.8 additionally compares the radial
    direction from the virtual junction center, so a 180-degree swapped run is
    heavily rejected while different center-to-end lengths remain allowed.
    """
    n = len(old_records)
    if n == 0 or len(new_ports) != n or n > 6:
        return None

    old_points = list(target_points) if target_points is not None else [r.get('origin') for r in old_records]
    old_center = virtual_junction_center(old_points, target_axes)
    new_axes = [connector_axis(c) for c in new_ports]
    new_center = virtual_junction_center([copy_xyz(c.Origin) for c in new_ports], new_axes)

    best = None
    for perm in itertools.permutations(range(n)):
        score = 0.0
        compatible = True
        size_ok = True
        max_line_offset = 0.0
        worst_axis_dot = 1.0
        worst_signed_dot = 1.0
        worst_radial_dot = 1.0
        reversed_ports = 0
        side_mismatches = 0
        axis_known = False

        for oi in range(n):
            conn = new_ports[perm[oi]]
            old = old_records[oi]
            penalty, ok, sok = _port_compatibility_penalty(conn, old)
            score += penalty
            compatible = compatible and ok
            size_ok = size_ok and sok

            ta = target_axes[oi]
            na = connector_axis(conn)
            if ta is not None and na is not None:
                axis_known = True
                signed_dot = max(-1.0, min(1.0, na.DotProduct(ta)))
                line_dot = abs(signed_dot)
                worst_axis_dot = min(worst_axis_dot, line_dot)
                worst_signed_dot = min(worst_signed_dot, signed_dot)
                if signed_dot < 0.0:
                    reversed_ports += 1
                score += (1.0 - line_dot) * 100000.0
                if signed_dot < 0.0:
                    score += 2.0
            else:
                score += 100.0

            target_point = old_points[oi] if oi < len(old_points) else old.get('origin')
            offset = point_to_axis_line_distance(conn.Origin, target_point, ta)
            max_line_offset = max(max_line_offset, offset)
            score += offset / max(CENTERLINE_TOLERANCE_FT, 1.0e-9) * 1000.0

            rd = _radial_match_dot(conn.Origin, new_center, target_point, old_center)
            if rd is not None:
                worst_radial_dot = min(worst_radial_dot, rd)
                # Strongly preserve the physical side of every Tee/Wye port.
                score += (1.0 - rd) * 200000.0
                if rd < 0.0:
                    side_mismatches += 1
                    score += 10000000.0

        mapped_keys = []
        mapped_origins = []
        mapped_axes = []
        for oi in range(n):
            mc = new_ports[perm[oi]]
            mapped_keys.append(connector_identity_key(mc))
            mapped_origins.append(copy_xyz(mc.Origin))
            mapped_axes.append(copy_xyz(connector_axis(mc)))

        item = {
            'perm': list(perm),
            'score': score,
            'compatible': compatible,
            'size_ok': size_ok,
            'max_line_offset': max_line_offset,
            'worst_axis_dot': worst_axis_dot,
            'worst_signed_dot': worst_signed_dot,
            'worst_radial_dot': worst_radial_dot,
            'side_mismatches': side_mismatches,
            'reversed_ports': reversed_ports,
            'axis_known': axis_known,

            # V4.8: freeze the exact post-transform connector identity used by
            # this assignment. Do NOT later re-interpret perm[] against a fresh
            # ConnectorSet enumeration; its order can differ after Regenerate().
            'mapped_port_keys': mapped_keys,
            'mapped_port_origins': mapped_origins,
            'mapped_port_axes': mapped_axes,
        }
        if best is None or item['score'] < best['score']:
            best = item

    return best


def _map_vector_between_frames(v, source_frame, target_frame):
    """Apply the proper rotation that maps source_frame onto target_frame."""
    try:
        su, sv, sw = source_frame
        tu, tv, tw = target_frame
        x = v.DotProduct(su)
        y = v.DotProduct(sv)
        z = v.DotProduct(sw)
        return (tu.Multiply(x) + tv.Multiply(y) + tw.Multiply(z))
    except Exception:
        return None


def _map_point_between_frames(point, source_center, target_center,
                              source_frame, target_frame):
    try:
        rel = point - source_center
        mapped_rel = _map_vector_between_frames(rel, source_frame, target_frame)
        if mapped_rel is None:
            return None
        return target_center + mapped_rel
    except Exception:
        return None


def _undirected_axis_signature(axes):
    """Sorted acute pairwise line angles in degrees (0..90)."""
    values = []
    for i in range(len(axes)):
        for j in range(i + 1, len(axes)):
            a = _safe_unit(axes[i])
            b = _safe_unit(axes[j])
            if a is None or b is None:
                continue
            try:
                d = abs(max(-1.0, min(1.0, a.DotProduct(b))))
                values.append(math.degrees(math.acos(d)))
            except Exception:
                pass
    values.sort()
    return values


def _signature_max_error(a, b):
    if len(a) != len(b):
        return 999.0
    if not a:
        return 0.0
    return max([abs(float(x) - float(y)) for x, y in zip(a, b)])


def _signature_text(sig):
    try:
        return u"[{}]".format(u", ".join([u"{:.2f}°".format(v) for v in sig]))
    except Exception:
        return safe_text(sig)


def choose_axis_center_alignment(created_id, old_records, links):
    """V4.8: find the best rigid axis-center alignment without trial-rotating Revit.

    V3.7 physically rotated the new fitting inside many SubTransactions while
    testing permutations/sign masks. Some MEP fitting families reject one of
    those temporary rotations, which made the search return None even when a
    valid geometric solution existed. V4.8 solves each candidate entirely in
    vector math, then the winning transform is applied once by the caller.

    Connector BasisZ is treated as an UNDIRECTED centerline for geometry. A
    per-port +/- sign is still tried so the final rotation is always a proper
    3D rotation (never a mirror/reflection).
    """
    created = doc.GetElement(created_id)
    if created is None:
        return None

    base_ports = physical_connectors(created)
    n = len(old_records)
    if n < 2 or len(base_ports) != n or n > 6:
        return None

    target_points, target_axes, target_sources = target_geometry_for_old_ports(old_records, links)
    if any(a is None for a in target_axes) or any(p is None for p in target_points):
        return None

    base_origins = [copy_xyz(c.Origin) for c in base_ports]
    base_axes = [connector_axis(c) for c in base_ports]
    if any(a is None for a in base_axes):
        return None

    source_center = virtual_junction_center(base_origins, base_axes)
    target_center = virtual_junction_center(target_points, target_axes)
    if source_center is None or target_center is None:
        return None

    target_sig = _undirected_axis_signature(target_axes)
    source_sig_all = _undirected_axis_signature(base_axes)
    raw_sig_error = _signature_max_error(source_sig_all, target_sig)

    best = None
    for perm in itertools.permutations(range(n)):
        mapped_source_axes = [base_axes[perm[oi]] for oi in range(n)]
        mapped_source_origins = [base_origins[perm[oi]] for oi in range(n)]

        for mask in range(1 << n):
            signed_targets = []
            for i in range(n):
                ta = target_axes[i]
                signed_targets.append(_neg_xyz(ta) if (mask & (1 << i)) else ta)

            pair = _best_axis_pair(mapped_source_axes, signed_targets)
            if pair is None:
                continue
            i, j = pair
            sf = _frame_from_two_axes(mapped_source_axes[i], mapped_source_axes[j])
            tf = _frame_from_two_axes(signed_targets[i], signed_targets[j])
            if sf is None or tf is None:
                continue

            score = 0.0
            compatible = True
            size_ok = True
            max_line_offset = 0.0
            worst_axis_dot = 1.0
            worst_signed_dot = 1.0
            reversed_ports = 0
            axis_known = False
            transformed_axes = []
            transformed_origins = []

            for oi in range(n):
                conn = base_ports[perm[oi]]
                old = old_records[oi]
                penalty, ok, sok = _port_compatibility_penalty(conn, old)
                score += penalty
                compatible = compatible and ok
                size_ok = size_ok and sok

                ra = _map_vector_between_frames(mapped_source_axes[oi], sf, tf)
                rp = _map_point_between_frames(mapped_source_origins[oi],
                                               source_center, target_center,
                                               sf, tf)
                transformed_axes.append(ra)
                transformed_origins.append(rp)
                ta = target_axes[oi]

                if ra is not None and ta is not None:
                    axis_known = True
                    signed_dot = max(-1.0, min(1.0, ra.DotProduct(ta)))
                    line_dot = abs(signed_dot)
                    worst_axis_dot = min(worst_axis_dot, line_dot)
                    worst_signed_dot = min(worst_signed_dot, signed_dot)
                    if signed_dot < 0.0:
                        reversed_ports += 1
                    score += (1.0 - line_dot) * 100000.0
                    if signed_dot < 0.0:
                        score += 2.0
                else:
                    score += 100000.0

                target_point = target_points[oi]
                offset = point_to_axis_line_distance(rp, target_point, ta)
                max_line_offset = max(max_line_offset, offset)
                score += offset / max(CENTERLINE_TOLERANCE_FT, 1.0e-9) * 1000.0

                # V4.8: preserve the side of each port relative to the virtual
                # junction center. This removes the 180-degree ambiguity of the
                # two collinear Tee run connectors without requiring equal
                # center-to-end lengths.
                rd = _radial_match_dot(rp, target_center, target_point, target_center)
                if rd is not None:
                    score += (1.0 - rd) * 200000.0
                    if rd < 0.0:
                        score += 10000000.0

            # Undirected line-angle mismatch is a useful diagnostic/tie-breaker.
            transformed_sig = _undirected_axis_signature(transformed_axes)
            angle_error_deg = _signature_max_error(transformed_sig, target_sig)
            score += angle_error_deg * 1000.0

            item = {
                'perm': list(perm),
                'score': score,
                'compatible': compatible,
                'size_ok': size_ok,
                'max_line_offset': max_line_offset,
                'worst_axis_dot': worst_axis_dot,
                'worst_signed_dot': worst_signed_dot,
                'reversed_ports': reversed_ports,
                'axis_known': axis_known,
                'source_axes': [copy_xyz(a) for a in mapped_source_axes],
                'target_axes': [copy_xyz(a) for a in target_axes],
                'target_points': [copy_xyz(p) for p in target_points],
                'target_sources': list(target_sources),
                'align_target_axes': [copy_xyz(a) for a in signed_targets],
                'source_center': copy_xyz(source_center),
                'target_center': copy_xyz(target_center),
                'angle_error': angle_error_deg,
                'raw_signature_error': raw_sig_error,
                'source_signature': source_sig_all,
                'target_signature': target_sig,
                'sign_mask': mask,
            }
            if best is None or item['score'] < best['score']:
                best = item

    return best

def axis_center_alignment_description(candidate):
    if candidate is None:
        return u"không có candidate axis-center"
    side_text = u"n/a"
    if 'worst_radial_dot' in candidate:
        try:
            side_text = u"{:.3f}".format(candidate.get('worst_radial_dot', 0.0))
        except Exception:
            pass
    source_text = u"/".join(sorted(set(candidate.get('target_sources', []) or [])))
    return (u"line offset max {:.6f} ft | |dot(axis-line)| xấu nhất {:.3f} | "
            u"dot(side) xấu nhất {} | reversed-arrow {} | angle signature error {:.3f}° | size {} | "
            u"new-line {} -> PIPE-line {} | target {}"
            .format(candidate.get('max_line_offset', 0.0),
                    candidate.get('worst_axis_dot', 0.0), side_text,
                    candidate.get('reversed_ports', 0),
                    candidate.get('angle_error', 0.0),
                    u"OK" if candidate.get('size_ok', False) else u"SAI",
                    _signature_text(candidate.get('source_signature', [])),
                    _signature_text(candidate.get('target_signature', [])), source_text))



def _all_uniform_round_target_radius(old_records):
    radii = []
    for old in old_records:
        sig = old.get('size')
        if sig is None or len(sig) != 2 or sig[0] != u'round':
            return None
        try:
            radii.append(float(sig[1]))
        except Exception:
            return None
    if not radii:
        return None
    r0 = radii[0]
    for r in radii[1:]:
        if abs(r - r0) > SIZE_TOLERANCE_FT:
            return None
    return sum(radii) / float(len(radii))


def _created_ports_match_uniform_radius(created, target_radius):
    ports = physical_connectors(created)
    if not ports:
        return False
    for c in ports:
        sig = connector_size_signature(c)
        if sig is None or len(sig) != 2 or sig[0] != u'round':
            return False
        if abs(float(sig[1]) - float(target_radius)) > SIZE_TOLERANCE_FT:
            return False
    return True


def _normalized_parameter_name(param_or_name):
    """Normalize a Revit parameter name for robust IronPython matching."""
    try:
        if hasattr(param_or_name, 'Definition'):
            value = param_or_name.Definition.Name
        else:
            value = param_or_name
    except Exception:
        value = param_or_name
    name = safe_text(value).strip().lower()
    # Normalize common separators so e.g. "Nominal-Diameter 1" and
    # "Nominal Diameter 1" are treated identically.
    for ch in [u'_', u'-', u'/', u'\\', u'(', u')', u'[', u']']:
        name = name.replace(ch, u' ')
    try:
        name = u' '.join(name.split())
    except Exception:
        pass
    return name


def _size_param_priority(param):
    """Rank likely connector-size instance parameters.

    V4.8 deliberately recognizes numbered custom parameters such as
    ``Nominal Diameter 1`` / ``Nominal Diameter 2``.  Some IronPython/Revit
    combinations were observed to miss these in the old compact token test,
    even though the same parameters appeared in diagnostics.
    """
    name = _normalized_parameter_name(param)
    compact = name.replace(u' ', u'')

    # Strong, explicit diameter/DN patterns.
    if (u'diameter' in name or u'đường kính' in name or
            u'nominaldiameter' in compact or
            u'pipe diameter' in name or u'connector diameter' in name):
        return 0

    # Common short custom names: D, D1, D2, DN, DN1, Dia1, etc.
    words = name.split()
    for word in words:
        w = word.strip().lower()
        if w in (u'd', u'dn', u'dia', u'diam', u'ø', u'φ'):
            return 0
        if ((w.startswith(u'dn') and w[2:].isdigit()) or
                (w.startswith(u'dia') and w[3:].isdigit()) or
                (w.startswith(u'd') and w[1:].isdigit())):
            return 0

    if u'radius' in name or u'bán kính' in name or u'size' in name or u'kích thước' in name:
        return 1
    return 5


def _is_safe_size_driver_parameter(param):
    """True for writable DOUBLE instance params worth trying as size drivers.

    This is intentionally name-based and conservative. Trials still run inside
    SubTransactions and are committed only when connector radii prove that the
    assignment is correct.
    """
    try:
        if param.IsReadOnly or param.StorageType != DB.StorageType.Double:
            return False
    except Exception:
        return False
    name = _normalized_parameter_name(param)
    if _size_param_priority(param) <= 1:
        return True
    # Extra fallback for localized/custom content that includes "nominal" but
    # an unusual diameter abbreviation.
    if u'nominal' in name and any(t in name for t in [u'dia', u'diam', u'dn', u'size']):
        return True
    return False


def _collect_size_driver_candidates(elem, max_items=10):
    """Return stable keys for plausible writable instance size parameters."""
    try:
        params = list(elem.Parameters)
    except Exception:
        params = []
    rows = []
    seen = set()
    for p in params:
        try:
            if not _is_safe_size_driver_parameter(p):
                continue
            pid_value, pname = _parameter_key(p)
            key = (pid_value, _normalized_parameter_name(pname))
            if key in seen:
                continue
            seen.add(key)
            rows.append((pid_value, pname, _size_param_priority(p)))
        except Exception:
            pass
    rows.sort(key=lambda x: (int(x[2]), _normalized_parameter_name(x[1])))
    return rows[:max_items]


def writable_double_parameter_diagnostics(elem, max_items=12):
    rows = []
    try:
        params = list(elem.Parameters)
    except Exception:
        params = []
    for p in params:
        try:
            if p.IsReadOnly or p.StorageType != DB.StorageType.Double:
                continue
            rows.append((safe_text(p.Definition.Name), float(p.AsDouble())))
        except Exception:
            pass
    rows = rows[:max_items]
    return u", ".join([u"{}={:.6f}".format(n, v) for n, v in rows])


def _count_created_ports_matching_uniform_radius(created, target_radius):
    ports = physical_connectors(created)
    if not ports:
        return 0, 0
    matched = 0
    for c in ports:
        sig = connector_size_signature(c)
        if sig is None or len(sig) != 2 or sig[0] != u'round':
            continue
        try:
            if abs(float(sig[1]) - float(target_radius)) <= SIZE_TOLERANCE_FT:
                matched += 1
        except Exception:
            pass
    return matched, len(ports)


def _parameter_key(param):
    try:
        pid = element_id_value(param.Id)
    except Exception:
        pid = None
    try:
        name = safe_text(param.Definition.Name)
    except Exception:
        name = u''
    return pid, name


def _find_writable_double_parameter(elem, pid_value, pname):
    """Reacquire an instance parameter after Regenerate/rollback.

    Parameter objects should not be kept as long-lived handles while trial
    transactions regenerate the family instance. Prefer Parameter.Id when it is
    available and fall back to the definition name.
    """
    try:
        for p in elem.Parameters:
            try:
                if p.IsReadOnly or p.StorageType != DB.StorageType.Double:
                    continue
                if pid_value is not None and element_id_value(p.Id) == pid_value:
                    return p
            except Exception:
                pass
    except Exception:
        pass
    try:
        for p in elem.GetParameters(pname):
            try:
                if (not p.IsReadOnly and
                        p.StorageType == DB.StorageType.Double):
                    return p
            except Exception:
                pass
    except Exception:
        pass
    return None


def _size_value_candidates(param_name, target_radius):
    """Return likely internal-unit values for a size-driving parameter.

    Most fitting content exposes Diameter/DN values as diameter, while some
    custom content exposes Radius. The name only controls the trial order; each
    trial is isolated in a SubTransaction and is kept only when connector sizes
    prove the value is correct.
    """
    name = safe_text(param_name).strip().lower()
    diameter = float(target_radius) * 2.0
    radius = float(target_radius)
    if (u'radius' in name or u'bán kính' in name or name in (u'r', u'rad')):
        return [radius, diameter]
    return [diameter, radius]


def _trial_size_parameter_set(created_id, assignments, target_radius, commit_if_match=False):
    """Trial one or more instance size parameters as an atomic group.

    Returns (all_match, matched_port_count, total_port_count, set_count, error).
    The SubTransaction is rolled back unless commit_if_match=True and every
    physical round connector reaches target_radius.
    """
    st = DB.SubTransaction(doc)
    started = False
    try:
        st.Start()
        started = True
        elem = doc.GetElement(created_id)
        if elem is None:
            st.RollBack()
            return False, 0, 0, 0, u'fitting mới không tồn tại'

        set_count = 0
        for pid_value, pname, value in assignments:
            p = _find_writable_double_parameter(elem, pid_value, pname)
            if p is None:
                continue
            try:
                p.Set(float(value))
                set_count += 1
            except Exception:
                pass

        if set_count != len(assignments):
            st.RollBack()
            return False, 0, 0, set_count, u'không set đủ parameter trong nhóm'

        doc.Regenerate()
        elem = doc.GetElement(created_id)
        matched, total = _count_created_ports_matching_uniform_radius(elem, target_radius)
        all_match = bool(total > 0 and matched == total)
        if all_match and commit_if_match:
            st.Commit()
        else:
            st.RollBack()
        return all_match, matched, total, set_count, u''
    except Exception as ex:
        if started:
            try:
                st.RollBack()
            except Exception:
                pass
        return False, 0, 0, 0, safe_text(ex)


def try_match_direct_fitting_size(created_id, old_records):
    """Best-effort sizing for exact-symbol direct placement (V4.8).

    V4.2 could only trial ONE instance parameter at a time. That fails on many
    Tee/Wye families where run and branch sizes are controlled by separate
    parameters such as ``Nominal Diameter 1`` and ``Nominal Diameter 2``. A
    single parameter may correctly resize only a subset of ports, then the
    trial is rolled back because the remaining ports still have the old size.

    V4.8 keeps the safe SubTransaction strategy but adds an atomic multi-
    parameter solver:
      1) direct Connector.Radius trial;
      2) probe each likely size parameter independently and record how many
         connectors it fixes;
      3) try the strongest parameter combinations (2..4 parameters) together;
      4) commit only when ALL physical round connectors match the target.

    Only writable DOUBLE *instance* parameters are touched. FamilySymbol/type
    parameters are deliberately not changed, so other instances of the same
    type cannot be resized accidentally.
    """
    created = doc.GetElement(created_id)
    if created is None:
        return False, u"fitting mới không tồn tại"

    # V4.8: unequal-size fittings need a different solver. Do not silently
    # keep the Family default size because the later alignment/connect check
    # will correctly reject it. Configure all required port sizes first.
    variable_result, variable_note = try_match_direct_fitting_variable_size(created_id, old_records)
    if variable_result is not None:
        return variable_result, variable_note

    target_radius = _all_uniform_round_target_radius(old_records)
    if target_radius is None:
        return True, u"size không-round: giữ theo Family"
    if _created_ports_match_uniform_radius(created, target_radius):
        return True, u"size connector đã đúng"

    # Trial 1: direct Radius property (works only if API/content allows it).
    trial = DB.SubTransaction(doc)
    try:
        trial.Start()
        current = doc.GetElement(created_id)
        changed = 0
        for c in physical_connectors(current):
            try:
                c.Radius = target_radius
                changed += 1
            except Exception:
                pass
        if changed:
            doc.Regenerate()
            current = doc.GetElement(created_id)
            if _created_ports_match_uniform_radius(current, target_radius):
                trial.Commit()
                return True, u"set Connector.Radius trực tiếp ({})".format(changed)
        trial.RollBack()
    except Exception:
        try:
            trial.RollBack()
        except Exception:
            pass

    # Collect only plausible INSTANCE size drivers. Do not touch type params.
    current = doc.GetElement(created_id)
    # V4.8: robustly collect custom parameters such as
    # "Nominal Diameter 1" / "Nominal Diameter 2".
    raw_candidates = _collect_size_driver_candidates(current, max_items=10)

    if not raw_candidates:
        type_ok, type_note = try_match_direct_fitting_size_by_type(created_id, old_records)
        if type_ok:
            return True, type_note
        current = doc.GetElement(created_id)
        current_sizes = [connector_size_signature(c) for c in physical_connectors(current)]
        return False, (u"không tìm được instance parameter Diameter/Radius/Size có thể ghi; "
                       u"target radius {:.6f} ft; size hiện tại {}; double params [{}] | {}"
                       .format(target_radius, safe_text(current_sizes),
                               writable_double_parameter_diagnostics(current), type_note))

    # Probe each parameter/value independently. The score is useful even when a
    # single parameter does not solve all ports; on a Tee it often reveals e.g.
    # Diameter 1 fixes run ports while Diameter 2 fixes branch port.
    probes = []
    for pid_value, pname, priority in raw_candidates:
        best = None
        for value in _size_value_candidates(pname, target_radius):
            assignment = [(pid_value, pname, value)]
            all_match, matched, total, set_count, err = _trial_size_parameter_set(
                created_id, assignment, target_radius, commit_if_match=False)
            record = {
                'pid': pid_value,
                'name': pname,
                'priority': priority,
                'value': value,
                'matched': matched,
                'total': total,
                'all': all_match,
                'error': err,
            }
            if best is None or matched > best['matched']:
                best = record
            # The first target value is preferred on ties because name-based
            # ordering already puts diameter vs radius in the safest order.
            if all_match:
                ok, m2, t2, sc2, err2 = _trial_size_parameter_set(
                    created_id, assignment, target_radius, commit_if_match=True)
                if ok:
                    return True, (u"auto-size V4.8: set 1 parameter '{}'={:.6f} ft; "
                                  u"connector {}/{} đúng size"
                                  .format(pname, value, m2, t2))
        if best is not None:
            probes.append(best)

    # Search combinations using each parameter's best independently-probed
    # value. Prefer params that fix more ports, then stronger size-like names.
    probes.sort(key=lambda r: (-int(r.get('matched', 0)), int(r.get('priority', 5)),
                               safe_text(r.get('name')).lower()))
    active = [r for r in probes if r.get('matched', 0) > 0]
    if len(active) < 2:
        # A parameter may participate in formulas only when another parameter
        # changes at the same time. Keep strong candidates even if its solo
        # probe had zero visible effect.
        active = probes[:]
    active = active[:6]

    max_group = min(4, len(active))
    for group_size in range(2, max_group + 1):
        for group in itertools.combinations(active, group_size):
            assignments = [(r['pid'], r['name'], r['value']) for r in group]
            ok, matched, total, set_count, err = _trial_size_parameter_set(
                created_id, assignments, target_radius, commit_if_match=True)
            if ok:
                names = u", ".join([u"{}={:.6f}".format(r['name'], r['value']) for r in group])
                return True, (u"auto-size V4.8 đa parameter: [{}] ft; connector {}/{} đúng size"
                              .format(names, matched, total))

    # Final safe attempt: all strong candidates together. This is useful when a
    # family has 3 independent diameter drivers. Still atomic and rolled back on
    # failure.
    if len(active) > 1:
        assignments = [(r['pid'], r['name'], r['value']) for r in active]
        ok, matched, total, set_count, err = _trial_size_parameter_set(
            created_id, assignments, target_radius, commit_if_match=True)
        if ok:
            names = u", ".join([u"{}={:.6f}".format(r['name'], r['value']) for r in active])
            return True, (u"auto-size V4.8 tất cả size-driver: [{}] ft; connector {}/{} đúng size"
                          .format(names, matched, total))

    current = doc.GetElement(created_id)
    current_sizes = [connector_size_signature(c) for c in physical_connectors(current)]
    probe_text = []
    for r in probes:
        probe_text.append(u"{}->{}/{} @ {:.6f}".format(
            r.get('name', u'?'), r.get('matched', 0), r.get('total', 0), r.get('value', 0.0)))
    type_ok, type_note = try_match_direct_fitting_size_by_type(created_id, old_records)
    if type_ok:
        return True, type_note
    return False, (u"auto-size V5.8.3 không giải được đồng thời tất cả connector bằng instance parameter; "
                   u"target radius {:.6f} ft; size hiện tại {}; probe [{}]; double params [{}] | {}"
                   .format(target_radius, safe_text(current_sizes), u"; ".join(probe_text),
                           writable_double_parameter_diagnostics(current), type_note))



def _round_target_radii(old_records):
    """Return one target radius per old physical port, or None if not all round."""
    radii = []
    for old in list(old_records or []):
        sig = old.get('size')
        if sig is None or len(sig) != 2 or sig[0] != u'round':
            return None
        try:
            radii.append(float(sig[1]))
        except Exception:
            return None
    return radii if radii else None


def _round_port_radii(elem):
    radii = []
    for c in physical_connectors(elem):
        sig = connector_size_signature(c)
        if sig is None or len(sig) != 2 or sig[0] != u'round':
            return None
        try:
            radii.append(float(sig[1]))
        except Exception:
            return None
    return radii if radii else None


def _count_radius_multiset_matches(actual_radii, target_radii):
    """Maximum 1D radius matches with tolerance, independent of connector order."""
    if actual_radii is None or target_radii is None:
        return 0, 0
    actual = sorted([float(x) for x in actual_radii])
    target = sorted([float(x) for x in target_radii])
    used = [False] * len(actual)
    matched = 0
    for tr in target:
        best_i = None
        best_err = 1.0e30
        for i, ar in enumerate(actual):
            if used[i]:
                continue
            err = abs(ar - tr)
            if err <= SIZE_TOLERANCE_FT and err < best_err:
                best_i = i
                best_err = err
        if best_i is not None:
            used[best_i] = True
            matched += 1
    return matched, len(target)


def _radius_multiset_matches(actual_radii, target_radii):
    matched, total = _count_radius_multiset_matches(actual_radii, target_radii)
    return bool(total > 0 and actual_radii is not None and
                len(actual_radii) == len(target_radii) and matched == total)



def _fitting_size_profile_label(target_radii):
    """Compact deterministic label such as DN25x40 for generated types."""
    vals = []
    for r in sorted([float(x) for x in list(target_radii or [])]):
        mm = r * 2.0 * 304.8
        if abs(mm - round(mm)) <= 0.02:
            text = u"{}".format(int(round(mm)))
        else:
            text = u"{:.1f}".format(mm).replace(u'.', u'_')
        if text not in vals:
            vals.append(text)
    return u"DN" + u"x".join(vals) if vals else u"SIZE"


def _safe_generated_type_name(base_name, target_radii, created_id_value):
    """Build a legal unique ElementType name for a temporary/generated size type."""
    bad = u'{}[]|;<>?`~'
    base = safe_text(base_name) or u'Fitting'
    for ch in bad:
        base = base.replace(ch, u'_')
    base = base.strip()
    if len(base) > 80:
        base = base[:80]
    return u"{}__AnTools_{}_{}".format(
        base, _fitting_size_profile_label(target_radii), safe_text(created_id_value))


def _same_family_symbols(symbol):
    result = []
    if symbol is None:
        return result
    try:
        fam = symbol.Family
        ids = list(fam.GetFamilySymbolIds()) if fam else []
    except Exception:
        ids = []
    for eid in ids:
        try:
            fs = doc.GetElement(eid)
            if isinstance(fs, DB.FamilySymbol):
                result.append(fs)
        except Exception:
            pass
    result.sort(key=lambda x: get_real_name(x).lower())
    return result


def _assign_instance_symbol_same_family(elem, symbol):
    """Assign a FamilySymbol from the same Family without cross-family ChangeTypeId."""
    if elem is None or symbol is None:
        raise Exception(u'Thiếu instance hoặc FamilySymbol')
    try:
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()
    except Exception:
        pass
    try:
        elem.Symbol = symbol
        return
    except Exception:
        pass
    # Same-family ChangeTypeId is safe and avoids the cross-family MEP warning.
    elem.ChangeTypeId(symbol.Id)


def _trial_existing_family_symbol_profile(created_id, symbol, target_radii, commit_if_match=False):
    st = DB.SubTransaction(doc)
    started = False
    try:
        st.Start()
        started = True
        elem = doc.GetElement(created_id)
        if elem is None:
            st.RollBack()
            return False, None, u'fitting mới không tồn tại'
        _assign_instance_symbol_same_family(elem, symbol)
        doc.Regenerate()
        elem = doc.GetElement(created_id)
        actual = _round_port_radii(elem)
        ok = _radius_multiset_matches(actual, target_radii)
        if ok and commit_if_match:
            st.Commit()
        else:
            st.RollBack()
        return ok, actual, u''
    except Exception as ex:
        if started:
            try:
                st.RollBack()
            except Exception:
                pass
        return False, None, safe_text(ex)


def _trial_duplicate_symbol_profile(created_id, assignments, target_radii, duplicate_name,
                                    commit_if_match=False):
    """Duplicate the selected type, change TYPE parameters, then test connector sizes.

    Failed trials are rolled back completely.  A successful trial commits the
    duplicate type, so existing instances of the user's original type are never resized.
    """
    st = DB.SubTransaction(doc)
    started = False
    try:
        st.Start()
        started = True
        elem = doc.GetElement(created_id)
        if elem is None:
            st.RollBack()
            return False, None, 0, u'fitting mới không tồn tại', None
        base_symbol = get_symbol_from_instance(elem)
        if base_symbol is None or not isinstance(base_symbol, DB.FamilySymbol):
            st.RollBack()
            return False, None, 0, u'không đọc được FamilySymbol hiện tại', None

        dup = base_symbol.Duplicate(duplicate_name)
        try:
            dup_symbol = dup if isinstance(dup, DB.FamilySymbol) else doc.GetElement(dup.Id)
        except Exception:
            dup_symbol = None
        if dup_symbol is None or not isinstance(dup_symbol, DB.FamilySymbol):
            st.RollBack()
            return False, None, 0, u'Duplicate không trả về FamilySymbol', None

        set_count = 0
        for pid_value, pname, value in assignments:
            p = _find_writable_double_parameter(dup_symbol, pid_value, pname)
            if p is None:
                continue
            try:
                p.Set(float(value))
                set_count += 1
            except Exception:
                pass
        if set_count != len(assignments):
            st.RollBack()
            return False, None, set_count, u'không set đủ TYPE parameter trong nhóm', None

        try:
            if not dup_symbol.IsActive:
                dup_symbol.Activate()
        except Exception:
            pass
        doc.Regenerate()
        elem = doc.GetElement(created_id)
        _assign_instance_symbol_same_family(elem, dup_symbol)
        doc.Regenerate()
        elem = doc.GetElement(created_id)
        actual = _round_port_radii(elem)
        ok = _radius_multiset_matches(actual, target_radii)
        if ok and commit_if_match:
            st.Commit()
            return True, actual, set_count, u'', dup_symbol.Id
        st.RollBack()
        return False, actual, set_count, u'', None
    except Exception as ex:
        if started:
            try:
                st.RollBack()
            except Exception:
                pass
        return False, None, 0, safe_text(ex), None


def try_match_direct_fitting_size_by_type(created_id, old_records, max_trials=180):
    """V5.8.3 fallback for content whose connector sizes are TYPE-driven.

    Strategy:
      1) try every already-loaded sibling FamilySymbol in the SAME Family;
      2) if none matches, duplicate the selected type and trial only safe writable
         size-like DOUBLE TYPE parameters;
      3) commit only a matching duplicated type. Existing instances are untouched.
    """
    target_radii = _round_target_radii(old_records)
    if target_radii is None or not target_radii:
        return False, u'type-size fallback chỉ hỗ trợ connector tròn'
    elem = doc.GetElement(created_id)
    if elem is None:
        return False, u'fitting mới không tồn tại'
    base_symbol = get_symbol_from_instance(elem)
    if base_symbol is None or not isinstance(base_symbol, DB.FamilySymbol):
        return False, u'không đọc được FamilySymbol để thử type-size fallback'

    # Prefer a size-specific type that is already loaded in the selected Family.
    existing_notes = []
    for fs in _same_family_symbols(base_symbol):
        try:
            if fs.Id == base_symbol.Id:
                continue
        except Exception:
            pass
        ok, actual, err = _trial_existing_family_symbol_profile(
            created_id, fs, target_radii, commit_if_match=False)
        if ok:
            ok2, actual2, err2 = _trial_existing_family_symbol_profile(
                created_id, fs, target_radii, commit_if_match=True)
            if ok2:
                return True, (u"V5.8.3 type-size: dùng type đã load cùng Family '{}'; "
                              u"connector profile {}"
                              .format(get_real_name(fs), safe_text(actual2)))
        if err:
            existing_notes.append(u"{}: {}".format(get_real_name(fs), err))

    type_candidates = _collect_size_driver_candidates(base_symbol, max_items=8)
    if not type_candidates:
        return False, (u"V5.8.3 type-size: không có type cùng Family khớp profile và không tìm được "
                       u"TYPE parameter Diameter/DN/Radius/Size có thể ghi; target radii {}; "
                       u"type double params [{}]"
                       .format(safe_text(sorted(target_radii)),
                               writable_double_parameter_diagnostics(base_symbol, max_items=20)))

    distinct_targets = []
    for r in sorted(target_radii, reverse=True):
        if not any(abs(float(r) - float(x)) <= SIZE_TOLERANCE_FT for x in distinct_targets):
            distinct_targets.append(float(r))

    # Values each type parameter may plausibly expect (diameter first, radius second).
    candidate_rows = []
    for pid_value, pname, priority in type_candidates:
        vals = []
        for tr in distinct_targets:
            for v in _size_value_candidates(pname, tr):
                if not any(abs(float(v) - float(x)) <= 1.0e-9 for x in vals):
                    vals.append(float(v))
        candidate_rows.append((pid_value, pname, priority, vals))

    created_value = element_id_value(created_id)
    base_name = get_real_name(base_symbol)
    profile_name = _safe_generated_type_name(base_name, target_radii, created_value)
    trial_count = 0
    probe = []
    max_group = min(4, len(candidate_rows))

    for group_size in range(1, max_group + 1):
        for group in itertools.combinations(candidate_rows, group_size):
            value_lists = [g[3] for g in group]
            if any(not vals for vals in value_lists):
                continue
            for values in itertools.product(*value_lists):
                trial_count += 1
                if trial_count > max_trials:
                    break
                assignments = [(group[i][0], group[i][1], values[i]) for i in range(group_size)]
                # Failed trials roll back, therefore the same candidate name can be reused.
                trial_name = profile_name
                ok, actual, set_count, err, new_type_id = _trial_duplicate_symbol_profile(
                    created_id, assignments, target_radii, trial_name, commit_if_match=True)
                probe.append((assignments, actual, err))
                if ok:
                    names = u", ".join([u"{}={:.6f}".format(a[1], a[2]) for a in assignments])
                    return True, (u"V5.8.3 TYPE auto-size: duplicate '{}' -> '{}'; [{}] ft; "
                                  u"connector profile {} đúng. Type gốc không bị sửa."
                                  .format(base_name, trial_name, names, safe_text(actual)))
            if trial_count > max_trials:
                break
        if trial_count > max_trials:
            break

    probe_text = []
    for assignments, actual, err in probe[:16]:
        names = u",".join([u"{}={:.4f}".format(a[1], a[2]) for a in assignments])
        probe_text.append(u"[{}]->{}{}".format(names, safe_text(actual),
                                                  (u" ERR=" + safe_text(err)) if err else u""))
    return False, (u"V5.8.3 TYPE auto-size không giải được profile; target radii {}; "
                   u"TYPE size-driver [{}]; thử {} tổ hợp; probe [{}]; type double params [{}]"
                   .format(safe_text(sorted(target_radii)),
                           u", ".join([safe_text(x[1]) for x in type_candidates]),
                           trial_count, u"; ".join(probe_text),
                           writable_double_parameter_diagnostics(base_symbol, max_items=20)))


def _trial_size_parameter_profile(created_id, assignments, target_radii,
                                  commit_if_match=False):
    """Trial an atomic parameter set against an UNEQUAL round-size profile."""
    st = DB.SubTransaction(doc)
    started = False
    try:
        st.Start()
        started = True
        elem = doc.GetElement(created_id)
        if elem is None:
            st.RollBack()
            return False, 0, 0, 0, u'fitting mới không tồn tại', None

        set_count = 0
        for pid_value, pname, value in assignments:
            p = _find_writable_double_parameter(elem, pid_value, pname)
            if p is None:
                continue
            try:
                p.Set(float(value))
                set_count += 1
            except Exception:
                pass
        if set_count != len(assignments):
            st.RollBack()
            return False, 0, len(target_radii), set_count, u'không set đủ parameter trong nhóm', None

        doc.Regenerate()
        elem = doc.GetElement(created_id)
        actual = _round_port_radii(elem)
        matched, total = _count_radius_multiset_matches(actual, target_radii)
        all_match = bool(actual is not None and len(actual) == len(target_radii) and matched == total)
        if all_match and commit_if_match:
            st.Commit()
        else:
            st.RollBack()
        return all_match, matched, total, set_count, u'', actual
    except Exception as ex:
        if started:
            try:
                st.RollBack()
            except Exception:
                pass
        return False, 0, len(target_radii or []), 0, safe_text(ex), None


def try_match_direct_fitting_variable_size(created_id, old_records):
    """V4.8 auto-size solver for reducing fittings with unequal round ports.

    The solver does NOT assume connector enumeration order. It compares the
    multiset of actual connector radii with the target multiset, then searches
    combinations of writable instance Diameter/DN/Radius parameters. This is
    intended for families such as reducing Tee/Elbow where e.g. Diameter 1
    drives the run and Diameter 2 drives the branch.
    """
    target_radii = _round_target_radii(old_records)
    if target_radii is None or len(target_radii) < 2:
        return True, u"size đa cỡ không-round: giữ theo Family"
    if not size_profile_is_variable([(u'round', r) for r in target_radii]):
        return None, u""

    current = doc.GetElement(created_id)
    if current is None:
        return False, u"fitting mới không tồn tại"
    actual = _round_port_radii(current)
    if _radius_multiset_matches(actual, target_radii):
        return True, u"size giảm đã đúng {}".format(safe_text(sorted(target_radii)))

    # V4.8: use the same robust collector as the uniform-size solver.
    # The log that led to this change showed writable ``Nominal Diameter 1``
    # and ``Nominal Diameter 2`` parameters but the V4.7 filter returned zero
    # candidates.
    raw_candidates = _collect_size_driver_candidates(current, max_items=10)

    if not raw_candidates:
        type_ok, type_note = try_match_direct_fitting_size_by_type(created_id, old_records)
        if type_ok:
            return True, type_note
        return False, (u"fitting giảm không có instance parameter Diameter/Radius/Size có thể ghi; "
                       u"target radii {}; hiện tại {}; double params [{}] | {}"
                       .format(safe_text(sorted(target_radii)), safe_text(actual),
                               writable_double_parameter_diagnostics(current), type_note))

    # Unique target radii, largest first (run size is commonly the larger one).
    distinct_targets = []
    for r in sorted(target_radii, reverse=True):
        if not any(abs(r - x) <= SIZE_TOLERANCE_FT for x in distinct_targets):
            distinct_targets.append(r)

    candidate_names = [safe_text(x[1]) for x in raw_candidates]

    # Probe every plausible parameter against every required target size.
    # Keep ALL target variants (diameter and radius form), not only the top
    # three. Reducing content can require D1=large and D2=small simultaneously;
    # one of those values can have little or no useful effect when probed alone.
    variants_by_param = {}
    diagnostics = []
    for pid_value, pname, priority in raw_candidates:
        key = (pid_value, pname)
        rows = []
        value_seen = set()
        for tr in distinct_targets:
            for value in _size_value_candidates(pname, tr):
                rounded = round(float(value), 9)
                if rounded in value_seen:
                    continue
                value_seen.add(rounded)
                assignment = [(pid_value, pname, value)]
                all_match, matched, total, set_count, err, after = _trial_size_parameter_profile(
                    created_id, assignment, target_radii, commit_if_match=False)
                row = {
                    'pid': pid_value, 'name': pname, 'priority': priority,
                    'value': value, 'matched': matched, 'total': total,
                    'all': all_match, 'error': err, 'after': after,
                }
                rows.append(row)
                diagnostics.append(row)
                if all_match:
                    ok, m2, t2, sc2, err2, after2 = _trial_size_parameter_profile(
                        created_id, assignment, target_radii, commit_if_match=True)
                    if ok:
                        return True, (u"auto-size V4.8 fitting giảm: '{}'={:.6f} ft; "
                                      u"profile {}/{} đúng"
                                      .format(pname, value, m2, t2))
        # Deterministic order: most useful solo result first, but retain every
        # unique required-size variant for grouped trials.
        rows.sort(key=lambda r: (-int(r.get('matched', 0)), int(r.get('priority', 5)),
                                 abs(float(r.get('value', 0.0)))))
        variants_by_param[key] = rows

    # Rank parameters by their best independent effect and keep enough for
    # custom families with D1/D2/D3.
    param_keys = list(variants_by_param.keys())
    param_keys.sort(key=lambda k: (
        -max([int(r.get('matched', 0)) for r in variants_by_param.get(k, [])] or [0]),
        min([int(r.get('priority', 5)) for r in variants_by_param.get(k, [])] or [5]),
        _normalized_parameter_name(k[1])))
    param_keys = param_keys[:8]

    trial_count = 0
    max_trials = 420

    # First, explicitly try the common reducing-family pattern: 2-3 diameter
    # parameters assigned to the distinct target sizes in every permutation.
    # Example from the user's Tee: target radii [large, large, small] and
    # writable "Nominal Diameter 1/2". This tests both
    #   D1=large, D2=small
    # and
    #   D1=small, D2=large
    # before the generic search.
    max_direct_group = min(3, len(param_keys))
    for group_size in range(2, max_direct_group + 1):
        for key_group in itertools.combinations(param_keys, group_size):
            # Build one diameter-first value list per parameter, followed by
            # radius-form values for unusual content.
            value_lists = []
            for key in key_group:
                pname = key[1]
                vals = []
                for tr in distinct_targets:
                    for v in _size_value_candidates(pname, tr):
                        if not any(abs(float(v) - float(x)) <= 1.0e-9 for x in vals):
                            vals.append(float(v))
                value_lists.append(vals)
            for values in itertools.product(*value_lists):
                trial_count += 1
                if trial_count > max_trials:
                    break
                assignments = [(key_group[i][0], key_group[i][1], values[i])
                               for i in range(group_size)]
                ok, matched, total, set_count, err, after = _trial_size_parameter_profile(
                    created_id, assignments, target_radii, commit_if_match=True)
                if ok:
                    names = u", ".join([u"{}={:.6f}".format(a[1], a[2]) for a in assignments])
                    return True, (u"auto-size V4.8 fitting giảm đa cỡ: [{}] ft; "
                                  u"target radii {}; connector {}/{} đúng"
                                  .format(names, safe_text(sorted(target_radii)), matched, total))
            if trial_count > max_trials:
                break
        if trial_count > max_trials:
            break

    # Generic grouped search using all retained variants. This catches families
    # where one parameter expects radius while another expects diameter.
    if trial_count <= max_trials:
        max_group = min(4, len(param_keys))
        for group_size in range(2, max_group + 1):
            for key_group in itertools.combinations(param_keys, group_size):
                variant_lists = [variants_by_param.get(k, []) for k in key_group]
                if any(not v for v in variant_lists):
                    continue
                for combo in itertools.product(*variant_lists):
                    trial_count += 1
                    if trial_count > max_trials:
                        break
                    assignments = [(r['pid'], r['name'], r['value']) for r in combo]
                    ok, matched, total, set_count, err, after = _trial_size_parameter_profile(
                        created_id, assignments, target_radii, commit_if_match=True)
                    if ok:
                        names = u", ".join([u"{}={:.6f}".format(r['name'], r['value']) for r in combo])
                        return True, (u"auto-size V4.8 fitting giảm đa cỡ: [{}] ft; "
                                      u"target radii {}; connector {}/{} đúng"
                                      .format(names, safe_text(sorted(target_radii)), matched, total))
                if trial_count > max_trials:
                    break
            if trial_count > max_trials:
                break

    current = doc.GetElement(created_id)
    actual = _round_port_radii(current)
    probe_rows = sorted(diagnostics, key=lambda r: (-int(r.get('matched', 0)),
                                                     safe_text(r.get('name')).lower(),
                                                     float(r.get('value', 0.0))))[:18]
    probe_text = u"; ".join([
        u"{}={:.6f}->{}/{} {}{}".format(
            r.get('name', u'?'), r.get('value', 0.0),
            r.get('matched', 0), r.get('total', 0),
            safe_text(r.get('after')),
            (u" ERR=" + safe_text(r.get('error'))) if r.get('error') else u"")
        for r in probe_rows])
    type_ok, type_note = try_match_direct_fitting_size_by_type(created_id, old_records)
    if type_ok:
        return True, type_note
    return False, (u"auto-size V5.8.3 fitting giảm không giải được instance profile đa cỡ; "
                   u"target radii {}; hiện tại {}; size-driver [{}]; thử {} tổ hợp; "
                   u"probe [{}]; double params [{}] | {}"
                   .format(safe_text(sorted(target_radii)), safe_text(actual),
                           u", ".join(candidate_names), trial_count, probe_text,
                           writable_double_parameter_diagnostics(current), type_note))


def _parameter_is_angle_like(param):
    """True only for parameters that are reasonably safe to trial as an angle."""
    try:
        definition = param.Definition
        try:
            dt = definition.GetDataType()
            try:
                if dt == DB.SpecTypeId.Angle:
                    return True
            except Exception:
                try:
                    if dt.Equals(DB.SpecTypeId.Angle):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        name = safe_text(definition.Name).strip().lower()
        tokens = [u'angle', u'góc', u'branch angle', u'elbow angle', u'junction angle',
                  u' tee angle', u'wye angle', u'roll angle']
        return any(t in name for t in tokens)
    except Exception:
        return False


def _element_axis_line_signature(elem):
    try:
        return _undirected_axis_signature([connector_axis(c) for c in physical_connectors(elem)])
    except Exception:
        return []


def try_match_direct_fitting_angle(created_id, old_records, links=None):
    """Best-effort match of fitting connector-axis geometry by angle parameters.

    This is deliberately conservative: only writable DOUBLE parameters that are
    Angle-spec or clearly angle-named are trialed, and every failed trial is
    rolled back. No type parameter is modified, so existing instances of the
    selected FamilySymbol are never changed globally.
    """
    created = doc.GetElement(created_id)
    if created is None:
        return False, u"fitting mới không tồn tại"

    if links is not None:
        target_axes = target_axes_for_old_ports(old_records, links)
    else:
        target_axes = [_safe_unit(r.get('axis')) for r in old_records]
    target_sig = _undirected_axis_signature(target_axes)
    current_sig = _element_axis_line_signature(created)
    current_err = _signature_max_error(current_sig, target_sig)
    if current_err <= 0.50:
        return True, (u"góc connector đã đúng: {}"
                      .format(_signature_text(current_sig)))

    # Candidate values come from the actual OLD connector centerlines, not from
    # parameter names. Use unique acute line angles (0..90 degrees).
    degrees_to_try = []
    for deg in target_sig:
        try:
            deg = float(deg)
        except Exception:
            continue
        if deg < 0.25:
            continue
        if all(abs(deg - x) > 0.25 for x in degrees_to_try):
            degrees_to_try.append(deg)
    # Typical fitting parameters may store the supplementary connector-arrow
    # angle, so test it too when distinct.
    for deg in list(degrees_to_try):
        sup = 180.0 - deg
        if 0.25 < sup < 179.75 and all(abs(sup - x) > 0.25 for x in degrees_to_try):
            degrees_to_try.append(sup)

    try:
        params = [p for p in created.Parameters
                  if (not p.IsReadOnly and p.StorageType == DB.StorageType.Double
                      and _parameter_is_angle_like(p))]
    except Exception:
        params = []

    best = None
    for seed in params:
        try:
            pname = safe_text(seed.Definition.Name)
        except Exception:
            continue
        for deg in degrees_to_try:
            st = DB.SubTransaction(doc)
            try:
                st.Start()
                elem = doc.GetElement(created_id)
                probe = None
                try:
                    for pp in elem.GetParameters(pname):
                        if (not pp.IsReadOnly and
                                pp.StorageType == DB.StorageType.Double and
                                _parameter_is_angle_like(pp)):
                            probe = pp
                            break
                except Exception:
                    pass
                if probe is None:
                    st.RollBack()
                    continue

                probe.Set(math.radians(float(deg)))
                doc.Regenerate()
                elem = doc.GetElement(created_id)
                sig = _element_axis_line_signature(elem)
                err = _signature_max_error(sig, target_sig)
                if best is None or err < best['error']:
                    best = {'name': pname, 'degree': deg, 'error': err, 'signature': sig}
                if err <= 0.50:
                    st.Commit()
                    return True, (u"set angle parameter '{}'={:.2f}°; new-line {}"
                                  .format(pname, deg, _signature_text(sig)))
                st.RollBack()
            except Exception:
                try:
                    st.RollBack()
                except Exception:
                    pass

    if best is not None:
        return False, (u"không khớp góc; current {} -> target {}; best '{}'={:.2f}° cho {} (error {:.2f}°)"
                       .format(_signature_text(current_sig), _signature_text(target_sig),
                               best['name'], best['degree'],
                               _signature_text(best['signature']), best['error']))
    return False, (u"không khớp góc; current {} -> target {}; không có writable instance Angle parameter để thử"
                   .format(_signature_text(current_sig), _signature_text(target_sig)))


def move_line_based_partner_endpoint(owner, old_endpoint, target_point):
    """Trim/extend only the endpoint formerly connected to the replaced fitting.

    The target must lie on the original MEPCurve centerline, so pipe direction,
    slope, diameter, type and the far endpoint remain unchanged.
    """
    if owner is None:
        raise Exception(u"Không tìm thấy owner của connector đối tác")

    try:
        location = owner.Location
        curve = location.Curve
    except Exception:
        raise Exception(u"Đối tượng ID {} không có LocationCurve để trim/extend"
                        .format(element_id_value(owner.Id)))

    try:
        p0 = copy_xyz(curve.GetEndPoint(0))
        p1 = copy_xyz(curve.GetEndPoint(1))
    except Exception:
        raise Exception(u"Đối tượng ID {} không có curve 2 đầu hợp lệ"
                        .format(element_id_value(owner.Id)))

    axis = _safe_unit(p1 - p0)
    if axis is None:
        raise Exception(u"Đối tượng ID {} có chiều dài bằng 0".format(element_id_value(owner.Id)))

    line_offset = point_to_axis_line_distance(target_point, p0, axis)
    if line_offset > CENTERLINE_TOLERANCE_FT:
        raise Exception(
            u"Không trim/extend pipe ID {} vì connector mới lệch centerline {:.6f} ft"
            .format(element_id_value(owner.Id), line_offset))

    d0 = xyz_distance(p0, old_endpoint)
    d1 = xyz_distance(p1, old_endpoint)
    if d0 <= d1:
        new_p0 = copy_xyz(target_point)
        new_p1 = p1
        old_near = p0
    else:
        new_p0 = p0
        new_p1 = copy_xyz(target_point)
        old_near = p1

    new_length = xyz_distance(new_p0, new_p1)
    if new_length < MIN_CURVE_LENGTH_FT:
        raise Exception(
            u"Trim/extend pipe ID {} sẽ tạo đoạn ống quá ngắn ({:.6f} ft)"
            .format(element_id_value(owner.Id), new_length))

    try:
        location.Curve = DB.Line.CreateBound(new_p0, new_p1)
        doc.Regenerate()
    except Exception as ex:
        raise Exception(u"Không chỉnh được đầu pipe ID {}: {}"
                        .format(element_id_value(owner.Id), safe_text(ex)))

    return xyz_distance(old_near, target_point), line_offset


def _reacquire_fitting_connector(created_id, expected_origin, old_record=None,
                                  expected_key=None, expected_axis=None):
    """Reacquire one fitting connector without depending on ConnectorSet order.

    V4.8 first matches the frozen connector identity captured immediately after
    the final alignment. If Revit invalidates/rebuilds that identity during
    Regenerate(), fall back to geometry + axis + domain/shape scoring.
    """
    elem = doc.GetElement(created_id)
    if elem is None:
        return None

    ports = physical_connectors(elem)

    # Best case: Connector.Id (or geometry fallback key) survived Regenerate().
    if expected_key is not None:
        for c in ports:
            try:
                if connector_identity_key(c) == expected_key:
                    return c
            except Exception:
                pass

    best = None
    best_score = 1.0e30
    ea = _safe_unit(expected_axis)

    for c in ports:
        try:
            score = xyz_distance(c.Origin, expected_origin)

            if old_record is not None:
                if old_record.get('domain') and connector_domain_name(c):
                    if old_record.get('domain') != connector_domain_name(c):
                        score += 1000000.0
                if old_record.get('shape') and connector_shape_name(c):
                    if old_record.get('shape') != connector_shape_name(c):
                        score += 100000.0
                if old_record.get('size') is not None:
                    try:
                        if not size_signatures_match(
                                connector_size_signature(c), old_record.get('size')):
                            score += 10000.0
                    except Exception:
                        pass

            # Axis is treated as an undirected line here. Its purpose is only
            # to distinguish the branch from the two run ports after a rebuild.
            ca = connector_axis(c)
            ca = _safe_unit(ca)
            if ea is not None and ca is not None:
                d = abs(max(-1.0, min(1.0, ea.DotProduct(ca))))
                score += (1.0 - d) * 1000.0

            if score < best_score:
                best_score = score
                best = c
        except Exception:
            pass
    return best


def _try_connect_pair_once(created_id, fitting_origin, old_record, partner_owner_id,
                           partner_origin, fitting_first=True,
                           fitting_key=None, fitting_axis=None):
    """Try one ConnectTo direction in a rollback-safe nested SubTransaction."""
    st = DB.SubTransaction(doc)
    err = u""
    try:
        st.Start()
        fc = _reacquire_fitting_connector(
            created_id, fitting_origin, old_record,
            expected_key=fitting_key, expected_axis=fitting_axis)
        owner = doc.GetElement(partner_owner_id)
        partner = find_connector_near(owner, partner_origin) if owner is not None else None
        if fc is None or partner is None:
            raise Exception(u"không reacquire được connector trước ConnectTo")

        if fitting_first:
            fc.ConnectTo(partner)
        else:
            partner.ConnectTo(fc)
        doc.Regenerate()

        # Reacquire BOTH connector handles after Regenerate. MEP connector
        # objects can become stale when topology changes.
        fc2 = _reacquire_fitting_connector(
            created_id, fitting_origin, old_record,
            expected_key=fitting_key, expected_axis=fitting_axis)
        owner2 = doc.GetElement(partner_owner_id)
        partner2 = find_connector_near(owner2, partner_origin) if owner2 is not None else None
        if fc2 is not None and partner2 is not None and connectors_are_connected(fc2, partner2):
            st.Commit()
            return True, u""

        st.RollBack()
        return False, u"ConnectTo không tạo pair vật lý"
    except Exception as ex:
        err = safe_text(ex)
        try:
            st.RollBack()
        except Exception:
            pass
        return False, err


def connect_axis_aligned_fitting(created, old_records, links, target_axes, assignment):
    """Reconnect fitting with side-locked mapping and fresh connector handles.

    V4.8 fixes two issues seen on a 45-degree Tee:
      1) the two run ports are no longer interchangeable (radial side locked);
      2) Connector handles are reacquired after every Regenerate and physical
         connection is checked with IsConnectedTo when available.
    ConnectTo is tried in both call directions inside rollback-safe trials.
    """
    created_id = created.Id
    new_ports = physical_connectors(created)
    if len(new_ports) != len(old_records):
        raise Exception(u"Số connector thay đổi trong lúc reconnect")

    # V4.8: use the stable connector snapshot captured by the exact
    # ConnectorSet that produced final_assignment. Never apply perm[] to this
    # freshly enumerated new_ports list because ConnectorSet order can change
    # after Regenerate(), causing a correct Tee mapping to jump to another port.
    mapped_origins = {}
    mapped_keys = {}
    mapped_axes = {}

    snap_origins = assignment.get('mapped_port_origins') or []
    snap_keys = assignment.get('mapped_port_keys') or []
    snap_axes = assignment.get('mapped_port_axes') or []

    if len(snap_origins) == len(old_records):
        for oi in range(len(old_records)):
            mapped_origins[oi] = copy_xyz(snap_origins[oi])
            mapped_keys[oi] = snap_keys[oi] if oi < len(snap_keys) else None
            mapped_axes[oi] = copy_xyz(snap_axes[oi]) if oi < len(snap_axes) else None
    else:
        # Compatibility fallback for an assignment generated by older code.
        for oi in range(len(old_records)):
            ni = assignment['perm'][oi]
            if ni < 0 or ni >= len(new_ports):
                raise Exception(u"Mapping connector bị trùng/không hợp lệ")
            mapped_origins[oi] = copy_xyz(new_ports[ni].Origin)
            mapped_keys[oi] = connector_identity_key(new_ports[ni])
            mapped_axes[oi] = copy_xyz(connector_axis(new_ports[ni]))

    if assignment.get('side_mismatches', 0) > 0:
        raise Exception(u"Mapping connector bị đảo phía junction ({} port)"
                        .format(assignment.get('side_mismatches', 0)))

    connected = 0
    adjusted = 0
    max_adjust = 0.0
    used_origins = []

    for oi, old in enumerate(old_records):
        link = find_link_for_old_port(old, links)
        if link is None:
            continue

        expected_fc_origin = mapped_origins[oi]
        # Prevent accidental reuse of the opposite run connector.
        for prior in used_origins:
            if xyz_distance(prior, expected_fc_origin) <= 1.0e-8:
                raise Exception(u"Mapping connector bị trùng origin")

        owner = doc.GetElement(link['partner_owner_id'])
        if owner is None:
            raise Exception(u"Mất đối tượng đối tác ID {}"
                            .format(element_id_value(link['partner_owner_id'])))

        partner = find_connector_near(owner, link['partner_origin'])
        expected_fc_key = mapped_keys.get(oi)
        expected_fc_axis = mapped_axes.get(oi)
        fc = _reacquire_fitting_connector(
            created_id, expected_fc_origin, old,
            expected_key=expected_fc_key, expected_axis=expected_fc_axis)
        if partner is None or fc is None:
            raise Exception(u"Không reacquire được connector fitting/pipe ID {}"
                            .format(element_id_value(owner.Id)))

        gap = xyz_distance(fc.Origin, partner.Origin)
        if gap > RECONNECT_TOLERANCE_FT:
            moved, line_offset = move_line_based_partner_endpoint(
                owner, copy_xyz(partner.Origin), copy_xyz(fc.Origin))
            adjusted += 1
            max_adjust = max(max_adjust, moved)

            owner = doc.GetElement(link['partner_owner_id'])
            partner = find_connector_near(owner, fc.Origin) if owner is not None else None
            expected_fc_key = mapped_keys.get(oi)
            expected_fc_axis = mapped_axes.get(oi)
            fc = _reacquire_fitting_connector(
                created_id, expected_fc_origin, old,
                expected_key=expected_fc_key, expected_axis=expected_fc_axis)
            if partner is None or fc is None:
                raise Exception(u"Sau trim/extend không reacquire được connector pipe ID {}"
                                .format(element_id_value(link['partner_owner_id'])))
            gap = xyz_distance(fc.Origin, partner.Origin)

        if gap > RECONNECT_TOLERANCE_FT:
            raise Exception(u"Connector vẫn hở {:.6f} ft sau trim/extend".format(gap))
        if not connector_sizes_match(fc, partner):
            raise Exception(u"Size connector fitting mới không khớp pipe ID {}"
                            .format(element_id_value(owner.Id)))

        dot_value = connector_dot(fc, partner)
        partner_target = copy_xyz(partner.Origin)
        fitting_target = copy_xyz(fc.Origin)

        if not connectors_are_connected(fc, partner):
            ok1, err1 = _try_connect_pair_once(
                created_id, fitting_target, old, link['partner_owner_id'],
                partner_target, fitting_first=True,
                fitting_key=expected_fc_key, fitting_axis=expected_fc_axis)
            if not ok1:
                ok2, err2 = _try_connect_pair_once(
                    created_id, fitting_target, old, link['partner_owner_id'],
                    partner_target, fitting_first=False,
                    fitting_key=expected_fc_key, fitting_axis=expected_fc_axis)
                if not ok2:
                    orientation = u"n/a"
                    try:
                        orientation = u"{:.3f}".format(float(dot_value))
                    except Exception:
                        pass
                    hint = u""
                    try:
                        if dot_value is not None and dot_value > -0.20:
                            hint = (u" | BasisZ hai connector không đối mặt (dot={}); "
                                    u"khả năng connector trong Family cần Flip Direction"
                                    .format(orientation))
                    except Exception:
                        pass
                    raise Exception(
                        u"ConnectTo không tạo quan hệ vật lý với pipe ID {} sau cả 2 chiều; "
                        u"fitting.ConnectTo: {}; pipe.ConnectTo: {}{}"
                        .format(element_id_value(owner.Id), err1, err2, hint))

        # Final check uses freshly acquired connector handles.
        fc_final = _reacquire_fitting_connector(
            created_id, fitting_target, old,
            expected_key=expected_fc_key, expected_axis=expected_fc_axis)
        owner_final = doc.GetElement(link['partner_owner_id'])
        partner_final = find_connector_near(owner_final, partner_target) if owner_final is not None else None
        if fc_final is None or partner_final is None or not connectors_are_connected(fc_final, partner_final):
            raise Exception(u"Sau ConnectTo không xác nhận được pair vật lý với pipe ID {}"
                            .format(element_id_value(link['partner_owner_id'])))

        link['partner_origin'] = copy_xyz(partner_final.Origin)
        link['fit_origin'] = copy_xyz(fc_final.Origin)
        used_origins.append(copy_xyz(fitting_target))
        connected += 1

    return connected, adjusted, max_adjust


def fitting_pivot_point(fitting, links=None):
    """Best pivot for 180-degree orientation trials without translating the fitting."""
    try:
        loc = fitting.Location
        if isinstance(loc, DB.LocationPoint):
            return copy_xyz(loc.Point)
    except Exception:
        pass
    try:
        tr = fitting.GetTransform()
        if tr and tr.Origin:
            return copy_xyz(tr.Origin)
    except Exception:
        pass

    pts = []
    if links:
        for link in links:
            try:
                pc = find_partner_connector(link)
                if pc is not None:
                    pts.append(copy_xyz(pc.Origin))
            except Exception:
                pass
    if not pts:
        for c in get_connectors(fitting):
            try:
                if not is_logical_connector(c):
                    pts.append(copy_xyz(c.Origin))
            except Exception:
                pass
    pts = [p for p in pts if p is not None]
    if not pts:
        return None
    return DB.XYZ(sum(p.X for p in pts) / len(pts),
                  sum(p.Y for p in pts) / len(pts),
                  sum(p.Z for p in pts) / len(pts))


def _axis_is_duplicate(axes, axis, tol=1.0e-6):
    if axis is None:
        return True
    try:
        a = axis.Normalize()
    except Exception:
        return True
    for existing in axes:
        try:
            # Parallel and anti-parallel axes define the same 180-degree rotation line.
            if abs(a.DotProduct(existing)) >= 1.0 - tol:
                return True
        except Exception:
            pass
    return False


def orientation_trial_axes(fitting, links):
    """Collect likely axes for a 180-degree correction of reversed fitting orientation."""
    axes = []

    def add(v):
        try:
            if v is None or v.GetLength() <= 1.0e-9:
                return
            vn = v.Normalize()
            if not _axis_is_duplicate(axes, vn):
                axes.append(vn)
        except Exception:
            pass

    # Global and instance axes.
    add(DB.XYZ.BasisX)
    add(DB.XYZ.BasisY)
    add(DB.XYZ.BasisZ)
    try:
        tr = fitting.GetTransform()
        add(tr.BasisX)
        add(tr.BasisY)
        add(tr.BasisZ)
    except Exception:
        pass

    partner_axes = []
    for link in links or []:
        try:
            pc = find_partner_connector(link)
            a = connector_axis(pc)
            if a is not None:
                partner_axes.append(a)
                add(a)
        except Exception:
            pass

    # Plane normals formed by connected runs/branches are useful for elbows/tees.
    for i in range(len(partner_axes)):
        for j in range(i + 1, len(partner_axes)):
            try:
                cr = partner_axes[i].CrossProduct(partner_axes[j])
                add(cr)
            except Exception:
                pass
    return axes


def best_fitting_partner_mapping(fitting, links):
    """Find the best connector-to-partner assignment using position, size and direction.

    Returns a dict with one fitting connector index for every link.  For a valid
    physical connection, origins must be coincident (within tolerance) and the
    connector BasisZ axes should be opposite, not parallel in the same direction.
    """
    fit_connectors = []
    for c in get_connectors(fitting):
        try:
            if not is_logical_connector(c):
                fit_connectors.append(c)
        except Exception:
            pass

    partners = []
    for link in links or []:
        pc = find_partner_connector(link)
        if pc is None:
            return None
        partners.append(pc)

    n = len(partners)
    if n == 0:
        return {
            'assignment': [], 'fit_connectors': fit_connectors,
            'partners': partners, 'score': 0.0, 'max_gap': 0.0,
            'worst_dot': -1.0, 'direction_known': False,
            'size_ok': True, 'compatible': True
        }
    if len(fit_connectors) < n:
        return None

    best = None
    for perm in itertools.permutations(range(len(fit_connectors)), n):
        score = 0.0
        max_gap = 0.0
        worst_dot = -1.0
        direction_known = False
        size_ok = True
        compatible = True

        for li, fi in enumerate(perm):
            fc = fit_connectors[fi]
            pc = partners[li]
            try:
                if connector_domain_name(fc) and connector_domain_name(pc):
                    if connector_domain_name(fc) != connector_domain_name(pc):
                        compatible = False
                        score += 1000000.0
                if connector_shape_name(fc) and connector_shape_name(pc):
                    if connector_shape_name(fc) != connector_shape_name(pc):
                        compatible = False
                        score += 100000.0
            except Exception:
                pass

            gap = xyz_distance(fc.Origin, pc.Origin)
            if gap > max_gap:
                max_gap = gap
            # Position dominates; a 1/16-in miss is already significant.
            score += gap / max(RECONNECT_TOLERANCE_FT, 1.0e-9)

            if not connector_sizes_match(fc, pc):
                size_ok = False
                score += 1000.0

            d = connector_dot(fc, pc)
            if d is not None:
                direction_known = True
                if d > worst_dot:
                    worst_dot = d
                # Ideal is -1.  Same direction (+1) gets a strong penalty.
                score += (d + 1.0) * 10.0

        result = {
            'assignment': list(perm),
            'fit_connectors': fit_connectors,
            'partners': partners,
            'score': score,
            'max_gap': max_gap,
            'worst_dot': worst_dot,
            'direction_known': direction_known,
            'size_ok': size_ok,
            'compatible': compatible,
        }
        if best is None or result['score'] < best['score']:
            best = result
    return best


def mapping_is_connectable(mapping):
    if mapping is None:
        return False
    if not mapping.get('compatible', False) or not mapping.get('size_ok', False):
        return False
    if mapping.get('max_gap', 1.0e30) > RECONNECT_TOLERANCE_FT:
        return False
    if mapping.get('direction_known', False):
        if mapping.get('worst_dot', 1.0) > CONNECTOR_OPPOSITE_DOT_LIMIT:
            return False
    return True


def mapping_description(mapping):
    if mapping is None:
        return u"không tạo được mapping connector"
    dot_text = u"n/a"
    if mapping.get('direction_known', False):
        dot_text = u"{:.3f}".format(mapping.get('worst_dot', 0.0))
    return (u"gap max {:.5f} ft | dot xấu nhất {} | size {} | domain/shape {}"
            .format(mapping.get('max_gap', 0.0), dot_text,
                    u"OK" if mapping.get('size_ok', False) else u"SAI",
                    u"OK" if mapping.get('compatible', False) else u"SAI"))


def auto_orient_fitting_180(fitting, links):
    """Try safe 180-degree orientation changes and keep only a fully connectable result.

    This fixes the common case where the new fitting instance is placed reversed
    relative to the existing pipes.  Every trial is isolated in a SubTransaction;
    if connector origins/sizes/directions do not all match, it is rolled back.
    """
    initial = best_fitting_partner_mapping(fitting, links)
    if mapping_is_connectable(initial):
        return True, u"orientation hiện tại đã đúng ({})".format(mapping_description(initial))

    pivot = fitting_pivot_point(fitting, links)
    if pivot is None:
        return False, u"không xác định được tâm xoay fitting; {}".format(mapping_description(initial))

    attempts = []

    # Native family 180 rotate can preserve authored constraints better than a raw transform.
    try:
        if isinstance(fitting, DB.FamilyInstance) and fitting.CanRotate:
            attempts.append((u"FamilyInstance.rotate() 180°", 'family_rotate', None))
    except Exception:
        pass

    try:
        if isinstance(fitting, DB.FamilyInstance) and fitting.CanFlipFacing:
            attempts.append((u"flipFacing", 'flip_facing', None))
    except Exception:
        pass
    try:
        if isinstance(fitting, DB.FamilyInstance) and fitting.CanFlipHand:
            attempts.append((u"flipHand", 'flip_hand', None))
    except Exception:
        pass
    try:
        if (isinstance(fitting, DB.FamilyInstance) and fitting.CanFlipFacing and
                fitting.CanFlipHand):
            attempts.append((u"flipFacing + flipHand", 'flip_both', None))
    except Exception:
        pass

    for axis in orientation_trial_axes(fitting, links):
        attempts.append((u"RotateElement 180°", 'axis180', axis))

    best_seen = initial
    for name, kind, axis in attempts:
        trial = DB.SubTransaction(doc)
        try:
            trial.Start()
            changed = False
            if kind == 'family_rotate':
                changed = bool(fitting.rotate())
            elif kind == 'flip_facing':
                changed = bool(fitting.flipFacing())
            elif kind == 'flip_hand':
                changed = bool(fitting.flipHand())
            elif kind == 'flip_both':
                changed = bool(fitting.flipFacing())
                changed = bool(fitting.flipHand()) or changed
            elif kind == 'axis180':
                line = DB.Line.CreateBound(pivot, pivot + axis)
                DB.ElementTransformUtils.RotateElement(doc, fitting.Id, line, math.pi)
                changed = True

            if not changed:
                trial.RollBack()
                continue

            doc.Regenerate()
            current = best_fitting_partner_mapping(fitting, links)
            if best_seen is None or (current is not None and current['score'] < best_seen['score']):
                best_seen = current

            if mapping_is_connectable(current):
                trial.Commit()
                return True, u"đã tự chỉnh '{}' ({})".format(name, mapping_description(current))

            trial.RollBack()
        except Exception:
            try:
                trial.RollBack()
            except Exception:
                pass

    return False, (u"không có phép xoay/flip 180° nào làm tất cả connector cùng đúng; {}. "
                   u"Khả năng cao hướng connector được khai báo ngược ngay trong Family."
                   .format(mapping_description(best_seen)))


def most_opposite_pair(connectors):
    """Return indices of the connector pair whose axes are most opposite."""
    best = None
    best_dot = 2.0
    n = len(connectors)
    for i in range(n):
        for j in range(i + 1, n):
            d = connector_dot(connectors[i], connectors[j])
            if d is None:
                continue
            if d < best_dot:
                best_dot = d
                best = (i, j)
    if best is None and n >= 2:
        best = (0, 1)
    return best


def order_tee_connectors(connectors):
    """NewTeeFitting expects first two connectors on the run and third on branch."""
    if len(connectors) != 3:
        return connectors
    pair = most_opposite_pair(connectors)
    if pair is None:
        return connectors
    i, j = pair
    k = [x for x in range(3) if x not in pair][0]
    return [connectors[i], connectors[j], connectors[k]]


def order_cross_connectors(connectors):
    """Group the four connectors as two opposite run pairs for NewCrossFitting."""
    if len(connectors) != 4:
        return connectors
    pair = most_opposite_pair(connectors)
    if pair is None:
        return connectors
    i, j = pair
    rest = [x for x in range(4) if x not in pair]
    return [connectors[i], connectors[j], connectors[rest[0]], connectors[rest[1]]]

def collect_pipes_and_fittings(selected_only=False, selected_ids=None):
    """Collect Pipe and Pipe Fitting.

    When selected_only=True, use the explicit preselection snapshot captured at
    command start. The modal UI never re-reads UIDocument.Selection, so the batch
    scope cannot change while the user edits settings.
    """
    if selected_only:
        pipes, fittings = [], []
        selected_ids = list(selected_ids or [])
        for raw_id in selected_ids:
            try:
                eid = raw_id if isinstance(raw_id, DB.ElementId) else make_element_id(raw_id)
                elem = doc.GetElement(eid)
                if elem is None:
                    continue
                if is_pipe(elem):
                    pipes.append(elem)
                elif is_pipe_fitting(elem):
                    fittings.append(elem)
            except Exception:
                pass
        return pipes, fittings

    pipes = list(DB.FilteredElementCollector(doc)\
                 .OfClass(DB.Plumbing.Pipe)\
                 .WhereElementIsNotElementType()\
                 .ToElements())
    fittings = list(DB.FilteredElementCollector(doc)\
                    .OfCategory(DB.BuiltInCategory.OST_PipeFitting)\
                    .WhereElementIsNotElementType()\
                    .ToElements())
    return pipes, fittings


class CmbItem(object):
    def __init__(self, name, value):
        self.Name = name
        self.Value = value

    def __repr__(self):
        return self.Name


class SkipWarningsPreprocessor(DB.IFailuresPreprocessor):
    """Delete warnings so one warning does not stop the whole batch."""
    def PreprocessFailures(self, failuresAccessor):
        try:
            failures = failuresAccessor.GetFailureMessages()
            for failure in failures:
                if failure.GetSeverity() == DB.FailureSeverity.Warning:
                    failuresAccessor.DeleteWarning(failure)
            return DB.FailureProcessingResult.Continue
        except Exception:
            return DB.FailureProcessingResult.Continue


# ============================================================
# Main window - modal, preselection snapshot (V5.8 Universal / Revit 2025-2026)
# ============================================================


class ReplaceElementsWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name, preselected_ids):
        forms.WPFWindow.__init__(self, xaml_file_name)
        try:
            self.Title = u"Correct Pipes & Fittings V5.8.3 Universal - Revit {}".format(REVIT_VERSION if REVIT_VERSION else u"?")
        except Exception:
            pass
        # V4.9: size the dialog against the actual Windows work area.  The XAML
        # also keeps the settings area inside a ScrollViewer, so on laptops or
        # high-DPI displays the Run button can never be pushed below the screen.
        self._apply_default_window_size()
        self.pipe_type_items = []
        self.fitting_type_items = []
        self.selected_fitting_type_ids = set()
        # V4.8: separate user-designated symbols for fittings whose physical
        # connector sizes are not equal (reducing Tee/Elbow/etc.). These may be
        # outside the selected PipeType Routing Preferences because direct-place
        # recreation can use the exact loaded FamilySymbol.
        self.selected_reducing_fitting_type_ids = set()
        self._refreshing_fitting_list = False
        self._refreshing_reducing_fitting_list = False
        self._loading_choices = False
        self.system_items = []
        self.routing_fitting_type_ids = None
        self.routing_ids_by_pipe_type_id = {}
        self.pipe_size_items = []
        self._updating_size_range = False
        # V5.8: exactly one fitting-source mode is active at a time.
        # 'auto'   = resolve FamilySymbol from selected PipeType Routing Preferences.
        # 'manual' = user selects at most one FamilySymbol per PartType, like V5.3.
        self.fitting_selection_mode = 'auto'
        self._updating_fitting_mode = False

        # V4.8: freeze the Revit selection at command start. The modal UI never
        # reads UIDocument.Selection again, so accidental selection changes cannot
        # silently change the batch scope.
        self.preselected_element_ids = []
        seen = set()
        for raw_id in list(preselected_ids or []):
            try:
                value = self.id_value(raw_id) if isinstance(raw_id, DB.ElementId) else int(raw_id)
                if value is not None and value not in seen:
                    seen.add(value)
                    self.preselected_element_ids.append(value)
            except Exception:
                pass

        self._preselected_pipe_count = 0
        self._preselected_fitting_count = 0
        for value in self.preselected_element_ids:
            try:
                elem = doc.GetElement(make_element_id(value))
                if is_pipe(elem):
                    self._preselected_pipe_count += 1
                elif is_pipe_fitting(elem):
                    self._preselected_fitting_count += 1
            except Exception:
                pass

        self.setup_data()
        self.load_last_choices()
        self._prefer_unique_preselected_system()
        self._update_preselection_status()

    def _apply_default_window_size(self):
        """Fit the modal window to the current Windows work area without hiding actions."""
        try:
            wa = System.Windows.SystemParameters.WorkArea
            work_w = float(wa.Width)
            work_h = float(wa.Height)

            # Prefer a roomy desktop size, but never exceed the usable monitor.
            desired_w = min(1220.0, work_w * 0.94)
            desired_h = min(940.0, work_h * 0.94)
            self.Width = max(760.0, desired_w) if work_w >= 780.0 else work_w * 0.98
            self.Height = max(580.0, desired_h) if work_h >= 600.0 else work_h * 0.98
            self.Width = min(self.Width, work_w * 0.98)
            self.Height = min(self.Height, work_h * 0.98)

            # Do not let MinWidth/MinHeight force the dialog outside a small or
            # high-DPI work area.  The ScrollViewer handles reduced dimensions.
            self.MinWidth = min(900.0, self.Width)
            self.MinHeight = min(620.0, self.Height)
            self.Left = float(wa.Left) + max(0.0, (work_w - self.Width) * 0.5)
            self.Top = float(wa.Top) + max(0.0, (work_h - self.Height) * 0.5)
        except Exception:
            # XAML dimensions remain as a safe fallback.
            pass

    def _is_manual_fitting_mode(self):
        return safe_text(getattr(self, 'fitting_selection_mode', 'auto')).lower() == 'manual'

    def _set_fitting_selection_mode(self, mode, log_change=False):
        """Enforce exactly one of the two fitting-source checkboxes."""
        mode = 'manual' if safe_text(mode).lower() == 'manual' else 'auto'
        self.fitting_selection_mode = mode
        try:
            self._updating_fitting_mode = True
            self.chk_ModeAutoRouting.IsChecked = (mode == 'auto')
            self.chk_ModeManualFittings.IsChecked = (mode == 'manual')
        except Exception:
            pass
        finally:
            self._updating_fitting_mode = False
        self._update_fitting_mode_ui(log_change)

    def _update_fitting_mode_ui(self, log_change=False):
        manual = self._is_manual_fitting_mode()
        try:
            self.panel_AutoRouting.Visibility = (System.Windows.Visibility.Collapsed
                                                 if manual else System.Windows.Visibility.Visible)
        except Exception:
            pass
        try:
            self.panel_ManualFittings.Visibility = (System.Windows.Visibility.Visible
                                                    if manual else System.Windows.Visibility.Collapsed)
        except Exception:
            pass
        try:
            self.txt_FittingModeStatus.Text = (
                u"CHẾ ĐỘ 2 — Chọn Fitting thủ công: mỗi PartType tối đa 1 type; fitting giảm dùng danh sách riêng."
                if manual else
                u"CHẾ ĐỘ 1 — Auto Routing: FamilySymbol được lấy tự động từ Routing Preferences của Pipe Type mới.")
        except Exception:
            pass
        try:
            self.btn_Replace.Content = (u"Thực hiện — Fitting thủ công + Size Range"
                                        if manual else
                                        u"Thực hiện — Auto Routing + Size Range")
        except Exception:
            pass
        # Refresh the manual lists whenever they become visible. Routing symbols
        # remain sorted first for convenience, but manual selection can use any
        # loaded Pipe Fitting FamilySymbol of the same PartType.
        if manual:
            try:
                self.refresh_fitting_list(self.txt_FilterFittings.Text)
                self.refresh_reducing_fitting_list(self.txt_FilterReducingFittings.Text)
            except Exception:
                pass
        if log_change:
            self.log(u"Chế độ Fitting V5.8.3: {}."
                     .format(u"CHỌN THỦ CÔNG như V5.3" if manual else u"AUTO ROUTING PREFERENCES"))

    def fitting_mode_click(self, sender, args):
        """Checkbox UI with radio-button semantics: exactly one option stays ticked."""
        if self._updating_fitting_mode or self._loading_choices:
            return
        try:
            sender_name = safe_text(getattr(sender, 'Name', u''))
            if sender_name == 'chk_ModeManualFittings':
                self._set_fitting_selection_mode('manual', True)
            else:
                self._set_fitting_selection_mode('auto', True)
        except Exception as ex:
            self.log(u"Không thể đổi chế độ Fitting: {}".format(safe_text(ex)))

    def _prefer_unique_preselected_system(self):
        """If the preselection has exactly one System Type, select it automatically."""
        system_values = set()
        for value in self.preselected_element_ids:
            try:
                elem = doc.GetElement(make_element_id(value))
                sid = get_system_type_id(elem)
                sid_value = self.id_value(sid)
                if sid_value is not None and sid != DB.ElementId.InvalidElementId:
                    system_values.add(sid_value)
            except Exception:
                pass
        if len(system_values) == 1:
            wanted = list(system_values)[0]
            if self._select_combo_by_id(self.cmb_Systems, wanted):
                try:
                    self.log(u"Tự chọn System Type theo preselection: {}".format(
                        self.cmb_Systems.SelectedItem.Name))
                except Exception:
                    pass
        elif len(system_values) > 1:
            self.log(u"Preselection có {} System Type; hãy kiểm tra combo System Type trước khi chạy.".format(
                len(system_values)))

    def _update_preselection_status(self):
        try:
            total = len(self.preselected_element_ids)
            self.txt_PreselectionStatus.Text = (
                u"Đã nhận {} đối tượng trước khi mở tool: {} Pipe + {} Fitting. "
                u"Phạm vi này được khóa cho lần chạy hiện tại.".format(
                    total, self._preselected_pipe_count, self._preselected_fitting_count))
            self.txt_PreselectionStatus.Foreground = System.Windows.Media.Brushes.DarkGreen
        except Exception:
            pass

    def log(self, msg):
        try:
            self.txt_Log.AppendText(safe_text(msg) + "\n")
            self.txt_Log.ScrollToEnd()
        except Exception:
            pass

    def id_value(self, eid):
        return element_id_value(eid)

    def _config_get(self, config, name, default=None):
        try:
            return getattr(config, name)
        except Exception:
            return default

    def _select_combo_by_id(self, combo, saved_id):
        try:
            wanted = int(saved_id)
        except Exception:
            return False
        for index in range(combo.Items.Count):
            try:
                if self.id_value(combo.Items[index].Value) == wanted:
                    combo.SelectedIndex = index
                    return True
            except Exception:
                pass
        return False

    def _select_combo_by_float_value(self, combo, saved_value, tolerance=1.0e-7):
        try:
            wanted = float(saved_value)
        except Exception:
            return False
        best_index = -1
        best_delta = None
        for index in range(combo.Items.Count):
            try:
                delta = abs(float(combo.Items[index].Value) - wanted)
                if delta <= tolerance:
                    combo.SelectedIndex = index
                    return True
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_index = index
            except Exception:
                pass
        # Do NOT silently snap to another model size. Saved range may belong to
        # another model; fall back to full current-model range instead.
        return False

    def _current_pipe_size_range(self):
        min_item = self.cmb_MinPipeSize.SelectedItem
        max_item = self.cmb_MaxPipeSize.SelectedItem
        if min_item is None or max_item is None:
            return None, None
        try:
            return float(min_item.Value), float(max_item.Value)
        except Exception:
            return None, None

    def _update_pipe_size_range_status(self):
        try:
            min_size, max_size = self._current_pipe_size_range()
            if min_size is None or max_size is None:
                self.txt_PipeSizeRangeStatus.Text = u"Model chưa có Pipe size hợp lệ để tạo range."
                self.txt_PipeSizeRangeStatus.Foreground = System.Windows.Media.Brushes.DarkRed
                return
            if min_size > max_size:
                self.txt_PipeSizeRangeStatus.Text = u"Range không hợp lệ: Min lớn hơn Max."
                self.txt_PipeSizeRangeStatus.Foreground = System.Windows.Media.Brushes.DarkRed
                return
            self.txt_PipeSizeRangeStatus.Text = (
                u"Sẽ xử lý Pipe từ {} đến {} (bao gồm hai đầu range). "
                u"Fitting chỉ được xử lý khi TẤT CẢ port size của fitting đều nằm trong range. "
                u"Danh sách size được lấy từ Pipe đang tồn tại trong model."
                .format(format_pipe_size(min_size), format_pipe_size(max_size)))
            self.txt_PipeSizeRangeStatus.Foreground = System.Windows.Media.Brushes.DarkGreen
        except Exception:
            pass

    def min_pipe_size_selection_changed(self, sender, args):
        if self._loading_choices or self._updating_size_range:
            return
        try:
            min_size, max_size = self._current_pipe_size_range()
            if min_size is not None and max_size is not None and min_size > max_size:
                self._updating_size_range = True
                # Traditional range behaviour: moving Min above Max also moves
                # Max to the same existing model size.
                self.cmb_MaxPipeSize.SelectedIndex = self.cmb_MinPipeSize.SelectedIndex
                self._updating_size_range = False
            self._update_pipe_size_range_status()
        except Exception:
            self._updating_size_range = False

    def max_pipe_size_selection_changed(self, sender, args):
        if self._loading_choices or self._updating_size_range:
            return
        try:
            min_size, max_size = self._current_pipe_size_range()
            if min_size is not None and max_size is not None and max_size < min_size:
                self._updating_size_range = True
                # Moving Max below Min also moves Min to that size.
                self.cmb_MinPipeSize.SelectedIndex = self.cmb_MaxPipeSize.SelectedIndex
                self._updating_size_range = False
            self._update_pipe_size_range_status()
        except Exception:
            self._updating_size_range = False

    def get_routing_fitting_type_ids(self, pipe_type):
        """Return fitting symbol IDs referenced by the selected PipeType routing preferences."""
        if not pipe_type:
            return None
        result = set()
        try:
            manager = pipe_type.RoutingPreferenceManager
        except Exception:
            return None

        group_names = [
            'Elbows', 'Junctions', 'Crosses', 'Transitions',
            'Unions', 'MechanicalJoints', 'Caps'
        ]
        for group_name in group_names:
            try:
                group = getattr(DB.RoutingPreferenceRuleGroupType, group_name)
                count = manager.GetNumberOfRules(group)
            except Exception:
                continue
            for index in range(count):
                try:
                    rule = manager.GetRule(group, index)
                    part_id = rule.MEPPartId
                    part = doc.GetElement(part_id)
                    if part is None:
                        continue

                    # Usually MEPPartId is a FamilySymbol. Keep a family fallback
                    # for API/content variations where a Family is returned.
                    if isinstance(part, DB.FamilySymbol):
                        result.add(self.id_value(part.Id))
                    elif isinstance(part, DB.Family):
                        for symbol_id in part.GetFamilySymbolIds():
                            result.add(self.id_value(symbol_id))
                    else:
                        try:
                            if category_is(part, DB.BuiltInCategory.OST_PipeFitting):
                                result.add(self.id_value(part.Id))
                        except Exception:
                            pass
                except Exception:
                    pass
        result.discard(None)
        return result

    def update_fittings_for_selected_pipe_type(self, clear_invalid=True):
        """V5.8: always read Routing Preferences, but preserve manual choices.

        Auto mode uses the routing rules directly at runtime. Manual mode keeps
        the user's one-type-per-PartType selections intact when Pipe Type changes.
        """
        selected = self.cmb_PipeTypes.SelectedItem
        key = self.id_value(selected.Value) if selected else None
        cached = self.routing_ids_by_pipe_type_id.get(key, None)
        self.routing_fitting_type_ids = set(cached) if cached is not None else set()

        pipe_type = doc.GetElement(selected.Value) if selected else None
        try:
            self.txt_RoutingSummary.Text = routing_preferences_summary(pipe_type)
        except Exception as ex:
            try:
                self.txt_RoutingSummary.Text = u"Không thể hiển thị Routing Preferences: {}".format(safe_text(ex))
            except Exception:
                pass

        # Manual selections are intentionally NOT overwritten with routing IDs.
        # Refresh only affects presentation/order and preserves selected ID sets.
        try:
            self.refresh_fitting_list(self.txt_FilterFittings.Text)
            self.refresh_reducing_fitting_list(self.txt_FilterReducingFittings.Text)
        except Exception:
            pass

        count = len(self.routing_fitting_type_ids)
        if selected:
            if self._is_manual_fitting_mode():
                self.log(u"Pipe Type '{}': đọc {} fitting type từ Routing Preferences để tham khảo; chế độ hiện tại dùng Fitting chọn thủ công."
                         .format(selected.Name, count))
            else:
                self.log(u"Pipe Type '{}': tự động đọc {} fitting type từ Routing Preferences; không cần chọn Fitting thủ công."
                         .format(selected.Name, count))

    def pipe_type_selection_changed(self, sender, args):
        if self._loading_choices:
            return
        try:
            self.update_fittings_for_selected_pipe_type(clear_invalid=True)
        except Exception as ex:
            self.log(u"Không thể đọc Routing Preferences của Pipe Type: {}".format(safe_text(ex)))

    def load_last_choices(self):
        try:
            self._loading_choices = True
            config = script.get_config()
            self._select_combo_by_id(self.cmb_Systems, self._config_get(config, 'last_system_type_id'))
            self._select_combo_by_id(self.cmb_PipeTypes, self._config_get(config, 'last_pipe_type_id'))

            # Preserve V5.3-style manual fitting choices even when Auto mode is active.
            self.selected_fitting_type_ids.clear()
            for value in safe_text(self._config_get(config, 'last_fitting_type_ids', u'')).split(','):
                try:
                    self.selected_fitting_type_ids.add(int(value.strip()))
                except Exception:
                    pass
            self.selected_reducing_fitting_type_ids.clear()
            for value in safe_text(self._config_get(config, 'last_reducing_fitting_type_ids', u'')).split(','):
                try:
                    self.selected_reducing_fitting_type_ids.add(int(value.strip()))
                except Exception:
                    pass
            try:
                self._normalize_selected_ids(self.selected_fitting_type_ids, NORMAL_FITTING_ROLES, require_routing=False)
                self._normalize_selected_ids(self.selected_reducing_fitting_type_ids, REDUCING_FITTING_ROLES, require_routing=False)
            except Exception:
                pass

            self.chk_ReplacePipes.IsChecked = bool(self._config_get(config, 'replace_pipes', True))
            self.chk_ReplaceFittings.IsChecked = bool(self._config_get(config, 'replace_fittings', True))
            try:
                self.chk_ForceReconnectFittings.IsChecked = bool(
                    self._config_get(config, 'force_reconnect_fittings', True))
            except Exception:
                pass

            # Restore manual-list filters.
            try:
                self.txt_FilterFittings.Text = safe_text(self._config_get(config, 'fitting_filter_text', u''))
                self.txt_FilterReducingFittings.Text = safe_text(
                    self._config_get(config, 'reducing_fitting_filter_text', u''))
            except Exception:
                pass

            # Restore Min/Max only when those exact sizes still exist in model.
            try:
                saved_min = self._config_get(config, 'min_pipe_size_ft', None)
                saved_max = self._config_get(config, 'max_pipe_size_ft', None)
                if saved_min is not None:
                    self._select_combo_by_float_value(self.cmb_MinPipeSize, saved_min)
                if saved_max is not None:
                    self._select_combo_by_float_value(self.cmb_MaxPipeSize, saved_max)
            except Exception:
                pass

            saved_mode = safe_text(self._config_get(config, 'fitting_selection_mode', u'auto')).lower()
            self._loading_choices = False
            self._set_fitting_selection_mode('manual' if saved_mode == 'manual' else 'auto', False)
            self._update_pipe_size_range_status()
            self.update_fittings_for_selected_pipe_type(clear_invalid=False)
            self.log(u"Đã khôi phục System/Pipe Type, Size Range và chế độ Fitting V5.8.3.")
        except Exception as ex:
            self._loading_choices = False
            try:
                self._set_fitting_selection_mode('auto', False)
                self.update_fittings_for_selected_pipe_type(clear_invalid=False)
            except Exception:
                pass
            self.log(u"Không thể khôi phục lựa chọn trước: {}".format(safe_text(ex)))

    def save_last_choices(self, selected_system, selected_pipe_type, selected_fitting_items,
                          selected_reducing_fitting_items, overwrite_pipes, overwrite_fittings,
                          selected_only, force_reconnect_fittings):
        try:
            config = script.get_config()
            config.last_system_type_id = self.id_value(selected_system.Value)
            config.last_pipe_type_id = self.id_value(selected_pipe_type.Value) if selected_pipe_type else -1
            # Always persist the manual choices, even if this run uses Auto Routing,
            # so switching back to manual mode restores the user's V5.3-style setup.
            config.last_fitting_type_ids = u','.join([
                safe_text(self.id_value(item.Value)) for item in list(selected_fitting_items or [])
            ])
            config.last_reducing_fitting_type_ids = u','.join([
                safe_text(self.id_value(item.Value)) for item in list(selected_reducing_fitting_items or [])
            ])
            config.fitting_selection_mode = safe_text(self.fitting_selection_mode)
            config.replace_pipes = bool(overwrite_pipes)
            config.replace_fittings = bool(overwrite_fittings)
            config.selected_only = bool(selected_only)
            config.force_reconnect_fittings = bool(force_reconnect_fittings)
            try:
                config.fitting_filter_text = safe_text(self.txt_FilterFittings.Text)
                config.reducing_fitting_filter_text = safe_text(self.txt_FilterReducingFittings.Text)
            except Exception:
                pass
            try:
                min_size, max_size = self._current_pipe_size_range()
                if min_size is not None:
                    config.min_pipe_size_ft = float(min_size)
                if max_size is not None:
                    config.max_pipe_size_ft = float(max_size)
            except Exception:
                pass
            script.save_config()
        except Exception as ex:
            self.log(u"Không thể lưu lựa chọn: {}".format(safe_text(ex)))

    def _rebuild_fitting_catalog(self, reason=None, log_stats=True):
        """V5.8 re-scan all currently LOADED Pipe Fitting FamilySymbols.

        This is used both at startup and after the user loads supplemental .RFA
        files.  The manual selection lists are rebuilt immediately without
        closing the modal UI.
        """
        before_ids = set()
        before_names = {}
        for item in list(self.fitting_type_items or []):
            try:
                iid = self.id_value(item.Value)
                if iid is not None:
                    before_ids.add(iid)
                    before_names[iid] = safe_text(item.Name)
            except Exception:
                pass

        try:
            _pipes, fittings = collect_pipes_and_fittings()
        except Exception:
            fittings = []

        all_routing_symbol_ids = set()
        for _ids in self.routing_ids_by_pipe_type_id.values():
            try:
                all_routing_symbol_ids.update(list(_ids or []))
            except Exception:
                pass

        fitting_symbols, fitting_scan_stats = collect_all_pipe_fitting_symbols(
            instance_fittings=fittings,
            routing_symbol_ids=all_routing_symbol_ids)

        discovered_roles = []
        for fs in fitting_symbols:
            try:
                discovered_roles.append(
                    fitting_role_from_part_type_value(get_part_type(fs)))
            except Exception:
                discovered_roles.append(u"Undefined")

        global NORMAL_FITTING_ROLES, REDUCING_FITTING_ROLES
        NORMAL_FITTING_ROLES = sort_pipe_fitting_roles(discovered_roles)
        REDUCING_FITTING_ROLES = tuple(NORMAL_FITTING_ROLES)

        def fitting_sort_key(fs):
            return (fitting_role_display_name(
                        fitting_role_from_part_type_value(get_part_type(fs))).lower(),
                    get_real_family_name(fs).lower(),
                    get_real_name(fs).lower())

        new_items = []
        after_ids = set()
        for fs in sorted(fitting_symbols, key=fitting_sort_key):
            try:
                part_type = get_part_type(fs)
                role = fitting_role_from_part_type_value(part_type)
                role_text = fitting_role_display_name(role)
                label = u"[{}] {} : {}".format(
                    role_text or u"Undefined",
                    get_real_family_name(fs),
                    get_real_name(fs))
                item = CmbItem(label, fs.Id)
                item.PartType = part_type
                item.Role = role
                new_items.append(item)
                iid = self.id_value(fs.Id)
                if iid is not None:
                    after_ids.add(iid)
            except Exception:
                pass

        self.fitting_type_items = new_items

        # IDs can change if a Family was reloaded. Keep only selections that are
        # still real symbols; never silently transfer a choice to a different type.
        self.selected_fitting_type_ids.intersection_update(after_ids)
        self.selected_reducing_fitting_type_ids.intersection_update(after_ids)

        try:
            self.lst_FittingTypes.DisplayMemberPath = "Name"
            self.lst_ReducingFittingTypes.DisplayMemberPath = "Name"
        except Exception:
            pass

        self.refresh_fitting_list(
            safe_text(getattr(self.txt_FilterFittings, 'Text', u'')))
        self.refresh_reducing_fitting_list(
            safe_text(getattr(self.txt_FilterReducingFittings, 'Text', u'')))

        added_ids = after_ids - before_ids
        removed_ids = before_ids - after_ids
        missing_pt = len([x for x in self.fitting_type_items
                          if getattr(x, 'PartType', None) is None])

        if log_stats:
            if reason:
                self.log(u"--- V5.8.3 FULL FITTING SCAN: {} ---".format(safe_text(reason)))
            self.log(
                u"V5.8.3 Full Fitting Scan: direct-category {} | symbol-category {} | "
                u"family-category {} | family-enumeration {} | all-element-types {} | "
                u"instance-type {} | routing-reference {} | UNIQUE {}."
                .format(fitting_scan_stats.get('direct_category', 0),
                        fitting_scan_stats.get('symbol_category', 0),
                        fitting_scan_stats.get('family_category', 0),
                        fitting_scan_stats.get('family_enumeration', 0),
                        fitting_scan_stats.get('all_element_types', 0),
                        fitting_scan_stats.get('instance_type', 0),
                        fitting_scan_stats.get('routing_reference', 0),
                        fitting_scan_stats.get('unique_total', len(self.fitting_type_items))))
            if added_ids:
                added_labels = []
                for item in self.fitting_type_items:
                    try:
                        if self.id_value(item.Value) in added_ids:
                            added_labels.append(safe_text(item.Name))
                    except Exception:
                        pass
                self.log(u"Mới phát hiện {} fitting type:".format(len(added_labels)))
                for label in added_labels[:50]:
                    self.log(u"  + {}".format(label))
                if len(added_labels) > 50:
                    self.log(u"  + ... {} type khác".format(len(added_labels) - 50))
            if missing_pt:
                self.log(
                    u"CẢNH BÁO: {} fitting type không đọc được FAMILY_CONTENT_PART_TYPE; "
                    u"vẫn hiển thị dưới nhóm Undefined.".format(missing_pt))

        return fitting_scan_stats, added_ids, removed_ids

    def setup_data(self):
        self.log(u"Đang đọc dữ liệu trong model...")
        self.log(u"V5.8.3 Universal | Revit {} | ElementId 64-bit Value mode | Transition = Native ChangeTypeId.".format(
            REVIT_VERSION if REVIT_VERSION else u"không xác định"))
        if REVIT_VERSION and REVIT_VERSION not in SUPPORTED_REVIT_VERSIONS:
            self.log(u"CẢNH BÁO: Revit {} chưa nằm trong phạm vi kiểm thử V5.8.3 (2025/2026); tool sẽ chạy ở compatibility mode.".format(REVIT_VERSION))
        pipes, fittings = collect_pipes_and_fittings()

        # V5.8: build Min/Max dropdowns from actual Pipe diameters present
        # anywhere in the current model. No arbitrary standard-size list is used.
        model_pipe_sizes = collect_model_pipe_sizes(pipes)
        for size_ft in model_pipe_sizes:
            item = CmbItem(format_pipe_size(size_ft), float(size_ft))
            self.pipe_size_items.append(item)
            self.cmb_MinPipeSize.Items.Add(item)
            self.cmb_MaxPipeSize.Items.Add(item)
        self.cmb_MinPipeSize.DisplayMemberPath = "Name"
        self.cmb_MaxPipeSize.DisplayMemberPath = "Name"
        if self.cmb_MinPipeSize.Items.Count > 0:
            self.cmb_MinPipeSize.SelectedIndex = 0
            self.cmb_MaxPipeSize.SelectedIndex = self.cmb_MaxPipeSize.Items.Count - 1
        self._update_pipe_size_range_status()
        self.log(u"V5.8.3 Size Range: đọc được {} kích thước Pipe đang có trong model ({} -> {}).".format(
            len(model_pipe_sizes),
            format_pipe_size(model_pipe_sizes[0]) if model_pipe_sizes else u"?",
            format_pipe_size(model_pipe_sizes[-1]) if model_pipe_sizes else u"?"))

        # Revit 2025/2026: ElementId is 64-bit. Never depend on IntegerValue here.
        # First detect PipingSystemType from actual Pipe/Fitting instances.
        system_type_ids = set()
        for e in pipes + fittings:
            sid = get_system_type_id(e)
            sid_value = element_id_value(sid)
            if sid_value is not None and is_valid_element_id(sid):
                system_type_ids.add(sid_value)

        # Build from the real PipingSystemType elements. If instance-based
        # detection returns nothing (for example disconnected/partially built
        # MEP content), fall back to all PipingSystemType definitions in model.
        all_system_types = []
        try:
            all_system_types = list(DB.FilteredElementCollector(doc)\
                                    .OfClass(DB.Plumbing.PipingSystemType)\
                                    .WhereElementIsElementType()\
                                    .ToElements())
        except Exception:
            all_system_types = []

        all_by_value = {}
        for st in all_system_types:
            value = element_id_value(st.Id)
            if value is not None:
                all_by_value[value] = st

        if not system_type_ids and all_by_value:
            system_type_ids.update(all_by_value.keys())
            self.log(u"Revit {}: không đọc được System Type từ instance; đã fallback sang {} Piping System Type trong model.".format(REVIT_VERSION if REVIT_VERSION else u"?", len(all_by_value)))

        system_type_items = []
        for raw_id in sorted(system_type_ids):
            st = all_by_value.get(raw_id)
            eid = st.Id if st is not None else make_element_id(raw_id)
            # Only show real PipingSystemType elements; ignore classification ids.
            if _is_piping_system_type_id(eid):
                system_type_items.append(CmbItem(get_system_type_name_by_id(eid), eid))

        # Absolute fallback: if the instance ids were stale/incomplete, show all
        # real PipingSystemType definitions so the System combo is never empty.
        if not system_type_items and all_by_value:
            for raw_id, st in sorted(all_by_value.items(), key=lambda kv: get_real_name(kv[1]).lower()):
                system_type_items.append(CmbItem(get_real_name(st), st.Id))

        for item in sorted(system_type_items, key=lambda x: x.Name.lower()):
            self.system_items.append(item)
            self.cmb_Systems.Items.Add(item)
        self.cmb_Systems.DisplayMemberPath = "Name"
        if self.cmb_Systems.Items.Count > 0:
            self.cmb_Systems.SelectedIndex = 0

        # Pipe types.
        pipe_types = list(DB.FilteredElementCollector(doc)\
                          .OfClass(DB.Plumbing.PipeType)\
                          .WhereElementIsElementType()\
                          .ToElements())
        pipe_types = sorted(pipe_types, key=lambda x: get_real_name(x).lower())
        for pt in pipe_types:
            item = CmbItem(get_real_name(pt), pt.Id)
            self.pipe_type_items.append(item)
            self.cmb_PipeTypes.Items.Add(item)
            try:
                pt_key = self.id_value(pt.Id)
                self.routing_ids_by_pipe_type_id[pt_key] = self.get_routing_fitting_type_ids(pt)
            except Exception:
                self.routing_ids_by_pipe_type_id[self.id_value(pt.Id)] = None
        self.cmb_PipeTypes.DisplayMemberPath = "Name"
        if self.cmb_PipeTypes.Items.Count > 0:
            self.cmb_PipeTypes.SelectedIndex = 0

        # V5.8: one catalog builder is shared by startup, manual Refresh, and
        # supplemental .RFA loading so the visible lists cannot diverge.
        self._rebuild_fitting_catalog(reason=u"Khởi tạo UI", log_stats=True)
        self.log(u"Tìm thấy {} system type, {} pipe type, {} fitting type, {} PartType Pipe Fitting.".format(
            self.cmb_Systems.Items.Count,
            self.cmb_PipeTypes.Items.Count,
            len(self.fitting_type_items),
            len(NORMAL_FITTING_ROLES)
        ))
        if NORMAL_FITTING_ROLES:
            self.log(u"PartType đã nhận diện: {}".format(
                u", ".join([fitting_role_display_name(r) for r in NORMAL_FITTING_ROLES])))

    def choose_replacement_fitting_type(self, fitting, selected_items=None, selected_reducing_items=None):
        """V5.8: resolve by either Auto Routing or V5.3-style manual selection."""
        if self._is_manual_fitting_mode():
            old_symbol = get_symbol_from_instance(fitting)
            old_part_type = get_part_type(old_symbol)
            role = fitting_role_from_part_type_value(old_part_type)
            variable_size = fitting_has_variable_port_sizes(fitting)
            size_text = fitting_size_profile_text(fitting)
            allowed_roles = REDUCING_FITTING_ROLES if variable_size else NORMAL_FITTING_ROLES
            source_label = u"Fitting giảm" if variable_size else u"Fitting thường"
            if role not in allowed_roles:
                raise Exception(
                    u"PartType '{}' không có trong danh sách Pipe Fitting đã load ở chế độ thủ công V5.8.3"
                    .format(part_type_enum_name(old_symbol)))
            candidates = list(selected_reducing_items or []) if variable_size else list(selected_items or [])
            same_role = []
            for item in candidates:
                try:
                    if getattr(item, 'Role', u'') == role:
                        same_role.append(item)
                except Exception:
                    pass
            if len(same_role) == 0:
                raise Exception(u"{} chưa chọn type PartType '{}'"
                                .format(source_label, fitting_role_display_name(role)))
            if len(same_role) > 1:
                raise Exception(u"{} đang có {} type PartType '{}'; chỉ được chọn đúng 1"
                                .format(source_label, len(same_role), fitting_role_display_name(role)))
            chosen = same_role[0]
            self.log(u"Fitting {}: MANUAL V5.8.3 -> {} {} chọn '{}'.{}"
                     .format(self.id_value(fitting.Id), source_label,
                             fitting_role_display_name(role), chosen.Name,
                             u" Profile giảm {}".format(size_text) if variable_size else u""))
            return chosen.Value

        selected_pipe = self.cmb_PipeTypes.SelectedItem
        if not selected_pipe:
            raise Exception(u"Chưa chọn Pipe Type mới")
        pipe_type = doc.GetElement(selected_pipe.Value)
        symbol, info = resolve_routing_symbol_for_fitting(pipe_type, fitting)
        primary = info.get('primary_size_ft')
        try:
            primary_text = u"{:.1f} mm".format(float(primary) * 304.8) if primary is not None else u"?"
        except Exception:
            primary_text = u"?"
        self.log(u"Fitting {}: AUTO ROUTING -> {} rule #{} [{}], Primary Size {}, chọn '{} : {}'."
                 .format(self.id_value(fitting.Id), info.get('group_display', u"?"),
                         info.get('rule_number', u"?"), info.get('range_text', u"?"),
                         primary_text, get_real_family_name(symbol), get_real_name(symbol)))
        if fitting_has_variable_port_sizes(fitting):
            self.log(u"Fitting {}: profile giảm {} -> FamilySymbol lấy từ Routing Preferences; auto-size khôi phục từng connector."
                     .format(self.id_value(fitting.Id), fitting_size_profile_text(fitting)))
        return symbol.Id

    def create_replacement_fitting_from_partners(self, old_symbol, partner_connectors,
                                                 expected_port_count=None):
        """Create a NEW routing fitting from partner pipe/equipment connectors.

        V3.2 IMPORTANT: topology is based on the TOTAL number of old fitting
        ports, not only the number of ports that happened to be connected.
        Therefore a 3-port Wye/Tee with one open branch is no longer mistaken
        for a 2-port elbow.
        """
        part_name = part_type_enum_name(old_symbol).lower()
        count = len(partner_connectors)
        topology_count = expected_port_count if expected_port_count else count

        # 3-port junctions: Tee, Wye, lateral, etc. Routing Preferences decides
        # which selected junction family is actually inserted.
        if (topology_count == 3 or u"tee" in part_name or u"junction" in part_name or
                u"wye" in part_name or u"lateral" in part_name):
            if count != 3:
                raise Exception(u"Junction 3 cổng cần đúng 3 connector đầu vào, hiện có {}".format(count))

            # V3.3: NewTeeFitting is sensitive to which two connectors are supplied
            # as the run. Do not trust a single BasisZ heuristic; try every unique
            # ordering in isolated nested SubTransactions.
            preferred = order_tee_connectors(partner_connectors)
            orders = [tuple(preferred)]
            for perm in itertools.permutations(partner_connectors, 3):
                if not any(all(perm[i] is o[i] for i in range(3)) for o in orders):
                    orders.append(tuple(perm))
            errors = []
            for idx, c in enumerate(orders):
                trial = DB.SubTransaction(doc)
                try:
                    trial.Start()
                    created = doc.Create.NewTeeFitting(c[0], c[1], c[2])
                    if created is None:
                        raise Exception(u"NewTeeFitting trả về None")
                    trial.Commit()
                    return created
                except Exception as tee_ex:
                    try:
                        trial.RollBack()
                    except Exception:
                        pass
                    errors.append(u"#{} {}".format(idx + 1, safe_text(tee_ex)))
            raise Exception(u"NewTeeFitting thử {} thứ tự đều thất bại: {}"
                            .format(len(orders), u" | ".join(errors)))

        if topology_count == 4 or u"cross" in part_name:
            if count != 4:
                raise Exception(u"Cross 4 cổng cần đúng 4 connector đầu vào, hiện có {}".format(count))
            c = order_cross_connectors(partner_connectors)
            return doc.Create.NewCrossFitting(c[0], c[1], c[2], c[3])

        if u"transition" in part_name or u"reducer" in part_name:
            if count != 2:
                raise Exception(u"Transition/Reducer cần đúng 2 connector đang kết nối")
            return doc.Create.NewTransitionFitting(partner_connectors[0], partner_connectors[1])

        if (u"union" in part_name or u"mechanicaljoint" in part_name or
                u"mechanical joint" in part_name or u"coupling" in part_name):
            if count != 2:
                raise Exception(u"Union/MechanicalJoint cần đúng 2 connector đang kết nối")
            return doc.Create.NewUnionFitting(partner_connectors[0], partner_connectors[1])

        if u"cap" in part_name or topology_count == 1:
            raise Exception(u"Fallback recreate chưa áp dụng cho Cap/EndCap")

        if topology_count == 2 and count == 2:
            # If metadata is incomplete, classify by geometry instead of always
            # assuming elbow. Collinear connectors are a straight fitting:
            # transition when sizes differ, union when sizes are equal.
            angle = connector_pair_angle_degrees(partner_connectors[0], partner_connectors[1])
            if angle is not None and (angle <= 10.0 or angle >= 170.0):
                if connector_sizes_match(partner_connectors[0], partner_connectors[1]):
                    return doc.Create.NewUnionFitting(partner_connectors[0], partner_connectors[1])
                return doc.Create.NewTransitionFitting(partner_connectors[0], partner_connectors[1])

            return doc.Create.NewElbowFitting(partner_connectors[0], partner_connectors[1])

        raise Exception(u"Không xác định được cách recreate fitting PartType '{}' topology {} cổng với {} connector"
                        .format(part_type_enum_name(old_symbol), topology_count, count))

    def _place_exact_fitting_symbol(self, desired_symbol, place_point, old_params):
        """Place exactly the selected FamilySymbol and safely restore instance data."""
        try:
            if not desired_symbol.IsActive:
                desired_symbol.Activate()
                doc.Regenerate()
        except Exception:
            pass

        created = None
        placement_errors = []
        try:
            created = doc.Create.NewFamilyInstance(
                place_point, desired_symbol, DB.Structure.StructuralType.NonStructural)
        except Exception as ex:
            placement_errors.append(safe_text(ex))

        if created is None:
            try:
                lvl_id = nearest_level_id(place_point.Z)
                lvl = doc.GetElement(lvl_id) if lvl_id else None
                if lvl is not None:
                    created = doc.Create.NewFamilyInstance(
                        place_point, desired_symbol, lvl,
                        DB.Structure.StructuralType.NonStructural)
            except Exception as ex:
                placement_errors.append(safe_text(ex))

        if created is None:
            raise Exception(u"Không thể đặt FamilySymbol trực tiếp: {}"
                            .format(u" | ".join(placement_errors)))

        created_id = created.Id
        doc.Regenerate()
        created = doc.GetElement(created_id)
        if created is None:
            raise Exception(u"Fitting mới không còn tồn tại sau placement")
        if created.GetTypeId() != desired_symbol.Id:
            actual = doc.GetElement(created.GetTypeId())
            raise Exception(
                u"NewFamilyInstance tự tạo type '{}' thay vì type yêu cầu '{}'; không gọi ChangeTypeId"
                .format(get_real_name(actual), get_real_name(desired_symbol)))

        skipped_identity = []
        restored = restore_instance_parameters(
            created, old_params, safe_cross_family=True, diagnostics=skipped_identity)
        doc.Regenerate()
        created = doc.GetElement(created_id)
        if created is None:
            raise Exception(u"Fitting mới không còn tồn tại sau restore parameter")
        if created.GetTypeId() != desired_symbol.Id:
            actual = doc.GetElement(created.GetTypeId())
            raise Exception(
                u"Safe restore làm đổi type '{}' khỏi type yêu cầu '{}'"
                .format(get_real_name(actual), get_real_name(desired_symbol)))
        return created, restored, skipped_identity

    def create_direct_single_port_fitting(self, desired_symbol, old_port_records, old_params, links):
        """V5.8 exact-symbol replacement for one-port Pipe Fittings (Cap/EndCap/etc.)."""
        if len(old_port_records) != 1 or len(links) != 1:
            raise Exception(u"Direct single-port cần đúng 1 connector và 1 kết nối thật")
        old = old_port_records[0]
        link = links[0]
        partner = find_partner_connector(link)
        if partner is None:
            raise Exception(u"Không tìm được connector pipe đối tác cho fitting 1 cổng")
        target_point = copy_xyz(partner.Origin)
        if target_point is None:
            target_point = copy_xyz(old.get('origin'))
        if target_point is None:
            raise Exception(u"Không xác định được điểm đặt fitting 1 cổng")

        created, restored, skipped_identity = self._place_exact_fitting_symbol(
            desired_symbol, target_point, old_params)
        created_id = created.Id

        size_ok, size_note = try_match_direct_fitting_size(created_id, old_port_records)
        doc.Regenerate()
        created = doc.GetElement(created_id)
        ports = physical_connectors(created) if created is not None else []
        if len(ports) != 1:
            raise Exception(u"Fitting 1 cổng mới có {} connector vật lý".format(len(ports)))
        fc = ports[0]
        if not size_ok and not size_signatures_match(connector_size_signature(fc), old.get('size')):
            raise Exception(u"Direct single-port: size connector không khớp sau auto-size | {}".format(size_note))

        # A cap has only one axis, so roll around the pipe axis is irrelevant for
        # a round pipe connector.  Align the connector BasisZ to face the live
        # partner connector, then move origins together.
        source_axis = connector_axis(fc)
        partner_axis = connector_axis(partner)
        target_axis = partner_axis.Multiply(-1.0) if partner_axis is not None else _safe_unit(old.get('axis'))
        axis, angle = _rotation_axis_angle(source_axis, target_axis)
        pivot = copy_xyz(fc.Origin)
        if axis is not None and abs(angle) > 1.0e-10:
            line = DB.Line.CreateBound(pivot, pivot + axis)
            DB.ElementTransformUtils.RotateElement(doc, created_id, line, angle)
            doc.Regenerate()

        created = doc.GetElement(created_id)
        ports = physical_connectors(created) if created is not None else []
        if len(ports) != 1:
            raise Exception(u"Không reacquire được connector fitting 1 cổng sau rotate")
        fc = ports[0]
        partner = find_partner_connector(link)
        if partner is None:
            raise Exception(u"Không reacquire được connector pipe sau rotate fitting 1 cổng")
        move = partner.Origin - fc.Origin
        if move.GetLength() > 1.0e-10:
            DB.ElementTransformUtils.MoveElement(doc, created_id, move)
            doc.Regenerate()

        created = doc.GetElement(created_id)
        ports = physical_connectors(created) if created is not None else []
        if len(ports) != 1:
            raise Exception(u"Không reacquire được connector fitting 1 cổng sau move")
        fc = ports[0]
        partner = find_partner_connector(link)
        if partner is None:
            raise Exception(u"Không reacquire được connector pipe trước ConnectTo")
        if xyz_distance(fc.Origin, partner.Origin) > RECONNECT_TOLERANCE_FT:
            raise Exception(u"Direct single-port: connector còn hở {:.6f} ft"
                            .format(xyz_distance(fc.Origin, partner.Origin)))
        if not connector_sizes_match(fc, partner):
            raise Exception(u"Direct single-port: size connector không khớp pipe ID {}"
                            .format(element_id_value(link['partner_owner_id'])))

        if not connectors_are_connected(fc, partner):
            fitting_target = copy_xyz(fc.Origin)
            partner_target = copy_xyz(partner.Origin)
            key = connector_identity_key(fc)
            fax = copy_xyz(connector_axis(fc))
            ok1, err1 = _try_connect_pair_once(
                created_id, fitting_target, old, link['partner_owner_id'],
                partner_target, fitting_first=True, fitting_key=key, fitting_axis=fax)
            if not ok1:
                ok2, err2 = _try_connect_pair_once(
                    created_id, fitting_target, old, link['partner_owner_id'],
                    partner_target, fitting_first=False, fitting_key=key, fitting_axis=fax)
                if not ok2:
                    raise Exception(u"Direct single-port ConnectTo thất bại: {} | {}"
                                    .format(err1, err2))

        created = doc.GetElement(created_id)
        note = (u"direct-single-port; exact FamilySymbol '{}'; auto-size {}; nối 1/1; "
                u"bỏ qua {} parameter identity/ElementId"
                .format(get_real_name(desired_symbol), size_note, len(skipped_identity)))
        return created, restored, note

    def create_direct_linear_fitting(self, desired_symbol, old_port_records, old_params, links):
        """V5.8 exact-symbol replacement for two-port collinear Pipe Fittings.

        This covers specialized straight fittings (Union, PipeFlange,
        PipeMechanicalCoupling and custom straight PartTypes) without asking
        Routing Preferences to substitute another Family.
        """
        if len(old_port_records) != 2:
            raise Exception(u"Direct-linear cần đúng 2 connector")
        target_points, target_axes, target_sources = target_geometry_for_old_ports(old_port_records, links)
        if len(target_points) != 2 or target_points[0] is None or target_points[1] is None:
            raise Exception(u"Direct-linear không đọc được 2 điểm connector mục tiêu")
        target_center = xyz_centroid(target_points)
        if target_center is None:
            raise Exception(u"Direct-linear không xác định được tâm mục tiêu")

        created, restored, skipped_identity = self._place_exact_fitting_symbol(
            desired_symbol, target_center, old_params)
        created_id = created.Id
        size_ok, size_note = try_match_direct_fitting_size(created_id, old_port_records)
        doc.Regenerate()
        created = doc.GetElement(created_id)
        new_ports = physical_connectors(created) if created is not None else []
        if len(new_ports) != 2:
            raise Exception(u"Direct-linear: fitting mới có {} connector, cần 2".format(len(new_ports)))

        # Choose the end mapping primarily by domain/shape/size.  Unequal-size
        # reducers are therefore oriented to the correct pipe end automatically.
        best_perm = None
        best_score = None
        for perm in ((0, 1), (1, 0)):
            score = 0.0
            for oi in range(2):
                penalty, compatible, sok = _port_compatibility_penalty(new_ports[perm[oi]], old_port_records[oi])
                score += penalty
                if not compatible:
                    score += 1.0e9
                if not sok:
                    score += 1.0e7
            if best_score is None or score < best_score:
                best_score = score
                best_perm = perm
        if best_perm is None:
            raise Exception(u"Direct-linear: không map được 2 đầu connector")

        p0 = copy_xyz(new_ports[best_perm[0]].Origin)
        p1 = copy_xyz(new_ports[best_perm[1]].Origin)
        source_center = xyz_centroid([p0, p1])
        source_line = _safe_unit(p1 - p0)
        target_line = _safe_unit(target_points[1] - target_points[0])
        if source_center is None or source_line is None or target_line is None:
            raise Exception(u"Direct-linear: không dựng được trục source/target")

        axis, angle = _rotation_axis_angle(source_line, target_line)
        if axis is not None and abs(angle) > 1.0e-10:
            line = DB.Line.CreateBound(source_center, source_center + axis)
            DB.ElementTransformUtils.RotateElement(doc, created_id, line, angle)
            doc.Regenerate()
        move = target_center - source_center
        if move.GetLength() > 1.0e-10:
            DB.ElementTransformUtils.MoveElement(doc, created_id, move)
            doc.Regenerate()

        created = doc.GetElement(created_id)
        current_ports = physical_connectors(created) if created is not None else []
        assignment = best_axis_line_assignment(current_ports, old_port_records, target_axes, target_points)
        if assignment is None:
            raise Exception(u"Direct-linear: không map được connector sau transform")
        if not assignment.get('compatible', False):
            raise Exception(u"Direct-linear: domain/shape không tương thích; {}"
                            .format(axis_center_alignment_description(assignment)))
        if not assignment.get('size_ok', False):
            raise Exception(u"Direct-linear: size connector không khớp sau auto-size; {} | {}"
                            .format(axis_center_alignment_description(assignment), size_note))
        if assignment.get('max_line_offset', 1.0e30) > CENTERLINE_TOLERANCE_FT:
            raise Exception(u"Direct-linear: connector mới lệch centerline; {}"
                            .format(axis_center_alignment_description(assignment)))
        if assignment.get('side_mismatches', 0) > 0:
            raise Exception(u"Direct-linear: hai đầu fitting bị đảo phía; {}"
                            .format(axis_center_alignment_description(assignment)))

        connected, adjusted, max_adjust = connect_axis_aligned_fitting(
            created, old_port_records, links, target_axes, assignment)
        doc.Regenerate()
        created = doc.GetElement(created_id)
        note = (u"direct-linear; exact FamilySymbol '{}'; {}; trim/extend {} đầu ống "
                u"(max {:.6f} ft); nối {}/{}; bỏ qua {} parameter identity/ElementId"
                .format(get_real_name(desired_symbol), size_note, adjusted, max_adjust,
                        connected, len(links), len(skipped_identity)))
        return created, restored, note

    def create_direct_aligned_fitting(self, desired_symbol, old_port_records, old_params, links):
        """V4.8 exact-symbol placement by connector axes + virtual junction center.

        Unlike V3.5, connector ORIGINS do not need to be congruent between the
        old and new families. Different center-to-end dimensions are accepted.
        After the exact FamilySymbol is oriented, only the disconnected end of a
        line-based partner pipe is trim/extended to the new connector origin.
        """
        if len(old_port_records) < 2:
            raise Exception(u"Direct-align V4.8 cần ít nhất 2 connector")

        try:
            if not desired_symbol.IsActive:
                desired_symbol.Activate()
                doc.Regenerate()
        except Exception:
            pass

        target_points_pre, target_axes_pre, target_sources_pre = target_geometry_for_old_ports(
            old_port_records, links)
        if not axes_have_nonparallel_pair(target_axes_pre):
            raise Exception(u"Direct-align V4.8: CENTERLINE pipe không có ít nhất một cặp không song song")
        self.log(u"V5.5/Universal PIPE-center: {}".format(
            target_pipe_geometry_description(old_port_records, links)))
        place_point = virtual_junction_center(target_points_pre, target_axes_pre)
        if place_point is None:
            place_point = xyz_centroid(target_points_pre)
        if place_point is None:
            raise Exception(u"Không có tọa độ connector cũ để đặt fitting trực tiếp")

        created = None
        placement_errors = []
        try:
            created = doc.Create.NewFamilyInstance(
                place_point, desired_symbol, DB.Structure.StructuralType.NonStructural)
        except Exception as ex:
            placement_errors.append(safe_text(ex))

        if created is None:
            try:
                lvl_id = nearest_level_id(place_point.Z)
                lvl = doc.GetElement(lvl_id) if lvl_id else None
                if lvl is not None:
                    created = doc.Create.NewFamilyInstance(
                        place_point, desired_symbol, lvl,
                        DB.Structure.StructuralType.NonStructural)
            except Exception as ex:
                placement_errors.append(safe_text(ex))

        if created is None:
            raise Exception(u"Không thể đặt FamilySymbol trực tiếp: {}"
                            .format(u" | ".join(placement_errors)))

        created_id = created.Id
        doc.Regenerate()

        created = doc.GetElement(created_id)
        if created is None:
            raise Exception(u"Direct-align: fitting mới không còn tồn tại sau placement")
        if created.GetTypeId() != desired_symbol.Id:
            actual = doc.GetElement(created.GetTypeId())
            raise Exception(
                u"NewFamilyInstance tự tạo type '{}' thay vì type yêu cầu '{}'; không gọi ChangeTypeId"
                .format(get_real_name(actual), get_real_name(desired_symbol)))

        skipped_identity = []
        restored_pre = restore_instance_parameters(
            created, old_params, safe_cross_family=True, diagnostics=skipped_identity)
        doc.Regenerate()

        created = doc.GetElement(created_id)
        if created is None:
            raise Exception(u"Direct-align: fitting mới không còn tồn tại sau restore parameter")
        if created.GetTypeId() != desired_symbol.Id:
            actual = doc.GetElement(created.GetTypeId())
            raise Exception(
                u"Safe restore làm đổi type '{}' khỏi type yêu cầu '{}'"
                .format(get_real_name(actual), get_real_name(desired_symbol)))

        angle_ok_pre, angle_note = try_match_direct_fitting_angle(created_id, old_port_records, links)
        doc.Regenerate()
        created = doc.GetElement(created_id)
        if created is None:
            raise Exception(u"Direct-align: fitting mới mất sau khi hiệu chỉnh góc")

        size_ok_pre, size_note = try_match_direct_fitting_size(created_id, old_port_records)
        doc.Regenerate()
        created = doc.GetElement(created_id)
        if created is None:
            raise Exception(u"Direct-align: fitting mới mất sau khi hiệu chỉnh size")

        new_ports = physical_connectors(created)
        if len(new_ports) != len(old_port_records):
            raise Exception(u"Direct-align: số connector mới {} khác connector cũ {}"
                            .format(len(new_ports), len(old_port_records)))

        candidate = choose_axis_center_alignment(created_id, old_port_records, links)
        if candidate is None:
            raise Exception(u"Direct-align: không tìm được phép căn axis-center | {}".format(angle_note))
        if not candidate.get('compatible', False):
            raise Exception(u"Direct-align: domain/shape không tương thích; {}"
                            .format(axis_center_alignment_description(candidate)))
        if not candidate.get('size_ok', False):
            raise Exception(u"Direct-align: size connector không khớp sau auto-size; {} | {}"
                            .format(axis_center_alignment_description(candidate), size_note))
        if candidate.get('axis_known', False) and candidate.get('worst_axis_dot', -1.0) < AXIS_ALIGNMENT_DOT_MIN:
            raise Exception(
                u"Direct-align: trục centerline của hai Family không tương thích; {} | {}"
                .format(axis_center_alignment_description(candidate), angle_note))
        if candidate.get('max_line_offset', 1.0e30) > CENTERLINE_TOLERANCE_FT:
            raise Exception(
                u"Direct-align: connector mới không nằm trên centerline ống cũ; {} | {}"
                .format(axis_center_alignment_description(candidate), angle_note))

        # Apply winning axis orientation and virtual-center translation once.
        created = doc.GetElement(created_id)
        align_text = align_element_by_connector_axes(
            created,
            candidate['source_axes'],
            candidate.get('align_target_axes', candidate['target_axes']),
            candidate['source_center'],
            candidate['target_center'])
        doc.Regenerate()

        created = doc.GetElement(created_id)
        if created is None:
            raise Exception(u"Direct-align: fitting mới mất sau transform")

        # Recompute mapping after the real transform.
        current_ports = physical_connectors(created)
        final_assignment = best_axis_line_assignment(
            current_ports, old_port_records, candidate['target_axes'], candidate.get('target_points'))
        if final_assignment is None:
            raise Exception(u"Direct-align: không map được connector sau transform")
        if final_assignment.get('max_line_offset', 1.0e30) > CENTERLINE_TOLERANCE_FT:
            raise Exception(
                u"Direct-align: sau transform connector lệch centerline; {}"
                .format(axis_center_alignment_description(final_assignment)))
        if final_assignment.get('axis_known', False) and final_assignment.get('worst_axis_dot', -1.0) < AXIS_ALIGNMENT_DOT_MIN:
            raise Exception(
                u"Direct-align: sau transform connector vẫn lệch trục centerline; {}"
                .format(axis_center_alignment_description(final_assignment)))
        if final_assignment.get('side_mismatches', 0) > 0:
            raise Exception(
                u"Direct-align: mapping sau transform bị đảo phía run/branch; {} port; dot(side) {:.3f}"
                .format(final_assignment.get('side_mismatches', 0),
                        final_assignment.get('worst_radial_dot', -1.0)))

        connected, adjusted, max_adjust = connect_axis_aligned_fitting(
            created, old_port_records, links, candidate['target_axes'], final_assignment)
        doc.Regenerate()

        diag = axis_center_alignment_description(candidate)
        skipped_text = u"; bỏ qua {} parameter identity/ElementId".format(len(skipped_identity))
        return created, restored_pre, (
            u"{}; exact FamilySymbol '{}'; khóa theo CENTERLINE THẬT của pipe (không dùng trục fitting cũ); "
            u"stable-port-map V4.8; {}; trim/extend {} đầu ống (max {:.6f} ft); nối {}/{}; {}{}"
            .format(align_text, get_real_name(desired_symbol), u"{}; {}".format(angle_note, size_note),
                    adjusted, max_adjust, connected, len(links),
                    diag, skipped_text))

    def change_fitting_family_by_recreate(self, fitting_id, new_type_id, label):
        """Cross-family replacement without calling ChangeTypeId on old fitting.

        V4.8 bypasses NewTeeFitting/NewCrossFitting entirely for cross-family 3/4-port
        fittings and directly places the exact FamilySymbol with axis-aware alignment.
        Example: a 3-port Wye with only two connected
        pipes receives one temporary branch pipe, is recreated as a 3-port
        Junction through Routing Preferences, then the temporary pipe is removed
        so the branch returns to its original OPEN state.
        """
        fitting = doc.GetElement(fitting_id)
        if fitting is None:
            return False, u"{} | fitting không còn tồn tại".format(label)

        old_symbol = get_symbol_from_instance(fitting)
        desired_symbol = doc.GetElement(new_type_id)
        if old_symbol is None or desired_symbol is None:
            return False, u"{} | Không đọc được FamilySymbol cũ/mới".format(label)

        old_pt = get_part_type(old_symbol)
        new_pt = get_part_type(desired_symbol)
        if old_pt is not None and new_pt is not None and old_pt != new_pt:
            # A lot of custom Wye/Junction content has inconsistent PartType
            # metadata. Only reject when topology counts are also incompatible.
            old_port_count_probe = len(physical_connectors(fitting))
            desired_port_count_probe = None
            try:
                # FamilySymbol itself has no project ConnectorManager; therefore
                # desired topology cannot always be read here. Keep the warning
                # but allow 3/4-port routing recreation to decide safely.
                desired_port_count_probe = None
            except Exception:
                pass
            if old_port_count_probe not in (3, 4):
                return False, (u"{} | PartType không khớp: {} -> {}"
                               .format(label, part_type_enum_name(old_symbol),
                                       part_type_enum_name(desired_symbol)))

        links = snapshot_fitting_connections(fitting)
        old_params = snapshot_instance_parameters(fitting)
        all_old_ports = physical_connectors(fitting)
        old_port_records = snapshot_port_geometry(fitting)
        total_port_count = len(all_old_ports)
        open_ports = get_open_physical_fitting_connectors(fitting, links)

        if not links:
            return False, u"{} | Fitting không có kết nối vật lý để recreate".format(label)

        # Diagnostics are captured before any deletion so they remain valid on error.
        current_partner_connectors = []
        for link in links:
            try:
                pc = find_partner_connector(link)
                if pc is not None:
                    current_partner_connectors.append(pc)
            except Exception:
                pass
        topology_diag = (u"PartType '{}', topology {} cổng, {} kết nối thật, {} cổng hở; {}"
                         .format(part_type_enum_name(old_symbol), total_port_count,
                                 len(links), len(open_ports),
                                 partner_geometry_description(current_partner_connectors)))

        st = DB.SubTransaction(doc)
        temp_records = []
        try:
            st.Start()

            # V4.8: cross-family fittings có góc are direct-placed, so temporary
            # topology pipes are deliberately NOT created.  They were only needed
            # by NewTeeFitting/NewCrossFitting, which V4.8 bypasses to avoid Revit's
            # non-ignorable family-change failure.
            if False and total_port_count in (3, 4) and len(links) < total_port_count:
                preferred_pipe_type_id = None
                try:
                    selected_pipe = self.cmb_PipeTypes.SelectedItem
                    preferred_pipe_type_id = selected_pipe.Value if selected_pipe else None
                except Exception:
                    pass

                sys_id, pipe_type_id, level_id = resolve_temp_pipe_ids(
                    fitting, links, preferred_pipe_type_id)

                required_missing = total_port_count - len(links)
                if len(open_ports) < required_missing:
                    raise Exception(
                        u"Topology cần {} cổng tạm nhưng chỉ nhận diện được {} connector hở"
                        .format(required_missing, len(open_ports)))

                for open_conn in open_ports[:required_missing]:
                    temp_records.append(create_temp_pipe_stub(
                        open_conn, sys_id, pipe_type_id, level_id))
                doc.Regenerate()

            # Resolve all REAL partner connectors while old topology exists.
            real_partner_connectors = []
            for link in links:
                pc = find_partner_connector(link)
                if pc is None:
                    raise Exception(u"Không tìm được connector đối tác ID {}"
                                    .format(element_id_value(link['partner_owner_id'])))
                real_partner_connectors.append(pc)

            disconnected = disconnect_snapshot_links(links)
            doc.Regenerate()

            old_fitting_id = fitting.Id
            doc.Delete(old_fitting_id)
            doc.Regenerate()

            # Re-resolve real partner connector handles after deletion/regeneration.
            real_partner_connectors = []
            for link in links:
                pc = find_partner_connector(link)
                if pc is None:
                    raise Exception(u"Sau khi xóa fitting, không tìm lại được connector đối tác ID {}"
                                    .format(element_id_value(link['partner_owner_id'])))
                real_partner_connectors.append(pc)

            temp_partner_connectors = []
            for record in temp_records:
                tc = resolve_temp_stub_connector(record)
                if tc is None:
                    raise Exception(u"Sau khi xóa fitting, không tìm lại được connector ống tạm")
                temp_partner_connectors.append(tc)

            all_partner_connectors = real_partner_connectors + temp_partner_connectors
            create_mode = u"routing"
            create_note = u""
            direct_restored_pre = 0

            # V5.8 IMPORTANT: cross-family replacement always places the EXACT
            # user-selected FamilySymbol.  Routing Preferences are not allowed to
            # silently substitute another PartType/Family.  Dispatch by topology:
            #   1 port  -> single-port exact placement (Cap/EndCap/etc.)
            #   2 ports collinear -> direct-linear (Union/Flange/Coupling/etc.)
            #   2 ports angled / 3+ ports -> existing axis-center solver
            # This is what makes the expanded PartType UI actionable rather than
            # only cosmetic.
            use_direct_exact = total_port_count >= 1
            if use_direct_exact:
                if temp_records:
                    remove_temp_stubs(temp_records, None)
                    temp_records = []
                    temp_partner_connectors = []
                    doc.Regenerate()

                if total_port_count == 1:
                    self.log(u"{}: V5.8.3 topology 1 cổng -> direct-place exact FamilySymbol '{}'."
                             .format(label, get_real_name(desired_symbol)))
                    created, direct_restored_pre, create_note = self.create_direct_single_port_fitting(
                        desired_symbol, old_port_records, old_params, links)
                    create_mode = u"direct-single"
                elif total_port_count == 2 and not port_axes_have_nonparallel_pair(old_port_records):
                    self.log(u"{}: V5.8.3 topology 2 cổng thẳng -> direct-linear exact FamilySymbol '{}'; không để Routing Preferences đổi Family."
                             .format(label, get_real_name(desired_symbol)))
                    created, direct_restored_pre, create_note = self.create_direct_linear_fitting(
                        desired_symbol, old_port_records, old_params, links)
                    create_mode = u"direct-linear"
                else:
                    self.log(u"{}: V5.8.3 topology {} cổng/có góc -> direct-place exact FamilySymbol '{}'; khóa theo CENTERLINE THẬT của pipe và cho phép trim/extend đầu ống."
                             .format(label, total_port_count, get_real_name(desired_symbol)))
                    created, direct_restored_pre, create_note = self.create_direct_aligned_fitting(
                        desired_symbol, old_port_records, old_params, links)
                    create_mode = u"direct-align"
            else:
                created = self.create_replacement_fitting_from_partners(
                    old_symbol, all_partner_connectors, total_port_count)

            if created is None:
                raise Exception(u"Revit không tạo được fitting thay thế")
            created_id = created.Id
            doc.Regenerate()

            created_symbol = get_symbol_from_instance(created)
            created_type_id = created.GetTypeId()

            # Routing Preferences may choose a size/rule-specific symbol. Accept it
            # only when selected. Same-Family type adjustment remains safe.
            created_id_value = element_id_value(created_type_id)
            selected_ids = set(self.selected_fitting_type_ids)
            selected_ids.update(self.selected_reducing_fitting_type_ids)

            if created_type_id != new_type_id:
                if create_mode.startswith(u"direct"):
                    raise Exception(u"Direct placement tạo sai type '{}' thay vì type đã chọn '{}'"
                                    .format(get_real_name(created_symbol), get_real_name(desired_symbol)))
                if same_family_for_type_ids(created_type_id, new_type_id):
                    created.ChangeTypeId(new_type_id)
                    doc.Regenerate()
                    created = doc.GetElement(created_id)
                    created_symbol = get_symbol_from_instance(created)
                    created_type_id = created.GetTypeId()
                    created_id_value = element_id_value(created_type_id)
                elif created_id_value not in selected_ids:
                    raise Exception(
                        u"Routing Preferences tạo '{}' nhưng type này chưa được chọn; "
                        u"không dùng ChangeTypeId khác Family để tránh lỗi Revit"
                        .format(get_real_name(created_symbol)))

            if create_mode.startswith(u"direct"):
                # Already restored safely inside direct placement. Do not repeat the
                # broad V3.4/V3.5 restore, which could write the old Family/Type ElementId.
                restored = direct_restored_pre
            else:
                # Cross-family routing recreation must also avoid identity ElementIds.
                restored = restore_instance_parameters(created, old_params, safe_cross_family=True)
            doc.Regenerate()

            # Remove construction stubs AFTER the junction has been created. The
            # corresponding replacement connectors intentionally remain open.
            removed_temp = remove_temp_stubs(temp_records, created_id)
            created = doc.GetElement(created_id)
            if created is None:
                raise Exception(u"Fitting mới bị mất sau khi xóa ống tạm")
            doc.Regenerate()

            # Validate only original REAL external connections. Open ports are
            # expected to remain open and are not treated as failures.
            new_connectors = physical_connectors(created)
            matched = 0
            used = set()
            for link in links:
                partner = find_partner_connector(link)
                if partner is None:
                    raise Exception(u"Mất connector đối tác khi kiểm tra kết nối")

                idx, fc = find_new_fitting_connector(new_connectors, link, used)
                if fc is None or not connectors_are_connected(fc, partner):
                    idx, fc = None, None
                    for ni, nc in enumerate(new_connectors):
                        if ni in used:
                            continue
                        if connectors_are_connected(nc, partner):
                            idx, fc = ni, nc
                            break
                if fc is None or not connectors_are_connected(fc, partner):
                    raise Exception(u"Fitting mới chưa nối lại đủ connector với hệ ống cũ")
                if not connector_sizes_match(fc, partner):
                    raise Exception(u"Size connector fitting mới không khớp connector ống")
                used.add(idx)
                matched += 1

            if matched != len(links):
                raise Exception(u"Chỉ khôi phục {}/{} kết nối thật".format(matched, len(links)))

            # Sanity check: replacement topology must still expose the same number
            # of physical ports as the old fitting.
            final_port_count = len(physical_connectors(created))
            if total_port_count and final_port_count != total_port_count:
                raise Exception(u"Số cổng fitting thay đổi {} -> {}"
                                .format(total_port_count, final_port_count))

            st.Commit()
            mode_text = u"recreate khác Family" if create_mode == u"routing" else u"direct-align V4.8 khác Family"
            note_text = (u"; " + create_note) if create_note else u""
            return True, (u"OK - {}; topology {} cổng; ngắt {} kết nối thật, "
                          u"dùng {} ống tạm cho cổng hở, nối lại {}/{}; khôi phục {} instance parameter; "
                          u"type thực tế '{}'{}"
                          .format(mode_text, total_port_count, disconnected, removed_temp,
                                  matched, len(links), restored,
                                  get_real_name(created_symbol), note_text))
        except Exception as ex:
            try:
                st.RollBack()
            except Exception:
                pass
            return False, (u"{} | Recreate fitting khác Family lỗi: {} | {}"
                           .format(label, safe_text(ex), topology_diag))

    def change_fitting_type_force_reconnect(self, fitting_id, new_type_id, label, first_error):
        """Fallback for SAME-FAMILY fitting type changes blocked by connections.

        Cross-family changes are handled by change_fitting_family_by_recreate()
        and never call ChangeTypeId on the existing fitting, avoiding Revit's
        unresolvable family-change failure dialog.
        """
        fitting = doc.GetElement(fitting_id)
        if fitting is None:
            return False, u"{} | fitting không còn tồn tại".format(label)

        links = snapshot_fitting_connections(fitting)
        old_transform = snapshot_transform(fitting)
        old_params = snapshot_instance_parameters(fitting)

        st = DB.SubTransaction(doc)
        try:
            st.Start()
            disconnected = disconnect_snapshot_links(links)
            doc.Regenerate()

            changed_id = fitting.ChangeTypeId(new_type_id)
            try:
                if changed_id and changed_id != DB.ElementId.InvalidElementId and changed_id != fitting.Id:
                    replacement = doc.GetElement(changed_id)
                    if replacement is not None:
                        fitting = replacement
            except Exception:
                pass

            doc.Regenerate()
            restored = restore_instance_parameters(fitting, old_params)
            doc.Regenerate()

            if not transform_is_preserved(fitting, old_transform):
                raise Exception(u"Fitting bị thay đổi vị trí/xoay sau khi đổi type")

            new_connectors = get_connectors(fitting)
            if links and len(new_connectors) < len(links):
                raise Exception(u"Type mới có ít connector hơn fitting cũ ({} < {})".format(
                    len(new_connectors), len(links)))

            # NEW V3: if the new type is reversed relative to the existing pipes,
            # try safe 180-degree rotate/flip candidates before ConnectTo().
            orientation_ok, orientation_msg = auto_orient_fitting_180(fitting, links)
            if not orientation_ok:
                raise Exception(
                    u"Fitting mới bị ngược hướng connector và không thể sửa bằng rigid rotate/flip: {}"
                    .format(orientation_msg))

            mapping = best_fitting_partner_mapping(fitting, links)
            if not mapping_is_connectable(mapping):
                raise Exception(u"Sau auto-orient connector vẫn chưa hợp lệ: {}"
                                .format(mapping_description(mapping)))

            reconnected = 0
            for link_index, link in enumerate(links):
                partner = find_partner_connector(link)
                if partner is None:
                    raise Exception(u"Không tìm lại được connector của đối tượng ID {}".format(
                        element_id_value(link['partner_owner_id'])))

                fit_index = mapping['assignment'][link_index]
                fit_conn = mapping['fit_connectors'][fit_index]

                gap = xyz_distance(fit_conn.Origin, partner.Origin)
                if gap > RECONNECT_TOLERANCE_FT:
                    raise Exception(
                        u"Connector sau auto-orient lệch {:.4f} ft (> {:.4f} ft)".format(
                            gap, RECONNECT_TOLERANCE_FT))

                dir_ok, dot_value = connector_pair_direction_ok(fit_conn, partner)
                if not dir_ok:
                    raise Exception(
                        u"Connector vẫn cùng hướng với ống (dot={:.3f}); cần sửa Connector Orientation trong Family"
                        .format(dot_value))

                if not connectors_are_connected(fit_conn, partner):
                    fit_conn.ConnectTo(partner)
                    doc.Regenerate()

                if not connectors_are_connected(fit_conn, partner):
                    raise Exception(u"ConnectTo không tạo lại kết nối vật lý")
                if not connector_sizes_match(fit_conn, partner):
                    raise Exception(u"Size connector fitting sau đổi type không khớp với connector ống/đối tượng cũ")

                reconnected += 1

            st.Commit()
            return True, (u"OK - cùng Family, đã ngắt/nối lại {}/{} connector; khôi phục {} instance parameter; {}"
                          .format(reconnected, len(links), restored, orientation_msg))
        except Exception as ex:
            try:
                st.RollBack()
            except Exception:
                pass
            return False, (u"{} | ChangeTypeId ban đầu lỗi: {} | Fallback ngắt/nối lại cũng lỗi: {}"
                           .format(label, safe_text(first_error), safe_text(ex)))

    def _is_transition_native_change(self, fitting, new_type_id):
        """True when the old or requested fitting type is a Revit Transition/Reducer.

        V5.8.3 deliberately routes these parts through the native Revit type-change
        operation.  This avoids the direct-linear reconstruction/rotation solver,
        which is unnecessary for a straight reducer and can fight families whose
        connector sizes are driven internally by Revit/content lookup logic.
        """
        try:
            old_symbol = get_symbol_from_instance(fitting)
        except Exception:
            old_symbol = None
        try:
            new_symbol = doc.GetElement(new_type_id)
        except Exception:
            new_symbol = None

        names = []
        for symbol in [old_symbol, new_symbol]:
            if symbol is None:
                continue
            try:
                names.append(safe_text(fitting_role_from_part_type_value(get_part_type(symbol))).lower())
            except Exception:
                pass
            try:
                names.append(safe_text(part_type_enum_name(symbol)).lower())
            except Exception:
                pass
        return any((u"transition" in name or u"reducer" in name) for name in names)

    def change_transition_type_native(self, fitting_id, new_type_id, label):
        """V5.8.3: let Revit perform a Transition type change natively.

        No disconnect/recreate, no auto-size probing, no rotation/flip, and no
        pipe trim/extend are performed here.  We only validate that Revit kept
        the existing external connections and that each connected end still
        matches its partner size.  Any failure rolls back this fitting only.
        """
        fitting = doc.GetElement(fitting_id)
        if fitting is None:
            return False, u"{} | Transition native: fitting không còn tồn tại".format(label)

        try:
            old_type_name = get_real_name(get_symbol_from_instance(fitting))
        except Exception:
            old_type_name = u"?"
        try:
            desired_symbol = doc.GetElement(new_type_id)
            new_type_name = get_real_name(desired_symbol)
            new_family_name = get_real_family_name(desired_symbol)
        except Exception:
            desired_symbol = None
            new_type_name = u"?"
            new_family_name = u"?"

        links = snapshot_fitting_connections(fitting)
        old_port_count = len(physical_connectors(fitting))

        st = DB.SubTransaction(doc)
        try:
            st.Start()
            self.log(
                u"{}: V5.8.3 Transition -> NATIVE REVIT ChangeTypeId '{}' -> '{} : {}'; "
                u"KHÔNG recreate, KHÔNG auto-size, KHÔNG rotate/flip, KHÔNG trim/extend pipe."
                .format(label, old_type_name, new_family_name, new_type_name))

            changed_id = fitting.ChangeTypeId(new_type_id)
            doc.Regenerate()

            actual = None
            try:
                if (changed_id and changed_id != DB.ElementId.InvalidElementId):
                    actual = doc.GetElement(changed_id)
            except Exception:
                actual = None
            if actual is None:
                try:
                    actual = doc.GetElement(fitting_id)
                except Exception:
                    actual = None
            if actual is None:
                raise Exception(u"Revit không trả lại Transition sau ChangeTypeId")

            try:
                if not same_element_id(actual.GetTypeId(), new_type_id):
                    actual_symbol = get_symbol_from_instance(actual)
                    raise Exception(
                        u"Revit tạo type thực tế '{} : {}' thay vì type yêu cầu '{} : {}'"
                        .format(get_real_family_name(actual_symbol), get_real_name(actual_symbol),
                                new_family_name, new_type_name))
            except Exception as verify_ex:
                if u"thay vì type yêu cầu" in safe_text(verify_ex):
                    raise

            new_port_count = len(physical_connectors(actual))
            if old_port_count and new_port_count != old_port_count:
                raise Exception(u"Số connector thay đổi {} -> {}".format(old_port_count, new_port_count))

            # Native change must preserve all existing physical relations.  We do
            # not attempt to repair them here because the user explicitly wants
            # Transition handled by Revit's own type-change behavior.
            matched = 0
            size_mismatch = []
            actual_connectors = list(physical_connectors(actual) or [])
            for link in links:
                partner = find_partner_connector(link)
                if partner is None:
                    raise Exception(
                        u"Không tìm lại được connector đối tác ID {} sau native ChangeTypeId"
                        .format(element_id_value(link.get('partner_owner_id'))))

                connected = None
                for fc in actual_connectors:
                    try:
                        if connectors_are_connected(fc, partner):
                            connected = fc
                            break
                    except Exception:
                        pass
                if connected is None:
                    raise Exception(
                        u"Native ChangeTypeId làm mất kết nối vật lý với đối tượng ID {}"
                        .format(element_id_value(link.get('partner_owner_id'))))
                matched += 1
                try:
                    if not connector_sizes_match(connected, partner):
                        size_mismatch.append(element_id_value(link.get('partner_owner_id')))
                except Exception:
                    pass

            if size_mismatch:
                raise Exception(
                    u"Native ChangeTypeId giữ kết nối nhưng size connector không khớp với đối tượng ID {}"
                    .format(u", ".join([safe_text(x) for x in size_mismatch])))

            st.Commit()
            return True, (
                u"OK - Transition Native Revit ChangeTypeId; type '{} : {}'; giữ {}/{} kết nối; "
                u"không recreate/rotate/auto-size/trim pipe"
                .format(new_family_name, new_type_name, matched, len(links)))
        except Exception as ex:
            try:
                st.RollBack()
            except Exception:
                pass
            return False, u"{} | Transition Native ChangeTypeId lỗi: {}".format(label, safe_text(ex))

    def change_type_safe(self, elem, new_type_id, label, allow_force_reconnect=False):
        """Change one element safely.

        V5.8.3 rules:
        - Transition/Reducer: always use native Revit ChangeTypeId; no recreate,
          no angle/rotation solver and no pipe trim/extend.
        - Pipe and same-Family non-Transition fitting: ChangeTypeId is allowed.
        - Different-Family non-Transition MEP fitting: avoid ChangeTypeId and use
          the existing safe recreate/direct-align workflow.
        """
        if elem is None or new_type_id is None:
            return False, u"Thiếu phần tử hoặc type mới"

        try:
            if elem.GetTypeId() == new_type_id:
                return False, u"Bỏ qua vì đã đúng type"
        except Exception:
            pass

        elem_id = elem.Id

        # V5.8.3: Transition/Reducer must use Revit's native type change exactly
        # as requested.  Do this BEFORE the cross-family recreate guard so a
        # different-family reducer never enters direct-linear/rotation/auto-size.
        if is_pipe_fitting(elem) and self._is_transition_native_change(elem, new_type_id):
            return self.change_transition_type_native(elem_id, new_type_id, label)

        # Avoid the unresolvable Revit failure BEFORE it can be posted for the
        # remaining MEP fitting PartTypes.
        if allow_force_reconnect and is_pipe_fitting(elem):
            try:
                if not same_family_for_type_ids(elem.GetTypeId(), new_type_id):
                    self.log(u"{}: V5.5/Universal phát hiện khác Family -> KHÔNG ChangeTypeId; chuyển sang recreate/direct-align.".format(label))
                    return self.change_fitting_family_by_recreate(elem_id, new_type_id, label)
            except Exception as ex:
                return False, u"{} | Không kiểm tra được Family cũ/mới: {}".format(label, safe_text(ex))

        st = DB.SubTransaction(doc)
        first_error = None
        try:
            st.Start()
            elem.ChangeTypeId(new_type_id)
            st.Commit()
            return True, u"OK"
        except Exception as ex:
            first_error = ex
            try:
                st.RollBack()
            except Exception:
                pass

        if allow_force_reconnect and is_pipe_fitting(doc.GetElement(elem_id)):
            return self.change_fitting_type_force_reconnect(elem_id, new_type_id, label, first_error)

        return False, u"{} | {}".format(label, safe_text(first_error))

    def element_id_int(self, eid):
        return element_id_value(eid)

    def _item_role(self, item):
        try:
            role = getattr(item, 'Role', u'')
            if role:
                return safe_text(role)
            return fitting_role_from_part_type_value(getattr(item, 'PartType', None))
        except Exception:
            return u''

    def _remove_other_selected_ids_for_role(self, selected_ids, keep_item, allowed_roles):
        """Keep at most one selected symbol for one PartType role."""
        role = self._item_role(keep_item)
        if role not in allowed_roles:
            return False
        keep_id = self.id_value(keep_item.Value)
        for other in self.fitting_type_items:
            try:
                if self._item_role(other) == role:
                    other_id = self.id_value(other.Value)
                    if other_id != keep_id:
                        selected_ids.discard(other_id)
            except Exception:
                pass
        selected_ids.add(keep_id)
        return True

    def _normalize_selected_ids(self, selected_ids, allowed_roles, require_routing=False):
        """Normalize old config/multi-selections to one symbol per PartType."""
        chosen_by_role = {}
        # Prefer selected IDs in deterministic fitting-list order.  This also
        # migrates V5.1 configs that may contain multiple types of one PartType.
        for item in self.fitting_type_items:
            try:
                item_id = self.id_value(item.Value)
                if item_id not in selected_ids:
                    continue
                role = self._item_role(item)
                if role not in allowed_roles:
                    continue
                if require_routing and self.routing_fitting_type_ids is not None and item_id not in self.routing_fitting_type_ids:
                    continue
                if role not in chosen_by_role:
                    chosen_by_role[role] = item_id
            except Exception:
                pass
        selected_ids.clear()
        for role in allowed_roles:
            if role in chosen_by_role:
                selected_ids.add(chosen_by_role[role])

    def _selection_summary(self, selected_ids, roles):
        """Compact summary for a potentially large set of Pipe Fitting PartTypes."""
        selected_roles = []
        for role in roles:
            found = False
            for item in self.fitting_type_items:
                try:
                    if self._item_role(item) == role and self.id_value(item.Value) in selected_ids:
                        found = True
                        break
                except Exception:
                    pass
            if found:
                selected_roles.append(fitting_role_display_name(role))
        if selected_roles:
            preview = u", ".join(selected_roles[:8])
            if len(selected_roles) > 8:
                preview += u", +{} nhóm".format(len(selected_roles) - 8)
            return u"đã chọn {}/{} PartType: {}".format(len(selected_roles), len(roles), preview)
        return u"đã chọn 0/{} PartType".format(len(roles))

    def refresh_fitting_list(self, filter_text=None):
        """Normal list: every loaded OST_PipeFitting PartType, one type per PartType.

        Symbols referenced by the selected PipeType Routing Preferences are
        sorted first, but non-routing symbols remain selectable for exact-symbol
        replacement of specialized/custom fittings.
        """
        try:
            self._normalize_selected_ids(
                self.selected_fitting_type_ids, NORMAL_FITTING_ROLES, require_routing=False)
            self._refreshing_fitting_list = True
            self.lst_FittingTypes.Items.Clear()

            ft = safe_text(filter_text).strip().lower()
            available_items = []
            for item in self.fitting_type_items:
                try:
                    item_id = self.id_value(item.Value)
                    if self._item_role(item) not in NORMAL_FITTING_ROLES:
                        continue
                    available_items.append(item)
                except Exception:
                    pass

            role_rank = dict((r, i) for i, r in enumerate(NORMAL_FITTING_ROLES))
            def _normal_item_sort_key(item):
                try:
                    iid = self.id_value(item.Value)
                    in_routing = (self.routing_fitting_type_ids is not None and
                                  iid in self.routing_fitting_type_ids)
                    return (0 if in_routing else 1,
                            role_rank.get(self._item_role(item), 99999),
                            safe_text(item.Name).lower())
                except Exception:
                    return (1, 99999, safe_text(getattr(item, 'Name', u'')).lower())
            available_items = sorted(available_items, key=_normal_item_sort_key)

            visible_count = 0
            for item in available_items:
                name = safe_text(item.Name).lower()
                if (not ft) or (ft in name):
                    self.lst_FittingTypes.Items.Add(item)
                    visible_count += 1
                    try:
                        if self.id_value(item.Value) in self.selected_fitting_type_ids:
                            self.lst_FittingTypes.SelectedItems.Add(item)
                    except Exception:
                        pass
            self._refreshing_fitting_list = False
            try:
                self.txt_FittingCount.Text = (
                    u"Hiển thị {}/{} | Mỗi nhóm tối đa 1 type — {}"
                    .format(visible_count, len(available_items),
                            self._selection_summary(self.selected_fitting_type_ids, NORMAL_FITTING_ROLES)))
            except Exception:
                pass
        except Exception as ex:
            self._refreshing_fitting_list = False
            self.log(u"Lỗi filter Fitting thường: {}".format(safe_text(ex)))

    def fitting_selection_changed(self, sender, args):
        """V5.8: selecting a second type in the same normal role replaces the first."""
        if self._refreshing_fitting_list:
            return
        try:
            for item in list(args.RemovedItems):
                try:
                    self.selected_fitting_type_ids.discard(self.id_value(item.Value))
                except Exception:
                    pass
            for item in list(args.AddedItems):
                self._remove_other_selected_ids_for_role(
                    self.selected_fitting_type_ids, item, NORMAL_FITTING_ROLES)
            self.refresh_fitting_list(self.txt_FilterFittings.Text)
        except Exception as ex:
            self.log(u"Không thể cập nhật lựa chọn Fitting thường: {}".format(safe_text(ex)))

    def get_selected_fitting_items(self):
        self._normalize_selected_ids(
            self.selected_fitting_type_ids, NORMAL_FITTING_ROLES, require_routing=False)
        return [item for item in self.fitting_type_items
                if self.id_value(item.Value) in self.selected_fitting_type_ids
                and self._item_role(item) in NORMAL_FITTING_ROLES]

    def filter_fittings_text_changed(self, sender, args):
        try:
            self.refresh_fitting_list(self.txt_FilterFittings.Text)
        except Exception:
            pass

    def clear_filter_click(self, sender, args):
        try:
            self.txt_FilterFittings.Text = u""
            self.refresh_fitting_list(u"")
        except Exception:
            pass

    def clear_selected_fittings_click(self, sender, args):
        try:
            self.selected_fitting_type_ids.clear()
            self.refresh_fitting_list(self.txt_FilterFittings.Text)
            self.log(u"Đã bỏ chọn toàn bộ Fitting thường.")
        except Exception as ex:
            self.log(u"Không thể bỏ chọn Fitting thường: {}".format(safe_text(ex)))

    def refresh_reducing_fitting_list(self, filter_text=None):
        """Reducing list: every loaded OST_PipeFitting PartType.

        The old instance decides whether the reducing list is needed by checking
        its live connector-size profile; one-port PartTypes are never classified
        as reducing because they cannot have unequal port sizes.
        """
        try:
            self._normalize_selected_ids(
                self.selected_reducing_fitting_type_ids, REDUCING_FITTING_ROLES, require_routing=False)
            self._refreshing_reducing_fitting_list = True
            self.lst_ReducingFittingTypes.Items.Clear()
            ft = safe_text(filter_text).strip().lower()
            available_items = [item for item in self.fitting_type_items
                               if self._item_role(item) in REDUCING_FITTING_ROLES]
            visible_count = 0
            for item in available_items:
                name = safe_text(item.Name).lower()
                if (not ft) or (ft in name):
                    self.lst_ReducingFittingTypes.Items.Add(item)
                    visible_count += 1
                    try:
                        if self.id_value(item.Value) in self.selected_reducing_fitting_type_ids:
                            self.lst_ReducingFittingTypes.SelectedItems.Add(item)
                    except Exception:
                        pass
            self._refreshing_reducing_fitting_list = False
            try:
                self.txt_ReducingFittingCount.Text = (
                    u"Hiển thị {}/{} | Mỗi nhóm tối đa 1 type — {}"
                    .format(visible_count, len(available_items),
                            self._selection_summary(self.selected_reducing_fitting_type_ids,
                                                    REDUCING_FITTING_ROLES)))
            except Exception:
                pass
        except Exception as ex:
            self._refreshing_reducing_fitting_list = False
            self.log(u"Lỗi filter Fitting giảm: {}".format(safe_text(ex)))

    def reducing_fitting_selection_changed(self, sender, args):
        """V5.8: selecting a second reducing type in one role replaces the first."""
        if self._refreshing_reducing_fitting_list:
            return
        try:
            for item in list(args.RemovedItems):
                try:
                    self.selected_reducing_fitting_type_ids.discard(self.id_value(item.Value))
                except Exception:
                    pass
            for item in list(args.AddedItems):
                self._remove_other_selected_ids_for_role(
                    self.selected_reducing_fitting_type_ids, item, REDUCING_FITTING_ROLES)
            self.refresh_reducing_fitting_list(self.txt_FilterReducingFittings.Text)
        except Exception as ex:
            self.log(u"Không thể cập nhật lựa chọn Fitting giảm: {}".format(safe_text(ex)))

    def get_selected_reducing_fitting_items(self):
        self._normalize_selected_ids(
            self.selected_reducing_fitting_type_ids, REDUCING_FITTING_ROLES, require_routing=False)
        return [item for item in self.fitting_type_items
                if self.id_value(item.Value) in self.selected_reducing_fitting_type_ids
                and self._item_role(item) in REDUCING_FITTING_ROLES]

    def filter_reducing_fittings_text_changed(self, sender, args):
        try:
            self.refresh_reducing_fitting_list(self.txt_FilterReducingFittings.Text)
        except Exception:
            pass

    def clear_reducing_filter_click(self, sender, args):
        try:
            self.txt_FilterReducingFittings.Text = u""
            self.refresh_reducing_fitting_list(u"")
        except Exception:
            pass

    def clear_selected_reducing_fittings_click(self, sender, args):
        try:
            self.selected_reducing_fitting_type_ids.clear()
            self.refresh_reducing_fitting_list(self.txt_FilterReducingFittings.Text)
            self.log(u"Đã bỏ chọn toàn bộ Fitting giảm.")
        except Exception as ex:
            self.log(u"Không thể bỏ chọn Fitting giảm: {}".format(safe_text(ex)))

    def _validate_fitting_type_selection(self, target_fittings, normal_items, reducing_items):
        """Require exactly one selected type for every PartType used by this batch."""
        normal_by_role = {}
        reducing_by_role = {}
        for item in list(normal_items or []):
            role = self._item_role(item)
            if role in NORMAL_FITTING_ROLES:
                normal_by_role.setdefault(role, []).append(item)
        for item in list(reducing_items or []):
            role = self._item_role(item)
            if role in REDUCING_FITTING_ROLES:
                reducing_by_role.setdefault(role, []).append(item)

        needed_normal = set()
        needed_reducing = set()
        unsupported = []
        for fit in list(target_fittings or []):
            try:
                sym = get_symbol_from_instance(fit)
                role = fitting_role_from_symbol(sym)
                variable = fitting_has_variable_port_sizes(fit)
                fit_id = self.id_value(fit.Id)
                if variable:
                    if role in REDUCING_FITTING_ROLES:
                        needed_reducing.add(role)
                    else:
                        unsupported.append(u"ID {}: {} giảm".format(
                            fit_id, part_type_enum_name(sym) or u"PartType không xác định"))
                else:
                    if role in NORMAL_FITTING_ROLES:
                        needed_normal.add(role)
                    else:
                        unsupported.append(u"ID {}: {}".format(
                            fit_id, part_type_enum_name(sym) or u"PartType không xác định"))
            except Exception:
                pass

        if unsupported:
            return False, (u"Selection có fitting với PartType không có FamilySymbol tương ứng "
                           u"được load trong project: {}. Hãy load/chọn một type cùng PartType."
                           .format(u"; ".join(unsupported[:8])))

        problems = []
        for role in NORMAL_FITTING_ROLES:
            count = len(normal_by_role.get(role, []))
            if count > 1:
                problems.append(u"Fitting thường {} đang chọn {} type (chỉ được 1)".format(fitting_role_display_name(role), count))
            if role in needed_normal and count != 1:
                problems.append(u"Fitting thường {} cần đúng 1 type, hiện có {}".format(fitting_role_display_name(role), count))
        for role in REDUCING_FITTING_ROLES:
            count = len(reducing_by_role.get(role, []))
            if count > 1:
                problems.append(u"Fitting giảm {} đang chọn {} type (chỉ được 1)".format(fitting_role_display_name(role), count))
            if role in needed_reducing and count != 1:
                problems.append(u"Fitting giảm {} cần đúng 1 type, hiện có {}".format(fitting_role_display_name(role), count))

        if problems:
            return False, u"; ".join(problems)
        return True, u""


    def replace_click(self, sender, args):
        """Run one batch against the immutable preselection snapshot."""
        selected_system = self.cmb_Systems.SelectedItem
        selected_pipe_type = self.cmb_PipeTypes.SelectedItem
        selected_fitting_items = self.get_selected_fitting_items()
        selected_reducing_fitting_items = self.get_selected_reducing_fitting_items()
        overwrite_pipes = bool(self.chk_ReplacePipes.IsChecked)
        overwrite_fittings = bool(self.chk_ReplaceFittings.IsChecked)
        selected_only = True
        try:
            force_reconnect_fittings = bool(self.chk_ForceReconnectFittings.IsChecked)
        except Exception:
            force_reconnect_fittings = True

        if not overwrite_pipes and not overwrite_fittings:
            forms.alert(u"Vui lòng chọn ít nhất một hạng mục: Thay Pipe hoặc Thay Fitting.", title="Thiếu dữ liệu")
            return
        if not selected_system:
            forms.alert(u"Vui lòng chọn System Type đang tồn tại.", title="Thiếu dữ liệu")
            return
        if not selected_pipe_type:
            forms.alert(u"Vui lòng chọn Pipe Type mới.", title="Thiếu dữ liệu")
            return

        min_pipe_size_ft, max_pipe_size_ft = self._current_pipe_size_range()
        if min_pipe_size_ft is None or max_pipe_size_ft is None:
            forms.alert(u"Model không có Pipe size hợp lệ để xác định Min/Max.", title="Thiếu Pipe Size")
            return
        if min_pipe_size_ft > max_pipe_size_ft:
            forms.alert(u"Pipe Size Min không được lớn hơn Pipe Size Max.", title="Range không hợp lệ")
            return

        system_type_id = selected_system.Value
        system_name = selected_system.Name
        new_pipe_type_id = selected_pipe_type.Value
        self.txt_Log.Clear()
        self.save_last_choices(selected_system, selected_pipe_type, selected_fitting_items,
                               selected_reducing_fitting_items, overwrite_pipes, overwrite_fittings,
                               selected_only, force_reconnect_fittings)

        scope_name = u"các đối tượng đã chọn trước khi mở tool"
        self.log(u"Bắt đầu xử lý System Type: {}".format(system_name))
        self.log(u"Phạm vi xử lý: {}.".format(scope_name))
        self.log(u"Giới hạn Pipe Size V5.8.3: {} <= Size <= {}.".format(
            format_pipe_size(min_pipe_size_ft), format_pipe_size(max_pipe_size_ft)))
        pipes, fittings = collect_pipes_and_fittings(True, self.preselected_element_ids)
        if selected_only and not pipes and not fittings:
            forms.alert(u"Các đối tượng đã chọn trước khi mở tool không còn hợp lệ. Hãy đóng tool, chọn lại Pipe/Fitting rồi chạy lệnh lại.", title="Không có đối tượng hợp lệ")
            return

        system_pipes = [p for p in pipes if same_element_id(get_system_type_id(p), system_type_id)] if overwrite_pipes else []
        system_fittings = [f for f in fittings if same_element_id(get_system_type_id(f), system_type_id)] if overwrite_fittings else []

        # V5.8: range is inclusive and uses the EXISTING size before any type
        # replacement.  A fitting is treated atomically: every physical port must
        # fit inside the selected range, otherwise the whole fitting is skipped.
        target_pipes = []
        range_skipped_lines = []
        for p in system_pipes:
            try:
                pipe_id_value = self.id_value(p.Id)
                diameter = get_pipe_diameter_ft(p)
                if diameter is not None and pipe_size_in_range(
                        p, min_pipe_size_ft, max_pipe_size_ft):
                    target_pipes.append(p)
                else:
                    range_skipped_lines.append(
                        u"Pipe ID {}: Bỏ qua do Size {} nằm ngoài range {} -> {}"
                        .format(pipe_id_value, format_pipe_size(diameter),
                                format_pipe_size(min_pipe_size_ft),
                                format_pipe_size(max_pipe_size_ft)))
            except Exception as ex:
                range_skipped_lines.append(
                    u"Pipe: Bỏ qua do không đọc được Size để kiểm tra range: {}".format(safe_text(ex)))

        target_fittings = []
        for f in system_fittings:
            try:
                fitting_id_value = self.id_value(f.Id)
                in_range, port_sizes = fitting_size_in_range(
                    f, min_pipe_size_ft, max_pipe_size_ft)
                if in_range:
                    target_fittings.append(f)
                else:
                    profile = u", ".join([format_pipe_size(x) for x in port_sizes]) if port_sizes else u"không đọc được"
                    range_skipped_lines.append(
                        u"Fitting ID {}: Bỏ qua do port size [{}] không nằm hoàn toàn trong range {} -> {}"
                        .format(fitting_id_value, profile,
                                format_pipe_size(min_pipe_size_ft),
                                format_pipe_size(max_pipe_size_ft)))
            except Exception as ex:
                range_skipped_lines.append(
                    u"Fitting: Bỏ qua do không đọc được port size để kiểm tra range: {}".format(safe_text(ex)))

        if not target_pipes and not target_fittings:
            self.log(u"Không có Pipe/Fitting nào trong System Type đã chọn nằm trong Pipe Size range hiện tại.")
            if range_skipped_lines:
                self.log(u"--- BỎ QUA DO SIZE RANGE ({}) ---".format(len(range_skipped_lines)))
                for line in range_skipped_lines:
                    self.log(line)
            return

        # V5.8 manual mode must have exactly one selected FamilySymbol for every
        # PartType that will actually be processed in this range. Validate before
        # opening the main Transaction so the model cannot be changed partially.
        if overwrite_fittings and self._is_manual_fitting_mode() and target_fittings:
            valid_manual, manual_problem = self._validate_fitting_type_selection(
                target_fittings, selected_fitting_items, selected_reducing_fitting_items)
            if not valid_manual:
                self.log(u"CHẾ ĐỘ THỦ CÔNG - thiếu/không hợp lệ Fitting Type: {}".format(manual_problem))
                forms.alert(
                    u"Chế độ Chọn Fitting thủ công chưa đủ type cho batch hiện tại:\n\n{}".format(manual_problem),
                    title="Thiếu Fitting Type")
                return

        self.log(u"Chế độ Fitting: {}.".format(
            u"CHỌN THỦ CÔNG như V5.3" if self._is_manual_fitting_mode() else u"AUTO ROUTING PREFERENCES"))

        changed, skipped, failed = 0, len(range_skipped_lines), []
        forced_reconnected = 0
        # V4.9: keep a complete audit trail in the on-screen log.  No final
        # result popup is shown after a committed batch.
        success_lines = []
        skipped_lines = list(range_skipped_lines)
        tx = DB.Transaction(doc, u"Replace Pipes and Fittings - Skip Failed")
        try:
            tx.Start()
            try:
                opts = tx.GetFailureHandlingOptions()
                opts.SetFailuresPreprocessor(SkipWarningsPreprocessor())
                opts.SetClearAfterRollback(True)
                tx.SetFailureHandlingOptions(opts)
            except Exception:
                pass
            for p in target_pipes:
                # Cache the ID BEFORE any API operation. Some Revit operations can
                # invalidate a managed Element wrapper; never read p.Id afterwards.
                try:
                    pipe_id_value = self.id_value(p.Id)
                except Exception:
                    pipe_id_value = u"?"
                pipe_label = u"Pipe ID {}".format(pipe_id_value)

                ok, msg = self.change_type_safe(
                    p, new_pipe_type_id, pipe_label, False)
                if ok:
                    changed += 1
                    success_lines.append(u"{}: {}".format(pipe_label, safe_text(msg) or u"OK"))
                else:
                    skipped += 1
                    if safe_text(msg).startswith(u"Bỏ qua"):
                        skipped_lines.append(u"{}: {}".format(pipe_label, safe_text(msg)))
                    else:
                        failed.append(u"{}: {}".format(pipe_label, safe_text(msg)))

            for f in target_fittings:
                # IMPORTANT: cache ID/label before change_type_safe(). Cross-family
                # replacement intentionally deletes the original fitting. Even if its
                # SubTransaction is later rolled back, the old Python/.NET wrapper may
                # remain invalid, so accessing f.Id here can throw
                # "referenced object is not valid" and hide the REAL fitting error.
                try:
                    fitting_id_value = self.id_value(f.Id)
                except Exception:
                    fitting_id_value = u"?"
                fitting_label = u"Fitting ID {}".format(fitting_id_value)

                try:
                    new_fit_type_id = self.choose_replacement_fitting_type(
                        f, selected_fitting_items, selected_reducing_fitting_items)
                    ok, msg = self.change_type_safe(
                        f, new_fit_type_id, fitting_label, force_reconnect_fittings)
                except Exception as fitting_ex:
                    ok = False
                    msg = u"Lỗi ngoài dự kiến khi xử lý fitting: {}\n{}".format(
                        safe_text(fitting_ex), traceback.format_exc())

                # From this point onward DO NOT access f or f.Id. The original fitting
                # may have been deleted/recreated, so only use the cached label.
                if ok:
                    changed += 1
                    success_lines.append(u"{}: {}".format(fitting_label, safe_text(msg) or u"OK"))
                    if (u"ngắt/nối lại" in safe_text(msg) or
                            u"recreate khác Family" in safe_text(msg)):
                        forced_reconnected += 1
                else:
                    skipped += 1
                    if safe_text(msg).startswith(u"Bỏ qua"):
                        skipped_lines.append(u"{}: {}".format(fitting_label, safe_text(msg)))
                    else:
                        failed.append(u"{}: {}".format(fitting_label, safe_text(msg)))
            tx.Commit()
        except Exception as ex:
            try:
                tx.RollBack()
            except Exception:
                pass
            forms.alert(u"Lỗi transaction chính, đã rollback toàn bộ:\n{}".format(safe_text(ex)), title="Lỗi")
            self.log(traceback.format_exc())
            return

        # V4.9: everything goes to the persistent UI log.  Do not show the
        # small completion alert; the user can inspect/copy all details here.
        self.log(u"")
        self.log(u"============================================================")
        self.log(u"KẾT QUẢ XỬ LÝ - System Type: {}".format(system_name))
        self.log(u"Phạm vi: {}".format(scope_name))
        self.log(u"Chế độ Fitting: {}".format(
            u"CHỌN THỦ CÔNG như V5.3" if self._is_manual_fitting_mode() else u"AUTO ROUTING PREFERENCES"))
        self.log(u"Pipe Size range: {} -> {} (inclusive)".format(
            format_pipe_size(min_pipe_size_ft), format_pipe_size(max_pipe_size_ft)))
        self.log(u"Thành công: {} | Fallback/recreate: {} | Bỏ qua không lỗi: {} | Bỏ qua do lỗi: {} | Tổng không thay đổi: {}".format(
            changed, forced_reconnected, len(skipped_lines), len(failed), skipped))

        if success_lines:
            self.log(u"--- THÀNH CÔNG ({}) ---".format(len(success_lines)))
            for line in success_lines:
                self.log(line)
        else:
            self.log(u"--- THÀNH CÔNG: không có phần tử nào thay đổi ---")

        if skipped_lines:
            self.log(u"--- BỎ QUA KHÔNG LỖI ({}) ---".format(len(skipped_lines)))
            for line in skipped_lines:
                self.log(line)

        if failed:
            self.log(u"--- BỎ QUA DO LỖI ({}) ---".format(len(failed)))
            for line in failed:
                self.log(line)
        else:
            self.log(u"--- LỖI: 0 ---")

        self.log(u"============================================================")
        self.log(u"Đã kết thúc batch. Selection Revit được xóa để tránh dùng lại ID fitting đã recreate.")
        self.log(u"Để xử lý vị trí khác: đóng cửa sổ này, chọn Pipe/Fitting mới trong Revit rồi chạy lệnh lại.")

        # Cross-family replacement can delete/recreate fittings. Clear the Revit
        # selection after a committed batch so the next invocation MUST start from
        # a fresh, intentional preselection and can never reuse stale fitting IDs.
        try:
            empty_ids = List[DB.ElementId]() if List is not None else []
            uidoc.Selection.SetElementIds(empty_ids)
        except Exception:
            pass

        # Keep the modal UI open so the result log remains visible.  Disable the
        # Run button because this window owns an immutable preselection snapshot.
        try:
            self.btn_Replace.IsEnabled = False
            self.btn_Replace.Content = u"Đã hoàn tất — đóng cửa sổ để chọn đối tượng mới"
        except Exception:
            pass
        try:
            self.txt_PreselectionStatus.Text = (
                u"Đã xử lý xong batch hiện tại. Selection Revit đã được xóa. "
                u"Xem kết quả chi tiết trong Log bên dưới; đóng UI trước khi chọn batch mới.")
            self.txt_PreselectionStatus.Foreground = System.Windows.Media.Brushes.DarkGreen
        except Exception:
            pass


# ============================================================
# Entry point - require preselection before opening UI (V5.8 Universal / Revit 2025-2026)
# ============================================================

script_dir = os.path.dirname(__file__)
xaml_path = os.path.join(script_dir, 'ui.xaml')


def get_valid_preselection_ids():
    """Snapshot current Revit selection; keep only host Pipe/Pipe Fitting IDs."""
    valid = []
    seen = set()
    pipe_count = 0
    fitting_count = 0
    ignored_count = 0
    try:
        current_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        current_ids = []

    for eid in current_ids:
        try:
            elem = doc.GetElement(eid)
            if is_pipe(elem):
                pipe_count += 1
            elif is_pipe_fitting(elem):
                fitting_count += 1
            else:
                ignored_count += 1
                continue
            value = element_id_value(eid)
            if value is None:
                ignored_count += 1
                continue
            if value not in seen:
                seen.add(value)
                valid.append(value)
        except Exception:
            ignored_count += 1

    return valid, pipe_count, fitting_count, ignored_count


if not os.path.exists(xaml_path):
    forms.alert(u"Không tìm thấy file giao diện ui.xaml!", title="Lỗi đường dẫn")
else:
    selected_ids, selected_pipe_count, selected_fitting_count, ignored_count = get_valid_preselection_ids()
    if not selected_ids:
        forms.alert(
            u"Hãy chọn Pipe và/hoặc Pipe Fitting trong Revit TRƯỚC khi chạy lệnh.\n\n"
            u"Tool sẽ không mở giao diện nếu selection không có Pipe/Pipe Fitting hợp lệ.",
            title="Chưa chọn đối tượng")
    else:
        try:
            win = ReplaceElementsWindow(xaml_path, selected_ids)
            if ignored_count:
                win.log(u"Selection ban đầu có {} đối tượng không phải Pipe/Pipe Fitting và đã được bỏ qua.".format(ignored_count))
            win.log(u"Đã khóa phạm vi preselection: {} Pipe + {} Fitting.".format(
                selected_pipe_count, selected_fitting_count))
            win.show_dialog()
        except Exception as ex:
            forms.alert(
                u"Không thể mở UI V5.8.3 Universal / Revit 2025-2026:\n{}\n\n{}".format(safe_text(ex), traceback.format_exc()),
                title="Correct Pipes & Fittings")
