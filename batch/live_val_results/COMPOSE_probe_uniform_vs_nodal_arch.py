"""COMPOSE_probe_uniform_vs_nodal_arch.py - A/B on the same twin-arch.

One build, two load cases solved together:
  case 1 = bar-uniform self-weight (the current apply_self_weight)
  case 2 = nodal-lumped self-weight (each bar's weight split 50/50 to its
           two end nodes - the classic truss lumping)
Compare sum(FZ) reactions of each against the same 149.0692 kN total.
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
print("PROBE A/B: bar-uniform vs nodal-lumped self-weight (one build)")
print("=" * 72)

run_tool("clear_structure", {"project_type": "3D"})
geom = compose_twin_arch()
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "UNIFORM",
                              "nature": "permanent"})
run_tool("create_load_case", {"case_id": 2, "case_name": "NODAL",
                              "nature": "permanent"})
sw = run_tool("apply_self_weight", {"case_id": 1})
tool_total = float(sw.get("total_self_weight_kn"))
per_bar = sw.get("per_bar") or []

# nodal lump: each bar's weight split 50/50 to its endpoint nodes (case 2)
spec_ends = {br["id"]: (br["n1"], br["n2"]) for br in geom["bars"]}
nodal_at = {}
for pb in per_bar:
    bid = int(pb["bar_id"])
    w = float(pb["weight_kn"])
    n1, n2 = spec_ends[bid]
    nodal_at[n1] = nodal_at.get(n1, 0.0) + w / 2.0
    nodal_at[n2] = nodal_at.get(n2, 0.0) + w / 2.0
print(f"tool total: {tool_total:.4f} kN; nodal lumps at {len(nodal_at)} nodes")
for nid, w in sorted(nodal_at.items()):
    run_tool("apply_nodal_load", {"node_id": nid, "case_id": 2,
                                  "fz_kn": -round(w, 5)})

st = run_tool("check_model_stability", {})
print("stability:", st.get("ok"), st.get("message"))
t0 = time.time()
sol = run_tool("solve", {})
print("solve:", sol.get("status"), f"({time.time()-t0:.1f}s)")

for case_id in (1, 2):
    run_tool("export_reactions", {"case_id": case_id})
    df = ex.reactions_df
    s = float(df["FZ_kN"].sum())
    err = abs(abs(s) - tool_total) / tool_total * 100
    print(f"case {case_id} sum(FZ) = {s:.4f} kN  (vs tool total {tool_total:.4f} "
          f"kN -> {err:.2f}% error)")
    print(df.to_string())
