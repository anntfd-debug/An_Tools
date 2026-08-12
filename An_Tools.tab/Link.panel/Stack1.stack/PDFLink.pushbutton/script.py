# -*- coding: utf-8 -*-

import os
import re
import json

from pyrevit import forms, script

from Autodesk.Revit.DB import (
    Transaction,
    XYZ,
    ViewType,
    ImageType,
    ImageTypeOptions,
    ImageInstance,
    ImagePlacementOptions,
    BoxPlacement
)

from Autodesk.Revit.Exceptions import OperationCanceledException

try:
    from Autodesk.Revit.DB import ImageTypeSource
except:
    ImageTypeSource = None


# ============================================================
# CONFIG
# ============================================================

CONFIG_SECTION = "pdf_link_view_state"
config = script.get_config(CONFIG_SECTION)

try:
    last_pdf_path = config.pdf_path
except:
    last_pdf_path = ""

try:
    last_scale = config.scale
except:
    last_scale = "1.0"

try:
    last_page = int(config.page)
except:
    last_page = 1

try:
    last_placement_mode = config.placement_mode
except:
    last_placement_mode = "Center Active View"

try:
    linked_views = json.loads(config.linked_views)
except:
    linked_views = {}


# ============================================================
# BASIC CHECK
# ============================================================

uidoc = __revit__.ActiveUIDocument

if uidoc is None:
    forms.alert("Không có document nào đang mở.", exitscript=True)

doc = uidoc.Document
active_view = doc.ActiveView

if active_view is None:
    forms.alert("Không tìm thấy Active View.", exitscript=True)

if active_view.IsTemplate:
    forms.alert("Active View hiện tại là View Template.", exitscript=True)

if active_view.ViewType in [ViewType.Schedule]:
    forms.alert("Không thể link PDF vào Schedule View.", exitscript=True)


# ============================================================
# PDF PAGE COUNT
# ============================================================

def get_pdf_page_count(pdf_path):
    """
    Đếm số trang PDF bằng scan cấu trúc PDF.
    Không cần cài thêm thư viện ngoài.
    """
    if not os.path.exists(pdf_path):
        return 0

    try:
        with open(pdf_path, "rb") as f:
            data = f.read()

        matches = re.findall(br"/Type\s*/Page\b", data)
        count = len(matches)

        if count > 0:
            return count
    except:
        pass

    return 1


# ============================================================
# UI FORM
# ============================================================

import System
from System.Windows.Forms import (
    Form,
    Label,
    TextBox,
    Button,
    OpenFileDialog,
    ComboBox,
    DialogResult,
    FormStartPosition,
    FormBorderStyle,
    ComboBoxStyle
)
from System.Drawing import Point, Size


