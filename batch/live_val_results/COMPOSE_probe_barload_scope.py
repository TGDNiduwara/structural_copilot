"""COMPOSE_probe_barload_scope.py - scope of the bar_uniform shortfall.

Q1 (user req 1): does the 15.68% bar_uniform shortfall hit ORDINARY
    apply_bar_load calls (not just self-weight)? -> BUILD 1 A/B on a deck UDL.
Q2 (user req 2): what topology triggers it? Two discriminators:
    BUILD 2: single-plane arch_truss (coincident end nodes + zero-length end
             verticals, but ONE plane) - if it fails, single-plane is NOT safe.
    BUILD 3: twin FLAT braced trusses (two planes + cross bracing, NO
             coincident nodes) - if it fails, multi-plane+bracing alone
             triggers it; if clean, the arch springing geometry is implicated.
BUILD 1 case 1 goes through the actual apply_bar_load TOOL dispatch; the
other bar_uniform loads use the same bridge method underneath.
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

RESULTS = []


def run_tool(tool, args):
    return json.loads(ex.dispatch(tool, args))


def compose(steps):
    for st in steps:
        run_tool("compose_structure", {"action": "step", "step": st})
    return run_tool("compose_structure", {"action": "finish"})["geometry"]


def twin_arch_steps():
    return [
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


def twin_flat_steps():
    return [
        {"op": "chord", "name": "top_a", "kind": "straight", "span": 30.0,
         "n_panels": 10, "elevation": 2.0, "plane": 0.0, "section": "IPE 300"},
        {"op": "chord", "name": "bot_a", "kind": "straight", "span": 30.0,
         "n_panels": 10, "elevation": 0.0, "plane": 0.0, "section": "IPE 300"},
        {"op": "web", "top": "top_a", "bottom": "bot_a", "pattern": "pratt",
         "web_section": "L 80x80x8"},
        {"op": "copy", "source": "top_a", "name": "top_b", "y_shift": 6.0},
        {"op": "copy", "source": "bot_a", "name": "bot_b", "y_shift": 6.0},
        {"op": "web", "top": "top_b", "bottom": "bot_b", "pattern": "pratt",
         "web_section": "L 80x80x8"},
        {"op": "bracing", "plane_a": "top_a", "plane_b": "top_b",
         "pattern": "cross", "section": "L 60x60x6"},
        {"op": "bracing", "plane_a": "bot_a", "plane_b": "bot_b",
         "pattern": "cross", "section": "L 60x60x6"},
        {"op": "support", "chain": "bot_a", "type": "pinned"},
        {"op": "support", "chain": "bot_b", "type": "pinned"},
    ]


def sum_fz(case_id):
    run_tool("export_reactions", {"case_id": case_id})
    df = ex.reactions_df
    return float(df["FZ_kN"].sum())


def report(tag, got, expected):
    err = abs(abs(got) - expected) / expected * 100
    RESULTS.append((tag, round(abs(got), 3), round(expected, 3),
                    round(err, 2)))
    print(f"  -> {tag}: sum(FZ)={got:.3f} kN vs expected {expected:.3f} kN "
          f"=> {err:.2f}% error")


print("=" * 72)
print("PROBE: scope of the bar_uniform shortfall")
print("=" * 72)

# ---------------- BUILD 1: twin-arch DECK UDL A/B (the real tool) ----------
print("\n[BUILD 1] twin-arch, deck UDL via apply_bar_load TOOL (A/B)")
run_tool("clear_structure", {"project_type": "3D"})
geom = compose(twin_arch_steps())
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "UDL_BAR",
                              "nature": "variable"})
run_tool("create_load_case", {"case_id": 2, "case_name": "UDL_NODAL",
                              "nature": "variable"})
deck_ids = set(range(21, 32)) | set(range(103, 114))
deck_chord_bars = [br["id"] for br in geom["bars"]
                   if br["n1"] in deck_ids and br["n2"] in deck_ids
                   and br["section"] == "IPE 500"]
print(f"  deck chord bars loaded: {len(deck_chord_bars)} (expect 20)")
Q = 10.0  # kN/m
total1 = 0.0
for bid in deck_chord_bars:
    run_tool("apply_bar_load", {"bar_id": int(bid), "case_id": 1,
                                "value_kn_m": -Q, "direction": "Z"})
    n1, n2 = b._bar_endpoints[bid]
    w = Q * b._bar_length(bid)
    total1 += w
    run_tool("apply_nodal_load", {"node_id": int(n1), "case_id": 2,
                                  "fz_kn": -w / 2.0})
    run_tool("apply_nodal_load", {"node_id": int(n2), "case_id": 2,
                                  "fz_kn": -w / 2.0})
print(f"  expected total per case: {total1:.3f} kN")
t0 = time.time()
sol = run_tool("solve", {})
print(f"  solve: {sol.get('status')} ({time.time()-t0:.1f}s)"
      + (f" warn={str(sol.get('warning'))[:80]}" if sol.get("warning") else ""))
report("BUILD1 case1 bar_uniform deck UDL", sum_fz(1), total1)
report("BUILD1 case2 nodal-lumped deck UDL", sum_fz(2), total1)

# ------------- BUILD 2: single-plane arch_truss, bar_uniform all bars -------
print("\n[BUILD 2] single-plane arch_truss (template) + bar_uniform -1 kN/m")
run_tool("clear_structure", {"project_type": "3D"})
run_tool("create_arch_truss", {"span": 30.0, "rise": 5.0, "panels": 10})
run_tool("create_load_case", {"case_id": 1, "case_name": "U1",
                              "nature": "variable"})
total2 = 0.0
for bid in sorted(b._bar_endpoints):
    L = b._bar_length(bid)
    if L <= 0.0:
        continue  # zero-length end verticals carry no load either way
    b.apply_bar_load(bid, 1, -1.0, "Z")
    total2 += L
print(f"  loaded at 1 kN/m; expected total = sum(L) = {total2:.3f} kN")
t0 = time.time()
sol = run_tool("solve", {})
print(f"  solve: {sol.get('status')} ({time.time()-t0:.1f}s)"
      + (f" warn={str(sol.get('warning'))[:80]}" if sol.get("warning") else ""))
report("BUILD2 single-plane arch bar_uniform", sum_fz(1), total2)

# ------------- BUILD 3: twin FLAT braced trusses, bar_uniform all bars ------
print("\n[BUILD 3] twin FLAT braced trusses + bar_uniform -1 kN/m")
run_tool("clear_structure", {"project_type": "3D"})
geom3 = compose(twin_flat_steps())
run_tool("create_structure_from_spec", {"spec": geom3})
run_tool("create_load_case", {"case_id": 1, "case_name": "U1",
                              "nature": "variable"})
total3 = 0.0
n_bars3 = 0
for bid in sorted(b._bar_endpoints):
    L = b._bar_length(bid)
    if L <= 0.0:
        continue
    b.apply_bar_load(bid, 1, -1.0, "Z")
    total3 += L
    n_bars3 += 1
print(f"  {n_bars3} bars loaded at 1 kN/m; expected total = {total3:.3f} kN")
t0 = time.time()
sol = run_tool("solve", {})
print(f"  solve: {sol.get('status')} ({time.time()-t0:.1f}s)"
      + (f" warn={str(sol.get('warning'))[:80]}" if sol.get("warning") else ""))
report("BUILD3 twin flat braced bar_uniform", sum_fz(1), total3)

print("\n=== SUMMARY ===")
for tag, got, exp, err in RESULTS:
    print(f"  {err:6.2f}%  {tag}  (got {got} / expected {exp} kN)")
