# -*- coding: utf-8 -*-
"""
Place Group From Linked Families
IronPython 2.7 / pyRevit

Chức năng:
- Pick nhiều RevitLinkInstance trước khi mở giao diện.
- Chỉ xử lý Link và Family đang hiển thị trong Active View.
- Lọc FamilyInstance theo Family Name hoặc Type Name bằng ô nhập nhiều dòng.
- Hoặc pick trực tiếp một FamilyInstance trong Revit Link và chỉ quét đúng Family đó; có thể khóa đúng Type đã pick.
- Đặt Model Group tại một trong bốn góc cục bộ:
  LEFT_FRONT, RIGHT_FRONT, LEFT_BACK, RIGHT_BACK.
- Xoay Group theo HandOrientation hoặc FacingOrientation.
- Chế độ kiểm tra tạo đúng Family thứ N do người dùng nhập.
- Offset X/Y có dấu: dương và âm theo trục cục bộ của Family.
- Cache kết quả được lưu bền giữa các lần chạy tool.
- Bỏ nested Family, loại nguồn trùng tuyệt đối và nguồn chồng vị trí.
- Progress visibility chỉ đếm các Family đã khớp bộ lọc nguồn.
- Có progress bar và Cancel. Cancel trong lúc đặt sẽ rollback toàn bộ lần chạy.

Ghi chú về visibility:
- Nếu Link hiển thị By Linked View (Revit 2024+), tool dùng linked view để
  thu hẹp tập ứng viên nhưng vẫn bắt buộc kiểm tra lại View Range của Active View
  trong host. Điều này ngăn Family ở tầng khác bị đặt Group trong mặt bằng hiện tại.
- Nếu Link hiển thị By Host View, tool lọc theo: Link hiện trong Active View,
  category không bị ẩn, Crop/Section Box và View Range của Active View.
"""

import json
import math
import os
import re
import traceback

from pyrevit import revit, DB, UI, forms, script
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType


doc = revit.doc
uidoc = revit.uidoc
active_view = revit.active_view
output = script.get_output()

try:
    text_type = unicode
except NameError:
    text_type = str


startup_notices = []
CACHE_VERSION = 4


# ============================================================
# TIỆN ÍCH CHUNG
# ============================================================

def safe_text(value):
    if value is None:
        return u""
    try:
        return text_type(value)
    except Exception:
        try:
            return text_type(str(value), "utf-8", "ignore")
        except Exception:
            return u""


def get_element_name(element):
    if element is None:
        return u""

    try:
        name = element.Name
        if name:
            return safe_text(name)
    except Exception:
        pass

    for bip in (
        DB.BuiltInParameter.SYMBOL_NAME_PARAM,
        DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,
        DB.BuiltInParameter.ELEM_TYPE_PARAM
    ):
        try:
            parameter = element.get_Parameter(bip)
            if parameter:
                value = parameter.AsString()
                if value:
                    return safe_text(value)
        except Exception:
            pass

    return u""


