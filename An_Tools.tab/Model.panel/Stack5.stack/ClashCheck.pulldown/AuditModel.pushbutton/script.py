# -*- coding: utf-8 -*-
"""
Pipe / Pipe Accessory / Pipe Fitting vs Linked Model Clash Checker
Version 2.9 - Active View stability / saved-link safety + region + pipe direction + grouped clash

HOST OBJECTS (independent checkboxes)
    - Pipes
        - Vertical Pipe
        - Horizontal / Sloped Pipe
    - Pipe Accessories
    - Pipe Fittings

LINKED TARGETS (independent checkboxes)
    - Rooms filtered by Room Name keywords
    - Structural Framing (beams / braces)
    - Structural Columns
    - Fire Rated Walls

WORKFLOW
    1. Run tool.
    2. Reuse saved links OR pick Architecture link group
       (Rooms + Fire Rated Walls).
    3. Pick Structure link group
       (Structural Framing + Structural Columns).
    4. UI opens: choose Entire Model / Active View / Pick Region in Active View.
    5. Optional Inspection Mode: choose one category, select + zoom it.
    6. If Inspection Mode is OFF, clash test runs normally.
    7. Output host ElementIds as clickable pyRevit links.

ROOM EXTENSION
    This does NOT modify linked Room parameters or geometry.
    The tool creates an in-memory extrusion above the native Room solid.
    Modes:
        - Manual: extend upward by N mm.
        - Auto: extend to nearest Floor underside (bbox Min.Z) above Room
                in the same Architecture link; manual N mm is fallback.

Designed for pyRevit / Revit 2025-2026.
Read-only: no Transaction is required.
"""

from pyrevit import revit, DB, UI, forms, script
import System
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List

import hashlib
import unicodedata
import difflib
import re


# ============================================================
# CONSTANTS
# ============================================================

FUZZY_THRESHOLD = 0.78
MM_PER_FOOT = 304.8
CONFIG_SEPARATOR = u"||"
GEOM_EPS_FT = 1.0 / MM_PER_FOOT      # ~1 mm
BBOX_EPS_FT = 1.0 / MM_PER_FOOT      # ~1 mm
AUTO_FLOOR_MATCH_TOL_FT = 10.0 / MM_PER_FOOT  # ~10 mm
MAX_WARNINGS = 80

# Pipe direction classification. A pipe is treated as vertical when its
# normalized axis is almost parallel to global Z. All other valid pipe axes
# (level or sloped) are classified as horizontal/sloped.
PIPE_VERTICAL_Z_COS = 0.9999

# V2.9 UI safety: selecting/zooming a very large set in Revit can be expensive.
# The output still reports every found element, but UI selection is capped.
MAX_HOST_UI_SELECTION = 300
MAX_LINKED_UI_SELECTION = 200


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


try:
    text_type = unicode
except NameError:
    text_type = str


# ============================================================
# BASIC HELPERS
# ============================================================

def to_text(value):
    if value is None:
        return u""
    if isinstance(value, text_type):
        return value
    try:
        return text_type(value)
    except Exception:
        try:
            return str(value)
        except Exception:
            return u""


def as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    txt = to_text(value).strip().lower()
    if txt in (u"1", u"true", u"yes", u"y", u"on"):
        return True
    if txt in (u"0", u"false", u"no", u"n", u"off"):
        return False
    try:
        return bool(value)
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        return float(to_text(value).replace(",", "."))
    except Exception:
        return default


def mm_to_ft(mm_value):
    return float(mm_value) / MM_PER_FOOT


def id_value(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        try:
            return int(element_id.IntegerValue)
        except Exception:
            return -1


def element_id_text(element):
    try:
        return to_text(id_value(element.Id))
    except Exception:
        return u"?"


def normalize_text(value):
    value = to_text(value).strip().lower()
    if not value:
        return u""

    value = value.replace(u"đ", u"d")

    try:
        value = unicodedata.normalize("NFD", value)
        value = u"".join(
            ch for ch in value
            if unicodedata.category(ch) != "Mn"
        )
    except Exception:
        pass

    value = re.sub(u"[^0-9a-z]+", u" ", value)
    return u" ".join(value.split())


# ============================================================
# PROJECT CONFIG
# ============================================================

def get_project_identity():
    try:
        pi = doc.ProjectInformation
        if pi and pi.UniqueId:
            return to_text(pi.UniqueId)
    except Exception:
        pass

    try:
        if doc.PathName:
            return to_text(doc.PathName)
    except Exception:
        pass

    return to_text(doc.Title)


def get_project_config():
    identity = get_project_identity()
    try:
        raw = identity.encode("utf-8")
    except Exception:
        raw = str(identity)
    digest = hashlib.sha1(raw).hexdigest()[:16]
    return script.get_config("pipe_linked_clash_v2_{0}".format(digest))


config = get_project_config()


# ============================================================
# API OBJECT SAFETY - V2.9
# ============================================================

def is_valid_api_object(obj):
    if obj is None:
        return False
    try:
        return bool(obj.IsValidObject)
    except Exception:
        # Some API wrappers do not expose IsValidObject. If the object exists,
        # allow later guarded checks to decide.
        return True


def is_valid_loaded_link(link):
    if not is_valid_api_object(link):
        return False
    try:
        current = doc.GetElement(link.Id)
        if current is None or not is_valid_api_object(current):
            return False
        if not isinstance(current, DB.RevitLinkInstance):
            return False
        if current.GetLinkDocument() is None:
            return False
        # Touch transform while still in managed/guarded code. A stale instance
        # should be rejected before it reaches linked-view collectors.
        current.GetTotalTransform()
        return True
    except Exception:
        return False


def get_active_view_visible_link_id_values(settings, warnings=None):
    """Return link instance ids that are eligible in the current Active View.

    This uses the normal HOST view collector first. The risky 3-argument
    linked-element collector is never called for a saved link unless its host
    link instance survives this validation.
    """
    if not is_active_view_scope(settings):
        return None

    cached = settings.get("_active_view_visible_link_ids")
    if cached is not None:
        return cached

    values = set()
    try:
        collector = (
            DB.FilteredElementCollector(doc, doc.ActiveView.Id)
            .OfClass(DB.RevitLinkInstance)
            .WhereElementIsNotElementType()
        )
        for link in collector:
            try:
                if is_valid_loaded_link(link):
                    values.add(id_value(link.Id))
            except Exception:
                pass
    except Exception as ex:
        if warnings is not None:
            warnings.append(u"Không xác minh được Revit Link trong Active View: {0}".format(ex))

    settings["_active_view_visible_link_ids"] = values
    return values


def sanitize_links_for_scope(links, settings, warnings, group_label):
    """Remove stale/unloaded/ineligible saved links before native view calls."""
    clean = []
    visible_ids = get_active_view_visible_link_id_values(settings, warnings)

    for link in links or []:
        if not is_valid_loaded_link(link):
            warnings.append(u"{0}: bỏ qua Link cũ/unloaded/không còn hợp lệ.".format(group_label))
            continue

        if visible_ids is not None and id_value(link.Id) not in visible_ids:
            warnings.append(
                u"{0}: Link '{1}' không có trong Active View hiện tại -> bỏ qua để tránh gọi linked-view collector không hợp lệ.".format(
                    group_label, get_link_label(link)
                )
            )
            continue

        clean.append(link)

    return clean

# ============================================================
# REVIT LINK PICKING / RESTORE
# ============================================================

class RevitLinkSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, DB.RevitLinkInstance)

    def AllowReference(self, reference, point):
        return False


def get_link_label(link_instance):
    try:
        link_doc = link_instance.GetLinkDocument()
        title = to_text(link_doc.Title) if link_doc else u"UNLOADED"
    except Exception:
        title = u"UNLOADED"

    try:
        inst_name = to_text(link_instance.Name)
    except Exception:
        inst_name = u""

    if inst_name and inst_name != title:
        return u"{0} [{1}]".format(title, inst_name)
    return title


def get_loaded_links():
    result = []
    collector = DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance)
    for link in collector:
        try:
            if link.GetLinkDocument() is not None:
                result.append(link)
        except Exception:
            pass
    return result


def resolve_links_from_config(config_key):
    try:
        saved = to_text(getattr(config, config_key, u""))
    except Exception:
        saved = u""

    if not saved:
        return []

    wanted = set([x for x in saved.split(CONFIG_SEPARATOR) if x])
    if not wanted:
        return []

    found = []
    for link in get_loaded_links():
        try:
            if link.UniqueId in wanted and is_valid_loaded_link(link):
                found.append(link)
        except Exception:
            pass
    return found


def save_link_group(config_key, links):
    values = []
    for link in links:
        try:
            values.append(to_text(link.UniqueId))
        except Exception:
            pass
    setattr(config, config_key, CONFIG_SEPARATOR.join(values))


def pick_link_group(prompt):
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            RevitLinkSelectionFilter(),
            prompt
        )
    except OperationCanceledException:
        script.exit()
    except Exception as ex:
        forms.alert(
            u"Không thể pick Revit Link.\n\n{0}".format(ex),
            title="Linked Clash Checker"
        )
        script.exit()

    result = []
    seen = set()
    for ref in refs:
        try:
            link = doc.GetElement(ref.ElementId)
            if not isinstance(link, DB.RevitLinkInstance):
                continue
            if link.GetLinkDocument() is None:
                continue
            uid = to_text(link.UniqueId)
            if uid in seen:
                continue
            seen.add(uid)
            result.append(link)
        except Exception:
            pass

    return result


