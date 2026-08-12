# -*- coding: utf-8 -*-
"""
Room Filled Region From Multiple Links
Compatible with pyRevit + IronPython 2.7 / Revit 2019-2026.

Features:
- Select any linked Room parameter, then match multi-line keywords against its value.
- Pick one or multiple Revit Link instances, then press Finish.
- Create, replace, or delete Filled Regions matching linked Rooms.
- Keep Room holes and transform linked boundaries to host coordinates.
- Optionally write linked Room Name to the Filled Region instance Comments parameter.
- Optionally write the active view Level name to a selected writable Filled Region instance Text parameter.
- Show one pyRevit Output window only, with Element ID and built-in zoom icon.
"""

import clr
import System
import traceback

clr.AddReference("System")
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")

from System.Collections.Generic import List
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.DB.ExtensibleStorage import (
    Schema, SchemaBuilder, Entity, AccessLevel
)
from pyrevit import revit, DB, forms, script


doc = revit.doc
uidoc = revit.uidoc
view = revit.active_view

TOOL_MARKER_PREFIX = u"PYREVIT_ROOM_FILLED_REGION|"
MODE_REPLACE = u"replace"
MODE_CREATE_ONLY = u"create_only"
MODE_DELETE_ONLY = u"delete_only"

REGION_MARKER_SCHEMA_GUID = System.Guid(
    "6D4F460B-111B-4A1C-A4D9-58B9AC208CDD"
)
REGION_MARKER_SCHEMA_NAME = "AnToolsRoomFilledRegionMarker"
REGION_MARKER_FIELD_NAME = "Marker"


# -----------------------------------------------------------------------------
# Compatibility helpers
# -----------------------------------------------------------------------------

def element_id_value(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        return int(element_id.IntegerValue)


def mm_to_internal(value_mm):
    value_mm = float(value_mm)
    try:
        return DB.UnitUtils.ConvertToInternalUnits(
            value_mm,
            DB.UnitTypeId.Millimeters
        )
    except Exception:
        return DB.UnitUtils.ConvertToInternalUnits(
            value_mm,
            DB.DisplayUnitType.DUT_MILLIMETERS
        )


def get_element_name(element):
    if element is None:
        return u""
    try:
        value = element.Name
        if value:
            return value
    except Exception:
        pass
    try:
        return DB.Element.Name.GetValue(element) or u""
    except Exception:
        return u""


def safe_text(value):
    if value is None:
        return u""
    try:
        return unicode(value)
    except Exception:
        return str(value)


def parse_float(text, default_value):
    try:
        return float(safe_text(text).strip().replace(",", "."))
    except Exception:
        return float(default_value)


def same_element_id(first_id, second_id):
    if first_id is None or second_id is None:
        return False
    try:
        return element_id_value(first_id) == element_id_value(second_id)
    except Exception:
        return False


def set_selection_and_zoom(element_ids):
    if not element_ids:
        return

    selected_ids = List[DB.ElementId]()
    for element_id in element_ids:
        selected_ids.Add(element_id)

    uidoc.Selection.SetElementIds(selected_ids)
    try:
        uidoc.ShowElements(selected_ids)
    except Exception:
        try:
            uidoc.ShowElements(element_ids[0])
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Preconditions
# -----------------------------------------------------------------------------

def validate_active_view():
    if doc.IsFamilyDocument:
        forms.alert(
            u"Tool chỉ chạy trong Project, không chạy trong Family Editor.",
            exitscript=True
        )

    if view is None or view.IsTemplate:
        forms.alert(u"Active View không hợp lệ.", exitscript=True)

    allowed_types = []
    for attr_name in ["FloorPlan", "CeilingPlan", "EngineeringPlan"]:
        try:
            allowed_types.append(getattr(DB.ViewType, attr_name))
        except Exception:
            pass

    if view.ViewType not in allowed_types or view.GenLevel is None:
        forms.alert(
            u"Hãy chạy tool trong Floor Plan, Ceiling Plan hoặc Engineering Plan có Level.",
            exitscript=True
        )


validate_active_view()


# -----------------------------------------------------------------------------
# Data collection
# -----------------------------------------------------------------------------

def collect_filled_region_types():
    result = []
    collector = DB.FilteredElementCollector(doc).OfClass(DB.FilledRegionType)
    for item in collector:
        result.append(item)
    result.sort(key=lambda x: get_element_name(x).lower())
    return result


FILLED_REGION_TYPES = collect_filled_region_types()
if not FILLED_REGION_TYPES:
    forms.alert(
        u"Model hiện tại chưa có Filled Region Type nào.",
        exitscript=True
    )


def create_temporary_region_loops():
    target_z = view.GenLevel.Elevation
    size = mm_to_internal(100.0)

    p0 = DB.XYZ(0.0, 0.0, target_z)
    p1 = DB.XYZ(size, 0.0, target_z)
    p2 = DB.XYZ(size, size, target_z)
    p3 = DB.XYZ(0.0, size, target_z)

    loop = DB.CurveLoop()
    loop.Append(DB.Line.CreateBound(p0, p1))
    loop.Append(DB.Line.CreateBound(p1, p2))
    loop.Append(DB.Line.CreateBound(p2, p3))
    loop.Append(DB.Line.CreateBound(p3, p0))

    loops = List[DB.CurveLoop]()
    loops.Add(loop)
    return loops


def parameter_id_value(parameter):
    if parameter is None:
        return None
    try:
        return element_id_value(parameter.Id)
    except Exception:
        return None


def collect_writable_filled_region_text_parameters(region_types):
    descriptors = []
    sample_region = None
    temporary_transaction = None

    try:
        sample_region = (
            DB.FilteredElementCollector(doc)
              .OfClass(DB.FilledRegion)
              .WhereElementIsNotElementType()
              .FirstElement()
        )
    except Exception:
        sample_region = None

    try:
        if sample_region is None:
            temporary_transaction = DB.Transaction(
                doc,
                "Read Filled Region Text Parameters"
            )
            temporary_transaction.Start()
            sample_region = DB.FilledRegion.Create(
                doc,
                region_types[0].Id,
                view.Id,
                create_temporary_region_loops()
            )
            doc.Regenerate()

        comments_parameter = None
        mark_parameter = None
        try:
            comments_parameter = sample_region.get_Parameter(
                DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
            )
        except Exception:
            comments_parameter = None
        try:
            mark_parameter = sample_region.get_Parameter(
                DB.BuiltInParameter.ALL_MODEL_MARK
            )
        except Exception:
            mark_parameter = None

        comments_id = parameter_id_value(comments_parameter)
        mark_id = parameter_id_value(mark_parameter)
        seen_ids = set()

        for parameter in sample_region.Parameters:
            try:
                if parameter.StorageType != DB.StorageType.String:
                    continue
                if parameter.IsReadOnly:
                    continue

                current_id = parameter_id_value(parameter)
                if current_id is None or current_id in seen_ids:
                    continue
                if comments_id is not None and current_id == comments_id:
                    continue

                definition = parameter.Definition
                if definition is None:
                    continue
                name = safe_text(definition.Name).strip()
                if not name:
                    continue

                seen_ids.add(current_id)
                descriptors.append({
                    "id_value": current_id,
                    "name": name,
                    "is_mark": mark_id is not None and current_id == mark_id,
                })
            except Exception:
                continue
    except Exception:
        descriptors = []
    finally:
        if temporary_transaction is not None:
            try:
                if temporary_transaction.GetStatus() == DB.TransactionStatus.Started:
                    temporary_transaction.RollBack()
            except Exception:
                pass

    descriptors.sort(key=lambda item: item["name"].lower())
    return descriptors


FILLED_REGION_TEXT_PARAMETERS = (
    collect_writable_filled_region_text_parameters(FILLED_REGION_TYPES)
)
ACTIVE_LEVEL_NAME = get_element_name(view.GenLevel).strip()


BOUNDARY_ITEMS = [
    (u"Finish - mặt hoàn thiện tường", DB.SpatialElementBoundaryLocation.Finish),
    (u"Center - tim tường", DB.SpatialElementBoundaryLocation.Center),
    (u"Core Boundary - mặt lõi tường", DB.SpatialElementBoundaryLocation.CoreBoundary),
    (u"Core Center - tim lõi tường", DB.SpatialElementBoundaryLocation.CoreCenter),
]


# -----------------------------------------------------------------------------
# Multiple link selection
# -----------------------------------------------------------------------------

class RevitLinkSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, DB.RevitLinkInstance)

    def AllowReference(self, reference, position):
        return False