def get_id_value(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        return int(element_id.IntegerValue)


def is_invalid_element_id(element_id):
    if element_id is None:
        return True
    try:
        return get_id_value(element_id) == get_id_value(DB.ElementId.InvalidElementId)
    except Exception:
        return True


def mm_to_internal(mm_value):
    try:
        return DB.UnitUtils.ConvertToInternalUnits(
            float(mm_value),
            DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertToInternalUnits(
            float(mm_value),
            DB.DisplayUnitType.DUT_MILLIMETERS
        )


def internal_to_mm(internal_value):
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(
            float(internal_value),
            DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertFromInternalUnits(
            float(internal_value),
            DB.DisplayUnitType.DUT_MILLIMETERS
        )


def format_mm(internal_value):
    try:
        return u"{0:.1f}".format(internal_to_mm(internal_value))
    except Exception:
        return u"-"


def format_angle_degrees(angle_radians):
    try:
        value = math.degrees(angle_radians)
        while value > 180.0:
            value -= 360.0
        while value <= -180.0:
            value += 360.0
        return u"{0:.2f}".format(value)
    except Exception:
        return u"-"


def show_output_report(
        status_title,
        summary_lines=None,
        notices=None,
        failed_items=None,
        created_records=None,
        duplicate_source_records=None,
        traceback_text=None):
    """In toàn bộ thông báo vào cửa sổ output của pyRevit."""

    summary_lines = summary_lines or []
    notices = notices or []
    failed_items = failed_items or []
    created_records = created_records or []
    duplicate_source_records = duplicate_source_records or []

    try:
        output.set_title(u"Place Group From Linked Families")
    except Exception:
        pass

    output.print_md(u"# Place Group From Linked Families")
    output.print_md(u"## {0}".format(safe_text(status_title)))

    if summary_lines:
        output.print_md(u"### Tóm tắt")
        for line in summary_lines:
            output.print_md(u"- {0}".format(safe_text(line)))

    if notices:
        output.print_md(u"### Thông báo và lưu ý")
        for notice in notices:
            output.print_md(u"- {0}".format(safe_text(notice)))

    if duplicate_source_records:
        output.print_md(u"### Các nguồn Family bị loại vì trùng")
        output.print_md(
            u"Danh sách dưới đây dùng để kiểm tra trường hợp nested Family, "
            u"hai Link chồng nhau hoặc hai Family cùng loại nằm gần như cùng vị trí."
        )
        duplicate_table = []
        for index, item in enumerate(duplicate_source_records):
            duplicate_table.append([
                index + 1,
                safe_text(item.get("reason", u"")),
                safe_text(item.get("kept_link", u"")),
                safe_text(item.get("kept_family", u"")),
                safe_text(item.get("removed_link", u"")),
                safe_text(item.get("removed_family", u"")),
                safe_text(item.get("distance_mm", u"-"))
            ])
        try:
            output.print_table(
                table_data=duplicate_table,
                columns=[
                    u"STT", u"Lý do", u"Link giữ lại", u"Family giữ lại",
                    u"Link bị loại", u"Family bị loại", u"Khoảng cách (mm)"
                ]
            )
        except Exception:
            for row in duplicate_table:
                output.print_md(
                    u"{0}. {1} | Giữ: {2} - {3} | Loại: {4} - {5} | {6} mm".format(*row)
                )

    if created_records:
        output.print_md(u"### Các Group đã tạo")
        output.print_md(
            u"Bấm vào tên Group để chọn phần tử; bấm biểu tượng **Show** bên cạnh liên kết để Revit zoom tới vị trí Group."
        )

        table_data = []
        for index, record in enumerate(created_records):
            group_id = record.get("group_id")
            group_id_value = record.get("group_id_value", u"?")
            try:
                zoom_link = output.linkify(
                    group_id,
                    title=u"Group {0}".format(group_id_value)
                )
            except Exception:
                zoom_link = u"Group {0}".format(group_id_value)

            point = record.get("point")
            if point is not None:
                x_text = format_mm(point.X)
                y_text = format_mm(point.Y)
                z_text = format_mm(point.Z)
            else:
                x_text = y_text = z_text = u"-"

            source_point = record.get("source_point")
            if source_point is not None:
                source_xyz = u"{0}; {1}; {2}".format(
                    format_mm(source_point.X),
                    format_mm(source_point.Y),
                    format_mm(source_point.Z)
                )
            else:
                source_xyz = u"-"

            geometry_source = u"Solid"
            if not record.get("used_solid", False):
                geometry_source = u"BoundingBox"

            mirror_text = u"Có" if record.get("mirrored", False) else u"Không"

            table_data.append([
                index + 1,
                zoom_link,
                safe_text(record.get("group_type_name", u"")),
                safe_text(record.get("source_family", u"")),
                safe_text(record.get("link_name", u"")),
                safe_text(record.get("link_instance_id", u"?")),
                safe_text(record.get("family_id", u"?")),
                safe_text(record.get("super_component_id", u"-")),
                source_xyz,
                x_text,
                y_text,
                z_text,
                safe_text(record.get("angle_deg", u"-")),
                safe_text(record.get("anchor_mode", u"")),
                geometry_source,
                mirror_text
            ])

        columns = [
            u"STT", u"Chọn / Zoom", u"Group Type", u"Family nguồn",
            u"Revit Link", u"Link ID", u"Family ID", u"SuperComponent ID",
            u"Điểm nguồn X;Y;Z (mm)", u"Group X (mm)", u"Group Y (mm)",
            u"Group Z (mm)", u"Góc (°)", u"Điểm đặt", u"Hình học",
            u"Family Mirror"
        ]

        try:
            output.print_table(table_data=table_data, columns=columns)
        except Exception:
            for row in table_data:
                output.print_md(
                    u"{0}. {1} | {2} | {3} | Link={4}; FamilyID={6}; "
                    u"X={9} mm; Y={10} mm; Z={11} mm; Góc={12}°".format(*row)
                )

    if failed_items:
        output.print_md(u"### Các Family không xử lý được")
        output.print_md(u"Tổng số lỗi: **{0}**".format(len(failed_items)))
        for item in failed_items:
            output.print_md(u"- {0}".format(safe_text(item)))

    if traceback_text:
        output.print_md(u"### Chi tiết kỹ thuật")
        try:
            output.print_code(safe_text(traceback_text))
        except Exception:
            output.print_md(u"```\n{0}\n```".format(safe_text(traceback_text)))

    output.print_md(u"---")
    output.print_md(
        u"Active View: **{0}**".format(
            get_element_name(active_view) if active_view is not None else u"Không xác định"
        )
    )

def parse_number(raw_value, field_name, minimum=None):
    text = safe_text(raw_value).strip().replace(u",", u".")
    if not text:
        value = 0.0
    else:
        try:
            value = float(text)
        except Exception:
            raise ValueError(u"'{0}' phải là một số hợp lệ.".format(field_name))

    if minimum is not None and value < minimum:
        raise ValueError(
            u"'{0}' phải lớn hơn hoặc bằng {1}.".format(field_name, minimum)
        )
    return value


def parse_integer(raw_value, field_name, minimum=None, maximum=None):
    text = safe_text(raw_value).strip()
    if not text:
        value = 1
    else:
        try:
            numeric_value = float(text.replace(u",", u"."))
            value = int(numeric_value)
            if abs(numeric_value - value) > 0.0000001:
                raise ValueError()
        except Exception:
            raise ValueError(u"'{0}' phải là số nguyên hợp lệ.".format(field_name))

    if minimum is not None and value < minimum:
        raise ValueError(
            u"'{0}' phải lớn hơn hoặc bằng {1}.".format(field_name, minimum)
        )

    if maximum is not None and value > maximum:
        raise ValueError(
            u"'{0}' phải nhỏ hơn hoặc bằng {1}.".format(field_name, maximum)
        )

    return value


def normalize_xy(vector):
    if vector is None:
        return None

    vector_xy = DB.XYZ(vector.X, vector.Y, 0.0)
    if vector_xy.GetLength() < 0.0000001:
        return None

    return vector_xy.Normalize()


def vector_angle_xy(vector):
    vector_xy = normalize_xy(vector)
    if vector_xy is None:
        return 0.0
    return math.atan2(vector_xy.Y, vector_xy.X)


def points_distance_3d(point_a, point_b):
    return (point_a - point_b).GetLength()


def bbox_local_corners(bbox):
    if bbox is None:
        return []

    min_pt = bbox.Min
    max_pt = bbox.Max

    return [
        DB.XYZ(min_pt.X, min_pt.Y, min_pt.Z),
        DB.XYZ(max_pt.X, min_pt.Y, min_pt.Z),
        DB.XYZ(min_pt.X, max_pt.Y, min_pt.Z),
        DB.XYZ(max_pt.X, max_pt.Y, min_pt.Z),
        DB.XYZ(min_pt.X, min_pt.Y, max_pt.Z),
        DB.XYZ(max_pt.X, min_pt.Y, max_pt.Z),
        DB.XYZ(min_pt.X, max_pt.Y, max_pt.Z),
        DB.XYZ(max_pt.X, max_pt.Y, max_pt.Z)
    ]


def bbox_model_corners(bbox):
    if bbox is None:
        return []

    try:
        transform = bbox.Transform
    except Exception:
        transform = DB.Transform.Identity

    result = []
    for point in bbox_local_corners(bbox):
        try:
            result.append(transform.OfPoint(point))
        except Exception:
            result.append(point)
    return result


def ranges_overlap(min_a, max_a, min_b, max_b, tolerance=0.000001):
    return not (
        max_a < min_b - tolerance or
        min_a > max_b + tolerance
    )


# ============================================================
# PICK NHIỀU REVIT LINK
# ============================================================

class RevitLinkSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        try:
            return isinstance(element, DB.RevitLinkInstance)
        except Exception:
            return False

    def AllowReference(self, reference, position):
        return False


class LinkItem(object):
    def __init__(self, link_instance):
        self.element = link_instance
        self.link_doc = link_instance.GetLinkDocument()
        self.uid = safe_text(link_instance.UniqueId)
        self.transform = link_instance.GetTotalTransform()

        instance_name = get_element_name(link_instance)
        document_name = u""
        try:
            document_name = safe_text(self.link_doc.Title)
        except Exception:
            pass

        if document_name and document_name.lower() not in instance_name.lower():
            self.display_name = u"{0}  |  {1}".format(instance_name, document_name)
        else:
            self.display_name = instance_name or document_name or u"Revit Link"


class GroupTypeItem(object):
    def __init__(self, group_type):
        self.element = group_type
        self.uid = safe_text(group_type.UniqueId)
        self.display_name = get_element_name(group_type) or u"Unnamed Group Type"


def get_family_unique_id(family_instance):
    try:
        return safe_text(family_instance.UniqueId)
    except Exception:
        return u""


def get_family_id_value(family_instance):
    try:
        return get_id_value(family_instance.Id)
    except Exception:
        return -1


def get_super_component(family_instance):
    try:
        return family_instance.SuperComponent
    except Exception:
        return None


def get_super_component_id_value(family_instance):
    super_component = get_super_component(family_instance)
    if super_component is None:
        return None
    try:
        return get_id_value(super_component.Id)
    except Exception:
        return None


def family_source_key(link_item, family_instance):
    family_uid = get_family_unique_id(family_instance)
    if family_uid:
        return u"{0}|UID|{1}".format(safe_text(link_item.uid), family_uid)
    return u"{0}|ID|{1}".format(
        safe_text(link_item.uid),
        get_family_id_value(family_instance)
    )


def family_semantic_key(family_instance):
    category_id = -1
    family_name = u""
    type_name = u""

    try:
        if family_instance.Category is not None:
            category_id = get_id_value(family_instance.Category.Id)
    except Exception:
        pass

    try:
        family_name = safe_text(family_instance.Symbol.Family.Name).strip().lower()
    except Exception:
        pass

    try:
        type_name = get_element_name(family_instance.Symbol).strip().lower()
    except Exception:
        pass

    return u"{0}|{1}|{2}".format(category_id, family_name, type_name)


class FamilyWorkItem(object):
    def __init__(self, link_item, family_instance):
        self.link_item = link_item
        self.family = family_instance
        self.source_key = family_source_key(link_item, family_instance)
        self.semantic_key = family_semantic_key(family_instance)
        self.source_point = None

    def get_source_point(self):
        if self.source_point is None:
            host_points = get_family_host_bbox_points(
                self.family,
                self.link_item,
                None
            )
            self.source_point = get_family_host_reference_point(
                self.family,
                self.link_item,
                host_points
            )
        return self.source_point


def new_scan_diagnostics():
    return {
        "candidate_total": 0,
        "nested_skipped": 0,
        "exact_source_duplicate_skipped": 0,
        "text_matched": 0,
        "visibility_skipped": 0,
        "overlap_source_skipped": 0,
        "final_count": 0,
        "duplicate_source_records": []
    }


def merge_scan_diagnostics(base, addition):
    result = new_scan_diagnostics()
    for key in result.keys():
        if key == "duplicate_source_records":
            result[key] = list((base or {}).get(key, [])) + list((addition or {}).get(key, []))
        else:
            try:
                result[key] = int((base or {}).get(key, 0)) + int((addition or {}).get(key, 0))
            except Exception:
                result[key] = 0
    return result


def scan_diagnostics_summary(diagnostics):
    diagnostics = diagnostics or new_scan_diagnostics()
    return [
        u"FamilyInstance ứng viên: {0}".format(diagnostics.get("candidate_total", 0)),
        u"Bỏ nested Family: {0}".format(diagnostics.get("nested_skipped", 0)),
        u"Bỏ nguồn trùng tuyệt đối: {0}".format(diagnostics.get("exact_source_duplicate_skipped", 0)),
        u"Khớp bộ lọc nguồn: {0}".format(diagnostics.get("text_matched", 0)),
        u"Không hiển thị trong Active View: {0}".format(diagnostics.get("visibility_skipped", 0)),
        u"Bỏ nguồn chồng vị trí: {0}".format(diagnostics.get("overlap_source_skipped", 0)),
        u"Family nguồn duy nhất sau xử lý: {0}".format(diagnostics.get("final_count", 0))
    ]

def pick_link_items():
    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element,
            RevitLinkSelectionFilter(),
            u"Chọn một hoặc nhiều Revit Link, sau đó bấm Finish"
        )
    except UI.Exceptions.OperationCanceledException:
        return []

    result = []
    used_ids = set()

    for reference in references:
        try:
            link_instance = doc.GetElement(reference.ElementId)
            if not isinstance(link_instance, DB.RevitLinkInstance):
                continue
            if link_instance.GetLinkDocument() is None:
                continue

            id_value = get_id_value(link_instance.Id)
            if id_value in used_ids:
                continue

            used_ids.add(id_value)
            result.append(LinkItem(link_instance))
        except Exception:
            pass

    return result


def is_model_group_type(group_type):
    try:
        category = group_type.Category
        if category is None:
            return False
        return get_id_value(category.Id) == int(DB.BuiltInCategory.OST_IOSModelGroups)
    except Exception:
        return False


def collect_model_group_types():
    items = []
    collector = DB.FilteredElementCollector(doc).OfClass(DB.GroupType)

    for group_type in collector:
        if is_model_group_type(group_type):
            items.append(GroupTypeItem(group_type))

    items.sort(key=lambda item: item.display_name.lower())
    return items


def get_link_ids_visible_in_active_view(view):
    result = set()
    try:
        collector = (
            DB.FilteredElementCollector(doc, view.Id)
            .OfClass(DB.RevitLinkInstance)
            .WhereElementIsNotElementType()
        )
        for link_instance in collector:
            result.add(get_id_value(link_instance.Id))
    except Exception:
        # Nếu view không hỗ trợ view-specific collector, giữ tập rỗng để caller
        # báo rõ thay vì quét nhầm toàn bộ model.
        pass
    return result


def keep_only_links_visible_in_active_view(link_items, view):
    visible_ids = get_link_ids_visible_in_active_view(view)
    visible_items = []
    hidden_items = []

    for item in link_items:
        if get_id_value(item.element.Id) in visible_ids:
            visible_items.append(item)
        else:
            hidden_items.append(item)

    return visible_items, hidden_items


# ============================================================
# LỌC FAMILY THEO TEXT
# ============================================================

def split_keywords(raw_text):
    text = safe_text(raw_text).strip().lower()
    if not text:
        return []

    parts = re.split(u"[;,\r\n]+", text)
    keywords = []
    for part in parts:
        keyword = safe_text(part).strip()
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords


def family_identity_text(family_instance):
    family_name = u""
    type_name = u""

    try:
        family_name = safe_text(family_instance.Symbol.Family.Name)
    except Exception:
        pass

    try:
        type_name = get_element_name(family_instance.Symbol)
    except Exception:
        pass

    return u"{0} {1}".format(family_name, type_name).lower()


def family_matches(family_instance, keywords):
    if not keywords:
        return True

    search_text = family_identity_text(family_instance)
    for keyword in keywords:
        if keyword in search_text:
            return True
    return False


def get_family_name(family_instance):
    try:
        return safe_text(family_instance.Symbol.Family.Name).strip()
    except Exception:
        return u""


def get_family_type_name(family_instance):
    try:
        return get_element_name(family_instance.Symbol).strip()
    except Exception:
        return u""


def family_matches_source_filter(
        family_instance,
        keywords,
        source_mode,
        picked_family_name,
        picked_type_name,
        lock_picked_type):
    """Khớp Family nguồn theo từ khóa hoặc theo Family đã pick trong Link."""
    mode = safe_text(source_mode).upper().strip()
    if mode == "PICK":
        target_family = safe_text(picked_family_name).strip().lower()
        if not target_family:
            return False

        current_family = get_family_name(family_instance).lower()
        if current_family != target_family:
            return False

        if lock_picked_type:
            target_type = safe_text(picked_type_name).strip().lower()
            current_type = get_family_type_name(family_instance).lower()
            return bool(target_type) and current_type == target_type

        return True

    return family_matches(family_instance, keywords)


def picked_family_filter_signature(source_mode, picked_family_name, picked_type_name, lock_picked_type):
    mode = safe_text(source_mode).upper().strip() or "KEYWORD"
    return u"MODE={0}|FAMILY={1}|TYPE={2}|LOCKTYPE={3}".format(
        mode,
        safe_text(picked_family_name).strip().lower(),
        safe_text(picked_type_name).strip().lower(),
        bool(lock_picked_type)
    )


def pick_linked_family_instance(allowed_link_items):
    """Pick một FamilyInstance trong một Revit Link đã được chọn ban đầu."""
    reference = uidoc.Selection.PickObject(
        ObjectType.LinkedElement,
        u"Pick một Family trong Revit Link đã chọn"
    )

    link_instance = doc.GetElement(reference.ElementId)
    if not isinstance(link_instance, DB.RevitLinkInstance):
        raise ValueError(u"Đối tượng đã pick không thuộc Revit Link.")

    allowed_map = {}
    for item in allowed_link_items:
        try:
            allowed_map[get_id_value(item.element.Id)] = item
        except Exception:
            pass

    link_id_value = get_id_value(link_instance.Id)
    link_item = allowed_map.get(link_id_value)
    if link_item is None:
        raise ValueError(
            u"Family đã pick thuộc Link không nằm trong danh sách Link đã chọn khi mở tool."
        )

    link_doc = link_instance.GetLinkDocument()
    if link_doc is None:
        raise ValueError(u"Revit Link chứa Family đã pick hiện không được load.")

    try:
        linked_element_id = reference.LinkedElementId
    except Exception:
        linked_element_id = None

    if linked_element_id is None or is_invalid_element_id(linked_element_id):
        raise ValueError(u"Không đọc được ElementId của phần tử trong Revit Link.")

    linked_element = link_doc.GetElement(linked_element_id)
    if not isinstance(linked_element, DB.FamilyInstance):
        raise ValueError(u"Đối tượng đã pick không phải FamilyInstance. Hãy pick đúng Family trong Link.")

    return link_item, linked_element


def get_document_cache_key():
    parts = []

    try:
        project_info = doc.ProjectInformation
        if project_info is not None:
            parts.append(safe_text(project_info.UniqueId))
    except Exception:
        pass

    try:
        path_name = safe_text(doc.PathName).strip()
        if path_name:
            parts.append(path_name.lower())
    except Exception:
        pass

    try:
        parts.append(safe_text(doc.Title).lower())
    except Exception:
        pass

    return u"|".join(parts)


def transform_signature(transform):
    try:
        values = [
            transform.Origin.X, transform.Origin.Y, transform.Origin.Z,
            transform.BasisX.X, transform.BasisX.Y, transform.BasisX.Z,
            transform.BasisY.X, transform.BasisY.Y, transform.BasisY.Z,
            transform.BasisZ.X, transform.BasisZ.Y, transform.BasisZ.Z
        ]
        return u",".join([u"{0:.8f}".format(value) for value in values])
    except Exception:
        return u""


def get_view_state_signature(view):
    parts = [safe_text(getattr(view, "ViewType", u""))]

    try:
        parts.append(u"CROP_ACTIVE={0}".format(bool(view.CropBoxActive)))
        crop_box = view.CropBox
        if crop_box is not None:
            parts.append(u"CROP_MIN={0:.8f},{1:.8f},{2:.8f}".format(
                crop_box.Min.X, crop_box.Min.Y, crop_box.Min.Z
            ))
            parts.append(u"CROP_MAX={0:.8f},{1:.8f},{2:.8f}".format(
                crop_box.Max.X, crop_box.Max.Y, crop_box.Max.Z
            ))
            parts.append(u"CROP_T={0}".format(
                transform_signature(crop_box.Transform)
            ))
    except Exception:
        pass

    if isinstance(view, DB.ViewPlan):
        try:
            view_range = view.GetViewRange()
            for plane_name, plane in (
                (u"TOP", DB.PlanViewPlane.TopClipPlane),
                (u"CUT", DB.PlanViewPlane.CutPlane),
                (u"BOTTOM", DB.PlanViewPlane.BottomClipPlane),
                (u"DEPTH", DB.PlanViewPlane.ViewDepthPlane)
            ):
                try:
                    level_id = view_range.GetLevelId(plane)
                    offset = view_range.GetOffset(plane)
                    parts.append(u"{0}={1},{2:.8f}".format(
                        plane_name,
                        get_id_value(level_id),
                        offset
                    ))
                except Exception:
                    pass
        except Exception:
            pass

    try:
        if isinstance(view, DB.View3D) and view.IsSectionBoxActive:
            section_box = view.GetSectionBox()
            if section_box is not None:
                parts.append(u"SECTION_MIN={0:.8f},{1:.8f},{2:.8f}".format(
                    section_box.Min.X, section_box.Min.Y, section_box.Min.Z
                ))
                parts.append(u"SECTION_MAX={0:.8f},{1:.8f},{2:.8f}".format(
                    section_box.Max.X, section_box.Max.Y, section_box.Max.Z
                ))
                parts.append(u"SECTION_T={0}".format(
                    transform_signature(section_box.Transform)
                ))
    except Exception:
        pass

    return u"|".join(parts)


def make_scan_signature(
        link_items,
        host_view,
        raw_filter_text,
        ignore_nested,
        source_dedup_enabled,
        source_overlap_tolerance_text,
        source_mode="KEYWORD",
        picked_family_name=u"",
        picked_type_name=u"",
        lock_picked_type=False):
    """Tạo chữ ký cache, bao gồm các thiết lập ảnh hưởng tới danh sách nguồn."""

    try:
        view_uid = safe_text(host_view.UniqueId)
    except Exception:
        try:
            view_uid = safe_text(get_id_value(host_view.Id))
        except Exception:
            view_uid = u"-1"

    link_keys = []
    for link_item in link_items:
        link_uid = safe_text(getattr(link_item, "uid", u""))
        linked_view_uid = u"HOST_VIEW"
        try:
            linked_view = get_linked_view(link_item, host_view)
            if linked_view is not None:
                linked_view_uid = safe_text(linked_view.UniqueId)
        except Exception:
            pass

        link_keys.append(u"{0}|{1}|{2}".format(
            link_uid,
            transform_signature(link_item.transform),
            linked_view_uid
        ))

    link_keys.sort()
    normalized_filter = u"|".join(split_keywords(raw_filter_text))
    raw_tolerance = safe_text(source_overlap_tolerance_text).strip().replace(u",", u".")
    try:
        normalized_tolerance = u"{0:.6f}".format(float(raw_tolerance or 0.0))
    except Exception:
        normalized_tolerance = raw_tolerance

    return (
        u"CACHEV={0};DOC={1};VIEW={2};VIEWSTATE={3};LINKS={4};FILTER={5};"
        u"IGNORE_NESTED={6};SOURCE_DEDUP={7};SOURCE_TOL={8};SOURCE_SELECTOR={9}"
    ).format(
        CACHE_VERSION,
        get_document_cache_key(),
        view_uid,
        get_view_state_signature(host_view),
        u";".join(link_keys),
        normalized_filter,
        bool(ignore_nested),
        bool(source_dedup_enabled),
        normalized_tolerance,
        picked_family_filter_signature(
            source_mode,
            picked_family_name,
            picked_type_name,
            lock_picked_type
        )
    )

def serialize_scan_cache(signature, work_items, diagnostics=None):
    entries = []
    used_source_keys = set()

    for work_item in work_items:
        source_key = family_source_key(work_item.link_item, work_item.family)
        if source_key in used_source_keys:
            continue
        used_source_keys.add(source_key)

        family_uid = get_family_unique_id(work_item.family)
        family_id = get_family_id_value(work_item.family)
        if not family_uid and family_id < 0:
            continue

        entries.append({
            "link_uid": safe_text(work_item.link_item.uid),
            "family_uid": family_uid,
            "family_id": family_id
        })

    payload = {
        "version": CACHE_VERSION,
        "signature": safe_text(signature),
        "entries": entries,
        "diagnostics": diagnostics or new_scan_diagnostics()
    }

    return safe_text(json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":")
    ))


