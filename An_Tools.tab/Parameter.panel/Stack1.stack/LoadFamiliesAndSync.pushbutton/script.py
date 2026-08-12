# -*- coding: utf-8 -*-
"""
Load Selected Families And Synchronize

Chức năng:
1. Chọn chế độ Override giống Revit.
2. Chọn một hoặc nhiều file Family (*.rfa).
3. Load hoặc reload Family vào model hiện hữu.
4. Tự động Synchronize with Central.
5. Nếu model không Workshared thì tự động Save.
6. Không mở cửa sổ pyRevit Output.

Cách chọn nhiều Family:
- Ctrl + Click: chọn từng file.
- Shift + Click: chọn một dải file.
- Ctrl + A: chọn toàn bộ file trong thư mục.

Tương thích:
- pyRevit
- IronPython 2.7
- Revit 2019 trở lên
"""

import clr
import os
import traceback

from pyrevit import revit
from pyrevit import DB
from pyrevit import forms
from pyrevit import script


# ============================================================
# .NET REFERENCES
# ============================================================

clr.AddReference("System.Windows.Forms")
clr.AddReference("RevitAPIUI")

from System.Windows.Forms import OpenFileDialog
from System.Windows.Forms import DialogResult

from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI import TaskDialogResult
from Autodesk.Revit.UI import TaskDialogCommandLinkId
from Autodesk.Revit.UI import TaskDialogCommonButtons


# ============================================================
# PYTHON 2 / PYTHON 3 COMPATIBILITY
# ============================================================

try:
    unicode
except NameError:
    unicode = str


# ============================================================
# PYREVIT CONTEXT
# ============================================================

doc = revit.doc
config = script.get_config()


# ============================================================
# USER SETTINGS
# ============================================================

# True:
# Sau khi Synchronize with Central, relinquish toàn bộ
# quyền sở hữu Workset và Element.
RELINQUISH_AFTER_SYNC = True


# True:
# Model không phải Workshared sẽ tự động Save.
#
# False:
# Không tự động Save model non-workshared.
SAVE_NON_WORKSHARED_MODEL = True


# Shared Family:
#
# DB.FamilySource.Family:
# Sử dụng Shared Family từ file RFA đang load.
#
# DB.FamilySource.Project:
# Giữ Shared Family hiện hữu trong Project.
SHARED_FAMILY_SOURCE = DB.FamilySource.Family


# Giá trị sẽ được thiết lập từ hộp thoại Override.
OVERWRITE_PARAMETER_VALUES = False
OVERRIDE_MODE_NAME = u""


# ============================================================
# SMALL NOTIFICATION
# ============================================================

def show_notification(
        message,
        title=u"Load Families And Sync"):
    """
    Hiển thị thông báo nhỏ dạng Windows Toast.

    Nếu phiên bản pyRevit hiện tại không hỗ trợ Toast,
    tự động chuyển sang hộp thoại thông báo.
    """

    try:
        forms.toast(
            message,
            title=title
        )

    except Exception:
        forms.alert(
            message,
            title=title
        )


# ============================================================
# DOCUMENT VALIDATION
# ============================================================

def validate_document():
    """Kiểm tra Revit document trước khi chạy."""

    if doc is None:
        forms.alert(
            u"Không tìm thấy Revit document đang mở.",
            title=u"Load Families And Sync",
            warn_icon=True,
            exitscript=True
        )

    if doc.IsFamilyDocument:
        forms.alert(
            u"Tool này chỉ chạy trong Revit Project (.rvt).\n\n"
            u"Không thể chạy trong Family Editor (.rfa).",
            title=u"Load Families And Sync",
            warn_icon=True,
            exitscript=True
        )

    if doc.IsReadOnly:
        forms.alert(
            u"Model hiện đang ở chế độ Read Only.\n\n"
            u"Không thể load Family.",
            title=u"Load Families And Sync",
            warn_icon=True,
            exitscript=True
        )


# ============================================================
# OVERRIDE MODE
# ============================================================

