"""COINC_probe_roundtrip.py - when do coincident nodes vanish?
Build F2, count live nodes (GetAll) and _node_coords at: right after build,
after load cases, after solve, then via export_structure_spec.
"""
from __future__ import annotations
import sys, json, time
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
run_tool = lambda t, a: json.loads(ex.dispatch(t, a))
F2 = json.load(open(r"batch/live_val_results/COINC_fixtures.json"))["F2"]

def live_nodes():
    coll = b.structure.Nodes.GetAll()
    n = int(coll.Count) if coll is not None else -1
    ids = []
    for i in range(1, n + 1):
        try:
            ids.append(int(coll.Get(i).Number))
        except Exception:
            pass
    return n, sorted(ids)

run_tool("clear_structure", {"project_type": "3D"})
run_tool("create_structure_from_spec", {"spec": F2})
n, ids = live_nodes()
print(f"AFTER BUILD: live={n} bookkeeping={len(b._node_coords)}")
print(f"  live ids: {ids}")
for cid in (1, 2, 3, 4):
    run_tool("create_load_case", {"case_id": cid, "case_name": f"C{cid}", "nature": "permanent"})
run_tool("apply_nodal_load", {"node_id": 4, "case_id": 1, "fz_kn": -10.0})
run_tool("apply_nodal_load", {"node_id": 102, "case_id": 2, "fz_kn": -10.0})
n, ids = live_nodes()
print(f"AFTER CASES: live={n}")
t0 = time.time()
run_tool("solve", {})
print(f"solve {time.time()-t0:.1f}s", file=sys.stderr)
n, ids = live_nodes()
print(f"AFTER SOLVE: live={n}  bookkeeping={len(b._node_coords)}")
print(f"  live ids: {ids}")
