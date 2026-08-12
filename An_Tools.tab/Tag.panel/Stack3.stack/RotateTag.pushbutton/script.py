# -*- coding: utf-8 -*-
import math
from pyrevit import revit, DB, forms

try:
    from pyrevit import EXEC_PARAMS
except Exception:
    EXEC_PARAMS = None


doc = revit.doc
view = revit.active_view

# Ten tham so trong Family Tag cua ban
# Luu y: parameter nay nen la kieu Angle va khong bi ReadOnly
ANGLE_PARAM_NAME = "Angle"


def is_shift_click():
    """Tra ve True khi lenh pyRevit duoc chay bang Shift + Click."""
    try:
        if bool(globals().get("__shiftclick__", False)):
            return True
    except Exception:
        pass

    try:
        if EXEC_PARAMS and bool(getattr(EXEC_PARAMS, "config_mode", False)):
            return True
    except Exception:
        pass

    return False


def get_selected_tags():
    """Lay cac IndependentTag dang duoc chon."""
    selection = revit.get_selection()
    return [t for t in selection if isinstance(t, DB.IndependentTag)]


def get_all_tags_in_active_view():
    """Lay tat ca IndependentTag trong active view."""
    return list(
        DB.FilteredElementCollector(doc, view.Id)
        .OfClass(DB.IndependentTag)
        .WhereElementIsNotElementType()
    )


def get_first_tagged_local_element(tag):
    """Lay element dau tien ma tag dang tag vao trong model hien hanh.

    Script nay chi xu ly tag local element. Neu tag vao linked model thi se bo qua.
    """
    tagged_ids = []

    try:
        if hasattr(tag, "GetTaggedLocalElementIds"):
            tagged_ids = list(tag.GetTaggedLocalElementIds())
        elif hasattr(tag, "TaggedLocalElementId"):
            tagged_ids = [tag.TaggedLocalElementId]
    except Exception:
        tagged_ids = []

    if not tagged_ids:
        return None

    tagged_id = tagged_ids[0]
    if tagged_id is None or tagged_id == DB.ElementId.InvalidElementId:
        return None

    try:
        return doc.GetElement(tagged_id)
    except Exception:
        return None


def get_element_horizontal_angle(element):
    """Tinh goc cua Pipe/Duct/Conduit theo truc XY va chuan hoa ve khoang -90 den +90 do."""
    if not isinstance(element, (DB.Plumbing.Pipe, DB.Mechanical.Duct, DB.Electrical.Conduit)):
        return None

    location = element.Location
    if not isinstance(location, DB.LocationCurve):
        return None

    curve = location.Curve
    if not isinstance(curve, DB.Line):
        return None

    direction = curve.Direction
    angle = math.atan2(direction.Y, direction.X)

    # Chuan hoa de text/tag khong bi lat nguoc
    if angle > math.pi / 2:
        angle -= math.pi
    elif angle <= -math.pi / 2:
        angle += math.pi

    return angle


def update_tag_angle(tag):
    """Cap nhat parameter Angle cho 1 tag. Tra ve: success, skipped, error_param."""
    tagged_element = get_first_tagged_local_element(tag)
    if tagged_element is None:
        return "skipped"

    angle = get_element_horizontal_angle(tagged_element)
    if angle is None:
        return "skipped"

    param = tag.LookupParameter(ANGLE_PARAM_NAME)
    if param and not param.IsReadOnly:
        try:
            param.Set(angle)
            return "success"
        except Exception:
            return "error_param"

    return "error_param"


def rotate_tags(tags, mode_name):
    if not tags:
        forms.toast("Khong co tag de xu ly.", title="Rotate Tag")
        return

    success_count = 0
    skipped_count = 0
    error_count = 0

    with revit.Transaction("Rotate Tag - {}".format(mode_name)):
        for tag in tags:
            result = update_tag_angle(tag)
            if result == "success":
                success_count += 1
            elif result == "error_param":
                error_count += 1
            else:
                skipped_count += 1

    msg = "Da cap nhat {} tag.".format(success_count)
    if skipped_count > 0:
        msg += " Bo qua {} tag.".format(skipped_count)
    if error_count > 0:
        msg += " Loi parameter {} tag.".format(error_count)

    forms.toast(msg, title="Rotate Tag")


def main():
    if is_shift_click():
        # Shift + Click: quet va rotate tat ca tag trong active view
        tags = get_all_tags_in_active_view()
        rotate_tags(tags, "Active View")
    else:
        # Click binh thuong: giu nguyen cach cu, chi rotate tag dang duoc chon
        tags = get_selected_tags()
        if not tags:
            forms.toast("Vui long chon Tag!", title="Rotate Tag")
            return
        rotate_tags(tags, "Selection")


main()
