"""probe3_save_first.py - tests whether SAVING the project before the
interactive solve avoids the save-changes dialog and yields real results.
If save-first produces non-zero forces/reactions, the app-path solve bug
is the save-changes dialog being auto-answered 'No'."""
from __future__ import annotations
import os, sys, tempfile
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from agent.tool_registry import ToolExecutor


def bar_count(b):
    coll = b.structure.Bars.GetAll()
    return int(coll.Count) if coll is not None else -1


def force_value(b, bar_id=1, case_id=1, ratio=0.5):
    try:
        f = b.structure.Results.Bars.Forces.Value(bar_id, case_id, ratio)
        return round(float(f.FX), 3), round(float(f.FZ), 3), round(float(f.MY), 3)
    except Exception as exc:
        return f"ERR {type(exc).__name__}: {str(exc)[:120]}"


ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
print("== fresh app connect ==")
print("pid:", b.connected_pid)

print("\n== build truss + self weight ==")
print(ex.dispatch("create_truss", {"span": 20.0, "height": 3.0, "panels": 6,
                                   "web_section": "L 50x50x5"})[:120])
ex.dispatch("create_load_case", {"case_id": 1, "case_name": "SW",
                                 "nature": "permanent"})
print(ex.dispatch("apply_self_weight", {"case_id": 1})[:120])
print("bars before:", bar_count(b))

print("\n== SAVE PROJECT FIRST (avoid save-changes dialog) ==")
rtd = os.path.join(tempfile.gettempdir(), "probe3_save_first.rtd")
try:
    b.project.SaveAs(rtd)
    print("SaveAs OK ->", rtd)
except Exception as exc:
    print("SaveAs failed:", type(exc).__name__, str(exc)[:160])

print("\n== solve ==")
print(ex.dispatch("solve", {"timeout_s": 120})[:200])

print("\n== AFTER solve ==")
print("bars:", bar_count(b))
print("force bar1@0.5 (FX,FZ,MY):", force_value(b))
print("force bar2@0.5 (FX,FZ,MY):", force_value(b, bar_id=2))
print("reactions (raw):")
try:
    print(b.export_reactions(case_id=1).to_string())
except Exception as exc:
    print("reactions ERR:", type(exc).__name__, str(exc)[:150])
