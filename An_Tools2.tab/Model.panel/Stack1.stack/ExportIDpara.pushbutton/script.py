# -*- coding: utf-8 -*-
"""
PIPE ACCESSORIES - EXPORT ALL VALUES OF ONE PARAMETER
=====================================================

WORKFLOW
--------
1. Có thể preselect một hoặc nhiều Pipe Accessories.
2. Run tool.
3. Chọn 1 Parameter.
4. Tool tự đọc TẤT CẢ các value của Parameter đó.
5. Xuất:
       - Parameter Value
       - Element ID
       - Family
       - Type

SCOPE
-----
Nếu có Pipe Accessories được preselect:
    -> chỉ xét các Pipe Accessories đã chọn.

Nếu không preselect:
    -> xét toàn bộ Pipe Accessories trong Active View.

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
# HELPERS
# ============================================================

def eid_int(element_id):
    """ElementId -> integer, compatible with newer Revit versions."""
    try:
        return element_id.Value
    except Exception:
        try:
            return element_id.IntegerValue
        except Exception:
            return -1


def is_pipe_accessory(element):
    """Check Pipe Accessory category."""
    if element is None:
        return False

    cat = element.Category

    if cat is None:
        return False

    try:
        return (
            cat.Id
            == DB.ElementId(
                int(DB.BuiltInCategory.OST_PipeAccessory)
            )
        )
    except Exception:
        return False


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
    """Return Family Name."""

    type_elem = get_type_element(element)

    if type_elem is None:
        return "<Unknown Family>"

    try:
        return type_elem.FamilyName
    except Exception:
        pass

    try:
        return type_elem.Family.Name
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
    """
    Convert Revit Parameter to readable text.
    """

    if param is None:
        return None

    try:
        if not param.HasValue:
            return None
    except Exception:
        pass

    # --------------------------------------------------------
    # Prefer Revit formatted value
    # e.g.:
    #   100 mm
    #   2.00 %
    # --------------------------------------------------------

    try:
        value_string = param.AsValueString()

        if value_string is not None:
            value_string = str(value_string).strip()

            if value_string:
                return value_string

    except Exception:
        pass

    # --------------------------------------------------------
    # Storage type fallback
    # --------------------------------------------------------

    try:
        storage_type = param.StorageType

        # STRING
        if storage_type == DB.StorageType.String:

            value = param.AsString()

            if value is None:
                return None

            value = str(value).strip()

            return value if value else None

        # INTEGER
        elif storage_type == DB.StorageType.Integer:

            return str(param.AsInteger())

        # DOUBLE
        elif storage_type == DB.StorageType.Double:

            return str(param.AsDouble())

        # ELEMENT ID
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
    """
    Get Instance or Type parameter.

    source:
        INSTANCE
        TYPE
    """

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


# ============================================================
# GET PIPE ACCESSORIES
# ============================================================

selected_ids = list(
    uidoc.Selection.GetElementIds()
)

selected_accessories = []

for element_id in selected_ids:

    element = doc.GetElement(element_id)

    if is_pipe_accessory(element):
        selected_accessories.append(element)


# ------------------------------------------------------------
# PRESELECT MODE
# ------------------------------------------------------------

if selected_accessories:

    accessories = selected_accessories

    scope_name = "Preselected Pipe Accessories"


# ------------------------------------------------------------
# ACTIVE VIEW MODE
# ------------------------------------------------------------

else:

    accessories = list(
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

    scope_name = "Active View"


if not accessories:

    forms.alert(
        "Không tìm thấy Pipe Accessories trong phạm vi hiện tại.",
        title="Pipe Accessories Parameter"
    )

    script.exit()


# ============================================================
# COLLECT AVAILABLE PARAMETERS
# ============================================================

parameter_options = {}


for element in accessories:

    # ========================================================
    # INSTANCE PARAMETERS
    # ========================================================

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


    # ========================================================
    # TYPE PARAMETERS
    # ========================================================

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
        "Không tìm thấy Parameter trên Pipe Accessories.",
        title="Pipe Accessories Parameter"
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
    title="Chọn Parameter của Pipe Accessories",
    button_name="Xuất tất cả Value",
    multiselect=False,
    width=500,
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


for element in accessories:

    param = get_parameter(
        element,
        parameter_name,
        parameter_source
    )

    value_text = parameter_value_to_text(param)


    # --------------------------------------------------------
    # NO VALUE
    # --------------------------------------------------------

    if value_text is None:

        no_value_elements.append(element)

        continue


    value_text = value_text.strip()


    if not value_text:

        no_value_elements.append(element)

        continue


    # --------------------------------------------------------
    # GROUP BY VALUE
    # --------------------------------------------------------

    if value_text not in value_groups:

        value_groups[value_text] = []


    value_groups[value_text].append(
        element
    )


# ============================================================
# OUTPUT TITLE
# ============================================================

output.set_title(
    "Pipe Accessories - {}".format(
        parameter_name
    )
)


# ============================================================
# SUMMARY
# ============================================================

output.print_md(
    "# PIPE ACCESSORIES PARAMETER VALUES"
)


output.print_md(
    "**Scope:** {}  \n"
    "**Parameter:** {}  \n"
    "**Pipe Accessories:** **{}**  \n"
    "**Unique Values:** **{}**"
    .format(
        scope_name,
        selected_parameter_label,
        len(accessories),
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

    elements = value_groups[value]

    # Stable sorting by ID
    elements = sorted(
        elements,
        key=lambda x: eid_int(x.Id)
    )

    for element in elements:

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
            "Family",
            "Type"
        ],
        title="All Parameter Values"
    )


else:

    output.print_md(
        "Không có Pipe Accessories nào có giá trị cho Parameter này."
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

    elements = value_groups[value]

    ids = []

    for element in elements:

        ids.append(
            str(
                eid_int(element.Id)
            )
        )


    summary_table.append(
        [
            value,
            len(elements),
            ", ".join(ids)
        ]
    )


if summary_table:

    output.print_table(
        table_data=summary_table,
        columns=[
            parameter_name,
            "Quantity",
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
            str(
                eid_int(element.Id)
            )
        )


    output.print_md(
        "**{} Pipe Accessories** không có giá trị `{}`:"
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