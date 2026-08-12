# -*- coding: utf-8 -*-
"""
Auto Connect Offset Pipes
pyRevit - IronPython 2.7

Quet chon nhieu pipe bang khung chu nhat, tu dong tim cac dau ong ho
cua hai pipe thang, gan song song, khac cao do va tao rolling offset
voi 2 elbow theo goc 90, 60, 45, 30, 22.5, 15 hoac goc tuy chon.
Goc duoc ghi nho; Shift+Click de mo lai giao dien thay doi goc.

Gioi han cua phien ban dau:
- Chi xu ly Pipe trong model hien tai, khong xu ly Revit Link.
- Pipe goc phai la doan thang va gan nam ngang.
- Hai dau can noi phai dang ho.
- Hai pipe phai cung duong kinh va cung Piping System Type.
- Moi pipe chi duoc ghep mot lan trong mot lan chay.
- Pipe Type can co Routing Preferences va elbow family ho tro goc da chon.
"""

import math
import traceback

from pyrevit import revit, DB, forms, script, EXEC_PARAMS
from Autodesk.Revit.DB.Plumbing import Pipe
from Autodesk.Revit.UI.Selection import ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
config = script.get_config()

FT_TO_MM = 304.8
EPS = 1.0e-9

try:
    text_type = unicode
except NameError:
    text_type = str

# -----------------------------------------------------------------------------
# CAC GIOI HAN CO THE CHINH SUA
# -----------------------------------------------------------------------------
MAX_PAIR_DISTANCE_MM = 3000.0     # Khoang cach 3D toi da de xem la mot cap
MAX_ENDPOINT_MOVE_MM = 5000.0    # Moi dau pipe duoc keo dai/rut ngan toi da
MIN_REMAINING_PIPE_MM = 100.0     # Chieu dai toi thieu cua pipe cu sau khi sua
PARALLEL_TOLERANCE_DEG = 2.0      # Sai so song song giua hai pipe
MAX_BASE_PIPE_SLOPE_DEG = 1.0     # Pipe goc phai gan nam ngang
DIAMETER_TOLERANCE_MM = 0.2
CONNECTOR_SEARCH_TOLERANCE_MM = 5.0
MIN_NEW_PIPE_LENGTH_MM = 10.0


# -----------------------------------------------------------------------------
# THONG BAO AN TOAN / CHAN DOAN
# -----------------------------------------------------------------------------
def safe_alert(message, title=u"Auto Connect Offset Pipes", warn=False):
    """Hien thong bao bang pyRevit forms; neu forms loi thi dung Revit TaskDialog."""
    try:
        return forms.alert(
            message,
            title=title,
            warn_icon=warn
        )
    except Exception:
        try:
            DB.TaskDialog.Show(title, message)
        except Exception:
            pass
        return None


def show_fatal_error(ex):
    details = traceback.format_exc()
    message = (
        u"Tool da dung do loi:\n\n{0}\n\n"
        u"Chi tiet da duoc ghi vao pyRevit Output."
    ).format(text_type(ex))

    try:
        output.print_md(u"# Auto Connect Offset Pipes - Fatal Error")
        output.print_md(u"```\n{0}\n```".format(details))
        output.show()
    except Exception:
        pass

    safe_alert(message, warn=True)


# -----------------------------------------------------------------------------
# DON VI VA HINH HOC
# -----------------------------------------------------------------------------
def mm_to_ft(value_mm):
    return float(value_mm) / FT_TO_MM


def ft_to_mm(value_ft):
    return float(value_ft) * FT_TO_MM


def deg_to_rad(value_deg):
    return float(value_deg) * math.pi / 180.0


def rad_to_deg(value_rad):
    return float(value_rad) * 180.0 / math.pi


def eid_value(element_id):
    try:
        return int(element_id.Value)
    except Exception:
        return int(element_id.IntegerValue)


def same_id(id1, id2):
    if id1 is None or id2 is None:
        return False
    return eid_value(id1) == eid_value(id2)


def xyz_length(vector):
    return math.sqrt(
        vector.X * vector.X +
        vector.Y * vector.Y +
        vector.Z * vector.Z
    )


def xy_length(vector):
    return math.sqrt(vector.X * vector.X + vector.Y * vector.Y)


