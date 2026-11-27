# -*- coding: utf-8 -*-
__title__ = "Schematic\nto Model"
__doc__ = ("Chon net ong hoac symbol tren schematic -> lay ID doi tuong model tuong ung. "
           "Ong: tim symbol o 2 dau net (sx/sy) roi do theo lien ket 'n' trong log de lay cac Pipe o giua.")

import os
import json
import math
import re
from collections import deque

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List

try:
    long
except NameError:          # CPython3 engine
    long = int
    basestring = str

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# ---------------------------------------------------------------------
# CAI DAT
# ---------------------------------------------------------------------
LOG_ROOT = os.path.join(os.environ.get("APPDATA", ""), "pyRevit", "DrainageSchematic", "logs")
END_TOL_PAPER_MM = 15.0       # symbol cach DAU net <= muc nay (tren giay) -> la doi tuong o dau ong
JOIN_TOL_PAPER_MM = 1.0       # 2 net co dau cach nhau <= muc nay -> coi la 1 tuyen ong lien tuc
SYMBOL_TOL_PAPER_MM = 8.0     # che do SYMBOL: sx/sy cach tam symbol <= muc nay
MAX_WALK = 2000
NEAR_LINE_PAPER_MM = 60.0     # dau net khong co symbol -> lay symbol gan net nhat trong pham vi nay
CALIB_BIN_PAPER_MM = 4.0      # o luoi khi do do lech sx/sy so voi symbol ve               # gioi han so node khi do theo lien ket 'n'
BOX_PAD_MM = 1500.0
LOCATE_VIEW_NAME = "SCH-LOCATE_{}".format(os.environ.get("USERNAME", "user"))

FT = 1.0 / 304.8

MODE_LINE = "Chon ONG (chi chon duoc line)"
MODE_SYMBOL = "Chon SYMBOL (fixture / fitting / accessory)"


# ---------------------------------------------------------------------
# TIEN ICH
# ---------------------------------------------------------------------
def to_int(v):
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, long)):
            return int(v)
        if isinstance(v, float) and v == int(v):
            return int(v)
        if isinstance(v, basestring) and v.strip().lstrip("-").isdigit():
            return int(v.strip())
    except Exception:
        pass
    return None


def to_float(v):
    try:
        if isinstance(v, bool) or v is None:
            return None
        return float(v)
    except Exception:
        return None


def make_eid(i):
    try:
        from System import Int64
        return DB.ElementId(Int64(i))
    except Exception:
        return DB.ElementId(int(i))


def copy_to_clipboard(text):
    try:
        script.clipboard_copy(text)
        return True
    except Exception:
        pass
    try:
        import clr
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import Clipboard
        Clipboard.SetText(text)
        return True
    except Exception:
        return False


def rec_value(rec, key):
    v = (rec or {}).get(key, "")
    return ", ".join(str(a) for a in v) if isinstance(v, list) else (v if v is not None else "")


def kind_of(rec):
    c = str((rec or {}).get("c", "")).lower()
    cat = str((rec or {}).get("cat", "")).lower()
    if c == "pipe" or cat == "pipes":
        return "pipe"
    if c == "fit" or "fitting" in cat:
        return "fit"
    if c == "fix" or "fixture" in cat:
        return "fix"
    if "accessor" in cat or c in ("acc", "pa"):
        return "acc"
    return c or "?"


