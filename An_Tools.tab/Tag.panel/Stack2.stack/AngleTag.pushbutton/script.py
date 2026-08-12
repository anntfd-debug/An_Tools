# -*- coding: utf-8 -*-
__title__ = "Set Leader Angle"
__doc__ = (
    "Click: pick liên tục Tag, Text Note hoặc Spot Elevation để đổi góc Leader; "
    "nhấn Esc để kết thúc.\n"
    "Shift+Click: nhập góc nghiêng và lưu lại cho những lần chạy tiếp theo."
)

import math

from pyrevit import revit, DB, UI, forms, script
from Autodesk.Revit.Exceptions import OperationCanceledException


doc = revit.doc
uidoc = revit.uidoc

DEFAULT_ANGLE_DEG = 45.0
TOLERANCE = 0.00001


class AnnotationSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return (
            isinstance(elem, DB.IndependentTag)
            or isinstance(elem, DB.TextNote)
            or isinstance(elem, DB.SpotDimension)
        )

    def AllowReference(self, ref, pos):
        return False


def is_shift_click():
    """Đọc trạng thái Shift+Click trong các phiên bản pyRevit khác nhau."""
    try:
        return bool(__shiftclick__)
    except:
        pass

    try:
        from pyrevit import EXEC_PARAMS
        return bool(EXEC_PARAMS.config_mode)
    except:
        return False


def get_saved_angle():
    config = script.get_config()
    try:
        angle = float(config.leader_angle_deg)
    except:
        angle = DEFAULT_ANGLE_DEG

    if angle <= 0.0 or angle > 90.0:
        angle = DEFAULT_ANGLE_DEG

    return angle


def save_angle(angle_deg):
    config = script.get_config()
    config.leader_angle_deg = float(angle_deg)
    script.save_config()


def format_angle(angle_deg):
    text = "{0:.4f}".format(float(angle_deg)).rstrip("0").rstrip(".")
    return text


def ask_and_save_angle():
    current_angle = get_saved_angle()

    while True:
        entered = forms.ask_for_string(
            default=format_angle(current_angle),
            prompt=(
                "Nhập góc nghiêng của đoạn Leader chéo so với phương ngang.\n\n"
                "Phạm vi: lớn hơn 0° đến 90°.\n"
                "Giá trị mặc định: 45°."
            ),
            title="Cài đặt góc Leader"
        )

        if entered is None:
            return

        try:
            angle_deg = float(entered.strip().replace(",", "."))
        except:
            forms.alert(
                "Giá trị góc không hợp lệ. Hãy nhập một số, ví dụ: 45 hoặc 30.5.",
                title="Cài đặt góc Leader"
            )
            continue

        if angle_deg <= 0.0 or angle_deg > 90.0:
            forms.alert(
                "Góc phải lớn hơn 0° và không vượt quá 90°.",
                title="Cài đặt góc Leader"
            )
            continue

        save_angle(angle_deg)
        forms.alert(
            "Đã lưu góc Leader: {0}°\n\n"
            "Các lần click thường tiếp theo sẽ dùng giá trị này.".format(
                format_angle(angle_deg)
            ),
            title="Cài đặt góc Leader"
        )
        return


def ensure_editable_leader(elem):
    """Bật Leader và chuyển Tag về Free End khi API cho phép."""
    if isinstance(elem, DB.IndependentTag):
        try:
            if not elem.HasLeader:
                elem.HasLeader = True
        except:
            pass

        # API mới: LeaderEndCondition được đặt theo từng tagged reference.
        if hasattr(elem, "GetTaggedReferences"):
            try:
                refs = elem.GetTaggedReferences()
                if refs:
                    for tagged_ref in refs:
                        if hasattr(elem, "SetLeaderEndCondition"):
                            try:
                                elem.SetLeaderEndCondition(
                                    tagged_ref,
                                    DB.LeaderEndCondition.Free
                                )
                            except:
                                pass
            except:
                pass

        # API cũ.
        try:
            elem.LeaderEndCondition = DB.LeaderEndCondition.Free
        except:
            pass

    elif isinstance(elem, DB.SpotDimension):
        try:
            if not elem.HasLeader:
                elem.HasLeader = True
        except:
            pass


def get_spot_text_position(spot):
    try:
        if hasattr(spot, "IsTextPositionAdjustable"):
            if not spot.IsTextPositionAdjustable():
                return None
        return spot.TextPosition
    except:
        return None


