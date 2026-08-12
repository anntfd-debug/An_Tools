# -*- coding: utf-8 -*-
__title__ = "Show\nElement IDs"
__doc__ = """
Xuất ID các đối tượng đang được chọn ra pyRevit Output.

- Nhấn vào từng Element ID để select và zoom tới đối tượng.
- Có liên kết zoom tới toàn bộ đối tượng đã chọn.
"""

from pyrevit import revit, DB, forms, script


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


def safe_text(value, default="-"):
    """Chuyển giá trị thành chuỗi an toàn."""
    try:
        if value is None:
            return default

        text = str(value).strip()
        return text if text else default

    except Exception:
        return default


def get_category_name(element):
    """Lấy tên Category của đối tượng."""
    try:
        if element.Category:
            return safe_text(element.Category.Name)
    except Exception:
        pass

    return "-"


def get_family_and_type(element):
    """Lấy Family Name và Type Name của đối tượng."""
    family_name = "-"
    type_name = "-"

    try:
        type_id = element.GetTypeId()

        if type_id and type_id != DB.ElementId.InvalidElementId:
            element_type = doc.GetElement(type_id)

            if element_type:
                # Tên Type
                try:
                    type_name = safe_text(element_type.Name)
                except Exception:
                    try:
                        parameter = element_type.get_Parameter(
                            DB.BuiltInParameter.SYMBOL_NAME_PARAM
                        )
                        if parameter:
                            type_name = safe_text(parameter.AsString())
                    except Exception:
                        pass

                # Tên Family
                try:
                    if isinstance(element_type, DB.FamilySymbol):
                        family_name = safe_text(element_type.FamilyName)
                    else:
                        parameter = element_type.get_Parameter(
                            DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME
                        )
                        if parameter:
                            family_name = safe_text(parameter.AsString())
                except Exception:
                    pass

    except Exception:
        pass

    return family_name, type_name


def main():
    selected_ids = list(uidoc.Selection.GetElementIds())

    if not selected_ids:
        forms.alert(
            "Chưa có đối tượng nào được chọn.\n\n"
            "Hãy chọn một hoặc nhiều đối tượng rồi chạy lại tool.",
            title="Show Element IDs",
            warn_icon=True
        )
        return

    # Sắp xếp theo Element ID
    selected_ids.sort(key=lambda element_id: element_id.IntegerValue)

    output.close_others()
    output.set_title("Selected Element IDs")

    output.print_md("# Selected Element IDs")
    output.print_md(
        "**Tổng số đối tượng:** `{}`".format(len(selected_ids))
    )

    # Link chọn và zoom đến toàn bộ đối tượng
    output.print_md(
        "### {}".format(
            output.linkify(
                selected_ids,
                title="Zoom đến toàn bộ đối tượng đã chọn"
            )
        )
    )

    table_data = []

    for index, element_id in enumerate(selected_ids, 1):
        element = doc.GetElement(element_id)

        if element is None:
            table_data.append([
                index,
                str(element_id.IntegerValue),
                "-",
                "-",
                "-",
                "Không tìm thấy đối tượng"
            ])
            continue

        category_name = get_category_name(element)
        family_name, type_name = get_family_and_type(element)

        # Linkify ElementId:
        # Khi nhấn vào link, Revit sẽ select và zoom tới element.
        id_link = output.linkify(
            element.Id,
            title=str(element.Id.IntegerValue)
        )

        table_data.append([
            index,
            id_link,
            category_name,
            family_name,
            type_name,
            safe_text(element.Name)
        ])

    output.print_table(
        table_data=table_data,
        columns=[
            "STT",
            "Element ID",
            "Category",
            "Family",
            "Type",
            "Element Name"
        ],
        formats=[
            "",
            "",
            "",
            "",
            "",
            ""
        ],
        title="Danh sách đối tượng được chọn"
    )

    output.print_md(
        "> Nhấn vào **Element ID** trong bảng để select và zoom "
        "đến từng đối tượng trong Revit."
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as ex:
        forms.alert(
            "Đã xảy ra lỗi:\n\n{}".format(ex),
            title="Show Element IDs",
            warn_icon=True
        )