# ---------------------------------------------------------------------
# TIM FILE LOG
# ---------------------------------------------------------------------
def norm(s):
    s = (s or "").lower()
    for suf in (" (2)", " (3)", " (4)"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    return "".join(ch for ch in s if ch.isalnum())


def choose_log(view):
    all_logs, match = [], None
    if os.path.isdir(LOG_ROOT):
        target = norm(view.Name)
        for root, dirs, files in os.walk(LOG_ROOT):
            if "last.json" not in files:
                continue
            path = os.path.join(root, "last.json")
            all_logs.append(path)
            folder = os.path.basename(root)
            base = folder.rsplit("_", 1)[0] if "_" in folder else folder
            if norm(base) == target or norm(folder) == target:
                if match is None or os.path.getmtime(path) > os.path.getmtime(match):
                    match = path
    if match:
        return match
    if all_logs:
        labels = dict((os.path.relpath(os.path.dirname(p), LOG_ROOT), p) for p in all_logs)
        pick = forms.SelectFromList.show(
            sorted(labels.keys()),
            title="Khong tim thay log trung ten view '{}' - chon log".format(view.Name),
            button_name="Chon", multiselect=False)
        if pick:
            return labels[pick]
    return forms.pick_file(file_ext="json",
                           init_dir=LOG_ROOT if os.path.isdir(LOG_ROOT) else None,
                           title="Chon file log (last.json)")


# ---------------------------------------------------------------------
# DOC LOG
#   elements: {"<id>": {"sx","sy" (ft, CHI symbol co), "p" (mm), "c","cat","t","s","l","sz","n"}}
#   Pipe KHONG co sx/sy -> tim qua lien ket 'n'
# ---------------------------------------------------------------------
class Graph(object):
    def __init__(self, elems):
        self.recs = {}
        self.adj = {}
        for sid, rec in elems.items():
            eid = to_int(sid)
            if eid is None or not isinstance(rec, dict):
                continue
            self.recs[eid] = rec
            for v in rec.get("n") or []:
                nid = to_int(v)
                if nid is None or nid == eid:
                    continue
                self.adj.setdefault(eid, set()).add(nid)
                self.adj.setdefault(nid, set()).add(eid)   # lien ket 2 chieu

    def nb(self, i):
        return self.adj.get(i, set())

    def shortest_path(self, a, b, blocked):
        """BFS a -> b, khong di xuyen qua node trong 'blocked' (symbol khac)."""
        prev = {a: None}
        q = deque([a])
        while q and len(prev) < MAX_WALK:
            cur = q.popleft()
            if cur == b:
                path = []
                while cur is not None:
                    path.append(cur)
                    cur = prev[cur]
                return path[::-1]
            for n in self.nb(cur):
                if n in prev:
                    continue
                if n != b and n in blocked:
                    continue
                prev[n] = cur
                q.append(n)
        return None

    def walk_from(self, a, symbols):
        """Di tu symbol a cho toi khi gap symbol khac hoac nhanh re (tee/junction).
        Tra ve list cac nhanh: [(path, ly_do_dung)]"""
        branches = []
        for first in self.nb(a):
            path = [a, first]
            prev, cur = a, first
            reason = "het lien ket"
            while len(path) < MAX_WALK:
                if cur in symbols:
                    reason = "symbol"
                    break
                nxt = [n for n in self.nb(cur) if n != prev]
                if len(nxt) != 1:
                    reason = "nhanh re" if len(nxt) > 1 else "het lien ket"
                    break
                prev, cur = cur, nxt[0]
                if cur in path:
                    reason = "vong lap"
                    break
                path.append(cur)
            branches.append((path, reason))
        return branches


def view_extent(view):
    xs, ys = [], []
    for el in DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType():
        try:
            bb = el.get_BoundingBox(view)
            if bb is None:
                continue
            xs.extend([bb.Min.X, bb.Max.X])
            ys.extend([bb.Min.Y, bb.Max.Y])
        except Exception:
            continue
    if not xs:
        return None
    return (min(xs) - 1.0, min(ys) - 1.0, max(xs) + 1.0, max(ys) + 1.0)


# ---------------------------------------------------------------------
# HIEU CHINH: sx/sy trong log co the lech mot khoang co dinh so voi symbol da ve
# -> do do lech bang cach "bo phieu" giua tat ca cap (log, symbol ve)
# ---------------------------------------------------------------------
FIX_KINDS = (
    ("WC", ("toilet", "wc", "water closet", "pan", "cistern", "closet")),
    ("URINAL", ("urinal",)),
    ("BASIN", ("basin", "lavatory", "hand wash", "handwash", "hwb", "whb")),
    ("SINK", ("sink", "cleaners", "bsk")),
    ("SHOWER", ("shower",)),
    ("BATH", ("bath",)),
    ("FW", ("floor waste", "gully", "fwg", "floor drain", "fw ")),
    ("TUNDISH", ("tundish",)),
    ("DW", ("dishwasher", "washing machine", "appliance")),
)


def fixture_kind(text):
    """Phan loai fixture theo ten family/type: WC, BASIN, SINK, FW... (None neu khong ro)."""
    t = " " + (text or "").lower().replace("_", " ").replace("-", " ") + " "
    for kind, keys in FIX_KINDS:
        for k in keys:
            if k in t:
                return kind
    return None


def drawn_label(el):
    try:
        sym = el.Symbol
        return "{} : {}".format(sym.FamilyName, DB.Element.Name.GetValue(sym))
    except Exception:
        try:
            return el.Name
        except Exception:
            return ""


def compatible(kind_a, kind_b):
    """Chi loai tru khi CA HAI deu biet loai va khac nhau (WC vs BASIN)."""
    return kind_a is None or kind_b is None or kind_a == kind_b


def drawn_symbol_points(view):
    pts = []
    for el in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.FamilyInstance):
        try:
            if isinstance(el.Location, DB.LocationPoint):
                pts.append((el.Location.Point, fixture_kind(drawn_label(el))))
        except Exception:
            pass
    return pts


