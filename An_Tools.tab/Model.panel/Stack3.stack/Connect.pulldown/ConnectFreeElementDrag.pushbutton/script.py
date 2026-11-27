# -*- coding: utf-8 -*-
__title__ = "Connect Free Elements Plus"
__doc__ = ("Tu dong noi cac connector dang ho: Pipe-Pipe, Pipe-Fitting, Pipe-Accessory, "
           "Fitting-Fitting. Uu tien keo dai/rut ngan dau ong, han che move doi tuong. "
           "Keo Pipe Accessory ve ong lien (khong ho), cat ong va noi accessory vao ong. "
           "Co progress bar va Cancel.")

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
import math

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# =====================================================================
# CAI DAT (mm / do)
# =====================================================================
SNAP_TOL_MM = 2.0          # 2 connector coi nhu trung nhau -> noi thang, khong move
PULL_MAX_MM = 2000.0       # khoang cach toi da keo dau Pipe ve Fitting/Accessory/Fixture
PIPE_PIPE_MAX_MM = 500.0   # khoang ho toi da giua 2 dau Pipe (thang hang / tao elbow)
FIT_MOVE_MAX_MM = 50.0     # khoang cach toi da duoc phep MOVE fitting/accessory
INSERT_MAX_MM = 2000.0     # che do cu: chen fitting/accessory vao giua ong
MIN_PIPE_LENGTH_MM = 10.0
ANGLE_TOL_DEG = 5.0
AXIS_OFFSET_TOL_MM = 5.0
MOVE_COST_WEIGHT = 3.0     # phat "chi phi" cho phuong an move -> uu tien keo ong truoc

# --- Plumbing Fixture -> Pipe (KHONG move fixture) ---
FIX_STUB_MAX_MM = 1500.0     # chieu dai toi da ong nhanh tao tu connector fixture toi truc ong
FIX_MIN_STUB_MM = 20.0       # ngan hon muc nay thi khong tao ong nhanh
FIX_LATERAL_TOL_MM = 10.0    # truc connector fixture duoc lech khoi truc ong toi da (mm)
FIX_END_ZONE_MM = 150.0      # diem giao cach dau ong < muc nay -> uu tien keo dau ong + elbow
FIX_EXTEND_MAX_MM = 1000.0   # duoc keo dai dau ong toi da de gap ong nhanh

FT = 1.0 / 304.8
SNAP_TOL = SNAP_TOL_MM * FT
PULL_MAX = PULL_MAX_MM * FT
PIPE_PIPE_MAX = PIPE_PIPE_MAX_MM * FT
FIT_MOVE_MAX = FIT_MOVE_MAX_MM * FT
INSERT_MAX = INSERT_MAX_MM * FT
MIN_PIPE_LENGTH = MIN_PIPE_LENGTH_MM * FT
AXIS_OFFSET_TOL = AXIS_OFFSET_TOL_MM * FT
ANGLE_TOL_COS = math.cos(math.radians(ANGLE_TOL_DEG))

CAT_PIPE_FITTING = int(DB.BuiltInCategory.OST_PipeFitting)
CAT_PIPE_ACCESSORY = int(DB.BuiltInCategory.OST_PipeAccessory)
CAT_PLUMBING_FIXTURE = int(DB.BuiltInCategory.OST_PlumbingFixtures)

OPT_SNAP = "1. Noi cac connector da trung nhau (khong move)"
OPT_PULL = "2. Keo dau Pipe ve Pipe Fitting / Pipe Accessory / Fixture (chi doi chieu dai ong)"
OPT_PIPE = "3. Noi Pipe - Pipe (thang hang: keo dai + union | lech goc: tao elbow)"
OPT_MOVE = "4. Noi Fitting/Accessory - Fitting/Accessory (move toi da {} mm)".format(int(FIT_MOVE_MAX_MM))
OPT_INSERT = "5. Che do cu: chen Fitting/Accessory vao giua ong (CO MOVE doi tuong)"
OPT_FIXTURE = "6. Noi Plumbing Fixture vao Pipe (tao ong nhanh + elbow/tee, KHONG move fixture)"

FIX_STUB_MAX = FIX_STUB_MAX_MM * FT
FIX_MIN_STUB = FIX_MIN_STUB_MM * FT
FIX_LATERAL_TOL = FIX_LATERAL_TOL_MM * FT
FIX_END_ZONE = FIX_END_ZONE_MM * FT
FIX_EXTEND_MAX = FIX_EXTEND_MAX_MM * FT

# --- Pipe Accessory -> ong lien (khong ho): keo accessory ve ong, cat ong, noi 2 dau ---
OPT_ACC = "7. Keo Pipe Accessory ve ong lien (khong ho), cat ong va noi accessory vao ong"
ACC_PULL_MAX_MM = 1000.0       # khoang cach toi da tu tam accessory toi truc ong
ACC_MIN_SEG_MM = 50.0          # moi doan ong con lai 2 ben accessory toi thieu (mm)
ACC_ALLOW_ROTATE = True        # cho phep xoay accessory cho thang truc ong (chi xoay quanh truc Z)
ACC_AXIS_TOL_DEG = 5.0         # lech goc <= gia tri nay coi nhu da thang hang (ong thoat nuoc co do doc)
ACC_ROTATE_PENALTY_MM = 300.0  # phat chi phi khi phai xoay -> uu tien ong da song song
ACC_SEARCH_VIEW_PIPES = True   # neu khong chon ong: tim them ong trong Active View
ACC_MATCH_SIZE = True          # thu doi size accessory = duong kinh ong
ACC_ALLOW_SIZE_MISMATCH = False  # True: van noi du size khac (se co warning)
# Kiem tra he thong cho accessory:
#   "off"    : bo qua (accessory se nhan he cua ong sau khi noi)
#   "prefer" : uu tien ong cung he, nhung van noi vao ong khac he neu khong co ong cung he (MAC DINH)
#   "strict" : chi noi ong cung he
ACC_SYSTEM_MODE = "prefer"
ACC_SYSTEM_PENALTY_MM = 1000.0  # phat chi phi khi khac he (che do "prefer")
ACC_SIZE_PARAMS = (("Nominal Diameter", 1.0), ("Diameter", 1.0), ("Nominal Size", 1.0),
                   ("Size", 1.0), ("DN", 1.0), ("Nominal Radius", 0.5), ("Radius", 0.5))
ACC_PULL_MAX = ACC_PULL_MAX_MM * FT
ACC_MIN_SEG = ACC_MIN_SEG_MM * FT
ACC_ROTATE_PENALTY = ACC_ROTATE_PENALTY_MM * FT
ACC_AXIS_TOL_COS = math.cos(math.radians(ACC_AXIS_TOL_DEG))
ACC_SYSTEM_PENALTY = ACC_SYSTEM_PENALTY_MM * FT


class Cancelled(Exception):
    pass


# =====================================================================
# TIEN ICH CHUNG
# =====================================================================
def eid_int(element_id):
    try:
        return element_id.Value          # Revit 2024+
    except Exception:
        return element_id.IntegerValue


def get_category_id(element):
    try:
        return element.Category.Id.IntegerValue
    except Exception:
        try:
            return int(element.Category.Id.Value)
        except Exception:
            return None


def is_pipe(element):
    return isinstance(element, DB.Plumbing.Pipe)


def element_kind(element):
    """pipe | fitting (fitting + accessory) | fixture | None"""
    if is_pipe(element):
        return "pipe"
    if isinstance(element, DB.FamilyInstance):
        cat = get_category_id(element)
        if cat in (CAT_PIPE_FITTING, CAT_PIPE_ACCESSORY):
            return "fitting"
        if cat == CAT_PLUMBING_FIXTURE:
            return "fixture"
    return None


