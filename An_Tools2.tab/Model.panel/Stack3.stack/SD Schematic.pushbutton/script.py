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

Branch order (same network):
  - Branches are laid out in flow order: the branch at the END of the run
    (farthest from the outlet) is pushed to the LEFT, then each next branch
    closer to the outlet, the outlet / stack on the right.
Custom label:
  - Column "Schematic Name" in the mapping table: text typed there is used as
    the label of that family type in the schematic (empty = Type Mark / Type Name).

Change log:
  - Every run saves a log of all Element IDs in scope (type, size, system,
    level, location, physical connections) per model + schematic name:
    %APPDATA%/pyRevit/DrainageSchematic/logs/<model>/<schematic>/
  - "Mark changes vs previous run": compares with the previous log and circles
    (or revision-clouds) the changed positions: NEW / MOD / DEL.

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
from Autodesk.Revit.DB.ExtensibleStorage import SchemaBuilder, Schema, Entity, AccessLevel

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
HZ_AVG = "Average"
HZ_LOW = "Lowest point (outlet end)"
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

MARK_CIRCLE = "Circle (detail lines, red)"
MARK_CLOUD = "Revision cloud (latest un-issued revision)"
MARK_STYLE_NAME = "SCH_CHANGE"
MARK_COLOR = (255, 0, 0)

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
    "label_offset_mm": 6.0,      # fallback (symbol extent unknown): label above insertion point
    "label_gap_mm": 1.5,         # paper mm between TOP of symbol graphics and bottom of label
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
    "mark_changes": False,
    "mark_style": MARK_CIRCLE,
    "mark_radius_mm": 6.0,       # paper mm
    "move_tol_mm": 20.0,         # model mm - smaller movements are ignored
    "log_keep": 20,              # number of history logs kept per schematic
    "fwg_names": "FWG, FW",      # Schematic Name of floor wastes: inlet pipe drawn below level
    "fwg_drop_mm": 3.0,          # paper mm below the level line
    "fwg_single_side": "Left",   # single inlet into FWG / FW: Left / Right / Center
    "fwg_45": True,              # 45 deg segment into FWG / FW
    "fwg_45_mm": 2.5,            # paper mm, size of that 45 deg segment
    "no_cross": True,            # sub-stacks ordered by junction height -> no crossing
    "auto_lsp": True,            # expand level spacing so branches stay between 2 levels
    "hmain_above_mm": 8.0,       # paper mm min. gap between the main and the level line BELOW it
    "lvl_free_mm": 15.0,         # paper mm kept free above a level (symbols + labels)
    "hmain_gap_mm": 6.0,         # paper mm between lowest branch of upper level and the main
    "hmain_real_z": True,        # horizontal main drawn at its REAL elevation (interpolated)
    "hmain_z_mode": "Average",   # Average of main pipes / Lowest point (outlet end)
    "hmain_clear_mm": 3.0,       # paper mm min. clearance between main and a level line
}

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "pyRevit", "DrainageSchematic")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
XAML_FILE = os.path.join(os.path.dirname(__file__), "ui.xaml")
LOG_ROOT = os.path.join(SETTINGS_DIR, "logs")


def load_settings():
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    for k in ("params", "mapping", "generated", "labels"):
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


VENT_COST_MULT = 25.0     # path cost multiplier for vent pipework (tree building)
VENT_COST_ADD = 3.0       # + fixed cost (ft) per vent element


class Tree(object):
    """Shortest-path tree from the outlet."""

    def __init__(self, net, root, root_pt, KIND, STK, TOPZ, vent_k=None):
        self.net = net
        self.KIND = KIND
        self.root = root
        self.is_item = lambda k: False
        # path choice uses a COST where vent pipework is heavily penalised, so a
        # fixture / branch is always reached through the drainage pipe it really
        # discharges to, never "backwards" through a vent loop. dist stays geometric.
        cost = {root: 0.0}
        dist, parent, depth = {root: 0.0}, {root: None}, {root: 0}
        entry, exitp = {root: root_pt}, {root: None}
        ecls = {root: None}
        done, done_set = [], set()
        heap = [(0.0, 0, root)]
        while heap:
            ck, _dp, k = heapq.heappop(heap)
            if k in done_set:
                continue
            d = dist[k]
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
                vent_edge = ct == "Vent" or cm == "Vent"
                if not vent_edge and vent_k is not None:
                    try:
                        vent_edge = bool(vent_k(j))
                    except Exception:
                        vent_edge = False
                nc = ck + (w * VENT_COST_MULT + VENT_COST_ADD if vent_edge else w)
                if j not in cost or nc < cost[j] - 1e-9:
                    cost[j] = nc
                    dist[j] = d + w
                    parent[j] = k
                    entry[j] = theirs
                    exitp[j] = mine
                    ecls[j] = ct
                    depth[j] = depth[k] + 1
                    heapq.heappush(heap, (nc, depth[j], j))
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
        # drawn right -> left: shorter side branches near the junction,
        # longer ones further left, straight run (end of line) left-most
        return sorted(self.children.get(k, []),
                      key=lambda c: (self.is_run(k, c), self.maxd[c], c))

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
        self.ks = [k] if k is not None else []     # element ids drawn by this node
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
            out[-1].ks.extend(n.ks)
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
    if sid_to is None or sid_from is None or sid_to == sid_from:
        return []
    # option OFF: hide system-change markers, but ALWAYS keep vent risers
    if not ctx.show_trans and not ctx.is_vent(sid_to):
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
        self.attach = None
        self.ez = None          # real model Z of the junction (sub-stack joins)


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
        self.main = []
        self.attach = None


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


# ----------------------------------------------------------------------------
# LINK  drafting element  ->  model element IDs (Extensible Storage + log "draw")
# ----------------------------------------------------------------------------
LINK_SCHEMA_GUID = "6f1d2c9a-3b7e-4c51-9a2e-5d8b7c4e1a23"
LINK_SCHEMA_NAME = "DrainageSchematicLink"
LINK_FIELD = "ModelIds"
TOPO_MAX = 3000


class Topo(object):
    """Parent pointers of all flow trees (toward the outlet) -> pipe runs per drawn line."""

    def __init__(self):
        self.parent = {}
        self.nch = {}
        self.items = set()
        self.stop = set()        # main path / stack nodes
        self.zl = {}             # element id -> level id of its mid-point

    def add_tree(self, tree):
        for k in tree.nodes:
            self.parent[k] = tree.parent.get(k)
            self.nch[k] = len(tree.children.get(k, []))

    def _brk(self, k):
        return k in self.items or k in self.stop or self.nch.get(k, 0) >= 2

    def arm(self, k):
        ids, cur, n = [], self.parent.get(k), 0
        while cur is not None and n < TOPO_MAX:
            n += 1
            if self._brk(cur):
                return ids, cur
            ids.append(cur)
            cur = self.parent.get(cur)
        return ids, cur

    def arms(self, ks):
        ids, jn = [], None
        for i, k in enumerate(ks or []):
            a, j = self.arm(k)
            if i == 0:
                jn = j
            for x in a:
                if x not in ids:
                    ids.append(x)
        return ids, jn

    def path_to(self, a, b):
        ids, cur, n = [], self.parent.get(a), 0
        while cur is not None and cur != b and n < TOPO_MAX:
            n += 1
            if cur in self.stop:
                break
            ids.append(cur)
            cur = self.parent.get(cur)
        return ids

    def between(self, a, b):
        if a is None:
            return []
        ids, cur, n = [], a, 0
        while cur is not None and n < TOPO_MAX:
            n += 1
            if cur not in self.items:
                ids.append(cur)
            if cur == b or (cur in self.stop and cur != a):
                break
            if cur in self.stop and b is None:
                break
            cur = self.parent.get(cur)
            if cur is not None and cur in self.items and cur != b:
                break
        return ids

    def up_to_junction(self, a):
        if a is None:
            return [], None
        ids = [] if a in self.items else [a]
        cur, n = self.parent.get(a), 0
        while cur is not None and n < TOPO_MAX:
            n += 1
            if self._brk(cur):
                if cur not in self.items:
                    ids.append(cur)
                return ids, cur
            ids.append(cur)
            cur = self.parent.get(cur)
        return ids, cur


TOPO = Topo()
_LINK_SCHEMA = [None]


def link_schema():
    if _LINK_SCHEMA[0] is not None:
        return _LINK_SCHEMA[0]
    guid = System.Guid(LINK_SCHEMA_GUID)
    sch = Schema.Lookup(guid)
    if sch is None:
        sb = SchemaBuilder(guid)
        sb.SetSchemaName(LINK_SCHEMA_NAME)
        sb.SetReadAccessLevel(AccessLevel.Public)
        sb.SetWriteAccessLevel(AccessLevel.Public)
        sb.AddSimpleField(LINK_FIELD, clr.GetClrType(System.String))
        sch = sb.Finish()
    _LINK_SCHEMA[0] = sch
    return sch


def write_links(links, warn):
    try:
        sch = link_schema()
        fld = sch.GetField(LINK_FIELD)
    except Exception as ex:
        warn.append(u"Cannot create link schema (" + str(ex) + u") - links only in log.")
        return 0
    n = 0
    for eid, ids in links.items():
        if not ids:
            continue
        el = doc.GetElement(to_eid(eid))
        if el is None:
            continue
        try:
            ent = Entity(sch)
            ent.Set[System.String](fld, u";".join(str(i) for i in ids))
            el.SetEntity(ent)
            n += 1
        except Exception:
            pass
    return n


