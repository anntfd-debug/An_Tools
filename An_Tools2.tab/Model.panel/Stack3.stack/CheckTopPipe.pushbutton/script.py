# -*- coding: utf-8 -*-
__title__ = "Pipe vs\nSlab/Beam"
__doc__ = (u"Quét tất cả ống ngang (có/không dốc) trong active view, liệt kê ống có khoảng cách "
           u"từ đỉnh ống đến đáy Floor/Beam của link < giá trị nhập (mm). "
           u"Click ID trong bảng kết quả để zoom tới ống. Shift+Click = đổi file link.")

import math
from System.Collections.Generic import List
from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, BoundingBoxIntersectsFilter, ElementMulticategoryFilter,
    ElementId, FamilyInstance, FilteredElementCollector, GeometryInstance, Line, Options, Outline,
    RevitLinkInstance, Solid, SolidCurveIntersectionOptions, SolidUtils, ViewDetailLevel,
    ViewSchedule, ViewSheet, XYZ)
from Autodesk.Revit.DB.Plumbing import Pipe, FlexPipe
from Autodesk.Revit.DB.Structure import StructuralType
from pyrevit import forms, script

# ------------------------------------------------------------------ SETTINGS
DEFAULT_THRESHOLD_MM = 300.0  # giá trị mặc định lần chạy đầu tiên
MAX_ANGLE_DEG = 45.0          # ống dốc quá góc này (so với phương ngang) coi là ống đứng -> bỏ qua
TOP_MODE = "highest"          # "highest": dùng đỉnh CAO NHẤT của ống cho mọi vị trí dọc ống
                              # "local"  : dùng đỉnh ống tại từng vị trí
LATERAL_RAYS = True           # True: bắn thêm 2 tia ở mép trái/phải ống (bắt dầm sát bên hông ống)
SAMPLE_STEP_MM = 150.0        # khoảng cách giữa các điểm lấy mẫu dọc ống
MAX_SAMPLES = 80              # tối đa số điểm lấy mẫu / 1 ống
INCLUDE_INSULATION = False    # True: đỉnh ống = đỉnh lớp bảo ôn
EXCLUDE_BRACES = True         # bỏ qua Structural Framing loại Brace (chỉ lấy dầm)
FT_TO_MM = 304.8
# ---------------------------------------------------------------------------

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document
cfg = script.get_config()
output = script.get_output()

try:
    SHIFT = bool(__shiftclick__)
except NameError:
    SHIFT = False

STEP_FT = SAMPLE_STEP_MM / FT_TO_MM
PAD = 0.02  # ~6 mm


# ------------------------------------------------------------ LINK SELECTION
def get_loaded_links():
    return [li for li in FilteredElementCollector(doc).OfClass(RevitLinkInstance)
            if li.GetLinkDocument() is not None]


def find_saved_link():
    uid = cfg.get_option('link_uid', '')
    name = cfg.get_option('link_name', '')
    links = get_loaded_links()
    if uid:
        for li in links:
            if li.UniqueId == uid:
                return li
    if name:
        for li in links:
            if li.Name == name:
                return li
    return None


def choose_link():
    links = get_loaded_links()
    if not links:
        forms.alert(u"Không có link Revit nào đang load trong project.", exitscript=True)
    names = {}
    for li in links:
        names[li.Name] = li
    picked = forms.SelectFromList.show(
        sorted(names.keys()), title=u"Chọn link chứa Floor & Beam",
        button_name=u"Chọn", multiselect=False)
    if not picked:
        script.exit()
    li = names[picked]
    cfg.link_uid = li.UniqueId
    try:
        cfg.link_name = li.Name
    except Exception:
        pass
    script.save_config()
    return li


# --------------------------------------------------------------- THRESHOLD
def ask_threshold():
    try:
        last = float(cfg.get_option('threshold_mm', DEFAULT_THRESHOLD_MM))
    except Exception:
        last = DEFAULT_THRESHOLD_MM
    default = u"{:g}".format(last)
    while True:
        s = forms.ask_for_string(
            default=default,
            prompt=u"Liệt kê ống có khoảng cách (đỉnh ống → đáy sàn/dầm) NHỎ HƠN (mm):",
            title=u"Ngưỡng khoảng cách")
        if s is None:
            script.exit()
        try:
            v = float(s.strip().replace(',', '.'))
            if v <= 0:
                raise ValueError()
        except Exception:
            forms.alert(u"Giá trị không hợp lệ. Nhập một số dương (mm).")
            default = s
            continue
        cfg.threshold_mm = v
        script.save_config()
        return v


