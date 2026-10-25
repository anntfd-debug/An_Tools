# -*- coding: utf-8 -*-
"""Drainage Schematic Generator
Generate a hydraulic drainage schematic (SD / SV / VP ...) in a drafting view
from the pipe network visible in the ACTIVE VIEW.

Source:
  1. Selected / picked system(s)  -> members of those systems visible in active view
  2. All visible in active view   -> use Temporary Isolate on a branch first

Items drawn:
  - Plumbing Fixtures: always.
  - Pipe Accessories / Pipe Fittings: only when the option is ticked AND the
    family type was added to the mapping table with the "+" button.
  - Vent systems (VP, SV, ...) leaving a drain are drawn as a vertical riser
    in the vent colour, in their real position in the flow.

Network grouping:
  - "Physical connection + System Data" (default): networks are traced through
    physical connectors regardless of System Name; each segment keeps its own
    System Data colour. Fixtures never bridge different classifications.
  - "System Name": one network per piping system.

Main pipe:
  - "Horizontal main" (default): ONE horizontal / graded pipe run is the
    reference main. Branches drop onto it and every stack is drawn as a RISER
    standing on that main at its real position in the flow (outlet on the
    right). The reference run is found automatically (follows the straight run
    of each tee through non-vertical pipes) or picked by the user.
  - "Vertical stack": previous behaviour, all branches join one stack.
  - 45 deg joins: every horizontal branch bends 45 deg down into its riser,
    and every riser bends 45 deg into the horizontal main (size = "45° join").

Compatible: Revit 2019 - 2026, pyRevit (IronPython engine).
UI: ui.xaml (WPF) in the same bundle folder.
"""
__title__ = "Drainage\nSchematic"
__author__ = "BIM Hydraulics"

import os
import re
import json
import heapq
import sys
from collections import OrderedDict

try:
    sys.setrecursionlimit(10000)
except Exception:
    pass

import clr
clr.AddReference("System")
clr.AddReference("System.Data")
clr.AddReference("PresentationFramework")
import System
from System.Data import DataTable
from System.Collections.Generic import List
from System.Windows.Controls import DataGridEditingUnit

from Autodesk.Revit import DB
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
import Autodesk.Revit.Exceptions as RvtExc

from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# ----------------------------------------------------------------------------
# CONSTANTS / SETTINGS
# ----------------------------------------------------------------------------
MM = 1.0 / 304.8
NONE_OPT = "<None>"
NO_SYS = "<No System>"
MODE_SYS = "From selected / picked system(s)"
MODE_VIEW = "All visible in active view (isolate branch first)"
VIEW_NEW = "Create new view"
VIEW_OVER = "Overwrite existing view"
ORDER_NET = "Network flow (outlet -> end)"
ORDER_X = "Model X"
ORDER_TYPE = "Type"
NET_PHYS = "Physical connection + System Data"
NET_SYS = "System Name (one network per system)"
MAIN_H_AUTO = "Horizontal main + risers (auto)"
MAIN_H_PICK = "Horizontal main + risers (pick reference pipe)"
MAIN_V = "Vertical stack (all branches to stack)"
ROOT_AUTO = "Auto (lowest open end)"
ROOT_PICK = "Pick outlet element(s)"
LABEL_NAME = "System Name"
LABEL_ABBR = "System Abbreviation"
GROUP_PREFIX = "DrainageSchematic"
VERT_COS = 0.95                 # |dZ|/L >= 0.95 -> vertical
RUN_DOT = -0.9                  # connector directions opposite -> straight run of tee
CLS_PRIORITY = {"Sanitary": 0, "Vent": 1, "OtherPipe": 2}
DRAIN_CLS = ("Sanitary", "Vent", "OtherPipe")
CLS_COLOR = {"Sanitary": (153, 76, 0), "Vent": (255, 128, 0), "OtherPipe": (0, 160, 0)}
DEFAULT_COLOR = (0, 112, 192)

DEFAULTS = {
    "view_mode": VIEW_NEW,
    "target_view": "",
    "new_view_name": "HYD DRAINAGE SCHEMATIC",
    "clear_all": False,
    "group_output": True,
    "scale": 100,
    "order_by": ORDER_NET,
    "net_mode": NET_PHYS,
    "main_mode": MAIN_H_AUTO,
    "hmain_drop_mm": 12.0,
    "root_mode": ROOT_AUTO,
    "stack_min_mm": 2000.0,
    "group_by": LABEL_NAME,
    "show_branch_tag": True,
    "show_xconn": True,
    "use_extras": False,
    "level_method": "Reference Level",
    "level_tol_mm": 300.0,
    "extra_levels_above": 0,
    "story_only": True,
    "from_lowest": True,
    "level_spacing_mm": 40.0,
    "symbol_spacing_mm": 12.0,
    "branch_gap_mm": 8.0,
    "branch_step_mm": 4.0,
    "tier_step_mm": 4.0,
    "upstream_rise_mm": 14.0,
    "chamfer_mm": 2.5,
    "chamfer_stack": True,
    "join_chamfer_mm": 4.0,     # 45 deg segment: branch -> riser and riser -> main
    "stack_gap_mm": 25.0,
    "symbol_offset_mm": 0.0,
    "branch_offset_mm": 5.0,
    "label_offset_mm": 6.0,
    "level_text_width_mm": 45.0,
    "level_line_style": "",
    "text_type": "",
    "combine": False,
    "add_label": True,
    "placeholder": True,
    "vent_to_atm": True,
    "pipe_line_weight": 4,
    "skip_nested": True,
    "riser_abbr": ["VP", "SV", "V", "VENT", "AAV"],
    "checked_systems": [],
}

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "pyRevit", "DrainageSchematic")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
XAML_FILE = os.path.join(os.path.dirname(__file__), "ui.xaml")


def load_settings():
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    for k in ("params", "mapping", "generated"):
        if not isinstance(data.get(k), dict):
            data[k] = {}
    if not isinstance(data.get("extras"), list):
        data["extras"] = []
    p = dict(DEFAULTS)
    p.update(data["params"])
    data["params"] = p
    return data


def save_settings(data):
    try:
        if not os.path.exists(SETTINGS_DIR):
            os.makedirs(SETTINGS_DIR)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as ex:
        output.print_md("**Warning:** cannot save settings: " + str(ex))


