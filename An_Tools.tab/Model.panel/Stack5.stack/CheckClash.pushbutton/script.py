# -*- coding: utf-8 -*-
import os
import math
from pyrevit import revit, DB, UI, forms, script
from System.Collections.Generic import List

doc = revit.doc
active_view = doc.ActiveView
cfg = script.get_config()

# 1. TÌM FILE LINK & CATEGORY
links = DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements()
loaded_links = [l for l in links if l.GetLinkDocument()]

if not loaded_links:
    forms.alert("Không tìm thấy file link nào được load trong dự án.")
    script.exit()

link_dict = {l.Name: l for l in loaded_links}
sorted_link_names = sorted(link_dict.keys())

# Lấy danh sách Model Categories
model_categories = set()
for c in doc.Settings.Categories:
    if c.CategoryType == DB.CategoryType.Model:
        model_categories.add(c.Name)
sorted_cat_names = sorted(list(model_categories))

# --- CÁC HÀM HỖ TRỢ XỬ LÝ HÌNH HỌC TỐI ƯU ---
def get_solids(element, transform=None):
    solids = []
    opt = DB.Options()
    opt.DetailLevel = DB.ViewDetailLevel.Fine 
    geom_elem = element.get_Geometry(opt)
    if not geom_elem: return solids
    
    for g in geom_elem:
        if isinstance(g, DB.Solid) and g.Volume > 0:
            s = DB.SolidUtils.CreateTransformed(g, transform) if transform else g
            solids.append(s)
        elif isinstance(g, DB.GeometryInstance):
            for ig in g.GetInstanceGeometry():
                if isinstance(ig, DB.Solid) and ig.Volume > 0:
                    s = DB.SolidUtils.CreateTransformed(ig, transform) if transform else ig
                    solids.append(s)
    return solids

def get_instance_outline(inst, transform, expand_ft=0.0):
    bb = inst.get_BoundingBox(None)
    if not bb: return None
    
    pts = [
        DB.XYZ(bb.Min.X, bb.Min.Y, bb.Min.Z), DB.XYZ(bb.Max.X, bb.Min.Y, bb.Min.Z),
        DB.XYZ(bb.Min.X, bb.Max.Y, bb.Min.Z), DB.XYZ(bb.Max.X, bb.Max.Y, bb.Min.Z),
        DB.XYZ(bb.Min.X, bb.Min.Y, bb.Max.Z), DB.XYZ(bb.Max.X, bb.Min.Y, bb.Max.Z),
        DB.XYZ(bb.Min.X, bb.Max.Y, bb.Max.Z), DB.XYZ(bb.Max.X, bb.Max.Y, bb.Max.Z)
    ]
    
    host_pts = [transform.OfPoint(pt) for pt in pts]
    min_x = min(p.X for p in host_pts) - expand_ft
    max_x = max(p.X for p in host_pts) + expand_ft
    min_y = min(p.Y for p in host_pts) - expand_ft
    max_y = max(p.Y for p in host_pts) + expand_ft
    min_z = min(p.Z for p in host_pts) - expand_ft
    max_z = max(p.Z for p in host_pts) + expand_ft
    
    return DB.Outline(DB.XYZ(min_x, min_y, min_z), DB.XYZ(max_x, max_y, max_z))

def is_within_clearance(solid_fam, solid_pipe, tol_ft):
    """Kiểm tra khoảng cách nhỏ nhất giữa 2 khối Solid dựa trên lưới tam giác"""
    try:
        fam_faces = [f for f in solid_fam.Faces]
        pipe_faces = [f for f in solid_pipe.Faces]
        
        pipe_pts = set()
        for f in pipe_faces:
            mesh = f.Triangulate()
            if mesh:
                for v in mesh.Vertices:
                    pipe_pts.add((round(v.X, 3), round(v.Y, 3), round(v.Z, 3)))
        pipe_pts_xyz = [DB.XYZ(p[0], p[1], p[2]) for p in pipe_pts]
        
        for pt in pipe_pts_xyz:
            for f in fam_faces:
                proj = f.Project(pt)
                if proj and proj.Distance <= tol_ft:
                    return True
                    
        fam_pts = set()
        for f in fam_faces:
            mesh = f.Triangulate()
            if mesh:
                for v in mesh.Vertices:
                    fam_pts.add((round(v.X, 3), round(v.Y, 3), round(v.Z, 3)))
        fam_pts_xyz = [DB.XYZ(p[0], p[1], p[2]) for p in fam_pts]
        
        for pt in fam_pts_xyz:
            for f in pipe_faces:
                proj = f.Project(pt)
                if proj and proj.Distance <= tol_ft:
                    return True
    except:
        pass
    return False