def _node_sids(nodes, out):
    for n in nodes:
        if n.sid is not None:
            out.append(n.sid)
        _node_sids(n.children, out)
    return out


def drain_sid(ctx, sid, nodes=None, fallback=None):
    """A pipe that carries fixture discharge is DRAINAGE: never return a vent system
    when a drainage system is available (nodes of the branch, then fallback)."""
    if sid is not None and not ctx.is_vent(sid):
        return sid
    if nodes:
        has_item = any(n.kind == "item" for n in nodes) or bool(flat_items(nodes))
        if has_item:
            ds = [i for i in _node_sids(nodes, []) if not ctx.is_vent(i)]
            if ds:
                return max(set(ds), key=ds.count)
            if fallback is not None and not ctx.is_vent(fallback):
                return fallback
    return sid


def subtree_drain_sid(tree, rk, ctx):
    """Most common drainage (non-vent) system in the sub-tree below rk."""
    cnt, todo, seen = {}, [rk], set()
    while todo:
        k = todo.pop()
        if k in seen:
            continue
        seen.add(k)
        if ctx.kind.get(k) != "fix":
            s0 = ctx.esys.get(k)
            if s0 is not None and not ctx.is_vent(s0):
                cnt[s0] = cnt.get(s0, 0) + 1
        todo.extend(tree.children.get(k, []))
    return max(cnt.keys(), key=lambda i: cnt[i]) if cnt else None


