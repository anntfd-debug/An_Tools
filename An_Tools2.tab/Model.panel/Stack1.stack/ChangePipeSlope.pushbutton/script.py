# -*- coding: utf-8 -*-
"""
CHANGE PIPE SLOPE - V1.1
========================

pyRevit / Revit 2025-2026

NORMAL CLICK
------------
- Preselect one or many Pipes.
- Run tool.
- Internal Pipe Fittings / Pipe Accessories between selected pipes are handled
  as part of the movable network when it is safe to do so.

SHIFT + CLICK
-------------
- Shift+Click the pyRevit button.
- Pick one Pipe.
- Tool walks physical piping connections and collects every connected Pipe
  having the same System Name, plus intermediate Pipe Fittings / Accessories.

SLOPE
-----
- Dropdown is read directly from Project Pipe Settings > Slopes.
- Last chosen slope is persisted by pyRevit config.
- Geometry is solved from an anchor; the script does NOT simply write a slope
  parameter and leave Revit to decide which connected segment should move.

ANCHOR PRIORITY
---------------
1. A fixed external fitting/accessory/equipment at the boundary of the target
   network. A fitting is preferred over other categories.
2. If the network is standalone, keep an internal fitting/accessory fixed;
   preference is given to an element far from free pipe ends.
3. For an isolated Pipe, keep one end fixed.

Typical reducer case:

    FIXED REDUCER                 FREE END MOVES
          [><]--------------------------o
           ^
           | temporary anchor

The reducer is temporarily pinned (when possible), the free end Z is changed,
and the actual geometric slope is validated after regeneration.

SAFETY
------
- Components with cycles are skipped.
- Components with more than one fixed external boundary are skipped because
  an exact common slope cannot be imposed without moving an external system.
- Vertical Pipes are skipped.
- Each connected component uses its own SubTransaction and rolls back if slope
  or physical connections fail validation.
- Full success is completely silent.
- Cancel is silent.
- Any failure / skipped / rolled-back component shows one small error message only.
"""

from __future__ import print_function

import math
from collections import deque

from pyrevit import revit, DB, script

from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from System.Windows.Forms import (
    Form, Label, ComboBox, Button, DialogResult,
    FormBorderStyle, FormStartPosition, ComboBoxStyle,
    MessageBox, MessageBoxButtons, MessageBoxIcon
)
from System.Drawing import Point, Size
from System.Collections.Generic import List


# ============================================================
# REVIT CONTEXT
# ============================================================

uidoc = revit.uidoc
doc = revit.doc


# ============================================================
# SETTINGS
# ============================================================

TOOL_TITLE = "Change Pipe Slope V1.1"
CONFIG_KEY = "change_pipe_slope_v1"
MAX_NETWORK_ELEMENTS = 10000

XYZ_TOL = 1e-7
HORIZONTAL_TOL = 1e-7
SLOPE_RATIO_TOL = 1e-5

# When a Pipe is perfectly horizontal, there is no existing slope direction to
# preserve. The movable end is lowered relative to the anchor.
HORIZONTAL_DEFAULT_SIGN = -1.0


# ============================================================
# ID / CATEGORY HELPERS
# ============================================================

def eid_value(eid):
    if eid is None:
        return None
    try:
        return int(eid.Value)
    except Exception:
        pass
    try:
        return int(eid.IntegerValue)
    except Exception:
        return None


def same_id(a, b):
    av = eid_value(a)
    bv = eid_value(b)
    return av is not None and bv is not None and av == bv


def cat_id_value(elem):
    if elem is None:
        return None
    try:
        if elem.Category is None:
            return None
        return eid_value(elem.Category.Id)
    except Exception:
        return None


def bic_id_value(bic):
    try:
        return eid_value(DB.ElementId(bic))
    except Exception:
        return None


PIPE_CAT = bic_id_value(DB.BuiltInCategory.OST_PipeCurves)
FITTING_CAT = bic_id_value(DB.BuiltInCategory.OST_PipeFitting)
ACCESSORY_CAT = bic_id_value(DB.BuiltInCategory.OST_PipeAccessory)
MECH_EQUIP_CAT = bic_id_value(DB.BuiltInCategory.OST_MechanicalEquipment)
PLUMB_FIXTURE_CAT = bic_id_value(DB.BuiltInCategory.OST_PlumbingFixtures)

