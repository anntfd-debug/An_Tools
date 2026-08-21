# -*- coding: utf-8 -*-
"""
FIX EXISTING 135-DEG TEE / WYE TO 45-DEG
========================================

PURPOSE
-------
Fix already-created Pipe Fittings where:

    - fitting has 3 connectors
    - fitting is connected directly to 3 Pipes
    - 2 Pipes form the MAIN run
    - MAIN is sloped
    - BRANCH connector / family angle is ~135 deg
    - desired angle is 45 deg

METHOD
------
1. Preselect Pipe Fittings.
2. Run tool.
3. Detect MAIN pair automatically from true 3D pipe centerlines.
4. Detect BRANCH pipe.
5. Try:
       branch_connector.Angle = 45 deg
6. If family does not expose writable Connector.Angle:
       fallback to writable instance angle parameter.
7. Regenerate.
8. Verify all original 3 pipes are still physically connected.
9. If validation fails:
       rollback that fitting only.

SAFE RULES
----------
- Does NOT delete/recreate fitting.
- Does NOT modify Tee 90 deg.
- Does NOT modify horizontal-main fitting by default.
- Does NOT blindly rotate fitting.
- Does NOT convert every geometric 135 deg to 45 deg.
- Each fitting uses its own SubTransaction.

pyRevit
Revit 2025 / 2026
"""

from pyrevit import revit, DB, script

import math


# ============================================================
# REVIT CONTEXT
# ============================================================

uidoc = revit.uidoc
doc = revit.doc

output = script.get_output()


# ============================================================
# SETTINGS
# ============================================================

# Current wrong family/connector angle
WRONG_ANGLE_DEG = 135.0

# Desired angle
TARGET_ANGLE_DEG = 45.0

# Angle tolerance
ANGLE_TOL_DEG = 2.0


# The two MAIN pipes should be almost collinear.
#
# abs(dot):
# 1.000 = perfectly parallel / opposite
# 0.985 ~= within ~10 degrees
#
RUN_COLLINEAR_DOT_MIN = 0.985


# Only process fittings whose MAIN pipe has slope.
#
# True:
#     target the problem described here.
#
# False:
#     also process horizontal / vertical main.
#
ONLY_SLOPED_MAIN = True


# Very small Z component = horizontal.
SLOPE_Z_TOL = 1e-6


# Almost vertical is not considered a "sloped main"
# for this tool.
VERTICAL_Z_LIMIT = 0.999999


# Parameter fallback.
#
# Used only if Connector.Angle cannot be written.
#
ANGLE_PARAMETER_KEYWORDS = (
    "angle",
    "branch angle",
    "tee angle",
    "wye angle",
    "angle 1",
    "angle1",
    "góc",
    "goc",
)


# ============================================================
# ELEMENT ID
# ============================================================

def eid_value(eid):
    """Revit 2025/2026 compatible ElementId -> int."""

    if eid is None:
        return None

    try:
        return int(eid.Value)
    except:
        pass

    try:
        return int(eid.IntegerValue)
    except:
        return None


def same_id(id1, id2):

    a = eid_value(id1)
    b = eid_value(id2)

    return (
        a is not None
        and b is not None
        and a == b
    )


# ============================================================
# UNIT - ANGLE
# ============================================================

def deg_to_internal(value_deg):
    """
    Convert degrees to Revit internal angle unit.
    Revit 2025/2026.
    """

    try:
        return DB.UnitUtils.ConvertToInternalUnits(
            float(value_deg),
            DB.UnitTypeId.Degrees
        )
    except:
        # Fallback: Revit API angle values are radians.
        return math.radians(float(value_deg))


def internal_to_deg(value_internal):

    try:
        return DB.UnitUtils.ConvertFromInternalUnits(
            float(value_internal),
            DB.UnitTypeId.Degrees
        )
    except:
        return math.degrees(float(value_internal))


def angle_close_deg(a, b, tol=ANGLE_TOL_DEG):

    try:
        return abs(float(a) - float(b)) <= float(tol)
    except:
        return False


# ============================================================
# CATEGORY
# ============================================================

PIPE_CATEGORY_ID = eid_value(
    DB.ElementId(
        DB.BuiltInCategory.OST_PipeCurves
    )
)

