# -*- coding: utf-8 -*-
"""
MEP ELEMENTS - EXPORT ALL VALUES OF ONE PARAMETER
=================================================

SUPPORTED CATEGORIES
--------------------
- Pipes
- Pipe Accessories
- Plumbing Fixtures

WORKFLOW
--------
1. Preselect one or more supported elements:
       - Pipes
       - Pipe Accessories
       - Plumbing Fixtures
   Mixed selection is supported.
2. Run tool.
3. Select one Instance or Type parameter.
4. Tool reads ALL values of that parameter in the selected scope.
5. Output:
       - Parameter Value
       - Element ID
       - Category
       - Family
       - Type

SCOPE
-----
If there are supported preselected elements:
    -> only those supported selected elements are processed.

If there is no preselection:
    -> preserve the old behavior:
       collect all Pipe Accessories in Active View.

If there is a preselection but none of the selected elements is supported:
    -> show a small warning and exit.

SUPPORT
-------
- Instance Parameter
- Type Parameter
- String
- Integer
- Double
- ElementId

pyRevit
Revit 2025 / 2026
"""

from pyrevit import revit, DB, forms, script


# ============================================================
# REVIT CONTEXT
# ============================================================

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# ============================================================
# CATEGORY DEFINITIONS
# ============================================================

SUPPORTED_CATEGORIES = (
    DB.BuiltInCategory.OST_PipeCurves,
    DB.BuiltInCategory.OST_PipeAccessory,
    DB.BuiltInCategory.OST_PlumbingFixtures,
)

CATEGORY_NAMES = {
    int(DB.BuiltInCategory.OST_PipeCurves): "Pipes",
    int(DB.BuiltInCategory.OST_PipeAccessory): "Pipe Accessories",
    int(DB.BuiltInCategory.OST_PlumbingFixtures): "Plumbing Fixtures",
}


# ============================================================
# HELPERS
# ============================================================

def eid_int(element_id):
    """ElementId -> integer, compatible with Revit 2025 / 2026."""
    if element_id is None:
        return -1

    try:
        return int(element_id.Value)
    except Exception:
        try:
            return int(element_id.IntegerValue)
        except Exception:
            return -1


def bic_int(bic):
    """BuiltInCategory -> integer."""
    try:
        return int(bic)
    except Exception:
        return -1


def get_category_bic_int(element):
    """Return the integer BuiltInCategory id for an element category."""
    if element is None:
        return None

    try:
        cat = element.Category
    except Exception:
        cat = None

    if cat is None:
        return None

    return eid_int(cat.Id)


def is_supported_element(element):
    """True for Pipes, Pipe Accessories, and Plumbing Fixtures."""
    cat_id = get_category_bic_int(element)

    if cat_id is None:
        return False

    supported_ids = set(
        bic_int(bic)
        for bic in SUPPORTED_CATEGORIES
    )

    return cat_id in supported_ids


def get_category_name(element):
    """Readable category name."""
    cat_id = get_category_bic_int(element)

    if cat_id in CATEGORY_NAMES:
        return CATEGORY_NAMES[cat_id]

    try:
        if element.Category and element.Category.Name:
            return element.Category.Name
    except Exception:
        pass

    return "<Unknown Category>"


def get_type_element(element):
    """Return ElementType."""
    if element is None:
        return None

    try:
        type_id = element.GetTypeId()

        if (
            type_id
            and type_id != DB.ElementId.InvalidElementId
        ):
            return doc.GetElement(type_id)

    except Exception:
        pass

    return None


def get_family_name(element):
    """
    Return Family Name.

    For loadable families such as Plumbing Fixtures / Pipe Accessories,
    this normally returns the actual family name.

    For system family elements such as Pipes, Revit may return a system
    family/type family label. This is expected.
    """
    type_elem = get_type_element(element)

    if type_elem is None:
        return "<Unknown Family>"

    try:
        family_name = type_elem.FamilyName

        if family_name:
            return family_name
    except Exception:
        pass

    try:
        family = type_elem.Family

        if family and family.Name:
            return family.Name
    except Exception:
        pass

    # Useful fallback for system-family types.
    try:
        if type_elem.Category and type_elem.Category.Name:
            return type_elem.Category.Name
    except Exception:
        pass

    return "<Unknown Family>"


def get_type_name(element):
    """Return Type Name."""
    type_elem = get_type_element(element)

    if type_elem is None:
        return "<Unknown Type>"

    try:
        name = type_elem.Name

        if name:
            return name
    except Exception:
        pass

    try:
        p = type_elem.get_Parameter(
            DB.BuiltInParameter.SYMBOL_NAME_PARAM
        )

        if p:
            name = p.AsString()

            if name:
                return name
    except Exception:
        pass

    return "<Unknown Type>"