def select_override_mode():
    """
    Hiển thị hai lựa chọn Override giống Revit.

    Return:
        False:
            Overwrite the existing version.

        True:
            Overwrite the existing version
            and its parameter values.

        None:
            Người dùng nhấn Cancel.
    """

    dialog = TaskDialog(
        u"Family Already Exists"
    )

    try:
        dialog.TitleAutoPrefix = False
    except Exception:
        pass

    dialog.MainInstruction = (
        u"Chọn cách xử lý Family đã tồn tại trong model"
    )

    dialog.MainContent = (
        u"Lựa chọn này sẽ áp dụng cho tất cả Family "
        u"được chọn trong lần chạy hiện tại."
    )

    dialog.AddCommandLink(
        TaskDialogCommandLinkId.CommandLink1,
        u"Overwrite the existing version",
        (
            u"Reload Family nhưng giữ lại các giá trị "
            u"Type Parameter hiện hữu trong Project."
        )
    )

    dialog.AddCommandLink(
        TaskDialogCommandLinkId.CommandLink2,
        (
            u"Overwrite the existing version "
            u"and its parameter values"
        ),
        (
            u"Reload Family và ghi đè các giá trị "
            u"Type Parameter bằng giá trị trong file RFA."
        )
    )

    dialog.CommonButtons = (
        TaskDialogCommonButtons.Cancel
    )

    result = dialog.Show()

    if result == TaskDialogResult.CommandLink1:
        return False

    if result == TaskDialogResult.CommandLink2:
        return True

    return None


# ============================================================
# FAMILY LOAD OPTIONS
# ============================================================

class FamilyLoadOptions(DB.IFamilyLoadOptions):
    """
    Xử lý Family đã tồn tại trong Project.

    OVERWRITE_PARAMETER_VALUES = False:
        Overwrite the existing version.

    OVERWRITE_PARAMETER_VALUES = True:
        Overwrite the existing version
        and its parameter values.
    """

    def OnFamilyFound(
            self,
            familyInUse,
            overwriteParameterValues):

        overwriteParameterValues.Value = (
            OVERWRITE_PARAMETER_VALUES
        )

        # True: tiếp tục reload Family.
        return True

    def OnSharedFamilyFound(
            self,
            sharedFamily,
            familyInUse,
            source,
            overwriteParameterValues):

        source.Value = SHARED_FAMILY_SOURCE

        overwriteParameterValues.Value = (
            OVERWRITE_PARAMETER_VALUES
        )

        return True


# ============================================================
# SELECT FAMILY FILES
# ============================================================

def select_family_files(initial_folder=None):
    """
    Mở cửa sổ Browser để chọn một hoặc nhiều file RFA.

    Return:
        Danh sách đường dẫn đầy đủ của các file đã chọn.

        Trả về None nếu người dùng nhấn Cancel.
    """

    dialog = OpenFileDialog()

    dialog.Title = (
        u"Chọn một hoặc nhiều Revit Family"
    )

    dialog.Filter = (
        u"Revit Family (*.rfa)|*.rfa"
    )

    dialog.DefaultExt = u"rfa"
    dialog.AddExtension = True

    # Cho phép chọn nhiều Family.
    dialog.Multiselect = True

    dialog.CheckFileExists = True
    dialog.CheckPathExists = True
    dialog.ValidateNames = True
    dialog.RestoreDirectory = True
    dialog.DereferenceLinks = True

    try:
        dialog.SupportMultiDottedExtensions = True
    except Exception:
        pass

    if initial_folder:
        try:
            if os.path.isdir(initial_folder):
                dialog.InitialDirectory = initial_folder
        except Exception:
            pass

    try:
        result = dialog.ShowDialog()

        if result != DialogResult.OK:
            return None

        selected_files = []

        for file_path in dialog.FileNames:

            path = unicode(file_path)

            if not path:
                continue

            if not os.path.isfile(path):
                continue

            if not path.lower().endswith(".rfa"):
                continue

            selected_files.append(path)

        return normalize_selected_files(
            selected_files
        )

    finally:
        try:
            dialog.Dispose()
        except Exception:
            pass


def normalize_selected_files(file_paths):
    """
    Loại bỏ đường dẫn trùng lặp.

    Việc so sánh đường dẫn không phân biệt
    chữ hoa và chữ thường.
    """

    unique_files = []
    existing_keys = set()

    for file_path in file_paths:

        try:
            normalized_path = os.path.normpath(
                file_path
            )
        except Exception:
            normalized_path = file_path

        comparison_key = normalized_path.lower()

        if comparison_key in existing_keys:
            continue

        existing_keys.add(comparison_key)
        unique_files.append(normalized_path)

    unique_files.sort(
        key=lambda path: os.path.basename(
            path
        ).lower()
    )

    return unique_files


# ============================================================
# CONFIRM LOAD
# ============================================================

def confirm_load(
        family_files,
        override_mode):
    """
    Hiển thị xác nhận ngắn trước khi load Family.
    """

    if len(family_files) == 1:
        selection_text = os.path.basename(
            family_files[0]
        )
    else:
        selection_text = (
            u"{0} Family đã được chọn"
        ).format(
            len(family_files)
        )

    message = (
        u"{0}\n\n"
        u"Chế độ Override:\n{1}\n\n"
        u"Tiếp tục Load Family và Sync model?"
    ).format(
        selection_text,
        override_mode
    )

    return forms.alert(
        message,
        title=u"Load Families And Sync",
        yes=True,
        no=True
    )


