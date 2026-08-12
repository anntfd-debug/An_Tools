# -*- coding: utf-8 -*-
__title__ = "Align Leader Angle"
__doc__ = (
    "Căn chỉnh góc nghiêng Leader của Tag, Text Note và Spot Elevation "
    "song song với đối tượng mẫu bằng cách dời điểm gấp theo phương ngang của View."
)

from pyrevit import revit, DB, UI, forms
from Autodesk.Revit.Exceptions import OperationCanceledException


doc = revit.doc
uidoc = revit.uidoc

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


def ensure_editable_leader(elem):
    """Bật Leader và chuyển Tag về Free End khi API cho phép."""
    if isinstance(elem, DB.IndependentTag):
        try:
            if not elem.HasLeader:
                elem.HasLeader = True
        except:
            pass

        # API mới: mỗi tagged reference có LeaderEndCondition riêng.
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
        # Spot Elevation phải có Leader mới đọc/ghi được các điểm Leader.
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
        handle     : dữ liệu dùng khi ghi lại Leader
        movable_pt : điểm gấp/điểm điều khiển sẽ được dịch theo trục ngang View
        anchor_pt  : đầu Leader cố định tại đối tượng được chú thích
        head_pt    : vị trí đầu Tag/Text, chỉ dùng tham khảo

    Vector góc được tính từ anchor_pt -> movable_pt.
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

        # Revit API mới.
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
                    # Leader thẳng: điểm Tag Head là điểm điều khiển còn lại.
                    movable_pt = head_pos

                handle = {
                    "kind": "independent_tag",
                    "reference": tagged_ref,
                    "has_elbow": has_elbow
                }
                return handle, movable_pt, anchor_pt, head_pos
            except:
                pass

        # Revit API cũ.
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

        # SpotSlope và một số kiểu Dimension đặc biệt không cho chỉnh text/leader.
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
            # Tên API cũ từng dùng từ Elbow.
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

        # Spot Elevation không có shoulder: align trực tiếp đầu Leader phía text.
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


def set_leader_points(elem, handle, new_movable_pt, anchor_pt):
    """Ghi điểm Leader mới. Trả về True khi chỉnh thành công."""
    if not handle or new_movable_pt is None:
        return False

    if isinstance(elem, DB.IndependentTag):
        tag = elem
        tagged_ref = handle.get("reference")

        if handle.get("kind") == "independent_tag" and tagged_ref:
            try:
                # Nếu Leader có elbow thì chỉ dời elbow; giữ nguyên đầu mũi tên.
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

            # Dời shoulder có thể kéo theo LeaderEndPosition và TextPosition.
            # Khôi phục để chỉ thay đổi góc nghiêng, không làm xê dịch text.
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
            # Không có shoulder thì phải dời đầu Leader phía text để đổi góc.
            try:
                elem.LeaderEndPosition = new_movable_pt
                return True
            except:
                return False

    return False


def main():
    try:
        target_refs = uidoc.Selection.PickObjects(
            UI.Selection.ObjectType.Element,
            AnnotationSelectionFilter(),
            "1. Chọn Tag / Text Note / Spot Elevation cần align (Finish khi xong)"
        )
        targets = [doc.GetElement(ref) for ref in target_refs]
    except OperationCanceledException:
        return

    if not targets:
        return

    try:
        source_ref = uidoc.Selection.PickObject(
            UI.Selection.ObjectType.Element,
            AnnotationSelectionFilter(),
            "2. Chọn Tag / Text Note / Spot Elevation mẫu"
        )
        source_elem = doc.GetElement(source_ref)
    except OperationCanceledException:
        return

    view = doc.ActiveView
    right_dir = view.RightDirection
    up_dir = view.UpDirection

    def get_uv(xyz_point):
        return (
            xyz_point.DotProduct(right_dir),
            xyz_point.DotProduct(up_dir)
        )

    aligned_count = 0
    skipped_count = 0

    with revit.Transaction("Align Leader Angle"):
        ensure_editable_leader(source_elem)
        for elem in targets:
            ensure_editable_leader(elem)

        doc.Regenerate()

        source_handle, source_movable, source_anchor, source_head = get_leader_data(
            source_elem
        )

        if source_movable is None or source_anchor is None:
            forms.alert(
                "Không đọc được Leader của đối tượng mẫu.\n\n"
                "Hãy dùng Spot Elevation/Tag/Text Note có Leader. "
                "Spot Slope và một số kiểu Dimension đặc biệt không được API hỗ trợ.",
                title="Không thể lấy góc Leader"
            )
            return

        source_movable_u, source_movable_v = get_uv(source_movable)
        source_anchor_u, source_anchor_v = get_uv(source_anchor)

        source_du = source_movable_u - source_anchor_u
        source_dv = source_movable_v - source_anchor_v

        for elem in targets:
            if elem.Id == source_elem.Id:
                continue

            handle, movable_pt, anchor_pt, head_pt = get_leader_data(elem)
            if movable_pt is None or anchor_pt is None:
                skipped_count += 1
                continue

            movable_u, movable_v = get_uv(movable_pt)
            anchor_u, anchor_v = get_uv(anchor_pt)

            if abs(source_dv) > TOLERANCE:
                # Giữ nguyên cao độ màn hình của điểm gấp target và chỉ dời ngang.
                ratio = source_du / source_dv
                new_movable_u = (
                    anchor_u
                    + (movable_v - anchor_v) * ratio
                )

                shift_u = new_movable_u - movable_u
                new_movable_pt = movable_pt + right_dir * shift_u
            else:
                # Leader mẫu nằm ngang: đưa điểm điều khiển target về cùng V với anchor.
                shift_v = anchor_v - movable_v
                new_movable_pt = movable_pt + up_dir * shift_v

            if set_leader_points(elem, handle, new_movable_pt, anchor_pt):
                aligned_count += 1
            else:
                skipped_count += 1

    if aligned_count == 0:
        forms.alert(
            "Không có đối tượng nào được align.\n\n"
            "Đã bỏ qua: {0} đối tượng.\n"
            "Kiểm tra Leader, kiểu Spot Dimension hoặc trạng thái Pin/Group.".format(
                skipped_count
            ),
            title="Align Leader Angle"
        )


if __name__ == "__main__":
    main()