TRANSIT_CATS = set([FITTING_CAT, ACCESSORY_CAT])
BOUNDARY_PRIORITY = {
    FITTING_CAT: 100,
    ACCESSORY_CAT: 90,
    MECH_EQUIP_CAT: 70,
    PLUMB_FIXTURE_CAT: 70,
    PIPE_CAT: 40,
}


def is_pipe(elem):
    if elem is None:
        return False
    try:
        return isinstance(elem, DB.Plumbing.Pipe)
    except Exception:
        return cat_id_value(elem) == PIPE_CAT


def is_transit(elem):
    return cat_id_value(elem) in TRANSIT_CATS


# ============================================================
# CONNECTOR HELPERS
# ============================================================

def iter_connectors(elem):
    """Return connector objects for Pipe / FamilyInstance MEP elements."""
    if elem is None:
        return []

    try:
        cm = elem.ConnectorManager
        if cm is not None:
            return [c for c in cm.Connectors]
    except Exception:
        pass

    try:
        mep = elem.MEPModel
        if mep is not None and mep.ConnectorManager is not None:
            return [c for c in mep.ConnectorManager.Connectors]
    except Exception:
        pass

    return []


def is_piping_connector(conn):
    try:
        return conn.Domain == DB.Domain.DomainPiping
    except Exception:
        return False


def is_logical_connector(conn):
    try:
        return conn.ConnectorType == DB.ConnectorType.Logical
    except Exception:
        return False


def physical_ref_connectors(conn):
    """Physical connector references only, excluding self/logical refs."""
    refs = []
    if conn is None or not is_piping_connector(conn) or is_logical_connector(conn):
        return refs

    try:
        owner_id = eid_value(conn.Owner.Id)
    except Exception:
        owner_id = None

    try:
        allrefs = conn.AllRefs
    except Exception:
        return refs

    for rc in allrefs:
        try:
            if rc is None or is_logical_connector(rc):
                continue
            if not is_piping_connector(rc):
                continue
            if rc.Owner is None:
                continue
            rid = eid_value(rc.Owner.Id)
            if rid is None or rid == owner_id:
                continue

            # IsConnectedTo is a useful extra guard against logical AllRefs.
            try:
                if not conn.IsConnectedTo(rc):
                    continue
            except Exception:
                pass

            refs.append(rc)
        except Exception:
            continue

    return refs


def physical_owner_ids(elem):
    result = set()
    for c in iter_connectors(elem):
        for rc in physical_ref_connectors(c):
            try:
                result.add(eid_value(rc.Owner.Id))
            except Exception:
                pass
    result.discard(None)
    return result


def connector_external_owner_ids(conn, work_ids):
    result = set()
    for rc in physical_ref_connectors(conn):
        try:
            oid = eid_value(rc.Owner.Id)
        except Exception:
            oid = None
        if oid is not None and oid not in work_ids:
            result.add(oid)
    return result


def elements_physically_connected(id_a, id_b):
    ea = doc.GetElement(DB.ElementId(id_a))
    if ea is None:
        return False
    for c in iter_connectors(ea):
        for rc in physical_ref_connectors(c):
            try:
                if eid_value(rc.Owner.Id) == id_b:
                    return True
            except Exception:
                pass
    return False


# ============================================================
# PIPE GEOMETRY
# ============================================================

def pipe_curve_points(pipe):
    try:
        curve = pipe.Location.Curve
        return curve.GetEndPoint(0), curve.GetEndPoint(1)
    except Exception:
        return None, None


def pipe_end_connectors(pipe):
    """Return two pipe end connectors ordered to match LocationCurve endpoints."""
    p0, p1 = pipe_curve_points(pipe)
    if p0 is None:
        return None, None

    ends = []
    for c in iter_connectors(pipe):
        if not is_piping_connector(c) or is_logical_connector(c):
            continue
        try:
            ctype = c.ConnectorType
            if ctype != DB.ConnectorType.End:
                continue
        except Exception:
            pass
        try:
            _ = c.Origin
            ends.append(c)
        except Exception:
            pass

    if len(ends) < 2:
        # Fallback: take the two connectors nearest the line ends.
        ends = []
        for c in iter_connectors(pipe):
            if not is_piping_connector(c) or is_logical_connector(c):
                continue
            try:
                _ = c.Origin
                ends.append(c)
            except Exception:
                pass

    if len(ends) < 2:
        return None, None

    # Pick connector closest to p0 and connector closest to p1, uniquely.
    c0 = min(ends, key=lambda c: c.Origin.DistanceTo(p0))
    remaining = [c for c in ends if c is not c0]
    if not remaining:
        return None, None
    c1 = min(remaining, key=lambda c: c.Origin.DistanceTo(p1))
    return c0, c1