# ============================================================
# LOAD ONE FAMILY
# ============================================================

def load_family_file(
        family_path,
        load_options):
    """
    Load hoặc reload một Family.

    Return:
        status:
            "success"
            "skipped"
            "failed"

        family_name
        error_message
    """

    filename = os.path.basename(
        family_path
    )

    family_name = os.path.splitext(
        filename
    )[0]

    transaction = DB.Transaction(
        doc,
        u"Load Family - {0}".format(
            filename
        )
    )

    try:
        transaction.Start()

        family_reference = (
            clr.Reference[DB.Family]()
        )

        load_result = doc.LoadFamily(
            family_path,
            load_options,
            family_reference
        )

        if not load_result:

            if (
                transaction.GetStatus()
                == DB.TransactionStatus.Started
            ):
                transaction.RollBack()

            return (
                "skipped",
                family_name,
                (
                    u"Family không có thay đổi hoặc "
                    u"không cần reload."
                )
            )

        commit_status = transaction.Commit()

        if (
            commit_status
            != DB.TransactionStatus.Committed
        ):
            return (
                "failed",
                family_name,
                u"Transaction không thể Commit."
            )

        loaded_family = family_reference.Value

        if loaded_family is not None:
            family_name = loaded_family.Name

        return (
            "success",
            family_name,
            None
        )

    except Exception as ex:

        try:
            if (
                transaction.GetStatus()
                == DB.TransactionStatus.Started
            ):
                transaction.RollBack()
        except Exception:
            pass

        return (
            "failed",
            family_name,
            unicode(ex)
        )


# ============================================================
# SYNCHRONIZE WITH CENTRAL
# ============================================================

def synchronize_workshared_model(
        loaded_count):
    """
    Synchronize model Workshared với Central.
    """

    transact_options = None
    sync_options = None

    try:
        transact_options = (
            DB.TransactWithCentralOptions()
        )

        sync_options = (
            DB.SynchronizeWithCentralOptions()
        )

        sync_options.Comment = (
            u"Loaded or reloaded {0} Family file(s) "
            u"by pyRevit."
        ).format(
            loaded_count
        )

        sync_options.SaveLocalBefore = True
        sync_options.SaveLocalAfter = True

        if RELINQUISH_AFTER_SYNC:

            relinquish_options = (
                DB.RelinquishOptions(True)
            )

            sync_options.SetRelinquishOptions(
                relinquish_options
            )

        # Phải gọi ngoài tất cả Transaction.
        doc.SynchronizeWithCentral(
            transact_options,
            sync_options
        )

        return (
            True,
            u"Đã Synchronize with Central."
        )

    except Exception as ex:

        return (
            False,
            (
                u"Không thể Synchronize with Central: {0}"
            ).format(
                unicode(ex)
            )
        )

    finally:

        if sync_options is not None:
            try:
                sync_options.Dispose()
            except Exception:
                pass

        if transact_options is not None:
            try:
                transact_options.Dispose()
            except Exception:
                pass


# ============================================================
# SAVE NON-WORKSHARED MODEL
# ============================================================

def save_non_workshared_model():
    """
    Save model khi model không phải Workshared.
    """

    if not SAVE_NON_WORKSHARED_MODEL:
        return (
            True,
            u"Model không Workshared. Không thực hiện Save."
        )

    if not doc.PathName:
        return (
            False,
            (
                u"Model chưa được Save As nên không thể "
                u"tự động Save."
            )
        )

    try:
        if doc.IsModified:

            doc.Save()

            return (
                True,
                u"Đã Save model."
            )

        return (
            True,
            u"Model không có thay đổi cần Save."
        )

    except Exception as ex:

        return (
            False,
            u"Không thể Save model: {0}".format(
                unicode(ex)
            )
        )


def synchronize_or_save_model(
        loaded_count):
    """
    Workshared:
        Synchronize with Central.

    Non-workshared:
        Save model.
    """

    if doc.IsWorkshared:
        return synchronize_workshared_model(
            loaded_count
        )

    return save_non_workshared_model()


# ============================================================
# FORMAT FINAL MESSAGE
# ============================================================

