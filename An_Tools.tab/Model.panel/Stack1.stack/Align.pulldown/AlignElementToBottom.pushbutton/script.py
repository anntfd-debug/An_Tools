# -*- coding: utf-8 -*-
__title__ = "Bottom To Bottom"
__doc__ = (
    "Align đáy hình học của các Family trong Active View vào mặt đáy "
    "của Revit Link được chọn. Có Progress Bar và Cancel."
)

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

import Autodesk.Revit.DB as DB
import Autodesk.Revit.UI as UI
import Autodesk.Revit.Exceptions as Exceptions

from pyrevit import revit, forms, script


doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()


# --------------------------------------------------
# 1. FILTER CHỌN ĐỐI TƯỢNG
# --------------------------------------------------
class FamilyInstanceFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, DB.FamilyInstance)

    def AllowReference(self, ref, pos):
        return False


class RevitLinkFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, DB.RevitLinkInstance)

    def AllowReference(self, ref, pos):
        return False


# --------------------------------------------------
# 2. HÀM HỖ TRỢ
# --------------------------------------------------
def get_id_value(element_id):
    """Tương thích cả ElementId.IntegerValue và ElementId.Value."""
    if element_id is None:
        return None

    try:
        return element_id.Value
    except Exception:
        return element_id.IntegerValue


def get_3d_view_family_type():
    """Lấy ViewFamilyType dùng để tạo View 3D tạm."""
    view_types = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.ViewFamilyType)
        .ToElements()
    )

    for view_type in view_types:
        if view_type.ViewFamily == DB.ViewFamily.ThreeDimensional:
            return view_type

    return None


def get_family_bottom_z(family_instance):
    """Lấy cao độ thấp nhất của BoundingBox Family trong hệ tọa độ model."""
    bbox = family_instance.get_BoundingBox(None)
    if bbox is None:
        return None

    return bbox.Min.Z


def find_target_bottom_z(intersector, link_instance_id_value, origin):
    """
    Bắn tia thẳng đứng lên trên và lấy giao điểm gần nhất thuộc đúng
    Revit Link đã chọn.

    ReferenceWithContext.Proximity được dùng thay cho GlobalPoint để
    tính cao độ ổn định hơn khi giao điểm nằm trong Revit Link.
    """
    direction = DB.XYZ.BasisZ
    hits = intersector.Find(origin, direction)

    nearest_proximity = None

    for hit in hits:
        ref = hit.GetReference()
        if ref is None:
            continue

        if get_id_value(ref.ElementId) != link_instance_id_value:
            continue

        proximity = hit.Proximity
        if proximity < 0:
            continue

        if nearest_proximity is None or proximity < nearest_proximity:
            nearest_proximity = proximity

    if nearest_proximity is None:
        return None

    return origin.Z + nearest_proximity


def build_result_message(total, success_count, no_hit_count,
                         no_location_count, no_bbox_count,
                         pinned_count, failed_count):
    lines = [
        "Tổng số Family đã kiểm tra: {}".format(total),
        "Align thành công: {}".format(success_count),
    ]

    if no_hit_count:
        lines.append(
            "Không tìm thấy hình học Link phù hợp: {}".format(no_hit_count)
        )

    if no_location_count:
        lines.append(
            "Không có LocationPoint: {}".format(no_location_count)
        )

    if no_bbox_count:
        lines.append(
            "Không lấy được BoundingBox: {}".format(no_bbox_count)
        )

    if pinned_count:
        lines.append(
            "Đang bị Pin nên không di chuyển: {}".format(pinned_count)
        )

    if failed_count:
        lines.append(
            "Lỗi khi xử lý: {} (xem pyRevit log)".format(failed_count)
        )

    return "\n".join(lines)


