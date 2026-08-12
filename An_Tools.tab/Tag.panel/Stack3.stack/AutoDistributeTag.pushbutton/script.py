# -*- coding: utf-8 -*-
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException

from pyrevit import revit, forms

import os
import json
import tempfile

# ============================================================
# AUTO ALIGN TAG / TEXTNOTE
#
# NORMAL CLICK:
#   - Dùng lại cấu hình đã lưu lần trước.
#   - Nếu chưa có cấu hình, sẽ mở giao diện cấu hình 1 lần đầu.
#   - Sau đó yêu cầu chọn nhiều Tag/TextNote rồi chạy.
#
# SHIFT + CLICK:
#   - Mở giao diện cấu hình.
#   - Chọn kiểu sắp xếp:
#       1) Theo chiều dọc Y
#          a. Giữ tag/text trên cùng, đẩy các tag phía dưới xuống.
#          b. Giữ tag/text dưới cùng, đẩy các tag khác lên trên.
#       2) Theo chiều ngang X
#          a. Giữ tag/text bên phải cùng, đẩy các tag còn lại sang trái.
#          b. Giữ tag/text bên trái cùng, đẩy các tag còn lại sang phải.
#   - Lưu cấu hình mới.
#   - Sau đó yêu cầu chọn nhiều Tag/TextNote rồi chạy.
#
# Ghi chú:
#   - Khoảng cách nhập là khoảng cách trên giấy, đơn vị mm.
#   - Script tự nhân theo view.Scale để ra khoảng cách trong model.
# ============================================================

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView

# --- FILE LƯU CẤU HÌNH ---
CONFIG_FILE = os.path.join(tempfile.gettempdir(), "pyrevit_tag_align_config.json")

DEFAULT_GAP_MM = "8.0"

AXIS_VERTICAL = "vertical"
AXIS_HORIZONTAL = "horizontal"

V_KEEP_TOP_PUSH_DOWN = "v_keep_top_push_down"
V_KEEP_BOTTOM_PUSH_UP = "v_keep_bottom_push_up"

H_KEEP_RIGHT_PUSH_LEFT = "h_keep_right_push_left"
H_KEEP_LEFT_PUSH_RIGHT = "h_keep_left_push_right"

SCHEMA_VERSION = 2


def get_default_config():
    return {
        'schema_version': SCHEMA_VERSION,
        'axis': AXIS_VERTICAL,
        'vertical_mode': V_KEEP_TOP_PUSH_DOWN,
        'horizontal_mode': H_KEEP_RIGHT_PUSH_LEFT,
        'gap_y_mm': DEFAULT_GAP_MM,
        'gap_x_mm': DEFAULT_GAP_MM
    }


def is_shift_click():
    """Kiểm tra người dùng có Shift+Click vào nút pyRevit hay không."""
    try:
        from pyrevit import EXEC_PARAMS
        if getattr(EXEC_PARAMS, 'config_mode', False):
            return True
    except Exception:
        pass

    # Fallback cho một số bản pyRevit cũ
    try:
        return bool(__shiftclick__)  # noqa: F821
    except Exception:
        return False


def normalize_positive_number_string(val, default_val=None):
    """Validate số dương, cho phép nhập dấu phẩy thập phân."""
    if val is None:
        return default_val

    text = str(val).strip()
    if not text:
        return default_val

    try:
        num = float(text.replace(',', '.'))
        if num <= 0:
            return default_val
        return text
    except Exception:
        return default_val


