# -*- coding: utf-8 -*-
"""
AUTO ROTATE SELECTED MEP AROUND CONNECTED EXTERNAL PIPE

Workflow:
1. Preselect Pipe / Pipe Fitting / Pipe Accessory
2. Run tool
3. Enter rotation angle
4. Tool automatically finds a Pipe that:
      - is NOT selected
      - is physically connected directly to the selected cluster
5. If exactly ONE external Pipe is found:
      -> use its actual centerline as rotation axis
6. Rotate all selected elements around that Pipe

Axis Pipe may be:
- Vertical
- Horizontal
- Sloped

RULE:
- Success     -> silent
- Cancel      -> silent
- Angle = 0   -> silent
- Error       -> popup only

pyRevit / Revit 2025-2026
"""

from pyrevit import revit

from Autodesk.Revit import DB
from Autodesk.Revit.UI import TaskDialog

from System.Collections.Generic import List

import math
import clr


# ============================================================
# WINFORMS
# ============================================================

clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from System.Windows.Forms import (
    Form,
    Label,
    TextBox,
    Button,
    DialogResult,
    FormBorderStyle,
    FormStartPosition
)

from System.Drawing import Point, Size


# ============================================================
# REVIT CONTEXT
# ============================================================

uidoc = revit.uidoc
doc = revit.doc


# ============================================================
# SETTINGS
# ============================================================

TARGET_BICS = (
    DB.BuiltInCategory.OST_PipeCurves,
    DB.BuiltInCategory.OST_PipeFitting,
    DB.BuiltInCategory.OST_PipeAccessory,
)

VECTOR_TOL = 1e-9


# ============================================================
# ERROR
# ============================================================

def show_error(message):

    try:
        TaskDialog.Show(
            "Rotate Around Connected Pipe",
            message
        )
    except:
        pass


# ============================================================
# ELEMENT ID
# ============================================================

def eid_value(eid):

    if eid is None:
        return None

    # Revit newer versions
    try:
        return int(eid.Value)
    except:
        pass

    # Older API compatibility
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
# CATEGORY
# ============================================================

def get_bic_id_value(bic):

    try:
        return eid_value(
            DB.ElementId(bic)
        )
    except:
        return None


def get_category_id_value(elem):

    if elem is None:
        return None

    try:

        cat = elem.Category

        if cat is None:
            return None

        return eid_value(cat.Id)

    except:
        return None


TARGET_CATEGORY_IDS = set()

for bic in TARGET_BICS:

    value = get_bic_id_value(bic)

    if value is not None:
        TARGET_CATEGORY_IDS.add(value)


PIPE_CATEGORY_ID = get_bic_id_value(
    DB.BuiltInCategory.OST_PipeCurves
)


def is_target_mep(elem):

    return (
        get_category_id_value(elem)
        in TARGET_CATEGORY_IDS
    )


def is_pipe(elem):

    if elem is None:
        return False

    return (
        get_category_id_value(elem)
        == PIPE_CATEGORY_ID
    )


# ============================================================
# CONNECTOR MANAGER
# ============================================================

def get_connector_manager(elem):
    """
    Supports:

    Pipe / MEPCurve
        -> elem.ConnectorManager

    Pipe Fitting / Pipe Accessory
        -> elem.MEPModel.ConnectorManager
    """

    if elem is None:
        return None

    # --------------------------------------------------------
    # MEPCurve / Pipe
    # --------------------------------------------------------

    try:

        cm = elem.ConnectorManager

        if cm is not None:
            return cm

    except:
        pass

    # --------------------------------------------------------
    # FamilyInstance
    # --------------------------------------------------------

    try:

        mep_model = elem.MEPModel

        if mep_model is not None:

            cm = mep_model.ConnectorManager

            if cm is not None:
                return cm

    except:
        pass

    return None


# ============================================================
# PHYSICAL CONNECTORS
# ============================================================

