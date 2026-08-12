# -*- coding: utf-8 -*-
__title__ = "Hide Filled Annotation"
__doc__ = "Hide in view tất cả Independent Tag, Spot Elevation và Text Note nằm dưới Filled Region"

import clr
clr.AddReference('RevitAPI')

from Autodesk.Revit.DB import *
from System.Collections.Generic import List

from pyrevit import revit, forms


doc = revit.doc


def get_uv(point, view):
    """
    Chuyển tọa độ XYZ về hệ tọa độ 2D của View hiện tại.
    """
    u = point.DotProduct(view.RightDirection)
    v = point.DotProduct(view.UpDirection)
    return u, v


def is_point_in_polygon_view(point, curve_loops, view):
    """
    Kiểm tra một điểm có nằm trong Filled Region hay không bằng Ray-Casting.
    Dùng được cho Plan, Section, Elevation, Detail và Drafting View.
    """
    inside = False
    pt_u, pt_v = get_uv(point, view)

    for loop in curve_loops:
        for curve in loop:
            pts = curve.Tessellate()
            for i in range(len(pts) - 1):
                p1_u, p1_v = get_uv(pts[i], view)
                p2_u, p2_v = get_uv(pts[i + 1], view)

                if (p1_v > pt_v) != (p2_v > pt_v):
                    intersect_u = (
                        (p2_u - p1_u) * (pt_v - p1_v) /
                        (p2_v - p1_v) + p1_u
                    )
                    if pt_u < intersect_u:
                        inside = not inside

    return inside


def get_bbox_center(element, view):
    """Lấy tâm BoundingBox của annotation trong View."""
    try:
        bbox = element.get_BoundingBox(view)
        if bbox:
            return (bbox.Min + bbox.Max) / 2.0
    except:
        pass
    return None


def append_point(points, point):
    """Thêm điểm hợp lệ và hạn chế điểm trùng."""
    if point is None:
        return

    for existing in points:
        try:
            if existing.DistanceTo(point) < 1e-9:
                return
        except:
            pass

    points.append(point)


def get_tag_test_points(tag, view):
    """
    Các điểm đại diện cho Independent Tag.
    Ưu tiên TagHeadPosition, sau đó dùng tâm BoundingBox.
    """
    points = []

    try:
        if tag.HasTagHeadPosition:
            append_point(points, tag.TagHeadPosition)
    except:
        pass

    append_point(points, get_bbox_center(tag, view))
    return points


def get_spot_test_points(spot, view):
    """
    Các điểm đại diện cho Spot Elevation.

    - Origin: vị trí đường kích thước/text của Spot.
    - Tâm BoundingBox: dự phòng khi Spot có leader hoặc Origin không đọc được.
    - LocationPoint: điểm mà Spot đang đo tới, dùng làm phương án cuối.
    """
    points = []

    try:
        append_point(points, spot.Origin)
    except:
        pass

    append_point(points, get_bbox_center(spot, view))

    try:
        location = spot.Location
        if isinstance(location, LocationPoint):
            append_point(points, location.Point)
    except:
        pass

    return points


def get_text_note_test_points(text_note, view):
    """
    Các điểm đại diện cho Text Note.

    - Coord: điểm định vị của Text Note.
    - Tâm BoundingBox: phù hợp hơn với Text Note dài, nhiều dòng hoặc có leader.
    - LocationPoint: phương án dự phòng cho các phiên bản Revit khác nhau.
    """
    points = []

    try:
        append_point(points, text_note.Coord)
    except:
        pass

    append_point(points, get_bbox_center(text_note, view))

    try:
        location = text_note.Location
        if isinstance(location, LocationPoint):
            append_point(points, location.Point)
    except:
        pass

    return points


def can_process_element(element, view):
    """Bỏ qua đối tượng đã ẩn hoặc không cho phép Hide in View."""
    try:
        if element.IsHidden(view):
            return False
    except:
        return False

    try:
        if not element.CanBeHidden(view):
            return False
    except:
        # Một số phiên bản Revit có thể không trả về ổn định cho annotation;
        # vẫn cho phép thử HideElements như hành vi cũ của tool.
        pass

    return True


def is_under_any_filled_region(test_points, filled_boundaries, view):
    """Trả về True nếu ít nhất một điểm đại diện nằm trong Filled Region."""
    for point in test_points:
        for boundaries in filled_boundaries:
            if is_point_in_polygon_view(point, boundaries, view):
                return True
    return False


class ViewOption(forms.TemplateListItem):
    """Định dạng tên View trong SelectFromList."""

    @property
    def name(self):
        return "[{}] {}".format(self.item.ViewType, self.item.Name)


