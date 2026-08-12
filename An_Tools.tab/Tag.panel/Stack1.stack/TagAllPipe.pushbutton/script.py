# -*- coding: utf-8 -*-
__title__ = 'Tag With Leader'

import os
import math
import traceback

from Autodesk.Revit.DB import (
    Transaction,
    FilteredElementCollector,
    BuiltInCategory,
    BuiltInParameter,
    Line,
    XYZ,
    IndependentTag,
    Reference,
    TagOrientation,
    LeaderEndCondition,
    Element,
    ElementId,
    PlanViewPlane,
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException
from pyrevit import forms, revit, script

# File XAML đặt cùng thư mục với script.py trong bundle pyRevit
xaml_file = os.path.join(os.path.dirname(__file__), 'ui_definition.xaml')

PIPE_CAT_ID = int(BuiltInCategory.OST_PipeCurves)
TAG_CAT_ID = int(BuiltInCategory.OST_PipeTags)
EPS = 0.000001


def mm_to_ft(mm):
    return mm / 304.8


def ft_to_mm(ft):
    return ft * 304.8


def get_elem_name(elem):
    try:
        return Element.Name.__get__(elem)
    except:
        try:
            return elem.Name
        except:
            return ""


def is_valid_element_id(eid):
    try:
        return eid and eid != ElementId.InvalidElementId
    except:
        return False


def is_pipe_element(elem):
    try:
        return elem and elem.Category and elem.Category.Id.IntegerValue == PIPE_CAT_ID
    except:
        return False


def get_curve(pipe):
    try:
        if pipe.Location and hasattr(pipe.Location, 'Curve') and pipe.Location.Curve:
            return pipe.Location.Curve
    except:
        pass
    return None


def get_curve_midpoint(curve):
    try:
        return curve.Evaluate(0.5, True)
    except:
        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
        return XYZ((p0.X + p1.X) / 2.0, (p0.Y + p1.Y) / 2.0, (p0.Z + p1.Z) / 2.0)


def get_pipe_z_range(pipe):
    """Lấy khoảng cao độ Z của ống. Ưu tiên bounding box để tính cả đường kính ống."""
    z_values = []

    try:
        bb = pipe.get_BoundingBox(None)
        if bb:
            z_values.append(bb.Min.Z)
            z_values.append(bb.Max.Z)
    except:
        pass

    try:
        curve = get_curve(pipe)
        if curve:
            z_values.append(curve.GetEndPoint(0).Z)
            z_values.append(curve.GetEndPoint(1).Z)
    except:
        pass

    if not z_values:
        return None

    return min(z_values), max(z_values)


def pipe_intersects_z_range(pipe, z_min, z_max):
    if z_min is None or z_max is None:
        return True

    pipe_range = get_pipe_z_range(pipe)
    if not pipe_range:
        return False

    p_min, p_max = pipe_range
    low = min(z_min, z_max)
    high = max(z_min, z_max)

    # Chỉ cần ống giao/cắt qua vùng view range là cho phép chọn/tag.
    return p_max >= low - EPS and p_min <= high + EPS


def get_view_level(doc, view):
    """Lấy Level gốc của active view, dùng cho manual View Range."""
    try:
        if hasattr(view, 'GenLevel') and view.GenLevel:
            return view.GenLevel
    except:
        pass

    # Một số version/template trả level qua parameter.
    for bip_name in ['PLAN_VIEW_LEVEL', 'VIEW_ASSOCIATED_LEVEL']:
        try:
            bip = getattr(BuiltInParameter, bip_name)
            param = view.get_Parameter(bip)
            if param:
                level_id = param.AsElementId()
                if is_valid_element_id(level_id):
                    level = doc.GetElement(level_id)
                    if level:
                        return level
        except:
            pass

    return None


def get_viewrange_plane_elevation(doc, view, view_range, plane):
    level = None
    try:
        level_id = view_range.GetLevelId(plane)
        if is_valid_element_id(level_id):
            level = doc.GetElement(level_id)
    except:
        level = None

    if not level:
        level = get_view_level(doc, view)

    if not level:
        return None

    try:
        offset = view_range.GetOffset(plane)
    except:
        offset = 0.0

    return level.Elevation + offset


def get_active_view_range(doc, view):
    """Trả về (bottom_z, top_z) theo cao độ project của View Range hiện hành."""
    try:
        vr = view.GetViewRange()
    except:
        return None

    bottom_z = get_viewrange_plane_elevation(doc, view, vr, PlanViewPlane.BottomClipPlane)
    top_z = get_viewrange_plane_elevation(doc, view, vr, PlanViewPlane.TopClipPlane)

    if bottom_z is None or top_z is None:
        return None

    return min(bottom_z, top_z), max(bottom_z, top_z)


def get_manual_view_range(doc, view, bottom_offset_mm, top_offset_mm):
    """Manual Bottom/Top offset tính theo Level của active view."""
    level = get_view_level(doc, view)
    if not level:
        return None

    bottom_z = level.Elevation + mm_to_ft(bottom_offset_mm)
    top_z = level.Elevation + mm_to_ft(top_offset_mm)
    return min(bottom_z, top_z), max(bottom_z, top_z)


def get_pipe_angle_xy(curve):
    """Góc của ống trên mặt bằng XY. Normalize để text không bị lộn ngược."""
    try:
        v = curve.GetEndPoint(1) - curve.GetEndPoint(0)
        xy_len = math.sqrt(v.X * v.X + v.Y * v.Y)
        if xy_len < EPS:
            return None

        angle = math.atan2(v.Y, v.X)

        # Giữ chữ đọc thuận: đưa góc về khoảng -90 đến +90 độ.
        if angle > math.pi / 2.0:
            angle -= math.pi
        elif angle <= -math.pi / 2.0:
            angle += math.pi

        return angle
    except:
        return None


def is_pipe_horizontal_on_plan(angle):
    if angle is None:
        return False
    # Trong khoảng khoảng 10 độ so với phương X thì xem là ống ngang.
    return abs(math.sin(angle)) <= math.sin(math.radians(10.0))


def is_pipe_vertical_on_plan(angle):
    if angle is None:
        return False
    # Trong khoảng khoảng 10 độ so với phương Y thì xem là ống dọc trên mặt bằng.
    return abs(math.cos(angle)) <= math.sin(math.radians(10.0))


def is_pipe_non_horizontal_on_plan(angle):
    """Ống theo phương Y hoặc ống xéo trên mặt bằng."""
    return angle is not None and not is_pipe_horizontal_on_plan(angle)


def build_tag_layout_items(valid_pipes, pick_point, step_ft):
    """
    Tạo danh sách layout tag.
    - Ống ngang theo phương X: xếp tag giãn cách theo Y như logic cũ.
    - Ống dọc theo phương Y hoặc ống xéo: xếp tag giãn cách theo X.

    Việc tách 2 nhóm giúp tag luôn giữ đúng thứ tự của ống:
    - Nhóm ngang: ống phía trên sẽ có tag phía trên.
    - Nhóm dọc/xéo: ống bên trái sẽ có tag bên trái, sau đó nhảy dần theo phương X.
    """
    pipe_infos = []
    for pipe in valid_pipes:
        curve = get_curve(pipe)
        if not curve:
            continue

        midpoint = get_curve_midpoint(curve)
        angle = get_pipe_angle_xy(curve)
        pipe_infos.append({
            'pipe': pipe,
            'curve': curve,
            'midpoint': midpoint,
            'angle': angle,
            'is_horizontal': is_pipe_horizontal_on_plan(angle),
        })

    horizontal_infos = [info for info in pipe_infos if info['is_horizontal']]
    non_horizontal_infos = [info for info in pipe_infos if not info['is_horizontal']]

    layout_items = []

    # Nhóm ống ngang: giữ cách chạy cũ, giãn cách theo Y từ điểm đáy được pick.
    horizontal_infos = sorted(
        horizontal_infos,
        key=lambda info: (
            -info['midpoint'].Y,
            info['midpoint'].X,
            info['pipe'].Id.IntegerValue
        )
    )

    total_horizontal = len(horizontal_infos)
    for i, info in enumerate(horizontal_infos):
        tag_y = pick_point.Y + (total_horizontal - 1 - i) * step_ft
        info['head_pos'] = XYZ(pick_point.X, tag_y, info['midpoint'].Z)
        info['layout_axis'] = 'Y'
        layout_items.append(info)

    # Nhóm ống dọc Y / xéo: giãn cách theo X, dùng cùng giá trị khoảng cách đã nhập.
    # Sort theo X để tag không bị đảo thứ tự so với vị trí ống trên mặt bằng.
    non_horizontal_infos = sorted(
        non_horizontal_infos,
        key=lambda info: (
            info['midpoint'].X,
            -info['midpoint'].Y,
            info['pipe'].Id.IntegerValue
        )
    )

    # Nếu chọn lẫn ống ngang và dọc/xéo, đặt nhóm dọc/xéo thấp hơn 1 bước để hạn chế chồng tag.
    non_horizontal_base_y = pick_point.Y
    if horizontal_infos and non_horizontal_infos:
        non_horizontal_base_y = pick_point.Y - step_ft

    for i, info in enumerate(non_horizontal_infos):
        tag_x = pick_point.X + i * step_ft
        info['head_pos'] = XYZ(tag_x, non_horizontal_base_y, info['midpoint'].Z)
        info['layout_axis'] = 'X'
        layout_items.append(info)

    return layout_items


def get_any_model_direction_orientation():
    try:
        return getattr(TagOrientation, 'AnyModelDirection')
    except:
        return None


def unique_orientations(orientations):
    result = []
    for ori in orientations:
        if ori is None:
            continue
        duplicated = False
        for existing in result:
            if existing == ori:
                duplicated = True
                break
        if not duplicated:
            result.append(ori)
    return result


def create_oriented_pipe_tag(doc, view, pipe, tag_type_id, tag_point, angle):
    """Tạo tag và cố gắng xoay tag theo phương ống trên mặt bằng."""
    any_dir = get_any_model_direction_orientation()
    horizontal = is_pipe_horizontal_on_plan(angle)
    vertical = is_pipe_vertical_on_plan(angle)

    if horizontal:
        orientations = [TagOrientation.Horizontal]
    elif vertical:
        orientations = [TagOrientation.Vertical, any_dir, TagOrientation.Horizontal]
    else:
        orientations = [any_dir, TagOrientation.Horizontal, TagOrientation.Vertical]

    last_error = None
    for orientation in unique_orientations(orientations):
        try:
            tag = IndependentTag.Create(
                doc,
                tag_type_id,
                view.Id,
                Reference(pipe),
                True,
                orientation,
                tag_point
            )

            # Với ống dọc/xéo, nếu Revit version/family cho phép thì xoay đúng theo góc ống.
            if angle is not None and not horizontal:
                try:
                    tag.RotationAngle = angle
                except:
                    pass

            return tag
        except Exception as ex:
            last_error = ex

    raise last_error


class PipeSelectionFilter(ISelectionFilter):
    def __init__(self, z_min=None, z_max=None):
        self.z_min = z_min
        self.z_max = z_max

    def AllowElement(self, elem):
        if not is_pipe_element(elem):
            return False

        # Khi có View Range manual/active, chỉ cho pick các ống nằm trong vùng cao độ đó.
        return pipe_intersects_z_range(elem, self.z_min, self.z_max)

    def AllowReference(self, ref, pt):
        return False


class TagPipesWPF(forms.WPFWindow):
    def __init__(self, xaml_file_path):
        forms.WPFWindow.__init__(self, xaml_file_path)

        self.config = script.get_config()
        # Chỉ lưu dữ liệu UI. Mọi thao tác Selection/Transaction của Revit
        # sẽ chạy SAU KHI cửa sổ WPF đã đóng để tránh Recovery/Fatal.
        self.result = None

        self.setup_tag_combobox()
        self.load_settings()
        self.update_view_range_note()
        self.update_manual_view_range_controls()

        self.btnExecute.Click += self.on_execute_click
        self.btnCancel.Click += self.on_close_click

        try:
            self.rbVrNone.Checked += self.on_view_range_mode_changed
            self.rbVrActive.Checked += self.on_view_range_mode_changed
            self.rbVrManual.Checked += self.on_view_range_mode_changed
        except:
            pass

    def load_settings(self):
        self.txtMinLen.Text = getattr(self.config, 'min_len', self.txtMinLen.Text)
        self.txtMinSize.Text = getattr(self.config, 'min_size', self.txtMinSize.Text)
        self.txtMaxSize.Text = getattr(self.config, 'max_size', self.txtMaxSize.Text)
        self.txtStepY.Text = getattr(self.config, 'step_y', self.txtStepY.Text)

        self.txtVrBottom.Text = getattr(self.config, 'vr_bottom_mm', self.txtVrBottom.Text)
        self.txtVrTop.Text = getattr(self.config, 'vr_top_mm', self.txtVrTop.Text)

        scope_selection = getattr(self.config, 'scope_selection', True)
        self.rbSelection.IsChecked = scope_selection
        self.rbView.IsChecked = not scope_selection

        # Ba chế độ điều kiện tag:
        # - all_replace: tag toàn bộ; ống đã có tag sẽ xóa tag cũ rồi tạo lại.
        # - untagged_only: chỉ tag các ống chưa có tag trong active view.
        # - tagged_additional: chỉ tag các ống đã có tag và giữ nguyên tag cũ.
        tag_condition = getattr(self.config, 'tag_condition', None)

        # Tương thích cấu hình cũ chỉ có biến Boolean tag_all_pipes.
        if tag_condition not in ['all_replace', 'untagged_only', 'tagged_additional']:
            legacy_tag_all = getattr(self.config, 'tag_all_pipes', True)
            tag_condition = 'all_replace' if legacy_tag_all else 'untagged_only'

        self.rbAllPipes.IsChecked = tag_condition == 'all_replace'
        self.rbUntaggedOnly.IsChecked = tag_condition == 'untagged_only'
        self.rbTaggedAdditional.IsChecked = tag_condition == 'tagged_additional'

        if (not self.rbAllPipes.IsChecked and
                not self.rbUntaggedOnly.IsChecked and
                not self.rbTaggedAdditional.IsChecked):
            self.rbAllPipes.IsChecked = True

        vr_mode = getattr(self.config, 'view_range_mode', 'none')
        self.rbVrNone.IsChecked = vr_mode == 'none'
        self.rbVrActive.IsChecked = vr_mode == 'active'
        self.rbVrManual.IsChecked = vr_mode == 'manual'

        if not self.rbVrNone.IsChecked and not self.rbVrActive.IsChecked and not self.rbVrManual.IsChecked:
            self.rbVrNone.IsChecked = True

    def setup_tag_combobox(self):
        doc = revit.doc
        pipe_tags = FilteredElementCollector(doc) \
            .OfCategory(BuiltInCategory.OST_PipeTags) \
            .WhereElementIsElementType() \
            .ToElements()

        self.tag_map = {}
        for t in pipe_tags:
            try:
                family_name = t.FamilyName
                type_name = get_elem_name(t)
                display_name = "{} : {}".format(family_name, type_name)
                self.tag_map[display_name] = t.Id
            except:
                continue

        sorted_tags = sorted(self.tag_map.keys())

        self.cbTagHorizontal.ItemsSource = sorted_tags
        self.cbTagNonHorizontal.ItemsSource = sorted_tags

        if self.tag_map:
            saved_horiz = getattr(self.config, 'tag_horiz', None)
            if saved_horiz in sorted_tags:
                self.cbTagHorizontal.SelectedItem = saved_horiz
            else:
                self.cbTagHorizontal.SelectedIndex = 0

            saved_non_horiz = getattr(self.config, 'tag_non_horiz', None)
            if saved_non_horiz in sorted_tags:
                self.cbTagNonHorizontal.SelectedItem = saved_non_horiz
            else:
                self.cbTagNonHorizontal.SelectedIndex = 0

    def on_view_range_mode_changed(self, sender, args):
        self.update_manual_view_range_controls()

    def update_manual_view_range_controls(self):
        try:
            enabled = bool(self.rbVrManual.IsChecked)
            self.txtVrBottom.IsEnabled = enabled
            self.txtVrTop.IsEnabled = enabled
        except:
            pass

    def update_view_range_note(self):
        doc = revit.doc
        active_view = revit.active_view
        level = get_view_level(doc, active_view)

        try:
            if level:
                level_name = get_elem_name(level)
                level_note = "Level gốc manual: {}".format(level_name)
            else:
                level_note = "Không tìm thấy Level gốc của active view"

            view_range = get_active_view_range(doc, active_view)
            if view_range and level:
                bottom_z, top_z = view_range
                bottom_rel = ft_to_mm(bottom_z - level.Elevation)
                top_rel = ft_to_mm(top_z - level.Elevation)
                self.txtVrStatus.Text = "{} | View Range hiện hành: Bottom {:.0f} mm, Top {:.0f} mm".format(level_note, bottom_rel, top_rel)
            else:
                self.txtVrStatus.Text = "{} | Active view không hỗ trợ đọc View Range.".format(level_note)
        except:
            pass

    def get_selected_view_range_mode(self):
        if bool(self.rbVrActive.IsChecked):
            return 'active'
        if bool(self.rbVrManual.IsChecked):
            return 'manual'
        return 'none'

    def get_selected_tag_condition(self):
        if bool(self.rbUntaggedOnly.IsChecked):
            return 'untagged_only'
        if bool(self.rbTaggedAdditional.IsChecked):
            return 'tagged_additional'
        return 'all_replace'

    def save_settings(self):
        self.config.min_len = self.txtMinLen.Text
        self.config.min_size = self.txtMinSize.Text
        self.config.max_size = self.txtMaxSize.Text
        self.config.step_y = self.txtStepY.Text
        self.config.vr_bottom_mm = self.txtVrBottom.Text
        self.config.vr_top_mm = self.txtVrTop.Text
        self.config.view_range_mode = self.get_selected_view_range_mode()
        self.config.scope_selection = bool(self.rbSelection.IsChecked)
        self.config.tag_condition = self.get_selected_tag_condition()
        # Giữ lại biến cũ để cấu hình vẫn tương thích nếu người dùng quay lại bản script cũ.
        self.config.tag_all_pipes = self.config.tag_condition == 'all_replace'
        self.config.tag_horiz = self.cbTagHorizontal.SelectedItem
        self.config.tag_non_horiz = self.cbTagNonHorizontal.SelectedItem
        script.save_config()

    def on_execute_click(self, sender, args):
        """
        Chỉ đọc/kiểm tra dữ liệu giao diện rồi đóng WPF.

        Không gọi PickObjects, PickPoint hoặc mở Transaction trong event WPF.
        Đây là thay đổi quan trọng để tránh Revit Recovery/Fatal khi model đang
        có các ống được preselect trước lúc chạy lệnh.
        """
        try:
            min_len = float(self.txtMinLen.Text)
            min_size = float(self.txtMinSize.Text)
            max_size = float(self.txtMaxSize.Text)
            step_y = float(self.txtStepY.Text)
            vr_bottom_mm = float(self.txtVrBottom.Text)
            vr_top_mm = float(self.txtVrTop.Text)

            if min_len < 0:
                forms.alert("Chiều dài ống tối thiểu không được nhỏ hơn 0.")
                return
            if min_size < 0 or max_size < 0:
                forms.alert("Đường kính ống Min/Max không được nhỏ hơn 0.")
                return
            if min_size > max_size:
                forms.alert("Đường kính ống Min không được lớn hơn đường kính Max.")
                return
            if step_y <= 0:
                forms.alert("Khoảng cách giữa các dòng tag phải lớn hơn 0.")
                return

            selected_tag_horiz = self.cbTagHorizontal.SelectedItem
            selected_tag_non_horiz = self.cbTagNonHorizontal.SelectedItem
            if not selected_tag_horiz or not selected_tag_non_horiz:
                forms.alert("Vui lòng chọn đầy đủ 2 loại Pipe Tag trước khi thực hiện.")
                return

            self.save_settings()

            self.result = {
                'min_len': min_len,
                'min_size': min_size,
                'max_size': max_size,
                'step_y': step_y,
                'vr_bottom_mm': vr_bottom_mm,
                'vr_top_mm': vr_top_mm,
                'view_range_mode': self.get_selected_view_range_mode(),
                'scope_selection': bool(self.rbSelection.IsChecked),
                'tag_condition': self.get_selected_tag_condition(),
                'tag_horiz_id': self.tag_map[selected_tag_horiz],
                'tag_non_horiz_id': self.tag_map[selected_tag_non_horiz],
            }
            self.Close()

        except ValueError:
            forms.alert("Vui lòng kiểm tra lại. Các ô thông số chỉ được nhập số hợp lệ.")
        except Exception as ex:
            self.result = None
            forms.alert("Lỗi giao diện: {}".format(ex))
            try:
                print(traceback.format_exc())
            except:
                pass
            self.Close()

    def get_tagged_pipe_dict(self, doc, active_view):
        rvt_year = int(doc.Application.VersionNumber)
        existing_tags = FilteredElementCollector(doc, active_view.Id) \
            .OfClass(IndependentTag) \
            .ToElements()

        tagged_dict = {}
        for tag in existing_tags:
            if tag.Category and tag.Category.Id.IntegerValue == TAG_CAT_ID:
                try:
                    if rvt_year >= 2022:
                        for ref in tag.GetTaggedReferences():
                            pid = ref.ElementId
                            if pid not in tagged_dict:
                                tagged_dict[pid] = []
                            tagged_dict[pid].append(tag.Id)
                    else:
                        pid = tag.TaggedLocalElementId
                        if pid not in tagged_dict:
                            tagged_dict[pid] = []
                        tagged_dict[pid].append(tag.Id)
                except:
                    pass
        return tagged_dict

    def is_valid_pipe_for_tagging(self, pipe, tagged_dict, tag_condition, min_length_ft, min_size_ft, max_size_ft, z_min, z_max):
        if not is_pipe_element(pipe):
            return False

        if not pipe_intersects_z_range(pipe, z_min, z_max):
            return False

        has_existing_tag = pipe.Id in tagged_dict

        if tag_condition == 'untagged_only' and has_existing_tag:
            return False

        # Option thứ 3: chỉ lấy ống đã có tag trong active view để tạo thêm tag mới.
        if tag_condition == 'tagged_additional' and not has_existing_tag:
            return False

        curve = get_curve(pipe)
        if not curve:
            return False

        # Bỏ qua ống đứng thật sự theo trục Z vì trong mặt bằng không có hướng XY để xoay tag.
        angle = get_pipe_angle_xy(curve)
        if angle is None:
            return False

        len_param = pipe.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH)
        if not len_param or len_param.AsDouble() < min_length_ft:
            return False

        size_param = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
        if size_param:
            pipe_size_ft = size_param.AsDouble()
            if pipe_size_ft < min_size_ft or pipe_size_ft > max_size_ft:
                return False

        return True

    def execute_tagging(self, min_len, min_size, max_size, pick_point, step_y, tag_condition,
                        tag_horiz_id, tag_non_horiz_id, raw_elements, z_min, z_max, view_range_mode):
        doc = revit.doc
        active_view = revit.active_view

        min_length_ft = mm_to_ft(min_len)
        min_size_ft = mm_to_ft(min_size)
        max_size_ft = mm_to_ft(max_size)
        step_y_ft = mm_to_ft(step_y)

        tag_horiz_elem = doc.GetElement(tag_horiz_id)
        tag_non_horiz_elem = doc.GetElement(tag_non_horiz_id)
        tagged_dict = self.get_tagged_pipe_dict(doc, active_view)

        valid_pipes = []
        for p in raw_elements:
            if self.is_valid_pipe_for_tagging(p, tagged_dict, tag_condition,
                                              min_length_ft, min_size_ft, max_size_ft,
                                              z_min, z_max):
                valid_pipes.append(p)

        if not valid_pipes:
            if tag_condition == 'tagged_additional':
                base_message = "Không tìm thấy ống đã có tag nào thỏa mãn điều kiện lọc."
            elif tag_condition == 'untagged_only':
                base_message = "Không tìm thấy ống chưa có tag nào thỏa mãn điều kiện lọc."
            else:
                base_message = "Không tìm thấy ống nào thỏa mãn điều kiện lọc hiện tại."

            if view_range_mode != 'none':
                base_message += "\nĐã áp dụng giới hạn View Range được chọn."

            forms.alert(base_message)
            return

        layout_items = build_tag_layout_items(valid_pipes, pick_point, step_y_ft)

        if not layout_items:
            forms.alert("Không tạo được layout tag cho các ống đã chọn.")
            return

        with Transaction(doc, "Tag Pipe With View Range") as t:
            t.Start()

            if not tag_horiz_elem.IsActive:
                tag_horiz_elem.Activate()
            if not tag_non_horiz_elem.IsActive:
                tag_non_horiz_elem.Activate()

            success_count = 0
            deleted_count = 0
            skipped_count = 0
            x_layout_count = 0
            y_layout_count = 0

            for info in layout_items:
                pipe = info['pipe']
                midpoint = info['midpoint']
                angle = info['angle']
                head_pos = info['head_pos']

                try:
                    # Chỉ option "Tag toàn bộ / cập nhật tag cũ" mới xóa tag cũ.
                    # Option "Tag tiếp vào ống đã có tag" giữ nguyên toàn bộ tag hiện hữu.
                    if tag_condition == 'all_replace' and pipe.Id in tagged_dict:
                        for old_tag_id in tagged_dict[pipe.Id]:
                            try:
                                doc.Delete(old_tag_id)
                                deleted_count += 1
                            except:
                                pass

                    if info['is_horizontal']:
                        current_tag_id = tag_horiz_id
                        y_layout_count += 1
                    else:
                        current_tag_id = tag_non_horiz_id
                        x_layout_count += 1

                    tag = create_oriented_pipe_tag(doc, active_view, pipe, current_tag_id, midpoint, angle)

                    try:
                        tag.LeaderEndCondition = LeaderEndCondition.Attached
                    except:
                        pass

                    tag.TagHeadPosition = head_pos

                    # Sau khi đưa tag head về hàng, set lại rotation lần nữa để tránh một số family bị reset.
                    if angle is not None and not is_pipe_horizontal_on_plan(angle):
                        try:
                            tag.RotationAngle = angle
                        except:
                            pass

                    success_count += 1
                except Exception as e_loop:
                    skipped_count += 1
                    print("Lỗi tại ống ID {}: {}".format(pipe.Id, e_loop))

            t.Commit()

        msg = "Đã đặt thành công {} tag ống.".format(success_count)
        if deleted_count > 0:
            msg += "\nĐã xóa/cập nhật {} tag cũ.".format(deleted_count)
        if tag_condition == 'tagged_additional' and success_count > 0:
            msg += "\nĐã giữ nguyên tag cũ và đặt thêm tag mới cho các ống đã có tag."
        if skipped_count > 0:
            msg += "\nBỏ qua {} ống do lỗi tạo/xoay tag.".format(skipped_count)
        if x_layout_count > 0:
            msg += "\nỐng dọc Y / ống xéo: đã giãn cách tag theo phương X."
        if y_layout_count > 0:
            msg += "\nỐng ngang X: đã giãn cách tag theo phương Y."
        if view_range_mode == 'manual':
            msg += "\nĐã áp dụng View Range thủ công."
        elif view_range_mode == 'active':
            msg += "\nĐã áp dụng View Range hiện hành của active view."

        forms.toast(msg)

    def on_close_click(self, sender, args):
        self.result = None
        self.Close()