def is_physical_connector(connector):
    """
    AllRefs may contain logical references.

    We only want physical MEP connectivity.
    """

    if connector is None:
        return False

    try:
        connector_type = connector.ConnectorType
    except:
        return False

    allowed = []

    # ConnectorType.End
    try:
        allowed.append(
            DB.ConnectorType.End
        )
    except:
        pass

    # ConnectorType.Curve
    try:
        allowed.append(
            DB.ConnectorType.Curve
        )
    except:
        pass

    # ConnectorType.Physical
    try:
        allowed.append(
            DB.ConnectorType.Physical
        )
    except:
        pass

    for item in allowed:

        try:
            if connector_type == item:
                return True
        except:
            pass

    return False


# ============================================================
# ANGLE DIALOG
# ============================================================

class AngleDialog(Form):

    def __init__(self):

        self.Text = (
            "Rotate Around Connected Pipe"
        )

        self.Size = Size(
            360,
            175
        )

        self.FormBorderStyle = (
            FormBorderStyle.FixedDialog
        )

        self.StartPosition = (
            FormStartPosition.CenterScreen
        )

        self.MaximizeBox = False
        self.MinimizeBox = False

        self.angle = None

        # ----------------------------------------------------
        # LABEL
        # ----------------------------------------------------

        label = Label()

        label.Text = (
            "Nhập góc quay (độ)\n"
            "Ví dụ: 30, -45, 90"
        )

        label.Location = Point(
            20,
            15
        )

        label.Size = Size(
            310,
            40
        )

        self.Controls.Add(label)

        # ----------------------------------------------------
        # TEXTBOX
        # ----------------------------------------------------

        self.angle_box = TextBox()

        self.angle_box.Text = "90"

        self.angle_box.Location = Point(
            20,
            60
        )

        self.angle_box.Size = Size(
            305,
            25
        )

        self.Controls.Add(
            self.angle_box
        )

        # ----------------------------------------------------
        # OK
        # ----------------------------------------------------

        ok_button = Button()

        ok_button.Text = "OK"

        ok_button.Location = Point(
            165,
            98
        )

        ok_button.Size = Size(
            75,
            28
        )

        ok_button.Click += self.ok_click

        self.Controls.Add(
            ok_button
        )

        # ----------------------------------------------------
        # CANCEL
        # ----------------------------------------------------

        cancel_button = Button()

        cancel_button.Text = "Cancel"

        cancel_button.Location = Point(
            250,
            98
        )

        cancel_button.Size = Size(
            75,
            28
        )

        cancel_button.Click += (
            self.cancel_click
        )

        self.Controls.Add(
            cancel_button
        )

        self.AcceptButton = ok_button
        self.CancelButton = cancel_button


    def ok_click(
        self,
        sender,
        args
    ):

        text = self.angle_box.Text

        if text is None:
            return

        text = (
            text
            .strip()
            .replace(",", ".")
        )

        try:

            value = float(text)

        except:

            show_error(
                "Góc không hợp lệ.\n\n"
                "Ví dụ:\n"
                "30\n"
                "-45\n"
                "22.5"
            )

            return

        self.angle = value

        self.DialogResult = (
            DialogResult.OK
        )

        self.Close()


    def cancel_click(
        self,
        sender,
        args
    ):

        self.DialogResult = (
            DialogResult.Cancel
        )

        self.Close()


def ask_angle():

    dialog = AngleDialog()

    result = dialog.ShowDialog()

    # Cancel = silent
    if result != DialogResult.OK:
        return None

    return dialog.angle


# ============================================================
# SELECTION
# ============================================================

def get_preselected_elements():

    result = []
    seen = set()

    try:

        selected_ids = (
            uidoc.Selection
            .GetElementIds()
        )

    except:
        return result

    for eid in selected_ids:

        try:
            elem = doc.GetElement(eid)
        except:
            elem = None

        if elem is None:
            continue

        if not is_target_mep(elem):
            continue

        value = eid_value(elem.Id)

        if value is None:
            continue

        if value in seen:
            continue

        seen.add(value)
        result.append(elem)

    return result


def build_selected_id_set(elements):

    values = set()

    for elem in elements:

        try:
            value = eid_value(elem.Id)
        except:
            value = None

        if value is not None:
            values.add(value)

    return values


# ============================================================
# FIND EXTERNAL CONNECTED PIPE
# ============================================================

