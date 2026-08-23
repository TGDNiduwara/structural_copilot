"""probe10_object_ids.py - what does record.Objects.Text / Get(k) really return?"""
from __future__ import annotations
import sys
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from agent.tool_registry import ToolExecutor
from win32com.client import CastTo

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot

ex.dispatch("create_truss", {"span": 20.0, "height": 3.0, "panels": 6,
                             "web_section": "L 50x50x5"})
ex.dispatch("create_load_case", {"case_id": 1, "case_name": "SW",
                                 "nature": "permanent"})
ex.dispatch("apply_self_weight", {"case_id": 1})

sc = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
rec = sc.Records.Get(1)
print("record Type:", rec.Type)
rng = rec.Objects
print("Objects.Count:", rng.Count)
print("Objects.Text repr:", repr(rng.Text) if hasattr(rng, "Text") else "(no Text)")
try:
    got = rng.Get(1)
    print("Get(1) ->", repr(got))
    print("Get(1) attrs: Number=", end="")
    try:
        print(got.Number)
    except Exception as e:
        print("ERR", e)
    print("Get(1) Name:", end="")
    try:
        print(got.Name)
    except Exception as e:
        print("ERR", e)
except Exception as exc:
    print("Get(1) ERR:", type(exc).__name__, str(exc)[:150])