# ----------------------------------------------------------------------------
# GENERIC HELPERS
# ----------------------------------------------------------------------------
def id_val(eid):
    """ElementId -> int (Revit 2019-2026)."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def is_valid_id(eid):
    return eid is not None and id_val(eid) > 0


def ename(e):
    try:
        return DB.Element.Name.__get__(e)
    except Exception:
        try:
            return e.Name
        except Exception:
            return "?"


def nat_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def to_float(v, d):
    try:
        return float(str(v).strip())
    except Exception:
        return d


def to_int(v, d):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return d


def sanitize(name):
    return re.sub(r"[\\:{}\[\]|;<>?`~/]", "_", name or "X")


def doc_key():
    try:
        if doc.IsWorkshared:
            mp = doc.GetWorksharingCentralModelPath()
            return DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(mp)
    except Exception:
        pass
    return doc.PathName or doc.Title


def bic_id(bic):
    return id_val(DB.ElementId(bic))


CAT_FIX = bic_id(DB.BuiltInCategory.OST_PlumbingFixtures)
CAT_ACC = bic_id(DB.BuiltInCategory.OST_PipeAccessory)
CAT_FIT = bic_id(DB.BuiltInCategory.OST_PipeFitting)
SCOPE_BICS = [DB.BuiltInCategory.OST_PipeCurves, DB.BuiltInCategory.OST_FlexPipeCurves,
              DB.BuiltInCategory.OST_PipeFitting, DB.BuiltInCategory.OST_PipeAccessory,
              DB.BuiltInCategory.OST_PlumbingFixtures]


def cat_name(el):
    try:
        return el.Category.Name
    except Exception:
        return "?"


def type_of(el):
    try:
        return doc.GetElement(el.GetTypeId())
    except Exception:
        return None


def model_key(el):
    t = type_of(el)
    fam = getattr(t, "FamilyName", None) or "?"
    return u"{} : {}".format(fam, ename(t) if t else "?")


def label_text(el):
    t = type_of(el)
    if t is None:
        return ""
    p = t.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_MARK)
    if p and p.AsString():
        return p.AsString()
    return ename(t)


def curve_pts(el):
    loc = el.Location
    if isinstance(loc, DB.LocationCurve):
        c = loc.Curve
        return c.GetEndPoint(0), c.GetEndPoint(1)
    return None


def elem_pt(el):
    loc = el.Location
    if isinstance(loc, DB.LocationPoint):
        return loc.Point
    cp = curve_pts(el)
    if cp:
        return (cp[0] + cp[1]) * 0.5
    bb = el.get_BoundingBox(None)
    if bb:
        return (bb.Min + bb.Max) * 0.5
    return None


def node_kind(el):
    if isinstance(el, DB.MEPCurve):
        return "pipe"
    try:
        c = id_val(el.Category.Id)
    except Exception:
        return "other"
    return {CAT_FIX: "fix", CAT_ACC: "acc", CAT_FIT: "fit"}.get(c, "other")


# ----------------------------------------------------------------------------
# CONNECTORS / SYSTEMS
# ----------------------------------------------------------------------------
_CONN = {}


def get_connectors(el):
    k = id_val(el.Id)
    if k in _CONN:
        return _CONN[k]
    res = []
    try:
        if isinstance(el, DB.FamilyInstance):
            mm = el.MEPModel
            cm = mm.ConnectorManager if mm is not None else None
        else:
            cm = getattr(el, "ConnectorManager", None)
        if cm is not None:
            for c in cm.Connectors:
                try:
                    if c.Domain != DB.Domain.DomainPiping:
                        continue
                    if c.ConnectorType == DB.ConnectorType.Logical:
                        continue
                except Exception:
                    continue
                res.append(c)
    except Exception:
        pass
    _CONN[k] = res
    return res


def conn_sys(c):
    try:
        return c.MEPSystem
    except Exception:
        return None


def conn_dir(c):
    try:
        return c.CoordinateSystem.BasisZ
    except Exception:
        return None


def conn_at(el, pt):
    if pt is None:
        return None
    best, bd = None, 1e18
    for c in get_connectors(el):
        try:
            d = c.Origin.DistanceTo(pt)
        except Exception:
            continue
        if d < bd:
            best, bd = c, d
    return best


def get_piping_systems(el):
    res = OrderedDict()
    for c in get_connectors(el):
        s = conn_sys(c)
        if s is not None:
            res[id_val(s.Id)] = s
    if isinstance(el, DB.MEPCurve):
        try:
            s = el.MEPSystem
            if s is not None:
                res[id_val(s.Id)] = s
        except Exception:
            pass
    return list(res.values())


class SysData(object):
    """System Data: name, abbreviation, classification, colour."""
    def __init__(self, s):
        self.id = id_val(s.Id)
        self.name = s.Name
        self.abbr = ""
        self.cls = "Undefined"
        self.count = 0
        try:
            self.cls = str(s.SystemType)
        except Exception:
            pass
        st = doc.GetElement(s.GetTypeId())
        rgb = None
        if st is not None:
            p = st.get_Parameter(DB.BuiltInParameter.RBS_SYSTEM_ABBREVIATION_PARAM)
            if p and p.AsString():
                self.abbr = p.AsString()
            try:
                c = st.LineColor
                if c is not None and c.IsValid:
                    rgb = (int(c.Red), int(c.Green), int(c.Blue))
            except Exception:
                rgb = None
        if rgb is None or rgb == (0, 0, 0):
            rgb = CLS_COLOR.get(self.cls, DEFAULT_COLOR)
        self.color = DB.Color(rgb[0], rgb[1], rgb[2])

    @property
    def priority(self):
        return CLS_PRIORITY.get(self.cls, 9)


class SysPickFilter(ISelectionFilter):
    def AllowElement(self, e):
        try:
            return len(get_piping_systems(e)) > 0
        except Exception:
            return False

    def AllowReference(self, ref, pt):
        return False


class IdPickFilter(ISelectionFilter):
    def __init__(self, ids):
        self.ids = ids

    def AllowElement(self, e):
        try:
            return id_val(e.Id) in self.ids
        except Exception:
            return False

    def AllowReference(self, ref, pt):
        return False


# ----------------------------------------------------------------------------
# LEVEL HELPERS
# ----------------------------------------------------------------------------
LEVEL_PARAMS = [DB.BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM,
                DB.BuiltInParameter.FAMILY_LEVEL_PARAM,
                DB.BuiltInParameter.INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM,
                DB.BuiltInParameter.RBS_START_LEVEL_PARAM]


def is_story(lvl):
    p = lvl.get_Parameter(DB.BuiltInParameter.LEVEL_IS_BUILDING_STORY)
    return p is None or p.AsInteger() == 1


def get_levels(story_only):
    lv = list(DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements())
    if story_only:
        st = [l for l in lv if is_story(l)]
        if st:
            lv = st
    return sorted(lv, key=lambda l: l.ProjectElevation)


def ref_level(el):
    lid = None
    try:
        lid = el.LevelId
    except Exception:
        lid = None
    if not is_valid_id(lid):
        lid = None
        for bip in LEVEL_PARAMS:
            try:
                p = el.get_Parameter(bip)
            except Exception:
                p = None
            if p is not None and p.StorageType == DB.StorageType.ElementId:
                v = p.AsElementId()
                if is_valid_id(v):
                    lid = v
                    break
    if lid is None:
        return None
    l = doc.GetElement(lid)
    return l if isinstance(l, DB.Level) else None


def snap_level(elev, levels, tol):
    chosen = levels[0]
    for l in levels:
        if l.ProjectElevation <= elev + tol + 1e-6:
            chosen = l
        else:
            break
    return chosen


def resolve_level(el, levels, method, tol):
    elev = None
    if method == "Elevation (Z)":
        pt = elem_pt(el)
        if pt is not None:
            elev = pt.Z
    if elev is None:
        rl = ref_level(el)
        if rl is not None:
            elev = rl.ProjectElevation
    if elev is None:
        pt = elem_pt(el)
        if pt is not None:
            elev = pt.Z
    if elev is None:
        return None
    return snap_level(elev, levels, tol)


# ----------------------------------------------------------------------------
# COLLECTORS
# ----------------------------------------------------------------------------
def collect_scope(view, skip_nested):
    out = OrderedDict()
    for bic in SCOPE_BICS:
        col = DB.FilteredElementCollector(doc, view.Id).OfCategory(bic) \
            .WhereElementIsNotElementType().ToElements()
        for e in col:
            try:
                if e.IsHidden(view):
                    continue
            except Exception:
                pass
            if skip_nested and isinstance(e, DB.FamilyInstance) and e.SuperComponent is not None:
                continue
            out[id_val(e.Id)] = e
    return out


def collect_symbols():
    res = {}
    for bic in (DB.BuiltInCategory.OST_DetailComponents, DB.BuiltInCategory.OST_GenericAnnotation):
        for s in DB.FilteredElementCollector(doc).OfCategory(bic).OfClass(DB.FamilySymbol):
            res[u"{} : {}".format(s.FamilyName, ename(s))] = s
    return res


def collect_line_styles():
    cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    res = {}
    for sub in cat.SubCategories:
        try:
            gs = sub.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
            if gs is not None:
                res[sub.Name] = gs
        except Exception:
            pass
    return res


def collect_text_types():
    return dict((ename(t), t) for t in DB.FilteredElementCollector(doc).OfClass(DB.TextNoteType))


def collect_drafting_views():
    return dict((v.Name, v) for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewDrafting)
                if not v.IsTemplate)


def get_or_create_line_style(name, color, weight):
    cats = doc.Settings.Categories
    lines_cat = cats.get_Item(DB.BuiltInCategory.OST_Lines)
    if lines_cat.SubCategories.Contains(name):
        sub = lines_cat.SubCategories.get_Item(name)
    else:
        sub = cats.NewSubcategory(lines_cat, name)
        try:
            sub.SetLineWeight(weight, DB.GraphicsStyleType.Projection)
        except Exception:
            pass
    if color is not None:
        try:
            sub.LineColor = color
        except Exception:
            pass
    return sub.GetGraphicsStyle(DB.GraphicsStyleType.Projection)


# ----------------------------------------------------------------------------
# NETWORK (topology)
# ----------------------------------------------------------------------------
class Net(object):
    """Connector graph of ONE piping system (System Name mode).
    adj[k] = [(other, my_origin, their_origin, my_class, their_class)]"""

    def __init__(self, sid, scope, sys_ids, KIND):
        self.sid = sid
        self.nodes = OrderedDict((k, e) for k, e in scope.items() if sid in sys_ids.get(k, ()))
        self.adj, self.open, self.cross, self.nconn = {}, {}, {}, {}
        for k, el in self.nodes.items():
            adj, op, cr = [], [], OrderedDict()
            is_fix = KIND.get(k) == "fix"
            for c in get_connectors(el):
                try:
                    org = c.Origin
                except Exception:
                    continue
                s = conn_sys(c)
                csid = id_val(s.Id) if s is not None else None
                if csid is not None and csid != sid:
                    if not is_fix and csid not in cr:
                        cr[csid] = (s, org)
                    continue
                linked = False
                try:
                    connected = c.IsConnected
                except Exception:
                    connected = False
                if connected:
                    for r in c.AllRefs:
                        try:
                            ow = r.Owner
                            if ow is None or isinstance(ow, DB.MEPSystem):
                                continue
                            if r.ConnectorType == DB.ConnectorType.Logical:
                                continue
                            oid = id_val(ow.Id)
                            if oid == k:
                                continue
                            if oid in self.nodes:
                                adj.append((oid, org, r.Origin, None, None))
                                linked = True
                                continue
                            if is_fix or KIND.get(oid) == "fix":
                                continue
                            rs = conn_sys(r)
                            if rs is not None and id_val(rs.Id) != sid:
                                if id_val(rs.Id) not in cr:
                                    cr[id_val(rs.Id)] = (rs, org)
                                linked = True
                        except Exception:
                            continue
                if not linked:
                    op.append(org)
            self.adj[k] = adj
            self.open[k] = op
            self.cross[k] = [(csid, v[0], v[1]) for csid, v in cr.items()]
            pts = [a[1] for a in adj] + op + [v[1] for v in cr.values()]
            self.nconn[k] = len(set((round(p.X, 4), round(p.Y, 4), round(p.Z, 4)) for p in pts))

    def components(self):
        und = dict((k, set()) for k in self.nodes)
        for k, lst in self.adj.items():
            for a in lst:
                und[k].add(a[0])
                und[a[0]].add(k)
        seen, comps = set(), []
        for k in self.nodes:
            if k in seen:
                continue
            comp, todo = [], [k]
            seen.add(k)
            while todo:
                n = todo.pop()
                comp.append(n)
                for j in und[n]:
                    if j not in seen:
                        seen.add(j)
                        todo.append(j)
            comps.append(comp)
        return comps


class PhysNet(Net):
    """Connector graph by PHYSICAL CONNECTION, independent of System Name."""

    def __init__(self, nodes, KIND, cls_of_conn):
        self.sid = None
        self.nodes = nodes
        self.adj, self.open, self.cross, self.nconn = {}, {}, {}, {}
        for k, el in nodes.items():
            adj, op, cr = [], [], OrderedDict()
            is_fix = KIND.get(k) == "fix"
            for c in get_connectors(el):
                try:
                    org = c.Origin
                except Exception:
                    continue
                cm = cls_of_conn(c)
                linked = False
                try:
                    connected = c.IsConnected
                except Exception:
                    connected = False
                if connected:
                    for r in c.AllRefs:
                        try:
                            ow = r.Owner
                            if ow is None or isinstance(ow, DB.MEPSystem):
                                continue
                            if r.ConnectorType == DB.ConnectorType.Logical:
                                continue
                            oid = id_val(ow.Id)
                            if oid == k:
                                continue
                            if oid in nodes:
                                adj.append((oid, org, r.Origin, cm, cls_of_conn(r)))
                                linked = True
                                continue
                            if is_fix or KIND.get(oid) == "fix":
                                continue
                            rs = conn_sys(r)
                            if rs is not None and id_val(rs.Id) not in cr:
                                cr[id_val(rs.Id)] = (rs, org)
                            linked = True
                        except Exception:
                            continue
                if not linked:
                    op.append(org)
            self.adj[k] = adj
            self.open[k] = op
            self.cross[k] = [(csid, v[0], v[1]) for csid, v in cr.items()]
            pts = [a[1] for a in adj] + op + [v[1] for v in cr.values()]
            self.nconn[k] = len(set((round(q.X, 4), round(q.Y, 4), round(q.Z, 4)) for q in pts))

    def sub(self, keep):
        n = Net.__new__(Net)
        n.sid = None
        n.nodes = OrderedDict((k, e) for k, e in self.nodes.items() if k in keep)
        n.adj = dict((k, [a for a in self.adj[k] if a[0] in keep]) for k in n.nodes)
        n.open = dict((k, self.open[k] + [a[1] for a in self.adj[k] if a[0] not in keep])
                      for k in n.nodes)
        n.cross = dict((k, self.cross[k]) for k in n.nodes)
        n.nconn = dict((k, self.nconn[k]) for k in n.nodes)
        return n


def find_stack_nodes(scope, KIND, VERT, stack_min):
    """STACK = continuous vertical run whose total vertical span >= stack_min."""
    link = set()
    for k, e in scope.items():
        kd = KIND.get(k)
        if kd == "pipe":
            if VERT.get(k):
                link.add(k)
        elif kd in ("fit", "acc"):
            nz = 0
            for c in get_connectors(e):
                d = conn_dir(c)
                if d is not None and abs(d.Z) >= VERT_COS:
                    nz += 1
            if nz >= 2:
                link.add(k)
    parent = dict((k, k) for k in link)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for k in link:
        for c in get_connectors(scope[k]):
            try:
                if not c.IsConnected:
                    continue
                for r in c.AllRefs:
                    ow = r.Owner
                    if ow is None or isinstance(ow, DB.MEPSystem):
                        continue
                    o = id_val(ow.Id)
                    if o != k and o in link:
                        ra, rb = find(k), find(o)
                        if ra != rb:
                            parent[ra] = rb
            except Exception:
                continue
    zr = {}
    for k in link:
        zs = []
        cp = curve_pts(scope[k])
        if cp:
            zs = [cp[0].Z, cp[1].Z]
        else:
            for c in get_connectors(scope[k]):
                try:
                    zs.append(c.Origin.Z)
                except Exception:
                    pass
        if not zs:
            continue
        r = find(k)
        lo, hi = zr.get(r, (1e18, -1e18))
        zr[r] = (min(lo, min(zs)), max(hi, max(zs)))
    res = set()
    for k in link:
        lo, hi = zr.get(find(k), (0.0, 0.0))
        if hi - lo >= stack_min - 1e-6:
            res.add(k)
    return res


def find_root(net, comp, KIND, picked):
    for k in comp:
        if k in picked:
            pts = list(net.open[k]) + [x[2] for x in net.cross[k]]
            return k, (min(pts, key=lambda p: p.Z) if pts else None)
    best = None
    for k in comp:
        if KIND.get(k) == "fix":
            continue
        cands = [(p, 0) for p in net.open[k]] + [(x[2], 1) for x in net.cross[k]]
        for p, pri in cands:
            key = (round(p.Z, 3), pri)
            if best is None or key < best[0]:
                best = (key, k, p)
    if best is not None:
        return best[1], best[2]
    cands = []
    for k in comp:
        if KIND.get(k) == "fix":
            continue
        pt = elem_pt(net.nodes[k])
        if pt is not None:
            cands.append((pt.Z, k))
    return (min(cands)[1], None) if cands else (None, None)


class Tree(object):
    """Shortest-path tree from the outlet."""

    def __init__(self, net, root, root_pt, KIND, STK, TOPZ):
        self.net = net
        self.KIND = KIND
        self.root = root
        self.is_item = lambda k: False
        dist, parent, depth = {root: 0.0}, {root: None}, {root: 0}
        entry, exitp = {root: root_pt}, {root: None}
        ecls = {root: None}
        done, done_set = [], set()
        heap = [(0.0, 0, root)]
        while heap:
            d, _dp, k = heapq.heappop(heap)
            if k in done_set:
                continue
            done_set.add(k)
            done.append(k)
            ein = entry[k]
            gate = KIND.get(k) == "fix" and k != root and ecls[k] is not None
            for j, mine, theirs, cm, ct in net.adj[k]:
                if j in done_set:
                    continue
                if gate and cm is not None and cm != ecls[k]:
                    continue
                w = mine.DistanceTo(ein) if ein is not None else 0.0
                nd = d + w
                if j not in dist or nd < dist[j] - 1e-9:
                    dist[j] = nd
                    parent[j] = k
                    entry[j] = theirs
                    exitp[j] = mine
                    ecls[j] = ct
                    depth[j] = depth[k] + 1
                    heapq.heappush(heap, (nd, depth[j], j))
        self.nodes = done
        self.dist, self.parent, self.entry, self.exitp = dist, parent, entry, exitp
        self.children = dict((k, []) for k in done)
        for k in done:
            p = parent[k]
            if p is not None and p in self.children:
                self.children[p].append(k)

        self.maxd, self.topv = {}, {}
        for k in sorted(done, key=lambda n: (-dist[n], -depth[n])):
            md = dist[k]
            tv = (TOPZ[k], k) if (k in STK and k in TOPZ) else None
            for c in self.children[k]:
                if self.maxd[c] > md:
                    md = self.maxd[c]
                if self.topv[c] is not None and (tv is None or self.topv[c][0] > tv[0]):
                    tv = self.topv[c]
            self.maxd[k], self.topv[k] = md, tv

    def is_host(self, k):
        if not self.is_item(k):
            return False
        kd = self.KIND.get(k)
        if kd == "fix":
            return True
        return kd == "acc" and self.net.nconn.get(k, 0) >= 3

    def is_run(self, k, c):
        el = self.net.nodes[k]
        cin = conn_at(el, self.entry[k])
        cout = conn_at(el, self.exitp[c])
        if cin is None or cout is None:
            return False
        try:
            if cin.Id == cout.Id:
                return False
        except Exception:
            pass
        din, dout = conn_dir(cin), conn_dir(cout)
        if din is None or dout is None:
            return False
        return din.DotProduct(dout) <= RUN_DOT

    def ordered_children(self, k):
        return sorted(self.children.get(k, []),
                      key=lambda c: (self.is_run(k, c), self.maxd[c]))

    def main_path(self, rk):
        """VERTICAL mode: outlet -> stack (follow run, prefer stack)."""
        path, k = [rk], rk
        guard = set([rk])
        while True:
            ch = [c for c in self.children.get(k, []) if c not in guard]
            if not ch:
                break
            if len(ch) == 1:
                nxt = ch[0]
            else:
                nxt = max(ch, key=lambda c: (self.topv[c] is not None,
                                             self.is_run(k, c),
                                             self.maxd[c]))
            path.append(nxt)
            guard.add(nxt)
            k = nxt
            if self.is_host(k) and self.topv[k] is None:
                break
        return path

    def hmain_path(self, rk, stk, ref=None):
        """HORIZONTAL mode: reference main through horizontal / graded pipes only.
        If a reference pipe is given, the main passes through it (outlet -> ref)
        and continues along the run beyond it. Never enters a stack."""
        path = []
        if ref is not None and ref in self.parent:
            k = ref
            while k is not None:
                path.append(k)
                k = self.parent[k]
            path.reverse()
        else:
            path = [rk]
        guard = set(path)
        k = path[-1]
        while not self.is_host(k) or k == rk:
            ch = [c for c in self.children.get(k, []) if c not in guard and c not in stk]
            if not ch:
                break
            if len(ch) == 1:
                nxt = ch[0]
            else:
                nxt = max(ch, key=lambda c: (self.is_run(k, c), self.maxd[c]))
            path.append(nxt)
            guard.add(nxt)
            k = nxt
        return path


# ----------------------------------------------------------------------------
# SCHEMATIC MODEL
# ----------------------------------------------------------------------------
class SNode(object):
    """kind 'item' / 'grp' / 'conn' (riser or system change point)."""
    def __init__(self, kind, k=None, children=None):
        self.kind = kind
        self.k = k
        self.children = children or []
        self.key = ""
        self.label = ""
        self.count = 1
        self.sid = None
        self.lid = None
        self.trans = False
        self.riser = False
        self.switch = False


def flat_items(nodes):
    out = []
    for n in nodes:
        if n.kind == "item":
            out.append(n.k)
        out.extend(flat_items(n.children))
    return out


def flat_count(nodes):
    c = 0
    for n in nodes:
        if n.kind == "item":
            c += n.count
        c += flat_count(n.children)
    return c


def tier_depth(nodes):
    d = 0
    for n in nodes:
        if n.kind == "grp":
            d = max(d, 1 + tier_depth(n.children))
    return d


def combine_nodes(nodes):
    out = []
    for n in nodes:
        n.children = combine_nodes(n.children)
        if (out and n.kind == "item" and not n.children and out[-1].kind == "item"
                and not out[-1].children and out[-1].key == n.key):
            out[-1].count += n.count
        else:
            out.append(n)
    return out


def seq_text(nodes):
    parts = []
    for n in nodes:
        if n.kind == "item":
            t = n.label + (u" x" + str(n.count) if n.count > 1 else u"")
            if n.children:
                t += u" [" + seq_text(n.children) + u"]"
        elif n.kind == "conn":
            t = u"<" + n.label + (u"^" if n.riser else u"") + u">"
        else:
            t = u"{" + seq_text(n.children) + u"}"
        parts.append(t)
    return u" > ".join(parts)


def edge_sid(ctx, k, sid_in):
    if ctx.kind.get(k) == "fix":
        return sid_in
    v = ctx.esys.get(k)
    return v if v is not None else sid_in


def trans_marker(ctx, k, sid_from, sid_to, body):
    if not ctx.show_trans or sid_to is None or sid_from is None or sid_to == sid_from:
        return []
    n = SNode("conn", k)
    n.sid = sid_to
    n.trans = True
    n.label = ctx.sys_short(sid_to)
    n.key = u"trans:" + str(sid_to)
    n.lid = ctx.level_of_k(k)
    n.switch = bool(body)
    n.riser = (not body) or ctx.is_vent(sid_to)
    return [n]


def build_nodes(tree, k, ctx, sid_in=None):
    cur = edge_sid(ctx, k, sid_in)
    ch = tree.ordered_children(k)
    me = []

    def sub(c):
        cs = edge_sid(ctx, c, cur)
        body = build_nodes(tree, c, ctx, cur)
        return trans_marker(ctx, k, cur, cs, body) + body

    if ctx.is_item(k) and ctx.item_level.get(k) is not None:
        node = ctx.mk_item(k)
        node.sid = cur
        if tree.is_host(k):
            node.children.extend(ctx.mk_conns(k))
            for c in ch:
                node.children.extend(sub(c))
            return [node]
        me.append(node)
    me.extend(ctx.mk_conns(k))
    parts = [p for p in (sub(c) for c in ch) if p]
    if len(parts) >= 2:
        out = list(me)
        for p in parts[:-1]:
            if len(p) == 1:
                out.append(p[0])
            else:
                g = SNode("grp", children=p)
                if p[0].kind == "conn" and p[0].switch:
                    g.sid = cur
                else:
                    g.sid = next((x.sid for x in p if x.sid is not None), cur)
                out.append(g)
        return out + parts[-1]
    return me + (parts[0] if parts else [])


class Branch(object):
    def __init__(self, nodes, key, lid, sub=None):
        self.nodes = nodes
        self.key = key
        self.lid = lid
        self.sub = sub
        self.no = 0
        self.y = 0.0
        self.sid = None


class Stack(object):
    """Vertical stack (VERTICAL mode) or riser standing on a horizontal main."""
    def __init__(self, label, meta, kind="net"):
        self.label = label
        self.meta = meta
        self.kind = kind            # net / simple / loose / hmain
        self.branches = []
        self.inline = []
        self.by_level = OrderedDict()
        self.bottom_y = None
        self.is_root = True
        self.x0 = 0.0
        self.stack_x = 0.0
        self.sid = None
        # horizontal main only
        self.slots = []             # [(key, "b", Branch) | (key, "r", Stack)]
        self.lid = None
        self.risers = []
        self.y_main = 0.0
        self.x_out = 0.0


class Ctx(object):
    pass


def node_lids(nodes, ctx):
    out = []
    for n in nodes:
        if n.kind == "item" and ctx.item_level.get(n.k) is not None:
            out.append(ctx.item_level[n.k])
        elif n.kind == "conn" and n.lid is not None:
            out.append(n.lid)
        out.extend(node_lids(n.children, ctx))
    return out


def analyse(tree, rk, label, meta, ctx):
    """VERTICAL stack: every tee on the main with item(s) on its side = 1 branch."""
    st = Stack(label, meta, "net")
    main = tree.main_path(rk)
    mset = set(main)
    subn = 0
    msids = [ctx.esys.get(t) for t in main if ctx.esys.get(t) is not None]
    st.sid = max(set(msids), key=msids.count) if msids else ctx.default_sid
    cur = st.sid
    for t in main:
        prev = cur
        cur = edge_sid(ctx, t, cur)
        own = trans_marker(ctx, t, prev, cur, [])
        for n in own:
            n.switch = False
            n.riser = ctx.is_vent(n.sid)
        if ctx.is_item(t) and ctx.item_level.get(t) is not None:
            if tree.is_host(t):
                nodes = own + build_nodes(tree, t, ctx, cur)
                br = Branch(nodes, (tree.dist[t], 1e9, 0.0), ctx.item_level[t])
                br.sid = cur
                st.branches.append(br)
                continue
            it = ctx.mk_item(t)
            it.sid = cur
            own.append(it)
        own.extend(ctx.mk_conns(t))
        for n in own:
            if n.sid is None:
                n.sid = cur
        own = [n for n in own if node_lids([n], ctx)]
        if own:
            if t in ctx.stk:
                for n in own:
                    st.inline.append((n, node_lids([n], ctx)[0]))
            else:
                br = Branch(own, (tree.dist[t], -1.0, 0.0), node_lids(own, ctx)[0])
                br.sid = cur
                st.branches.append(br)
        for c in tree.ordered_children(t):
            if c in mset:
                continue
            csid = edge_sid(ctx, c, cur)
            body = build_nodes(tree, c, ctx, cur)
            nodes = trans_marker(ctx, t, cur, csid, body) + body
            lids = node_lids(nodes, ctx)
            if not lids:
                continue
            ilids = [ctx.item_level[x] for x in flat_items(nodes)
                     if ctx.item_level.get(x) is not None]
            ep = tree.entry[c]
            ez = ep.Z if ep is not None else 0.0
            key = (tree.dist[t], tree.dist[c], ez)
            if tree.topv[c] is not None and len(set(ilids)) > 1:
                subn += 1
                sub = analyse(tree, c, label + u"." + str(subn), meta, ctx)
                sub.is_root = False
                br = Branch([], key, ctx.level_of_z(ez), sub)
                br.sid = csid
                st.branches.append(br)
            else:
                if len(nodes) == 1 and nodes[0].kind == "grp":
                    nodes = nodes[0].children
                pick = ilids or lids
                br = Branch(nodes, key, max(set(pick), key=pick.count))
                br.sid = cur if (nodes and nodes[0].kind == "conn" and nodes[0].switch) else csid
                st.branches.append(br)
    st.branches.sort(key=lambda b: b.key)
    for i, b in enumerate(st.branches):
        b.no = i + 1
    return st


def analyse_h(tree, rk, label, meta, ctx, ref=None):
    """HORIZONTAL main: one horizontal / graded run = reference main.
    - side outlet whose sub-tree contains a stack -> RISER standing on the main
      (drawn as a vertical stack with its own branches per level)
    - other side outlets / items on the main      -> branches dropping onto it
    Returns None when the network has no horizontal main (pure stack)."""
    main = tree.hmain_path(rk, ctx.stk, ref)
    if not any(ctx.kind.get(t) == "pipe" and t not in ctx.stk for t in main):
        return None
    hm = Stack(label, meta, "hmain")
    mset = set(main)
    msids = [ctx.esys.get(t) for t in main if ctx.esys.get(t) is not None]
    hm.sid = max(set(msids), key=msids.count) if msids else ctx.default_sid
    zs = [ctx.zof(t) for t in main if ctx.kind.get(t) == "pipe" and ctx.zof(t) is not None]
    zs.sort()
    hm.lid = ctx.level_of_z(zs[len(zs) // 2]) if zs else ctx.level_of_k(rk)
    cur = hm.sid
    rn = 0
    for t in main:
        prev = cur
        cur = edge_sid(ctx, t, cur)
        own = trans_marker(ctx, t, prev, cur, [])
        for n in own:
            n.switch = False
            n.riser = ctx.is_vent(n.sid)
        if ctx.is_item(t) and ctx.item_level.get(t) is not None:
            if tree.is_host(t):
                nodes = own + build_nodes(tree, t, ctx, cur)
                br = Branch(nodes, (tree.dist[t], 1e9, 0.0), ctx.item_level[t])
                br.sid = cur
                hm.slots.append((br.key, "b", br))
                continue
            it = ctx.mk_item(t)
            it.sid = cur
            own.append(it)
        own.extend(ctx.mk_conns(t))
        for n in own:
            if n.sid is None:
                n.sid = cur
        own = [n for n in own if node_lids([n], ctx)]
        if own:
            br = Branch(own, (tree.dist[t], -1.0, 0.0), node_lids(own, ctx)[0])
            br.sid = cur
            hm.slots.append((br.key, "b", br))
        for c in tree.ordered_children(t):
            if c in mset:
                continue
            csid = edge_sid(ctx, c, cur)
            ep = tree.entry[c]
            ez = ep.Z if ep is not None else 0.0
            key = (tree.dist[t], tree.dist[c], ez)
            if tree.topv[c] is not None:
                sub = analyse(tree, c, u"", meta, ctx)
                if not sub.branches and not sub.inline:
                    continue
                rn += 1
                sub.label = (ctx.sys_short(sub.sid) if sub.sid is not None else u"R") + \
                    u" R" + str(rn)
                sub.is_root = False
                sub.vent_top = ctx.is_vent(sub.sid)
                hm.slots.append((key, "r", sub))
                hm.risers.append(sub)
                continue
            body = build_nodes(tree, c, ctx, cur)
            nodes = trans_marker(ctx, t, cur, csid, body) + body
            lids = node_lids(nodes, ctx)
            if not lids:
                continue
            ilids = [ctx.item_level[x] for x in flat_items(nodes)
                     if ctx.item_level.get(x) is not None]
            if len(nodes) == 1 and nodes[0].kind == "grp":
                nodes = nodes[0].children
            pick = ilids or lids
            br = Branch(nodes, key, max(set(pick), key=pick.count))
            br.sid = cur if (nodes and nodes[0].kind == "conn" and nodes[0].switch) else csid
            hm.slots.append((key, "b", br))
    hm.slots.sort(key=lambda s: s[0])
    no = 0
    for _k, kind, obj in hm.slots:
        if kind == "b":
            no += 1
            obj.no = no
            hm.branches.append(obj)
    return hm


def iter_stacks(st):
    yield st
    for b in st.branches:
        if b.sub is not None:
            for s in iter_stacks(b.sub):
                yield s
    for r in st.risers:
        for s in iter_stacks(r):
            yield s


# ----------------------------------------------------------------------------
# UI  (WPF - layout defined in ui.xaml, same folder as this script)
# ----------------------------------------------------------------------------
def _new_table(cols):
    dt = DataTable()
    for name, typ in cols:
        dt.Columns.Add(name, clr.GetClrType(typ))
    return dt


class SchematicWindow(forms.WPFWindow):
    def __init__(self, P, sys_rows, map_rows, extra_cands, saved_map,
                 sym_options, view_names, ls_names, tt_names):
        forms.WPFWindow.__init__(self, XAML_FILE)
        self.P = dict(P)
        self.sym_options = sym_options
        self.opt_set = set(sym_options)
        self.view_names = view_names
        self.extra_cands = extra_cands
        self.saved_map = saved_map
        self.mapping = {}
        self.extras = []
        self.checked = []
        self.ok = False

        over = P["view_mode"] == VIEW_OVER and len(view_names) > 0
        self.rb_over.IsChecked = over
        self.rb_new.IsChecked = not over
        self.rb_over.IsEnabled = len(view_names) > 0
        self._fill(self.cb_view, view_names, P["target_view"])
        self.tb_vname.Text = str(P["new_view_name"])
        self.ck_clear_all.IsChecked = bool(P["clear_all"])
        self.ck_group.IsChecked = bool(P["group_output"])
        self.tb_scale.Text = str(P["scale"])

        self._fill(self.cb_order, [ORDER_NET, ORDER_X, ORDER_TYPE], P["order_by"])
        self._fill(self.cb_netmode, [NET_PHYS, NET_SYS], P["net_mode"])
        self._fill(self.cb_mainmode, [MAIN_H_AUTO, MAIN_H_PICK, MAIN_V], P["main_mode"])
        self.tb_hdrop.Text = str(P["hmain_drop_mm"])
        self._fill(self.cb_root, [ROOT_AUTO, ROOT_PICK], P["root_mode"])
        self.tb_stackmin.Text = str(P["stack_min_mm"])
        self._fill(self.cb_group, [LABEL_NAME, LABEL_ABBR], P["group_by"])
        self.ck_btag.IsChecked = bool(P["show_branch_tag"])
        self.ck_xconn.IsChecked = bool(P["show_xconn"])

        self._fill(self.cb_lmethod, ["Reference Level", "Elevation (Z)"], P["level_method"])
        self.tb_tol.Text = str(P["level_tol_mm"])
        self.tb_extra.Text = str(P["extra_levels_above"])
        self.ck_story.IsChecked = bool(P["story_only"])
        self.ck_lowest.IsChecked = bool(P["from_lowest"])

        self.tb_lsp.Text = str(P["level_spacing_mm"])
        self.tb_ssp.Text = str(P["symbol_spacing_mm"])
        self.tb_bgap.Text = str(P["branch_gap_mm"])
        self.tb_bstep.Text = str(P["branch_step_mm"])
        self.tb_tstep.Text = str(P["tier_step_mm"])
        self.tb_rise.Text = str(P["upstream_rise_mm"])
        self.tb_chamfer.Text = str(P["chamfer_mm"])
        self.ck_chstack.IsChecked = bool(P["chamfer_stack"])
        self.tb_jch.Text = str(P.get("join_chamfer_mm", 4.0))
        self.tb_gap.Text = str(P["stack_gap_mm"])
        self.tb_soff.Text = str(P["symbol_offset_mm"])
        self.tb_boff.Text = str(P["branch_offset_mm"])
        self.tb_loff.Text = str(P["label_offset_mm"])
        self.tb_ltw.Text = str(P["level_text_width_mm"])

        ls_default = P["level_line_style"] or next((n for n in ls_names if "Thin" in n), "")
        self._fill(self.cb_lstyle, ls_names, ls_default)
        self._fill(self.cb_ttype, tt_names, P["text_type"])
        self.ck_combine.IsChecked = bool(P["combine"])
        self.ck_label.IsChecked = bool(P["add_label"])
        self.ck_ph.IsChecked = bool(P["placeholder"])
        self.ck_vent.IsChecked = bool(P["vent_to_atm"])

        self.dt_sys = _new_table([("Name", System.String), ("Label", System.String),
                                  ("Checked", System.Boolean)])
        for name, lbl, chk in sys_rows:
            self.dt_sys.Rows.Add(name, lbl, bool(chk))
        self.lst_systems.ItemsSource = self.dt_sys.DefaultView

        self.dt_map = _new_table([("Category", System.String), ("ModelKey", System.String),
                                  ("Qty", System.Int32), ("Symbol", System.String),
                                  ("IsExtra", System.Boolean)])
        for cat, key, cnt, val, extra in map_rows:
            self._add_row(cat, key, cnt, val, extra)
        self.grid_map.Columns[3].ItemsSource = List[System.String](sym_options)
        self.grid_map.ItemsSource = self.dt_map.DefaultView
        self.ck_extra.IsChecked = bool(P["use_extras"])

        self.rb_new.Checked += self.on_vmode
        self.rb_over.Checked += self.on_vmode
        self.cb_order.SelectionChanged += self.on_order
        self.cb_mainmode.SelectionChanged += self.on_order
        self.btn_all.Click += self.on_all
        self.btn_none.Click += self.on_none
        self.btn_auto.Click += self.on_auto
        self.btn_clear.Click += self.on_clear
        self.btn_add.Click += self.on_add
        self.btn_rem.Click += self.on_rem
        self.ck_extra.Checked += self.on_extra
        self.ck_extra.Unchecked += self.on_extra
        self.btn_run.Click += self.on_run
        self.btn_cancel.Click += self.on_cancel
        self.tb_filter.TextChanged += self.on_filter
        self.grid_map.CellEditEnding += self.on_cell_edit
        self.on_vmode(None, None)
        self.on_order(None, None)
        self.on_extra(None, None)

    @staticmethod
    def _fill(cb, items, value):
        cb.Items.Clear()
        for it in items:
            cb.Items.Add(it)
        if value in items:
            cb.SelectedItem = value
        elif cb.Items.Count:
            cb.SelectedIndex = 0

    @staticmethod
    def _sel(cb):
        return str(cb.SelectedItem) if cb.SelectedItem is not None else ""

    def _add_row(self, cat, key, cnt, val, extra):
        self.dt_map.Rows.Add(cat, key, int(cnt), val if val in self.opt_set else NONE_OPT,
                             bool(extra))

    def _keys_in_grid(self):
        return set(str(r["ModelKey"]) for r in self.dt_map.Rows)

    def _commit(self):
        try:
            self.grid_map.CommitEdit(DataGridEditingUnit.Row, True)
        except Exception:
            pass
        for dt in (self.dt_map, self.dt_sys):
            for r in dt.Rows:
                try:
                    r.EndEdit()
                except Exception:
                    pass

    def _apply_filter(self):
        conds = []
        if not self.ck_extra.IsChecked:
            conds.append("IsExtra = false")
        txt = (self.tb_filter.Text or "").replace("'", "''").replace("[", "[[]") \
            .replace("*", "[*]").replace("%", "[%]")
        if txt:
            conds.append("(ModelKey LIKE '%" + txt + "%' OR Category LIKE '%" + txt +
                         "%' OR Symbol LIKE '%" + txt + "%')")
        self.dt_map.DefaultView.RowFilter = " AND ".join(conds)

    def _update_status(self):
        rows = list(self.dt_map.Rows)
        fx = [r for r in rows if not bool(r["IsExtra"])]
        ex = [r for r in rows if bool(r["IsExtra"])]
        use = bool(self.ck_extra.IsChecked)
        unm = [r for r in (rows if use else fx) if str(r["Symbol"]) in ("", NONE_OPT)]
        self.txt_status.Text = (str(len(fx)) + u" fixture types  |  " + str(len(ex)) +
                                u" accessory/fitting types" + (u"" if use else u" (off)") +
                                u"  |  " + str(len(unm)) + u" unmapped")

    def on_vmode(self, sender, args):
        over = bool(self.rb_over.IsChecked)
        self.cb_view.IsEnabled = over
        self.ck_clear_all.IsEnabled = over
        self.tb_vname.IsEnabled = not over

    def on_order(self, sender, args):
        net = self._sel(self.cb_order) == ORDER_NET
        for c in (self.cb_root, self.tb_stackmin, self.ck_btag, self.ck_xconn,
                  self.cb_netmode, self.cb_mainmode):
            c.IsEnabled = net
        self.tb_hdrop.IsEnabled = net and self._sel(self.cb_mainmode) != MAIN_V

    def on_extra(self, sender, args):
        use = bool(self.ck_extra.IsChecked)
        self.btn_add.IsEnabled = use
        self.btn_rem.IsEnabled = use
        self._apply_filter()
        self._update_status()

    def on_add(self, sender, args):
        self._commit()
        have = self._keys_in_grid()
        disp = OrderedDict()
        for key in sorted(self.extra_cands.keys(),
                          key=lambda k: (self.extra_cands[k][0], nat_key(k))):
            if key in have:
                continue
            cat, qty = self.extra_cands[key]
            disp[cat + u"  |  " + key + u"   (" + str(qty) + u")"] = key
        if not disp:
            forms.alert("All pipe accessory / fitting types in scope are already in the table.")
            return
        res = forms.SelectFromList.show(list(disp.keys()), title="Add pipe accessories / fittings",
                                        multiselect=True, button_name="Add",
                                        width=760, height=560)
        if not res:
            return
        for d in res:
            key = disp.get(d)
            if key is None:
                continue
            cat, qty = self.extra_cands[key]
            self._add_row(cat, key, qty, self.saved_map.get(key, NONE_OPT), True)
        self._apply_filter()
        self._update_status()

    def on_rem(self, sender, args):
        self._commit()
        rows = []
        for drv in list(self.grid_map.SelectedItems):
            try:
                if bool(drv.Row["IsExtra"]):
                    rows.append(drv.Row)
            except Exception:
                pass
        if not rows:
            forms.alert("Select accessory / fitting rows to remove (fixture rows cannot be removed).")
            return
        for r in rows:
            self.dt_map.Rows.Remove(r)
        self._update_status()

    def on_all(self, sender, args):
        for r in self.dt_sys.Rows:
            r["Checked"] = True

    def on_none(self, sender, args):
        for r in self.dt_sys.Rows:
            r["Checked"] = False

    def on_filter(self, sender, args):
        self._apply_filter()

    def on_cell_edit(self, sender, args):
        self.Dispatcher.BeginInvoke(System.Action(self._update_status))

    def on_clear(self, sender, args):
        for drv in list(self.grid_map.SelectedItems):
            try:
                drv.Row["Symbol"] = NONE_OPT
            except Exception:
                pass
        self._update_status()

    def on_auto(self, sender, args):
        self._commit()
        for row in self.dt_map.Rows:
            if str(row["Symbol"]) not in ("", NONE_OPT):
                continue
            parts = str(row["ModelKey"]).split(" : ")
            mfam, mtype = parts[0].lower(), parts[-1].lower()
            hit = None
            for o in self.sym_options[1:]:
                ofam, otype = (o.split(" : ") + [""])[:2]
                if otype.lower() == mtype or ofam.lower() == mtype:
                    hit = o
                    break
            if hit is None:
                for o in self.sym_options[1:]:
                    ofam = o.split(" : ")[0].lower()
                    if (mtype and mtype in ofam) or (mfam and mfam in ofam):
                        hit = o
                        break
            if hit:
                row["Symbol"] = hit
        self._update_status()

    def on_cancel(self, sender, args):
        self.ok = False
        self.Close()

    def on_run(self, sender, args):
        self._commit()
        P = self.P
        P["view_mode"] = VIEW_OVER if self.rb_over.IsChecked else VIEW_NEW
        P["target_view"] = self._sel(self.cb_view)
        if P["view_mode"] == VIEW_OVER and P["target_view"] not in self.view_names:
            forms.alert("Select an existing drafting view to overwrite.")
            return
        P["new_view_name"] = self.tb_vname.Text.strip() or DEFAULTS["new_view_name"]
        P["clear_all"] = bool(self.ck_clear_all.IsChecked)
        P["group_output"] = bool(self.ck_group.IsChecked)
        P["scale"] = max(1, to_int(self.tb_scale.Text, 100))
        P["order_by"] = self._sel(self.cb_order)
        P["net_mode"] = self._sel(self.cb_netmode)
        P["main_mode"] = self._sel(self.cb_mainmode)
        P["hmain_drop_mm"] = max(0.0, to_float(self.tb_hdrop.Text, 12.0))
        P["root_mode"] = self._sel(self.cb_root)
        P["stack_min_mm"] = max(100.0, to_float(self.tb_stackmin.Text, 2000.0))
        P["group_by"] = self._sel(self.cb_group)
        P["show_branch_tag"] = bool(self.ck_btag.IsChecked)
        P["show_xconn"] = bool(self.ck_xconn.IsChecked)
        P["use_extras"] = bool(self.ck_extra.IsChecked)
        P["level_method"] = self._sel(self.cb_lmethod)
        P["level_tol_mm"] = to_float(self.tb_tol.Text, 300.0)
        P["extra_levels_above"] = max(0, to_int(self.tb_extra.Text, 0))
        P["story_only"] = bool(self.ck_story.IsChecked)
        P["from_lowest"] = bool(self.ck_lowest.IsChecked)
        P["level_spacing_mm"] = to_float(self.tb_lsp.Text, 40.0)
        P["symbol_spacing_mm"] = to_float(self.tb_ssp.Text, 12.0)
        P["branch_gap_mm"] = to_float(self.tb_bgap.Text, 8.0)
        P["branch_step_mm"] = to_float(self.tb_bstep.Text, 4.0)
        P["tier_step_mm"] = max(0.5, to_float(self.tb_tstep.Text, 4.0))
        P["upstream_rise_mm"] = max(1.0, to_float(self.tb_rise.Text, 14.0))
        P["chamfer_mm"] = max(0.0, to_float(self.tb_chamfer.Text, 2.5))
        P["chamfer_stack"] = bool(self.ck_chstack.IsChecked)
        P["join_chamfer_mm"] = max(0.0, to_float(self.tb_jch.Text, 4.0))
        P["stack_gap_mm"] = to_float(self.tb_gap.Text, 25.0)
        P["symbol_offset_mm"] = to_float(self.tb_soff.Text, 0.0)
        P["branch_offset_mm"] = to_float(self.tb_boff.Text, 5.0)
        P["label_offset_mm"] = to_float(self.tb_loff.Text, 6.0)
        P["level_text_width_mm"] = to_float(self.tb_ltw.Text, 45.0)
        P["level_line_style"] = self._sel(self.cb_lstyle)
        P["text_type"] = self._sel(self.cb_ttype)
        P["combine"] = bool(self.ck_combine.IsChecked)
        P["add_label"] = bool(self.ck_label.IsChecked)
        P["placeholder"] = bool(self.ck_ph.IsChecked)
        P["vent_to_atm"] = bool(self.ck_vent.IsChecked)

        self.mapping, self.extras = {}, []
        for r in self.dt_map.Rows:
            key = str(r["ModelKey"])
            v = r["Symbol"]
            self.mapping[key] = str(v) if v not in (None, System.DBNull.Value) else NONE_OPT
            if bool(r["IsExtra"]):
                self.extras.append(key)
        self.checked = [str(r["Name"]) for r in self.dt_sys.Rows if bool(r["Checked"])]
        if not self.checked:
            forms.alert("Tick at least one system.")
            return
        self.ok = True
        self.Close()


# ----------------------------------------------------------------------------
# DRAWING
# ----------------------------------------------------------------------------
class Drawer(object):
    def __init__(self, view, text_type):
        self.view = view
        self.scale = view.Scale
        self.tt = text_type
        self.ids = []
        self.tol = doc.Application.ShortCurveTolerance
        self.text_h = 2.5 * MM * self.scale
        if text_type is not None:
            p = text_type.get_Parameter(DB.BuiltInParameter.TEXT_SIZE)
            if p:
                self.text_h = p.AsDouble() * self.scale

    def pm(self, mm):
        return mm * MM * self.scale

    def line(self, x1, y1, x2, y2, gs):
        p1 = DB.XYZ(x1, y1, 0)
        p2 = DB.XYZ(x2, y2, 0)
        if p1.DistanceTo(p2) < self.tol:
            return None
        c = doc.Create.NewDetailCurve(self.view, DB.Line.CreateBound(p1, p2))
        if gs is not None:
            try:
                c.LineStyle = gs
            except Exception:
                pass
        self.ids.append(c.Id)
        return c

    def dot(self, x, y, r, gs):
        try:
            arc = DB.Arc.Create(DB.Plane.CreateByNormalAndOrigin(DB.XYZ.BasisZ, DB.XYZ(x, y, 0)),
                                r, 0.0, 2 * 3.141592653589793)
            c = doc.Create.NewDetailCurve(self.view, arc)
            if gs is not None:
                c.LineStyle = gs
            self.ids.append(c.Id)
        except Exception:
            pass

    def text(self, x, y, s, center=False):
        if self.tt is None or not s:
            return None
        opts = DB.TextNoteOptions(self.tt.Id)
        opts.HorizontalAlignment = (DB.HorizontalTextAlignment.Center if center
                                    else DB.HorizontalTextAlignment.Left)
        tn = DB.TextNote.Create(doc, self.view.Id, DB.XYZ(x, y, 0), s, opts)
        self.ids.append(tn.Id)
        return tn

    def symbol(self, sym, x, y):
        if not sym.IsActive:
            sym.Activate()
            doc.Regenerate()
        inst = doc.Create.NewFamilyInstance(DB.XYZ(x, y, 0), sym, self.view)
        self.ids.append(inst.Id)
        return inst


class Layout(object):
    def __init__(self, dr, P, ly, top_y, sym_map, conn_gs):
        self.dr, self.P, self.ly, self.top_y = dr, P, ly, top_y
        self.sym_map, self.conn_gs = sym_map, conn_gs
        pm = dr.pm
        self.sp = pm(P["symbol_spacing_mm"])
        self.bgap = pm(P["branch_gap_mm"])
        self.bstep = pm(P["branch_step_mm"])
        self.tstep = pm(P["tier_step_mm"])
        self.rise = pm(P["upstream_rise_mm"])
        self.lsp = pm(P["level_spacing_mm"])
        self.ch = min(pm(P["chamfer_mm"]), 0.4 * self.sp)
        self.ch_stack = bool(P["chamfer_stack"]) and self.ch > dr.tol
        # 45 deg join (branch -> riser, riser foot -> horizontal main)
        self.jch = pm(P.get("join_chamfer_mm", 4.0)) if self.ch_stack else 0.0
        self.stats = {"placed": 0, "placeholder": 0, "skipped": 0, "conn": 0, "riser": 0}

    def g(self, sid, default):
        if sid is None:
            return default
        return self.conn_gs.get(sid, default)

    # ---------- geometry ----------
    def up_items(self, n):
        out = []
        for c in n.children:
            if c.kind in ("item", "conn"):
                out.append(c)
            else:
                out.extend(self.up_items(c))
        return out

    def w_node(self, n):
        if n.kind == "conn":
            return self.sp
        if n.kind == "item":
            ups = self.up_items(n)
            return max(self.sp, sum(self.w_node(u) for u in ups))
        return self.w_list(n.children) + self.tstep

    def w_list(self, nodes):
        return sum(self.w_node(n) for n in nodes)

    def bwidth(self, b):
        return (self.sp if b.sub is not None else self.w_list(b.nodes)) + self.bgap

    def col_width(self, st):
        w = self.sp
        for brs in st.by_level.values():
            w = max(w, sum(self.bwidth(b) for b in brs))
        return w

    def slots(self, st):
        by = OrderedDict()
        for b in st.branches:
            if b.lid in self.ly:
                by.setdefault(b.lid, []).append(b)
        for lid, brs in by.items():
            ordered = [b for b in brs if b.sub is not None] + [b for b in brs if b.sub is None]
            cur = self.ly[lid] - self.dr.pm(self.P["branch_offset_mm"])
            for b in reversed(ordered):
                depth = 0 if b.sub is not None else tier_depth(b.nodes)
                b.y = cur - depth * self.tstep
                cur = b.y - self.bstep - self.jch
            by[lid] = ordered
            for b in ordered:
                if b.sub is not None:
                    b.sub.bottom_y = b.y
                    self.slots(b.sub)
        st.by_level = by

    def place(self, st, x):
        subs = sorted([b for b in st.branches if b.sub is not None and b.lid in self.ly],
                      key=lambda b: b.y)
        for b in subs:
            x = self.place(b.sub, x)
        st.x0 = x
        st.stack_x = x + self.col_width(st)
        extra = self.dr.pm(20) if st.inline else 0.0
        return st.stack_x + extra + self.dr.pm(self.P["stack_gap_mm"])

    # ---------- horizontal main ----------
    def place_h(self, hm, x):
        """Lay out a horizontal main: outlet on the right, slots (branches /
        risers) right -> left in flow order from the outlet."""
        pm = self.dr.pm
        lid = hm.lid if hm.lid in self.ly else min(self.ly.keys(), key=lambda i: self.ly[i])
        hm.y_main = self.ly[lid] - pm(self.P["branch_offset_mm"]) - pm(self.P["hmain_drop_mm"])
        gap = pm(self.P["stack_gap_mm"])
        widths = []
        for _k, kind, obj in hm.slots:
            if kind == "r":
                self.slots(obj)
                widths.append(self.place(obj, 0.0) - gap + self.bgap)
            else:
                widths.append(self.bwidth(obj))
        tail = self.sp * 1.5
        total = sum(widths) + tail
        hm.x0 = x
        hm.x_out = x + total
        xr = hm.x_out - tail
        hm.pos = []
        for (_k, kind, obj), w in zip(hm.slots, widths):
            if kind == "r":
                self.place(obj, xr - w)
                obj.bottom_y = hm.y_main
                obj.on_main = True
            hm.pos.append(xr)
            xr -= w
        return hm.x_out + gap

    def draw_h(self, hm):
        dr, P, pm = self.dr, self.P, self.dr.pm
        gs = self.g(hm.sid, hm.meta.get("gs"))
        y = hm.y_main
        x_min = hm.x_out
        for (_k, kind, obj), xr in zip(hm.slots, hm.pos):
            if kind == "r":
                self.draw(obj)                                  # riser on the main
                x_min = min(x_min, obj.stack_x + (self.jch if self.jch > dr.tol else 0.0))
                continue
            b = obj
            bgs = self.g(b.sid, gs)
            lid = b.lid if b.lid in self.ly else hm.lid
            sy = self.ly.get(lid, y) + pm(P["symbol_offset_mm"])
            left = self.draw_list(b.nodes, y, xr - self.bgap * 0.5, sy, bgs, False, base=False)
            x_min = min(x_min, left)
            if P["show_branch_tag"]:
                dr.text(left, y - pm(0.8), u"B{} ({})".format(b.no, flat_count(b.nodes)))
        dr.line(x_min, y, hm.x_out, y, gs)                       # reference main
        a = pm(1.5)                                              # flow arrow at outlet
        dr.line(hm.x_out - a, y + a * 0.5, hm.x_out, y, gs)
        dr.line(hm.x_out - a, y - a * 0.5, hm.x_out, y, gs)
        dr.text(hm.x_out - pm(2), y - pm(4), hm.label)

    # ---------- primitives ----------
    def drop(self, x, y_top, y_line, gs):
        c = self.ch
        if c > self.dr.tol and (y_top - y_line) > 2 * c:
            self.dr.line(x, y_top, x, y_line + c, gs)
            self.dr.line(x, y_line + c, x + c, y_line, gs)
            return x + c
        self.dr.line(x, y_top, x, y_line, gs)
        return x

    def join_stack(self, xa, y, xs, gs):
        """Horizontal branch -> 45 deg -> riser / stack. Returns y on the riser."""
        c = self.jch
        if c > self.dr.tol:
            c = min(c, max(xs - xa, self.dr.tol * 2) + self.bgap * 0.5)
            self.dr.line(min(xa, xs - c), y, xs - c, y, gs)
            self.dr.line(xs - c, y, xs, y - c, gs)
            return y - c
        self.dr.line(xa, y, xs, y, gs)
        return y

    def put_symbol(self, en, ex, sy, center_label=True):
        dr, P, pm = self.dr, self.P, self.dr.pm
        n = en["count"]
        lab = en["label"] + (u" x" + str(n) if n > 1 else u"")
        sym = self.sym_map.get(en["key"])
        if sym is not None:
            dr.symbol(sym, ex, sy)
            self.stats["placed"] += n
            if P["add_label"]:
                if center_label:
                    dr.text(ex, sy + pm(P["label_offset_mm"]) + dr.text_h, lab, True)
                else:
                    dr.text(ex + pm(2), sy + dr.text_h * 0.5, lab)
            return True
        if P["placeholder"]:
            if center_label:
                dr.text(ex, sy + pm(2) + dr.text_h, lab or "?", True)
            else:
                dr.text(ex + pm(2), sy + dr.text_h * 0.5, lab or "?")
            self.stats["placeholder"] += n
            return True
        self.stats["skipped"] += n
        return False

    def draw_conn(self, n, x0, y0, x1, y1):
        dr, pm = self.dr, self.dr.pm
        gs = self.conn_gs.get(n.sid)
        if n.trans and not n.riser:
            dr.dot(x0, y0, pm(0.6), gs)
            dr.text(x0, y0 + pm(1) + dr.text_h, n.label, True)
            self.stats["conn"] += 1
            return
        dr.line(x0, y0, x1, y1, gs)
        if abs(x1 - x0) < 1e-9:
            dr.text(x1, y1 + pm(1) + dr.text_h, n.label, True)
        else:
            dr.text(x1 + pm(1), y1 + dr.text_h * 0.5, n.label)
        self.stats["riser"] += 1

    # ---------- drawing ----------
    def draw_item(self, n, cx, sy, gs, loose):
        ok = self.put_symbol({"key": n.key, "label": n.label, "count": n.count}, cx, sy)
        gs = self.g(n.sid, gs)
        ups = self.up_items(n)
        if ups:
            uy = sy + self.rise
            total = sum(self.w_node(u) for u in ups)
            xr = cx + total * 0.5
            for u in ups:
                wu = self.w_node(u)
                ucx = xr - wu * 0.5
                if u.kind == "conn":
                    if not loose:
                        self.dr.line(ucx, sy, cx, sy, gs)
                        self.draw_conn(u, ucx, sy, ucx, uy)
                elif self.draw_item(u, ucx, uy, gs, loose) and not loose:
                    ugs = self.g(u.sid, gs)
                    self.dr.line(ucx, uy, ucx, sy, ugs)
                    self.dr.line(ucx, sy, cx, sy, ugs)
                xr -= wu
        return ok

    def draw_list(self, nodes, y_line, x_right, sy, gs, loose, base=True):
        """Draw nodes right -> left and (if base) the horizontal line they sit on.
        Returns the left-most connection x on y_line."""
        x = x_right
        x_min = x_right
        breaks = []
        for n in nodes:
            w = self.w_node(n)
            ngs = self.g(n.sid, gs)
            cx = x - w * 0.5
            if n.kind == "item":
                if self.draw_item(n, cx, sy, gs, loose) and not loose:
                    x_min = min(x_min, self.drop(cx, sy, y_line, ngs))
            elif n.kind == "conn":
                if not loose:
                    top = max(sy + self.rise * 0.5, y_line + self.rise * 0.5)
                    self.draw_conn(n, cx, y_line, cx, top)
                    x_min = min(x_min, cx)
                    if n.switch:
                        breaks.append((cx, ngs))
            else:
                yj = y_line + self.tstep
                xd = x - self.tstep
                self.draw_list(n.children, yj, xd, sy, ngs, loose)
                if not loose:
                    self.dr.line(xd, yj, x, y_line, ngs)
                    x_min = min(x_min, x)
            x -= w
        if not loose and base:
            start, cgs = x_right, gs
            for bx, bgs in breaks:
                if bx > x_min:
                    self.dr.line(bx, y_line, start, y_line, cgs)
                    start, cgs = bx, bgs
            self.dr.line(x_min, y_line, start, y_line, cgs)
        return x_min

    def draw(self, st):
        if st.kind == "hmain":
            return self.draw_h(st)
        dr, P, pm = self.dr, self.P, self.dr.pm
        gs = self.g(st.sid, st.meta.get("gs"))
        loose = st.kind == "loose"
        ys = []
        for lid, ordered in st.by_level.items():
            xr = st.stack_x
            sy = self.ly[lid] + pm(P["symbol_offset_mm"])
            for b in reversed(ordered):
                w = self.bwidth(b)
                bgs = self.g(b.sid, gs)
                if b.sub is not None:
                    ys.append(self.join_stack(b.sub.stack_x, b.y, st.stack_x, bgs))
                    if P["show_branch_tag"]:
                        dr.text(b.sub.stack_x + pm(1), b.y - pm(0.8), u"B" + str(b.no))
                    xr -= w
                    continue
                x_right = xr - self.bgap * 0.5
                left = self.draw_list(b.nodes, b.y, x_right, sy, bgs, loose)
                if not loose:
                    ys.append(self.join_stack(x_right, b.y, st.stack_x, bgs))
                    if st.kind == "net" and P["show_branch_tag"]:
                        dr.text(left, b.y - pm(0.8),
                                u"B{} ({})".format(b.no, flat_count(b.nodes)))
                xr -= w

        per_lvl = OrderedDict()
        for n, lid in st.inline:
            if lid in self.ly:
                per_lvl.setdefault(lid, []).append(n)
        for lid, ns in per_lvl.items():
            for j, n in enumerate(ns):
                y = self.ly[lid] + self.lsp * 0.3 + j * self.sp
                if n.kind == "conn":
                    self.draw_conn(n, st.stack_x, y, st.stack_x + pm(10), y)
                else:
                    self.put_symbol({"key": n.key, "label": n.label, "count": n.count},
                                    st.stack_x, y, center_label=False)
                ys.append(y)

        if loose:
            y0 = min(self.ly.values()) - self.lsp * 0.3
            dr.text((st.x0 + st.stack_x) * 0.5, y0, st.label, True)
        elif ys:
            y_bot = st.bottom_y if st.bottom_y is not None else min(ys) - self.lsp * 0.15
            y_top = max(ys)
            vent_top = (st.is_root and st.meta.get("cls") == "Vent") or getattr(st, "vent_top", False)
            if vent_top and P["vent_to_atm"]:
                y_top = self.top_y + self.lsp * 0.6
                dr.text(st.stack_x, y_top + pm(2) + dr.text_h, "VENT TO ATMOSPHERE", True)
            c = self.jch
            if getattr(st, "on_main", False) and c > dr.tol and (y_top - y_bot) > 2 * c:
                dr.line(st.stack_x, y_bot + c, st.stack_x, y_top, gs)
                dr.line(st.stack_x, y_bot + c, st.stack_x + c, y_bot, gs)
            else:
                dr.line(st.stack_x, y_bot, st.stack_x, y_top, gs)
            if st.bottom_y is None:
                dr.text(st.stack_x, y_bot - pm(1.5), st.label, True)
            elif not vent_top:
                dr.text(st.stack_x, y_top + pm(2) + dr.text_h, st.label, True)
            else:
                dr.text(st.stack_x + pm(1), y_top - pm(4), st.label)

        for b in st.branches:
            if b.sub is not None and b.lid in self.ly:
                self.draw(b.sub)


# ----------------------------------------------------------------------------
# VIEW OUTPUT
# ----------------------------------------------------------------------------
def purge_group_types():
    for gt in DB.FilteredElementCollector(doc).OfClass(DB.GroupType):
        try:
            if ename(gt).startswith(GROUP_PREFIX) and gt.Groups.Size == 0:
                doc.Delete(gt.Id)
        except Exception:
            pass


def clear_view(v, clear_all, tracked_uids):
    deleted = 0
    vid = id_val(v.Id)
    groups = [g for g in DB.FilteredElementCollector(doc, v.Id).OfClass(DB.Group)
              if clear_all or ename(g.GroupType).startswith(GROUP_PREFIX)]
    for g in groups:
        try:
            doc.Delete(g.Id)
            deleted += 1
        except Exception:
            pass
    ids = []
    if clear_all:
        for e in DB.FilteredElementCollector(doc, v.Id).WhereElementIsNotElementType():
            try:
                if id_val(e.OwnerViewId) == vid and not isinstance(e, DB.View):
                    ids.append(e.Id)
            except Exception:
                pass
    else:
        for uid in tracked_uids:
            e = doc.GetElement(uid)
            if e is not None:
                try:
                    if id_val(e.OwnerViewId) == vid:
                        ids.append(e.Id)
                except Exception:
                    pass
    for i in ids:
        if doc.GetElement(i) is None:
            continue
        try:
            doc.Delete(i)
            deleted += 1
        except Exception:
            pass
    purge_group_types()
    return deleted


def create_drafting_view(base_name):
    vft = next(v for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType)
               if v.ViewFamily == DB.ViewFamily.Drafting)
    sview = DB.ViewDrafting.Create(doc, vft.Id)
    existing = set(v.Name for v in DB.FilteredElementCollector(doc).OfClass(DB.View))
    name, i = base_name, 1
    while name in existing:
        i += 1
        name = base_name + u" (" + str(i) + u")"
    sview.Name = name
    return sview


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    view = doc.ActiveView
    if view is None or view.IsTemplate or isinstance(view, (DB.ViewDrafting, DB.ViewSheet,
                                                            DB.ViewSchedule)):
        forms.alert("Open a model view (plan / 3D / section) containing the drainage system.",
                    exitscript=True)
    if not os.path.exists(XAML_FILE):
        forms.alert("ui.xaml not found next to script.py", exitscript=True)

    settings = load_settings()
    P = settings["params"]

    mode = forms.CommandSwitchWindow.show([MODE_SYS, MODE_VIEW], message="Source of elements:")
    if not mode:
        return

    scope = collect_scope(view, P.get("skip_nested", True))
    picked_names = None

    if mode == MODE_SYS:
        systems = []
        for i in uidoc.Selection.GetElementIds():
            systems.extend(get_piping_systems(doc.GetElement(i)))
        if not systems:
            try:
                refs = uidoc.Selection.PickObjects(
                    ObjectType.Element, SysPickFilter(),
                    "Pick pipe(s) / fitting(s) of the system(s), then click Finish")
            except RvtExc.OperationCanceledException:
                return
            for r in refs:
                systems.extend(get_piping_systems(doc.GetElement(r)))
        uniq = OrderedDict((id_val(s.Id), s) for s in systems)
        if not uniq:
            forms.alert("No piping system found in selection.", exitscript=True)
        member_ids = set()
        for s in uniq.values():
            for getter in ("Elements", "PipingNetwork"):
                try:
                    for e in getattr(s, getter):
                        member_ids.add(id_val(e.Id))
                except Exception:
                    pass
        scope = OrderedDict((k, e) for k, e in scope.items() if k in member_ids)
        picked_names = set(s.Name for s in uniq.values())

    # ---- caches ----
    KIND, VERT, TOPZ, SYS_IDS, MKEY = {}, {}, {}, {}, {}
    sysdata = OrderedDict()
    sys_by_id = {}
    for k, e in scope.items():
        kd = node_kind(e)
        KIND[k] = kd
        if kd in ("fix", "acc", "fit"):
            MKEY[k] = model_key(e)
        if kd == "pipe":
            cp = curve_pts(e)
            if cp:
                d = cp[1] - cp[0]
                L = d.GetLength()
                VERT[k] = L > 1e-6 and abs(d.Z) / L >= VERT_COS
                TOPZ[k] = max(cp[0].Z, cp[1].Z)
        ids = set()
        for s in get_piping_systems(e):
            sd = sysdata.get(s.Name)
            if sd is None:
                sd = SysData(s)
                sysdata[s.Name] = sd
                sys_by_id[sd.id] = sd
            if kd == "fix":
                sd.count += 1
            ids.add(id_val(s.Id))
        SYS_IDS[k] = ids

    ESYS = {}
    for k, e in scope.items():
        if KIND[k] == "fix":
            continue
        sid0 = None
        if KIND[k] == "pipe":
            try:
                s0 = e.MEPSystem
                sid0 = id_val(s0.Id) if s0 is not None else None
            except Exception:
                sid0 = None
        if sid0 is None:
            cnt = {}
            for c in get_connectors(e):
                s0 = conn_sys(c)
                if s0 is not None:
                    cnt[id_val(s0.Id)] = cnt.get(id_val(s0.Id), 0) + 1
            if cnt:
                sid0 = max(cnt.keys(), key=lambda i: (cnt[i], -i))
        ESYS[k] = sid0

    fix_ids = [k for k in scope if KIND[k] == "fix"]
    extra_cands = OrderedDict()
    for k in scope:
        if KIND[k] in ("acc", "fit"):
            cat, q = extra_cands.get(MKEY[k], (cat_name(scope[k]), 0))
            extra_cands[MKEY[k]] = (cat, q + 1)
    if not fix_ids and not extra_cands:
        forms.alert("No Plumbing Fixtures / Pipe Accessories / Fittings found in scope.",
                    exitscript=True)

    saved_checked = set(P.get("checked_systems") or [])
    sys_rows = []
    for sd in sorted(sysdata.values(), key=lambda d: (d.priority, nat_key(d.name))):
        if picked_names is not None:
            chk = sd.name in picked_names
        elif saved_checked & set(sysdata.keys()):
            chk = sd.name in saved_checked
        else:
            chk = sd.cls in DRAIN_CLS
        lbl = (sd.name + u"   [" + (sd.abbr or u"-") + u" | " + sd.cls + u"]   (" +
               str(sd.count) + u" fixtures)")
        sys_rows.append((sd.name, lbl, chk))
    no_sys_count = len([k for k in fix_ids if not SYS_IDS[k]])
    if no_sys_count:
        sys_rows.append((NO_SYS, NO_SYS + u"   (" + str(no_sys_count) + u" items)",
                         NO_SYS in saved_checked and picked_names is None))

    counts, cats = OrderedDict(), {}
    for k in fix_ids:
        counts[MKEY[k]] = counts.get(MKEY[k], 0) + 1
        cats[MKEY[k]] = cat_name(scope[k])
    sym_lookup = collect_symbols()
    sym_options = [NONE_OPT] + sorted(sym_lookup.keys(), key=nat_key)
    smap = settings["mapping"]
    map_rows = [(cats[mk], mk, counts[mk], smap.get(mk, NONE_OPT), False)
                for mk in sorted(counts.keys(), key=lambda m: (cats[m], nat_key(m)))]
    for mk in settings["extras"]:
        if mk in extra_cands and mk not in counts:
            map_rows.append((extra_cands[mk][0], mk, extra_cands[mk][1],
                             smap.get(mk, NONE_OPT), True))

    dviews = collect_drafting_views()
    lstyles = collect_line_styles()
    ttypes = collect_text_types()

    frm = SchematicWindow(P, sys_rows, map_rows, extra_cands, smap, sym_options,
                          sorted(dviews.keys(), key=nat_key),
                          sorted(lstyles.keys(), key=nat_key),
                          sorted(ttypes.keys(), key=nat_key))
    frm.ShowDialog()
    if not frm.ok:
        return

    P = frm.P
    P["checked_systems"] = list(frm.checked)
    settings["params"] = P
    for mk, v in frm.mapping.items():
        if v and v != NONE_OPT:
            smap[mk] = v
        elif mk in smap:
            del smap[mk]
    settings["extras"] = [mk for mk in settings["extras"] if mk not in extra_cands] + frm.extras
    save_settings(settings)

    extra_set = set(frm.extras) if P["use_extras"] else set()
    sym_ids = fix_ids + [k for k in scope if KIND[k] in ("acc", "fit") and MKEY[k] in extra_set]
    if not sym_ids:
        forms.alert("Nothing to draw (no fixtures and no accessory / fitting added).",
                    exitscript=True)
    counts_all = OrderedDict()
    for k in sym_ids:
        counts_all[MKEY[k]] = counts_all.get(MKEY[k], 0) + 1

    checked = set(frm.checked)
    levels = get_levels(P["story_only"])
    if not levels:
        forms.alert("No levels found.", exitscript=True)
    tol = P["level_tol_mm"] * MM
    lvl_by_id = dict((id_val(l.Id), l) for l in levels)
    item_level = {}
    for k in sym_ids:
        l = resolve_level(scope[k], levels, P["level_method"], tol)
        if l is not None:
            item_level[k] = id_val(l.Id)

    prim = {}
    for k in sym_ids:
        cands = [sys_by_id[s] for s in SYS_IDS[k] if s in sys_by_id and sys_by_id[s].name in checked]
        if cands:
            prim[k] = sorted(cands, key=lambda d: (d.priority, nat_key(d.name)))[0].id
        elif not SYS_IDS[k] and NO_SYS in checked:
            prim[k] = None

    by_abbr = P["group_by"] == LABEL_ABBR
    riser_abbr = set(a.upper() for a in (P.get("riser_abbr") or []))

    def meta_of(sd):
        if sd is None:
            return {"label": NO_SYS, "cls": "Undefined", "priority": 9, "color": None,
                    "abbr": "NOSYS", "short": "NOSYS"}
        if by_abbr:
            lbl = sd.abbr or sd.name
        else:
            lbl = sd.name if (not sd.abbr or sd.abbr in sd.name) else sd.name + u" (" + sd.abbr + u")"
        return {"label": lbl, "cls": sd.cls, "priority": sd.priority,
                "color": sd.color, "abbr": sd.abbr or sd.name, "short": sd.abbr or sd.name}

    def ensure_sys(sid0, sobj=None):
        if sid0 in sys_by_id:
            return True
        if sobj is None:
            return False
        try:
            sys_by_id[sid0] = SysData(sobj)
            return True
        except Exception:
            return False

    def is_vent(sid0):
        sd = sys_by_id.get(sid0)
        if sd is None:
            return False
        return sd.cls == "Vent" or (sd.abbr or "").upper() in riser_abbr

    def x_of(k):
        pt = elem_pt(scope[k])
        return pt.X if pt is not None else 0.0

    def mk_item(k):
        n = SNode("item", k)
        n.key = MKEY[k]
        n.label = label_text(scope[k])
        return n

    def level_of_z(z):
        return id_val(snap_level(z, levels, tol).Id)

    def per_level_stack(label, meta, ks, kind, sort_mode):
        st = Stack(label, meta, kind)
        per = OrderedDict()
        for k in ks:
            if item_level.get(k) is not None:
                per.setdefault(item_level[k], []).append(k)
        for lid in sorted(per.keys(), key=lambda i: lvl_by_id[i].ProjectElevation):
            items = per[lid]
            if sort_mode == ORDER_TYPE:
                items.sort(key=lambda k: (nat_key(MKEY[k]), x_of(k)))
            else:
                items.sort(key=x_of, reverse=True)
            nodes = []
            for k in items:
                n = mk_item(k)
                n.sid = prim.get(k)
                nodes.append(n)
            st.branches.append(Branch(nodes, (lvl_by_id[lid].ProjectElevation, 0), lid))
        for i, b in enumerate(st.branches):
            b.no = i + 1
        return st

    stacks = []
    loose_by_sys = OrderedDict()
    conn_sids = set()
    checked_sds = sorted([sd for sd in sysdata.values() if sd.name in checked],
                         key=lambda d: (d.priority, nat_key(d.name)))
    use_h = P["main_mode"] != MAIN_V

    if P["order_by"] == ORDER_NET:
        picked_roots = set()
        if P["root_mode"] == ROOT_PICK:
            allow = set(k for k in scope if KIND[k] in ("pipe", "fit", "acc"))
            try:
                refs = uidoc.Selection.PickObjects(
                    ObjectType.Element, IdPickFilter(allow),
                    "Pick OUTLET pipe/fitting of each network (start of network), then Finish")
                picked_roots = set(id_val(r.ElementId) for r in refs)
            except RvtExc.OperationCanceledException:
                picked_roots = set()
        ref_pipes = []
        if P["main_mode"] == MAIN_H_PICK:
            allow = set(k for k in scope if KIND[k] == "pipe" and not VERT.get(k))
            try:
                refs = uidoc.Selection.PickObjects(
                    ObjectType.Element, IdPickFilter(allow),
                    "Pick ONE horizontal / graded pipe per network = reference main, then Finish")
                ref_pipes = [id_val(r.ElementId) for r in refs]
            except RvtExc.OperationCanceledException:
                ref_pipes = []

        ctx = Ctx()
        ctx.item_level = item_level
        ctx.mk_item = mk_item
        ctx.level_of_z = level_of_z
        ctx.stk = find_stack_nodes(scope, KIND, VERT, P["stack_min_mm"] * MM)
        ctx.net = None
        ctx.kind = KIND
        ctx.esys = ESYS
        ctx.show_trans = bool(P["show_xconn"])
        ctx.default_sid = None
        ctx.is_vent = is_vent

        def zof(k):
            pt = elem_pt(scope[k])
            return pt.Z if pt is not None else None
        ctx.zof = zof

        def sys_short(sid0):
            if sid0 not in sys_by_id:
                return "?"
            return meta_of(sys_by_id[sid0])["short"]
        ctx.sys_short = sys_short

        def level_of_k(k):
            z = zof(k)
            return level_of_z(z) if z is not None else None
        ctx.level_of_k = level_of_k

        def mk_conns(k):
            if not P["show_xconn"] or ctx.net is None or KIND.get(k) == "fix":
                return []
            out = []
            for csid, sobj, org in ctx.net.cross.get(k, []):
                if not ensure_sys(csid, sobj):
                    continue
                n = SNode("conn", k)
                n.sid = csid
                n.label = sys_short(csid)
                n.key = u"conn:" + str(csid)
                n.lid = level_of_z(org.Z)
                n.riser = True
                conn_sids.add(csid)
                out.append(n)
            return out
        ctx.mk_conns = mk_conns

        def build_network(tree, root, label, meta):
            """Horizontal main when possible, else vertical stack."""
            if use_h:
                ref = next((p for p in ref_pipes if p in tree.parent), None)
                hm = analyse_h(tree, root, label, meta, ctx, ref)
                if hm is not None:
                    return hm
            return analyse(tree, root, label, meta, ctx)

        if P["net_mode"] == NET_PHYS:
            checked_ids = set(sd.id for sd in checked_sds)

            def in_scope(k):
                ids = SYS_IDS.get(k) or set()
                if not ids:
                    return NO_SYS in checked
                return bool(ids & checked_ids)

            def cls_of_conn(c):
                s0 = conn_sys(c)
                if s0 is None:
                    return None
                i0 = id_val(s0.Id)
                if not ensure_sys(i0, s0):
                    return None
                return sys_by_id[i0].cls

            sym_set = set(k for k in sym_ids if in_scope(k))
            ctx.is_item = lambda k: k in sym_set
            pnodes = OrderedDict((k, e) for k, e in scope.items() if in_scope(k))
            pnet = PhysNet(pnodes, KIND, cls_of_conn)
            ctx.net = pnet
            n_net = 0
            for comp in pnet.components():
                if not any(KIND.get(k) in ("pipe", "fit") for k in comp):
                    for k in comp:
                        if k in sym_set:
                            loose_by_sys.setdefault(prim.get(k), []).append(k)
                    continue
                remaining = set(comp)
                while any(KIND.get(k) != "fix" for k in remaining):
                    subnet = pnet.sub(remaining)
                    order = [k for k in comp if k in remaining]
                    root, root_pt = find_root(subnet, order, KIND, picked_roots)
                    if root is None:
                        break
                    tree = Tree(subnet, root, root_pt, KIND, ctx.stk, TOPZ)
                    tree.is_item = ctx.is_item
                    remaining -= set(tree.nodes)
                    sids = [ESYS[k] for k in tree.nodes
                            if KIND.get(k) == "pipe" and ESYS.get(k) is not None]
                    if not sids:
                        sids = [ESYS[k] for k in tree.nodes if ESYS.get(k) is not None]
                    dom = max(set(sids), key=sids.count) if sids else None
                    meta = dict(meta_of(sys_by_id.get(dom)))
                    meta["systems"] = sorted(set(i for i in sids if i is not None))
                    # label: dominant system + other ABBREVIATIONS (unique)
                    dshort = sys_short(dom) if dom is not None else u""
                    oth = []
                    for i in sorted(set(sids), key=lambda i: (
                            sys_by_id[i].priority if i in sys_by_id else 9, sys_short(i))):
                        sh = sys_short(i)
                        if sh != dshort and sh not in oth:
                            oth.append(sh)
                    n_net += 1
                    lbl = meta["label"] + ((u" + " + u" + ".join(oth)) if oth else u"")
                    ctx.default_sid = dom
                    stacks.append(build_network(tree, root, u"N" + str(n_net) + u" " + lbl, meta))
                for k in comp:
                    if k in remaining and k in sym_set:
                        loose_by_sys.setdefault(prim.get(k), []).append(k)
        else:
            for sd in checked_sds:
                sid = sd.id
                ctx.is_item = (lambda s: (lambda k: prim.get(k, -1) == s))(sid)
                net = Net(sid, scope, SYS_IDS, KIND)
                ctx.net = net
                ctx.esys = dict((k, sid) for k in net.nodes)
                ctx.default_sid = sid
                meta = meta_of(sd)
                comps = net.components()
                pipe_comps = [c for c in comps if any(KIND.get(k) in ("pipe", "fit") for k in c)]
                n = 0
                for comp in comps:
                    if comp not in pipe_comps:
                        loose_by_sys.setdefault(sid, []).extend([k for k in comp if ctx.is_item(k)])
                        continue
                    root, root_pt = find_root(net, comp, KIND, picked_roots)
                    if root is None:
                        continue
                    tree = Tree(net, root, root_pt, KIND, ctx.stk, TOPZ)
                    tree.is_item = ctx.is_item
                    n += 1
                    lbl = meta["label"] if len(pipe_comps) == 1 else meta["label"] + u"-" + str(n)
                    stacks.append(build_network(tree, root, lbl, meta))
                    reached = set(tree.nodes)
                    loose_by_sys.setdefault(sid, []).extend(
                        [k for k in comp if ctx.is_item(k) and k not in reached])
            nos = [k for k in sym_ids if k in prim and prim[k] is None]
            if nos:
                loose_by_sys[None] = nos
        for sid, ks in loose_by_sys.items():
            if not ks:
                continue
            meta = meta_of(sys_by_id.get(sid))
            st = per_level_stack(u"NOT CONNECTED - " + meta["label"], meta, ks, "loose", ORDER_X)
            st.sid = sid
            stacks.append(st)
    else:
        groups = OrderedDict()
        for k in sym_ids:
            if k in prim:
                groups.setdefault(prim[k], []).append(k)
        order = [sd.id for sd in checked_sds if sd.id in groups] + ([None] if None in groups else [])
        for sid in order:
            meta = meta_of(sys_by_id.get(sid))
            st = per_level_stack(meta["label"], meta, groups[sid], "simple", P["order_by"])
            st.sid = sid
            for b in st.branches:
                b.sid = sid
            stacks.append(st)

    stacks = [s for s in stacks if s.branches or s.inline or s.risers]
    if not stacks:
        forms.alert("Nothing to draw with the selected systems.", exitscript=True)

    # ---- levels to draw ----
    used = set()
    for root_st in stacks:
        for st in iter_stacks(root_st):
            if st.kind == "hmain" and st.lid is not None:
                used.add(st.lid)
            for b in st.branches:
                if b.lid is not None:
                    used.add(b.lid)
            for _n, lid in st.inline:
                used.add(lid)
    used = [lvl_by_id[i] for i in used if i in lvl_by_id]
    top = max(used, key=lambda l: l.ProjectElevation)
    low = min(used, key=lambda l: l.ProjectElevation)
    levels_draw = [l for l in levels if l.ProjectElevation <= top.ProjectElevation + 1e-6]
    if not P["from_lowest"]:
        levels_draw = [l for l in levels_draw if l.ProjectElevation >= low.ProjectElevation - 1e-6]
    above = [l for l in levels if l.ProjectElevation > top.ProjectElevation + 1e-6]
    levels_draw += above[:P["extra_levels_above"]]

    if P["combine"]:
        for root_st in stacks:
            for st in iter_stacks(root_st):
                for b in st.branches:
                    b.nodes = combine_nodes(b.nodes)

    sym_map = {}
    unmapped = set()
    for mk in counts_all.keys():
        v = smap.get(mk)
        if v and v in sym_lookup:
            sym_map[mk] = sym_lookup[v]
        else:
            unmapped.add(mk)

    all_sids = set(conn_sids)

    def _walk(ns):
        for n in ns:
            if n.sid is not None:
                all_sids.add(n.sid)
            _walk(n.children)
    for root_st in stacks:
        all_sids.update(root_st.meta.get("systems", []))
        for st in iter_stacks(root_st):
            if st.sid is not None:
                all_sids.add(st.sid)
            for b in st.branches:
                if b.sid is not None:
                    all_sids.add(b.sid)
                _walk(b.nodes)
            _walk([n for n, _l in st.inline])

    # ---- transaction ----
    dkey = doc_key()
    gen = settings["generated"].setdefault(dkey, {})
    warn = []
    weight = int(P.get("pipe_line_weight", 4))
    t = DB.Transaction(doc, "Drainage Schematic")
    t.Start()
    try:
        overwrite = P["view_mode"] == VIEW_OVER and P["target_view"] in dviews
        if overwrite:
            sview = dviews[P["target_view"]]
            n_del = clear_view(sview, P["clear_all"], gen.get(sview.UniqueId, []))
        else:
            sview = create_drafting_view(P["new_view_name"])
            n_del = 0
        if sview.Scale != P["scale"]:
            sview.Scale = P["scale"]

        for root_st in stacks:
            m = root_st.meta
            if "gs" not in m:
                m["gs"] = get_or_create_line_style("SCH_" + sanitize(m["abbr"]), m["color"], weight)
            for st in iter_stacks(root_st):
                st.meta = m
        conn_gs = {}
        for csid in all_sids:
            if csid not in sys_by_id:
                continue
            m = meta_of(sys_by_id[csid])
            conn_gs[csid] = get_or_create_line_style("SCH_" + sanitize(m["abbr"]), m["color"], weight)

        dr = Drawer(sview, ttypes.get(P["text_type"]))
        pm = dr.pm
        lsp = pm(P["level_spacing_mm"])
        ly = OrderedDict((id_val(l.Id), i * lsp) for i, l in enumerate(levels_draw))
        lay = Layout(dr, P, ly, ly[id_val(top.Id)], sym_map, conn_gs)

        x = 0.0
        for st in stacks:
            if st.kind == "hmain":
                x = lay.place_h(st, x)
            else:
                lay.slots(st)
                x = lay.place(st, x)
        for st in stacks:
            lay.draw(st)

        level_gs = lstyles.get(P["level_line_style"])
        x_left = -pm(P["level_text_width_mm"])
        for l in levels_draw:
            y = ly[id_val(l.Id)]
            dr.line(x_left, y, x, y, level_gs)
            dr.text(x_left, y + dr.text_h * 3.0 + pm(1),
                    l.Name + u"\rRL " + u"{:+.3f}".format(l.Elevation * 0.3048))

        if P["group_output"] and dr.ids:
            try:
                grp = doc.Create.NewGroup(List[DB.ElementId](dr.ids))
                stamp = System.DateTime.Now.ToString("yyyyMMdd_HHmmss")
                try:
                    grp.GroupType.Name = GROUP_PREFIX + "_" + sanitize(sview.Name) + "_" + stamp
                except Exception:
                    pass
            except Exception as ex:
                warn.append("Could not group generated items: " + str(ex))
        created_uids = []
        for i in dr.ids:
            e = doc.GetElement(i)
            if e is not None:
                created_uids.append(e.UniqueId)
        t.Commit()
    except Exception as ex:
        t.RollBack()
        forms.alert("Failed, transaction rolled back:\n" + str(ex))
        return

    gen[sview.UniqueId] = created_uids
    save_settings(settings)

    try:
        uidoc.ActiveView = sview
    except Exception:
        pass

    # ---- report ----
    try:
        output.print_md(u"## Drainage Schematic -> **" + sview.Name + u"** (" +
                        (u"overwritten, " + str(n_del) + u" old items removed" if overwrite
                         else u"new view") + u")")
        s = lay.stats
        output.print_md(u"Top level: **" + top.Name + u"** | Levels drawn: **" + str(len(levels_draw)) +
                        u"** | Symbols placed: **" + str(s["placed"]) +
                        u"** | Placeholders: **" + str(s["placeholder"]) +
                        u"** | Skipped: **" + str(s["skipped"]) +
                        u"** | Vent risers: **" + str(s["riser"]) +
                        u"** | System change points: **" + str(s["conn"]) + u"**")
        if P["order_by"] == ORDER_NET:
            output.print_md(u"Sequence: `>` start -> end, `{...}` sub-branch tier, "
                            u"`[...]` drains into item, `<VP^>` vent riser, `<SD>` system change. "
                            u"Main: **" + P["main_mode"] + u"**, grouping: **" + P["net_mode"] + u"**.")
        rows = []

        def add_stack_rows(st):
            for b in st.branches:
                lv = lvl_by_id[b.lid].Name if b.lid in lvl_by_id else "?"
                if b.sub is not None:
                    rows.append([st.label, "B" + str(b.no), lv, "-", u"sub-stack -> " + b.sub.label])
                else:
                    tag = "B" + str(b.no) if st.kind in ("net", "hmain") else "-"
                    sysn = sys_by_id[b.sid].name if b.sid in sys_by_id else ""
                    rows.append([st.label, tag, lv, flat_count(b.nodes),
                                 seq_text(b.nodes) + (u"   (" + sysn + u")" if sysn else u"")])
            for n, lid in st.inline:
                lv = lvl_by_id[lid].Name if lid in lvl_by_id else "?"
                rows.append([st.label, "on stack", lv, 1 if n.kind == "item" else "-", seq_text([n])])

        for root_st in stacks:
            if root_st.kind == "hmain":
                mlv = lvl_by_id[root_st.lid].Name if root_st.lid in lvl_by_id else "?"
                for _k, kind, obj in root_st.slots:
                    if kind == "r":
                        rows.append([root_st.label, "RISER", mlv, "-", u"riser -> " + obj.label])
                        for st in iter_stacks(obj):
                            add_stack_rows(st)
                    else:
                        lv = lvl_by_id[obj.lid].Name if obj.lid in lvl_by_id else "?"
                        sysn = sys_by_id[obj.sid].name if obj.sid in sys_by_id else ""
                        rows.append([root_st.label, "B" + str(obj.no), lv, flat_count(obj.nodes),
                                     seq_text(obj.nodes) + (u"   (" + sysn + u")" if sysn else u"")])
            else:
                for st in iter_stacks(root_st):
                    add_stack_rows(st)
        output.print_table(table_data=rows, columns=["Network / Stack", "Branch", "Level", "Qty",
                                                     "Sequence (start -> end)"])
        for w in warn:
            output.print_md(u"**Warning:** " + w)
        if unmapped:
            output.print_md(u"**Unmapped families** (map them in the table next run):")
            for mk in sorted(unmapped, key=nat_key):
                output.print_md(u"- " + mk)
    except Exception as ex:
        output.print_md(u"Schematic created. Report could not be printed: " + str(ex))


main()
