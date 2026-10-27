# -*- coding: utf-8 -*-
__title__ = "Auto\nConnect"
__doc__ = ("Quét family/conduit chưa kết nối quanh đối tượng được chọn (R=300mm), "
           "kéo về (xoay/kéo dài nếu cần) và kết nối. Hỗ trợ Pipe, Duct, Conduit, Fire Alarm Device.")

import math
from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilyInstance, MEPCurve, ConnectorType,
    ConnectorProfileType, Domain, XYZ, Line, ElementTransformUtils,
    Transaction, SubTransaction, ElementId, LocationPoint,
    IFailuresPreprocessor, FailureProcessingResult, FailureSeverity
)
from Autodesk.Revit.DB.Electrical import Conduit
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView
output = script.get_output()

# ======================= CẤU HÌNH =======================
RADIUS_MM                  = 300.0  # bán kính cầu quét
ALLOW_ROTATE               = True   # cho phép xoay family/conduit xung quanh
PRESERVE_PLANE_STRICT      = False  # True = chỉ xoay trong mặt phẳng family, không nghiêng
ONLY_FULLY_FREE            = True   # family: chỉ kéo family chưa có connector nào kết nối
CHECK_SIZE                 = False  # True = bỏ qua cặp connector khác size
SIZE_TOL_MM                = 1.0
ANG_TOL                    = math.radians(0.5)

INCLUDE_CONDUIT_CANDIDATES = True   # cho phép kéo/kéo dài đầu conduit về target
CONDUIT_TO_CONDUIT         = False  # nối thẳng conduit-conduit (không có fitting)
COLINEAR_TOL_MM            = 2.0    # sai lệch trục cho phép khi kéo dài conduit
MIN_CONDUIT_LEN_MM         = 25.0   # chiều dài conduit tối thiểu sau khi kéo

ALLOWED_DOMAINS = [Domain.DomainPiping,
                   Domain.DomainHvac,
                   Domain.DomainCableTrayConduit]   # Electrical (circuit) = logical, bỏ qua
# ========================================================

R        = RADIUS_MM / 304.8
COL_TOL  = COLINEAR_TOL_MM / 304.8
MIN_LEN  = MIN_CONDUIT_LEN_MM / 304.8
CT_SURFACE = getattr(ConnectorType, "MasterSurface", None)


def eid_int(i):
    try:
        return i.Value
    except AttributeError:
        return i.IntegerValue


# ---------------- Connector helpers ----------------
def get_cm(e):
    try:
        if isinstance(e, MEPCurve):
            return e.ConnectorManager
        if isinstance(e, FamilyInstance) and e.MEPModel is not None:
            return e.MEPModel.ConnectorManager
    except Exception:
        pass
    return None


def all_conns(e):
    res = []
    cm = get_cm(e)
    if cm is None:
        return res
    for c in cm.Connectors:
        try:
            if c.Domain in ALLOWED_DOMAINS:
                res.append(c)
        except Exception:
            pass
    return res


def is_end(c):
    try:
        return c.ConnectorType == ConnectorType.End
    except Exception:
        return False


def is_surface(c):
    if CT_SURFACE is None:
        return False
    try:
        return c.ConnectorType == CT_SURFACE
    except Exception:
        return False


def end_conns(e):
    return [c for c in all_conns(e) if is_end(c)]


def free_end_conns(e):
    return [c for c in end_conns(e) if not c.IsConnected]


def target_conns(e):
    """Connector End còn trống + Surface connector (nhận được nhiều kết nối)."""
    return [c for c in all_conns(e)
            if (is_end(c) and not c.IsConnected) or is_surface(c)]


def conn_by_id(e, cid):
    for c in all_conns(e):
        if c.Id == cid:
            return c
    return None


def conn_size(c):
    try:
        if c.Shape == ConnectorProfileType.Round:
            return (round(c.Radius * 2 * 304.8, 1),)
        return (round(c.Width * 304.8, 1), round(c.Height * 304.8, 1))
    except Exception:
        return None


def size_match(a, b):
    sa, sb = conn_size(a), conn_size(b)
    if sa is None or sb is None or len(sa) != len(sb):
        return False
    return all(abs(x - y) <= SIZE_TOL_MM for x, y in zip(sa, sb))