def load_config():
    """Đọc cấu hình đã lưu. Hỗ trợ cả config cũ chỉ có last_gap."""
    if not os.path.exists(CONFIG_FILE):
        return None

    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
    except Exception:
        return None

    cfg = get_default_config()

    # Tương thích file cũ của script trước: {'last_gap': '8.0'}
    old_gap = normalize_positive_number_string(data.get('last_gap', None), None)
    if old_gap:
        cfg['gap_y_mm'] = old_gap
        cfg['gap_x_mm'] = old_gap

    axis = data.get('axis', cfg['axis'])
    if axis in [AXIS_VERTICAL, AXIS_HORIZONTAL]:
        cfg['axis'] = axis

    vertical_mode = data.get('vertical_mode', cfg['vertical_mode'])
    if vertical_mode in [V_KEEP_TOP_PUSH_DOWN, V_KEEP_BOTTOM_PUSH_UP]:
        cfg['vertical_mode'] = vertical_mode

    horizontal_mode = data.get('horizontal_mode', cfg['horizontal_mode'])
    if horizontal_mode in [H_KEEP_RIGHT_PUSH_LEFT, H_KEEP_LEFT_PUSH_RIGHT]:
        cfg['horizontal_mode'] = horizontal_mode

    gap_y = normalize_positive_number_string(data.get('gap_y_mm', cfg['gap_y_mm']), cfg['gap_y_mm'])
    gap_x = normalize_positive_number_string(data.get('gap_x_mm', cfg['gap_x_mm']), cfg['gap_x_mm'])
    cfg['gap_y_mm'] = gap_y
    cfg['gap_x_mm'] = gap_x

    return cfg


def save_config(cfg):
    """Lưu cấu hình để lần click thường tiếp theo dùng lại."""
    try:
        data = dict(cfg)
        data['schema_version'] = SCHEMA_VERSION

        # Giữ thêm last_gap để tương thích nếu quay lại code cũ.
        if data.get('axis') == AXIS_HORIZONTAL:
            data['last_gap'] = data.get('gap_x_mm', DEFAULT_GAP_MM)
        else:
            data['last_gap'] = data.get('gap_y_mm', DEFAULT_GAP_MM)

        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


def ask_gap_mm(default_value, title, prompt):
    """Hỏi khoảng cách mm."""
    user_input = forms.ask_for_string(
        default=str(default_value),
        prompt=prompt,
        title=title
    )

    if user_input is None:
        return None

    val = normalize_positive_number_string(user_input, None)
    if val is None:
        forms.alert("Khoảng cách nhập vào không hợp lệ. Vui lòng nhập số lớn hơn 0.")
        return None

    return val


def show_config_dialog(saved_cfg=None):
    """Giao diện cấu hình khi Shift+Click hoặc lần đầu chưa có config."""
    cfg = saved_cfg if saved_cfg else get_default_config()

    axis_options = [
        "Sắp xếp theo chiều DỌC - trục Y",
        "Sắp xếp theo chiều NGANG - trục X"
    ]

    axis_choice = forms.CommandSwitchWindow.show(
        axis_options,
        message="Chọn kiểu đẩy Tag/Text Note ra xa nhau"
    )

    if not axis_choice:
        return None

    if axis_choice == axis_options[0]:
        cfg['axis'] = AXIS_VERTICAL

        vertical_options = [
            "1. Giữ nguyên tag/text TRÊN CÙNG, đẩy các tag phía dưới xuống",
            "2. Giữ nguyên tag/text DƯỚI CÙNG, đẩy các tag khác lên trên"
        ]

        vertical_choice = forms.CommandSwitchWindow.show(
            vertical_options,
            message="Chọn hướng sắp xếp theo chiều dọc"
        )

        if not vertical_choice:
            return None

        if vertical_choice == vertical_options[0]:
            cfg['vertical_mode'] = V_KEEP_TOP_PUSH_DOWN
        else:
            cfg['vertical_mode'] = V_KEEP_BOTTOM_PUSH_UP

        gap_y = ask_gap_mm(
            cfg.get('gap_y_mm', DEFAULT_GAP_MM),
            "Khoảng cách dọc",
            "Nhập khoảng cách giữa các tag/text theo chiều dọc trên giấy (mm):"
        )
        if gap_y is None:
            return None

        cfg['gap_y_mm'] = gap_y

    else:
        cfg['axis'] = AXIS_HORIZONTAL

        horizontal_options = [
            "1. Giữ nguyên tag/text BÊN PHẢI CÙNG, đẩy các tag còn lại sang trái",
            "2. Giữ nguyên tag/text BÊN TRÁI CÙNG, đẩy các tag còn lại sang phải"
        ]

        horizontal_choice = forms.CommandSwitchWindow.show(
            horizontal_options,
            message="Chọn hướng sắp xếp theo chiều ngang"
        )

        if not horizontal_choice:
            return None

        if horizontal_choice == horizontal_options[0]:
            cfg['horizontal_mode'] = H_KEEP_RIGHT_PUSH_LEFT
        else:
            cfg['horizontal_mode'] = H_KEEP_LEFT_PUSH_RIGHT

        gap_x = ask_gap_mm(
            cfg.get('gap_x_mm', DEFAULT_GAP_MM),
            "Khoảng cách ngang",
            "Nhập khoảng cách giữa các tag/text theo chiều ngang trên giấy (mm):"
        )
        if gap_x is None:
            return None

        cfg['gap_x_mm'] = gap_x

    save_config(cfg)
    return cfg