PIPE_FITTING_CATEGORY_ID = eid_value(
    DB.ElementId(
        DB.BuiltInCategory.OST_PipeFitting
    )
)


def category_id(elem):

    if elem is None:
        return None

    try:

        cat = elem.Category

        if cat is None:
            return None

        return eid_value(cat.Id)

    except:
        return None


def is_pipe(elem):

    return (
        elem is not None
        and category_id(elem) == PIPE_CATEGORY_ID
    )


def is_pipe_fitting(elem):

    return (
        elem is not None
        and category_id(elem) == PIPE_FITTING_CATEGORY_ID
    )


# ============================================================
# CONNECTOR
# ============================================================

def get_connectors(elem):
    """
    Get MEP connectors from FamilyInstance.
    """

    if elem is None:
        return []

    try:

        mep = elem.MEPModel

        if mep is None:
            return []

        cm = mep.ConnectorManager

        if cm is None:
            return []

        return [
            c
            for c in cm.Connectors
        ]

    except:
        return []


def is_physical_connector(conn):
    """
    Filter logical connectors out of AllRefs.
    """

    if conn is None:
        return False

    try:

        ct = conn.ConnectorType

        physical_types = []

        try:
            physical_types.append(
                DB.ConnectorType.End
            )
        except:
            pass

        try:
            physical_types.append(
                DB.ConnectorType.Curve
            )
        except:
            pass

        try:
            physical_types.append(
                DB.ConnectorType.Physical
            )
        except:
            pass

        if physical_types:
            return ct in physical_types

    except:
        pass

    # Fallback
    return True


def connector_is_connected_to(c1, c2):

    if c1 is None or c2 is None:
        return False

    try:
        return bool(
            c1.IsConnectedTo(c2)
        )
    except:
        return False


def get_connected_pipe(fitting, fit_conn):
    """
    Get the Pipe physically connected to one fitting connector.

    Returns:
        (pipe, pipe_connector)

    or:
        (None, None)
    """

    if fitting is None or fit_conn is None:
        return None, None

    try:
        refs = fit_conn.AllRefs
    except:
        return None, None

    for ref_conn in refs:

        try:

            if ref_conn is None:
                continue

            if not is_physical_connector(ref_conn):
                continue

            owner = ref_conn.Owner

            if owner is None:
                continue

            if same_id(
                owner.Id,
                fitting.Id
            ):
                continue

            if not is_pipe(owner):
                continue

            # Confirm actual physical relation.
            if not connector_is_connected_to(
                fit_conn,
                ref_conn
            ):
                continue

            return owner, ref_conn

        except:
            continue

    return None, None


# ============================================================
# PIPE GEOMETRY
# ============================================================

def get_pipe_direction(pipe):
    """
    TRUE 3D direction of Pipe.Location.Curve.

    Z is NOT removed.

    This is important for sloped pipes.
    """

    if pipe is None:
        return None

    try:

        loc = pipe.Location

        if loc is None:
            return None

        curve = loc.Curve

        if curve is None:
            return None

        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)

        v = p1 - p0

        if v.GetLength() <= 1e-12:
            return None

        return v.Normalize()

    except:
        return None


def abs_dot(v1, v2):

    if v1 is None or v2 is None:
        return -1.0

    try:

        d = v1.DotProduct(v2)

        if d > 1.0:
            d = 1.0

        if d < -1.0:
            d = -1.0

        return abs(d)

    except:
        return -1.0


def is_sloped_direction(v):
    """
    Horizontal:
        Z ~= 0

    Vertical:
        abs(Z) ~= 1

    Sloped:
        0 < abs(Z) < 1
    """

    if v is None:
        return False

    try:

        z = abs(v.Z)

        if z <= SLOPE_Z_TOL:
            return False

        if z >= VERTICAL_Z_LIMIT:
            return False

        return True

    except:
        return False


# ============================================================
# TEE TOPOLOGY
# ============================================================