def is_pinned(element):
    try:
        return element.Pinned
    except Exception:
        return False


def get_connectors(element, only_open=False):
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


def get_conn_by_id(element, conn_id):
    for conn in get_connectors(element):
        try:
            if conn.Id == conn_id:
                return conn
        except Exception:
            continue
    return None


def get_open_connectors_for_matching(element):
    """Pipe: chi lay connector End. Fitting/Accessory/Fixture: connector vat ly dang ho."""
    result = []
    for conn in get_connectors(element, only_open=True):
        try:
            if is_pipe(element) and conn.ConnectorType != DB.ConnectorType.End:
                continue
            result.append(conn)
        except Exception:
            continue
    return result


def connector_size(conn):
    try:
        if conn.Shape == DB.ConnectorProfileType.Round:
            return conn.Radius * 2.0
        if conn.Shape == DB.ConnectorProfileType.Rectangular:
            return max(conn.Width, conn.Height)
    except Exception:
        pass
    return None


def connectors_compatible(c1, c2):
    try:
        if c1.Domain != c2.Domain:
            return False
    except Exception:
        pass
    try:
        if c1.Shape != c2.Shape:
            return False
    except Exception:
        pass
    s1 = connector_size(c1)
    s2 = connector_size(c2)
    if s1 is not None and s2 is not None and abs(s1 - s2) > SNAP_TOL:
        return False
    return True


def get_connector_direction(conn):
    try:
        v = conn.CoordinateSystem.BasisZ
        if v.GetLength() < 1e-9:
            return None
        return v.Normalize()
    except Exception:
        return None


def get_pipe_direction(pipe):
    try:
        curve = pipe.Location.Curve
        if not isinstance(curve, DB.Line):
            return None
        v = curve.GetEndPoint(1).Subtract(curve.GetEndPoint(0))
        if v.GetLength() < 1e-9:
            return None
        return v.Normalize()
    except Exception:
        return None


def is_parallel(v1, v2):
    if v1 is None or v2 is None:
        return False
    try:
        return abs(v1.DotProduct(v2)) >= ANGLE_TOL_COS
    except Exception:
        return False


def is_facing(c1, c2):
    """2 connector doi dien nhau (BasisZ nguoc chieu)."""
    d1 = get_connector_direction(c1)
    d2 = get_connector_direction(c2)
    if d1 is None or d2 is None:
        return False
    return d1.DotProduct(d2) <= -ANGLE_TOL_COS


def is_target_on_pipe_axis(pipe, point):
    try:
        curve = pipe.Location.Curve
        if not isinstance(curve, DB.Line):
            return False
        unbound = DB.Line.CreateUnbound(curve.GetEndPoint(0), curve.Direction)
        proj = unbound.Project(point)
        return proj is not None and proj.Distance <= AXIS_OFFSET_TOL
    except Exception:
        return False


def compute_new_pipe_line(pipe, pipe_conn, target):
    """Tinh line moi khi dua dau ong (ung voi pipe_conn) toi target.
    Tra ve (p0, p1) hoac None neu ong bi dao chieu / qua ngan."""
    try:
        curve = pipe.Location.Curve
        if not isinstance(curve, DB.Line):
            return None
        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
        if pipe_conn.Origin.DistanceTo(p0) <= pipe_conn.Origin.DistanceTo(p1):
            n0, n1 = target, p1
        else:
            n0, n1 = p0, target
        if n0.DistanceTo(n1) < MIN_PIPE_LENGTH:
            return None
        old_v = p1.Subtract(p0).Normalize()
        new_v = n1.Subtract(n0).Normalize()
        if old_v.DotProduct(new_v) < ANGLE_TOL_COS:
            return None
        return n0, n1
    except Exception:
        return None


def closest_between_lines(p1, d1, p2, d2):
    """Diem gan nhau nhat giua 2 duong thang vo han p1+s*d1 va p2+t*d2 (d da normalize)."""
    w = p1.Subtract(p2)
    b = d1.DotProduct(d2)
    d = d1.DotProduct(w)
    e = d2.DotProduct(w)
    denom = 1.0 - b * b
    if abs(denom) < 1e-9:
        return None
    s = (b * e - d) / denom
    t = (e - b * d) / denom
    q1 = p1.Add(d1.Multiply(s))
    q2 = p2.Add(d2.Multiply(t))
    return s, t, q1.DistanceTo(q2)


def connect_pipe_to_pipe(c1, c2):
    """Uu tien union theo Routing Preference, neu khong co thi noi thang."""
    try:
        doc.Create.NewUnionFitting(c1, c2)
        return
    except Exception:
        pass
    c1.ConnectTo(c2)


