# -*- coding: utf-8 -*-
from __future__ import unicode_literals
__title__ = u'Tag Without Leader'
__doc__ = u'Chọn loại Pipe Tag, lọc chiều dài Min/Max, kích thước Min/Max, hướng ống, View Range và tự lưu cấu hình lần chạy trước.'

from Autodesk.Revit.DB import *
from Autodesk.Revit.DB.Plumbing import Pipe
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
# Lưu ý quan trọng:
# - Một số bản pyRevit bị lỗi mã hóa khi dùng forms.toast/forms.alert với tiếng Việt.
# - Dấu tiếng Việt bị biến thành dạng mojibake như: "ÄÃ£...".
# - Đây không phải lỗi font của Windows, mà là lỗi đường truyền encoding của pyRevit UI.
# - Cách ổn định nhất là KHÔNG dùng forms.toast/forms.alert cho tiếng Việt;
#   chuyển toàn bộ thông báo sang Revit native TaskDialog.
try:
    unicode
except NameError:
    unicode = str


TASK_DIALOG_TITLE = u'Tag Pipes Pro'


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
    """Hiển thị thông báo Unicode bằng TaskDialog của Revit.

    Không dùng pyrevit.forms.toast/forms.alert vì một số máy sẽ lỗi dấu tiếng Việt.
    """
    msg = ensure_unicode(message)
    ttl = ensure_unicode(title)
    try:
        TaskDialog.Show(ttl, msg)
    except:
        try:
            # Fallback cuối cùng: in ra pyRevit output.
            print(msg.encode('utf-8') if hasattr(msg, 'encode') else msg)
        except:
            pass


def alert_msg(message):
    revit_message(message)


def toast_msg(message):
    # Giữ tên hàm để không phải đổi logic bên dưới, nhưng thực tế dùng TaskDialog.
    revit_message(message)


# -----------------------------------------------------------------------------
# FILE PATHS
# -----------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(__file__)
xaml_file = os.path.join(SCRIPT_DIR, 'ui_definition.xaml')
CONFIG_FILENAME = 'tag_parallel_pipes_settings.json'
CONFIG_FILE = os.path.join(SCRIPT_DIR, CONFIG_FILENAME)


def get_fallback_config_file():
    """Fallback lưu setting vào AppData nếu thư mục bundle không cho ghi file."""
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
    'use_max_length': False,     # False = không giới hạn chiều dài tối đa
    'min_size_mm': '15',
    'max_size_mm': '1000',
    'use_max_size': True,        # False = không giới hạn đường kính lớn nhất
    'scope': 'selection',        # selection | view
    'orientation': 'all',        # all | horizontal | vertical
    'tag_condition': 'all',      # all | untagged
    'tag_side': 'left',           # left | right
    'view_range_mode': 'none',    # none | manual | active
    'view_range_bottom_mm': '-1000',
    'view_range_top_mm': '3000',
    'tag_type': ''
}


def to_text(value):
    """Chuyển giá trị sang Unicode text, tương thích IronPython/Python."""
    return ensure_unicode(value)


def setting_to_bool(value, default=False):
    """Đọc giá trị bool từ JSON cũ/mới một cách an toàn."""
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
    """Ưu tiên lưu cùng thư mục script. Nếu không được thì lưu vào AppData."""
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
# HELPERS
# -----------------------------------------------------------------------------
def mm_to_feet(mm, scale):
    return (mm * scale) / 304.8


def reverse_xyz(vec):
    """Đảo chiều vector, tương thích nhiều phiên bản Revit/IronPython."""
    try:
        return vec.Negate()
    except:
        return vec * -1.0


def are_pipes_parallel(p1, p2):
    d1 = (p1.Location.Curve.GetEndPoint(1) - p1.Location.Curve.GetEndPoint(0)).Normalize()
    d2 = (p2.Location.Curve.GetEndPoint(1) - p2.Location.Curve.GetEndPoint(0)).Normalize()
    return abs(d1.DotProduct(d2)) > 0.95