def analyze_tee(fitting):
    """
    Analyze fitting topology.

    Returns dictionary:

    {
        "items": [...],
        "main_a": item,
        "main_b": item,
        "branch": item,
        "main_dot": ...
    }

    Each item:

    {
        "fit_conn": Connector,
        "pipe": Pipe,
        "pipe_conn": Connector,
        "direction": XYZ
    }

    Main pair is selected by maximum abs(dot)
    of their TRUE 3D centerlines.
    """

    connectors = get_connectors(fitting)

    if len(connectors) != 3:
        return None, "Fitting không có đúng 3 connector"

    items = []

    pipe_ids = set()

    for fit_conn in connectors:

        pipe, pipe_conn = get_connected_pipe(
            fitting,
            fit_conn
        )

        if pipe is None:
            return (
                None,
                "Có connector không nối trực tiếp vào Pipe"
            )

        pid = eid_value(pipe.Id)

        if pid in pipe_ids:
            return (
                None,
                "Không xác định được 3 Pipe riêng biệt"
            )

        pipe_ids.add(pid)

        direction = get_pipe_direction(pipe)

        if direction is None:
            return (
                None,
                "Không đọc được centerline Pipe ID {}".format(
                    pid
                )
            )

        items.append({
            "fit_conn": fit_conn,
            "pipe": pipe,
            "pipe_conn": pipe_conn,
            "direction": direction,
        })

    # --------------------------------------------------------
    # Find the most collinear pair.
    # --------------------------------------------------------

    pairs = (
        (0, 1),
        (0, 2),
        (1, 2),
    )

    best_pair = None
    best_dot = -1.0

    for i, j in pairs:

        d = abs_dot(
            items[i]["direction"],
            items[j]["direction"]
        )

        if d > best_dot:

            best_dot = d
            best_pair = (i, j)

    if best_pair is None:
        return None, "Không xác định được MAIN run"

    if best_dot < RUN_COLLINEAR_DOT_MIN:
        return (
            None,
            "Không tìm thấy 2 Pipe MAIN đủ thẳng hàng "
            "(abs dot = {:.6f})".format(
                best_dot
            )
        )

    i, j = best_pair

    branch_index = (
        set([0, 1, 2])
        - set([i, j])
    ).pop()

    main_a = items[i]
    main_b = items[j]
    branch = items[branch_index]

    # --------------------------------------------------------
    # Main slope validation
    # --------------------------------------------------------

    main_is_sloped = (
        is_sloped_direction(
            main_a["direction"]
        )
        or
        is_sloped_direction(
            main_b["direction"]
        )
    )

    if ONLY_SLOPED_MAIN and not main_is_sloped:
        return (
            None,
            "MAIN không có slope"
        )

    return {
        "items": items,
        "main_a": main_a,
        "main_b": main_b,
        "branch": branch,
        "main_dot": best_dot,
        "main_is_sloped": main_is_sloped,
    }, None


# ============================================================
# CONNECTION SNAPSHOT
# ============================================================

def connected_pipe_ids(fitting):
    """
    Return set of physically connected Pipe IDs.
    """

    result = set()

    for conn in get_connectors(fitting):

        pipe, pipe_conn = get_connected_pipe(
            fitting,
            conn
        )

        if pipe is not None:

            pid = eid_value(pipe.Id)

            if pid is not None:
                result.add(pid)

    return result


def find_connector_for_pipe(
    fitting,
    target_pipe_id
):
    """
    Reacquire fitting connector connected
    to a particular Pipe after Regenerate().
    """

    for conn in get_connectors(fitting):

        pipe, pipe_conn = get_connected_pipe(
            fitting,
            conn
        )

        if pipe is None:
            continue

        if same_id(
            pipe.Id,
            target_pipe_id
        ):
            return conn

    return None


# ============================================================
# ANGLE PARAMETER FALLBACK
# ============================================================

def parameter_name(param):

    try:
        return param.Definition.Name or ""
    except:
        return ""


def parameter_is_angle_candidate(param):
    """
    Look for a writable Double instance parameter
    that appears to represent an angle.
    """

    if param is None:
        return False

    try:

        if param.IsReadOnly:
            return False

        if param.StorageType != DB.StorageType.Double:
            return False

    except:
        return False

    name = parameter_name(param)

    try:
        lname = name.lower()
    except:
        lname = name

    for keyword in ANGLE_PARAMETER_KEYWORDS:

        try:

            if keyword.lower() in lname:
                return True

        except:
            continue

    return False


