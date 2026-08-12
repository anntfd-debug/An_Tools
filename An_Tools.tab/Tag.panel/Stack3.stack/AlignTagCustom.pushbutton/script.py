# -*- coding: utf-8 -*-

from pyrevit import revit, forms
from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Windows.Input import Keyboard, ModifierKeys
from System.Windows import SystemParameters

import os
import re
import json
import math


doc = revit.doc
uidoc = revit.uidoc

SCRIPT_DIR = os.path.dirname(__file__)
XAML_PATH = os.path.join(SCRIPT_DIR, "ui.xaml")
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")


# ============================================================
# Units
# ============================================================

def mm_to_ft(value_mm):
    return float(value_mm) / 304.8


def parse_mm(text_value):
    if not text_value:
        return 0.0

    text_value = str(text_value)
    text_value = text_value.replace(",", ".")

    match = re.search(r"[-+]?\d*\.?\d+", text_value)

    if not match:
        return 0.0

    return float(match.group())


def format_mm(value):
    try:
        number_value = float(value)

        if number_value == int(number_value):
            return str(int(number_value)) + " mm"

        return str(number_value) + " mm"

    except:
        return "10 mm"


def format_degree(value):
    try:
        number_value = float(value)

        if number_value == int(number_value):
            return str(int(number_value)) + " deg"

        return str(number_value) + " deg"

    except:
        return "45 deg"


# ============================================================
# Settings
# ============================================================

DEFAULT_SETTINGS = {
    "shoulder_mm": 10,
    "elbow_mm": 10,
    "shoulder_enabled": True,
    "elbow_enabled": True,
    "tag_spacing_mm": 50,
    "custom_angle_enabled": False,
    "custom_angle_deg": 45,
    "mode": "Customize",
    "leader_types": ["90UD"],
    "distribute_axis": "X"
}


def get_default_settings():
    data = {}
    data["shoulder_mm"] = DEFAULT_SETTINGS["shoulder_mm"]
    data["elbow_mm"] = DEFAULT_SETTINGS["elbow_mm"]
    data["shoulder_enabled"] = DEFAULT_SETTINGS["shoulder_enabled"]
    data["elbow_enabled"] = DEFAULT_SETTINGS["elbow_enabled"]
    data["tag_spacing_mm"] = DEFAULT_SETTINGS["tag_spacing_mm"]
    data["custom_angle_enabled"] = DEFAULT_SETTINGS["custom_angle_enabled"]
    data["custom_angle_deg"] = DEFAULT_SETTINGS["custom_angle_deg"]
    data["mode"] = DEFAULT_SETTINGS["mode"]
    data["leader_types"] = list(DEFAULT_SETTINGS["leader_types"])
    data["distribute_axis"] = DEFAULT_SETTINGS["distribute_axis"]
    return data


def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        return get_default_settings()

    try:
        f = open(SETTINGS_PATH, "r")
        data = json.load(f)
        f.close()

        settings = get_default_settings()

        for key in settings.keys():
            if key in data:
                settings[key] = data[key]

        return settings

    except:
        return get_default_settings()


def save_settings(settings):
    try:
        f = open(SETTINGS_PATH, "w")
        json.dump(settings, f, indent=4)
        f.close()
    except:
        pass


# ============================================================
# Vector helpers
# ============================================================

def normalize_vector(vector_value):
    try:
        return vector_value.Normalize()
    except:
        return vector_value


def sign_from_dot(vector_value, axis_value):
    try:
        dot_value = vector_value.DotProduct(axis_value)

        if dot_value >= 0:
            return 1

        return -1

    except:
        return 1


def get_view_axes():
    active_view = doc.ActiveView

    right_axis = normalize_vector(active_view.RightDirection)
    up_axis = normalize_vector(active_view.UpDirection)

    return right_axis, up_axis


def get_view_direction():
    try:
        return normalize_vector(doc.ActiveView.ViewDirection)
    except:
        return XYZ(0, 0, 1)


def point_from_view_scalars(reference_point, right_axis, up_axis, right_scalar, up_scalar):
    """
    Rebuild a point from view X/Y scalar coordinates and keep the same
    depth on the active view direction.

    This avoids small diagonal errors when making 90-degree leaders.
    """

    view_direction = get_view_direction()
    depth_scalar = reference_point.DotProduct(view_direction)

    return (
        right_axis.Multiply(right_scalar)
        + up_axis.Multiply(up_scalar)
        + view_direction.Multiply(depth_scalar)
    )


