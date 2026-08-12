# -*- coding: utf-8 -*-

from pyrevit import revit
from pyrevit import DB
from pyrevit import forms
from pyrevit import script

try:
    from pyrevit import EXEC_PARAMS
except Exception:
    EXEC_PARAMS = None

from Autodesk.Revit.DB.Analysis import PathOfTravel
from Autodesk.Revit.UI.Selection import ISelectionFilter
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

from System.Collections.Generic import List

import traceback


doc = revit.doc
uidoc = revit.uidoc
active_view = revit.active_view


# ============================================================
# CONFIGURATION
# ============================================================

CONFIG_SECTION = "AN_PATH_OF_TRAVEL_TEMPLATE"
config = script.get_config(CONFIG_SECTION)

ONE_MM = 1.0 / 304.8
POINT_TOLERANCE = ONE_MM


# ============================================================
# SELECTION FILTER
# ============================================================

class PathOfTravelFilter(ISelectionFilter):

    def AllowElement(self, element):
        return isinstance(element, PathOfTravel)

    def AllowReference(self, reference, point):
        return False


# ============================================================
# GENERAL FUNCTIONS
# ============================================================

def is_config_mode():
    """
    Return True when command is executed with Shift + Click.
    """

    try:
        if EXEC_PARAMS is not None:
            return bool(EXEC_PARAMS.config_mode)
    except Exception:
        pass

    try:
        return bool(__shiftclick__)
    except Exception:
        return False


def get_document_key():
    """
    Return a stable key for the current Revit document.
    """

    try:
        return doc.ProjectInformation.UniqueId
    except Exception:
        return doc.Title


def get_count(collection):
    try:
        return collection.Count
    except Exception:
        return len(collection)


# ============================================================
# TEMPLATE FUNCTIONS
# ============================================================

def validate_template(path):
    """
    Check whether the saved template can be used.
    """

    if path is None:
        return False

    if not isinstance(path, PathOfTravel):
        return False

    try:
        if not path.IsValidObject:
            return False
    except Exception:
        return False

    if path.OwnerViewId != active_view.Id:
        return False

    if path.GroupId != DB.ElementId.InvalidElementId:
        return False

    return True


def get_saved_template():
    """
    Load the saved Path of Travel from pyRevit configuration.
    """

    try:
        saved_doc_key = str(config.document_key)
        saved_view_uid = str(config.view_unique_id)
        saved_path_uid = str(config.template_unique_id)
    except Exception:
        return None

    if saved_doc_key != get_document_key():
        return None

    if saved_view_uid != active_view.UniqueId:
        return None

    if not saved_path_uid:
        return None

    try:
        path = doc.GetElement(saved_path_uid)
    except Exception:
        return None

    if validate_template(path):
        return path

    return None


def pick_template():
    """
    Ask the user to select a Path of Travel in the active view.
    """

    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            PathOfTravelFilter(),
            "Select a Path of Travel to use as template"
        )

    except OperationCanceledException:
        return None

    path = doc.GetElement(reference.ElementId)

    if path is None:
        return None

    if path.OwnerViewId != active_view.Id:
        forms.alert(
            "The template Path of Travel must belong to the active view.",
            title="Invalid Template",
            warn_icon=True
        )
        return None

    if path.GroupId != DB.ElementId.InvalidElementId:
        forms.alert(
            "The template Path of Travel cannot be inside a group.",
            title="Invalid Template",
            warn_icon=True
        )
        return None

    return path


def save_template(path):
    """
    Save template information into pyRevit configuration.
    """

    config.document_key = get_document_key()
    config.view_unique_id = active_view.UniqueId
    config.template_unique_id = path.UniqueId

    script.save_config()


# ============================================================
# POINT PICKING
# ============================================================

def pick_path_points(path_z):
    """
    Pick points continuously until ESC is pressed.

    First point:
        Path start.

    Last point:
        Path end.

    Middle points:
        Path waypoints.
    """

    points = []

    while True:
        point_number = len(points) + 1

        prompt = (
            "Pick path point {0}. "
            "Press ESC to finish"
        ).format(point_number)

        try:
            picked_point = uidoc.Selection.PickPoint(prompt)

        except OperationCanceledException:
            break

        point = DB.XYZ(
            picked_point.X,
            picked_point.Y,
            path_z
        )

        # Ignore duplicated consecutive points.
        if points:
            if point.DistanceTo(points[-1]) <= POINT_TOLERANCE:
                continue

        points.append(point)

    return points


# ============================================================
# PATH FUNCTIONS
# ============================================================

def clear_waypoints(path):
    """
    Remove all waypoints copied from the template.
    """

    waypoints = path.GetWaypoints()
    waypoint_count = get_count(waypoints)

    for index in range(
        waypoint_count - 1,
        -1,
        -1
    ):
        path.RemoveWaypoint(index)


def get_copied_path(template):
    """
    Copy the selected template Path of Travel.

    CopyElement can return more than one ElementId, so search
    the result for the copied PathOfTravel element.
    """

    copied_ids = DB.ElementTransformUtils.CopyElement(
        doc,
        template.Id,
        DB.XYZ(0.0, 0.0, 0.0)
    )

    for copied_id in copied_ids:
        copied_element = doc.GetElement(copied_id)

        if isinstance(copied_element, PathOfTravel):
            return copied_element

    return None