def get_perpendicular_distance(p1, p2):
    pt1 = p1.Location.Curve.Evaluate(0.5, True)
    pt2 = p2.Location.Curve.Evaluate(0.5, True)
    d1 = (p1.Location.Curve.GetEndPoint(1) - p1.Location.Curve.GetEndPoint(0)).Normalize()
    v = pt2 - pt1
    return (v - d1 * v.DotProduct(d1)).GetLength()


def parse_mm_value(text_value, field_name):
    """Đọc số mm từ TextBox, cho phép nhập dấu phẩy hoặc dấu chấm."""
    try:
        return float(to_text(text_value).replace(',', '.'))
    except:
        raise ValueError(u'{} phải là số hợp lệ.'.format(field_name))


def get_view_base_elevation(view):
    """Cao độ Level gắn với active view. Nếu không lấy được thì dùng 0."""
    try:
        level = view.GenLevel
        if level:
            return level.Elevation
    except:
        pass
    return 0.0


def is_valid_level_id(level_id):
    try:
        return level_id and level_id.IntegerValue != -1
    except:
        return False


def get_plan_view_plane_z(doc, view, plan_view_range, plane):
    """Trả về cao độ tuyệt đối của một mặt phẳng View Range."""
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
    """Lấy khoảng Z của View Range hiện hành trong active view.

    Dùng Top Clip làm giới hạn trên. Giới hạn dưới lấy thấp nhất giữa
    Bottom Clip và View Depth để không bỏ sót các phần tử đang được view hiển thị.
    """
    try:
        pvr = view.GetViewRange()
    except:
        return None

    top_z = get_plan_view_plane_z(doc, view, pvr, PlanViewPlane.TopClipPlane)
    bottom_z = get_plan_view_plane_z(doc, view, pvr, PlanViewPlane.BottomClipPlane)
    view_depth_z = get_plan_view_plane_z(doc, view, pvr, PlanViewPlane.ViewDepthPlane)

    values = []
    if top_z is not None:
        values.append(top_z)
    if bottom_z is not None:
        values.append(bottom_z)
    if view_depth_z is not None:
        values.append(view_depth_z)

    if len(values) < 2:
        return None

    upper = top_z if top_z is not None else max(values)
    lower_candidates = []
    if bottom_z is not None:
        lower_candidates.append(bottom_z)
    if view_depth_z is not None:
        lower_candidates.append(view_depth_z)
    lower = min(lower_candidates) if lower_candidates else min(values)

    if upper < lower:
        tmp = upper
        upper = lower
        lower = tmp

    return (lower, upper)


def get_element_z_span(elem):
    """Lấy khoảng cao độ Z của element để so với View Range."""
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
        return None


def element_overlaps_z_range(elem, bottom_z, top_z):
    """True nếu element có phần giao với dải View Range."""
    span = get_element_z_span(elem)
    if not span:
        return False

    elem_min_z, elem_max_z = span
    return elem_max_z >= bottom_z and elem_min_z <= top_z


# BỘ LỌC CHỈ CHO PHÉP CHỌN ỐNG KHI THAO TÁC TRÊN MÀN HÌNH
class PipeSelectionFilter(ISelectionFilter):
    def __init__(self, view_range_bounds=None):
        # view_range_bounds = (bottom_z, top_z) theo đơn vị internal feet của Revit.
        # Khi có giá trị này, user chỉ quét/chọn được pipe có giao với dải View Range.
        self.view_range_bounds = view_range_bounds

    def AllowElement(self, elem):
        if not (elem and elem.Category and elem.Category.Id.IntegerValue == int(BuiltInCategory.OST_PipeCurves)):
            return False

        if self.view_range_bounds:
            try:
                return element_overlaps_z_range(elem, self.view_range_bounds[0], self.view_range_bounds[1])
            except:
                return False

        return True

    def AllowReference(self, reference, position):
        return False