def get_spot_leader_end_position(spot):
    try:
        return spot.LeaderEndPosition
    except:
        return None


def get_leader_data(elem):
    """
    Trả về:
        handle     : dữ liệu cần dùng khi ghi lại Leader
        movable_pt : elbow/shoulder/điểm điều khiển sẽ được trượt ngang
        anchor_pt  : đầu Leader tại đối tượng được chú thích, được giữ nguyên
        head_pt    : vị trí text/tag, dùng để xác định hướng trái/phải khi cần
    """
    if isinstance(elem, DB.IndependentTag):
        tag = elem

        try:
            if not tag.HasLeader:
                return None, None, None, None
        except:
            return None, None, None, None

        tagged_ref = None
        if hasattr(tag, "GetTaggedReferences"):
            try:
                refs = tag.GetTaggedReferences()
                if refs and len(refs) > 0:
                    tagged_ref = refs[0]
            except:
                pass

        head_pos = None
        try:
            head_pos = tag.TagHeadPosition
        except:
            pass

        # API mới.
        if tagged_ref and hasattr(tag, "GetLeaderEnd"):
            try:
                anchor_pt = tag.GetLeaderEnd(tagged_ref)

                has_elbow = False
                if hasattr(tag, "HasLeaderElbow"):
                    try:
                        has_elbow = tag.HasLeaderElbow(tagged_ref)
                    except:
                        has_elbow = False

                if has_elbow:
                    movable_pt = tag.GetLeaderElbow(tagged_ref)
                else:
                    movable_pt = head_pos

                handle = {
                    "kind": "independent_tag",
                    "reference": tagged_ref,
                    "has_elbow": has_elbow
                }
                return handle, movable_pt, anchor_pt, head_pos
            except:
                pass

        # API cũ.
        try:
            anchor_pt = tag.LeaderEnd
            has_elbow = False
            try:
                has_elbow = tag.HasLeaderElbow
            except:
                has_elbow = True

            if has_elbow:
                try:
                    movable_pt = tag.LeaderElbow
                except:
                    movable_pt = head_pos
            else:
                movable_pt = head_pos

            handle = {
                "kind": "independent_tag_legacy",
                "reference": None,
                "has_elbow": has_elbow
            }
            return handle, movable_pt, anchor_pt, head_pos
        except:
            return None, None, None, None

    if isinstance(elem, DB.TextNote):
        try:
            leaders = elem.GetLeaders()
            if leaders and len(leaders) > 0:
                leader = leaders[0]
                handle = {
                    "kind": "text_note",
                    "leader": leader
                }
                return handle, leader.Elbow, leader.End, elem.Coord
        except:
            pass

        return None, None, None, None

    if isinstance(elem, DB.SpotDimension):
        spot = elem

        try:
            if not spot.HasLeader:
                return None, None, None, None
        except:
            return None, None, None, None

        # Spot Slope và một số loại SpotDimension không cho chỉnh text/leader.
        try:
            if hasattr(spot, "IsTextPositionAdjustable"):
                if not spot.IsTextPositionAdjustable():
                    return None, None, None, None
        except:
            pass

        try:
            anchor_pt = spot.Origin
        except:
            return None, None, None, None

        has_shoulder = False
        try:
            has_shoulder = spot.LeaderHasShoulder
        except:
            try:
                has_shoulder = spot.LeaderHasElbow
            except:
                has_shoulder = False

        if has_shoulder:
            try:
                movable_pt = spot.LeaderShoulderPosition
            except:
                try:
                    movable_pt = spot.LeaderElbowPosition
                except:
                    return None, None, None, None

            handle = {
                "kind": "spot_dimension",
                "mode": "shoulder",
                "leader_end": get_spot_leader_end_position(spot),
                "text_position": get_spot_text_position(spot)
            }
            return handle, movable_pt, anchor_pt, handle["text_position"]

        # Spot Elevation không có shoulder.
        movable_pt = get_spot_leader_end_position(spot)
        if movable_pt is None:
            return None, None, None, None

        handle = {
            "kind": "spot_dimension",
            "mode": "direct",
            "leader_end": movable_pt,
            "text_position": get_spot_text_position(spot)
        }
        return handle, movable_pt, anchor_pt, handle["text_position"]

    return None, None, None, None


