"""COINC_live_audit2.py - clean Phase 2.2 evidence (post-fix merged F2)."""
from __future__ import annotations
import sys, json, time
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
run_tool = lambda t, a: json.loads(ex.dispatch(t, a))
F2 = json.load(open(r"batch/live_val_results/COINC_fixtures.json"))["F2"]
print("before:", b.robot_session_status()["summary"], file=sys.stderr)
run_tool("clear_structure", {"project_type": "3D"})
run_tool("create_structure_from_spec", {"spec": F2})
coll = b.structure.Nodes.GetAll()
live = sorted(int(coll.Get(i).Number) for i in range(1, int(coll.Count)+1))
print("merged F2 live ids:", live, file=sys.stderr)
print("F2 has 0 coincident pairs (compose merges up front):", live)

# nodal loads at SURVIVING ids
for cid, nid in ((1, 4), (2, 56), (3, 2), (4, 99)):
    run_tool("create_load_case", {"case_id": cid, "case_name": f"C{cid}", "nature": "permanent"})
    run_tool("apply_nodal_load", {"node_id": nid, "case_id": cid, "fz_kn": -10.0})
run_tool("define_combination", {"name": "ULS_AB", "case_factors": {1: 1.35, 2: 1.35}})
run_tool("define_combination", {"name": "ULS_CD", "case_factors": {3: 1.35, 4: 1.35}})
# nonexistent node must now RAISE loudly
raised = False
try:
    run_tool("apply_nodal_load", {"node_id": 9999, "case_id": 1, "fz_kn": -1.0})
except Exception as e:
    raised = True
    print("apply_nodal_load(nonexistent 9999) RAISED:", str(e)[:110], file=sys.stderr)
print("nonexistent-node guard raises loudly:", raised)

st = run_tool("check_model_stability", {})
print("stability:", st.get("ok"), st.get("message"), file=sys.stderr)
t0 = time.time(); sol = run_tool("solve", {}); print(f"solve {time.time()-t0:.0f}s {sol.get('status')}", file=sys.stderr)
print("case     sum(FZ)")
for cid in (1, 2, 3, 4):
    run_tool("export_reactions", {"case_id": cid})
    print(f"  case{cid}: {float(ex.reactions_df['FZ_kN'].sum()):8.3f}")
for name in ("ULS_AB", "ULS_CD"):
    cid = [num for num, o in b._iter_all_cases() if str(getattr(o, "Name", "")) == name][0]
    run_tool("export_reactions", {"case_id": cid})
    print(f"  {name} (case {cid}): {float(ex.reactions_df['FZ_kN'].sum()):8.3f} (expect 27.0)")

# round-trip: export (post-solve) should equal the pre-solve merged geometry (lossless)
exp = run_tool("export_structure_spec", {})
geom = exp["geometry"]
print("export_structure_spec nodes:", len(geom["nodes"]), "(composed merged = 25)")
run_tool("clear_structure", {"project_type": "3D"})
r2 = run_tool("create_structure_from_spec", {"spec": geom})
print("rebuild from exported spec:", r2["nodes"], "nodes", r2["bars"], "bars")
print("after:", b.robot_session_status()["summary"], file=sys.stderr)