# =====================================================================
# PROGRESS BAR
# =====================================================================
class Progress(object):
    def __init__(self, pb):
        self.pb = pb
        self.i = 0
        self.max = 1
        self.step = 1

    def start(self, title, total):
        self.i = 0
        self.max = max(int(total), 1)
        self.step = max(1, self.max // 200)
        try:
            self.pb.title = title + " ({value}/{max_value})"
        except Exception:
            pass
        self.pb.update_progress(0, self.max)

    def tick(self):
        if self.pb.cancelled:
            raise Cancelled()
        self.i += 1
        if self.i % self.step == 0 or self.i >= self.max:
            self.pb.update_progress(min(self.i, self.max), self.max)


# =====================================================================
# DANH GIA 1 CAP CONNECTOR -> CHON PHUONG AN IT MOVE NHAT
# =====================================================================
def evaluate_pair(ea, ca, eb, cb, opts):
    """Tra ve (cost, action, ea, ca, eb, cb) hoac None.
    action: snap | pull | extend | elbow | move"""
    ka = element_kind(ea)
    kb = element_kind(eb)
    if ka is None or kb is None:
        return None
    # Dua Pipe len truoc
    if kb == "pipe" and ka != "pipe":
        ea, ca, eb, cb = eb, cb, ea, ca
        ka, kb = kb, ka

    if not connectors_compatible(ca, cb):
        return None

    dist = ca.Origin.DistanceTo(cb.Origin)
    facing = is_facing(ca, cb)

    # 1. Da trung nhau -> chi ConnectTo
    if OPT_SNAP in opts and dist <= SNAP_TOL and facing:
        return (dist, "snap", ea, ca, eb, cb)

    # 2. Pipe - Pipe
    if ka == "pipe" and kb == "pipe":
        if OPT_PIPE not in opts or (is_pinned(ea) and is_pinned(eb)):
            return None
        da = get_connector_direction(ca)
        db = get_connector_direction(cb)
        if da is None or db is None:
            return None
        if facing:
            # Thang hang: keo dai 1 ong toi dau ong kia
            v = cb.Origin.Subtract(ca.Origin)
            s = v.DotProduct(da)
            lateral = v.Subtract(da.Multiply(s)).GetLength()
            if s <= SNAP_TOL or s > PIPE_PIPE_MAX or lateral > AXIS_OFFSET_TOL:
                return None
            ok_a = (not is_pinned(ea)) and compute_new_pipe_line(ea, ca, cb.Origin)
            ok_b = (not is_pinned(eb)) and compute_new_pipe_line(eb, cb, ca.Origin)
            if not ok_a and not ok_b:
                return None
            return (s, "extend", ea, ca, eb, cb)
        if is_parallel(da, db):
            return None  # song song lech truc -> khong xu ly
        if is_pinned(ea) or is_pinned(eb):
            return None
        res = closest_between_lines(ca.Origin, da, cb.Origin, db)
        if res is None:
            return None
        s, t, gap = res
        if gap > AXIS_OFFSET_TOL or abs(s) > PIPE_PIPE_MAX or abs(t) > PIPE_PIPE_MAX:
            return None
        return (max(abs(s), abs(t)), "elbow", ea, ca, eb, cb)

    # 3. Pipe - Fitting/Accessory/Fixture: chi keo dau ong
    if ka == "pipe":
        if OPT_PULL not in opts or is_pinned(ea):
            return None
        if dist > PULL_MAX or not facing:
            return None
        if not is_parallel(get_pipe_direction(ea), get_connector_direction(cb)):
            return None
        if not is_target_on_pipe_axis(ea, cb.Origin):
            return None
        if not compute_new_pipe_line(ea, ca, cb.Origin):
            return None
        return (dist, "pull", ea, ca, eb, cb)

    # 4. Fitting/Accessory - Fitting/Accessory/Fixture: move nho
    if OPT_MOVE not in opts:
        return None
    if ka != "fitting" and kb != "fitting":
        return None  # fixture - fixture: khong xu ly
    if dist > FIT_MOVE_MAX or not facing:
        return None
    return (dist * MOVE_COST_WEIGHT, "move", ea, ca, eb, cb)


# =====================================================================
# THUC HIEN TUNG PHUONG AN
# =====================================================================
def apply_pipe_line(pipe, new_line):
    pipe.Location.Curve = DB.Line.CreateBound(new_line[0], new_line[1])
    doc.Regenerate()


def do_snap(ea, ca, eb, cb):
    if is_pipe(ea) and is_pipe(eb):
        connect_pipe_to_pipe(ca, cb)
    else:
        ca.ConnectTo(cb)
    return None


def do_pull(pipe, pconn, item, iconn):
    cid = pconn.Id
    target = iconn.Origin
    new_line = compute_new_pipe_line(pipe, pconn, target)
    if not new_line:
        raise Exception("Khong keo duoc dau ong.")
    apply_pipe_line(pipe, new_line)
    new_conn = get_conn_by_id(pipe, cid)
    if new_conn is None or new_conn.IsConnected:
        raise Exception("Khong tim thay connector ong sau khi keo.")
    if new_conn.Origin.DistanceTo(target) > SNAP_TOL:
        raise Exception("Dau ong chua trung connector dich.")
    new_conn.ConnectTo(iconn)
    return None


def do_extend(pa, ca, pb, cb):
    mover = None
    if not is_pinned(pa):
        line = compute_new_pipe_line(pa, ca, cb.Origin)
        if line:
            mover = (pa, ca, pb, cb, line)
    if mover is None and not is_pinned(pb):
        line = compute_new_pipe_line(pb, cb, ca.Origin)
        if line:
            mover = (pb, cb, pa, ca, line)
    if mover is None:
        raise Exception("Khong keo dai duoc ong nao.")
    mpipe, mconn, opipe, oconn, line = mover
    mid, oid = mconn.Id, oconn.Id
    apply_pipe_line(mpipe, line)
    c1 = get_conn_by_id(mpipe, mid)
    c2 = get_conn_by_id(opipe, oid)
    if c1 is None or c2 is None or c1.Origin.DistanceTo(c2.Origin) > SNAP_TOL:
        raise Exception("Dau ong chua trung nhau sau khi keo.")
    connect_pipe_to_pipe(c1, c2)
    return None


def do_elbow(pa, ca, pb, cb):
    fitting = doc.Create.NewElbowFitting(ca, cb)
    if fitting is None:
        raise Exception("Khong tao duoc elbow.")
    return None


def movable_info(element, conn_id, move_vec):
    """(ok, so connector dang noi). Chi cho move neu cac doi tuong dang noi la Pipe
    va huong move song song truc ong do (ong chi dan ra/co lai, khong lam lech doc)."""
    if is_pinned(element) or element_kind(element) != "fitting":
        return False, 99
    vec_len = move_vec.GetLength()
    move_dir = move_vec.Normalize() if vec_len > 1e-9 else None
    count = 0
    for conn in get_connectors(element):
        try:
            if conn.Id == conn_id or not conn.IsConnected:
                continue
            for ref in conn.AllRefs:
                owner = ref.Owner
                if owner is None or owner.Id == element.Id:
                    continue
                if ref.ConnectorType == DB.ConnectorType.Logical:
                    continue
                if not is_pipe(owner):
                    return False, 99
                if move_dir is not None and not is_parallel(get_pipe_direction(owner), move_dir):
                    return False, 99
            count += 1
        except Exception:
            return False, 99
    return True, count


def count_connected(element):
    n = 0
    for conn in get_connectors(element):
        try:
            if conn.IsConnected:
                n += 1
        except Exception:
            pass
    return n


def do_move(ea, ca, eb, cb):
    options = []
    for m, mc, t, tc in ((ea, ca, eb, cb), (eb, cb, ea, ca)):
        vec = tc.Origin.Subtract(mc.Origin)
        ok, n = movable_info(m, mc.Id, vec)
        if ok:
            options.append((n, m, mc, t, tc, vec))
    if not options:
        raise Exception("Khong co doi tuong nao duoc phep move.")
    options.sort(key=lambda x: x[0])
    n, mover, mconn, target, tconn, vec = options[0]
    mid, tid = mconn.Id, tconn.Id
    before = count_connected(mover)
    DB.ElementTransformUtils.MoveElement(doc, mover.Id, vec)
    doc.Regenerate()
    c1 = get_conn_by_id(mover, mid)
    c2 = get_conn_by_id(target, tid)
    if c1 is None or c2 is None or c1.Origin.DistanceTo(c2.Origin) > SNAP_TOL:
        raise Exception("Connector chua trung nhau sau khi move.")
    if count_connected(mover) < before:
        raise Exception("Move lam dut ket noi cu.")
    c1.ConnectTo(c2)
    return (mover.Id, vec.GetLength() * 304.8)


ACTIONS = {
    "snap": do_snap,
    "pull": do_pull,
    "extend": do_extend,
    "elbow": do_elbow,
    "move": do_move,
}


# =====================================================================
# LUOI KHONG GIAN DE TIM CAP CONNECTOR GAN NHAU (NHANH KHI NHIEU DOI TUONG)
# =====================================================================
class ConnRecord(object):
    __slots__ = ("elem", "cid", "origin", "key")

    def __init__(self, elem, conn):
        self.elem = elem
        self.cid = conn.Id
        self.origin = conn.Origin
        self.key = (eid_int(elem.Id), conn.Id)


class SpatialGrid(object):
    def __init__(self, cell):
        self.cell = max(cell, 1e-3)
        self.cells = {}

    def _key(self, p):
        c = self.cell
        return (int(math.floor(p.X / c)), int(math.floor(p.Y / c)), int(math.floor(p.Z / c)))

    def add(self, rec):
        self.cells.setdefault(self._key(rec.origin), []).append(rec)

    def query(self, p):
        kx, ky, kz = self._key(p)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for rec in self.cells.get((kx + dx, ky + dy, kz + dz), ()):
                        yield rec


def build_candidates(elements, opts, prog):
    radius = 0.0
    if OPT_SNAP in opts:
        radius = max(radius, SNAP_TOL)
    if OPT_PULL in opts:
        radius = max(radius, PULL_MAX)
    if OPT_PIPE in opts:
        radius = max(radius, PIPE_PIPE_MAX * 1.5)
    if OPT_MOVE in opts:
        radius = max(radius, FIT_MOVE_MAX)

    records = []
    prog.start("Doc connector dang ho", len(elements))
    for el in elements:
        prog.tick()
        for conn in get_open_connectors_for_matching(el):
            try:
                records.append(ConnRecord(el, conn))
            except Exception:
                continue

    grid = SpatialGrid(radius)
    for rec in records:
        grid.add(rec)

    candidates = []
    seen = set()
    prog.start("Tim cap connector gan nhau", len(records))
    for rec in records:
        prog.tick()
        for other in grid.query(rec.origin):
            if other.key[0] == rec.key[0]:
                continue
            pair_key = (rec.key, other.key) if rec.key < other.key else (other.key, rec.key)
            if pair_key in seen:
                continue
            seen.add(pair_key)
            if rec.origin.DistanceTo(other.origin) > radius:
                continue
            ca = get_conn_by_id(rec.elem, rec.cid)
            cb = get_conn_by_id(other.elem, other.cid)
            if ca is None or cb is None:
                continue
            try:
                res = evaluate_pair(rec.elem, ca, other.elem, cb, opts)
            except Exception:
                res = None
            if res:
                # luu (cost, action, rec_a, rec_b) theo thu tu da sap xep trong evaluate_pair
                ra, rb = (rec, other) if res[2].Id == rec.elem.Id and res[3].Id == rec.cid else (other, rec)
                candidates.append((res[0], res[1], ra, rb))
    candidates.sort(key=lambda c: c[0])
    return candidates


def process_candidates(candidates, opts, prog, stats, moved_log):
    prog.start("Ket noi doi tuong", len(candidates))
    for cost, action, ra, rb in candidates:
        prog.tick()
        ca = get_conn_by_id(ra.elem, ra.cid)
        cb = get_conn_by_id(rb.elem, rb.cid)
        if ca is None or cb is None:
            continue
        try:
            if ca.IsConnected or cb.IsConnected:
                continue
        except Exception:
            continue
        # Danh gia lai voi hinh hoc hien tai (co the da thay doi sau cac buoc truoc)
        try:
            res = evaluate_pair(ra.elem, ca, rb.elem, cb, opts)
        except Exception:
            res = None
        if not res or res[1] != action:
            continue
        _, action, ea, ca, eb, cb = res

        sub = DB.SubTransaction(doc)
        sub.Start()
        try:
            info = ACTIONS[action](ea, ca, eb, cb)
            sub.Commit()
            stats[action] += 1
            if action == "move" and info:
                moved_log.append(info)
        except Exception:
            sub.RollBack()
            stats["failed"] += 1


# =====================================================================
# PLUMBING FIXTURE -> PIPE (tao ong nhanh theo huong connector, KHONG move fixture)
# =====================================================================
def get_pipe_system_type_id(pipe):
    try:
        p = pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM)
        if p:
            sid = p.AsElementId()
            if sid and sid != DB.ElementId.InvalidElementId:
                return sid
    except Exception:
        pass
    return None