class TagParallelPipesWPF(forms.WPFWindow):
    def __init__(self, xaml_file_path):
        forms.WPFWindow.__init__(self, xaml_file_path)

        self.result = None
        self.settings = load_settings()
        self.setup_tag_combobox()
        self.apply_saved_settings()

        self.btnExecute.Click += self.on_execute_click
        self.btnCancel.Click += self.on_close_click

    # -------------------------------------------------------------------------
    # UI SETTINGS
    # -------------------------------------------------------------------------
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

    def apply_saved_settings(self):
        self.txtSpacing.Text = to_text(self.settings.get('spacing_mm', DEFAULT_SETTINGS['spacing_mm']))
        self.txtMinLength.Text = to_text(self.settings.get('min_length_mm', DEFAULT_SETTINGS['min_length_mm']))
        self.txtMaxLength.Text = to_text(self.settings.get('max_length_mm', DEFAULT_SETTINGS['max_length_mm']))
        self.chkLimitMaxLength.IsChecked = setting_to_bool(
            self.settings.get('use_max_length', DEFAULT_SETTINGS['use_max_length']),
            DEFAULT_SETTINGS['use_max_length']
        )
        self.txtMinSize.Text = to_text(self.settings.get('min_size_mm', DEFAULT_SETTINGS['min_size_mm']))
        self.txtMaxSize.Text = to_text(self.settings.get('max_size_mm', DEFAULT_SETTINGS['max_size_mm']))
        self.chkLimitMaxSize.IsChecked = setting_to_bool(
            self.settings.get('use_max_size', DEFAULT_SETTINGS['use_max_size']),
            DEFAULT_SETTINGS['use_max_size']
        )
        self.txtRangeBottom.Text = to_text(self.settings.get('view_range_bottom_mm', DEFAULT_SETTINGS['view_range_bottom_mm']))
        self.txtRangeTop.Text = to_text(self.settings.get('view_range_top_mm', DEFAULT_SETTINGS['view_range_top_mm']))

        self.set_radio_value({
            'view': self.rbView,
            'selection': self.rbSelection
        }, self.settings.get('scope', 'selection'), 'selection')

        self.set_radio_value({
            'all': self.rbOrientationAll,
            'horizontal': self.rbOrientationHorizontal,
            'vertical': self.rbOrientationVertical
        }, self.settings.get('orientation', 'all'), 'all')

        self.set_radio_value({
            'all': self.rbAllPipes,
            'untagged': self.rbUntaggedOnly
        }, self.settings.get('tag_condition', 'all'), 'all')

        self.set_radio_value({
            'left': self.rbTagSideLeft,
            'right': self.rbTagSideRight
        }, self.settings.get('tag_side', 'left'), 'left')

        self.set_radio_value({
            'none': self.rbViewRangeNone,
            'manual': self.rbViewRangeManual,
            'active': self.rbViewRangeActive
        }, self.settings.get('view_range_mode', 'none'), 'none')

        saved_tag = self.settings.get('tag_type', '')
        if saved_tag and saved_tag in self.tag_map:
            self.cbTagTypes.SelectedItem = saved_tag
        elif self.tag_map and self.cbTagTypes.SelectedIndex < 0:
            self.cbTagTypes.SelectedIndex = 0

        if os.path.exists(CONFIG_FILE) or os.path.exists(FALLBACK_CONFIG_FILE):
            self.set_status(u'Đã nạp cấu hình lần chạy trước.')
        else:
            self.set_status(u'Chưa có cấu hình đã lưu. Tool sẽ tự lưu sau khi bấm Đặt Tag.')

    def get_current_settings(self):
        if bool(self.rbSelection.IsChecked):
            scope = 'selection'
        else:
            scope = 'view'

        if bool(self.rbOrientationHorizontal.IsChecked):
            orientation = 'horizontal'
        elif bool(self.rbOrientationVertical.IsChecked):
            orientation = 'vertical'
        else:
            orientation = 'all'

        if bool(self.rbUntaggedOnly.IsChecked):
            tag_condition = 'untagged'
        else:
            tag_condition = 'all'

        if bool(self.rbTagSideRight.IsChecked):
            tag_side = 'right'
        else:
            tag_side = 'left'

        if bool(self.rbViewRangeManual.IsChecked):
            view_range_mode = 'manual'
        elif bool(self.rbViewRangeActive.IsChecked):
            view_range_mode = 'active'
        else:
            view_range_mode = 'none'

        return {
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
            'tag_side': tag_side,
            'view_range_mode': view_range_mode,
            'view_range_bottom_mm': to_text(self.txtRangeBottom.Text),
            'view_range_top_mm': to_text(self.txtRangeTop.Text),
            'tag_type': to_text(self.cbTagTypes.SelectedItem)
        }

    # -------------------------------------------------------------------------
    # TAG COMBOBOX
    # -------------------------------------------------------------------------
    def setup_tag_combobox(self):
        doc = revit.doc
        pipe_tag_symbols = FilteredElementCollector(doc)\
                            .OfClass(FamilySymbol)\
                            .OfCategory(BuiltInCategory.OST_PipeTags)\
                            .ToElements()

        self.tag_map = {}
        for t in pipe_tag_symbols:
            type_param = t.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
            type_name = type_param.AsString() if type_param else 'Unknown Type'

            fam_param = t.get_Parameter(BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM)
            fam_name = fam_param.AsString() if fam_param else 'Unknown Family'

            if type_name != 'Unknown Type':
                display_name = '{} - {}'.format(fam_name, type_name)
                self.tag_map[display_name] = t

        self.cbTagTypes.ItemsSource = sorted(self.tag_map.keys())
        if self.tag_map:
            self.cbTagTypes.SelectedIndex = 0

    # -------------------------------------------------------------------------
    # MAIN COMMAND - CHỈ ĐỌC UI VÀ ĐÓNG CỬA SỔ
    # -------------------------------------------------------------------------
    def on_execute_click(self, sender, args):
        """Đọc và kiểm tra dữ liệu UI rồi đóng WPF.

        Không gọi PickObjects, không thao tác Selection và không mở Transaction
        trong event của WPF. Việc tách này tránh xung đột trạng thái modal của
        Revit khi user đã preselect ống trước lúc chạy lệnh.
        """
        try:
            spacing_mm = parse_mm_value(self.txtSpacing.Text, u'Khoảng cách xếp bậc')
            min_length_mm = parse_mm_value(self.txtMinLength.Text, u'Chiều dài ống tối thiểu')
            use_max_length = bool(self.chkLimitMaxLength.IsChecked)
            max_length_mm = 0.0
            if use_max_length:
                max_length_mm = parse_mm_value(self.txtMaxLength.Text, u'Chiều dài ống tối đa')

            min_size_mm = parse_mm_value(self.txtMinSize.Text, u'Đường kính ống nhỏ nhất')
            use_max_size = bool(self.chkLimitMaxSize.IsChecked)
            max_size_mm = 0.0
            if use_max_size:
                max_size_mm = parse_mm_value(self.txtMaxSize.Text, u'Đường kính ống lớn nhất')

            if spacing_mm <= 0:
                alert_msg(u'Khoảng cách xếp bậc phải lớn hơn 0.')
                return
            if min_length_mm < 0:
                alert_msg(u'Chiều dài ống tối thiểu không được nhỏ hơn 0.')
                return
            if use_max_length:
                if max_length_mm < 0:
                    alert_msg(u'Chiều dài ống tối đa không được nhỏ hơn 0.')
                    return
                if min_length_mm > max_length_mm:
                    alert_msg(u'Chiều dài ống tối thiểu không được lớn hơn chiều dài tối đa.')
                    return
            if min_size_mm < 0:
                alert_msg(u'Đường kính ống nhỏ nhất không được nhỏ hơn 0.')
                return
            if use_max_size:
                if max_size_mm < 0:
                    alert_msg(u'Đường kính ống lớn nhất không được nhỏ hơn 0.')
                    return
                if min_size_mm > max_size_mm:
                    alert_msg(u'Đường kính ống nhỏ nhất không được lớn hơn đường kính lớn nhất.')
                    return

            scope = 'selection' if bool(self.rbSelection.IsChecked) else 'view'

            if bool(self.rbOrientationHorizontal.IsChecked):
                orientation = 'horizontal'
            elif bool(self.rbOrientationVertical.IsChecked):
                orientation = 'vertical'
            else:
                orientation = 'all'

            tag_condition = 'untagged' if bool(self.rbUntaggedOnly.IsChecked) else 'all'
            tag_side = 'right' if bool(self.rbTagSideRight.IsChecked) else 'left'

            if bool(self.rbViewRangeManual.IsChecked):
                view_range_mode = 'manual'
            elif bool(self.rbViewRangeActive.IsChecked):
                view_range_mode = 'active'
            else:
                view_range_mode = 'none'

            # Chỉ ép kiểu các ô View Range khi chế độ manual thực sự được dùng.
            # Nhờ vậy nội dung tạm thời trong hai ô này không chặn các mode khác.
            if view_range_mode == 'manual':
                range_bottom_mm = parse_mm_value(self.txtRangeBottom.Text, u'Cao độ dưới View Range')
                range_top_mm = parse_mm_value(self.txtRangeTop.Text, u'Cao độ trên View Range')
                if range_top_mm <= range_bottom_mm:
                    alert_msg(u'Cao độ trên View Range phải lớn hơn cao độ dưới.')
                    return
            else:
                range_bottom_mm = 0.0
                range_top_mm = 0.0

            selected_tag_name = self.cbTagTypes.SelectedItem
            if not selected_tag_name or selected_tag_name not in self.tag_map:
                alert_msg(u'Vui lòng chọn loại Tag trước khi thực hiện.')
                return

            saved_path = save_settings(self.get_current_settings())
            if saved_path:
                self.set_status(u'Đã lưu cấu hình hiện tại.')

            self.result = {
                'spacing_mm': spacing_mm,
                'min_length_mm': min_length_mm,
                'max_length_mm': max_length_mm,
                'use_max_length': use_max_length,
                'min_size_mm': min_size_mm,
                'max_size_mm': max_size_mm,
                'use_max_size': use_max_size,
                'scope': scope,
                'orientation': orientation,
                'tag_condition': tag_condition,
                'tag_side': tag_side,
                'view_range_mode': view_range_mode,
                'view_range_bottom_mm': range_bottom_mm,
                'view_range_top_mm': range_top_mm,
                'tag_type_id': self.tag_map[selected_tag_name].Id
            }
            self.Close()

        except ValueError as ex:
            alert_msg(ensure_unicode(ex))
        except Exception as ex:
            self.result = None
            alert_msg(u'Lỗi giao diện: {}'.format(ensure_unicode(ex)))
            try:
                print(traceback.format_exc())
            except:
                pass

    def on_close_click(self, sender, args):
        self.result = None
        self.Close()