def build_final_message(
        selected_count,
        successful_count,
        skipped_count,
        failed_count,
        sync_message,
        cancelled):
    """
    Tạo nội dung Toast kết quả.
    """

    message = (
        u"Đã xử lý {0} Family\n"
        u"Thành công: {1} | Bỏ qua: {2} | Lỗi: {3}\n"
        u"{4}"
    ).format(
        selected_count,
        successful_count,
        skipped_count,
        failed_count,
        sync_message
    )

    if cancelled:
        message += (
            u"\nQuá trình đã được dừng giữa chừng."
        )

    return message


# ============================================================
# MAIN
# ============================================================

validate_document()


# ------------------------------------------------------------
# SELECT OVERRIDE MODE
# ------------------------------------------------------------

override_selection = select_override_mode()

if override_selection is None:
    script.exit()

OVERWRITE_PARAMETER_VALUES = (
    override_selection
)

if OVERWRITE_PARAMETER_VALUES:

    OVERRIDE_MODE_NAME = (
        u"Overwrite the existing version "
        u"and its parameter values"
    )

else:

    OVERRIDE_MODE_NAME = (
        u"Overwrite the existing version"
    )


# ------------------------------------------------------------
# SELECT ONE OR MULTIPLE FAMILY FILES
# ------------------------------------------------------------

last_family_folder = getattr(
    config,
    "last_family_folder",
    None
)

selected_family_files = select_family_files(
    last_family_folder
)

if not selected_family_files:
    script.exit()


# Ghi nhớ thư mục chứa file đầu tiên.
try:
    config.last_family_folder = os.path.dirname(
        selected_family_files[0]
    )

    script.save_config()

except Exception:
    pass


# ------------------------------------------------------------
# CONFIRM LOAD
# ------------------------------------------------------------

confirmed = confirm_load(
    selected_family_files,
    OVERRIDE_MODE_NAME
)

if not confirmed:
    script.exit()


# ------------------------------------------------------------
# START NOTIFICATION
# ------------------------------------------------------------

show_notification(
    u"Đang load {0} Family..."
    .format(
        len(selected_family_files)
    )
)


# ------------------------------------------------------------
# PREPARE RESULTS
# ------------------------------------------------------------

load_options = FamilyLoadOptions()

successful_families = []
skipped_families = []
failed_families = []

cancelled_by_user = False


# ------------------------------------------------------------
# LOAD SELECTED FAMILIES
# ------------------------------------------------------------

transaction_group = DB.TransactionGroup(
    doc,
    u"Load Selected Families"
)

try:
    transaction_group.Start()

    total_files = len(
        selected_family_files
    )

    progress_title = (
        u"Loading Family {value} of {max_value}"
    )

    with forms.ProgressBar(
            title=progress_title,
            cancellable=True,
            step=1) as progress_bar:

        for index, family_path in enumerate(
                selected_family_files):

            if progress_bar.cancelled:
                cancelled_by_user = True
                break

            progress_bar.update_progress(
                index + 1,
                total_files
            )

            status, family_name, error_message = (
                load_family_file(
                    family_path,
                    load_options
                )
            )

            if status == "success":

                successful_families.append({
                    "name": family_name,
                    "path": family_path
                })

            elif status == "skipped":

                skipped_families.append({
                    "name": family_name,
                    "path": family_path,
                    "message": error_message
                })

            else:

                failed_families.append({
                    "name": family_name,
                    "path": family_path,
                    "error": error_message
                })

    # Giữ lại các Family đã load thành công,
    # kể cả khi người dùng dừng giữa chừng.
    if successful_families:
        transaction_group.Assimilate()
    else:
        transaction_group.RollBack()

except Exception:

    try:
        if (
            transaction_group.GetStatus()
            == DB.TransactionStatus.Started
        ):
            transaction_group.RollBack()
    except Exception:
        pass

    forms.alert(
        (
            u"Đã xảy ra lỗi trong quá trình load Family.\n\n"
            u"{0}"
        ).format(
            traceback.format_exc()
        ),
        title=u"Load Families And Sync",
        warn_icon=True,
        exitscript=True
    )


# ------------------------------------------------------------
# SYNCHRONIZE OR SAVE
# ------------------------------------------------------------

sync_success = False
sync_message = (
    u"Không thực hiện Synchronize."
)

if successful_families:

    sync_success, sync_message = (
        synchronize_or_save_model(
            len(successful_families)
        )
    )

else:

    sync_message = (
        u"Không có Family nào thay đổi, "
        u"không thực hiện Sync."
    )


# ------------------------------------------------------------
# FINAL SMALL NOTIFICATION
# ------------------------------------------------------------

final_message = build_final_message(
    len(selected_family_files),
    len(successful_families),
    len(skipped_families),
    len(failed_families),
    sync_message,
    cancelled_by_user
)

show_notification(
    final_message
)