def get_pipe_classification_name(pipe):
    sid = get_pipe_system_type_id(pipe)
    if sid is None:
        return None
    try:
        return str(doc.GetElement(sid).SystemClassification)
    except Exception:
        return None


def system_match(fconn, pipe):
    """Khong noi nham he: Sanitary vao Sanitary, CW vao CW, HW vao HW..."""
    try:
        a = str(fconn.PipeSystemType)
    except Exception:
        a = None
    b = get_pipe_classification_name(pipe)
    if not a or not b:
        return True
    if a in ("UndefinedSystemType", "Fitting", "Global", "OtherPipe"):
        return True
    return a == b


def get_level_id(pipe):
    try:
        if pipe.ReferenceLevel:
            return pipe.ReferenceLevel.Id
    except Exception:
        pass
    try:
        if pipe.LevelId != DB.ElementId.InvalidElementId:
            return pipe.LevelId
    except Exception:
        pass
    return doc.ActiveView.GenLevel.Id


def nearest_end_connector(pipe, point, only_open=False):
    best, best_d = None, float("inf")
    for conn in get_connectors(pipe, only_open):
        try:
            if conn.ConnectorType != DB.ConnectorType.End:
                continue
            d = conn.Origin.DistanceTo(point)
            if d < best_d:
                best, best_d = conn, d
        except Exception:
            continue
    return best, best_d


def create_stub(fconn, end_pt, ref_pipe):
    """Tao ong nhanh tu connector fixture toi end_pt, cung System/Pipe Type voi ong dich."""
    sys_id = get_pipe_system_type_id(ref_pipe)
    if sys_id is None:
        raise Exception("Ong dich chua co System Type.")
    start = fconn.Origin
    stub = DB.Plumbing.Pipe.Create(doc, sys_id, ref_pipe.GetTypeId(), get_level_id(ref_pipe), start, end_pt)
    dia = connector_size(fconn)
    if dia:
        try:
            stub.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM).Set(dia)
        except Exception:
            pass
    doc.Regenerate()
    sconn, _ = nearest_end_connector(stub, start, True)
    if sconn is None:
        raise Exception("Khong lay duoc connector dau ong nhanh.")
    sconn.ConnectTo(fconn)
    econn, d = nearest_end_connector(stub, end_pt, True)
    if econn is None or d > SNAP_TOL:
        raise Exception("Khong lay duoc connector cuoi ong nhanh.")
    return stub, econn


def plan_fixture_conn(fconn, pipes):
    """Tim ong dich tot nhat cho 1 connector fixture.
    Tra ve ((cost, mode, pipe, P, end_conn_id), reason)."""
    origin = fconn.Origin
    fdir = get_connector_direction(fconn)
    if fdir is None:
        return None, "Connector fixture khong doc duoc huong"
    best = None
    reason = "Khong co ong phu hop trong {} mm theo huong connector".format(int(FIX_STUB_MAX_MM))
    for pipe in pipes:
        try:
            if not pipe.IsValidObject:
                continue
            curve = pipe.Location.Curve
            if not isinstance(curve, DB.Line):
                continue
            pdir = curve.Direction.Normalize()
            if is_parallel(fdir, pdir):
                continue  # truong hop thang hang da xu ly o che do 2
            if not system_match(fconn, pipe):
                continue
            p0 = curve.GetEndPoint(0)
            length = curve.Length
            res = closest_between_lines(origin, fdir, p0, pdir)
            if res is None:
                continue
            s, t, gap = res
            if s < FIX_MIN_STUB or s > FIX_STUB_MAX:
                continue
            if gap > FIX_LATERAL_TOL:
                reason = "Truc connector lech truc ong {:.0f} mm (> {} mm)".format(gap * 304.8, int(FIX_LATERAL_TOL_MM))
                continue
            P = p0.Add(pdir.Multiply(t))
            mode, end_conn_id = None, None
            if FIX_END_ZONE <= t <= length - FIX_END_ZONE:
                mode = "tee"
            else:
                end_idx = 0 if t < length / 2.0 else 1
                dist_end = abs(t) if end_idx == 0 else abs(t - length)
                if dist_end > FIX_EXTEND_MAX:
                    continue
                econn, d = nearest_end_connector(pipe, curve.GetEndPoint(end_idx), True)
                if econn is not None and d <= SNAP_TOL and not is_pinned(pipe) \
                        and compute_new_pipe_line(pipe, econn, P):
                    mode, end_conn_id = "elbow", econn.Id
                elif 5 * MIN_PIPE_LENGTH < t < length - 5 * MIN_PIPE_LENGTH and not is_pinned(pipe):
                    mode = "tee"
                else:
                    continue
            if mode == "tee" and is_pinned(pipe):
                continue
            cost = s
            if best is None or cost < best[0]:
                best = (cost, mode, pipe, P, end_conn_id)
        except Exception:
            continue
    return best, reason