def ask_reuse_link_groups(saved_arch, saved_struct):
    if not saved_arch and not saved_struct:
        return False

    lines = []
    if saved_arch:
        lines.append(u"ARCH / ROOM + FIRE WALL:")
        for link in saved_arch:
            lines.append(u"  • {0}".format(get_link_label(link)))
    else:
        lines.append(u"ARCH / ROOM + FIRE WALL: không tìm thấy link đã lưu")

    lines.append(u"")

    if saved_struct:
        lines.append(u"STRUCTURE:")
        for link in saved_struct:
            lines.append(u"  • {0}".format(get_link_label(link)))
    else:
        lines.append(u"STRUCTURE: không tìm thấy link đã lưu")

    dialog = UI.TaskDialog("Linked Clash Checker")
    dialog.MainInstruction = u"Đã tìm thấy lựa chọn Link của lần chạy trước."
    dialog.MainContent = u"\n".join(lines)
    dialog.AddCommandLink(
        UI.TaskDialogCommandLinkId.CommandLink1,
        u"Dùng lại các Link còn hợp lệ"
    )
    dialog.AddCommandLink(
        UI.TaskDialogCommandLinkId.CommandLink2,
        u"Pick lại 2 nhóm Link"
    )
    dialog.CommonButtons = UI.TaskDialogCommonButtons.Cancel

    result = dialog.Show()
    if result == UI.TaskDialogResult.CommandLink1:
        return True
    if result == UI.TaskDialogResult.CommandLink2:
        return False
    script.exit()


def get_link_groups():
    saved_arch = resolve_links_from_config("arch_link_uids")
    saved_struct = resolve_links_from_config("struct_link_uids")

    use_saved = ask_reuse_link_groups(saved_arch, saved_struct)

    if use_saved:
        return saved_arch, saved_struct

    arch_links = pick_link_group(
        u"BƯỚC 1/2 - Chọn Link chứa ROOM + FIRE RATED WALL. "
        u"Có thể chọn nhiều Link, sau đó bấm Finish."
    )

    struct_links = pick_link_group(
        u"BƯỚC 2/2 - Chọn Link STRUCTURE chứa BEAM + COLUMN. "
        u"Có thể chọn nhiều Link, sau đó bấm Finish."
    )

    if not arch_links and not struct_links:
        forms.alert(
            u"Bạn chưa chọn Link nào.",
            title="Linked Clash Checker"
        )
        script.exit()

    save_link_group("arch_link_uids", arch_links)
    save_link_group("struct_link_uids", struct_links)
    script.save_config()

    return arch_links, struct_links


# ============================================================
# WPF OPTIONS UI (external ui.xaml)
# ============================================================

# UI is stored separately in ui.xaml inside this pushbutton bundle.
UI_XAML_PATH = script.get_bundle_file("ui.xaml")
EXPECTED_UI_VERSION = u"2.8"


def validate_ui_bundle(window):
    """Fail clearly if script.py and ui.xaml are from different versions."""
    required_controls = [
        "tb_ui_version",
        "cb_inspection_mode",
        "cb_inspection_category",
        "rb_scope_model",
        "rb_scope_view",
        "rb_scope_region",
        "cb_zoom_linked_after_clash",
        "cb_pipe_vertical",
        "cb_pipe_horizontal",
        "btn_run",
    ]
    missing = []
    for control_name in required_controls:
        try:
            control = window.FindName(control_name)
        except Exception:
            control = None
        if control is None:
            missing.append(control_name)

    if missing:
        forms.alert(
            u"ui.xaml không đúng phiên bản V{0}.\n\n"
            u"Thiếu control: {1}\n\n"
            u"Hãy chép đè CẢ script.py và ui.xaml trong cùng thư mục .pushbutton, "
            u"sau đó Reload pyRevit.".format(
                EXPECTED_UI_VERSION,
                u", ".join(missing)
            ),
            title=u"Linked Clash Checker - UI VERSION MISMATCH"
        )
        script.exit()

    try:
        window.tb_ui_version.Text = u"UI V{0} - Inspection Mode ready".format(EXPECTED_UI_VERSION)
    except Exception:
        pass


class OptionsWindow(forms.WPFWindow):
    def __init__(self, arch_links, struct_links):
        forms.WPFWindow.__init__(self, UI_XAML_PATH)
        validate_ui_bundle(self)
        self.result = None

        scope = to_text(getattr(config, "scope_mode", u"model")).lower()
        self.rb_scope_view.IsChecked = (scope == u"view")
        self.rb_scope_region.IsChecked = (scope == u"region")
        self.rb_scope_model.IsChecked = not bool(self.rb_scope_view.IsChecked or self.rb_scope_region.IsChecked)

        try:
            active_name = to_text(doc.ActiveView.Name)
            active_type = to_text(doc.ActiveView.ViewType)
        except Exception:
            active_name = u"?"
            active_type = u"?"
        self.tb_active_view.Text = u"Active View: {0} | {1}".format(active_name, active_type)

        self.cb_inspection_mode.IsChecked = as_bool(getattr(config, "inspection_mode", getattr(config, "inspect_select", False)), False)
        saved_inspect = to_text(getattr(config, "inspection_category", getattr(config, "inspect_category", u"pipe"))).lower()
        self._set_inspect_category(saved_inspect)

        self.cb_pipe.IsChecked = as_bool(getattr(config, "host_pipe", True), True)
        self.cb_pipe_vertical.IsChecked = as_bool(getattr(config, "host_pipe_vertical", True), True)
        self.cb_pipe_horizontal.IsChecked = as_bool(getattr(config, "host_pipe_horizontal", True), True)
        self.cb_accessory.IsChecked = as_bool(getattr(config, "host_accessory", True), True)
        self.cb_fitting.IsChecked = as_bool(getattr(config, "host_fitting", True), True)

        self.cb_room.IsChecked = as_bool(getattr(config, "target_room", True), True)
        self.cb_beam.IsChecked = as_bool(getattr(config, "target_beam", True), True)
        self.cb_column.IsChecked = as_bool(getattr(config, "target_column", True), True)
        self.cb_firewall.IsChecked = as_bool(getattr(config, "target_firewall", True), True)

        self.tb_keywords.Text = to_text(getattr(config, "room_keywords", u"Toilet, WC"))

        self.cb_extend_room.IsChecked = as_bool(getattr(config, "extend_room", False), False)
        ext_mode = to_text(getattr(config, "room_ext_mode", u"manual")).lower()
        self.rb_auto.IsChecked = (ext_mode == u"auto")
        self.rb_manual.IsChecked = not bool(self.rb_auto.IsChecked)
        self.tb_offset_mm.Text = to_text(getattr(config, "room_offset_mm", u"300"))
        self.cb_zoom_linked_after_clash.IsChecked = as_bool(
            getattr(config, "zoom_linked_after_clash", False), False
        )

        self.tb_arch_links.Text = self._links_text(arch_links)
        self.tb_struct_links.Text = self._links_text(struct_links)

        self._sync_all_checks()
        self._sync_inspect_ui()

        self.cb_host_all.Checked += self.host_all_changed
        self.cb_host_all.Unchecked += self.host_all_changed
        self.cb_target_all.Checked += self.target_all_changed
        self.cb_target_all.Unchecked += self.target_all_changed
        self.cb_inspection_mode.Checked += self.inspection_mode_changed
        self.cb_inspection_mode.Unchecked += self.inspection_mode_changed
        self.btn_run.Click += self.run_clicked
        self.btn_cancel.Click += self.cancel_clicked

    def _links_text(self, links):
        if not links:
            return u"(none)"
        return u"\n".join([u"• {0}".format(get_link_label(x)) for x in links])

    def _set_inspect_category(self, tag):
        try:
            items = self.cb_inspection_category.Items
            for index in range(items.Count):
                item = items[index]
                if to_text(item.Tag).lower() == tag:
                    self.cb_inspection_category.SelectedIndex = index
                    return
        except Exception:
            pass
        self.cb_inspection_category.SelectedIndex = 0

    def _get_inspect_category(self):
        try:
            item = self.cb_inspection_category.SelectedItem
            if item is not None:
                return to_text(item.Tag).lower()
        except Exception:
            pass
        return u"pipe"

    def _sync_inspect_ui(self):
        inspect_on = bool(self.cb_inspection_mode.IsChecked)
        self.cb_inspection_category.IsEnabled = inspect_on
        try:
            self.cb_zoom_linked_after_clash.IsEnabled = not inspect_on
        except Exception:
            pass
        try:
            self.sp_inspection_options.Visibility = System.Windows.Visibility.Visible if inspect_on else System.Windows.Visibility.Collapsed
        except Exception:
            pass
        self.btn_run.Content = u"SELECT / ZOOM" if inspect_on else u"RUN CHECK"

    def _sync_all_checks(self):
        self.cb_host_all.IsChecked = bool(
            self.cb_pipe.IsChecked and
            self.cb_pipe_vertical.IsChecked and
            self.cb_pipe_horizontal.IsChecked and
            self.cb_accessory.IsChecked and
            self.cb_fitting.IsChecked
        )
        self.cb_target_all.IsChecked = bool(
            self.cb_room.IsChecked and
            self.cb_beam.IsChecked and
            self.cb_column.IsChecked and
            self.cb_firewall.IsChecked
        )

    def host_all_changed(self, sender, args):
        state = bool(self.cb_host_all.IsChecked)
        self.cb_pipe.IsChecked = state
        self.cb_pipe_vertical.IsChecked = state
        self.cb_pipe_horizontal.IsChecked = state
        self.cb_accessory.IsChecked = state
        self.cb_fitting.IsChecked = state

    def target_all_changed(self, sender, args):
        state = bool(self.cb_target_all.IsChecked)
        self.cb_room.IsChecked = state
        self.cb_beam.IsChecked = state
        self.cb_column.IsChecked = state
        self.cb_firewall.IsChecked = state

    def inspection_mode_changed(self, sender, args):
        self._sync_inspect_ui()

    def cancel_clicked(self, sender, args):
        self.result = None
        self.Close()

    def run_clicked(self, sender, args):
        if bool(self.rb_scope_region.IsChecked):
            scope_mode = u"region"
        elif bool(self.rb_scope_view.IsChecked):
            scope_mode = u"view"
        else:
            scope_mode = u"model"
        inspection_mode = bool(self.cb_inspection_mode.IsChecked)
        inspection_category = self._get_inspect_category()

        host_pipe = bool(self.cb_pipe.IsChecked)
        host_pipe_vertical = bool(self.cb_pipe_vertical.IsChecked)
        host_pipe_horizontal = bool(self.cb_pipe_horizontal.IsChecked)
        host_accessory = bool(self.cb_accessory.IsChecked)
        host_fitting = bool(self.cb_fitting.IsChecked)

        target_room = bool(self.cb_room.IsChecked)
        target_beam = bool(self.cb_beam.IsChecked)
        target_column = bool(self.cb_column.IsChecked)
        target_firewall = bool(self.cb_firewall.IsChecked)

        raw_keywords = to_text(self.tb_keywords.Text).strip()

        pipe_direction_required = (
            ((not inspection_mode) and host_pipe) or
            (inspection_mode and inspection_category == u"pipe")
        )
        if pipe_direction_required and not (host_pipe_vertical or host_pipe_horizontal):
            forms.alert(
                u"Hãy tick Vertical Pipe, Horizontal / Sloped Pipe, hoặc cả hai.",
                title="Linked Clash Checker"
            )
            return

        if inspection_mode:
            if inspection_category == u"room" and not raw_keywords:
                forms.alert(u"Inspect Room cần Room Name keyword.", title="Linked Clash Checker")
                return
        else:
            if not (host_pipe or host_accessory or host_fitting):
                forms.alert(u"Hãy chọn ít nhất 1 nhóm đối tượng MEP trong Host.", title="Linked Clash Checker")
                return

            if not (target_room or target_beam or target_column or target_firewall):
                forms.alert(u"Hãy chọn ít nhất 1 nhóm đối tượng trong Link.", title="Linked Clash Checker")
                return

            if target_room and not raw_keywords:
                forms.alert(u"Đang bật Room Name nhưng chưa nhập keyword Room.", title="Linked Clash Checker")
                return

        offset_mm = safe_float(self.tb_offset_mm.Text, -1.0)
        if (not inspection_mode) and bool(self.cb_extend_room.IsChecked) and offset_mm < 0:
            forms.alert(u"Offset Room phải >= 0 mm.", title="Linked Clash Checker")
            return

        self.result = {
            "scope_mode": scope_mode,
            "inspection_mode": inspection_mode,
            "inspect_select": inspection_mode,
            "inspection_category": inspection_category,
            "inspect_category": inspection_category,
            "host_pipe": host_pipe,
            "host_pipe_vertical": host_pipe_vertical,
            "host_pipe_horizontal": host_pipe_horizontal,
            "host_accessory": host_accessory,
            "host_fitting": host_fitting,
            "target_room": target_room,
            "target_beam": target_beam,
            "target_column": target_column,
            "target_firewall": target_firewall,
            "room_keywords": raw_keywords,
            "extend_room": bool(self.cb_extend_room.IsChecked),
            "room_ext_mode": u"auto" if bool(self.rb_auto.IsChecked) else u"manual",
            "room_offset_mm": max(0.0, offset_mm),
            "zoom_linked_after_clash": bool(self.cb_zoom_linked_after_clash.IsChecked)
        }

        config.scope_mode = scope_mode
        config.inspection_mode = inspection_mode
        config.inspect_select = inspection_mode
        config.inspection_category = inspection_category
        config.inspect_category = inspection_category
        config.host_pipe = host_pipe
        config.host_pipe_vertical = host_pipe_vertical
        config.host_pipe_horizontal = host_pipe_horizontal
        config.host_accessory = host_accessory
        config.host_fitting = host_fitting
        config.target_room = target_room
        config.target_beam = target_beam
        config.target_column = target_column
        config.target_firewall = target_firewall
        config.room_keywords = raw_keywords
        config.extend_room = bool(self.cb_extend_room.IsChecked)
        config.room_ext_mode = self.result["room_ext_mode"]
        config.room_offset_mm = to_text(self.result["room_offset_mm"])
        config.zoom_linked_after_clash = bool(self.result["zoom_linked_after_clash"])
        script.save_config()

        self.Close()