# -----------------------------------------------------------------------------
# COMMAND LOGIC - CHỈ CHẠY SAU KHI WPF ĐÃ ĐÓNG HOÀN TOÀN
# -----------------------------------------------------------------------------
def get_id_value(element_id):
    """Lấy ID dạng số, tương thích Revit mới và cũ."""
    try:
        return element_id.Value
    except:
        try:
            return element_id.IntegerValue
        except:
            return None


def is_pipe_element(elem):
    try:
        return (elem and elem.Category and
                get_id_value(elem.Category.Id) == int(BuiltInCategory.OST_PipeCurves))
    except:
        return False


def has_valid_curve(elem):
    try:
        return bool(elem.Location and hasattr(elem.Location, 'Curve') and elem.Location.Curve)
    except:
        return False


def deduplicate_pipes(elements):
    result = []
    seen_ids = set()

    for elem in elements or []:
        try:
            if not is_pipe_element(elem) or not has_valid_curve(elem):
                continue
            eid = get_id_value(elem.Id)
            if eid is None or eid in seen_ids:
                continue
            seen_ids.add(eid)
            result.append(elem)
        except:
            continue

    return result


def get_preselected_pipes(doc, uidoc):
    """Đọc các Pipe đã được chọn trước khi mở lệnh."""
    elements = []
    try:
        selected_ids = list(uidoc.Selection.GetElementIds())
    except:
        selected_ids = []

    for elem_id in selected_ids:
        try:
            elements.append(doc.GetElement(elem_id))
        except:
            pass

    return deduplicate_pipes(elements)