def analyse(tree, rk, label, meta, ctx, sid_in=None):
    """VERTICAL stack: every tee on the main with item(s) on its side = 1 branch."""
    st = Stack(label, meta, "net")
    main = tree.main_path(rk)
    st.main = list(main)
    TOPO.stop.update(main)
    mset = set(main)
    subn = 0
    msids = [ctx.esys.get(t) for t in main if ctx.esys.get(t) is not None]
    dsids = [i for i in msids if not ctx.is_vent(i)]      # drainage part of the stack
    pick = dsids or msids
    st.sid = max(set(pick), key=pick.count) if pick else ctx.default_sid
    if st.sid is None or ctx.is_vent(st.sid):
        # stack path runs (partly) through vent pipework but fixtures discharge into it:
        # it is a DRAINAGE stack -> drainage system of its sub-tree, else of the parent
        ds = subtree_drain_sid(tree, rk, ctx)
        if ds is None and sid_in is not None and not ctx.is_vent(sid_in):
            ds = sid_in
        if ds is None and ctx.default_sid is not None and not ctx.is_vent(ctx.default_sid):
            ds = ctx.default_sid
        if ds is not None:
            st.sid = ds
    # stack continues upward as a vent (top element of the stack path is vent)
    top_sid = next((ctx.esys.get(t) for t in reversed(main) if ctx.esys.get(t) is not None), None)
    st.vent_top = top_sid is not None and ctx.is_vent(top_sid)
    st.vent_sid = top_sid if st.vent_top else None
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
            key = (tree.dist[t], tree.maxd[c], ez)   # far from outlet -> left
            if tree.topv[c] is not None and len(set(ilids)) > 1:
                subn += 1
                sub = analyse(tree, c, label + u"." + str(subn), meta, ctx, cur)
                sub.is_root = False
                br = Branch([], key, ctx.level_of_z(ez), sub)
                br.ez = ez if ep is not None else None
                br.sid = csid if (csid is not None and not ctx.is_vent(csid)) else sub.sid
                st.branches.append(br)
            else:
                if len(nodes) == 1 and nodes[0].kind == "grp":
                    nodes = nodes[0].children
                pick = ilids or lids
                br = Branch(nodes, key, max(set(pick), key=pick.count))
                br.sid = drain_sid(ctx, cur if (nodes and nodes[0].kind == "conn" and nodes[0].switch)
                               else csid, nodes, cur)
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
    hm.main = list(main)
    TOPO.stop.update(main)
    mset = set(main)
    msids = [ctx.esys.get(t) for t in main if ctx.esys.get(t) is not None]
    hm.sid = max(set(msids), key=msids.count) if msids else ctx.default_sid
    zs = [ctx.zof(t) for t in main if ctx.kind.get(t) == "pipe" and ctx.zof(t) is not None]
    zs.sort()
    hm.lid = ctx.level_of_z(zs[len(zs) // 2]) if zs else ctx.level_of_k(rk)
    # REAL elevation of the main (model Z, internal units) -> interpolated on the schematic
    hm.z_avg = zs[len(zs) // 2] if zs else None
    zlow = []
    for t in main:
        if ctx.kind.get(t) == "pipe" and t not in ctx.stk:
            cp = curve_pts(ctx.net.nodes[t]) if ctx.net is not None and t in ctx.net.nodes else None
            if cp:
                zlow.extend([cp[0].Z, cp[1].Z])
    hm.z_low = min(zlow) if zlow else hm.z_avg
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
                br.attach = t
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
            br.attach = t
            hm.slots.append((br.key, "b", br))
        for c in tree.ordered_children(t):
            if c in mset:
                continue
            csid = edge_sid(ctx, c, cur)
            ep = tree.entry[c]
            ez = ep.Z if ep is not None else 0.0
            key = (tree.dist[t], tree.maxd[c], ez)   # far from outlet -> left
            if tree.topv[c] is not None:
                sub = analyse(tree, c, u"", meta, ctx, cur)
                if not sub.branches and not sub.inline:
                    continue
                rn += 1
                sub.label = (ctx.sys_short(sub.sid) if sub.sid is not None else u"R") + \
                    u" R" + str(rn)
                sub.is_root = False
                # vent_top / vent_sid already set by analyse() (top of the stack path)
                sub.attach = t
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
            br.sid = drain_sid(ctx, cur if (nodes and nodes[0].kind == "conn" and nodes[0].switch)
                               else csid, nodes, cur)
            br.attach = t
            hm.slots.append((key, "b", br))
    hm.slots.sort(key=lambda s: s[0])          # [0] nearest outlet (right) ... [-1] end of run (left)
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
                 sym_options, view_names, ls_names, tt_names,
                 saved_labels=None, log_info=None):
        forms.WPFWindow.__init__(self, XAML_FILE)
        self.P = dict(P)
        self.sym_options = sym_options
        self.opt_set = set(sym_options)
        self.view_names = view_names
        self.extra_cands = extra_cands
        self.saved_map = saved_map
        self.saved_labels = saved_labels or {}
        self.log_info = log_info
        self.labels = {}
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
        self.ck_hreal.IsChecked = bool(P.get("hmain_real_z", True))
        self._fill(self.cb_hz, [HZ_AVG, HZ_LOW], P.get("hmain_z_mode", HZ_AVG))
        self.tb_hclear.Text = str(P.get("hmain_clear_mm", 3.0))
        self._fill(self.cb_root, [ROOT_AUTO, ROOT_PICK], P["root_mode"])
        self.tb_stackmin.Text = str(P["stack_min_mm"])
        self._fill(self.cb_group, [LABEL_NAME, LABEL_ABBR], P["group_by"])
        self.ck_btag.IsChecked = bool(P["show_branch_tag"])
        self.ck_nocross.IsChecked = bool(P.get("no_cross", True))
        self.ck_xconn.IsChecked = bool(P["show_xconn"])

        self._fill(self.cb_lmethod, ["Reference Level", "Elevation (Z)"], P["level_method"])
        self.tb_tol.Text = str(P["level_tol_mm"])
        self.tb_extra.Text = str(P["extra_levels_above"])
        self.ck_story.IsChecked = bool(P["story_only"])
        self.ck_lowest.IsChecked = bool(P["from_lowest"])

        self.tb_lsp.Text = str(P["level_spacing_mm"])
        self.ck_autolsp.IsChecked = bool(P.get("auto_lsp", True))
        self.tb_lfree.Text = str(P.get("lvl_free_mm", 15.0))
        self.tb_hgap.Text = str(P.get("hmain_gap_mm", 6.0))
        self.tb_habove.Text = str(P.get("hmain_above_mm", 8.0))
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
        self.tb_loff.Text = str(P.get("label_gap_mm", 1.5))
        self.tb_ltw.Text = str(P["level_text_width_mm"])
        self.tb_fwgnames.Text = str(P.get("fwg_names", "FWG, FW"))
        self.tb_fwgdrop.Text = str(P.get("fwg_drop_mm", 3.0))
        self._fill(self.cb_fwgside, ["Left", "Right", "Center"], P.get("fwg_single_side", "Left"))
        self.ck_fwg45.IsChecked = bool(P.get("fwg_45", True))
        self.tb_fwg45.Text = str(P.get("fwg_45_mm", 2.5))

        ls_default = P["level_line_style"] or next((n for n in ls_names if "Thin" in n), "")
        self._fill(self.cb_lstyle, ls_names, ls_default)
        self._fill(self.cb_ttype, tt_names, P["text_type"])
        self.ck_combine.IsChecked = bool(P["combine"])
        self.ck_label.IsChecked = bool(P["add_label"])
        self.ck_ph.IsChecked = bool(P["placeholder"])
        self.ck_vent.IsChecked = bool(P["vent_to_atm"])

        self.ck_mark.IsChecked = bool(P["mark_changes"])
        self._fill(self.cb_markstyle, [MARK_CIRCLE, MARK_CLOUD], P["mark_style"])
        self.tb_mrad.Text = str(P["mark_radius_mm"])
        self.tb_mtol.Text = str(P["move_tol_mm"])

        self.dt_sys = _new_table([("Name", System.String), ("Label", System.String),
                                  ("Checked", System.Boolean)])
        for name, lbl, chk in sys_rows:
            self.dt_sys.Rows.Add(name, lbl, bool(chk))
        self.lst_systems.ItemsSource = self.dt_sys.DefaultView

        self.dt_map = _new_table([("Category", System.String), ("ModelKey", System.String),
                                  ("Qty", System.Int32), ("Symbol", System.String),
                                  ("IsExtra", System.Boolean), ("SchName", System.String)])
        for cat, key, cnt, val, extra, lbl in map_rows:
            self._add_row(cat, key, cnt, val, extra, lbl)
        self.grid_map.Columns[3].ItemsSource = List[System.String](sym_options)
        self.grid_map.ItemsSource = self.dt_map.DefaultView
        self.ck_extra.IsChecked = bool(P["use_extras"])

        self.rb_new.Checked += self.on_vmode
        self.rb_over.Checked += self.on_vmode
        self.cb_order.SelectionChanged += self.on_order
        self.cb_mainmode.SelectionChanged += self.on_order
        self.ck_hreal.Checked += self.on_order
        self.ck_hreal.Unchecked += self.on_order
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
        self.rb_new.Checked += self.on_loginfo
        self.rb_over.Checked += self.on_loginfo
        self.tb_vname.TextChanged += self.on_loginfo
        self.cb_view.SelectionChanged += self.on_loginfo
        self.ck_mark.Checked += self.on_mark
        self.ck_mark.Unchecked += self.on_mark
        self.tb_filter.TextChanged += self.on_filter
        self.grid_map.CellEditEnding += self.on_cell_edit
        self.on_vmode(None, None)
        self.on_order(None, None)
        self.on_extra(None, None)
        self.on_mark(None, None)

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

    def _add_row(self, cat, key, cnt, val, extra, lbl=u""):
        self.dt_map.Rows.Add(cat, key, int(cnt), val if val in self.opt_set else NONE_OPT,
                             bool(extra), lbl or u"")

    @staticmethod
    def _cell(r, col):
        v = r[col]
        if v is None or v is System.DBNull.Value:
            return u""
        return str(v)

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
                         "%' OR Symbol LIKE '%" + txt + "%' OR SchName LIKE '%" + txt + "%')")
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

    def _log_name(self):
        if self.rb_over.IsChecked:
            return self._sel(self.cb_view)
        return (self.tb_vname.Text or u"").strip() or DEFAULTS["new_view_name"]

    def on_loginfo(self, sender, args):
        if self.log_info is None:
            return
        try:
            self.txt_loginfo.Text = self.log_info(self._log_name())
        except Exception:
            self.txt_loginfo.Text = u""

    def on_mark(self, sender, args):
        on = bool(self.ck_mark.IsChecked)
        for c in (self.cb_markstyle, self.tb_mrad, self.tb_mtol):
            c.IsEnabled = on
        self.on_loginfo(None, None)

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
        hmode = net and self._sel(self.cb_mainmode) != MAIN_V
        real = bool(self.ck_hreal.IsChecked)
        self.ck_hreal.IsEnabled = hmode
        self.cb_hz.IsEnabled = hmode and real
        self.tb_hclear.IsEnabled = hmode and real
        self.tb_hdrop.IsEnabled = hmode and not real

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
            self._add_row(cat, key, qty, self.saved_map.get(key, NONE_OPT), True,
                          self.saved_labels.get(key, u""))
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
        P["hmain_real_z"] = bool(self.ck_hreal.IsChecked)
        P["hmain_z_mode"] = self._sel(self.cb_hz) or HZ_AVG
        P["hmain_clear_mm"] = max(0.0, to_float(self.tb_hclear.Text, 3.0))
        P["root_mode"] = self._sel(self.cb_root)
        P["stack_min_mm"] = max(100.0, to_float(self.tb_stackmin.Text, 2000.0))
        P["group_by"] = self._sel(self.cb_group)
        P["show_branch_tag"] = bool(self.ck_btag.IsChecked)
        P["no_cross"] = bool(self.ck_nocross.IsChecked)
        P["show_xconn"] = bool(self.ck_xconn.IsChecked)
        P["use_extras"] = bool(self.ck_extra.IsChecked)
        P["level_method"] = self._sel(self.cb_lmethod)
        P["level_tol_mm"] = to_float(self.tb_tol.Text, 300.0)
        P["extra_levels_above"] = max(0, to_int(self.tb_extra.Text, 0))
        P["story_only"] = bool(self.ck_story.IsChecked)
        P["from_lowest"] = bool(self.ck_lowest.IsChecked)
        P["level_spacing_mm"] = to_float(self.tb_lsp.Text, 40.0)
        P["auto_lsp"] = bool(self.ck_autolsp.IsChecked)
        P["lvl_free_mm"] = max(0.0, to_float(self.tb_lfree.Text, 15.0))
        P["hmain_gap_mm"] = max(0.0, to_float(self.tb_hgap.Text, 6.0))
        P["hmain_above_mm"] = max(0.0, to_float(self.tb_habove.Text, 8.0))
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
        P["label_gap_mm"] = max(0.0, to_float(self.tb_loff.Text, 1.5))
        P["level_text_width_mm"] = to_float(self.tb_ltw.Text, 45.0)
        P["fwg_names"] = (self.tb_fwgnames.Text or u"").strip()
        P["fwg_drop_mm"] = max(0.0, to_float(self.tb_fwgdrop.Text, 3.0))
        P["fwg_single_side"] = self._sel(self.cb_fwgside) or "Left"
        P["fwg_45"] = bool(self.ck_fwg45.IsChecked)
        P["fwg_45_mm"] = max(0.0, to_float(self.tb_fwg45.Text, 2.5))
        P["level_line_style"] = self._sel(self.cb_lstyle)
        P["text_type"] = self._sel(self.cb_ttype)
        P["combine"] = bool(self.ck_combine.IsChecked)
        P["add_label"] = bool(self.ck_label.IsChecked)
        P["placeholder"] = bool(self.ck_ph.IsChecked)
        P["vent_to_atm"] = bool(self.ck_vent.IsChecked)
        P["mark_changes"] = bool(self.ck_mark.IsChecked)
        P["mark_style"] = self._sel(self.cb_markstyle) or MARK_CIRCLE
        P["mark_radius_mm"] = max(1.0, to_float(self.tb_mrad.Text, 6.0))
        P["move_tol_mm"] = max(0.0, to_float(self.tb_mtol.Text, 20.0))

        self.mapping, self.extras, self.labels = {}, [], {}
        for r in self.dt_map.Rows:
            key = str(r["ModelKey"])
            self.labels[key] = self._cell(r, "SchName").strip()
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
        self.links = OrderedDict()   # drafting element id -> [model ids]
        self.tol = doc.Application.ShortCurveTolerance
        self.text_h = 2.5 * MM * self.scale
        if text_type is not None:
            p = text_type.get_Parameter(DB.BuiltInParameter.TEXT_SIZE)
            if p:
                self.text_h = p.AsDouble() * self.scale

    def pm(self, mm):
        return mm * MM * self.scale

    def sym_extent(self, sym, inst, x, y):
        """(left, right, bottom, top) of the symbol graphics relative to its
        insertion point, measured once per type in this view. None if unknown."""
        if not hasattr(self, "_ext"):
            self._ext = {}
        key = int(id_val(sym.Id))
        if key in self._ext:
            return self._ext[key]
        ext = None
        try:
            doc.Regenerate()
            bb = inst.get_BoundingBox(self.view)
            if bb is not None and bb.Max.Y - bb.Min.Y > 1e-9:
                ext = (bb.Min.X - x, bb.Max.X - x, bb.Min.Y - y, bb.Max.Y - y)
        except Exception:
            ext = None
        self._ext[key] = ext
        return ext

    def link(self, el, ids):
        if el is None or not ids:
            return
        out = []
        for i in ids:
            try:
                i = int(i)
            except Exception:
                continue
            if i not in out:
                out.append(i)
        if out:
            self.links[int(id_val(el.Id))] = out

    def line(self, x1, y1, x2, y2, gs, ids=None):
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
        self.link(c, ids)
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

    def text(self, x, y, s, center=False, ids=None):
        if self.tt is None or not s:
            return None
        opts = DB.TextNoteOptions(self.tt.Id)
        opts.HorizontalAlignment = (DB.HorizontalTextAlignment.Center if center
                                    else DB.HorizontalTextAlignment.Left)
        tn = DB.TextNote.Create(doc, self.view.Id, DB.XYZ(x, y, 0), s, opts)
        self.ids.append(tn.Id)
        self.link(tn, ids)
        return tn

    def symbol(self, sym, x, y, ids=None):
        if not sym.IsActive:
            sym.Activate()
            doc.Regenerate()
        inst = doc.Create.NewFamilyInstance(DB.XYZ(x, y, 0), sym, self.view)
        self.ids.append(inst.Id)
        self.link(inst, ids)
        return inst


def _neg_key(k):
    """Sort helper: reverse order of a branch key (number / tuple / text)."""
    try:
        return -float(k)
    except Exception:
        pass
    if isinstance(k, (tuple, list)):
        return tuple(_neg_key(i) for i in k)
    return tuple(-ord(c) for c in u"%s" % (k,))


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
        self.pos = {}           # element id -> (x, y) of its symbol in the drafting view
        self.esys = {}          # element id -> system id (set after construction)
        self.is_vent = lambda sid: False
        # floor waste (FWG / FW): horizontal pipes of upstream fixtures enter BELOW the level
        self.fwg_names = set(t.strip().upper() for t in
                             re.split(r"[,;]", P.get("fwg_names", "FWG, FW") or u"") if t.strip())
        self.fwg_drop = pm(max(0.0, to_float(P.get("fwg_drop_mm", 3.0), 3.0)))
        side = P.get("fwg_single_side", "Left")
        self.fwg_side = -1.0 if side == "Left" else (1.0 if side == "Right" else 0.0)
        self.fwg45 = pm(max(0.0, to_float(P.get("fwg_45_mm", 2.5), 2.5))) if P.get("fwg_45", True) else 0.0
        self._yline = None      # branch line Y under the host being drawn
        self.topo = TOPO
        self.last_tail = ([], None)
        self.lz = {}            # level id -> ProjectElevation (set after construction)
        self.ldepth = {}        # level id -> paper depth used by branches BELOW that level
        self.lup = {}

    def join_lid(self, b):
        """Storey of a sub-stack junction from its REAL model Z.
        Branches are drawn hanging BELOW their level, so a junction that sits ABOVE
        its snapped level (inside the storey above that level) belongs to the next
        drawn level up. A junction at / below a level (within tolerance) keeps it.
        Nothing is ever forced above or below a level: the model Z decides."""
        if b.ez is None or b.lid not in self.lz:
            return b.lid
        eps = 1e-3
        if b.ez <= self.lz[b.lid] + eps:
            return b.lid
        ups = [(z, l) for l, z in self.lz.items() if l in self.ly and z > self.lz[b.lid] + eps]
        if not ups:
            return b.lid                       # above the top drawn level: keep
        return min(ups)[1]

    # ---------- auto level spacing ----------
    def _walk_branches(self, st, out):
        if st.kind == "hmain":
            for _k, kind, obj in st.slots:
                if kind == "r":
                    self._walk_branches(obj, out)
            return out
        for b in st.branches:
            if b.lid in self.ly:
                out.append(b)
            if b.sub is not None:
                self._walk_branches(b.sub, out)
        for r in getattr(st, "risers", []) or []:
            self._walk_branches(r, out)
        return out

    def measure_depth(self, stacks):
        """Depth (below its level) of the lowest branch hanging from every level.
        Independent of the absolute level Y (b.y is laid out relative to its level)."""
        d = {}
        self.lup = {}           # level id -> height used by lifted joins ABOVE that level
        for st in stacks:
            for b in self._walk_branches(st, []):
                dep = self.ly[b.lid] - b.y + self.jch
                if dep > d.get(b.lid, 0.0):
                    d[b.lid] = dep
                if dep < 0:
                    h = -dep + self.jch
                    if h > self.lup.get(b.lid, 0.0):
                        self.lup[b.lid] = h
        self.ldepth = d
        return d

    def expand_levels(self, levels_draw, stacks, id_of):
        """Rebuild level Y: gap(lower -> upper) = max(level spacing,
        depth of branches under upper + free space above lower
        [+ room for a horizontal main sitting in this storey])."""
        pm = self.dr.pm
        free = pm(max(0.0, to_float(self.P.get("lvl_free_mm", 15.0), 15.0)))
        hgap = pm(max(0.0, to_float(self.P.get("hmain_gap_mm", 6.0), 6.0)))
        hclr = pm(max(0.0, to_float(self.P.get("hmain_clear_mm", 3.0), 3.0)))
        ids = [id_of(l) for l in levels_draw]
        zs = [self.lz.get(i) for i in ids]
        mains = [st for st in stacks if st.kind == "hmain"]
        y = 0.0
        new = OrderedDict()
        new[ids[0]] = 0.0
        self.gaps = {}
        for i in range(1, len(ids)):
            lo, up = ids[i - 1], ids[i]
            need = self.ldepth.get(up, 0.0) + free
            if self.lup.get(lo, 0.0) > 0:
                # lifted sub-stack joins of the lowest level sit in this storey
                need = max(need, self.ldepth.get(up, 0.0) + self.bstep + self.lup[lo])
            for hm in mains:
                z = hm.z_low if self.P.get("hmain_z_mode") == HZ_LOW else hm.z_avg
                if z is None or zs[i - 1] is None or zs[i] is None:
                    inside = hm.lid == lo
                else:
                    inside = zs[i - 1] - 1e-6 <= z < zs[i] - 1e-6
                if inside:
                    habv = pm(max(0.0, to_float(self.P.get("hmain_above_mm", 8.0), 8.0)))
                    need = max(need, self.ldepth.get(up, 0.0) + hgap + max(hclr, habv) * 2.0)
            g = max(self.lsp, need)
            y += g
            new[up] = y
            self.gaps[up] = g
        for k, v in new.items():
            self.ly[k] = v

    def y_of_z(self, z):
        """Model Z -> schematic Y, linear interpolation between the two drawn levels
        that bracket Z (extrapolated below the lowest / above the highest level)."""
        pts = sorted((self.lz[l], y) for l, y in self.ly.items() if l in self.lz)
        if z is None or not pts:
            return None
        if len(pts) == 1:
            return pts[0][1]
        if z <= pts[0][0]:
            a, b = pts[0], pts[1]
        elif z >= pts[-1][0]:
            a, b = pts[-2], pts[-1]
        else:
            a, b = pts[0], pts[1]
            for i in range(len(pts) - 1):
                if pts[i][0] <= z <= pts[i + 1][0]:
                    a, b = pts[i], pts[i + 1]
                    break
        dz = b[0] - a[0]
        if abs(dz) < 1e-9:
            return a[1]
        return a[1] + (z - a[0]) / dz * (b[1] - a[1])

    def main_y(self, hm):
        """Y of a horizontal main: real elevation (option) or old 'level - drop'."""
        pm = self.dr.pm
        lid = hm.lid if hm.lid in self.ly else min(self.ly.keys(), key=lambda i: self.ly[i])
        old = self.ly[lid] - pm(self.P["branch_offset_mm"]) - pm(self.P["hmain_drop_mm"])
        if not self.P.get("hmain_real_z", True):
            return old
        z = hm.z_low if self.P.get("hmain_z_mode") == HZ_LOW else hm.z_avg
        y = self.y_of_z(z)
        if y is None:
            return old
        clr = pm(max(0.0, to_float(self.P.get("hmain_clear_mm", 3.0), 3.0)))
        if clr > 0:
            yl = min(self.ly.values(), key=lambda v: abs(v - y))
            if abs(y - yl) < clr:
                y = yl - clr if y <= yl else yl + clr
        if self.ldepth:
            # keep the main BELOW the lowest branch hanging from the level above it
            hgap = pm(max(0.0, to_float(self.P.get("hmain_gap_mm", 6.0), 6.0)))
            above = [(v, l) for l, v in self.ly.items() if v > y + 1e-9]
            below = [v for v in self.ly.values() if v <= y + 1e-9]
            top_lim = None
            if above:
                vu, lu = min(above)
                top_lim = vu - self.ldepth.get(lu, 0.0) - hgap
            bot_lim = (max(below) + max(clr, pm(to_float(self.P.get("hmain_above_mm", 8.0), 8.0)))
                       if below else None)
            if top_lim is not None and y > top_lim:
                # pushed down by the branches above -> centre in the free band,
                # never hugging the level line below
                y = top_lim if bot_lim is None else max(bot_lim, (top_lim + bot_lim) * 0.5)
            elif bot_lim is not None and y < bot_lim:
                y = bot_lim
        return y

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

    def host_ups(self, n):
        """Upstream nodes of a host symbol. A system-change marker (trans, not riser)
        followed by its item gets no own slot (no stub past the host centre); it is
        drawn on that item's drop instead. -> (nodes with slot, {id(item): [markers]})"""
        raw = self.up_items(n)
        out, att, pend = [], {}, []
        for u in raw:
            if u.kind == "conn" and u.trans and not u.riser:
                pend.append(u)
                continue
            if pend:
                if u.kind == "item":
                    att.setdefault(id(u), []).extend(pend)
                else:
                    out.extend(pend)
                pend = []
            out.append(u)
        out.extend(pend)
        return out, att

    def draw_trans_on_drop(self, m, x, y):
        dr, pm = self.dr, self.dr.pm
        gs = self.conn_gs.get(m.sid)
        ids = list(m.ks)
        dr.dot(x, y, pm(0.6), gs)
        dr.text(x + pm(1.2), y + dr.text_h * 0.5, m.label, False, ids)
        for k in ids:
            self.pos.setdefault(int(k), (x, y))
        self.stats["conn"] += 1

    def w_node(self, n):
        if n.kind == "conn":
            return self.sp
        if n.kind == "item":
            ups = self.host_ups(n)[0]
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
            if b.sub is not None:
                b.lid = self.join_lid(b)
            if b.lid in self.ly:
                by.setdefault(b.lid, []).append(b)
        for lid, brs in by.items():
            # flow order: end of the run (far from outlet) -> left, near outlet -> right
            far_first = lambda lst: sorted(lst, key=lambda b: b.key, reverse=True)
            ordered = far_first([b for b in brs if b.sub is not None]) + \
                far_first([b for b in brs if b.sub is None])
            # lay out every sub-stack FIRST: its own branches on this storey must sit
            # ABOVE the point where it joins this stack (else they hang below its foot)
            for b in ordered:
                if b.sub is not None:
                    self.slots(b.sub)
            cur = self.ly[lid] - self.dr.pm(self.P["branch_offset_mm"])
            for b in reversed(ordered):
                depth = 0 if b.sub is not None else tier_depth(b.nodes)
                b.y = cur - depth * self.tstep
                if b.sub is None and self.fwg45 > self.dr.tol and self.has_fwg(b.nodes):
                    # room for the 45 deg inlet into FWG / FW above the branch line
                    sy0 = self.ly[lid] + self.dr.pm(self.P["symbol_offset_mm"])
                    need = self.fwg_drop + self.fwg45 + self.ch + self.dr.pm(1.0) + depth * self.tstep
                    b.y = min(b.y, sy0 - need)
                if b.sub is not None:
                    low = self.sub_low(b.sub, lid)
                    if low is not None:
                        b.y = min(b.y, low - self.bstep - self.jch)
                cur = b.y - self.bstep - self.jch
            by[lid] = ordered
            for b in ordered:
                if b.sub is not None:
                    b.sub.bottom_y = b.y
        st.by_level = by

    def sub_low(self, st, lid):
        """Lowest branch Y of stack st (and its sub-stacks) hanging from level lid."""
        ys = []
        for b in st.by_level.get(lid, []) if getattr(st, "by_level", None) else []:
            ys.append(b.y)
            if b.sub is not None:
                v = self.sub_low(b.sub, lid)
                if v is not None:
                    ys.append(v)
        return min(ys) if ys else None

    def place(self, st, x):
        subs = [b for b in st.branches if b.sub is not None and b.lid in self.ly]
        if self.P.get("no_cross", True):
            # junction LOWEST on the stack -> left-most. Its horizontal join then passes
            # BELOW the bottom of every sub-stack placed to its right (they all join
            # higher), so a branch never crosses another riser. Tie: farthest left.
            subs = sorted(subs, key=lambda b: (round(b.y if b.y is not None else 0.0, 6),
                                               _neg_key(b.key)))
        else:
            subs = sorted(subs, key=lambda b: b.key, reverse=True)  # farthest sub-stack left-most
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
        hm.y_main = self.main_y(hm)
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
        main = list(hm.main or [])
        idx = dict((k, i) for i, k in enumerate(main))
        cuts = [(hm.x_out, 0)]
        for (_k, kind, obj), xr in zip(hm.slots, hm.pos):
            i = idx.get(getattr(obj, "attach", None))
            if i is not None and x_min < xr < hm.x_out:
                cuts.append((xr, i))
        cuts.append((x_min, len(main) - 1 if main else 0))
        for (xa, ia), (xb, ib) in zip(cuts, cuts[1:]):
            lo, hi = min(ia, ib), max(ia, ib)
            ids = [k for k in main[lo:hi + 1] if k not in self.topo.items]
            dr.line(xb, y, xa, y, gs, ids)                       # reference main (split)
        a = pm(1.5)                                              # flow arrow at outlet
        dr.line(hm.x_out - a, y + a * 0.5, hm.x_out, y, gs, main[:1])
        dr.line(hm.x_out - a, y - a * 0.5, hm.x_out, y, gs, main[:1])
        dr.text(hm.x_out - pm(2), y - pm(4), hm.label)

    # ---------- primitives ----------
    def drop(self, x, y_top, y_line, gs, ids=None):
        """Fixture drop -> 45 deg into the horizontal branch. Short drops (e.g. into a
        sub-branch tier just under the level) get a smaller 45 deg segment instead of
        a straight vertical, so EVERY drop enters its branch at 45 deg."""
        span = y_top - y_line
        c = min(self.ch, span * 0.5) if self.ch > self.dr.tol else 0.0
        if c > self.dr.tol:
            self.dr.line(x, y_top, x, y_line + c, gs, ids)
            self.dr.line(x, y_line + c, x + c, y_line, gs, ids)
            return x + c
        self.dr.line(x, y_top, x, y_line, gs, ids)
        return x

    def join_stack(self, xa, y, xs, gs, ids=None):
        """Horizontal branch -> 45 deg -> riser / stack. Returns y on the riser."""
        c = self.jch
        if c > self.dr.tol:
            c = min(c, max(xs - xa, self.dr.tol * 2) + self.bgap * 0.5)
            self.dr.line(min(xa, xs - c), y, xs - c, y, gs, ids)
            self.dr.line(xs - c, y, xs, y - c, gs, ids)
            return y - c
        self.dr.line(xa, y, xs, y, gs, ids)
        return y

    def put_symbol(self, en, ex, sy, center_label=True):
        dr, P, pm = self.dr, self.P, self.dr.pm
        n = en["count"]
        for k in en.get("ks") or []:
            self.pos[int(k)] = (ex, sy)
        lab = en["label"] + (u" x" + str(n) if n > 1 else u"")
        ks = list(en.get("ks") or [])
        sym = self.sym_map.get(en["key"])
        if sym is not None:
            inst = dr.symbol(sym, ex, sy, ks)
            self.stats["placed"] += n
            if P["add_label"]:
                gap = pm(P.get("label_gap_mm", 1.5))
                ext = dr.sym_extent(sym, inst, ex, sy)
                if center_label:
                    if ext is not None:
                        tx = ex + (ext[0] + ext[1]) * 0.5
                        ty = sy + max(ext[3], 0.0) + gap + dr.text_h
                    else:
                        tx, ty = ex, sy + pm(P["label_offset_mm"]) + dr.text_h
                    dr.text(tx, ty, lab, True, ks)
                else:
                    tx = ex + (max(ext[1], 0.0) if ext is not None else 0.0) + max(gap, pm(1))
                    dr.text(tx, sy + dr.text_h * 0.5, lab, False, ks)
            return True
        if P["placeholder"]:
            if center_label:
                dr.text(ex, sy + pm(2) + dr.text_h, lab or "?", True, ks)
            else:
                dr.text(ex + pm(2), sy + dr.text_h * 0.5, lab or "?", False, ks)
            self.stats["placeholder"] += n
            return True
        self.stats["skipped"] += n
        return False

    def draw_conn(self, n, x0, y0, x1, y1):
        dr, pm = self.dr, self.dr.pm
        gs = self.conn_gs.get(n.sid)
        ids = list(n.ks)
        if n.trans and not n.riser:
            dr.dot(x0, y0, pm(0.6), gs)
            dr.text(x0, y0 + pm(1) + dr.text_h, n.label, True, ids)
            self.stats["conn"] += 1
            return
        dr.line(x0, y0, x1, y1, gs, ids)
        if abs(x1 - x0) < 1e-9:
            dr.text(x1, y1 + pm(1) + dr.text_h, n.label, True, ids)
        else:
            dr.text(x1 + pm(1), y1 + dr.text_h * 0.5, n.label, False, ids)
        self.stats["riser"] += 1

    # ---------- drawing ----------
    def is_fwg(self, n):
        """Host symbol whose Schematic Name (label) is FWG / FW ..."""
        if n is None or n.kind != "item" or not self.fwg_names:
            return False
        return (n.label or u"").strip().upper() in self.fwg_names

    def has_fwg(self, nodes):
        """True when a FWG / FW host with upstream inlets is inside these nodes."""
        for n in nodes or []:
            if n.kind == "item" and self.is_fwg(n) and self.host_ups(n)[0]:
                return True
            if self.has_fwg(n.children):
                return True
        return False

    def fw_inlet(self, ucx, cx, hy, s, c, gs, ids, done45):
        """Horizontal pipe from an upstream drop at the inlet level (hy), ending with a
        45 deg segment falling DOWN into the FWG / FW drop (flow direction correct).
        c = 0 -> old straight horizontal into the FWG centre."""
        tol = self.dr.tol
        xk = cx + s * c                     # start of the 45 deg segment
        if abs(ucx - xk) > tol:
            self.dr.line(ucx, hy, xk, hy, gs, ids)
        if c > tol and s not in done45:
            self.dr.line(xk, hy, cx, hy - c, gs, ids)
            done45.add(s)

    def draw_item(self, n, cx, sy, gs, loose):
        ok = self.put_symbol({"key": n.key, "label": n.label, "count": n.count, "ks": n.ks},
                             cx, sy)
        gs = self.g(n.sid, gs)
        y_line = self._yline            # branch line under this host (None = unknown)
        self._yline = None              # upstream items never inherit it
        ups, att = self.host_ups(n)
        if ups:
            tol = self.dr.tol
            uy = sy + self.rise
            fwg = self.is_fwg(n)
            # host = FWG / FW -> inlet point slightly BELOW the level line
            hy = sy - self.fwg_drop if (fwg and self.fwg_drop > tol) else sy
            total = sum(self.w_node(u) for u in ups)
            xr = cx + total * 0.5
            # FWG / FW with ONE inlet: shift it half a slot aside
            if fwg and len(ups) == 1 and self.fwg_side:
                xr += self.fwg_side * self.sp * 0.5
            ucs = []
            for u in ups:
                wu = self.w_node(u)
                ucs.append((u, xr - wu * 0.5))
                xr -= wu
            # 45 deg inlet size per side (-1 left, +1 right) of the FWG / FW
            c45 = {-1: 0.0, 1: 0.0}
            if fwg and self.fwg45 > tol and not loose:
                lim = self.fwg45
                if y_line is not None and hy > y_line:
                    # 45 deg end point (hy - c) stays above the FWG drop chamfer / branch
                    lim = min(lim, hy - y_line - self.ch - self.dr.pm(1.0))
                for s in (-1, 1):
                    ds = [abs(x - cx) for _u, x in ucs if (x - cx) * s > tol]
                    if ds and lim > tol:
                        c45[s] = min(lim, min(ds))                  # always exactly 45 deg
            done45 = set()
            for u, ucx in ucs:
                s = 1 if ucx > cx + tol else (-1 if ucx < cx - tol else 0)
                c = c45.get(s, 0.0) if s else 0.0
                yh = hy                         # horizontal stays at inlet level
                if u.kind == "conn":
                    if not loose:
                        self.fw_inlet(ucx, cx, hy, s, c, gs, list(u.ks), done45)
                        self.draw_conn(u, ucx, yh, ucx, uy)
                elif self.draw_item(u, ucx, uy, gs, loose) and not loose:
                    ugs = self.g(u.sid, gs)
                    uids = []
                    for uk in u.ks:
                        for x in self.topo.path_to(uk, n.k):
                            if x not in uids:
                                uids.append(x)
                    self.dr.line(ucx, uy, ucx, yh, ugs, uids)          # fixture drop
                    self.fw_inlet(ucx, cx, hy, s, c, ugs, uids, done45)
                    for i, m in enumerate(att.get(id(u)) or []):      # system change on the drop
                        f = min(0.35 + 0.2 * i, 0.8)
                        self.draw_trans_on_drop(m, ucx, yh + (uy - yh) * f)
        return ok

    def draw_list(self, nodes, y_line, x_right, sy, gs, loose, base=True, top=True):
        """Draw nodes right -> left; base line split at every drop, each piece
        linked to the model pipes between the two junctions."""
        topo = self.topo
        x = x_right
        x_min = x_right
        breaks = []
        drops = []
        for n in nodes:
            w = self.w_node(n)
            ngs = self.g(n.sid, gs)
            cx = x - w * 0.5
            if n.kind == "item":
                self._yline = y_line
                if self.draw_item(n, cx, sy, gs, loose) and not loose:
                    arm, jn = topo.arms(n.ks)
                    xd = self.drop(cx, sy, y_line, ngs, arm)
                    x_min = min(x_min, xd)
                    drops.append((xd, jn))
            elif n.kind == "conn":
                if not loose:
                    top_y = max(sy + self.rise * 0.5, y_line + self.rise * 0.5)
                    self.draw_conn(n, cx, y_line, cx, top_y)
                    x_min = min(x_min, cx)
                    drops.append((cx, n.k))
                    if n.switch:
                        breaks.append((cx, ngs))
            else:
                yj = y_line + self.tstep
                xd = x - self.tstep
                self.draw_list(n.children, yj, xd, sy, ngs, loose, True, False)
                tail_ids, tail_jn = self.last_tail
                if not loose:
                    self.dr.line(xd, yj, x, y_line, ngs, tail_ids)
                    x_min = min(x_min, x)
                    drops.append((x, tail_jn))
            x -= w
        j0 = drops[0][1] if drops else None
        tail = (topo.between(j0, None), None) if top else topo.up_to_junction(j0)
        if not loose and base:
            xs = [x_right, x_min] + [d[0] for d in drops if x_min < d[0] < x_right] + \
                 [bx for bx, _g in breaks if x_min < bx < x_right]
            cuts = sorted(set(round(v, 9) for v in xs), reverse=True)
            for xa, xb in zip(cuts, cuts[1:]):
                mid = (xa + xb) * 0.5
                cgs = gs
                for bx, bgs in breaks:
                    if bx > x_min and mid < bx:
                        cgs = bgs
                right = [i for i, d in enumerate(drops) if d[0] >= xa - 1e-9]
                if not right:
                    ids = tail[0]
                else:
                    i = right[-1]
                    ids = topo.between(drops[i + 1][1], drops[i][1]) if i + 1 < len(drops) else []
                self.dr.line(xb, y_line, xa, y_line, cgs, ids)
        self.last_tail = tail
        return x_min

    def seg_gs(self, ids, gs):
        """Graphics of a stack piece = system of ITS OWN pipes (vent part -> vent colour)."""
        sids = [self.esys.get(k) for k in ids if self.esys.get(k) is not None]
        if not sids:
            return gs
        dr_ = [i for i in sids if not self.is_vent(i)]
        if dr_:                       # any drainage pipe in this piece -> drainage colour
            return gs
        return self.g(max(set(sids), key=sids.count), gs)

    def stack_line(self, st, y0, y1, gs, y_vent=None, y_dtop=None):
        """Stack line split at every level; each piece links the stack pipes of that storey
        and takes the colour of those pipes. Above y_vent (top drainage junction of a
        stack that continues as a vent) the line is drawn in the vent colour."""
        main = [k for k in (st.main or []) if k not in self.topo.items]
        lv = sorted(self.ly.items(), key=lambda kv: kv[1])
        cut_pts = [v for _l, v in lv if y0 + 1e-9 < v < y1 - 1e-9]
        if y_vent is not None and y0 + 1e-9 < y_vent < y1 - 1e-9:
            cut_pts.append(y_vent)
        cuts = [y0] + sorted(set(cut_pts)) + [y1]
        vgs = self.g(getattr(st, "vent_sid", None), gs) if y_vent is not None else None
        for ya, yb in zip(cuts, cuts[1:]):
            below = [l for l, v in lv if v <= ya + 1e-9]
            lid = below[-1] if below else None
            ids = [k for k in main if self.topo.zl.get(k) == lid] if lid is not None else []
            if vgs is not None and ya >= y_vent - 1e-9:
                vids = [k for k in ids if self.is_vent(self.esys.get(k))]
                self.dr.line(st.stack_x, ya, st.stack_x, yb, vgs, vids or ids or main)
            elif y_dtop is not None and yb <= y_dtop + 1e-9:
                # below the top branch junction the stack carries discharge -> drainage colour
                self.dr.line(st.stack_x, ya, st.stack_x, yb, gs, ids or main)
            else:
                self.dr.line(st.stack_x, ya, st.stack_x, yb, self.seg_gs(ids, gs), ids or main)

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
                    # sub-stack ends with its own 45 deg foot -> horizontal starts after it
                    xa = b.sub.stack_x + (self.jch if self.jch > dr.tol else 0.0)
                    ys.append(self.join_stack(xa, b.y, st.stack_x, bgs,
                                              [k for k in b.sub.main[:2]
                                               if k not in self.topo.items]))
                    if P["show_branch_tag"]:
                        dr.text(b.sub.stack_x + pm(1), b.y - pm(0.8), u"B" + str(b.no))
                    xr -= w
                    continue
                x_right = xr - self.bgap * 0.5
                left = self.draw_list(b.nodes, b.y, x_right, sy, bgs, loose)
                if not loose:
                    ys.append(self.join_stack(x_right, b.y, st.stack_x, bgs, self.last_tail[0]))
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
                    self.put_symbol({"key": n.key, "label": n.label, "count": n.count,
                                     "ks": n.ks},
                                    st.stack_x, y, center_label=False)
                ys.append(y)

        if loose:
            y0 = min(self.ly.values()) - self.lsp * 0.3
            dr.text((st.x0 + st.stack_x) * 0.5, y0, st.label, True)
        elif ys:
            y_bot = st.bottom_y if st.bottom_y is not None else min(ys) - self.lsp * 0.15
            y_top = max(ys)
            y_dtop = y_top
            y_vent = None
            vent_top = (st.is_root and st.meta.get("cls") == "Vent") or getattr(st, "vent_top", False)
            if vent_top and P["vent_to_atm"]:
                # drainage stack continuing as a vent: vent colour above the top junction
                if getattr(st, "vent_sid", None) is not None and not self.is_vent(st.sid):
                    y_vent = y_top
                y_top = self.top_y + self.lsp * 0.6
                dr.text(st.stack_x, y_top + pm(2) + dr.text_h, "VENT TO ATMOSPHERE", True)
            c = self.jch
            # 45 deg foot at the LOWEST point of every riser that turns into a horizontal
            # (horizontal main OR the branch of a parent stack). Short risers get a smaller
            # 45 deg + a short horizontal so the parent line still starts at stack_x + jch.
            foot = st.bottom_y is not None and c > dr.tol and (y_top - y_bot) > dr.tol
            cf = min(c, (y_top - y_bot) * 0.5) if foot else 0.0
            self.stack_line(st, y_bot + cf if foot else y_bot, y_top, gs, y_vent, y_dtop)
            if foot:
                fids = [k for k in st.main[:2] if k not in self.topo.items]
                dr.line(st.stack_x, y_bot + cf, st.stack_x + cf, y_bot, gs, fids)
                if c - cf > dr.tol:
                    dr.line(st.stack_x + cf, y_bot, st.stack_x + c, y_bot, gs, fids)
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
# CHANGE LOG  (snapshot of every Element ID per run + diff with previous run)
# ----------------------------------------------------------------------------
def _fnv(s):
    """Stable short hash (same result in every session / engine)."""
    h = 2166136261
    for ch in (s or u""):
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return "%08x" % h


def _safe(s, n):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s or "X").strip("_")[:n] or "X"


