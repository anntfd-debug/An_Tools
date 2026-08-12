# -*- coding: utf-8 -*-
"""pyRevit - Scan a configurable vertical clearance zone below one Family.

Normal workflow:
1. Pick one FamilyInstance.
2. The tool finds every visible instance of the same Family in the active view
   (all Family Types).
3. From the bottom-centre of each instance, find the nearest Floor below.
4. Build a saved, user-configurable diameter cylinder down to that Floor.
5. Report instances whose cylinder intersects the configured scan categories.

The scan is read-only and does not need a Revit transaction.
"""

import math

from Autodesk.Revit.DB import (
    Arc,
    BuiltInCategory,
    BuiltInParameter,
    CurveLoop,
    ElementCategoryFilter,
    ElementId,
    ElementIntersectsSolidFilter,
    ElementFilter,
    FamilyInstance,
    FilteredElementCollector,
    GeometryCreationUtilities,
    LogicalOrFilter,
    Options,
    ReferenceIntersector,
    RevitLinkInstance,
    SolidUtils,
    FindReferenceTarget,
    View3D,
    ViewDetailLevel,
    XYZ,
)
from System.Collections.Generic import List
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from pyrevit import EXEC_PARAMS, forms, revit, script


doc = revit.doc
uidoc = revit.uidoc
active_view = doc.ActiveView
output = script.get_output()

DEFAULT_DIAMETER_MM = 300.0
IGNORED_WORKSET_NAME = "98_Clearance Zones"
TOLERANCE_FT = 1.0 / 304.8       # 1 mm
MAX_RAY_FT = 100000.0 / 304.8    # 100 m
DOWN = XYZ(0.0, 0.0, -1.0)

TARGET_BICS = [
    BuiltInCategory.OST_PipeCurves,
    BuiltInCategory.OST_DuctCurves,
    BuiltInCategory.OST_PipeFitting,
    BuiltInCategory.OST_DuctFitting,
    BuiltInCategory.OST_PipeAccessory,
    BuiltInCategory.OST_DuctAccessory,
    BuiltInCategory.OST_MechanicalEquipment,
    BuiltInCategory.OST_StructuralFraming,
    BuiltInCategory.OST_GenericModel,
]


class FamilyInstanceFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, FamilyInstance)

    def AllowReference(self, reference, point):
        return False


def get_scan_diameter():
    """Read saved diameter; Shift+Click lets the user replace it."""
    config = script.get_config()
    try:
        saved_value = float(config.scan_diameter_mm)
        if saved_value <= 0.0:
            saved_value = DEFAULT_DIAMETER_MM
    except Exception:
        saved_value = DEFAULT_DIAMETER_MM

    if EXEC_PARAMS.config_mode:
        entered = forms.ask_for_string(
            default=("{0:g}".format(saved_value)),
            prompt="Enter scan diameter D (mm):",
            title="Floor Clearance Scan - Settings"
        )
        if entered is None:
            script.exit()
        try:
            # Accept either decimal dot or decimal comma.
            new_value = float(entered.strip().replace(",", "."))
        except Exception:
            forms.alert("Diameter must be a valid number in mm.", exitscript=True)
        if new_value <= 0.0:
            forms.alert("Diameter must be greater than 0 mm.", exitscript=True)
        saved_value = new_value
        config.scan_diameter_mm = saved_value
        script.save_config()

    return saved_value


def get_family_id(instance):
    symbol = instance.Symbol
    if symbol is None or symbol.Family is None:
        return ElementId.InvalidElementId
    return symbol.Family.Id


def iter_solids(geometry_element):
    """Yield positive-volume solids, including nested GeometryInstance data."""
    if geometry_element is None:
        return
    for geo_obj in geometry_element:
        if hasattr(geo_obj, "Volume") and geo_obj.Volume > 1e-9:
            yield geo_obj
        elif hasattr(geo_obj, "GetInstanceGeometry"):
            try:
                nested = geo_obj.GetInstanceGeometry()
                for solid in iter_solids(nested):
                    yield solid
            except Exception:
                pass


def get_real_geometry_bounds(element):
    """Bounds from model solids only; ignores calculation points and controls."""
    opts = Options()
    opts.ComputeReferences = False
    opts.IncludeNonVisibleObjects = False
    opts.DetailLevel = ViewDetailLevel.Fine

    min_x = min_y = min_z = None
    max_x = max_y = max_z = None
    try:
        geometry = element.get_Geometry(opts)
    except Exception:
        geometry = None

    for solid in iter_solids(geometry):
        bbox = solid.GetBoundingBox()
        if bbox is None:
            continue
        # Solid bounding boxes may have their own transform.
        transform = bbox.Transform
        for x in (bbox.Min.X, bbox.Max.X):
            for y in (bbox.Min.Y, bbox.Max.Y):
                for z in (bbox.Min.Z, bbox.Max.Z):
                    p = transform.OfPoint(XYZ(x, y, z))
                    min_x = p.X if min_x is None else min(min_x, p.X)
                    min_y = p.Y if min_y is None else min(min_y, p.Y)
                    min_z = p.Z if min_z is None else min(min_z, p.Z)
                    max_x = p.X if max_x is None else max(max_x, p.X)
                    max_y = p.Y if max_y is None else max(max_y, p.Y)
                    max_z = p.Z if max_z is None else max(max_z, p.Z)

    if min_x is None:
        # Fallback only when the Family has no usable model solid.
        bbox = element.get_BoundingBox(None)
        if bbox is None:
            return None
        min_x, min_y, min_z = bbox.Min.X, bbox.Min.Y, bbox.Min.Z
        max_x, max_y, max_z = bbox.Max.X, bbox.Max.Y, bbox.Max.Z

    return (XYZ(min_x, min_y, min_z), XYZ(max_x, max_y, max_z))