def save_persistent_scan_cache(
        config,
        signature,
        work_items,
        filter_text=None,
        diagnostics=None):
    config.scan_cache_json = serialize_scan_cache(
        signature,
        work_items,
        diagnostics
    )
    if filter_text is not None:
        config.family_filter = safe_text(filter_text)
    script.save_config()


def get_cached_family(link_doc, family_uid, family_id):
    family = None

    if family_uid:
        try:
            family = link_doc.GetElement(family_uid)
        except Exception:
            family = None

    if family is None:
        try:
            if int(family_id) >= 0:
                family = link_doc.GetElement(DB.ElementId(int(family_id)))
        except Exception:
            family = None

    if isinstance(family, DB.FamilyInstance):
        return family

    return None


def load_persistent_scan_cache(
        config,
        expected_signature,
        link_items,
        ignore_nested,
        source_dedup_enabled,
        source_overlap_tolerance_internal):
    raw_cache = safe_text(getattr(config, "scan_cache_json", u"")).strip()
    if not raw_cache:
        return None, u"Không có cache đã lưu.", None

    try:
        payload = json.loads(raw_cache)
    except Exception:
        return None, u"Cache đã lưu bị lỗi định dạng.", None

    try:
        if int(payload.get("version", -1)) != CACHE_VERSION:
            return None, u"Cache thuộc phiên bản tool cũ.", None
    except Exception:
        return None, u"Cache thuộc phiên bản tool cũ.", None

    if safe_text(payload.get("signature", u"")) != safe_text(expected_signature):
        return None, u"Cache không khớp Active View, Link, bộ lọc hoặc thiết lập chống trùng.", None

    link_map = {}
    for link_item in link_items:
        link_map[safe_text(link_item.uid)] = link_item

    restored = []
    used_source_keys = set()
    entries = payload.get("entries", []) or []
    defensive_exact_duplicate_count = 0
    defensive_nested_count = 0

    for entry in entries:
        link_uid = safe_text(entry.get("link_uid", u""))
        link_item = link_map.get(link_uid)
        if link_item is None:
            return None, u"Một Revit Link trong cache không còn được chọn hoặc đã thay đổi.", None

        family = get_cached_family(
            link_item.link_doc,
            safe_text(entry.get("family_uid", u"")),
            entry.get("family_id", -1)
        )
        if family is None:
            return None, u"Một Family trong cache không còn tồn tại hoặc Link đã reload.", None

        if ignore_nested and get_super_component(family) is not None:
            defensive_nested_count += 1
            continue

        source_key = family_source_key(link_item, family)
        if source_key in used_source_keys:
            defensive_exact_duplicate_count += 1
            continue
        used_source_keys.add(source_key)
        restored.append(FamilyWorkItem(link_item, family))

    restored.sort(
        key=lambda item: (
            item.link_item.display_name.lower(),
            get_family_id_value(item.family)
        )
    )

    diagnostics = payload.get("diagnostics", None) or new_scan_diagnostics()
    diagnostics = dict(diagnostics)
    diagnostics["exact_source_duplicate_skipped"] = int(
        diagnostics.get("exact_source_duplicate_skipped", 0)
    ) + defensive_exact_duplicate_count
    diagnostics["nested_skipped"] = int(
        diagnostics.get("nested_skipped", 0)
    ) + defensive_nested_count

    if source_dedup_enabled:
        restored, overlap_records = deduplicate_overlapping_sources(
            restored,
            source_overlap_tolerance_internal
        )
        if overlap_records:
            diagnostics["overlap_source_skipped"] = int(
                diagnostics.get("overlap_source_skipped", 0)
            ) + len(overlap_records)
            diagnostics["duplicate_source_records"] = list(
                diagnostics.get("duplicate_source_records", [])
            ) + overlap_records

    diagnostics["final_count"] = len(restored)
    return restored, u"Đã nạp cache bền từ lần kiểm tra trước.", diagnostics

def describe_family(family_instance):
    family_name = u"Unknown Family"
    type_name = u"Unknown Type"

    try:
        family_name = safe_text(family_instance.Symbol.Family.Name)
    except Exception:
        pass

    try:
        type_name = get_element_name(family_instance.Symbol)
    except Exception:
        pass

    try:
        element_id = get_id_value(family_instance.Id)
    except Exception:
        element_id = u"?"

    return u"{0} : {1} [ID {2}]".format(family_name, type_name, element_id)


# ============================================================
# NHẬN DIỆN FAMILY HIỂN THỊ TRONG ACTIVE VIEW
# ============================================================

def get_link_graphics_settings(view, link_instance):
    """Revit 2024+. Trả về None trên phiên bản cũ hoặc view không hỗ trợ."""
    try:
        settings = view.GetLinkOverrides(link_instance.Id)
        if settings is not None:
            return settings
    except Exception:
        pass

    try:
        type_id = link_instance.GetTypeId()
        settings = view.GetLinkOverrides(type_id)
        if settings is not None:
            return settings
    except Exception:
        pass

    return None


def get_linked_view(link_item, host_view):
    settings = get_link_graphics_settings(host_view, link_item.element)
    if settings is None:
        return None

    try:
        visibility_type = safe_text(settings.LinkVisibilityType).lower()
        linked_view_id = settings.LinkedViewId

        if "bylinkview" not in visibility_type and "by link view" not in visibility_type:
            return None
        if is_invalid_element_id(linked_view_id):
            return None

        linked_view = link_item.link_doc.GetElement(linked_view_id)
        if isinstance(linked_view, DB.View) and not linked_view.IsTemplate:
            return linked_view
    except Exception:
        pass

    return None