def log_base_name(name):
    """'HYD SCHEMATIC (2)' -> 'HYD SCHEMATIC' so new copies share one log."""
    return re.sub(r"\s*\(\d+\)$", "", (name or u"").strip()) or DEFAULTS["new_view_name"]


def log_dir(dkey, sch_name):
    sch_name = log_base_name(sch_name)
    mdl = os.path.splitext(os.path.basename(dkey or "model"))[0]
    return os.path.join(LOG_ROOT, _safe(mdl, 40) + "_" + _fnv(dkey),
                        _safe(sch_name, 60) + "_" + _fnv(sch_name))


def load_last_log(path):
    f = os.path.join(path, "last.json")
    if not os.path.exists(f):
        return None
    try:
        with open(f, "r") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("elements"), dict):
            return data
    except Exception:
        pass
    return None


def save_log(path, data, keep):
    try:
        if not os.path.exists(path):
            os.makedirs(path)
        txt = json.dumps(data, indent=0, sort_keys=True)
        with open(os.path.join(path, "run_" + data["stamp"] + ".json"), "w") as fh:
            fh.write(txt)
        with open(os.path.join(path, "last.json"), "w") as fh:
            fh.write(txt)
        runs = sorted(f for f in os.listdir(path) if f.startswith("run_") and f.endswith(".json"))
        for f in runs[:max(0, len(runs) - max(1, keep))]:
            try:
                os.remove(os.path.join(path, f))
            except Exception:
                pass
        return True
    except Exception as ex:
        output.print_md(u"**Warning:** cannot save change log: " + str(ex))
        return False


