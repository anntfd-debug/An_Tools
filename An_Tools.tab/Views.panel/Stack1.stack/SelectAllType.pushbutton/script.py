# -*- coding: utf-8 -*-
__title__ = "Select All Type"
__doc__ = (
    "Pick one sample element and scan matching elements in Active View. "
    "Shows a cancellable progress bar while scanning. "
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

# Updating the UI for every element can slow down large views.
# Increase this value if the view contains many thousands of elements.
PROGRESS_STEP = 10


def eid_value(eid):
    """Return ElementId as an integer, compatible with Revit 2019-2026."""
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
    """Return all TypeIds belonging to the same loadable Family."""
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
        return (FilteredElementCollector(doc, view_id)
                .OfClass(FamilyInstance)
                .WhereElementIsNotElementType())

    if isinstance(sample_elem, IndependentTag):
        return (FilteredElementCollector(doc, view_id)
                .OfClass(IndependentTag)
                .WhereElementIsNotElementType())

    if isinstance(sample_elem, TextNote):
        return (FilteredElementCollector(doc, view_id)
                .OfClass(TextNote)
                .WhereElementIsNotElementType())

    if isinstance(sample_elem, FilledRegion):
        return (FilteredElementCollector(doc, view_id)
                .OfClass(FilledRegion)
                .WhereElementIsNotElementType())

    # Fallback for other view-specific annotations.
    return FilteredElementCollector(doc, view_id).WhereElementIsNotElementType()


def scan_matching_elements(collector, sample_elem, sample_type_value,
                           select_all_filled_region_types,
                           family_type_values):
    """Scan the collector with a cancellable pyRevit progress bar."""
    selected_ids = []

    try:
        total = collector.GetElementCount()
    except:
        # Compatibility fallback. Normally not reached in Revit 2019-2026.
        total = 1

    if total < 1:
        return selected_ids, False

    cancelled = False
    title = "Scanning Active View... {value} of {max_value}"

    with forms.ProgressBar(
        title=title,
        cancellable=True,
        step=PROGRESS_STEP
    ) as progress_bar:
        for index, elem in enumerate(collector, 1):
            # Check cancellation before processing the next element.
            if progress_bar.cancelled:
                cancelled = True
                break

            try:
                # Filled Region: select every type in Active View.
                if select_all_filled_region_types:
                    selected_ids.append(elem.Id)

                # FamilyInstance / family-based Tag: all types in same Family.
                elif family_type_values is not None:
                    if (same_category(elem, sample_elem)
                            and eid_value(elem.GetTypeId()) in family_type_values):
                        selected_ids.append(elem.Id)

                # TextNote and other non-family annotations:
                # same Category and same Type only.
                elif (same_category(elem, sample_elem)
                      and eid_value(elem.GetTypeId()) == sample_type_value):
                    selected_ids.append(elem.Id)
            except:
                # Skip an invalid/inaccessible element and continue scanning.
                pass

            # Force the last update even when total is not divisible by step.
            if index % PROGRESS_STEP == 0 or index == total:
                progress_bar.update_progress(index, total)

    return selected_ids, cancelled


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

    sample_type_id = sample_elem.GetTypeId()
    sample_type_value = eid_value(sample_type_id)

    # 2. FilledRegion: select all FilledRegion types in Active View.
    select_all_filled_region_types = isinstance(sample_elem, FilledRegion)

    # 3. Loadable families: collect all TypeIds in the selected Family.
    family_type_ids = None
    family_name = None
    if not select_all_filled_region_types:
        family_type_ids, family_name = get_family_symbol_type_ids(sample_elem)

    family_type_values = None
    if family_type_ids:
        family_type_values = set(
            eid_value(type_id) for type_id in family_type_ids
        )

    collector = get_collector_by_sample(sample_elem)

    # 4. Scan with progress bar and Cancel button.
    selected_ids, cancelled = scan_matching_elements(
        collector,
        sample_elem,
        sample_type_value,
        select_all_filled_region_types,
        family_type_values
    )

    # Do not apply a partial selection when the user cancels.
    if cancelled:
        forms.toast(
            "Scanning cancelled. The current selection was not changed.",
            title="pyRevit Cancelled"
        )
        return

    # 5. Apply selection after a complete scan.
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
