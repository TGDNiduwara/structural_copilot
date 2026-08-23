"""COMPOSE_probe_uniform_vs_nodal.py - how does Robot apply bar-uniform PZ?

Minimal decisive test: a single bar pinned at both ends, uniform -1 kN/m:
  Bar A (horizontal, (0,0,0)-(3,0,0))  -> total should be 3.000 kN
  Bar B (diagonal,  (0,0,0)-(3,0,3))   -> total should be 3*sqrt(2) if PZ is
                                          global-Z per unit bar length.
Settles whether bar-uniform loads are trusted on 3D geometry.
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


def run_tool(tool, args):
    return json.loads(ex.dispatch(tool, args))


def reactions():
    run_tool("export_reactions", {"case_id": 1})
    df = ex.reactions_df
    print(df.to_string())
    return float(df["FZ_kN"].sum())


def test_bar(name, n1c, n2c):
    print(f"\n=== bar '{name}': {n1c} -> {n2c} ===")
    run_tool("clear_structure", {"project_type": "3D"})
    spec = {
        "project": "3D",
        "nodes": [
            {"id": 1, "x": n1c[0], "y": n1c[1], "z": n1c[2]},
            {"id": 2, "x": n2c[0], "y": n2c[1], "z": n2c[2]},
        ],
        "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "IPE 200"}],
        "supports": [{"node": 1, "type": "pinned"},
                     {"node": 2, "type": "pinned"}],
    }
    run_tool("create_structure_from_spec", {"spec": spec})
    run_tool("create_load_case", {"case_id": 1, "case_name": "U",
                                  "nature": "permanent"})
    run_tool("apply_bar_load", {"bar_id": 1, "case_id": 1,
                                "value_kn_m": -1.0, "direction": "Z"})
    length = ((n2c[0]-n1c[0])**2 + (n2c[1]-n1c[1])**2
              + (n2c[2]-n1c[2])**2) ** 0.5
    print(f"  bar length = {length:.4f} m; expected load = {length:.4f} kN")
    t0 = time.time()
    sol = run_tool("solve", {})
    print(f"  solve: {sol.get('status')} ({time.time()-t0:.1f}s)")
    r = reactions()
    err = abs(abs(r) - length) / length * 100
    print(f"  sum(FZ) reactions = {r:.4f} kN vs expected {length:.4f} "
          f"({err:.2f}%)")
    return err


print("=" * 72)
print("PROBE: bar-uniform PZ on horizontal vs diagonal 3D bars")
print("=" * 72)
e_h = test_bar("horizontal", (0.0, 0.0, 0.0), (3.0, 0.0, 0.0))
e_d = test_bar("diagonal", (0.0, 0.0, 0.0), (3.0, 0.0, 3.0))
e_3d = test_bar("3D-slab (y-spanning)", (0.0, 0.0, 0.0), (3.0, 6.0, 0.0))
print("\n=== RESULTS ===")
print(f"  horizontal: {e_h:.2f}% error")
print(f"  diagonal  : {e_d:.2f}% error")
print(f"  3D y-span : {e_3d:.2f}% error")