# ============================================================
# Selection filter
# ============================================================

class IndependentTagSelectionFilter(ISelectionFilter):

    def AllowElement(self, element):
        if isinstance(element, IndependentTag):
            return True

        return False

    def AllowReference(self, reference, position):
        return False


def is_shift_pressed():
    try:
        if Keyboard.Modifiers & ModifierKeys.Shift:
            return True

        return False

    except:
        return False


def pick_tags_after_apply(allow_multiple):
    tag_filter = IndependentTagSelectionFilter()
    tags = []

    if allow_multiple:
        picked_refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            tag_filter,
            "Select tags. Click Finish to apply this batch. Press Esc to exit."
        )

        for picked_ref in picked_refs:
            element = doc.GetElement(picked_ref.ElementId)

            if isinstance(element, IndependentTag):
                tags.append(element)

        return tags

    picked_ref = uidoc.Selection.PickObject(
        ObjectType.Element,
        tag_filter,
        "Select one tag. Hold Shift before selecting to use multiple selection. Press Esc to exit."
    )

    element = doc.GetElement(picked_ref.ElementId)

    if isinstance(element, IndependentTag):
        tags.append(element)

    return tags


# ============================================================
# Tag helpers
# ============================================================

def get_first_tag_reference(tag):
    try:
        refs = list(tag.GetTaggedReferences())

        if len(refs) > 0:
            return refs[0]

    except:
        pass

    return None


def ensure_free_leader(tag):
    try:
        tag.LeaderEndCondition = LeaderEndCondition.Free
    except:
        pass


def get_tag_current_direction_signs(tag, ref, right_axis, up_axis):
    try:
        leader_end = tag.GetLeaderEnd(ref)
        tag_head = tag.TagHeadPosition

        vector_value = tag_head - leader_end

        right_sign = sign_from_dot(vector_value, right_axis)
        up_sign = sign_from_dot(vector_value, up_axis)

        return right_sign, up_sign

    except:
        return 1, 1


# ============================================================
# Geometry
# ============================================================

def get_current_leader_geometry(tag, ref):
    leader_end = tag.GetLeaderEnd(ref)
    tag_head = tag.TagHeadPosition

    try:
        leader_elbow = tag.GetLeaderElbow(ref)
    except:
        # A completely straight leader may not expose an elbow yet.
        # Treat its current elbow length as zero instead of inventing a value.
        leader_elbow = leader_end

    return leader_end, leader_elbow, tag_head


def get_planar_length(vector_value, right_axis, up_axis):
    x_value = vector_value.DotProduct(right_axis)
    y_value = vector_value.DotProduct(up_axis)
    return math.sqrt(x_value * x_value + y_value * y_value)


def get_axis_sign(vector_value, axis_value, fallback_sign):
    try:
        scalar_value = vector_value.DotProduct(axis_value)

        if abs(scalar_value) > 0.0000001:
            if scalar_value >= 0:
                return 1

            return -1

    except:
        pass

    return fallback_sign