class LinkPdfForm(Form):
    def __init__(self, default_path, default_scale, default_page, default_placement):
        self.Text = "Link PDF to Active View"
        self.Size = Size(640, 285)
        self.StartPosition = FormStartPosition.CenterScreen
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.MaximizeBox = False
        self.MinimizeBox = False

        self.pdf_path = None
        self.scale_value = None
        self.page_number = None
        self.placement_mode = None

        # PDF file
        lbl_path = Label()
        lbl_path.Text = "PDF file:"
        lbl_path.Location = Point(15, 20)
        lbl_path.Size = Size(100, 22)
        self.Controls.Add(lbl_path)

        self.txt_path = TextBox()
        self.txt_path.Text = default_path if default_path else ""
        self.txt_path.Location = Point(120, 18)
        self.txt_path.Size = Size(370, 24)
        self.Controls.Add(self.txt_path)

        btn_browse = Button()
        btn_browse.Text = "Browse..."
        btn_browse.Location = Point(500, 16)
        btn_browse.Size = Size(90, 28)
        btn_browse.Click += self.browse_pdf
        self.Controls.Add(btn_browse)

        # Scale
        lbl_scale = Label()
        lbl_scale.Text = "Scale:"
        lbl_scale.Location = Point(15, 60)
        lbl_scale.Size = Size(100, 22)
        self.Controls.Add(lbl_scale)

        self.txt_scale = TextBox()
        self.txt_scale.Text = default_scale if default_scale else "1.0"
        self.txt_scale.Location = Point(120, 58)
        self.txt_scale.Size = Size(110, 24)
        self.Controls.Add(self.txt_scale)

        # Page
        lbl_page = Label()
        lbl_page.Text = "PDF page:"
        lbl_page.Location = Point(15, 100)
        lbl_page.Size = Size(100, 22)
        self.Controls.Add(lbl_page)

        self.cbo_page = ComboBox()
        self.cbo_page.Location = Point(120, 98)
        self.cbo_page.Size = Size(110, 24)
        self.cbo_page.DropDownStyle = ComboBoxStyle.DropDownList
        self.Controls.Add(self.cbo_page)

        btn_scan = Button()
        btn_scan.Text = "Scan pages"
        btn_scan.Location = Point(245, 96)
        btn_scan.Size = Size(100, 28)
        btn_scan.Click += self.scan_pages
        self.Controls.Add(btn_scan)

        # Placement mode
        lbl_placement = Label()
        lbl_placement.Text = "Placement:"
        lbl_placement.Location = Point(15, 140)
        lbl_placement.Size = Size(100, 22)
        self.Controls.Add(lbl_placement)

        self.cbo_placement = ComboBox()
        self.cbo_placement.Location = Point(120, 138)
        self.cbo_placement.Size = Size(300, 24)
        self.cbo_placement.DropDownStyle = ComboBoxStyle.DropDownList
        self.cbo_placement.Items.Add("Center Active View")
        self.cbo_placement.Items.Add("Pick Point in Active View")
        self.Controls.Add(self.cbo_placement)

        if default_placement == "Pick Point in Active View":
            self.cbo_placement.SelectedIndex = 1
        else:
            self.cbo_placement.SelectedIndex = 0

        # Info
        lbl_info = Label()
        lbl_info.Text = "Nếu chọn Pick Point, Revit sẽ yêu cầu click điểm đặt sau khi load PDF link."
        lbl_info.Location = Point(120, 175)
        lbl_info.Size = Size(460, 22)
        self.Controls.Add(lbl_info)

        # Buttons
        btn_ok = Button()
        btn_ok.Text = "Load / Link PDF"
        btn_ok.Location = Point(375, 210)
        btn_ok.Size = Size(110, 28)
        btn_ok.Click += self.ok_click
        self.Controls.Add(btn_ok)

        btn_cancel = Button()
        btn_cancel.Text = "Cancel"
        btn_cancel.Location = Point(500, 210)
        btn_cancel.Size = Size(90, 28)
        btn_cancel.Click += self.cancel_click
        self.Controls.Add(btn_cancel)

        self.populate_pages(default_page)

    def browse_pdf(self, sender, args):
        dialog = OpenFileDialog()
        dialog.Filter = "PDF files (*.pdf)|*.pdf"
        dialog.Title = "Select PDF file"

        current_path = self.txt_path.Text.strip()

        if current_path and os.path.exists(os.path.dirname(current_path)):
            dialog.InitialDirectory = os.path.dirname(current_path)

        if dialog.ShowDialog() == DialogResult.OK:
            self.txt_path.Text = dialog.FileName
            self.populate_pages(1)

    def scan_pages(self, sender, args):
        self.populate_pages(1)

    def populate_pages(self, preferred_page):
        self.cbo_page.Items.Clear()

        pdf_path = self.txt_path.Text.strip()

        if not pdf_path or not os.path.exists(pdf_path):
            self.cbo_page.Items.Add("1")
            self.cbo_page.SelectedIndex = 0
            return

        page_count = get_pdf_page_count(pdf_path)

        if page_count < 1:
            page_count = 1

        for i in range(1, page_count + 1):
            self.cbo_page.Items.Add(str(i))

        index = preferred_page - 1

        if index < 0:
            index = 0

        if index >= page_count:
            index = page_count - 1

        self.cbo_page.SelectedIndex = index

    def ok_click(self, sender, args):
        pdf_path = self.txt_path.Text.strip()
        scale_text = self.txt_scale.Text.strip()

        if not pdf_path:
            forms.alert("Vui lòng chọn PDF file.")
            return

        if not os.path.exists(pdf_path):
            forms.alert("PDF file không tồn tại:\n{}".format(pdf_path))
            return

        if not pdf_path.lower().endswith(".pdf"):
            forms.alert("File được chọn không phải PDF.")
            return

        try:
            scale_value = float(scale_text)
        except:
            forms.alert("Scale không hợp lệ. Ví dụ hợp lệ: 1, 0.5, 2")
            return

        if scale_value <= 0:
            forms.alert("Scale phải lớn hơn 0.")
            return

        try:
            page_number = int(str(self.cbo_page.SelectedItem))
        except:
            forms.alert("Vui lòng chọn trang PDF.")
            return

        self.pdf_path = pdf_path
        self.scale_value = scale_value
        self.page_number = page_number
        self.placement_mode = str(self.cbo_placement.SelectedItem)

        self.DialogResult = DialogResult.OK
        self.Close()

    def cancel_click(self, sender, args):
        self.DialogResult = DialogResult.Cancel
        self.Close()


