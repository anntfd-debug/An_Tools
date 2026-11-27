# -*- coding: utf-8 -*-
__title__ = "Scan\nDetail Item"
__doc__ = ("Chon 1 detail item mau trong link, lay tat ca cac vung (mau) ben trong, "
           "chon 1 hoac nhieu mau roi quet ong dung, Pipe Accessories, Plumbing Fixtures "
           "giao voi cac mau do. Tuy chon tao Revision Cloud quanh doi tuong bi bat.")

import os
import math
from collections import defaultdict, OrderedDict

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.DB.Plumbing import Pipe
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

from System.Collections.Generic import List
from System.Windows import Point as WPoint
from System.Windows import FontWeights
from System.Windows.Controls import Canvas
from System.Windows.Controls import TextBlock as WTextBlock
from System.Windows.Shapes import Polygon as WPolygon
from System.Windows.Media import SolidColorBrush, Brushes, PointCollection
from System.Windows.Media import Color as WColor

doc = revit.doc
uidoc = revit.uidoc
active_view = doc.ActiveView
output = script.get_output()

MM_TO_FT = 1.0 / 304.8
FT2_TO_M2 = 0.09290304
GRID = 5.0
EPS = 1e-9
FOOT_GEOM = 0
FOOT_POINT = 1
FOOT_BBOX = 2
NEW_REV = u"<Tạo Revision mới>"
DEFAULT_CLOUD_TYPE = u"<Mặc định>"


# =========================================================
# Helpers
# =========================================================
def eid_int(eid):
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def make_eid(n):
    try:
        return DB.ElementId(long(n))
    except Exception:
        return DB.ElementId(int(n))


def elem_name(el):
    try:
        return DB.Element.Name.GetValue(el)
    except Exception:
        try:
            return el.Name
        except Exception:
            return u""


def detail_full_name(e):
    fam = u""
    typ = u""
    try:
        sym = e.Symbol
        fam = sym.FamilyName
        typ = elem_name(sym)
    except Exception:
        t = e.Document.GetElement(e.GetTypeId())
        if t:
            typ = elem_name(t)
            try:
                fam = t.FamilyName
            except Exception:
                pass
    return u"{} : {}".format(fam, typ)


def family_name(e):
    try:
        return e.Symbol.FamilyName
    except Exception:
        return u""


def type_name(el):
    t = el.Document.GetElement(el.GetTypeId())
    if not t:
        return elem_name(el)
    fam = u""
    try:
        fam = t.FamilyName
    except Exception:
        pass
    if fam:
        return u"{} : {}".format(fam, elem_name(t))
    return elem_name(t)


def is_detail_component(el):
    try:
        return eid_int(el.Category.Id) == int(DB.BuiltInCategory.OST_DetailComponents)
    except Exception:
        return False


def pat_label(indices):
    return u", ".join(u"#{}".format(i + 1) for i in indices)


def parse_float(txt, default):
    try:
        return float((txt or u"").replace(u",", u"."))
    except ValueError:
        return default


# =========================================================
# Link / linked view
# =========================================================
def get_linked_view_id(view, inst):
    views = [view]
    tid = view.ViewTemplateId
    if tid is not None and eid_int(tid) > 0:
        tpl = doc.GetElement(tid)
        if tpl is not None:
            views.append(tpl)
    for v in views:
        for lid in (inst.Id, inst.GetTypeId()):
            try:
                s = v.GetLinkOverrides(lid)
            except Exception:
                s = None
            if s is None:
                continue
            try:
                vid = s.LinkedViewId
            except Exception:
                vid = None
            if vid is not None and eid_int(vid) > 0:
                return vid, str(s.LinkVisibilityType)
    return None, None


def get_links():
    res = []
    for inst in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance):
        ldoc = inst.GetLinkDocument()
        if ldoc is None:
            continue
        vid, mode = get_linked_view_id(active_view, inst)
        res.append({"inst": inst, "doc": ldoc, "name": inst.Name,
                    "auto_vid": vid, "auto_mode": mode})
    res.sort(key=lambda x: x.get("name"))
    return res


def collect_detail_items(ldoc, view_id, keyword, fam_only):
    key = (keyword or u"").strip().lower()
    res = []
    col = DB.FilteredElementCollector(ldoc, view_id) \
        .OfCategory(DB.BuiltInCategory.OST_DetailComponents) \
        .WhereElementIsNotElementType()
    for e in col:
        if fam_only and family_name(e) != fam_only:
            continue
        n = detail_full_name(e)
        if key and key not in n.lower():
            continue
        res.append((e, n))
    return res


class LinkedDetailFilter(ISelectionFilter):
    def AllowElement(self, e):
        return isinstance(e, DB.RevitLinkInstance)

    def AllowReference(self, ref, pt):
        try:
            inst = doc.GetElement(ref.ElementId)
            ldoc = inst.GetLinkDocument()
            el = ldoc.GetElement(ref.LinkedElementId)
            return is_detail_component(el)
        except Exception:
            return False


# =========================================================
# Revision / cloud type
# =========================================================
def safe_rev_number(r):
    try:
        return r.RevisionNumber or u"-"
    except Exception:
        return u"Per Sheet"


def get_open_revisions():
    """List (label, revision) cac revision chua Issued."""
    res = []
    for rid in DB.Revision.GetAllRevisionIds(doc):
        r = doc.GetElement(rid)
        if r is None:
            continue
        try:
            if r.Issued:
                continue
        except Exception:
            pass
        lbl = u"Seq {} | {} | {} | {} (Id {})".format(
            r.SequenceNumber, safe_rev_number(r), r.Description or u"-",
            r.RevisionDate or u"-", eid_int(r.Id))
        res.append((lbl, r))
    return res


def get_cloud_types():
    """List (label, typeId) cac type cua Revision Cloud (neu phien ban Revit co)."""
    res = []
    try:
        col = DB.FilteredElementCollector(doc) \
            .OfCategory(DB.BuiltInCategory.OST_RevisionClouds) \
            .WhereElementIsElementType()
        for t in col:
            res.append((elem_name(t) or u"Type {}".format(eid_int(t.Id)), t.Id))
    except Exception:
        pass
    res.sort(key=lambda x: x[0])
    return res


# =========================================================
# 2D utils
# =========================================================
def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(pts):
    P = sorted(set((round(x, 6), round(y, 6)) for x, y in pts))
    if len(P) <= 2:
        return P
    lower = []
    for p in P:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(P):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def poly_area(poly):
    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def outer_loop(loops):
    best = None
    best_a = -1.0
    for lp in loops:
        a = abs(poly_area(lp))
        if a > best_a:
            best_a = a
            best = lp
    return best, best_a


def loops_centroid(loops):
    best, _a = outer_loop(loops)
    if not best:
        return (0.0, 0.0)
    return (sum(p[0] for p in best) / len(best), sum(p[1] for p in best) / len(best))