_LOGINFO_CACHE = {}


def log_info_text(dkey, sch_name):
    path = log_dir(dkey, sch_name)
    f = os.path.join(path, "last.json")
    if not os.path.exists(f):
        return u"Log '" + log_base_name(sch_name) + u"': no previous log - this run = baseline"
    try:
        mt = os.path.getmtime(f)
    except Exception:
        mt = 0
    if _LOGINFO_CACHE.get(path, (None,))[0] != mt:
        d = load_last_log(path) or {}
        _LOGINFO_CACHE[path] = (mt, d.get("time", "?"), len(d.get("elements", {})))
    _mt, tm, n = _LOGINFO_CACHE[path]
    return (u"Log '" + log_base_name(sch_name) + u"': previous run " + tm +
            u"  (" + str(n) + u" elements)")


def neighbour_ids(el):
    k = int(id_val(el.Id))
    res = set()
    for c in get_connectors(el):
        try:
            if not c.IsConnected:
                continue
            for r in c.AllRefs:
                ow = r.Owner
                if ow is None or isinstance(ow, DB.MEPSystem):
                    continue
                if r.ConnectorType == DB.ConnectorType.Logical:
                    continue
                o = int(id_val(ow.Id))
                if o != k:
                    res.add(o)
        except Exception:
            continue
    return res