def loc_point(e):
    loc = e.Location
    if isinstance(loc, LocationPoint):
        return loc.Point
    return None


def elem_name(e):
    try:
        return e.Name
    except Exception:
        return ""


# ---------------- Surface connector helpers ----------------
def project_to_plane(a, p):
    n = a.CoordinateSystem.BasisZ
    return p.Subtract(n.Multiply(p.Subtract(a.Origin).DotProduct(n)))


def clamp_to_surface(a, p):
    cs = a.CoordinateSystem
    o = a.Origin
    v = p.Subtract(o)
    x = v.DotProduct(cs.BasisX)
    y = v.DotProduct(cs.BasisY)
    try:
        if a.Shape == ConnectorProfileType.Round:
            r = a.Radius
            L = math.hypot(x, y)
            if L > r and L > 1e-9:
                x, y = x * r / L, y * r / L
        else:
            hw, hh = a.Width / 2.0, a.Height / 2.0
            x = max(-hw, min(hw, x))
            y = max(-hh, min(hh, y))
    except Exception:
        pass
    return o.Add(cs.BasisX.Multiply(x)).Add(cs.BasisY.Multiply(y))


def target_point(a, ref_pt):
    """Điểm nối trên target: End = Origin; Surface = hình chiếu của ref_pt lên mặt."""
    if is_surface(a):
        return clamp_to_surface(a, project_to_plane(a, ref_pt))
    return a.Origin


# ---------------- Failure handling ----------------
class SwallowWarnings(IFailuresPreprocessor):
    def PreprocessFailures(self, fa):
        for f in fa.GetFailureMessages():
            if f.GetSeverity() == FailureSeverity.Warning:
                fa.DeleteWarning(f)
        return FailureProcessingResult.Continue


# ---------------- Selection ----------------
class ConnFilter(ISelectionFilter):
    def AllowElement(self, e):
        return len(target_conns(e)) > 0

    def AllowReference(self, ref, pt):
        return False


def get_targets():
    ids = list(uidoc.Selection.GetElementIds())
    elems = [doc.GetElement(i) for i in ids]
    elems = [e for e in elems if e is not None and target_conns(e)]
    if elems:
        return elems
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element, ConnFilter(),
            "Chọn family / conduit / fire alarm device cần nhận kết nối, nhấn Finish")
        return [doc.GetElement(r.ElementId) for r in refs]
    except OperationCanceledException:
        return []


# ---------------- Rotation ----------------
def plane_normal(e):
    if isinstance(e, FamilyInstance):
        try:
            return e.GetTransform().BasisZ.Normalize()
        except Exception:
            pass
    return XYZ.BasisZ        # Conduit / MEPCurve: ưu tiên xoay quanh trục đứng


def choose_axis(cand, cur, target):
    """Trả về (axis, giữ_mặt_phẳng)."""
    n = plane_normal(cand)
    if abs(cur.DotProduct(n)) < 1e-3 and abs(target.DotProduct(n)) < 1e-3:
        return n, True
    cr = cur.CrossProduct(target)
    if cr.GetLength() > 1e-6:
        return cr.Normalize(), False
    if abs(cur.DotProduct(n)) < 1e-3:
        return n, True
    alt = cur.CrossProduct(XYZ.BasisZ)
    if alt.GetLength() < 1e-6:
        alt = cur.CrossProduct(XYZ.BasisX)
    return alt.Normalize(), False


def align_candidate(cand, b_id, target_dir):
    b = conn_by_id(cand, b_id)
    cur = b.CoordinateSystem.BasisZ
    if cur.AngleTo(target_dir) <= ANG_TOL:
        return True, ""
    if not ALLOW_ROTATE:
        return False, "Cần xoay nhưng ALLOW_ROTATE = False"
    axis, in_plane = choose_axis(cand, cur, target_dir)
    if PRESERVE_PLANE_STRICT and not in_plane:
        return False, "Phải nghiêng family mới khớp (PRESERVE_PLANE_STRICT)"
    ang = cur.AngleOnPlaneTo(target_dir, axis)
    ElementTransformUtils.RotateElement(
        doc, cand.Id, Line.CreateUnbound(b.Origin, axis), ang)
    doc.Regenerate()
    b = conn_by_id(cand, b_id)
    if b is None or b.CoordinateSystem.BasisZ.AngleTo(target_dir) > ANG_TOL * 2:
        return False, "Không xoay được theo trục yêu cầu"
    return True, "Xoay {:.1f}° ({})".format(
        math.degrees(ang), "trong mặt phẳng" if in_plane else "nghiêng")


