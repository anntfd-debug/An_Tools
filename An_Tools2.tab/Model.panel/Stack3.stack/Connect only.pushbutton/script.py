# -*- coding: utf-8 -*-
"""
pyRevit - Connect second MEP element to first MEP element

Quy trinh:
1. Chon doi tuong thu nhat: giu nguyen vi tri.
2. Chon doi tuong thu hai: doi tuong se duoc di chuyen.
3. Tim cap connector mo gan nhau nhat.
4. Chi dich chuyen doi tuong thu hai de 2 connector trung vi tri.
5. Khong xoay, khong align goc, khong thay doi goc doc.
6. Connect connector thu hai vao connector thu nhat.

Tuong thich:
- Revit 2019-2026
- pyRevit IronPython 2.7
- Khong dung f-string
"""

from pyrevit import revit, DB, UI, script
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException


# ============================================================
# CAU HINH
# ============================================================

# True:
# Khong cho phep di chuyen doi tuong thu hai neu no dang co
# connector ket noi voi he thong khac.
#
# False:
# Van thu di chuyen doi tuong thu hai. Revit co the keo theo
# hoac thay doi cac doi tuong dang ket noi voi no.
BLOCK_SECOND_IF_ALREADY_CONNECTED = True


# Sai so vi tri theo feet.
# 1.0e-6 ft xap xi 0.0003 mm.
POSITION_TOLERANCE = 1.0e-6


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# ============================================================
# HAM HO TRO
# ============================================================

def get_category_id_value(element):
    """Lay Category Id an toan cho cac phien ban Revit."""
    try:
        if element is None or element.Category is None:
            return None

        category_id = element.Category.Id

        try:
            return category_id.IntegerValue
        except Exception:
            try:
                return category_id.Value
            except Exception:
                return None
    except Exception:
        return None


def get_allowed_category_ids():
    """Danh sach category duoc phep chon."""
    return [
        int(DB.BuiltInCategory.OST_PipeCurves),
        int(DB.BuiltInCategory.OST_PipeFitting),
        int(DB.BuiltInCategory.OST_PipeAccessory)
    ]


def is_allowed_element(element):
    """Kiem tra doi tuong co phai Pipe, Pipe Fitting hoac Accessory."""
    if element is None:
        return False

    category_value = get_category_id_value(element)
    return category_value in get_allowed_category_ids()


class PipeElementSelectionFilter(ISelectionFilter):
    """Chi cho phep chon Pipe, Pipe Fitting va Pipe Accessory."""

    def AllowElement(self, element):
        return is_allowed_element(element)

    def AllowReference(self, reference, position):
        return False


def get_connector_manager(element):
    """
    Lay ConnectorManager tu:
    - MEPCurve, vi du Pipe
    - FamilyInstance, vi du Pipe Fitting va Pipe Accessory
    """
    if element is None:
        return None

    # Pipe va cac doi tuong ke thua MEPCurve
    try:
        connector_manager = element.ConnectorManager
        if connector_manager is not None:
            return connector_manager
    except Exception:
        pass

    # FamilyInstance: fitting va accessory
    try:
        mep_model = element.MEPModel
        if mep_model is not None:
            connector_manager = mep_model.ConnectorManager
            if connector_manager is not None:
                return connector_manager
    except Exception:
        pass

    return None


def get_all_connectors(element):
    """Lay tat ca connector cua doi tuong."""
    connector_manager = get_connector_manager(element)

    if connector_manager is None:
        return []

    connectors = []

    try:
        for connector in connector_manager.Connectors:
            connectors.append(connector)
    except Exception:
        return []

    return connectors


def is_end_connector(connector):
    """
    Chi lay connector vat ly tai dau ong/fitting/accessory.
    Bo qua Logical connector.
    """
    if connector is None:
        return False

    try:
        return connector.ConnectorType == DB.ConnectorType.End
    except Exception:
        return False


def is_piping_connector(connector):
    """Chi lay connector thuoc Piping domain."""
    if connector is None:
        return False

    try:
        return connector.Domain == DB.Domain.DomainPiping
    except Exception:
        return False


def is_connector_connected(connector):
    """Kiem tra connector da co ket noi hay chua."""
    try:
        return connector.IsConnected
    except Exception:
        return False


def get_open_piping_connectors(element):
    """Lay cac piping end connector chua ket noi."""
    result = []

    for connector in get_all_connectors(element):
        if not is_end_connector(connector):
            continue

        if not is_piping_connector(connector):
            continue

        if is_connector_connected(connector):
            continue

        result.append(connector)

    return result


