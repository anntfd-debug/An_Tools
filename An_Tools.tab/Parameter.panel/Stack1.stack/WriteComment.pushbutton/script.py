# -*- coding: utf-8 -*-
__title__ = "Write Comment"
__doc__ = "Lấy comment của ống đứng thấp nhất để điền vào tât cả các ống phía trên đến khi hết trong active view (write comment for stack)."
from pyrevit import revit, DB
from collections import defaultdict

doc = revit.doc
active_view = revit.active_view

# 1. Collect elements visible in the active view
# You can filter by specific categories like OST_Pipe if needed
elements = DB.FilteredElementCollector(doc, active_view.Id) \
             .WhereElementIsNotElementType() \
             .ToElements()

# Dictionary to group elements by their horizontal (X, Y) position
# Key: (rounded X, rounded Y) | Value: List of (Z_coordinate, element)
groups = defaultdict(list)

# Precision factor to group elements that are slightly offset (in feet)
precision = 4 

for el in elements:
    loc = el.Location
    point = None
    
    # Get the location point for hosted components/fittings
    if isinstance(loc, DB.LocationPoint):
        point = loc.Point
    # Get the midpoint for linear elements like pipes/ducts
    elif isinstance(loc, DB.LocationCurve):
        point = loc.Curve.Evaluate(0.5, True)
    
    if point:
        # Create a key based on X and Y to identify the vertical stack
        key = (round(point.X, precision), round(point.Y, precision))
        groups[key].append((point.Z, el))

# 2. Process groups and transfer parameter values
with revit.Transaction("Transfer Comments Up Vertical Stacks"):
    updated_count = 0
    
    for coord in groups:
        stack = groups[coord]
        
        # Skip if there is only one element at this location
        if len(stack) < 2:
            continue
            
        # Sort the stack by Z elevation (lowest to highest)
        stack.sort(key=lambda x: x[0])
        
        # Reference the lowest element in the stack
        lowest_el = stack[0][1]
        
        # Retrieve the "Comments" value from the base element
        source_param = lowest_el.LookupParameter("Comments")
        
        if source_param and source_param.HasValue:
            comment_value = source_param.AsString()
            
            # Apply this value to all elements above it in the same stack
            for i in range(1, len(stack)):
                target_el = stack[i][1]
                target_param = target_el.LookupParameter("Comments")
                
                if target_param and not target_param.IsReadOnly:
                    target_param.Set(comment_value)
                    updated_count += 1

    print("Success! Processed {} vertical stacks.".format(len(groups)))
    print("Updated 'Comments' for {} elements.".format(updated_count))