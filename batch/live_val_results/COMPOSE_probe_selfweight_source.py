"""COMPOSE_probe_selfweight_source.py - WHERE does the 125.692 kN come from?

Decisive isolation experiment (the user's instruction #2):
  Phase A: build the twin-arch + a load case, SOLVE with NO explicit loads.
           reactions(A) tells us if Robot's automatic self-weight is on
           by default (if ~125.692 kN -> yes, auto self-weight is the
           source and apply_self_weight's records are NOT driving results).
  Phase B: apply_self_weight, dump the RAW record fields for one bar and
           re-read ALL records correctly, SOLVE again.
           reactions(B) tells us what the explicit records actually add.
"""
from __future__ import annotations
import json
import re
import sys
import time

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
    fin = run_tool("compose_structure", {"action": "finish"})
    return fin["geometry"]


def reactions():
    run_tool("export_reactions", {"case_id": 1})
    df = ex.reactions_df
    fz = float(df["FZ_kN"].sum()) if "FZ_kN" in df.columns else float("nan")
    print("  reactions:")
    print(df.to_string())
    return fz


def read_all_load_records():
    """Correct readback of every uniform-load record (ObjectList via Text)."""
    case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
    rec_count = int(case.Records.Count)
    bars: set = set()
    total = 0.0
    samples = []
    for i in range(1, rec_count + 1):
        rec = case.Records.Get(i)
        try:
            rtype = int(rec.Type)
        except Exception:
            continue
        if rtype != 4:
            continue
        rv = CastTo(rec, "IRobotBarUniformRecord")
        pz = float(rv.Values.GetValue(2))
        try:
            txt = str(rv.Objects.Text or "")
        except Exception:
            txt = ""
        ids = [int(x) for x in re.findall(r"\d+", txt)]
        for bid in ids:
            bars.add(bid)
            total += pz * b._bar_length(bid)
        if len(samples) < 3:
            samples.append((i, pz, txt))
    return rec_count, bars, total, samples


print("=" * 72)
print("PROBE: is Robot's automatic self-weight the real source?")
print("=" * 72)

run_tool("clear_structure", {"project_type": "3D"})
geom = compose_twin_arch()
print("composed:", len(geom["nodes"]), "nodes,", len(geom["bars"]), "bars")
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "SW",
                              "nature": "permanent"})

# ---- PHASE A: solve with NO explicit loads ---------------------------------
print("\n[PHASE A] solve with NO explicit loads (case 1 exists only):")
t0 = time.time()
solA = run_tool("solve", {})
print("  solve:", solA.get("status"), f"({time.time()-t0:.1f}s)")
RA = reactions()
print(f"  -> sum(FZ) with NO explicit loads = {RA:.3f} kN")
print(f"  -> if this ~= 125.69, Robot auto self-weight is the source "
      f"(apply_self_weight writes are NOT driving results)")

# ---- PHASE B: FRESH model + apply_self_weight BEFORE any solve ---------------
print("\n[PHASE B] fresh model + apply_self_weight + raw record dump:")
run_tool("clear_structure", {"project_type": "3D"})
geom2 = compose_twin_arch()
run_tool("create_structure_from_spec", {"spec": geom2})
run_tool("create_load_case", {"case_id": 1, "case_name": "SW",
                              "nature": "permanent"})
sw = run_tool("apply_self_weight", {"case_id": 1})
print("  tool total_self_weight_kn:", sw.get("total_self_weight_kn"),
      " bars:", sw.get("bars"))
# dump the raw record for one deck chord bar (bar id 12 if it exists)
case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
for i in range(1, int(case.Records.Count) + 1):
    rec = case.Records.Get(i)
    try:
        rv = CastTo(rec, "IRobotBarUniformRecord")
    except Exception:
        continue
    txt = str(rv.Objects.Text or "")
    if "12" in txt.split():
        print("  RAW record for bar 12:")
        print("    Type:", int(rec.Type))
        for idx, name in ((0, "PX"), (1, "PY"), (2, "PZ")):
            try:
                print(f"    Values.GetValue({idx}) [{name}]:",
                      rv.Values.GetValue(idx))
            except Exception as e:
                print(f"    Values.GetValue({idx}) err:", e)
        print("    Objects.Text:", txt)
        try:
            print("    Objects.Count:", rv.Objects.Count)
        except Exception as e:
            print("    Objects.Count err:", e)
        break
nrec, bars, total, samples = read_all_load_records()
print(f"  ALL records: count={nrec}, bars loaded={len(bars)}/138, "
      f"sum(PZ*L)={total:.3f} kN")
for s in samples:
    print(f"    record #{s[0]}: PZ={s[1]:+.4f}  objects={s[2]!r}")
t0 = time.time()
solB = run_tool("solve", {})
print("  solve:", solB.get("status"), f"({time.time()-t0:.1f}s)")
RB = reactions()
print(f"  -> sum(FZ) with explicit records = {RB:.3f} kN")

print("\n=== INTERPRETATION ===")
print(f"  Phase A (no loads)          = {RA:.3f} kN")
print(f"  Phase B (explicit records)  = {RB:.3f} kN")
print(f"  tool's computed total       = {sw.get('total_self_weight_kn')} kN")
if abs(RA - RB) < 0.5:
    print("  => explicit load records add ~NOTHING to reactions -> the "
          "125.69 kN comes from Robot's own automatic self-weight; "
          "apply_self_weight writes are either wrong-case, wrong-record-type "
          "or silently ignored.")
elif abs(RB - 149.07) < 1.5:
    print("  => explicit records ARE the only source and now balance.")
else:
    print("  => neither: records add", RB - RA, "kN but tool says",
          sw.get("total_self_weight_kn"))