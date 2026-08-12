# -*- coding: utf-8 -*-
__title__ = "Connect Free Elements Plus"
__doc__ = "Noi dau ong ho vao Pipe Fitting/Pipe Accessory; neu khong co dau ong phu hop thi giu che do chen phu kien vao giua ong."

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType
import math

doc = revit.doc
uidoc = revit.uidoc

MAX_DIST_MM = 2000
MAX_DIST_FEET = MAX_DIST_MM / 304.8
TOL_MM = 2
TOL_FEET = TOL_MM / 304.8
MIN_PIPE_LENGTH_MM = 10
MIN_PIPE_LENGTH_FEET = MIN_PIPE_LENGTH_MM / 304.8
ANGLE_TOL_DEG = 5.0
ANGLE_TOL_COS = math.cos(math.radians(ANGLE_TOL_DEG))
AXIS_OFFSET_TOL_MM = 5.0
AXIS_OFFSET_TOL_FEET = AXIS_OFFSET_TOL_MM / 304.8


def get_category_id(element):
    try:
        return element.Category.Id.IntegerValue
    except Exception:
        return None


def get_connectors(element, only_open=False):
    """Lay connector vat ly cua Pipe, Pipe Fitting va Pipe Accessory."""
    connectors = []
    con_mgr = None

    try:
        if hasattr(element, "ConnectorManager") and element.ConnectorManager:
            con_mgr = element.ConnectorManager
        elif hasattr(element, "MEPModel") and element.MEPModel:
            con_mgr = element.MEPModel.ConnectorManager
    except Exception:
        con_mgr = None

    if con_mgr:
        for conn in con_mgr.Connectors:
            try:
                if conn.ConnectorType == DB.ConnectorType.Logical:
                    continue
                if only_open and conn.IsConnected:
                    continue
                connectors.append(conn)
            except Exception:
                continue
    return connectors


def is_pipe(element):
    return isinstance(element, DB.Plumbing.Pipe)


def is_target_item(element):
    """Doi tuong nhan dau ong: Pipe Fitting, Pipe Accessory, Plumbing Fixture."""
    if not isinstance(element, DB.FamilyInstance):
        return False
    valid_ids = [
        int(DB.BuiltInCategory.OST_PipeAccessory),
        int(DB.BuiltInCategory.OST_PipeFitting),
        int(DB.BuiltInCategory.OST_PlumbingFixtures)
    ]
    return get_category_id(element) in valid_ids


def connector_size(conn):
    """Tra ve kich thuoc dai dien de tranh noi nham size."""
    try:
        if conn.Shape == DB.ConnectorProfileType.Round:
            return conn.Radius * 2.0
        if conn.Shape == DB.ConnectorProfileType.Rectangular:
            return max(conn.Width, conn.Height)
    except Exception:
        pass
    return None


def connectors_compatible(pipe_conn, item_conn):
    """Kiem tra domain, shape va size. Khong chan neu API khong doc duoc thong tin."""
    try:
        if pipe_conn.Domain != item_conn.Domain:
            return False
    except Exception:
        pass

    try:
        if pipe_conn.Shape != item_conn.Shape:
            return False
    except Exception:
        pass

    size_1 = connector_size(pipe_conn)
    size_2 = connector_size(item_conn)
    if size_1 is not None and size_2 is not None:
        if abs(size_1 - size_2) > TOL_FEET:
            return False
    return True



def get_pipe_direction(pipe):
    """Tra ve vector don vi theo truc ong thang."""
    try:
        curve = pipe.Location.Curve
        if not isinstance(curve, DB.Line):
            return None
        vector = curve.GetEndPoint(1).Subtract(curve.GetEndPoint(0))
        if vector.GetLength() < 1e-9:
            return None
        return vector.Normalize()
    except Exception:
        return None


def get_connector_direction(conn):
    """BasisZ la huong truc cua connector MEP."""
    try:
        vector = conn.CoordinateSystem.BasisZ
        if vector.GetLength() < 1e-9:
            return None
        return vector.Normalize()
    except Exception:
        return None


def is_parallel_direction(vector_1, vector_2):
    """Cung phuong gom ca cung chieu va nguoc chieu."""
    if vector_1 is None or vector_2 is None:
        return False
    try:
        return abs(vector_1.DotProduct(vector_2)) >= ANGLE_TOL_COS
    except Exception:
        return False