def set_leader_point(elem, handle, new_movable_pt, anchor_pt):
    """Ghi điểm điều khiển Leader mới. Trả về True khi thành công."""
    if not handle or new_movable_pt is None:
        return False

    if isinstance(elem, DB.IndependentTag):
        tag = elem
        tagged_ref = handle.get("reference")

        if handle.get("kind") == "independent_tag" and tagged_ref:
            try:
                if handle.get("has_elbow"):
                    tag.SetLeaderElbow(tagged_ref, new_movable_pt)
                else:
                    tag.TagHeadPosition = new_movable_pt

                tag.SetLeaderEnd(tagged_ref, anchor_pt)
                return True
            except:
                pass

        try:
            if handle.get("has_elbow"):
                tag.LeaderElbow = new_movable_pt
            else:
                tag.TagHeadPosition = new_movable_pt

            tag.LeaderEnd = anchor_pt
            return True
        except:
            return False

    if isinstance(elem, DB.TextNote):
        leader = handle.get("leader")
        if leader is None:
            return False

        try:
            leader.Elbow = new_movable_pt
            leader.End = anchor_pt
            return True
        except:
            return False

    if isinstance(elem, DB.SpotDimension):
        mode = handle.get("mode")

        if mode == "shoulder":
            old_leader_end = handle.get("leader_end")
            old_text_position = handle.get("text_position")

            try:
                if hasattr(elem, "LeaderShoulderPosition"):
                    elem.LeaderShoulderPosition = new_movable_pt
                else:
                    elem.LeaderElbowPosition = new_movable_pt
            except:
                return False

            # Dời shoulder có thể kéo theo text và đầu Leader phía text.
            # Khôi phục hai điểm này để chỉ thay đổi góc đoạn Leader chéo.
            if old_leader_end is not None:
                try:
                    elem.LeaderEndPosition = old_leader_end
                except:
                    pass

            if old_text_position is not None:
                try:
                    elem.TextPosition = old_text_position
                except:
                    pass

            return True

        if mode == "direct":
            try:
                elem.LeaderEndPosition = new_movable_pt
                return True
            except:
                return False

    return False


def get_uv(point, right_dir, up_dir):
    return (
        point.DotProduct(right_dir),
        point.DotProduct(up_dir)
    )


def get_horizontal_sign(movable_u, anchor_u, head_pt, right_dir, up_dir):
    """Giữ nguyên hướng Leader đang nghiêng sang trái hoặc sang phải."""
    du = movable_u - anchor_u
    if abs(du) > TOLERANCE:
        return 1.0 if du > 0.0 else -1.0

    if head_pt is not None:
        try:
            head_u, unused_head_v = get_uv(head_pt, right_dir, up_dir)
            head_du = head_u - anchor_u
            if abs(head_du) > TOLERANCE:
                return 1.0 if head_du > 0.0 else -1.0
        except:
            pass

    return 1.0


def calculate_new_movable_point(
        movable_pt,
        anchor_pt,
        head_pt,
        angle_deg,
        right_dir,
        up_dir):
    """
    Góc được đo so với phương ngang của Active View.

    Giữ nguyên tọa độ V của elbow/shoulder và chỉ trượt theo RightDirection.
    Do đó text và đoạn Leader ngang ít bị xê dịch nhất.
    """
    movable_u, movable_v = get_uv(movable_pt, right_dir, up_dir)
    anchor_u, anchor_v = get_uv(anchor_pt, right_dir, up_dir)

    dv = movable_v - anchor_v

    # Khi đoạn Leader hiện tại nằm ngang, việc chỉ trượt theo X không thể tạo
    # một góc nghiêng mới mà vẫn giữ nguyên vị trí text/điểm gấp theo phương Y.
    if abs(dv) <= TOLERANCE and angle_deg < 89.9999:
        return None, "horizontal"

    direction_sign = get_horizontal_sign(
        movable_u,
        anchor_u,
        head_pt,
        right_dir,
        up_dir
    )

    if angle_deg >= 89.9999:
        required_abs_du = 0.0
    else:
        angle_rad = math.radians(angle_deg)
        tangent = math.tan(angle_rad)
        if abs(tangent) <= TOLERANCE:
            return None, "invalid_angle"

        required_abs_du = abs(dv) / abs(tangent)

    new_movable_u = anchor_u + direction_sign * required_abs_du
    shift_u = new_movable_u - movable_u
    new_movable_pt = movable_pt + right_dir * shift_u

    return new_movable_pt, None


