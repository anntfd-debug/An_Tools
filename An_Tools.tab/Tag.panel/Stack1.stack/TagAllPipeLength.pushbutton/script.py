# -*- coding: utf-8 -*-
from __future__ import unicode_literals
__title__ = u'Tag MEP Pro V3.1'
__doc__ = u'Tạo mới, align hoặc chuẩn hóa Tag cho Pipe, Conduit, Fire Alarm Device, Plumbing Fixture và Pipe Accessory; lọc chiều dài, kích thước, hướng, View Range và tự lưu cấu hình.'

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.Exceptions import OperationCanceledException
from pyrevit import revit, UI, script, forms
import os
import json
import traceback

# -----------------------------------------------------------------------------
# UNICODE / TIẾNG VIỆT HELPERS
# -----------------------------------------------------------------------------
# Không dùng forms.toast/forms.alert cho tiếng Việt (lỗi mojibake trên một số bản
# pyRevit) -> dùng Revit native TaskDialog.
try:
    unicode
except NameError:
    unicode = str

TASK_DIALOG_TITLE = u'Tag MEP Pro'


def ensure_unicode(value):
    if value is None:
        return u''
    if isinstance(value, unicode):
        return value
    try:
        return value.decode('utf-8')
    except:
        try:
            return unicode(value)
        except:
            return str(value)


def revit_message(message, title=TASK_DIALOG_TITLE):
    msg = ensure_unicode(message)
    ttl = ensure_unicode(title)
    try:
        TaskDialog.Show(ttl, msg)
    except:
        try:
            print(msg.encode('utf-8') if hasattr(msg, 'encode') else msg)
        except:
            pass


def alert_msg(message):
    revit_message(message)


def toast_msg(message):
    revit_message(message)


def log(message):
    try:
        print(ensure_unicode(message))
    except:
        pass


# -----------------------------------------------------------------------------
# ĐỊNH NGHĨA CÁC LOẠI ĐỐI TƯỢNG ĐƯỢC TAG
# -----------------------------------------------------------------------------
# kind: 'pipe' | 'conduit' | 'fa' | 'pf' | 'pa'
#   - linear = True  -> đối tượng dạng đường (LocationCurve), xếp bậc theo cụm song song
#   - linear = False -> đối tượng dạng điểm (FamilyInstance), tag lệch trái/phải
KIND_ORDER = ['pipe', 'conduit', 'fa', 'pf', 'pa']

KIND_INFO = {
    'pipe': {
        'label': u'Pipe',
        'cat': BuiltInCategory.OST_PipeCurves,
        'tag_cat': BuiltInCategory.OST_PipeTags,
        'size_bip': BuiltInParameter.RBS_PIPE_DIAMETER_PARAM,
        'linear': True,
    },
    'conduit': {
        'label': u'Conduit',
        'cat': BuiltInCategory.OST_Conduit,
        'tag_cat': BuiltInCategory.OST_ConduitTags,
        'size_bip': BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM,
        'linear': True,
    },
    'fa': {
        'label': u'Fire Alarm Device',
        'cat': BuiltInCategory.OST_FireAlarmDevices,
        'tag_cat': BuiltInCategory.OST_FireAlarmDeviceTags,
        'size_bip': None,
        'linear': False,
    },
    'pf': {
        'label': u'Plumbing Fixture',
        'cat': BuiltInCategory.OST_PlumbingFixtures,
        'tag_cat': BuiltInCategory.OST_PlumbingFixtureTags,
        'size_bip': None,
        'linear': False,
    },
    'pa': {
        'label': u'Pipe Accessory',
        'cat': BuiltInCategory.OST_PipeAccessory,
        'tag_cat': BuiltInCategory.OST_PipeAccessoryTags,
        'size_bip': None,
        'linear': False,
    },
}

CAT_TO_KIND = {}
TAG_CAT_TO_KIND = {}
for _k in KIND_ORDER:
    CAT_TO_KIND[int(KIND_INFO[_k]['cat'])] = _k
    TAG_CAT_TO_KIND[int(KIND_INFO[_k]['tag_cat'])] = _k


# -----------------------------------------------------------------------------
# FILE PATHS / SETTINGS
# -----------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(__file__)
xaml_file = os.path.join(SCRIPT_DIR, 'ui_definition.xaml')
# Giữ nguyên tên file cấu hình để nạp lại được setting của bản V2.0.
CONFIG_FILENAME = 'tag_parallel_pipes_settings.json'
CONFIG_FILE = os.path.join(SCRIPT_DIR, CONFIG_FILENAME)


def get_fallback_config_file():
    appdata = os.getenv('APPDATA')
    if appdata:
        folder = os.path.join(appdata, 'pyRevit_TagPipesPro')
    else:
        folder = os.path.join(os.path.expanduser('~'), '.pyRevit_TagPipesPro')
    try:
        if not os.path.exists(folder):
            os.makedirs(folder)
    except:
        pass
    return os.path.join(folder, CONFIG_FILENAME)


FALLBACK_CONFIG_FILE = get_fallback_config_file()

DEFAULT_SETTINGS = {
    'spacing_mm': '5.0',
    'min_length_mm': '500',
    'max_length_mm': '10000',
    'use_max_length': False,
    'min_size_mm': '15',
    'max_size_mm': '1000',
    'use_max_size': True,
    'scope': 'selection',          # selection | view
    'orientation': 'all',          # all | horizontal | vertical
    'tag_condition': 'all',        # all | untagged
    'operation_mode': 'create',    # create | align | normalize
    'tag_side': 'left',            # left | right
    'view_range_mode': 'none',     # none | manual | active
    'view_range_bottom_mm': '-1000',
    'view_range_top_mm': '3000',
    # Đối tượng cần tag (mới)
    'use_pipe': True,
    'use_conduit': False,
    'use_fa': False,
    'tag_type': '',                # Pipe tag (giữ key cũ để tương thích V2.0)
    'conduit_tag_type': '',
    'fa_tag_type': '',
    'use_pf': False,
    'use_pa': False,
    'pf_tag_type': '',
    'pa_tag_type': '',
    # Fire Alarm Device (mới)
    'device_offset_mm': '6.0',
    'device_leader': False,
}


def to_text(value):
    return ensure_unicode(value)