def resolve_z_filter(doc, active_view, options):
    """Tính View Range sau khi WPF đã đóng."""
    mode = options['view_range_mode']

    if mode == 'none':
        return None, None

    if mode == 'active':
        z_range = get_active_view_range(doc, active_view)
        if not z_range:
            forms.alert(
                "Active view hiện hành không đọc được View Range.\n"
                "Vui lòng chuyển sang view mặt bằng hoặc chọn 'Không lọc View Range'."
            )
            return None
        return z_range[0], z_range[1]

    z_range = get_manual_view_range(
        doc,
        active_view,
        options['vr_bottom_mm'],
        options['vr_top_mm']
    )
    if not z_range:
        forms.alert("Không tìm thấy Level của active view để tính View Range thủ công.")
        return None
    return z_range[0], z_range[1]


def get_preselected_pipes(doc, uidoc, z_min, z_max):
    """
    Đọc Selection hiện có trước khi gọi tool.
    Chỉ lấy Pipe hợp lệ và loại trùng ElementId.
    """
    result = []
    seen_ids = set()

    try:
        selected_ids = uidoc.Selection.GetElementIds()
    except:
        selected_ids = []

    for elem_id in selected_ids:
        try:
            elem = doc.GetElement(elem_id)
            if not is_pipe_element(elem):
                continue
            if not pipe_intersects_z_range(elem, z_min, z_max):
                continue

            int_id = elem.Id.IntegerValue
            if int_id in seen_ids:
                continue

            seen_ids.add(int_id)
            result.append(elem)
        except:
            continue

    return result


