"""probe11_L_catalog.py - which equal-angle 'L legxlegxt' forms resolve in
Robot's live catalogs? Builds a bar for each candidate and reports which
LoadFromDBase2 names are accepted (so available_sections('L') can return
resolvable names instead of bare leg sizes like 'L 100')."""
from __future__ import annotations
import sys
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from agent.tool_registry import ToolExecutor

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot

b.new_3d_frame()
b.create_node(1, 0, 0, 0)
b.create_node(2, 1, 0, 0)

candidates = [
    "L 40x40x5", "L 45x45x5", "L 50x50x5", "L 60x60x6",
    "L 65x65x6", "L 70x70x5", "L 80x80x8", "L 90x90x8",
    "L 100x100x5", "L 100x100x10", "L 120x120x5", "L 120x120x10",
    "L 150x150x10", "L 150x150x15",
]
ok, bad = [], []
for i, name in enumerate(candidates, start=1):
    bar_id = i
    try:
        b.create_bar(bar_id, 1, 2, name)
        ok.append(name)
    except Exception as exc:  # noqa: BLE001
        bad.append((name, str(exc)[:90]))
print("RESOLVE:", ok)
print("REJECT :", bad)