def setting_to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    text = to_text(value).strip().lower()
    if text in ('true', '1', 'yes', 'on'):
        return True
    if text in ('false', '0', 'no', 'off', ''):
        return False
    return default


def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    for path in [CONFIG_FILE, FALLBACK_CONFIG_FILE]:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, 'r') as fp:
                saved_data = json.load(fp)
            if isinstance(saved_data, dict):
                settings.update(saved_data)
            break
        except:
            pass
    return settings


def write_settings_file(path, settings):
    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder)
    with open(path, 'w') as fp:
        json.dump(settings, fp, indent=2, sort_keys=True)


def save_settings(settings):
    try:
        write_settings_file(CONFIG_FILE, settings)
        return CONFIG_FILE
    except:
        try:
            write_settings_file(FALLBACK_CONFIG_FILE, settings)
            return FALLBACK_CONFIG_FILE
        except:
            return None


# -----------------------------------------------------------------------------
# HELPERS CHUNG
# -----------------------------------------------------------------------------
def mm_to_feet(mm, scale):
    return (mm * scale) / 304.8


def reverse_xyz(vec):
    try:
        return vec.Negate()
    except:
        return vec * -1.0


def get_id_value(element_id):
    """Lấy ID dạng số, tương thích Revit 2024+ (Value) và bản cũ (IntegerValue)."""
    try:
        return int(element_id.Value)
    except:
        try:
            return int(element_id.IntegerValue)
        except:
            return None


def parse_mm_value(text_value, field_name):
    try:
        return float(to_text(text_value).replace(',', '.'))
    except:
        raise ValueError(u'{} phải là số hợp lệ.'.format(field_name))


def get_element_kind(elem):
    """Trả về 'pipe' | 'conduit' | 'fa' hoặc None."""
    try:
        if not elem or not elem.Category:
            return None
        return CAT_TO_KIND.get(get_id_value(elem.Category.Id))
    except:
        return None


def get_tag_kind(tag):
    try:
        if not tag or not tag.Category:
            return None
        return TAG_CAT_TO_KIND.get(get_id_value(tag.Category.Id))
    except:
        return None


def has_valid_curve(elem):
    try:
        return bool(elem.Location and hasattr(elem.Location, 'Curve') and elem.Location.Curve)
    except:
        return False


def is_valid_target(elem, allowed_kinds):
    kind = get_element_kind(elem)
    if kind is None or kind not in allowed_kinds:
        return False
    if KIND_INFO[kind]['linear']:
        return has_valid_curve(elem)
    return get_element_point(elem) is not None


def get_element_point(elem):
    """Điểm đặt của thiết bị: LocationPoint, nếu không có thì tâm BoundingBox."""
    try:
        loc = elem.Location
        if loc and hasattr(loc, 'Point') and loc.Point:
            return loc.Point
    except:
        pass
    try:
        bb = elem.get_BoundingBox(None)
        if bb:
            return (bb.Min + bb.Max) * 0.5
    except:
        pass
    return None


def are_curves_parallel(e1, e2):
    d1 = (e1.Location.Curve.GetEndPoint(1) - e1.Location.Curve.GetEndPoint(0)).Normalize()
    d2 = (e2.Location.Curve.GetEndPoint(1) - e2.Location.Curve.GetEndPoint(0)).Normalize()
    return abs(d1.DotProduct(d2)) > 0.95


def get_perpendicular_distance(e1, e2):
    pt1 = e1.Location.Curve.Evaluate(0.5, True)
    pt2 = e2.Location.Curve.Evaluate(0.5, True)
    d1 = (e1.Location.Curve.GetEndPoint(1) - e1.Location.Curve.GetEndPoint(0)).Normalize()
    v = pt2 - pt1
    return (v - d1 * v.DotProduct(d1)).GetLength()


# -----------------------------------------------------------------------------
# VIEW RANGE
# -----------------------------------------------------------------------------
def get_view_base_elevation(view):
    try:
        level = view.GenLevel
        if level:
            return level.Elevation
    except:
        pass
    return 0.0


def is_valid_level_id(level_id):
    try:
        return level_id and get_id_value(level_id) != -1
    except:
        return False


def get_plan_view_plane_z(doc, view, plan_view_range, plane):
    try:
        level_id = plan_view_range.GetLevelId(plane)
        offset = plan_view_range.GetOffset(plane)
        level_elevation = get_view_base_elevation(view)
        if is_valid_level_id(level_id):
            level = doc.GetElement(level_id)
            if level and hasattr(level, 'Elevation'):
                level_elevation = level.Elevation
        return level_elevation + offset
    except:
        return None


def get_active_view_range_bounds(doc, view):
    try:
        pvr = view.GetViewRange()
    except:
        return None
    top_z = get_plan_view_plane_z(doc, view, pvr, PlanViewPlane.TopClipPlane)
    bottom_z = get_plan_view_plane_z(doc, view, pvr, PlanViewPlane.BottomClipPlane)
    view_depth_z = get_plan_view_plane_z(doc, view, pvr, PlanViewPlane.ViewDepthPlane)
    values = [v for v in (top_z, bottom_z, view_depth_z) if v is not None]
    if len(values) < 2:
        return None
    upper = top_z if top_z is not None else max(values)
    lower_candidates = [v for v in (bottom_z, view_depth_z) if v is not None]
    lower = min(lower_candidates) if lower_candidates else min(values)
    if upper < lower:
        upper, lower = lower, upper
    return (lower, upper)


def get_element_z_span(elem):
    try:
        bb = elem.get_BoundingBox(None)
        if bb:
            return (bb.Min.Z, bb.Max.Z)
    except:
        pass
    try:
        curve = elem.Location.Curve
        z0 = curve.GetEndPoint(0).Z
        z1 = curve.GetEndPoint(1).Z
        return (min(z0, z1), max(z0, z1))
    except:
        pass
    pt = get_element_point(elem)
    if pt is not None:
        return (pt.Z, pt.Z)
    return None


def element_overlaps_z_range(elem, bottom_z, top_z):
    span = get_element_z_span(elem)
    if not span:
        return False
    return span[1] >= bottom_z and span[0] <= top_z


