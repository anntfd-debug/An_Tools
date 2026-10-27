# -*- coding: utf-8 -*-
__title__ = "Scan PA\nComment"
__doc__ = ("Chọn 1 family trong active view -> lấy tất cả instance cùng type trong view -> "
           "quét vùng cầu/khối bán kính 250mm quanh mỗi instance -> nếu chạm Pipe Accessory "
           "(Host / Link / cả hai, chỉ trong active view) thì lấy Comments của PA gần nhất "
           "ghi vào Comments.")

import math
from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
active_view = doc.ActiveView
output = script.get_output()

# ============================== CONFIG ==============================
RADIUS_MM = 300.0            # bán kính quét
SCAN_SHAPE = "sphere"        # "sphere" = hình cầu | "cube" = khối lập phương cạnh 2R
                             # "bbox"   = bounding box của family mở rộng thêm R mỗi phía
SCAN_FROM = "location"       # tâm quét cho sphere/cube: "location" = điểm insert | "bbox_center"
ACTIVE_VIEW_ONLY = True      # True = chỉ quét PA đang hiển thị trong active view
ASK_SCOPE = True             # True = hỏi phạm vi quét mỗi lần chạy
SCAN_SCOPE = "host"          # dùng khi ASK_SCOPE = False: "host" | "link" | "both"
USE_POINT_FALLBACK = True    # nếu PA không có solid geometry: tính là chạm khi điểm insert nằm trong vùng quét
OVERWRITE_EXISTING = True    # False = không ghi đè Comments đã có giá trị
TARGET_BIC = DB.BuiltInCategory.OST_PipeAccessory
# ====================================================================

SCOPE_OPTIONS = [
    ("Chỉ model hiện hành (Host)", "host"),
    ("Chỉ Revit Link", "link"),
    ("Host + Link", "both"),
]
SCOPE_LABEL = dict((v, k) for (k, v) in SCOPE_OPTIONS)


def mm_to_ft(v):
    return v / 304.8


def eid_int(eid):
    try:
        return eid.IntegerValue
    except Exception:
        return eid.Value  # Revit 2024+


class FamInstSelFilter(ISelectionFilter):
    def AllowElement(self, e):
        return isinstance(e, DB.FamilyInstance)

    def AllowReference(self, ref, pt):
        return False


# ---------------------------- SELECTION -----------------------------
def get_source_instance():
    for eid in uidoc.Selection.GetElementIds():
        e = doc.GetElement(eid)
        if isinstance(e, DB.FamilyInstance):
            return e
    try:
        ref = uidoc.Selection.PickObject(ObjectType.Element, FamInstSelFilter(),
                                         "Chọn 1 family trong active view")
        return doc.GetElement(ref.ElementId)
    except OperationCanceledException:
        return None


def get_same_type_in_view(src):
    sym_id = src.Symbol.Id
    col = DB.FilteredElementCollector(doc, active_view.Id) \
        .WherePasses(DB.FamilyInstanceFilter(doc, sym_id)) \
        .WhereElementIsNotElementType()
    return list(col)


def ask_scope():
    if not ASK_SCOPE:
        return SCAN_SCOPE
    labels = [k for (k, v) in SCOPE_OPTIONS]
    sel = forms.CommandSwitchWindow.show(labels, message="Quét Pipe Accessory trong:")
    if not sel:
        return None
    for (k, v) in SCOPE_OPTIONS:
        if k == sel:
            return v
    return None


# ------------------------------ HELPERS -----------------------------
def get_type_name(el, d):
    t = d.GetElement(el.GetTypeId())
    if t is None:
        return "", ""
    tname = ""
    p = t.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
    if p and p.AsString():
        tname = p.AsString()
    else:
        try:
            tname = DB.Element.Name.GetValue(t)
        except Exception:
            tname = getattr(t, "Name", "") or ""
    fname = ""
    if isinstance(t, DB.FamilySymbol):
        try:
            fname = t.FamilyName or ""
        except Exception:
            fname = ""
    return tname, fname


def get_comment(el):
    p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if p is None:
        return ""
    v = p.AsString()
    return v.strip() if v else ""


def bb_center(el):
    bb = el.get_BoundingBox(None)
    if bb is None:
        return None
    return bb.Min.Add(bb.Max).Multiply(0.5)


def el_point(el):
    loc = el.Location
    if isinstance(loc, DB.LocationPoint):
        return loc.Point
    if isinstance(loc, DB.LocationCurve):
        return loc.Curve.Evaluate(0.5, True)
    return bb_center(el)


def scan_center(el):
    if SCAN_FROM == "location":
        loc = el.Location
        if isinstance(loc, DB.LocationPoint):
            return loc.Point
    return bb_center(el)


