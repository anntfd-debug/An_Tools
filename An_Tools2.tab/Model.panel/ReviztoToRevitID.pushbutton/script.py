# -*- coding: utf-8 -*-
"""Revizto -> Revit Element ID
Doc file CSV (IssueID, X_m, Y_m, Z_m, File, Count) tao tu Revizto export,
tim phan tu gan diem issue nhat trong tung file link (hoac host),
xuat CSV de dan vao sheet Revit_Input.
Toa do X/Y/Z la shared coordinates (m) theo cot 'Coordinates (m)' cua Revizto.
"""
__title__ = "Revizto\nTo RevitID"
__author__ = "An Nguyen"

import csv
import io
import os
import math

from Autodesk.Revit.DB import (
    FilteredElementCollector, RevitLinkInstance, XYZ, Outline,
    BoundingBoxIntersectsFilter, CategoryType, BuiltInCategory,
    LocationCurve, ElementId, Transform
)
from pyrevit import forms, script

doc = __revit__.ActiveUIDocument.Document
output = script.get_output()
cfg = script.get_config()

FT = 0.3048
EXCLUDE_BIC = set([
    int(BuiltInCategory.OST_Rooms), int(BuiltInCategory.OST_MEPSpaces),
    int(BuiltInCategory.OST_Areas), int(BuiltInCategory.OST_Levels),
    int(BuiltInCategory.OST_Grids), int(BuiltInCategory.OST_Cameras),
    int(BuiltInCategory.OST_Lines), int(BuiltInCategory.OST_RvtLinks),
    int(BuiltInCategory.OST_SectionBox), int(BuiltInCategory.OST_VolumeOfInterest),
    int(BuiltInCategory.OST_ProjectBasePoint), int(BuiltInCategory.OST_SharedBasePoint),
    int(BuiltInCategory.OST_HVAC_Zones),
])


def eid_int(eid):
    try:
        return eid.Value          # Revit 2024+
    except AttributeError:
        return eid.IntegerValue


def norm(name):
    n = os.path.basename(name or "").strip().lower()
    if n.endswith(".rvt"):
        n = n[:-4]
    return n


# ---------- settings ----------
radius_m = float(getattr(cfg, "radius_m", 0.5))
max_cand = int(getattr(cfg, "max_cand", 5))
skip_insul = bool(getattr(cfg, "skip_insul", True))

in_path = forms.pick_file(file_ext="csv", title="Chon NWCH_VolE_pyRevit_input.csv")
if not in_path:
    script.exit()

val = forms.ask_for_string(default=str(radius_m),
                           prompt="Ban kinh tim quanh diem issue (m):",
                           title="Revizto To RevitID")
if val is None:
    script.exit()
try:
    radius_m = max(0.05, min(5.0, float(val)))
except ValueError:
    forms.alert("Ban kinh khong hop le.", exitscript=True)

skip_insul = forms.alert("Bo qua Pipe/Duct Insulation khi chon phan tu?",
                         yes=True, no=True)
cfg.radius_m = radius_m
cfg.max_cand = max_cand
cfg.skip_insul = bool(skip_insul)
script.save_config()

if skip_insul:
    EXCLUDE_BIC.add(int(BuiltInCategory.OST_PipeInsulations))
    EXCLUDE_BIC.add(int(BuiltInCategory.OST_DuctInsulations))
    EXCLUDE_BIC.add(int(BuiltInCategory.OST_DuctLinings))

# ---------- models: host + links ----------
models = {}   # norm name -> (doc, transform host->model)
host_names = [norm(doc.Title), norm(doc.PathName)]
for n in host_names:
    if n:
        models[n] = (doc, Transform.Identity)
for li in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
    ldoc = li.GetLinkDocument()
    if ldoc is None:
        continue
    inv = li.GetTotalTransform().Inverse
    for n in (norm(ldoc.Title), norm(ldoc.PathName)):
        if n and n not in models:
            models[n] = (ldoc, inv)

# shared -> internal (host)
tf_total = doc.ActiveProjectLocation.GetTotalTransform()
TF_OPTIONS = [("OfPoint", tf_total), ("Inverse", tf_total.Inverse)]

# ---------- read input ----------
with io.open(in_path, "r", encoding="utf-8-sig") as fh:
    rows = list(csv.DictReader(fh))

missing = sorted(set(r["File"] for r in rows if norm(r["File"]) not in models))
if missing:
    msg = "Cac file sau KHONG duoc load (host/link) - se bo qua:\n" + "\n".join(missing)
    if not forms.alert(msg + "\n\nTiep tuc?", yes=True, no=True):
        script.exit()