def show_options(arch_links, struct_links):
    window = OptionsWindow(arch_links, struct_links)
    window.ShowDialog()
    if window.result is None:
        script.exit()
    return window.result


# ============================================================
# ROOM NAME MATCHING
# ============================================================

def parse_keywords(raw_text):
    values = re.split(u"[,;|\n\r]+", to_text(raw_text))
    result = []
    seen = set()
    for value in values:
        raw = to_text(value).strip()
        norm = normalize_text(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result.append({"raw": raw, "normalized": norm})
    return result


def similarity(a, b):
    try:
        return difflib.SequenceMatcher(None, a, b).ratio()
    except Exception:
        return 0.0


def keyword_score(room_name_norm, keyword_norm):
    if not room_name_norm or not keyword_norm:
        return 0.0
    if room_name_norm == keyword_norm:
        return 1.0
    if len(keyword_norm) >= 2 and keyword_norm in room_name_norm:
        return 1.0

    scores = [similarity(keyword_norm, room_name_norm)]
    room_words = room_name_norm.split()
    keyword_words = keyword_norm.split()

    for word in room_words:
        scores.append(similarity(keyword_norm, word))

    kw_count = max(1, len(keyword_words))
    for win_size in (max(1, kw_count - 1), kw_count, kw_count + 1):
        if win_size > len(room_words):
            continue
        for i in range(0, len(room_words) - win_size + 1):
            frag = u" ".join(room_words[i:i + win_size])
            scores.append(similarity(keyword_norm, frag))

    return max(scores) if scores else 0.0


def room_name_matches(room_name, keywords):
    room_norm = normalize_text(room_name)
    best_keyword = None
    best_score = 0.0
    for item in keywords:
        score = keyword_score(room_norm, item["normalized"])
        if score > best_score:
            best_score = score
            best_keyword = item["raw"]
    return best_score >= FUZZY_THRESHOLD, best_keyword, best_score


def get_room_name(room):
    try:
        p = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
        if p and p.AsString():
            return to_text(p.AsString())
    except Exception:
        pass
    try:
        return to_text(room.Name)
    except Exception:
        return u""


def get_room_number(room):
    try:
        p = room.get_Parameter(DB.BuiltInParameter.ROOM_NUMBER)
        if p and p.AsString():
            return to_text(p.AsString())
    except Exception:
        pass
    try:
        return to_text(room.Number)
    except Exception:
        return u""


# ============================================================
# FIRE RATING
# ============================================================

def parameter_string(param):
    if param is None:
        return u""
    try:
        value = param.AsString()
        if value:
            return to_text(value).strip()
    except Exception:
        pass
    try:
        value = param.AsValueString()
        if value:
            return to_text(value).strip()
    except Exception:
        pass
    return u""


def get_fire_rating_text(wall, link_doc):
    candidates = []

    # Preferred: built-in FIRE_RATING when available in this Revit version.
    try:
        bip = getattr(DB.BuiltInParameter, "FIRE_RATING")
        try:
            candidates.append(parameter_string(wall.get_Parameter(bip)))
        except Exception:
            pass

        try:
            wall_type = link_doc.GetElement(wall.GetTypeId())
            if wall_type:
                candidates.append(parameter_string(wall_type.get_Parameter(bip)))
        except Exception:
            pass
    except Exception:
        wall_type = None

    # Fallback: inspect parameter names on instance and type.
    elems = [wall]
    try:
        wall_type = link_doc.GetElement(wall.GetTypeId())
        if wall_type:
            elems.append(wall_type)
    except Exception:
        pass

    for elem in elems:
        try:
            for p in elem.Parameters:
                try:
                    pname = normalize_text(p.Definition.Name)
                except Exception:
                    continue
                if u"fire" in pname and (u"rating" in pname or u"resistance" in pname):
                    candidates.append(parameter_string(p))
        except Exception:
            pass

    for value in candidates:
        txt = to_text(value).strip()
        norm = normalize_text(txt)
        if not norm:
            continue
        if norm in (u"0", u"0 hr", u"0hr", u"none", u"na", u"n a", u"no"):
            continue
        if u"not rated" in norm or u"unrated" in norm or u"non rated" in norm:
            continue
        return txt

    return u""


# ============================================================
# GEOMETRY HELPERS
# ============================================================

def get_geometry_options():
    opts = DB.Options()
    opts.IncludeNonVisibleObjects = False
    try:
        opts.DetailLevel = DB.ViewDetailLevel.Fine
    except Exception:
        pass
    return opts


def collect_solids_from_geometry(geometry_element, solids):
    if geometry_element is None:
        return

    try:
        iterator = geometry_element
    except Exception:
        return

    for obj in iterator:
        if isinstance(obj, DB.Solid):
            try:
                if obj.Volume > 1e-9 and obj.Faces.Size > 0:
                    solids.append(obj)
            except Exception:
                pass

        elif isinstance(obj, DB.GeometryInstance):
            try:
                inst_geom = obj.GetInstanceGeometry()
                collect_solids_from_geometry(inst_geom, solids)
            except Exception:
                pass


def get_element_solids_in_doc_coords(element):
    solids = []
    try:
        geom = element.get_Geometry(get_geometry_options())
        collect_solids_from_geometry(geom, solids)
    except Exception:
        pass
    return solids


def transform_solids(solids, transform):
    result = []
    for solid in solids:
        try:
            transformed = DB.SolidUtils.CreateTransformed(solid, transform)
            if transformed and transformed.Volume > 1e-9:
                result.append(transformed)
        except Exception:
            pass
    return result


def solid_outline(solid):
    """Axis-aligned host Outline around a Solid, used only as quick filter."""
    points = []

    try:
        for edge in solid.Edges:
            try:
                tess = edge.Tessellate()
                for p in tess:
                    points.append(p)
            except Exception:
                pass
    except Exception:
        pass

    if not points:
        return None

    min_x = min(p.X for p in points) - BBOX_EPS_FT
    min_y = min(p.Y for p in points) - BBOX_EPS_FT
    min_z = min(p.Z for p in points) - BBOX_EPS_FT
    max_x = max(p.X for p in points) + BBOX_EPS_FT
    max_y = max(p.Y for p in points) + BBOX_EPS_FT
    max_z = max(p.Z for p in points) + BBOX_EPS_FT

    try:
        return DB.Outline(DB.XYZ(min_x, min_y, min_z), DB.XYZ(max_x, max_y, max_z))
    except Exception:
        return None


def bbox_xy_overlap(a, b, tol=0.0):
    if a is None or b is None:
        return False
    try:
        return not (
            a.Max.X < b.Min.X - tol or
            a.Min.X > b.Max.X + tol or
            a.Max.Y < b.Min.Y - tol or
            a.Min.Y > b.Max.Y + tol
        )
    except Exception:
        return False


# ============================================================
# ROOM SOLID + TEMPORARY VERTICAL EXTENSION
# ============================================================

def build_room_calculator(link_doc):
    options = DB.SpatialElementBoundaryOptions()
    try:
        options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    except Exception:
        pass
    return DB.SpatialElementGeometryCalculator(link_doc, options)


def get_native_room_solid(room, calculator):
    try:
        result = calculator.CalculateSpatialElementGeometry(room)
        solid = result.GetGeometry()
        if solid and solid.Volume > 1e-9:
            return solid
    except Exception:
        pass
    return None


def room_boundary_loops_at_z(room, target_z):
    options = DB.SpatialElementBoundaryOptions()
    try:
        options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    except Exception:
        pass

    try:
        segment_groups = room.GetBoundarySegments(options)
    except Exception:
        return []

    if not segment_groups:
        return []

    loops = []

    for group in segment_groups:
        loop = DB.CurveLoop()
        curve_count = 0
        for seg in group:
            try:
                curve = seg.GetCurve()
                p0 = curve.GetEndPoint(0)
                dz = target_z - p0.Z
                tr = DB.Transform.CreateTranslation(DB.XYZ(0.0, 0.0, dz))
                moved = curve.CreateTransformed(tr)
                loop.Append(moved)
                curve_count += 1
            except Exception:
                curve_count = 0
                break
        if curve_count > 0:
            loops.append(loop)

    return loops


def collect_floor_bboxes(link_doc):
    result = []
    try:
        floors = (
            DB.FilteredElementCollector(link_doc)
            .OfCategory(DB.BuiltInCategory.OST_Floors)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        floors = []

    for floor in floors:
        try:
            bbox = floor.get_BoundingBox(None)
            if bbox is not None:
                result.append((floor, bbox))
        except Exception:
            pass
    return result


def find_nearest_floor_bottom(room_bbox, room_top_z, floor_bboxes):
    if room_bbox is None:
        return None, None

    best_floor = None
    best_z = None

    for floor, bbox in floor_bboxes:
        if bbox is None:
            continue
        try:
            floor_bottom = bbox.Min.Z
        except Exception:
            continue

        # Accept a Floor whose underside is above the Room top or almost
        # coincident with it. The small negative tolerance avoids treating
        # an already-correct Room as an Auto-search failure.
        if floor_bottom < room_top_z - AUTO_FLOOR_MATCH_TOL_FT:
            continue

        if not bbox_xy_overlap(room_bbox, bbox, GEOM_EPS_FT):
            continue

        if best_z is None or floor_bottom < best_z:
            best_z = floor_bottom
            best_floor = floor

    return best_floor, best_z


def create_room_extension_solid(room, room_top_z, extension_height):
    if extension_height <= GEOM_EPS_FT:
        return None

    loops = room_boundary_loops_at_z(room, room_top_z)
    if not loops:
        return None

    try:
        net_loops = List[DB.CurveLoop]()
        for loop in loops:
            net_loops.Add(loop)
        return DB.GeometryCreationUtilities.CreateExtrusionGeometry(
            net_loops,
            DB.XYZ.BasisZ,
            extension_height
        )
    except Exception:
        return None


def get_room_check_solids_link_coords(room, calculator, settings, floor_bboxes):
    native = get_native_room_solid(room, calculator)
    if native is None:
        return [], {"mode": u"native-failed", "extension_mm": 0.0}

    solids = [native]
    info = {"mode": u"native", "extension_mm": 0.0, "floor_id": None}

    if not settings["extend_room"]:
        return solids, info

    try:
        room_bbox = room.get_BoundingBox(None)
    except Exception:
        room_bbox = None

    if room_bbox is None:
        return solids, info

    room_top_z = room_bbox.Max.Z
    fallback_ft = mm_to_ft(settings["room_offset_mm"])
    target_top_z = room_top_z

    if settings["room_ext_mode"] == u"auto":
        floor, floor_bottom_z = find_nearest_floor_bottom(
            room_bbox,
            room_top_z,
            floor_bboxes
        )

        if floor_bottom_z is not None:
            target_top_z = max(room_top_z, floor_bottom_z)
            info["mode"] = u"auto-floor"
            info["floor_id"] = id_value(floor.Id)
        else:
            target_top_z = room_top_z + fallback_ft
            info["mode"] = u"auto-fallback"
    else:
        target_top_z = room_top_z + fallback_ft
        info["mode"] = u"manual"

    extension_height = max(0.0, target_top_z - room_top_z)
    extension = create_room_extension_solid(room, room_top_z, extension_height)

    if extension is not None:
        solids.append(extension)
        info["extension_mm"] = extension_height * MM_PER_FOOT
    elif extension_height > GEOM_EPS_FT:
        info["mode"] = info["mode"] + u"-failed"

    return solids, info


# ============================================================
# SCOPE HELPERS - ENTIRE MODEL / ACTIVE VIEW / ACTIVE VIEW REGION
# ============================================================

def scope_mode(settings):
    return to_text(settings.get("scope_mode", u"model")).lower()


def is_active_view_scope(settings):
    return scope_mode(settings) in (u"view", u"region")


def is_region_scope(settings):
    return scope_mode(settings) == u"region"


def scope_label(settings):
    mode = scope_mode(settings)
    if mode == u"region":
        return u"Pick Region in Active View"
    if mode == u"view":
        return u"Active View"
    return u"Entire Model"


def validate_scope(settings):
    if not is_active_view_scope(settings):
        return True, u""

    try:
        view = doc.ActiveView
        if view is None:
            return False, u"Không có Active View hợp lệ."

        try:
            valid = DB.FilteredElementCollector.IsViewValidForElementIteration(view.Id)
            if not valid:
                return False, u"Active View hiện tại không hỗ trợ element iteration."
        except Exception:
            # Constructor below is the final authority on older/newer builds.
            DB.FilteredElementCollector(doc, view.Id)

        return True, u""
    except Exception as ex:
        return False, u"Không thể dùng Active View làm scope: {0}".format(ex)


def make_host_collector(settings):
    if is_active_view_scope(settings):
        return DB.FilteredElementCollector(doc, doc.ActiveView.Id)
    return DB.FilteredElementCollector(doc)


def make_link_collector(link, settings):
    """
    Entire Model:
        collector belongs directly to linked Document.

    Active View / Pick Region in Active View (Revit 2024+):
        collector searches visible linked elements for this specific
        RevitLinkInstance in the host Active View.

    NOTE:
        Region scope intentionally limits HOST MEP only. Linked targets
        remain Active-View scoped because the rectangle is used to choose
        which host Pipe / Accessory / Fitting participate in clash checking.
    """
    if not is_valid_loaded_link(link):
        return None

    if is_active_view_scope(settings):
        visible_ids = get_active_view_visible_link_id_values(settings)
        if visible_ids is not None and id_value(link.Id) not in visible_ids:
            return None
        # Only call the native 3-argument collector after validating both the
        # current view and this exact link instance in that view.
        return DB.FilteredElementCollector(doc, doc.ActiveView.Id, link.Id)

    link_doc = link.GetLinkDocument()
    if link_doc is None:
        return None
    return DB.FilteredElementCollector(link_doc)


def collect_link_category(link, bic, settings, warnings, context_label):
    try:
        collector = make_link_collector(link, settings)
        if collector is None:
            return []
        return list(
            collector
            .OfCategory(bic)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception as ex:
        warnings.append(
            u"Không collect được {0} | Link {1} | Scope {2}: {3}".format(
                context_label,
                get_link_label(link),
                scope_label(settings),
                ex
            )
        )
        return []


# ============================================================
# ACTIVE VIEW RECTANGLE REGION - V2.8
# ============================================================

class HostRegionSelectionFilter(ISelectionFilter):
    """Native Revit rectangle filter for enabled host MEP categories."""

    def __init__(self, settings):
        self.settings = settings
        self.inspection_mode = bool(settings.get("inspection_mode", settings.get("inspect_select", False)))
        self.inspection_category = to_text(
            settings.get("inspection_category", settings.get("inspect_category", u"pipe"))
        ).lower()

    def _category_allowed(self, element):
        try:
            cat_id = id_value(element.Category.Id)
        except Exception:
            return False

        pipe_id = int(DB.BuiltInCategory.OST_PipeCurves)
        accessory_id = int(DB.BuiltInCategory.OST_PipeAccessory)
        fitting_id = int(DB.BuiltInCategory.OST_PipeFitting)

        if self.inspection_mode:
            if self.inspection_category == u"pipe":
                return cat_id == pipe_id
            if self.inspection_category == u"accessory":
                return cat_id == accessory_id
            if self.inspection_category == u"fitting":
                return cat_id == fitting_id
            return False

        if cat_id == pipe_id:
            return bool(self.settings.get("host_pipe", False))
        if cat_id == accessory_id:
            return bool(self.settings.get("host_accessory", False))
        if cat_id == fitting_id:
            return bool(self.settings.get("host_fitting", False))
        return False

    def AllowElement(self, element):
        if element is None or not self._category_allowed(element):
            return False

        if is_pipe_element(element):
            return pipe_orientation_matches_settings(element, self.settings)
        return True

    def AllowReference(self, reference, point):
        return False


def pick_host_region_elements(settings):
    """Prompt a native Revit rectangle selection in the current Active View.

    The returned IDs become a hard whitelist for host MEP clash candidates.
    Linked Room/Beam/Column/Fire Wall targets are still collected from the
    Active View, but only these picked host MEP elements can be reported as
    clashes.
    """
    if not is_region_scope(settings):
        return True, u""

    inspection_mode = bool(settings.get("inspection_mode", settings.get("inspect_select", False)))
    inspection_category = to_text(
        settings.get("inspection_category", settings.get("inspect_category", u"pipe"))
    ).lower()

    # Region applies only to host MEP. For linked inspection, simply use the
    # Active View collector and do not force an irrelevant host rectangle.
    if inspection_mode and inspection_category not in (u"pipe", u"accessory", u"fitting"):
        settings["_region_host_id_values"] = None
        settings["_region_host_count"] = 0
        return True, u""

    prompt = (
        u"Kéo một vùng trong Active View để chọn Pipe / Pipe Accessories / "
        u"Pipe Fittings dùng cho clash check. ESC để hủy."
    )

    try:
        selection_filter = HostRegionSelectionFilter(settings)
        elements = uidoc.Selection.PickElementsByRectangle(selection_filter, prompt)
    except OperationCanceledException:
        return False, u"Đã hủy chọn vùng Active View."
    except Exception as ex:
        return False, u"Không thể chọn vùng trong Active View: {0}".format(ex)

    id_values = set()
    try:
        for element in elements:
            if element is not None:
                id_values.add(id_value(element.Id))
    except Exception:
        pass

    settings["_region_host_id_values"] = id_values
    settings["_region_host_count"] = len(id_values)

    if not id_values:
        return False, u"Vùng đã chọn không chứa Pipe / Pipe Accessories / Pipe Fittings phù hợp với checkbox hiện tại."

    return True, u""


def host_id_in_region(element_id, settings):
    if not is_region_scope(settings):
        return True
    allowed = settings.get("_region_host_id_values")
    if allowed is None:
        return True
    try:
        return id_value(element_id) in allowed
    except Exception:
        return False


# ============================================================
# PIPE DIRECTION CLASSIFICATION - V2.8
# ============================================================

def is_pipe_element(element):
    try:
        return id_value(element.Category.Id) == int(DB.BuiltInCategory.OST_PipeCurves)
    except Exception:
        return False


def get_pipe_axis_direction(pipe):
    """Return a normalized pipe axis direction, or None if unavailable."""
    try:
        location = pipe.Location
        curve = location.Curve if location is not None else None
        if curve is None:
            return None

        # Line exposes Direction directly. For any other valid curve, use
        # the chord between its two endpoints as a stable fallback.
        try:
            direction = curve.Direction
            if direction is not None:
                return direction.Normalize()
        except Exception:
            pass

        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
        vector = p1 - p0
        if vector.GetLength() <= 1.0e-9:
            return None
        return vector.Normalize()
    except Exception:
        return None


def get_pipe_orientation(pipe):
    """Classify pipe as 'vertical', 'horizontal', or 'unknown'.

    Horizontal includes both zero-slope and sloped pipes.
    """
    direction = get_pipe_axis_direction(pipe)
    if direction is None:
        return u"unknown"

    try:
        if abs(float(direction.Z)) >= PIPE_VERTICAL_Z_COS:
            return u"vertical"
    except Exception:
        return u"unknown"

    return u"horizontal"


def pipe_orientation_matches_settings(pipe, settings):
    """Apply the vertical/horizontal pipe toggles."""
    want_vertical = bool(settings.get("host_pipe_vertical", True))
    want_horizontal = bool(settings.get("host_pipe_horizontal", True))

    # Both selected = preserve historical behavior and include every pipe,
    # even a rare pipe whose direction cannot be resolved.
    if want_vertical and want_horizontal:
        return True

    orientation = get_pipe_orientation(pipe)
    if orientation == u"vertical":
        return want_vertical
    if orientation == u"horizontal":
        return want_horizontal

    # Unknown direction is intentionally excluded when filtering to only one
    # orientation so the result does not silently mix categories.
    return False


def pipe_orientation_label(pipe):
    orientation = get_pipe_orientation(pipe)
    if orientation == u"vertical":
        return u"Vertical Pipe"
    if orientation == u"horizontal":
        return u"Horizontal / Sloped Pipe"
    return u"Pipe"


# ============================================================
# HOST CATEGORY FILTER / INTERSECTION
# ============================================================

def build_host_filter(settings):
    filters = []

    if settings["host_pipe"]:
        filters.append(DB.ElementCategoryFilter(DB.BuiltInCategory.OST_PipeCurves))
    if settings["host_accessory"]:
        filters.append(DB.ElementCategoryFilter(DB.BuiltInCategory.OST_PipeAccessory))
    if settings["host_fitting"]:
        filters.append(DB.ElementCategoryFilter(DB.BuiltInCategory.OST_PipeFitting))

    if len(filters) == 1:
        return filters[0]

    if len(filters) > 1:
        net_filters = List[DB.ElementFilter]()
        for item in filters:
            net_filters.Add(item)
        return DB.LogicalOrFilter(net_filters)

    return None


def find_host_elements_intersecting_solid(host_filter, host_solid, settings):
    collector = make_host_collector(settings).WhereElementIsNotElementType()

    if host_filter is not None:
        collector = collector.WherePasses(host_filter)

    outline = solid_outline(host_solid)
    if outline is not None:
        try:
            collector = collector.WherePasses(DB.BoundingBoxIntersectsFilter(outline))
        except Exception:
            pass

    collector = collector.WherePasses(DB.ElementIntersectsSolidFilter(host_solid))

    try:
        ids = list(collector.ToElementIds())
    except Exception:
        return []

    # V2.8 region scope is a hard whitelist picked by the user in Active View.
    if is_region_scope(settings):
        ids = [element_id for element_id in ids if host_id_in_region(element_id, settings)]

    # Category/solid filters cannot classify pipe orientation, so apply the
    # Pipe direction filter after intersection. Accessories/Fittings are
    # never affected by this filter.
    if not settings.get("host_pipe", False):
        return ids

    if settings.get("host_pipe_vertical", True) and settings.get("host_pipe_horizontal", True):
        return ids

    filtered = []
    for element_id in ids:
        try:
            element = doc.GetElement(element_id)
            if element is None:
                continue
            if is_pipe_element(element):
                if pipe_orientation_matches_settings(element, settings):
                    filtered.append(element_id)
            else:
                filtered.append(element_id)
        except Exception:
            pass
    return filtered


# ============================================================
# TARGET RECORD CREATION
# ============================================================

def make_target_key(link, element):
    try:
        link_uid = to_text(link.UniqueId)
    except Exception:
        link_uid = to_text(id_value(link.Id))
    return u"{0}:{1}".format(link_uid, id_value(element.Id))


def get_element_type_name(element, element_doc):
    try:
        type_elem = element_doc.GetElement(element.GetTypeId())
        if type_elem:
            try:
                p = type_elem.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
                if p and p.AsString():
                    return to_text(p.AsString())
            except Exception:
                pass
            try:
                return to_text(type_elem.Name)
            except Exception:
                pass
    except Exception:
        pass
    return u""


def build_target_records(arch_links, struct_links, settings, warnings):
    records = []
    keywords = parse_keywords(settings["room_keywords"]) if settings["target_room"] else []

    # ARCHITECTURE LINKS: Rooms + Fire Rated Walls
    for link in arch_links:
        link_doc = link.GetLinkDocument()
        if link_doc is None:
            warnings.append(u"Architecture Link unloaded: {0}".format(get_link_label(link)))
            continue

        transform = link.GetTotalTransform()

        room_calc = None
        floor_bboxes = []
        if settings["target_room"]:
            try:
                room_calc = build_room_calculator(link_doc)
            except Exception as ex:
                warnings.append(u"Không tạo được Room calculator cho {0}: {1}".format(get_link_label(link), ex))

            if settings["extend_room"] and settings["room_ext_mode"] == u"auto":
                floor_bboxes = collect_floor_bboxes(link_doc)

            rooms = collect_link_category(
                link,
                DB.BuiltInCategory.OST_Rooms,
                settings,
                warnings,
                u"Room"
            )

            if room_calc is not None:
                for room in rooms:
                    try:
                        if room.Area <= 0:
                            continue
                    except Exception:
                        pass

                    room_name = get_room_name(room)
                    matched, keyword, score = room_name_matches(room_name, keywords)
                    if not matched:
                        continue

                    records.append({
                        "kind": u"Room",
                        "link": link,
                        "link_doc": link_doc,
                        "transform": transform,
                        "element": room,
                        "key": make_target_key(link, room),
                        "room_calc": room_calc,
                        "floor_bboxes": floor_bboxes,
                        "label": u"Room {0} - {1}".format(get_room_number(room), room_name),
                        "keyword": keyword,
                        "score": score,
                        "extra": u""
                    })

        if settings["target_firewall"]:
            walls = collect_link_category(
                link,
                DB.BuiltInCategory.OST_Walls,
                settings,
                warnings,
                u"Wall"
            )

            for wall in walls:
                rating = get_fire_rating_text(wall, link_doc)
                if not rating:
                    continue
                type_name = get_element_type_name(wall, link_doc)
                records.append({
                    "kind": u"Fire Rated Wall",
                    "link": link,
                    "link_doc": link_doc,
                    "transform": transform,
                    "element": wall,
                    "key": make_target_key(link, wall),
                    "label": u"Wall {0}".format(type_name or element_id_text(wall)),
                    "extra": u"Fire Rating: {0}".format(rating)
                })

    # STRUCTURE LINKS: Framing + Columns
    for link in struct_links:
        link_doc = link.GetLinkDocument()
        if link_doc is None:
            warnings.append(u"Structure Link unloaded: {0}".format(get_link_label(link)))
            continue

        transform = link.GetTotalTransform()

        category_requests = []
        if settings["target_beam"]:
            category_requests.append((u"Structural Framing", DB.BuiltInCategory.OST_StructuralFraming))
        if settings["target_column"]:
            category_requests.append((u"Structural Column", DB.BuiltInCategory.OST_StructuralColumns))

        for kind, bic in category_requests:
            elems = collect_link_category(
                link,
                bic,
                settings,
                warnings,
                kind
            )

            for elem in elems:
                type_name = get_element_type_name(elem, link_doc)
                records.append({
                    "kind": kind,
                    "link": link,
                    "link_doc": link_doc,
                    "transform": transform,
                    "element": elem,
                    "key": make_target_key(link, elem),
                    "label": u"{0} {1}".format(kind, type_name or element_id_text(elem)),
                    "extra": u""
                })

    return records


# ============================================================
# TARGET SOLIDS
# ============================================================

def get_target_host_solids(record, settings, warnings):
    elem = record["element"]

    if record["kind"] == u"Room":
        link_solids, ext_info = get_room_check_solids_link_coords(
            elem,
            record["room_calc"],
            settings,
            record["floor_bboxes"]
        )

        if not link_solids:
            warnings.append(
                u"Room không lấy được solid: {0} | Link {1}".format(
                    record["label"], get_link_label(record["link"])
                )
            )
            return [], ext_info

        return transform_solids(link_solids, record["transform"]), ext_info

    link_solids = get_element_solids_in_doc_coords(elem)
    if not link_solids:
        warnings.append(
            u"Không lấy được solid: {0} ID {1} | Link {2}".format(
                record["kind"], element_id_text(elem), get_link_label(record["link"])
            )
        )
        return [], None

    return transform_solids(link_solids, record["transform"]), None


# ============================================================
# CLASH SCAN
# ============================================================

def host_category_label(element):
    try:
        cid = id_value(element.Category.Id)
        if cid == int(DB.BuiltInCategory.OST_PipeCurves):
            return pipe_orientation_label(element)
        if cid == int(DB.BuiltInCategory.OST_PipeAccessory):
            return u"Pipe Accessory"
        if cid == int(DB.BuiltInCategory.OST_PipeFitting):
            return u"Pipe Fitting"
    except Exception:
        pass

    try:
        return to_text(element.Category.Name)
    except Exception:
        return u"Element"


def scan_clashes(records, settings, warnings):
    host_filter = build_host_filter(settings)
    clash_results = {}
    pair_seen = set()
    target_stats = {}
    room_extension_stats = {
        "manual": 0,
        "auto-floor": 0,
        "auto-fallback": 0,
        "failed": 0
    }

    total = len(records)

    # ProgressBar is optional. If unavailable in an older pyRevit build,
    # run the same processing without it.
    progress = None
    try:
        progress = forms.ProgressBar(
            title=u"Linked Clash Checker {value}/{max_value}",
            cancellable=True,
            step=1
        )
        progress.__enter__()
    except Exception:
        progress = None

    try:
        for index, record in enumerate(records):
            if progress is not None:
                try:
                    progress.update_progress(index + 1, total)
                    if progress.cancelled:
                        warnings.append(u"Người dùng đã Cancel quá trình scan tại {0}/{1}.".format(index + 1, total))
                        break
                except Exception:
                    pass

            kind = record["kind"]
            target_stats[kind] = target_stats.get(kind, 0) + 1

            host_solids, ext_info = get_target_host_solids(record, settings, warnings)
            if not host_solids:
                continue

            if kind == u"Room" and ext_info is not None:
                mode = to_text(ext_info.get("mode", u"native"))
                if u"failed" in mode:
                    room_extension_stats["failed"] += 1
                elif mode in room_extension_stats:
                    room_extension_stats[mode] += 1

                ext_mm = ext_info.get("extension_mm", 0.0)
                floor_id = ext_info.get("floor_id", None)
                if ext_mm > 0.0:
                    if floor_id is not None:
                        record["extra"] = u"Room extension: {0:.0f} mm → Floor ID {1}".format(ext_mm, floor_id)
                    else:
                        record["extra"] = u"Room extension: {0:.0f} mm ({1})".format(ext_mm, mode)

            # One target element can contain several solids. Dedupe host-target pair.
            for host_solid in host_solids:
                try:
                    host_ids = find_host_elements_intersecting_solid(host_filter, host_solid, settings)
                except Exception as ex:
                    warnings.append(
                        u"Intersection lỗi: {0} ID {1}: {2}".format(
                            kind, element_id_text(record["element"]), ex
                        )
                    )
                    continue

                for host_id in host_ids:
                    host_key = id_value(host_id)
                    pair_key = u"{0}|{1}".format(host_key, record["key"])
                    if pair_key in pair_seen:
                        continue
                    pair_seen.add(pair_key)

                    if host_key not in clash_results:
                        host_elem = doc.GetElement(host_id)
                        clash_results[host_key] = {
                            "id": host_id,
                            "category": host_category_label(host_elem) if host_elem else u"Host Element",
                            "hits": []
                        }

                    clash_results[host_key]["hits"].append({
                        "kind": kind,
                        "link": get_link_label(record["link"]),
                        "link_instance": record["link"],
                        "linked_element": record["element"],
                        "linked_id": id_value(record["element"].Id),
                        "label": record["label"],
                        "extra": record.get("extra", u""),
                        "keyword": record.get("keyword", None),
                        "score": record.get("score", None)
                    })

    finally:
        if progress is not None:
            try:
                progress.__exit__(None, None, None)
            except Exception:
                pass

    return clash_results, target_stats, room_extension_stats


# ============================================================
# INSPECTION MODE / SELECT-ZOOM - V2.9
# ============================================================

INSPECT_LABELS = {
    u"pipe": u"Pipe",
    u"accessory": u"Pipe Accessories",
    u"fitting": u"Pipe Fittings",
    u"room": u"Room",
    u"beam": u"Structural Framing",
    u"column": u"Structural Column",
    u"firewall": u"Fire Rated Wall"
}


def inspect_host_category(category_key, settings, warnings):
    mapping = {
        u"pipe": DB.BuiltInCategory.OST_PipeCurves,
        u"accessory": DB.BuiltInCategory.OST_PipeAccessory,
        u"fitting": DB.BuiltInCategory.OST_PipeFitting
    }
    bic = mapping.get(category_key)
    if bic is None:
        return []

    try:
        elems = list(
            make_host_collector(settings)
            .OfCategory(bic)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception as ex:
        warnings.append(u"Không collect được host {0}: {1}".format(INSPECT_LABELS.get(category_key, category_key), ex))
        return []

    records = []
    for elem in elems:
        if is_region_scope(settings) and not host_id_in_region(elem.Id, settings):
            continue
        if category_key == u"pipe" and not pipe_orientation_matches_settings(elem, settings):
            continue

        kind_label = INSPECT_LABELS.get(category_key, category_key)
        if category_key == u"pipe":
            kind_label = pipe_orientation_label(elem)

        records.append({
            "host": True,
            "kind": kind_label,
            "element": elem,
            "id": elem.Id,
            "label": get_element_type_name(elem, doc) or element_id_text(elem)
        })
    return records


def inspect_linked_category(category_key, arch_links, struct_links, settings, warnings):
    records = []

    if category_key in (u"room", u"firewall"):
        links = arch_links
    else:
        links = struct_links

    if category_key == u"room":
        bic = DB.BuiltInCategory.OST_Rooms
        keywords = parse_keywords(settings.get("room_keywords", u""))
        kind = u"Room"
    elif category_key == u"firewall":
        bic = DB.BuiltInCategory.OST_Walls
        keywords = []
        kind = u"Fire Rated Wall"
    elif category_key == u"beam":
        bic = DB.BuiltInCategory.OST_StructuralFraming
        keywords = []
        kind = u"Structural Framing"
    elif category_key == u"column":
        bic = DB.BuiltInCategory.OST_StructuralColumns
        keywords = []
        kind = u"Structural Column"
    else:
        return []

    for link in links:
        link_doc = link.GetLinkDocument()
        if link_doc is None:
            warnings.append(u"Link unloaded: {0}".format(get_link_label(link)))
            continue

        elems = collect_link_category(link, bic, settings, warnings, kind)

        for elem in elems:
            if category_key == u"room":
                try:
                    if elem.Area <= 0:
                        continue
                except Exception:
                    pass

                room_name = get_room_name(elem)
                matched, keyword, score = room_name_matches(room_name, keywords)
                if not matched:
                    continue
                label = u"Room {0} - {1}".format(get_room_number(elem), room_name)
                extra = u"Keyword: {0} | Match: {1:.0f}%".format(keyword, score * 100.0)

            elif category_key == u"firewall":
                rating = get_fire_rating_text(elem, link_doc)
                if not rating:
                    continue
                type_name = get_element_type_name(elem, link_doc)
                label = u"Wall {0}".format(type_name or element_id_text(elem))
                extra = u"Fire Rating: {0}".format(rating)

            else:
                type_name = get_element_type_name(elem, link_doc)
                label = u"{0} {1}".format(kind, type_name or element_id_text(elem))
                extra = u""

            records.append({
                "host": False,
                "kind": kind,
                "element": elem,
                "id": elem.Id,
                "link": link,
                "link_doc": link_doc,
                "label": label,
                "extra": extra
            })

    return records


def get_bbox_host_points(element, link=None):
    try:
        bbox = element.get_BoundingBox(None)
    except Exception:
        bbox = None

    if bbox is None:
        return []

    try:
        box_tr = bbox.Transform
    except Exception:
        box_tr = DB.Transform.Identity

    mn = bbox.Min
    mx = bbox.Max
    local_points = [
        DB.XYZ(mn.X, mn.Y, mn.Z),
        DB.XYZ(mx.X, mn.Y, mn.Z),
        DB.XYZ(mn.X, mx.Y, mn.Z),
        DB.XYZ(mx.X, mx.Y, mn.Z),
        DB.XYZ(mn.X, mn.Y, mx.Z),
        DB.XYZ(mx.X, mn.Y, mx.Z),
        DB.XYZ(mn.X, mx.Y, mx.Z),
        DB.XYZ(mx.X, mx.Y, mx.Z)
    ]

    link_tr = None
    if link is not None:
        try:
            link_tr = link.GetTotalTransform()
        except Exception:
            link_tr = None

    result = []
    for point in local_points:
        try:
            p = box_tr.OfPoint(point)
            if link_tr is not None:
                p = link_tr.OfPoint(p)
            result.append(p)
        except Exception:
            pass
    return result


def get_active_ui_view():
    try:
        for ui_view in uidoc.GetOpenUIViews():
            if id_value(ui_view.ViewId) == id_value(doc.ActiveView.Id):
                return ui_view
    except Exception:
        pass
    return None


def zoom_to_linked_records(records):
    points = []
    for record in records:
        points.extend(get_bbox_host_points(record["element"], record.get("link")))

    if not points:
        return False, u"Không lấy được bounding box để zoom."

    try:
        min_x = min(p.X for p in points)
        min_y = min(p.Y for p in points)
        min_z = min(p.Z for p in points)
        max_x = max(p.X for p in points)
        max_y = max(p.Y for p in points)
        max_z = max(p.Z for p in points)

        span = max(max_x - min_x, max_y - min_y, max_z - min_z)
        padding = max(1.0, span * 0.05)

        p1 = DB.XYZ(min_x - padding, min_y - padding, min_z - padding)
        p2 = DB.XYZ(max_x + padding, max_y + padding, max_z + padding)

        ui_view = get_active_ui_view()
        if ui_view is None:
            return False, u"Không tìm thấy UIView của Active View."

        ui_view.ZoomAndCenterRectangle(p1, p2)
        return True, u""
    except Exception as ex:
        return False, to_text(ex)


def zoom_to_host_records(records):
    points = []
    for record in records:
        points.extend(get_bbox_host_points(record.get("element"), None))

    if not points:
        return False, u"Không lấy được bounding box host để zoom."

    try:
        min_x = min(p.X for p in points)
        min_y = min(p.Y for p in points)
        min_z = min(p.Z for p in points)
        max_x = max(p.X for p in points)
        max_y = max(p.Y for p in points)
        max_z = max(p.Z for p in points)

        span = max(max_x - min_x, max_y - min_y, max_z - min_z)
        padding = max(1.0, span * 0.05)
        p1 = DB.XYZ(min_x - padding, min_y - padding, min_z - padding)
        p2 = DB.XYZ(max_x + padding, max_y + padding, max_z + padding)

        ui_view = get_active_ui_view()
        if ui_view is None:
            return False, u"Không tìm thấy UIView của Active View."
        ui_view.ZoomAndCenterRectangle(p1, p2)
        return True, u""
    except Exception as ex:
        return False, to_text(ex)


def select_host_records(records, warnings):
    if not records:
        try:
            uidoc.Selection.SetElementIds(List[DB.ElementId]())
        except Exception:
            pass
        return 0

    safe_records = records[:MAX_HOST_UI_SELECTION]
    if len(records) > MAX_HOST_UI_SELECTION:
        warnings.append(
            u"Tìm thấy {0} host elements. Vì an toàn UI, chỉ select/zoom {1} phần tử đầu; output vẫn liệt kê toàn bộ.".format(
                len(records), MAX_HOST_UI_SELECTION
            )
        )

    ids = List[DB.ElementId]()
    for record in safe_records:
        try:
            elem = record.get("element")
            if elem is not None and is_valid_api_object(elem):
                ids.Add(record["id"])
        except Exception:
            pass

    if ids.Count == 0:
        return 0

    try:
        uidoc.Selection.SetElementIds(ids)
    except Exception as ex:
        warnings.append(u"Không select được host elements: {0}".format(ex))
        return 0

    # V2.9: do NOT call uidoc.ShowElements() on a bulk MEP set. Zoom with the
    # current UIView and bounding boxes instead; this avoids asking Revit to
    # resolve/show a potentially huge mixed set through a native UI call.
    zoom_ok, zoom_error = zoom_to_host_records(safe_records)
    if not zoom_ok and zoom_error:
        warnings.append(u"Không zoom được host elements: {0}".format(zoom_error))

    return ids.Count


def select_linked_records(records, warnings):
    refs = List[DB.Reference]()

    if not records:
        try:
            uidoc.Selection.SetElementIds(List[DB.ElementId]())
        except Exception:
            pass
        return 0

    safe_records = records[:MAX_LINKED_UI_SELECTION]
    if len(records) > MAX_LINKED_UI_SELECTION:
        warnings.append(
            u"Tìm thấy {0} linked elements. Vì an toàn UI, chỉ select/zoom {1} phần tử đầu; output vẫn liệt kê toàn bộ.".format(
                len(records), MAX_LINKED_UI_SELECTION
            )
        )

    valid_records = []
    for record in safe_records:
        try:
            link = record.get("link")
            elem = record.get("element")
            if not is_valid_loaded_link(link) or not is_valid_api_object(elem):
                continue
            link_ref = DB.Reference(elem).CreateLinkReference(link)
            refs.Add(link_ref)
            valid_records.append(record)
        except Exception as ex:
            warnings.append(
                u"Không tạo linked Reference | {0} | Linked ID {1}: {2}".format(
                    get_link_label(record.get("link")),
                    id_value(record.get("id")),
                    ex
                )
            )

    if refs.Count > 0:
        try:
            uidoc.Selection.SetReferences(refs)
        except Exception as ex:
            warnings.append(u"Không SetReferences được linked elements: {0}".format(ex))
            return 0

    zoom_ok, zoom_error = zoom_to_linked_records(valid_records)
    if not zoom_ok and zoom_error:
        warnings.append(u"Không zoom được linked elements: {0}".format(zoom_error))

    return refs.Count


def print_inspect_results(settings, category_key, records, warnings):
    label = INSPECT_LABELS.get(category_key, category_key)
    output.print_md(u"# INSPECTION MODE - LINKED CLASH CHECKER V2.9")
    print(u"Scope: {0}".format(scope_label(settings)))
    if is_region_scope(settings) and category_key in (u"pipe", u"accessory", u"fitting"):
        print(u"Host MEP trong vùng đã chọn: {0}".format(settings.get("_region_host_count", 0)))
    print(u"Category: {0}".format(label))
    print(u"Found: {0}".format(len(records)))
    print(u"")

    if not records:
        output.print_md(u"### Không tìm thấy đối tượng phù hợp.")
    elif records[0].get("host"):
        all_ids = [record["id"] for record in records]
        try:
            print(output.linkify(all_ids, title=u"SELECT / ZOOM TẤT CẢ {0}".format(label.upper())))
        except Exception:
            pass
        print(u"")

        for index, record in enumerate(records):
            eid = id_value(record["id"])
            try:
                clickable = output.linkify(record["id"], title=u"{0} ID {1}".format(label, eid))
            except Exception:
                clickable = u"{0} ID {1}".format(label, eid)
            print(u"{0}. {1}".format(index + 1, clickable))
    else:
        print(u"Các linked element bên dưới đã được tool select bằng linked Reference và zoom trong Active View.")
        print(u"Linked ID không dùng pyRevit linkify vì linkify chỉ nhận ElementId của current project.")
        print(u"")

        for index, record in enumerate(records):
            print(u"{0}. {1} | Linked ID {2}".format(index + 1, record["kind"], id_value(record["id"])))
            print(u"    Link: {0}".format(get_link_label(record["link"])))
            print(u"    {0}".format(record["label"]))
            if record.get("extra"):
                print(u"    {0}".format(record["extra"]))

    if warnings:
        output.print_md(u"## Cảnh báo ({0})".format(len(warnings)))
        for warning in warnings[:MAX_WARNINGS]:
            print(u"- {0}".format(warning))
        if len(warnings) > MAX_WARNINGS:
            print(u"... còn {0} cảnh báo khác.".format(len(warnings) - MAX_WARNINGS))


def run_inspect_mode(arch_links, struct_links, settings):
    category_key = to_text(settings.get("inspection_category", settings.get("inspect_category", u"pipe"))).lower()
    warnings = []

    if category_key in (u"pipe", u"accessory", u"fitting"):
        records = inspect_host_category(category_key, settings, warnings)
        selected_count = select_host_records(records, warnings)
    else:
        records = inspect_linked_category(category_key, arch_links, struct_links, settings, warnings)
        selected_count = select_linked_records(records, warnings)

    print_inspect_results(settings, category_key, records, warnings)
    print(u"")
    print(u"Selected / referenced: {0}".format(selected_count))


# ============================================================
# CLASH LINKED TARGET NAVIGATION - V2.9
# ============================================================

def linked_records_from_clashes(clash_results):
    """Build unique linked records from clash hits for select + zoom."""
    result = []
    seen = set()

    for host_key in sorted(clash_results.keys()):
        data = clash_results[host_key]
        for hit in data.get("hits", []):
            link = hit.get("link_instance")
            elem = hit.get("linked_element")
            if link is None or elem is None:
                continue

            try:
                key = u"{0}|{1}".format(to_text(link.UniqueId), id_value(elem.Id))
            except Exception:
                key = u"{0}|{1}".format(id_value(link.Id), id_value(elem.Id))

            if key in seen:
                continue
            seen.add(key)

            result.append({
                "host": False,
                "kind": hit.get("kind", u"Linked Element"),
                "id": elem.Id,
                "element": elem,
                "link": link,
                "label": hit.get("label", u""),
                "extra": hit.get("extra", u"")
            })

    return result


CLASH_HEADER_ORDER = [
    u"Room",
    u"Structural Framing",
    u"Structural Column",
    u"Fire Rated Wall"
]


def enabled_clash_headers(settings):
    values = []
    if settings.get("target_room"):
        values.append(u"Room")
    if settings.get("target_beam"):
        values.append(u"Structural Framing")
    if settings.get("target_column"):
        values.append(u"Structural Column")
    if settings.get("target_firewall"):
        values.append(u"Fire Rated Wall")
    return values


def grouped_clash_pairs(clash_results):
    """Return kind -> [(host_key, host_data, hit), ...]."""
    grouped = {}
    for kind in CLASH_HEADER_ORDER:
        grouped[kind] = []

    for host_key in sorted(clash_results.keys()):
        data = clash_results[host_key]
        for hit in data.get("hits", []):
            kind = hit.get("kind", u"Other")
            grouped.setdefault(kind, []).append((host_key, data, hit))

    return grouped


# ============================================================
# OUTPUT
# ============================================================

def selected_host_labels(settings):
    values = []
    if settings["host_pipe"]:
        pipe_parts = []
        if settings.get("host_pipe_vertical", True):
            pipe_parts.append(u"Vertical")
        if settings.get("host_pipe_horizontal", True):
            pipe_parts.append(u"Horizontal / Sloped")
        if len(pipe_parts) == 2:
            values.append(u"Pipe [Vertical + Horizontal / Sloped]")
        elif pipe_parts:
            values.append(u"Pipe [{0}]".format(pipe_parts[0]))
        else:
            values.append(u"Pipe [NONE]")
    if settings["host_accessory"]:
        values.append(u"Pipe Accessories")
    if settings["host_fitting"]:
        values.append(u"Pipe Fittings")
    return values


def selected_target_labels(settings):
    values = []
    if settings["target_room"]:
        values.append(u"Room Name")
    if settings["target_beam"]:
        values.append(u"Structural Framing")
    if settings["target_column"]:
        values.append(u"Structural Column")
    if settings["target_firewall"]:
        values.append(u"Fire Rated Wall")
    return values


def print_results(arch_links, struct_links, settings, clash_results, target_stats, room_ext_stats, warnings):
    output.print_md(u"# LINKED CLASH CHECKER V2.9")

    output.print_md(u"## Thiết lập")
    print(u"Scope: {0}".format(scope_label(settings)))
    if is_active_view_scope(settings):
        try:
            print(u"Active View: {0}".format(to_text(doc.ActiveView.Name)))
        except Exception:
            pass
    if is_region_scope(settings):
        print(u"Host MEP trong vùng đã chọn: {0}".format(settings.get("_region_host_count", 0)))
    print(u"HOST: {0}".format(u", ".join(selected_host_labels(settings))))
    print(u"LINK TARGET: {0}".format(u", ".join(selected_target_labels(settings))))
    print(u"Zoom linked clash targets after check: {0}".format(
        u"ON" if settings.get("zoom_linked_after_clash", False) else u"OFF"
    ))
    if settings.get("_linked_zoom_count") is not None:
        print(u"Linked target đã Select / Zoom: {0}".format(settings.get("_linked_zoom_count", 0)))

    if settings["target_room"]:
        print(u"Room keywords: {0}".format(settings["room_keywords"]))
        print(u"Fuzzy threshold: {0:.0f}%".format(FUZZY_THRESHOLD * 100.0))
        if settings["extend_room"]:
            print(u"Room extension: ON | Mode: {0} | Offset/fallback: {1:.0f} mm".format(
                settings["room_ext_mode"], settings["room_offset_mm"]
            ))
        else:
            print(u"Room extension: OFF")

    print(u"")
    print(u"Architecture links:")
    for link in arch_links:
        print(u"  - {0}".format(get_link_label(link)))
    if not arch_links:
        print(u"  - (none)")

    print(u"Structure links:")
    for link in struct_links:
        print(u"  - {0}".format(get_link_label(link)))
    if not struct_links:
        print(u"  - (none)")

    output.print_md(u"## Target đã quét")
    for kind in sorted(target_stats.keys()):
        print(u"{0}: {1}".format(kind, target_stats[kind]))

    if settings["target_room"] and settings["extend_room"]:
        print(u"")
        print(u"Room extension stats:")
        print(u"  Manual: {0}".format(room_ext_stats.get("manual", 0)))
        print(u"  Auto → Floor: {0}".format(room_ext_stats.get("auto-floor", 0)))
        print(u"  Auto fallback: {0}".format(room_ext_stats.get("auto-fallback", 0)))
        print(u"  Extension failed: {0}".format(room_ext_stats.get("failed", 0)))

    output.print_md(u"## Kết quả")
    print(u"Host element có va chạm: {0}".format(len(clash_results)))
    pair_count = sum(len(item["hits"]) for item in clash_results.values())
    print(u"Tổng clash pairs: {0}".format(pair_count))
    print(u"")

    if not clash_results:
        output.print_md(u"### Không tìm thấy va chạm theo các lựa chọn hiện tại.")
    else:
        all_ids = [clash_results[k]["id"] for k in sorted(clash_results.keys())]
        try:
            print(output.linkify(all_ids, title=u"SELECT / ZOOM TẤT CẢ HOST ELEMENT VA CHẠM"))
        except Exception:
            pass
        print(u"")

        grouped = grouped_clash_pairs(clash_results)
        global_index = 0

        # IMPORTANT: numbering does NOT reset between headers.
        for kind in enabled_clash_headers(settings):
            pairs = grouped.get(kind, [])
            output.print_md(u"## {0} CLASH ({1})".format(kind.upper(), len(pairs)))

            if not pairs:
                print(u"Không có clash trong nhóm này.")
                print(u"")
                continue

            for host_key, data, hit in pairs:
                global_index += 1
                try:
                    host_link = output.linkify(
                        data["id"],
                        title=u"{0} ID {1}".format(data["category"], host_key)
                    )
                except Exception:
                    host_link = u"{0} ID {1}".format(data["category"], host_key)

                print(u"{0}. {1}".format(global_index, host_link))
                print(u"    ↳ {0} | Linked ID {1}".format(hit["kind"], hit["linked_id"]))
                print(u"       Link: {0}".format(hit["link"]))
                print(u"       Target: {0}".format(hit["label"]))
                if hit.get("keyword") is not None:
                    print(u"       Keyword: {0} | Match: {1:.0f}%".format(
                        hit["keyword"], float(hit.get("score", 0.0)) * 100.0
                    ))
                if hit.get("extra"):
                    print(u"       {0}".format(hit["extra"]))
                print(u"------------------------------------------------------------")

    if warnings:
        output.print_md(u"## Cảnh báo / phần tử bỏ qua ({0})".format(len(warnings)))
        for warning in warnings[:MAX_WARNINGS]:
            print(u"- {0}".format(warning))
        if len(warnings) > MAX_WARNINGS:
            print(u"... còn {0} cảnh báo khác.".format(len(warnings) - MAX_WARNINGS))


# ============================================================
# MAIN
# ============================================================

def main():
    # User-requested flow: pick/reuse links FIRST, then show options UI.
    arch_links, struct_links = get_link_groups()

    settings = show_options(arch_links, struct_links)

    valid_scope, scope_error = validate_scope(settings)
    if not valid_scope:
        forms.alert(scope_error, title="Linked Clash Checker")
        script.exit()

    # V2.9: when Region scope is selected, the WPF window is already closed.
    # Revit now prompts for a native rectangle selection in the Active View.
    region_ok, region_error = pick_host_region_elements(settings)
    if not region_ok:
        if region_error:
            forms.alert(region_error, title="Linked Clash Checker - Active View Region")
        script.exit()

    # --------------------------------------------------------
    # INSPECTION MODE / SELECT-ZOOM - V2.9 SAFE MODE
    # --------------------------------------------------------
    if settings.get("inspection_mode", settings.get("inspect_select", False)):
        category_key = to_text(settings.get("inspection_category", settings.get("inspect_category", u"pipe"))).lower()

        # IMPORTANT: Host MEP inspection must not touch saved Revit Links at
        # all. Changing Active View between runs therefore cannot make an old
        # saved Link participate in Pipe / Accessory / Fitting inspection.
        if category_key in (u"pipe", u"accessory", u"fitting"):
            run_inspect_mode([], [], settings)
            return

        link_safety_warnings = []
        if is_active_view_scope(settings):
            arch_links = sanitize_links_for_scope(arch_links, settings, link_safety_warnings, u"Architecture Link")
            struct_links = sanitize_links_for_scope(struct_links, settings, link_safety_warnings, u"Structure Link")
            for warning in link_safety_warnings:
                print(u"[SAFE MODE] {0}".format(warning))

        if category_key in (u"room", u"firewall") and not arch_links:
            forms.alert(
                u"Inspect {0} không có Architecture Link hợp lệ trong scope hiện tại.\n\n"
                u"Nếu đang dùng Active View/Region: Link đã lưu có thể không hiển thị trong view này. "
                u"Hãy chuyển Entire Model hoặc Pick lại Link phù hợp.".format(
                    INSPECT_LABELS.get(category_key, category_key)
                ),
                title="Linked Clash Checker"
            )
            script.exit()

        if category_key in (u"beam", u"column") and not struct_links:
            forms.alert(
                u"Inspect {0} không có Structure Link hợp lệ trong scope hiện tại.\n\n"
                u"Nếu đang dùng Active View/Region: Link đã lưu có thể không hiển thị trong view này. "
                u"Hãy chuyển Entire Model hoặc Pick lại Link phù hợp.".format(
                    INSPECT_LABELS.get(category_key, category_key)
                ),
                title="Linked Clash Checker"
            )
            script.exit()

        run_inspect_mode(arch_links, struct_links, settings)
        return

    # --------------------------------------------------------
    # NORMAL CLASH MODE - V2.9 SAFE LINK VALIDATION
    # --------------------------------------------------------
    link_safety_warnings = []
    if is_active_view_scope(settings):
        arch_links = sanitize_links_for_scope(arch_links, settings, link_safety_warnings, u"Architecture Link")
        struct_links = sanitize_links_for_scope(struct_links, settings, link_safety_warnings, u"Structure Link")
        for warning in link_safety_warnings:
            print(u"[SAFE MODE] {0}".format(warning))

    if (settings["target_room"] or settings["target_firewall"]) and not arch_links:
        forms.alert(
            u"Không có Architecture Link hợp lệ trong scope hiện tại.\n\n"
            u"Nếu đang dùng Active View/Region: Link đã lưu có thể không hiển thị trong view này. "
            u"Hãy chuyển Entire Model hoặc Pick lại Link phù hợp.",
            title="Linked Clash Checker"
        )
        script.exit()

    if (settings["target_beam"] or settings["target_column"]) and not struct_links:
        forms.alert(
            u"Không có Structure Link hợp lệ trong scope hiện tại.\n\n"
            u"Nếu đang dùng Active View/Region: Link đã lưu có thể không hiển thị trong view này. "
            u"Hãy chuyển Entire Model hoặc Pick lại Link phù hợp.",
            title="Linked Clash Checker"
        )
        script.exit()

    warnings = []
    records = build_target_records(arch_links, struct_links, settings, warnings)

    if not records:
        print_results(
            arch_links, struct_links, settings,
            {}, {}, {}, warnings
        )
        return

    clash_results, target_stats, room_ext_stats = scan_clashes(records, settings, warnings)

    if settings.get("zoom_linked_after_clash", False) and clash_results:
        linked_clash_records = linked_records_from_clashes(clash_results)
        if linked_clash_records:
            selected_count = select_linked_records(linked_clash_records, warnings)
            settings["_linked_zoom_count"] = selected_count

    print_results(
        arch_links,
        struct_links,
        settings,
        clash_results,
        target_stats,
        room_ext_stats,
        warnings
    )


if __name__ == "__main__":
    main()