try:
    picked_refs = uidoc.Selection.PickObjects(
        ObjectType.Element,
        RevitLinkSelectionFilter(),
        u"Chọn một hoặc nhiều Revit Link chứa Room, sau đó nhấn Finish"
    )
except OperationCanceledException:
    script.exit()

if picked_refs is None or picked_refs.Count == 0:
    script.exit()

link_contexts = []
skipped_links = []
seen_link_ids = set()

for picked_ref in picked_refs:
    link_instance = doc.GetElement(picked_ref.ElementId)
    if link_instance is None:
        continue

    link_id_value = element_id_value(link_instance.Id)
    if link_id_value in seen_link_ids:
        continue
    seen_link_ids.add(link_id_value)

    link_doc = link_instance.GetLinkDocument()
    link_name = get_element_name(link_instance)
    if link_doc is None:
        skipped_links.append((
            link_name or u"Revit Link ID {}".format(link_id_value),
            u"Link đang Unloaded hoặc không thể đọc dữ liệu"
        ))
        continue

    try:
        link_transform = link_instance.GetTotalTransform()
    except Exception:
        link_transform = link_instance.GetTransform()

    link_contexts.append({
        "instance": link_instance,
        "doc": link_doc,
        "transform": link_transform,
        "name": link_name or u"Revit Link ID {}".format(link_id_value),
    })

if not link_contexts:
    unloaded_names = [item[0] for item in skipped_links]
    details = u"\n".join(unloaded_names)
    forms.alert(
        u"Không có Revit Link đang Loaded để đọc Room.{}".format(
            u"\n\nLink không đọc được:\n{}".format(details) if details else u""
        ),
        exitscript=True
    )


# -----------------------------------------------------------------------------
# Linked Room parameter collection
# -----------------------------------------------------------------------------

def storage_type_label(storage_type):
    try:
        if storage_type == DB.StorageType.String:
            return u"Text"
        if storage_type == DB.StorageType.Integer:
            return u"Integer / Yes-No"
        if storage_type == DB.StorageType.Double:
            return u"Number / Length"
        if storage_type == DB.StorageType.ElementId:
            return u"Element"
    except Exception:
        pass
    return safe_text(storage_type)


def get_parameter_shared_guid(parameter):
    try:
        if parameter.IsShared:
            return safe_text(parameter.GUID).strip().lower()
    except Exception:
        pass
    return u""


def build_room_parameter_descriptor(parameter):
    if parameter is None:
        return None

    try:
        definition = parameter.Definition
        name = safe_text(definition.Name).strip() if definition else u""
        if not name:
            return None

        storage_type = parameter.StorageType
        storage_key = safe_text(storage_type)
        parameter_id = parameter_id_value(parameter)
        shared_guid = get_parameter_shared_guid(parameter)

        if parameter_id is not None and parameter_id < 0:
            key = u"BIP:{}".format(parameter_id)
            source = u"Built-in"
        elif shared_guid:
            key = u"GUID:{}".format(shared_guid)
            source = u"Shared"
        else:
            key = u"NAME:{}|ST:{}".format(name.lower(), storage_key)
            source = u"Project"

        return {
            "key": key,
            "name": name,
            "storage_key": storage_key,
            "storage_label": storage_type_label(storage_type),
            "source": source,
            "parameter_id": parameter_id,
            "shared_guid": shared_guid,
            "link_names": set(),
            "room_count": 0,
        }
    except Exception:
        return None