def is_connector_parallel_to_pipe(pipe, item_conn):
    """Chi chap nhan connector fitting/accessory song song voi truc pipe."""
    return is_parallel_direction(get_pipe_direction(pipe), get_connector_direction(item_conn))


def is_target_on_pipe_axis(pipe, target_point):
    """Khong cho keo cheo: connector dich phai nam gan duong truc keo dai cua pipe."""
    try:
        curve = pipe.Location.Curve
        if not isinstance(curve, DB.Line):
            return False
        projection = curve.Project(target_point)
        return projection is not None and projection.Distance <= AXIS_OFFSET_TOL_FEET
    except Exception:
        return False

def get_pipe_end_connectors(pipe, only_open=True):
    """Chi lay connector End cua ong thang; sap xep theo dau 0 va dau 1 cua Location.Curve."""
    try:
        curve = pipe.Location.Curve
        if not isinstance(curve, DB.Line):
            return []
        end_0 = curve.GetEndPoint(0)
        end_1 = curve.GetEndPoint(1)
    except Exception:
        return []

    result = []
    for conn in get_connectors(pipe, only_open):
        try:
            if conn.ConnectorType != DB.ConnectorType.End:
                continue
            d0 = conn.Origin.DistanceTo(end_0)
            d1 = conn.Origin.DistanceTo(end_1)
            end_index = 0 if d0 <= d1 else 1
            result.append((conn, end_index))
        except Exception:
            continue
    return result


def pull_pipe_end_to_connector(pipe, end_index, item_conn):
    """Uu tien giu nguyen fitting/accessory va keo dau ong den dung Origin cua connector."""
    curve = pipe.Location.Curve
    if not isinstance(curve, DB.Line):
        raise Exception("Chi ho tro ong thang khi keo dau ong.")
    if not is_connector_parallel_to_pipe(pipe, item_conn):
        raise Exception("Connector cua fitting/accessory khong cung phuong voi pipe.")
    if not is_target_on_pipe_axis(pipe, item_conn.Origin):
        raise Exception("Connector dich khong nam tren truc keo dai cua pipe.")

    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    target = item_conn.Origin

    if end_index == 0:
        new_p0, new_p1 = target, p1
    else:
        new_p0, new_p1 = p0, target

    if new_p0.DistanceTo(new_p1) < MIN_PIPE_LENGTH_FEET:
        raise Exception("Ong sau khi keo qua ngan.")

    pipe.Location.Curve = DB.Line.CreateBound(new_p0, new_p1)
    doc.Regenerate()

    candidates = get_pipe_end_connectors(pipe, only_open=True)
    if not candidates:
        raise Exception("Khong tim thay connector ho cua ong sau khi keo.")

    new_pipe_conn = min(candidates, key=lambda pair: pair[0].Origin.DistanceTo(target))[0]
    if not connectors_compatible(new_pipe_conn, item_conn):
        raise Exception("Connector khong tuong thich.")

    if new_pipe_conn.Origin.DistanceTo(target) > TOL_FEET:
        raise Exception("Dau ong chua trung voi connector dich.")

    new_pipe_conn.ConnectTo(item_conn)


def find_best_free_pipe_end(item_conn, pipes, used_keys):
    """Tim dau ong ho gan nhat, uu tien dung connector cua fitting/accessory lam dich."""
    best = None
    best_dist = float('inf')

    for pipe in pipes:
        for pipe_conn, end_index in get_pipe_end_connectors(pipe, only_open=True):
            key = (pipe.Id.IntegerValue, end_index)
            if key in used_keys:
                continue
            if not connectors_compatible(pipe_conn, item_conn):
                continue
            if not is_connector_parallel_to_pipe(pipe, item_conn):
                continue
            if not is_target_on_pipe_axis(pipe, item_conn.Origin):
                continue

            dist = pipe_conn.Origin.DistanceTo(item_conn.Origin)
            if dist <= MAX_DIST_FEET and dist < best_dist:
                best = (pipe, pipe_conn, end_index)
                best_dist = dist

    return best, best_dist