def horizontal_length(a, b):
    dx = b.X - a.X
    dy = b.Y - a.Y
    return math.sqrt(dx * dx + dy * dy)


def actual_slope_ratio(pipe):
    p0, p1 = pipe_curve_points(pipe)
    if p0 is None:
        return None
    run = horizontal_length(p0, p1)
    if run <= HORIZONTAL_TOL:
        return None
    return abs(p1.Z - p0.Z) / run


def connected_to_owner(conn, owner_id):
    for rc in physical_ref_connectors(conn):
        try:
            if eid_value(rc.Owner.Id) == owner_id:
                return True
        except Exception:
            pass
    return False


def set_pipe_end_z(pipe, anchor_index, desired_movable_z):
    """Keep anchor endpoint XYZ; keep movable XY; only change movable Z."""
    p0, p1 = pipe_curve_points(pipe)
    if p0 is None:
        raise Exception("Pipe has no valid LocationCurve")

    if anchor_index == 0:
        anchor = p0
        movable = p1
        new_movable = DB.XYZ(movable.X, movable.Y, desired_movable_z)
        new_curve = DB.Line.CreateBound(anchor, new_movable)
    else:
        anchor = p1
        movable = p0
        new_movable = DB.XYZ(movable.X, movable.Y, desired_movable_z)
        new_curve = DB.Line.CreateBound(new_movable, anchor)

    pipe.Location.Curve = new_curve


# ============================================================
# SYSTEM NAME
# ============================================================

def get_system_name(pipe):
    if pipe is None:
        return ""

    try:
        sysobj = pipe.MEPSystem
        if sysobj is not None and sysobj.Name:
            return str(sysobj.Name)
    except Exception:
        pass

    try:
        p = pipe.get_Parameter(DB.BuiltInParameter.RBS_SYSTEM_NAME_PARAM)
        if p is not None:
            s = p.AsString()
            if s:
                return str(s)
    except Exception:
        pass

    return ""


# ============================================================
# SHIFT PICK
# ============================================================

class PipeSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return is_pipe(elem)

    def AllowReference(self, reference, position):
        return False


def shift_clicked():
    try:
        return bool(__shiftclick__)
    except Exception:
        return False


def pick_seed_pipe():
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element,
            PipeSelectionFilter(),
            "Pick one Pipe. Tool will collect connected Pipes with the same System Name."
        )
        return doc.GetElement(ref.ElementId)
    except Exception:
        return None


# ============================================================
# TARGET COLLECTION
# ============================================================

def collect_shift_network(seed_pipe):
    """Walk piping connections, keeping Pipes only when System Name matches."""
    target_name = get_system_name(seed_pipe)
    if not target_name:
        raise Exception("Picked Pipe has no System Name.")

    seed_id = eid_value(seed_pipe.Id)
    visited = set()
    target_pipes = set()
    transit_candidates = set()
    q = deque([seed_id])

    while q:
        current_id = q.popleft()
        if current_id in visited:
            continue
        visited.add(current_id)

        if len(visited) > MAX_NETWORK_ELEMENTS:
            raise Exception(
                "Connected network exceeds safety limit of {0} elements.".format(
                    MAX_NETWORK_ELEMENTS
                )
            )

        elem = doc.GetElement(DB.ElementId(current_id))
        if elem is None:
            continue

        if is_pipe(elem):
            if get_system_name(elem) != target_name:
                continue
            target_pipes.add(current_id)
        elif is_transit(elem):
            transit_candidates.add(current_id)
        else:
            continue

        for oid in physical_owner_ids(elem):
            other = doc.GetElement(DB.ElementId(oid))
            if other is None:
                continue
            if is_pipe(other):
                if get_system_name(other) == target_name and oid not in visited:
                    q.append(oid)
            elif is_transit(other):
                if oid not in visited:
                    q.append(oid)
            # Equipment / fixtures / other categories are hard boundaries.

    return target_name, target_pipes, transit_candidates


def collect_normal_transit_candidates(target_pipe_ids, explicitly_selected_transits):
    """Find fittings/accessories that may safely bridge the selected Pipes."""
    candidates = set(explicitly_selected_transits)
    q = deque(list(target_pipe_ids))
    visited = set()

    while q:
        eid = q.popleft()
        if eid in visited:
            continue
        visited.add(eid)

        elem = doc.GetElement(DB.ElementId(eid))
        if elem is None:
            continue

        for oid in physical_owner_ids(elem):
            other = doc.GetElement(DB.ElementId(oid))
            if other is None:
                continue

            if is_transit(other):
                if oid not in candidates:
                    candidates.add(oid)
                    q.append(oid)
            elif is_pipe(other):
                # Never expand normal mode into an unselected Pipe.
                continue

    return candidates