def process_one_element(elem, angle_deg, right_dir, up_dir):
    """
    Xử lý một đối tượng được pick.

    Trả về:
        changed    : True nếu đã đổi được Leader.
        error_code : mã lỗi nếu không đổi được.
    """
    if elem is None:
        return False, "invalid_element"

    error_code = None
    changed = False

    with revit.Transaction(
            "Set Leader Angle {0} deg".format(format_angle(angle_deg))):
        ensure_editable_leader(elem)
        doc.Regenerate()

        handle, movable_pt, anchor_pt, head_pt = get_leader_data(elem)

        if movable_pt is None or anchor_pt is None:
            error_code = "no_leader"
        else:
            new_movable_pt, error_code = calculate_new_movable_point(
                movable_pt,
                anchor_pt,
                head_pt,
                angle_deg,
                right_dir,
                up_dir
            )

            if new_movable_pt is not None:
                changed = set_leader_point(
                    elem,
                    handle,
                    new_movable_pt,
                    anchor_pt
                )

                if not changed:
                    error_code = "write_failed"

    return changed, error_code


def show_failure_summary(angle_deg, failed_items):
    """Chỉ hiện một thông báo tổng hợp sau khi người dùng nhấn Esc."""
    if not failed_items:
        return

    counts = {}
    for unused_elem_id, error_code in failed_items:
        counts[error_code] = counts.get(error_code, 0) + 1

    lines = [
        "Đã kết thúc lệnh.",
        "",
        "Có {0} đối tượng không đổi được góc Leader {1}°:".format(
            len(failed_items),
            format_angle(angle_deg)
        )
    ]

    if counts.get("horizontal"):
        lines.append(
            "• {0} đối tượng có đoạn Leader đang nằm ngang hoàn toàn.".format(
                counts["horizontal"]
            )
        )

    if counts.get("no_leader"):
        lines.append(
            "• {0} đối tượng không đọc được Leader hoặc không hỗ trợ chỉnh qua API.".format(
                counts["no_leader"]
            )
        )

    other_count = (
        counts.get("write_failed", 0)
        + counts.get("invalid_element", 0)
    )
    if other_count:
        lines.append(
            "• {0} đối tượng không ghi được vị trí Leader; có thể đang Pin, "
            "nằm trong Group hoặc bị giới hạn chỉnh sửa.".format(other_count)
        )

    failed_ids = [str(elem_id) for elem_id, unused_error in failed_items if elem_id]
    if failed_ids:
        lines.extend([
            "",
            "Element ID: {0}".format(", ".join(failed_ids))
        ])

    forms.alert(
        "\n".join(lines),
        title="Set Leader Angle"
    )


def main():
    if is_shift_click():
        ask_and_save_angle()
        return

    angle_deg = get_saved_angle()

    view = doc.ActiveView
    right_dir = view.RightDirection
    up_dir = view.UpDirection

    changed_count = 0
    failed_items = []

    # PickObject chỉ chọn một đối tượng mỗi lần. Đặt nó trong vòng lặp để
    # người dùng có thể pick liên tục và dùng Esc làm tín hiệu kết thúc.
    while True:
        try:
            picked_ref = uidoc.Selection.PickObject(
                UI.Selection.ObjectType.Element,
                AnnotationSelectionFilter(),
                (
                    "Pick Tag / Text Note / Spot Elevation để đặt góc Leader = "
                    "{0}°. Nhấn Esc để kết thúc."
                ).format(format_angle(angle_deg))
            )
        except OperationCanceledException:
            break

        elem = doc.GetElement(picked_ref)
        elem_id = None
        try:
            elem_id = elem.Id.IntegerValue
        except:
            pass

        try:
            changed, error_code = process_one_element(
                elem,
                angle_deg,
                right_dir,
                up_dir
            )
        except Exception:
            # Không để một đối tượng lỗi làm dừng toàn bộ chuỗi pick.
            changed = False
            error_code = "write_failed"

        if changed:
            changed_count += 1
        else:
            failed_items.append((elem_id, error_code or "write_failed"))

    # Thành công hoàn toàn thì kết thúc im lặng. Chỉ báo một lần nếu có lỗi.
    show_failure_summary(angle_deg, failed_items)


if __name__ == "__main__":
    main()