form = LinkPdfForm(
    last_pdf_path,
    last_scale,
    last_page,
    last_placement_mode
)

result = form.ShowDialog()

if result != DialogResult.OK:
    script.exit()

pdf_path = form.pdf_path
scale_value = form.scale_value
page_number = form.page_number
placement_mode = form.placement_mode


# ============================================================
# HELPERS
# ============================================================

def get_active_view_center(uidoc, active_view):
    try:
        for uiview in uidoc.GetOpenUIViews():
            if uiview.ViewId == active_view.Id:
                corners = uiview.GetZoomCorners()
                p1 = corners[0]
                p2 = corners[1]

                return XYZ(
                    (p1.X + p2.X) / 2.0,
                    (p1.Y + p2.Y) / 2.0,
                    (p1.Z + p2.Z) / 2.0
                )
    except:
        pass

    try:
        return active_view.Origin
    except:
        return XYZ(0, 0, 0)


def create_pdf_link_options(pdf_path, page_number):
    if ImageTypeSource is None:
        raise Exception(
            "Revit API version này không hỗ trợ ImageTypeSource. "
            "Có thể Revit version của bạn chưa hỗ trợ link PDF bằng API."
        )

    options = ImageTypeOptions(
        pdf_path,
        False,
        ImageTypeSource.Link
    )

    try:
        options.PageNumber = page_number
    except:
        pass

    return options


def create_center_placement_options(center_point):
    """
    Điểm đặt là tâm PDF.
    """
    try:
        return ImagePlacementOptions(center_point, BoxPlacement.Center)
    except:
        placement_options = ImagePlacementOptions()

        try:
            placement_options.Location = center_point
        except:
            pass

        try:
            placement_options.PlacementPoint = BoxPlacement.Center
        except:
            pass

        return placement_options


def apply_instance_scale(image_instance, scale_value):
    try:
        image_instance.LockProportions = True
    except:
        pass

    applied = False

    try:
        image_instance.WidthScale = scale_value
        image_instance.HeightScale = scale_value
        applied = True
    except:
        pass

    if not applied:
        for pname in ["Scale", "Width Scale", "Height Scale"]:
            try:
                param = image_instance.LookupParameter(pname)
                if param and not param.IsReadOnly:
                    param.Set(scale_value)
                    applied = True
            except:
                pass

    return applied