def filter_internal_transits(target_pipe_ids, candidate_transit_ids):
    """
    A transit element is movable only when every physical neighbor belongs to
    target Pipes or to the candidate transit set. Anything connected to an
    external Pipe/equipment becomes a fixed boundary instead.
    """
    allowed = set(target_pipe_ids) | set(candidate_transit_ids)
    internal = set()

    for tid in candidate_transit_ids:
        elem = doc.GetElement(DB.ElementId(tid))
        if elem is None or not is_transit(elem):
            continue

        owners = physical_owner_ids(elem)
        if not owners:
            continue

        safe = True
        has_target_pipe = False
        for oid in owners:
            other = doc.GetElement(DB.ElementId(oid))
            if other is None:
                continue
            if is_pipe(other):
                if oid not in target_pipe_ids:
                    safe = False
                    break
                has_target_pipe = True
            elif is_transit(other):
                if oid not in allowed:
                    safe = False
                    break
            else:
                safe = False
                break

        if safe and has_target_pipe:
            internal.add(tid)

    # Remove transits that became connected to another candidate transit that
    # was rejected, because that rejected transit is now an external boundary.
    changed = True
    while changed:
        changed = False
        for tid in list(internal):
            elem = doc.GetElement(DB.ElementId(tid))
            owners = physical_owner_ids(elem)
            for oid in owners:
                other = doc.GetElement(DB.ElementId(oid))
                if is_transit(other) and oid in candidate_transit_ids and oid not in internal:
                    internal.remove(tid)
                    changed = True
                    break

    return internal


# ============================================================
# GRAPH
# ============================================================

def build_graph(work_ids):
    graph = dict((eid, set()) for eid in work_ids)
    for eid in work_ids:
        elem = doc.GetElement(DB.ElementId(eid))
        if elem is None:
            continue
        for oid in physical_owner_ids(elem):
            if oid in work_ids:
                graph[eid].add(oid)
                graph[oid].add(eid)
    return graph


def graph_components(graph):
    result = []
    seen = set()
    for start in graph:
        if start in seen:
            continue
        comp = set()
        q = deque([start])
        seen.add(start)
        while q:
            n = q.popleft()
            comp.add(n)
            for nb in graph.get(n, []):
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        result.append(comp)
    return result


def component_is_tree(graph, comp):
    if len(comp) <= 1:
        return True
    edge_count = 0
    for n in comp:
        edge_count += len([x for x in graph.get(n, []) if x in comp])
    edge_count = edge_count // 2
    return edge_count == len(comp) - 1


def component_pipe_ids(comp):
    return set([eid for eid in comp if is_pipe(doc.GetElement(DB.ElementId(eid)))])


def free_endpoint_records(pipe_ids, work_ids):
    records = []
    for pid in pipe_ids:
        pipe = doc.GetElement(DB.ElementId(pid))
        c0, c1 = pipe_end_connectors(pipe)
        for idx, c in enumerate([c0, c1]):
            if c is None:
                continue
            refs = physical_ref_connectors(c)
            if len(refs) == 0:
                records.append((pid, idx))
    return records


def boundary_endpoint_records(pipe_ids, work_ids):
    records = []
    for pid in pipe_ids:
        pipe = doc.GetElement(DB.ElementId(pid))
        c0, c1 = pipe_end_connectors(pipe)
        for idx, c in enumerate([c0, c1]):
            if c is None:
                continue
            outside = connector_external_owner_ids(c, work_ids)
            for oid in outside:
                owner = doc.GetElement(DB.ElementId(oid))
                priority = BOUNDARY_PRIORITY.get(cat_id_value(owner), 10)
                records.append((priority, pid, idx, oid))
    return records


def shortest_distances(graph, start, comp):
    dist = {start: 0}
    q = deque([start])
    while q:
        n = q.popleft()
        for nb in graph.get(n, []):
            if nb not in comp or nb in dist:
                continue
            dist[nb] = dist[n] + 1
            q.append(nb)
    return dist