# ------------------------------------------------------------------ PIPE
def pipe_radius(pipe):
    p = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_OUTER_DIAMETER)
    if p is None or not p.HasValue or p.AsDouble() <= 0:
        p = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    d = p.AsDouble() if (p is not None and p.HasValue) else 0.0
    r = d / 2.0
    if INCLUDE_INSULATION:
        ip = pipe.get_Parameter(BuiltInParameter.RBS_REFERENCE_INSULATION_THICKNESS)
        if ip is not None and ip.HasValue:
            r += ip.AsDouble()
    return r


def build_pipe_info(pipe):
    """Trả về dict thông tin ống ngang, hoặc None nếu ống đứng / không có curve."""
    loc = pipe.Location
    if loc is None or not hasattr(loc, 'Curve'):
        return None
    try:
        pts = list(loc.Curve.Tessellate())
    except Exception:
        return None
    if len(pts) < 2:
        return None

    a, b = pts[0], pts[-1]
    plan = math.hypot(b.X - a.X, b.Y - a.Y)
    dz = abs(b.Z - a.Z)
    if plan < 1e-6 or math.degrees(math.atan2(dz, plan)) > MAX_ANGLE_DEG:
        return None

    r = pipe_radius(pipe)
    segs = []
    total = 0.0
    for p0, p1 in zip(pts[:-1], pts[1:]):
        dx, dy, dzz = p1.X - p0.X, p1.Y - p0.Y, p1.Z - p0.Z
        L = math.hypot(dx, dy)
        if L < 1e-6:
            continue
        segs.append((p0, L, dx, dy, dzz))
        total += L
    if not segs:
        return None

    step = max(STEP_FT, total / float(MAX_SAMPLES))
    samples = []   # (x, y, nx, ny, local_top_z)
    first = True
    for p0, L, dx, dy, dzz in segs:
        n = max(1, int(math.ceil(L / step)))
        cos_s = L / math.sqrt(L * L + dzz * dzz)
        nx, ny = -dy / L, dx / L
        for k in range(0 if first else 1, n + 1):
            t = float(k) / n
            samples.append((p0.X + dx * t, p0.Y + dy * t, nx, ny,
                            p0.Z + dzz * t + r / cos_s))
        first = False

    tops = [s[4] for s in samples]
    xs = [s[0] for s in samples]
    ys = [s[1] for s in samples]
    return {
        'pipe': pipe, 'r': r, 'samples': samples,
        'top_ref': max(tops), 'top_min': min(tops),
        'x0': min(xs) - r - PAD, 'x1': max(xs) + r + PAD,
        'y0': min(ys) - r - PAD, 'y1': max(ys) + r + PAD,
    }


# -------------------------------------------------------------- LINK GEOMETRY
def collect_solids(geom, out):
    for g in geom:
        if isinstance(g, Solid):
            if g.Volume > 1e-9 and g.Faces.Size > 0:
                out.append(g)
        elif isinstance(g, GeometryInstance):
            collect_solids(g.GetInstanceGeometry(), out)


def host_bbox(el, tf):
    bb = el.get_BoundingBox(None)
    if bb is None:
        return None
    mn, mx = bb.Min, bb.Max
    xs, ys, zs = [], [], []
    for x in (mn.X, mx.X):
        for y in (mn.Y, mx.Y):
            for z in (mn.Z, mx.Z):
                p = tf.OfPoint(XYZ(x, y, z))
                xs.append(p.X)
                ys.append(p.Y)
                zs.append(p.Z)
    return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)


_GOPT = Options()
_GOPT.DetailLevel = ViewDetailLevel.Fine
_GOPT.ComputeReferences = False


def get_solids(entry, tf):
    """entry = [el, x0,y0,z0,x1,y1,z1, solids_cache]  (solid đã transform sang toạ độ host)."""
    if entry[7] is None:
        sol = []
        try:
            geom = entry[0].get_Geometry(_GOPT)
            raw = []
            if geom is not None:
                collect_solids(geom, raw)
            for s in raw:
                try:
                    sol.append(SolidUtils.CreateTransformed(s, tf))
                except Exception:
                    pass
        except Exception:
            pass
        entry[7] = sol
    return entry[7]