def parameter_value_to_text(param):
    """Convert Revit Parameter to readable text."""
    if param is None:
        return None

    try:
        if not param.HasValue:
            return None
    except Exception:
        pass

    # Prefer Revit-formatted display text first.
    try:
        value_string = param.AsValueString()

        if value_string is not None:
            value_string = str(value_string).strip()

            if value_string:
                return value_string
    except Exception:
        pass

    # Storage-type fallback.
    try:
        storage_type = param.StorageType

        if storage_type == DB.StorageType.String:
            value = param.AsString()

            if value is None:
                return None

            value = str(value).strip()
            return value if value else None

        elif storage_type == DB.StorageType.Integer:
            return str(param.AsInteger())

        elif storage_type == DB.StorageType.Double:
            return str(param.AsDouble())

        elif storage_type == DB.StorageType.ElementId:
            value_id = param.AsElementId()

            if value_id is None:
                return None

            referenced_element = doc.GetElement(value_id)

            if referenced_element is not None:
                try:
                    name = referenced_element.Name

                    if name:
                        return name
                except Exception:
                    pass

            return str(eid_int(value_id))

    except Exception:
        pass

    return None


def get_parameter(element, parameter_name, source):
    """Get Instance or Type parameter."""
    if source == "INSTANCE":
        target = element
    else:
        target = get_type_element(element)

    if target is None:
        return None

    try:
        return target.LookupParameter(parameter_name)
    except Exception:
        return None


