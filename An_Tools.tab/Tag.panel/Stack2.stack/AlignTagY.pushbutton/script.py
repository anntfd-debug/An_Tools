# -*- coding: utf-8 -*-
from Autodesk.Revit.DB import *
from pyrevit import revit

def align_n_tags_vertical(tag_list, spacing_mm):
    doc = revit.doc
    view = doc.ActiveView
    scale = float(view.Scale)
    
    # Quy đổi khoảng cách mm trên bản vẽ ra đơn vị feet trong mô hình
    spacing_feet = (spacing_mm * scale) / 304.8
    
    # Sắp xếp các tag từ trên xuống dưới hoặc dưới lên trên
    # Ở đây: sắp xếp theo Y giảm dần (tag nào nằm cao nhất sẽ làm mốc)
    sorted_tags = sorted(tag_list, key=lambda t: t.TagHeadPosition.Y, reverse=True)
    
    # Lấy tag gốc (tag nằm cao nhất) làm mốc
    base_tag = sorted_tags[0]
    base_pos = base_tag.TagHeadPosition
    
    # Lặp qua các tag còn lại để di chuyển dọc theo trục Y
    for i in range(1, len(sorted_tags)):
        tag = sorted_tags[i]
        
        if tag.LeaderEndCondition != LeaderEndCondition.Free:
            tag.LeaderEndCondition = LeaderEndCondition.Free
        
        refs = list(tag.GetTaggedReferences())
        if not refs: continue
        ref_to_use = refs[0]
        old_leader_end = tag.GetLeaderEnd(ref_to_use)
        
        # Tính toán vị trí mới: Giữ nguyên X, giảm dần Y
        new_y = base_pos.Y - (i * spacing_feet)
        new_head_pos = XYZ(base_pos.X, new_y, base_pos.Z)
        
        # Áp dụng
        tag.TagHeadPosition = new_head_pos
        tag.SetLeaderEnd(ref_to_use, old_leader_end)

# --- CẤU HÌNH ---
SPACING_ON_SHEET_MM = 4.0 
# ----------------

selection = revit.get_selection()
tags = [x for x in selection if isinstance(x, IndependentTag)]

if len(tags) >= 2:
    with revit.Transaction("Align N Tags Vertically"):
        align_n_tags_vertical(tags, SPACING_ON_SHEET_MM)