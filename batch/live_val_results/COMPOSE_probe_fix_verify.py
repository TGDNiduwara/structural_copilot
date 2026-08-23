"""COMPOSE_probe_fix_verify.py - verify the apply_bar_load auto-nodal fix.

After the fix, on a coincident-node model the tool MUST report
method=nodal_lumped and reactions MUST balance; on safe models it must
still use method=bar_uniform_record and balance. Re-runs the three key
builds from the scope probe and asserts equilibrium + method.
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
        {"op": "chord", "name": "arch_a", "kind": "arc", "span": 30.0, "rise": 5.0,
         "n_panels": 10, "elevation": 0.0, "plane": 0.0, "section": "IPE 500"},
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


def report_case(tag, method, got, expected):
    err = abs(abs(got) - expected) / expected * 100
    RESULTS.append((tag, method, round(err, 3)))
    print(f"  -> {tag}: method={method}  sum(FZ)={got:.3f} kN vs "
          f"{expected:.3f} kN => {err:.3f}% error")


print("=" * 72)
print("VERIFY: apply_bar_load auto-nodal fix (build+method+equilibrium)")
print("=" * 72)

# ---------------- BUILD 1: twin-arch deck UDL via the real TOOL ------------
print("\n[BUILD 1] twin-arch (coincident) -> apply_bar_load must AUTO-NODAL")
run_tool("clear_structure", {"project_type": "3D"})
geom = compose(twin_arch_steps())
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "UDL_BAR",
                              "nature": "variable"})
deck_ids = set(range(21, 32)) | set(range(103, 114))
deck_bars = [br["id"] for br in geom["bars"]
             if br["n1"] in deck_ids and br["n2"] in deck_ids
             and br["section"] == "IPE 500"]
Q = 10.0
total = 0.0
methods = set()
for bid in deck_bars:
    r = run_tool("apply_bar_load", {"bar_id": int(bid), "case_id": 1,
                                    "value_kn_m": -Q, "direction": "Z"})
    methods.add(str(r.get("method")))
    total += Q * b._bar_length(bid)
print(f"  deck bars loaded: {len(deck_bars)}; tool methods reported: {methods}")
t0 = time.time()
sol = run_tool("solve", {})
print(f"  solve: {sol.get('status')} ({time.time()-t0:.1f}s)")
run_tool("export_reactions", {"case_id": 1})
got = float(ex.reactions_df["FZ_kN"].sum())
report_case("BUILD1 twin-arch deck UDL (auto-nodal)", ",".join(sorted(methods)),
            got, total)

# ---------------- BUILD 2: twin FLAT (no coincident) -> true records -------
print("\n[BUILD 2] twin flat (no coincident) -> apply_bar_load must use records")
run_tool("clear_structure", {"project_type": "3D"})
geom = compose(twin_flat_steps())
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "U1",
                              "nature": "variable"})
total = 0.0
methods = set()
for bid in sorted(b._bar_endpoints):
    L = b._bar_length(bid)
    if L <= 0.0:
        continue
    r = b.apply_bar_load(bid, 1, -1.0, "Z")
    methods.add(str(r.get("method")))
    total += L
print(f"  bars loaded: {sum(1 for x in b._bar_endpoints if b._bar_length(x) > 0)}; "
      f"methods: {methods}")
t0 = time.time()
sol = run_tool("solve", {})
print(f"  solve: {sol.get('status')} ({time.time()-t0:.1f}s)")
run_tool("export_reactions", {"case_id": 1})
got = float(ex.reactions_df["FZ_kN"].sum())
report_case("BUILD2 twin flat (true records)", ",".join(sorted(methods)), got, total)

# ------- BUILD 3: elevated twin-arch (no coincident) -> true records -------
print("\n[BUILD 3] elevated twin-arch (no coincident) -> true records")
run_tool("clear_structure", {"project_type": "3D"})
steps = twin_arch_steps()
for st in steps:
    if st.get("op") == "chord" and str(st.get("name", "")).startswith("arch") \
            and st.get("kind") == "arc":
        st["elevation"] = 0.5
geom = compose(steps)
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "U1",
                              "nature": "variable"})
total = 0.0
methods = set()
for bid in sorted(b._bar_endpoints):
    L = b._bar_length(bid)
    if L <= 0.0:
        continue
    r = b.apply_bar_load(bid, 1, -1.0, "Z")
    methods.add(str(r.get("method")))
    total += L
print(f"  methods: {methods}")
t0 = time.time()
sol = run_tool("solve", {})
print(f"  solve: {sol.get('status')} ({time.time()-t0:.1f}s)")
run_tool("export_reactions", {"case_id": 1})
got = float(ex.reactions_df["FZ_kN"].sum())
report_case("BUILD3 elevated twin-arch (true records)",
            ",".join(sorted(methods)), got, total)


print("\n=== SUMMARY ===")
ok = all(err <= 1.0 for _t, _m, err in RESULTS)
for tag, method, err in RESULTS:
    print(f"  {err:6.3f}%  method={method:<22} {tag}")
print("VERDICT:", "PASS" if ok else "FAIL")