def resolve_view_range_bounds(doc, active_view, options):
    """Tính View Range sau khi cửa sổ WPF đã đóng."""
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


def collect_input_pipes(doc, uidoc, active_view, options, view_range_bounds):
    """Thu thập Pipe theo thứ tự an toàn.

    - Chế độ toàn view: collector theo active view.
    - Chế độ selection: ưu tiên Pipe đã chọn trước khi mở tool.
    - Chỉ khi không có preselection mới gọi PickObjects.
    """
    if options['scope'] == 'view':
        elements = FilteredElementCollector(doc, active_view.Id)\
                     .OfCategory(BuiltInCategory.OST_PipeCurves)\
                     .WhereElementIsNotElementType()\
                     .ToElements()
        return deduplicate_pipes(elements), False

    preselected = get_preselected_pipes(doc, uidoc)
    if preselected:
        return preselected, True

    pipe_filter = PipeSelectionFilter(view_range_bounds)
    mode = options['view_range_mode']
    if mode == 'manual':
        pick_message = u'Chưa có Pipe được chọn trước. Chỉ chọn được Pipe nằm trong View Range thủ công -> Nhấn FINISH.'
    elif mode == 'active':
        pick_message = u'Chưa có Pipe được chọn trước. Chỉ chọn được Pipe nằm trong View Range hiện hành -> Nhấn FINISH.'
    else:
        pick_message = u'Chưa có Pipe được chọn trước. Quét/click chọn các Pipe cần đặt Tag -> Nhấn FINISH.'

    selected_refs = uidoc.Selection.PickObjects(
        ObjectType.Element,
        pipe_filter,
        pick_message
    )

    elements = []
    for ref in selected_refs:
        try:
            elements.append(doc.GetElement(ref.ElementId))
        except:
            pass

    return deduplicate_pipes(elements), False


