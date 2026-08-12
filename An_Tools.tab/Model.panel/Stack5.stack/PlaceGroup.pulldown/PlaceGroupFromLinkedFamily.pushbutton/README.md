# Place Group From Linked Families — v7 Duplicate-Safe

Tool pyRevit đặt Model Group theo FamilyInstance nằm trong một hoặc nhiều Revit Link, tại bốn góc cục bộ của Family và xoay theo hướng Family.

## Xử lý trùng theo bốn lớp

### 1. Bỏ nested Family

Khi bật **Bỏ qua nested Family có SuperComponent**, tool loại các `FamilyInstance` nằm bên trong một Family cha:

```python
family_instance.SuperComponent is not None
```

Điều này ngăn thiết bị chính và shared nested Family cùng tạo Group.

### 2. Loại nguồn trùng tuyệt đối

Tool luôn tạo khóa nguồn:

```text
Revit Link Instance UniqueId + FamilyInstance UniqueId
```

Mỗi khóa chỉ được giữ một lần. Cơ chế này áp dụng khi:

- Quét Family mới.
- Ghi cache.
- Đọc cache.
- Chuẩn hóa lại danh sách ngay trước transaction.

### 3. Loại thiết bị nguồn chồng vị trí

Khi bật **Loại Family nguồn cùng Category + Family + Type bị chồng vị trí**, tool so sánh điểm đặt thật của Family trong hệ tọa độ host.

Hai nguồn chỉ được xem là trùng khi đồng thời:

- Cùng Category.
- Cùng Family Name.
- Cùng Type Name.
- Khoảng cách giữa hai điểm nguồn nhỏ hơn dung sai người dùng nhập.

Mặc định dung sai là `10 mm`. Cơ chế này xử lý trường hợp hai Revit Link chồng lên nhau hoặc cùng một thiết bị xuất hiện từ hai nguồn gần như trùng vị trí.

### 4. Kiểm tra Group đã tồn tại

Đây là lớp riêng sau khi đã chuẩn hóa Family nguồn. Tool kiểm tra các Group cùng Group Type đã có gần điểm đặt và bỏ qua theo dung sai Group.

Hai dung sai độc lập:

- **Dung sai nhận diện nguồn chồng vị trí**: dùng trước khi tạo Group.
- **Dung sai kiểm tra Group đã có**: dùng trong transaction đặt Group.

## Báo cáo pyRevit

Sau khi chạy, output pyRevit hiển thị:

- Tổng FamilyInstance ứng viên.
- Số nested Family bị bỏ.
- Số nguồn trùng tuyệt đối bị bỏ.
- Số Family khớp text.
- Số Family không hiển thị trong Active View.
- Số nguồn chồng vị trí bị bỏ.
- Tổng Family nguồn duy nhất cuối cùng.
- Số Group đã tạo.
- Số Group đã tồn tại bị bỏ qua.
- Danh sách nguồn bị loại vì trùng.
- Danh sách Group đã tạo, có liên kết chọn và zoom.

Bảng Group có thêm:

- Revit Link Instance ID.
- Family Element ID trong Link.
- SuperComponent ID.
- Điểm nguồn X/Y/Z trong host.
- Điểm đặt Group X/Y/Z.

## Cache bền

Cache chỉ được dùng lại khi không đổi:

- Project.
- Active View và trạng thái Crop/Section/View Range.
- Các Revit Link đã pick và transform của Link.
- Linked View đang sử dụng.
- Nội dung lọc Family/Type.
- Option bỏ nested Family.
- Option loại nguồn chồng vị trí.
- Dung sai nhận diện nguồn chồng vị trí.

Cache lưu danh sách đã loại trùng. Khi phục hồi, tool vẫn kiểm tra lại khóa nguồn và nested Family để phòng dữ liệu cache lỗi.

## Chức năng khác

- Pick nhiều Revit Link.
- Chỉ xử lý Link hiển thị trong Active View.
- Lọc Family/Type bằng ô nhiều dòng; mỗi dòng, dấu `;` hoặc `,` là điều kiện OR.
- Lọc theo Crop Box, Section Box, Category visibility và Plan View Range.
- Hỗ trợ By Host View và By Linked View.
- Đặt Group tại:
  - `LEFT_FRONT`
  - `RIGHT_FRONT`
  - `LEFT_BACK`
  - `RIGHT_BACK`
- Offset X/Y nhận số âm và dương.
- Xoay theo HandOrientation hoặc FacingOrientation.
- Tạo thử đúng Family thứ N trong danh sách nguồn duy nhất.
- Dùng Solid thật hoặc BoundingBox dự phòng.
- Progress bar có Cancel.
- Cancel lúc đặt Group rollback toàn bộ lần chạy.

## Quy trình khuyến nghị

1. Chạy tool và pick các Link.
2. Nhập Family/Type cần lọc.
3. Giữ bật **Bỏ nested Family**.
4. Giữ bật **Loại nguồn chồng vị trí**, bắt đầu với dung sai `10 mm`.
5. Bấm **Kiểm tra số lượng**.
6. Xem số nguồn bị loại và danh sách chi tiết trong output sau khi chạy.
7. Tạo thử một Family thứ N.
8. Kiểm tra Group bằng liên kết Zoom.
9. Bỏ chế độ thử và tạo hàng loạt.

## Cấu trúc

```text
PlaceGroupFromLinkedFamily.pushbutton
├── script.py
├── ui.xaml
├── bundle.yaml
└── README.md
```

Tool dùng cú pháp tương thích IronPython 2.7 của pyRevit.
