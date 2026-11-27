# -*- coding: utf-8 -*-
__title__ = "Cloud\nBy ID"
__author__ = "An Nguyen"
__doc__ = """Nhập danh sách Element ID -> tìm các đối tượng hiển thị trong Active View
-> vẽ 1 Revision Cloud bao quanh mỗi ID, đúng vị trí trên mặt bằng / mặt cắt.
Comments của cloud sẽ ghi 'ID: xxxx' để truy vết."""

import re
from System import Int64
from System.Collections.Generic import List
from Autodesk.Revit.DB import (
    ElementId, XYZ, Line, Curve, Revision, RevisionCloud, ViewType,
    FilteredElementCollector, BuiltInParameter, Transaction
)
from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView
output = script.get_output()

MM = 1.0 / 304.8  # mm -> feet


# ---------------------------------------------------------------- helpers
def to_eid(n):
    try:
        return ElementId(Int64(int(n)))      # Revit 2024+
    except Exception:
        return ElementId(int(n))             # Revit cũ


def eid_int(eid):
    try:
        return eid.Value                     # Revit 2024+
    except Exception:
        return eid.IntegerValue


def parse_ids(text):
    seen, result = set(), []
    for s in re.findall(r"\d+", text or ""):
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def get_uv_box(el):
    """Bounding box của element chiếu lên mặt phẳng view -> (umin, vmin, umax, vmax)."""
    bb = el.get_BoundingBox(view) or el.get_BoundingBox(None)
    if bb is None:
        return None
    t = bb.Transform
    org, right, up = view.Origin, view.RightDirection, view.UpDirection
    us, vs = [], []
    for x in (bb.Min.X, bb.Max.X):
        for y in (bb.Min.Y, bb.Max.Y):
            for z in (bb.Min.Z, bb.Max.Z):
                p = t.OfPoint(XYZ(x, y, z))
                d = p - org
                us.append(d.DotProduct(right))
                vs.append(d.DotProduct(up))
    return min(us), min(vs), max(us), max(vs)


def build_rect(box, offset, min_size):
    umin, vmin, umax, vmax = box
    umin -= offset; vmin -= offset; umax += offset; vmax += offset
    # đảm bảo kích thước tối thiểu (cloud quá nhỏ sẽ lỗi)
    if umax - umin < min_size:
        c = (umax + umin) / 2.0
        umin, umax = c - min_size / 2.0, c + min_size / 2.0
    if vmax - vmin < min_size:
        c = (vmax + vmin) / 2.0
        vmin, vmax = c - min_size / 2.0, c + min_size / 2.0

    org, right, up = view.Origin, view.RightDirection, view.UpDirection

    def P(u, v):
        return org + right.Multiply(u) + up.Multiply(v)

    p1, p2, p3, p4 = P(umin, vmin), P(umax, vmin), P(umax, vmax), P(umin, vmax)
    curves = List[Curve]()
    for a, b in ((p1, p2), (p2, p3), (p3, p4), (p4, p1)):
        curves.Add(Line.CreateBound(a, b))
    return curves


def safe_rev_number(r):
    """RevisionNumber lỗi khi project đánh số revision theo sheet (Per Sheet)."""
    try:
        return r.RevisionNumber or "-"
    except Exception:
        return "Per Sheet"


def pick_revision():
    revs = [doc.GetElement(i) for i in Revision.GetAllRevisionIds(doc)]
    open_revs = [r for r in revs if not r.Issued]
    NEW = "<Tạo Revision mới>"
    opts = {}
    for r in open_revs:
        name = "Seq {} | {} | {} | {} (Id {})".format(
            r.SequenceNumber, safe_rev_number(r), r.Description or "-",
            r.RevisionDate or "-", eid_int(r.Id))
        opts[name] = r
    choices = [NEW] + sorted(opts.keys())
    sel = forms.SelectFromList.show(choices, title="Chọn Revision cho Cloud",
                                    multiselect=False, button_name="Chọn")
    if not sel:
        return None, False
    if sel == NEW:
        return None, True
    return opts[sel], False


# ---------------------------------------------------------------- checks
if view.ViewType in (ViewType.ThreeD, ViewType.DrawingSheet, ViewType.Schedule,
                     ViewType.ProjectBrowser, ViewType.SystemBrowser, ViewType.Internal):
    forms.alert("Active view không hỗ trợ Revision Cloud.\n"
                "Hãy mở mặt bằng / mặt cắt / mặt đứng / detail view.", exitscript=True)

text = forms.ask_for_string(
    prompt="Nhập Element ID (cách nhau bằng dấu phẩy, khoảng trắng, ; hoặc xuống dòng):",
    title="Cloud By ID", default="")
id_list = parse_ids(text)
if not id_list:
    forms.alert("Không có ID hợp lệ.", exitscript=True)

off_txt = forms.ask_for_string(prompt="Khoảng offset cloud quanh đối tượng (mm):",
                               title="Cloud By ID", default="300")
try:
    offset_mm = max(0.0, float(off_txt))
except Exception:
    offset_mm = 300.0
offset = offset_mm * MM
min_size = 600 * MM

rev, create_new = pick_revision()
if rev is None and not create_new:
    script.exit()

# Các element thực sự hiển thị trong active view (loại phần bị ẩn / category tắt)
visible_ids = set(eid_int(i) for i in
                  FilteredElementCollector(doc, view.Id)
                  .WhereElementIsNotElementType().ToElementIds())

# ---------------------------------------------------------------- main
rows, created = [], []
found_ids = []

t = Transaction(doc, "Cloud By ID")
t.Start()
try:
    if create_new:
        rev = Revision.Create(doc)
        rev.Description = "Cloud By ID"

    for s in id_list:
        eid = to_eid(s)
        el = doc.GetElement(eid)
        if el is None:
            rows.append([s, "-", "Không tồn tại trong model"])
            continue
        cat = el.Category.Name if el.Category else el.GetType().Name
        if eid_int(eid) not in visible_ids:
            rows.append([output.linkify(eid), cat, "Không hiển thị trong Active View"])
            continue
        box = get_uv_box(el)
        if box is None:
            rows.append([output.linkify(eid), cat, "Không có Bounding Box"])
            continue
        try:
            curves = build_rect(box, offset, min_size)
            cloud = RevisionCloud.Create(doc, view, rev.Id, curves)
            p = cloud.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and not p.IsReadOnly:
                p.Set("ID: {}".format(s))
            created.append(cloud.Id)
            found_ids.append(eid)
            rows.append([output.linkify(eid), cat, "OK - Cloud " + str(eid_int(cloud.Id))])
        except Exception as ex:
            rows.append([output.linkify(eid), cat, "Lỗi tạo cloud: {}".format(ex)])
    t.Commit()
except Exception as ex:
    t.RollBack()
    forms.alert("Lỗi: {}".format(ex), exitscript=True)

# ---------------------------------------------------------------- report
output.print_md("## Cloud By ID - View: **{}**".format(view.Name))
output.print_md("Revision: Seq {} | Tạo: **{}/{}** cloud".format(
    rev.SequenceNumber, len(created), len(id_list)))
output.print_table(rows, columns=["Element ID", "Category", "Kết quả"])

if created:
    sel = List[ElementId](found_ids + created)
    uidoc.Selection.SetElementIds(sel)