def get_run_config():
    """
    Lấy cấu hình cho lần chạy hiện tại.
    - Shift+Click: luôn mở UI cấu hình.
    - Normal Click: dùng cấu hình cũ.
    - Chưa có cấu hình: mở UI lần đầu.
    """
    saved_cfg = load_config()

    if is_shift_click() or saved_cfg is None:
        return show_config_dialog(saved_cfg)

    return saved_cfg


# --- FILTER CHỌN TAG / TEXTNOTE SAU KHI NHẤN LỆNH ---
class TagTextSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, IndependentTag) or isinstance(elem, TextNote)

    def AllowReference(self, reference, position):
        return False


def pick_tag_text_elements():
    """Yêu cầu chọn nhiều Tag hoặc TextNote sau khi bấm lệnh."""
    try:
        picked_refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            TagTextSelectionFilter(),
            "Chọn nhiều Tag hoặc Text Note cần sắp xếp, sau đó bấm Finish."
        )
    except OperationCanceledException:
        return []
    except Exception as ex:
        forms.alert("Không thể chọn đối tượng.\n{}".format(ex))
        return []

    elements = []
    for ref in picked_refs:
        elem = doc.GetElement(ref.ElementId)
        if isinstance(elem, (IndependentTag, TextNote)):
            elements.append(elem)

    return elements


# --- HÀM BỔ TRỢ HÌNH HỌC 2D ---
def get_dot_product(pt, vector):
    """Tính hình chiếu của điểm lên vector của view."""
    return pt.X * vector.X + pt.Y * vector.Y + pt.Z * vector.Z


def get_tag_host_point(tag, doc, view):
    """Tìm điểm kết nối leader cố định trên đối tượng được tag."""
    host_id = None

    if hasattr(tag, "GetTaggedLocalElementIds"):
        host_ids = tag.GetTaggedLocalElementIds()
        if host_ids and host_ids.Count > 0:
            host_id = list(host_ids)[0]
    elif hasattr(tag, "TaggedLocalElementId"):
        host_id = tag.TaggedLocalElementId

    if not host_id or host_id == ElementId.InvalidElementId:
        return None

    host = doc.GetElement(host_id)
    if not host:
        return None

    if hasattr(host, 'Location') and hasattr(host.Location, 'Curve'):
        curve = host.Location.Curve
        return curve.Evaluate(0.5, True)  # Trung điểm đối tượng dạng line/curve

    bbox = host.get_BoundingBox(view)
    if bbox:
        return (bbox.Min + bbox.Max) / 2.0

    return None


def get_textnote_leader_host_point(textnote):
    """Lấy điểm End của leader đầu tiên của TextNote."""
    try:
        leaders = textnote.GetLeaders()
        if leaders and len(leaders) > 0:
            return leaders[0].End
    except Exception:
        pass
    return None


def restore_textnote_leader_end(textnote, host_pt):
    """Sau khi Move TextNote, đưa leader end về lại vị trí host cũ."""
    try:
        leaders = textnote.GetLeaders()
        if leaders and len(leaders) > 0:
            leaders[0].End = host_pt
    except Exception:
        pass


# --- THUẬT TOÁN KIỂM TRA GIAO CẮT 2 ĐOẠN THẲNG ---
def ccw(A, B, C):
    """Kiểm tra chiều quay của 3 điểm."""
    return (C['y'] - A['y']) * (B['x'] - A['x']) > (B['y'] - A['y']) * (C['x'] - A['x'])


def is_intersect(p1, q1, p2, q2):
    """Trả về True nếu đoạn thẳng p1-q1 cắt đoạn thẳng p2-q2."""
    return ccw(p1, p2, q2) != ccw(q1, p2, q2) and ccw(p1, q1, p2) != ccw(p1, q1, q2)