# -----------------------------------------------------------------------------
# SELECTION FILTER - Pipe / Conduit / Fire Alarm Device
# -----------------------------------------------------------------------------
class TagTargetSelectionFilter(ISelectionFilter):
    def __init__(self, allowed_kinds, view_range_bounds=None):
        self.allowed_kinds = list(allowed_kinds)
        self.view_range_bounds = view_range_bounds

    def AllowElement(self, elem):
        try:
            if not is_valid_target(elem, self.allowed_kinds):
                return False
            if self.view_range_bounds:
                return element_overlaps_z_range(
                    elem, self.view_range_bounds[0], self.view_range_bounds[1])
            return True
        except:
            return False

    def AllowReference(self, reference, position):
        return False


# -----------------------------------------------------------------------------
# WPF WINDOW
# -----------------------------------------------------------------------------
class TagMEPWPF(forms.WPFWindow):
    def __init__(self, xaml_file_path):
        forms.WPFWindow.__init__(self, xaml_file_path)
        self.result = None
        self.settings = load_settings()

        # kind -> (checkbox, combobox, settings key của checkbox, settings key của tag type)
        self.kind_controls = {
            'pipe': (self.chkPipe, self.cbPipeTagTypes, 'use_pipe', 'tag_type'),
            'conduit': (self.chkConduit, self.cbConduitTagTypes, 'use_conduit', 'conduit_tag_type'),
            'fa': (self.chkFireAlarm, self.cbFireAlarmTagTypes, 'use_fa', 'fa_tag_type'),
            'pf': (self.chkPlumbingFixture, self.cbPlumbingFixtureTagTypes, 'use_pf', 'pf_tag_type'),
            'pa': (self.chkPipeAccessory, self.cbPipeAccessoryTagTypes, 'use_pa', 'pa_tag_type'),
        }
        self.tag_maps = {}

        self.setup_tag_comboboxes()
        self.apply_saved_settings()

        # Card thông số thiết bị chỉ bật khi có ít nhất 1 loại thiết bị dạng điểm được tick
        for kind in KIND_ORDER:
            if not KIND_INFO[kind]['linear']:
                chk = self.kind_controls[kind][0]
                chk.Checked += self.on_kind_toggled
                chk.Unchecked += self.on_kind_toggled
        self.on_kind_toggled(None, None)

        self.btnExecute.Click += self.on_execute_click
        self.btnCancel.Click += self.on_close_click

    def on_kind_toggled(self, sender, args):
        try:
            self.cardDevice.IsEnabled = any(
                bool(self.kind_controls[k][0].IsChecked)
                for k in KIND_ORDER if not KIND_INFO[k]['linear'])
        except:
            pass

    # ------------------------------------------------------------------
    def set_status(self, message):
        try:
            self.txtSettingsStatus.Text = message
        except:
            pass

    def set_radio_value(self, radio_map, saved_value, default_value):
        if saved_value not in radio_map:
            saved_value = default_value
        for key in radio_map:
            radio_map[key].IsChecked = (key == saved_value)

    # ------------------------------------------------------------------
    def build_tag_map(self, doc, tag_bic):
        symbols = FilteredElementCollector(doc)\
            .OfClass(FamilySymbol)\
            .OfCategory(tag_bic)\
            .ToElements()
        tag_map = {}
        for t in symbols:
            type_param = t.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
            type_name = type_param.AsString() if type_param else None
            fam_param = t.get_Parameter(BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM)
            fam_name = fam_param.AsString() if fam_param else u'Unknown Family'
            if type_name:
                tag_map[u'{} - {}'.format(fam_name, type_name)] = t
        return tag_map

    def setup_tag_comboboxes(self):
        doc = revit.doc
        for kind in KIND_ORDER:
            chk, cb, _, _ = self.kind_controls[kind]
            tag_map = self.build_tag_map(doc, KIND_INFO[kind]['tag_cat'])
            self.tag_maps[kind] = tag_map
            cb.ItemsSource = sorted(tag_map.keys())
            if tag_map:
                cb.SelectedIndex = 0
            else:
                try:
                    chk.ToolTip = u'Model chưa load family {} Tag. Align vẫn dùng được; Tạo mới / Đổi Type cần load tag trước.'.format(
                        KIND_INFO[kind]['label'])
                except:
                    pass

    # ------------------------------------------------------------------
    def apply_saved_settings(self):
        s = self.settings
        self.txtSpacing.Text = to_text(s.get('spacing_mm', DEFAULT_SETTINGS['spacing_mm']))
        self.txtMinLength.Text = to_text(s.get('min_length_mm', DEFAULT_SETTINGS['min_length_mm']))
        self.txtMaxLength.Text = to_text(s.get('max_length_mm', DEFAULT_SETTINGS['max_length_mm']))
        self.chkLimitMaxLength.IsChecked = setting_to_bool(
            s.get('use_max_length', DEFAULT_SETTINGS['use_max_length']), DEFAULT_SETTINGS['use_max_length'])
        self.txtMinSize.Text = to_text(s.get('min_size_mm', DEFAULT_SETTINGS['min_size_mm']))
        self.txtMaxSize.Text = to_text(s.get('max_size_mm', DEFAULT_SETTINGS['max_size_mm']))
        self.chkLimitMaxSize.IsChecked = setting_to_bool(
            s.get('use_max_size', DEFAULT_SETTINGS['use_max_size']), DEFAULT_SETTINGS['use_max_size'])
        self.txtRangeBottom.Text = to_text(s.get('view_range_bottom_mm', DEFAULT_SETTINGS['view_range_bottom_mm']))
        self.txtRangeTop.Text = to_text(s.get('view_range_top_mm', DEFAULT_SETTINGS['view_range_top_mm']))
        self.txtDeviceOffset.Text = to_text(s.get('device_offset_mm', DEFAULT_SETTINGS['device_offset_mm']))
        self.chkDeviceLeader.IsChecked = setting_to_bool(
            s.get('device_leader', DEFAULT_SETTINGS['device_leader']), DEFAULT_SETTINGS['device_leader'])

        self.set_radio_value({'view': self.rbView, 'selection': self.rbSelection},
                             s.get('scope', 'selection'), 'selection')
        self.set_radio_value({'all': self.rbOrientationAll,
                              'horizontal': self.rbOrientationHorizontal,
                              'vertical': self.rbOrientationVertical},
                             s.get('orientation', 'all'), 'all')
        self.set_radio_value({'all': self.rbAllPipes, 'untagged': self.rbUntaggedOnly},
                             s.get('tag_condition', 'all'), 'all')
        self.set_radio_value({'create': self.rbOperationCreate,
                              'align': self.rbOperationAlign,
                              'normalize': self.rbOperationNormalize},
                             s.get('operation_mode', 'create'), 'create')
        self.set_radio_value({'left': self.rbTagSideLeft, 'right': self.rbTagSideRight},
                             s.get('tag_side', 'left'), 'left')
        self.set_radio_value({'none': self.rbViewRangeNone,
                              'manual': self.rbViewRangeManual,
                              'active': self.rbViewRangeActive},
                             s.get('view_range_mode', 'none'), 'none')

        for kind in KIND_ORDER:
            chk, cb, use_key, type_key = self.kind_controls[kind]
            chk.IsChecked = setting_to_bool(s.get(use_key, DEFAULT_SETTINGS[use_key]), DEFAULT_SETTINGS[use_key])
            saved_tag = s.get(type_key, '')
            tag_map = self.tag_maps.get(kind, {})
            if saved_tag and saved_tag in tag_map:
                cb.SelectedItem = saved_tag
            elif tag_map and cb.SelectedIndex < 0:
                cb.SelectedIndex = 0

        if os.path.exists(CONFIG_FILE) or os.path.exists(FALLBACK_CONFIG_FILE):
            self.set_status(u'Đã nạp cấu hình lần chạy trước.')
        else:
            self.set_status(u'Chưa có cấu hình đã lưu. Tool sẽ tự lưu sau khi bấm Thực hiện.')

    # ------------------------------------------------------------------
    def read_radio_options(self):
        scope = 'selection' if bool(self.rbSelection.IsChecked) else 'view'
        if bool(self.rbOrientationHorizontal.IsChecked):
            orientation = 'horizontal'
        elif bool(self.rbOrientationVertical.IsChecked):
            orientation = 'vertical'
        else:
            orientation = 'all'
        tag_condition = 'untagged' if bool(self.rbUntaggedOnly.IsChecked) else 'all'
        if bool(self.rbOperationAlign.IsChecked):
            operation_mode = 'align'
        elif bool(self.rbOperationNormalize.IsChecked):
            operation_mode = 'normalize'
        else:
            operation_mode = 'create'
        tag_side = 'right' if bool(self.rbTagSideRight.IsChecked) else 'left'
        if bool(self.rbViewRangeManual.IsChecked):
            view_range_mode = 'manual'
        elif bool(self.rbViewRangeActive.IsChecked):
            view_range_mode = 'active'
        else:
            view_range_mode = 'none'
        return scope, orientation, tag_condition, operation_mode, tag_side, view_range_mode

    def get_current_settings(self):
        scope, orientation, tag_condition, operation_mode, tag_side, view_range_mode = \
            self.read_radio_options()
        data = {
            'spacing_mm': to_text(self.txtSpacing.Text),
            'min_length_mm': to_text(self.txtMinLength.Text),
            'max_length_mm': to_text(self.txtMaxLength.Text),
            'use_max_length': bool(self.chkLimitMaxLength.IsChecked),
            'min_size_mm': to_text(self.txtMinSize.Text),
            'max_size_mm': to_text(self.txtMaxSize.Text),
            'use_max_size': bool(self.chkLimitMaxSize.IsChecked),
            'scope': scope,
            'orientation': orientation,
            'tag_condition': tag_condition,
            'operation_mode': operation_mode,
            'tag_side': tag_side,
            'view_range_mode': view_range_mode,
            'view_range_bottom_mm': to_text(self.txtRangeBottom.Text),
            'view_range_top_mm': to_text(self.txtRangeTop.Text),
            'device_offset_mm': to_text(self.txtDeviceOffset.Text),
            'device_leader': bool(self.chkDeviceLeader.IsChecked),
        }
        for kind in KIND_ORDER:
            chk, cb, use_key, type_key = self.kind_controls[kind]
            data[use_key] = bool(chk.IsChecked)
            data[type_key] = to_text(cb.SelectedItem) if cb.SelectedItem else u''
        return data

    # ------------------------------------------------------------------
    def on_execute_click(self, sender, args):
        """Chỉ đọc/kiểm tra UI rồi đóng WPF. Không Pick/Transaction trong event WPF."""
        try:
            scope, orientation, tag_condition, operation_mode, tag_side, view_range_mode = \
                self.read_radio_options()

            # --- Đối tượng cần tag ---
            selected_kinds = [k for k in KIND_ORDER if bool(self.kind_controls[k][0].IsChecked)]
            if not selected_kinds:
                alert_msg(u'Vui lòng tick ít nhất một loại đối tượng cần tag.')
                return

            tag_type_ids = {}
            for kind in selected_kinds:
                cb = self.kind_controls[kind][1]
                name = cb.SelectedItem
                tag_map = self.tag_maps.get(kind, {})
                if name and name in tag_map:
                    tag_type_ids[kind] = tag_map[name].Id
                elif operation_mode in ('create', 'normalize'):
                    alert_msg(u'Chưa có/chưa chọn Tag Type cho {}. Hãy load family tag hoặc bỏ tick đối tượng này.'.format(
                        KIND_INFO[kind]['label']))
                    return

            has_linear = any(KIND_INFO[k]['linear'] for k in selected_kinds)
            has_device = any(not KIND_INFO[k]['linear'] for k in selected_kinds)

            # --- Thông số Pipe/Conduit ---
            spacing_mm = parse_mm_value(self.txtSpacing.Text, u'Khoảng cách xếp bậc')
            min_length_mm = parse_mm_value(self.txtMinLength.Text, u'Chiều dài tối thiểu')
            use_max_length = bool(self.chkLimitMaxLength.IsChecked)
            max_length_mm = parse_mm_value(self.txtMaxLength.Text, u'Chiều dài tối đa') if use_max_length else 0.0
            min_size_mm = parse_mm_value(self.txtMinSize.Text, u'Đường kính nhỏ nhất')
            use_max_size = bool(self.chkLimitMaxSize.IsChecked)
            max_size_mm = parse_mm_value(self.txtMaxSize.Text, u'Đường kính lớn nhất') if use_max_size else 0.0

            if has_linear:
                if spacing_mm <= 0:
                    alert_msg(u'Khoảng cách xếp bậc phải lớn hơn 0.')
                    return
                if min_length_mm < 0:
                    alert_msg(u'Chiều dài tối thiểu không được nhỏ hơn 0.')
                    return
                if use_max_length and (max_length_mm < 0 or min_length_mm > max_length_mm):
                    alert_msg(u'Chiều dài tối đa phải ≥ 0 và không nhỏ hơn chiều dài tối thiểu.')
                    return
                if min_size_mm < 0:
                    alert_msg(u'Đường kính nhỏ nhất không được nhỏ hơn 0.')
                    return
                if use_max_size and (max_size_mm < 0 or min_size_mm > max_size_mm):
                    alert_msg(u'Đường kính lớn nhất phải ≥ 0 và không nhỏ hơn đường kính nhỏ nhất.')
                    return

            # --- Thông số Fire Alarm Device ---
            device_offset_mm = 0.0
            device_leader = bool(self.chkDeviceLeader.IsChecked)
            if has_device:
                device_offset_mm = parse_mm_value(self.txtDeviceOffset.Text, u'Khoảng cách tag thiết bị')
                if device_offset_mm < 0:
                    alert_msg(u'Khoảng cách tag thiết bị không được nhỏ hơn 0.')
                    return

            # --- View Range ---
            if view_range_mode == 'manual':
                range_bottom_mm = parse_mm_value(self.txtRangeBottom.Text, u'Cao độ dưới View Range')
                range_top_mm = parse_mm_value(self.txtRangeTop.Text, u'Cao độ trên View Range')
                if range_top_mm <= range_bottom_mm:
                    alert_msg(u'Cao độ trên View Range phải lớn hơn cao độ dưới.')
                    return
            else:
                range_bottom_mm = 0.0
                range_top_mm = 0.0

            if save_settings(self.get_current_settings()):
                self.set_status(u'Đã lưu cấu hình hiện tại.')

            self.result = {
                'kinds': selected_kinds,
                'tag_type_ids': tag_type_ids,
                'spacing_mm': spacing_mm,
                'min_length_mm': min_length_mm,
                'max_length_mm': max_length_mm,
                'use_max_length': use_max_length,
                'min_size_mm': min_size_mm,
                'max_size_mm': max_size_mm,
                'use_max_size': use_max_size,
                'device_offset_mm': device_offset_mm,
                'device_leader': device_leader,
                'scope': scope,
                'orientation': orientation,
                'tag_condition': tag_condition,
                'operation_mode': operation_mode,
                'tag_side': tag_side,
                'view_range_mode': view_range_mode,
                'view_range_bottom_mm': range_bottom_mm,
                'view_range_top_mm': range_top_mm,
            }
            self.Close()

        except ValueError as ex:
            alert_msg(ensure_unicode(ex))
        except Exception as ex:
            self.result = None
            alert_msg(u'Lỗi giao diện: {}'.format(ensure_unicode(ex)))
            log(traceback.format_exc())

    def on_close_click(self, sender, args):
        self.result = None
        self.Close()