def pick_or_collect_pipes(doc, uidoc, active_view, options, z_min, z_max):
    """
    Quy tắc lựa chọn:
    1. Nếu chế độ Selection và đã chọn ống trước khi chạy tool: dùng trực tiếp.
    2. Nếu chưa preselect ống: mới gọi PickObjects.
    3. Nếu chế độ View: thu thập toàn bộ Pipe trong active view.
    """
    if not options['scope_selection']:
        return FilteredElementCollector(doc, active_view.Id) \
            .OfCategory(BuiltInCategory.OST_PipeCurves) \
            .WhereElementIsNotElementType() \
            .ToElements()

    preselected_pipes = get_preselected_pipes(doc, uidoc, z_min, z_max)
    if preselected_pipes:
        return preselected_pipes

    mode = options['view_range_mode']
    if mode == 'manual':
        pick_msg = (
            "Chưa có ống được chọn trước. Chỉ chọn được ống nằm trong "
            "View Range thủ công -> Nhấn Finish."
        )
    elif mode == 'active':
        pick_msg = (
            "Chưa có ống được chọn trước. Chỉ chọn được ống nằm trong "
            "View Range hiện hành -> Nhấn Finish."
        )
    else:
        pick_msg = "Chưa có ống được chọn trước. Quét/click chọn ống -> Nhấn Finish."

    pipe_refs = uidoc.Selection.PickObjects(
        ObjectType.Element,
        PipeSelectionFilter(z_min, z_max),
        pick_msg
    )

    result = []
    seen_ids = set()
    for ref in pipe_refs:
        try:
            elem = doc.GetElement(ref.ElementId)
            if not is_pipe_element(elem):
                continue
            int_id = elem.Id.IntegerValue
            if int_id in seen_ids:
                continue
            seen_ids.add(int_id)
            result.append(elem)
        except:
            continue

    return result