def collect_linked_room_filter_parameters(contexts):
    descriptor_by_key = {}
    scanned_room_count = 0
    scanned_document_keys = set()

    for context in contexts:
        link_doc = context["doc"]
        link_name = context["name"]

        # The same linked document can be placed more than once. Its Room
        # parameter definitions only need to be scanned once.
        try:
            document_key = safe_text(link_doc.PathName).lower()
        except Exception:
            document_key = u""
        if not document_key:
            try:
                document_key = safe_text(link_doc.Title).lower()
            except Exception:
                document_key = safe_text(id(link_doc))

        if document_key in scanned_document_keys:
            continue
        scanned_document_keys.add(document_key)

        try:
            rooms = list(
                DB.FilteredElementCollector(link_doc)
                  .OfCategory(DB.BuiltInCategory.OST_Rooms)
                  .WhereElementIsNotElementType()
            )
        except Exception:
            continue

        scanned_room_count += len(rooms)
        for room in rooms:
            try:
                parameters = room.Parameters
            except Exception:
                continue

            for parameter in parameters:
                descriptor = build_room_parameter_descriptor(parameter)
                if descriptor is None:
                    continue

                key = descriptor["key"]
                existing = descriptor_by_key.get(key)
                if existing is None:
                    existing = descriptor
                    descriptor_by_key[key] = existing

                existing["link_names"].add(link_name)
                existing["room_count"] += 1

    descriptors = list(descriptor_by_key.values())
    descriptors.sort(
        key=lambda item: (
            0 if item["key"].startswith(u"BIP:") else 1,
            item["name"].lower(),
            item["source"].lower(),
            item["key"].lower()
        )
    )
    return descriptors, scanned_room_count


def room_name_parameter_key():
    try:
        built_in_value = int(
            System.Convert.ToInt32(DB.BuiltInParameter.ROOM_NAME)
        )
        return u"BIP:{}".format(built_in_value)
    except Exception:
        return u""


ROOM_FILTER_PARAMETERS, PARAMETER_SCAN_ROOM_COUNT = (
    collect_linked_room_filter_parameters(link_contexts)
)

if not ROOM_FILTER_PARAMETERS:
    forms.alert(
        u"Không tìm thấy parameter nào trên Room trong các Revit Link đã chọn.",
        exitscript=True
    )


# -----------------------------------------------------------------------------
# Settings window
# -----------------------------------------------------------------------------