def normalize_xy(vector):
    length = xy_length(vector)
    if length < EPS:
        return None
    return DB.XYZ(vector.X / length, vector.Y / length, 0.0)


def dot3(vector1, vector2):
    return (
        vector1.X * vector2.X +
        vector1.Y * vector2.Y +
        vector1.Z * vector2.Z
    )


def dot2(vector1, vector2):
    return vector1.X * vector2.X + vector1.Y * vector2.Y


def negate(vector):
    return DB.XYZ(-vector.X, -vector.Y, -vector.Z)


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def angle_between_deg(vector1, vector2):
    length1 = xyz_length(vector1)
    length2 = xyz_length(vector2)
    if length1 < EPS or length2 < EPS:
        return None

    cosine = clamp(dot3(vector1, vector2) / (length1 * length2), -1.0, 1.0)
    return rad_to_deg(math.acos(cosine))


# -----------------------------------------------------------------------------
# PIPE / CONNECTOR
# -----------------------------------------------------------------------------
def get_pipe_line(pipe):
    try:
        curve = pipe.Location.Curve
        if isinstance(curve, DB.Line):
            return curve
    except Exception:
        pass
    return None


def get_pipe_diameter(pipe):
    parameter = pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    if parameter is None or not parameter.HasValue:
        return None
    return parameter.AsDouble()


def get_system_type_id(pipe):
    parameter = pipe.get_Parameter(
        DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM
    )
    if parameter is not None and parameter.HasValue:
        system_type_id = parameter.AsElementId()
        if system_type_id is not None and eid_value(system_type_id) > 0:
            return system_type_id

    try:
        mep_system = pipe.MEPSystem
        if mep_system is not None:
            system_type_id = mep_system.GetTypeId()
            if system_type_id is not None and eid_value(system_type_id) > 0:
                return system_type_id
    except Exception:
        pass

    return None


def get_level_id(pipe):
    try:
        level = pipe.ReferenceLevel
        if level is not None:
            return level.Id
    except Exception:
        pass

    try:
        parameter = pipe.get_Parameter(
            DB.BuiltInParameter.RBS_START_LEVEL_PARAM
        )
        if parameter is not None and parameter.HasValue:
            level_id = parameter.AsElementId()
            if level_id is not None and eid_value(level_id) > 0:
                return level_id
    except Exception:
        pass

    return None


def get_end_connectors(pipe, unconnected_only=True):
    connectors = []
    try:
        for connector in pipe.ConnectorManager.Connectors:
            if connector.ConnectorType != DB.ConnectorType.End:
                continue
            if unconnected_only and connector.IsConnected:
                continue
            connectors.append(connector)
    except Exception:
        pass
    return connectors


def get_connector_near(pipe, point, unconnected_only=True):
    best_connector = None
    best_distance = None

    for connector in get_end_connectors(pipe, unconnected_only):
        try:
            distance = connector.Origin.DistanceTo(point)
        except Exception:
            continue

        if best_distance is None or distance < best_distance:
            best_connector = connector
            best_distance = distance

    return best_connector, best_distance


def set_pipe_endpoint(pipe, endpoint_index, new_point):
    line = get_pipe_line(pipe)
    if line is None:
        raise Exception(u"Pipe khong phai doan thang.")

    point0 = line.GetEndPoint(0)
    point1 = line.GetEndPoint(1)

    if endpoint_index == 0:
        point0 = new_point
    else:
        point1 = new_point

    if point0.DistanceTo(point1) < mm_to_ft(MIN_REMAINING_PIPE_MM):
        raise Exception(u"Pipe cu se ngan hon gioi han toi thieu.")

    pipe.Location.Curve = DB.Line.CreateBound(point0, point1)


def create_elbow(connector1, connector2):
    first_error = None
    try:
        return doc.Create.NewElbowFitting(connector1, connector2)
    except Exception as ex:
        first_error = ex

    try:
        return doc.Create.NewElbowFitting(connector2, connector1)
    except Exception:
        raise first_error


# -----------------------------------------------------------------------------
# SELECTION FILTER VA FAILURE PROCESSOR
# -----------------------------------------------------------------------------
class PipeSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, Pipe)

    def AllowReference(self, reference, point):
        return False