def find_wrong_angle_parameter(fitting):
    """
    Find writable fitting parameter whose
    current value is approximately 135 degrees.
    """

    candidates = []

    try:
        params = fitting.Parameters
    except:
        return None

    for param in params:

        if not parameter_is_angle_candidate(param):
            continue

        try:

            value_internal = param.AsDouble()

            value_deg = internal_to_deg(
                value_internal
            )

        except:
            continue

        if not angle_close_deg(
            value_deg,
            WRONG_ANGLE_DEG
        ):
            continue

        name = parameter_name(param)

        # Ranking:
        # prefer names that explicitly say branch.
        try:
            lname = name.lower()
        except:
            lname = name

        rank = 10

        if "branch" in lname:
            rank = 0
        elif lname == "angle":
            rank = 1
        elif "angle 1" in lname:
            rank = 2
        elif "angle1" in lname:
            rank = 3
        else:
            rank = 5

        candidates.append(
            (
                rank,
                name,
                param,
                value_deg
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            x[0],
            x[1]
        )
    )

    return candidates[0][2]


# ============================================================
# TRY FIX CONNECTOR ANGLE
# ============================================================

def get_connector_angle_deg(conn):

    if conn is None:
        return None

    try:

        value = conn.Angle

        return internal_to_deg(value)

    except:
        return None


def try_set_connector_angle(
    branch_conn,
    target_deg
):
    """
    First/preferred method.

    Connector.Angle is writable only when
    family connector angle is mapped to
    a writable instance parameter.
    """

    if branch_conn is None:
        return False, None

    old_deg = get_connector_angle_deg(
        branch_conn
    )

    if old_deg is None:
        return False, None

    # Only touch an actual 135-deg connector.
    if not angle_close_deg(
        old_deg,
        WRONG_ANGLE_DEG
    ):
        return False, old_deg

    try:

        branch_conn.Angle = deg_to_internal(
            target_deg
        )

        return True, old_deg

    except:
        return False, old_deg


# ============================================================
# TRY PARAMETER FALLBACK
# ============================================================

def try_set_parameter_angle(
    fitting,
    target_deg
):

    param = find_wrong_angle_parameter(
        fitting
    )

    if param is None:
        return False, None, None

    name = parameter_name(param)

    try:

        old_deg = internal_to_deg(
            param.AsDouble()
        )

        ok = param.Set(
            deg_to_internal(
                target_deg
            )
        )

        if not ok:
            return False, name, old_deg

        return True, name, old_deg

    except:
        return False, name, None


# ============================================================
# VALIDATION
# ============================================================

def validate_after_fix(
    fitting_id,
    original_pipe_ids,
    branch_pipe_id,
    method,
    parameter_name_used=None
):
    """
    Verify:
    - fitting still exists
    - still connected to exact original 3 Pipes
    - angle now ~45
    """

    fitting = doc.GetElement(
        fitting_id
    )

    if fitting is None:
        return False, "Fitting không còn tồn tại"

    new_pipe_ids = connected_pipe_ids(
        fitting
    )

    if new_pipe_ids != original_pipe_ids:

        return (
            False,
            "Connection thay đổi: {} -> {}".format(
                sorted(original_pipe_ids),
                sorted(new_pipe_ids)
            )
        )

    # --------------------------------------------------------
    # Connector.Angle validation
    # --------------------------------------------------------

    if method == "CONNECTOR":

        branch_conn = find_connector_for_pipe(
            fitting,
            branch_pipe_id
        )

        if branch_conn is None:
            return (
                False,
                "Không tìm lại được BRANCH connector"
            )

        new_deg = get_connector_angle_deg(
            branch_conn
        )

        if new_deg is None:
            return (
                False,
                "Không đọc lại được Connector.Angle"
            )

        if not angle_close_deg(
            new_deg,
            TARGET_ANGLE_DEG
        ):
            return (
                False,
                "Connector.Angle sau sửa = {:.3f}°".format(
                    new_deg
                )
            )

        return (
            True,
            "Connector.Angle = {:.3f}°".format(
                new_deg
            )
        )

    # --------------------------------------------------------
    # Parameter fallback validation
    # --------------------------------------------------------

    if method == "PARAMETER":

        param = None

        if parameter_name_used:

            try:
                param = fitting.LookupParameter(
                    parameter_name_used
                )
            except:
                param = None

        if param is None:
            return (
                False,
                "Không tìm lại được parameter '{}'".format(
                    parameter_name_used
                )
            )

        try:

            new_deg = internal_to_deg(
                param.AsDouble()
            )

        except:

            return (
                False,
                "Không đọc lại được angle parameter"
            )

        if not angle_close_deg(
            new_deg,
            TARGET_ANGLE_DEG
        ):
            return (
                False,
                "{} sau sửa = {:.3f}°".format(
                    parameter_name_used,
                    new_deg
                )
            )

        return (
            True,
            "{} = {:.3f}°".format(
                parameter_name_used,
                new_deg
            )
        )

    return False, "Unknown fix method"