def connect_detached_items(items, pipes):
    """Che do moi: keo cac dau ong ho den Pipe Fitting/Pipe Accessory dang bi dut."""
    success_connections = 0
    touched_items = set()
    used_pipe_ends = set()

    # Xu ly tung item; connector nao gan dau ong nhat se duoc noi truoc.
    for item in items:
        while True:
            open_item_conns = get_connectors(item, only_open=True)
            if not open_item_conns:
                break

            choices = []
            for item_conn in open_item_conns:
                best, dist = find_best_free_pipe_end(item_conn, pipes, used_pipe_ends)
                if best:
                    choices.append((dist, item_conn, best))

            if not choices:
                break

            choices.sort(key=lambda value: value[0])
            dist, item_conn, best = choices[0]
            pipe, old_pipe_conn, end_index = best
            key = (pipe.Id.IntegerValue, end_index)

            sub_trans = DB.SubTransaction(doc)
            sub_trans.Start()
            try:
                pull_pipe_end_to_connector(pipe, end_index, item_conn)
                sub_trans.Commit()
                used_pipe_ends.add(key)
                touched_items.add(item.Id.IntegerValue)
                success_connections += 1
            except Exception:
                sub_trans.RollBack()
                used_pipe_ends.add(key)

    return success_connections, touched_items


def insert_item_into_pipe(item, pipes):
    """Che do cu: dua item vao tim ong, cat ong va noi hai dau."""
    open_conns = get_connectors(item, only_open=True)
    if len(open_conns) != 2:
        return False, None

    item_pt = open_conns[0].Origin.Add(open_conns[1].Origin).Multiply(0.5)
    closest_pipe = None
    min_dist = float('inf')
    best_proj = None

    for pipe in list(pipes):
        try:
            pipe_curve = pipe.Location.Curve
            if not isinstance(pipe_curve, DB.Line):
                continue
            # Che do chen cu cung chi xu ly khi ca hai connector cua item
            # deu cung phuong voi truc pipe.
            if not all(is_connector_parallel_to_pipe(pipe, conn) for conn in open_conns):
                continue
            proj = pipe_curve.Project(item_pt)
            if proj and proj.Distance < min_dist:
                min_dist = proj.Distance
                closest_pipe = pipe
                best_proj = proj
        except Exception:
            continue

    if not closest_pipe or min_dist > MAX_DIST_FEET:
        return False, None

    sub_trans = DB.SubTransaction(doc)
    sub_trans.Start()
    try:
        split_pt = best_proj.XYZPoint
        translation_vec = split_pt.Subtract(item_pt)
        if translation_vec.GetLength() > TOL_FEET:
            DB.ElementTransformUtils.MoveElement(doc, item.Id, translation_vec)
            doc.Regenerate()

        open_conns = get_connectors(item, only_open=True)
        if len(open_conns) != 2:
            raise Exception("Item khong con dung 2 connector ho.")

        current_item_pt = open_conns[0].Origin.Add(open_conns[1].Origin).Multiply(0.5)
        current_curve = closest_pipe.Location.Curve
        final_project = current_curve.Project(current_item_pt)
        if not final_project:
            raise Exception("Khong chieu duoc item len ong.")
        split_pt = final_project.XYZPoint

        if split_pt.DistanceTo(current_curve.GetEndPoint(0)) < MIN_PIPE_LENGTH_FEET:
            raise Exception("Diem cat qua gan dau ong 0.")
        if split_pt.DistanceTo(current_curve.GetEndPoint(1)) < MIN_PIPE_LENGTH_FEET:
            raise Exception("Diem cat qua gan dau ong 1.")

        new_pipe_id = DB.Plumbing.PlumbingUtils.BreakCurve(doc, closest_pipe.Id, split_pt)
        new_pipe = doc.GetElement(new_pipe_id)
        doc.Regenerate()

        c1 = closest_pipe.Location.Curve
        c2 = new_pipe.Location.Curve
        p1_far = c1.GetEndPoint(0) if c1.GetEndPoint(0).DistanceTo(split_pt) > c1.GetEndPoint(1).DistanceTo(split_pt) else c1.GetEndPoint(1)
        p2_far = c2.GetEndPoint(0) if c2.GetEndPoint(0).DistanceTo(split_pt) > c2.GetEndPoint(1).DistanceTo(split_pt) else c2.GetEndPoint(1)

        if open_conns[0].Origin.DistanceTo(p1_far) < open_conns[1].Origin.DistanceTo(p1_far):
            item_conn1, item_conn2 = open_conns[0], open_conns[1]
        else:
            item_conn1, item_conn2 = open_conns[1], open_conns[0]

        p1_start, p1_end = c1.GetEndPoint(0), c1.GetEndPoint(1)
        if p1_start.DistanceTo(split_pt) < p1_end.DistanceTo(split_pt):
            closest_pipe.Location.Curve = DB.Line.CreateBound(item_conn1.Origin, p1_end)
        else:
            closest_pipe.Location.Curve = DB.Line.CreateBound(p1_start, item_conn1.Origin)

        p2_start, p2_end = c2.GetEndPoint(0), c2.GetEndPoint(1)
        if p2_start.DistanceTo(split_pt) < p2_end.DistanceTo(split_pt):
            new_pipe.Location.Curve = DB.Line.CreateBound(item_conn2.Origin, p2_end)
        else:
            new_pipe.Location.Curve = DB.Line.CreateBound(p2_start, item_conn2.Origin)

        doc.Regenerate()

        pipe1_conn = min(get_pipe_end_connectors(closest_pipe, True),
                         key=lambda pair: pair[0].Origin.DistanceTo(item_conn1.Origin))[0]
        pipe2_conn = min(get_pipe_end_connectors(new_pipe, True),
                         key=lambda pair: pair[0].Origin.DistanceTo(item_conn2.Origin))[0]

        if not connectors_compatible(pipe1_conn, item_conn1):
            raise Exception("Dau ong 1 khong tuong thich.")
        if not connectors_compatible(pipe2_conn, item_conn2):
            raise Exception("Dau ong 2 khong tuong thich.")

        pipe1_conn.ConnectTo(item_conn1)
        pipe2_conn.ConnectTo(item_conn2)

        sub_trans.Commit()
        return True, new_pipe
    except Exception:
        sub_trans.RollBack()
        return False, None


