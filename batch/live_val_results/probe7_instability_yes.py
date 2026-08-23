"""probe7_instability_yes.py - interactive path but the instability dialog
is answered 'Yes' (continue) instead of 'No' (abort). If results become
correct, the interactive zero-results bug is the wrong button on the
Instability type 3 dialog, not the model."""
from __future__ import annotations
import sys
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from agent.tool_registry import ToolExecutor

import batch.headless_driver as hd
# Monkeypatch the pattern the interactive _guarded_calculate uses: answer
# the instability dialog with Yes (continue) instead of No (abort).
HD_PATTERNS = dict(hd.DEFAULT_DIALOG_PATTERNS)
HD_PATTERNS["instabilit"] = {"action": "click", "button_text": "Yes"}
hd.DEFAULT_DIALOG_PATTERNS = HD_PATTERNS

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot

print("== build truss + self weight ==")
print(ex.dispatch("create_truss", {"span": 20.0, "height": 3.0, "panels": 6,
                                   "web_section": "L 50x50x5"})[:100])
ex.dispatch("create_load_case", {"case_id": 1, "case_name": "SW",
                                 "nature": "permanent"})
print(ex.dispatch("apply_self_weight", {"case_id": 1})[:100])

print("\n== solve (instability answered Yes) ==")
print(ex.dispatch("solve", {"timeout_s": 120})[:200])

print("\n== results ==")
for bid in (1, 2, 10):
    try:
        f = b.structure.Results.Bars.Forces.Value(bid, 1, 0.5)
        print(f"  bar {bid} @mid (FX,FZ,MY):",
              round(float(f.FX),3), round(float(f.FZ),3), round(float(f.MY),3))
    except Exception as exc:
        print(f"  bar {bid} ERR:", type(exc).__name__, str(exc)[:100])
print("reactions:")
try:
    print(b.export_reactions(case_id=1).to_string())
except Exception as exc:
    print("reactions ERR:", type(exc).__name__, str(exc)[:120])