def connect_fixture_conn(fixture, fcid, plan, pipes):
    _, mode, pipe, P, end_conn_id = plan
    fconn = get_conn_by_id(fixture, fcid)
    if fconn is None or fconn.IsConnected:
        raise Exception("Connector fixture khong con ho.")

    if mode == "elbow":
        econn = get_conn_by_id(pipe, end_conn_id)
        line = compute_new_pipe_line(pipe, econn, P)
        if not line:
            raise Exception("Khong keo duoc dau ong.")
        apply_pipe_line(pipe, line)
        econn = get_conn_by_id(pipe, end_conn_id)
        stub, sconn = create_stub(fconn, P, pipe)
        if doc.Create.NewElbowFitting(sconn, econn) is None:
            raise Exception("Khong tao duoc elbow.")
        return "fix_elbow"

    # mode == "tee": cat ong tai diem giao -> tee; that bai thi thu tap (takeoff)
    stub, sconn = create_stub(fconn, P, pipe)
    stub_id = stub.Id
    sub = DB.SubTransaction(doc)
    sub.Start()
    try:
        new_id = DB.Plumbing.PlumbingUtils.BreakCurve(doc, pipe.Id, P)
        new_pipe = doc.GetElement(new_id)
        doc.Regenerate()
        c1, _ = nearest_end_connector(pipe, P)
        c2, _ = nearest_end_connector(new_pipe, P)
        if c1 is None or c2 is None:
            raise Exception("Khong lay duoc connector sau khi cat ong.")
        if c1.IsConnected:
            for ref in list(c1.AllRefs):
                try:
                    if ref.Owner.Id == new_pipe.Id:
                        c1.DisconnectFrom(ref)
                except Exception:
                    pass
        tee = doc.Create.NewTeeFitting(c1, c2, sconn)
        if tee is None:
            raise Exception("Khong tao duoc tee.")
        sub.Commit()
        pipes.append(new_pipe)
        return "fix_tee"
    except Exception:
        sub.RollBack()

    stub = doc.GetElement(stub_id)
    sconn, _ = nearest_end_connector(stub, P, True)
    if sconn is None:
        raise Exception("Khong lay lai duoc connector ong nhanh.")
    tap = doc.Create.NewTakeoffFitting(sconn, pipe)
    if tap is None:
        raise Exception("Khong tao duoc tee/tap (kiem tra Routing Preferences).")
    return "fix_tap"


def process_fixtures(fixtures, pipes, prog, stats, fix_log):
    prog.start("Noi Plumbing Fixture vao Pipe", len(fixtures))
    for fx in fixtures:
        prog.tick()
        for fconn in get_connectors(fx, only_open=True):
            try:
                if fconn.Domain != DB.Domain.DomainPiping:
                    continue
                fcid = fconn.Id
            except Exception:
                continue
            plan, reason = plan_fixture_conn(fconn, [p for p in pipes if p.IsValidObject])
            if not plan:
                fix_log.append((fx.Id, reason))
                continue
            sub = DB.SubTransaction(doc)
            sub.Start()
            try:
                key = connect_fixture_conn(fx, fcid, plan, pipes)
                sub.Commit()
                stats[key] += 1
            except Exception as ex:
                sub.RollBack()
                stats["failed"] += 1
                fix_log.append((fx.Id, "Loi: {}".format(ex)))


# =====================================================================
# PIPE ACCESSORY -> ONG LIEN (KHONG HO): KEO VE, CAT ONG, NOI 2 DAU
# =====================================================================
def pipe_diameter(pipe):
    try:
        p = pipe.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
        if p:
            return p.AsDouble()
    except Exception:
        pass
    return None


def acc_inline_conns(item):
    """Accessory 'thang': dung 2 connector piping, ca 2 dang ho, huong nguoc nhau.
    Tra ve ((c1, c2), None) hoac (None, ly do)."""
    conns = []
    for c in get_connectors(item):
        try:
            if c.Domain == DB.Domain.DomainPiping:
                conns.append(c)
        except Exception:
            continue
    if len(conns) != 2:
        return None, "Accessory khong co dung 2 connector piping"
    if conns[0].IsConnected or conns[1].IsConnected:
        return None, "Accessory da noi 1 dau"
    d1 = get_connector_direction(conns[0])
    d2 = get_connector_direction(conns[1])
    if d1 is None or d2 is None or d1.DotProduct(d2) > -ANGLE_TOL_COS:
        return None, "2 connector accessory khong thang hang (van goc...)"
    return (conns[0], conns[1]), None


def acc_center_axis(c1, c2):
    """Truc accessory lay theo huong connector (BasisZ), khong theo vector noi 2 origin
    (origin connector co the lech tam trong family)."""
    center = c1.Origin.Add(c2.Origin).Multiply(0.5)
    d1 = get_connector_direction(c1)
    d2 = get_connector_direction(c2)
    if d1 is not None and d2 is not None:
        v = d2.Subtract(d1)
        if v.GetLength() > 1e-6:
            return center, v.Normalize()
    v = c2.Origin.Subtract(c1.Origin)
    if v.GetLength() > 1e-6:
        return center, v.Normalize()
    return center, d2


def acc_parallel(v1, v2):
    if v1 is None or v2 is None:
        return False
    try:
        return abs(v1.DotProduct(v2)) >= ACC_AXIS_TOL_COS
    except Exception:
        return False


def horiz(v):
    h = DB.XYZ(v.X, v.Y, 0)
    return h.Normalize() if h.GetLength() > 1e-6 else None


def conn_system_name(conn):
    try:
        return str(conn.PipeSystemType)
    except Exception:
        return "?"


def pipe_system_label(pipe):
    sid = get_pipe_system_type_id(pipe)
    try:
        st = doc.GetElement(sid)
        return "{} ({})".format(DB.Element.Name.GetValue(st), st.SystemClassification)
    except Exception:
        return get_pipe_classification_name(pipe) or "?"


def plan_accessory(item, pipes):
    """Tim ong tot nhat de chen accessory. Tra ve ((cost, pipe, sys_note), reason)."""
    conns, why = acc_inline_conns(item)
    if conns is None:
        return None, why
    c1, c2 = conns
    center, axis = acc_center_axis(c1, c2)
    if axis is None:
        return None, "Khong doc duoc huong accessory"
    half = c1.Origin.DistanceTo(c2.Origin) / 2.0
    best = None
    reasons = []
    for pipe in pipes:
        try:
            if not pipe.IsValidObject or is_pinned(pipe):
                continue
            curve = pipe.Location.Curve
            if not isinstance(curve, DB.Line):
                continue
            p0 = curve.GetEndPoint(0)
            pdir = curve.Direction.Normalize()
            length = curve.Length
            t = center.Subtract(p0).DotProduct(pdir)
            P = p0.Add(pdir.Multiply(t))
            dist = center.DistanceTo(P)
            if dist > ACC_PULL_MAX:
                continue
            margin = half + ACC_MIN_SEG
            if t < margin or t > length - margin:
                reasons.append((dist, "Ong {} : vi tri chen qua gan dau ong (can >= {:.0f} mm)".format(
                    eid_int(pipe.Id), margin * 304.8)))
                continue
            parallel = acc_parallel(axis, pdir)
            if not parallel and not ACC_ALLOW_ROTATE:
                reasons.append((dist, "Ong {} : khong song song (ACC_ALLOW_ROTATE = False)".format(eid_int(pipe.Id))))
                continue
            sys_note = None
            cost = dist + (0.0 if parallel else ACC_ROTATE_PENALTY)
            if ACC_SYSTEM_MODE != "off" and not system_match(c1, pipe):
                sys_note = "accessory {} -> ong {}".format(conn_system_name(c1), pipe_system_label(pipe))
                if ACC_SYSTEM_MODE == "strict":
                    reasons.append((dist, "Ong {} khac he: {}".format(eid_int(pipe.Id), sys_note)))
                    continue
                cost += ACC_SYSTEM_PENALTY
            if best is None or cost < best[0]:
                best = (cost, pipe, sys_note)
        except Exception:
            continue
    if best:
        return best, None
    if reasons:
        reasons.sort(key=lambda r: r[0])
        return None, reasons[0][1]
    return None, "Khong co ong nao trong {} mm".format(int(ACC_PULL_MAX_MM))


