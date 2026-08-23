"""probe_app_solve.py - reproduces the regression's exact app-path scenario
and dumps ENOUGH raw detail to explain zero reactions: actual member-force
VALUES (not just row counts), support node ids vs truss_spec expectation,
the case's load records (CastTo IRobotSimpleCase), and the exported spec."""
from __future__ import annotations
import json, sys
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from agent.tool_registry import ToolExecutor
from tools.robot_tool import RobotBridge, RobotEnum
from win32com.client import CastTo

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot

print("== build ==")
r = ex.dispatch("create_truss", {"span": 20.0, "height": 3.0, "panels": 6,
                                 "web_section": "L 50x50x5"})
print("create_truss:", r[:220])
ex.dispatch("create_load_case", {"case_id": 1, "case_name": "SW",
                                 "nature": "permanent"})
sw = ex.dispatch("apply_self_weight", {"case_id": 1})
print("apply_self_weight:", sw[:300])

print("\n== supports in the LIVE model (which nodes are pinned?) ==")
for nid in sorted(b._node_coords):
    try:
        node = b.structure.Nodes.Get(nid)
        if bool(node.HasLabel(RobotEnum.I_LT_SUPPORT)):
            print(f"  node {nid} has support label: "
                  f"{node.GetLabelName(RobotEnum.I_LT_SUPPORT)}")
    except Exception as e:
        print(f"  node {nid} support probe failed: {e}")
print("expected truss supports:", b.truss_spec(span=20.0, height=3.0,
      panels=6, web_section="L 50x50x5")["supports"])

print("\n== solve ==")
print("solve:", ex.dispatch("solve", {"timeout_s": 120})[:200])

print("\n== member force VALUES (bar 1, stations 0..2) ==")
df = b.export_all_member_forces(case_id=1, divisions=4)
sub = df[df["Bar_ID"] <= 3]
print(sub[["Bar_ID", "Position_m", "FX_kN", "FZ_kN", "MY_kNm"]].to_string())

print("\n== reactions raw ==")
rd = b.export_reactions(case_id=1)
print(rd.to_string())

print("\n== case 1 load records (CastTo IRobotSimpleCase) ==")
case = b.structure.Cases.Get(1)
try:
    sc = CastTo(case, "IRobotSimpleCase")
    print("CastTo OK; Records.Count =", sc.Records.Count)
    for i in range(1, int(sc.Records.Count) + 1):
        rec = sc.Records.Get(i)
        print(f"  record {i}: type={rec.Type} label={rec.Number} "
              f"objects={rec.Objects.Count}")
except Exception as exc:
    print("CastTo/Records failed:", exc)

print("\n== export_structure_spec ==")
try:
    spec = b.export_structure_spec()
    print("nodes:", len(spec.get("nodes", [])),
          "bars:", len(spec.get("bars", [])),
          "supports:", len(spec.get("supports", [])),
          "cases:", len(spec.get("cases", [])),
          "loads:", len(spec.get("loads", [])))
except Exception as exc:
    print("export_structure_spec raised:", exc)
