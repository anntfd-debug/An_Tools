Correct Pipes & Fittings V4.7

Workflow:
1) Preselect Pipe/Pipe Fitting in Revit.
2) Run tool.
3) Left list: normal/equal-size fitting types from selected PipeType Routing Preferences.
4) Right list: reducing/unequal-size fitting types (all loaded Pipe Fitting types).
5) Run replacement.

V4.7 automatically detects unequal physical connector sizes on each old fitting.
- Equal sizes -> uses the normal fitting list.
- Unequal sizes -> uses the reducing fitting list, matched by PartType.
- Direct placement includes a multi-size auto-size solver for families with separate size parameters (e.g. Nominal Diameter 1 / 2 / 3).