def rotate_to_axis(item, center, axis, pdir):
    """Chi xoay quanh truc Z (family level-based xoay duoc), bo qua do doc ong."""
    a = horiz(axis)
    p = horiz(pdir)
    if a is None or p is None:
        return 0.0
    if a.DotProduct(p) < 0:
        p = p.Negate()
    dot = max(-1.0, min(1.0, a.DotProduct(p)))
    angle = math.acos(dot)
    if angle < math.radians(0.01):
        return 0.0
    if a.CrossProduct(p).Z < 0:
        angle = -angle
    line = DB.Line.CreateBound(center, center.Add(DB.XYZ.BasisZ))
    DB.ElementTransformUtils.RotateElement(doc, item.Id, line, angle)
    doc.Regenerate()
    return abs(math.degrees(angle))


def size_ok(item, cid, dia):
    c = get_conn_by_id(item, cid)
    s = connector_size(c) if c else None
    return s is None or abs(s - dia) <= SNAP_TOL


def try_match_size(item, cids, dia):
    """Doi parameter instance cua accessory de connector = duong kinh ong."""
    if all(size_ok(item, cid, dia) for cid in cids):
        return True
    if not ACC_MATCH_SIZE:
        return False
    for name, factor in ACC_SIZE_PARAMS:
        p = item.LookupParameter(name)
        if p is None or p.IsReadOnly or p.StorageType != DB.StorageType.Double:
            continue
        sub = DB.SubTransaction(doc)
        sub.Start()
        try:
            p.Set(dia * factor)
            doc.Regenerate()
            if all(size_ok(item, cid, dia) for cid in cids):
                sub.Commit()
                return True
            sub.RollBack()
        except Exception:
            sub.RollBack()
    return False


def detach_break_point(seg, other, P):
    """Sau BreakCurve: ngat ket noi giua 2 doan ong tai diem cat,
    xoa fitting Revit tu tao tai diem cat (neu co)."""
    conn, _ = nearest_end_connector(seg, P)
    if conn is None or not conn.IsConnected:
        return
    for ref in list(conn.AllRefs):
        try:
            owner = ref.Owner
            if owner is None or owner.Id == seg.Id or ref.ConnectorType == DB.ConnectorType.Logical:
                continue
            if owner.Id == other.Id:
                conn.DisconnectFrom(ref)
            elif element_kind(owner) == "fitting" and ref.Origin.DistanceTo(P) <= SNAP_TOL:
                doc.Delete(owner.Id)
                doc.Regenerate()
        except Exception:
            continue


def insert_accessory(item, pipe):
    """Xoay (neu can) -> doi size -> keo tam accessory len truc ong -> cat ong
    -> rut 2 dau ong ve 2 connector accessory -> ConnectTo."""
    conns, why = acc_inline_conns(item)
    if conns is None:
        raise Exception(why)
    cid1, cid2 = conns[0].Id, conns[1].Id
    curve = pipe.Location.Curve
    p0 = curve.GetEndPoint(0)
    pdir = curve.Direction.Normalize()

    # 1. Xoay cho thang truc ong
    center, axis = acc_center_axis(conns[0], conns[1])
    rot_deg = 0.0
    if not acc_parallel(axis, pdir):
        if not ACC_ALLOW_ROTATE:
            raise Exception("Accessory khong song song ong")
        rot_deg = rotate_to_axis(item, center, axis, pdir)
        center, axis = acc_center_axis(get_conn_by_id(item, cid1), get_conn_by_id(item, cid2))
        if not acc_parallel(axis, pdir):
            ang = math.degrees(math.acos(max(-1.0, min(1.0, abs(axis.DotProduct(pdir))))))
            raise Exception("Accessory lech ong {:.1f} do sau khi xoay (> {} do)".format(ang, ACC_AXIS_TOL_DEG))

    # 2. Doi size theo ong
    dia = pipe_diameter(pipe)
    mismatch = False
    if dia and not try_match_size(item, (cid1, cid2), dia):
        if not ACC_ALLOW_SIZE_MISMATCH:
            c = get_conn_by_id(item, cid1)
            raise Exception("Size accessory {:.0f} khac ong {:.0f} mm, khong doi duoc size".format(
                (connector_size(c) or 0) * 304.8, dia * 304.8))
        mismatch = True

    # 3. Keo tam accessory len truc ong
    c1 = get_conn_by_id(item, cid1)
    c2 = get_conn_by_id(item, cid2)
    center, _ = acc_center_axis(c1, c2)
    t = center.Subtract(p0).DotProduct(pdir)
    P = p0.Add(pdir.Multiply(t))
    move_vec = P.Subtract(center)
    move_mm = move_vec.GetLength() * 304.8
    if move_vec.GetLength() > 1e-9:
        DB.ElementTransformUtils.MoveElement(doc, item.Id, move_vec)
        doc.Regenerate()
    c1 = get_conn_by_id(item, cid1)
    c2 = get_conn_by_id(item, cid2)
    half = c1.Origin.DistanceTo(c2.Origin) / 2.0
    margin = half + ACC_MIN_SEG
    if t < margin or t > curve.Length - margin:
        raise Exception("Accessory qua gan dau ong sau khi doi size/keo.")

    # 4. Cat ong tai tam accessory
    new_id = DB.Plumbing.PlumbingUtils.BreakCurve(doc, pipe.Id, P)
    new_pipe = doc.GetElement(new_id)
    doc.Regenerate()
    if new_pipe is None:
        raise Exception("Khong cat duoc ong.")
    detach_break_point(pipe, new_pipe, P)
    detach_break_point(new_pipe, pipe, P)

    # 5. Ghep cap doan ong <-> connector accessory theo HUONG connector (BasisZ),
    #    khong theo khoang cach origin (origin 2 connector co the trung nhau / lech tam)
    segs = []
    for seg in (pipe, new_pipe):
        near, d = nearest_end_connector(seg, P, True)
        if near is None:
            near, d = nearest_end_connector(seg, P)
        if near is None or d > half + ACC_MIN_SEG + SNAP_TOL:
            raise Exception("Khong tim thay dau ong tai diem cat (ong {}).".format(eid_int(seg.Id)))
        cur = seg.Location.Curve
        e0, e1 = cur.GetEndPoint(0), cur.GetEndPoint(1)
        far_pt = e0 if e0.DistanceTo(P) > e1.DistanceTo(P) else e1
        u = far_pt.Subtract(P)
        u = u.Normalize() if u.GetLength() > 1e-9 else pdir
        segs.append((seg, near.Id, far_pt, u))

    def side_score(cid, u):
        c = get_conn_by_id(item, cid)
        sc = 0.0
        dz = get_connector_direction(c)
        if dz is not None:
            sc += dz.DotProduct(u) * 10.0
        sc += c.Origin.Subtract(P).DotProduct(u)
        return sc

    uA = segs[0][3]
    a1 = side_score(cid1, uA) - side_score(cid2, uA)
    if abs(a1) < 1e-6:
        raise Exception("Khong phan biet duoc 2 connector accessory (huong/vi tri trung nhau).")
    pairs = [(segs[0], cid1 if a1 > 0 else cid2), (segs[1], cid2 if a1 > 0 else cid1)]

    for (seg, near_id, far_pt, u), icid in pairs:
        ic = get_conn_by_id(item, icid)
        new_v = ic.Origin.Subtract(far_pt)
        if new_v.GetLength() < MIN_PIPE_LENGTH:
            raise Exception("Doan ong {} qua ngan sau khi rut.".format(eid_int(seg.Id)))
        if new_v.Normalize().DotProduct(u.Negate()) < ACC_AXIS_TOL_COS:
            raise Exception("Rut ong {} ve accessory bi lech phuong.".format(eid_int(seg.Id)))
        cur = seg.Location.Curve
        if cur.GetEndPoint(0).DistanceTo(far_pt) < 1e-6:
            apply_pipe_line(seg, (far_pt, ic.Origin))
        else:
            apply_pipe_line(seg, (ic.Origin, far_pt))
        pc, dd = nearest_end_connector(seg, ic.Origin)
        ic = get_conn_by_id(item, icid)
        if pc is None or ic is None or dd > SNAP_TOL:
            raise Exception("Dau ong {} chua trung connector accessory.".format(eid_int(seg.Id)))
        if not pc.IsConnected or not any(r.Owner.Id == item.Id for r in pc.AllRefs):
            pc.ConnectTo(ic)

    info = "noi vao ong {}, keo {:.0f} mm".format(eid_int(pipe.Id), move_mm)
    if rot_deg > 0.01:
        info += ", xoay {:.1f} do".format(rot_deg)
    if mismatch:
        info += ", KHAC SIZE - kiem tra lai"
    return info, new_pipe