def build_link_index(link_inst, infos, thr_ft):
    ldoc = link_inst.GetLinkDocument()
    tf = link_inst.GetTotalTransform()
    inv = tf.Inverse

    gx0 = min(i['x0'] for i in infos)
    gy0 = min(i['y0'] for i in infos)
    gz0 = min(i['top_min'] for i in infos) - 0.1
    gx1 = max(i['x1'] for i in infos)
    gy1 = max(i['y1'] for i in infos)
    gz1 = max(i['top_ref'] for i in infos) + thr_ft + 0.1

    xs, ys, zs = [], [], []
    for x in (gx0, gx1):
        for y in (gy0, gy1):
            for z in (gz0, gz1):
                p = inv.OfPoint(XYZ(x, y, z))
                xs.append(p.X)
                ys.append(p.Y)
                zs.append(p.Z)
    outline = Outline(XYZ(min(xs), min(ys), min(zs)), XYZ(max(xs), max(ys), max(zs)))

    cats = List[BuiltInCategory]([BuiltInCategory.OST_Floors,
                                  BuiltInCategory.OST_StructuralFraming])
    elems = (FilteredElementCollector(ldoc)
             .WherePasses(ElementMulticategoryFilter(cats))
             .WherePasses(BoundingBoxIntersectsFilter(outline))
             .WhereElementIsNotElementType()
             .ToElements())

    index = []
    for el in elems:
        if EXCLUDE_BRACES and isinstance(el, FamilyInstance):
            try:
                if el.StructuralType == StructuralType.Brace:
                    continue
            except Exception:
                pass
        bb = host_bbox(el, tf)
        if bb is None:
            continue
        index.append([el, bb[0], bb[1], bb[2], bb[3], bb[4], bb[5], None])
    return index, tf


# ------------------------------------------------------------------ ANALYSIS
_IOPT = SolidCurveIntersectionOptions()


def analyze(info, index, tf, thr_ft):
    """Trả về (clearance_ft, top_z, underside_z, element) nhỏ nhất < thr_ft, hoặc None."""
    z0 = info['top_min']
    z1 = info['top_ref'] + thr_ft
    cands = [e for e in index
             if e[4] >= info['x0'] and e[1] <= info['x1'] and
             e[5] >= info['y0'] and e[2] <= info['y1'] and
             e[6] >= z0 and e[3] <= z1]
    if not cands:
        return None

    r = info['r']
    offs = [0.0, r, -r] if (LATERAL_RAYS and r > 1e-6) else [0.0]
    best = None
    for (x, y, nx, ny, ltop) in info['samples']:
        start = info['top_ref'] if TOP_MODE == "highest" else ltop
        end = start + thr_ft
        for off in offs:
            px, py = x + nx * off, y + ny * off
            near = [e for e in cands
                    if e[1] - PAD <= px <= e[4] + PAD and e[2] - PAD <= py <= e[5] + PAD
                    and e[6] >= start and e[3] <= end]
            if not near:
                continue
            ray = Line.CreateBound(XYZ(px, py, start), XYZ(px, py, end))
            for e in near:
                for s in get_solids(e, tf):
                    try:
                        res = s.IntersectWithCurve(ray, _IOPT)
                    except Exception:
                        continue
                    if res is None:
                        continue
                    for i in range(res.SegmentCount):
                        c = res.GetCurveSegment(i)
                        z = min(c.GetEndPoint(0).Z, c.GetEndPoint(1).Z)
                        clr = max(0.0, z - start)
                        if best is None or clr < best[0]:
                            best = (clr, start, z, e[0])
    if best is not None and best[0] < thr_ft - 1e-9:
        return best
    return None


# ---------------------------------------------------------------------- MAIN
view = doc.ActiveView
if isinstance(view, (ViewSheet, ViewSchedule)):
    forms.alert(u"Hãy mở một view mặt bằng / mặt cắt / 3D chứa ống rồi chạy lại.", exitscript=True)

link = None if SHIFT else find_saved_link()
if link is None:
    link = choose_link()

thr_mm = ask_threshold()
thr_ft = thr_mm / FT_TO_MM