# -----------------------------------------------------------------------------
# COMMAND LOGIC - CHỈ CHẠY SAU KHI WPF ĐÃ ĐÓNG
# -----------------------------------------------------------------------------
def get_revit_year(doc):
    try:
        return int(doc.Application.VersionNumber)
    except:
        return 2022


def deduplicate_targets(elements, allowed_kinds):
    result = []
    seen_ids = set()
    for elem in elements or []:
        try:
            if not is_valid_target(elem, allowed_kinds):
                continue
            eid = get_id_value(elem.Id)
            if eid is None or eid in seen_ids:
                continue
            seen_ids.add(eid)
            result.append(elem)
        except:
            continue
    return result


def get_tag_host_ids(doc, tag):
    """Trả về list ElementId host local của tag (Revit cũ/mới)."""
    ids = []
    if get_revit_year(doc) >= 2022:
        try:
            for tagged_ref in tag.GetTaggedReferences():
                ids.append(tagged_ref.ElementId)
        except:
            pass
    else:
        try:
            ids.append(tag.TaggedLocalElementId)
        except:
            pass
    return ids


def get_hosts_from_tag(doc, tag, allowed_kinds):
    """Resolve host từ Pipe/Conduit/Fire Alarm Device Tag. Host phải cùng loại với tag."""
    result = []
    if not tag or not isinstance(tag, IndependentTag):
        return result
    tag_kind = get_tag_kind(tag)
    if tag_kind is None or tag_kind not in allowed_kinds:
        return result
    for host_id in get_tag_host_ids(doc, tag):
        try:
            elem = doc.GetElement(host_id)
            if get_element_kind(elem) == tag_kind:
                result.append(elem)
        except:
            pass
    return result


