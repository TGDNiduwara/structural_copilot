"""COINC_AUDIT_BAR_DELETION3.py - single-session definitive delete-vs-renumber."""
from __future__ import annotations
import json, math, sys, time
ROOT = r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot"
sys.path.insert(0, ROOT)
from win32com.client import CastTo
from agent.tool_registry import ToolExecutor
from tools.robot_tool import RobotEnum

STEPS = [
    {"op": "chord", "name": "arch_a", "kind": "arc", "span": 30.0, "rise": 5.0, "n_panels": 10, "elevation": 0.0, "plane": 0.0, "section": "IPE 500"},
    {"op": "chord", "name": "deck_a", "kind": "straight", "span": 30.0, "n_panels": 10, "elevation": 0.0, "plane": 0.0, "section": "IPE 500"},
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
run("create_structure_from_spec", {"spec": geom})
print("built")

def scan(b, label):
    coll = b.structure.Bars.GetAll()
    n = int(coll.Count)
    rows = {}
    for i in range(1, n + 1):
        num = int(coll.Get(i).Number)
        bar = CastTo(b.structure.Bars.Get(num), "IRobotBar")
        sec = str(bar.GetLabel(RobotEnum.I_LT_BAR_SECTION).Name)
        n1, n2 = int(bar.StartNode), int(bar.EndNode)
        n1o = CastTo(b.structure.Nodes.Get(n1), "IRobotNode")
        n2o = CastTo(b.structure.Nodes.Get(n2), "IRobotNode")
        p1 = (round(float(n1o.X), 6), round(float(n1o.Y), 6), round(float(n1o.Z), 6))
        p2 = (round(float(n2o.X), 6), round(float(n2o.Y), 6), round(float(n2o.Z), 6))
        rows[num] = {"sec": sec, "conn": frozenset((p1, p2)), "p1": p1, "p2": p2}
    print(f"{label}: {n} bars")
    return rows

pre = scan(b, "PRE-SOLVE")

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
post = scan(b, "POST-SOLVE")

pre_ids = set(pre)
post_ids = set(post)
print(f"pre ids {len(pre_ids)} post ids {len(post_ids)}; gone ids: {sorted(pre_ids - post_ids)}")

# connection survival: for each pre bar connection, is it in post?
pre_conn_owner = {}
for num, r in pre.items():
    pre_conn_owner.setdefault(r["conn"], []).append(num)
post_conn_owner = {}
for num, r in post.items():
    post_conn_owner.setdefault(r["conn"], []).append(num)

TARGETS = [11, 20, 102, 111, 31, 40, 122, 131]
print("\nTARGET bar | pre_conn | post_owner(s) [sec] | verdict")
for t in TARGETS:
    conn = pre[t]["conn"]
    owners = post_conn_owner.get(conn, [])
    if owners:
        secs = {post[o]["sec"] for o in owners}
        print(f"{t:>10} | yes      | {owners} {secs} | CONNECTION SURVIVES (renumbered or same)")
    else:
        print(f"{t:>10} | yes      | none               | CONNECTION GONE")

# which pre connections are MISSING post-solve (the truly removed bars)
missing = [num for num, r in pre.items() if r["conn"] not in post_conn_owner]
print(f"\npre bars whose connection is ABSENT post-solve ({len(missing)}): {sorted(missing)}")
for m in sorted(missing):
    print(f"  {m:>4} {pre[m]['sec']:<9} {pre[m]['p1']} -> {pre[m]['p2']}")

# load records: which numbers do UDL records reference
case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
records = case.Records
nrec = int(records.Count)
rec_bars = {}
for i in range(1, nrec + 1):
    rec = records.Get(i)
    if int(rec.Type) != RobotEnum.I_LRT_BAR_UNIFORM:
        continue
    vals = [float(rec.GetValue(j)) for j in (0, 1, 2)]
    kn = max(abs(vals[0]), abs(vals[1]), abs(vals[2]))
    for rid in b._record_object_ids(rec):
        rec_bars[rid] = kn
print(f"\nUDL records ({nrec} records) target bars: {sorted(rec_bars)}")
missing_rec = [r for r in rec_bars if r not in post_ids]
print(f"records referencing bars NOT in live post set: {missing_rec}")
load_total = sum(kn * b._bar_length(r) for r, kn in rec_bars.items() if r in post_ids)
print(f"UDL load on LIVE bars: {load_total:.3f} kN")

run("export_reactions", {"case_id": 1})
print(f"reaction sum(FZ) = {float(ex.reactions_df['FZ_kN'].sum()):.3f} kN")
ex.robot.close()
print("robot closed")