def minmax(pts):
    xs = [p.X for p in pts]
    ys = [p.Y for p in pts]
    zs = [p.Z for p in pts]
    return DB.XYZ(min(xs), min(ys), min(zs)), DB.XYZ(max(xs), max(ys), max(zs))


# ------------------------ ACTIVE VIEW FILTER ------------------------
def host_visible_ids():
    """Id các PA trong host đang hiển thị trong active view."""
    ids = DB.FilteredElementCollector(doc, active_view.Id).OfCategory(TARGET_BIC) \
        .WhereElementIsNotElementType().ToElementIds()
    return set(eid_int(i) for i in ids)


def link_visible_ids(li):
    """
    Trả về (allowed_ids, mode):
      - allowed_ids = set id PA trong link đang hiển thị trong view (Revit 2024+)
      - allowed_ids = None -> không lọc được theo view, quét toàn bộ link (Revit cũ)
    """
    try:
        col = DB.FilteredElementCollector(doc, active_view.Id, li.Id)   # Revit 2024+
        ids = col.OfCategory(TARGET_BIC).WhereElementIsNotElementType().ToElementIds()
        return set(eid_int(i) for i in ids), "view"
    except Exception:
        return None, "full"


def get_links():
    """(link_inst, link_doc, transform, allowed_ids, mode) cho các link đang load."""
    res = []
    for li in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance):
        ld = li.GetLinkDocument()
        if ld is None:
            continue
        allowed, mode = None, "full"
        if ACTIVE_VIEW_ONLY:
            try:
                if li.IsHidden(active_view):
                    continue                       # link bị ẩn trong view -> bỏ
            except Exception:
                pass
            allowed, mode = link_visible_ids(li)
        res.append((li, ld, li.GetTotalTransform(), allowed, mode))
    return res


# --------------------------- SOLID BUILDERS -------------------------
def make_curveloop_list(loop):
    LoopList = List[DB.CurveLoop]
    loops = LoopList()
    loops.Add(loop)
    return loops


def make_sphere(c, r):
    frame = DB.Frame(c, DB.XYZ.BasisX, DB.XYZ.BasisY, DB.XYZ.BasisZ)
    top = c.Add(DB.XYZ(0, 0, r))
    bot = c.Add(DB.XYZ(0, 0, -r))
    side = c.Add(DB.XYZ(r, 0, 0))
    loop = DB.CurveLoop()
    loop.Append(DB.Arc.Create(bot, top, side))     # nửa đường tròn trong mặt XZ
    loop.Append(DB.Line.CreateBound(top, bot))     # đoạn thẳng trên trục Z
    return DB.GeometryCreationUtilities.CreateRevolvedGeometry(
        frame, make_curveloop_list(loop), 0.0, 2.0 * math.pi)


def make_box(mn, mx):
    p0 = DB.XYZ(mn.X, mn.Y, mn.Z)
    p1 = DB.XYZ(mx.X, mn.Y, mn.Z)
    p2 = DB.XYZ(mx.X, mx.Y, mn.Z)
    p3 = DB.XYZ(mn.X, mx.Y, mn.Z)
    loop = DB.CurveLoop()
    loop.Append(DB.Line.CreateBound(p0, p1))
    loop.Append(DB.Line.CreateBound(p1, p2))
    loop.Append(DB.Line.CreateBound(p2, p3))
    loop.Append(DB.Line.CreateBound(p3, p0))
    return DB.GeometryCreationUtilities.CreateExtrusionGeometry(
        make_curveloop_list(loop), DB.XYZ.BasisZ, mx.Z - mn.Z)


class ScanRegion(object):
    """Vùng quét trong toạ độ host."""

    def __init__(self, el, r):
        self.r = r
        self.kind = SCAN_SHAPE
        rv = DB.XYZ(r, r, r)
        if self.kind == "bbox":
            bb = el.get_BoundingBox(None)
            if bb is None:
                raise Exception("Không lấy được bounding box")
            self.mn = bb.Min.Subtract(rv)
            self.mx = bb.Max.Add(rv)
            self.center = self.mn.Add(self.mx).Multiply(0.5)
            self.solid = make_box(self.mn, self.mx)
        else:
            c = scan_center(el)
            if c is None:
                raise Exception("Không lấy được vị trí")
            self.center = c
            self.mn = c.Subtract(rv)
            self.mx = c.Add(rv)
            if self.kind == "sphere":
                self.solid = make_sphere(c, r)
            else:
                self.solid = make_box(self.mn, self.mx)

    def contains(self, p):
        if self.kind == "sphere":
            return p.DistanceTo(self.center) <= self.r + 1e-9
        return (self.mn.X <= p.X <= self.mx.X and
                self.mn.Y <= p.Y <= self.mx.Y and
                self.mn.Z <= p.Z <= self.mx.Z)

    def corners(self):
        a, b = self.mn, self.mx
        return [DB.XYZ(x, y, z) for x in (a.X, b.X) for y in (a.Y, b.Y) for z in (a.Z, b.Z)]


