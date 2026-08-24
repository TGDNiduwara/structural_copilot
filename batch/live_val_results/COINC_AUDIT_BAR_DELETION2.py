"""COINC_AUDIT_BAR_DELETION2.py - READ-ONLY: which ids vanished + did loads survive.

Rebuild + solve the twin-arch, then post-solve: (1) diff the live bar id set
against the 138 pre-solve ids to find which are truly gone, (2) read the
load records to see whether the -10 kN/m deck UDL records reference the OLD
(pre-solve) bar ids (dangling -> load lost) or the NEW renumbered ids.
"""
from __future__ import annotations
import json, math, sys, time

ROOT = r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot"
sys.path.insert(0, ROOT)
from win32com.client import CastTo
from agent.tool_registry import ToolExecutor
from tools.robot_tool import RobotEnum

STEPS = [
    {"op": "chord", "name": "arch_a", "kind": "arc", "span": 30.0, "rise": 5.0,
     "n_panels": 10, "elevation": 0.0, "plane": 0.0, "section": "IPE 500"},
    {"op": "chord", "name": "deck_a", "kind": "straight", "span": 30.0,
     "n_panels": 10, "elevation": 0.0, "plane": 0.0, "section": "IPE 500"},
    {"op": "web", "top": "arch_a", "bottom": "deck_a", "pattern": "pratt", "web_section": "L 80x80x8"},
    {"op": "copy", "source": "arch_a", "name": "arch_b", "y_shift": 6.0},
    {"op": "copy", "source": "deck_a", "name": "deck_b", "y_shift": 6.0},
    {"op": "web", "top": "arch_b", "bottom": "deck_b", "pattern": "pratt", "web_section": "L 80x80x8"},
    {"op": "bracing", "plane_a": "arch_a", "plane_b": "arch_b", "pattern": "cross", "section": "L 60x60x6"},
    {"op": "bracing", "plane_a": "deck_a", "plane_b": "deck_b", "pattern": "cross", "section": "L 60x60x6"},
    {"op": "support", "chain": "deck_a", "type": "pinned"},
    {"op": "support", "chain": "deck_b", "type": "pinned"},
]

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
run = lambda t, a: json.loads(ex.dispatch(t, a))
b = ex.robot

run("clear_structure", {"project_type": "3D"})
for st in STEPS:
    run("compose_structure", {"action": "step", "step": st})
fin = run("compose_structure", {"action": "finish"})
geom = fin["geometry"]
print(f"compose: {fin['counts']}")
run("create_structure_from_spec", {"spec": geom})
pre_ids = set(int(xb["id"]) for xb in geom["bars"])
print(f"pre-solve bar ids: {len(pre_ids)} (min {min(pre_ids)} max {max(pre_ids)})")

nmap = {int(n["id"]): (float(n.get("x", 0)), float(n.get("y", 0)), float(n.get("z", 0))) for n in geom["nodes"]}
deck = [int(xb["id"]) for xb in geom["bars"]
        if str(xb.get("section")).startswith("IPE 500")
        and abs(nmap[int(xb["n1"])][2]) < 1e-9 and abs(nmap[int(xb["n2"])][2]) < 1e-9]
run("create_load_case", {"case_id": 1, "case_name": "DECK", "nature": "permanent"})
for bid in deck:
    run("apply_bar_load", {"bar_id": bid, "case_id": 1, "value_kn_m": -10.0, "direction": "Z"})
t0 = time.time()
run("solve", {"timeout_s": 150})
print(f"solve {time.time()-t0:.0f}s")

coll = b.structure.Bars.GetAll()
n = int(coll.Count)
post_ids = set()
conn_of = {}
for i in range(1, n + 1):
    obj = coll.Get(i)
    bid = int(obj.Number)
    post_ids.add(bid)
print(f"post-solve live bar ids: {len(post_ids)}")
print(f"TRULY GONE (pre - post): {sorted(pre_ids - post_ids)}")
print(f"NEW (post - pre): {sorted(post_ids - pre_ids)[:20]}")

# load records: which bar ids do the UDL records target?
case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
records = case.Records
nrec = int(records.Count)
print(f"case records: {nrec}")
old_refs = set(); new_refs = set(); other_refs = set()
for i in range(1, nrec + 1):
    rec = records.Get(i)
    rt = int(rec.Type)
    if rt != RobotEnum.I_LRT_BAR_UNIFORM:
        continue
    vals = [float(rec.GetValue(j)) for j in (0, 1, 2)]
    kn = max(abs(vals[0]), abs(vals[1]), abs(vals[2]))
    for rid in b._record_object_ids(rec):
        if rid in pre_ids and rid not in post_ids:
            old_refs.add(rid)
        elif rid in post_ids:
            new_refs.add(rid)
        else:
            other_refs.add(rid)
print(f"UDL records reference OLD (vanished) ids: {sorted(old_refs)}")
print(f"UDL records reference LIVE ids: {sorted(new_refs)[:30]}...")
print(f"UDL records reference unknown ids: {sorted(other_refs)}")

# total load represented by the records on live ids vs on vanished ids
live_total = 0.0; gone_total = 0.0
for i in range(1, nrec + 1):
    rec = records.Get(i)
    rt = int(rec.Type)
    if rt != RobotEnum.I_LRT_BAR_UNIFORM:
        continue
    vals = [float(rec.GetValue(j)) for j in (0, 1, 2)]
    kn = max(abs(vals[0]), abs(vals[1]), abs(vals[2]))
    for rid in b._record_object_ids(rec):
        L = b._bar_length(rid)
        if rid in pre_ids and rid not in post_ids:
            gone_total += kn * L
        else:
            live_total += kn * L
print(f"UDL load on LIVE bars: {live_total:.3f} kN; on VANISHED bars: {gone_total:.3f} kN")

run("export_reactions", {"case_id": 1})
print(f"reaction sum(FZ) = {float(ex.reactions_df['FZ_kN'].sum()):.3f} kN")
ex.robot.close()
print("robot closed")