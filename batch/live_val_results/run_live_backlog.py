"""
run_live_backlog.py - live verification of the 6 COM paths built this
session, run AFTER the live-validation chain writes CHAIN_DONE (one-seat
Robot license: the chain holds it until then).

Each check opens/closes its own HeadlessSession, prints PASS/FAIL with
actual numbers and writes live_check_N.json. Exit code = number of failed
checks.

"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
VAL = os.path.join(ROOT, "batch", "live_val_results")
sys.path.insert(0, ROOT)

from batch.headless_driver import HeadlessSession  # noqa: E402
from tools.robot_tool import RobotBridge, RobotEnum  # noqa: E402

RESULTS = {}


def _finish(n, payload):
    RESULTS[n] = payload
    with open(os.path.join(VAL, f"live_check_{n}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    ok = payload.get("ok")
    print(f"[check {n}] {'PASS' if ok else 'FAIL'} - {payload.get('summary', '')}",
          flush=True)


# ------------------------------------------------------------------ #
# 1. export_structure_spec round-trip
# ------------------------------------------------------------------ #
def check_export_spec():
    with HeadlessSession(visible=False) as s:
        bridge = s.bridge
        geom = RobotBridge.truss_spec(span=6.0, height=1.5, panels=3)
        s.clear_structure("3D")
        s.build_from_spec(geom)
        bridge.create_load_case(1, "DL", RobotEnum.I_CN_PERMANENT)
        geo = bridge.get_model_geometry()
        bottoms = sorted(nid for nid, c in geo["nodes"].items()
                         if abs(c[2]) < 1e-9)
        load_node = bottoms[len(bottoms) // 2]
        bridge.apply_nodal_load(load_node, 1, fz_kn=-10.0)
        s.solve_all(["static"])
        w1 = s.get_weight()["weight_kg"]
        u1 = s.get_utilization_summary(1)["max_utilization"]

        exp = bridge.export_structure_spec()
        exp_nodes = {(int(n["id"]), round(float(n["x"]), 6),
                      round(float(n["z"]), 6)) for n in exp["nodes"]}
        in_nodes = {(int(n["id"]), round(float(n["x"]), 6),
                    round(float(n["z"]), 6)) for n in geom["nodes"]}
        exp_bars = {(int(b["id"]), int(b["n1"]), int(b["n2"]),
                     str(b["section"])) for b in exp["bars"]}
        in_bars = {(int(b["id"]), int(b["n1"]), int(b["n2"]),
                    str(b["section"])) for b in geom["bars"]}
        exp_supp = {(int(x["node"]), str(x["type"])) for x in exp["supports"]}
        in_supp = {(int(x["node"]), str(x["type"])) for x in geom["supports"]}
        has_case = any(c["id"] == 1 for c in exp["cases"])
        has_load = any(l.get("kind") == "nodal" and l.get("node") == load_node
                       for l in exp["loads"])

        # rebuild from the exported spec and re-solve
        s.clear_structure("3D")
        s.build_from_spec(exp)
        s.solve_all(["static"])
        w2 = s.get_weight()["weight_kg"]
        u2 = s.get_utilization_summary(1)["max_utilization"]

        same = (exp_nodes == in_nodes and exp_bars == in_bars
                and exp_supp == in_supp and has_case and has_load)
        w_err = abs(w2 - w1) / max(w1, 1e-9)
        u_err = abs((u2 or 0.0) - (u1 or 0.0))
        ok = same and w_err < 0.005 and u_err < 0.001
        _finish(1, {
            "ok": ok, "nodes_match": exp_nodes == in_nodes,
            "bars_match": exp_bars == in_bars, "supports_match": exp_supp == in_supp,
            "case_roundtrip": has_case, "load_roundtrip": has_load,
            "weight_before_kg": w1, "weight_after_rebuild_kg": w2,
            "weight_rel_err": round(w_err, 6),
            "util_before": u1, "util_after": u2, "util_abs_err": round(u_err, 6),
            "summary": f"truss {len(exp['nodes'])} nodes/{len(exp['bars'])} bars "
                       f"round-tripped; weight {w1}->{w2} kg",
        })

# ------------------------------------------------------------------ #
# 2. validate_stability live delegation
# ------------------------------------------------------------------ #
def check_stability():
    portal = {
        "project": "3D",
        "nodes": [{"id": 1, "x": 0, "z": 0}, {"id": 2, "x": 0, "z": 3},
                  {"id": 3, "x": 6, "z": 3}, {"id": 4, "x": 6, "z": 0}],
        "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "HEA 200"},
                 {"id": 2, "n1": 2, "n2": 3, "section": "IPE 300"},
                 {"id": 3, "n1": 3, "n2": 4, "section": "HEA 200"}],
        "supports": [{"node": 1, "type": "pinned"},
                     {"node": 4, "type": "pinned"}],
    }
    with HeadlessSession(visible=False) as s:
        s.clear_structure("3D")
        s.build_from_spec(portal)
        r1 = s.validate_stability()          # delegates to the bridge
        s.bridge.create_node(999, 50.0, 0.0, 50.0)   # floating node
        r2 = s.validate_stability()
    ok = (r1.get("ok") is True and "no mechanism detected" in r1["message"]
          and r2.get("ok") is False and r2.get("mechanism") is True
          and 999 in (r2.get("nodes") or []))
    _finish(2, {
        "ok": ok, "stable": r1, "mechanism": r2,
        "summary": f"stable '{r1['message']}' | mechanism '{r2['message']}'",
    })


# ------------------------------------------------------------------ #
# 3. apply_self_weight
# ------------------------------------------------------------------ #
def check_self_weight():
    with HeadlessSession(visible=False) as s:
        bridge = s.bridge
        s.clear_structure("3D")
        bridge.create_node(1, 0, 0, 0)
        bridge.create_node(2, 6, 0, 0)
        bridge.create_bar(1, 1, 2, "IPE 300")
        bridge.set_support(1, "pinned")
        bridge.set_support(2, "pinned")
        bridge.create_load_case(1, "SW", RobotEnum.I_CN_PERMANENT)
        summ = bridge.apply_self_weight(1)
        s.solve_all(["static"])
        reac = bridge.export_reactions(case_id=1)
        total_reac = float(reac["FZ_kN"].abs().sum())
        exp = bridge.export_structure_spec()
        applied = [l for l in exp["loads"] if l["kind"] == "bar_uniform"]

    table_kg_m = RobotBridge._SECTION_UNIT_MASS_TABLE["IPE300"]
    hand_kn_m = table_kg_m * 9.81 / 1000.0
    hand_total = hand_kn_m * 6.0
    applied_val = applied[0]["value"] if applied else None
    unit = summ["per_bar"][0]["load_kn_m"]
    ok = (abs(unit - hand_kn_m) / hand_kn_m < 0.01
          and abs(total_reac - hand_total) / hand_total < 0.01
          and applied_val is not None
          and abs(abs(applied_val) - hand_kn_m) / hand_kn_m < 0.01)
    _finish(3, {
        "ok": ok, "unit_mass_kg_m": table_kg_m, "hand_kn_m": round(hand_kn_m, 6),
        "applied_load_kn_m": applied_val, "total_hand_kn": round(hand_total, 6),
        "total_reaction_kn": round(total_reac, 6),
        "summary": f"{table_kg_m} kg/m -> {hand_kn_m:.5f} kN/m, applied "
                   f"{applied_val}, reactions {total_reac:.4f} kN vs "
                   f"{hand_total:.4f} kN",
    })

# ------------------------------------------------------------------ #
# 4. spring supports
# ------------------------------------------------------------------ #
def check_spring():
    with HeadlessSession(visible=False) as s:
        bridge = s.bridge
        s.clear_structure("3D")
        bridge.create_node(1, 0, 0, 0)
        bridge.create_node(2, 0, 0, 1)
        bridge.create_bar(1, 1, 2, "IPE 200")
        bridge.set_support(1, "spring",
                           {"UX": 1e12, "UY": 1e12, "UZ": 5000.0})
        bridge.create_load_case(1, "P", RobotEnum.I_CN_IMPOSED)
        bridge.apply_nodal_load(2, 1, fz_kn=-10.0)
        s.solve_all(["static"])
        disp = bridge.export_node_displacements(case_id=1)
        uz = float(disp.loc[disp["Node_ID"] == 2, "UZ_m"].iloc[0])

    a = 2.85e-3     # IPE 200 area m2 (nominal)
    e = 210e6       # kN/m2
    k = 5000.0      # kN/m spring
    target = 10.0 * (1.0 / k + 1.0 / (e * a))   # metres
    err = abs(abs(uz) - target) / target
    # Tightened pass band: ±10% (was ±30%). ±30% only proved the spring was
    # not dead (>100x failure); ±10% also catches smaller real errors such
    # as a kN/m vs N/m unit mismatch in any of KX/KY/KZ (would shift tip
    # deflection ~1000x) or a K value misapplied out of band. Hand-calc
    # target and comparison logic are intentionally unchanged.
    tolerance = 0.10
    ok = err < tolerance
    _finish(4, {
        "ok": ok, "uz_m": uz, "target_m": round(target, 7),
        "rel_err": round(err, 4), "tolerance_band": tolerance,
        "summary": f"tip UZ {uz*1000:.3f} mm vs hand F/K={target*1000:.3f} mm "
                   f"(spring K=5000 kN/m, {tolerance*100:.0f}% band)",
    })


# ------------------------------------------------------------------ #
# 5. generate_code_combinations
# ------------------------------------------------------------------ #
def check_combinations():
    with HeadlessSession(visible=False) as s:
        bridge = s.bridge
        s.clear_structure("3D")
        bridge.create_node(1, 0, 0, 0)
        bridge.create_node(2, 6, 0, 0)
        bridge.create_bar(1, 1, 2, "IPE 300")
        bridge.set_support(1, "pinned")
        bridge.set_support(2, "pinned")
        bridge.create_load_case(1, "DL", RobotEnum.I_CN_PERMANENT)
        bridge.create_load_case(2, "LL", RobotEnum.I_CN_IMPOSED)
        bridge.apply_bar_load(1, 1, -10.0, "Z")
        bridge.apply_bar_load(1, 2, -5.0, "Z")
        plans = RobotBridge.eurocode_combination_factors(
            [(1, "permanent"), (2, "imposed")], "ULS_SLS_basic")
        for p in plans:
            bridge.define_combination(p["name"], p["case_factors"],
                                      p["combination_type"])
        ids = {}
        for num, obj in bridge._iter_all_cases():
            if bridge._as_combination(obj) is not None:
                ids[str(obj.Name)] = int(num)
        s.solve_all(["static"])
        measured = {}
        for name, expect in (("ULS_2", 94.5), ("SLS_char", 67.5)):
            df = bridge.export_all_member_forces(case_id=ids[name],
                                                 divisions=4)
            mid = df[(df["Position_m"] - 3.0).abs() < 0.01]
            measured[name] = float(mid["MY_kNm"].abs().max())

    checks = {name: abs(measured[name] - expect) / expect < 0.01
              for name, expect in (("ULS_2", 94.5), ("SLS_char", 67.5))}
    ok = all(checks.values())
    _finish(5, {
        "ok": ok, "created_ids": ids, "measured": measured,
        "expected": {"ULS_2": 94.5, "SLS_char": 67.5},
        "checks": checks,
        "summary": f"ULS_2 midspan {measured['ULS_2']:.2f} kNm (exp 94.5), "
                   f"SLS_char {measured['SLS_char']:.2f} kNm (exp 67.5)",
    })

# ------------------------------------------------------------------ #
# 6. compare_topologies live
# ------------------------------------------------------------------ #
def check_compare_topologies():
    from batch.topology_compare import compare_topologies
    db = os.path.join(VAL, "live_compare_runs.db")
    log = os.path.join(VAL, "live_compare.log")
    variants = [
        {"name": "truss", "generator": "create_truss",
         "generator_args": {"span": 6.0, "height": 1.5, "panels": 3}},
        {"name": "frame", "generator": "create_braced_frame",
         "generator_args": {"height": 3.0, "width": 6.0}},
    ]
    load_spec = {
        "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
        "loads": [{"kind": "bar_uniform", "bar": 1, "case": 1,
                   "direction": "Z", "value": -10.0}],
    }
    sizing = {"column": ["HEA 200"], "beam": ["IPE 300", "IPE 330"],
              "brace": ["L 100"]}
    result = compare_topologies(variants, load_spec, sizing=sizing,
                                db_path=db, log_path=log)
    ranked = result["variants"]
    ok = (result["status"] == "ok" and len(ranked) == 2
          and all(v["weight_kg"] is not None for v in ranked)
          and ranked[0]["weight_kg"] <= ranked[1]["weight_kg"])
    _finish(6, {
        "ok": ok, "ranked": [
            {"name": v["name"], "run_id": v["run_id"],
             "weight_kg": v["weight_kg"],
             "max_utilization": v["max_utilization"],
             "sections": v["sections"],
             "grid_candidates": v["grid_candidates"]}
            for v in ranked],
        "summary": " | ".join(
            f"{v['name']}={v['weight_kg']}kg(run {v['run_id']})" for v in ranked),
    })


def wait_for_chain_done(timeout_h: float = 10.0) -> bool:
    marker = os.path.join(VAL, "CHAIN_DONE")
    deadline = time.time() + timeout_h * 3600.0
    print(f"waiting for {marker} (up to {timeout_h}h)...", flush=True)
    while time.time() < deadline:
        if os.path.exists(marker):
            print("CHAIN_DONE found - seat free.", flush=True)
            return True
        time.sleep(60)
    print("TIMEOUT waiting for CHAIN_DONE.", flush=True)
    return False


def main():
    print("=" * 72)
    print("Live backlog verification (6 COM paths)")
    print("=" * 72)
    if not wait_for_chain_done():
        sys.exit(1)
    checks = [check_export_spec, check_stability, check_self_weight,
              check_spring, check_combinations, check_compare_topologies]
    for i, fn in enumerate(checks, start=1):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            _finish(i, {"ok": False, "summary": f"exception: {exc}"})
    failed = sum(1 for n in range(1, 7)
                 if not RESULTS.get(n, {}).get("ok"))
    print(f"\nBACKLOG: {6 - failed}/6 checks passed.")
    with open(os.path.join(VAL, "LIVE_BACKLOG_DONE"), "w",
              encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()