# 1) Gom ống trong active view
raw = list(FilteredElementCollector(doc, view.Id).OfClass(Pipe).WhereElementIsNotElementType())
try:
    raw += list(FilteredElementCollector(doc, view.Id).OfClass(FlexPipe).WhereElementIsNotElementType())
except Exception:
    pass

infos = []
skipped_vertical = 0
for p in raw:
    try:
        inf = build_pipe_info(p)
    except Exception:
        inf = None
    if inf is None:
        skipped_vertical += 1
    else:
        infos.append(inf)

if not infos:
    forms.alert(u"Không tìm thấy ống ngang nào trong view hiện tại.", exitscript=True)

# 2) Index Floor/Beam của link (chỉ phần nằm trong vùng ống + ngưỡng)
index, tf = build_link_index(link, infos, thr_ft)

# 3) Quét có progress bar + cancel
results = []
cancelled = False
done = 0
total = len(infos)
with forms.ProgressBar(title=u"Đang quét ống... ({value}/{max_value})",
                       cancellable=True) as pb:
    for i, inf in enumerate(infos):
        if pb.cancelled:
            cancelled = True
            break
        try:
            hit = analyze(inf, index, tf, thr_ft)
        except Exception:
            hit = None
        if hit is not None:
            results.append((hit[0], inf, hit))
        done = i + 1
        if i % 3 == 0 or done == total:
            pb.update_progress(done, total)

# 4) Xuất kết quả
results.sort(key=lambda r: r[0])

if not results:
    forms.alert(u"{}Không có ống nào có khoảng cách < {:g} mm.\n"
                u"Đã quét {}/{} ống ngang (bỏ qua {} ống đứng/không hợp lệ).".format(
                    u"(ĐÃ HỦY - kết quả một phần)\n" if cancelled else u"",
                    thr_mm, done, total, skipped_vertical),
                title=u"Pipe vs Slab/Beam", warn_icon=False)
    script.exit()

output.set_title(u"Pipe vs Slab/Beam")
output.print_md(u"## Ống có khoảng cách đỉnh ống → đáy sàn/dầm < {:g} mm".format(thr_mm))
output.print_md(
    u"- **View:** {}  \n- **Link:** {}  \n- **Đã quét:** {}/{} ống ngang "
    u"(bỏ qua {} ống đứng/không hợp lệ)  \n- **Chế độ đỉnh ống:** {}  \n- **Số ống vi phạm:** {}".format(
        view.Name, link.Name, done, total, skipped_vertical,
        u"đỉnh cao nhất của ống" if TOP_MODE == "highest" else u"đỉnh tại từng vị trí",
        len(results)))
if cancelled:
    output.print_md(u"> ⚠ **Đã hủy giữa chừng** - đây chỉ là kết quả một phần.")

ids = List[ElementId]()
for r in results:
    ids.Add(r[1]['pipe'].Id)
output.print_md(u"Click ID để zoom tới ống: " + output.linkify(ids, u"Chọn/zoom tất cả {} ống".format(len(results))))

rows = []
for n, (clr, inf, hit) in enumerate(results, 1):
    p = inf['pipe']
    dp = p.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    size = u"{:.0f}".format(dp.AsDouble() * FT_TO_MM) if (dp is not None and dp.HasValue) else u"-"
    sp = p.get_Parameter(BuiltInParameter.RBS_SYSTEM_NAME_PARAM)
    sysname = sp.AsString() if (sp is not None and sp.HasValue) else u"-"
    el = hit[3]
    kind = el.Category.Name if el.Category else u"Element"
    clr_mm = clr * FT_TO_MM
    rows.append([
        n,
        output.linkify(p.Id, p.Id.ToString()),
        size,
        sysname or u"-",
        u"{:,.0f}".format(hit[1] * FT_TO_MM),
        u"{:,.0f}".format(hit[2] * FT_TO_MM),
        (u"{:,.0f}".format(clr_mm) if clr_mm >= 1.0 else u"0 (chạm/xuyên)"),
        u"{} (Id {})".format(kind, el.Id.ToString()),
    ])

output.print_table(
    table_data=rows,
    columns=[u"#", u"Pipe ID (zoom)", u"Ø (mm)", u"System", u"Đỉnh ống Z (mm)",
             u"Đáy sàn/dầm Z (mm)", u"Khoảng cách (mm)", u"Vật cản (trong link)"])
