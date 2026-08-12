# -*- coding: utf-8 -*-
__title__ = "L0.0 To Top"
__doc__ = (
    "Align cao do Origin 0.0 cua cac Family trong Active View vao mat tren "
    "cua Revit Link duoc chon. Co Progress Bar va Cancel."
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
# 1. FILTER CHON DOI TUONG
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
# 2. HAM HO TRO
# --------------------------------------------------
def get_id_value(element_id):
    """Tuong thich ElementId.Value va ElementId.IntegerValue."""
    if element_id is None:
        return None

    try:
        return element_id.Value
    except Exception:
        return element_id.IntegerValue


def get_3d_view_family_type():
    """Lay ViewFamilyType dung de tao View 3D tam cho raycast."""
    view_types = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.ViewFamilyType)
        .ToElements()
    )

    for view_type in view_types:
        if view_type.ViewFamily == DB.ViewFamily.ThreeDimensional:
            return view_type

    return None


def find_target_top_z(intersector, link_instance_id_value, origin):
    """
    Ban tia thang dung tu tren xuong va lay giao diem gan nhat thuoc dung
    Revit Link da chon.

    Giao diem gan nhat theo huong -Z chinh la mat hinh hoc cao nhat nam
    ben duoi diem xuat phat.
    """
    direction = DB.XYZ(0.0, 0.0, -1.0)
    hits = intersector.Find(origin, direction)

    nearest_proximity = None

    for hit in hits:
        ref = hit.GetReference()
        if ref is None:
            continue

        # Khi ReferenceIntersector tim trong Revit Link, ElementId la ID
        # cua RevitLinkInstance trong model hien tai.
        if get_id_value(ref.ElementId) != link_instance_id_value:
            continue

        proximity = hit.Proximity
        if proximity < 0:
            continue

        if nearest_proximity is None or proximity < nearest_proximity:
            nearest_proximity = proximity

    if nearest_proximity is None:
        return None

    return origin.Z - nearest_proximity


def build_result_message(total, success_count, no_hit_count,
                         no_location_count, pinned_count, failed_count):
    lines = [
        "Tong so Family da kiem tra: {}".format(total),
        "Align thanh cong: {}".format(success_count),
    ]

    if no_hit_count:
        lines.append(
            "Khong tim thay hinh hoc Link phu hop: {}".format(no_hit_count)
        )

    if no_location_count:
        lines.append(
            "Khong co LocationPoint: {}".format(no_location_count)
        )

    if pinned_count:
        lines.append(
            "Dang bi Pin nen khong di chuyen: {}".format(pinned_count)
        )

    if failed_count:
        lines.append(
            "Loi khi xu ly: {} (xem pyRevit log)".format(failed_count)
        )

    return "\n".join(lines)


# --------------------------------------------------
# 3. CHUONG TRINH CHINH
# --------------------------------------------------
def main():
    # ----------------------------------------------
    # Chon Family mau va Revit Link
    # ----------------------------------------------
    try:
        selected_family_refs = uidoc.Selection.PickObjects(
            UI.Selection.ObjectType.Element,
            FamilyInstanceFilter(),
            "1/2: Chon Family mau roi nhan Finish"
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
                "Khong lay duoc Family tu cac doi tuong da chon.",
                title="L0.0 To Top"
            )
            return

        selected_link_ref = uidoc.Selection.PickObject(
            UI.Selection.ObjectType.Element,
            RevitLinkFilter(),
            "2/2: Chon Revit Link dung lam mat tren muc tieu"
        )

        link_instance = doc.GetElement(selected_link_ref.ElementId)

    except Exceptions.OperationCanceledException:
        # Nhan Esc trong giai doan Pick thi thoat yen lang.
        return

    if link_instance is None:
        forms.alert(
            "Khong lay duoc Revit Link da chon.",
            title="L0.0 To Top"
        )
        return

    if link_instance.GetLinkDocument() is None:
        forms.alert(
            "Revit Link dang bi Unload hoac khong the truy cap.",
            title="L0.0 To Top"
        )
        return

    # ----------------------------------------------
    # Thu thap Family trong Active View
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
            "Khong the thu thap Family trong Active View: {}".format(
                collect_error
            )
        )
        forms.alert(
            "Active View hien tai khong ho tro thu thap doi tuong de chay lenh.",
            title="L0.0 To Top"
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
            "Khong tim thay instance thuoc cac Family da chon trong Active View.",
            title="L0.0 To Top"
        )
        return

    view_family_type_3d = get_3d_view_family_type()
    if view_family_type_3d is None:
        forms.alert(
            "Khong tim thay ViewFamilyType 3D de tao View raycast tam.",
            title="L0.0 To Top"
        )
        return

    # ----------------------------------------------
    # Raycast + Move trong mot Transaction.
    # Cancel se RollBack toan bo thay doi.
    # ----------------------------------------------
    transaction = DB.Transaction(
        doc,
        "Align Family Origin to Link Top"
    )

    temp_view = None
    cancelled = False

    success_count = 0
    no_hit_count = 0
    no_location_count = 0
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

        progress_title = "Dang align Family: {value} / {max_value}"

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

                    point = location.Point
                    current_origin_z = point.Z

                    # Giu nguyen pham vi cua code goc:
                    # bat dau cao hon Origin Family 30 feet va ban xuong.
                    origin = DB.XYZ(
                        point.X,
                        point.Y,
                        current_origin_z + 30.0
                    )

                    target_top_z = find_target_top_z(
                        intersector,
                        link_instance_id_value,
                        origin
                    )

                    if target_top_z is None:
                        no_hit_count += 1
                        continue

                    delta_z = target_top_z - current_origin_z

                    # Khong goi MoveElement neu khoang dich chuyen gan bang 0.
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
                        "Family ID {} xu ly loi: {}".format(
                            get_id_value(instance.Id),
                            instance_error
                        )
                    )

        if cancelled:
            transaction.RollBack()
            forms.alert(
                "Da huy. Toan bo thay doi cua lan chay nay da duoc hoan tac.",
                title="L0.0 To Top"
            )
            return

        # Xoa View 3D tam truoc khi Commit.
        if temp_view is not None and temp_view.IsValidObject:
            doc.Delete(temp_view.Id)

        transaction.Commit()

    except Exception as error:
        if transaction.HasStarted():
            transaction.RollBack()

        logger.error("L0.0 To Top loi: {}".format(error))
        forms.alert(
            "Lenh gap loi va toan bo thay doi da duoc hoan tac.\n\n{}".format(
                error
            ),
            title="L0.0 To Top"
        )
        return

    # ----------------------------------------------
    # Bao cao ket qua
    # ----------------------------------------------
    result_message = build_result_message(
        total_count,
        success_count,
        no_hit_count,
        no_location_count,
        pinned_count,
        failed_count
    )

    forms.alert(
        result_message,
        title="Ket qua L0.0 To Top"
    )


if __name__ == "__main__":
    main()