# ============================================================
# FIX ONE FITTING
# ============================================================

def fix_one_fitting(fitting):
    """
    Returns:

        {
            "status": "FIXED" / "SKIP" / "ERROR",
            "message": ...,
            "method": ...
        }
    """

    fitting_id = fitting.Id
    fitting_id_value = eid_value(
        fitting_id
    )

    # --------------------------------------------------------
    # Analyze topology
    # --------------------------------------------------------

    analysis, reason = analyze_tee(
        fitting
    )

    if analysis is None:

        return {
            "status": "SKIP",
            "message": reason,
            "method": None,
        }

    branch_item = analysis["branch"]

    branch_conn = branch_item[
        "fit_conn"
    ]

    branch_pipe = branch_item[
        "pipe"
    ]

    branch_pipe_id = branch_pipe.Id

    original_pipe_ids = connected_pipe_ids(
        fitting
    )

    if len(original_pipe_ids) != 3:

        return {
            "status": "SKIP",
            "message": (
                "Fitting không nối đủ 3 Pipe"
            ),
            "method": None,
        }

    # --------------------------------------------------------
    # Inspect current connector angle.
    # --------------------------------------------------------

    branch_angle_deg = get_connector_angle_deg(
        branch_conn
    )

    # If Connector.Angle gives a meaningful
    # non-135 value, do not blindly modify it.
    if (
        branch_angle_deg is not None
        and abs(branch_angle_deg) > 1e-8
        and not angle_close_deg(
            branch_angle_deg,
            WRONG_ANGLE_DEG
        )
    ):

        # It may already be 45.
        if angle_close_deg(
            branch_angle_deg,
            TARGET_ANGLE_DEG
        ):

            return {
                "status": "SKIP",
                "message": (
                    "BRANCH đã là {:.3f}°"
                ).format(
                    branch_angle_deg
                ),
                "method": None,
            }

    # --------------------------------------------------------
    # Each fitting has its own rollback scope.
    # --------------------------------------------------------

    st = DB.SubTransaction(doc)

    try:

        st.Start()

        method = None
        parameter_used = None
        old_angle = None

        # ====================================================
        # METHOD 1
        # Connector.Angle
        # ====================================================

        ok, connector_old_deg = (
            try_set_connector_angle(
                branch_conn,
                TARGET_ANGLE_DEG
            )
        )

        if ok:

            method = "CONNECTOR"
            old_angle = connector_old_deg

        # ====================================================
        # METHOD 2
        # Fallback angle parameter
        # ====================================================

        if not ok:

            (
                ok,
                parameter_used,
                parameter_old_deg
            ) = try_set_parameter_angle(
                fitting,
                TARGET_ANGLE_DEG
            )

            if ok:

                method = "PARAMETER"
                old_angle = parameter_old_deg

        # ====================================================
        # No writable angle
        # ====================================================

        if not ok:

            st.RollBack()

            return {
                "status": "SKIP",
                "message": (
                    "Không tìm thấy BRANCH Connector.Angle "
                    "hoặc instance angle parameter "
                    "có thể sửa từ ~135°"
                ),
                "method": None,
            }

        # ====================================================
        # Regenerate
        # ====================================================

        doc.Regenerate()

        # ====================================================
        # Validate
        # ====================================================

        valid, validation_message = (
            validate_after_fix(
                fitting_id,
                original_pipe_ids,
                branch_pipe_id,
                method,
                parameter_used
            )
        )

        if not valid:

            st.RollBack()

            return {
                "status": "ERROR",
                "message": (
                    "Rollback: {}"
                ).format(
                    validation_message
                ),
                "method": method,
            }

        # ====================================================
        # Commit this fitting
        # ====================================================

        st.Commit()

        return {
            "status": "FIXED",
            "message": (
                "{:.3f}° -> {:.3f}° | {}"
            ).format(
                old_angle
                if old_angle is not None
                else WRONG_ANGLE_DEG,
                TARGET_ANGLE_DEG,
                validation_message
            ),
            "method": method,
        }

    except Exception as ex:

        try:

            if st.GetStatus() == (
                DB.TransactionStatus.Started
            ):
                st.RollBack()

        except:
            pass

        return {
            "status": "ERROR",
            "message": str(ex),
            "method": None,
        }