def xyz_to_2d_dict(x, y):
    return {'x': x, 'y': y}


def get_target_2d_for_index(index, cfg, anchor_x, anchor_y, gap_ft):
    """Tọa độ target 2D theo index sau khi sắp xếp."""
    axis = cfg.get('axis', AXIS_VERTICAL)

    if axis == AXIS_VERTICAL:
        vertical_mode = cfg.get('vertical_mode', V_KEEP_TOP_PUSH_DOWN)

        if vertical_mode == V_KEEP_BOTTOM_PUSH_UP:
            return xyz_to_2d_dict(anchor_x, anchor_y + index * gap_ft)

        # Mặc định: giữ trên cùng, đẩy xuống
        return xyz_to_2d_dict(anchor_x, anchor_y - index * gap_ft)

    horizontal_mode = cfg.get('horizontal_mode', H_KEEP_RIGHT_PUSH_LEFT)

    if horizontal_mode == H_KEEP_LEFT_PUSH_RIGHT:
        return xyz_to_2d_dict(anchor_x + index * gap_ft, anchor_y)

    # Mặc định: giữ phải cùng, đẩy sang trái
    return xyz_to_2d_dict(anchor_x - index * gap_ft, anchor_y)


def sort_elements_for_mode(elem_data, cfg):
    """
    Sắp xếp danh sách theo mode.
    Phần tử index 0 luôn là phần tử được giữ nguyên vị trí.
    """
    axis = cfg.get('axis', AXIS_VERTICAL)

    if axis == AXIS_VERTICAL:
        vertical_mode = cfg.get('vertical_mode', V_KEEP_TOP_PUSH_DOWN)

        if vertical_mode == V_KEEP_BOTTOM_PUSH_UP:
            # Dưới cùng đứng yên, các tag khác đẩy lên trên.
            elem_data.sort(key=lambda k: k['head_y'])
        else:
            # Trên cùng đứng yên, các tag phía dưới đẩy xuống.
            elem_data.sort(key=lambda k: k['head_y'], reverse=True)

    else:
        horizontal_mode = cfg.get('horizontal_mode', H_KEEP_RIGHT_PUSH_LEFT)

        if horizontal_mode == H_KEEP_LEFT_PUSH_RIGHT:
            # Trái cùng đứng yên, các tag khác đẩy sang phải.
            elem_data.sort(key=lambda k: k['head_x'])
        else:
            # Phải cùng đứng yên, các tag khác đẩy sang trái.
            elem_data.sort(key=lambda k: k['head_x'], reverse=True)

    return elem_data


def uncross_leaders_keep_anchor(elem_data, cfg, anchor_x, anchor_y, gap_ft):
    """
    Đảo thứ tự chống leader giao cắt nhưng giữ nguyên phần tử neo ở index 0.
    Như vậy tag/text trên cùng, dưới cùng, trái cùng hoặc phải cùng sẽ không bị đổi chỗ.
    """
    if len(elem_data) < 3:
        return elem_data

    swapped = True
    max_iters = 100
    iters = 0

    while swapped and iters < max_iters:
        swapped = False
        iters += 1

        # Bắt đầu từ 1 để không đổi vị trí phần tử neo index 0.
        for i in range(1, len(elem_data) - 1):
            h1 = xyz_to_2d_dict(elem_data[i]['host_x'], elem_data[i]['host_y'])
            h2 = xyz_to_2d_dict(elem_data[i + 1]['host_x'], elem_data[i + 1]['host_y'])

            t1 = get_target_2d_for_index(i, cfg, anchor_x, anchor_y, gap_ft)
            t2 = get_target_2d_for_index(i + 1, cfg, anchor_x, anchor_y, gap_ft)

            if is_intersect(h1, t1, h2, t2):
                elem_data[i], elem_data[i + 1] = elem_data[i + 1], elem_data[i]
                swapped = True

    return elem_data


