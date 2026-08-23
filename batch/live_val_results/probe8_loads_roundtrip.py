"""probe8_loads_roundtrip.py - confirms _read_case_loads (CastTo fix) now
round-trips self-weight bar_uniform loads through export_structure_spec."""
from __future__ import annotations
import sys
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from agent.tool_registry import ToolExecutor

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot

ex.dispatch("create_truss", {"span": 20.0, "height": 3.0, "panels": 6,
                             "web_section": "L 50x50x5"})
ex.dispatch("create_load_case", {"case_id": 1, "case_name": "SW",
                                 "nature": "permanent"})
print(ex.dispatch("apply_self_weight", {"case_id": 1})[:160])

spec = b.export_structure_spec()
print("\ncases in spec:", len(spec.get("cases", [])))
print("loads in spec:", len(spec.get("loads", [])))
for ld in spec.get("loads", [])[:4]:
    print("  load:", ld)
print("\nread_case_loads direct (case obj):",
      len(b._read_case_loads(b.structure.Cases.Get(1), 1)))