def calibrate(symbols, view):
    scale = max(1, view.Scale)
    drawn = drawn_symbol_points(view)
    if not drawn or not symbols:
        return 0.0, 0.0, 1, "Khong co symbol ve trong view de hieu chinh."
    cell = CALIB_BIN_PAPER_MM * scale * FT
    best = None
    for fy in (1, -1):
        votes = {}
        for s in symbols:
            for d, dk in drawn:
                if dk is None or s["kind"] is None or dk != s["kind"]:
                    continue                      # chi dung cap chac chan cung loai de do do lech
                dx, dy = d.X - s["x"], d.Y - fy * s["y"]
                k = (int(math.floor(dx / cell)), int(math.floor(dy / cell)))
                votes[k] = votes.get(k, 0) + 1
        # gop o lan can 3x3
        for k in votes:
            c = sum(votes.get((k[0] + i, k[1] + j), 0) for i in (-1, 0, 1) for j in (-1, 0, 1))
            if best is None or c > best[0]:
                best = (c, fy, k)
    if best is None:
        return 0.0, 0.0, 1, "Khong nhan dien duoc loai symbol de hieu chinh - dung sx/sy goc."
    count, fy, k = best
    # tinh lai chinh xac bang median cua cap gan nhat
    dx0, dy0 = (k[0] + 0.5) * cell, (k[1] + 0.5) * cell
    dxs, dys = [], []
    for s in symbols:
        x, y = s["x"] + dx0, fy * s["y"] + dy0
        same = [p for p, pk in drawn if compatible(pk, s["kind"])]
        if not same:
            continue
        d = min(same, key=lambda p: math.hypot(p.X - x, p.Y - y))
        if math.hypot(d.X - x, d.Y - y) <= 3 * cell:
            dxs.append(d.X - s["x"])
            dys.append(d.Y - fy * s["y"])
    if len(dxs) < max(3, 0.2 * len(symbols)):
        return 0.0, 0.0, 1, "Hieu chinh KHONG tin cay ({} / {} symbol khop) - dung sx/sy goc.".format(
            len(dxs), len(symbols))
    dxs.sort()
    dys.sort()
    dx, dy = dxs[len(dxs) // 2], dys[len(dys) // 2]
    to_paper = 304.8 / scale
    return dx, dy, fy, "Hieu chinh sx/sy: lech X={:.1f} mm, Y={:.1f} mm tren giay{} | {} / {} symbol khop".format(
        dx * to_paper, dy * to_paper, " (lat truc Y)" if fy < 0 else "", len(dxs), len(symbols))


def ids_in_element(el, g):
    """Tim ID model ghi san trong parameter / extensible storage cua doi tuong ve."""
    found = []
    texts = []
    try:
        for prm in el.Parameters:
            try:
                if prm.StorageType == DB.StorageType.String:
                    texts.append(prm.AsString() or "")
                elif prm.StorageType == DB.StorageType.Integer:
                    texts.append(str(prm.AsInteger()))
            except Exception:
                pass
    except Exception:
        pass
    try:
        for guid in el.GetEntitySchemaGuids():
            sch = DB.ExtensibleStorage.Schema.Lookup(guid)
            ent = el.GetEntity(sch)
            for fld in sch.ListFields():
                try:
                    texts.append(str(ent.Get[str](fld)))
                except Exception:
                    try:
                        texts.append(str(ent.Get[int](fld)))
                    except Exception:
                        pass
    except Exception:
        pass
    for t in texts:
        for m in re.findall(r"\d{5,}", t or ""):
            i = int(m)
            if i in g.recs and i not in found:
                found.append(i)
    return found


def load_log(data, view):
    elems = data.get("elements") if isinstance(data, dict) else None
    if not isinstance(elems, dict):
        return None, [], "Log khong co muc 'elements'."
    g = Graph(elems)
    raw = []
    for eid, rec in g.recs.items():
        x, y = to_float(rec.get("sx")), to_float(rec.get("sy"))
        if x is not None and y is not None:
            raw.append((eid, rec, x, y))
    if not raw:
        return g, [], "Element trong log khong co sx/sy."
    # kiem tra don vi sx/sy
    ext = view_extent(view)
    f, frac = 1.0, None
    if ext:
        best = -1.0
        for ff in (1.0, FT):
            inside = sum(1 for r in raw if ext[0] <= r[2] * ff <= ext[2] and ext[1] <= r[3] * ff <= ext[3])
            fr = float(inside) / len(raw)
            if fr > best:
                best, f = fr, ff
        frac = best
    symbols = [{"id": eid, "x": x * f, "y": y * f, "rx": x, "ry": y, "rec": rec,
                "kind": fixture_kind(rec.get("t", "")) if kind_of(rec) == "fix" else None}
               for eid, rec, x, y in raw]
    dx, dy, fy, cmsg = calibrate(symbols, view)
    for sym in symbols:
        sym["x"] = sym["x"] + dx
        sym["y"] = fy * sym["y"] + dy
    n_pipe = sum(1 for r in g.recs.values() if kind_of(r) == "pipe")
    msg = ("Element trong log: {} (pipe: {}) | symbol co sx/sy: {} | lien ket n: {} node | "
           "nam trong view: {}").format(len(g.recs), n_pipe, len(symbols), len(g.adj),
                                       "-" if frac is None else "{:.0%}".format(frac))
    return g, symbols, msg + "\n" + cmsg


def model_point(rec):
    pts = []
    for it in (rec or {}).get("p") or []:
        if isinstance(it, (list, tuple)) and len(it) >= 3:
            try:
                pts.append([float(a) for a in it[:3]])
            except Exception:
                pass
    if not pts:
        return None
    c = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
    return DB.XYZ(c[0] * FT, c[1] * FT, c[2] * FT)     # p luu mm


# ---------------------------------------------------------------------
# CHON DOI TUONG VE
# ---------------------------------------------------------------------
class LineFilter(ISelectionFilter):
    def AllowElement(self, el):
        if isinstance(el, DB.CurveElement):
            return True
        if isinstance(el, DB.Group):
            return any(isinstance(doc.GetElement(m), DB.CurveElement) for m in el.GetMemberIds())
        return False

    def AllowReference(self, ref, pt):
        return False


class SymbolFilter(ISelectionFilter):
    def AllowElement(self, el):
        return isinstance(el, (DB.FamilyInstance, DB.TextNote, DB.IndependentTag))

    def AllowReference(self, ref, pt):
        return False


def drawn_curves(el):
    curves = []
    if isinstance(el, DB.CurveElement):
        try:
            curves.append(el.GeometryCurve)
        except Exception:
            pass
    elif isinstance(el, DB.Group):
        for mid in el.GetMemberIds():
            curves.extend(drawn_curves(doc.GetElement(mid)))
    return curves


def drawn_point(el, view):
    try:
        if isinstance(el, DB.FamilyInstance) and isinstance(el.Location, DB.LocationPoint):
            return el.Location.Point
        if isinstance(el, DB.TextNote):
            return el.Coord
        if isinstance(el, DB.IndependentTag):
            return el.TagHeadPosition
    except Exception:
        pass
    bb = el.get_BoundingBox(view)
    return (bb.Min + bb.Max) * 0.5 if bb else None


def d2(p, x, y):
    return math.hypot(p.X - x, p.Y - y)


def build_chains(items, scale):
    """Gop cac net duoc chon co dau trung nhau thanh 1 tuyen. Tra ve [(el_ids, [diem_dau_tu_do])]."""
    tol = JOIN_TOL_PAPER_MM * scale * FT
    segs = []
    for el in items:
        for c in drawn_curves(el):
            try:
                segs.append((el.Id, c.GetEndPoint(0), c.GetEndPoint(1)))
            except Exception:
                pass
    n = len(segs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if any(a.DistanceTo(b) <= tol for a in segs[i][1:] for b in segs[j][1:]):
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    chains = []
    for idxs in groups.values():
        ends = []
        for i in idxs:
            ends.extend([segs[i][1], segs[i][2]])
        free = []
        for k, p in enumerate(ends):
            if sum(1 for q in ends if p.DistanceTo(q) <= tol) == 1:
                free.append(p)
        if not free:                       # vong kin -> lay 2 diem xa nhau nhat
            free = [ends[0], max(ends, key=lambda q: q.DistanceTo(ends[0]))]
        ids = []
        for i in idxs:
            if segs[i][0] not in ids:
                ids.append(segs[i][0])
        chains.append((ids, free))
    return chains


def symbol_at(pt, symbols, scale):
    tol = END_TOL_PAPER_MM * scale * FT
    ranked = sorted(((d2(pt, s["x"], s["y"]), s) for s in symbols), key=lambda a: a[0])
    if ranked and ranked[0][0] <= tol:
        return ranked[0]
    return None


# ---------------------------------------------------------------------
# NET ONG -> PIPE
# ---------------------------------------------------------------------
def resolve_chain(free_ends, symbols, g, scale):
    """Tra ve (path_ids, mo_ta, debug_lines)."""
    to_paper = 304.8 / scale
    sym_ids = set(s["id"] for s in symbols)
    ends = []
    dbg = []
    for p in free_ends:
        hit = symbol_at(p, symbols, scale)
        if hit:
            ends.append((p, hit[1]))
            dbg.append("Dau net -> {} {} ({}) cach {:.1f} mm".format(
                hit[1]["id"], kind_of(hit[1]["rec"]), rec_value(hit[1]["rec"], "t"), hit[0] * to_paper))
        else:
            near = min(symbols, key=lambda s: d2(p, s["x"], s["y"]))
            dbg.append("Dau net -> khong co symbol (gan nhat {} cach {:.1f} mm)".format(
                near["id"], d2(p, near["x"], near["y"]) * to_paper))

    distinct = []
    for p, s in ends:
        if all(s["id"] != q[1]["id"] for q in distinct):
            distinct.append((p, s))

    # 1. Co symbol o >= 2 dau -> duong ngan nhat giua chung qua lien ket n
    if len(distinct) >= 2:
        best = None
        for i in range(len(distinct)):
            for j in range(i + 1, len(distinct)):
                a, b = distinct[i][1]["id"], distinct[j][1]["id"]
                path = g.shortest_path(a, b, sym_ids)
                if path and (best is None or len(path) < len(best)):
                    best = path
        if best:
            return best, "duong noi giua 2 symbol o 2 dau net", dbg
        dbg.append("Khong tim duoc duong noi giua 2 symbol qua lien ket 'n' (khong cung mang?)")

    # 2. Chi 1 dau co symbol -> di tu symbol do toi symbol/nhanh re dau tien,
    #    chon nhanh co diem cuoi gan dau net con lai nhat
    if distinct:
        start_pt, start = distinct[0]
        other = [p for p in free_ends if p.DistanceTo(start_pt) > 1e-6]
        branches = g.walk_from(start["id"], sym_ids)
        if not branches:
            dbg.append("Symbol {} khong co lien ket 'n' trong log".format(start["id"]))
            return None, "khong tim thay", dbg

        def score(br):
            path, reason = br
            last = path[-1]
            s = next((x for x in symbols if x["id"] == last), None)
            if s is not None and other:
                return min(d2(o, s["x"], s["y"]) for o in other)
            pm = model_point(g.recs.get(last))
            # khong co toa do schematic -> uu tien nhanh co pipe
            has_pipe = any(kind_of(g.recs.get(i)) == "pipe" for i in path)
            return 1e9 if not has_pipe else 1e6 - len(path)
        branches.sort(key=score)
        path, reason = branches[0]
        if len(branches) > 1:
            dbg.append("Symbol {} co {} nhanh lien ket - da chon nhanh dung o '{}'".format(
                start["id"], len(branches), reason))
        return path, "di tu symbol o dau net toi {}".format(reason), dbg

    return None, "khong co symbol nao o dau net", dbg


def nearest_symbol_to_curves(curves, symbols, scale):
    best = None
    for s in symbols:
        for c in curves:
            try:
                r = c.Project(DB.XYZ(s["x"], s["y"], c.GetEndPoint(0).Z))
                d = r.Distance if r else float("inf")
            except Exception:
                d = float("inf")
            if best is None or d < best[0]:
                best = (d, s)
    if best and best[0] <= NEAR_LINE_PAPER_MM * scale * FT:
        return best
    return None


def match_symbol(el, symbols, view, scale):
    """Tra ve (symbol_log, khoang_cach_mm, loai_symbol_ve, so_ung_vien_bi_loai)."""
    to_paper = 304.8 / scale
    pt = drawn_point(el, view)
    if pt is None:
        return None, 0, None, 0
    dk = fixture_kind(drawn_label(el))
    pool = [q for q in symbols if compatible(dk, q["kind"])]
    removed = len(symbols) - len(pool)
    if not pool:
        return None, 0, dk, removed
    ranked = sorted(((d2(pt, q["x"], q["y"]), q) for q in pool), key=lambda a: a[0])
    return ranked[0][1], ranked[0][0] * to_paper, dk, removed


# ---------------------------------------------------------------------
# HIEN THI TRONG MODEL
# ---------------------------------------------------------------------
def get_locate_view():
    for v in DB.FilteredElementCollector(doc).OfClass(DB.View3D):
        if not v.IsTemplate and v.Name == LOCATE_VIEW_NAME:
            return v
    vft = next(t for t in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType)
               if t.ViewFamily == DB.ViewFamily.ThreeDimensional)
    with revit.Transaction("Tao view SCH-LOCATE"):
        v = DB.View3D.CreateIsometric(doc, vft.Id)
        v.Name = LOCATE_VIEW_NAME
    return v


def show_in_model(elements, fallback_pts, use_3d):
    ids = List[DB.ElementId]([e.Id for e in elements])
    if not use_3d and elements:
        uidoc.Selection.SetElementIds(ids)
        uidoc.ShowElements(ids)
        return
    mins, maxs = [], []
    for e in elements:
        bb = e.get_BoundingBox(None)
        if bb:
            mins.append(bb.Min)
            maxs.append(bb.Max)
    for p in fallback_pts:
        mins.append(p)
        maxs.append(p)
    if not mins:
        return
    pad = BOX_PAD_MM * FT
    box = DB.BoundingBoxXYZ()
    box.Min = DB.XYZ(min(p.X for p in mins) - pad, min(p.Y for p in mins) - pad, min(p.Z for p in mins) - pad)
    box.Max = DB.XYZ(max(p.X for p in maxs) + pad, max(p.Y for p in maxs) + pad, max(p.Z for p in maxs) + pad)
    view = get_locate_view()
    with revit.Transaction("Section box SCH-LOCATE"):
        view.IsSectionBoxActive = True
        view.SetSectionBox(box)
    uidoc.ActiveView = view
    if elements:
        uidoc.Selection.SetElementIds(ids)
        uidoc.ShowElements(ids)
    else:
        for uv in uidoc.GetOpenUIViews():
            if uv.ViewId == view.Id:
                uv.ZoomAndCenterRectangle(box.Min, box.Max)


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------
def get_drawn_selection(view, mode):
    flt = LineFilter() if mode == MODE_LINE else SymbolFilter()
    pre = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    pre = [e for e in pre if e is not None and e.OwnerViewId == view.Id and flt.AllowElement(e)]
    if pre:
        return pre
    prompt = ("Chon cac net ONG (co the chon nhieu doan lien nhau), bam Finish" if mode == MODE_LINE
              else "Chon cac SYMBOL tren schematic, bam Finish")
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, flt, prompt)
        return [doc.GetElement(r.ElementId) for r in refs]
    except OperationCanceledException:
        return []


