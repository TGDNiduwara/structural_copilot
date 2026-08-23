"""COMPOSE_probe_geometry_divergence.py - bookkeeping vs live Robot model.

Builds the twin-arch, then dumps:
  - the bridge's SPEC bookkeeping (_node_coords / _bar_endpoints)
  - the LIVE Robot model (node id->coords, bar id->endpoints)
  - a per-bar length comparison (spec length vs live Robot geometry)
  - the actual load records (raw fields) for a sample of bars
and prints where they diverge. No solve needed.
"""
from __future__ import annotations
import json
import sys

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
    return run_tool("compose_structure", {"action": "finish"})["geometry"]


def dist(a, bb):
    return ((a[0] - bb[0]) ** 2 + (a[1] - bb[1]) ** 2 + (a[2] - bb[2]) ** 2) ** 0.5


print("=" * 72)
print("PROBE: bridge bookkeeping vs LIVE Robot model")
print("=" * 72)

run_tool("clear_structure", {"project_type": "3D"})
geom = compose_twin_arch()
print("composed spec:", len(geom["nodes"]), "nodes,", len(geom["bars"]), "bars")

run_tool("create_structure_from_spec", {"spec": geom})
run_tool("create_load_case", {"case_id": 1, "case_name": "SW",
                              "nature": "permanent"})

# bridge SPEC bookkeeping
spec_coords = {nd["id"]: (float(nd["x"]), float(nd["y"]), float(nd["z"]))
               for nd in geom["nodes"]}
spec_ends = {br["id"]: (int(br["n1"]), int(br["n2"])) for br in geom["bars"]}
print("\n-- bridge _node_coords / _bar_endpoints (spec) -----------")
print("  _node_coords count:", len(b._node_coords))
print("  _bar_endpoints count:", len(b._bar_endpoints))

# LIVE model
nc = b.structure.Nodes.GetAll()
live_nid = {}
for i in range(1, int(nc.Count) + 1):
    nd = nc.Get(i)
    live_nid[int(nd.Number)] = (
        round(float(nd.X), 6), round(float(nd.Y), 6), round(float(nd.Z), 6))
bc = b.structure.Bars.GetAll()
live_bars = {}
for i in range(1, int(bc.Count) + 1):
    bar = bc.Get(i)
    live_bars[int(bar.Number)] = (int(bar.StartNode), int(bar.EndNode))
print("\n-- LIVE Robot model --------------------------------------")
print("  live node ids:", sorted(live_nid))
print("  live node count:", len(live_nid), "bar count:", len(live_bars))

# node comparison: for each SPEC node, find a LIVE node at the SAME coordinate
coord_to_live_id = {}
for nid, c in live_nid.items():
    coord_to_live_id.setdefault(c, []).append(nid)
spec_to_live = {}
for snid, c in spec_coords.items():
    cand = coord_to_live_id.get(c, [])
    spec_to_live[snid] = snid if snid in cand else (cand[0] if cand else None)
print("\n-- spec node -> live node id mapping (by coordinate) -----")
renumbered = {snid: lnid for snid, lnid in spec_to_live.items()
              if lnid is not None and lnid != snid}
print(f"  nodes whose live id DIFFERS from spec id: {len(renumbered)}")
for snid, lnid in sorted(renumbered.items())[:12]:
    print(f"    spec node {snid} @ {spec_coords[snid]} -> live node {lnid}")
missing_nodes = sorted(s for s, l in spec_to_live.items() if l is None)
print("  spec nodes NOT found by coordinate in live model:",
      missing_nodes[:20], f"({len(missing_nodes)})")

# bar comparison: does live bar (by id) connect the same COORDINATE pair?
print("\n-- per-bar endpoint coordinate comparison ----------------")
length_diffs = []
unresolved = 0
for bid, (ln1, ln2) in live_bars.items():
    sn1, sn2 = spec_ends.get(bid, (None, None))
    if sn1 is None:
        continue
    c1, c2 = spec_coords[sn1], spec_coords[sn2]
    lc1, lc2 = live_nid.get(ln1), live_nid.get(ln2)
    if lc1 is None or lc2 is None:
        unresolved += 1
        continue
    L_spec = dist(c1, c2)
    L_live = dist(lc1, lc2)
    if abs(L_spec - L_live) > 1e-6:
        length_diffs.append((bid, sn1, sn2, c1, c2, ln1, ln2, lc1, lc2,
                             round(L_spec, 4), round(L_live, 4)))
print(f"  bars whose live endpoint GEOMETRY length differs from spec: "
      f"{len(length_diffs)}")
for row in length_diffs[:10]:
    print("   ", row)
print(f"  bars with unresolvable live endpoints: {unresolved}")

# load records: dump raw fields for the first 3 uniform records
print("\n-- load records (raw) ------------------------------------")
case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
nrec = int(case.Records.Count)
print("  record count:", nrec)
shown = 0
for i in range(1, nrec + 1):
    if shown >= 3:
        break
    rec = case.Records.Get(i)
    try:
        rv = CastTo(rec, "IRobotBarUniformRecord")
    except Exception as e:
        print(f"    rec#{i} not bar-uniform ({e}); type={rec.Type}")
        continue
    txt = str(rv.Objects.Text or "")
    vals = [rv.Values.GetValue(k) for k in (0, 1, 2)]
    print(f"    rec#{i}: type={rec.Type} PX/PY/PZ={vals} objects={txt!r}")
    shown += 1