def build_element_data(elements):
    """Đọc thông tin tọa độ host/head của tag/text."""
    view_up = view.UpDirection
    view_right = view.RightDirection

    elem_data = []

    for elem in elements:
        host_pt = None
        head_pos = None

        if isinstance(elem, IndependentTag):
            host_pt = get_tag_host_point(elem, doc, view)
            head_pos = elem.TagHeadPosition

        elif isinstance(elem, TextNote):
            host_pt = get_textnote_leader_host_point(elem)
            head_pos = elem.Coord

        if not host_pt or not head_pos:
            continue

        elem_data.append({
            'element': elem,
            'is_tag': isinstance(elem, IndependentTag),
            'host_x': get_dot_product(host_pt, view_right),
            'host_y': get_dot_product(host_pt, view_up),
            'head_x': get_dot_product(head_pos, view_right),
            'head_y': get_dot_product(head_pos, view_up),
            'original_pos': head_pos,
            'host_pt_xyz': host_pt
        })

    return elem_data


def get_gap_for_current_axis(cfg):
    """Lấy khoảng cách phù hợp theo kiểu dọc/ngang."""
    axis = cfg.get('axis', AXIS_VERTICAL)

    if axis == AXIS_HORIZONTAL:
        val = cfg.get('gap_x_mm', DEFAULT_GAP_MM)
    else:
        val = cfg.get('gap_y_mm', DEFAULT_GAP_MM)

    try:
        gap_mm = float(str(val).replace(',', '.'))
        if gap_mm <= 0:
            gap_mm = float(DEFAULT_GAP_MM)
    except Exception:
        gap_mm = float(DEFAULT_GAP_MM)

    # Khoảng cách trên giấy mm -> feet model theo tỉ lệ view.
    return (gap_mm / 304.8) * view.Scale


def apply_targets(elem_data, cfg, gap_ft):
    """Tạo target_x/target_y cho từng tag/text."""
    elem_data = sort_elements_for_mode(elem_data, cfg)

    anchor = elem_data[0]
    anchor_x = anchor['head_x']
    anchor_y = anchor['head_y']

    elem_data = uncross_leaders_keep_anchor(elem_data, cfg, anchor_x, anchor_y, gap_ft)

    for index, item in enumerate(elem_data):
        target_2d = get_target_2d_for_index(index, cfg, anchor_x, anchor_y, gap_ft)
        item['target_x'] = target_2d['x']
        item['target_y'] = target_2d['y']

    return elem_data


def move_elements_to_targets(elem_data):
    """Áp dụng di chuyển vào Revit."""
    view_up = view.UpDirection
    view_right = view.RightDirection

    with revit.Transaction("Auto Align Tag/Text - Saved UI Mode"):
        for item in elem_data:
            elem = item['element']
            original_pos = item['original_pos']

            delta_x = item['target_x'] - item['head_x']
            delta_y = item['target_y'] - item['head_y']

            vec_x = view_right.Multiply(delta_x)
            vec_y = view_up.Multiply(delta_y)
            translation_vector = vec_x.Add(vec_y)

            if item['is_tag']:
                elem.TagHeadPosition = original_pos.Add(translation_vector)
                try:
                    elem.HasLeader = True
                except Exception:
                    pass
            else:
                ElementTransformUtils.MoveElement(doc, elem.Id, translation_vector)
                restore_textnote_leader_end(elem, item['host_pt_xyz'])


def main():
    cfg = get_run_config()
    if cfg is None:
        return

    # Nhấn lệnh trước -> chọn nhiều tag/text -> Finish -> chạy.
    elements = pick_tag_text_elements()
    if not elements:
        return

    if len(elements) < 2:
        forms.alert("Vui lòng chọn ít nhất 2 Tag hoặc Text Note để sắp xếp.")
        return

    elem_data = build_element_data(elements)

    if not elem_data:
        forms.alert("Không thể đọc dữ liệu từ các đối tượng đã chọn.\nLưu ý: Text Note cần có leader, Tag cần có host hợp lệ.")
        return

    if len(elem_data) < 2:
        forms.alert("Chỉ đọc được 1 đối tượng hợp lệ. Vui lòng chọn thêm Tag/Text Note có leader hoặc host hợp lệ.")
        return

    gap_ft = get_gap_for_current_axis(cfg)
    elem_data = apply_targets(elem_data, cfg, gap_ft)
    move_elements_to_targets(elem_data)


if __name__ == '__main__':
    main()
