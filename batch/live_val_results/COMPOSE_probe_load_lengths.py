"""COMPOSE_probe_load_lengths.py - tool load values x ROBOT Geometry.Length.

Sum(PZ * Robot_bar.Geometry.Length) using Robot's OWN length property. If
this ~= 125.69 (the reactions), then the tool's self-weight TOTAL is wrong
because it uses a different length source than Robot applies. Also dump
raw record fields for a sample to settle how records are read back.
"""
from __future__ import annotations
import json
import sys

ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)

from agent.tool_registry import ToolExecutor
from win32com.client import CastTo

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
print("PROBE: tool load values x Robot Geometry.Length")
print("=" * 72)

run_tool("clear_structure", {"project_type": "3D"})
geom = compose_twin_arch()
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "SW",
                              "nature": "permanent"})
sw = run_tool("apply_self_weight", {"case_id": 1})
per_bar = sw.get("per_bar") or []
print("tool total_self_weight_kn:", sw.get("total_self_weight_kn"),
      "bars:", sw.get("bars"))

tool_len_sum = 0.0
robot_len_sum = 0.0
n_robot_len_zero = 0
length_diffs = []
for pb in per_bar:
    bid = int(pb["bar_id"])
    kn_m = abs(float(pb["load_kn_m"]))
    tool_L = float(pb["length_m"])
    tool_len_sum += kn_m * tool_L
    try:
        robot_L = float(b.structure.Bars.Get(bid).Geometry.Length)
    except Exception as e:
        robot_L = 0.0
        n_robot_len_zero += 1
    robot_len_sum += kn_m * robot_L
    if abs(robot_L - tool_L) > 1e-6:
        length_diffs.append((bid, pb["section"], round(tool_L, 4),
                             round(robot_L, 4), kn_m))
print(f"  sum(tool_length * kn_m)       = {tool_len_sum:.4f} kN")
print(f"  sum(Robot_Geometry.Length*kn_m)= {robot_len_sum:.4f} kN")
print(f"  bars where Robot length != tool length: {len(length_diffs)}")
for row in length_diffs[:12]:
    print("   ", row)
print(f"  bars with Robot length 0 / unreadable: {n_robot_len_zero}")
print(f"  => if robot_len_sum ~= 125.69: the tool's self-weight total uses a "
      f"DIFFERENT length than Robot applies -> tool total is wrong, not the model")

# raw record dump for the FIRST record
print("\n-- raw record #1 fields --------------------------------")
case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
nrec = int(case.Records.Count)
print("  case record count:", nrec)
if nrec > 0:
    rec = case.Records.Get(1)
    print("  Type:", rec.Type)
    rv = CastTo(rec, "IRobotBarUniformRecord")
    for k in range(0, 12):
        try:
            print(f"    Values.GetValue({k}) = {rv.Values.GetValue(k)}")
        except Exception:
            break
    rng = rv.Objects
    print("    Objects.Text:", repr(str(rng.Text)))
    try:
        print("    Objects.Count:", rng.Count)
    except Exception as e:
        print("    Objects.Count err:", e)
    try:
        print("    Objects.Get(1):", rng.Get(1))
    except Exception as e:
        print("    Objects.Get(1) err:", e)
