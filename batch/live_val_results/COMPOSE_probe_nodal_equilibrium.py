"""COMPOSE_probe_nodal_equilibrium.py - known nodal load vs reactions.

Applies 4 x 25 kN (total 100 kN) at the deck midspan nodes, solves, and
checks sum(FZ) reactions == 100 kN. Isolates structure+solve+export from
the uniform-load self-weight question.
"""
from __future__ import annotations
import json
import sys
import time

ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)

from agent.tool_registry import ToolExecutor

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot


def run_tool(tool, args):
    return json.loads(ex.dispatch(tool, args))


def compose_twin_arch():
    steps = [
        {"op": "chord", "name": "arch_a", "kind": "arc", "span": 30.0,
         "rise": 5.0, "n_panels": 10, "elevation": 0.0, "plane": 0.0,
         "section": "IPE 500"},
        {"op": "chord", "name": "deck_a", "kind": "straight", "span": 30.0,
         "n_panels": 10, "elevation": 0.0, "plane": 0.0, "section": "IPE 500"},
        {"op": "web", "top": "arch_a", "bottom": "deck_a", "pattern": "pratt",
         "web_section": "L 80x80x8"},
        {"op": "copy", "source": "arch_a", "name": "arch_b", "y_shift": 6.0},
        {"op": "copy", "source": "deck_a", "name": "deck_b", "y_shift": 6.0},
        {"op": "web", "top": "arch_b", "bottom": "deck_b", "pattern": "pratt",
         "web_section": "L 80x80x8"},
        {"op": "bracing", "plane_a": "arch_a", "plane_b": "arch_b",
         "pattern": "cross", "section": "L 60x60x6"},
        {"op": "bracing", "plane_a": "deck_a", "plane_b": "deck_b",
         "pattern": "cross", "section": "L 60x60x6"},
        {"op": "support", "chain": "deck_a", "type": "pinned"},
        {"op": "support", "chain": "deck_b", "type": "pinned"},
    ]
    for st in steps:
        run_tool("compose_structure", {"action": "step", "step": st})
    return run_tool("compose_structure", {"action": "finish"})["geometry"]


print("=" * 72)
print("PROBE: known nodal load (4 x 25 kN = 100 kN) vs reactions")
print("=" * 72)

run_tool("clear_structure", {"project_type": "3D"})
geom = compose_twin_arch()
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "NODAL",
                              "nature": "permanent"})

# deck_a = spec nodes 21-31 (y=0), deck_b = 103-113 (y=6). Apply -25 kN at
# four symmetric deck nodes: quarter + midspan of each deck. Total = -100 kN.
for nid in (24, 26, 106, 108):
    run_tool("apply_nodal_load", {"node_id": int(nid), "case_id": 1,
                                  "fz_kn": -25.0})

st = run_tool("check_model_stability", {})
print("stability:", st.get("ok"), st.get("message"))
t0 = time.time()
sol = run_tool("solve", {})
print("solve:", sol.get("status"), f"({time.time()-t0:.1f}s)")
if sol.get("warning"):
    print("  warning:", str(sol["warning"])[:180])

run_tool("export_reactions", {"case_id": 1})
df = ex.reactions_df
print("reactions:")
print(df.to_string())
sum_fz = float(df["FZ_kN"].sum())
print(f"  sum(FZ) reactions = {sum_fz:.3f} kN  (applied = 100 kN)")

# which nodes are the deck midspan nodes?
coords = {nd["id"]: (nd["x"], nd["y"], nd["z"]) for nd in geom["nodes"]}
print("  deck midspan spec nodes 26/31/108/113 coords:",
      coords.get(26), coords.get(31), coords.get(108), coords.get(113))