# ------------------------------- SCAN -------------------------------
def find_in_doc(d, region, to_host, exclude_ids, allowed_ids):
    """
    to_host     = None (host) hoặc Transform link -> host
    allowed_ids = None (không lọc) hoặc set id được phép (đang hiển thị trong view)
    """
    if allowed_ids is not None and not allowed_ids:
        return []

    if to_host is None:
        solid = region.solid
        pts = region.corners()
    else:
        inv = to_host.Inverse
        solid = DB.SolidUtils.CreateTransformed(region.solid, inv)
        pts = [inv.OfPoint(p) for p in region.corners()]

    mn, mx = minmax(pts)
    bb_filter = DB.BoundingBoxIntersectsFilter(DB.Outline(mn, mx))
    cands = list(DB.FilteredElementCollector(d).OfCategory(TARGET_BIC)
                 .WhereElementIsNotElementType().WherePasses(bb_filter))
    if allowed_ids is not None:
        cands = [e for e in cands if eid_int(e.Id) in allowed_ids]
    if not cands:
        return []

    solid_ids = set()
    try:
        ids = DB.FilteredElementCollector(d).OfCategory(TARGET_BIC) \
            .WhereElementIsNotElementType().WherePasses(bb_filter) \
            .WherePasses(DB.ElementIntersectsSolidFilter(solid)).ToElementIds()
        solid_ids = set(eid_int(i) for i in ids)
    except Exception:
        pass

    out = []
    for e in cands:
        if eid_int(e.Id) in exclude_ids:
            continue
        p = el_point(e)
        p_host = None
        if p is not None:
            p_host = p if to_host is None else to_host.OfPoint(p)
        hit = eid_int(e.Id) in solid_ids
        if not hit and USE_POINT_FALLBACK and p_host is not None and region.contains(p_host):
            hit = True
        if hit:
            dist = p_host.DistanceTo(region.center) if p_host is not None else None
            out.append((e, d, dist))
    return out


def scan_element(el, links, source_ids, use_host, use_links, host_allowed):
    region = ScanRegion(el, mm_to_ft(RADIUS_MM))
    found = []   # (pa, pa_doc, dist_ft, source_name)
    if use_host:
        for (e, d, dist) in find_in_doc(doc, region, None, source_ids, host_allowed):
            found.append((e, d, dist, "Host"))
    if use_links:
        for (li, ld, T, allowed, mode) in links:
            for (e, d, dist) in find_in_doc(ld, region, T, set(), allowed):
                found.append((e, d, dist, li.Name))
    found.sort(key=lambda x: x[2] if x[2] is not None else 1e9)
    return found


def pick_comment(found):
    """Chọn PA gần nhất có Comments khác rỗng; trả thêm danh sách comment khác nhau."""
    chosen = None
    distinct = []
    for (e, d, dist, src) in found:
        c = get_comment(e)
        if not c:
            continue
        if c not in distinct:
            distinct.append(c)
        if chosen is None:
            chosen = (e, d, dist, src, c)
    return chosen, distinct


def write_comment(el, value):
    p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if p is None or p.IsReadOnly:
        return "Comments read-only"
    if (not OVERWRITE_EXISTING) and p.AsString():
        return "Bỏ qua (đã có: {})".format(p.AsString())
    try:
        p.Set(value)
        return "OK"
    except Exception as ex:
        return "Lỗi: {}".format(ex)