def get_preselected_targets(doc, uidoc, active_view, allowed_kinds):
    elements = []
    try:
        selected_ids = list(uidoc.Selection.GetElementIds())
    except:
        selected_ids = []
    active_view_id = get_id_value(active_view.Id) if active_view else None
    for elem_id in selected_ids:
        try:
            elem = doc.GetElement(elem_id)
        except:
            elem = None
        if not elem:
            continue
        if get_element_kind(elem) in allowed_kinds:
            elements.append(elem)
            continue
        if isinstance(elem, IndependentTag):
            try:
                if active_view_id is not None and get_id_value(elem.OwnerViewId) != active_view_id:
                    continue
            except:
                pass
            elements.extend(get_hosts_from_tag(doc, elem, allowed_kinds))
    return deduplicate_targets(elements, allowed_kinds)


def resolve_view_range_bounds(doc, active_view, options):
    mode = options['view_range_mode']
    if mode == 'none':
        return None
    if mode == 'active':
        bounds = get_active_view_range_bounds(doc, active_view)
        if not bounds:
            alert_msg(u'Active view hiện tại không hỗ trợ View Range. Hãy chọn Plan View hoặc dùng chế độ nhập View Range thủ công.')
            return False
        return bounds
    base_z = get_view_base_elevation(active_view)
    bottom_z = base_z + options['view_range_bottom_mm'] / 304.8
    top_z = base_z + options['view_range_top_mm'] / 304.8
    return (min(bottom_z, top_z), max(bottom_z, top_z))