def find_external_connected_pipes(
    selected_elements
):
    """
    Find Pipes that:

        1. Are NOT in current selection
        2. Have a physical connector directly connected
           to any selected Pipe / Fitting / Accessory

    Returns:

        {
            pipe_id_value: {
                "pipe": Pipe,
                "connected_selected_ids": set(...)
            }
        }
    """

    selected_ids = build_selected_id_set(
        selected_elements
    )

    candidates = {}

    # ========================================================
    # CHECK EVERY SELECTED ELEMENT
    # ========================================================

    for selected_elem in selected_elements:

        selected_elem_id = eid_value(
            selected_elem.Id
        )

        cm = get_connector_manager(
            selected_elem
        )

        if cm is None:
            continue

        # ====================================================
        # EVERY CONNECTOR
        # ====================================================

        try:
            connectors = cm.Connectors
        except:
            continue

        for connector in connectors:

            if connector is None:
                continue

            # -----------------------------------------------
            # Physical connector only
            # -----------------------------------------------

            if not is_physical_connector(
                connector
            ):
                continue

            # -----------------------------------------------
            # Must actually be connected
            # -----------------------------------------------

            try:

                if not connector.IsConnected:
                    continue

            except:
                continue

            # -----------------------------------------------
            # AllRefs
            # -----------------------------------------------

            try:
                refs = connector.AllRefs
            except:
                continue

            if refs is None:
                continue

            # ===============================================
            # CONNECTED CONNECTORS
            # ===============================================

            for connected_connector in refs:

                if connected_connector is None:
                    continue

                if not is_physical_connector(
                    connected_connector
                ):
                    continue

                # -------------------------------------------
                # OWNER
                # -------------------------------------------

                try:
                    owner = connected_connector.Owner
                except:
                    owner = None

                if owner is None:
                    continue

                owner_id = eid_value(
                    owner.Id
                )

                if owner_id is None:
                    continue

                # Same element
                if (
                    selected_elem_id is not None
                    and owner_id == selected_elem_id
                ):
                    continue

                # -------------------------------------------
                # MUST BE OUTSIDE CURRENT SELECTION
                # -------------------------------------------

                if owner_id in selected_ids:
                    continue

                # -------------------------------------------
                # MUST BE PIPE
                # -------------------------------------------

                if not is_pipe(owner):
                    continue

                # -------------------------------------------
                # UNIQUE PIPE
                # -------------------------------------------

                if owner_id not in candidates:

                    candidates[owner_id] = {
                        "pipe": owner,
                        "connected_selected_ids": set()
                    }

                if selected_elem_id is not None:

                    candidates[
                        owner_id
                    ][
                        "connected_selected_ids"
                    ].add(
                        selected_elem_id
                    )

    return candidates


# ============================================================
# RESOLVE SINGLE AXIS PIPE
# ============================================================

def get_automatic_axis_pipe(
    selected_elements
):
    """
    Exactly 1 external connected Pipe:
        -> return it

    0:
        -> error

    >1:
        -> error to avoid rotating around wrong Pipe
    """

    candidates = (
        find_external_connected_pipes(
            selected_elements
        )
    )

    count = len(candidates)

    # ========================================================
    # NONE
    # ========================================================

    if count == 0:

        show_error(
            "Không tìm thấy Pipe làm trục quay.\n\n"
            "Yêu cầu:\n"
            "phải có ít nhất 1 Pipe KHÔNG nằm trong Selection "
            "nhưng đang nối trực tiếp bằng connector vật lý "
            "với cụm được chọn."
        )

        return None

    # ========================================================
    # MULTIPLE
    # ========================================================

    if count > 1:

        pipe_ids = sorted(
            candidates.keys()
        )

        lines = []

        for pipe_id in pipe_ids[:12]:

            info = candidates[
                pipe_id
            ]

            selected_connection_count = len(
                info[
                    "connected_selected_ids"
                ]
            )

            lines.append(
                "Pipe {}  (nối với {} phần tử được chọn)".format(
                    pipe_id,
                    selected_connection_count
                )
            )

        message = (
            "Tìm thấy {} Pipe ngoài Selection "
            "đang nối vào cụm được chọn.\n\n"
            "Tool không tự chọn một Pipe bất kỳ "
            "để tránh quay sai trục.\n\n"
            "Pipe tìm thấy:\n{}"
        ).format(
            count,
            "\n".join(lines)
        )

        if count > 12:
            message += "\n..."

        message += (
            "\n\nHãy mở rộng/thu hẹp Selection "
            "để chỉ còn đúng 1 Pipe ngoài Selection "
            "nối vào cụm."
        )

        show_error(message)

        return None

    # ========================================================
    # EXACTLY ONE
    # ========================================================

    for info in candidates.values():
        return info["pipe"]

    return None