def row_for(eid, rec, m_el, role):
    return [str(eid), role, rec_value(rec, "cat") or kind_of(rec), rec_value(rec, "t"),
            rec_value(rec, "s"), rec_value(rec, "l"), rec_value(rec, "sz"),
            "Co" if m_el is not None else "Da xoa"]


def main():
    sch_view = doc.ActiveView
    if sch_view.ViewType not in (DB.ViewType.DraftingView, DB.ViewType.Detail,
                                 DB.ViewType.FloorPlan, DB.ViewType.Section, DB.ViewType.Legend):
        forms.alert("Hay mo view schematic (Drafting View) truoc khi chay.", exitscript=True)

    log_path = choose_log(sch_view)
    if not log_path:
        script.exit()
    with open(log_path, "r") as f:
        data = json.load(f)
    g, symbols, msg = load_log(data, sch_view)
    if not symbols:
        forms.alert(msg, exitscript=True)

    mode = forms.CommandSwitchWindow.show([MODE_LINE, MODE_SYMBOL], message="Lay ID cua:")
    if not mode:
        script.exit()
    drawn = get_drawn_selection(sch_view, mode)
    if not drawn:
        script.exit()

    scale = max(1, sch_view.Scale)
    output.print_md("## Schematic -> Model")
    print("Log: {}".format(log_path))
    print(msg)

    found = {}           # id -> (rec, m_el, role)
    rows = []

    if mode == MODE_LINE:
        for ids, free in build_chains(drawn, scale):
            output.print_md("### Tuyen ong: {}".format(", ".join(output.linkify(i) for i in ids)))
            direct = []
            for i in ids:
                direct.extend(ids_in_element(doc.GetElement(i), g))
            if direct:
                path, how, dbg = direct, "ID ghi san trong parameter cua net", []
            else:
                path, how, dbg = resolve_chain(free, symbols, g, scale)
                if not path:
                    curves = []
                    for i in ids:
                        curves.extend(drawn_curves(doc.GetElement(i)))
                    hit = nearest_symbol_to_curves(curves, symbols, scale)
                    if hit:
                        dbg.append("Lay symbol gan net nhat: {} cach {:.1f} mm".format(
                            hit[1]["id"], hit[0] * 304.8 / scale))
                        path, how, more = resolve_chain(
                            [DB.XYZ(hit[1]["x"], hit[1]["y"], 0)] + list(free), symbols, g, scale)
                        dbg.extend(more)
            for line in dbg:
                print("   " + line)
            if not path:
                print("   => Khong tim thay Pipe ({})".format(how))
                continue
            pipes = [i for i in path if kind_of(g.recs.get(i)) == "pipe"]
            print("   => {} | {} doi tuong tren duong, {} Pipe".format(how, len(path), len(pipes)))
            for i in path:
                rec = g.recs.get(i, {})
                k = kind_of(rec)
                m_el = doc.GetElement(make_eid(i))
                role = "PIPE" if k == "pipe" else ("dau net" if i in (path[0], path[-1]) else "tren duong")
                print("      {} {:<4} | {} | {} | DN{}{}".format(
                    output.linkify(m_el.Id) if m_el is not None else i, k,
                    rec_value(rec, "t"), rec_value(rec, "s"), rec_value(rec, "sz"),
                    "" if m_el is not None else "  <-- DA BI XOA"))
                if k == "pipe":
                    found[i] = (rec, m_el, role)
                    rows.append(row_for(i, rec, m_el, role))
            if not pipes:
                print("   => Duong noi khong co Pipe nao (2 symbol noi truc tiep qua fitting).")
    else:
        for el in drawn:
            output.print_md("### Symbol {}".format(output.linkify(el.Id)))
            direct = ids_in_element(el, g)
            if direct:
                for i in direct:
                    rec = g.recs[i]
                    m_el = doc.GetElement(make_eid(i))
                    print("   {} | {} | {} | (ID ghi san trong parameter)".format(
                        output.linkify(m_el.Id) if m_el is not None else i, kind_of(rec), rec_value(rec, "t")))
                    found[i] = (rec, m_el, "symbol")
                    rows.append(row_for(i, rec, m_el, "symbol"))
                continue
            s, d, dk, removed = match_symbol(el, symbols, sch_view, scale)
            print("   Symbol ve: {} -> loai {} (loai bo {} doi tuong log khac loai)".format(
                drawn_label(el), dk or "khong ro", removed))
            pt = drawn_point(el, sch_view)
            if pt is not None:
                print("   Symbol ve tai X={:.2f} Y={:.2f} ft".format(pt.X, pt.Y))
                to_paper = 304.8 / scale
                pool = [q for q in symbols if compatible(dk, q["kind"])]
                for dd, ss in sorted(((d2(pt, q["x"], q["y"]), q) for q in pool), key=lambda a: a[0])[:3]:
                    print("      log {} {} | sx={:.2f} sy={:.2f} | lech dX={:.1f} dY={:.1f} mm giay".format(
                        ss["id"], rec_value(ss["rec"], "t"), ss["rx"], ss["ry"],
                        (pt.X - ss["x"]) * to_paper, (pt.Y - ss["y"]) * to_paper))
            if s is None:
                print("   Khong tim thay.")
                continue
            m_el = doc.GetElement(make_eid(s["id"]))
            warn = "" if d <= SYMBOL_TOL_PAPER_MM else "  (NGOAI dung sai {} mm)".format(int(SYMBOL_TOL_PAPER_MM))
            print("   {} | {} | {} | cach {:.1f} mm{}".format(
                output.linkify(m_el.Id) if m_el is not None else s["id"],
                kind_of(s["rec"]), rec_value(s["rec"], "t"), d, warn))
            found[s["id"]] = (s["rec"], m_el, "symbol")
            rows.append(row_for(s["id"], s["rec"], m_el, "symbol"))

    if not found:
        forms.alert("Khong tim thay doi tuong model nao. Xem cua so output.")
        return

    output.print_md("### Tong hop: {} doi tuong model".format(len(found)))
    output.print_table(rows, columns=["ID", "Vai tro", "Category", "Family : Type", "System",
                                      "Level", "Size", "Trong model"])
    id_text = ";".join(str(i) for i in sorted(found))
    print("ID (cach nhau ';'): {}".format(id_text))
    if copy_to_clipboard(id_text):
        print("Da copy danh sach ID vao clipboard (dan vao Manage > Select by ID).")

    alive = [v[1] for v in found.values() if v[1] is not None]
    deleted_pts = [p for p in (model_point(v[0]) for v in found.values() if v[1] is None) if p]
    choice = forms.CommandSwitchWindow.show(
        ["Chon + zoom trong 3D view rieng (Section box)",
         "Chon + zoom (Revit tu chon view)",
         "Chi lay ID, khong zoom"],
        message="{} doi tuong model ({} da bi xoa). Tiep theo:".format(len(found), len(found) - len(alive)))
    if not choice or choice.startswith("Chi"):
        return
    show_in_model(alive, deleted_pts, use_3d=choice.startswith("Chon + zoom trong 3D"))


if __name__ == "__main__":
    main()