# ---------------- Connect actions ----------------
def check_and_connect(t, aid, e, bid):
    a = conn_by_id(t, aid)
    b = conn_by_id(e, bid)
    if b.Origin.DistanceTo(target_point(a, b.Origin)) > 1e-4:
        raise Exception("Connector chưa trùng sau khi kéo (family bị host giữ?)")
    b.ConnectTo(a)
    doc.Regenerate()


def move_and_connect(t, aid, e, bid):
    """Kéo nguyên khối (family hoặc conduit tự do): xoay nếu cần -> move -> connect."""
    a = conn_by_id(t, aid)
    tdir = a.CoordinateSystem.BasisZ.Negate()
    ok, note = align_candidate(e, bid, tdir)
    if not ok:
        raise Exception(note)
    a = conn_by_id(t, aid)
    b = conn_by_id(e, bid)
    v = target_point(a, b.Origin).Subtract(b.Origin)
    if v.GetLength() > 1e-9:
        ElementTransformUtils.MoveElement(doc, e.Id, v)
        doc.Regenerate()
    check_and_connect(t, aid, e, bid)
    return "Move" + (" + " + note if note else "")


def try_stretch_conduit(t, aid, cd, bid):
    """Kéo dài / rút ngắn đầu conduit dọc theo trục. Trả về (True, note) hoặc (None, lý do)."""
    a = conn_by_id(t, aid)
    b = conn_by_id(cd, bid)
    crv = cd.Location.Curve
    p0, p1 = crv.GetEndPoint(0), crv.GetEndPoint(1)
    idx = 0 if b.Origin.DistanceTo(p0) <= b.Origin.DistanceTo(p1) else 1
    fixed = p1 if idx == 0 else p0
    d = b.Origin.Subtract(fixed).Normalize()
    n = a.CoordinateSystem.BasisZ

    if d.AngleTo(n.Negate()) > ANG_TOL:
        return None, "Conduit không đối hướng connector target"

    if is_surface(a):
        s = a.Origin.Subtract(fixed).DotProduct(n) / d.DotProduct(n)
        p = fixed.Add(d.Multiply(s))
        if p.DistanceTo(clamp_to_surface(a, p)) > COL_TOL:
            return None, "Trục conduit nằm ngoài surface connector"
    else:
        p = a.Origin
        s = p.Subtract(fixed).DotProduct(d)
        off = p.Subtract(fixed.Add(d.Multiply(s))).GetLength()
        if off > COL_TOL:
            return None, "Lệch trục {:.1f} mm".format(off * 304.8)

    if s < MIN_LEN:
        return None, "Conduit quá ngắn sau khi kéo"

    old_len = b.Origin.DistanceTo(fixed)
    cd.Location.Curve = Line.CreateBound(p, fixed) if idx == 0 else Line.CreateBound(fixed, p)
    doc.Regenerate()
    return True, "Stretch {:+.0f} mm".format((s - old_len) * 304.8)


def connect_conduit(t, aid, cd, bid):
    others = [c for c in end_conns(cd) if c.Id != bid]
    other_connected = any(c.IsConnected for c in others)
    res, note = try_stretch_conduit(t, aid, cd, bid)
    if res is None:
        if other_connected:
            raise Exception("Đầu kia đã nối, không kéo thẳng được: " + note)
        return move_and_connect(t, aid, cd, bid)
    check_and_connect(t, aid, cd, bid)
    return note


# ---------------- Main ----------------
def is_movable(e):
    if e.Pinned:
        return False
    if e.GroupId != ElementId.InvalidElementId:
        return False
    return True