# ============================================================
# CANONICAL AXIS DIRECTION
# ============================================================

def canonicalize_direction(direction):
    """
    Normalize +/- direction so identical geometrical Pipes
    produce consistent positive/negative rotation behavior.

    Priority:

    Sloped / vertical:
        prefer +Z

    Horizontal:
        prefer +X

    If X = 0:
        prefer +Y
    """

    d = direction

    # --------------------------------------------------------
    # VERTICAL / SLOPED
    # --------------------------------------------------------

    if abs(d.Z) > VECTOR_TOL:

        if d.Z < 0:
            d = d.Negate()

        return d

    # --------------------------------------------------------
    # HORIZONTAL
    # --------------------------------------------------------

    if abs(d.X) > VECTOR_TOL:

        if d.X < 0:
            d = d.Negate()

        return d

    # --------------------------------------------------------
    # Y AXIS
    # --------------------------------------------------------

    if d.Y < 0:

        d = d.Negate()

    return d


# ============================================================
# CREATE AXIS FROM PIPE
# ============================================================

def get_pipe_rotation_axis(pipe):
    """
    Use ACTUAL Pipe centerline.

    Supports:
    - Vertical
    - Horizontal
    - Sloped

    Pipe must be straight.
    """

    if pipe is None:

        return (
            None,
            "Pipe làm trục không hợp lệ."
        )

    if not is_pipe(pipe):

        return (
            None,
            "Đối tượng làm trục không phải Pipe."
        )

    # ========================================================
    # LOCATION CURVE
    # ========================================================

    try:
        location = pipe.Location
    except Exception as ex:

        return (
            None,
            "Không đọc được Location của Pipe.\n\n"
            "{}".format(ex)
        )

    if not isinstance(
        location,
        DB.LocationCurve
    ):

        return (
            None,
            "Pipe làm trục không có LocationCurve."
        )

    # ========================================================
    # CURVE
    # ========================================================

    try:
        curve = location.Curve
    except Exception as ex:

        return (
            None,
            "Không đọc được centerline Pipe.\n\n"
            "{}".format(ex)
        )

    if curve is None:

        return (
            None,
            "Pipe không có centerline hợp lệ."
        )

    # Axis must be straight
    if not isinstance(
        curve,
        DB.Line
    ):

        return (
            None,
            "Pipe tự động tìm thấy không phải đoạn thẳng.\n\n"
            "Tool chỉ dùng Pipe có centerline dạng Line."
        )

    # ========================================================
    # END POINTS
    # ========================================================

    try:

        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)

    except Exception as ex:

        return (
            None,
            "Không đọc được hai đầu Pipe.\n\n"
            "{}".format(ex)
        )

    vector = p1 - p0

    try:
        length = vector.GetLength()
    except:
        length = 0.0

    if length <= VECTOR_TOL:

        return (
            None,
            "Pipe làm trục có chiều dài không hợp lệ."
        )

    # ========================================================
    # DIRECTION
    # ========================================================

    try:

        direction = (
            vector.Normalize()
        )

    except:

        return (
            None,
            "Không xác định được hướng Pipe làm trục."
        )

    direction = (
        canonicalize_direction(
            direction
        )
    )

    # ========================================================
    # MID POINT
    # ========================================================

    midpoint = DB.XYZ(
        (p0.X + p1.X) * 0.5,
        (p0.Y + p1.Y) * 0.5,
        (p0.Z + p1.Z) * 0.5
    )

    # ========================================================
    # UNBOUNDED CENTERLINE
    # ========================================================

    try:

        axis = DB.Line.CreateUnbound(
            midpoint,
            direction
        )

    except Exception as ex:

        return (
            None,
            "Không tạo được trục quay.\n\n"
            "{}".format(ex)
        )

    return (
        axis,
        None
    )