def kinds_label(kinds):
    return u' / '.join([KIND_INFO[k]['label'] for k in kinds])


def collect_input_targets(doc, uidoc, active_view, options, view_range_bounds):
    kinds = options['kinds']
    if options['scope'] == 'view':
        elements = []
        for kind in kinds:
            try:
                elements.extend(
                    FilteredElementCollector(doc, active_view.Id)
                    .OfCategory(KIND_INFO[kind]['cat'])
                    .WhereElementIsNotElementType()
                    .ToElements())
            except:
                pass
        return deduplicate_targets(elements, kinds), False

    preselected = get_preselected_targets(doc, uidoc, active_view, kinds)
    if preselected:
        return preselected, True

    label = kinds_label(kinds)
    mode = options['view_range_mode']
    if mode == 'manual':
        pick_message = u'Chưa chọn trước. Chỉ chọn được {} nằm trong View Range thủ công -> Nhấn FINISH.'.format(label)
    elif mode == 'active':
        pick_message = u'Chưa chọn trước. Chỉ chọn được {} nằm trong View Range hiện hành -> Nhấn FINISH.'.format(label)
    else:
        pick_message = u'Chưa chọn trước. Quét/click chọn {} cần đặt Tag -> Nhấn FINISH.'.format(label)

    selected_refs = uidoc.Selection.PickObjects(
        ObjectType.Element,
        TagTargetSelectionFilter(kinds, view_range_bounds),
        pick_message)
    elements = []
    for ref in selected_refs:
        try:
            elements.append(doc.GetElement(ref.ElementId))
        except:
            pass
    return deduplicate_targets(elements, kinds), False


def get_tagged_dict(doc, active_view, kinds):
    """{host_id_value: [tag ElementId,...]} - chỉ tính tag đúng category của host."""
    existing_tags = FilteredElementCollector(doc, active_view.Id)\
        .OfClass(IndependentTag)\
        .ToElements()
    tagged_dict = {}
    for tag in existing_tags:
        try:
            tag_kind = get_tag_kind(tag)
            if tag_kind is None or tag_kind not in kinds:
                continue
            for host_id in get_tag_host_ids(doc, tag):
                hid = get_id_value(host_id)
                if hid is None:
                    continue
                host = doc.GetElement(host_id)
                if get_element_kind(host) != tag_kind:
                    continue
                tagged_dict.setdefault(hid, []).append(tag.Id)
        except:
            pass
    return tagged_dict


def get_size_ft(elem, kind):
    bip = KIND_INFO[kind]['size_bip']
    if bip is None:
        return None
    try:
        p = elem.get_Parameter(bip)
        if p and p.HasValue:
            return p.AsDouble()
    except:
        pass
    try:
        return elem.Diameter
    except:
        return None


def filter_targets(raw_elements, options, view_range_bounds, tagged_dict):
    """Trả về (linear_elements, device_elements, view_range_skipped)."""
    min_length_ft = options['min_length_mm'] / 304.8
    use_max_length = options.get('use_max_length', False)
    max_length_ft = options['max_length_mm'] / 304.8 if use_max_length else None
    min_size_ft = options['min_size_mm'] / 304.8
    use_max_size = options.get('use_max_size', True)
    max_size_ft = options['max_size_mm'] / 304.8 if use_max_size else None
    operation_mode = options.get('operation_mode', 'create')

    linear_elements = []
    device_elements = []
    view_range_skipped = 0

    for elem in deduplicate_targets(raw_elements, options['kinds']):
        try:
            kind = get_element_kind(elem)

            if view_range_bounds and not element_overlaps_z_range(
                    elem, view_range_bounds[0], view_range_bounds[1]):
                view_range_skipped += 1
                continue

            eid = get_id_value(elem.Id)
            if operation_mode == 'create':
                if options['tag_condition'] == 'untagged' and eid in tagged_dict:
                    continue
            else:
                if eid not in tagged_dict:
                    continue

            # Thiết bị dạng điểm (FA / Plumbing Fixture / Pipe Accessory): không lọc hướng/chiều dài/kích thước
            if not KIND_INFO[kind]['linear']:
                device_elements.append(elem)
                continue

            curve = elem.Location.Curve
            vector = curve.GetEndPoint(1) - curve.GetEndPoint(0)
            if vector.GetLength() <= 0.000001:
                continue
            is_vertical = abs(vector.Normalize().Z) > 0.99
            if options['orientation'] == 'horizontal' and is_vertical:
                continue
            if options['orientation'] == 'vertical' and not is_vertical:
                continue

            length_param = elem.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH)
            if length_param:
                length_ft = length_param.AsDouble()
                if length_ft < min_length_ft:
                    continue
                if use_max_length and length_ft > max_length_ft:
                    continue

            size_ft = get_size_ft(elem, kind)
            if size_ft is not None:
                if size_ft < min_size_ft:
                    continue
                if use_max_size and size_ft > max_size_ft:
                    continue

            linear_elements.append(elem)
        except Exception as ex:
            log(u'Bỏ qua ID {} khi lọc: {}'.format(elem.Id, ensure_unicode(ex)))

    return linear_elements, device_elements, view_range_skipped


def build_parallel_clusters(linear_elements):
    """Gom cụm Pipe + Conduit song song gần nhau để xếp bậc tag không chồng nhau."""
    clusters = []
    unclustered = list(linear_elements)
    while unclustered:
        current = unclustered.pop(0)
        cluster = [current]
        remaining = []
        for elem in unclustered:
            try:
                if are_curves_parallel(current, elem) and get_perpendicular_distance(current, elem) < 5.0:
                    cluster.append(elem)
                else:
                    remaining.append(elem)
            except:
                remaining.append(elem)
        unclustered = remaining
        clusters.append(cluster)
    return clusters