class SettingsWindow(forms.WPFWindow):
    def __init__(
            self,
            xaml_file,
            region_types,
            text_parameters,
            room_filter_parameters,
            active_level_name,
            link_contexts,
            parameter_scan_room_count,
            config):
        forms.WPFWindow.__init__(self, xaml_file)

        self.accepted = False
        self.region_types = region_types
        self.text_parameters = text_parameters
        self.room_filter_parameters = room_filter_parameters
        self.active_level_name = active_level_name
        self.link_contexts = link_contexts
        self.parameter_scan_room_count = parameter_scan_room_count
        self.config = config

        self.region_type_by_label = {}
        region_labels = []
        for region_type in region_types:
            label = get_element_name(region_type)
            if label in self.region_type_by_label:
                label = u"{}  [ID {}]".format(
                    label,
                    element_id_value(region_type.Id)
                )
            self.region_type_by_label[label] = region_type
            region_labels.append(label)

        self.cmbRegionType.ItemsSource = region_labels
        self.cmbBoundary.ItemsSource = [item[0] for item in BOUNDARY_ITEMS]

        self.room_filter_parameter_by_label = {}
        room_filter_labels = []
        for descriptor in room_filter_parameters:
            label = u"{}  [{} | {}]".format(
                descriptor["name"],
                descriptor["source"],
                descriptor["storage_label"]
            )
            if label in self.room_filter_parameter_by_label:
                suffix = descriptor["shared_guid"][:8] if descriptor["shared_guid"] else descriptor["key"]
                label = u"{}  [{}]".format(label, suffix)

            self.room_filter_parameter_by_label[label] = descriptor
            room_filter_labels.append(label)

        self.cmbRoomFilterParameter.ItemsSource = room_filter_labels

        saved_room_filter_key = safe_text(
            getattr(config, "room_filter_parameter_key", u"")
        )
        default_room_name_key = room_name_parameter_key()
        selected_room_filter_index = -1

        preferred_keys = []
        if saved_room_filter_key:
            preferred_keys.append(saved_room_filter_key)
        if default_room_name_key and default_room_name_key not in preferred_keys:
            preferred_keys.append(default_room_name_key)

        for preferred_key in preferred_keys:
            for index, label in enumerate(room_filter_labels):
                descriptor = self.room_filter_parameter_by_label[label]
                if descriptor["key"] == preferred_key:
                    selected_room_filter_index = index
                    break
            if selected_room_filter_index >= 0:
                break

        if selected_room_filter_index < 0 and room_filter_labels:
            selected_room_filter_index = 0
        self.cmbRoomFilterParameter.SelectedIndex = selected_room_filter_index

        self.txtRoomParameterStatus.Text = (
            u"Đã quét {} parameter từ {} Room trong {} link đang Loaded. "
            u"Parameter được chọn sẽ dùng để so khớp với các từ khóa bên dưới."
        ).format(
            len(room_filter_parameters),
            parameter_scan_room_count,
            len(link_contexts)
        )
        try:
            self.txtRoomParameterStatus.ToolTip = u"\n".join(
                [context["name"] for context in link_contexts]
            )
        except Exception:
            pass

        self.level_parameter_by_label = {}
        level_parameter_labels = []
        for descriptor in text_parameters:
            label = descriptor["name"]
            if label in self.level_parameter_by_label:
                label = u"{}  [ID {}]".format(
                    label,
                    descriptor["id_value"]
                )
            self.level_parameter_by_label[label] = descriptor
            level_parameter_labels.append(label)

        self.cmbLevelParameter.ItemsSource = level_parameter_labels
        self.txtActiveLevelName.Text = active_level_name or u"(Không đọc được Level)"

        self.txtKeyword.Text = safe_text(getattr(config, "keyword", u""))
        self.txtLevelTolerance.Text = safe_text(
            getattr(config, "level_tolerance_mm", u"500")
        )
        self.chkCropOnly.IsChecked = bool(
            getattr(config, "crop_only", True)
        )
        self.chkMatchPhase.IsChecked = bool(
            getattr(config, "match_phase", True)
        )
        self.chkFillRoomNameToComments.IsChecked = bool(
            getattr(
                config,
                "fill_room_name_to_comments",
                getattr(config, "fill_room_name_to_mark", True)
            )
        )
        self.chkFillLevelToParameter.IsChecked = bool(
            getattr(config, "fill_level_to_parameter", True)
        )

        saved_level_parameter_id = safe_text(
            getattr(config, "level_parameter_id", u"")
        )
        selected_level_parameter_index = -1
        for index, label in enumerate(level_parameter_labels):
            descriptor = self.level_parameter_by_label[label]
            if safe_text(descriptor["id_value"]) == saved_level_parameter_id:
                selected_level_parameter_index = index
                break
        if selected_level_parameter_index < 0 and level_parameter_labels:
            selected_level_parameter_index = 0
        self.cmbLevelParameter.SelectedIndex = selected_level_parameter_index

        if not level_parameter_labels:
            self.chkFillLevelToParameter.IsChecked = False
            self.chkFillLevelToParameter.IsEnabled = False
            self.cmbLevelParameter.IsEnabled = False
            self.txtLevelParameterStatus.Text = (
                u"Không tìm thấy instance parameter kiểu Text có thể ghi. "
                u"Comments đã được loại khỏi danh sách vì được dùng để ghi Room Name."
            )
        else:
            self.txtLevelParameterStatus.Text = (
                u"Danh sách chỉ gồm instance parameter kiểu Text có thể ghi. "
                u"Comments không được hiển thị để tránh ghi đè Room Name."
            )

        saved_type_id = safe_text(getattr(config, "region_type_id", u""))
        selected_region_index = 0
        for index, label in enumerate(region_labels):
            region_type = self.region_type_by_label[label]
            if safe_text(element_id_value(region_type.Id)) == saved_type_id:
                selected_region_index = index
                break
        self.cmbRegionType.SelectedIndex = selected_region_index

        saved_boundary_index = int(getattr(config, "boundary_index", 0))
        if saved_boundary_index < 0 or saved_boundary_index >= len(BOUNDARY_ITEMS):
            saved_boundary_index = 0
        self.cmbBoundary.SelectedIndex = saved_boundary_index

        saved_mode = safe_text(getattr(config, "operation_mode", MODE_REPLACE))
        if saved_mode == MODE_CREATE_ONLY:
            self.rdoCreateOnly.IsChecked = True
        elif saved_mode == MODE_DELETE_ONLY:
            self.rdoDeleteOnly.IsChecked = True
        else:
            self.rdoReplace.IsChecked = True

        self.chkFillLevelToParameter.Checked += self.level_option_changed
        self.chkFillLevelToParameter.Unchecked += self.level_option_changed
        self.level_option_changed(None, None)

        self.btnCreate.Click += self.create_click
        self.btnCancel.Click += self.cancel_click

    def level_option_changed(self, sender, args):
        has_parameters = bool(self.level_parameter_by_label)
        self.cmbLevelParameter.IsEnabled = (
            has_parameters and bool(self.chkFillLevelToParameter.IsChecked)
        )

    def create_click(self, sender, args):
        keyword_text = safe_text(self.txtKeyword.Text).strip()
        if not keyword_text:
            forms.alert(u"Hãy nhập giá trị parameter cần tìm.")
            return

        selected_room_filter_label = self.cmbRoomFilterParameter.SelectedItem
        if selected_room_filter_label is None:
            forms.alert(u"Hãy chọn parameter của Room dùng để lọc.")
            return

        if self.cmbRegionType.SelectedItem is None:
            forms.alert(u"Hãy chọn Filled Region Type.")
            return

        tolerance_mm = parse_float(self.txtLevelTolerance.Text, 500.0)
        if tolerance_mm < 0:
            forms.alert(u"Sai số cao độ phải lớn hơn hoặc bằng 0 mm.")
            return

        if bool(self.rdoDeleteOnly.IsChecked):
            operation_mode = MODE_DELETE_ONLY
        elif bool(self.rdoCreateOnly.IsChecked):
            operation_mode = MODE_CREATE_ONLY
        else:
            operation_mode = MODE_REPLACE

        fill_level_to_parameter = bool(
            self.chkFillLevelToParameter.IsChecked
        )
        selected_level_parameter = None
        selected_label = self.cmbLevelParameter.SelectedItem
        if selected_label is not None:
            selected_level_parameter = self.level_parameter_by_label[
                safe_text(selected_label)
            ]

        if (
                fill_level_to_parameter
                and operation_mode != MODE_DELETE_ONLY
                and selected_level_parameter is None):
            forms.alert(
                u"Hãy chọn instance parameter để điền tên Level."
            )
            return

        self.keyword_text = keyword_text
        self.room_filter_parameter = self.room_filter_parameter_by_label[
            safe_text(selected_room_filter_label)
        ]
        self.region_type = self.region_type_by_label[
            safe_text(self.cmbRegionType.SelectedItem)
        ]
        self.boundary_index = int(self.cmbBoundary.SelectedIndex)
        self.boundary_location = BOUNDARY_ITEMS[self.boundary_index][1]
        self.level_tolerance_mm = tolerance_mm
        self.crop_only = bool(self.chkCropOnly.IsChecked)
        self.match_phase = bool(self.chkMatchPhase.IsChecked)
        self.fill_room_name_to_comments = bool(
            self.chkFillRoomNameToComments.IsChecked
        )
        self.fill_level_to_parameter = fill_level_to_parameter
        self.level_parameter = selected_level_parameter
        self.operation_mode = operation_mode
        self.accepted = True
        self.Close()

    def cancel_click(self, sender, args):
        self.accepted = False
        self.Close()


config = script.get_config()
xaml_path = script.get_bundle_file("ui.xaml")
window = SettingsWindow(
    xaml_path,
    FILLED_REGION_TYPES,
    FILLED_REGION_TEXT_PARAMETERS,
    ROOM_FILTER_PARAMETERS,
    ACTIVE_LEVEL_NAME,
    link_contexts,
    PARAMETER_SCAN_ROOM_COUNT,
    config
)
window.ShowDialog()

if not window.accepted:
    script.exit()

config.keyword = window.keyword_text
config.room_filter_parameter_key = window.room_filter_parameter["key"]
config.region_type_id = safe_text(element_id_value(window.region_type.Id))
config.boundary_index = window.boundary_index
config.level_tolerance_mm = safe_text(window.level_tolerance_mm)
config.crop_only = window.crop_only
config.match_phase = window.match_phase
config.fill_room_name_to_comments = window.fill_room_name_to_comments
config.fill_level_to_parameter = window.fill_level_to_parameter
config.level_parameter_id = (
    safe_text(window.level_parameter["id_value"])
    if window.level_parameter is not None else u""
)
config.operation_mode = window.operation_mode
script.save_config()


# -----------------------------------------------------------------------------
# Room filtering helpers
# -----------------------------------------------------------------------------

