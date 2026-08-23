"""COMPOSE_probe_readback.py - read every load record CORRECTLY.

Uses the proven readback pattern (record.GetValue + _record_object_ids) to
compute the ACTUAL load Robot holds, bar by bar, and compare to the tool's
total. Settles whether the records are wrong (value/target) or the reactions
read is inconsistent.
"""
from __future__ import annotations
import json
import sys

ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)

from agent.tool_registry import ToolExecutor
from win32com.client import CastTo
from tools.robot_tool import RobotEnum

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
print("PROBE: correct readback of every load record")
print("=" * 72)

run_tool("clear_structure", {"project_type": "3D"})
geom = compose_twin_arch()
run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "SW",
                              "nature": "permanent"})
sw = run_tool("apply_self_weight", {"case_id": 1})
tool_total = float(sw.get("total_self_weight_kn"))
per_bar = {int(p["bar_id"]): p for p in (sw.get("per_bar") or [])}
print("tool total:", tool_total, "bars:", sw.get("bars"))

case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
records = case.Records
n = int(records.Count)
print("case records:", n)

record_total = 0.0
bars_in_records: set = set()
by_rtype = {}
samples = []
for i in range(1, n + 1):
    rec = records.Get(i)
    rt = int(rec.Type)
    by_rtype[rt] = by_rtype.get(rt, 0) + 1
    if rt != RobotEnum.I_LRT_BAR_UNIFORM:
        continue
    vals = [float(rec.GetValue(j)) for j in (0, 1, 2)]
    objs = b._record_object_ids(rec)
    for bid in objs:
        bars_in_records.add(bid)
        kn_m = max(abs(vals[0]), abs(vals[1]), abs(vals[2]))
        L = b._bar_length(bid)
        record_total += kn_m * L
    if len(samples) < 5:
        samples.append((i, rt, vals, objs))
print("  record types present:", by_rtype)
for s in samples:
    print(f"    record #{s[0]}: type={s[1]} values(X,Y,Z)={s[2]} bars={s[3]}")

# per-section totals from the actual records
spec_coords = {nd["id"]: (nd["x"], nd["y"], nd["z"]) for nd in geom["nodes"]}
spec_ends = {br["id"]: (br["n1"], br["n2"]) for br in geom["bars"]}
sec_of_bar = {br["id"]: br["section"] for br in geom["bars"]}
sec_totals = {}
for i in range(1, n + 1):
    rec = records.Get(i)
    rt = int(rec.Type)
    if rt != RobotEnum.I_LRT_BAR_UNIFORM:
        continue
    vals = [float(rec.GetValue(j)) for j in (0, 1, 2)]
    kn_m = max(abs(vals[0]), abs(vals[1]), abs(vals[2]))
    for bid in b._record_object_ids(rec):
        L = b._bar_length(bid)
        sec = sec_of_bar.get(bid, "?")
        sec_totals[sec] = sec_totals.get(sec, 0.0) + kn_m * L

print(f"  sum(|PZ| x spec_length) over records = {record_total:.4f} kN")
print(f"  tool total_self_weight_kn            = {tool_total:.4f} kN")
print(f"  bars referenced by records: {len(bars_in_records)} of 138")
print("  per-section total from records:")
for sec, t in sorted(sec_totals.items()):
    print(f"    {sec:<12} {t:8.3f} kN")
print("  per-section total from tool per_bar:")
from collections import defaultdict
pb_sec = defaultdict(float)
for p in per_bar.values():
    pb_sec[p["section"]] += p["weight_kn"]
for sec, t in sorted(pb_sec.items()):
    print(f"    {sec:<12} {t:8.3f} kN")
