"""COINC_live_audit.py - Phase 2.2 tools vs coincident-node fixtures.
F2 (10 coincident pairs: supports / midspan / free end) vs F3 (control).
Tools: apply_nodal_load attribution, define_combination read-back,
export_structure_spec round-trip, check_model_stability.
"""
from __future__ import annotations
import sys, json, traceback
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
print("before:", b.robot_session_status()["summary"], file=sys.stderr)
fixtures = json.load(open(r"batch/live_val_results/COINC_fixtures.json"))
F2, F3 = fixtures["F2"], fixtures["F3"]

# --- build F2 ---
run_tool = lambda t, a: json.loads(ex.dispatch(t, a))
run_tool("clear_structure", {"project_type": "3D"})
r = run_tool("create_structure_from_spec", {"spec": F2})
print("F2 build:", {k: r[k] for k in ("nodes", "bars")}, file=sys.stderr)
for cid, name in ((1, "MID_D"), (2, "MID_I"), (3, "SUP_D"), (4, "SUP_A")):
    run_tool("create_load_case", {"case_id": cid, "case_name": name,
                                  "nature": "permanent"})
run_tool("apply_nodal_load", {"node_id": 4, "case_id": 1, "fz_kn": -10.0})
run_tool("apply_nodal_load", {"node_id": 102, "case_id": 2, "fz_kn": -10.0})
run_tool("apply_nodal_load", {"node_id": 1, "case_id": 3, "fz_kn": -10.0})
run_tool("apply_nodal_load", {"node_id": 56, "case_id": 4, "fz_kn": -10.0})
run_tool("define_combination", {"name": "ULS_MID", "case_factors": {1: 1.35, 2: 1.35}})
run_tool("define_combination", {"name": "ULS_SUP", "case_factors": {3: 1.35, 4: 1.35}})

st = run_tool("check_model_stability", {})
print("F2 stability:", st.get("ok"), st.get("message"), file=sys.stderr)

import time
t0 = time.time()
sol = run_tool("solve", {})
print(f"solve {sol.get('status')} ({time.time()-t0:.1f}s)", file=sys.stderr)

# reactions per case + combos
print("case     sum(FZ)   (expect 10.0 each; combos 27.0)")
for cid, exp in ((1, 10.0), (2, 10.0), (3, 10.0), (4, 10.0)):
    run_tool("export_reactions", {"case_id": cid})
    s = float(ex.reactions_df["FZ_kN"].sum())
    print(f"  case{cid}: {s:8.3f}  (err {abs(abs(s)-exp)/exp*100:.2f}%)")
# combo ids: find by name
for cid, exp in ((None, 27.0), (None, 27.0)):
    pass
for name in ("ULS_MID", "ULS_SUP"):
    cid = [num for num, obj in b._iter_all_cases()
           if str(getattr(obj, "Name", "")) == name][0]
    run_tool("export_reactions", {"case_id": cid})
    s = float(ex.reactions_df["FZ_kN"].sum())
    print(f"  {name} (case {cid}): {s:8.3f}  (err {abs(abs(s)-27.0)/27.0*100:.2f}%)")

# --- export_structure_spec round-trip on F2 ---
exp = run_tool("export_structure_spec", {})
geom = exp["geometry"]
node_ids = [n["id"] for n in geom["nodes"]]
print("export_structure_spec nodes:", len(node_ids), "(F2 has 35)")
pairs = [(56,1),(62,7),(4,102),(142,13),(143,14),(144,15),(145,16),(146,17),(147,18),(148,19)]
missing = [p for p in pairs if p[0] not in node_ids or p[1] not in node_ids]
print("  coincident-pair ids both present:", "NO" if missing else "YES", missing)
run_tool("clear_structure", {"project_type": "3D"})
r2 = run_tool("create_structure_from_spec", {"spec": geom})
print("  rebuild from exported spec:", r2["nodes"], "nodes, ", r2["bars"], "bars")

# --- F3 control: stability + a nodal load ---
run_tool("clear_structure", {"project_type": "3D"})
run_tool("create_structure_from_spec", {"spec": F3})
st3 = run_tool("check_model_stability", {})
print("F3 stability:", st3.get("ok"), st3.get("message"), file=sys.stderr)
print("after:", b.robot_session_status()["summary"], file=sys.stderr)