def choose_internal_anchor(graph, comp, pipe_ids):
    """Choose fitting/accessory far from nearest free Pipe where possible."""
    candidates = []
    for eid in comp:
        elem = doc.GetElement(DB.ElementId(eid))
        if is_transit(elem):
            candidates.append(eid)

    if not candidates:
        return None

    free_recs = free_endpoint_records(pipe_ids, comp)
    free_pipe_ids = set([r[0] for r in free_recs])

    if not free_pipe_ids:
        # Stable deterministic fallback.
        return sorted(candidates)[0]

    # Maximize distance from the nearest free-ended Pipe. This chooses an
    # interior/far fitting rather than anchoring the free side itself.
    best = None
    best_score = -1
    all_dist = {}
    for fp in free_pipe_ids:
        all_dist[fp] = shortest_distances(graph, fp, comp)

    for cand in candidates:
        vals = []
        for fp in free_pipe_ids:
            d = all_dist.get(fp, {}).get(cand)
            if d is not None:
                vals.append(d)
        score = min(vals) if vals else 0
        if score > best_score:
            best_score = score
            best = cand

    return best


def orient_tree(graph, comp, root_id):
    parent = {root_id: None}
    order = []
    q = deque([root_id])

    while q:
        n = q.popleft()
        order.append(n)
        for nb in graph.get(n, []):
            if nb not in comp or nb in parent:
                continue
            parent[nb] = n
            q.append(nb)

    children = dict((n, []) for n in comp)
    for n, p in parent.items():
        if p is not None:
            children[p].append(n)

    return parent, children, order


def descendants(children, start):
    result = set()
    q = deque(children.get(start, []))
    while q:
        n = q.popleft()
        if n in result:
            continue
        result.add(n)
        for ch in children.get(n, []):
            q.append(ch)
    return result


# ============================================================
# ROOT / ANCHOR RESOLUTION
# ============================================================

def resolve_component_root(graph, comp):
    pipe_ids = component_pipe_ids(comp)
    if not pipe_ids:
        return None

    boundaries = boundary_endpoint_records(pipe_ids, comp)

    # Collapse duplicates caused by connector refs but preserve unique pipe end.
    unique = {}
    for rec in boundaries:
        priority, pid, idx, oid = rec
        key = (pid, idx)
        old = unique.get(key)
        if old is None or priority > old[0]:
            unique[key] = rec
    boundaries = list(unique.values())

    if len(boundaries) > 1:
        return {
            "ok": False,
            "reason": "more than one fixed external boundary ({0})".format(len(boundaries))
        }

    if len(boundaries) == 1:
        priority, pid, idx, owner_id = boundaries[0]
        return {
            "ok": True,
            "root_id": pid,
            "root_pipe_anchor_index": idx,
            "anchor_element_id": owner_id,
            "root_kind": "external"
        }

    internal_anchor = choose_internal_anchor(graph, comp, pipe_ids)
    if internal_anchor is not None:
        return {
            "ok": True,
            "root_id": internal_anchor,
            "root_pipe_anchor_index": None,
            "anchor_element_id": internal_anchor,
            "root_kind": "internal"
        }

    # Isolated Pipe or direct Pipe chain without fittings.
    root_pipe = sorted(pipe_ids)[0]
    return {
        "ok": True,
        "root_id": root_pipe,
        "root_pipe_anchor_index": 0,
        "anchor_element_id": None,
        "root_kind": "pipe_end"
    }


# ============================================================
# CONNECTION SNAPSHOT / VALIDATION
# ============================================================

def snapshot_connections(comp):
    pairs = set()
    for eid in comp:
        elem = doc.GetElement(DB.ElementId(eid))
        if elem is None:
            continue
        for oid in physical_owner_ids(elem):
            # Include internal + external physical edges touching the component.
            a = min(eid, oid)
            b = max(eid, oid)
            pairs.add((a, b))
    return pairs


def validate_connections(pairs):
    broken = []
    for a, b in pairs:
        if not elements_physically_connected(a, b):
            broken.append((a, b))
    return broken


# ============================================================
# TEMPORARY PIN
# ============================================================

def try_pin(elem):
    if elem is None:
        return None
    try:
        old = bool(elem.Pinned)
        if not old:
            elem.Pinned = True
        return old
    except Exception:
        return None


def restore_pin(elem, old_state):
    if elem is None or old_state is None:
        return
    try:
        elem.Pinned = bool(old_state)
    except Exception:
        pass


# ============================================================
# PIPE PROCESSING
# ============================================================

def anchor_index_for_parent(pipe, parent_id):
    c0, c1 = pipe_end_connectors(pipe)
    if c0 is None or c1 is None:
        return None
    if connected_to_owner(c0, parent_id):
        return 0
    if connected_to_owner(c1, parent_id):
        return 1
    return None


