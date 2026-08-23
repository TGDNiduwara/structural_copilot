"""COMPOSE_LIVE_twin_arch.py - the offline-compose -> REAL Robot gate.

Builds a twin-arch bridge ENTIRELY through the compose_structure tool
(one step per call, exactly as the LLM would), passes the finished geometry
to create_structure_from_spec, then: check_model_stability -> solve ->
reactions balance the applied load within the same 2% tolerance used for
every other live equilibrium check in this validation chain.

Run:  .\\venv\\Scripts\\python.exe batch\\live_val_results\\COMPOSE_LIVE_twin_arch.py
"""
from __future__ import annotations
import json
import os
import sys
import time

ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)

from agent.tool_registry import ToolExecutor  # noqa: E402

EVIDENCE = os.path.join(ROOT, "batch", "live_val_results",
                        "EVIDENCE_4_compose_live_twin_arch.json")
TOLERANCE = 0.02  # 2% reaction-vs-applied balance (same as all other live checks)

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
bridge = ex.robot
steps_log = []


def step(name, detail, ok=True):
    steps_log.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: {detail}")


def run_tool(tool, args):
    return json.loads(ex.dispatch(tool, args))


print("=" * 72)
print("COMPOSE_LIVE: twin-arch bridge via compose_structure (one step per call)")
print("=" * 72)

# -- 0. seat / session sanity ------------------------------------------------
before = run_tool("robot_session_status", {})
step("robot_session_status before", str(before.get("session"))[:120])
# fresh blank model so re-runs never inherit a previous build
run_tool("clear_structure", {"project_type": "3D"})

# -- 1. compose the twin arch, ONE STEP PER CALL (the reliability rule) ------
COMPOSE_STEPS = [
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
for i, st in enumerate(COMPOSE_STEPS, 1):
    r = run_tool("compose_structure", {"action": "step", "step": st})
    step(f"compose step {i} ({st['op']} {st.get('name', '')})",
         r.get("message", str(r)))

fin = run_tool("compose_structure", {"action": "finish"})
step("compose finish", fin.get("message", str(fin)), fin.get("status") == "ok")
geom = fin.get("geometry")
if not geom:
    print("NO GEOMETRY - aborting")
    sys.exit(1)
step("composed geometry counts",
     str(fin.get("counts")), fin.get("counts", {}).get("nodes") == 44)

# -- 2. build through the real Robot ------------------------------------------
build = run_tool("create_structure_from_spec", {"spec": geom})
step("create_structure_from_spec", str(build.get("message", build))[:160])
time.sleep(1.0)
summary = run_tool("get_structure_summary", {})
step("get_structure_summary", str(summary)[:200])
bar_count = summary.get("bar_count", summary.get("bars", -1))
node_count = summary.get("node_count", summary.get("nodes", -1))
if int(bar_count) != 142:
    print("  !! expected 142 bars in Robot, got", bar_count)

# -- 3. load case + self-weight ------------------------------------------------
run_tool("create_load_case", {"case_id": 1, "case_name": "SW",
                              "nature": "permanent"})
sw = run_tool("apply_self_weight", {"case_id": 1})
sw_total = float(sw.get("total_self_weight_kn",
                        sw.get("self_weight_total_kn", 0.0)))
step("apply_self_weight", f"self_weight_total_kn={sw_total:.4f}",
     sw_total > 0.0)
per_bar = sw.get("per_bar") or []
by_sec: dict = {}
for pb in per_bar:
    by_sec.setdefault(pb.get("section"), [0, 0.0])
    by_sec[pb.get("section")][0] += 1
    by_sec[pb.get("section")][1] += pb.get("weight_kn", 0.0)
print("  per-section self-weight breakdown:")
for sec, (cnt, wt) in sorted(by_sec.items()):
    print(f"    {sec:<12} bars={cnt:<4} weight={wt:8.3f} kN")
print(f"    TOTAL                      bars={len(per_bar)} weight={sw_total:.3f} kN")

# cross-check: do the LIVE Robot bar endpoints match the composed spec?
live_spec = run_tool("export_structure_spec", {})
live_bars = live_spec.get("bars") or []
spec_by_id = {br["id"]: (br["n1"], br["n2"]) for br in geom["bars"]}
mismatch = 0
for br in live_bars:
    if spec_by_id.get(br["id"]) != (br["n1"], br["n2"]):
        mismatch += 1
step("live bar endpoints match composed spec",
     f"{len(live_bars)} live bars, {mismatch} endpoint mismatches vs spec",
     len(live_bars) == 142 and mismatch == 0)

# -- 4. mechanism pre-check ----------------------------------------------------
st = run_tool("check_model_stability", {})
step("check_model_stability", f"ok={st.get('ok')} {st.get('message', '')}",
     st.get("ok") is True)

# -- 5. solve ------------------------------------------------------------------
t0 = time.time()
sol = run_tool("solve", {})
step("solve", f"{sol.get('status')} {sol.get('message', '')[:120]} "
     f"({time.time() - t0:.1f}s)", sol.get("status") in ("ok", "ok_with_warning"))
if sol.get("warning"):
    print("  warning:", str(sol["warning"])[:200])

# -- 6. reactions balance applied load -----------------------------------------
run_tool("export_reactions", {"case_id": 1})
df = ex.reactions_df
print("  reactions table:")
print(df.to_string())
sum_fz = float(df["FZ_kN"].sum()) if "FZ_kN" in df.columns else None
if sum_fz is None:
    cols = [c for c in df.columns if "FZ" in str(c)]
    sum_fz = float(df[cols[0]].sum()) if cols else float("nan")
rel_err = abs(abs(sum_fz) - sw_total) / sw_total
step("reactions balance applied load",
     f"sum(FZ)={sum_fz:.4f} kN vs applied {sw_total:.4f} kN, "
     f"rel err {rel_err * 100:.2f}%", rel_err <= TOLERANCE)

# -- 7. report -----------------------------------------------------------------
verdict = all(s["ok"] for s in steps_log)
print("=" * 72)
print("LIVE VERDICT:", "PASS" if verdict else "FAIL",
      "- compose_structure -> Robot -> solve -> equilibrium")
print("=" * 72)
evidence = {
    "name": "EVIDENCE_4_compose_live_twin_arch",
    "verdict": "PASS" if verdict else "FAIL",
    "tolerance": TOLERANCE,
    "geometry": {
        "compose_steps": len(COMPOSE_STEPS),
        "counts": fin.get("counts"),
        "robot_summary": summary,
    },
    "self_weight_kn": sw_total,
    "sum_reactions_fz_kn": sum_fz,
    "reaction_rel_err": rel_err,
    "steps": steps_log,
}
with open(EVIDENCE, "w", encoding="utf-8") as fh:
    json.dump(evidence, fh, indent=2, default=str)
print("evidence written:", EVIDENCE)
sys.exit(0 if verdict else 1)