def category_is_visible_by_host_view(family_instance, host_view):
    try:
        category = family_instance.Category
        if category is None:
            return False

        # Built-in category ids có cùng giá trị giữa host và link.
        if host_view.GetCategoryHidden(category.Id):
            return False
    except Exception:
        # Một số category/link setting không cho query; không loại nhầm.
        pass

    return True


def get_plan_view_z_range(view_plan):
    """
    Trả về khoảng Z host theo View Range. Bao gồm View Depth để các phần tử
    nhìn xuống dưới Bottom Clip vẫn được giữ lại.
    """
    try:
        view_range = view_plan.GetViewRange()
    except Exception:
        return None

    elevations = []
    plane_names = [
        "TopClipPlane",
        "CutPlane",
        "BottomClipPlane",
        "ViewDepthPlane"
    ]

    for plane_name in plane_names:
        try:
            plane = getattr(DB.PlanViewPlane, plane_name)
            level_id = view_range.GetLevelId(plane)
            offset = view_range.GetOffset(plane)

            if is_invalid_element_id(level_id):
                continue

            level = doc.GetElement(level_id)
            if isinstance(level, DB.Level):
                elevations.append(level.Elevation + offset)
        except Exception:
            pass

    if not elevations:
        return None

    return min(elevations), max(elevations)


def get_active_view_clip_box(view):
    """
    Trả về (BoundingBoxXYZ, check_z).
    - Plan: CropBox chỉ kiểm XY; Z dùng View Range riêng.
    - Section/Elevation/3D: Crop/SectionBox kiểm cả XYZ.
    """
    try:
        if isinstance(view, DB.View3D) and view.IsSectionBoxActive:
            return view.GetSectionBox(), True
    except Exception:
        pass

    try:
        if view.CropBoxActive:
            check_z = not isinstance(view, DB.ViewPlan)
            return view.CropBox, check_z
    except Exception:
        pass

    return None, False


def points_intersect_oriented_box(host_points, oriented_box, check_z):
    if not host_points or oriented_box is None:
        return True

    try:
        inverse = oriented_box.Transform.Inverse
    except Exception:
        inverse = DB.Transform.Identity

    local_points = []
    for point in host_points:
        try:
            local_points.append(inverse.OfPoint(point))
        except Exception:
            local_points.append(point)

    xs = [point.X for point in local_points]
    ys = [point.Y for point in local_points]
    zs = [point.Z for point in local_points]

    min_pt = oriented_box.Min
    max_pt = oriented_box.Max

    if not ranges_overlap(min(xs), max(xs), min_pt.X, max_pt.X):
        return False
    if not ranges_overlap(min(ys), max(ys), min_pt.Y, max_pt.Y):
        return False
    if check_z and not ranges_overlap(min(zs), max(zs), min_pt.Z, max_pt.Z):
        return False

    return True


def get_family_host_bbox_points(family_instance, link_item, linked_view=None):
    bbox = None

    if linked_view is not None:
        try:
            bbox = family_instance.get_BoundingBox(linked_view)
        except Exception:
            bbox = None

    if bbox is None:
        try:
            bbox = family_instance.get_BoundingBox(None)
        except Exception:
            bbox = None

    if bbox is None:
        return []

    result = []
    for link_point in bbox_model_corners(bbox):
        try:
            result.append(link_item.transform.OfPoint(link_point))
        except Exception:
            pass
    return result


def get_family_host_reference_point(family_instance, link_item, host_points=None):
    """
    Lấy cao độ đại diện của Family trong hệ tọa độ host.

    Ưu tiên LocationPoint/LocationCurve vì đây là cao độ đặt thật của Family.
    BoundingBox center chỉ là fallback. Việc kiểm tra thêm điểm đại diện giúp
    ngăn Family ở tầng trên lọt qua chỉ vì BoundingBox hoặc linked view rộng.
    """
    link_point = None

    try:
        location = family_instance.Location

        if isinstance(location, DB.LocationPoint):
            link_point = location.Point

        elif isinstance(location, DB.LocationCurve):
            curve = location.Curve
            if curve is not None:
                link_point = curve.Evaluate(0.5, True)
    except Exception:
        link_point = None

    if link_point is not None:
        try:
            return link_item.transform.OfPoint(link_point)
        except Exception:
            pass

    if host_points:
        try:
            count = float(len(host_points))
            return DB.XYZ(
                sum(point.X for point in host_points) / count,
                sum(point.Y for point in host_points) / count,
                sum(point.Z for point in host_points) / count
            )
        except Exception:
            pass

    return None


def family_intersects_active_view(family_instance, link_item, host_view, linked_view):
    host_points = get_family_host_bbox_points(
        family_instance,
        link_item,
        linked_view
    )
    if not host_points:
        return False

    clip_box, check_z = get_active_view_clip_box(host_view)
    if clip_box is not None:
        if not points_intersect_oriented_box(host_points, clip_box, check_z):
            return False

    # Luôn kiểm tra View Range của Active View trong host, kể cả Link đang dùng
    # By Linked View. Collector của linked view chỉ cho biết Family hiện trong
    # linked view đó, không đảm bảo Family nằm trong tầng của host active view.
    if isinstance(host_view, DB.ViewPlan):
        z_range = get_plan_view_z_range(host_view)
        if z_range is not None:
            z_min = z_range[0]
            z_max = z_range[1]
            z_values = [point.Z for point in host_points]

            # Bước 1: hình học Family phải giao với View Range của host.
            if not ranges_overlap(
                min(z_values),
                max(z_values),
                z_min,
                z_max
            ):
                return False

            # Bước 2: điểm đặt đại diện cũng phải nằm trong View Range. Bước này
            # loại Family ở tầng trên/dưới có BoundingBox bất thường hoặc family
            # cao nhiều tầng nhưng điểm đặt không thuộc tầng đang mở.
            reference_point = get_family_host_reference_point(
                family_instance,
                link_item,
                host_points
            )

            if reference_point is not None:
                z_tolerance = mm_to_internal(5.0)
                if (
                    reference_point.Z < z_min - z_tolerance
                    or reference_point.Z > z_max + z_tolerance
                ):
                    return False

    return True


def collect_link_candidate_families(link_item, host_view):
    linked_view = get_linked_view(link_item, host_view)

    if linked_view is not None:
        try:
            collector = (
                DB.FilteredElementCollector(link_item.link_doc, linked_view.Id)
                .OfClass(DB.FamilyInstance)
                .WhereElementIsNotElementType()
            )
            return list(collector), linked_view
        except Exception:
            pass

    collector = (
        DB.FilteredElementCollector(link_item.link_doc)
        .OfClass(DB.FamilyInstance)
        .WhereElementIsNotElementType()
    )
    return list(collector), None


def spatial_cell_key(point, tolerance):
    if point is None or tolerance <= 0.0:
        return None
    return (
        int(math.floor(point.X / tolerance)),
        int(math.floor(point.Y / tolerance)),
        int(math.floor(point.Z / tolerance))
    )


def make_duplicate_source_record(reason, kept_item, removed_item, distance_internal=None):
    distance_text = u"-"
    if distance_internal is not None:
        try:
            distance_text = u"{0:.2f}".format(internal_to_mm(distance_internal))
        except Exception:
            pass

    return {
        "reason": reason,
        "kept_link": kept_item.link_item.display_name,
        "kept_family": describe_family(kept_item.family),
        "removed_link": removed_item.link_item.display_name,
        "removed_family": describe_family(removed_item.family),
        "distance_mm": distance_text
    }


def deduplicate_overlapping_sources(work_items, tolerance):
    """
    Loại hai nguồn có cùng Category + Family + Type và điểm gốc trong host gần nhau.
    Dùng grid bucket để tránh so sánh O(n²) trên danh sách lớn.
    """
    if tolerance is None or tolerance <= 0.0:
        return list(work_items), []

    kept = []
    buckets = {}
    duplicate_records = []

    for work_item in work_items:
        point = work_item.get_source_point()
        if point is None:
            kept.append(work_item)
            continue

        cell = spatial_cell_key(point, tolerance)
        semantic_key = work_item.semantic_key
        duplicate_of = None
        duplicate_distance = None

        for dx in (-1, 0, 1):
            if duplicate_of is not None:
                break
            for dy in (-1, 0, 1):
                if duplicate_of is not None:
                    break
                for dz in (-1, 0, 1):
                    bucket_key = (
                        semantic_key,
                        cell[0] + dx,
                        cell[1] + dy,
                        cell[2] + dz
                    )
                    for kept_item in buckets.get(bucket_key, []):
                        kept_point = kept_item.get_source_point()
                        if kept_point is None:
                            continue
                        distance = points_distance_3d(point, kept_point)
                        if distance <= tolerance:
                            duplicate_of = kept_item
                            duplicate_distance = distance
                            break
                    if duplicate_of is not None:
                        break

        if duplicate_of is not None:
            duplicate_records.append(make_duplicate_source_record(
                u"Cùng Family/Type và chồng vị trí",
                duplicate_of,
                work_item,
                duplicate_distance
            ))
            continue

        kept.append(work_item)
        bucket_key = (semantic_key, cell[0], cell[1], cell[2])
        buckets.setdefault(bucket_key, []).append(work_item)

    return kept, duplicate_records


def collect_visible_matching_work_items(
        link_items,
        host_view,
        keywords,
        ignore_nested,
        source_dedup_enabled,
        source_overlap_tolerance_internal,
        progress_enabled,
        source_mode="KEYWORD",
        picked_family_name=u"",
        picked_type_name=u"",
        lock_picked_type=False):
    """Quét, lọc visibility và chuẩn hóa danh sách Family nguồn."""

    diagnostics = new_scan_diagnostics()
    matched_candidates = []
    used_source_map = {}

    for link_item in link_items:
        candidates, linked_view = collect_link_candidate_families(
            link_item,
            host_view
        )
        diagnostics["candidate_total"] += len(candidates)

        for family_instance in candidates:
            try:
                if not family_matches_source_filter(
                        family_instance,
                        keywords,
                        source_mode,
                        picked_family_name,
                        picked_type_name,
                        lock_picked_type):
                    continue

                diagnostics["text_matched"] += 1
                current_item = FamilyWorkItem(link_item, family_instance)

                super_component = get_super_component(family_instance)
                if ignore_nested and super_component is not None:
                    diagnostics["nested_skipped"] += 1
                    diagnostics["duplicate_source_records"].append({
                        "reason": u"Nested Family có SuperComponent",
                        "kept_link": link_item.display_name,
                        "kept_family": describe_family(super_component),
                        "removed_link": link_item.display_name,
                        "removed_family": describe_family(family_instance),
                        "distance_mm": u"-"
                    })
                    continue

                source_key = current_item.source_key
                kept_item = used_source_map.get(source_key)
                if kept_item is not None:
                    diagnostics["exact_source_duplicate_skipped"] += 1
                    diagnostics["duplicate_source_records"].append(
                        make_duplicate_source_record(
                            u"Trùng Link UniqueId + Family UniqueId",
                            kept_item,
                            current_item,
                            0.0
                        )
                    )
                    continue

                used_source_map[source_key] = current_item
                matched_candidates.append((
                    link_item,
                    family_instance,
                    linked_view
                ))
            except Exception:
                pass

    visible_items = []
    cancelled = False
    total_matched = len(matched_candidates)

    def process_visibility(link_item, family_instance, linked_view):
        if linked_view is None and not category_is_visible_by_host_view(
            family_instance,
            host_view
        ):
            diagnostics["visibility_skipped"] += 1
            return

        if not family_intersects_active_view(
            family_instance,
            link_item,
            host_view,
            linked_view
        ):
            diagnostics["visibility_skipped"] += 1
            return

        visible_items.append(FamilyWorkItem(link_item, family_instance))

    if progress_enabled and total_matched > 0:
        with forms.ProgressBar(
            title=(
                u"Đang kiểm tra hiển thị các Family đã khớp bộ lọc... "
                u"{value} / {max_value}"
            ),
            cancellable=True,
            step=1
        ) as progress:
            for index, candidate in enumerate(matched_candidates):
                if progress.cancelled:
                    cancelled = True
                    break

                link_item, family_instance, linked_view = candidate
                try:
                    process_visibility(link_item, family_instance, linked_view)
                except Exception:
                    diagnostics["visibility_skipped"] += 1

                progress.update_progress(index + 1, total_matched)

            if progress.cancelled:
                cancelled = True
    else:
        for link_item, family_instance, linked_view in matched_candidates:
            try:
                process_visibility(link_item, family_instance, linked_view)
            except Exception:
                diagnostics["visibility_skipped"] += 1

    visible_items.sort(
        key=lambda item: (
            item.link_item.display_name.lower(),
            get_family_id_value(item.family)
        )
    )

    if source_dedup_enabled:
        visible_items, overlap_records = deduplicate_overlapping_sources(
            visible_items,
            source_overlap_tolerance_internal
        )
        diagnostics["overlap_source_skipped"] = len(overlap_records)
        diagnostics["duplicate_source_records"] = list(
            diagnostics.get("duplicate_source_records", [])
        ) + overlap_records

    diagnostics["final_count"] = len(visible_items)
    return visible_items, cancelled, diagnostics