def calculate_leader_points(
        tag,
        ref,
        leader_type,
        shoulder_ft,
        elbow_ft,
        right_axis,
        up_axis,
        use_custom_angle,
        custom_angle_deg,
        use_shoulder_length,
        use_elbow_length):

    leader_end, current_elbow, current_head = get_current_leader_geometry(tag, ref)

    fallback_right_sign, fallback_up_sign = get_tag_current_direction_signs(
        tag,
        ref,
        right_axis,
        up_axis
    )

    current_elbow_vector = current_elbow - leader_end
    current_shoulder_vector = current_head - current_elbow

    current_elbow_length = get_planar_length(
        current_elbow_vector,
        right_axis,
        up_axis
    )
    current_shoulder_length = get_planar_length(
        current_shoulder_vector,
        right_axis,
        up_axis
    )

    elbow_right_sign = get_axis_sign(
        current_elbow_vector,
        right_axis,
        fallback_right_sign
    )
    elbow_up_sign = get_axis_sign(
        current_elbow_vector,
        up_axis,
        fallback_up_sign
    )
    shoulder_right_sign = get_axis_sign(
        current_shoulder_vector,
        right_axis,
        fallback_right_sign
    )
    shoulder_up_sign = get_axis_sign(
        current_shoulder_vector,
        up_axis,
        fallback_up_sign
    )

    target_shoulder_ft = shoulder_ft

    if not use_shoulder_length:
        target_shoulder_ft = current_shoulder_length

    target_elbow_ft = elbow_ft

    if not use_elbow_length:
        target_elbow_ft = current_elbow_length

    # Work in active-view X/Y scalar coordinates.
    end_x = leader_end.DotProduct(right_axis)
    end_y = leader_end.DotProduct(up_axis)

    elbow_x = end_x
    elbow_y = end_y
    head_x = end_x
    head_y = end_y

    if leader_type == "90UD":
        elbow_x = end_x
        elbow_y = end_y + elbow_up_sign * target_elbow_ft
        head_x = elbow_x + shoulder_right_sign * target_shoulder_ft
        head_y = elbow_y

    elif leader_type == "90LR":
        elbow_x = end_x + elbow_right_sign * target_elbow_ft
        elbow_y = end_y
        head_x = elbow_x
        head_y = elbow_y + shoulder_up_sign * target_shoulder_ft

    elif leader_type == "45UD" or leader_type == "45LR":
        angle_deg = 45.0

        if use_custom_angle:
            angle_deg = custom_angle_deg

        angle_rad = math.radians(angle_deg)

        if use_elbow_length:
            # Keep the original behavior: the entered Elbow Length is the
            # horizontal run used to build the diagonal segment.
            diagonal_x = target_elbow_ft
            diagonal_y = target_elbow_ft * math.tan(angle_rad)
        else:
            # The unchecked value means keep the actual current diagonal
            # segment length, even when the selected angle is rebuilt.
            diagonal_x = target_elbow_ft * math.cos(angle_rad)
            diagonal_y = target_elbow_ft * math.sin(angle_rad)

        diagonal_right_sign = elbow_right_sign
        diagonal_up_sign = elbow_up_sign

        elbow_x = end_x + diagonal_right_sign * diagonal_x
        elbow_y = end_y + diagonal_up_sign * diagonal_y

        if leader_type == "45UD":
            head_x = elbow_x + shoulder_right_sign * target_shoulder_ft
            head_y = elbow_y
        else:
            head_x = elbow_x
            head_y = elbow_y + shoulder_up_sign * target_shoulder_ft

    elif leader_type == "STRAIGHTUD":
        straight_up_sign = get_axis_sign(
            current_head - leader_end,
            up_axis,
            fallback_up_sign
        )
        elbow_x = end_x
        elbow_y = end_y + straight_up_sign * target_elbow_ft
        head_x = end_x
        head_y = elbow_y + straight_up_sign * target_shoulder_ft

    elif leader_type == "STRAIGHTLR":
        straight_right_sign = get_axis_sign(
            current_head - leader_end,
            right_axis,
            fallback_right_sign
        )
        elbow_x = end_x + straight_right_sign * target_elbow_ft
        elbow_y = end_y
        head_x = elbow_x + straight_right_sign * target_shoulder_ft
        head_y = end_y

    else:
        elbow_x = end_x
        elbow_y = end_y + elbow_up_sign * target_elbow_ft
        head_x = elbow_x + shoulder_right_sign * target_shoulder_ft
        head_y = elbow_y

    elbow_pt = point_from_view_scalars(
        leader_end,
        right_axis,
        up_axis,
        elbow_x,
        elbow_y
    )
    head_pt = point_from_view_scalars(
        leader_end,
        right_axis,
        up_axis,
        head_x,
        head_y
    )

    return elbow_pt, head_pt


def get_auto_leader_type(tag):
    try:
        ref = get_first_tag_reference(tag)

        if ref is None:
            return "90UD"

        leader_end = tag.GetLeaderEnd(ref)
        tag_head = tag.TagHeadPosition

        right_axis, up_axis = get_view_axes()
        vector_value = tag_head - leader_end

        x_len = abs(vector_value.DotProduct(right_axis))
        y_len = abs(vector_value.DotProduct(up_axis))

        if x_len >= y_len:
            return "90UD"

        return "90LR"

    except:
        return "90UD"


def get_scalar_on_axis(point_value, axis_value):
    return point_value.DotProduct(axis_value)