def move_elements_z(ids, dz):
    if not ids or abs(dz) <= XYZ_TOL:
        return
    net_ids = List[DB.ElementId]()
    for eid in ids:
        net_ids.Add(DB.ElementId(eid))
    DB.ElementTransformUtils.MoveElements(doc, net_ids, DB.XYZ(0.0, 0.0, dz))


def process_component(graph, comp, slope_ratio):
    root_info = resolve_component_root(graph, comp)
    if not root_info or not root_info.get("ok"):
        reason = root_info.get("reason", "could not resolve anchor") if root_info else "no Pipes"
        return False, reason, 0

    if not component_is_tree(graph, comp):
        return False, "network contains a cycle", 0

    root_id = root_info["root_id"]
    parent, children, order = orient_tree(graph, comp, root_id)
    if len(parent) != len(comp):
        return False, "network orientation failed", 0

    # Preflight: every non-root Pipe must connect to its parent at one endpoint.
    for eid in order:
        elem = doc.GetElement(DB.ElementId(eid))
        if not is_pipe(elem):
            continue
        if eid == root_id and root_info["root_pipe_anchor_index"] is not None:
            continue
        p = parent.get(eid)
        if p is None:
            return False, "Pipe root has no resolvable anchor end", 0
        if anchor_index_for_parent(elem, p) is None:
            return False, "Pipe {0} cannot identify parent connector".format(eid), 0

    original_pairs = snapshot_connections(comp)
    st = DB.SubTransaction(doc)
    st.Start()

    anchor_elem = None
    anchor_old_pin = None
    changed = 0

    try:
        anchor_id = root_info.get("anchor_element_id")
        if anchor_id is not None:
            anchor_elem = doc.GetElement(DB.ElementId(anchor_id))
            anchor_old_pin = try_pin(anchor_elem)

        doc.Regenerate()

        # Process pipes from the anchor outward.
        for eid in order:
            pipe = doc.GetElement(DB.ElementId(eid))
            if not is_pipe(pipe):
                continue

            if eid == root_id and root_info["root_pipe_anchor_index"] is not None:
                anchor_idx = root_info["root_pipe_anchor_index"]
            else:
                p = parent.get(eid)
                anchor_idx = anchor_index_for_parent(pipe, p)

            if anchor_idx is None:
                raise Exception("Pipe {0}: anchor endpoint not found".format(eid))

            p0, p1 = pipe_curve_points(pipe)
            if p0 is None:
                raise Exception("Pipe {0}: invalid LocationCurve".format(eid))

            anchor_pt = p0 if anchor_idx == 0 else p1
            movable_pt = p1 if anchor_idx == 0 else p0
            run = horizontal_length(anchor_pt, movable_pt)

            if run <= HORIZONTAL_TOL:
                # Vertical pipe: slope in percent is not meaningful.
                continue

            dz_now = movable_pt.Z - anchor_pt.Z
            if abs(dz_now) > XYZ_TOL:
                sign = 1.0 if dz_now > 0.0 else -1.0
            else:
                sign = HORIZONTAL_DEFAULT_SIGN

            desired_z = anchor_pt.Z + sign * run * slope_ratio
            delta_z = desired_z - movable_pt.Z

            # Everything downstream moves rigidly in Z first. This preserves
            # downstream internal connections while the current Pipe changes
            # slope about its anchored end.
            sub_ids = descendants(children, eid)
            if sub_ids:
                move_elements_z(sub_ids, delta_z)
                doc.Regenerate()

            # Enforce exact geometry on the current Pipe. The anchor XY/Z is
            # preserved; movable XY is preserved; only movable Z is changed.
            set_pipe_end_z(pipe, anchor_idx, desired_z)
            doc.Regenerate()
            changed += 1

            actual = actual_slope_ratio(pipe)
            if actual is None:
                raise Exception("Pipe {0}: slope validation unavailable".format(eid))
            if abs(actual - slope_ratio) > SLOPE_RATIO_TOL:
                raise Exception(
                    "Pipe {0}: target {1:.6f}, actual {2:.6f}".format(
                        eid, slope_ratio, actual
                    )
                )

        broken = validate_connections(original_pairs)
        if broken:
            text = ", ".join(["{0}-{1}".format(a, b) for a, b in broken[:8]])
            raise Exception("physical connection lost: {0}".format(text))

        restore_pin(anchor_elem, anchor_old_pin)
        doc.Regenerate()
        st.Commit()
        return True, "", changed

    except Exception as ex:
        try:
            restore_pin(anchor_elem, anchor_old_pin)
        except Exception:
            pass
        try:
            st.RollBack()
        except Exception:
            pass
        return False, str(ex), 0