def get_id_int(element_id):
    try:
        return element_id.IntegerValue
    except:
        try:
            return element_id.Value
        except:
            return -1


def save_linked_view_state(doc, view, pdf_path, page_number, scale_value, placement_mode):
    view_key = view.UniqueId

    linked_views[view_key] = {
        "doc_title": doc.Title,
        "view_id": get_id_int(view.Id),
        "view_name": view.Name,
        "pdf_path": pdf_path,
        "page": page_number,
        "scale": scale_value,
        "placement_mode": placement_mode
    }

    config.pdf_path = pdf_path
    config.scale = str(scale_value)
    config.page = str(page_number)
    config.placement_mode = placement_mode
    config.linked_views = json.dumps(linked_views)

    script.save_config()


def delete_element_safely(doc, element_id):
    try:
        t_del = Transaction(doc, "Delete unused PDF link after cancel")
        t_del.Start()
        doc.Delete(element_id)
        t_del.Commit()
    except:
        try:
            if t_del.HasStarted():
                t_del.RollBack()
        except:
            pass


# ============================================================
# MAIN
# ============================================================

pdf_type = None
pdf_type_id = None
placement_point = None

try:
    # --------------------------------------------------------
    # STEP 1: Load PDF link/type first
    # --------------------------------------------------------
    t1 = Transaction(doc, "Load PDF Link")
    t1.Start()

    pdf_options = create_pdf_link_options(pdf_path, page_number)
    pdf_type = ImageType.Create(doc, pdf_options)
    pdf_type_id = pdf_type.Id

    t1.Commit()

    # --------------------------------------------------------
    # STEP 2: Choose placement point
    # --------------------------------------------------------
    if placement_mode == "Pick Point in Active View":
        try:
            placement_point = uidoc.Selection.PickPoint(
                "Chọn điểm đặt PDF link. Điểm này sẽ là tâm của PDF."
            )
        except OperationCanceledException:
            # Nếu user cancel khi chọn điểm, xóa PDF type vừa load để tránh rác project
            if pdf_type_id:
                delete_element_safely(doc, pdf_type_id)

            script.exit()
    else:
        placement_point = get_active_view_center(uidoc, active_view)

    # --------------------------------------------------------
    # STEP 3: Place PDF instance
    # --------------------------------------------------------
    t2 = Transaction(doc, "Place PDF Link in Active View")
    t2.Start()

    placement_options = create_center_placement_options(placement_point)

    pdf_instance = ImageInstance.Create(
        doc,
        active_view,
        pdf_type_id,
        placement_options
    )

    scale_applied = apply_instance_scale(pdf_instance, scale_value)

    t2.Commit()

    # --------------------------------------------------------
    # STEP 4: Save state
    # --------------------------------------------------------
    save_linked_view_state(
        doc,
        active_view,
        pdf_path,
        page_number,
        scale_value,
        placement_mode
    )

    msg = "Đã link PDF vào Active View.\n\n"
    msg += "View: {}\n".format(active_view.Name)
    msg += "File:\n{}\n\n".format(pdf_path)
    msg += "Page: {}\n".format(page_number)
    msg += "Scale: {}\n".format(scale_value)
    msg += "Placement: {}\n".format(placement_mode)

    if placement_mode == "Pick Point in Active View":
        msg += "Điểm đặt: Picked point = center of PDF"
    else:
        msg += "Điểm đặt: Center PDF to center Active View"

    if not scale_applied:
        msg += "\n\nLưu ý: PDF đã link nhưng API không set được Scale trong Revit version này."

    forms.alert(msg)

except Exception as e:
    try:
        if 't1' in globals() and t1.HasStarted():
            t1.RollBack()
    except:
        pass

    try:
        if 't2' in globals() and t2.HasStarted():
            t2.RollBack()
    except:
        pass

    forms.alert("Lỗi khi link PDF:\n\n{}".format(str(e)))