def get_tagged_pipe_dict(doc, active_view):
    rvt_year = int(doc.Application.VersionNumber)
    existing_tags = FilteredElementCollector(doc, active_view.Id)\
                        .OfClass(IndependentTag)\
                        .ToElements()

    tagged_dict = {}
    pipe_tag_cat_id = int(BuiltInCategory.OST_PipeTags)

    for tag in existing_tags:
        try:
            if not tag.Category or get_id_value(tag.Category.Id) != pipe_tag_cat_id:
                continue

            if rvt_year >= 2022:
                refs = tag.GetTaggedReferences()
                for tagged_ref in refs:
                    pid = get_id_value(tagged_ref.ElementId)
                    if pid is None:
                        continue
                    tagged_dict.setdefault(pid, []).append(tag.Id)
            else:
                pid = get_id_value(tag.TaggedLocalElementId)
                if pid is not None:
                    tagged_dict.setdefault(pid, []).append(tag.Id)
        except:
            pass

    return tagged_dict


def filter_pipes(raw_elements, options, view_range_bounds, tagged_dict):
    min_length_ft = options['min_length_mm'] / 304.8
    use_max_length = options.get('use_max_length', False)
    max_length_ft = options['max_length_mm'] / 304.8 if use_max_length else None

    min_size_ft = options['min_size_mm'] / 304.8
    use_max_size = options.get('use_max_size', True)
    max_size_ft = options['max_size_mm'] / 304.8 if use_max_size else None

    pipes_to_process = []
    view_range_skipped = 0

    for elem in deduplicate_pipes(raw_elements):
        try:
            if view_range_bounds and not element_overlaps_z_range(
                    elem, view_range_bounds[0], view_range_bounds[1]):
                view_range_skipped += 1
                continue

            curve = elem.Location.Curve
            ep0 = curve.GetEndPoint(0)
            ep1 = curve.GetEndPoint(1)
            vector = ep1 - ep0
            if vector.GetLength() <= 0.000001:
                continue

            dir_vec = vector.Normalize()
            is_vertical = abs(dir_vec.Z) > 0.99
            is_horizontal = not is_vertical

            if options['orientation'] == 'horizontal' and not is_horizontal:
                continue
            if options['orientation'] == 'vertical' and not is_vertical:
                continue

            pipe_id_value = get_id_value(elem.Id)
            if options['tag_condition'] == 'untagged' and pipe_id_value in tagged_dict:
                continue

            length_param = elem.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH)
            if length_param:
                pipe_length_ft = length_param.AsDouble()
                if pipe_length_ft < min_length_ft:
                    continue
                if use_max_length and pipe_length_ft > max_length_ft:
                    continue

            size_param = elem.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
            if size_param:
                pipe_size_ft = size_param.AsDouble()
                if pipe_size_ft < min_size_ft:
                    continue
                if use_max_size and pipe_size_ft > max_size_ft:
                    continue

            pipes_to_process.append(elem)
        except Exception as ex:
            try:
                print(ensure_unicode(u'Bỏ qua Pipe ID {} khi lọc: {}'.format(elem.Id, ensure_unicode(ex))))
            except:
                pass

    return pipes_to_process, view_range_skipped