def find_usable_3d_view():
    views = FilteredElementCollector(doc).OfClass(View3D)
    for view in views:
        if not view.IsTemplate:
            return view
    return None


def make_floor_intersector(view3d):
    floor_filter = ElementCategoryFilter(BuiltInCategory.OST_Floors)
    finder = ReferenceIntersector(
        floor_filter, FindReferenceTarget.Element, view3d
    )
    # This also allows a linked Floor to be used as the bottom reference.
    try:
        finder.FindReferencesInRevitLinks = True
    except Exception:
        pass
    return finder


def nearest_floor_distance(finder, origin):
    # Start 1 mm below the Family bottom so a hosted face at the same elevation
    # is not returned as a zero-height scan.
    ray_origin = origin - XYZ.BasisZ.Multiply(TOLERANCE_FT)
    hits = finder.Find(ray_origin, DOWN)
    if hits is None:
        return None
    best = None
    for hit in hits:
        distance = hit.Proximity + TOLERANCE_FT
        if distance <= TOLERANCE_FT or distance > MAX_RAY_FT:
            continue
        if best is None or distance < best:
            best = distance
    return best


def make_scan_cylinder(origin, height):
    # A tiny gap at each end avoids counting tangential contact at the Family
    # bottom and at the Floor surface as an MEP collision.
    clear_height = height - (2.0 * TOLERANCE_FT)
    if clear_height <= TOLERANCE_FT:
        return None
    centre = origin - XYZ.BasisZ.Multiply(TOLERANCE_FT)
    loop = CurveLoop()
    loop.Append(Arc.Create(centre, RADIUS_FT, 0.0, math.pi, XYZ.BasisX, XYZ.BasisY))
    loop.Append(Arc.Create(centre, RADIUS_FT, math.pi, 2.0 * math.pi, XYZ.BasisX, XYZ.BasisY))
    return GeometryCreationUtilities.CreateExtrusionGeometry(
        [loop], DOWN, clear_height
    )


def get_obstacle_filter():
    filters = [ElementCategoryFilter(bic) for bic in TARGET_BICS]
    return LogicalOrFilter(List[ElementFilter](filters))


def normalize_workset_name(name):
    """Normalize Workset text for a safe case-insensitive comparison."""
    if not name:
        return ""
    try:
        return " ".join(name.strip().lower().split())
    except Exception:
        return ""


def get_element_workset_name(element, source_doc):
    """Return an element Workset name, with a parameter fallback."""
    if element is None or source_doc is None:
        return None

    # Primary method: Element.WorksetId -> WorksetTable.
    try:
        workset_id = element.WorksetId
        workset = source_doc.GetWorksetTable().GetWorkset(workset_id)
        if workset is not None:
            return workset.Name
    except Exception:
        pass

    # Fallback for element types/API cases where WorksetId is not exposed
    # reliably. ELEM_PARTITION_PARAM stores the Workset id.
    try:
        param = element.get_Parameter(BuiltInParameter.ELEM_PARTITION_PARAM)
        if param is not None:
            workset_id = ElementId(param.AsInteger())
            workset = source_doc.GetWorksetTable().GetWorkset(workset_id)
            if workset is not None:
                return workset.Name
    except Exception:
        pass

    return None


def is_on_ignored_workset(element, source_doc):
    """True when the collision element is on 98_Clearance Zones."""
    element_workset = get_element_workset_name(element, source_doc)
    return (normalize_workset_name(element_workset) ==
            normalize_workset_name(IGNORED_WORKSET_NAME))


def get_visible_loaded_links():
    """Return loaded Revit links visible in the active host view."""
    links = []
    collector = FilteredElementCollector(doc, active_view.Id).OfClass(
        RevitLinkInstance
    )
    for link_instance in collector:
        try:
            if link_instance.GetLinkDocument() is not None:
                links.append(link_instance)
        except Exception:
            pass
    return links