# ============================================================
# PROJECT SLOPE SETTINGS
# ============================================================

def get_project_slopes():
    values = []
    try:
        ps = DB.Plumbing.PipeSettings.GetPipeSettings(doc)
        for v in ps.GetPipeSlopes():
            fv = float(v)
            if 0.0 <= fv <= 100.0:
                values.append(fv)
    except Exception:
        pass

    # Unique sorted values while preserving enough precision.
    unique = []
    seen = set()
    for v in sorted(values):
        key = round(v, 8)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def slope_label(value):
    text = ("{0:.6f}".format(float(value))).rstrip("0").rstrip(".")
    return text + " %"


# ============================================================
# PERSISTENCE
# ============================================================

def load_last_slope():
    try:
        cfg = script.get_config(CONFIG_KEY)
        raw = getattr(cfg, "last_slope_percent", None)
        if raw is None:
            return None
        return float(raw)
    except Exception:
        return None


def save_last_slope(value):
    try:
        cfg = script.get_config(CONFIG_KEY)
        # Store numeric text only. Do not store '%' in config values.
        cfg.last_slope_percent = str(float(value))
        script.save_config()
    except Exception:
        pass


# ============================================================
# UI
# ============================================================

class SlopeDialog(Form):
    def __init__(self, slope_values, last_value, mode_text, target_text):
        self.slope_values = slope_values
        self.selected_value = None

        self.Text = TOOL_TITLE
        self.Size = Size(470, 235)
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.StartPosition = FormStartPosition.CenterScreen
        self.TopMost = True

        lbl_mode = Label()
        lbl_mode.Text = mode_text
        lbl_mode.Location = Point(20, 18)
        lbl_mode.Size = Size(415, 24)
        self.Controls.Add(lbl_mode)

        lbl_target = Label()
        lbl_target.Text = target_text
        lbl_target.Location = Point(20, 45)
        lbl_target.Size = Size(415, 38)
        self.Controls.Add(lbl_target)

        lbl = Label()
        lbl.Text = "Slope from Project Pipe Settings:"
        lbl.Location = Point(20, 93)
        lbl.Size = Size(250, 22)
        self.Controls.Add(lbl)

        self.combo = ComboBox()
        self.combo.Location = Point(275, 90)
        self.combo.Size = Size(160, 26)
        self.combo.DropDownStyle = ComboBoxStyle.DropDownList
        self.Controls.Add(self.combo)

        self.label_to_value = {}
        for val in slope_values:
            label = slope_label(val)
            self.label_to_value[label] = val
            self.combo.Items.Add(label)

        select_index = 0
        if last_value is not None and slope_values:
            best_i = 0
            best_d = abs(slope_values[0] - last_value)
            for i, v in enumerate(slope_values):
                d = abs(v - last_value)
                if d < best_d:
                    best_d = d
                    best_i = i
            select_index = best_i
        if self.combo.Items.Count > 0:
            self.combo.SelectedIndex = select_index

        note = Label()
        note.Text = "Anchor is kept fixed. Horizontal pipes fall away from the anchor."
        note.Location = Point(20, 125)
        note.Size = Size(415, 22)
        self.Controls.Add(note)

        btn_ok = Button()
        btn_ok.Text = "Apply"
        btn_ok.Location = Point(270, 155)
        btn_ok.Size = Size(78, 30)
        btn_ok.Click += self.on_ok
        self.Controls.Add(btn_ok)
        self.AcceptButton = btn_ok

        btn_cancel = Button()
        btn_cancel.Text = "Cancel"
        btn_cancel.Location = Point(357, 155)
        btn_cancel.Size = Size(78, 30)
        btn_cancel.DialogResult = DialogResult.Cancel
        self.Controls.Add(btn_cancel)
        self.CancelButton = btn_cancel

    def on_ok(self, sender, args):
        if self.combo.SelectedItem is None:
            return
        label = str(self.combo.SelectedItem)
        self.selected_value = self.label_to_value.get(label)
        if self.selected_value is None:
            return
        self.DialogResult = DialogResult.OK
        self.Close()


