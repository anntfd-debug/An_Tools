# -*- coding: utf-8 -*-
import os
import sys
import re
import json
import math

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Xaml")

from pyrevit import forms, script
from Autodesk.Revit.DB import *
from Autodesk.Revit.DB.Structure import StructuralType
try:
    from Autodesk.Revit.DB.Plumbing import PipeInsulation
except Exception:
    PipeInsulation = None
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
import System
from System.Collections.Generic import List
from System.Windows.Markup import XamlReader


doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
logger = script.get_logger()
output = script.get_output()


# ==============================================================================
# CẤU HÌNH FAMILY SLEEVE VÀ PARAMETER CHIỀU DÀY
# ==============================================================================
FAMILY_CONFIG = {
    "GEN_PENO_Circular_Castin": "NWCH_PEN_Thickness",
    "GEN_PENO_Square_Castin": "NWCH_PEN_Thickness",
    "Your_Other_Sleeve_Family_Name_1": "Sleeve_Thickness",
    "Your_Other_Sleeve_Family_Name_2": "Thickness"
}

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")
MAX_NESTED_DEPTH = 8

# Góc nhỏ nhất giữa trục MEP và BỀ MẶT Wall/Floor/Structural Framing để được xem là xuyên thật.
# 0°  = chạy song song bề mặt -> bỏ qua.
# 90° = xuyên vuông góc bề mặt -> chấp nhận.
# Mặc định 30°: vẫn cho phép ống xuyên xiên, nhưng loại các trường hợp gần song song.
MIN_PENETRATION_ANGLE_TO_SURFACE_DEG = 30.0

# Bỏ các đoạn giao cực ngắn do tiếp tuyến/sai số hình học.
MIN_INTERSECTION_LENGTH_FT = 2.0 / 304.8  # 2 mm
ANGLE_EPSILON = 1.0e-9

# So sánh size theo 0.1 mm để loại sai số số thực do đơn vị nội bộ của Revit.
# Đây vẫn là khớp bằng nhau, không còn dùng dung sai ±5 mm như bản cũ.
SIZE_KEY_DECIMALS = 1