def split_keywords(text):
    normalized = safe_text(text)
    normalized = normalized.replace(u"\r\n", u";")
    normalized = normalized.replace(u"\r", u";")
    normalized = normalized.replace(u"\n", u";")
    normalized = normalized.replace(u",", u";")

    result = []
    existing = set()
    for item in normalized.split(u";"):
        item = item.strip().lower()
        if item and item not in existing:
            existing.add(item)
            result.append(item)
    return result


def room_name(room):
    try:
        parameter = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
        if parameter:
            return safe_text(parameter.AsString())
    except Exception:
        pass
    return u""


def room_number(room):
    try:
        parameter = room.get_Parameter(DB.BuiltInParameter.ROOM_NUMBER)
        if parameter:
            return safe_text(parameter.AsString())
    except Exception:
        pass
    return u""


def find_room_parameter_by_descriptor(room, descriptor):
    if room is None or descriptor is None:
        return None

    target_key = descriptor.get("key", u"")
    if not target_key:
        return None

    for parameter in room.Parameters:
        try:
            current_descriptor = build_room_parameter_descriptor(parameter)
            if current_descriptor and current_descriptor["key"] == target_key:
                return parameter
        except Exception:
            continue
    return None


def parameter_value_for_keyword(parameter, parameter_doc):
    if parameter is None:
        return u""

    values = []

    def append_value(value):
        text_value = safe_text(value).strip()
        if text_value and text_value not in values:
            values.append(text_value)

    try:
        append_value(parameter.AsString())
    except Exception:
        pass

    try:
        append_value(parameter.AsValueString())
    except Exception:
        pass

    try:
        if parameter.StorageType == DB.StorageType.Integer:
            append_value(parameter.AsInteger())
        elif parameter.StorageType == DB.StorageType.Double:
            append_value(parameter.AsDouble())
        elif parameter.StorageType == DB.StorageType.ElementId:
            element_id = parameter.AsElementId()
            if element_id is not None:
                linked_element = parameter_doc.GetElement(element_id)
                if linked_element is not None:
                    append_value(get_element_name(linked_element))
                append_value(element_id_value(element_id))
    except Exception:
        pass

    return u" | ".join(values)


def room_filter_parameter_value(room, link_doc, descriptor):
    parameter = find_room_parameter_by_descriptor(room, descriptor)
    return parameter, parameter_value_for_keyword(parameter, link_doc)


def room_has_area(room):
    try:
        return room.Area > 1e-9
    except Exception:
        return False


def keyword_matches(name, keywords):
    value = safe_text(name).lower()
    for keyword in keywords:
        if keyword in value:
            return True
    return False


def get_active_view_phase_name():
    try:
        parameter = view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE)
        if parameter is None:
            return u""
        phase = doc.GetElement(parameter.AsElementId())
        return get_element_name(phase)
    except Exception:
        return u""


def find_link_phase_id_by_name(link_doc, phase_name):
    if not phase_name:
        return None
    try:
        for phase in link_doc.Phases:
            if get_element_name(phase).strip().lower() == phase_name.strip().lower():
                return phase.Id
    except Exception:
        pass
    return None


def room_phase_matches(room, target_phase_id):
    if target_phase_id is None:
        return True
    try:
        parameter = room.get_Parameter(DB.BuiltInParameter.ROOM_PHASE)
        if parameter is None:
            return True
        return same_element_id(parameter.AsElementId(), target_phase_id)
    except Exception:
        return True


def room_matches_active_level(room, link_doc, link_transform, tolerance_internal):
    try:
        linked_level = link_doc.GetElement(room.LevelId)
        if linked_level is None:
            return True

        linked_level_point = DB.XYZ(0.0, 0.0, linked_level.Elevation)
        host_level_point = link_transform.OfPoint(linked_level_point)
        active_level_z = view.GenLevel.Elevation
        return abs(host_level_point.Z - active_level_z) <= tolerance_internal
    except Exception:
        return True


def bbox_corners(bbox):
    min_pt = bbox.Min
    max_pt = bbox.Max
    result = []
    for x in [min_pt.X, max_pt.X]:
        for y in [min_pt.Y, max_pt.Y]:
            for z in [min_pt.Z, max_pt.Z]:
                result.append(DB.XYZ(x, y, z))
    return result


def room_intersects_active_crop(room, link_transform, crop_only):
    if not crop_only:
        return True

    try:
        if not view.CropBoxActive:
            return True

        room_bbox = room.get_BoundingBox(None)
        crop_bbox = view.CropBox
        if room_bbox is None or crop_bbox is None:
            return True

        crop_inverse = crop_bbox.Transform.Inverse
        local_points = []
        for point in bbox_corners(room_bbox):
            host_point = link_transform.OfPoint(point)
            local_points.append(crop_inverse.OfPoint(host_point))

        room_min_x = min([point.X for point in local_points])
        room_max_x = max([point.X for point in local_points])
        room_min_y = min([point.Y for point in local_points])
        room_max_y = max([point.Y for point in local_points])

        crop_min_x = crop_bbox.Min.X
        crop_max_x = crop_bbox.Max.X
        crop_min_y = crop_bbox.Min.Y
        crop_max_y = crop_bbox.Max.Y

        if room_max_x < crop_min_x or room_min_x > crop_max_x:
            return False
        if room_max_y < crop_min_y or room_min_y > crop_max_y:
            return False
        return True
    except Exception:
        return True


# -----------------------------------------------------------------------------
# Boundary conversion and bounds
# -----------------------------------------------------------------------------

def boundary_loop_to_host_curve_loop(boundary_segments, link_transform, target_z):
    if boundary_segments is None or boundary_segments.Count == 0:
        return None

    transformed_curves = []
    source_z = None

    for segment in boundary_segments:
        source_curve = segment.GetCurve()
        if source_curve is None:
            continue

        host_curve = source_curve.CreateTransformed(link_transform)
        if source_z is None:
            source_z = host_curve.GetEndPoint(0).Z
        transformed_curves.append(host_curve)

    if not transformed_curves or source_z is None:
        return None

    translation = DB.Transform.CreateTranslation(
        DB.XYZ(0.0, 0.0, target_z - source_z)
    )

    curve_loop = DB.CurveLoop()
    for host_curve in transformed_curves:
        flattened_curve = host_curve.CreateTransformed(translation)
        curve_loop.Append(flattened_curve)

    return curve_loop