def build_parallel_clusters(pipes_to_process):
    clusters = []
    unclustered = list(pipes_to_process)

    while unclustered:
        current = unclustered.pop(0)
        cluster = [current]
        remaining = []

        for pipe in unclustered:
            try:
                if are_pipes_parallel(current, pipe) and get_perpendicular_distance(current, pipe) < 5.0:
                    cluster.append(pipe)
                else:
                    remaining.append(pipe)
            except:
                remaining.append(pipe)

        unclustered = remaining
        clusters.append(cluster)

    return clusters


def create_pipe_tags(doc, active_view, options, clusters, tagged_dict):
    tag_symbol = doc.GetElement(options['tag_type_id'])
    if not tag_symbol:
        raise Exception(u'Không tìm thấy loại Pipe Tag đã chọn trong model.')

    view_scale = active_view.Scale
    if view_scale <= 0:
        raise Exception(u'Active view không có tỷ lệ hợp lệ để tính khoảng cách tag.')

    view_right = active_view.RightDirection
    view_up = active_view.UpDirection

    first_offset_mm = 4.0
    model_first_offset = mm_to_feet(first_offset_mm, view_scale)
    model_spacing = mm_to_feet(options['spacing_mm'], view_scale)
    tag_all_pipes = options['tag_condition'] == 'all'
    tag_side_left = options['tag_side'] == 'left'

    success_count = 0
    deleted_count = 0
    skipped_count = 0
    deleted_tag_keys = set()

    with revit.Transaction('Tag Pipes Pro'):
        if not tag_symbol.IsActive:
            tag_symbol.Activate()
            doc.Regenerate()

        for cluster in clusters:
            if not cluster:
                continue

            try:
                first_curve = cluster[0].Location.Curve
                direction_vector = first_curve.GetEndPoint(1) - first_curve.GetEndPoint(0)
                if direction_vector.GetLength() <= 0.000001:
                    skipped_count += len(cluster)
                    continue
                pipe_direction = direction_vector.Normalize()

                if abs(pipe_direction.DotProduct(view_right)) > 0.707:
                    dir_perp = view_up
                    dir_para = reverse_xyz(view_right) if tag_side_left else view_right
                    sorted_cluster = sorted(
                        cluster,
                        key=lambda p: p.Location.Curve.Evaluate(0.5, True).DotProduct(view_up)
                    )
                else:
                    dir_perp = reverse_xyz(view_right) if tag_side_left else view_right
                    dir_para = view_up
                    sorted_cluster = sorted(
                        cluster,
                        key=lambda p: p.Location.Curve.Evaluate(0.5, True).DotProduct(view_right)
                    )
            except Exception as ex:
                skipped_count += len(cluster)
                try:
                    print(ensure_unicode(u'Lỗi xử lý cụm Pipe: {}'.format(ensure_unicode(ex))))
                except:
                    pass
                continue

            for index, pipe in enumerate(sorted_cluster):
                try:
                    pipe_id_value = get_id_value(pipe.Id)

                    if tag_all_pipes and pipe_id_value in tagged_dict:
                        for old_tag_id in tagged_dict[pipe_id_value]:
                            old_tag_key = get_id_value(old_tag_id)
                            if old_tag_key in deleted_tag_keys:
                                continue
                            try:
                                doc.Delete(old_tag_id)
                                deleted_tag_keys.add(old_tag_key)
                                deleted_count += 1
                            except:
                                pass

                    mid_point = pipe.Location.Curve.Evaluate(0.5, True)
                    vec_perp = dir_perp * model_first_offset
                    vec_para = dir_para * (index * model_spacing)
                    head_pos = mid_point + vec_perp + vec_para

                    new_tag = IndependentTag.Create(
                        doc,
                        tag_symbol.Id,
                        active_view.Id,
                        Reference(pipe),
                        False,
                        TagOrientation.Horizontal,
                        head_pos
                    )

                    # Giữ nguyên logic leader của bản gốc.
                    try:
                        new_tag.HasLeader = True
                    except:
                        pass
                    try:
                        new_tag.LeaderEndCondition = LeaderEndCondition.Attached
                    except:
                        pass

                    success_count += 1

                except Exception as ex:
                    skipped_count += 1
                    try:
                        print(ensure_unicode(u'Lỗi tại Pipe ID {}: {}'.format(pipe.Id, ensure_unicode(ex))))
                    except:
                        pass

    return success_count, deleted_count, skipped_count