# ==============================================================================
# TIỆN ÍCH CHUNG
# ==============================================================================
def get_element_id_value(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        try:
            return int(element_id.IntegerValue)
        except Exception:
            try:
                return int(element_id)
            except Exception:
                return -1


def make_element_id(value):
    try:
        return ElementId(int(value))
    except Exception:
        return None


def safe_text(value):
    if value is None:
        return ""
    try:
        return unicode(value)
    except Exception:
        try:
            return str(value)
        except Exception:
            return ""


def safe_markdown_text(value):
    return safe_text(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def get_category_name(elem):
    try:
        return elem.Category.Name if elem and elem.Category else ""
    except Exception:
        return ""


def extract_max_size(text):
    if not text:
        return None
    normalized_text = safe_text(text).replace(",", ".")
    numbers = re.findall(r'\d+(?:\.\d+)?', normalized_text)
    if not numbers:
        return None
    return max(float(n) for n in numbers)


def extract_single_number(text):
    if not text:
        return None
    normalized_text = safe_text(text).replace(",", ".")
    match = re.search(r'\d+(?:\.\d+)?', normalized_text)
    if match:
        return float(match.group())
    return None


def internal_length_to_mm(value):
    if value is None:
        return None
    try:
        return UnitUtils.ConvertFromInternalUnits(
            float(value),
            UnitTypeId.Millimeters
        )
    except Exception:
        try:
            return UnitUtils.ConvertFromInternalUnits(
                float(value),
                DisplayUnitType.DUT_MILLIMETERS
            )
        except Exception:
            try:
                return float(value) * 304.8
            except Exception:
                return None


def normalize_size_mm(value):
    if value is None:
        return None
    try:
        return round(float(value), SIZE_KEY_DECIMALS)
    except Exception:
        return None


def format_size_mm(value):
    normalized = normalize_size_mm(value)
    if normalized is None:
        return ""
    if abs(normalized - round(normalized)) < 1.0e-6:
        return str(int(round(normalized)))
    text = ("{:.%df}" % SIZE_KEY_DECIMALS).format(normalized)
    return text.rstrip("0").rstrip(".")


def get_family_name(symbol):
    try:
        return symbol.Family.Name
    except Exception:
        try:
            parameter = symbol.Family.LookupParameter("Family Name")
            return parameter.AsString() if parameter else "UnknownFamily"
        except Exception:
            return "UnknownFamily"


def get_symbol_display_name(symbol):
    family_name = get_family_name(symbol)
    try:
        parameter = symbol.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        if parameter:
            type_name = parameter.AsString()
        else:
            parameter = symbol.LookupParameter("Type Name")
            type_name = parameter.AsString() if parameter else "UnknownType"
    except Exception:
        type_name = "UnknownType"
    return "{} : {}".format(family_name, type_name)


def get_link_instance_name(link_instance):
    """Giữ nguyên tên đầy đủ của instance, không cắt tại '.rvt'."""
    try:
        name = link_instance.Name
        if name:
            return safe_text(name)
    except Exception:
        pass

    try:
        link_type = link_instance.Document.GetElement(link_instance.GetTypeId())
        if link_type:
            return safe_text(link_type.Name)
    except Exception:
        pass

    return "Revit Link ID {}".format(get_element_id_value(link_instance.Id))


def get_document_title(link_doc):
    try:
        return safe_text(link_doc.Title)
    except Exception:
        return ""


# ==============================================================================
# LƯU / TẢI CẤU HÌNH
# ==============================================================================
def default_settings():
    return {
        "mapping": [],
        "root_link_ids": [],
        "scan_wall": True,
        "scan_floor": True,
        "scan_beam": False,
        "replace_mode": False
    }


def save_settings(state):
    data = default_settings()
    data.update(state or {})

    # Chỉ ghi dữ liệu JSON đơn giản, không ghi object Revit/WPF.
    clean_data = {
        "mapping": data.get("mapping", []),
        "root_link_ids": [int(x) for x in data.get("root_link_ids", [])],
        "scan_wall": bool(data.get("scan_wall", True)),
        "scan_floor": bool(data.get("scan_floor", True)),
        "scan_beam": bool(data.get("scan_beam", False)),
        "replace_mode": bool(data.get("replace_mode", False))
    }

    try:
        with open(SETTINGS_FILE, "w") as stream:
            json.dump(clean_data, stream, indent=2)
    except Exception as ex:
        logger.warning("Không thể lưu settings.json: {}".format(ex))


def load_settings():
    data = default_settings()
    if not os.path.exists(SETTINGS_FILE):
        return data

    try:
        with open(SETTINGS_FILE, "r") as stream:
            loaded = json.load(stream)
        if isinstance(loaded, dict):
            data.update(loaded)
    except Exception as ex:
        logger.warning("Không thể đọc settings.json: {}".format(ex))

    # Tương thích settings cũ có key 'links'. Không thể khôi phục chắc chắn theo tên,
    # nên chỉ giữ mapping và dùng danh sách ID mới khi người dùng pick lại.
    if "root_link_ids" not in data:
        data["root_link_ids"] = []

    return data


# ==============================================================================
# BỘ LỌC PICK
# ==============================================================================
class PipeSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        try:
            if not elem.Category:
                return False
            category_id = get_element_id_value(elem.Category.Id)
            valid_categories = [
                int(BuiltInCategory.OST_PipeCurves),
                int(BuiltInCategory.OST_PipeAccessory),
                int(BuiltInCategory.OST_PipeFitting),
                int(BuiltInCategory.OST_PlumbingFixtures)
            ]
            return category_id in valid_categories
        except Exception:
            return False

    def AllowReference(self, reference, position):
        return False


class RevitLinkSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, RevitLinkInstance)

    def AllowReference(self, reference, position):
        return False


# ==============================================================================
# CÂY LINK / NESTED LINK
# ==============================================================================
class LinkTarget(object):
    def __init__(self, root_instance, chain, target_doc, transform_to_host, path_names):
        self.root_instance = root_instance
        self.chain = list(chain)
        self.doc = target_doc
        self.transform = transform_to_host
        self.path_names = list(path_names)
        self.path_name = "  >  ".join(self.path_names)
        self.depth = len(self.chain) - 1

    @property
    def root_id(self):
        return get_element_id_value(self.root_instance.Id)

    @property
    def target_instance(self):
        return self.chain[-1]

    @property
    def target_instance_id(self):
        return get_element_id_value(self.target_instance.Id)


def collect_nested_link_targets(root_instance, max_depth=MAX_NESTED_DEPTH):
    """Thu thập link gốc và nested link, giữ transform/path cho từng instance."""
    targets = []
    root_doc = root_instance.GetLinkDocument()
    if not root_doc:
        return targets

    root_transform = root_instance.GetTotalTransform()
    root_name = get_link_instance_name(root_instance)

    def recurse(current_instance, current_doc, transform_to_host, chain, path_names, depth, ancestry_docs):
        targets.append(LinkTarget(
            root_instance=root_instance,
            chain=chain,
            target_doc=current_doc,
            transform_to_host=transform_to_host,
            path_names=path_names
        ))

        if depth >= max_depth:
            return

        try:
            nested_instances = list(
                FilteredElementCollector(current_doc)
                .OfClass(RevitLinkInstance)
                .WhereElementIsNotElementType()
                .ToElements()
            )
        except Exception:
            nested_instances = []

        for nested_instance in nested_instances:
            try:
                nested_doc = nested_instance.GetLinkDocument()
            except Exception:
                nested_doc = None

            if not nested_doc:
                continue

            # Tránh vòng lặp attachment bất thường A > B > A.
            nested_doc_key = None
            try:
                nested_doc_key = safe_text(nested_doc.PathName) or safe_text(nested_doc.Title)
            except Exception:
                nested_doc_key = safe_text(nested_doc.Title)

            if nested_doc_key in ancestry_docs:
                continue

            try:
                nested_transform = nested_instance.GetTotalTransform()
                combined_transform = transform_to_host.Multiply(nested_transform)
            except Exception:
                continue

            nested_name = get_link_instance_name(nested_instance)
            next_ancestry = set(ancestry_docs)
            next_ancestry.add(nested_doc_key)

            recurse(
                nested_instance,
                nested_doc,
                combined_transform,
                chain + [nested_instance],
                path_names + [nested_name],
                depth + 1,
                next_ancestry
            )

    root_key = get_document_title(root_doc)
    try:
        root_key = safe_text(root_doc.PathName) or root_key
    except Exception:
        pass

    recurse(
        root_instance,
        root_doc,
        root_transform,
        [root_instance],
        [root_name],
        0,
        set([root_key])
    )
    return targets


def collect_targets_from_root_ids(root_link_ids):
    valid_roots = []
    targets = []
    seen_root_ids = set()

    for raw_id in root_link_ids:
        element_id = make_element_id(raw_id)
        if not element_id:
            continue
        elem = doc.GetElement(element_id)
        if not isinstance(elem, RevitLinkInstance):
            continue

        root_id = get_element_id_value(elem.Id)
        if root_id in seen_root_ids:
            continue
        seen_root_ids.add(root_id)
        valid_roots.append(elem)
        targets.extend(collect_nested_link_targets(elem))

    return valid_roots, targets


def create_host_reference_from_nested(reference_in_target_doc, link_chain):
    """Nâng reference từ nested document qua từng link instance về host document."""
    if reference_in_target_doc is None:
        return None

    current_reference = reference_in_target_doc
    try:
        for link_instance in reversed(link_chain):
            current_reference = current_reference.CreateLinkReference(link_instance)
        return current_reference
    except Exception:
        return None


# ==============================================================================
# BÁO CÁO WPF SAU KHI CHẠY
# ==============================================================================
REPORT_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        Title="Auto Sleeve - Kết quả" Width="760" Height="500"
        MinWidth="620" MinHeight="380" WindowStartupLocation="CenterScreen"
        Background="#F4F7FB">
    <Grid Margin="16">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <Border Background="#17324D" CornerRadius="8" Padding="16">
            <StackPanel>
                <TextBlock Text="AUTO SLEEVE" Foreground="White" FontSize="20" FontWeight="Bold"/>
                <TextBlock Name="TxtSummary" Foreground="#DCEBFA" Margin="0,5,0,0" TextWrapping="Wrap"/>
            </StackPanel>
        </Border>

        <Border Grid.Row="1" Background="White" CornerRadius="7" Margin="0,12,0,6" Padding="8">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="95"/>
                    <ColumnDefinition Width="90"/>
                    <ColumnDefinition Width="95"/>
                </Grid.ColumnDefinitions>
                <TextBlock Text="Family : Type Sleeve" FontWeight="SemiBold" Foreground="#334155"/>
                <TextBlock Grid.Column="1" Text="Kích thước" FontWeight="SemiBold" TextAlignment="Center" Foreground="#334155"/>
                <TextBlock Grid.Column="2" Text="Số lượng" FontWeight="SemiBold" TextAlignment="Center" Foreground="#334155"/>
                <TextBlock Grid.Column="3" Text="Thao tác" FontWeight="SemiBold" TextAlignment="Center" Foreground="#334155"/>
            </Grid>
        </Border>

        <Border Grid.Row="2" Background="White" CornerRadius="7" Padding="8">
            <ScrollViewer VerticalScrollBarVisibility="Auto">
                <StackPanel Name="DataContainer"/>
            </ScrollViewer>
        </Border>

        <Button Name="BtnClose" Grid.Row="3" Content="Đóng" Width="100" Height="32"
                HorizontalAlignment="Right" Margin="0,12,0,0" Background="#17324D"
                Foreground="White" FontWeight="SemiBold" BorderThickness="0"/>
    </Grid>
</Window>
"""


class ReportApp(object):
    def __init__(self, tracking_data, total_deleted, total_placed):
        self.window = XamlReader.Parse(REPORT_XAML)
        self.container = self.window.FindName("DataContainer")
        self.txt_summary = self.window.FindName("TxtSummary")
        self.btn_close = self.window.FindName("BtnClose")
        self.btn_close.Click += self.close_click

        self.txt_summary.Text = (
            "Đã đặt {} sleeve mới · Đã xóa {} sleeve cũ trùng lặp. "
            "Có thể zoom theo từng nhóm bên dưới hoặc dùng ID trong pyRevit Output."
        ).format(total_placed, total_deleted)

        for key, ids in tracking_data.items():
            type_name, pipe_size = key
            self.add_row(type_name, pipe_size, len(ids), ids)

    def add_row(self, type_name, size, count, ids):
        row_border = System.Windows.Controls.Border()
        row_border.BorderBrush = System.Windows.Media.BrushConverter().ConvertFromString("#E2E8F0")
        row_border.BorderThickness = System.Windows.Thickness(0, 0, 0, 1)
        row_border.Padding = System.Windows.Thickness(4, 8, 4, 8)

        row = System.Windows.Controls.Grid()
        widths = [
            System.Windows.GridLength(1.0, System.Windows.GridUnitType.Star),
            System.Windows.GridLength(95.0),
            System.Windows.GridLength(90.0),
            System.Windows.GridLength(95.0)
        ]
        for width in widths:
            column = System.Windows.Controls.ColumnDefinition()
            column.Width = width
            row.ColumnDefinitions.Add(column)

        tb_type = System.Windows.Controls.TextBlock()
        tb_type.Text = safe_text(type_name)
        tb_type.TextWrapping = System.Windows.TextWrapping.Wrap
        tb_type.VerticalAlignment = System.Windows.VerticalAlignment.Center
        System.Windows.Controls.Grid.SetColumn(tb_type, 0)

        tb_size = System.Windows.Controls.TextBlock()
        tb_size.Text = "DN{}".format(int(size))
        tb_size.TextAlignment = System.Windows.TextAlignment.Center
        tb_size.VerticalAlignment = System.Windows.VerticalAlignment.Center
        System.Windows.Controls.Grid.SetColumn(tb_size, 1)

        tb_count = System.Windows.Controls.TextBlock()
        tb_count.Text = str(count)
        tb_count.TextAlignment = System.Windows.TextAlignment.Center
        tb_count.VerticalAlignment = System.Windows.VerticalAlignment.Center
        System.Windows.Controls.Grid.SetColumn(tb_count, 2)

        btn_zoom = System.Windows.Controls.Button()
        btn_zoom.Content = "Zoom nhóm"
        btn_zoom.Width = 84
        btn_zoom.Height = 26
        btn_zoom.Cursor = System.Windows.Input.Cursors.Hand
        btn_zoom.Click += self.create_zoom_handler(ids)
        System.Windows.Controls.Grid.SetColumn(btn_zoom, 3)

        row.Children.Add(tb_type)
        row.Children.Add(tb_size)
        row.Children.Add(tb_count)
        row.Children.Add(btn_zoom)
        row_border.Child = row
        self.container.Children.Add(row_border)

    def create_zoom_handler(self, element_ids):
        captured_ids = list(element_ids)

        def handler(sender, event_args):
            id_list = List[ElementId]()
            for element_id in captured_ids:
                id_list.Add(element_id)
            uidoc.Selection.SetElementIds(id_list)
            uidoc.ShowElements(id_list)

        return handler

    def show_dialog(self):
        self.window.ShowDialog()

    def close_click(self, sender, event_args):
        self.window.Close()


# ==============================================================================
# PYREVIT OUTPUT
# ==============================================================================
def print_pyrevit_report(placed_records, skipped_records, missing_size_records, total_deleted, total_placed, scan_wall, scan_floor, scan_beam):
    output.set_title("Auto Sleeve - Kết quả")
    output.print_md("# KẾT QUẢ AUTO PLACE SLEEVE")

    missing_size_groups = {}
    for item in missing_size_records:
        required_key = normalize_size_mm(item.get("required_diameter_mm"))
        if required_key is None:
            continue
        group = missing_size_groups.setdefault(required_key, {
            "ids": [],
            "combinations": set()
        })
        group["ids"].append(item["source_id"])
        combination_text = "{} + 2×{}".format(
            format_size_mm(item.get("nominal_diameter_mm")),
            format_size_mm(item.get("insulation_thickness_mm"))
        )
        group["combinations"].add(combination_text)

    scan_text = []
    if scan_wall:
        scan_text.append("Wall")
    if scan_floor:
        scan_text.append("Floor")
    if scan_beam:
        scan_text.append("Structural Framing")

    output.print_md(
        "**Phạm vi kết cấu:** {}  |  **Đặt mới:** {}  |  **Xóa cũ:** {}  |  **Bỏ qua trùng:** {}  |  **Thiếu size mapping:** {}  |  **Góc xuyên tối thiểu:** {:.1f}° so với bề mặt".format(
            " + ".join(scan_text),
            total_placed,
            total_deleted,
            len(skipped_records),
            len(missing_size_groups),
            MIN_PENETRATION_ANGLE_TO_SURFACE_DEG
        )
    )

    if placed_records:
        all_new_ids = [item["sleeve_id"] for item in placed_records]
        group_size = 75
        for start_index in range(0, len(all_new_ids), group_size):
            id_group = all_new_ids[start_index:start_index + group_size]
            end_index = start_index + len(id_group)
            output.print_md(output.linkify(
                id_group,
                title="Chọn/zoom sleeve {}-{}".format(start_index + 1, end_index)
            ))

        output.print_md("## Sleeve mới")
        output.print_md(
            "| STT | Sleeve ID | Đối tượng MEP | Category | ĐK ống | Cách nhiệt | Required diameter | Family : Type | Loại host | Góc với bề mặt | Link / Nested link | Host ID |"
        )
        output.print_md("|---:|---|---|---|---:|---:|---:|---|---|---:|---|---:|")

        for index, item in enumerate(placed_records, 1):
            sleeve_id = item["sleeve_id"]
            source_id = item["source_id"]
            sleeve_link = output.linkify(
                sleeve_id,
                title="ID {} - Zoom sleeve".format(get_element_id_value(sleeve_id))
            )
            source_link = output.linkify(
                source_id,
                title="ID {} - Zoom MEP".format(get_element_id_value(source_id))
            )

            angle_value = item.get("angle_to_surface", None)
            angle_text = ""
            if angle_value is not None:
                try:
                    angle_text = "{:.1f}°".format(float(angle_value))
                except Exception:
                    angle_text = safe_text(angle_value)

            output.print_md(
                "| {} | {} | {} | {} | {} mm | {} mm | {} mm | {} | {} | {} | {} | {} |".format(
                    index,
                    sleeve_link,
                    source_link,
                    safe_markdown_text(item.get("source_category", "")),
                    format_size_mm(item.get("nominal_diameter_mm")),
                    format_size_mm(item.get("insulation_thickness_mm")),
                    format_size_mm(item.get("required_diameter_mm")),
                    safe_markdown_text(item.get("sleeve_type", "")),
                    safe_markdown_text(item.get("host_category", "")),
                    safe_markdown_text(angle_text),
                    safe_markdown_text(item.get("link_name", "")),
                    safe_markdown_text(item.get("host_id", ""))
                )
            )

    if skipped_records:
        output.print_md("## Sleeve hiện có được bỏ qua do trùng")
        output.print_md("| STT | Sleeve hiện có | Đối tượng MEP | ĐK ống | Cách nhiệt | Required diameter | Link / Nested link |")
        output.print_md("|---:|---|---|---:|---:|---:|---|")

        for index, item in enumerate(skipped_records, 1):
            existing_link = output.linkify(
                item["sleeve_id"],
                title="ID {} - Zoom sleeve hiện có".format(get_element_id_value(item["sleeve_id"]))
            )
            source_link = output.linkify(
                item["source_id"],
                title="ID {} - Zoom MEP".format(get_element_id_value(item["source_id"]))
            )
            output.print_md(
                "| {} | {} | {} | {} mm | {} mm | {} mm | {} |".format(
                    index,
                    existing_link,
                    source_link,
                    format_size_mm(item.get("nominal_diameter_mm")),
                    format_size_mm(item.get("insulation_thickness_mm")),
                    format_size_mm(item.get("required_diameter_mm")),
                    safe_markdown_text(item.get("link_name", ""))
                )
            )

    if missing_size_groups:
        output.print_md("## ⚠ Thiếu size đã cài đặt cho sleeve")
        output.print_md(
            "> Các đối tượng dưới đây không được đặt sleeve vì **Required diameter** "
            "không bằng bất kỳ size mapping nào. Hãy bổ sung đúng các size in đậm trong giao diện."
        )
        output.print_md(
            "| STT | Required diameter cần bổ sung | Công thức xuất hiện | Số đối tượng | Chọn / zoom |"
        )
        output.print_md("|---:|---:|---|---:|---|")
        for index, required_key in enumerate(sorted(missing_size_groups.keys()), 1):
            group = missing_size_groups[required_key]
            combinations = ", ".join(sorted(group["combinations"]))
            ids = group["ids"]
            source_link = output.linkify(
                ids,
                title="Chọn/zoom {} đối tượng thiếu size {} mm".format(
                    len(ids),
                    format_size_mm(required_key)
                )
            )
            output.print_md(
                "| {} | **{} mm** | {} | {} | {} |".format(
                    index,
                    format_size_mm(required_key),
                    safe_markdown_text(combinations),
                    len(ids),
                    source_link
                )
            )

    if not placed_records and not skipped_records and not missing_size_records:
        output.print_md("Không tìm thấy vị trí xuyên phù hợp để đặt sleeve.")

    output.print_md(
        "> Bấm vào **ID** để chọn đối tượng; bấm biểu tượng kính lúp cạnh ID để zoom/show trong Revit."
    )


# ==============================================================================
# GIAO DIỆN CHÍNH
# ==============================================================================
class AutoSleeveApp(object):
    def __init__(self, xaml_file_path, state, available_required_sizes=None):
        self.dialog_result = False
        self.request_pick_links = False
        self.sleeve_types = self.get_sleeve_family_types()
        self.rows_data = []
        self.state = default_settings()
        self.state.update(state or {})
        self.selected_root_ids = list(self.state.get("root_link_ids", []))

        size_values = []
        for value in (available_required_sizes or []):
            normalized = normalize_size_mm(value)
            if normalized is not None and normalized not in size_values:
                size_values.append(normalized)

        # Giữ cả size đã lưu từ trước, kể cả khi hiện tại model không còn pipe size đó.
        for mapping in self.state.get("mapping", []):
            value = extract_single_number(mapping.get("size", ""))
            normalized = normalize_size_mm(value)
            if normalized is not None and normalized not in size_values:
                size_values.append(normalized)

        self.available_size_values = sorted(size_values)
        self.available_size_options = [
            format_size_mm(value) for value in self.available_size_values
        ]

        if not self.sleeve_types:
            forms.alert(
                "Không tìm thấy Family sleeve nào trong FAMILY_CONFIG ở dự án.",
                title="Auto Sleeve",
                exitscript=True
            )

        xaml_content = System.IO.File.ReadAllText(xaml_file_path, System.Text.Encoding.UTF8)
        xaml_clean = re.sub(r'Click\s*=\s*"[^"]*"', '', xaml_content)
        self.window = XamlReader.Parse(xaml_clean)

        self.mapping_container = self.window.FindName("MappingContainer")
        self.link_container = self.window.FindName("LinkContainer")
        self.txt_link_summary = self.window.FindName("TxtLinkSummary")
        self.txt_size_summary = self.window.FindName("TxtSizeSummary")
        self.txt_validation = self.window.FindName("TxtValidation")

        self.btn_add_row = self.window.FindName("BtnAddRow")
        self.btn_pick_links = self.window.FindName("BtnPickLinks")
        self.btn_clear_links = self.window.FindName("BtnClearLinks")
        self.btn_run = self.window.FindName("BtnRun")
        self.btn_cancel = self.window.FindName("BtnCancel")

        self.chk_wall = self.window.FindName("ChkWall")
        self.chk_floor = self.window.FindName("ChkFloor")
        self.chk_beam = self.window.FindName("ChkBeam")
        self.rb_skip = self.window.FindName("RbSkip")
        self.rb_replace = self.window.FindName("RbReplace")

        self.btn_add_row.Click += self.add_row_click
        self.btn_pick_links.Click += self.pick_links_click
        self.btn_clear_links.Click += self.clear_links_click
        self.btn_run.Click += self.run_click
        self.btn_cancel.Click += self.cancel_click

        if self.txt_size_summary:
            if self.available_size_options:
                self.txt_size_summary.Text = (
                    "Đã quét {} cỡ sleeve yêu cầu từ toàn bộ Pipe trong model "
                    "(đã cộng 2 × cách nhiệt). Có thể chọn hoặc gõ số khác."
                ).format(len(self.available_size_options))
            else:
                self.txt_size_summary.Text = (
                    "Không tìm thấy Pipe có kích thước hợp lệ trong model. "
                    "Vẫn có thể gõ size bằng tay."
                )

        self.apply_state()

    def get_sleeve_family_types(self):
        sleeve_types = []
        for family in FilteredElementCollector(doc).OfClass(Family):
            if family.Name in FAMILY_CONFIG:
                for symbol_id in family.GetFamilySymbolIds():
                    symbol = doc.GetElement(symbol_id)
                    if symbol:
                        sleeve_types.append(symbol)
        return sleeve_types

    def apply_state(self):
        mappings = self.state.get("mapping", [])
        if mappings:
            for mapping in mappings:
                self.add_new_row(
                    default_size=mapping.get("size", ""),
                    default_type=mapping.get("type", "")
                )
        else:
            self.add_new_row()

        self.chk_wall.IsChecked = bool(self.state.get("scan_wall", True))
        self.chk_floor.IsChecked = bool(self.state.get("scan_floor", True))
        self.chk_beam.IsChecked = bool(self.state.get("scan_beam", False))

        replace_mode = bool(self.state.get("replace_mode", False))
        self.rb_replace.IsChecked = replace_mode
        self.rb_skip.IsChecked = not replace_mode

        self.refresh_link_display()

    def capture_state(self):
        mappings = []
        for cmb_size, cmb_type in self.rows_data:
            mappings.append({
                "size": safe_text(cmb_size.Text),
                "type": safe_text(cmb_type.SelectedItem) if cmb_type.SelectedItem else ""
            })

        return {
            "mapping": mappings,
            "root_link_ids": list(self.selected_root_ids),
            "scan_wall": bool(self.chk_wall.IsChecked),
            "scan_floor": bool(self.chk_floor.IsChecked),
            "scan_beam": bool(self.chk_beam.IsChecked),
            "replace_mode": bool(self.rb_replace.IsChecked)
        }

    def add_new_row(self, default_size="", default_type=""):
        row_border = System.Windows.Controls.Border()
        row_border.Background = System.Windows.Media.Brushes.White
        row_border.BorderBrush = System.Windows.Media.BrushConverter().ConvertFromString("#E2E8F0")
        row_border.BorderThickness = System.Windows.Thickness(1)
        row_border.CornerRadius = System.Windows.CornerRadius(6)
        row_border.Padding = System.Windows.Thickness(10, 8, 10, 8)
        row_border.Margin = System.Windows.Thickness(0, 0, 0, 8)

        row = System.Windows.Controls.Grid()
        col_label = System.Windows.Controls.ColumnDefinition()
        col_label.Width = System.Windows.GridLength(122.0)
        col_size = System.Windows.Controls.ColumnDefinition()
        col_size.Width = System.Windows.GridLength(125.0)
        col_arrow = System.Windows.Controls.ColumnDefinition()
        col_arrow.Width = System.Windows.GridLength(48.0)
        col_type = System.Windows.Controls.ColumnDefinition()
        col_type.Width = System.Windows.GridLength(1.0, System.Windows.GridUnitType.Star)
        col_delete = System.Windows.Controls.ColumnDefinition()
        col_delete.Width = System.Windows.GridLength(38.0)
        for column in [col_label, col_size, col_arrow, col_type, col_delete]:
            row.ColumnDefinitions.Add(column)

        label = System.Windows.Controls.TextBlock()
        label.Text = "Cỡ sleeve yêu cầu:"
        label.VerticalAlignment = System.Windows.VerticalAlignment.Center
        label.Foreground = System.Windows.Media.BrushConverter().ConvertFromString("#334155")
        System.Windows.Controls.Grid.SetColumn(label, 0)

        cmb_size = System.Windows.Controls.ComboBox()
        cmb_size.Height = 30
        cmb_size.Padding = System.Windows.Thickness(5, 2, 5, 2)
        cmb_size.IsEditable = True
        cmb_size.IsTextSearchEnabled = True
        cmb_size.StaysOpenOnEdit = True
        cmb_size.ItemsSource = list(self.available_size_options)
        cmb_size.ToolTip = (
            "Required diameter (mm) = đường kính ống + 2 × chiều dày cách nhiệt. "
            "Danh sách được quét từ toàn bộ Pipe trong model; vẫn có thể gõ số."
        )
        default_size_text = safe_text(default_size).strip()
        if default_size_text:
            default_number = extract_single_number(default_size_text)
            default_normalized = normalize_size_mm(default_number)
            default_display = format_size_mm(default_normalized)
            if default_display in self.available_size_options:
                cmb_size.SelectedItem = default_display
            else:
                cmb_size.Text = default_size_text
        elif self.available_size_options:
            cmb_size.SelectedIndex = 0
        System.Windows.Controls.Grid.SetColumn(cmb_size, 1)

        arrow = System.Windows.Controls.TextBlock()
        arrow.Text = "→"
        arrow.FontSize = 18
        arrow.TextAlignment = System.Windows.TextAlignment.Center
        arrow.VerticalAlignment = System.Windows.VerticalAlignment.Center
        arrow.Foreground = System.Windows.Media.BrushConverter().ConvertFromString("#64748B")
        System.Windows.Controls.Grid.SetColumn(arrow, 2)

        cmb_type = System.Windows.Controls.ComboBox()
        cmb_type.Height = 30
        cmb_type.Padding = System.Windows.Thickness(5, 2, 5, 2)
        type_names = [get_symbol_display_name(symbol) for symbol in self.sleeve_types]
        cmb_type.ItemsSource = type_names
        if default_type in type_names:
            cmb_type.SelectedItem = default_type
        elif type_names:
            cmb_type.SelectedIndex = 0
        System.Windows.Controls.Grid.SetColumn(cmb_type, 3)

        btn_delete = System.Windows.Controls.Button()
        btn_delete.Content = "×"
        btn_delete.Width = 28
        btn_delete.Height = 28
        btn_delete.Margin = System.Windows.Thickness(8, 0, 0, 0)
        btn_delete.Background = System.Windows.Media.BrushConverter().ConvertFromString("#FEE2E2")
        btn_delete.Foreground = System.Windows.Media.BrushConverter().ConvertFromString("#B91C1C")
        btn_delete.BorderBrush = System.Windows.Media.BrushConverter().ConvertFromString("#FECACA")
        btn_delete.FontWeight = System.Windows.FontWeights.Bold
        btn_delete.Cursor = System.Windows.Input.Cursors.Hand
        System.Windows.Controls.Grid.SetColumn(btn_delete, 4)

        def remove_row(sender, event_args):
            if len(self.rows_data) <= 1:
                self.txt_validation.Text = "Cần giữ lại ít nhất một dòng ánh xạ kích thước."
                return
            self.mapping_container.Children.Remove(row_border)
            self.rows_data.remove((cmb_size, cmb_type))

        btn_delete.Click += remove_row

        row.Children.Add(label)
        row.Children.Add(cmb_size)
        row.Children.Add(arrow)
        row.Children.Add(cmb_type)
        row.Children.Add(btn_delete)
        row_border.Child = row
        self.mapping_container.Children.Add(row_border)
        self.rows_data.append((cmb_size, cmb_type))

    def refresh_link_display(self):
        self.link_container.Children.Clear()
        valid_roots, targets = collect_targets_from_root_ids(self.selected_root_ids)
        self.selected_root_ids = [get_element_id_value(root.Id) for root in valid_roots]

        if not valid_roots:
            empty_border = System.Windows.Controls.Border()
            empty_border.Background = System.Windows.Media.BrushConverter().ConvertFromString("#F8FAFC")
            empty_border.BorderBrush = System.Windows.Media.BrushConverter().ConvertFromString("#CBD5E1")
            empty_border.BorderThickness = System.Windows.Thickness(1)
            empty_border.CornerRadius = System.Windows.CornerRadius(6)
            empty_border.Padding = System.Windows.Thickness(14)

            empty_text = System.Windows.Controls.TextBlock()
            empty_text.Text = (
                "Chưa pick link nào. Nhấn ‘PICK NHIỀU LINK’, chọn các Revit Link trong model "
                "rồi nhấn Finish trên thanh Options Bar."
            )
            empty_text.TextWrapping = System.Windows.TextWrapping.Wrap
            empty_text.Foreground = System.Windows.Media.BrushConverter().ConvertFromString("#64748B")
            empty_border.Child = empty_text
            self.link_container.Children.Add(empty_border)
            self.txt_link_summary.Text = "0 link gốc · 0 nested link"
            return

        nested_count = max(0, len(targets) - len(valid_roots))
        self.txt_link_summary.Text = "{} link gốc · {} nested link · {} phạm vi link được quét".format(
            len(valid_roots), nested_count, len(targets)
        )

        targets_by_root = {}
        for target in targets:
            targets_by_root.setdefault(target.root_id, []).append(target)

        for root_index, root in enumerate(valid_roots, 1):
            root_id = get_element_id_value(root.Id)
            root_targets = targets_by_root.get(root_id, [])
            root_doc = root.GetLinkDocument()

            card = System.Windows.Controls.Border()
            card.Background = System.Windows.Media.Brushes.White
            card.BorderBrush = System.Windows.Media.BrushConverter().ConvertFromString("#D8E2EC")
            card.BorderThickness = System.Windows.Thickness(1)
            card.CornerRadius = System.Windows.CornerRadius(7)
            card.Padding = System.Windows.Thickness(12)
            card.Margin = System.Windows.Thickness(0, 0, 0, 9)

            panel = System.Windows.Controls.StackPanel()

            title_grid = System.Windows.Controls.Grid()
            title_col = System.Windows.Controls.ColumnDefinition()
            title_col.Width = System.Windows.GridLength(1.0, System.Windows.GridUnitType.Star)
            remove_col = System.Windows.Controls.ColumnDefinition()
            remove_col.Width = System.Windows.GridLength(34.0)
            title_grid.ColumnDefinitions.Add(title_col)
            title_grid.ColumnDefinitions.Add(remove_col)

            title = System.Windows.Controls.TextBlock()
            title.Text = "{}. {}".format(root_index, get_link_instance_name(root))
            title.FontWeight = System.Windows.FontWeights.SemiBold
            title.FontSize = 13
            title.Foreground = System.Windows.Media.BrushConverter().ConvertFromString("#17324D")
            title.TextWrapping = System.Windows.TextWrapping.Wrap
            System.Windows.Controls.Grid.SetColumn(title, 0)

            btn_remove = System.Windows.Controls.Button()
            btn_remove.Content = "×"
            btn_remove.Width = 26
            btn_remove.Height = 24
            btn_remove.ToolTip = "Bỏ link này khỏi danh sách"
            btn_remove.Background = System.Windows.Media.BrushConverter().ConvertFromString("#FEE2E2")
            btn_remove.Foreground = System.Windows.Media.BrushConverter().ConvertFromString("#B91C1C")
            btn_remove.BorderBrush = System.Windows.Media.BrushConverter().ConvertFromString("#FECACA")
            btn_remove.Cursor = System.Windows.Input.Cursors.Hand
            System.Windows.Controls.Grid.SetColumn(btn_remove, 1)

            def remove_root(sender, event_args, captured_id=root_id):
                self.selected_root_ids = [x for x in self.selected_root_ids if int(x) != int(captured_id)]
                self.refresh_link_display()

            btn_remove.Click += remove_root
            title_grid.Children.Add(title)
            title_grid.Children.Add(btn_remove)
            panel.Children.Add(title_grid)

            meta = System.Windows.Controls.TextBlock()
            meta.Text = "Root Instance ID: {}   ·   Document: {}".format(
                root_id,
                get_document_title(root_doc) if root_doc else "Link chưa load"
            )
            meta.Foreground = System.Windows.Media.BrushConverter().ConvertFromString("#64748B")
            meta.FontSize = 11
            meta.Margin = System.Windows.Thickness(0, 4, 0, 6)
            meta.TextWrapping = System.Windows.TextWrapping.Wrap
            panel.Children.Add(meta)

            for target in root_targets:
                path_text = System.Windows.Controls.TextBlock()
                prefix = "• " if target.depth == 0 else "↳ "
                path_text.Text = "{}{}  [Instance ID {}]".format(
                    prefix,
                    target.path_name,
                    target.target_instance_id
                )
                path_text.TextWrapping = System.Windows.TextWrapping.Wrap
                path_text.Margin = System.Windows.Thickness(8 + target.depth * 12, 2, 0, 2)
                path_text.Foreground = System.Windows.Media.BrushConverter().ConvertFromString(
                    "#334155" if target.depth == 0 else "#475569"
                )
                panel.Children.Add(path_text)

            card.Child = panel
            self.link_container.Children.Add(card)

    def add_row_click(self, sender, event_args):
        self.txt_validation.Text = ""
        self.add_new_row()

    def pick_links_click(self, sender, event_args):
        """
        Không gọi PickObjects trực tiếp khi cửa sổ đang ShowDialog.
        Chỉ lưu state + đóng cửa sổ; main() sẽ thực hiện pick bên ngoài WPF rồi mở lại.
        """
        self.state = self.capture_state()
        self.request_pick_links = True
        self.window.Close()

    def clear_links_click(self, sender, event_args):
        self.selected_root_ids = []
        self.txt_validation.Text = ""
        self.refresh_link_display()

    def validate_before_run(self):
        if (not bool(self.chk_wall.IsChecked)
                and not bool(self.chk_floor.IsChecked)
                and not bool(self.chk_beam.IsChecked)):
            return (
                "Hãy chọn ít nhất Wall, Floor hoặc Structural Framing. "
                "Có thể chọn đồng thời nhiều loại kết cấu."
            )

        if not self.selected_root_ids:
            return "Chưa có Revit Link nào được pick."

        has_valid_mapping = False
        for cmb_size, cmb_type in self.rows_data:
            if extract_single_number(safe_text(cmb_size.Text)) is not None and cmb_type.SelectedItem:
                has_valid_mapping = True
                break
        if not has_valid_mapping:
            return "Hãy nhập ít nhất một kích thước hợp lệ và chọn Sleeve Type."

        return ""

    def run_click(self, sender, event_args):
        validation_error = self.validate_before_run()
        if validation_error:
            self.txt_validation.Text = validation_error
            return

        self.state = self.capture_state()
        save_settings(self.state)
        self.dialog_result = True
        self.window.Close()

    def cancel_click(self, sender, event_args):
        self.dialog_result = False
        self.request_pick_links = False
        self.window.Close()

    def show_dialog(self):
        return self.window.ShowDialog()


# ==============================================================================
# HÌNH HỌC MEP / LINK
# ==============================================================================
def get_extended_curve(curve, ext_ft=2.0):
    try:
        if isinstance(curve, Line):
            point_0 = curve.GetEndPoint(0)
            point_1 = curve.GetEndPoint(1)
            if point_0.DistanceTo(point_1) > 0.01:
                direction = (point_1 - point_0).Normalize()
                return Line.CreateBound(
                    point_0 - direction * ext_ft,
                    point_1 + direction * ext_ft
                )
    except Exception:
        pass
    return curve


def get_axis_curve(elem):
    try:
        location = elem.Location
    except Exception:
        location = None

    if location and hasattr(location, "Curve") and location.Curve:
        return get_extended_curve(location.Curve)

    try:
        if hasattr(elem, "MEPModel") and elem.MEPModel and elem.MEPModel.ConnectorManager:
            connectors = [
                connector for connector in elem.MEPModel.ConnectorManager.Connectors
                if connector.ConnectorType != ConnectorType.Logical
            ]
            if len(connectors) >= 2:
                point_1 = connectors[0].Origin
                point_2 = connectors[1].Origin
                if point_1.DistanceTo(point_2) > 0.01:
                    return get_extended_curve(Line.CreateBound(point_1, point_2))
            elif len(connectors) == 1:
                point = connectors[0].Origin
                z_vector = connectors[0].CoordinateSystem.BasisZ
                return Line.CreateBound(point - z_vector * 5.0, point + z_vector * 5.0)
    except Exception:
        pass

    if isinstance(location, LocationPoint):
        point = location.Point
        return Line.CreateBound(point - XYZ.BasisZ * 5.0, point + XYZ.BasisZ * 5.0)

    return None


def get_size_string(elem):
    parameter_names = [
        "Size", "Diameter", "Kích thước", "Đường kính",
        "Nominal Diameter", "Kích cỡ"
    ]
    for name in parameter_names:
        parameter = elem.LookupParameter(name)
        if parameter and parameter.HasValue:
            try:
                value = parameter.AsString()
                if value:
                    return value
            except Exception:
                pass
            try:
                value = parameter.AsValueString()
                if value:
                    return value
            except Exception:
                pass

    builtin_parameters = [
        BuiltInParameter.RBS_CALCULATED_SIZE,
        BuiltInParameter.RBS_PIPE_DIAMETER_PARAM
    ]
    for builtin_parameter in builtin_parameters:
        try:
            parameter = elem.get_Parameter(builtin_parameter)
            if parameter and parameter.HasValue:
                value = parameter.AsString()
                if value:
                    return value
                value = parameter.AsValueString()
                if value:
                    return value
        except Exception:
            pass

    return ""


def get_length_parameter_mm(parameter):
    if not parameter or not parameter.HasValue:
        return None
    try:
        if parameter.StorageType == StorageType.Double:
            return internal_length_to_mm(parameter.AsDouble())
    except Exception:
        pass
    try:
        return extract_single_number(parameter.AsValueString())
    except Exception:
        return None


def get_nominal_diameter_mm(elem):
    # Pipe thật: ưu tiên parameter số để không phụ thuộc cách hiển thị đơn vị.
    try:
        parameter = elem.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
        value = get_length_parameter_mm(parameter)
        if value is not None and value > 0:
            return value
    except Exception:
        pass

    try:
        diameter_value = elem.Diameter
        value = internal_length_to_mm(diameter_value)
        if value is not None and value > 0:
            return value
    except Exception:
        pass

    # Fitting / Accessory / Plumbing Fixture: dùng Size hoặc Calculated Size.
    size_string = get_size_string(elem)
    return extract_max_size(size_string)


def get_insulation_element_thickness_mm(insulation_element):
    if insulation_element is None:
        return 0.0

    try:
        value = internal_length_to_mm(insulation_element.Thickness)
        if value is not None and value >= 0:
            return value
    except Exception:
        pass

    builtin_names = [
        "RBS_PIPE_INSULATION_THICKNESS",
        "RBS_REFERENCE_INSULATION_THICKNESS",
        "RBS_INSULATION_THICKNESS"
    ]
    for builtin_name in builtin_names:
        try:
            builtin_parameter = getattr(BuiltInParameter, builtin_name)
            parameter = insulation_element.get_Parameter(builtin_parameter)
            value = get_length_parameter_mm(parameter)
            if value is not None and value >= 0:
                return value
        except Exception:
            pass

    for parameter_name in [
            "Thickness", "Insulation Thickness",
            "Chiều dày", "Chiều dày cách nhiệt"]:
        try:
            parameter = insulation_element.LookupParameter(parameter_name)
            value = get_length_parameter_mm(parameter)
            if value is not None and value >= 0:
                return value
        except Exception:
            pass

    return 0.0


def build_pipe_insulation_index(document):
    """Map HostElementId -> chiều dày cách nhiệt lớn nhất, đơn vị mm."""
    result = {}
    try:
        insulation_elements = list(
            FilteredElementCollector(document)
            .OfCategory(BuiltInCategory.OST_PipeInsulations)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        insulation_elements = []

    for insulation_element in insulation_elements:
        try:
            host_id = get_element_id_value(insulation_element.HostElementId)
        except Exception:
            host_id = -1
        if host_id < 0:
            continue
        thickness_mm = get_insulation_element_thickness_mm(insulation_element)
        if thickness_mm > result.get(host_id, 0.0):
            result[host_id] = thickness_mm
    return result


def get_insulation_thickness_mm(elem, insulation_index=None):
    element_id_value = get_element_id_value(elem.Id)
    if insulation_index and element_id_value in insulation_index:
        return insulation_index[element_id_value]

    # Dự phòng cho một số bản Revit/API khi insulation không lấy được qua collector.
    insulation_ids = []
    try:
        insulation_ids = list(
            InsulationLiningBase.GetInsulationIds(doc, elem.Id)
        )
    except Exception:
        try:
            if PipeInsulation:
                insulation_ids = list(
                    PipeInsulation.GetInsulationIds(doc, elem.Id)
                )
        except Exception:
            insulation_ids = []

    maximum_thickness = 0.0
    for insulation_id in insulation_ids:
        thickness_mm = get_insulation_element_thickness_mm(
            doc.GetElement(insulation_id)
        )
        maximum_thickness = max(maximum_thickness, thickness_mm)

    if maximum_thickness > 0:
        return maximum_thickness

    # Dự phòng cuối: một số family/category hiển thị chiều dày ngay trên host.
    for builtin_name in [
            "RBS_PIPE_INSULATION_THICKNESS",
            "RBS_REFERENCE_INSULATION_THICKNESS",
            "RBS_INSULATION_THICKNESS"]:
        try:
            builtin_parameter = getattr(BuiltInParameter, builtin_name)
            value = get_length_parameter_mm(
                elem.get_Parameter(builtin_parameter)
            )
            if value is not None and value > 0:
                return value
        except Exception:
            pass

    for parameter_name in [
            "Insulation Thickness", "Thickness",
            "Chiều dày cách nhiệt", "Chiều dày"]:
        try:
            value = get_length_parameter_mm(
                elem.LookupParameter(parameter_name)
            )
            if value is not None and value > 0:
                return value
        except Exception:
            pass

    return 0.0


def get_required_diameter_info(elem, insulation_index=None):
    nominal_mm = get_nominal_diameter_mm(elem)
    if nominal_mm is None or nominal_mm <= 0:
        return None

    insulation_mm = get_insulation_thickness_mm(elem, insulation_index)
    required_mm = nominal_mm + 2.0 * insulation_mm
    return {
        "nominal_diameter_mm": normalize_size_mm(nominal_mm),
        "insulation_thickness_mm": normalize_size_mm(insulation_mm) or 0.0,
        "required_diameter_mm": normalize_size_mm(required_mm),
        "required_size_key": normalize_size_mm(required_mm)
    }


def collect_model_required_pipe_sizes(document, insulation_index=None):
    size_values = set()
    try:
        pipe_elements = list(
            FilteredElementCollector(document)
            .OfCategory(BuiltInCategory.OST_PipeCurves)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        pipe_elements = []

    for pipe_element in pipe_elements:
        info = get_required_diameter_info(pipe_element, insulation_index)
        if info and info["required_size_key"] is not None:
            size_values.add(info["required_size_key"])
    return sorted(size_values)


def get_thickness_by_ray(solid, point, normal):
    start = point - normal * 5.0
    end = point + normal * 5.0
    bound_line = Line.CreateBound(start, end)
    options = SolidCurveIntersectionOptions()
    try:
        results = solid.IntersectWithCurve(bound_line, options)
        if results and results.SegmentCount > 0:
            # Tổng chiều dài các đoạn cắt sẽ chính xác hơn với solid có lỗ/rỗng.
            total_length = 0.0
            for index in range(results.SegmentCount):
                total_length += results.GetCurveSegment(index).Length
            return total_length if total_length > 0 else None
    except Exception:
        pass
    return None


def reset_sleeve_host_offset(sleeve):
    """
    Ép Offset from Host về 0.0 sau khi tạo face-based sleeve.

    NewFamilyInstance(reference, point, ...) có thể tự sinh một offset bằng
    khoảng cách từ điểm truyền vào đến mặt host. Family sleeve đang dùng đã
    được dựng đối xứng qua mặt tham chiếu, vì vậy offset 0.0 mới đưa chiều dài
    sleeve vào đúng giữa Wall/Floor/Structural Framing.
    """
    if sleeve is None:
        return False

    candidate_parameters = []

    try:
        parameter = sleeve.get_Parameter(
            BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM
        )
        if parameter:
            candidate_parameters.append(parameter)
    except Exception:
        pass

    # Dự phòng cho family/template hoặc bản Revit hiển thị tên parameter khác.
    for parameter_name in [
            "Offset from Host",
            "Host Offset",
            "Offset"]:
        try:
            parameter = sleeve.LookupParameter(parameter_name)
        except Exception:
            parameter = None
        if parameter and parameter not in candidate_parameters:
            candidate_parameters.append(parameter)

    for parameter in candidate_parameters:
        try:
            if parameter.IsReadOnly:
                continue
            if parameter.StorageType != StorageType.Double:
                continue
            parameter.Set(0.0)
            return True
        except Exception:
            continue

    return False


def iter_solid_contexts(geometry_element, accumulated_transform=None, references_are_valid=True):
    """
    Duyệt Solid cùng transform từ hệ tọa độ Solid về hệ tọa độ element/link.

    Structural Framing thường trả về GeometryInstance. Với trường hợp này phải dùng
    GetSymbolGeometry() để giữ Reference thật của mặt family; GetInstanceGeometry()
    chỉ được dùng làm phương án dự phòng cho phân tích hình học vì Reference của nó
    không thích hợp để host family mới.
    """
    if not geometry_element:
        return

    if accumulated_transform is None:
        accumulated_transform = Transform.Identity

    for geometry_object in geometry_element:
        if isinstance(geometry_object, Solid):
            try:
                is_valid_solid = (
                    geometry_object.Volume > 0
                    and geometry_object.Faces.Size > 0
                )
            except Exception:
                is_valid_solid = False

            if is_valid_solid:
                yield geometry_object, accumulated_transform, references_are_valid

        elif isinstance(geometry_object, GeometryInstance):
            instance_transform = accumulated_transform
            try:
                instance_transform = accumulated_transform.Multiply(
                    geometry_object.Transform
                )
            except Exception:
                pass

            # Ưu tiên SymbolGeometry vì đây là geometry thật, giữ được Reference.
            try:
                nested_geometry = geometry_object.GetSymbolGeometry()
            except Exception:
                nested_geometry = None

            if nested_geometry:
                for context in iter_solid_contexts(
                        nested_geometry,
                        instance_transform,
                        references_are_valid):
                    yield context
                continue

            # Dự phòng hiếm gặp: vẫn dùng để phát hiện giao cắt nhưng không dùng
            # Reference của mặt để host family.
            try:
                nested_geometry = geometry_object.GetInstanceGeometry()
            except Exception:
                nested_geometry = None

            if nested_geometry:
                for context in iter_solid_contexts(
                        nested_geometry,
                        accumulated_transform,
                        False):
                    yield context


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def get_curve_direction(curve):
    """Lấy vector tiếp tuyến của trục MEP trong hệ tọa độ hiện tại của curve."""
    if not curve:
        return None

    try:
        point_0 = curve.GetEndPoint(0)
        point_1 = curve.GetEndPoint(1)
        vector = point_1 - point_0
        if vector.GetLength() > ANGLE_EPSILON:
            return vector.Normalize()
    except Exception:
        pass

    try:
        derivatives = curve.ComputeDerivatives(0.5, True)
        vector = derivatives.BasisX
        if vector and vector.GetLength() > ANGLE_EPSILON:
            return vector.Normalize()
    except Exception:
        pass

    return None


def get_floor_surface_normal_local(floor_element, local_point):
    """Lấy pháp tuyến mặt trên/dưới của Floor gần điểm xuyên nhất."""
    candidate_faces = []

    for shell_layer_type in [ShellLayerType.Exterior, ShellLayerType.Interior]:
        try:
            references = HostObjectUtils.GetTopFaces(floor_element) \
                if shell_layer_type == ShellLayerType.Exterior \
                else HostObjectUtils.GetBottomFaces(floor_element)
        except Exception:
            references = []

        for reference in references:
            try:
                face = floor_element.GetGeometryObjectFromReference(reference)
            except Exception:
                face = None
            if face:
                candidate_faces.append(face)

    best_normal = None
    best_distance = float("inf")
    for face in candidate_faces:
        try:
            projection = face.Project(local_point)
        except Exception:
            projection = None
        if not projection:
            continue

        try:
            normal = face.ComputeNormal(projection.UVPoint).Normalize()
        except Exception:
            continue

        if projection.Distance < best_distance:
            best_distance = projection.Distance
            best_normal = normal

    return best_normal


def get_location_curve_direction(elem):
    """Lấy hướng trục phần tử trong hệ tọa độ document chứa phần tử."""
    try:
        location = elem.Location
        if location and hasattr(location, "Curve") and location.Curve:
            return get_curve_direction(location.Curve)
    except Exception:
        pass
    return None


def transform_direction_to_geometry(direction_in_element, geometry_to_element_transform):
    """Đổi vector từ hệ element/link về hệ tọa độ của Solid family."""
    if not direction_in_element:
        return None
    try:
        transformed = geometry_to_element_transform.Inverse.OfVector(
            direction_in_element
        )
        if transformed.GetLength() > ANGLE_EPSILON:
            return transformed.Normalize()
    except Exception:
        pass
    return None


def choose_structural_framing_face(
        solid, local_point, axis_direction, beam_axis_direction=None):
    """
    Chọn đúng mặt bị xuyên của Structural Framing.

    - Ống xuyên ngang: ưu tiên mặt đứng của beam, hoạt động như Wall.
    - Ống xuyên đứng: ưu tiên mặt trên/dưới, hoạt động như Floor.
    - Loại mặt đầu beam (normal gần song song trục beam) để tránh đặt sleeve
      khi MEP chỉ đi vào đầu dầm hoặc chạy dọc theo dầm.
    """
    best_data = None
    best_score = None

    for face in solid.Faces:
        try:
            projection = face.Project(local_point)
        except Exception:
            projection = None
        if not projection:
            continue

        try:
            normal = face.ComputeNormal(projection.UVPoint).Normalize()
            penetration_alignment = abs(
                axis_direction.Normalize().DotProduct(normal)
            )
        except Exception:
            continue

        # Tương đương điều kiện góc xuyên tối thiểu so với bề mặt.
        minimum_alignment = math.sin(math.radians(
            MIN_PENETRATION_ANGLE_TO_SURFACE_DEG
        ))
        if penetration_alignment + 1.0e-6 < minimum_alignment:
            continue

        # Mặt đầu beam có normal gần song song trục LocationCurve.
        # Đây thường không phải vị trí cần đặt sleeve kỹ thuật.
        if beam_axis_direction:
            try:
                end_face_alignment = abs(
                    beam_axis_direction.Normalize().DotProduct(normal)
                )
                if end_face_alignment > 0.85:
                    continue
            except Exception:
                pass

        # Alignment quyết định chính; khoảng cách chỉ dùng để phá hòa.
        # Ưu tiên nhẹ mặt có Reference thật để host family face-based.
        has_reference_bonus = 1.0 if face.Reference else 0.0
        score = (
            penetration_alignment * 10000.0
            + has_reference_bonus * 10.0
            - projection.Distance
        )

        if best_score is None or score > best_score:
            best_score = score
            best_data = (face, projection, normal)

    return best_data


def get_structural_framing_host_label(global_normal):
    """Tên báo cáo giúp phân biệt beam đang được xử lý như Wall hay Floor."""
    try:
        if abs(global_normal.Normalize().Z) >= 0.70710678:
            return "Structural Framing - mặt ngang như Floor"
    except Exception:
        pass
    return "Structural Framing - mặt đứng như Wall"


def get_host_surface_normal_local(host_element, local_point, fallback_face, fallback_projection):
    """
    Xác định pháp tuyến đại diện của bề mặt cần xuyên.

    - Wall: ưu tiên Wall.Orientation để không nhầm mặt đầu/mặt trên của tường.
    - Floor: ưu tiên mặt Top/Bottom để không nhầm cạnh đứng của sàn.
    - Structural Framing được chọn riêng theo hướng xuyên trước khi gọi hàm này.
    - Family/host khác: dùng face gần điểm giao nhất.
    """
    try:
        category_id = get_element_id_value(host_element.Category.Id)
    except Exception:
        category_id = -1

    if category_id == int(BuiltInCategory.OST_Walls):
        try:
            wall_normal = host_element.Orientation
            if wall_normal and wall_normal.GetLength() > ANGLE_EPSILON:
                return wall_normal.Normalize()
        except Exception:
            pass

    if category_id == int(BuiltInCategory.OST_Floors):
        floor_normal = get_floor_surface_normal_local(host_element, local_point)
        if floor_normal:
            return floor_normal

    try:
        return fallback_face.ComputeNormal(fallback_projection.UVPoint).Normalize()
    except Exception:
        return None


def get_penetration_angles(axis_direction, surface_normal):
    """
    Trả về:
      - góc giữa trục MEP và pháp tuyến bề mặt;
      - góc giữa trục MEP và chính bề mặt.

    Dùng trị tuyệt đối dot product vì chiều vector không ảnh hưởng kết quả.
    """
    if not axis_direction or not surface_normal:
        return None, None

    try:
        dot_value = abs(axis_direction.Normalize().DotProduct(surface_normal.Normalize()))
        dot_value = clamp(dot_value, 0.0, 1.0)
        angle_to_normal = math.degrees(math.acos(dot_value))
        angle_to_surface = 90.0 - angle_to_normal
        return angle_to_normal, angle_to_surface
    except Exception:
        return None, None


def is_valid_penetration_angle(axis_direction, surface_normal):
    """Chỉ nhận đối tượng thật sự xuyên bề mặt, bỏ đối tượng gần song song."""
    angle_to_normal, angle_to_surface = get_penetration_angles(
        axis_direction,
        surface_normal
    )
    if angle_to_surface is None:
        return False, None, None

    is_valid = (
        angle_to_surface + 1.0e-6
        >= MIN_PENETRATION_ANGLE_TO_SURFACE_DEG
    )
    return is_valid, angle_to_normal, angle_to_surface


def get_linked_face_reference(link_target, host_element, mep_element):
    pipe_curve_global = get_axis_curve(mep_element)
    if not pipe_curve_global:
        return None

    try:
        inverse_link_transform = link_target.transform.Inverse
        pipe_curve_in_element = pipe_curve_global.CreateTransformed(
            inverse_link_transform
        )
    except Exception:
        return None

    geometry_options = Options()
    geometry_options.ComputeReferences = True
    geometry_options.DetailLevel = ViewDetailLevel.Fine
    geometry_options.IncludeNonVisibleObjects = True

    try:
        geometry_element = host_element.get_Geometry(geometry_options)
    except Exception:
        geometry_element = None

    if not geometry_element:
        return None

    try:
        host_category_id = get_element_id_value(host_element.Category.Id)
    except Exception:
        host_category_id = -1

    is_structural_framing = (
        host_category_id == int(BuiltInCategory.OST_StructuralFraming)
    )
    beam_axis_in_element = (
        get_location_curve_direction(host_element)
        if is_structural_framing else None
    )

    intersection_options = SolidCurveIntersectionOptions()

    for solid, geometry_to_element_transform, references_are_valid in \
            iter_solid_contexts(geometry_element):
        try:
            pipe_curve_local = pipe_curve_in_element.CreateTransformed(
                geometry_to_element_transform.Inverse
            )
        except Exception:
            continue

        axis_direction_local = get_curve_direction(pipe_curve_local)
        if not axis_direction_local:
            continue

        beam_axis_local = transform_direction_to_geometry(
            beam_axis_in_element,
            geometry_to_element_transform
        )

        try:
            results = solid.IntersectWithCurve(
                pipe_curve_local,
                intersection_options
            )
        except Exception:
            continue

        if not results or results.SegmentCount <= 0:
            continue

        # Có thể solid trả về nhiều đoạn. Duyệt từng đoạn thay vì chỉ lấy đoạn đầu.
        for segment_index in range(results.SegmentCount):
            try:
                intersect_segment = results.GetCurveSegment(segment_index)
            except Exception:
                continue

            # Loại tiếp xúc tiếp tuyến hoặc sai số giao cực nhỏ.
            try:
                if intersect_segment.Length < MIN_INTERSECTION_LENGTH_FT:
                    continue
            except Exception:
                continue

            local_point = intersect_segment.Evaluate(0.5, True)

            selected_face = None
            selected_projection = None
            surface_normal_local = None

            if is_structural_framing:
                beam_face_data = choose_structural_framing_face(
                    solid,
                    local_point,
                    axis_direction_local,
                    beam_axis_local
                )
                if beam_face_data:
                    (selected_face,
                     selected_projection,
                     surface_normal_local) = beam_face_data
            else:
                min_distance = float("inf")
                for face in solid.Faces:
                    try:
                        projection = face.Project(local_point)
                    except Exception:
                        projection = None
                    if projection and projection.Distance < min_distance:
                        min_distance = projection.Distance
                        selected_face = face
                        selected_projection = projection

                if selected_face and selected_projection:
                    # Wall/Floor đang ở hệ tọa độ element vì geometry trực tiếp
                    # thường có transform Identity. Nếu có transform khác, normal
                    # fallback của face vẫn được đổi về hệ element ở bước dưới.
                    surface_normal_local = get_host_surface_normal_local(
                        host_element,
                        geometry_to_element_transform.OfPoint(local_point),
                        selected_face,
                        selected_projection
                    )
                    if surface_normal_local:
                        try:
                            surface_normal_local = (
                                geometry_to_element_transform.Inverse.OfVector(
                                    surface_normal_local
                                ).Normalize()
                            )
                        except Exception:
                            pass

            if (not selected_face
                    or not selected_projection
                    or not surface_normal_local):
                continue

            valid_angle, angle_to_normal, angle_to_surface = is_valid_penetration_angle(
                axis_direction_local,
                surface_normal_local
            )
            if not valid_angle:
                # Trục MEP chạy gần song song với bề mặt -> không đặt sleeve.
                continue

            host_reference = None
            if references_are_valid and selected_face.Reference:
                host_reference = create_host_reference_from_nested(
                    selected_face.Reference,
                    link_target.chain
                )

            try:
                point_in_element = geometry_to_element_transform.OfPoint(
                    local_point
                )
                global_point = link_target.transform.OfPoint(point_in_element)

                normal_in_element = geometry_to_element_transform.OfVector(
                    surface_normal_local
                ).Normalize()
                normal_global = link_target.transform.OfVector(
                    normal_in_element
                ).Normalize()

                thickness = get_thickness_by_ray(
                    solid,
                    local_point,
                    surface_normal_local
                )

                host_label = get_category_name(host_element)
                if is_structural_framing:
                    host_label = get_structural_framing_host_label(
                        normal_global
                    )

                return (
                    global_point,
                    host_reference,
                    normal_global,
                    thickness,
                    angle_to_surface,
                    angle_to_normal,
                    host_label
                )
            except Exception:
                continue

    return None


# ==============================================================================
# MAIN
# ==============================================================================
def get_selected_mep_elements():
    selected_elements = []
    selected_ids = uidoc.Selection.GetElementIds()

    if selected_ids:
        valid_categories = [
            int(BuiltInCategory.OST_PipeCurves),
            int(BuiltInCategory.OST_PipeAccessory),
            int(BuiltInCategory.OST_PipeFitting),
            int(BuiltInCategory.OST_PlumbingFixtures)
        ]
        for element_id in selected_ids:
            elem = doc.GetElement(element_id)
            try:
                if elem.Category and get_element_id_value(elem.Category.Id) in valid_categories:
                    selected_elements.append(elem)
            except Exception:
                pass

    if selected_elements:
        return selected_elements

    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element,
            PipeSelectionFilter(),
            "Chọn Pipe / Pipe Accessory / Pipe Fitting / Plumbing Fixture, sau đó nhấn Finish"
        )
        return [doc.GetElement(reference.ElementId) for reference in references]
    except OperationCanceledException:
        return []
    except Exception as ex:
        forms.alert("Không thể chọn đối tượng MEP:\n{}".format(ex), title="Auto Sleeve")
        return []


def pick_multiple_root_links():
    """Được gọi ngoài WPF ShowDialog để Revit có thể chuyển sang chế độ pick."""
    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element,
            RevitLinkSelectionFilter(),
            "Chọn nhiều Revit Link, sau đó nhấn Finish trên Options Bar"
        )
    except OperationCanceledException:
        return None
    except Exception as ex:
        forms.alert("Không thể pick Revit Link:\n{}".format(ex), title="Auto Sleeve")
        return None

    selected_ids = []
    seen = set()
    for reference in references:
        element_id = reference.ElementId
        elem = doc.GetElement(element_id)
        if not isinstance(elem, RevitLinkInstance):
            continue
        raw_id = get_element_id_value(elem.Id)
        if raw_id not in seen:
            seen.add(raw_id)
            selected_ids.append(raw_id)
    return selected_ids


def build_numeric_mapping(app):
    numeric_mapping = {}
    for cmb_size, cmb_type in app.rows_data:
        number = extract_single_number(safe_text(cmb_size.Text).strip())
        size_key = normalize_size_mm(number)
        type_text = safe_text(cmb_type.SelectedItem) if cmb_type.SelectedItem else ""
        if size_key is None or not type_text:
            continue

        symbol = next(
            (candidate for candidate in app.sleeve_types
             if get_symbol_display_name(candidate) == type_text),
            None
        )
        if symbol:
            numeric_mapping[size_key] = symbol
    return numeric_mapping


def collect_host_elements(link_target, scan_wall, scan_floor, scan_beam):
    elements = []
    if scan_wall:
        try:
            elements.extend(list(
                FilteredElementCollector(link_target.doc)
                .OfCategory(BuiltInCategory.OST_Walls)
                .WhereElementIsNotElementType()
                .ToElements()
            ))
        except Exception:
            pass

    if scan_floor:
        try:
            elements.extend(list(
                FilteredElementCollector(link_target.doc)
                .OfCategory(BuiltInCategory.OST_Floors)
                .WhereElementIsNotElementType()
                .ToElements()
            ))
        except Exception:
            pass

    if scan_beam:
        try:
            elements.extend(list(
                FilteredElementCollector(link_target.doc)
                .OfCategory(BuiltInCategory.OST_StructuralFraming)
                .WhereElementIsNotElementType()
                .ToElements()
            ))
        except Exception:
            pass

    return elements


def main():
    selected_elements = get_selected_mep_elements()
    if not selected_elements:
        return

    current_dir = os.path.dirname(__file__)
    xaml_path = os.path.join(current_dir, "ui.xaml")
    if not os.path.exists(xaml_path):
        forms.alert("Không tìm thấy file ui.xaml trong cùng thư mục với script.py.", exitscript=True)

    insulation_index = build_pipe_insulation_index(doc)
    available_required_sizes = collect_model_required_pipe_sizes(
        doc, insulation_index
    )

    ui_state = load_settings()
    app = None

    # Vòng lặp dialog: khi bấm Pick, đóng dialog trước; pick bên ngoài; sau đó mở dialog mới.
    while True:
        app = AutoSleeveApp(
            xaml_path,
            ui_state,
            available_required_sizes
        )
        app.show_dialog()
        ui_state = app.capture_state()

        if app.request_pick_links:
            picked_ids = pick_multiple_root_links()
            if picked_ids is not None:
                # Danh sách mới thay thế danh sách cũ để người dùng kiểm soát rõ kết quả pick.
                ui_state["root_link_ids"] = picked_ids
                save_settings(ui_state)
            continue

        if not app.dialog_result:
            return
        break

    numeric_mapping = build_numeric_mapping(app)
    if not numeric_mapping:
        forms.alert("Không có ánh xạ kích thước hợp lệ.", title="Auto Sleeve")
        return

    scan_wall = bool(ui_state.get("scan_wall", True))
    scan_floor = bool(ui_state.get("scan_floor", True))
    scan_beam = bool(ui_state.get("scan_beam", False))
    if not scan_wall and not scan_floor and not scan_beam:
        forms.alert(
            "Hãy chọn ít nhất Wall, Floor hoặc Structural Framing.",
            title="Auto Sleeve"
        )
        return

    selected_roots, link_targets = collect_targets_from_root_ids(ui_state.get("root_link_ids", []))
    if not selected_roots or not link_targets:
        forms.alert("Các link đã pick không còn tồn tại hoặc chưa được load.", title="Auto Sleeve")
        return

    is_replace_mode = bool(ui_state.get("replace_mode", False))

    existing_sleeves = list(
        FilteredElementCollector(doc)
        .OfClass(FamilyInstance)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    existing_sleeves = [
        sleeve for sleeve in existing_sleeves
        if sleeve.Symbol and sleeve.Symbol.Family
        and get_family_name(sleeve.Symbol) in FAMILY_CONFIG
    ]

    deleted_count = 0
    placed_count = 0
    placed_tracker = {}
    placed_records = []
    skipped_records = []
    skipped_record_keys = set()
    created_sleeve_ids = set()

    # Tính trước required diameter cho toàn bộ đối tượng MEP đã chọn.
    # Các size thiếu mapping vẫn được báo cáo, còn size hợp lệ vẫn tiếp tục chạy.
    selected_size_info = {}
    missing_size_records = []
    for mep_element in selected_elements:
        element_id_value = get_element_id_value(mep_element.Id)
        size_info = get_required_diameter_info(
            mep_element,
            insulation_index
        )
        selected_size_info[element_id_value] = size_info
        if not size_info:
            continue
        if size_info["required_size_key"] not in numeric_mapping:
            missing_size_records.append({
                "source_id": mep_element.Id,
                "source_category": get_category_name(mep_element),
                "nominal_diameter_mm": size_info["nominal_diameter_mm"],
                "insulation_thickness_mm": size_info["insulation_thickness_mm"],
                "required_diameter_mm": size_info["required_diameter_mm"]
            })

    with Transaction(doc, "Auto Place Sleeves MEP - Picked Links") as transaction:
        transaction.Start()

        for symbol in numeric_mapping.values():
            if not symbol.IsActive:
                symbol.Activate()
        doc.Regenerate()

        for link_target in link_targets:
            host_elements = collect_host_elements(link_target, scan_wall, scan_floor, scan_beam)
            if not host_elements:
                continue

            for host_element in host_elements:
                host_category_name = get_category_name(host_element)

                for mep_element in selected_elements:
                    size_info = selected_size_info.get(
                        get_element_id_value(mep_element.Id)
                    )
                    if not size_info:
                        continue

                    nominal_diameter_mm = size_info["nominal_diameter_mm"]
                    insulation_thickness_mm = size_info["insulation_thickness_mm"]
                    required_diameter_mm = size_info["required_diameter_mm"]
                    required_size_key = size_info["required_size_key"]

                    # Khớp bằng nhau theo key 0.1 mm; không lấy sleeve gần nhất.
                    chosen_symbol = numeric_mapping.get(required_size_key)
                    if not chosen_symbol:
                        continue

                    intersection_data = get_linked_face_reference(
                        link_target,
                        host_element,
                        mep_element
                    )
                    if intersection_data is None:
                        continue

                    (intersection_point, face_reference, global_normal, thickness_value,
                     angle_to_surface, angle_to_normal, host_surface_label) = intersection_data
                    pipe_curve_global = get_axis_curve(mep_element)
                    is_duplicate = False
                    sleeve_to_delete = None

                    for existing_sleeve in list(existing_sleeves):
                        try:
                            sleeve_location = existing_sleeve.Location
                            if not isinstance(sleeve_location, LocationPoint):
                                continue
                            sleeve_point = sleeve_location.Point
                        except Exception:
                            continue

                        if sleeve_point.DistanceTo(intersection_point) >= 0.5:
                            continue

                        if pipe_curve_global:
                            try:
                                projection = pipe_curve_global.Project(sleeve_point)
                            except Exception:
                                projection = None
                            if projection and projection.Distance < 0.3:
                                is_duplicate = True
                                sleeve_to_delete = existing_sleeve
                                break

                    if is_duplicate:
                        duplicate_id_value = get_element_id_value(sleeve_to_delete.Id)

                        # Không xử lý lại sleeve vừa tạo trong chính lần chạy hiện tại.
                        if duplicate_id_value in created_sleeve_ids:
                            continue

                        if not is_replace_mode:
                            skip_key = (
                                duplicate_id_value,
                                get_element_id_value(mep_element.Id),
                                link_target.path_name
                            )
                            if skip_key not in skipped_record_keys:
                                skipped_record_keys.add(skip_key)
                                skipped_records.append({
                                    "sleeve_id": sleeve_to_delete.Id,
                                    "source_id": mep_element.Id,
                                    "nominal_diameter_mm": nominal_diameter_mm,
                                    "insulation_thickness_mm": insulation_thickness_mm,
                                    "required_diameter_mm": required_diameter_mm,
                                    "link_name": link_target.path_name
                                })
                            continue

                        try:
                            doc.Delete(sleeve_to_delete.Id)
                            if sleeve_to_delete in existing_sleeves:
                                existing_sleeves.remove(sleeve_to_delete)
                            deleted_count += 1
                        except Exception:
                            pass

                    try:
                        if face_reference:
                            reference_direction = XYZ.BasisZ
                            if abs(global_normal.Z) > 0.9:
                                reference_direction = XYZ.BasisX
                            new_sleeve = doc.Create.NewFamilyInstance(
                                face_reference,
                                intersection_point,
                                reference_direction,
                                chosen_symbol
                            )
                        else:
                            new_sleeve = doc.Create.NewFamilyInstance(
                                intersection_point,
                                chosen_symbol,
                                StructuralType.NonStructural
                            )
                    except Exception as create_error:
                        logger.warning(
                            "Không tạo được sleeve tại {} / {}: {}".format(
                                link_target.path_name,
                                get_element_id_value(host_element.Id),
                                create_error
                            )
                        )
                        continue

                    if thickness_value:
                        family_name = get_family_name(chosen_symbol)
                        parameter_name = FAMILY_CONFIG.get(family_name, "NWCH_PEN_Thickness")
                        parameter = new_sleeve.LookupParameter(parameter_name)
                        if parameter and not parameter.IsReadOnly:
                            try:
                                parameter.Set(thickness_value)
                            except Exception:
                                pass

                    # Không cộng/trừ nửa chiều dày host. Với family sleeve này,
                    # Offset from Host = 0.0 đã là đúng tâm chiều dài của host.
                    # Revit có thể tự sinh offset khi điểm tạo nằm giữa solid,
                    # nên luôn ép lại về 0 ngay sau khi tạo.
                    reset_sleeve_host_offset(new_sleeve)

                    type_display = get_symbol_display_name(chosen_symbol)
                    tracker_key = (type_display, required_diameter_mm)
                    if tracker_key not in placed_tracker:
                        placed_tracker[tracker_key] = []
                    placed_tracker[tracker_key].append(new_sleeve.Id)

                    placed_records.append({
                        "sleeve_id": new_sleeve.Id,
                        "source_id": mep_element.Id,
                        "source_category": get_category_name(mep_element),
                        "nominal_diameter_mm": nominal_diameter_mm,
                        "insulation_thickness_mm": insulation_thickness_mm,
                        "required_diameter_mm": required_diameter_mm,
                        "sleeve_type": type_display,
                        "host_category": host_surface_label or host_category_name,
                        "link_name": link_target.path_name,
                        "host_id": get_element_id_value(host_element.Id),
                        "angle_to_surface": angle_to_surface,
                        "angle_to_normal": angle_to_normal
                    })

                    existing_sleeves.append(new_sleeve)
                    created_sleeve_ids.add(get_element_id_value(new_sleeve.Id))
                    placed_count += 1

        transaction.Commit()

    save_settings(ui_state)
    print_pyrevit_report(
        placed_records,
        skipped_records,
        missing_size_records,
        deleted_count,
        placed_count,
        scan_wall,
        scan_floor,
        scan_beam
    )

    # Chỉ sử dụng pyRevit Output cho báo cáo sau khi chạy.
    # Không mở cửa sổ WPF ReportApp và không hiện forms.alert kết quả.


if __name__ == "__main__":
    main()