# 2. LỚP XỬ LÝ GIAO DIỆN WPF
class ClashWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        
        # Load dữ liệu cho 2 ListBox phía trên
        self.lbLinks.ItemsSource = sorted_link_names
        self.lbCategories.ItemsSource = sorted_cat_names
        
        self.master_families = cfg.get_option('saved_families', [])
        self.lbFamilies.ItemsSource = self.master_families
        
        self.current_selections = set()
        self.selected_links = []
        self.selected_families_names = []
        self.is_run_clicked = False

    def lbFamilies_SelectionChanged(self, sender, e):
        for item in e.AddedItems:
            self.current_selections.add(item)
        for item in e.RemovedItems:
            if item in self.current_selections:
                self.current_selections.remove(item)

    def txtSearch_TextChanged(self, sender, e):
        search_text = self.txtSearch.Text.lower()
        if not search_text:
            self.lbFamilies.ItemsSource = self.master_families
        else:
            filtered_list = [item for item in self.master_families if search_text in item.lower()]
            self.lbFamilies.ItemsSource = filtered_list
            
        for item in self.current_selections:
            if item in self.lbFamilies.Items:
                self.lbFamilies.SelectedItems.Add(item)

    def btnLoadFamilies_Click(self, sender, e):
        selected_link_names = [item for item in self.lbLinks.SelectedItems]
        selected_cat_names = [item for item in self.lbCategories.SelectedItems]
        
        if not selected_link_names:
            forms.alert("Vui lòng tick chọn ít nhất 1 File Link!")
            return
            
        family_set = set()
        for l_name in selected_link_names:
            l_doc = link_dict[l_name].GetLinkDocument()
            link_instances = DB.FilteredElementCollector(l_doc).OfClass(DB.FamilyInstance).WhereElementIsNotElementType().ToElements()
            
            for inst in link_instances:
                if inst.Symbol and inst.Symbol.Family and inst.Category:
                    # Nếu có chọn Category thì lọc, không chọn thì lấy hết
                    if not selected_cat_names or inst.Category.Name in selected_cat_names:
                        family_set.add(inst.Symbol.Family.Name)
                    
        self.master_families = sorted(list(family_set))
        self.lbFamilies.ItemsSource = self.master_families
        self.current_selections.clear()
        self.txtSearch.Text = ""
        
        cfg.saved_families = self.master_families
        script.save_config()
        
        msg = "Đã tìm thấy và lưu {} tên Family!".format(len(self.master_families))
        if selected_cat_names:
            msg += "\n(Đã lọc theo {} Categories)".format(len(selected_cat_names))
        forms.alert(msg)

    def btnDeleteSelected_Click(self, sender, e):
        if not self.current_selections:
            forms.alert("Vui lòng bôi đen (chọn) các Family cần xóa!")
            return
            
        self.master_families = [item for item in self.master_families if item not in self.current_selections]
        self.current_selections.clear()
        
        cfg.saved_families = self.master_families
        script.save_config()
        self.txtSearch_TextChanged(None, None)

    def btnDeleteAll_Click(self, sender, e):
        if not self.master_families: return
        if forms.alert("Bạn có chắc chắn muốn xóa TOÀN BỘ danh sách đã lưu?", yes=True, no=True):
            self.master_families = []
            self.current_selections.clear()
            cfg.saved_families = []
            script.save_config()
            self.txtSearch_TextChanged(None, None)

    def btnRun_Click(self, sender, e):
        self.selected_links = [item for item in self.lbLinks.SelectedItems]
        self.selected_families_names = list(self.current_selections)
        
        if not self.selected_links or not self.selected_families_names:
            forms.alert("Cần tick chọn ít nhất 1 File Link và 1 Family để chạy kiểm tra!")
            return
        self.is_run_clicked = True
        self.Close()

# 3. KHỞI CHẠY GIAO DIỆN
xaml_path = os.path.join(os.path.dirname(__file__), 'ui.xaml')
window = ClashWindow(xaml_path)
window.ShowDialog()

if not window.is_run_clicked:
    script.exit()

