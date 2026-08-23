"""probe5_interactive0.py - same interactive ToolExecutor path, but set
robot_app.Interactive=0 before solve (suppresses the save-changes dialog).
If results become non-zero, the interactive save-dialog + 'No' click is
the cause of the app-path zero-results bug."""
from __future__ import annotations
import sys
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from agent.tool_registry import ToolExecutor

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot

print("== build truss + self weight ==")
print(ex.dispatch("create_truss", {"span": 20.0, "height": 3.0, "panels": 6,
                                   "web_section": "L 50x50x5"})[:120])
ex.dispatch("create_load_case", {"case_id": 1, "case_name": "SW",
                                 "nature": "permanent"})
print(ex.dispatch("apply_self_weight", {"case_id": 1})[:120])
print("bars before:", b.structure.Bars.GetAll().Count)

print("\n== SUPPRESS interactive dialogs: Interactive=0 ==")
try:
    b.robot_app.Interactive = 0
    print("Interactive set to 0")
except Exception as exc:
    print("could not set Interactive:", exc)

print("\n== solve ==")
print(ex.dispatch("solve", {"timeout_s": 120})[:200])

print("\n== AFTER solve ==")
print("bars:", b.structure.Bars.GetAll().Count)
for bid in (1, 2, 10):
    try:
        f = b.structure.Results.Bars.Forces.Value(bid, 1, 0.5)
        print(f"  bar {bid} @mid (FX,FZ,MY):",
              round(float(f.FX), 3), round(float(f.FZ), 3), round(float(f.MY), 3))
    except Exception as exc:
        print(f"  bar {bid} ERR:", type(exc).__name__, str(exc)[:100])
print("reactions:")
try:
    print(b.export_reactions(case_id=1).to_string())
except Exception as exc:
    print("reactions ERR:", type(exc).__name__, str(exc)[:120])