def get_room_curve_loops(room, boundary_location, link_transform):
    options = DB.SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = boundary_location

    boundary_result = room.GetBoundarySegments(options)
    if boundary_result is None or boundary_result.Count == 0:
        return None

    target_z = view.GenLevel.Elevation
    loops = List[DB.CurveLoop]()

    for boundary_segments in boundary_result:
        curve_loop = boundary_loop_to_host_curve_loop(
            boundary_segments,
            link_transform,
            target_z
        )
        if curve_loop is not None:
            loops.Add(curve_loop)

    if loops.Count == 0:
        return None
    return loops


def iter_curve_points(curve):
    try:
        points = curve.Tessellate()
        if points is not None and points.Count > 0:
            for point in points:
                yield point
            return
    except Exception:
        pass

    try:
        yield curve.GetEndPoint(0)
        yield curve.GetEndPoint(1)
    except Exception:
        return


def curve_loops_xy_bounds(curve_loops):
    if curve_loops is None:
        return None

    xs = []
    ys = []
    for curve_loop in curve_loops:
        for curve in curve_loop:
            for point in iter_curve_points(curve):
                xs.append(point.X)
                ys.append(point.Y)

    if not xs or not ys:
        return None

    return (min(xs), min(ys), max(xs), max(ys))


def element_xy_bounds(element):
    try:
        bbox = element.get_BoundingBox(view)
        if bbox is None:
            bbox = element.get_BoundingBox(None)
        if bbox is None:
            return None
        return (bbox.Min.X, bbox.Min.Y, bbox.Max.X, bbox.Max.Y)
    except Exception:
        return None


def bounds_are_close(first_bounds, second_bounds, tolerance):
    if first_bounds is None or second_bounds is None:
        return False

    for index in range(4):
        if abs(first_bounds[index] - second_bounds[index]) > tolerance:
            return False
    return True


# -----------------------------------------------------------------------------
# Filled Region marker and deletion helpers
# -----------------------------------------------------------------------------

def build_region_marker(link_instance, room):
    return u"{}{}|{}".format(
        TOOL_MARKER_PREFIX,
        element_id_value(link_instance.Id),
        safe_text(room.UniqueId)
    )


def get_or_create_region_marker_schema():
    schema = Schema.Lookup(REGION_MARKER_SCHEMA_GUID)
    if schema is not None:
        return schema

    builder = SchemaBuilder(REGION_MARKER_SCHEMA_GUID)
    builder.SetSchemaName(REGION_MARKER_SCHEMA_NAME)
    builder.SetDocumentation(
        "Stores the linked Room identity used by the An-Tools "
        "Room Filled Region command."
    )
    builder.SetReadAccessLevel(AccessLevel.Public)
    builder.SetWriteAccessLevel(AccessLevel.Public)
    builder.AddSimpleField(
        REGION_MARKER_FIELD_NAME,
        System.String
    )
    return builder.Finish()


def get_legacy_comments_value(region):
    try:
        parameter = region.get_Parameter(
            DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
        )
        if parameter:
            value = parameter.AsString()
            if value:
                return safe_text(value)
    except Exception:
        pass
    return u""


def get_region_marker(region):
    # Current versions store the internal identity in Extensible Storage,
    # leaving visible project parameters free for user data.
    try:
        schema = Schema.Lookup(REGION_MARKER_SCHEMA_GUID)
        if schema is not None:
            entity = region.GetEntity(schema)
            if entity is not None and entity.IsValid():
                field = schema.GetField(REGION_MARKER_FIELD_NAME)
                if field is not None:
                    value = entity.Get[System.String](field)
                    if value:
                        return safe_text(value)
    except Exception:
        pass

    # Backward compatibility: older tool versions stored the marker in
    # Comments. Only treat it as a marker when the expected prefix exists.
    legacy_value = get_legacy_comments_value(region)
    if legacy_value.startswith(TOOL_MARKER_PREFIX):
        return legacy_value
    return u""


def set_region_marker(region, marker):
    try:
        schema = get_or_create_region_marker_schema()
        field = schema.GetField(REGION_MARKER_FIELD_NAME)
        if field is None:
            return False

        entity = Entity(schema)
        entity.Set[System.String](field, safe_text(marker))
        region.SetEntity(entity)
        return True
    except Exception:
        return False


def get_filled_region_comments_parameter(region):
    parameter = None

    try:
        parameter = region.get_Parameter(
            DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
        )
    except Exception:
        parameter = None

    # Fallback only. Built-in lookup is preferred because LookupParameter
    # depends on the Revit display language.
    if parameter is None:
        try:
            parameter = region.LookupParameter("Comments")
        except Exception:
            parameter = None

    return parameter


def set_filled_region_comments(region, room_name_value):
    value = safe_text(room_name_value).strip()
    if not value:
        return False, u"Room Name trống"

    parameter = get_filled_region_comments_parameter(region)
    if parameter is None:
        return False, u"Không tìm thấy instance parameter Comments"

    try:
        if parameter.StorageType != DB.StorageType.String:
            return False, u"Parameter Comments không phải kiểu Text"
        if parameter.IsReadOnly:
            return False, u"Parameter Comments đang Read-only"
    except Exception as parameter_error:
        return False, safe_text(parameter_error)

    try:
        parameter.Set(value)
        return True, u""
    except Exception as comments_error:
        return False, safe_text(comments_error)


def find_parameter_by_descriptor(element, descriptor):
    if element is None or descriptor is None:
        return None

    target_id = descriptor.get("id_value")
    for parameter in element.Parameters:
        try:
            if parameter_id_value(parameter) == target_id:
                return parameter
        except Exception:
            continue
    return None


def set_filled_region_text_parameter(region, descriptor, text_value):
    value = safe_text(text_value).strip()
    if not value:
        return False, u"Tên Level của Active View đang trống"

    parameter = find_parameter_by_descriptor(region, descriptor)
    if parameter is None:
        return False, u"Không tìm thấy parameter đã chọn trên Filled Region"

    try:
        if parameter.StorageType != DB.StorageType.String:
            return False, u"Parameter đã chọn không còn là kiểu Text"
        if parameter.IsReadOnly:
            return False, u"Parameter đã chọn đang Read-only"
    except Exception as parameter_error:
        return False, safe_text(parameter_error)

    try:
        parameter.Set(value)
        return True, u""
    except Exception as set_error:
        return False, safe_text(set_error)