def main():
    selection_ids = uidoc.Selection.GetElementIds()
    selected_elements = [doc.GetElement(eid) for eid in selection_ids]

    if not selected_elements:
        forms.toast("Vui long chon Pipe, Pipe Fitting va Pipe Accessory.", title="Huong dan")
        try:
            refs = uidoc.Selection.PickObjects(
                ObjectType.Element,
                "Quet chon Pipe, Pipe Fitting va Pipe Accessory, sau do bam Finish"
            )
            selected_elements = [doc.GetElement(ref.ElementId) for ref in refs]
        except Exception:
            script.exit()

    pipes = [el for el in selected_elements if el and is_pipe(el)]
    items = [el for el in selected_elements if el and is_target_item(el) and get_connectors(el, True)]

    if not pipes or not items:
        forms.alert(
            "Khong tim thay du Pipe va Pipe Fitting/Pipe Accessory co connector dang ho.",
            title="Thong bao"
        )
        script.exit()

    direct_connections = 0
    inserted_items = 0

    with revit.Transaction("Connect detached pipe fittings and accessories"):
        # Uu tien 1: fitting/accessory dung yen, keo dau ong ho ve connector cua item.
        direct_connections, touched_items = connect_detached_items(items, pipes)
        doc.Regenerate()

        # Uu tien 2: giu lai cach cu cho item van con dung 2 dau ho.
        for item in items:
            if item.Id.IntegerValue in touched_items:
                continue
            if len(get_connectors(item, True)) != 2:
                continue
            ok, new_pipe = insert_item_into_pipe(item, pipes)
            if ok:
                inserted_items += 1
                if new_pipe:
                    pipes.append(new_pipe)

    forms.alert(
        "Da hoan thanh!\n\n"
        "- Dau ong duoc keo ve fitting/accessory: {}\n"
        "- Fitting/accessory duoc chen vao ong theo cach cu: {}\n"
        "- Sai so huong toi da: {} do | lech truc toi da: {} mm".format(
            direct_connections, inserted_items, ANGLE_TOL_DEG, AXIS_OFFSET_TOL_MM
        ),
        title="Hoan thanh",
        warn_icon=False
    )


if __name__ == '__main__':
    main()