# ============================================================
# HÌNH HỌC FAMILY TRONG LINK
# ============================================================

def get_family_origin(family_instance):
    location = family_instance.Location

    if isinstance(location, DB.LocationPoint):
        return location.Point

    if isinstance(location, DB.LocationCurve):
        curve = location.Curve
        if curve is not None:
            return curve.Evaluate(0.5, True)

    try:
        transform = family_instance.GetTransform()
        if transform is not None:
            return transform.Origin
    except Exception:
        pass

    bbox = family_instance.get_BoundingBox(None)
    if bbox is not None:
        points = bbox_model_corners(bbox)
        if points:
            return DB.XYZ(
                sum(point.X for point in points) / len(points),
                sum(point.Y for point in points) / len(points),
                sum(point.Z for point in points) / len(points)
            )

    return None


def get_family_local_axes(family_instance):
    local_x = None
    local_y = None

    try:
        local_x = normalize_xy(family_instance.HandOrientation)
    except Exception:
        pass

    try:
        local_y = normalize_xy(family_instance.FacingOrientation)
    except Exception:
        pass

    try:
        instance_transform = family_instance.GetTransform()
    except Exception:
        instance_transform = None

    if local_x is None and instance_transform is not None:
        local_x = normalize_xy(instance_transform.BasisX)

    if local_y is None and instance_transform is not None:
        local_y = normalize_xy(instance_transform.BasisY)

    if local_x is None:
        local_x = DB.XYZ.BasisX

    if local_y is None:
        local_y = DB.XYZ.BasisY

    if abs(local_x.DotProduct(local_y)) > 0.999:
        local_y = DB.XYZ(-local_x.Y, local_x.X, 0.0)

    return local_x, local_y


def transformed_point(transform, point):
    if transform is None:
        return point
    return transform.OfPoint(point)


def collect_geometry_points(geometry_element, accumulated_transform, points, depth):
    if geometry_element is None or depth > 12:
        return

    for geometry_object in geometry_element:
        try:
            if isinstance(geometry_object, DB.GeometryInstance):
                nested_transform = accumulated_transform.Multiply(
                    geometry_object.Transform
                )
                symbol_geometry = geometry_object.GetSymbolGeometry()
                collect_geometry_points(
                    symbol_geometry,
                    nested_transform,
                    points,
                    depth + 1
                )

            elif isinstance(geometry_object, DB.Solid):
                if geometry_object.Faces.Size == 0 or geometry_object.Edges.Size == 0:
                    continue

                for edge in geometry_object.Edges:
                    try:
                        for point in edge.Tessellate():
                            points.append(
                                transformed_point(accumulated_transform, point)
                            )
                    except Exception:
                        try:
                            curve = edge.AsCurve()
                            points.append(
                                transformed_point(
                                    accumulated_transform,
                                    curve.GetEndPoint(0)
                                )
                            )
                            points.append(
                                transformed_point(
                                    accumulated_transform,
                                    curve.GetEndPoint(1)
                                )
                            )
                        except Exception:
                            pass

            elif isinstance(geometry_object, DB.Mesh):
                try:
                    for point in geometry_object.Vertices:
                        points.append(
                            transformed_point(accumulated_transform, point)
                        )
                except Exception:
                    pass

        except Exception:
            continue


def get_solid_geometry_points(family_instance):
    options = DB.Options()
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = False
    options.DetailLevel = DB.ViewDetailLevel.Fine

    geometry = family_instance.get_Geometry(options)
    points = []

    collect_geometry_points(
        geometry,
        DB.Transform.Identity,
        points,
        0
    )

    return points


def get_bbox_points(family_instance):
    bbox = family_instance.get_BoundingBox(None)
    return bbox_model_corners(bbox)


def get_family_extents(family_instance, use_solid):
    origin = get_family_origin(family_instance)
    if origin is None:
        return None

    local_x, local_y = get_family_local_axes(family_instance)

    points = []
    used_solid = False

    if use_solid:
        try:
            points = get_solid_geometry_points(family_instance)
            used_solid = len(points) > 0
        except Exception:
            points = []

    if not points:
        points = get_bbox_points(family_instance)

    if not points:
        return None

    x_values = []
    y_values = []

    for point in points:
        relative = point - origin
        x_values.append(relative.DotProduct(local_x))
        y_values.append(relative.DotProduct(local_y))

    return {
        "origin": origin,
        "local_x": local_x,
        "local_y": local_y,
        "min_x": min(x_values),
        "max_x": max(x_values),
        "min_y": min(y_values),
        "max_y": max(y_values),
        "used_solid": used_solid
    }


def get_anchor_point(family_instance, anchor_mode, offset_x, offset_y, use_solid):
    extents = get_family_extents(family_instance, use_solid)
    if extents is None:
        return None, False

    mode = safe_text(anchor_mode).upper().strip()

    if mode.startswith("LEFT_"):
        x_value = extents["min_x"]
    elif mode.startswith("RIGHT_"):
        x_value = extents["max_x"]
    else:
        raise ValueError(u"Anchor mode không hợp lệ: {0}".format(mode))

    if mode.endswith("_FRONT"):
        y_value = extents["max_y"]
    elif mode.endswith("_BACK"):
        y_value = extents["min_y"]
    else:
        raise ValueError(u"Anchor mode không hợp lệ: {0}".format(mode))

    # Offset có dấu theo hệ trục cục bộ của Family.
    x_value += offset_x
    y_value += offset_y

    origin = extents["origin"]
    local_x = extents["local_x"]
    local_y = extents["local_y"]

    target = (
        origin
        + local_x.Multiply(x_value)
        + local_y.Multiply(y_value)
    )

    target = DB.XYZ(target.X, target.Y, origin.Z)
    return target, extents["used_solid"]


def get_rotation_direction(family_instance, rotation_basis):
    basis = safe_text(rotation_basis).upper().strip()

    if basis == "FACING":
        try:
            direction = normalize_xy(family_instance.FacingOrientation)
            if direction is not None:
                return direction
        except Exception:
            pass
    else:
        try:
            direction = normalize_xy(family_instance.HandOrientation)
            if direction is not None:
                return direction
        except Exception:
            pass

    local_x, local_y = get_family_local_axes(family_instance)
    if basis == "FACING":
        return local_y
    return local_x


# ============================================================
# KIỂM TRA GROUP TRÙNG
# ============================================================

def get_group_location(group):
    try:
        location = group.Location
        if isinstance(location, DB.LocationPoint):
            return location.Point
    except Exception:
        pass

    try:
        bbox = group.get_BoundingBox(None)
        if bbox is not None:
            points = bbox_model_corners(bbox)
            if points:
                return DB.XYZ(
                    sum(point.X for point in points) / len(points),
                    sum(point.Y for point in points) / len(points),
                    sum(point.Z for point in points) / len(points)
                )
    except Exception:
        pass

    return None


def collect_existing_group_points(group_type):
    result = []
    collector = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.Group)
        .WhereElementIsNotElementType()
    )

    target_type_id = get_id_value(group_type.Id)

    for group in collector:
        try:
            if get_id_value(group.GroupType.Id) != target_type_id:
                continue
            point = get_group_location(group)
            if point is not None:
                result.append(point)
        except Exception:
            pass

    return result


def has_nearby_point(target_point, existing_points, tolerance):
    if tolerance <= 0.0:
        return False

    for point in existing_points:
        try:
            if points_distance_3d(target_point, point) <= tolerance:
                return True
        except Exception:
            pass

    return False


# ============================================================
# GIAO DIỆN NGƯỜI DÙNG
# ============================================================

