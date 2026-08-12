# -*- coding: utf-8 -*-
from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# -----------------------------
# Selection filter: chỉ chọn Line
# -----------------------------
class LineSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        # Detail Line / Model Line đều là CurveElement
        return isinstance(elem, DB.CurveElement)

    def AllowReference(self, reference, position):
        return False


# -----------------------------
# Convert đơn vị
# Revit internal unit = feet
# -----------------------------
def feet_to_mm(value_ft):
    return value_ft * 304.8


def feet_to_m(value_ft):
    return value_ft * 0.3048


# -----------------------------
# Lấy line đang chọn trước
# Nếu chưa chọn thì yêu cầu chọn
# -----------------------------
selected_ids = list(uidoc.Selection.GetElementIds())

line_elements = []

for eid in selected_ids:
    elem = doc.GetElement(eid)
    if isinstance(elem, DB.CurveElement):
        line_elements.append(elem)


# Nếu chưa có line được chọn sẵn thì cho pick nhiều line
if not line_elements:
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            LineSelectionFilter(),
            "Select detail lines / model lines to calculate total length"
        )

        for r in refs:
            elem = doc.GetElement(r.ElementId)
            if isinstance(elem, DB.CurveElement):
                line_elements.append(elem)

    except:
        forms.alert("Cancelled.", exitscript=True)


# -----------------------------
# Tính tổng chiều dài
# -----------------------------
total_length_ft = 0.0
valid_count = 0

for elem in line_elements:
    try:
        curve = elem.GeometryCurve
        if curve:
            total_length_ft += curve.Length
            valid_count += 1
    except:
        pass


# -----------------------------
# Kết quả
# -----------------------------
total_length_mm = feet_to_mm(total_length_ft)
total_length_m = feet_to_m(total_length_ft)

msg = """
Number of selected lines: {0}

Total length:
- {1:.2f} mm
- {2:.3f} m
""".format(valid_count, total_length_mm, total_length_m)

forms.alert(msg, title="Total Line Length")