def find_collisions(scan_solid, source_id, category_filter, visible_links):
    """Find category collisions in host and visible loaded link instances."""
    solid_filter = ElementIntersectsSolidFilter(scan_solid)
    collector = FilteredElementCollector(doc).WhereElementIsNotElementType()
    collector.WherePasses(category_filter)
    collector.WherePasses(solid_filter)
    result = []
    for element in collector:
        if (element.Id != source_id and
                not is_on_ignored_workset(element, doc)):
            result.append(("host", element.Id, None))

    for link_instance in visible_links:
        link_doc = link_instance.GetLinkDocument()
        if link_doc is None:
            continue
        try:
            # ElementIntersectsSolidFilter in a link document requires the
            # host scan solid to be transformed into that link's coordinates.
            inverse_transform = link_instance.GetTotalTransform().Inverse
            linked_scan_solid = SolidUtils.CreateTransformed(
                scan_solid, inverse_transform
            )
            linked_solid_filter = ElementIntersectsSolidFilter(
                linked_scan_solid
            )
            linked_collector = FilteredElementCollector(
                link_doc
            ).WhereElementIsNotElementType()
            linked_collector.WherePasses(category_filter)
            linked_collector.WherePasses(linked_solid_filter)
            for linked_element in linked_collector:
                if not is_on_ignored_workset(linked_element, link_doc):
                    result.append(
                        ("link", linked_element.Id, link_instance.Id)
                    )
        except Exception:
            # One unloaded, invalid, or non-invertible link must not stop the
            # remaining Family instances from being checked.
            continue
    return result


def format_collision(collision):
    source, element_id, link_instance_id = collision
    if source == "host":
        return output.linkify(element_id)
    return "Link {0} -> Linked Element ID {1}".format(
        output.linkify(link_instance_id), element_id.IntegerValue
    )


DIAMETER_MM = get_scan_diameter()
RADIUS_FT = (DIAMETER_MM * 0.5) / 304.8


try:
    picked_ref = uidoc.Selection.PickObject(
        ObjectType.Element,
        FamilyInstanceFilter(),
        "Pick one Family. All Types of this Family visible in active view will be scanned."
    )
except Exception:
    script.exit()

picked = doc.GetElement(picked_ref.ElementId)
family_id = get_family_id(picked)
if family_id == ElementId.InvalidElementId:
    forms.alert("The selected element does not have a valid Family.", exitscript=True)

family_name = picked.Symbol.Family.Name
instances = []
for item in FilteredElementCollector(doc, active_view.Id).OfClass(FamilyInstance):
    if get_family_id(item) == family_id:
        instances.append(item)

if not instances:
    forms.alert("No instance of the selected Family is visible in the active view.", exitscript=True)

view3d = find_usable_3d_view()
if view3d is None:
    forms.alert(
        "A non-template 3D view is required to find the nearest Floor.\n"
        "Please create one 3D view, then run the tool again.",
        exitscript=True
    )

floor_finder = make_floor_intersector(view3d)
obstacle_filter = get_obstacle_filter()
visible_links = get_visible_loaded_links()
failures = []
no_floor = []
no_geometry = []

with forms.ProgressBar(
    title="Scanning {value} of {max_value}",
    cancellable=True,
    step=1
) as progress:
    total = len(instances)
    for index, instance in enumerate(instances):
        if progress.cancelled:
            break
        progress.update_progress(index + 1, total)

        bounds = get_real_geometry_bounds(instance)
        if bounds is None:
            no_geometry.append(instance.Id)
            continue
        pmin, pmax = bounds
        origin = XYZ(
            (pmin.X + pmax.X) * 0.5,
            (pmin.Y + pmax.Y) * 0.5,
            pmin.Z
        )
        floor_distance = nearest_floor_distance(floor_finder, origin)
        if floor_distance is None:
            no_floor.append(instance.Id)
            continue
        cylinder = make_scan_cylinder(origin, floor_distance)
        if cylinder is None:
            continue
        collisions = find_collisions(
            cylinder, instance.Id, obstacle_filter, visible_links
        )
        if collisions:
            failures.append((instance.Id, collisions))

output.print_md("# Floor clearance scan")
output.print_md("**Scan diameter D:** {0:g} mm  ".format(DIAMETER_MM))
output.print_md("**Ignored Workset:** {0}  ".format(IGNORED_WORKSET_NAME))
output.print_md("**Family:** {0}  ".format(family_name))
output.print_md("**Instances visible in active view:** {0}  ".format(len(instances)))
output.print_md("**Loaded links visible in active view:** {0}  ".format(len(visible_links)))
output.print_md("**Instances with collision:** {0}".format(len(failures)))

if failures:
    output.print_md("## Colliding Family instances")
    for family_instance_id, obstacle_ids in failures:
        family_link = output.linkify(family_instance_id)
        obstacle_links = ", ".join([format_collision(x) for x in obstacle_ids])
        output.print_md(
            "- Family {0} | Obstacle ID: {1}".format(family_link, obstacle_links)
        )
    output.print_md("**All colliding Family IDs:** {0}".format(
        ", ".join([str(x.IntegerValue) for x, unused in failures])
    ))
else:
    output.print_md("No collision was found below this Family.")

if no_floor:
    output.print_md("## Skipped - no Floor below")
    output.print_md(", ".join([output.linkify(x) for x in no_floor]))

if no_geometry:
    output.print_md("## Skipped - no usable Family solid/bounding box")
    output.print_md(", ".join([output.linkify(x) for x in no_geometry]))