def conn_sizes_mm(el):
    out = set()
    for c in get_connectors(el):
        try:
            if c.Shape == DB.ConnectorProfileType.Round:
                out.add(int(round(c.Radius * 2.0 / MM)))
        except Exception:
            pass
    return sorted(out)


def snap_points_mm(el):
    cp = curve_pts(el)
    pts = list(cp) if cp else ([elem_pt(el)] if elem_pt(el) is not None else [])
    return [[int(round(p.X / MM)), int(round(p.Y / MM)), int(round(p.Z / MM))] for p in pts]


def pts_shift(a, b):
    """Max point displacement (mm) between two snapshots, None if not comparable."""
    if not a or not b or len(a) != len(b):
        return None

    def dist(p, q):
        return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2) ** 0.5

    d1 = max(dist(p, q) for p, q in zip(a, b))
    if len(a) == 2:
        d1 = min(d1, max(dist(a[0], b[1]), dist(a[1], b[0])))
    return d1


def diff_logs(prev_el, cur_el, tol_mm):
    """-> OrderedDict  id(str) -> (status NEW / MOD / DEL, [reasons])"""
    ch = OrderedDict()
    for sk, c in cur_el.items():
        p = prev_el.get(sk)
        if p is None:
            ch[sk] = ("NEW", [u"added"])
            continue
        rs = []
        if p.get("t") != c.get("t"):
            rs.append(u"type: " + (p.get("t") or u"?") + u" -> " + (c.get("t") or u"?"))
        elif p.get("tm") != c.get("tm"):
            rs.append(u"type mark: " + (p.get("tm") or u"-") + u" -> " + (c.get("tm") or u"-"))
        if p.get("sz") != c.get("sz"):
            rs.append(u"size: " + str(p.get("sz")) + u" -> " + str(c.get("sz")))
        if p.get("s") != c.get("s"):
            rs.append(u"system: " + u", ".join(p.get("s") or []) + u" -> " +
                      u", ".join(c.get("s") or []))
        if p.get("l") != c.get("l"):
            rs.append(u"level: " + (p.get("l") or u"?") + u" -> " + (c.get("l") or u"?"))
        d = pts_shift(p.get("p"), c.get("p"))
        if d is None:
            if p.get("p") != c.get("p"):
                rs.append(u"geometry")
        elif d > tol_mm:
            rs.append(u"moved " + str(int(round(d))) + u" mm")
        if (p.get("n") or []) != (c.get("n") or []):
            rs.append(u"connection")
        if rs:
            ch[sk] = ("MOD", rs)
    for sk in prev_el:
        if sk not in cur_el:
            ch[sk] = ("DEL", [u"removed / out of scope"])
    return ch