def region_area(loops):
    best, best_a = outer_loop(loops)
    holes = 0.0
    for lp in loops:
        if lp is not best:
            holes += abs(poly_area(lp))
    return max(0.0, best_a - holes)


def aabb(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def rect_poly(bb, pad=0.0):
    x0 = bb[0] - pad
    y0 = bb[1] - pad
    x1 = bb[2] + pad
    y1 = bb[3] + pad
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def aabb_overlap(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def cells(bb):
    x0 = int(math.floor(bb[0] / GRID))
    x1 = int(math.floor(bb[2] / GRID))
    y0 = int(math.floor(bb[1] / GRID))
    y1 = int(math.floor(bb[3] / GRID))
    for i in range(x0, x1 + 1):
        for j in range(y0, y1 + 1):
            yield (i, j)


def pt_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xcross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xcross:
                inside = not inside
        j = i
    return inside


def pt_in_region(x, y, loops):
    c = 0
    for lp in loops:
        if pt_in_poly(x, y, lp):
            c += 1
    return (c % 2) == 1


def dist_pt_seg(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    L2 = dx * dx + dy * dy
    if L2 < EPS:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_pt_poly(px, py, poly):
    n = len(poly)
    best = None
    for i in range(n):
        d = dist_pt_seg(px, py, poly[i], poly[(i + 1) % n])
        if best is None or d < best:
            best = d
    return best if best is not None else 1e18


def _orient(a, b, c):
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if abs(v) < 1e-12:
        return 0
    if v > 0:
        return 1
    return -1


def _on_seg(a, b, c):
    return (min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9 and
            min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9)


def seg_intersect(p1, p2, q1, q2):
    o1 = _orient(p1, p2, q1)
    o2 = _orient(p1, p2, q2)
    o3 = _orient(q1, q2, p1)
    o4 = _orient(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_seg(p1, p2, q1):
        return True
    if o2 == 0 and _on_seg(p1, p2, q2):
        return True
    if o3 == 0 and _on_seg(q1, q2, p1):
        return True
    if o4 == 0 and _on_seg(q1, q2, p2):
        return True
    return False


def offset_hull(poly, d):
    if d <= 0:
        return poly
    pts = []
    for x, y in poly:
        for k in range(8):
            a = k * math.pi / 4.0
            pts.append((x + d * math.cos(a), y + d * math.sin(a)))
    return convex_hull(pts)


# =========================================================
# Mau (vung) trong detail item
# =========================================================
def _face_loops_xyz(f):
    loops = []
    try:
        cloops = f.GetEdgesAsCurveLoops()
    except Exception:
        return loops
    for cl in cloops:
        pts = []
        for c in cl:
            try:
                tp = list(c.Tessellate())
            except Exception:
                tp = []
            if len(tp) > 1:
                tp = tp[:-1]
            pts.extend(tp)
        if len(pts) >= 3:
            loops.append(pts)
    return loops


def _walk_faces(geo, out):
    if geo is None:
        return
    for g in geo:
        if isinstance(g, DB.GeometryInstance):
            _walk_faces(g.GetInstanceGeometry(), out)
        elif isinstance(g, DB.Solid):
            try:
                if g.Faces.Size == 0:
                    continue
            except Exception:
                continue
            for f in g.Faces:
                loops = _face_loops_xyz(f)
                if loops:
                    out.append(loops)


def extract_patterns(e, tf):
    ldoc = e.Document
    opt = DB.Options()
    try:
        ov = ldoc.GetElement(e.OwnerViewId)
        if ov is not None:
            opt.View = ov
    except Exception:
        pass
    raw = []
    try:
        _walk_faces(e.get_Geometry(opt), raw)
    except Exception:
        pass
    try:
        inv = e.GetTransform().Inverse
    except Exception:
        inv = DB.Transform.Identity
    pats = []
    for loops in raw:
        host = []
        local = []
        nv = 0
        for lp in loops:
            host.append([(q.X, q.Y) for q in (tf.OfPoint(p) for p in lp)])
            local.append([(q.X, q.Y) for q in (inv.OfPoint(p) for p in lp)])
            nv += len(lp)
        pats.append({"host": host, "local": local, "nv": nv,
                     "area": region_area(local), "c_local": loops_centroid(local)})
    return pats


def pick_pattern(pats, ref, stat, used):
    if not pats:
        return None
    idx = ref.get("index")
    if (idx < len(pats) and len(pats) == ref.get("count") and
            pats[idx].get("nv") == ref.get("nv") and idx not in used):
        stat["index"] = stat.get("index", 0) + 1
        return idx
    rc = ref.get("c_local")
    best = None
    best_d = None
    for i in range(len(pats)):
        if i in used:
            continue
        c = pats[i].get("c_local")
        d = math.hypot(c[0] - rc[0], c[1] - rc[1])
        if best_d is None or d < best_d:
            best_d = d
            best = i
    if best is not None:
        stat["nearest"] = stat.get("nearest", 0) + 1
    return best


# =========================================================
# Hinh chieu that cua accessory / fixture
# =========================================================
def _collect_solid_pts(geo, pts):
    if geo is None:
        return
    for g in geo:
        if isinstance(g, DB.GeometryInstance):
            _collect_solid_pts(g.GetInstanceGeometry(), pts)
        elif isinstance(g, DB.Solid):
            try:
                if g.Volume <= 1e-9:
                    continue
            except Exception:
                continue
            for ed in g.Edges:
                try:
                    pts.extend(list(ed.Tessellate()))
                except Exception:
                    pass
        elif isinstance(g, DB.Mesh):
            for vtx in g.Vertices:
                pts.append(vtx)


def model_footprint(e):
    pts = []
    opt = DB.Options()
    opt.DetailLevel = DB.ViewDetailLevel.Fine
    try:
        _collect_solid_pts(e.get_Geometry(opt), pts)
    except Exception:
        pass
    if len(pts) < 3:
        return None
    hull = convex_hull([(p.X, p.Y) for p in pts])
    if len(hull) < 3:
        return None
    return hull


def location_xy(e):
    loc = e.Location
    if isinstance(loc, DB.LocationPoint):
        p = loc.Point
        return (p.X, p.Y)
    bb = e.get_BoundingBox(None)
    if bb is not None:
        return ((bb.Min.X + bb.Max.X) / 2.0, (bb.Min.Y + bb.Max.Y) / 2.0)
    return None


# =========================================================
# Kiem tra hien thi chat trong active view
# =========================================================
def _level_elev(lid):
    if lid is None or eid_int(lid) <= 0:
        return None
    lv = doc.GetElement(lid)
    if isinstance(lv, DB.Level):
        return lv.Elevation
    return None


class ViewVisibility(object):
    def __init__(self, view, cut_only):
        self.view = view
        self.cut_only = cut_only
        self.zmin = None
        self.zmax = None
        self.zcut = None
        self.hide_filters = []
        self.crop_inv = None
        self.crop_min = None
        self.crop_max = None
        self._init_range()
        self._init_filters()
        self._init_crop()

    def _plane_z(self, vr, plane):
        try:
            lid = vr.GetLevelId(plane)
        except Exception:
            return None
        base = _level_elev(lid)
        if base is None:
            return None
        try:
            return base + vr.GetOffset(plane)
        except Exception:
            return base

    def _init_range(self):
        try:
            vr = self.view.GetViewRange()
        except Exception:
            return
        P = DB.PlanViewPlane
        top = self._plane_z(vr, P.TopClipPlane)
        cut = self._plane_z(vr, P.CutPlane)
        bot = self._plane_z(vr, P.BottomClipPlane)
        dep = self._plane_z(vr, P.ViewDepthPlane)
        lows = [z for z in (dep, bot) if z is not None]
        self.zmin = min(lows) if lows else None
        self.zmax = top
        self.zcut = cut
        try:
            if self.view.ViewType == DB.ViewType.CeilingPlan:
                ups = [z for z in (dep, top) if z is not None]
                self.zmin = cut
                self.zmax = max(ups) if ups else None
        except Exception:
            pass

    def _init_filters(self):
        views = [self.view]
        try:
            tid = self.view.ViewTemplateId
            if tid is not None and eid_int(tid) > 0:
                tpl = doc.GetElement(tid)
                if tpl is not None:
                    views.append(tpl)
        except Exception:
            pass
        seen = set()
        for v in views:
            try:
                fids = list(v.GetFilters())
            except Exception:
                fids = []
            for fid in fids:
                k = eid_int(fid)
                if k in seen:
                    continue
                try:
                    visible = v.GetFilterVisibility(fid)
                except Exception:
                    visible = True
                try:
                    enabled = v.GetIsFilterEnabled(fid)
                except Exception:
                    enabled = True
                if visible or not enabled:
                    continue
                seen.add(k)
                fe = doc.GetElement(fid)
                if isinstance(fe, DB.ParameterFilterElement):
                    cats = set(eid_int(c) for c in fe.GetCategories())
                    try:
                        ef = fe.GetElementFilter()
                    except Exception:
                        ef = None
                    self.hide_filters.append(("param", cats, ef))
                elif isinstance(fe, DB.SelectionFilterElement):
                    ids = set(eid_int(i) for i in fe.GetElementIds())
                    self.hide_filters.append(("sel", ids, None))

    def _init_crop(self):
        try:
            if not self.view.CropBoxActive:
                return
            cb = self.view.CropBox
            self.crop_inv = cb.Transform.Inverse
            self.crop_min = cb.Min
            self.crop_max = cb.Max
        except Exception:
            self.crop_inv = None

    def reason(self, e):
        v = self.view
        try:
            if e.IsHidden(v):
                return u"Hide in view"
        except Exception:
            pass
        try:
            if v.GetCategoryHidden(e.Category.Id):
                return u"Category tắt"
        except Exception:
            pass
        cat_k = None
        try:
            cat_k = eid_int(e.Category.Id)
        except Exception:
            pass
        for kind, data, ef in self.hide_filters:
            if kind == "sel":
                if eid_int(e.Id) in data:
                    return u"View filter (selection)"
            else:
                if cat_k is not None and cat_k not in data:
                    continue
                if ef is None:
                    return u"View filter"
                try:
                    if ef.PassesFilter(e):
                        return u"View filter"
                except Exception:
                    pass
        bb = e.get_BoundingBox(None)
        if bb is not None:
            lo = bb.Min.Z
            hi = bb.Max.Z
            if self.cut_only and self.zcut is not None:
                if not (lo - 1e-6 <= self.zcut <= hi + 1e-6):
                    return u"Không cắt qua cut plane"
            if self.zmin is not None and hi < self.zmin - 1e-6:
                return u"Dưới view range"
            if self.zmax is not None and lo > self.zmax + 1e-6:
                return u"Trên view range"
            if self.crop_inv is not None:
                c = self.crop_inv.OfPoint((bb.Min + bb.Max) / 2.0)
                if (c.X < self.crop_min.X or c.X > self.crop_max.X or
                        c.Y < self.crop_min.Y or c.Y > self.crop_max.Y):
                    return u"Ngoài crop region"
        return None


# =========================================================
# Targets in host model
# =========================================================
def _collector(bic, use_view):
    if use_view:
        c = DB.FilteredElementCollector(doc, active_view.Id)
    else:
        c = DB.FilteredElementCollector(doc)
    return c.OfCategory(bic).WhereElementIsNotElementType()


def _iter_targets(bic, opts, tstat, vis):
    for e in _collector(bic, opts.get("inview")):
        if vis is not None:
            why = vis.reason(e)
            if why is not None:
                tstat["hidden"] = tstat.get("hidden", 0) + 1
                hr = tstat.setdefault("hidden_reason", {})
                hr[why] = hr.get(why, 0) + 1
                continue
        yield e


def _circle_target(e, cat, c, r, name):
    return {"el": e, "cat": cat, "kind": "circle", "c": c, "r": r,
            "bb": (c[0] - r, c[1] - r, c[0] + r, c[1] + r), "name": name}


def collect_targets(opts, tstat):
    targets = []
    tol = opts.get("tol")
    cos_tol = math.cos(math.radians(opts.get("angle")))
    foot = opts.get("foot")
    vis = None
    if opts.get("inview"):
        vis = ViewVisibility(active_view, opts.get("cutonly"))

    if opts.get("pipe"):
        n_all = 0
        for e in _iter_targets(DB.BuiltInCategory.OST_PipeCurves, opts, tstat, vis):
            if not isinstance(e, Pipe):
                continue
            loc = e.Location
            if not isinstance(loc, DB.LocationCurve):
                continue
            crv = loc.Curve
            if not isinstance(crv, DB.Line):
                continue
            n_all += 1
            p0 = crv.GetEndPoint(0)
            p1 = crv.GetEndPoint(1)
            d = p1 - p0
            L = d.GetLength()
            if L < 1e-6:
                continue
            if abs(d.Z) / L < cos_tol:
                continue
            od = None
            prm = e.get_Parameter(DB.BuiltInParameter.RBS_PIPE_OUTER_DIAMETER)
            if prm is not None and prm.HasValue:
                od = prm.AsDouble()
            if not od:
                od = e.Diameter
            r = od / 2.0 + tol
            c = ((p0.X + p1.X) / 2.0, (p0.Y + p1.Y) / 2.0)
            size = u""
            sp = e.get_Parameter(DB.BuiltInParameter.RBS_CALCULATED_SIZE)
            if sp is not None and sp.HasValue:
                size = sp.AsString() or u""
            targets.append(_circle_target(e, "pipe", c, r,
                                          u"{} - {}".format(type_name(e), size)))
        tstat.update({"pipe_all": n_all})

    groups = ((opts.get("acc"), DB.BuiltInCategory.OST_PipeAccessory, "acc"),
              (opts.get("fix"), DB.BuiltInCategory.OST_PlumbingFixtures, "fix"))
    for flag, bic, cat in groups:
        if not flag:
            continue
        for e in _iter_targets(bic, opts, tstat, vis):
            name = type_name(e)
            if foot == FOOT_POINT:
                c = location_xy(e)
                if c is None:
                    continue
                targets.append(_circle_target(e, cat, c, max(tol, 1e-4), name))
                continue
            poly = None
            if foot == FOOT_GEOM:
                poly = model_footprint(e)
                if poly is not None:
                    poly = offset_hull(poly, tol)
                    tstat.update({"geom": tstat.get("geom", 0) + 1})
            if poly is None:
                bb = e.get_BoundingBox(None)
                if bb is None:
                    continue
                poly = rect_poly((bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y), tol)
                tstat.update({"bbox": tstat.get("bbox", 0) + 1})
            targets.append({"el": e, "cat": cat, "kind": "poly",
                            "poly": poly, "bb": aabb(poly), "name": name})
    for t in targets:
        k = t.get("cat")
        tstat.update({k: tstat.get(k, 0) + 1})
    return targets


def is_hit(t, loops):
    if t.get("kind") == "circle":
        cx, cy = t.get("c")
        r = t.get("r")
        if pt_in_region(cx, cy, loops):
            return True
        for lp in loops:
            if dist_pt_poly(cx, cy, lp) <= r:
                return True
        return False

    R = t.get("poly")
    nr = len(R)
    for x, y in R:
        if pt_in_region(x, y, loops):
            return True
    outer, _a = outer_loop(loops)
    for x, y in outer:
        if pt_in_poly(x, y, R):
            return True
    for lp in loops:
        n = len(lp)
        for i in range(n):
            a1 = lp[i]
            a2 = lp[(i + 1) % n]
            for j in range(nr):
                if seg_intersect(a1, a2, R[j], R[(j + 1) % nr]):
                    return True
    return False


# =========================================================
# Ve detail line
# =========================================================
def _view_z():
    z = 0.0
    try:
        lvl = active_view.GenLevel
        if lvl is not None:
            z = lvl.Elevation
    except Exception:
        pass
    return z


def _draw_poly(lp, z):
    n = 0
    m = len(lp)
    for i in range(m):
        a = lp[i]
        b = lp[(i + 1) % m]
        if math.hypot(a[0] - b[0], a[1] - b[1]) < 0.003:
            continue
        try:
            ln = DB.Line.CreateBound(DB.XYZ(a[0], a[1], z), DB.XYZ(b[0], b[1], z))
            doc.Create.NewDetailCurve(active_view, ln)
            n += 1
        except Exception:
            pass
    return n


def _circle_poly(c, r):
    r = max(r, 0.01)
    return [(c[0] + r * math.cos(k * math.pi / 8.0), c[1] + r * math.sin(k * math.pi / 8.0))
            for k in range(16)]


def draw_polys(poly_list, title):
    z = _view_z()
    n = 0
    t = DB.Transaction(doc, title)
    t.Start()
    try:
        for lp in poly_list:
            n += _draw_poly(lp, z)
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return n


# =========================================================
# Revision Cloud
# =========================================================
def element_uv_box(el):
    """Bounding box cua element chieu len mat phang active view -> (umin, vmin, umax, vmax)."""
    bb = el.get_BoundingBox(active_view)
    if bb is None:
        bb = el.get_BoundingBox(None)
    if bb is None:
        return None
    t = bb.Transform
    org = active_view.Origin
    right = active_view.RightDirection
    up = active_view.UpDirection
    us = []
    vs = []
    for x in (bb.Min.X, bb.Max.X):
        for y in (bb.Min.Y, bb.Max.Y):
            for z in (bb.Min.Z, bb.Max.Z):
                p = t.OfPoint(DB.XYZ(x, y, z))
                d = p - org
                us.append(d.DotProduct(right))
                vs.append(d.DotProduct(up))
    return (min(us), min(vs), max(us), max(vs))


def grow_box(box, offset, min_size):
    umin = box[0] - offset
    vmin = box[1] - offset
    umax = box[2] + offset
    vmax = box[3] + offset
    if umax - umin < min_size:
        c = (umax + umin) / 2.0
        umin = c - min_size / 2.0
        umax = c + min_size / 2.0
    if vmax - vmin < min_size:
        c = (vmax + vmin) / 2.0
        vmin = c - min_size / 2.0
        vmax = c + min_size / 2.0
    return (umin, vmin, umax, vmax)


def merge_boxes(groups):
    """groups: list {"box": (..), "ids": [..]} -> gop cac box chong nhau."""
    changed = True
    while changed:
        changed = False
        out = []
        while groups:
            g = groups.pop()
            i = 0
            while i < len(groups):
                h = groups[i]
                if aabb_overlap(g.get("box"), h.get("box")):
                    a = g.get("box")
                    b = h.get("box")
                    g = {"box": (min(a[0], b[0]), min(a[1], b[1]),
                                 max(a[2], b[2]), max(a[3], b[3])),
                         "ids": g.get("ids") + h.get("ids")}
                    groups.pop(i)
                    changed = True
                else:
                    i += 1
            out.append(g)
        groups = out
    return groups


def box_curves(box):
    org = active_view.Origin
    right = active_view.RightDirection
    up = active_view.UpDirection

    def P(u, v):
        return org + right.Multiply(u) + up.Multiply(v)

    p1 = P(box[0], box[1])
    p2 = P(box[2], box[1])
    p3 = P(box[2], box[3])
    p4 = P(box[0], box[3])
    curves = List[DB.Curve]()
    for a, b in ((p1, p2), (p2, p3), (p3, p4), (p4, p1)):
        curves.Add(DB.Line.CreateBound(a, b))
    return curves


def create_clouds(hit_elems, copts):
    """hit_elems: list element. Tra ve (so cloud, list cloud id, list loi, revision)."""
    offset = copts.get("offset") * MM_TO_FT
    min_size = copts.get("min") * MM_TO_FT
    groups = []
    errors = []
    for el in hit_elems:
        box = element_uv_box(el)
        if box is None:
            errors.append(u"{}: không có bounding box".format(eid_int(el.Id)))
            continue
        groups.append({"box": grow_box(box, offset, min_size), "ids": [eid_int(el.Id)]})
    if copts.get("merge"):
        groups = merge_boxes(groups)

    created = []
    rev = copts.get("revision")
    t = DB.Transaction(doc, "Scan Detail Item - Revision Cloud")
    t.Start()
    try:
        if rev is None:
            rev = DB.Revision.Create(doc)
            rev.Description = u"Scan Detail Item"
        type_id = copts.get("type_id")
        for g in groups:
            try:
                cloud = DB.RevisionCloud.Create(doc, active_view, rev.Id, box_curves(g.get("box")))
            except Exception as ex:
                errors.append(u"{}: lỗi tạo cloud - {}".format(
                    u", ".join(str(i) for i in g.get("ids")), ex))
                continue
            if type_id is not None:
                try:
                    cloud.ChangeTypeId(type_id)
                except Exception as ex:
                    errors.append(u"Cloud {}: không đổi được type - {}".format(
                        eid_int(cloud.Id), ex))
            p = cloud.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p is not None and not p.IsReadOnly:
                txt = u"ID: " + u", ".join(str(i) for i in g.get("ids"))
                p.Set(txt[:250])
            created.append(cloud.Id)
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    return created, errors, rev


# =========================================================
# UI
# =========================================================
def _brush(r, g, b, a=255):
    return SolidColorBrush(WColor.FromArgb(a, r, g, b))


class ScanWindow(forms.WPFWindow):
    def __init__(self, xaml, links, state):
        forms.WPFWindow.__init__(self, xaml)
        self.links = links
        self.state = state
        self.views = []
        self.result = None
        self.action = None
        self.sample = None
        self.sample_link = None
        self.patterns = []
        self.pat_items = []
        self.revs = []
        self.cloud_types = []
        self._busy = True

        for lk in links:
            self.cb_link.Items.Add(lk.get("name"))
        self._load_cloud_options()

        self.cb_link.SelectionChanged += self.on_link_changed
        self.btn_pick.Click += self.on_pick
        self.btn_by_id.Click += self.on_by_id
        self.lb_patterns.SelectionChanged += self.on_pattern_changed
        self.btn_sel_all.Click += self.on_sel_all
        self.btn_sel_none.Click += self.on_sel_none
        self.btn_draw_sample.Click += self.on_draw_sample
        self.btn_count.Click += self.on_count
        self.btn_run.Click += self.on_run
        self.btn_cancel.Click += self.on_cancel

        self._restore()
        self._busy = False
        self._draw_preview()

    # ---------- cloud options ----------
    def _load_cloud_options(self):
        self.cb_revision.Items.Clear()
        self.revs = get_open_revisions()
        for lbl, _r in self.revs:
            self.cb_revision.Items.Add(lbl)
        self.cb_revision.Items.Add(NEW_REV)
        self.cb_revision.SelectedIndex = (len(self.revs) - 1) if self.revs else 0

        self.cb_cloudtype.Items.Clear()
        self.cloud_types = get_cloud_types()
        self.cb_cloudtype.Items.Add(DEFAULT_CLOUD_TYPE)
        for lbl, _tid in self.cloud_types:
            self.cb_cloudtype.Items.Add(lbl)
        self.cb_cloudtype.SelectedIndex = 0

    # ---------- state ----------
    def _restore(self):
        st = self.state
        idx = 0
        lname = st.get("link_name")
        found = False
        if lname:
            for i in range(len(self.links)):
                if self.links[i].get("name") == lname:
                    idx = i
                    found = True
                    break
        if not found:
            for i in range(len(self.links)):
                if self.links[i].get("auto_vid") is not None:
                    idx = i
                    break
        self.cb_link.SelectedIndex = idx
        self._load_views()

        for name in ("tb_contains", "tb_tol", "tb_angle", "tb_detail_id",
                     "tb_cloud_offset", "tb_cloud_min"):
            v = st.get(name)
            if v is not None:
                getattr(self, name).Text = v
        for name in ("chk_samefam", "chk_pipe", "chk_acc", "chk_fix", "chk_inview",
                     "chk_cutonly", "chk_draw", "chk_cloud", "chk_cloud_merge"):
            v = st.get(name)
            if v is not None:
                getattr(self, name).IsChecked = v
        for name in ("cb_foot", "cb_revision", "cb_cloudtype"):
            v = st.get(name)
            if v is not None and 0 <= v < getattr(self, name).Items.Count:
                getattr(self, name).SelectedIndex = v

        did = st.get("detail_id")
        if did is not None:
            self._load_sample(did, st.get("pattern_indices"))

    def save_state(self):
        st = self.state
        lk = self._link()
        if lk is not None:
            st["link_name"] = lk.get("name")
        for name in ("tb_contains", "tb_tol", "tb_angle", "tb_detail_id",
                     "tb_cloud_offset", "tb_cloud_min"):
            st[name] = getattr(self, name).Text
        for name in ("chk_samefam", "chk_pipe", "chk_acc", "chk_fix", "chk_inview",
                     "chk_cutonly", "chk_draw", "chk_cloud", "chk_cloud_merge"):
            st[name] = getattr(self, name).IsChecked
        for name in ("cb_foot", "cb_revision", "cb_cloudtype"):
            st[name] = getattr(self, name).SelectedIndex
        if self.sample is not None:
            st["detail_id"] = eid_int(self.sample.Id)
        st["pattern_indices"] = self._sel_indices()

    # ---------- link / view ----------
    def _link(self):
        i = self.cb_link.SelectedIndex
        if 0 <= i < len(self.links):
            return self.links[i]
        return None

    def _view(self):
        i = self.cb_view.SelectedIndex
        if 0 <= i < len(self.views):
            return self.views[i]
        return None

    def _load_views(self):
        self.cb_view.Items.Clear()
        self.views = []
        lk = self._link()
        if lk is None:
            return
        vps = [v for v in DB.FilteredElementCollector(lk.get("doc")).OfClass(DB.ViewPlan)
               if not v.IsTemplate]
        vps.sort(key=lambda v: v.Name)
        sel = -1
        auto_vid = lk.get("auto_vid")
        auto_int = eid_int(auto_vid) if auto_vid is not None else None
        for i in range(len(vps)):
            v = vps[i]
            self.views.append(v)
            self.cb_view.Items.Add(u"{}".format(v.Name))
            if auto_int is not None and eid_int(v.Id) == auto_int:
                sel = i
        if sel >= 0:
            self.tb_view_info.Text = u"Linked view tự lấy từ V/G của active view ({}).".format(
                lk.get("auto_mode"))
            self.cb_view.SelectedIndex = sel
        else:
            self.tb_view_info.Text = (u"Không tìm thấy linked view trong V/G của active view. "
                                      u"Hãy chọn view thủ công.")
            self.cb_view.SelectedIndex = 0 if vps else -1

    def _select_view_id(self, vid):
        k = eid_int(vid)
        for i in range(len(self.views)):
            if eid_int(self.views[i].Id) == k:
                self.cb_view.SelectedIndex = i
                return True
        return False

    def on_link_changed(self, sender, args):
        if self._busy:
            return
        self._load_views()
        self._clear_sample()

    # ---------- pattern selection ----------
    def _sel_indices(self):
        res = set()
        for it in self.lb_patterns.SelectedItems:
            s = u"{}".format(it)
            for i in range(len(self.pat_items)):
                if self.pat_items[i] == s:
                    res.add(i)
                    break
        return sorted(res)

    def _set_selection(self, indices):
        self.lb_patterns.UnselectAll()
        for i in indices:
            if 0 <= i < len(self.pat_items):
                self.lb_patterns.SelectedItems.Add(self.pat_items[i])

    def _update_sel_info(self):
        sel = self._sel_indices()
        if sel:
            self.tb_sel_info.Text = u"Đang chọn {} mẫu: {}".format(len(sel), pat_label(sel))
        else:
            self.tb_sel_info.Text = u"Chưa chọn mẫu nào"

    def on_sel_all(self, sender, args):
        self.lb_patterns.SelectAll()

    def on_sel_none(self, sender, args):
        self.lb_patterns.UnselectAll()

    def on_pattern_changed(self, sender, args):
        if self._busy:
            return
        self._draw_preview()

    # ---------- sample ----------
    def _clear_sample(self):
        self.sample = None
        self.sample_link = None
        self.patterns = []
        self.pat_items = []
        self.lb_patterns.Items.Clear()
        self.cv_preview.Children.Clear()
        self.tb_sample_info.Text = u"Chưa có detail item mẫu."
        self.tb_sel_info.Text = u""

    def _load_sample(self, detail_id, sel_indices=None):
        lk = self._link()
        if lk is None:
            return False
        try:
            el = lk.get("doc").GetElement(make_eid(detail_id))
        except Exception:
            el = None
        if el is None or not is_detail_component(el):
            self._clear_sample()
            self.tb_sample_info.Text = u"ID {} không phải Detail Item trong link '{}'.".format(
                detail_id, lk.get("name"))
            return False
        self.sample = el
        self.sample_link = lk
        self.tb_detail_id.Text = str(detail_id)
        try:
            self._select_view_id(el.OwnerViewId)
        except Exception:
            pass
        tf = lk.get("inst").GetTotalTransform()
        self.patterns = extract_patterns(el, tf)
        if not (self.tb_contains.Text or u"").strip():
            self.tb_contains.Text = family_name(el)

        was_busy = self._busy
        self._busy = True
        self.lb_patterns.Items.Clear()
        self.pat_items = []
        for i in range(len(self.patterns)):
            p = self.patterns[i]
            c = p.get("c_local")
            s = u"Mẫu #{:<2} | DT {:>8.3f} m2 | {:>3} đỉnh | tâm cục bộ ({:.0f}, {:.0f}) mm".format(
                i + 1, p.get("area") * FT2_TO_M2, p.get("nv"), c[0] * 304.8, c[1] * 304.8)
            self.pat_items.append(s)
            self.lb_patterns.Items.Add(s)
        self.tb_sample_info.Text = u"Mẫu: ID {}  |  {}  |  {} vùng".format(
            detail_id, detail_full_name(el), len(self.patterns))
        if not self.patterns:
            self.tb_sample_info.Text += u"  -> Không đọc được vùng nào."
        if sel_indices:
            self._set_selection(sel_indices)
        self._busy = was_busy
        self._draw_preview()
        return True

    def on_pick(self, sender, args):
        self.save_state()
        self.action = "pick"
        self.Close()

    def on_by_id(self, sender, args):
        txt = (self.tb_detail_id.Text or u"").strip()
        try:
            did = int(txt)
        except ValueError:
            forms.alert(u"ID không hợp lệ.")
            return
        self._load_sample(did)

    def _draw_preview(self):
        self._update_sel_info()
        cv = self.cv_preview
        cv.Children.Clear()
        if not self.patterns:
            return
        pts = [p for pat in self.patterns for lp in pat.get("local") for p in lp]
        if not pts:
            return
        bb = aabb(pts)
        W = cv.Width
        H = cv.Height
        pad = 12.0
        sx = (W - 2 * pad) / max(bb[2] - bb[0], 1e-6)
        sy = (H - 2 * pad) / max(bb[3] - bb[1], 1e-6)
        s = min(sx, sy)
        ox = pad + ((W - 2 * pad) - (bb[2] - bb[0]) * s) / 2.0
        oy = pad + ((H - 2 * pad) - (bb[3] - bb[1]) * s) / 2.0

        def tr(p):
            return WPoint(ox + (p[0] - bb[0]) * s, H - (oy + (p[1] - bb[1]) * s))

        sel = set(self._sel_indices())
        order = [i for i in range(len(self.patterns)) if i not in sel]
        order.extend(sorted(sel))
        for i in order:
            pat = self.patterns[i]
            is_sel = (i in sel)
            for lp in pat.get("local"):
                pg = WPolygon()
                pc = PointCollection()
                for p in lp:
                    pc.Add(tr(p))
                pg.Points = pc
                if is_sel:
                    pg.Stroke = _brush(230, 120, 0)
                    pg.StrokeThickness = 2.5
                    pg.Fill = _brush(255, 165, 0, 110)
                else:
                    pg.Stroke = _brush(90, 90, 90)
                    pg.StrokeThickness = 1.0
                    pg.Fill = _brush(200, 200, 200, 40)
                cv.Children.Add(pg)
            c = loops_centroid(pat.get("local"))
            lbl = WTextBlock()
            lbl.Text = u"#{}".format(i + 1)
            lbl.FontWeight = FontWeights.Bold
            lbl.Foreground = _brush(200, 0, 0) if is_sel else Brushes.Black
            cp = tr(c)
            Canvas.SetLeft(lbl, cp.X - 8)
            Canvas.SetTop(lbl, cp.Y - 8)
            cv.Children.Add(lbl)

    def on_draw_sample(self, sender, args):
        sel = self._sel_indices()
        if not sel:
            forms.alert(u"Chưa chọn mẫu.")
            return
        polys = []
        for i in sel:
            polys.extend(self.patterns[i].get("host"))
        try:
            n = draw_polys(polys, "Scan Detail Item - ve thu mau")
            self.tb_sample_info.Text = (u"Đã vẽ {} đoạn của mẫu {} lên active view. "
                                        u"Ctrl+Z để xoá.").format(n, pat_label(sel))
        except Exception as ex:
            forms.alert(u"Lỗi vẽ: {}".format(ex))

    # ---------- count / run ----------
    def _scope(self):
        lk = self._link()
        v = self._view()
        if lk is None or v is None:
            return None
        fam_only = None
        if self.chk_samefam.IsChecked and self.sample is not None:
            fam_only = family_name(self.sample)
        return collect_detail_items(lk.get("doc"), v.Id, self.tb_contains.Text, fam_only)

    def on_count(self, sender, args):
        items = self._scope()
        if items is None:
            self.tb_count.Text = u"Chưa chọn link / linked view."
            return
        cnt = defaultdict(int)
        for _e, n in items:
            cnt[n] += 1
        parts = [u"{} x {}".format(cnt.get(n), n) for n in sorted(cnt.keys())]
        self.tb_count.Text = u"Tìm thấy {} detail item. {}".format(len(items), u" | ".join(parts))

    def _cloud_options(self):
        """Tra ve dict tuy chon cloud, hoac None neu khong tao, hoac False neu loi nhap."""
        if not self.chk_cloud.IsChecked:
            return None
        off = parse_float(self.tb_cloud_offset.Text, None)
        mn = parse_float(self.tb_cloud_min.Text, None)
        if off is None or mn is None or off < 0 or mn <= 0:
            forms.alert(u"Kích thước cloud không hợp lệ (offset >= 0, cạnh tối thiểu > 0).")
            return False
        ri = self.cb_revision.SelectedIndex
        rev = None
        if 0 <= ri < len(self.revs):
            rev = self.revs[ri][1]
        ti = self.cb_cloudtype.SelectedIndex
        type_id = None
        type_lbl = DEFAULT_CLOUD_TYPE
        if 1 <= ti <= len(self.cloud_types):
            type_lbl, type_id = self.cloud_types[ti - 1]
        rev_lbl = self.revs[ri][0] if rev is not None else NEW_REV
        return {"offset": off, "min": mn, "revision": rev, "rev_label": rev_lbl,
                "type_id": type_id, "type_label": type_lbl,
                "merge": bool(self.chk_cloud_merge.IsChecked)}

    def on_run(self, sender, args):
        lk = self._link()
        v = self._view()
        if lk is None or v is None:
            forms.alert(u"Chưa chọn link / linked view.")
            return
        if self.sample is None or not self.patterns:
            forms.alert(u"Chọn detail item mẫu trước.")
            return
        sel = self._sel_indices()
        if not sel:
            forms.alert(u"Chọn ít nhất 1 mẫu cần quét.")
            return
        tol = parse_float(self.tb_tol.Text or u"0", None)
        ang = parse_float(self.tb_angle.Text or u"1", None)
        if tol is None or ang is None:
            forms.alert(u"Dung sai / góc không hợp lệ.")
            return
        tol = max(0.0, tol)
        ang = min(max(ang, 0.01), 45.0)
        pipe = bool(self.chk_pipe.IsChecked)
        acc = bool(self.chk_acc.IsChecked)
        fix = bool(self.chk_fix.IsChecked)
        if not (pipe or acc or fix):
            forms.alert(u"Chọn ít nhất 1 loại đối tượng.")
            return
        cloud = self._cloud_options()
        if cloud is False:
            return
        items = self._scope()
        if not items:
            forms.alert(u"Không có detail item nào trong phạm vi.")
            return
        refs = []
        for pi in sel:
            p = self.patterns[pi]
            refs.append({"index": pi, "count": len(self.patterns), "nv": p.get("nv"),
                         "c_local": p.get("c_local")})
        foot = self.cb_foot.SelectedIndex
        if foot < 0:
            foot = FOOT_GEOM
        self.save_state()
        self.result = {"link": lk, "view": v, "items": items, "refs": refs,
                       "sample_id": eid_int(self.sample.Id),
                       "tol": tol * MM_TO_FT, "tol_mm": tol, "angle": ang,
                       "pipe": pipe, "acc": acc, "fix": fix,
                       "inview": bool(self.chk_inview.IsChecked),
                       "cutonly": bool(self.chk_cutonly.IsChecked),
                       "draw": bool(self.chk_draw.IsChecked), "foot": foot,
                       "cloud": cloud}
        self.action = "run"
        self.Close()

    def on_cancel(self, sender, args):
        self.action = None
        self.Close()


# =========================================================
# Pick
# =========================================================
def do_pick(state):
    try:
        ref = uidoc.Selection.PickObject(ObjectType.LinkedElement, LinkedDetailFilter(),
                                         u"Chọn 1 detail item trong link (ESC để huỷ)")
    except OperationCanceledException:
        return
    except Exception as ex:
        forms.alert(u"Không chọn được: {}\nHãy nhập ID detail item.".format(ex))
        return
    inst = doc.GetElement(ref.ElementId)
    state["link_name"] = inst.Name
    state["detail_id"] = eid_int(ref.LinkedElementId)
    state["pattern_indices"] = None
    state["tb_contains"] = u""


# =========================================================
# Scan + report
# =========================================================
def run_scan(opts):
    link = opts.get("link")
    lview = opts.get("view")
    tf = link.get("inst").GetTotalTransform()
    refs = opts.get("refs")
    items = opts.get("items")
    pstat = {}
    tstat = {}

    targets = collect_targets(opts, tstat)
    hits = {"pipe": OrderedDict(), "acc": OrderedDict(), "fix": OrderedDict()}
    dinfo = []
    no_zone = 0
    grid = defaultdict(list)
    total = len(items) + len(targets)
    cancelled = False
    step = 0

    with forms.ProgressBar(title=u"Đang quét... ({value}/{max_value})",
                           cancellable=True) as pb:
        for e, name in items:
            step += 1
            if pb.cancelled:
                cancelled = True
                break
            pats = extract_patterns(e, tf)
            used = set()
            got = 0
            for ref in refs:
                pi = pick_pattern(pats, ref, pstat, used)
                if pi is None:
                    continue
                used.add(pi)
                got += 1
                loops = pats[pi].get("host")
                pts = [q for lp in loops for q in lp]
                bb = aabb(pts)
                idx = len(dinfo)
                dinfo.append({"id": eid_int(e.Id), "name": name, "loops": loops,
                              "bb": bb, "pat": ref.get("index")})
                for c in cells(bb):
                    grid[c].append(idx)
            if got == 0:
                no_zone += 1
            if step % 5 == 0:
                pb.update_progress(step, total)

        if not cancelled:
            for t in targets:
                step += 1
                if pb.cancelled:
                    cancelled = True
                    break
                cand = set()
                for c in cells(t.get("bb")):
                    cand.update(grid.get(c, ()))
                for k in cand:
                    d = dinfo[k]
                    if not aabb_overlap(d.get("bb"), t.get("bb")):
                        continue
                    if is_hit(t, d.get("loops")):
                        tid = eid_int(t.get("el").Id)
                        bucket = hits.get(t.get("cat"))
                        rec = bucket.setdefault(tid, {"t": t, "d": set(), "p": set()})
                        rec.get("d").add(d.get("id"))
                        rec.get("p").add(d.get("pat"))
                if step % 20 == 0:
                    pb.update_progress(step, total)

    # ---------- Detail line ----------
    n_drawn = 0
    draw_err = None
    if opts.get("draw") and dinfo:
        polys = []
        for d in dinfo:
            polys.extend(d.get("loops"))
        for cat in ("pipe", "acc", "fix"):
            for rec in hits.get(cat).values():
                tt = rec.get("t")
                if tt.get("kind") == "circle":
                    polys.append(_circle_poly(tt.get("c"), tt.get("r")))
                else:
                    polys.append(tt.get("poly"))
        try:
            n_drawn = draw_polys(polys, "Scan Detail Item - ve ket qua")
        except Exception as ex:
            draw_err = str(ex)

    # ---------- Revision cloud ----------
    copts = opts.get("cloud")
    cloud_ids = []
    cloud_errs = []
    cloud_rev = None
    cloud_fatal = None
    hit_elems = []
    for cat in ("pipe", "acc", "fix"):
        for rec in hits.get(cat).values():
            hit_elems.append(rec.get("t").get("el"))
    if copts and hit_elems and not cancelled:
        try:
            cloud_ids, cloud_errs, cloud_rev = create_clouds(hit_elems, copts)
        except Exception as ex:
            cloud_fatal = str(ex)

    # ---------- Report ----------
    output.close_others()
    output.print_md(u"## Kết quả quét Detail Item")
    if cancelled:
        output.print_md(u"**Đã huỷ - kết quả chưa đầy đủ, không tạo cloud.**")
    foot_txt = (u"Hình chiếu thật", u"Điểm đặt family", u"Bounding box")[opts.get("foot")]
    sel_idx = [r.get("index") for r in refs]
    output.print_md(
        u"- Link: **{}**\n- Linked view: **{}**\n"
        u"- Detail item mẫu: ID {} | Mẫu quét: **{}** (trên {} vùng)\n"
        u"- Detail item trong phạm vi: {} | vùng đã lấy: {} | item không lấy được vùng: {}\n"
        u"- Ghép mẫu theo thứ tự: {} | theo vị trí gần nhất: {}\n"
        u"- Accessory / Fixture kiểm tra theo: **{}** (hình chiếu thật: {}, bounding box: {})\n"
        u"- Ống đứng đưa vào quét: {} / {} ống thẳng | accessory: {} | fixture: {}\n"
        u"- Dung sai: {} mm | Góc lệch đứng <= {} độ | Chỉ active view: {} | Chỉ cắt qua cut plane: {}".format(
            link.get("name"), lview.Name,
            opts.get("sample_id"), pat_label(sel_idx), refs[0].get("count"),
            len(items), len(dinfo), no_zone,
            pstat.get("index", 0), pstat.get("nearest", 0),
            foot_txt, tstat.get("geom", 0), tstat.get("bbox", 0),
            tstat.get("pipe", 0), tstat.get("pipe_all", 0),
            tstat.get("acc", 0), tstat.get("fix", 0),
            opts.get("tol_mm"), opts.get("angle"), opts.get("inview"), opts.get("cutonly")))
    if opts.get("inview"):
        hr = tstat.get("hidden_reason", {})
        why = u", ".join(u"{} {}".format(k, hr.get(k)) for k in sorted(hr.keys()))
        output.print_md(u"- Loại bỏ vì không hiển thị trong active view: {} ({})".format(
            tstat.get("hidden", 0), why or u"-"))
    if opts.get("draw"):
        output.print_md(u"- Detail line đã vẽ: {}".format(n_drawn))
    if draw_err:
        output.print_md(u"- Lỗi vẽ detail line: {}".format(draw_err))

    if copts:
        output.print_md(u"### Revision Cloud")
        if cloud_fatal:
            output.print_md(u"- **Lỗi tạo cloud:** {}".format(cloud_fatal))
        elif not hit_elems:
            output.print_md(u"- Không có đối tượng bị bắt nên không tạo cloud.")
        elif not cancelled:
            rev_txt = copts.get("rev_label")
            if cloud_rev is not None and copts.get("revision") is None:
                rev_txt = u"Revision mới - Seq {}".format(cloud_rev.SequenceNumber)
            output.print_md(
                u"- Đã tạo **{}** cloud cho {} đối tượng | Revision: {} | Type: {}\n"
                u"- Offset {} mm | Cạnh tối thiểu {} mm | Gộp cloud chồng nhau: {}".format(
                    len(cloud_ids), len(hit_elems), rev_txt, copts.get("type_label"),
                    copts.get("offset"), copts.get("min"), copts.get("merge")))
            if cloud_ids:
                print(output.linkify(cloud_ids, title=u"Chọn tất cả cloud ({})".format(len(cloud_ids))))
            for er in cloud_errs:
                output.print_md(u"- {}".format(er))

    titles = (("pipe", u"Ống đứng"),
              ("acc", u"Pipe Accessories"),
              ("fix", u"Plumbing Fixtures"))
    all_ids = []
    for cat, title in titles:
        if not opts.get(cat):
            continue
        data = hits.get(cat)
        output.print_md(u"### {} ({})".format(title, len(data)))
        if not data:
            continue
        rows = []
        ids = []
        for tid in data.keys():
            rec = data.get(tid)
            tt = rec.get("t")
            el = tt.get("el")
            ids.append(el.Id)
            rows.append([output.linkify(el.Id), tt.get("name"),
                         pat_label(sorted(rec.get("p"))),
                         u", ".join(str(x) for x in sorted(rec.get("d")))])
        output.print_table(rows, columns=[u"ID", u"Family : Type", u"Mẫu",
                                          u"Detail item ID (link)"])
        print(output.linkify(ids, title=u"Chọn tất cả {} ({})".format(title, len(ids))))
        all_ids.extend(ids)

    if all_ids:
        output.print_md(u"### Tất cả ID ({})".format(len(all_ids)))
        print(output.linkify(all_ids, title=u"Chọn tất cả"))
        print(u";".join(str(eid_int(i)) for i in all_ids))
    else:
        output.print_md(u"**Không có đối tượng nào giao với các mẫu đã chọn.**")


# =========================================================
# Main
# =========================================================
def main():
    if not isinstance(active_view, DB.ViewPlan):
        forms.alert(u"Active view phải là Plan view.", exitscript=True)
        return
    links = get_links()
    if not links:
        forms.alert(u"Không có Revit link nào được load.", exitscript=True)
        return
    xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
    state = {}
    while True:
        win = ScanWindow(xaml_path, links, state)
        win.ShowDialog()
        if win.action == "pick":
            do_pick(state)
            continue
        if win.action == "run" and win.result:
            run_scan(win.result)
        break


main()
