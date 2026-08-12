# -*- coding: utf-8 -*-

from pyrevit import revit, DB, UI, script
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException

doc = revit.doc
uidoc = revit.uidoc
out = script.get_output()


class LinkSelectionFilter(ISelectionFilter):
    """Chi cho phep pick element trong Revit Link."""
    def AllowElement(self, elem):
        return isinstance(elem, DB.RevitLinkInstance)

    def AllowReference(self, reference, point):
        return True


def get_link_type_name(link_inst):
    """Lay ten type cua RevitLinkInstance."""
    try:
        link_type = link_inst.Document.GetElement(link_inst.GetTypeId())
        if link_type:
            try:
                return link_type.Name
            except:
                pass

            p = link_type.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
            if p:
                return p.AsString()
    except:
        pass

    return ""


def get_doc_title_from_link(link_inst):
    """Lay ten document link neu link document load duoc."""
    try:
        link_doc = link_inst.GetLinkDocument()
        if link_doc:
            return link_doc.Title
    except:
        pass

    return ""


try:
    ref = uidoc.Selection.PickObject(
        ObjectType.LinkedElement,
        LinkSelectionFilter(),
        "Pick one element inside linked / nested linked model"
    )

except OperationCanceledException:
    script.exit()


# Link cap 1 nam trong host model
host_link_inst = doc.GetElement(ref.ElementId)

if not isinstance(host_link_inst, DB.RevitLinkInstance):
    UI.TaskDialog.Show("Result", "Selected object is not inside a Revit Link.")
    script.exit()

parent_link_doc = host_link_inst.GetLinkDocument()

if parent_link_doc is None:
    UI.TaskDialog.Show(
        "Result",
        "Cannot read parent link document. Link may be unloaded."
    )
    script.exit()

linked_elem_id = ref.LinkedElementId

if linked_elem_id == DB.ElementId.InvalidElementId:
    UI.TaskDialog.Show(
        "Result",
        "Cannot get LinkedElementId from selected object."
    )
    script.exit()

# Element top-level trong parent link document
linked_elem = parent_link_doc.GetElement(linked_elem_id)

parent_link_name = host_link_inst.Name
parent_link_type = get_link_type_name(host_link_inst)
parent_doc_title = get_doc_title_from_link(host_link_inst)

# Neu element top-level nay la RevitLinkInstance => day la nested link
if isinstance(linked_elem, DB.RevitLinkInstance):
    nested_link_inst = linked_elem
    nested_link_name = nested_link_inst.Name
    nested_link_type = get_link_type_name(nested_link_inst)

    msg = []
    msg.append("NESTED LINK FOUND")
    msg.append("")
    msg.append("Parent link instance:")
    msg.append("  {}".format(parent_link_name))
    msg.append("")
    msg.append("Parent link type / file:")
    msg.append("  {}".format(parent_link_type or parent_doc_title))
    msg.append("")
    msg.append("Nested link instance:")
    msg.append("  {}".format(nested_link_name))
    msg.append("")
    msg.append("Nested link type / file:")
    msg.append("  {}".format(nested_link_type))
    msg.append("")
    msg.append("Nested link ElementId in parent link:")
    msg.append("  {}".format(nested_link_inst.Id.IntegerValue))

    result = "\n".join(msg)

else:
    # Truong hop pick element nam truc tiep trong link cap 1, khong phai nested link
    cate_name = ""
    try:
        cate_name = linked_elem.Category.Name if linked_elem.Category else ""
    except:
        pass

    msg = []
    msg.append("NOT A NESTED LINK")
    msg.append("")
    msg.append("Selected element is directly inside parent link.")
    msg.append("")
    msg.append("Parent link instance:")
    msg.append("  {}".format(parent_link_name))
    msg.append("")
    msg.append("Parent link type / file:")
    msg.append("  {}".format(parent_link_type or parent_doc_title))
    msg.append("")
    msg.append("Selected linked element Id:")
    msg.append("  {}".format(linked_elem.Id.IntegerValue if linked_elem else "None"))
    msg.append("")
    msg.append("Selected linked element category:")
    msg.append("  {}".format(cate_name))

    result = "\n".join(msg)

# Hien thi dialog ngan gon
UI.TaskDialog.Show("Nested Link Result", result)