def collect_candidates(target_ids):
    cands = []
    for e in FilteredElementCollector(doc, view.Id).OfClass(FamilyInstance) \
                                                   .WhereElementIsNotElementType():
        if eid_int(e.Id) in target_ids or not is_movable(e):
            continue
        if e.SuperComponent is not None:
            continue
        cons = end_conns(e)
        if not cons:
            continue
        free = [c for c in cons if not c.IsConnected]
        if not free:
            continue
        if ONLY_FULLY_FREE and len(free) != len(cons):
            continue
        cands.append(e)

    if INCLUDE_CONDUIT_CANDIDATES:
        for e in FilteredElementCollector(doc, view.Id).OfClass(Conduit):
            if eid_int(e.Id) in target_ids or not is_movable(e):
                continue
            if free_end_conns(e):
                cands.append(e)
    return cands


def build_jobs(targets, cands):
    pairs = []
    for t in targets:
        t_is_cd = isinstance(t, Conduit)
        for a in target_conns(t):
            surf = is_surface(a)
            for e in cands:
                e_is_cd = isinstance(e, Conduit)
                if t_is_cd and e_is_cd and not CONDUIT_TO_CONDUIT:
                    continue
                lp = None if e_is_cd else loc_point(e)
                for b in free_end_conns(e):
                    if b.Domain != a.Domain:
                        continue
                    if CHECK_SIZE and not surf and not size_match(a, b):
                        continue
                    p = target_point(a, b.Origin)
                    d = p.DistanceTo(b.Origin)
                    d_loc = p.DistanceTo(lp) if lp else 1e9
                    if min(d, d_loc) <= R:
                        pairs.append((d, t, a.Id, surf, e, b.Id))

    pairs.sort(key=lambda x: x[0])
    used_a, used_e, jobs = set(), set(), []
    for d, t, aid, surf, e, bid in pairs:
        ka = (eid_int(t.Id), aid)
        ke = eid_int(e.Id)
        if ke in used_e or (not surf and ka in used_a):
            continue
        if not surf:
            used_a.add(ka)
        used_e.add(ke)
        jobs.append((d, t, aid, e, bid))
    return jobs


def main():
    targets = get_targets()
    if not targets:
        forms.alert("Không có đối tượng nào được chọn.", exitscript=True)

    target_ids = set(eid_int(t.Id) for t in targets)
    cands = collect_candidates(target_ids)
    jobs = build_jobs(targets, cands)

    if not jobs:
        forms.alert("Không tìm thấy family/conduit chưa kết nối trong bán kính {} mm."
                    .format(RADIUS_MM), exitscript=True)

    rows = []
    ok_count = 0
    tx = Transaction(doc, "Auto Connect Nearby (Pipe/Conduit/FA)")
    opts = tx.GetFailureHandlingOptions()
    opts.SetFailuresPreprocessor(SwallowWarnings())
    tx.SetFailureHandlingOptions(opts)
    tx.Start()
    try:
        for d, t, aid, e, bid in jobs:
            st = SubTransaction(doc)
            st.Start()
            try:
                if isinstance(e, Conduit):
                    note = connect_conduit(t, aid, e, bid)
                else:
                    note = move_and_connect(t, aid, e, bid)
                st.Commit()
                ok_count += 1
                status = "OK"
            except Exception as ex:
                if st.HasStarted() and not st.HasEnded():
                    st.RollBack()
                status = "SKIP"
                note = str(ex)
            cat = e.Category.Name if e.Category else ""
            rows.append([output.linkify(t.Id), output.linkify(e.Id), cat,
                         elem_name(e), "{:.0f}".format(d * 304.8), status, note])
        tx.Commit()
    except Exception as ex:
        if tx.HasStarted() and not tx.HasEnded():
            tx.RollBack()
        forms.alert("Lỗi: {}".format(ex))
        return

    output.print_md("## Auto Connect – {} / {} cặp đã kết nối".format(ok_count, len(jobs)))
    output.print_table(rows, columns=["Target", "Đối tượng kéo về", "Category", "Type",
                                      "Khoảng cách (mm)", "Kết quả", "Ghi chú / Cách nối"])


main()