def copy_writable_parameters(source, target):
    """
    Copy writable instance parameters.

    This function is used only when Revit cannot directly copy
    the template Path of Travel and the tool must create a new one.
    """

    for source_parameter in source.Parameters:
        try:
            if source_parameter.IsReadOnly:
                continue

            target_parameter = target.get_Parameter(
                source_parameter.Definition
            )

            if target_parameter is None:
                continue

            if target_parameter.IsReadOnly:
                continue

            if (
                target_parameter.StorageType
                != source_parameter.StorageType
            ):
                continue

            storage_type = source_parameter.StorageType

            if storage_type == DB.StorageType.String:
                value = source_parameter.AsString()

                if value is None:
                    value = ""

                target_parameter.Set(value)

            elif storage_type == DB.StorageType.Double:
                target_parameter.Set(
                    source_parameter.AsDouble()
                )

            elif storage_type == DB.StorageType.Integer:
                target_parameter.Set(
                    source_parameter.AsInteger()
                )

            elif storage_type == DB.StorageType.ElementId:
                target_parameter.Set(
                    source_parameter.AsElementId()
                )

        except Exception:
            continue


def force_graphics_refresh(path):
    """
    Move the path 1 mm and move it back.

    This forces Revit to rebuild the Path of Travel graphics.
    """

    move_vector = DB.XYZ(
        ONE_MM,
        0.0,
        0.0
    )

    return_vector = DB.XYZ(
        -ONE_MM,
        0.0,
        0.0
    )

    DB.ElementTransformUtils.MoveElement(
        doc,
        path.Id,
        move_vector
    )

    doc.Regenerate()

    DB.ElementTransformUtils.MoveElement(
        doc,
        path.Id,
        return_vector
    )

    path.Update()
    doc.Regenerate()


def create_path_from_template(template, points):
    """
    Create a new Path of Travel from picked points.

    Priority:
    1. Copy the template Path of Travel.
    2. If copying fails, create a new Path of Travel.
    """

    transaction = DB.Transaction(
        doc,
        "Create Path of Travel from picked points"
    )

    new_path = None

    try:
        transaction.Start()

        # ----------------------------------------------------
        # Try to copy the template.
        # ----------------------------------------------------

        subtransaction = DB.SubTransaction(doc)

        try:
            subtransaction.Start()

            new_path = get_copied_path(template)

            if new_path is None:
                raise Exception(
                    "Template copy did not create a path."
                )

            subtransaction.Commit()

        except Exception:
            if (
                subtransaction.GetStatus()
                == DB.TransactionStatus.Started
            ):
                subtransaction.RollBack()

            new_path = None

        # ----------------------------------------------------
        # Fallback: create a new path.
        # ----------------------------------------------------

        if new_path is None:
            new_path = PathOfTravel.Create(
                active_view,
                points[0],
                points[-1]
            )

            if new_path is None:
                raise Exception(
                    "Revit could not create the new path."
                )

            try:
                new_path.LineStyle = template.LineStyle
            except Exception:
                pass

            copy_writable_parameters(
                template,
                new_path
            )

        # ----------------------------------------------------
        # Prepare the copied path.
        # ----------------------------------------------------

        if new_path.Pinned:
            new_path.Pinned = False

        clear_waypoints(new_path)

        # ----------------------------------------------------
        # Set path direction.
        # ----------------------------------------------------

        new_path.PathStart = points[0]
        new_path.PathEnd = points[-1]

        new_path.Update()
        doc.Regenerate()

        # ----------------------------------------------------
        # Insert middle points as waypoints.
        # ----------------------------------------------------

        waypoint_index = 0

        for point in points[1:-1]:
            new_path.InsertWaypoint(
                point,
                waypoint_index
            )

            waypoint_index += 1

            doc.Regenerate()

        new_path.Update()
        doc.Regenerate()

        # ----------------------------------------------------
        # Fix Revit graphics.
        # ----------------------------------------------------

        force_graphics_refresh(new_path)

        transaction.Commit()

        return new_path

    except Exception:
        if (
            transaction.GetStatus()
            == DB.TransactionStatus.Started
        ):
            transaction.RollBack()

        raise


def select_new_path(path):
    """
    Select the new Path of Travel after creation.
    """

    try:
        selected_ids = List[DB.ElementId]()
        selected_ids.Add(path.Id)

        uidoc.Selection.SetElementIds(
            selected_ids
        )

    except Exception:
        pass


# ============================================================
# MAIN
# ============================================================

def main():
    template = get_saved_template()

    # --------------------------------------------------------
    # First run or Shift + Click:
    # select and save a new template.
    # --------------------------------------------------------

    if template is None or is_config_mode():
        template = pick_template()

        if template is None:
            return

        save_template(template)

        forms.alert(
            "Template Path of Travel saved.\n\n"
            "Run the command again and pick points from start to end.\n\n"
            "Use Shift + Click to select another template.",
            title="Template Saved"
        )

        return

    # --------------------------------------------------------
    # Normal run:
    # pick points and create a new path.
    # --------------------------------------------------------

    points = pick_path_points(
        template.PathStart.Z
    )

    # ESC before picking any point.
    if len(points) == 0:
        return

    # Path requires at least a start and end point.
    if len(points) < 2:
        forms.alert(
            "At least two points are required.\n\n"
            "Point 1 is the path start.\n"
            "The last point is the path end.",
            title="Not Enough Points",
            warn_icon=True
        )

        return

    new_path = create_path_from_template(
        template,
        points
    )

    select_new_path(new_path)

    uidoc.RefreshActiveView()


# ============================================================
# RUN
# ============================================================

try:
    main()

except Exception as error:
    forms.alert(
        "Failed to create Path of Travel.\n\n"
        "Error: {0}\n\n"
        "{1}".format(
            str(error),
            traceback.format_exc()
        ),
        title="Error",
        warn_icon=True
    )