def collect_existing_regions_of_selected_type(region_type_id):
    result = []
    collector = (
        DB.FilteredElementCollector(doc, view.Id)
        .OfClass(DB.FilledRegion)
        .WhereElementIsNotElementType()
    )

    for region in collector:
        try:
            if same_element_id(region.GetTypeId(), region_type_id):
                result.append(region)
        except Exception:
            continue
    return result


def find_regions_to_delete(room_data_items, region_type_id):
    marker_set = set()
    expected_bounds = []

    for room_data in room_data_items:
        marker_set.add(room_data["marker"])
        if room_data["bounds"] is not None:
            expected_bounds.append(room_data["bounds"])

    legacy_match_tolerance = mm_to_internal(25.0)

    result = []
    seen_ids = set()
    for region in collect_existing_regions_of_selected_type(region_type_id):
        delete_region = False
        marker = get_region_marker(region)

        if marker and marker in marker_set:
            delete_region = True
        elif not marker or not marker.startswith(TOOL_MARKER_PREFIX):
            region_bounds = element_xy_bounds(region)
            for room_bounds in expected_bounds:
                if bounds_are_close(
                    region_bounds,
                    room_bounds,
                    legacy_match_tolerance
                ):
                    delete_region = True
                    break

        if delete_region:
            id_value = element_id_value(region.Id)
            if id_value not in seen_ids:
                seen_ids.add(id_value)
                result.append(region)

    return result


# -----------------------------------------------------------------------------
# pyRevit Output helpers
# -----------------------------------------------------------------------------

def clean_output_text(value):
    text = safe_text(value)
    text = text.replace(u"\r\n", u" ")
    text = text.replace(u"\r", u" ")
    text = text.replace(u"\n", u" ")
    text = text.replace(u"|", u"-")
    return text.strip()


def show_pyrevit_result(
        summary_lines,
        created_items,
        warning_items,
        error_items):
    output = script.get_output()

    try:
        output.close_others()
    except Exception:
        pass

    try:
        output.set_title(u"Room Filled Region - Kết quả")
        output.resize(980, 720)
        output.center()
    except Exception:
        pass

    output.print_md(u"# ROOM FILLED REGION - KẾT QUẢ")
    output.print_md(u"## Tổng hợp")

    for line in summary_lines:
        output.print_md(u"- {}".format(clean_output_text(line)))

    output.print_md(u"---")
    output.print_md(u"## Filled Region vừa tạo ({})".format(len(created_items)))

    if created_items:
        output.print_md(
            u"Bấm vào **Element ID** để chọn Filled Region; "
            u"bấm biểu tượng **kính lúp** bên cạnh để chọn và zoom đến vùng."
        )

        for item in created_items:
            region = item[0]
            label = clean_output_text(item[1])
            region_id_value = element_id_value(region.Id)
            element_link = output.linkify(
                region.Id,
                title=u"ID {}".format(region_id_value)
            )
            output.print_md(
                u"{} &nbsp;&nbsp; {}".format(
                    label,
                    element_link
                )
            )

        all_ids = [item[0].Id for item in created_items]
        output.print_md(u"---")
        output.print_md(
            u"**Tất cả Filled Region:** {}".format(
                output.linkify(
                    all_ids,
                    title=u"{} vùng".format(len(all_ids))
                )
            )
        )
    else:
        output.print_md(u"Không có Filled Region mới được tạo.")

    if warning_items:
        output.print_md(u"---")
        output.print_md(u"## Cảnh báo ({})".format(len(warning_items)))
        for index, item in enumerate(warning_items, 1):
            output.print_md(
                u"{}. **{}** — {}".format(
                    index,
                    clean_output_text(item[0]),
                    clean_output_text(item[1])
                )
            )

    if error_items:
        output.print_md(u"---")
        output.print_md(u"## Lỗi / bỏ qua ({})".format(len(error_items)))
        for index, item in enumerate(error_items, 1):
            output.print_md(
                u"{}. **{}** — {}".format(
                    index,
                    clean_output_text(item[0]),
                    clean_output_text(item[1])
                )
            )

    try:
        output.show()
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Main process
# -----------------------------------------------------------------------------

keywords = split_keywords(window.keyword_text)
if not keywords:
    forms.alert(u"Không đọc được từ khóa hợp lệ.", exitscript=True)

level_tolerance = mm_to_internal(window.level_tolerance_mm)
active_phase_name = get_active_view_phase_name()

room_data_items = []
failed_items = list(skipped_links)
matched_rooms_count = 0
scanned_rooms_count = 0
rooms_without_filter_parameter_count = 0

for context in link_contexts:
    link_instance = context["instance"]
    link_doc = context["doc"]
    link_transform = context["transform"]
    link_name = context["name"]

    link_phase_id = None
    if window.match_phase:
        link_phase_id = find_link_phase_id_by_name(
            link_doc,
            active_phase_name
        )

    try:
        all_rooms = list(
            DB.FilteredElementCollector(link_doc)
              .OfCategory(DB.BuiltInCategory.OST_Rooms)
              .WhereElementIsNotElementType()
        )
    except Exception as collector_error:
        failed_items.append((
            link_name,
            u"Không thể thu thập Room: {}".format(safe_text(collector_error))
        ))
        continue

    scanned_rooms_count += len(all_rooms)

    for room in all_rooms:
        if not room_has_area(room):
            continue

        filter_parameter, filter_value = room_filter_parameter_value(
            room,
            link_doc,
            window.room_filter_parameter
        )
        if filter_parameter is None:
            rooms_without_filter_parameter_count += 1
            continue
        if not keyword_matches(filter_value, keywords):
            continue
        if not room_phase_matches(room, link_phase_id):
            continue
        if not room_matches_active_level(
            room,
            link_doc,
            link_transform,
            level_tolerance
        ):
            continue
        if not room_intersects_active_crop(
            room,
            link_transform,
            window.crop_only
        ):
            continue

        matched_rooms_count += 1
        number = room_number(room)
        name = room_name(room)
        room_label = u"{} - {}".format(number, name).strip(u" -")
        full_label = room_label

        try:
            curve_loops = get_room_curve_loops(
                room,
                window.boundary_location,
                link_transform
            )
            if curve_loops is None or curve_loops.Count == 0:
                failed_items.append((
                    full_label,
                    u"Room không có đường bao hợp lệ"
                ))
                continue

            room_data_items.append({
                "room": room,
                "label": full_label,
                "room_name": name,
                "filter_value": filter_value,
                "curve_loops": curve_loops,
                "bounds": curve_loops_xy_bounds(curve_loops),
                "marker": build_region_marker(link_instance, room),
            })
        except Exception as room_error:
            failed_items.append((full_label, safe_text(room_error)))

