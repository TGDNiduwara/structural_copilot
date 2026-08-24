"""COINC_AUDIT_SECTION_CHECK_POSTSOLVE.py - READ-ONLY.

Rebuilds the compose twin-arch, reads the 40 IPE-500 chord bar sections /
lengths / endpoint node ids BEFORE solve, applies the E1 deck bar_uniform
(20 bars, -10 kN/m Z), CALLS CALCULATE, then re-reads the SAME bars while
Robot is still open, plus live bar/node counts and the reaction sum.
Robot is NOT closed until the final export_reactions read.
"""
from __future__ import annotations
import json
import math
import sys
import time

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

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
run = lambda t, a: json.loads(ex.dispatch(t, a))
b = ex.robot


def read_bar(bid: int) -> dict:
    """Live COM read: section name, start/end node ids + coords, length."""
    bar = CastTo(b.structure.Bars.Get(bid), "IRobotBar")
    sec = str(bar.GetLabel(RobotEnum.I_LT_BAR_SECTION).Name)
    n1 = int(bar.StartNode)
    n2 = int(bar.EndNode)
    n1o = CastTo(b.structure.Nodes.Get(n1), "IRobotNode")
    n2o = CastTo(b.structure.Nodes.Get(n2), "IRobotNode")
    p1 = (float(n1o.X), float(n1o.Y), float(n1o.Z))
    p2 = (float(n2o.X), float(n2o.Y), float(n2o.Z))
    L = math.sqrt(sum((p2[k] - p1[k]) ** 2 for k in range(3)))
    return {"section": sec, "n1": n1, "n2": n2, "p1": p1, "p2": p2, "length": round(L, 4)}


def live_counts():
    bars_coll = b.structure.Bars.GetAll()
    nodes_coll = b.structure.Nodes.GetAll()
    return int(bars_coll.Count), int(nodes_coll.Count)


# ---- rebuild -------------------------------------------------------------
run("clear_structure", {"project_type": "3D"})
for st in STEPS:
    run("compose_structure", {"action": "step", "step": st})
fin = run("compose_structure", {"action": "finish"})
geom = fin["geometry"]
print(f"compose finish: {fin['counts']} merged_coincident={geom.get('__merged_coincident_nodes')}")
run("create_structure_from_spec", {"spec": geom})

nmap = {int(n["id"]): (float(n.get("x", 0)), float(n.get("y", 0)), float(n.get("z", 0))) for n in geom["nodes"]}
deck = []; arch = []
for xb in geom["bars"]:
    if not str(xb.get("section")).startswith("IPE 500"):
        continue
    z1, z2 = nmap[int(xb["n1"])][2], nmap[int(xb["n2"])][2]
    (deck if (abs(z1) < 1e-9 and abs(z2) < 1e-9) else arch).append(int(xb["id"]))
chord_ids = sorted(deck + arch)
print(f"chord bars: deck={len(deck)} arch={len(arch)}")

# ---- PRE-SOLVE read -------------------------------------------------------
pre = {bid: read_bar(bid) for bid in chord_ids}
print(f"PRE-SOLVE live counts: bars={live_counts()[0]} nodes={live_counts()[1]}")

# ---- E1 loads + CALCULATE -------------------------------------------------
run("create_load_case", {"case_id": 1, "case_name": "DECK", "nature": "permanent"})
for bid in deck:
    r = run("apply_bar_load", {"bar_id": bid, "case_id": 1, "value_kn_m": -10.0, "direction": "Z"})
    assert r.get("method") == "bar_uniform_record", (bid, r)
t0 = time.time()
run("solve", {"timeout_s": 150})
print(f"solve {time.time()-t0:.0f}s")

# ---- POST-SOLVE read (Robot still open) ------------------------------------
post_counts = live_counts()
print(f"POST-SOLVE live counts: bars={post_counts[0]} nodes={post_counts[1]}")
# enumerate the LIVE bar/node id sets post-Calculate and compare to pre
post_bar_ids = set(b._enumerate_bar_ids(b.structure.Bars, post_counts[0]))
post_node_ids = set()
coll = b.structure.Nodes.GetAll()
for _i in range(1, int(coll.Count) + 1):
    try:
        post_node_ids.add(int(coll.Get(_i).Number))
    except Exception:
        pass
print(f"POST-SOLVE bar id count={len(post_bar_ids)}; chord ids still present: {len(set(chord_ids) & post_bar_ids)}/{len(chord_ids)}")
print(f"POST-SOLVE node ids: {sorted(post_node_ids)}")
post = {}
for bid in chord_ids:
    try:
        post[bid] = read_bar(bid)
    except Exception as exc:
        post[bid] = {"section": "GET_FAILED", "n1": None, "n2": None, "length": None, "err": str(exc)[:60]}

# ---- diff -----------------------------------------------------------------
print("\nbar_id | type | pre_sec | post_sec | pre_L | post_L | pre_n1,n2 | post_n1,n2 | notes")
issues = []
for bid in chord_ids:
    typ = "DECK" if bid in deck else "ARCH"
    a, c = pre[bid], post[bid]
    if c.get("section") == "GET_FAILED":
        print(f"{bid:>6} | {typ:>4} | {a['section']:<8} | GET_FAILED | {a['length']:6.2f} |  n/a  | {a['n1']},{a['n2']} |  n/a  | GET_FAILED_POST_SOLVE")
        issues.append((bid, ["GET_FAILED_POST_SOLVE"]))
        continue
    note = []
    if a["section"] != c["section"]:
        note.append("SECTION_CHANGED")
    if abs(a["length"] - c["length"]) > 1e-6:
        note.append(f"LEN {a['length']}->{c['length']}")
    if (a["n1"], a["n2"]) != (c["n1"], c["n2"]):
        note.append(f"NODES {a['n1']},{a['n2']}->{c['n1']},{c['n2']}")
    if a["section"] != "IPE 500" or c["section"] != "IPE 500":
        note.append("NOT_IPE500")
    if note:
        issues.append((bid, note))
    print(f"{bid:>6} | {typ:>4} | {a['section']:<8} | {c['section']:<8} | {a['length']:6.2f} | {c['length']:6.2f} | {a['n1']},{a['n2']} | {c['n1']},{c['n2']} | {'; '.join(note)}")

print(f"\nDIFF SUMMARY: {len(issues)} bar(s) with any change -> {issues if issues else 'NONE'}")

# ---- the 4 end deck bars + matching end arch bars --------------------------
print("\nEND BARS (4 deck + 4 arch) pre vs post:")
for bid in [31, 40, 122, 131, 11, 20, 102, 111]:
    a, c = pre[bid], post[bid]
    same = a == c
    print(f"  bar {bid}: {'IDENTICAL' if same else 'CHANGED '} pre={a} post={c}")

# ---- reaction sum (Robot still open) ---------------------------------------
run("export_reactions", {"case_id": 1})
total = float(ex.reactions_df["FZ_kN"].sum())
print(f"\nreaction sum(FZ) = {total:.3f} kN (E1 expectation: 480.000 -> 20% shortfall)")

json.dump({"pre": pre, "post": post, "pre_counts": live_counts(), "reaction_sum_fz": total},
          open(r"batch/live_val_results/COINC_AUDIT_SECTION_POSTSOLVE.json", "w"), indent=1)
ex.robot.close()
print("robot closed; results written to COINC_AUDIT_SECTION_POSTSOLVE.json")