# 4. THU THẬP ĐỐI TƯỢNG ĐỂ CHẠY
instances_to_process = []
for l_name in window.selected_links:
    l_inst = link_dict[l_name]
    l_doc = l_inst.GetLinkDocument()
    transform = l_inst.GetTransform()
    
    link_instances = DB.FilteredElementCollector(l_doc).OfClass(DB.FamilyInstance).WhereElementIsNotElementType().ToElements()
    for inst in link_instances:
        if inst.Symbol and inst.Symbol.Family:
            # Thuật toán quét dựa trên tên Family đã được người dùng tích chọn
            if inst.Symbol.Family.Name in window.selected_families_names:
                instances_to_process.append((inst, transform))

if not instances_to_process:
    forms.alert("Không tìm thấy đối tượng Family nào khớp với lựa chọn của bạn trong View này.")
    script.exit()

# --- 5. TIẾN HÀNH QUÉT VA CHẠM DUNG SAI ---
t = DB.Transaction(doc, "Check Clash and Draw Detail Lines")
t.Start()

radius_ft = 500 / 304.8
view_origin = active_view.Origin
view_normal = active_view.ViewDirection

drawn_ids = List[DB.ElementId]()
is_cancelled = False
total_count = len(instances_to_process)

pipe_solid_cache = {}
tolerance_ft = 20 / 304.8  # Dung sai < 20mm

with forms.ProgressBar(title='Đang quét độ chính xác cao... ({value} / {max})', cancellable=True) as pb:
    for index, data in enumerate(instances_to_process):
        if pb.cancelled:
            is_cancelled = True
            break
            
        pb.update_progress(index, total_count)
        inst, transform = data
        
        outline = get_instance_outline(inst, transform, expand_ft=tolerance_ft)
        if not outline: continue
        
        bb_filter = DB.BoundingBoxIntersectsFilter(outline)
        candidate_pipes = DB.FilteredElementCollector(doc, active_view.Id)\
                            .OfCategory(DB.BuiltInCategory.OST_PipeCurves)\
                            .WherePasses(bb_filter)\
                            .ToElements()
        
        is_clashing = False
        
        if candidate_pipes:
            inst_solids = get_solids(inst, transform)
            
            for pipe in candidate_pipes:
                if is_clashing: break
                
                if pipe.Id not in pipe_solid_cache:
                    pipe_solid_cache[pipe.Id] = get_solids(pipe)
                pipe_solids = pipe_solid_cache[pipe.Id]
                
                for p_solid in pipe_solids:
                    if is_clashing: break
                    for i_solid in inst_solids:
                        try:
                            # 1. Giao cắt vật lý xuyên qua nhau
                            inter = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                                i_solid, p_solid, DB.BooleanOperationsType.Intersect)
                            
                            if inter and inter.Volume > 0.000001:
                                is_clashing = True
                                break
                            
                            # 2. Kiểm tra khoảng cách < 20mm
                            if is_within_clearance(i_solid, p_solid, tolerance_ft):
                                is_clashing = True
                                break
                        except:
                            try:
                                solid_filter = DB.ElementIntersectsSolidFilter(i_solid)
                                if solid_filter.PassesFilter(pipe):
                                    is_clashing = True
                                    break
                            except: pass

        if not is_clashing:
            loc = inst.Location
            if isinstance(loc, DB.LocationPoint):
                pt_transformed = transform.OfPoint(loc.Point)
                v = pt_transformed - view_origin
                dist = v.DotProduct(view_normal)
                projected_pt = pt_transformed - view_normal.Multiply(dist)
                
                try:
                    arc = DB.Arc.Create(projected_pt, radius_ft, 0, 2 * math.pi, active_view.RightDirection, active_view.UpDirection)
                    detail_curve = doc.Create.NewDetailCurve(active_view, arc)
                    drawn_ids.Add(detail_curve.Id)
                except: pass

# 6. CÔ LẬP
if drawn_ids.Count > 0:
    try:
        active_view.IsolateElementsTemporary(drawn_ids)
    except Exception:
        pass

t.Commit()

if is_cancelled:
    forms.alert("Đã HỦY tiến trình! Đã kịp vẽ và cô lập {} vị trí trước khi dừng.".format(drawn_ids.Count))
elif drawn_ids.Count == 0:
    forms.alert("Không tìm thấy vị trí nào an toàn. Tất cả Family đều đang bị va chạm hoặc nằm cách ống dưới 20mm!")
else:
    forms.alert("Hoàn thành! Đã vẽ và cô lập {} đường tròn tại các vị trí không va chạm.".format(drawn_ids.Count))