class SettingsWindow(forms.WPFWindow):
    def __init__(self, xaml_file, link_items, group_items, config, view):
        forms.WPFWindow.__init__(self, xaml_file)

        self.link_items = link_items
        self.group_items = group_items
        self.config = config
        self.host_view = view
        self.result = None

        self.cached_matched_items = None
        self.cached_scan_signature = None
        self.cached_scan_diagnostics = None
        self.cache_source = None

        self.picked_family_name = u""
        self.picked_type_name = u""
        self.picked_link_name = u""

        self.active_view_text.Text = u"Active View: {0}".format(
            get_element_name(view) or safe_text(view.ViewType)
        )

        for item in link_items:
            self.links_list.Items.Add(item.display_name)

        for item in group_items:
            self.group_combo.Items.Add(item.display_name)

        self.rotation_basis_combo.Items.Add(u"Xoay theo HandOrientation")
        self.rotation_basis_combo.Items.Add(u"Xoay theo FacingOrientation")

        self.restore_config()

        self.scan_button.Click += self.scan_clicked
        self.run_button.Click += self.run_clicked
        self.cancel_button.Click += self.cancel_clicked
        self.pick_family_button.Click += self.pick_family_clicked
        self.keyword_mode_radio.Checked += self.source_mode_changed
        self.pick_mode_radio.Checked += self.source_mode_changed
        self.pick_exact_type_checkbox.Checked += self.scan_setting_changed
        self.pick_exact_type_checkbox.Unchecked += self.scan_setting_changed
        self.filter_text.TextChanged += self.scan_setting_changed
        self.ignore_nested_checkbox.Checked += self.scan_setting_changed
        self.ignore_nested_checkbox.Unchecked += self.scan_setting_changed
        self.source_dedup_checkbox.Checked += self.scan_setting_changed
        self.source_dedup_checkbox.Unchecked += self.scan_setting_changed
        self.source_overlap_tolerance_text.TextChanged += self.scan_setting_changed
        self.test_one_checkbox.Checked += self.test_one_changed
        self.test_one_checkbox.Unchecked += self.test_one_changed

        self.update_source_mode_state()
        self.update_test_index_state()
        self.try_load_persistent_cache()

    def restore_config(self):
        last_group_uid = safe_text(getattr(self.config, "last_group_uid", u""))
        self.group_combo.SelectedIndex = self.find_uid_index(
            self.group_items,
            last_group_uid
        )

        self.filter_text.Text = safe_text(
            getattr(self.config, "family_filter", u"")
        )

        self.picked_family_name = safe_text(
            getattr(self.config, "picked_family_name", u"")
        ).strip()
        self.picked_type_name = safe_text(
            getattr(self.config, "picked_type_name", u"")
        ).strip()
        self.pick_exact_type_checkbox.IsChecked = bool(
            getattr(self.config, "pick_exact_type", False)
        )

        source_mode = safe_text(
            getattr(self.config, "source_filter_mode", u"KEYWORD")
        ).upper().strip()
        if source_mode == "PICK" and self.picked_family_name:
            self.pick_mode_radio.IsChecked = True
        else:
            self.keyword_mode_radio.IsChecked = True
        self.update_picked_family_text()

        self.offset_x_text.Text = safe_text(
            getattr(self.config, "offset_x_mm", u"0")
        )
        self.offset_y_text.Text = safe_text(
            getattr(self.config, "offset_y_mm", u"0")
        )
        self.rotation_offset_text.Text = safe_text(
            getattr(self.config, "rotation_offset_deg", u"0")
        )
        self.duplicate_tolerance_text.Text = safe_text(
            getattr(self.config, "duplicate_tolerance_mm", u"10")
        )
        self.source_overlap_tolerance_text.Text = safe_text(
            getattr(self.config, "source_overlap_tolerance_mm", u"10")
        )
        self.test_index_text.Text = safe_text(
            getattr(self.config, "test_family_index", u"1")
        )

        self.skip_duplicate_checkbox.IsChecked = bool(
            getattr(self.config, "skip_duplicate", True)
        )
        self.ignore_nested_checkbox.IsChecked = bool(
            getattr(self.config, "ignore_nested", True)
        )
        self.source_dedup_checkbox.IsChecked = bool(
            getattr(self.config, "source_dedup_enabled", True)
        )
        self.use_solid_checkbox.IsChecked = bool(
            getattr(self.config, "use_solid", True)
        )
        self.test_one_checkbox.IsChecked = bool(
            getattr(self.config, "test_one", True)
        )

        rotation_basis = safe_text(
            getattr(self.config, "rotation_basis", u"HAND")
        ).upper()
        if rotation_basis == "FACING":
            self.rotation_basis_combo.SelectedIndex = 1
        else:
            self.rotation_basis_combo.SelectedIndex = 0

        anchor_mode = safe_text(
            getattr(self.config, "anchor_mode", u"RIGHT_FRONT")
        ).upper()
        anchor_controls = {
            "LEFT_FRONT": self.left_front_radio,
            "RIGHT_FRONT": self.right_front_radio,
            "LEFT_BACK": self.left_back_radio,
            "RIGHT_BACK": self.right_back_radio
        }
        if anchor_mode not in anchor_controls:
            anchor_mode = "RIGHT_FRONT"
        anchor_controls[anchor_mode].IsChecked = True

    def find_uid_index(self, items, uid):
        if uid:
            for index, item in enumerate(items):
                if item.uid == uid:
                    return index
        return 0 if items else -1

    def get_selected_group_item(self):
        index = self.group_combo.SelectedIndex
        if index < 0 or index >= len(self.group_items):
            return None
        return self.group_items[index]

    def get_anchor_mode(self):
        if self.left_front_radio.IsChecked:
            return "LEFT_FRONT"
        if self.right_front_radio.IsChecked:
            return "RIGHT_FRONT"
        if self.left_back_radio.IsChecked:
            return "LEFT_BACK"
        if self.right_back_radio.IsChecked:
            return "RIGHT_BACK"
        return "RIGHT_FRONT"

    def get_source_mode(self):
        if self.pick_mode_radio.IsChecked is True:
            return "PICK"
        return "KEYWORD"

    def update_picked_family_text(self):
        if not self.picked_family_name:
            self.picked_family_text.Text = u"Chưa pick Family từ Link."
            return

        description = u"Family: {0}".format(self.picked_family_name)
        if self.picked_type_name:
            description += u"  |  Type: {0}".format(self.picked_type_name)
        if self.picked_link_name:
            description += u"  |  Link: {0}".format(self.picked_link_name)
        self.picked_family_text.Text = description

    def update_source_mode_state(self):
        pick_mode = self.get_source_mode() == "PICK"
        self.filter_text.IsEnabled = not pick_mode
        self.pick_family_button.IsEnabled = pick_mode
        self.pick_exact_type_checkbox.IsEnabled = pick_mode
        self.picked_family_text.IsEnabled = pick_mode

    def source_mode_changed(self, sender, args):
        self.update_source_mode_state()
        self.scan_setting_changed(sender, args)

    def pick_family_clicked(self, sender, args):
        try:
            with forms.WindowToggler(self):
                link_item, family_instance = pick_linked_family_instance(self.link_items)

            family_name = get_family_name(family_instance)
            type_name = get_family_type_name(family_instance)
            if not family_name:
                raise ValueError(u"Không đọc được Family Name của phần tử đã pick.")

            self.picked_family_name = family_name
            self.picked_type_name = type_name
            self.picked_link_name = link_item.display_name
            self.pick_mode_radio.IsChecked = True
            self.update_picked_family_text()
            self.invalidate_scan_cache()
            self.match_count_text.Text = (
                u"Đã pick Family '{0}'. Bấm Kiểm tra số lượng hoặc Bắt đầu."
            ).format(family_name)
            self.update_test_index_hint()
        except UI.Exceptions.OperationCanceledException:
            pass
        except Exception as ex:
            self.match_count_text.Text = u"Không thể dùng Family đã pick: {0}".format(
                safe_text(ex)
            )

    def get_source_overlap_tolerance_mm(self):
        return parse_number(
            self.source_overlap_tolerance_text.Text,
            u"Dung sai nhận diện nguồn chồng vị trí",
            0.0
        )

    def current_scan_signature(self):
        return make_scan_signature(
            self.link_items,
            self.host_view,
            safe_text(self.filter_text.Text),
            self.ignore_nested_checkbox.IsChecked is True,
            self.source_dedup_checkbox.IsChecked is True,
            safe_text(self.source_overlap_tolerance_text.Text),
            self.get_source_mode(),
            self.picked_family_name,
            self.picked_type_name,
            self.pick_exact_type_checkbox.IsChecked is True
        )

    def invalidate_scan_cache(self):
        self.cached_matched_items = None
        self.cached_scan_signature = None
        self.cached_scan_diagnostics = None
        self.cache_source = None

    def update_test_index_hint(self):
        count = 0
        if self.cached_matched_items is not None:
            count = len(self.cached_matched_items)
        if count > 0:
            self.test_index_hint.Text = u"Nhập từ 1 đến {0}".format(count)
        else:
            self.test_index_hint.Text = u"Nhập từ 1 đến tổng số Family đã kiểm tra"

    def try_load_persistent_cache(self):
        try:
            source_tolerance = mm_to_internal(
                self.get_source_overlap_tolerance_mm()
            )
        except Exception:
            self.invalidate_scan_cache()
            self.match_count_text.Text = u"Dung sai nguồn chồng vị trí chưa hợp lệ."
            self.update_test_index_hint()
            return False

        signature = self.current_scan_signature()
        items, message, diagnostics = load_persistent_scan_cache(
            self.config,
            signature,
            self.link_items,
            self.ignore_nested_checkbox.IsChecked is True,
            self.source_dedup_checkbox.IsChecked is True,
            source_tolerance
        )

        if items is not None:
            self.cached_matched_items = list(items)
            self.cached_scan_signature = signature
            self.cached_scan_diagnostics = diagnostics or new_scan_diagnostics()
            self.cache_source = "persistent"
            self.match_count_text.Text = (
                u"Đã nạp cache: {0} Family duy nhất. Không cần kiểm tra lại."
            ).format(len(items))
            self.update_test_index_hint()
            return True

        self.invalidate_scan_cache()
        if safe_text(getattr(self.config, "scan_cache_json", u"")).strip():
            self.match_count_text.Text = safe_text(message)
        else:
            self.match_count_text.Text = u"Chưa kiểm tra"
        self.update_test_index_hint()
        return False

    def scan_setting_changed(self, sender, args):
        if self.try_load_persistent_cache():
            return
        self.match_count_text.Text = (
            u"Thiết lập quét đã thay đổi. Bấm Kiểm tra để tạo cache mới, "
            u"hoặc bấm Bắt đầu để tool tự quét một lần."
        )

    def test_one_changed(self, sender, args):
        self.update_test_index_state()

    def update_test_index_state(self):
        enabled = self.test_one_checkbox.IsChecked is True
        self.test_index_text.IsEnabled = enabled
        self.test_index_label.IsEnabled = enabled
        self.test_index_hint.IsEnabled = enabled
        self.update_test_index_hint()

    def scan_clicked(self, sender, args):
        if self.get_source_mode() == "PICK" and not self.picked_family_name:
            self.match_count_text.Text = u"Chế độ Pick Family: hãy bấm 'Pick Family từ Link' trước."
            return

        try:
            raw_filter_text = safe_text(self.filter_text.Text)
            keywords = split_keywords(raw_filter_text)
            source_tolerance_mm = self.get_source_overlap_tolerance_mm()
            matched, cancelled, diagnostics = collect_visible_matching_work_items(
                self.link_items,
                self.host_view,
                keywords,
                self.ignore_nested_checkbox.IsChecked is True,
                self.source_dedup_checkbox.IsChecked is True,
                mm_to_internal(source_tolerance_mm),
                True,
                self.get_source_mode(),
                self.picked_family_name,
                self.picked_type_name,
                self.pick_exact_type_checkbox.IsChecked is True
            )

            if cancelled:
                self.match_count_text.Text = (
                    u"Đã hủy kiểm tra. Cache hợp lệ trước đó vẫn được giữ nếu có."
                )
                return

            signature = self.current_scan_signature()
            self.cached_matched_items = list(matched)
            self.cached_scan_signature = signature
            self.cached_scan_diagnostics = diagnostics
            self.cache_source = "fresh_scan"

            save_persistent_scan_cache(
                self.config,
                signature,
                matched,
                raw_filter_text,
                diagnostics
            )

            self.match_count_text.Text = (
                u"Tìm thấy {0} Family duy nhất; bỏ nested {1}, nguồn trùng {2}. Cache đã lưu."
            ).format(
                len(matched),
                diagnostics.get("nested_skipped", 0),
                diagnostics.get("exact_source_duplicate_skipped", 0)
                + diagnostics.get("overlap_source_skipped", 0)
            )
            self.update_test_index_hint()
        except Exception as ex:
            self.invalidate_scan_cache()
            self.match_count_text.Text = u"Lỗi quét: {0}".format(safe_text(ex))

    def run_clicked(self, sender, args):
        group_item = self.get_selected_group_item()
        if group_item is None:
            self.match_count_text.Text = u"Hãy chọn Model Group Type cần đặt."
            return

        if self.get_source_mode() == "PICK" and not self.picked_family_name:
            self.match_count_text.Text = u"Chế độ Pick Family: hãy bấm 'Pick Family từ Link' trước."
            return

        try:
            offset_x_mm = parse_number(self.offset_x_text.Text, u"Offset X")
            offset_y_mm = parse_number(self.offset_y_text.Text, u"Offset Y")
            rotation_offset_deg = parse_number(
                self.rotation_offset_text.Text,
                u"Hiệu chỉnh góc"
            )
            duplicate_tolerance_mm = parse_number(
                self.duplicate_tolerance_text.Text,
                u"Dung sai kiểm tra Group trùng",
                0.0
            )
            source_overlap_tolerance_mm = self.get_source_overlap_tolerance_mm()

            test_one = self.test_one_checkbox.IsChecked is True
            test_family_index = 1
            if test_one:
                maximum = None
                if self.cached_matched_items is not None:
                    maximum = len(self.cached_matched_items)
                test_family_index = parse_integer(
                    self.test_index_text.Text,
                    u"Vị trí Family tạo thử",
                    1,
                    maximum
                )
        except ValueError as ex:
            self.match_count_text.Text = safe_text(ex)
            return

        rotation_basis = "HAND"
        if self.rotation_basis_combo.SelectedIndex == 1:
            rotation_basis = "FACING"

        raw_filter_text = safe_text(self.filter_text.Text)
        current_scan_signature = self.current_scan_signature()

        cached_items = None
        cached_diagnostics = None
        cache_used = False
        cache_source = None
        if (
            self.cached_matched_items is not None and
            self.cached_scan_signature == current_scan_signature
        ):
            cached_items = list(self.cached_matched_items)
            cached_diagnostics = dict(
                self.cached_scan_diagnostics or new_scan_diagnostics()
            )
            cache_used = True
            cache_source = self.cache_source

        self.result = {
            "config": self.config,
            "link_items": self.link_items,
            "group_item": group_item,
            "filter_text": raw_filter_text,
            "source_filter_mode": self.get_source_mode(),
            "picked_family_name": self.picked_family_name,
            "picked_type_name": self.picked_type_name,
            "pick_exact_type": self.pick_exact_type_checkbox.IsChecked is True,
            "scan_signature": current_scan_signature,
            "cached_matched_items": cached_items,
            "cached_scan_diagnostics": cached_diagnostics,
            "cache_used": cache_used,
            "cache_source": cache_source,
            "anchor_mode": self.get_anchor_mode(),
            "offset_x_mm": offset_x_mm,
            "offset_y_mm": offset_y_mm,
            "rotation_offset_deg": rotation_offset_deg,
            "rotation_basis": rotation_basis,
            "ignore_nested": self.ignore_nested_checkbox.IsChecked is True,
            "source_dedup_enabled": self.source_dedup_checkbox.IsChecked is True,
            "source_overlap_tolerance_mm": source_overlap_tolerance_mm,
            "skip_duplicate": self.skip_duplicate_checkbox.IsChecked is True,
            "duplicate_tolerance_mm": duplicate_tolerance_mm,
            "use_solid": self.use_solid_checkbox.IsChecked is True,
            "test_one": test_one,
            "test_family_index": test_family_index
        }
        self.Close()

    def cancel_clicked(self, sender, args):
        self.result = None
        self.Close()