def has_any_connected_connector(element):
    """Kiem tra doi tuong co connector nao dang ket noi khong."""
    for connector in get_all_connectors(element):
        if not is_end_connector(connector):
            continue

        if not is_piping_connector(connector):
            continue

        if is_connector_connected(connector):
            return True

    return False


def xyz_distance(point_1, point_2):
    """Tinh khoang cach giua hai XYZ."""
    return point_1.DistanceTo(point_2)


def connectors_have_same_shape(connector_1, connector_2):
    """
    Uu tien connector cung shape:
    - Round voi Round
    - Rectangular voi Rectangular
    - Oval voi Oval

    Neu khong doc duoc shape thi khong chan ket noi.
    """
    try:
        return connector_1.Shape == connector_2.Shape
    except Exception:
        return True


def connectors_have_same_domain(connector_1, connector_2):
    """Kiem tra hai connector cung domain."""
    try:
        return connector_1.Domain == connector_2.Domain
    except Exception:
        return False


def find_nearest_connector_pair(element_1, element_2):
    """
    Tim cap connector mo gan nhau nhat.

    Return:
        connector cua element 1,
        connector cua element 2,
        khoang cach
    """
    connectors_1 = get_open_piping_connectors(element_1)
    connectors_2 = get_open_piping_connectors(element_2)

    if not connectors_1:
        raise Exception(
            "Doi tuong thu nhat khong co Piping End Connector dang mo."
        )

    if not connectors_2:
        raise Exception(
            "Doi tuong thu hai khong co Piping End Connector dang mo."
        )

    best_connector_1 = None
    best_connector_2 = None
    best_distance = None

    for connector_1 in connectors_1:
        for connector_2 in connectors_2:

            if not connectors_have_same_domain(connector_1, connector_2):
                continue

            if not connectors_have_same_shape(connector_1, connector_2):
                continue

            try:
                distance = xyz_distance(
                    connector_1.Origin,
                    connector_2.Origin
                )
            except Exception:
                continue

            if best_distance is None or distance < best_distance:
                best_connector_1 = connector_1
                best_connector_2 = connector_2
                best_distance = distance

    if best_connector_1 is None or best_connector_2 is None:
        raise Exception(
            "Khong tim thay cap connector piping phu hop giua hai doi tuong."
        )

    return best_connector_1, best_connector_2, best_distance


def get_nearest_open_connector_to_point(element, target_point):
    """
    Sau khi di chuyen va Regenerate, lay lai connector gan target_point nhat.
    Viec lay lai connector giup tranh su dung connector cu sau khi element
    da thay doi hinh hoc.
    """
    connectors = get_open_piping_connectors(element)

    nearest_connector = None
    nearest_distance = None

    for connector in connectors:
        try:
            distance = connector.Origin.DistanceTo(target_point)
        except Exception:
            continue

        if nearest_distance is None or distance < nearest_distance:
            nearest_connector = connector
            nearest_distance = distance

    return nearest_connector, nearest_distance


def element_name(element):
    """Tao ten hien thi cho thong bao."""
    if element is None:
        return "Unknown"

    category_name = "Unknown Category"

    try:
        if element.Category is not None:
            category_name = element.Category.Name
    except Exception:
        pass

    try:
        element_id = element.Id.IntegerValue
    except Exception:
        try:
            element_id = element.Id.Value
        except Exception:
            element_id = "?"

    return "{0} - Id {1}".format(category_name, element_id)


def show_error(message):
    UI.TaskDialog.Show("Connect Pipe Elements", message)


def show_success(element_1, element_2, moved_distance):
    distance_mm = moved_distance * 304.8

    message = (
        "Ket noi thanh cong.\n\n"
        "Doi tuong thu nhat:\n"
        "{0}\n"
        "- Duoc giu nguyen vi tri.\n\n"
        "Doi tuong thu hai:\n"
        "{1}\n"
        "- Da dich chuyen: {2:.2f} mm\n"
        "- Khong xoay doi tuong.\n"
        "- Khong align goc doc theo doi tuong thu nhat."
    ).format(
        element_name(element_1),
        element_name(element_2),
        distance_mm
    )

    UI.TaskDialog.Show("Connect Pipe Elements", message)


# ============================================================
# CHON DOI TUONG
# ============================================================

selection_filter = PipeElementSelectionFilter()