def run_tool():
    doc = revit.doc
    uidoc = revit.uidoc
    active_view = revit.active_view

    win = TagPipesWPF(xaml_file)
    win.show_dialog()

    options = win.result
    if not options:
        return

    z_filter = resolve_z_filter(doc, active_view, options)
    if z_filter is None:
        return
    z_min, z_max = z_filter

    try:
        raw_elements = pick_or_collect_pipes(
            doc,
            uidoc,
            active_view,
            options,
            z_min,
            z_max
        )

        if not raw_elements:
            forms.alert("Không có ống nào được chọn để đặt tag.")
            return

        # Chỉ gọi PickPoint sau khi WPF đã đóng hoàn toàn.
        pick_point = uidoc.Selection.PickPoint(
            "Click chọn vị trí ĐÁY của chùm tag"
        )

    except OperationCanceledException:
        return

    try:
        win.execute_tagging(
            options['min_len'],
            options['min_size'],
            options['max_size'],
            pick_point,
            options['step_y'],
            options['tag_condition'],
            options['tag_horiz_id'],
            options['tag_non_horiz_id'],
            raw_elements,
            z_min,
            z_max,
            options['view_range_mode']
        )
    except Exception as ex:
        forms.alert("Lỗi hệ thống: {}".format(ex))
        try:
            print(traceback.format_exc())
        except:
            pass


if __name__ == "__main__":
    run_tool()