# ============================================================
# MAIN PROCESS
# ============================================================

def save_user_config(config, settings):
    config.last_group_uid = settings["group_item"].uid
    config.family_filter = settings["filter_text"]
    config.source_filter_mode = settings.get("source_filter_mode", "KEYWORD")
    config.picked_family_name = settings.get("picked_family_name", u"")
    config.picked_type_name = settings.get("picked_type_name", u"")
    config.pick_exact_type = settings.get("pick_exact_type", False)
    config.anchor_mode = settings["anchor_mode"]
    config.offset_x_mm = safe_text(settings["offset_x_mm"])
    config.offset_y_mm = safe_text(settings["offset_y_mm"])
    config.rotation_offset_deg = safe_text(settings["rotation_offset_deg"])
    config.rotation_basis = settings["rotation_basis"]
    config.ignore_nested = settings["ignore_nested"]
    config.source_dedup_enabled = settings["source_dedup_enabled"]
    config.source_overlap_tolerance_mm = safe_text(
        settings["source_overlap_tolerance_mm"]
    )
    config.skip_duplicate = settings["skip_duplicate"]
    config.duplicate_tolerance_mm = safe_text(
        settings["duplicate_tolerance_mm"]
    )
    config.use_solid = settings["use_solid"]
    config.test_one = settings["test_one"]
    config.test_family_index = safe_text(settings["test_family_index"])
    script.save_config()


def defensively_normalize_work_items(
        work_items,
        ignore_nested,
        source_dedup_enabled,
        source_overlap_tolerance_internal):
    """Lớp bảo vệ cuối trước transaction, kể cả khi cache cũ hoặc dữ liệu lỗi."""
    diagnostics = new_scan_diagnostics()
    normalized = []
    used_source_keys = set()

    for work_item in work_items or []:
        family_instance = work_item.family

        if ignore_nested and get_super_component(family_instance) is not None:
            diagnostics["nested_skipped"] += 1
            continue

        source_key = family_source_key(work_item.link_item, family_instance)
        if source_key in used_source_keys:
            diagnostics["exact_source_duplicate_skipped"] += 1
            continue

        used_source_keys.add(source_key)
        normalized.append(work_item)

    normalized.sort(
        key=lambda item: (
            item.link_item.display_name.lower(),
            get_family_id_value(item.family)
        )
    )

    if source_dedup_enabled:
        normalized, overlap_records = deduplicate_overlapping_sources(
            normalized,
            source_overlap_tolerance_internal
        )
        diagnostics["overlap_source_skipped"] = len(overlap_records)
        diagnostics["duplicate_source_records"] = list(
            diagnostics.get("duplicate_source_records", [])
        ) + overlap_records

    diagnostics["final_count"] = len(normalized)
    return normalized, diagnostics