def show_error(message):
    """Show one compact failure message. Success/cancel stay silent."""
    try:
        MessageBox.Show(
            str(message),
            TOOL_TITLE,
            MessageBoxButtons.OK,
            MessageBoxIcon.Error
        )
        return
    except Exception:
        pass

    # Safe fallback if WinForms message box is unavailable.
    try:
        TaskDialog.Show(TOOL_TITLE, str(message))
    except Exception:
        pass


def compact_failure_message(failed, target_count, changed_total):
    """Build a short user-facing summary; avoid opening pyRevit output."""
    if not failed:
        return None

    lines = [
        "Không thể đổi slope cho toàn bộ đối tượng đã chọn.",
        "",
        "Đã đổi: {0}/{1} Pipe".format(changed_total, target_count),
        "Cụm thất bại / rollback: {0}".format(len(failed)),
    ]

    # Keep the popup deliberately small. Show only the first few failures.
    max_items = 3
    for i, item in enumerate(failed[:max_items]):
        pids, reason = item
        ids_text = ", ".join([str(x) for x in pids[:4]])
        if len(pids) > 4:
            ids_text += ", ..."
        lines.append("")
        lines.append("Pipe [{0}]".format(ids_text))
        lines.append(str(reason))

    if len(failed) > max_items:
        lines.append("")
        lines.append("... và {0} cụm khác.".format(len(failed) - max_items))

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main():
    shift_mode = shift_clicked()

    target_pipe_ids = set()
    transit_candidates = set()
    system_name = ""

    if shift_mode:
        seed = pick_seed_pipe()
        if seed is None:
            return
        try:
            system_name, target_pipe_ids, transit_candidates = collect_shift_network(seed)
        except Exception as ex:
            show_error(str(ex))
            return
        mode_text = "SHIFT mode - Connected System"
        target_text = "System: {0} | Pipes found: {1}".format(
            system_name, len(target_pipe_ids)
        )
    else:
        selected_ids = list(uidoc.Selection.GetElementIds())
        explicit_transits = set()

        for sid in selected_ids:
            elem = doc.GetElement(sid)
            if is_pipe(elem):
                target_pipe_ids.add(eid_value(sid))
            elif is_transit(elem):
                explicit_transits.add(eid_value(sid))

        if not target_pipe_ids:
            show_error(
                "Preselect at least one Pipe, then run the tool.\n\n"
                "Use Shift+Click on the button to pick one Pipe and process "
                "all physically connected Pipes with the same System Name."
            )
            return

        transit_candidates = collect_normal_transit_candidates(
            target_pipe_ids, explicit_transits
        )
        mode_text = "NORMAL mode - Current Selection"
        target_text = "Selected Pipes: {0}".format(len(target_pipe_ids))

    internal_transits = filter_internal_transits(
        target_pipe_ids, transit_candidates
    )
    work_ids = set(target_pipe_ids) | set(internal_transits)

    slopes = get_project_slopes()
    if not slopes:
        show_error(
            "No slope values were found in Project Pipe Settings > Slopes."
        )
        return

    dlg = SlopeDialog(slopes, load_last_slope(), mode_text, target_text)
    result = dlg.ShowDialog()
    if result != DialogResult.OK or dlg.selected_value is None:
        return

    slope_percent = float(dlg.selected_value)
    slope_ratio = slope_percent / 100.0
    save_last_slope(slope_percent)

    graph = build_graph(work_ids)
    comps = graph_components(graph)

    changed_total = 0
    success_components = 0
    failed = []

    t = DB.Transaction(doc, "Change Pipe Slope")
    t.Start()
    try:
        for comp in comps:
            pids = component_pipe_ids(comp)
            if not pids:
                continue

            ok, reason, changed = process_component(graph, comp, slope_ratio)
            if ok:
                success_components += 1
                changed_total += changed
            else:
                failed.append((sorted(list(pids)), reason))

        t.Commit()
    except Exception as ex:
        try:
            t.RollBack()
        except Exception:
            pass
        show_error("Transaction failed:\n{0}".format(ex))
        return

    # Select successfully targeted Pipes again for convenience.
    try:
        ids = List[DB.ElementId]()
        for pid in sorted(target_pipe_ids):
            ids.Add(DB.ElementId(pid))
        uidoc.Selection.SetElementIds(ids)
    except Exception:
        pass

    # Full success is intentionally silent.
    # If at least one component failed/rolled back, show one compact message.
    if failed:
        message = compact_failure_message(
            failed,
            len(target_pipe_ids),
            changed_total
        )
        if message:
            show_error(message)



if __name__ == "__main__":
    main()