class DeleteWarnings(DB.IFailuresPreprocessor):
    def PreprocessFailures(self, failures_accessor):
        try:
            for failure_message in failures_accessor.GetFailureMessages():
                if failure_message.GetSeverity() == DB.FailureSeverity.Warning:
                    failures_accessor.DeleteWarning(failure_message)
        except Exception:
            pass
        return DB.FailureProcessingResult.Continue


# -----------------------------------------------------------------------------
# GIAO DIEN CHON GOC VA GHI NHO CAU HINH
# -----------------------------------------------------------------------------
def has_saved_angle():
    """Tra ve True neu nut da tung luu mot goc hop le."""
    try:
        angle_deg = float(config.last_angle)
        return 2.0 <= angle_deg <= 90.0
    except Exception:
        return False


def get_last_angle(default_angle=45.0):
    try:
        angle_deg = float(config.last_angle)
        if 2.0 <= angle_deg <= 90.0:
            return angle_deg
    except Exception:
        pass
    return float(default_angle)


def save_last_angle(angle_deg):
    config.last_angle = float(angle_deg)
    script.save_config()


def is_shift_click():
    """Doc trang thai Shift+Click, ho tro ca pyRevit moi va cu."""
    try:
        return bool(EXEC_PARAMS.config_mode)
    except Exception:
        pass

    # Du phong cho mot so phien ban pyRevit cu.
    try:
        return bool(globals().get('__shiftclick__', False))
    except Exception:
        return False


def choose_angle():
    """Mo giao dien chon goc va luu lai khi nguoi dung xac nhan."""
    last_angle = get_last_angle()
    options = [
        u"90 do",
        u"60 do",
        u"45 do",
        u"30 do",
        u"22.5 do",
        u"15 do",
        u"Goc tuy chon"
    ]

    try:
        selected = forms.SelectFromList.show(
            options,
            title=u"Chon goc elbow / rolling offset",
            multiselect=False,
            button_name=u"Luu va tiep tuc"
        )
    except Exception as ex:
        raise Exception(
            u"Khong mo duoc giao dien chon goc. "
            u"Hay dat engine cua nut ve IronPython. Chi tiet: {0}"
            .format(text_type(ex))
        )

    if selected is None:
        return None

    if selected == u"Goc tuy chon":
        try:
            value = forms.ask_for_string(
                default=u"{0:g}".format(last_angle),
                prompt=u"Nhap goc tu 2 den 90 do:",
                title=u"Goc tuy chon"
            )
        except Exception as ex:
            raise Exception(
                u"Khong mo duoc o nhap goc. Chi tiet: {0}"
                .format(text_type(ex))
            )

        if value is None:
            return None

        try:
            angle_deg = float(value.strip().replace(",", "."))
        except Exception:
            safe_alert(u"Gia tri goc khong hop le.", warn=True)
            return None
    else:
        angle_deg = float(selected.replace(u" do", ""))

    if angle_deg < 2.0 or angle_deg > 90.0:
        safe_alert(u"Goc phai nam trong khoang 2 den 90 do.", warn=True)
        return None

    save_last_angle(angle_deg)
    return angle_deg


def resolve_requested_angle():
    """
    Lan dau: bat buoc chon goc.
    Chay thuong: dung goc da luu.
    Shift+Click: mo lai giao dien de thay doi goc.
    """
    if is_shift_click() or not has_saved_angle():
        return choose_angle()
    return get_last_angle()


# -----------------------------------------------------------------------------
# DU LIEU DAU PIPE
# -----------------------------------------------------------------------------
def build_endpoint(pipe, connector):
    line = get_pipe_line(pipe)
    if line is None:
        return None

    point0 = line.GetEndPoint(0)
    point1 = line.GetEndPoint(1)
    line_vector = point1 - point0

    horizontal_length = xy_length(line_vector)
    if horizontal_length < EPS:
        return None

    slope_angle = rad_to_deg(
        math.atan2(abs(line_vector.Z), horizontal_length)
    )
    if slope_angle > MAX_BASE_PIPE_SLOPE_DEG:
        return None

    connector_point = connector.Origin
    distance0 = connector_point.DistanceTo(point0)
    distance1 = connector_point.DistanceTo(point1)
    endpoint_index = 0 if distance0 <= distance1 else 1

    endpoint = point0 if endpoint_index == 0 else point1
    far_point = point1 if endpoint_index == 0 else point0
    outward = normalize_xy(endpoint - far_point)
    if outward is None:
        return None

    diameter = get_pipe_diameter(pipe)
    system_type_id = get_system_type_id(pipe)
    level_id = get_level_id(pipe)

    if diameter is None or system_type_id is None or level_id is None:
        return None

    return {
        "pipe": pipe,
        "pipe_id": eid_value(pipe.Id),
        "endpoint_index": endpoint_index,
        "point": endpoint,
        "far_point": far_point,
        "outward": outward,
        "length": endpoint.DistanceTo(far_point),
        "diameter": diameter,
        "system_type_id": system_type_id,
        "pipe_type_id": pipe.GetTypeId(),
        "level_id": level_id
    }