def distribute_items_by_spacing(items, spacing_ft, axis_mode, right_axis, up_axis):
    """
    Distribute tag heads by spacing and align the perpendicular coordinate.

    If axis_mode == "X":
        - Space tags along view X direction.
        - Force all tag heads to have the same view Y coordinate.
        - Result: tag text boxes align horizontally.

    If axis_mode == "Y":
        - Space tags along view Y direction.
        - Force all tag heads to have the same view X coordinate.
        - Result: tag text boxes align vertically.

    TagHeadPosition is used as the center point of tag text box.
    """

    if len(items) < 2:
        return items

    if spacing_ft <= 0:
        return items

    # --------------------------------------------------------
    # Define spacing axis and lock axis
    # --------------------------------------------------------
    if axis_mode == "Y":
        # Spacing theo phuong Y, khoa toa do X
        spacing_axis = up_axis
        lock_axis = right_axis
    else:
        # Spacing theo phuong X, khoa toa do Y
        spacing_axis = right_axis
        lock_axis = up_axis

    # --------------------------------------------------------
    # Sort tags by current position on spacing axis
    # --------------------------------------------------------
    sorted_items = sorted(
        items,
        key=lambda item: get_scalar_on_axis(item["head"], spacing_axis)
    )

    # --------------------------------------------------------
    # Base point:
    # Lay tag dau tien sau khi sort lam moc.
    # Tag dau giu scalar tren truc spacing va truc lock.
    # --------------------------------------------------------
    base_head = sorted_items[0]["head"]

    base_spacing_scalar = get_scalar_on_axis(base_head, spacing_axis)
    base_lock_scalar = get_scalar_on_axis(base_head, lock_axis)

    # --------------------------------------------------------
    # Re-position each tag
    # --------------------------------------------------------
    for index, item in enumerate(sorted_items):

        current_head = item["head"]

        current_spacing_scalar = get_scalar_on_axis(current_head, spacing_axis)
        current_lock_scalar = get_scalar_on_axis(current_head, lock_axis)

        target_spacing_scalar = base_spacing_scalar + spacing_ft * index
        target_lock_scalar = base_lock_scalar

        delta_spacing = target_spacing_scalar - current_spacing_scalar
        delta_lock = target_lock_scalar - current_lock_scalar

        delta_vector = spacing_axis.Multiply(delta_spacing) + lock_axis.Multiply(delta_lock)

        # Move both tag head and elbow together to keep leader shape
        item["head"] = item["head"] + delta_vector
        item["elbow"] = item["elbow"] + delta_vector

    return sorted_items


def process_tags(
        tags,
        selected_types,
        use_auto,
        shoulder_ft,
        elbow_ft,
        spacing_ft,
        axis_mode,
        use_custom_angle,
        custom_angle_deg,
        use_shoulder_length,
        use_elbow_length):
    right_axis, up_axis = get_view_axes()

    items = []
    failed_messages = []

    with revit.Transaction("Align Tag Leader"):

        for index, tag in enumerate(tags):

            try:
                if not tag.HasLeader:
                    tag.HasLeader = True
            except:
                pass

            ref = get_first_tag_reference(tag)

            if ref is None:
                failed_messages.append(
                    "Tag Id " + str(tag.Id.IntegerValue) + ": Can not get tag reference."
                )
                continue

            ensure_free_leader(tag)

            if use_auto:
                leader_type = get_auto_leader_type(tag)
            else:
                if len(selected_types) == 1:
                    leader_type = selected_types[0]
                else:
                    leader_type = selected_types[index % len(selected_types)]

            try:
                elbow_pt, head_pt = calculate_leader_points(
                    tag,
                    ref,
                    leader_type,
                    shoulder_ft,
                    elbow_ft,
                    right_axis,
                    up_axis,
                    use_custom_angle,
                    custom_angle_deg,
                    use_shoulder_length,
                    use_elbow_length
                )

                item = {}
                item["tag"] = tag
                item["ref"] = ref
                item["leader_type"] = leader_type
                item["elbow"] = elbow_pt
                item["head"] = head_pt
                items.append(item)

            except Exception as ex:
                failed_messages.append(
                    "Tag Id " + str(tag.Id.IntegerValue) + ": " + str(ex)
                )

        # Exact spacing and fixed leader lengths are geometrically
        # incompatible when each tag has a different fixed leader end.
        # In partial-length mode, prioritize the user's request to preserve
        # the unchecked segment and skip distribution.
        if len(items) >= 2 and use_shoulder_length and use_elbow_length:
            items = distribute_items_by_spacing(
                items,
                spacing_ft,
                axis_mode,
                right_axis,
                up_axis
            )

        # Set tag heads first, regenerate, then set elbows.
        # If SetLeaderElbow is called before TagHeadPosition, Revit can slightly
        # recalculate the elbow while moving the tag head, creating a non-90 angle.
        for item in items:
            try:
                item["tag"].TagHeadPosition = item["head"]

            except Exception as ex:
                failed_messages.append(
                    "Tag Id " + str(item["tag"].Id.IntegerValue) + ": " + str(ex)
                )

        try:
            doc.Regenerate()
        except:
            pass

        for item in items:
            try:
                item["tag"].SetLeaderElbow(item["ref"], item["elbow"])

            except Exception as ex:
                failed_messages.append(
                    "Tag Id " + str(item["tag"].Id.IntegerValue) + ": " + str(ex)
                )

        # One more regeneration helps Revit commit the tag-head/leader relation.
        # Then re-apply the elbow for 90-degree types to keep the corner exact.
        try:
            doc.Regenerate()
        except:
            pass

        for item in items:
            try:
                if item["leader_type"] == "90UD" or item["leader_type"] == "90LR":
                    item["tag"].SetLeaderElbow(item["ref"], item["elbow"])

            except Exception as ex:
                failed_messages.append(
                    "Tag Id " + str(item["tag"].Id.IntegerValue) + ": " + str(ex)
                )

    return failed_messages