try:
    reference_1 = uidoc.Selection.PickObject(
        ObjectType.Element,
        selection_filter,
        "Chon doi tuong thu nhat - doi tuong nay se duoc giu nguyen"
    )

    element_1 = doc.GetElement(reference_1.ElementId)

    reference_2 = uidoc.Selection.PickObject(
        ObjectType.Element,
        selection_filter,
        "Chon doi tuong thu hai - doi tuong nay se duoc di chuyen de ket noi"
    )

    element_2 = doc.GetElement(reference_2.ElementId)

except OperationCanceledException:
    script.exit()

except Exception as selection_error:
    show_error(
        "Khong the chon doi tuong.\n\n{0}".format(
            str(selection_error)
        )
    )
    script.exit()


# ============================================================
# KIEM TRA DAU VAO
# ============================================================

if element_1 is None or element_2 is None:
    show_error("Khong lay duoc doi tuong da chon.")
    script.exit()


if element_1.Id == element_2.Id:
    show_error("Khong duoc chon cung mot doi tuong hai lan.")
    script.exit()


try:
    if element_1.Pinned:
        # Element 1 khong di chuyen, nen pinned khong phai loi.
        pass
except Exception:
    pass


try:
    if element_2.Pinned:
        show_error(
            "Doi tuong thu hai dang bi Pin.\n"
            "Hay Unpin doi tuong thu hai truoc khi chay lenh."
        )
        script.exit()
except Exception:
    pass


if BLOCK_SECOND_IF_ALREADY_CONNECTED:
    if has_any_connected_connector(element_2):
        show_error(
            "Doi tuong thu hai dang co connector ket noi voi he thong khac.\n\n"
            "Lenh da dung de tranh keo theo hoac lam thay doi he thong hien huu.\n"
            "Hay disconnect doi tuong thu hai truoc khi chay lenh."
        )
        script.exit()


# ============================================================
# TIM CONNECTOR TRUOC KHI MO TRANSACTION
# ============================================================

try:
    first_connector, second_connector, initial_distance = (
        find_nearest_connector_pair(element_1, element_2)
    )

    # Luu vi tri connector cua doi tuong thu nhat.
    target_point = first_connector.Origin

    # Vector di chuyen:
    # Connector 2 den Connector 1.
    translation = target_point - second_connector.Origin

except Exception as connector_error:
    show_error(str(connector_error))
    script.exit()


# ============================================================
# DI CHUYEN VA KET NOI
# ============================================================

transaction = DB.Transaction(
    doc,
    "Connect second pipe element to first"
)

try:
    transaction.Start()

    # Chi dich chuyen, khong Rotate.
    # Vi vay huong va goc doc cua doi tuong thu hai duoc giu nguyen.
    if translation.GetLength() > POSITION_TOLERANCE:
        DB.ElementTransformUtils.MoveElement(
            doc,
            element_2.Id,
            translation
        )

    doc.Regenerate()

    # Lay lai connector sau khi doi tuong thu hai da di chuyen.
    refreshed_first_connector, first_distance = (
        get_nearest_open_connector_to_point(
            element_1,
            target_point
        )
    )

    refreshed_second_connector, second_distance = (
        get_nearest_open_connector_to_point(
            element_2,
            target_point
        )
    )

    if refreshed_first_connector is None:
        raise Exception(
            "Khong tim lai duoc connector cua doi tuong thu nhat."
        )

    if refreshed_second_connector is None:
        raise Exception(
            "Khong tim lai duoc connector cua doi tuong thu hai."
        )

    if not connectors_have_same_domain(
        refreshed_first_connector,
        refreshed_second_connector
    ):
        raise Exception(
            "Hai connector khong cung MEP domain."
        )

    if not connectors_have_same_shape(
        refreshed_first_connector,
        refreshed_second_connector
    ):
        raise Exception(
            "Hai connector khong cung hinh dang."
        )

    # Connect second vao first.
    refreshed_second_connector.ConnectTo(
        refreshed_first_connector
    )

    doc.Regenerate()

    transaction.Commit()

except Exception as connect_error:
    if transaction.HasStarted():
        transaction.RollBack()

    show_error(
        "Khong the ket noi hai doi tuong.\n\n"
        "Ly do co the gom:\n"
        "- Hai connector khong cung kich thuoc hoac hinh dang.\n"
        "- Connector khong cho phep ket noi truc tiep.\n"
        "- Huong connector khong phu hop.\n"
        "- Doi tuong bi rang buoc boi he thong hien huu.\n"
        "- Revit can fitting trung gian de ket noi.\n\n"
        "Chi tiet:\n{0}".format(str(connect_error))
    )

    script.exit()


# ============================================================
# THONG BAO KET QUA
# ============================================================

show_success(
    element_1,
    element_2,
    initial_distance
)