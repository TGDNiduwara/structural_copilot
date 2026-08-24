"""COINC_probe_merged_nodal.py - nodal loads on merged-model node ids."""
from __future__ import annotations
import sys, json, time
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor
from tools.robot_tool import RobotEnum

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
run_tool = lambda t, a: json.loads(ex.dispatch(t, a))
F2 = json.load(open(r"batch/live_val_results/COINC_fixtures.json"))["F2"]
print("F2 spec nodes:", len(F2["nodes"]), "ids:", sorted(n["id"] for n in F2["nodes"]), file=sys.stderr)
run_tool("clear_structure", {"project_type": "3D"})
run_tool("create_structure_from_spec", {"spec": F2})
# live ids
coll = b.structure.Nodes.GetAll(); live = [int(coll.Get(i).Number) for i in range(1, int(coll.Count)+1)]
print("live ids:", sorted(live), file=sys.stderr)
for cid, nid in ((1, 1), (2, 4), (3, 56), (4, 102)):
    run_tool("create_load_case", {"case_id": cid, "case_name": f"C{cid}", "nature": "permanent"})
    try:
        r = run_tool("apply_nodal_load", {"node_id": nid, "case_id": cid, "fz_kn": -10.0})
        print(f"apply_nodal_load node {nid} -> {r}")
    except Exception as e:
        print(f"apply_nodal_load node {nid} RAISED: {e}")
t0=time.time(); run_tool("solve", {}); print(f"solve {time.time()-t0:.0f}s", file=sys.stderr)
for cid in (1, 2, 3, 4):
    run_tool("export_reactions", {"case_id": cid})
    df = ex.reactions_df
    print(f"case {cid}: sum FZ = {df['FZ_kN'].sum():.3f}")
