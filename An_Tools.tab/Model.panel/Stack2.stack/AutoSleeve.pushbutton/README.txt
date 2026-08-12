AUTO SLEEVE - STRUCTURAL FRAMING + CENTERING UPDATE

Thay hai file hiện tại trong thư mục pushbutton bằng:
- script.py
- ui.xaml

Nội dung cập nhật:
1. Thêm checkbox Structural Framing.
2. Lưu trạng thái scan_beam trong settings.json.
3. Quét Structural Framing trong link gốc và nested link.
4. Tự chọn mặt beam theo hướng xuyên:
   - pháp tuyến gần nằm ngang: mặt đứng, xử lý như Wall;
   - pháp tuyến gần thẳng đứng: mặt ngang, xử lý như Floor.
5. Loại mặt đầu beam gần song song với LocationCurve của beam.
6. Dùng GeometryInstance.GetSymbolGeometry() để giữ Reference thật của mặt family khi host sleeve.
7. Tách hai điểm hình học:
   - điểm trên mặt host dùng để tạo face-based family;
   - điểm giữa đoạn MEP xuyên solid dùng làm tâm chiều dài sleeve.
8. Sau khi tạo và gán chiều dày, tool tự căn LocationPoint của sleeve về tâm kết cấu:
   - ưu tiên ElementTransformUtils.MoveElement;
   - nếu host constraint chặn, tự thử INSTANCE_FREE_HOST_OFFSET_PARAM theo cả hai chiều.
9. pyRevit Output báo trạng thái căn tâm và sai lệch còn lại theo mm nếu chưa đạt.
10. Sau khi chạy không mở cửa sổ WPF hoặc forms.alert kết quả; chỉ dùng pyRevit Output.

Lưu ý:
- Checkbox Structural Framing mặc định tắt để không thay đổi phạm vi quét của cấu hình cũ.
- Dung sai kiểm tra căn tâm hiện tại là 0.5 mm.
- Chưa thể chạy kiểm thử trực tiếp trong Revit từ môi trường này; đã kiểm tra cú pháp Python và cấu trúc XML/XAML.