def run_tool(settings, initial_notices=None):
    group_type = settings["group_item"].element
    keywords = split_keywords(settings["filter_text"])
    source_mode = safe_text(settings.get("source_filter_mode", "KEYWORD")).upper().strip()
    picked_family_name = safe_text(settings.get("picked_family_name", u"")).strip()
    picked_type_name = safe_text(settings.get("picked_type_name", u"")).strip()
    pick_exact_type = settings.get("pick_exact_type", False) is True
    report_notices = list(initial_notices or [])

    source_overlap_tolerance = mm_to_internal(
        settings["source_overlap_tolerance_mm"]
    )

    context_lines = [
        u"Active View: {0}".format(
            get_element_name(active_view) or safe_text(active_view.ViewType)
        ),
        u"Revit Link được xử lý: {0}".format(len(settings["link_items"])),
        u"Model Group Type: {0}".format(settings["group_item"].display_name),
        u"Điểm đặt: {0}".format(settings["anchor_mode"]),
        u"Offset X / Y có dấu: {0:+.1f} / {1:+.1f} mm".format(
            settings["offset_x_mm"],
            settings["offset_y_mm"]
        ),
        u"Hiệu chỉnh góc: {0:.2f}°; cơ sở xoay: {1}".format(
            settings["rotation_offset_deg"],
            settings["rotation_basis"]
        ),
        u"Bỏ nested Family: {0}".format(
            u"Có" if settings["ignore_nested"] else u"Không"
        ),
        u"Loại nguồn chồng vị trí: {0}; dung sai {1:.2f} mm".format(
            u"Có" if settings["source_dedup_enabled"] else u"Không",
            settings["source_overlap_tolerance_mm"]
        )
    ]

    filter_display = safe_text(settings["filter_text"]).strip()
    if source_mode == "PICK":
        pick_description = u"Family pick chính xác: {0}".format(
            picked_family_name or u"(chưa xác định)"
        )
        if pick_exact_type:
            pick_description += u" | Type chính xác: {0}".format(
                picked_type_name or u"(không đọc được)"
            )
        else:
            pick_description += u" | Type: tất cả Type của Family này"
        context_lines.append(pick_description)
    elif filter_display:
        context_lines.append(
            u"Từ khóa lọc: {0}".format(
                filter_display.replace(u"\r", u" ").replace(u"\n", u"; ")
            )
        )
    else:
        context_lines.append(u"Từ khóa lọc: để trống, lấy tất cả Family phù hợp")

    matched_items = settings.get("cached_matched_items")
    scan_diagnostics = settings.get("cached_scan_diagnostics")
    scan_cancelled = False

    expected_signature = make_scan_signature(
        settings["link_items"],
        active_view,
        settings["filter_text"],
        settings["ignore_nested"],
        settings["source_dedup_enabled"],
        safe_text(settings["source_overlap_tolerance_mm"]),
        source_mode,
        picked_family_name,
        picked_type_name,
        pick_exact_type
    )

    cache_is_valid = (
        matched_items is not None and
        settings.get("scan_signature") == expected_signature
    )

    if cache_is_valid:
        matched_items = list(matched_items)
        scan_diagnostics = dict(scan_diagnostics or new_scan_diagnostics())
        if settings.get("cache_source") == "persistent":
            report_notices.append(
                u"Đã dùng cache bền từ lần chạy trước; không quét lại Family."
            )
        else:
            report_notices.append(
                u"Đã dùng cache vừa kiểm tra; không quét lại Family."
            )
    else:
        matched_items, scan_cancelled, scan_diagnostics = (
            collect_visible_matching_work_items(
                settings["link_items"],
                active_view,
                keywords,
                settings["ignore_nested"],
                settings["source_dedup_enabled"],
                source_overlap_tolerance,
                True,
                source_mode,
                picked_family_name,
                picked_type_name,
                pick_exact_type
            )
        )
        report_notices.append(
            u"Không có cache hợp lệ nên tool đã quét một lần; progress chỉ đếm các Family đã khớp bộ lọc nguồn."
        )

    if scan_cancelled:
        show_output_report(
            u"Đã hủy khi quét Family",
            summary_lines=context_lines + [u"Chưa có Group nào được tạo."],
            notices=report_notices
        )
        return

    # Lớp bảo vệ cuối: cache và danh sách quét đều phải qua chuẩn hóa lần nữa.
    matched_items, defensive_diagnostics = defensively_normalize_work_items(
        matched_items,
        settings["ignore_nested"],
        settings["source_dedup_enabled"],
        source_overlap_tolerance
    )

    if scan_diagnostics is None:
        scan_diagnostics = new_scan_diagnostics()

    # Chỉ cộng các loại trùng phát sinh trong lớp bảo vệ cuối; không cộng final_count.
    scan_diagnostics = dict(scan_diagnostics)
    scan_diagnostics["nested_skipped"] = int(
        scan_diagnostics.get("nested_skipped", 0)
    ) + int(defensive_diagnostics.get("nested_skipped", 0))
    scan_diagnostics["exact_source_duplicate_skipped"] = int(
        scan_diagnostics.get("exact_source_duplicate_skipped", 0)
    ) + int(defensive_diagnostics.get("exact_source_duplicate_skipped", 0))
    scan_diagnostics["overlap_source_skipped"] = int(
        scan_diagnostics.get("overlap_source_skipped", 0)
    ) + int(defensive_diagnostics.get("overlap_source_skipped", 0))
    scan_diagnostics["duplicate_source_records"] = list(
        scan_diagnostics.get("duplicate_source_records", [])
    ) + list(defensive_diagnostics.get("duplicate_source_records", []))
    scan_diagnostics["final_count"] = len(matched_items)

    if not cache_is_valid:
        try:
            save_persistent_scan_cache(
                settings["config"],
                expected_signature,
                matched_items,
                settings["filter_text"],
                scan_diagnostics
            )
            report_notices.append(
                u"Kết quả quét đã được chuẩn hóa và lưu cache cho các lần chạy sau."
            )
        except Exception as cache_error:
            report_notices.append(
                u"Không lưu được cache bền: {0}".format(safe_text(cache_error))
            )

    duplicate_source_records = list(
        scan_diagnostics.get("duplicate_source_records", [])
    )
    if len(duplicate_source_records) > 100:
        report_notices.append(
            u"Có {0} nguồn trùng; bảng chi tiết chỉ hiển thị 100 dòng đầu.".format(
                len(duplicate_source_records)
            )
        )
        duplicate_source_records = duplicate_source_records[:100]

    if not matched_items:
        show_output_report(
            u"Không tìm thấy Family nguồn duy nhất phù hợp",
            summary_lines=context_lines + scan_diagnostics_summary(scan_diagnostics) + [
                u"Group đã tạo: 0"
            ],
            notices=report_notices + [
                u"Kiểm tra bộ lọc nguồn (từ khóa hoặc Family đã pick), nested Family, "
                u"dung sai nguồn chồng vị trí, Crop/Section Box, View Range và Visibility/Graphics của Revit Link."
            ],
            duplicate_source_records=duplicate_source_records
        )
        return

    work_items_to_process = list(matched_items)
    if settings["test_one"]:
        test_index = int(settings.get("test_family_index", 1))
        if test_index < 1 or test_index > len(matched_items):
            show_output_report(
                u"Vị trí Family tạo thử không hợp lệ",
                summary_lines=context_lines + scan_diagnostics_summary(scan_diagnostics) + [
                    u"Vị trí yêu cầu: {0}. Hãy nhập từ 1 đến {1}.".format(
                        test_index,
                        len(matched_items)
                    )
                ],
                notices=report_notices,
                duplicate_source_records=duplicate_source_records
            )
            return

        selected_work_item = matched_items[test_index - 1]
        work_items_to_process = [selected_work_item]
        report_notices.append(
            u"Chế độ thử đang dùng Family thứ {0}/{1}: {2} | {3}.".format(
                test_index,
                len(matched_items),
                selected_work_item.link_item.display_name,
                describe_family(selected_work_item.family)
            )
        )

    offset_x = mm_to_internal(settings["offset_x_mm"])
    offset_y = mm_to_internal(settings["offset_y_mm"])
    duplicate_tolerance = mm_to_internal(settings["duplicate_tolerance_mm"])
    rotation_offset = math.radians(settings["rotation_offset_deg"])

    if settings["skip_duplicate"]:
        existing_group_points = collect_existing_group_points(group_type)
    else:
        existing_group_points = []

    created_count = 0
    existing_group_duplicate_count = 0
    fallback_bbox_count = 0
    mirrored_count = 0
    failed_items = []
    created_records = []
    cancelled = False

    transaction_name = u"Test Place One Group From Linked Family"
    if not settings["test_one"]:
        transaction_name = u"Place Groups From Linked Families"

    transaction = DB.Transaction(doc, transaction_name)

    try:
        transaction.Start()
        total = len(work_items_to_process)

        with forms.ProgressBar(
            title=u"Đang đặt Group... {value} / {max_value}",
            cancellable=True,
            step=1
        ) as progress:

            for index, work_item in enumerate(work_items_to_process):
                if progress.cancelled:
                    cancelled = True
                    break

                family_instance = work_item.family
                link_item = work_item.link_item
                sub_transaction = DB.SubTransaction(doc)
                sub_transaction.Start()

                try:
                    link_anchor, used_solid = get_anchor_point(
                        family_instance,
                        settings["anchor_mode"],
                        offset_x,
                        offset_y,
                        settings["use_solid"]
                    )

                    if link_anchor is None:
                        raise Exception(u"Không xác định được điểm đặt của Family.")

                    if settings["use_solid"] and not used_solid:
                        fallback_bbox_count += 1

                    host_anchor = link_item.transform.OfPoint(link_anchor)

                    if settings["skip_duplicate"] and has_nearby_point(
                        host_anchor,
                        existing_group_points,
                        duplicate_tolerance
                    ):
                        existing_group_duplicate_count += 1
                        sub_transaction.RollBack()
                    else:
                        link_direction = get_rotation_direction(
                            family_instance,
                            settings["rotation_basis"]
                        )
                        host_direction = link_item.transform.OfVector(link_direction)
                        angle = vector_angle_xy(host_direction) + rotation_offset

                        new_group = doc.Create.PlaceGroup(host_anchor, group_type)
                        if new_group is None:
                            raise Exception(u"Revit không tạo được Group.")

                        if abs(angle) > 0.0000001:
                            rotation_axis = DB.Line.CreateBound(
                                host_anchor,
                                host_anchor + DB.XYZ.BasisZ
                            )
                            DB.ElementTransformUtils.RotateElement(
                                doc,
                                new_group.Id,
                                rotation_axis,
                                angle
                            )

                        is_mirrored = False
                        try:
                            is_mirrored = family_instance.Mirrored is True
                            if is_mirrored:
                                mirrored_count += 1
                        except Exception:
                            pass

                        source_point = work_item.get_source_point()
                        group_id = new_group.Id
                        group_id_value = get_id_value(group_id)

                        sub_transaction.Commit()
                        existing_group_points.append(host_anchor)
                        created_count += 1
                        created_records.append({
                            "group_id": group_id,
                            "group_id_value": group_id_value,
                            "group_type_name": settings["group_item"].display_name,
                            "source_family": describe_family(family_instance),
                            "link_name": link_item.display_name,
                            "link_instance_id": get_id_value(link_item.element.Id),
                            "family_id": get_family_id_value(family_instance),
                            "super_component_id": (
                                get_super_component_id_value(family_instance)
                                if get_super_component_id_value(family_instance) is not None
                                else u"-"
                            ),
                            "source_point": source_point,
                            "point": host_anchor,
                            "angle_deg": format_angle_degrees(angle),
                            "anchor_mode": settings["anchor_mode"],
                            "used_solid": used_solid,
                            "mirrored": is_mirrored
                        })

                except Exception as item_error:
                    try:
                        if sub_transaction.GetStatus() == DB.TransactionStatus.Started:
                            sub_transaction.RollBack()
                    except Exception:
                        pass

                    failed_items.append(
                        u"{0} | {1}: {2}".format(
                            link_item.display_name,
                            describe_family(family_instance),
                            safe_text(item_error)
                        )
                    )

                progress.update_progress(index + 1, total)

            if progress.cancelled:
                cancelled = True

        if cancelled:
            transaction.RollBack()
            show_output_report(
                u"Đã hủy thao tác đặt Group",
                summary_lines=context_lines + scan_diagnostics_summary(scan_diagnostics) + [
                    u"Toàn bộ Group của lần chạy đã được hoàn tác."
                ],
                notices=report_notices,
                failed_items=failed_items,
                duplicate_source_records=duplicate_source_records
            )
            return

        transaction.Commit()

    except Exception:
        try:
            if transaction.GetStatus() == DB.TransactionStatus.Started:
                transaction.RollBack()
        except Exception:
            pass

        show_output_report(
            u"Tool không thể hoàn thành",
            summary_lines=context_lines + scan_diagnostics_summary(scan_diagnostics) + [
                u"Toàn bộ thay đổi của lần chạy đã được hoàn tác."
            ],
            notices=report_notices,
            failed_items=failed_items,
            duplicate_source_records=duplicate_source_records,
            traceback_text=traceback.format_exc()
        )
        return

    result_lines = context_lines + scan_diagnostics_summary(scan_diagnostics) + [
        u"Group đã tạo: {0}".format(created_count),
        u"Bỏ qua vì Group cùng Type đã tồn tại: {0}".format(
            existing_group_duplicate_count
        ),
        u"Không xử lý được: {0}".format(len(failed_items))
    ]

    if settings["test_one"]:
        if created_count == 1:
            status_title = u"Đã tạo 1 Group kiểm tra"
            report_notices.extend([
                u"Đã tạo đúng Family thứ {0} trong danh sách nguồn duy nhất.".format(
                    settings.get("test_family_index", 1)
                ),
                u"Kiểm tra vị trí và góc xoay bằng liên kết Zoom trong bảng bên dưới.",
                u"Khi kết quả đúng, chạy lại tool và bỏ chọn chế độ tạo thử.",
                u"Giữ bật kiểm tra Group trùng để lần chạy hàng loạt bỏ qua Group thử."
            ])
        else:
            status_title = u"Không tạo được Group kiểm tra"
    else:
        status_title = u"Đã hoàn thành tạo Group hàng loạt"

    if fallback_bbox_count:
        report_notices.append(
            u"Có {0} Family không đọc được Solid và đã dùng BoundingBox dự phòng.".format(
                fallback_bbox_count
            )
        )

    if mirrored_count:
        report_notices.append(
            u"Có {0} Family nguồn bị Mirror. Tool đã xoay Group nhưng chưa mirror hình học Group.".format(
                mirrored_count
            )
        )

    show_output_report(
        status_title,
        summary_lines=result_lines,
        notices=report_notices,
        failed_items=failed_items,
        created_records=created_records,
        duplicate_source_records=duplicate_source_records
    )


# ============================================================
# KHỞI CHẠY
# ============================================================

if doc is None:
    show_output_report(
        u"Không thể chạy tool",
        summary_lines=[u"Không có Revit document đang mở."]
    )
    script.exit()

if doc.IsFamilyDocument:
    show_output_report(
        u"Không thể chạy tool",
        summary_lines=[
            u"Tool chỉ chạy trong Project Document, không chạy trong Family Editor."
        ]
    )
    script.exit()

if active_view is None or active_view.IsTemplate:
    show_output_report(
        u"Không thể chạy tool",
        summary_lines=[
            u"Hãy mở một model view hợp lệ trước khi chạy tool."
        ]
    )
    script.exit()

selected_links = pick_link_items()
if not selected_links:
    script.exit()

selected_links, hidden_links = keep_only_links_visible_in_active_view(
    selected_links,
    active_view
)

if not selected_links:
    hidden_names = u"; ".join(
        [item.display_name for item in hidden_links]
    )
    show_output_report(
        u"Không có Revit Link hợp lệ",
        summary_lines=[
            u"Các Revit Link đã pick không hiển thị trong Active View.",
            u"Link đã pick: {0}".format(hidden_names or u"Không xác định")
        ],
        notices=[
            u"Bật Revit Link trong Visibility/Graphics hoặc chọn Link khác."
        ]
    )
    script.exit()

if hidden_links:
    hidden_names = u"; ".join(
        [item.display_name for item in hidden_links]
    )
    startup_notices.append(
        u"Các Link không hiển thị trong Active View đã bị bỏ qua: {0}".format(
            hidden_names
        )
    )

group_types = collect_model_group_types()
if not group_types:
    show_output_report(
        u"Không có Model Group Type",
        summary_lines=[
            u"Model hiện tại chưa có Model Group Type."
        ],
        notices=[
            u"Tạo hoặc load Model Group trước khi chạy tool."
        ]
    )
    script.exit()

config = script.get_config()
xaml_path = script.get_bundle_file("ui.xaml")

if not xaml_path or not os.path.exists(xaml_path):
    show_output_report(
        u"Thiếu file giao diện",
        summary_lines=[
            u"Không tìm thấy file ui.xaml trong thư mục pushbutton."
        ]
    )
    script.exit()

window = SettingsWindow(
    xaml_path,
    selected_links,
    group_types,
    config,
    active_view
)
window.ShowDialog()

if window.result is None:
    script.exit()

save_user_config(config, window.result)
run_tool(window.result, startup_notices)

