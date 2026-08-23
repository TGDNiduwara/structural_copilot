"""probe9_loads_debug.py - step-by-step diagnosis of _read_case_loads."""
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

print("\n== step 1: CastTo + Records ==")
try:
    sc = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
    print("CastTo OK; Records.Count =", sc.Records.Count)
    rec = sc.Records.Get(1)
    print("record1 Type:", rec.Type)
    print("record1 has Objects:", hasattr(rec, "Objects"))
    objs = rec.Objects
    print("record1 Objects.Count:", objs.Count)
    for j in (0, 1, 2):
        try:
            print(f"  GetValue({j}) =", rec.GetValue(j))
        except Exception as exc:
            print(f"  GetValue({j}) ERR:", type(exc).__name__, str(exc)[:100])
except Exception as exc:
    print("CastTo/Records ERR:", type(exc).__name__, str(exc)[:200])

print("\n== step 2: _record_object_ids ==")
try:
    rec = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase").Records.Get(1)
    print("ids:", b._record_object_ids(rec))
except Exception as exc:
    print("_record_object_ids ERR:", type(exc).__name__, str(exc)[:150])

print("\n== step 3: full _read_case_loads with traceback ==")
import traceback
try:
    out = b._read_case_loads(b.structure.Cases.Get(1), 1)
    print("result len:", len(out))
except Exception:
    traceback.print_exc()