def collect_open_endpoints(pipes):
    endpoints = []

    for pipe in pipes:
        try:
            if pipe.Pinned:
                continue
            if eid_value(pipe.GroupId) > 0:
                continue
        except Exception:
            continue

        for connector in get_end_connectors(pipe, True):
            endpoint = build_endpoint(pipe, connector)
            if endpoint is not None:
                endpoints.append(endpoint)

    return endpoints


# -----------------------------------------------------------------------------
# TINH ROLLING OFFSET
# -----------------------------------------------------------------------------
def calculate_pair(endpoint1, endpoint2, requested_angle):
    if endpoint1["pipe_id"] == endpoint2["pipe_id"]:
        return None

    diameter_difference = abs(
        ft_to_mm(endpoint1["diameter"] - endpoint2["diameter"])
    )
    if diameter_difference > DIAMETER_TOLERANCE_MM:
        return None

    if not same_id(
        endpoint1["system_type_id"],
        endpoint2["system_type_id"]
    ):
        return None

    point1 = endpoint1["point"]
    point2 = endpoint2["point"]
    delta = point2 - point1

    if delta.GetLength() > mm_to_ft(MAX_PAIR_DISTANCE_MM):
        return None

    if abs(delta.Z) < mm_to_ft(1.0):
        return None

    outward1 = endpoint1["outward"]
    outward2 = endpoint2["outward"]

    parallel_error = angle_between_deg(outward1, negate(outward2))
    if parallel_error is None or parallel_error > PARALLEL_TOLERANCE_DEG:
        return None

    plan_delta = DB.XYZ(delta.X, delta.Y, 0.0)
    plan_distance = xy_length(plan_delta)

    # Khi hai dau khong trung XY, ca hai dau phai gan huong vao nhau.
    if plan_distance > mm_to_ft(1.0):
        plan_direction = normalize_xy(plan_delta)
        if plan_direction is None:
            return None
        if dot2(plan_direction, outward1) < -0.05:
            return None
        if dot2(negate(plan_direction), outward2) < -0.05:
            return None

    # Truc trung binh cua hai pipe. outward1 va -outward2 cung chieu.
    axis = normalize_xy(outward1 - outward2)
    if axis is None:
        axis = outward1

    current_along = dot2(plan_delta, axis)
    if current_along < -mm_to_ft(5.0):
        return None

    lateral_x = delta.X - axis.X * current_along
    lateral_y = delta.Y - axis.Y * current_along

    # Thanh phan vuong goc voi truc pipe gom ca lech ngang va lech cao do.
    perpendicular = math.sqrt(
        lateral_x * lateral_x +
        lateral_y * lateral_y +
        delta.Z * delta.Z
    )

    if requested_angle >= 89.999:
        required_along = 0.0
    else:
        required_along = perpendicular / math.tan(
            deg_to_rad(requested_angle)
        )

    # t > 0: keo dai dau pipe ra ngoai.
    # t < 0: rut ngan pipe de tao them khoang chay cho goc nho.
    total_move = current_along - required_along

    max_move = mm_to_ft(MAX_ENDPOINT_MOVE_MM)
    min_remaining = mm_to_ft(MIN_REMAINING_PIPE_MM)

    lower1 = max(-max_move, min_remaining - endpoint1["length"])
    upper1 = max_move
    lower2 = max(-max_move, min_remaining - endpoint2["length"])
    upper2 = max_move

    t1_min = max(lower1, total_move - upper2)
    t1_max = min(upper1, total_move - lower2)

    if t1_min > t1_max + EPS:
        return None

    move1 = clamp(total_move / 2.0, t1_min, t1_max)
    move2 = total_move - move1

    new_point1 = point1 + outward1.Multiply(move1)
    new_point2 = point2 + outward2.Multiply(move2)
    new_segment = new_point2 - new_point1

    if new_segment.GetLength() < mm_to_ft(MIN_NEW_PIPE_LENGTH_MM):
        return None

    actual_angle1 = angle_between_deg(outward1, new_segment)
    actual_angle2 = angle_between_deg(new_segment, negate(outward2))

    if actual_angle1 is None or actual_angle2 is None:
        return None

    angle_tolerance = max(0.75, PARALLEL_TOLERANCE_DEG + 0.25)
    error1 = abs(actual_angle1 - requested_angle)
    error2 = abs(actual_angle2 - requested_angle)

    if error1 > angle_tolerance or error2 > angle_tolerance:
        return None

    score = (
        delta.GetLength() +
        0.2 * (abs(move1) + abs(move2)) +
        mm_to_ft(100.0) * (error1 + error2)
    )

    return {
        "endpoint1": endpoint1,
        "endpoint2": endpoint2,
        "new_point1": new_point1,
        "new_point2": new_point2,
        "move1": move1,
        "move2": move2,
        "actual_angle1": actual_angle1,
        "actual_angle2": actual_angle2,
        "new_length": new_segment.GetLength(),
        "score": score
    }