# ============================================================
# GET PRESELECTION
# ============================================================

def get_selected_fittings():

    result = []

    try:
        selected_ids = list(
            uidoc.Selection.GetElementIds()
        )
    except:
        selected_ids = []

    for eid in selected_ids:

        try:
            elem = doc.GetElement(eid)
        except:
            elem = None

        if elem is None:
            continue

        if not is_pipe_fitting(elem):
            continue

        # Require exactly 3 MEP connectors.
        if len(get_connectors(elem)) != 3:
            continue

        result.append(elem)

    return result


# ============================================================
# DISPLAY
# ============================================================

def fitting_name(fitting):

    if fitting is None:
        return ""

    try:

        family_name = (
            fitting.Symbol.Family.Name
        )

    except:
        family_name = ""

    try:

        type_name = fitting.Symbol.Name

    except:
        type_name = ""

    if family_name and type_name:
        return "{} : {}".format(
            family_name,
            type_name
        )

    if family_name:
        return family_name

    return type_name


# ============================================================
# MAIN
# ============================================================

fittings = get_selected_fittings()


if not fittings:

    try:

        DB.TaskDialog.Show(
            "Fix Tee 135 to 45",
            (
                "Hãy preselect các Pipe Fitting Tee/Wye "
                "cần sửa rồi chạy lại tool."
            )
        )

    except:
        pass

    script.exit()


# ============================================================
# TRANSACTION
# ============================================================

fixed = []
skipped = []
errors = []


tx = DB.Transaction(
    doc,
    "Fix Sloped Tee 135 to 45"
)


try:

    tx.Start()

    for fitting in fittings:

        fid = eid_value(
            fitting.Id
        )

        name = fitting_name(
            fitting
        )

        result = fix_one_fitting(
            fitting
        )

        record = {
            "id": fid,
            "name": name,
            "message": result["message"],
            "method": result["method"],
        }

        if result["status"] == "FIXED":
            fixed.append(record)

        elif result["status"] == "SKIP":
            skipped.append(record)

        else:
            errors.append(record)

    tx.Commit()


except Exception as ex:

    try:

        if tx.GetStatus() == (
            DB.TransactionStatus.Started
        ):
            tx.RollBack()

    except:
        pass

    print(
        "FATAL ERROR: {}".format(
            ex
        )
    )

    script.exit()


# ============================================================
# OUTPUT
# ============================================================

print("")
print("=" * 70)
print("FIX SLOPED TEE / WYE 135 -> 45")
print("=" * 70)

print("")
print(
    "Selected : {}".format(
        len(fittings)
    )
)

print(
    "Fixed    : {}".format(
        len(fixed)
    )
)

print(
    "Skipped  : {}".format(
        len(skipped)
    )
)

print(
    "Errors   : {}".format(
        len(errors)
    )
)


# ------------------------------------------------------------
# Fixed
# ------------------------------------------------------------

if fixed:

    print("")
    print("FIXED")
    print("-" * 70)

    for item in fixed:

        print(
            "ID {} | {} | {} | {}".format(
                item["id"],
                item["name"],
                item["method"],
                item["message"]
            )
        )


# ------------------------------------------------------------
# Skipped
# ------------------------------------------------------------

if skipped:

    print("")
    print("SKIPPED")
    print("-" * 70)

    for item in skipped:

        print(
            "ID {} | {} | {}".format(
                item["id"],
                item["name"],
                item["message"]
            )
        )


# ------------------------------------------------------------
# Errors
# ------------------------------------------------------------

if errors:

    print("")
    print("ERRORS")
    print("-" * 70)

    for item in errors:

        print(
            "ID {} | {} | {}".format(
                item["id"],
                item["name"],
                item["message"]
            )
        )


print("")
print("=" * 70)