def show_result(options, success_count, deleted_count, skipped_count,
                view_range_skipped, used_preselection):
    lines = []

    if options['tag_condition'] == 'all' and deleted_count > 0:
        lines.append(u'Đã xóa {} tag cũ.'.format(deleted_count))
    lines.append(u'Đã tạo mới {} tag xếp bậc.'.format(success_count))

    if skipped_count > 0:
        lines.append(u'Bỏ qua {} Pipe do lỗi khi tạo tag.'.format(skipped_count))
    if view_range_skipped > 0:
        lines.append(u'Bỏ qua {} Pipe nằm ngoài View Range.'.format(view_range_skipped))
    if used_preselection:
        lines.append(u'Đã dùng trực tiếp các Pipe được chọn trước khi mở lệnh.')

    toast_msg(u'\n'.join(lines))


def run_tool():
    doc = revit.doc
    uidoc = revit.uidoc
    active_view = doc.ActiveView

    win = TagParallelPipesWPF(xaml_file)
    win.show_dialog()

    options = win.result
    if not options:
        return

    view_range_bounds = resolve_view_range_bounds(doc, active_view, options)
    if view_range_bounds is False:
        return

    try:
        raw_elements, used_preselection = collect_input_pipes(
            doc,
            uidoc,
            active_view,
            options,
            view_range_bounds
        )
    except OperationCanceledException:
        return

    if not raw_elements:
        alert_msg(u'Không có Pipe nào được chọn để đặt Tag.')
        return

    try:
        tagged_dict = get_tagged_pipe_dict(doc, active_view)
        pipes_to_process, view_range_skipped = filter_pipes(
            raw_elements,
            options,
            view_range_bounds,
            tagged_dict
        )

        if not pipes_to_process:
            if view_range_bounds and view_range_skipped > 0:
                alert_msg(u'Không tìm thấy Pipe nào thỏa mãn điều kiện lọc. Có {} Pipe bị bỏ qua vì nằm ngoài View Range.'.format(view_range_skipped))
            else:
                alert_msg(u'Không tìm thấy Pipe nào thỏa mãn các điều kiện lọc.')
            return

        clusters = build_parallel_clusters(pipes_to_process)
        success_count, deleted_count, skipped_count = create_pipe_tags(
            doc,
            active_view,
            options,
            clusters,
            tagged_dict
        )

        show_result(
            options,
            success_count,
            deleted_count,
            skipped_count,
            view_range_skipped,
            used_preselection
        )

    except Exception as ex:
        alert_msg(u'Lỗi hệ thống: {}'.format(ensure_unicode(ex)))
        try:
            print(traceback.format_exc())
        except:
            pass


if __name__ == '__main__':
    run_tool()