def main():
    # 1. Chọn phạm vi chạy
    options = {
        "Chỉ chạy trên View hiện tại (Active View)": "active",
        "Mở bảng chọn nhiều View (Có Filter)": "multi"
    }

    selected_option = forms.CommandSwitchWindow.show(
        options.keys(),
        message="Bạn muốn ẩn Tag, Spot Elevation và Text Note dưới Filled Region ở đâu?"
    )

    if not selected_option:
        return

    mode = options[selected_option]
    selected_views = []

    valid_view_types = [
        ViewType.FloorPlan,
        ViewType.CeilingPlan,
        ViewType.EngineeringPlan,
        ViewType.Section,
        ViewType.Elevation,
        ViewType.Detail,
        ViewType.DraftingView
    ]

    # 2. Lấy danh sách View
    if mode == "active":
        active_view = doc.ActiveView
        if active_view.ViewType not in valid_view_types:
            forms.alert(
                "View hiện hành không hỗ trợ Filled Region / Annotation 2D.",
                title="Thông báo"
            )
            return

        selected_views.append(active_view)

    elif mode == "multi":
        all_views = (
            FilteredElementCollector(doc)
            .OfCategory(BuiltInCategory.OST_Views)
            .WhereElementIsNotElementType()
            .ToElements()
        )

        selectable_views = []
        for view in all_views:
            if not view.IsTemplate and view.ViewType in valid_view_types:
                selectable_views.append(view)

        view_options = [ViewOption(view) for view in selectable_views]
        view_options.sort(key=lambda item: item.name)

        selected_multi_views = forms.SelectFromList.show(
            view_options,
            title="Chọn View để ẩn Tag / Spot Elevation / Text Note",
            multiselect=True,
            button_name="Thực thi ẩn Annotation"
        )

        if not selected_multi_views:
            return

        selected_views = selected_multi_views

    # 3. Xử lý
    total_tags_hidden = 0
    total_spots_hidden = 0
    total_text_notes_hidden = 0
    views_processed = 0

    with revit.Transaction("Hide Tags, Spot Elevations and Text Notes under Filled Regions"):
        for view in selected_views:
            filled_regions = (
                FilteredElementCollector(doc, view.Id)
                .OfClass(FilledRegion)
                .ToElements()
            )

            if not filled_regions:
                continue

            # Cache boundary để không gọi GetBoundaries lặp lại cho từng annotation.
            filled_boundaries = []
            for filled_region in filled_regions:
                try:
                    filled_boundaries.append(filled_region.GetBoundaries())
                except:
                    pass

            if not filled_boundaries:
                continue

            tags = (
                FilteredElementCollector(doc, view.Id)
                .OfClass(IndependentTag)
                .WhereElementIsNotElementType()
                .ToElements()
            )

            spot_elevations = (
                FilteredElementCollector(doc, view.Id)
                .OfCategory(BuiltInCategory.OST_SpotElevations)
                .OfClass(SpotDimension)
                .WhereElementIsNotElementType()
                .ToElements()
            )

            text_notes = (
                FilteredElementCollector(doc, view.Id)
                .OfClass(TextNote)
                .WhereElementIsNotElementType()
                .ToElements()
            )

            ids_to_hide = []
            hidden_tags_in_view = 0
            hidden_spots_in_view = 0
            hidden_text_notes_in_view = 0

            for tag in tags:
                if not can_process_element(tag, view):
                    continue

                test_points = get_tag_test_points(tag, view)
                if not test_points:
                    continue

                if is_under_any_filled_region(test_points, filled_boundaries, view):
                    ids_to_hide.append(tag.Id)
                    hidden_tags_in_view += 1

            for spot in spot_elevations:
                if not can_process_element(spot, view):
                    continue

                test_points = get_spot_test_points(spot, view)
                if not test_points:
                    continue

                if is_under_any_filled_region(test_points, filled_boundaries, view):
                    ids_to_hide.append(spot.Id)
                    hidden_spots_in_view += 1

            for text_note in text_notes:
                if not can_process_element(text_note, view):
                    continue

                test_points = get_text_note_test_points(text_note, view)
                if not test_points:
                    continue

                if is_under_any_filled_region(test_points, filled_boundaries, view):
                    ids_to_hide.append(text_note.Id)
                    hidden_text_notes_in_view += 1

            if ids_to_hide:
                view.HideElements(List[ElementId](ids_to_hide))

                total_tags_hidden += hidden_tags_in_view
                total_spots_hidden += hidden_spots_in_view
                total_text_notes_hidden += hidden_text_notes_in_view
                views_processed += 1

    # 4. Kết quả
    total_hidden = total_tags_hidden + total_spots_hidden + total_text_notes_hidden

    if total_hidden > 0:
        result_msg = (
            "Đã hoàn tất!\n\n"
            "👉 Independent Tag đã ẩn: **{}**\n"
            "👉 Spot Elevation đã ẩn: **{}**\n"
            "👉 Text Note đã ẩn: **{}**\n"
            "👉 Tổng cộng: **{}** annotation\n"
            "👉 Có đối tượng được ẩn trên: **{}/{}** View."
        ).format(
            total_tags_hidden,
            total_spots_hidden,
            total_text_notes_hidden,
            total_hidden,
            views_processed,
            len(selected_views)
        )

        forms.alert(result_msg, title="Thành công")
    else:
        forms.alert(
            "Không tìm thấy Independent Tag, Spot Elevation hoặc Text Note nào "
            "bị Filled Region che khuất trong (các) View đã chọn.",
            title="Kết quả"
        )


if __name__ == '__main__':
    main()
