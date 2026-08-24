"""COINC_AUDIT_live.py - live Robot quantification (E1-E7) vs OpenSeesPy ideal refs.

Approved to use the single Robot seat. Each experiment builds, solves and
exports reactions; diffs are against the OpenSeesPy references in
COINC_AUDIT_OPENSEES_refs.json. Records are written to
COINC_AUDIT_LIVE_RESULTS.json.
"""
from __future__ import annotations
import json
import sys
import time

ROOT = r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot"
sys.path.insert(0, ROOT)

from agent.tool_registry import ToolExecutor

payload = json.load(open(r"batch/live_val_results/COINC_AUDIT_OPENSEES_specs.json"))
S2 = payload["S2_premerge"]
S3 = payload["S3_merged"]

RESULTS = {}

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
run = lambda t, a: json.loads(ex.dispatch(t, a))


def reactions_fz_sum() -> tuple[float, list]:
    run("export_reactions", {"case_id": 1})
    df = ex.reactions_df
    cols = [c for c in ("FZ_kN", "FZ") if c in df.columns]
    rows = [(int(r["Node_ID"]), float(r[cols[0]])) for _, r in df.iterrows()]
    return sum(v for _, v in rows), rows


def pct(a: float, b: float) -> float:
    return abs(a - b) / abs(b) * 100.0 if b else 0.0


def record(name: str, ok: bool, summary: str, extra=None) -> None:
    d = {"ok": ok, "summary": summary}
    if extra:
        d["extra"] = extra
    RESULTS[name] = d
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: {summary}", flush=True)


def build_spec(spec):
    run("clear_structure", {"project_type": "3D"})
    for n in spec["nodes"]:
        run(
            "create_node",
            {"node_id": int(n["id"]), "x": float(n.get("x", 0)), "y": float(n.get("y", 0)), "z": float(n.get("z", 0))},
        )
    for b in spec["bars"]:
        run(
            "create_bar",
            {"bar_id": int(b["id"]), "start_node": int(b["n1"]), "end_node": int(b["n2"]), "section_name": str(b["section"])},
        )
    for s in spec["supports"]:
        run("set_support", {"node_id": int(s["node"]), "support_type": str(s["type"])})
    run("create_load_case", {"case_id": 1, "case_name": "C1", "nature": "permanent"})


def solve_and_reactions():
    t0 = time.time()
    run("solve", {"timeout_s": 150})
    dt = time.time() - t0
    total, rows = reactions_fz_sum()
    return total, rows, dt