def build_pairs(endpoints, requested_angle):
    candidates = []

    for index1 in range(len(endpoints)):
        for index2 in range(index1 + 1, len(endpoints)):
            pair = calculate_pair(
                endpoints[index1],
                endpoints[index2],
                requested_angle
            )
            if pair is not None:
                candidates.append(pair)

    candidates.sort(key=lambda item: item["score"])

    # Greedy nearest pairing. Moi pipe chi tham gia mot cap.
    pairs = []
    used_pipe_ids = set()

    for candidate in candidates:
        pipe_id1 = candidate["endpoint1"]["pipe_id"]
        pipe_id2 = candidate["endpoint2"]["pipe_id"]

        if pipe_id1 in used_pipe_ids or pipe_id2 in used_pipe_ids:
            continue

        used_pipe_ids.add(pipe_id1)
        used_pipe_ids.add(pipe_id2)
        pairs.append(candidate)

    return pairs


# -----------------------------------------------------------------------------
# TAO PIPE NOI VA 2 ELBOW
# -----------------------------------------------------------------------------
def connect_pair(pair):
    endpoint1 = pair["endpoint1"]
    endpoint2 = pair["endpoint2"]
    pipe1 = endpoint1["pipe"]
    pipe2 = endpoint2["pipe"]
    point1 = pair["new_point1"]
    point2 = pair["new_point2"]

    set_pipe_endpoint(pipe1, endpoint1["endpoint_index"], point1)
    set_pipe_endpoint(pipe2, endpoint2["endpoint_index"], point2)
    doc.Regenerate()

    connector1, distance1 = get_connector_near(pipe1, point1, True)
    connector2, distance2 = get_connector_near(pipe2, point2, True)
    search_tolerance = mm_to_ft(CONNECTOR_SEARCH_TOLERANCE_MM)

    if connector1 is None or distance1 is None or distance1 > search_tolerance:
        raise Exception(u"Khong tim lai duoc connector cua pipe 1.")
    if connector2 is None or distance2 is None or distance2 > search_tolerance:
        raise Exception(u"Khong tim lai duoc connector cua pipe 2.")

    new_pipe = Pipe.Create(
        doc,
        endpoint1["system_type_id"],
        endpoint1["pipe_type_id"],
        endpoint1["level_id"],
        point1,
        point2
    )

    diameter_parameter = new_pipe.get_Parameter(
        DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM
    )
    if diameter_parameter is not None and not diameter_parameter.IsReadOnly:
        diameter_parameter.Set(endpoint1["diameter"])

    doc.Regenerate()

    new_connector1, new_distance1 = get_connector_near(new_pipe, point1, True)
    if new_connector1 is None:
        raise Exception(u"Khong tim duoc connector dau 1 cua pipe noi moi.")

    create_elbow(connector1, new_connector1)
    doc.Regenerate()

    # Elbow dau tien co the trim pipe moi, nen doc lai connector dau thu hai.
    connector2, distance2 = get_connector_near(pipe2, point2, True)
    new_connector2, new_distance2 = get_connector_near(new_pipe, point2, True)

    if connector2 is None or new_connector2 is None:
        raise Exception(u"Khong tim duoc connector de tao elbow thu hai.")

    create_elbow(connector2, new_connector2)
    doc.Regenerate()

    return new_pipe


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    if doc is None or uidoc is None:
        safe_alert(u"Khong tim thay Revit Document dang hoat dong.", warn=True)
        return

    if doc.IsFamilyDocument:
        safe_alert(u"Tool chi chay trong Project, khong chay trong Family Editor.", warn=True)
        return

    requested_angle = resolve_requested_angle()
    if requested_angle is None:
        return

    try:
        selected_elements = uidoc.Selection.PickElementsByRectangle(
            PipeSelectionFilter(),
            u"Quet vung chua cac cap pipe can noi"
        )
    except OperationCanceledException:
        return

    pipes = [element for element in selected_elements if isinstance(element, Pipe)]
    if len(pipes) < 2:
        safe_alert(u"Can quet chon it nhat 2 pipe.", warn=True)
        return

    endpoints = collect_open_endpoints(pipes)
    pairs = build_pairs(endpoints, requested_angle)

    if not pairs:
        forms.alert(
            u"Khong tim thay cap pipe phu hop.\n\n"
            u"Kiem tra cac dieu kien:\n"
            u"- Dau pipe dang ho.\n"
            u"- Hai pipe gan song song va huong vao nhau.\n"
            u"- Cung duong kinh va cung Piping System Type.\n"
            u"- Khoang cach khong vuot qua {0:g} mm.\n"
            u"- Pipe con du chieu dai de tao goc {1:g} do."
            .format(MAX_PAIR_DISTANCE_MM, requested_angle),
            warn_icon=True
        )
        return

    confirmed = forms.alert(
        u"Da quet {0} pipe.\n"
        u"Tim duoc {1} cap se noi theo goc {2:g} do.\n\n"
        u"Moi pipe chi duoc ghep mot lan trong lan chay nay.\n"
        u"Tiep tuc?"
        .format(len(pipes), len(pairs), requested_angle),
        yes=True,
        no=True
    )
    if not confirmed:
        return

    success_rows = []
    failure_rows = []

    transaction_group = DB.TransactionGroup(
        doc,
        u"Auto Connect Offset Pipes"
    )
    transaction_group.Start()

    try:
        for pair_index, pair in enumerate(pairs):
            transaction = DB.Transaction(
                doc,
                u"Connect Pipe Pair {0}".format(pair_index + 1)
            )
            transaction.Start()

            warning_processor = DeleteWarnings()
            failure_options = transaction.GetFailureHandlingOptions()
            failure_options.SetFailuresPreprocessor(warning_processor)
            transaction.SetFailureHandlingOptions(failure_options)

            try:
                new_pipe = connect_pair(pair)
                transaction.Commit()

                success_rows.append([
                    pair["endpoint1"]["pipe_id"],
                    pair["endpoint2"]["pipe_id"],
                    eid_value(new_pipe.Id),
                    "{0:.2f}".format(pair["actual_angle1"]),
                    "{0:.2f}".format(pair["actual_angle2"]),
                    "{0:.1f}".format(ft_to_mm(pair["move1"])),
                    "{0:.1f}".format(ft_to_mm(pair["move2"])),
                    "{0:.1f}".format(ft_to_mm(pair["new_length"]))
                ])

            except Exception as ex:
                try:
                    if transaction.GetStatus() == DB.TransactionStatus.Started:
                        transaction.RollBack()
                except Exception:
                    pass

                failure_rows.append([
                    pair["endpoint1"]["pipe_id"],
                    pair["endpoint2"]["pipe_id"],
                    text_type(ex),
                    traceback.format_exc()
                ])

        transaction_group.Assimilate()

    except Exception:
        try:
            transaction_group.RollBack()
        except Exception:
            pass
        raise

    
# Khong dung dieu kien __name__ == "__main__" vi mot so pyRevit executor
# co the gan ten module khac, lam script im lang va khong goi main().
try:
    main()
except OperationCanceledException:
    pass
except Exception as fatal_exception:
    show_fatal_error(fatal_exception)