def get_cluster_layout(active_view, options, cluster):
    view_scale = active_view.Scale
    if view_scale <= 0:
        raise Exception(u'Active view không có tỷ lệ hợp lệ để tính khoảng cách tag.')
    view_right = active_view.RightDirection
    view_up = active_view.UpDirection
    tag_side_left = options['tag_side'] == 'left'

    first_curve = cluster[0].Location.Curve
    direction_vector = first_curve.GetEndPoint(1) - first_curve.GetEndPoint(0)
    if direction_vector.GetLength() <= 0.000001:
        raise Exception(u'Đối tượng trong cụm không có hướng hợp lệ.')
    direction = direction_vector.Normalize()

    if abs(direction.DotProduct(view_right)) > 0.707:
        dir_perp = view_up
        dir_para = reverse_xyz(view_right) if tag_side_left else view_right
        sorted_cluster = sorted(cluster, key=lambda e: e.Location.Curve.Evaluate(0.5, True).DotProduct(view_up))
    else:
        dir_perp = reverse_xyz(view_right) if tag_side_left else view_right
        dir_para = view_up
        sorted_cluster = sorted(cluster, key=lambda e: e.Location.Curve.Evaluate(0.5, True).DotProduct(view_right))

    model_first_offset = mm_to_feet(4.0, view_scale)
    model_spacing = mm_to_feet(options['spacing_mm'], view_scale)
    return sorted_cluster, dir_perp, dir_para, model_first_offset, model_spacing


def get_linear_tag_head(elem, index, dir_perp, dir_para, model_first_offset, model_spacing):
    mid_point = elem.Location.Curve.Evaluate(0.5, True)
    return mid_point + dir_perp * model_first_offset + dir_para * (index * model_spacing)


def get_device_tag_head(elem, active_view, options):
    view_scale = active_view.Scale
    if view_scale <= 0:
        raise Exception(u'Active view không có tỷ lệ hợp lệ để tính khoảng cách tag.')
    pt = get_element_point(elem)
    if pt is None:
        raise Exception(u'Không xác định được vị trí thiết bị.')
    view_right = active_view.RightDirection
    side = reverse_xyz(view_right) if options['tag_side'] == 'left' else view_right
    return pt + side * mm_to_feet(options['device_offset_mm'], view_scale)


def build_tag_jobs(active_view, options, clusters, devices):
    """Danh sách (element, kind, head_pos) dùng chung cho Create / Align / Normalize."""
    jobs = []
    skipped = 0
    for cluster in clusters:
        if not cluster:
            continue
        try:
            sorted_cluster, dir_perp, dir_para, first_off, spacing = \
                get_cluster_layout(active_view, options, cluster)
        except Exception as ex:
            skipped += len(cluster)
            log(u'Lỗi xử lý cụm: {}'.format(ensure_unicode(ex)))
            continue
        for index, elem in enumerate(sorted_cluster):
            try:
                head = get_linear_tag_head(elem, index, dir_perp, dir_para, first_off, spacing)
                jobs.append((elem, get_element_kind(elem), head))
            except Exception as ex:
                skipped += 1
                log(u'Lỗi tính vị trí tag ID {}: {}'.format(elem.Id, ensure_unicode(ex)))
    for elem in devices:
        try:
            jobs.append((elem, get_element_kind(elem), get_device_tag_head(elem, active_view, options)))
        except Exception as ex:
            skipped += 1
            log(u'Lỗi tính vị trí tag thiết bị ID {}: {}'.format(elem.Id, ensure_unicode(ex)))
    return jobs, skipped


def kind_wants_leader(kind, options):
    if KIND_INFO[kind]['linear']:
        return True
    return bool(options.get('device_leader', False))


def apply_leader(tag, want_leader):
    try:
        tag.HasLeader = want_leader
    except:
        pass
    if want_leader:
        try:
            tag.LeaderEndCondition = LeaderEndCondition.Attached
        except:
            pass


def get_tag_symbols(doc, options, kinds_needed):
    symbols = {}
    for kind in kinds_needed:
        type_id = options['tag_type_ids'].get(kind)
        sym = doc.GetElement(type_id) if type_id else None
        if not sym:
            raise Exception(u'Không tìm thấy Tag Type đã chọn cho {}.'.format(KIND_INFO[kind]['label']))
        symbols[kind] = sym
    return symbols


def activate_symbols(doc, symbols):
    changed = False
    for sym in symbols.values():
        if not sym.IsActive:
            sym.Activate()
            changed = True
    if changed:
        doc.Regenerate()


def new_counter():
    return dict((k, 0) for k in KIND_ORDER)


def create_tags(doc, active_view, options, jobs, tagged_dict):
    kinds_needed = sorted(set([j[1] for j in jobs]))
    symbols = get_tag_symbols(doc, options, kinds_needed)
    tag_all = options['tag_condition'] == 'all'

    created = new_counter()
    deleted_count = 0
    skipped_count = 0
    deleted_keys = set()

    with revit.Transaction('Tag MEP Pro - Create'):
        activate_symbols(doc, symbols)
        for elem, kind, head_pos in jobs:
            try:
                eid = get_id_value(elem.Id)
                if tag_all and eid in tagged_dict:
                    for old_tag_id in tagged_dict[eid]:
                        key = get_id_value(old_tag_id)
                        if key in deleted_keys:
                            continue
                        try:
                            doc.Delete(old_tag_id)
                            deleted_keys.add(key)
                            deleted_count += 1
                        except:
                            pass

                want_leader = kind_wants_leader(kind, options)
                new_tag = IndependentTag.Create(
                    doc,
                    symbols[kind].Id,
                    active_view.Id,
                    Reference(elem),
                    want_leader,
                    TagOrientation.Horizontal,
                    head_pos)
                apply_leader(new_tag, want_leader)
                # Đảm bảo TagHead đúng vị trí tính toán (thiết bị không leader).
                try:
                    new_tag.TagHeadPosition = head_pos
                except:
                    pass
                created[kind] += 1
            except Exception as ex:
                skipped_count += 1
                log(u'Lỗi tại {} ID {}: {}'.format(KIND_INFO[kind]['label'], elem.Id, ensure_unicode(ex)))

    return {'created': created, 'deleted': deleted_count, 'aligned': new_counter(),
            'type_changed': 0, 'skipped': skipped_count, 'duplicate_tags': 0}