COMPOSE_STEPS = [
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


def deck_bars_of(geom):
    nmap = {int(n["id"]): (float(n.get("x", 0)), float(n.get("y", 0)), float(n.get("z", 0))) for n in geom["nodes"]}
    out = []
    for b in geom["bars"]:
        if not str(b.get("section")).startswith("IPE 500"):
            continue
        z1 = nmap[int(b["n1"])][2]
        z2 = nmap[int(b["n2"])][2]
        if abs(z1) < 1e-9 and abs(z2) < 1e-9:
            out.append(int(b["id"]))
    return out


# ---------------------------------------------------------------------------
print("=" * 72)
print("COINC AUDIT LIVE (E1-E7) - single Robot seat, OpenSeesPy ideal refs")
print("=" * 72)

# ---- E1: compose twin-arch + deck bar_uniform (Goal 1: compose path) --------
run("clear_structure", {"project_type": "3D"})
for st in COMPOSE_STEPS:
    run("compose_structure", {"action": "step", "step": st})
fin = run("compose_structure", {"action": "finish"})
geom = fin["geometry"]
print(f"  compose finish: {fin['counts']} merged_coincident={geom.get('__merged_coincident_nodes')}")
run("create_structure_from_spec", {"spec": geom})
run("create_load_case", {"case_id": 1, "case_name": "DECK", "nature": "permanent"})
deck = deck_bars_of(geom)
for b in deck:
    r = run("apply_bar_load", {"bar_id": b, "case_id": 1, "value_kn_m": -10.0, "direction": "Z"})
    assert r.get("method") == "bar_uniform_record", (b, r)
total, rows, dt = solve_and_reactions()
record("E1_compose_deck_uniform", abs(total - 600.0) / 600.0 * 100.0 < 1.0,
       f"sumFZ={total:.3f} vs 600.0 kN (open ref) err={pct(total, 600.0):.4f}% (solve {dt:.0f}s)",
       {"rows": rows, "method": "bar_uniform_record", "merged": geom.get("__merged_coincident_nodes")})

# ---- E2: hand-built S2 + apply_bar_load (protection on the skip-compose path) ---
build_spec(S2)
r1 = run("apply_bar_load", {"bar_id": 1, "case_id": 1, "value_kn_m": -10.0, "direction": "Z"})
r2 = run("apply_bar_load", {"bar_id": 2, "case_id": 1, "value_kn_m": -10.0, "direction": "Z"})
total, rows, dt = solve_and_reactions()
ok = str(r1.get("method")) == "nodal_lumped" and abs(total - 300.0) / 300.0 * 100.0 < 1.0
record("E2_S2_apply_bar_load", ok,
       f"method={r1.get('method')} sumFZ={total:.3f} vs 300.0 (ref) err={pct(total, 300.0):.4f}%",
       {"rows": rows, "r1_method": r1.get("method")})

# ---- E3: S2 + force_record=True (true records bypass the protection) ----------
build_spec(S2)
ex.robot.apply_bar_load(1, 1, -10.0, "Z", force_record=True)
ex.robot.apply_bar_load(2, 1, -10.0, "Z", force_record=True)
total, rows, dt = solve_and_reactions()
record("E3_S2_force_record", True,
       f"sumFZ={total:.3f} vs 300.0 (ref) shortfall={100.0 - total / 300.0 * 100.0:.2f}%",
       {"rows": rows})

# ---- E4: S2 + bar_concentrated (THE GAP: no coincident check) ------------------
build_spec(S2)
ex.robot.apply_bar_concentrated(1, 1, fz_kn=-50.0, ratio=0.5)
total, rows, dt = solve_and_reactions()
record("E4_S2_bar_concentrated", True,
       f"sumFZ={total:.3f} vs 50.0 (ref) shortfall={100.0 - total / 50.0 * 100.0:.2f}%",
       {"rows": rows})

# ---- E5: S2 + nodal load on the merged-away node 4 ------------------------------
build_spec(S2)
run("apply_nodal_load", {"node_id": 4, "case_id": 1, "fz_kn": -30.0})
total, rows, dt = solve_and_reactions()
record("E5_S2_nodal_at_node4", True,
       f"sumFZ={total:.3f} vs 30.0 (ref on surviving node) retained={100.0 * total / 30.0:.1f}%",
       {"rows": rows})

# ---- E6: S2 + apply_self_weight (nodal by design) -------------------------------
build_spec(S2)
sw = run("apply_self_weight", {"case_id": 1})
total, rows, dt = solve_and_reactions()
reported = float(sw.get("total_self_weight_kn", 0.0))
record("E6_S2_selfweight",
       str(sw.get("method")) == "nodal_lumped" and abs(total - reported) / reported * 100.0 < 1.0,
       f"method={sw.get('method')} reported={reported:.4f} sumFZ={total:.4f} err={pct(total, reported):.4f}%",
       {"rows": rows, "reported_total": reported})

# ---- E7: control - hand-built S3 (merged) + apply_bar_load -----------------------
build_spec(S3)
r1 = run("apply_bar_load", {"bar_id": 1, "case_id": 1, "value_kn_m": -10.0, "direction": "Z"})
run("apply_bar_load", {"bar_id": 2, "case_id": 1, "value_kn_m": -10.0, "direction": "Z"})
total, rows, dt = solve_and_reactions()
record("E7_S3_control",
       str(r1.get("method")) == "bar_uniform_record" and abs(total - 300.0) / 300.0 * 100.0 < 1.0,
       f"method={r1.get('method')} sumFZ={total:.3f} vs 300.0 err={pct(total, 300.0):.4f}%",
       {"rows": rows, "r1_method": r1.get("method")})

print("=" * 72)
print("SUMMARY")
for k, v in RESULTS.items():
    print(f"  [{'OK ' if v['ok'] else 'FAIL'}] {k}: {v['summary']}")
json.dump(RESULTS, open(r"batch/live_val_results/COINC_AUDIT_LIVE_RESULTS.json", "w"), indent=1)
ex.robot.close()
print("results written to COINC_AUDIT_LIVE_RESULTS.json; robot closed")
