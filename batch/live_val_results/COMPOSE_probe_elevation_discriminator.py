"""COMPOSE_probe_elevation_discriminator.py - pin the exact trigger.

BUILD 4: twin-arch but the arch springs from z=0.5 (elevation=0.5) so NO
node is coincident with another and every end vertical is a real 0.5 m bar.
bar_uniform -1 kN/m on all bars. If exact -> coincident node pairs are the
trigger. If still short -> arch/curved geometry or valence, detector must
be broader.
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


STEPS = [
    {"op": "chord", "name": "arch_a", "kind": "arc", "span": 30.0,
     "rise": 5.0, "n_panels": 10, "elevation": 0.5, "plane": 0.0,
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

print("=" * 72)
print("BUILD 4: twin-arch with arch elevated 0.5 m (NO coincident nodes)")
print("=" * 72)
run_tool("clear_structure", {"project_type": "3D"})
for st in STEPS:
    run_tool("compose_structure", {"action": "step", "step": st})
geom = run_tool("compose_structure", {"action": "finish"})["geometry"]

# verify: no coincident node pairs, no zero-length bars
coords = [tuple(round(c, 6) for c in (n["x"], n["y"], n["z"]))
          for n in geom["nodes"]]
dupes = len(coords) - len(set(coords))
cmap = {n["id"]: (n["x"], n["y"], n["z"]) for n in geom["nodes"]}
zl = [br["id"] for br in geom["bars"] if cmap[br["n1"]] == cmap[br["n2"]]]
print(f"  coincident node pairs: {dupes}; zero-length bars: {len(zl)}")
print(f"  nodes={len(geom['nodes'])} bars={len(geom['bars'])}")

run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "U1",
                              "nature": "variable"})
total = 0.0
for bid in sorted(b._bar_endpoints):
    L = b._bar_length(bid)
    if L <= 0.0:
        continue
    b.apply_bar_load(bid, 1, -1.0, "Z")
    total += L
print(f"  loaded at 1 kN/m; expected total = {total:.3f} kN")
t0 = time.time()
sol = run_tool("solve", {})
print(f"  solve: {sol.get('status')} ({time.time()-t0:.1f}s)"
      + (f" warn={str(sol.get('warning'))[:80]}" if sol.get("warning") else ""))
run_tool("export_reactions", {"case_id": 1})
got = float(ex.reactions_df["FZ_kN"].sum())
err = abs(abs(got) - total) / total * 100
print(f"  -> sum(FZ)={got:.3f} kN vs expected {total:.3f} kN => {err:.2f}% error")
