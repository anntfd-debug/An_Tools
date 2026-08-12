# -*- coding: utf-8 -*-
__title__ = "Select All Type"
__doc__ = (
    "Pick one sample element and select matching elements in Active View. "
    "FamilyInstance and family-based Tags: select all Types in the same Family. "
    "Filled Region: select all Filled Region Types in Active View. "
    "Text and other annotations: select the same Type."
)

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ObjectType
from System.Collections.Generic import List
from pyrevit import forms, revit

uidoc = revit.uidoc
doc = revit.doc
view = revit.active_view


def eid_value(eid):
    """Return ElementId as an integer, compatible with multiple Revit versions."""
    if eid is None:
        return -1

    try:
        return eid.IntegerValue
    except:
        try:
            return eid.Value
        except:
            return -1


def get_family_symbol_type_ids(elem):
    """
    Return all TypeIds belonging to the same loadable Family.

    Supported examples:
    - FamilyInstance: Generic Annotation, Detail Item, Door, Equipment...
    - IndependentTag whose type is a FamilySymbol.
    """
    try:
        if isinstance(elem, FamilyInstance):
            symbol = elem.Symbol
            if symbol is not None and symbol.Family is not None:
                family = symbol.Family
                return list(family.GetFamilySymbolIds()), family.Name
    except:
        pass

    try:
        elem_type = doc.GetElement(elem.GetTypeId())
        if isinstance(elem_type, FamilySymbol):
            family = elem_type.Family
            if family is not None:
                return list(family.GetFamilySymbolIds()), family.Name
    except:
        pass

    return None, None


def same_category(elem_a, elem_b):
    """Check whether two elements belong to the same Revit category."""
    try:
        if elem_a.Category is not None and elem_b.Category is not None:
            return eid_value(elem_a.Category.Id) == eid_value(elem_b.Category.Id)
    except:
        pass

    return False


def get_collector_by_sample(sample_elem):
    """Collect only element instances available in the active view."""
    view_id = view.Id

    if isinstance(sample_elem, FamilyInstance):
        return (
            FilteredElementCollector(doc, view_id)
            .OfClass(FamilyInstance)
            .WhereElementIsNotElementType()
        )

    if isinstance(sample_elem, IndependentTag):
        return (
            FilteredElementCollector(doc, view_id)
            .OfClass(IndependentTag)
            .WhereElementIsNotElementType()
        )

    if isinstance(sample_elem, TextNote):
        return (
            FilteredElementCollector(doc, view_id)
            .OfClass(TextNote)
            .WhereElementIsNotElementType()
        )

    if isinstance(sample_elem, FilledRegion):
        return (
            FilteredElementCollector(doc, view_id)
            .OfClass(FilledRegion)
            .WhereElementIsNotElementType()
        )

    # Fallback for other view-specific annotations:
    # Detail Line, Dimension, Revision Cloud, etc.
    return FilteredElementCollector(doc, view_id).WhereElementIsNotElementType()


def main():
    # 1. Pick a sample element.
    try:
        picked_ref = uidoc.Selection.PickObject(
            ObjectType.Element,
            "Pick one sample element in Active View..."
        )
        sample_elem = doc.GetElement(picked_ref.ElementId)
    except:
        return

    if sample_elem is None:
        return

    selected_ids = []
    sample_type_id = sample_elem.GetTypeId()
    sample_type_value = eid_value(sample_type_id)

    # 2. FilledRegion is a system family and has no loadable Family object.
    # Therefore, when a FilledRegion is picked, select every FilledRegion
    # instance of every FilledRegionType in the active view.
    select_all_filled_region_types = isinstance(sample_elem, FilledRegion)

    # 3. Loadable family elements: collect all TypeIds in the same Family.
    family_type_ids = None
    family_name = None

    if not select_all_filled_region_types:
        family_type_ids, family_name = get_family_symbol_type_ids(sample_elem)

    family_type_values = None
    if family_type_ids:
        family_type_values = set(eid_value(type_id) for type_id in family_type_ids)

    collector = get_collector_by_sample(sample_elem)

    # 4. Filter matching instances in Active View.
    for elem in collector:
        try:
            # Filled Region: all types in Active View.
            if select_all_filled_region_types:
                selected_ids.append(elem.Id)
                continue

            # FamilyInstance / family-based Tag: all Types in same Family.
            if family_type_values is not None:
                if (
                    same_category(elem, sample_elem)
                    and eid_value(elem.GetTypeId()) in family_type_values
                ):
                    selected_ids.append(elem.Id)
                continue

            # TextNote and other non-family annotation:
            # same Category and same Type only.
            if (
                same_category(elem, sample_elem)
                and eid_value(elem.GetTypeId()) == sample_type_value
            ):
                selected_ids.append(elem.Id)

        except:
            pass

    # 5. Apply selection.
    if not selected_ids:
        forms.alert(
            "No matching elements found in Active View.",
            title="Notice"
        )
        return

    uidoc.Selection.SetElementIds(List[ElementId](selected_ids))

    if select_all_filled_region_types:
        message = (
            "Selected {} Filled Region elements - all Types in Active View."
            .format(len(selected_ids))
        )
    elif family_name:
        message = (
            "Selected {} elements in Active View - Family: {}"
            .format(len(selected_ids), family_name)
        )
    else:
        try:
            sample_type = doc.GetElement(sample_type_id)
            type_name = sample_type.Name if sample_type is not None else "Same Type"
        except:
            type_name = "Same Type"

        message = (
            "Selected {} elements in Active View - Type: {}"
            .format(len(selected_ids), type_name)
        )

    forms.toast(message, title="pyRevit Success")


if __name__ == "__main__":
    main()
