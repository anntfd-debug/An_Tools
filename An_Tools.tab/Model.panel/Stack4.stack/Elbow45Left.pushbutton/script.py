# -*- coding: utf-8 -*-
import math

from pyrevit import revit, DB, UI, forms
from Autodesk.Revit.Exceptions import OperationCanceledException


doc = revit.doc
uidoc = revit.uidoc


class PipeSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, DB.Plumbing.Pipe)

    def AllowReference(self, ref, pos):
        return True


def get_closest_end_connector(element, pick_point):
    """Lấy end connector gần điểm click nhất."""
    connector_manager = getattr(element, "ConnectorManager", None)
    if connector_manager is None:
        return None

    closest_conn = None
    min_dist = float("inf")

    for conn in connector_manager.Connectors:
        # Chỉ xét connector ở hai đầu ống, bỏ qua connector logic nếu có.
        if conn.ConnectorType != DB.ConnectorType.End:
            continue

        dist = conn.Origin.DistanceTo(pick_point)
        if dist < min_dist:
            min_dist = dist
            closest_conn = conn

    return closest_conn


def get_pipe_data(pipe):
    """Đọc các dữ liệu cần thiết để tạo đoạn ống mới."""
    system_param = pipe.get_Parameter(
        DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM
    )
    diameter_param = pipe.get_Parameter(
        DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM
    )

    if system_param is None:
        raise Exception("Không đọc được Piping System Type của ống gốc.")

    system_type_id = system_param.AsElementId()
    if system_type_id == DB.ElementId.InvalidElementId:
        raise Exception("Ống gốc chưa có Piping System Type hợp lệ.")

    reference_level = pipe.ReferenceLevel
    if reference_level is None:
        raise Exception("Không đọc được Reference Level của ống gốc.")

    diameter = diameter_param.AsDouble() if diameter_param else None

    return (
        system_type_id,
        pipe.GetTypeId(),
        reference_level.Id,
        diameter,
    )


def main():
    try:
        ref = uidoc.Selection.PickObject(
            UI.Selection.ObjectType.Element,
            PipeSelectionFilter(),
            "Chọn một điểm gần đầu ống để tạo co 45 độ sang trái"
        )
    except OperationCanceledException:
        # Nhấn ESC/Cancel: thoát lệnh bình thường, không hiện traceback.
        return

    pipe = doc.GetElement(ref.ElementId)
    pick_point = ref.GlobalPoint

    if pipe is None or pick_point is None:
        forms.alert("Không đọc được ống hoặc vị trí click.", exitscript=True)

    conn1 = get_closest_end_connector(pipe, pick_point)

    if conn1 is None:
        forms.alert("Không tìm thấy end connector trên ống này.", exitscript=True)

    if conn1.IsConnected:
        forms.alert(
            "Đầu ống này đã được kết nối. Vui lòng chọn một đầu ống hở.",
            exitscript=True
        )

    try:
        system_type_id, pipe_type_id, level_id, diameter = get_pipe_data(pipe)
    except Exception as ex:
        forms.alert(str(ex), exitscript=True)

    # BasisZ của connector đầu ống là hướng đi ra khỏi đầu ống.
    v_out = conn1.CoordinateSystem.BasisZ.Normalize()

    # Xoay 45 độ sang trái quanh trục Z toàn cục.
    # Phép xoay này giữ nguyên thành phần Z nên giữ nguyên độ dốc.
    rot_transform = DB.Transform.CreateRotation(
        DB.XYZ.BasisZ,
        math.pi / 4.0
    )
    v_new = rot_transform.OfVector(v_out).Normalize()

    p_start = conn1.Origin
    new_pipe_length = 2.0  # feet, xấp xỉ 610 mm
    p_end = p_start + v_new.Multiply(new_pipe_length)

    transaction = DB.Transaction(doc, "Tạo Elbow 45 độ sang trái")

    try:
        transaction.Start()

        new_pipe = DB.Plumbing.Pipe.Create(
            doc,
            system_type_id,
            pipe_type_id,
            level_id,
            p_start,
            p_end
        )

        if diameter is not None:
            new_diameter_param = new_pipe.get_Parameter(
                DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM
            )
            if new_diameter_param and not new_diameter_param.IsReadOnly:
                new_diameter_param.Set(diameter)

        # Cập nhật hình học/connector sau khi tạo và đổi đường kính.
        doc.Regenerate()

        conn2 = get_closest_end_connector(new_pipe, p_start)
        if conn2 is None:
            raise Exception("Không tìm thấy connector đầu của đoạn ống mới.")

        doc.Create.NewElbowFitting(conn1, conn2)

        transaction.Commit()

    except Exception as ex:
        # Không để lại đoạn ống rời nếu fitting tạo thất bại.
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()

        forms.alert(
            "Không thể tạo fitting 45 độ.\n\n"
            "Hãy kiểm tra:\n"
            "- Routing Preferences của Pipe Type;\n"
            "- Family elbow có hỗ trợ góc 45 độ;\n"
            "- Đầu ống đang chọn thực sự đang hở;\n"
            "- Đường kính và khoảng hình học hợp lệ.\n\n"
            "Chi tiết lỗi:\n{}".format(ex)
        )
        return


if __name__ == "__main__":
    main()