def process_existing_tags(doc, active_view, options, jobs, tagged_dict, change_type=False):
    """Align hoặc Đổi Type + Align tag hiện có. Không xóa/tạo lại tag."""
    symbols = {}
    if change_type:
        kinds_needed = sorted(set([j[1] for j in jobs]))
        symbols = get_tag_symbols(doc, options, kinds_needed)

    aligned = new_counter()
    type_changed_count = 0
    skipped_count = 0
    duplicate_tags = 0

    name = 'Tag MEP Pro - Normalize' if change_type else 'Tag MEP Pro - Align'
    with revit.Transaction(name):
        if change_type:
            activate_symbols(doc, symbols)
        for elem, kind, head_pos in jobs:
            tag_ids = list(tagged_dict.get(get_id_value(elem.Id), []))
            if not tag_ids:
                continue
            if len(tag_ids) > 1:
                duplicate_tags += len(tag_ids) - 1
            for tag_id in tag_ids:
                subtrans = None
                type_changed_this = False
                try:
                    tag = doc.GetElement(tag_id)
                    if not tag or not isinstance(tag, IndependentTag):
                        skipped_count += 1
                        continue
                    if change_type:
                        subtrans = SubTransaction(doc)
                        subtrans.Start()
                        sym = symbols[kind]
                        if get_id_value(tag.GetTypeId()) != get_id_value(sym.Id):
                            tag.ChangeTypeId(sym.Id)
                            type_changed_this = True
                        try:
                            tag.TagOrientation = TagOrientation.Horizontal
                        except:
                            pass
                        apply_leader(tag, kind_wants_leader(kind, options))
                    tag.TagHeadPosition = head_pos
                    if subtrans:
                        subtrans.Commit()
                    aligned[kind] += 1
                    if type_changed_this:
                        type_changed_count += 1
                except Exception as ex:
                    if subtrans:
                        try:
                            subtrans.RollBack()
                        except:
                            pass
                    skipped_count += 1
                    log(u'Lỗi xử lý Tag ID {} của {} ID {}: {}'.format(
                        tag_id, KIND_INFO[kind]['label'], elem.Id, ensure_unicode(ex)))

    return {'created': new_counter(), 'deleted': 0, 'aligned': aligned,
            'type_changed': type_changed_count, 'skipped': skipped_count,
            'duplicate_tags': duplicate_tags}


def format_breakdown(counter):
    parts = [u'{}: {}'.format(KIND_INFO[k]['label'], counter[k]) for k in KIND_ORDER if counter.get(k)]
    return u' ({})'.format(u', '.join(parts)) if parts else u''


def show_result(options, result, view_range_skipped, used_preselection):
    lines = []
    mode = options.get('operation_mode', 'create')
    if mode == 'create':
        if options['tag_condition'] == 'all' and result['deleted'] > 0:
            lines.append(u'Đã xóa {} tag cũ.'.format(result['deleted']))
        total = sum(result['created'].values())
        lines.append(u'Đã tạo mới {} tag{}.'.format(total, format_breakdown(result['created'])))
    elif mode == 'align':
        total = sum(result['aligned'].values())
        lines.append(u'Đã align {} tag hiện có{}.'.format(total, format_breakdown(result['aligned'])))
        lines.append(u'Không đổi Tag Type, leader hoặc orientation.')
    else:
        total = sum(result['aligned'].values())
        lines.append(u'Đã đưa {} tag về vị trí cấu hình{}.'.format(total, format_breakdown(result['aligned'])))
        lines.append(u'Đã đổi type cho {} tag.'.format(result['type_changed']))
        lines.append(u'Đã áp dụng Horizontal + Leader theo cấu hình.')
    if result['duplicate_tags'] > 0:
        lines.append(u'Phát hiện {} tag bổ sung trên các đối tượng có nhiều tag; các tag này cũng được xử lý.'.format(result['duplicate_tags']))
    if result['skipped'] > 0:
        lines.append(u'Bỏ qua {} tag/đối tượng do lỗi (xem cửa sổ pyRevit).'.format(result['skipped']))
    if view_range_skipped > 0:
        lines.append(u'Bỏ qua {} đối tượng nằm ngoài View Range.'.format(view_range_skipped))
    if used_preselection:
        lines.append(u'Đã dùng trực tiếp các đối tượng được chọn trước khi mở lệnh.')
    toast_msg(u'\n'.join(lines))


def run_tool():
    doc = revit.doc
    uidoc = revit.uidoc
    active_view = doc.ActiveView

    win = TagMEPWPF(xaml_file)
    win.show_dialog()
    options = win.result
    if not options:
        return

    view_range_bounds = resolve_view_range_bounds(doc, active_view, options)
    if view_range_bounds is False:
        return

    try:
        raw_elements, used_preselection = collect_input_targets(
            doc, uidoc, active_view, options, view_range_bounds)
    except OperationCanceledException:
        return

    label = kinds_label(options['kinds'])
    if not raw_elements:
        alert_msg(u'Không có {} nào trong phạm vi xử lý.'.format(label))
        return

    try:
        tagged_dict = get_tagged_dict(doc, active_view, options['kinds'])
        linear_elements, devices, view_range_skipped = filter_targets(
            raw_elements, options, view_range_bounds, tagged_dict)

        if not linear_elements and not devices:
            mode = options.get('operation_mode', 'create')
            if mode in ('align', 'normalize'):
                alert_msg(u'Không tìm thấy {} đã có tag trong active view và thỏa mãn các điều kiện lọc.'.format(label))
            elif view_range_bounds and view_range_skipped > 0:
                alert_msg(u'Không có đối tượng thỏa mãn điều kiện lọc. {} đối tượng bị bỏ qua vì nằm ngoài View Range.'.format(view_range_skipped))
            else:
                alert_msg(u'Không tìm thấy {} nào thỏa mãn các điều kiện lọc.'.format(label))
            return

        clusters = build_parallel_clusters(linear_elements)
        jobs, layout_skipped = build_tag_jobs(active_view, options, clusters, devices)

        mode = options.get('operation_mode', 'create')
        if mode == 'align':
            result = process_existing_tags(doc, active_view, options, jobs, tagged_dict, False)
        elif mode == 'normalize':
            result = process_existing_tags(doc, active_view, options, jobs, tagged_dict, True)
        else:
            result = create_tags(doc, active_view, options, jobs, tagged_dict)

        result['skipped'] += layout_skipped
        show_result(options, result, view_range_skipped, used_preselection)

    except Exception as ex:
        alert_msg(u'Lỗi hệ thống: {}'.format(ensure_unicode(ex)))
        log(traceback.format_exc())


if __name__ == '__main__':
    run_tool()
