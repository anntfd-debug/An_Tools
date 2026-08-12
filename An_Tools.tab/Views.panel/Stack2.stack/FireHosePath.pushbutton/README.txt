Fire Cabinet Coverage Routes - Revit 2025 / pyRevit

Files:
- script.py
- ui.xaml

Replace the existing pushbutton bundle files with these two files.

Main output:
- One longest owned Dijkstra route per connected cabinet.
- X marker at cabinet centre.
- Plus marker at route endpoint.
- Optional diamond marker for each uncovered cluster.
- No uncovered Filled Regions are created.
- Previous route results are managed by Extensible Storage and deleted on the next run in the active view.

Recommended first test:
Run on a detached/test copy of the project and confirm the selected Detail Line style, cabinet count and linked-cabinet diagnostics before using on the production model.
