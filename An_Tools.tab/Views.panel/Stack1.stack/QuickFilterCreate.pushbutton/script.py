# -*- coding: utf-8 -*-
import clr
import os
import System
from System.Collections.Generic import List
from pyrevit import revit, DB, forms

doc = revit.doc

# Lớp hỗ trợ map dữ liệu Category ra XAML, thêm thuộc tính IsChecked
class CategoryItem(object):
    def __init__(self, name, cat_id):
        self.Name = name
        self.Id = cat_id
        self.IsChecked = False # Lưu trạng thái check để không bị mất khi search

class ParamItem(object):
    def __init__(self, name, param_id):
        self.Name = name
        self.Id = param_id

class FilterCreatorWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        self.checked_cat_ids = set() 
        self.all_cat_items = [] # Danh sách gốc lưu toàn bộ category
        self.populate_categories()

    def populate_categories(self):
        filterable_cat_ids = DB.ParameterFilterUtilities.GetAllFilterableCategories()
        
        for cat_id in filterable_cat_ids:
            try:
                cat = DB.Category.GetCategory(doc, cat_id)
                if cat:
                    self.all_cat_items.append(CategoryItem(cat.Name, cat_id))
            except:
                pass
        
        # Sắp xếp và nạp vào UI lần đầu
        self.all_cat_items.sort(key=lambda x: x.Name)
        self.lb_Categories.ItemsSource = self.all_cat_items

    # Hàm xử lý khi gõ text vào ô Search
    def tb_SearchCategory_TextChanged(self, sender, args):
        search_text = self.tb_SearchCategory.Text.lower()
        
        if not search_text:
            # Nếu xóa hết text, hiển thị lại toàn bộ
            self.lb_Categories.ItemsSource = self.all_cat_items
        else:
            # Lọc các category có tên chứa từ khóa
            filtered_items = [item for item in self.all_cat_items if search_text in item.Name.lower()]
            self.lb_Categories.ItemsSource = filtered_items

    def Category_Checked(self, sender, args):
        cat_id = sender.Tag
        is_checked = sender.IsChecked
        
        # Cập nhật trạng thái IsChecked vào danh sách gốc để giữ nguyên khi search
        for item in self.all_cat_items:
            if item.Id == cat_id:
                item.IsChecked = is_checked
                break

        if is_checked:
            self.checked_cat_ids.add(cat_id)
        else:
            self.checked_cat_ids.discard(cat_id)
            
        self.update_parameters()

    def update_parameters(self):
        if not self.checked_cat_ids:
            self.cb_Parameters.ItemsSource = None
            return

        dotnet_id_list = List[DB.ElementId]()
        for cid in self.checked_cat_ids:
            dotnet_id_list.Add(cid)

        common_param_ids = DB.ParameterFilterUtilities.GetFilterableParametersInCommon(doc, dotnet_id_list)
        
        param_items = []
        for p_id in common_param_ids:
            name = self.get_parameter_name(p_id)
            param_items.append(ParamItem(name, p_id))

        param_items.sort(key=lambda x: x.Name)
        self.cb_Parameters.ItemsSource = param_items
        if param_items:
            self.cb_Parameters.SelectedIndex = 0

    def get_parameter_name(self, param_id):
        try:
            if param_id.IntegerValue < 0:
                bip = System.Enum.ToObject(DB.BuiltInParameter, param_id.IntegerValue)
                return DB.LabelUtils.GetLabelFor(bip)
            else:
                param_elem = doc.GetElement(param_id)
                if param_elem:
                    return param_elem.Name
        except:
            return "Unknown Parameter"
        return "Unknown Parameter"

    def btn_Create_Click(self, sender, args):
        filter_name = self.tb_FilterName.Text
        selected_param = self.cb_Parameters.SelectedItem
        param_value = self.tb_Value.Text

        if not filter_name:
            forms.alert("Vui lòng nhập tên Filter.")
            return
        
        if not self.checked_cat_ids:
            forms.alert("Vui lòng chọn ít nhất 1 Category.")
            return

        cat_list = List[DB.ElementId](self.checked_cat_ids)
        self.Close()

        with revit.Transaction("Create Dynamic Filter"):
            try:
                filter_elem = DB.ParameterFilterElement.Create(doc, filter_name, cat_list)
                
                if selected_param and param_value:
                    param_id = selected_param.Id
                    evaluator = DB.FilterStringEquals()
                    
                    try:
                        rule = DB.ParameterFilterRuleFactory.CreateEqualsRule(param_id, param_value, True)
                    except TypeError:
                        rule = DB.ParameterFilterRuleFactory.CreateEqualsRule(param_id, param_value)

                    elem_filter = DB.ElementParameterFilter(rule)
                    filter_elem.SetElementFilter(elem_filter)

                active_view = doc.ActiveView
                target_view = active_view
                
                if active_view.ViewTemplateId != DB.ElementId.InvalidElementId:
                    target_view = doc.GetElement(active_view.ViewTemplateId)
                    view_type = "View Template"
                else:
                    view_type = "Active View"

                if not target_view.IsFilterApplied(filter_elem.Id):
                    target_view.AddFilter(filter_elem.Id)
                    target_view.SetFilterVisibility(filter_elem.Id, True)
                    print("=> Đã tạo thành công Filter: '{}'".format(filter_name))
                    print("=> Đã gán vào {}: {}".format(view_type, target_view.Name))
                else:
                    print("=> Filter đã tồn tại trong View/Template.")

            except Exception as e:
                forms.alert("Lỗi quá trình tạo Filter:\n{}".format(e))

# Thực thi
xaml_path = os.path.join(os.path.dirname(__file__), 'FilterUI.xaml')
dialog = FilterCreatorWindow(xaml_path)
dialog.ShowDialog()