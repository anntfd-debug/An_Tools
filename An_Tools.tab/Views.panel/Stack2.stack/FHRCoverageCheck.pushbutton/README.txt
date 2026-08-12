Fire Cabinet Coverage - Updated

Files:
- script.py
- ui.xaml

Main changes:
1. Automatic cabinet scan reads host-model FamilyInstances only. Cabinet sources are never collected from Revit links.
2. Revit links are still processed as walls, doors, columns and equipment obstacles, preserving the previous route model.
3. New optional selected-door gateway mode.
4. After Run, select multiple doors from the host model.
5. Each valid selected door is assigned to the cabinet with the shortest real walkable-grid route to that door.
6. A cabinet assigned to one or more selected doors is removed as a direct Dijkstra source. Its final coverage begins at the assigned door gateway(s), including the cabinet-to-door path as initial travel cost.
7. Cabinets not assigned to a selected door retain the original direct-source behavior.
8. Added gateway diagnostics and a Forced door(s) column to the cabinet report.

Installation:
Place script.py and ui.xaml in the same pyRevit pushbutton bundle folder.