# ============================================================
# Window
# ============================================================

class AlignTagLeaderWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        self.btnApply.Click += self.apply_click
        self.btnCancel.Click += self.cancel_click

        self.cb90UD.Click += self.checkbox_click
        self.cb90LR.Click += self.checkbox_click
        self.cb45UD.Click += self.checkbox_click
        self.cb45LR.Click += self.checkbox_click
        self.cbStraightUD.Click += self.checkbox_click
        self.cbStraightLR.Click += self.checkbox_click

        self.cbDistributeX.Click += self.axis_x_click
        self.cbDistributeY.Click += self.axis_y_click
        self.cbCustomAngle.Click += self.custom_angle_click
        self.cbUseShoulderLength.Click += self.length_checkbox_click
        self.cbUseElbowLength.Click += self.length_checkbox_click

        # Mo rong giao dien theo kich thuoc man hinh, nhung khong vuot qua
        # vung lam viec cua Windows. ScrollViewer trong XAML van duoc giu lai
        # de hien thanh cuon khi man hinh nho hoac user thu nho cua so.
        self.configure_window_size()
        self.load_ui_settings()

    def configure_window_size(self):
        """
        Dat kich thuoc cua so lon de hien du thong tin tren man hinh thong dung.
        Neu man hinh nho hon, cua so tu gioi han trong WorkArea va thanh cuon
        ngang/doc cua ScrollViewer se tiep tuc hoat dong.
        """
        try:
            work_area = SystemParameters.WorkArea

            screen_margin = 36.0
            available_width = max(900.0, work_area.Width - screen_margin)
            available_height = max(600.0, work_area.Height - screen_margin)

            self.MaxWidth = available_width
            self.MaxHeight = available_height

            self.Width = min(1680.0, available_width)
            self.Height = min(850.0, available_height)

        except:
            # Neu khong doc duoc WorkArea, su dung kich thuoc khai bao trong XAML.
            pass

    def load_ui_settings(self):
        settings = load_settings()

        self.tbShoulder.Text = format_mm(settings["shoulder_mm"])
        self.tbElbow.Text = format_mm(settings["elbow_mm"])
        self.cbUseShoulderLength.IsChecked = bool(settings["shoulder_enabled"])
        self.cbUseElbowLength.IsChecked = bool(settings["elbow_enabled"])
        self.tbTagSpacing.Text = format_mm(settings["tag_spacing_mm"])
        self.cbCustomAngle.IsChecked = bool(settings["custom_angle_enabled"])
        self.tbCustomAngle.Text = format_degree(settings["custom_angle_deg"])
        self.update_custom_angle_state()
        self.update_length_input_states()

        if settings["mode"] == "Auto":
            self.rbAuto.IsChecked = True
            self.rbCustomize.IsChecked = False
        else:
            self.rbAuto.IsChecked = False
            self.rbCustomize.IsChecked = True

        leader_types = settings["leader_types"]

        self.cb90UD.IsChecked = "90UD" in leader_types
        self.cb90LR.IsChecked = "90LR" in leader_types
        self.cb45UD.IsChecked = "45UD" in leader_types
        self.cb45LR.IsChecked = "45LR" in leader_types
        self.cbStraightUD.IsChecked = "STRAIGHTUD" in leader_types
        self.cbStraightLR.IsChecked = "STRAIGHTLR" in leader_types

        axis_mode = settings["distribute_axis"]

        if axis_mode == "Y":
            self.cbDistributeX.IsChecked = False
            self.cbDistributeY.IsChecked = True
        else:
            self.cbDistributeX.IsChecked = True
            self.cbDistributeY.IsChecked = False

    def save_ui_settings(
            self,
            shoulder_mm,
            elbow_mm,
            spacing_mm,
            custom_angle_enabled,
            custom_angle_deg,
            shoulder_enabled,
            elbow_enabled):
        mode_value = "Customize"

        if self.rbAuto.IsChecked:
            mode_value = "Auto"

        axis_mode = "X"

        if self.cbDistributeY.IsChecked:
            axis_mode = "Y"

        settings = {}
        settings["shoulder_mm"] = shoulder_mm
        settings["elbow_mm"] = elbow_mm
        settings["shoulder_enabled"] = shoulder_enabled
        settings["elbow_enabled"] = elbow_enabled
        settings["tag_spacing_mm"] = spacing_mm
        settings["custom_angle_enabled"] = custom_angle_enabled
        settings["custom_angle_deg"] = custom_angle_deg
        settings["mode"] = mode_value
        settings["leader_types"] = self.get_checked_leader_types()
        settings["distribute_axis"] = axis_mode

        save_settings(settings)

    def update_custom_angle_state(self):
        enabled = False

        try:
            enabled = bool(self.cbCustomAngle.IsChecked)
        except:
            enabled = False

        self.tbCustomAngle.IsEnabled = enabled

        if enabled:
            self.tbCustomAngle.Opacity = 1.0
        else:
            self.tbCustomAngle.Opacity = 0.55

    def update_length_input_states(self):
        shoulder_enabled = bool(self.cbUseShoulderLength.IsChecked)
        elbow_enabled = bool(self.cbUseElbowLength.IsChecked)

        self.tbShoulder.IsEnabled = shoulder_enabled
        self.tbElbow.IsEnabled = elbow_enabled

        if shoulder_enabled:
            self.tbShoulder.Opacity = 1.0
        else:
            self.tbShoulder.Opacity = 0.55

        if elbow_enabled:
            self.tbElbow.Opacity = 1.0
        else:
            self.tbElbow.Opacity = 0.55

    def length_checkbox_click(self, sender, args):
        shoulder_enabled = bool(self.cbUseShoulderLength.IsChecked)
        elbow_enabled = bool(self.cbUseElbowLength.IsChecked)

        # Always keep at least one length active. The requested mode is either
        # both lengths, shoulder only, or elbow only.
        if not shoulder_enabled and not elbow_enabled:
            sender.IsChecked = True

        self.update_length_input_states()

    def custom_angle_click(self, sender, args):
        self.update_custom_angle_state()

    def cancel_click(self, sender, args):
        self.Close()

    def axis_x_click(self, sender, args):
        self.cbDistributeX.IsChecked = True
        self.cbDistributeY.IsChecked = False

    def axis_y_click(self, sender, args):
        self.cbDistributeX.IsChecked = False
        self.cbDistributeY.IsChecked = True

    def checkbox_click(self, sender, args):
        """
        Chi cho phep chon duy nhat 1 leader type.
        Chon checkbox nay thi cac checkbox khac tu dong tat.
        Neu user bo tick checkbox dang chon, tool se tick lai de dam bao luon co 1 type.
        """

        all_checkboxes = [
            self.cb90UD,
            self.cb90LR,
            self.cb45UD,
            self.cb45LR,
            self.cbStraightUD,
            self.cbStraightLR
        ]

        # Neu user dang tick vao checkbox nay
        if sender.IsChecked:
            for cb in all_checkboxes:
                if cb != sender:
                    cb.IsChecked = False

        # Neu user bo tick checkbox duy nhat, tick lai checkbox do
        else:
            has_checked = False

            for cb in all_checkboxes:
                if cb.IsChecked:
                    has_checked = True
                    break

            if not has_checked:
                sender.IsChecked = True

    def get_checked_leader_types(self):
        result = []

        if self.cb90UD.IsChecked:
            result.append("90UD")

        if self.cb90LR.IsChecked:
            result.append("90LR")

        if self.cb45UD.IsChecked:
            result.append("45UD")

        if self.cb45LR.IsChecked:
            result.append("45LR")

        if self.cbStraightUD.IsChecked:
            result.append("STRAIGHTUD")

        if self.cbStraightLR.IsChecked:
            result.append("STRAIGHTLR")

        return result

    def apply_click(self, sender, args):
        shoulder_mm = parse_mm(self.tbShoulder.Text)
        elbow_mm = parse_mm(self.tbElbow.Text)
        spacing_mm = parse_mm(self.tbTagSpacing.Text)

        use_shoulder_length = bool(self.cbUseShoulderLength.IsChecked)
        use_elbow_length = bool(self.cbUseElbowLength.IsChecked)
        use_custom_angle = bool(self.cbCustomAngle.IsChecked)
        custom_angle_deg = parse_mm(self.tbCustomAngle.Text)

        # Khi checkbox tat, gia tri trong o goc khong anh huong den ket qua.
        # Neu o dang rong/khong hop le thi van luu lai moc 45 do mac dinh.
        if not use_custom_angle and custom_angle_deg <= 0:
            custom_angle_deg = 45.0

        if not use_shoulder_length and not use_elbow_length:
            forms.alert(
                "Phai tick it nhat Shoulder Length hoac Elbow Length.",
                exitscript=False
            )
            return

        if use_shoulder_length and shoulder_mm <= 0:
            forms.alert("Shoulder Length phai lon hon 0.", exitscript=False)
            return

        if use_elbow_length and elbow_mm <= 0:
            forms.alert("Elbow Length phai lon hon 0.", exitscript=False)
            return

        # A disabled text box is not used for geometry, but keep a valid value
        # in settings so it is ready when the option is enabled again.
        if shoulder_mm <= 0:
            shoulder_mm = DEFAULT_SETTINGS["shoulder_mm"]

        if elbow_mm <= 0:
            elbow_mm = DEFAULT_SETTINGS["elbow_mm"]

        if spacing_mm <= 0:
            forms.alert("Tag spacing phai lon hon 0.", exitscript=False)
            return

        if use_custom_angle:
            if custom_angle_deg <= 0 or custom_angle_deg >= 90:
                forms.alert(
                    "Custom Angle phai lon hon 0 do va nho hon 90 do.",
                    exitscript=False
                )
                return

        selected_types = self.get_checked_leader_types()

        if self.rbCustomize.IsChecked:
            if len(selected_types) == 0:
                forms.alert("Vui long chon it nhat 1 leader type.", exitscript=False)
                return

        self.save_ui_settings(
            shoulder_mm,
            elbow_mm,
            spacing_mm,
            use_custom_angle,
            custom_angle_deg,
            use_shoulder_length,
            use_elbow_length
        )

        shoulder_ft = mm_to_ft(shoulder_mm)
        elbow_ft = mm_to_ft(elbow_mm)
        spacing_ft = mm_to_ft(spacing_mm)

        use_auto = False

        if self.rbAuto.IsChecked:
            use_auto = True

        axis_mode = "X"

        if self.cbDistributeY.IsChecked:
            axis_mode = "Y"

        # Lay trang thai Shift mot lan duy nhat tai luc nhan Apply
        # Neu giu Shift khi nhan Apply, ca phien lenh se la multi-pick
        # Neu khong giu Shift khi nhan Apply, ca phien lenh se la single-pick
        allow_multiple_session = is_shift_pressed()

        self.Hide()

        # ====================================================
        # Giu lenh lien tuc cho den khi nhan Esc
        # ====================================================

        while True:
            try:
                tags = pick_tags_after_apply(allow_multiple_session)

            except OperationCanceledException:
                break

            except:
                break

            if len(tags) == 0:
                continue

            failed_messages = process_tags(
                tags,
                selected_types,
                use_auto,
                shoulder_ft,
                elbow_ft,
                spacing_ft,
                axis_mode,
                use_custom_angle,
                custom_angle_deg,
                use_shoulder_length,
                use_elbow_length
            )

            if len(failed_messages) > 0:
                forms.alert(
                    "Mot so tag khong align duoc:\n\n" + "\n".join(failed_messages),
                    exitscript=False
                )

        self.Close()


# ============================================================
# Run
# ============================================================

if not os.path.exists(XAML_PATH):
    forms.alert("Khong tim thay file ui.xaml.", exitscript=True)

window = AlignTagLeaderWindow(XAML_PATH)
window.ShowDialog()