def mark_targets(changes, pos, prev_el, cur_el, max_hops=60):
    """Changed element -> drafting position to circle.
    Drawn item                  -> its own symbol.
    Pipe / fitting / not drawn  -> nearest drawn symbol(s) through the pipework.
    Removed drawn item          -> its position in the previous schematic.
    Returns list of dicts {x, y, st:set, ids:list}, merged by position."""
    adj = {}
    for sk, c in cur_el.items():
        k = int(sk)
        for o in c.get("n") or []:
            if str(o) in cur_el:
                adj.setdefault(k, set()).add(o)
                adj.setdefault(o, set()).add(k)

    def nearest(starts):
        seen = set(starts)
        front = list(starts)
        for _h in range(max_hops):
            hit = [k for k in front if k in pos]
            if hit:
                return hit[:3]
            nxt = []
            for k in front:
                for o in adj.get(k, ()):
                    if o not in seen:
                        seen.add(o)
                        nxt.append(o)
            if not nxt:
                return []
            front = nxt
        return []

    marks = OrderedDict()

    def add(x, y, st, sk):
        key = (round(x, 3), round(y, 3))
        m = marks.get(key)
        if m is None:
            m = {"x": x, "y": y, "st": set(), "ids": []}
            marks[key] = m
        m["st"].add(st)
        m["ids"].append(sk)

    unplaced = []
    for sk, (st, _rs) in changes.items():
        k = int(sk)
        if st == "DEL":
            p = prev_el.get(sk, {})
            if p.get("sx") is not None and p.get("sy") is not None:
                add(p["sx"], p["sy"], st, sk)
                continue
            starts = [o for o in (p.get("n") or []) if str(o) in cur_el]
        else:
            starts = [k]
        hits = nearest(starts) if starts else []
        if not hits:
            unplaced.append(sk)
            continue
        for h in hits:
            add(pos[h][0], pos[h][1], st, sk)
    return list(marks.values()), unplaced


def latest_open_revision():
    best = None
    try:
        for i in DB.Revision.GetAllRevisionIds(doc):
            r = doc.GetElement(i)
            if r is not None and not r.Issued:
                best = r
    except Exception:
        return None
    return best.Id if best is not None else None


def draw_marks(dr, marks, P, warn):
    """Circle / cloud each changed position + tag 'C<n> NEW/MOD/DEL'."""
    if not marks:
        return 0
    r = dr.pm(P["mark_radius_mm"])
    gs = get_or_create_line_style(MARK_STYLE_NAME, DB.Color(*MARK_COLOR), 5)
    rev_id = None
    if P["mark_style"] == MARK_CLOUD:
        rev_id = latest_open_revision()
        if rev_id is None:
            warn.append(u"No un-issued revision in the project - changes marked with circles.")
    for i, m in enumerate(marks):
        x, y = m["x"], m["y"]
        done = False
        if rev_id is not None:
            try:
                p = [DB.XYZ(x - r, y - r, 0), DB.XYZ(x + r, y - r, 0),
                     DB.XYZ(x + r, y + r, 0), DB.XYZ(x - r, y + r, 0)]
                crv = List[DB.Curve]([DB.Line.CreateBound(p[j], p[(j + 1) % 4]) for j in range(4)])
                rc = DB.RevisionCloud.Create(doc, dr.view, rev_id, crv)
                dr.ids.append(rc.Id)
                done = True
            except Exception as ex:
                if not warn or not warn[-1].startswith(u"Revision cloud"):
                    warn.append(u"Revision cloud failed (" + str(ex) + u") - circles used.")
        if not done:
            dr.dot(x, y, r, gs)
        st = u"/".join(s for s in ("NEW", "MOD", "DEL") if s in m["st"])
        m["no"] = i + 1
        dr.text(x + r * 0.75, y + r * 0.75 + dr.text_h, u"C" + str(i + 1) + u" " + st)
    return len(marks)