# ============================================================
# PINNED CHECK
# ============================================================

def get_pinned_elements(elements):

    result = []

    for elem in elements:

        try:

            if elem.Pinned:
                result.append(elem)

        except:
            pass

    return result


# ============================================================
# ROTATE
# ============================================================

def rotate_elements(
    elements,
    axis,
    angle_deg
):

    if not elements:
        return True

    ids = List[DB.ElementId]()

    for elem in elements:

        try:
            ids.Add(elem.Id)
        except:
            pass

    if ids.Count == 0:
        return True

    # degrees -> radians
    angle_rad = math.radians(
        angle_deg
    )

    transaction = DB.Transaction(
        doc,
        "Rotate MEP Around Connected Pipe"
    )

    started = False

    try:

        transaction.Start()
        started = True

        DB.ElementTransformUtils.RotateElements(
            doc,
            ids,
            axis,
            angle_rad
        )

        transaction.Commit()

        return True

    except Exception as ex:

        if started:

            try:
                transaction.RollBack()
            except:
                pass

        show_error(
            "Không thể quay cụm được chọn.\n\n"
            "Có thể do:\n"
            "- Constraint\n"
            "- Element đang Pin\n"
            "- Cụm còn nối với các phần tử ngoài Selection\n"
            "- Group / Host constraint\n"
            "- Revit không cho phép transform\n\n"
            "Chi tiết:\n{}".format(ex)
        )

        return False


# ============================================================
# KEEP RESULT SELECTED
# ============================================================

def set_selection(elements):

    ids = List[DB.ElementId]()

    for elem in elements:

        try:
            ids.Add(elem.Id)
        except:
            pass

    try:

        uidoc.Selection.SetElementIds(
            ids
        )

    except:
        # Rotation already succeeded.
        # No need to annoy the user.
        pass


# ============================================================
# MAIN
# ============================================================

def main():

    # ========================================================
    # 1. READ CURRENT SELECTION
    # ========================================================

    selected = (
        get_preselected_elements()
    )

    if not selected:

        show_error(
            "Không có Pipe / Pipe Fitting / "
            "Pipe Accessory hợp lệ trong Selection."
        )

        return

    # ========================================================
    # 2. AUTO FIND EXTERNAL CONNECTED PIPE
    # ========================================================

    axis_pipe = (
        get_automatic_axis_pipe(
            selected
        )
    )

    if axis_pipe is None:
        return

    # ========================================================
    # 3. CREATE AXIS FROM THAT PIPE
    # ========================================================

    axis, axis_error = (
        get_pipe_rotation_axis(
            axis_pipe
        )
    )

    if axis is None:

        if axis_error:
            show_error(
                axis_error
            )

        return

    # ========================================================
    # 4. PINNED CHECK
    # ========================================================

    pinned = (
        get_pinned_elements(
            selected
        )
    )

    if pinned:

        ids_text = []

        for elem in pinned[:10]:

            value = eid_value(
                elem.Id
            )

            if value is not None:

                ids_text.append(
                    str(value)
                )

        message = (
            "Có {} phần tử đang Pin.\n\n"
            "Hãy Unpin trước khi quay."
        ).format(
            len(pinned)
        )

        if ids_text:

            message += (
                "\n\nElementId:\n"
                + "\n".join(ids_text)
            )

        if len(pinned) > 10:
            message += "\n..."

        show_error(message)

        return

    # ========================================================
    # 5. ASK ANGLE
    # ========================================================

    angle_deg = ask_angle()

    # Cancel
    if angle_deg is None:
        return

    # 0 degrees
    if abs(angle_deg) < VECTOR_TOL:
        return

    # ========================================================
    # 6. ROTATE
    # ========================================================

    success = rotate_elements(
        selected,
        axis,
        angle_deg
    )

    if not success:
        return

    # ========================================================
    # 7. KEEP ORIGINAL CLUSTER SELECTED
    # ========================================================

    set_selection(
        selected
    )

    # ========================================================
    # SUCCESS = SILENT
    # ========================================================


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()