# -*- coding: utf-8 -*-
__title__ = "Isolate Vert Pipe"
__doc__ = "Cách ly tất cả ống đứng trong active view"
from pyrevit import revit, DB
from System.Collections.Generic import List

doc = revit.doc
active_view = revit.active_view

# Use OST_PipeCurves for Pipe elements
try:
    pipe_category = DB.BuiltInCategory.OST_PipeCurves
except AttributeError:
    pipe_category = DB.BuiltInCategory.OST_Pipe

# Collect pipes in active view
pipes = DB.FilteredElementCollector(doc, active_view.Id) \
          .OfCategory(pipe_category) \
          .WhereElementIsNotElementType() \
          .ToElements()

vertical_pipes = []
tolerance = 0.001 

# Start Transaction
with revit.Transaction("Isolate Vertical Pipes"):
    for pipe in pipes:
        loc = pipe.Location
        if hasattr(loc, 'Curve'):
            curve = loc.Curve
            if isinstance(curve, DB.Line):
                direction = curve.Direction.Normalize()
                # Check verticality
                if abs(direction.X) < tolerance and abs(direction.Y) < tolerance:
                    vertical_pipes.append(pipe.Id)

    # REPLACED HIDING BLOCK WITH ISOLATE BLOCK
    if vertical_pipes:
        try:
            ids_to_isolate = List[DB.ElementId](vertical_pipes)
            # This triggers the "Cyan Border" temporary mode in Revit
            active_view.IsolateElementsTemporary(ids_to_isolate)
            print("Success: Isolated {} vertical pipes.".format(len(vertical_pipes)))
            print("Note: View is now in Temporary Hide/Isolate mode (Cyan border).")
        except Exception as e:
            print("Could not isolate elements: {}".format(e))
    else:
        print("No vertical pipes found to isolate.")