def to_eid(k):
    try:
        if int(doc.Application.VersionNumber) >= 2024:
            return DB.ElementId(System.Int64(k))
    except Exception:
        pass
    return DB.ElementId(int(k))


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
    TOPO.__init__()
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
    slabels = settings["labels"]
    map_rows = [(cats[mk], mk, counts[mk], smap.get(mk, NONE_OPT), False, slabels.get(mk, u""))
                for mk in sorted(counts.keys(), key=lambda m: (cats[m], nat_key(m)))]
    for mk in settings["extras"]:
        if mk in extra_cands and mk not in counts:
            map_rows.append((extra_cands[mk][0], mk, extra_cands[mk][1],
                             smap.get(mk, NONE_OPT), True, slabels.get(mk, u"")))
    dkey = doc_key()

    dviews = collect_drafting_views()
    lstyles = collect_line_styles()
    ttypes = collect_text_types()

    frm = SchematicWindow(P, sys_rows, map_rows, extra_cands, smap, sym_options,
                          sorted(dviews.keys(), key=nat_key),
                          sorted(lstyles.keys(), key=nat_key),
                          sorted(ttypes.keys(), key=nat_key),
                          saved_labels=slabels,
                          log_info=lambda nm: log_info_text(dkey, nm))
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
    for mk, v in frm.labels.items():
        if v:
            slabels[mk] = v
        elif mk in slabels:
            del slabels[mk]
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
        n.label = slabels.get(MKEY[k]) or label_text(scope[k])
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
        TOPO.items = set(sym_ids)
        for _k in scope:
            _z = zof(_k)
            if _z is not None:
                TOPO.zl[_k] = level_of_z(_z)

        def mk_conns(k):
            if ctx.net is None or KIND.get(k) == "fix":
                return []
            out = []
            for csid, sobj, org in ctx.net.cross.get(k, []):
                if not ensure_sys(csid, sobj):
                    continue
                # option OFF: only vent risers are kept (connection points hidden)
                if not P["show_xconn"] and not is_vent(csid):
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
                    tree = Tree(subnet, root, root_pt, KIND, ctx.stk, TOPZ,
                                vent_k=lambda k: KIND.get(k) != "fix" and is_vent(ESYS.get(k)))
                    tree.is_item = ctx.is_item
                    TOPO.add_tree(tree)
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
                    TOPO.add_tree(tree)
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

    # ---- change log: snapshot of every element ID in scope ----
    chk_sids = set(sd.id for sd in sysdata.values() if sd.name in checked)

    def in_log(k):
        ids = SYS_IDS.get(k) or set()
        if not ids:
            return NO_SYS in checked
        return bool(ids & chk_sids)

    sys_name_by_id = dict((sd.id, sd.name) for sd in sysdata.values())
    cur_el = OrderedDict()
    for k, e in scope.items():
        if not in_log(k):
            continue
        try:
            lv = resolve_level(e, levels, P["level_method"], tol)
            rec = {
                "c": KIND.get(k),
                "cat": cat_name(e),
                "t": MKEY.get(k) or model_key(e),
                "tm": label_text(e),
                "sz": conn_sizes_mm(e),
                "s": sorted(sys_name_by_id.get(i, str(i)) for i in SYS_IDS.get(k, ())),
                "l": lv.Name if lv is not None else u"",
                "p": snap_points_mm(e),
                "n": sorted(neighbour_ids(e)),
            }
            if KIND.get(k) in ("fix", "acc", "fit"):
                rec["lb"] = slabels.get(MKEY[k]) or rec["tm"]
            cur_el[str(k)] = rec
        except Exception:
            continue
    log_name = log_base_name(P["target_view"] if (P["view_mode"] == VIEW_OVER and
                                                 P["target_view"] in dviews)
                             else P["new_view_name"])
    log_path = log_dir(dkey, log_name)
    prev_log = load_last_log(log_path)
    changes = diff_logs(prev_log["elements"], cur_el, P["move_tol_mm"]) if prev_log else None
    marks, unplaced = [], []

    # ---- transaction ----
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
        lay.esys = ESYS
        lay.lz = dict((id_val(l.Id), l.ProjectElevation) for l in levels_draw)
        lay.is_vent = is_vent

        def _place_all():
            xx = 0.0
            for st in stacks:
                if st.kind == "hmain":
                    xx = lay.place_h(st, xx)
                else:
                    lay.slots(st)
                    xx = lay.place(st, xx)
            return xx

        x = _place_all()
        if P.get("auto_lsp", True) and len(levels_draw) > 1:
            lay.measure_depth(stacks)                 # pass 1: how deep is every level?
            lay.expand_levels(levels_draw, stacks, lambda l: id_val(l.Id))
            lay.top_y = ly[id_val(top.Id)]
            x = _place_all()                          # pass 2: final layout
            grown = [l.Name for l in levels_draw
                     if lay.gaps.get(id_val(l.Id), 0.0) > lsp + 1e-9]
            if grown:
                output.print_md(u"Level spacing expanded below: **" + u", ".join(grown) + u"**")
        for st in stacks:
            lay.draw(st)

        level_gs = lstyles.get(P["level_line_style"])
        x_left = -pm(P["level_text_width_mm"])
        for l in levels_draw:
            y = ly[id_val(l.Id)]
            dr.line(x_left, y, x, y, level_gs)
            dr.text(x_left, y + dr.text_h * 3.0 + pm(1),
                    l.Name + u"\rRL " + u"{:+.3f}".format(l.Elevation * 0.3048))

        n_links = write_links(dr.links, warn)       # model IDs -> drafting elements
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
        # marks are NOT grouped (easy to delete), but tracked for overwrite clean-up
        if P["mark_changes"] and changes:
            marks, unplaced = mark_targets(changes, lay.pos, prev_log["elements"], cur_el)
            draw_marks(dr, marks, P, warn)
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

    # ---- save change log (schematic position of every drawn element) ----
    for k, (sx, sy) in lay.pos.items():
        rec = cur_el.get(str(k))
        if rec is not None:
            rec["sx"], rec["sy"] = sx, sy
    now = System.DateTime.Now
    log_data = {
        "time": now.ToString("yyyy-MM-dd HH:mm:ss"),
        "stamp": now.ToString("yyyyMMdd_HHmmss"),
        "user": doc.Application.Username,
        "model": dkey,
        "schematic": log_name,
        "view": sview.Name,
        "view_uid": sview.UniqueId,
        "systems": sorted(checked),
        "elements": cur_el,
        "draw": dict((str(k), v) for k, v in dr.links.items()),
    }
    log_saved = save_log(log_path, log_data, int(P.get("log_keep", 20)))

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
                        u"** | System change points: **" + str(s["conn"]) + u"**" +
                        u" | Linked drafting elements: **" + str(n_links) + u"**")
        if P["order_by"] == ORDER_NET:
            output.print_md(u"Sequence: `>` start -> end, `{...}` sub-branch tier, "
                            u"`[...]` drains into item, `<VP^>` vent riser, `<SD>` system change. "
                            u"Main: **" + P["main_mode"] + u"**, grouping: **" + P["net_mode"] + u"**.")
        for root_st in stacks:
            if root_st.kind == "hmain":
                z = root_st.z_low if P.get("hmain_z_mode") == HZ_LOW else root_st.z_avg
                if z is not None:
                    output.print_md(u"Horizontal main **" + root_st.label + u"**: RL " +
                                    u"{:+.3f}".format(z * 0.3048) + u" (project origin) - " +
                                    (u"drawn at real elevation" if P.get("hmain_real_z", True)
                                     else u"drawn at level - drop"))
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

    # ---- change report ----
    try:
        output.print_md(u"---")
        output.print_md(u"## Change log - **" + log_name + u"**")
        output.print_md(u"Log folder: `" + log_path + u"`" +
                        (u"" if log_saved else u"  (**not saved**)"))
        if prev_log is None:
            output.print_md(u"No previous log - this run (" + str(len(cur_el)) +
                            u" elements) is saved as the **baseline**. "
                            u"Changes will be detected from the next run.")
            return
        cnt = {"NEW": 0, "MOD": 0, "DEL": 0}
        for st, _rs in changes.values():
            cnt[st] += 1
        output.print_md(u"Compared with run **" + prev_log.get("time", "?") + u"** (" +
                        prev_log.get("user", "?") + u", " + str(len(prev_log["elements"])) +
                        u" elements)  ->  now " + str(len(cur_el)) + u" elements.  " +
                        u"**NEW: " + str(cnt["NEW"]) + u"  |  MOD: " + str(cnt["MOD"]) +
                        u"  |  DEL: " + str(cnt["DEL"]) + u"**")
        if not changes:
            output.print_md(u"No change since the previous run.")
            return
        if set(prev_log.get("systems") or []) != set(checked):
            output.print_md(u"**Note:** ticked systems differ from the previous run - "
                            u"elements of un-ticked systems are reported as DEL / NEW.")
        mark_of = {}
        for m in marks:
            for sk in m["ids"]:
                mark_of.setdefault(sk, []).append(u"C" + str(m.get("no", "?")))
        if P["mark_changes"]:
            output.print_md(u"Marked positions in view: **" + str(len(marks)) + u"**" +
                            (u"  |  not placeable: " + str(len(unplaced)) if unplaced else u""))
        rows = []
        order = {"NEW": 0, "MOD": 1, "DEL": 2}
        for sk, (st, rs) in sorted(changes.items(), key=lambda kv: (order[kv[1][0]], int(kv[0]))):
            rec = cur_el.get(sk) or prev_log["elements"].get(sk) or {}
            if st == "DEL":
                eid = sk
            else:
                try:
                    eid = output.linkify(to_eid(int(sk)))
                except Exception:
                    eid = sk
            rows.append([u", ".join(mark_of.get(sk, [])) or u"-", st, eid,
                         rec.get("cat", u"?"), rec.get("lb") or rec.get("t", u"?"),
                         rec.get("l", u""), u"; ".join(rs)])
        output.print_table(table_data=rows[:1000],
                           columns=["Mark", "Status", "Element ID", "Category",
                                    "Type / Label", "Level", "Change"])
        if len(rows) > 1000:
            output.print_md(u"... " + str(len(rows) - 1000) + u" more (see log files).")
    except Exception as ex:
        output.print_md(u"Change report could not be printed: " + str(ex))


main()
