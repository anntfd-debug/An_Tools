# -*- coding: utf-8 -*-
from pyrevit import revit, DB
from collections import defaultdict

# Lấy Document và Active View hiện tại
doc = revit.doc
view = doc.ActiveView

def remove_duplicate_pipe_tags():
    # Gom tất cả các tag trong view hiện tại
    tags = DB.FilteredElementCollector(doc, view.Id) \
             .OfClass(DB.IndependentTag) \
             .ToElements()

    # Dictionary để nhóm tag theo ID của ống (host element)
    tagged_dict = defaultdict(list)

    for tag in tags:
        # Kiểm tra xem tag có thuộc category Pipe Tags không
        if tag.Category and tag.Category.Id.IntegerValue == int(DB.BuiltInCategory.OST_PipeTags):
            
            # Tương thích với nhiều phiên bản Revit API (2022+ và cũ hơn)
            try:
                # Dành cho Revit 2022 trở lên
                host_ids = list(tag.GetTaggedLocalElementIds())
                if host_ids:
                    host_id = host_ids[0].IntegerValue
                    tagged_dict[host_id].append(tag)
            except AttributeError:
                # Fallback cho các bản Revit cũ hơn
                host_id = tag.TaggedLocalElementId
                if host_id != DB.ElementId.InvalidElementId:
                    tagged_dict[host_id.IntegerValue].append(tag)

    # Danh sách ID các tag cần xóa
    tags_to_delete = []
    
    for host_id, tag_list in tagged_dict.items():
        # Nếu một ống có nhiều hơn 1 tag
        if len(tag_list) > 1:
            # Giữ lại tag đầu tiên (tag_list[0]), đưa các tag từ vị trí thứ 1 trở đi vào list xóa
            for duplicate_tag in tag_list[1:]:
                tags_to_delete.append(duplicate_tag.Id)

    # Thực hiện xóa các tag thừa
    if tags_to_delete:
        # Sử dụng thư viện transaction của pyrevit cho an toàn và gọn nhẹ
        with revit.Transaction("Xóa Duplicate Pipe Tags"):
            for tag_id in tags_to_delete:
                doc.Delete(tag_id)
                
        print("✅ Thành công! Đã dọn dẹp {} pipe tag bị trùng lặp trong view hiện tại.".format(len(tags_to_delete)))
    else:
        print("✨ Không tìm thấy pipe tag nào bị trùng lặp trong view này (mỗi ống chỉ có tối đa 1 tag).")

# Chạy hàm
if __name__ == '__main__':
    remove_duplicate_pipe_tags()