def get_view_pipes():
    try:
        return list(DB.FilteredElementCollector(doc, doc.ActiveView.Id)
                    .OfClass(DB.Plumbing.Pipe).WhereElementIsNotElementType())
    except Exception:
        return []


def process_accessories(items, pipes, prog, stats, acc_ok_log, acc_log):
    prog.start("Chen Pipe Accessory vao ong lien", len(items))
    for item in items:
        prog.tick()
        try:
            if not item.IsValidObject or is_pinned(item):
                continue
        except Exception:
            continue
        plan, reason = plan_accessory(item, [p for p in pipes if p.IsValidObject])
        if not plan:
            acc_log.append((item.Id, reason))
            continue
        sub = DB.SubTransaction(doc)
        sub.Start()
        try:
            info, new_pipe = insert_accessory(item, plan[1])
            sub.Commit()
            stats["acc"] += 1
            if plan[2]:
                info += " | KHAC HE ({}) - accessory da nhan he cua ong".format(plan[2])
            acc_ok_log.append((item.Id, info))
            if new_pipe is not None:
                pipes.append(new_pipe)
        except Exception as ex:
            sub.RollBack()
            stats["failed"] += 1
            acc_log.append((item.Id, "Loi: {}".format(ex)))


# =====================================================================
# CHE DO CU: CHEN FITTING/ACCESSORY VAO GIUA ONG
# =====================================================================
def insert_item_into_pipe(item, pipes):
    open_conns = get_connectors(item, only_open=True)
    if len(open_conns) != 2:
        return False, None
    item_pt = open_conns[0].Origin.Add(open_conns[1].Origin).Multiply(0.5)
    closest_pipe, min_dist, best_proj = None, float("inf"), None
    for pipe in list(pipes):
        try:
            curve = pipe.Location.Curve
            if not isinstance(curve, DB.Line) or is_pinned(pipe):
                continue
            pdir = get_pipe_direction(pipe)
            if not all(is_parallel(pdir, get_connector_direction(c)) for c in open_conns):
                continue
            proj = curve.Project(item_pt)
            if proj and proj.Distance < min_dist:
                min_dist, closest_pipe, best_proj = proj.Distance, pipe, proj
        except Exception:
            continue
    if not closest_pipe or min_dist > INSERT_MAX:
        return False, None

    sub = DB.SubTransaction(doc)
    sub.Start()
    try:
        split_pt = best_proj.XYZPoint
        vec = split_pt.Subtract(item_pt)
        if vec.GetLength() > SNAP_TOL:
            DB.ElementTransformUtils.MoveElement(doc, item.Id, vec)
            doc.Regenerate()
        open_conns = get_connectors(item, only_open=True)
        if len(open_conns) != 2:
            raise Exception("Item khong con dung 2 connector ho.")
        cur_pt = open_conns[0].Origin.Add(open_conns[1].Origin).Multiply(0.5)
        cur_curve = closest_pipe.Location.Curve
        fp = cur_curve.Project(cur_pt)
        if not fp:
            raise Exception("Khong chieu duoc item len ong.")
        split_pt = fp.XYZPoint
        if split_pt.DistanceTo(cur_curve.GetEndPoint(0)) < MIN_PIPE_LENGTH:
            raise Exception("Diem cat qua gan dau ong 0.")
        if split_pt.DistanceTo(cur_curve.GetEndPoint(1)) < MIN_PIPE_LENGTH:
            raise Exception("Diem cat qua gan dau ong 1.")

        new_pipe_id = DB.Plumbing.PlumbingUtils.BreakCurve(doc, closest_pipe.Id, split_pt)
        new_pipe = doc.GetElement(new_pipe_id)
        doc.Regenerate()

        c1 = closest_pipe.Location.Curve
        c2 = new_pipe.Location.Curve
        p1_far = c1.GetEndPoint(0) if c1.GetEndPoint(0).DistanceTo(split_pt) > c1.GetEndPoint(1).DistanceTo(split_pt) else c1.GetEndPoint(1)
        if open_conns[0].Origin.DistanceTo(p1_far) < open_conns[1].Origin.DistanceTo(p1_far):
            ic1, ic2 = open_conns[0], open_conns[1]
        else:
            ic1, ic2 = open_conns[1], open_conns[0]

        s1, e1 = c1.GetEndPoint(0), c1.GetEndPoint(1)
        if s1.DistanceTo(split_pt) < e1.DistanceTo(split_pt):
            closest_pipe.Location.Curve = DB.Line.CreateBound(ic1.Origin, e1)
        else:
            closest_pipe.Location.Curve = DB.Line.CreateBound(s1, ic1.Origin)
        s2, e2 = c2.GetEndPoint(0), c2.GetEndPoint(1)
        if s2.DistanceTo(split_pt) < e2.DistanceTo(split_pt):
            new_pipe.Location.Curve = DB.Line.CreateBound(ic2.Origin, e2)
        else:
            new_pipe.Location.Curve = DB.Line.CreateBound(s2, ic2.Origin)
        doc.Regenerate()

        pc1 = min(get_open_connectors_for_matching(closest_pipe), key=lambda c: c.Origin.DistanceTo(ic1.Origin))
        pc2 = min(get_open_connectors_for_matching(new_pipe), key=lambda c: c.Origin.DistanceTo(ic2.Origin))
        if not connectors_compatible(pc1, ic1) or not connectors_compatible(pc2, ic2):
            raise Exception("Connector khong tuong thich.")
        pc1.ConnectTo(ic1)
        pc2.ConnectTo(ic2)
        sub.Commit()
        return True, new_pipe
    except Exception:
        sub.RollBack()
        return False, None


# =====================================================================
# SELECTION / TRANSACTION
# =====================================================================
class PipeNetworkFilter(ISelectionFilter):
    def AllowElement(self, element):
        return element_kind(element) is not None

    def AllowReference(self, reference, point):
        return False


class WarningSwallower(DB.IFailuresPreprocessor):
    """An cac warning (vd: 'slightly off axis') de khong bat popup lien tuc."""
    def PreprocessFailures(self, failures_accessor):
        try:
            for f in failures_accessor.GetFailureMessages():
                if f.GetSeverity() == DB.FailureSeverity.Warning:
                    failures_accessor.DeleteWarning(f)
        except Exception:
            pass
        return DB.FailureProcessingResult.Continue


