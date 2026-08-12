# -*- coding: utf-8 -*-

from pyrevit import forms, script
from Autodesk.Revit.DB import (
    FilteredElementCollector,
    View,
    Transaction,
    ElementId
)

uidoc = __revit__.ActiveUIDocument

if uidoc is None:
    forms.alert("Không có document nào đang mở.", exitscript=True)

doc = uidoc.Document
active_view = doc.ActiveView

if active_view is None:
    forms.alert("Không tìm thấy Active View.", exitscript=True)

if active_view.IsTemplate:
    forms.alert("Active View hiện tại là View Template.", exitscript=True)


def get_id_int(element_id):
    """Lấy integer id, hỗ trợ nhiều version Revit."""
    try:
        return element_id.IntegerValue
    except:
        try:
            return element_id.Value
        except:
            return -999999


class TemplateOption(object):
    def __init__(self, display_name, real_name, element_id, is_current=False):
        self.real_name = real_name
        self.element_id = element_id
        self.is_current = is_current

        # pyRevit SelectFromList hiển thị thuộc tính .name
        # Dùng Unicode bold để giả lập in đậm chữ CURRENT
        if is_current:
            self.name = u"✅  𝗖𝗨𝗥𝗥𝗘𝗡𝗧  -  " + display_name
        else:
            self.name = display_name

    def __str__(self):
        return self.name


# View Template hiện hành
current_template_id = active_view.ViewTemplateId
current_template_id_int = get_id_int(current_template_id)

current_template_name = "<None>"

if current_template_id_int != -1:
    current_template = doc.GetElement(current_template_id)
    if current_template:
        current_template_name = current_template.Name


# Lấy tất cả View Template cùng ViewType với Active View
all_views = FilteredElementCollector(doc).OfClass(View).ToElements()

view_templates = []

for v in all_views:
    if v.IsTemplate and v.ViewType == active_view.ViewType:
        view_templates.append(v)

view_templates = sorted(view_templates, key=lambda x: x.Name)


options = []

# Option <None>
none_is_current = current_template_id_int == -1

options.append(
    TemplateOption(
        "<None> - Tắt View Template",
        "<None>",
        ElementId.InvalidElementId,
        none_is_current
    )
)

# Các View Template
for vt in view_templates:
    vt_id_int = get_id_int(vt.Id)
    is_current = vt_id_int == current_template_id_int

    options.append(
        TemplateOption(
            vt.Name,
            vt.Name,
            vt.Id,
            is_current
        )
    )


selected = forms.SelectFromList.show(
    options,
    title="View Template hiện hành: {}".format(current_template_name),
    button_name="Apply",
    multiselect=False,
    name_attr="name"
)

# Bấm Cancel thì thoát im lặng
if selected is None:
    script.exit()


try:
    t = Transaction(doc, "Apply View Template to Active View")
    t.Start()

    active_view.ViewTemplateId = selected.element_id

    t.Commit()

except Exception as e:
    try:
        if t.HasStarted():
            t.RollBack()
    except:
        pass

    forms.alert("Lỗi khi apply View Template:\n{}".format(str(e)))