def collect_active_view_pipe_accessories():
    """
    Preserve the original no-preselection behavior:
    collect Pipe Accessories in Active View only.
    """
    try:
        return list(
            DB.FilteredElementCollector(
                doc,
                doc.ActiveView.Id
            )
            .OfCategory(
                DB.BuiltInCategory.OST_PipeAccessory
            )
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        return []


def category_count_text(elements):
    """Return compact category counts for summary."""
    counts = {}

    for element in elements:
        name = get_category_name(element)
        counts[name] = counts.get(name, 0) + 1

    parts = []

    for name in sorted(counts.keys(), key=lambda x: x.lower()):
        parts.append("{}: {}".format(name, counts[name]))

    return " | ".join(parts)


# ============================================================
# GET ELEMENT SCOPE
# ============================================================

selected_ids = list(
    uidoc.Selection.GetElementIds()
)

selected_elements = []

for element_id in selected_ids:
    element = doc.GetElement(element_id)

    if element is not None:
        selected_elements.append(element)


# ------------------------------------------------------------
# PRESELECT MODE
# ------------------------------------------------------------

if selected_ids:
    elements = [
        element
        for element in selected_elements
        if is_supported_element(element)
    ]

    if not elements:
        forms.alert(
            "Selection hiện tại không có đối tượng được hỗ trợ.\n\n"
            "Hãy chọn một hoặc nhiều:\n"
            "- Pipes\n"
            "- Pipe Accessories\n"
            "- Plumbing Fixtures",
            title="Export MEP Parameter"
        )
        script.exit()

    ignored_count = len(selected_elements) - len(elements)

    scope_name = "Preselected supported elements"

    if ignored_count > 0:
        scope_name += " (ignored {} unsupported)".format(
            ignored_count
        )


# ------------------------------------------------------------
# NO PRESELECTION -> PRESERVE OLD ACTIVE VIEW BEHAVIOR
# ------------------------------------------------------------

else:
    elements = collect_active_view_pipe_accessories()
    scope_name = "Active View - Pipe Accessories"


if not elements:
    forms.alert(
        "Không tìm thấy đối tượng trong phạm vi hiện tại.",
        title="Export MEP Parameter"
    )
    script.exit()


# ============================================================
# COLLECT AVAILABLE PARAMETERS
# ============================================================

parameter_options = {}


for element in elements:

    # --------------------------------------------------------
    # INSTANCE PARAMETERS
    # --------------------------------------------------------
    try:
        for param in element.Parameters:
            try:
                parameter_name = param.Definition.Name
            except Exception:
                continue

            if not parameter_name:
                continue

            label = "[Instance] {}".format(
                parameter_name
            )

            parameter_options[label] = {
                "source": "INSTANCE",
                "name": parameter_name
            }

    except Exception:
        pass

    # --------------------------------------------------------
    # TYPE PARAMETERS
    # --------------------------------------------------------
    type_element = get_type_element(element)

    if type_element is None:
        continue

    try:
        for param in type_element.Parameters:
            try:
                parameter_name = param.Definition.Name
            except Exception:
                continue

            if not parameter_name:
                continue

            label = "[Type] {}".format(
                parameter_name
            )

            parameter_options[label] = {
                "source": "TYPE",
                "name": parameter_name
            }

    except Exception:
        pass


# ============================================================
# CHECK PARAMETER LIST
# ============================================================

if not parameter_options:
    forms.alert(
        "Không tìm thấy Parameter trên các đối tượng đã chọn.",
        title="Export MEP Parameter"
    )
    script.exit()


# ============================================================
# SELECT PARAMETER
# ============================================================

parameter_labels = sorted(
    parameter_options.keys(),
    key=lambda x: x.lower()
)


selected_parameter_label = forms.SelectFromList.show(
    parameter_labels,
    title="Chọn Parameter để Export",
    button_name="Xuất tất cả Value",
    multiselect=False,
    width=520,
    height=650
)


if not selected_parameter_label:
    script.exit()


parameter_info = parameter_options[
    selected_parameter_label
]

parameter_source = parameter_info["source"]
parameter_name = parameter_info["name"]


# ============================================================
# COLLECT ALL VALUES
# ============================================================

value_groups = {}
no_value_elements = []


for element in elements:
    param = get_parameter(
        element,
        parameter_name,
        parameter_source
    )

    value_text = parameter_value_to_text(param)

    if value_text is None:
        no_value_elements.append(element)
        continue

    value_text = value_text.strip()

    if not value_text:
        no_value_elements.append(element)
        continue

    if value_text not in value_groups:
        value_groups[value_text] = []

    value_groups[value_text].append(
        element
    )


# ============================================================
# OUTPUT TITLE
# ============================================================

output.set_title(
    "MEP Parameter - {}".format(
        parameter_name
    )
)


# ============================================================
# SUMMARY
# ============================================================

output.print_md(
    "# MEP PARAMETER VALUES"
)

output.print_md(
    "**Scope:** {}  \n"
    "**Parameter:** {}  \n"
    "**Elements:** **{}**  \n"
    "**Categories:** {}  \n"
    "**Unique Values:** **{}**"
    .format(
        scope_name,
        selected_parameter_label,
        len(elements),
        category_count_text(elements),
        len(value_groups)
    )
)

output.print_md("---")


# ============================================================
# SORT VALUES
# ============================================================

sorted_values = sorted(
    value_groups.keys(),
    key=lambda x: x.lower()
)


# ============================================================
# BUILD FULL TABLE
# ============================================================

table_data = []
row_number = 1


for value in sorted_values:
    grouped_elements = value_groups[value]

    grouped_elements = sorted(
        grouped_elements,
        key=lambda x: eid_int(x.Id)
    )

    for element in grouped_elements:
        category_name = get_category_name(
            element
        )

        family_name = get_family_name(
            element
        )

        type_name = get_type_name(
            element
        )

        try:
            id_display = output.linkify(
                element.Id
            )
        except Exception:
            id_display = str(
                eid_int(element.Id)
            )

        table_data.append(
            [
                row_number,
                value,
                id_display,
                category_name,
                family_name,
                type_name
            ]
        )

        row_number += 1


# ============================================================
# PRINT TABLE
# ============================================================

if table_data:
    output.print_table(
        table_data=table_data,
        columns=[
            "#",
            parameter_name,
            "Element ID",
            "Category",
            "Family",
            "Type"
        ],
        title="All Parameter Values"
    )

else:
    output.print_md(
        "Không có đối tượng nào có giá trị cho Parameter này."
    )


# ============================================================
# SUMMARY BY VALUE
# ============================================================

output.print_md("---")

output.print_md(
    "## Summary by Value"
)

summary_table = []


for value in sorted_values:
    grouped_elements = value_groups[value]

    ids = []
    category_counts = {}

    for element in grouped_elements:
        ids.append(
            str(
                eid_int(element.Id)
            )
        )

        category_name = get_category_name(
            element
        )

        category_counts[category_name] = (
            category_counts.get(category_name, 0) + 1
        )

    category_summary = " | ".join(
        "{}: {}".format(
            name,
            category_counts[name]
        )
        for name in sorted(
            category_counts.keys(),
            key=lambda x: x.lower()
        )
    )

    summary_table.append(
        [
            value,
            len(grouped_elements),
            category_summary,
            ", ".join(ids)
        ]
    )


if summary_table:
    output.print_table(
        table_data=summary_table,
        columns=[
            parameter_name,
            "Quantity",
            "Categories",
            "Element IDs"
        ],
        title="Value Summary"
    )


# ============================================================
# NO VALUE
# ============================================================

if no_value_elements:
    output.print_md("---")

    output.print_md(
        "## Không có giá trị"
    )

    no_value_ids = []

    for element in no_value_elements:
        no_value_ids.append(
            "{} [{}]".format(
                eid_int(element.Id),
                get_category_name(element)
            )
        )

    output.print_md(
        "**{} elements** không có giá trị `{}`:"
        .format(
            len(no_value_elements),
            parameter_name
        )
    )

    output.print_md(
        "`{}`".format(
            ", ".join(no_value_ids)
        )
    )
