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
if int(bar_count) != 138:
    print("  !! expected 138 bars in Robot (142 - 4 degenerate end verticals), got", bar_count)

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
live_geom = live_spec.get("geometry") or {}
live_bars = live_geom.get("bars") or []
live_nodes = live_geom.get("nodes") or []
spec_by_id = {br["id"]: (br["n1"], br["n2"]) for br in geom["bars"]}
mismatch = 0
missing = 0
for br in live_bars:
    if br["id"] not in spec_by_id:
        missing += 1
    elif spec_by_id[br["id"]] != (br["n1"], br["n2"]):
        mismatch += 1
step("live bar endpoints match composed spec",
     f"{len(live_bars)} live bars (spec has {len(geom['bars'])}), "
     f"{missing} unknown ids, {mismatch} endpoint mismatches",
     len(live_bars) == 138 and mismatch == 0 and missing == 0)

# live node/coordinate verification (the source of truth for the earlier
# support-node anomaly: reactions reported nodes 21/31/103/113, NOT our deck
# ends 1/11/22/32 - dump what Robot actually holds and compare to the spec).
from tools.robot_tool import RobotEnum
b = ex.robot
nc = b.structure.Nodes.GetAll()
live_nid_coords = {}
for i in range(1, int(nc.Count) + 1):
    nd = nc.Get(i)
    live_nid_coords[int(nd.Number)] = (
        round(float(nd.X), 6), round(float(nd.Y), 6), round(float(nd.Z), 6))
print("  live node ids:", sorted(live_nid_coords))
print("  live node count:", len(live_nid_coords))
# nodes that CARRY a support label, with coordinates
sup_nodes = []
for nid, c in live_nid_coords.items():
    try:
        lbl = b.structure.Nodes.Get(nid).GetLabelName(RobotEnum.I_LT_SUPPORT)
    except Exception:
        lbl = None
    if lbl:
        sup_nodes.append((nid, c, lbl))
print("  live nodes WITH support label (id, coords, label):")
for row in sup_nodes:
    print("   ", row)
# compare spec node coords vs live coords by id
spec_nid = {nd["id"]: (nd["x"], nd["y"], nd["z"]) for nd in geom["nodes"]}
coord_mismatch = [
    nid for nid, c in live_nid_coords.items()
    if nid in spec_nid and spec_nid[nid] != c]
print(f"  node coordinate mismatches vs spec: {len(coord_mismatch)} "
      f"({coord_mismatch[:10]})")
# the intended support coordinates must carry supports in the live model
intended_support_coords = {(0.0, 0.0, 0.0), (30.0, 0.0, 0.0),
                           (0.0, 6.0, 0.0), (30.0, 6.0, 0.0)}
supported_coords = {c for _nid, c, _lbl in sup_nodes}
step("intended deck-end supports present in live model",
     f"supported coords={sorted(supported_coords)}",
     intended_support_coords.issubset(supported_coords))

# degenerate-geometry check: any composed bars whose endpoints are coincident
# (the arch ends touch the deck ends at z=0 -> zero-length end verticals)?
spec_coords = {nd["id"]: (nd["x"], nd["y"], nd["z"]) for nd in geom["nodes"]}
zero_len = []
for br in geom["bars"]:
    if spec_coords[br["n1"]] == spec_coords[br["n2"]]:
        zero_len.append(br["id"])
print(f"  composed bars with COINCIDENT endpoints: {zero_len} "
      f"({len(zero_len)} total)")
step("no degenerate zero-length bars in composed spec",
     f"{len(zero_len)} zero-length bar(s): {zero_len[:12]}",
     not zero_len)

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

# -- 5b. what load records does Robot ACTUALLY hold? ---------------------------
from win32com.client import CastTo
case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
rec_count = int(case.Records.Count)
bars_with_load: set = set()
applied_kn = 0.0
bad = 0
for i in range(1, rec_count + 1):
    rec = case.Records.Get(i)
    try:
        rtype = int(rec.Type)
    except Exception:
        rtype = -1
    if rtype != 4:  # I_LRT_BAR_UNIFORM
        continue
    rv = CastTo(rec, "IRobotBarUniformRecord")
    pz = float(rv.Values.GetValue(2))  # I_BURV_PZ
    rng = rv.Objects
    obj_count = int(rng.Count)
    for j in range(1, obj_count + 1):
        try:
            bid = int(rng.Get(j))
        except Exception:
            bad += 1
            continue
        bars_with_load.add(bid)
        L = b._bar_length(bid)
        if L <= 0.0:
            bad += 1
        applied_kn += pz * L
print(f"  Robot load records: {rec_count}; bars WITH a registered load: "
      f"{len(bars_with_load)} of 138; sum(PZ*L) from records = {applied_kn:.3f} kN "
      f"(bad/zero-length refs: {bad})")
missing = sorted(set(range(1, 139)) - bars_with_load)
print(f"  bar ids with NO registered load: {missing[:20]}{'...' if len(missing) > 20 else ''} "
      f"({len(missing)} total)")
step("all 138 bars carry a registered load",
     f"{len(bars_with_load)}/138 bars loaded, records={rec_count}",
     len(bars_with_load) == 138 and applied_kn > 0)
step("Robot-registered load matches tool total",
     f"robot sum(PZ*L)={applied_kn:.3f} kN vs tool total {sw_total:.3f} kN",
     abs(applied_kn - sw_total) / sw_total <= TOLERANCE)

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