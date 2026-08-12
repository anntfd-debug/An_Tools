# -*- coding: utf-8 -*-
from pyrevit import revit, DB, forms, script

doc = revit.doc
uidoc = revit.uidoc

def get_connectors(element):
    """Hàm lấy các Connector vật lý chuẩn cho MEP Element."""
    connectors = []
    con_mgr = None
    if hasattr(element, "ConnectorManager") and element.ConnectorManager:
        con_mgr = element.ConnectorManager
    elif hasattr(element, "MEPModel") and element.MEPModel:
        con_mgr = element.MEPModel.ConnectorManager
                
    if con_mgr:
        for c in con_mgr.Connectors:
            if c.ConnectorType != DB.ConnectorType.Logical:
                connectors.append(c)
    return connectors

def main():
    # 1. Lấy danh sách đối tượng ĐÃ CHỌN TRƯỚC trên màn hình
    selection_ids = uidoc.Selection.GetElementIds()
    
    if not selection_ids:
        forms.alert("Vui lòng CHỌN các phụ kiện cần xóa TRƯỚC khi bấm lệnh này!", title="Chưa chọn đối tượng")
        script.exit()

    selected_elements = [doc.GetElement(eid) for eid in selection_ids]

    inline_items_to_heal = []
    items_to_delete_only = []

    categories_to_check = [
        int(DB.BuiltInCategory.OST_PipeAccessory),
        int(DB.BuiltInCategory.OST_PipeFitting),
        int(DB.BuiltInCategory.OST_PlumbingFixtures)
    ]

    # 2. Phân tích trạng thái kết nối
    for el in selected_elements:
        if not el or not isinstance(el, DB.FamilyInstance):
            continue
        
        if el.Category.Id.IntegerValue in categories_to_check:
            conns = get_connectors(el)
            pipe_connections = [] 
            
            for c in conns:
                if c.IsConnected:
                    for ref in c.AllRefs:
                        if ref.ConnectorType != DB.ConnectorType.Logical and isinstance(ref.Owner, DB.Plumbing.Pipe):
                            pipe_connections.append((c, ref.Owner, ref))
            
            if len(pipe_connections) == 2:
                inline_items_to_heal.append({
                    'item': el,
                    'pipe1': pipe_connections[0][1],
                    'pipe1_conn': pipe_connections[0][2],
                    'item_conn1': pipe_connections[0][0],
                    'pipe2': pipe_connections[1][1],
                    'pipe2_conn': pipe_connections[1][2],
                    'item_conn2': pipe_connections[1][0]
                })
            else:
                items_to_delete_only.append(el)

    if not inline_items_to_heal and not items_to_delete_only:
        forms.alert("Các cấu kiện đang chọn không chứa phụ kiện thích hợp để xóa phục hồi ống!", title="Thông báo")
        script.exit()

    heal_count = 0
    delete_count = 0

    # 3. Tiến hành xử lý xóa và chữa lành ống thẳng ngay
    with revit.Transaction("Heal Selected Pipes"):
        for data in inline_items_to_heal:
            item = data['item']
            pipe1 = data['pipe1']
            pipe2 = data['pipe2']
            pipe1_conn = data['pipe1_conn']
            pipe2_conn = data['pipe2_conn']
            item_conn1 = data['item_conn1']
            item_conn2 = data['item_conn2']
            
            sub_trans = DB.SubTransaction(doc)
            sub_trans.Start()
            try:
                item_pt = item.Location.Point if hasattr(item.Location, "Point") else item_conn1.Origin.Add(item_conn2.Origin).Multiply(0.5)
                
                # Gộp ống cùng loại/cùng cỡ
                if pipe1.Diameter == pipe2.Diameter and pipe1.GetTypeId() == pipe2.GetTypeId():
                    c1 = pipe1.Location.Curve
                    p1_start, p1_end = c1.GetEndPoint(0), c1.GetEndPoint(1)
                    
                    if p1_start.DistanceTo(pipe1_conn.Origin) < p1_end.DistanceTo(pipe1_conn.Origin):
                        pt_far1 = p1_end
                        far1_is_start = False
                    else:
                        pt_far1 = p1_start
                        far1_is_start = True
                        
                    c2 = pipe2.Location.Curve
                    p2_start, p2_end = c2.GetEndPoint(0), c2.GetEndPoint(1)
                    
                    if p2_start.DistanceTo(pipe2_conn.Origin) < p2_end.DistanceTo(pipe2_conn.Origin):
                        pt_far2 = p2_end
                    else:
                        pt_far2 = p2_start
                        
                    p2_far_conn = None
                    for c in get_connectors(pipe2):
                        if c.Origin.DistanceTo(pt_far2) < 0.01 and c.Id != pipe2_conn.Id:
                            p2_far_conn = c
                            break
                            
                    ext_conn = None
                    if p2_far_conn and p2_far_conn.IsConnected:
                        for ref in p2_far_conn.AllRefs:
                            if ref.Owner.Id != pipe2.Id and ref.ConnectorType != DB.ConnectorType.Logical:
                                ext_conn = ref
                                break
                    
                    if ext_conn and p2_far_conn:
                        p2_far_conn.DisconnectFrom(ext_conn)
                    
                    pipe1_conn.DisconnectFrom(item_conn1)
                    pipe2_conn.DisconnectFrom(item_conn2)
                    
                    doc.Delete(pipe2.Id)
                    doc.Delete(item.Id)
                    doc.Regenerate()
                    
                    if far1_is_start:
                        new_curve = DB.Line.CreateBound(pt_far1, pt_far2)
                    else:
                        new_curve = DB.Line.CreateBound(pt_far2, pt_far1)
                    pipe1.Location.Curve = new_curve
                    doc.Regenerate()
                    
                    if ext_conn:
                        pipe1_conns = get_connectors(pipe1)
                        new_conn_at_far2 = min(pipe1_conns, key=lambda c: c.Origin.DistanceTo(pt_far2))
                        if not new_conn_at_far2.IsConnected:
                            new_conn_at_far2.ConnectTo(ext_conn)
                            
                # Khác kích thước -> Tạo Transition mới thay thế
                else:
                    pipe1_conn.DisconnectFrom(item_conn1)
                    pipe2_conn.DisconnectFrom(item_conn2)
                    doc.Delete(item.Id)
                    doc.Regenerate()
                    
                    c1 = pipe1.Location.Curve
                    p1_start, p1_end = c1.GetEndPoint(0), c1.GetEndPoint(1)
                    if p1_start.DistanceTo(pipe1_conn.Origin) < p1_end.DistanceTo(pipe1_conn.Origin):
                        pipe1.Location.Curve = DB.Line.CreateBound(item_pt, p1_end)
                    else:
                        pipe1.Location.Curve = DB.Line.CreateBound(p1_start, item_pt)
                        
                    c2 = pipe2.Location.Curve
                    p2_start, p2_end = c2.GetEndPoint(0), c2.GetEndPoint(1)
                    if p2_start.DistanceTo(pipe2_conn.Origin) < p2_end.DistanceTo(pipe2_conn.Origin):
                        pipe2.Location.Curve = DB.Line.CreateBound(item_pt, p2_end)
                    else:
                        pipe2.Location.Curve = DB.Line.CreateBound(p2_start, item_pt)
                    
                    doc.Regenerate()
                    doc.Create.NewTransitionFitting(pipe1_conn, pipe2_conn)
                
                sub_trans.Commit()
                heal_count += 1
            except:
                sub_trans.RollBack()
                try:
                    with DB.SubTransaction(doc) as del_sub:
                        del_sub.Start()
                        doc.Delete(item.Id)
                        del_sub.Commit()
                        delete_count += 1
                except:
                    pass

        for item in items_to_delete_only:
            try:
                doc.Delete(item.Id)
                delete_count += 1
            except:
                continue

    forms.alert("Đã hoàn thành gộp cấu kiện chọn trước!\n\n- Xóa & Nối liền ống thành công: {} vị trí.\n- Chỉ xóa cấu kiện (Không gộp ống): {} vị trí.".format(heal_count, delete_count), 
                title="Hoàn thành", warn_icon=False)

if __name__ == '__main__':
    main()