def get_selection():
    ids = uidoc.Selection.GetElementIds()
    elements = [doc.GetElement(i) for i in ids]
    elements = [e for e in elements if e and element_kind(e)]
    if elements:
        return elements
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element, PipeNetworkFilter(),
            "Quet chon Pipe, Pipe Fitting, Pipe Accessory (Plumbing Fixture neu can), sau do bam Finish")
        return [doc.GetElement(r.ElementId) for r in refs]
    except Exception:
        script.exit()


def ask_options():
    items = [
        forms.TemplateListItem(OPT_SNAP, checked=True),
        forms.TemplateListItem(OPT_PULL, checked=True),
        forms.TemplateListItem(OPT_PIPE, checked=True),
        forms.TemplateListItem(OPT_MOVE, checked=True),
        forms.TemplateListItem(OPT_INSERT, checked=False),
        forms.TemplateListItem(OPT_FIXTURE, checked=True),
        forms.TemplateListItem(OPT_ACC, checked=True),
    ]
    res = forms.SelectFromList.show(
        items, multiselect=True, title="Connect Free Elements Plus - chon che do",
        button_name="Chay", width=700, height=420)
    if not res:
        script.exit()
    return set(res)


def main():
    elements = get_selection()
    all_pipes = [e for e in elements if e and is_pipe(e)]   # ke ca ong da noi du (lam ong dich cho tee)
    elements = [e for e in elements if e and element_kind(e) and get_open_connectors_for_matching(e)]
    has_acc = any(get_category_id(e) == CAT_PIPE_ACCESSORY for e in elements)
    if not elements or (len(elements) + len(all_pipes) < 2 and not has_acc):
        forms.alert("Can it nhat 2 doi tuong, trong do co doi tuong co connector dang ho.", title="Thong bao")
        script.exit()

    opts = ask_options()
    stats = dict(snap=0, pull=0, extend=0, elbow=0, move=0, insert=0,
                 fix_elbow=0, fix_tee=0, fix_tap=0, acc=0, failed=0)
    fix_log = []
    acc_log = []
    acc_ok_log = []
    moved_log = []
    cancelled = False

    t = DB.Transaction(doc, "Connect Free Elements Plus")
    t.Start()
    fho = t.GetFailureHandlingOptions()
    fho.SetFailuresPreprocessor(WarningSwallower())
    t.SetFailureHandlingOptions(fho)

    try:
        with forms.ProgressBar(title="Connect Free Elements Plus", cancellable=True) as pb:
            prog = Progress(pb)
            try:
                if opts & set([OPT_SNAP, OPT_PULL, OPT_PIPE, OPT_MOVE]):
                    candidates = build_candidates(elements, opts, prog)
                    process_candidates(candidates, opts, prog, stats, moved_log)
                    doc.Regenerate()

                if OPT_FIXTURE in opts:
                    fixtures = [e for e in elements if element_kind(e) == "fixture"
                                and get_connectors(e, True)]
                    if fixtures and all_pipes:
                        process_fixtures(fixtures, all_pipes, prog, stats, fix_log)
                        doc.Regenerate()

                if OPT_ACC in opts:
                    accs = [e for e in elements if e.IsValidObject
                            and get_category_id(e) == CAT_PIPE_ACCESSORY and not is_pinned(e)
                            and acc_inline_conns(e)[0] is not None]
                    if accs:
                        acc_pipes = [p for p in all_pipes if p.IsValidObject]
                        if ACC_SEARCH_VIEW_PIPES:
                            ids = set(eid_int(p.Id) for p in acc_pipes)
                            acc_pipes += [p for p in get_view_pipes() if eid_int(p.Id) not in ids]
                        process_accessories(accs, acc_pipes, prog, stats, acc_ok_log, acc_log)
                        doc.Regenerate()

                if OPT_INSERT in opts:
                    pipes = [e for e in elements if is_pipe(e)]
                    items = [e for e in elements if element_kind(e) == "fitting"
                             and len(get_connectors(e, True)) == 2 and not is_pinned(e)]
                    prog.start("Chen fitting/accessory vao ong", len(items))
                    for item in items:
                        prog.tick()
                        if len(get_connectors(item, True)) != 2:
                            continue
                        ok, new_pipe = insert_item_into_pipe(item, pipes)
                        if ok:
                            stats["insert"] += 1
                            moved_log.append((item.Id, -1))
                            if new_pipe:
                                pipes.append(new_pipe)
            except Cancelled:
                cancelled = True

        if cancelled:
            keep = forms.alert("Da Cancel.\n\nGiu lai cac ket noi da thuc hien truoc khi Cancel?",
                               title="Cancel", yes=True, no=True)
            if keep:
                t.Commit()
            else:
                t.RollBack()
                forms.alert("Da hoan tac toan bo.", title="Cancel", warn_icon=False)
                return
        else:
            t.Commit()
    except Exception as ex:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        forms.alert("Loi: {}".format(ex), title="Loi")
        return

    # ---------- Bao cao ----------
    output.print_md("## Connect Free Elements Plus{}".format(" (da Cancel giua chung)" if cancelled else ""))
    output.print_md(
        "| Phuong an | So ket noi | Co move doi tuong? |\n|---|---|---|\n"
        "| Noi connector da trung | {snap} | Khong |\n"
        "| Keo dau Pipe ve Fitting/Accessory/Fixture | {pull} | Khong (chi doi chieu dai ong) |\n"
        "| Pipe - Pipe thang hang (union) | {extend} | Khong (chi doi chieu dai ong) |\n"
        "| Pipe - Pipe lech goc (elbow) | {elbow} | Khong (ong tu cat/keo toi elbow) |\n"
        "| Fitting/Accessory - Fitting/Accessory | {move} | Co (<= {mx} mm) |\n"
        "| Che do cu: chen vao ong | {insert} | Co |\n"
        "| Fixture -> Pipe: ong nhanh + elbow (keo dau ong) | {fix_elbow} | Khong move fixture |\n"
        "| Fixture -> Pipe: ong nhanh + tee (cat ong) | {fix_tee} | Khong move fixture |\n"
        "| Fixture -> Pipe: ong nhanh + tap | {fix_tap} | Khong move fixture |\n"
        "| Accessory -> ong lien: keo ve + cat ong + noi 2 dau | {acc} | Co (keo/xoay accessory) |\n"
        "| Phuong an thu nhung that bai (da rollback) | {failed} | - |".format(mx=int(FIT_MOVE_MAX_MM), **stats))
    if moved_log:
        output.print_md("### Doi tuong da bi move (kiem tra lai):")
        for eid, dist_mm in moved_log:
            label = "chen vao ong" if dist_mm < 0 else "{:.1f} mm".format(dist_mm)
            print("{}  -  {}".format(output.linkify(eid), label))

    if acc_ok_log:
        output.print_md("### Pipe Accessory da chen vao ong (kiem tra lai):")
        for eid, info in acc_ok_log:
            print("{}  -  {}".format(output.linkify(eid), info))

    if acc_log:
        output.print_md("### Pipe Accessory chua chen duoc:")
        for eid, why in acc_log:
            print("{}  -  {}".format(output.linkify(eid), why))

    if fix_log:
        output.print_md("### Plumbing Fixture chua noi duoc:")
        for eid, why in fix_log:
            print("{}  -  {}".format(output.linkify(eid), why))

    total = stats["fix_elbow"] + stats["fix_tee"] + stats["fix_tap"] + stats["snap"] + stats["pull"] + stats["extend"] + stats["elbow"] + stats["move"] + stats["insert"] + stats["acc"]
    forms.toast("Da noi {} ket noi | move {} doi tuong".format(total, len(moved_log) + len(acc_ok_log)),
                title="Connect Free Elements Plus")


if __name__ == "__main__":
    main()