def model_elements_near(mdoc, p_model, r_ft):
    ol = Outline(XYZ(p_model.X - r_ft, p_model.Y - r_ft, p_model.Z - r_ft),
                 XYZ(p_model.X + r_ft, p_model.Y + r_ft, p_model.Z + r_ft))
    col = FilteredElementCollector(mdoc).WhereElementIsNotElementType() \
        .WherePasses(BoundingBoxIntersectsFilter(ol))
    res = []
    for e in col:
        cat = e.Category
        if cat is None or cat.CategoryType != CategoryType.Model:
            continue
        if eid_int(cat.Id) in EXCLUDE_BIC:
            continue
        bb = e.get_BoundingBox(None)
        if bb is None:
            continue
        # distance point -> bbox
        dx = max(bb.Min.X - p_model.X, 0, p_model.X - bb.Max.X)
        dy = max(bb.Min.Y - p_model.Y, 0, p_model.Y - bb.Max.Y)
        dz = max(bb.Min.Z - p_model.Z, 0, p_model.Z - bb.Max.Z)
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        loc = e.Location
        dc = d
        if isinstance(loc, LocationCurve):
            try:
                dc = loc.Curve.Distance(p_model)
            except Exception:
                pass
        size = bb.Max.DistanceTo(bb.Min)
        res.append((d, dc, size, e))
    # gan nhat bbox, roi gan truc ong, roi phan tu nho hon
    res.sort(key=lambda t: (round(t[0], 4), t[1], t[2]))
    return res


def fam_type(mdoc, e):
    try:
        t = mdoc.GetElement(e.GetTypeId())
        if t is None:
            return e.Name
        fam = getattr(t, "FamilyName", "") or ""
        return (fam + " : " + t.Name) if fam else t.Name
    except Exception:
        return ""


out_rows = []
r_ft = radius_m / FT
n = len(rows)
cancelled = False
with forms.ProgressBar(title="Revizto To RevitID ({value}/{max_value})",
                       cancellable=True) as pb:
    for i, r in enumerate(rows):
        if pb.cancelled:
            cancelled = True
            break
        pb.update_progress(i + 1, n)
        issue, fname, cnt = r["IssueID"], r["File"], int(r["Count"])
        key_base = "{}|{}|".format(issue, fname)
        m = models.get(norm(fname))
        if m is None:
            for k in range(1, cnt + 1):
                out_rows.append([key_base + str(k), issue, fname, k, "", "", "", "", "", "",
                                 "File chua load"])
            continue
        mdoc, to_model = m
        shared = XYZ(float(r["X_m"]) / FT, float(r["Y_m"]) / FT, float(r["Z_m"]) / FT)
        found, used = [], ""
        for tag, tf in TF_OPTIONS:
            p_host = tf.OfPoint(shared)
            found = model_elements_near(mdoc, to_model.OfPoint(p_host), r_ft)
            if found:
                used = tag
                break
        cand_txt = "; ".join("{}:{:.0f}".format(eid_int(t[3].Id), t[0] * FT * 1000)
                             for t in found[:max_cand])
        for k in range(1, cnt + 1):
            if k <= len(found):
                d, dc, size, e = found[k - 1]
                cat = e.Category.Name if e.Category else ""
                out_rows.append([key_base + str(k), issue, fname, k, eid_int(e.Id),
                                 e.UniqueId, cat, fam_type(mdoc, e),
                                 round(d * FT * 1000, 0), cand_txt,
                                 "tf=" + used])
            else:
                out_rows.append([key_base + str(k), issue, fname, k, "", "", "", "", "",
                                 cand_txt, "Khong tim thay trong ban kinh"])

save_path = forms.save_file(file_ext="csv",
                            default_name=os.path.splitext(os.path.basename(in_path))[0] + "_RevitID")
if not save_path:
    script.exit()
# ghi UTF-8 BOM de Excel doc dung
import codecs
with codecs.open(save_path, "w", encoding="utf-8-sig") as fh:
    hdr = ["Key", "IssueID", "File", "Index", "RevitID", "UniqueId", "Category",
           "FamilyType", "Dist_mm", "Candidates", "Note"]
    fh.write(u",".join(hdr) + u"\r\n")
    for row in out_rows:
        cells = []
        for v in row:
            s = u"{}".format(v)
            if any(ch in s for ch in [",", '"', "\n"]):
                s = u'"' + s.replace(u'"', u'""') + u'"'
            cells.append(s)
        fh.write(u",".join(cells) + u"\r\n")

ok = sum(1 for x in out_rows if x[4] != "")
output.print_md("## Revizto To RevitID")
output.print_md("- Dong input: **{}**{}".format(n, " (DA HUY giua chung)" if cancelled else ""))
output.print_md("- Phan tu tim duoc: **{} / {}**".format(ok, len(out_rows)))
output.print_md("- Ban kinh: {} m | Bo qua insulation: {}".format(radius_m, bool(skip_insul)))
if missing:
    output.print_md("- File chua load: " + ", ".join(missing))
output.print_md("- Da luu: `{}`".format(save_path))