# ------------------------------- MAIN -------------------------------
def main():
    if active_view.ViewType in (DB.ViewType.DrawingSheet, DB.ViewType.Schedule,
                                DB.ViewType.Legend, DB.ViewType.DraftingView):
        forms.alert("Active view phải là view mô hình (plan / section / 3D).", exitscript=True)

    src = get_source_instance()
    if src is None:
        script.exit()

    scope = ask_scope()
    if scope not in ("host", "link", "both"):
        script.exit()
    use_host = scope in ("host", "both")
    use_links = scope in ("link", "both")

    links = get_links() if use_links else []
    if scope == "link" and not links:
        forms.alert("Không có Revit Link nào đang load / hiển thị trong active view.",
                    exitscript=True)

    host_allowed = None
    if use_host and ACTIVE_VIEW_ONLY:
        host_allowed = host_visible_ids()

    instances = get_same_type_in_view(src)
    if not instances:
        forms.alert("Không tìm thấy instance cùng type trong active view.", exitscript=True)

    source_ids = set(eid_int(e.Id) for e in instances)
    src_tname, src_fname = get_type_name(src, doc)

    n = len(instances)
    total_steps = n * 2          # n bước quét + n bước ghi Comments
    step = 0
    cancelled = False
    results = []
    t = None

    try:
        with forms.ProgressBar(title="Scan PA – Fill Comments ({value}/{max_value})",
                               cancellable=True) as pb:

            # ---------- Phase 1: quét (chỉ đọc, không đổi model) ----------
            for el in instances:
                if pb.cancelled:
                    cancelled = True
                    break
                r = {"el": el, "value": None, "pa": None, "pa_doc": None, "dist": None,
                     "src": "", "count": 0, "distinct": [], "status": ""}
                try:
                    found = scan_element(el, links, source_ids, use_host, use_links,
                                         host_allowed)
                    r["count"] = len(found)
                    chosen, distinct = pick_comment(found)
                    r["distinct"] = distinct
                    if chosen:
                        r["pa"], r["pa_doc"], r["dist"], r["src"], r["value"] = chosen
                    elif found:
                        r["pa"], r["pa_doc"], r["dist"], r["src"] = found[0]
                        r["status"] = "PA trong vùng quét không có Comments"
                    else:
                        r["status"] = "Không chạm Pipe Accessory"
                except Exception as ex:
                    r["status"] = "Lỗi quét: {}".format(ex)
                results.append(r)
                step += 1
                pb.update_progress(step, total_steps)

            # ---------- Phase 2: ghi Comments ----------
            if not cancelled:
                t = DB.Transaction(doc, "Scan PA - Fill Comments")
                t.Start()
                for r in results:
                    if pb.cancelled:
                        cancelled = True
                        break
                    if not r["status"] and r["value"]:
                        r["status"] = write_comment(r["el"], r["value"])
                        if r["status"] == "OK" and len(r["distinct"]) > 1:
                            r["status"] = "OK (⚠ {} comment khác nhau: {})".format(
                                len(r["distinct"]), " / ".join(r["distinct"]))
                    step += 1
                    pb.update_progress(step, total_steps)

                if cancelled:
                    t.RollBack()
                else:
                    t.Commit()

        if cancelled:
            forms.alert("Đã hủy. Không có thay đổi nào được ghi vào model.\n"
                        "(Đã xử lý {}/{} bước)".format(step, total_steps))
            return
    except Exception as ex:
        try:
            if t is not None and t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        forms.alert("Lỗi: {}".format(ex))
        return

    # ---------------------------- REPORT ----------------------------
    output.print_md("## Scan Pipe Accessory – Fill Comments")
    output.print_md("**Source type:** {} : {}  |  **Số instance:** {}  |  "
                    "**Vùng quét:** {} R={}mm  |  **Phạm vi:** {}  |  **Chỉ active view:** {}"
                    .format(src_fname, src_tname, n, SCAN_SHAPE, int(RADIUS_MM),
                            SCOPE_LABEL.get(scope, scope),
                            "Có" if ACTIVE_VIEW_ONLY else "Không"))
    if use_host and host_allowed is not None:
        output.print_md("- Host: {} PA hiển thị trong view".format(len(host_allowed)))
    for (li, ld, T, allowed, mode) in links:
        if mode == "view":
            output.print_md("- Link **{}**: {} PA hiển thị trong view".format(li.Name, len(allowed)))
        else:
            output.print_md("- Link **{}**: ⚠ không lọc được theo view (Revit < 2024), "
                            "quét toàn bộ link".format(li.Name))

    rows = []
    ok = 0
    for r in results:
        if r["status"].startswith("OK"):
            ok += 1
        pa = r["pa"]
        if pa is None:
            pa_id, pa_type = "-", "-"
        else:
            pa_id = output.linkify(pa.Id) if r["src"] == "Host" else eid_int(pa.Id)
            pa_type = get_type_name(pa, r["pa_doc"])[0] or "-"
        dist_mm = round(r["dist"] * 304.8, 0) if r["dist"] is not None else "-"
        rows.append([output.linkify(r["el"].Id), r["value"] or "-", pa_id, pa_type,
                     r["src"] or "-", dist_mm, r["count"], r["status"]])
    output.print_table(rows, columns=["Element", "Comment ghi", "PA ID", "PA Type", "Source",
                                      "KC tới PA (mm)", "Số PA trong vùng", "Trạng thái"])
    output.print_md("**Đã ghi Comments:** {}/{}".format(ok, n))


main()