# --------------------------------------------------
# 3. CHƯƠNG TRÌNH CHÍNH
# --------------------------------------------------
def main():
    # ----------------------------------------------
    # Chọn Family mẫu và Revit Link
    # ----------------------------------------------
    try:
        selected_family_refs = uidoc.Selection.PickObjects(
            UI.Selection.ObjectType.Element,
            FamilyInstanceFilter(),
            "1/2: Chọn Family mẫu rồi nhấn Finish"
        )

        target_family_id_values = set()

        for selected_ref in selected_family_refs:
            family_instance = doc.GetElement(selected_ref.ElementId)
            if family_instance is None:
                continue

            symbol = family_instance.Symbol
            if symbol is None or symbol.Family is None:
                continue

            target_family_id_values.add(
                get_id_value(symbol.Family.Id)
            )

        if not target_family_id_values:
            forms.alert(
                "Không lấy được Family từ các đối tượng đã chọn.",
                title="Bottom To Bottom"
            )
            return

        selected_link_ref = uidoc.Selection.PickObject(
            UI.Selection.ObjectType.Element,
            RevitLinkFilter(),
            "2/2: Chọn Revit Link dùng làm mục tiêu"
        )

        link_instance = doc.GetElement(selected_link_ref.ElementId)

    except Exceptions.OperationCanceledException:
        # Người dùng nhấn Esc trong giai đoạn Pick.
        return

    if link_instance is None:
        forms.alert(
            "Không lấy được Revit Link đã chọn.",
            title="Bottom To Bottom"
        )
        return

    if link_instance.GetLinkDocument() is None:
        forms.alert(
            "Revit Link đang bị Unload hoặc không thể truy cập.",
            title="Bottom To Bottom"
        )
        return

    # ----------------------------------------------
    # Thu thập Family trong Active View
    # ----------------------------------------------
    active_view = doc.ActiveView

    try:
        all_instances = (
            DB.FilteredElementCollector(doc, active_view.Id)
            .OfClass(DB.FamilyInstance)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception as collect_error:
        logger.error(
            "Không thể thu thập Family trong Active View: {}".format(
                collect_error
            )
        )
        forms.alert(
            "Active View hiện tại không hỗ trợ thu thập đối tượng để chạy lệnh.",
            title="Bottom To Bottom"
        )
        return

    instances_to_align = []

    for instance in all_instances:
        try:
            symbol = instance.Symbol
            if symbol is None or symbol.Family is None:
                continue

            family_id_value = get_id_value(symbol.Family.Id)
            if family_id_value in target_family_id_values:
                instances_to_align.append(instance)
        except Exception:
            continue

    if not instances_to_align:
        forms.alert(
            "Không tìm thấy instance thuộc các Family đã chọn trong Active View.",
            title="Bottom To Bottom"
        )
        return

    view_family_type_3d = get_3d_view_family_type()
    if view_family_type_3d is None:
        forms.alert(
            "Không tìm thấy ViewFamilyType 3D để tạo View raycast tạm.",
            title="Bottom To Bottom"
        )
        return

    # ----------------------------------------------
    # Raycast + Move trong một Transaction
    # Cancel sẽ RollBack toàn bộ thay đổi.
    # ----------------------------------------------
    transaction = DB.Transaction(
        doc,
        "Align Multiple Families to Link Bottom"
    )

    temp_view = None
    cancelled = False

    success_count = 0
    no_hit_count = 0
    no_location_count = 0
    no_bbox_count = 0
    pinned_count = 0
    failed_count = 0

    total_count = len(instances_to_align)
    link_instance_id_value = get_id_value(link_instance.Id)

    try:
        transaction.Start()

        temp_view = DB.View3D.CreateIsometric(
            doc,
            view_family_type_3d.Id
        )
        doc.Regenerate()

        intersector = DB.ReferenceIntersector(temp_view)
        intersector.FindReferencesInRevitLinks = True

        progress_title = (
            "Đang align Family: {value} / {max_value}"
        )

        with forms.ProgressBar(
            title=progress_title,
            cancellable=True,
            step=1
        ) as progress_bar:

            for index, instance in enumerate(instances_to_align):
                if progress_bar.cancelled:
                    cancelled = True
                    break

                progress_bar.update_progress(index + 1, total_count)

                try:
                    if instance.Pinned:
                        pinned_count += 1
                        continue

                    location = instance.Location
                    if not isinstance(location, DB.LocationPoint):
                        no_location_count += 1
                        continue

                    family_bottom_z = get_family_bottom_z(instance)
                    if family_bottom_z is None:
                        no_bbox_count += 1
                        continue

                    point = location.Point

                    # Giữ nguyên phạm vi tìm kiếm ban đầu: bắt đầu thấp hơn
                    # đáy Family 10 feet và bắn tia thẳng đứng lên trên.
                    origin = DB.XYZ(
                        point.X,
                        point.Y,
                        family_bottom_z - 10.0
                    )

                    target_bottom_z = find_target_bottom_z(
                        intersector,
                        link_instance_id_value,
                        origin
                    )

                    if target_bottom_z is None:
                        no_hit_count += 1
                        continue

                    delta_z = target_bottom_z - family_bottom_z

                    # Không gọi MoveElement khi khoảng dịch chuyển gần bằng 0.
                    if abs(delta_z) > 1e-9:
                        DB.ElementTransformUtils.MoveElement(
                            doc,
                            instance.Id,
                            DB.XYZ(0.0, 0.0, delta_z)
                        )

                    success_count += 1

                except Exception as instance_error:
                    failed_count += 1
                    logger.error(
                        "Family ID {} xử lý lỗi: {}".format(
                            get_id_value(instance.Id),
                            instance_error
                        )
                    )

        if cancelled:
            transaction.RollBack()
            forms.alert(
                "Đã hủy. Toàn bộ thay đổi của lần chạy này đã được hoàn tác.",
                title="Bottom To Bottom"
            )
            return

        # Xóa View 3D tạm trước khi Commit.
        if temp_view is not None and temp_view.IsValidObject:
            doc.Delete(temp_view.Id)

        transaction.Commit()

    except Exception as error:
        if transaction.HasStarted():
            transaction.RollBack()

        logger.error("Bottom To Bottom lỗi: {}".format(error))
        forms.alert(
            "Lệnh gặp lỗi và toàn bộ thay đổi đã được hoàn tác.\n\n{}".format(
                error
            ),
            title="Bottom To Bottom"
        )
        return

    # ----------------------------------------------
    # Báo cáo kết quả
    # ----------------------------------------------
    result_message = build_result_message(
        total_count,
        success_count,
        no_hit_count,
        no_location_count,
        no_bbox_count,
        pinned_count,
        failed_count
    )

    forms.alert(
        result_message,
        title="Kết quả Bottom To Bottom"
    )


if __name__ == "__main__":
    main()