regions_to_delete = []
if window.operation_mode in [MODE_REPLACE, MODE_DELETE_ONLY]:
    regions_to_delete = find_regions_to_delete(
        room_data_items,
        window.region_type.Id
    )

created_items = []
warning_items = []
deleted_count = 0
comments_updated_count = 0
comments_failed_count = 0
level_updated_count = 0
level_failed_count = 0

has_transaction_work = bool(regions_to_delete)
if window.operation_mode != MODE_DELETE_ONLY and room_data_items:
    has_transaction_work = True

if has_transaction_work:
    transaction = DB.Transaction(
        doc,
        "Process Filled Regions From Multiple Linked Rooms"
    )
    transaction.Start()

    try:
        for region in regions_to_delete:
            try:
                doc.Delete(region.Id)
                deleted_count += 1
            except Exception as delete_error:
                failed_items.append((
                    u"Filled Region ID {}".format(
                        element_id_value(region.Id)
                    ),
                    u"Không thể xóa: {}".format(
                        safe_text(delete_error)
                    )
                ))

        if window.operation_mode != MODE_DELETE_ONLY:
            for room_data in room_data_items:
                try:
                    region = DB.FilledRegion.Create(
                        doc,
                        window.region_type.Id,
                        view.Id,
                        room_data["curve_loops"]
                    )
                    marker_ok = set_region_marker(
                        region,
                        room_data["marker"]
                    )
                    if not marker_ok:
                        warning_items.append((
                            room_data["label"],
                            u"Filled Region đã tạo nhưng không lưu được mã "
                            u"nhận diện nội bộ. Lần chạy sau tool sẽ đối chiếu "
                            u"theo kích thước phủ bì."
                        ))

                    if window.fill_room_name_to_comments:
                        comments_ok, comments_message = (
                            set_filled_region_comments(
                                region,
                                room_data["room_name"]
                            )
                        )
                        if comments_ok:
                            comments_updated_count += 1
                        else:
                            comments_failed_count += 1
                            warning_items.append((
                                room_data["label"],
                                u"Filled Region đã tạo nhưng không thể điền "
                                u"Room Name vào Comments: {}".format(
                                    comments_message
                                )
                            ))

                    if window.fill_level_to_parameter:
                        level_ok, level_message = (
                            set_filled_region_text_parameter(
                                region,
                                window.level_parameter,
                                ACTIVE_LEVEL_NAME
                            )
                        )
                        if level_ok:
                            level_updated_count += 1
                        else:
                            level_failed_count += 1
                            warning_items.append((
                                room_data["label"],
                                u"Filled Region đã tạo nhưng không thể điền "
                                u"Level '{}' vào parameter '{}': {}".format(
                                    ACTIVE_LEVEL_NAME,
                                    window.level_parameter["name"],
                                    level_message
                                )
                            ))

                    created_items.append((region, room_data["label"]))
                except Exception as create_error:
                    failed_items.append((
                        room_data["label"],
                        safe_text(create_error)
                    ))

        transaction.Commit()
    except Exception:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        failed_items.append((
            u"Transaction",
            traceback.format_exc()
        ))
        created_items = []
        warning_items = []
        deleted_count = 0
        comments_updated_count = 0
        comments_failed_count = 0
        level_updated_count = 0
        level_failed_count = 0

mode_labels = {
    MODE_REPLACE: u"Xóa vùng cũ và tạo lại",
    MODE_CREATE_ONLY: u"Chỉ tạo vùng mới",
    MODE_DELETE_ONLY: u"Chỉ xóa vùng cũ",
}

selected_link_names = [context["name"] for context in link_contexts]
selected_link_count = len(link_contexts) + len(skipped_links)
loaded_link_count = len(link_contexts)

summary_lines = [
    u"Chế độ: {}".format(
        mode_labels.get(window.operation_mode, window.operation_mode)
    ),
    u"Revit Link đã chọn: {} | đọc được: {}".format(
        selected_link_count,
        loaded_link_count
    ),
    u"Room đã quét: {} | phù hợp: {}".format(
        scanned_rooms_count,
        matched_rooms_count
    ),
    u"Parameter dùng để lọc: {} [{} | {}]".format(
        window.room_filter_parameter["name"],
        window.room_filter_parameter["source"],
        window.room_filter_parameter["storage_label"]
    ),
    u"Room không có parameter đã chọn: {}".format(
        rooms_without_filter_parameter_count
    ),
    u"Từ khóa: {}".format(window.keyword_text),
    u"Đã xóa: {} Filled Region".format(deleted_count),
    u"Đã tạo: {} Filled Region".format(len(created_items)),
    u"Điền Room Name vào Comments: {}".format(
        u"Bật" if window.fill_room_name_to_comments else u"Tắt"
    ),
    u"Comments đã điền: {} | không điền được: {}".format(
        comments_updated_count,
        comments_failed_count
    ),
    u"Level của Active View: {}".format(ACTIVE_LEVEL_NAME),
    u"Điền Level vào parameter: {}".format(
        (
            u"Bật - {}".format(window.level_parameter["name"])
            if window.fill_level_to_parameter and window.level_parameter
            else u"Tắt"
        )
    ),
    u"Level đã điền: {} | không điền được: {}".format(
        level_updated_count,
        level_failed_count
    ),
    u"Cảnh báo: {} | lỗi / bỏ qua: {}".format(
        len(warning_items),
        len(failed_items)
    ),
    u"Filled Region Type: {}".format(
        get_element_name(window.region_type)
    ),
    u"Boundary: {}".format(
        BOUNDARY_ITEMS[window.boundary_index][0]
    ),
]

if selected_link_names:
    summary_lines.append(
        u"Links: {}".format(u"; ".join(selected_link_names))
    )
elif skipped_links:
    summary_lines.append(
        u"Không có link nào đang Loaded để đọc Room."
    )

show_pyrevit_result(
    summary_lines,
    created_items,
    warning_items,
    failed_items
)
