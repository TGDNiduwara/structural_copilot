"""COINC_AUDIT_SECTION_CHECK.py - READ-ONLY section diagnosis.

Rebuilds the compose twin-arch (same COMPOSE_STEPS as the E1 audit), then
reads back the LIVE COM section label of every IPE-500 chord bar (20 deck +
20 arch, both planes) and diffs against the compose-finish spec sections.
No code is changed; only the live model is inspected.
"""
from __future__ import annotations
import json
import sys
import math

ROOT = r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot"
sys.path.insert(0, ROOT)

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

run("clear_structure", {"project_type": "3D"})
for st in STEPS:
    run("compose_structure", {"action": "step", "step": st})
fin = run("compose_structure", {"action": "finish"})
geom = fin["geometry"]
print(f"compose finish: {fin['counts']} merged_coincident={geom.get('__merged_coincident_nodes')}")
run("create_structure_from_spec", {"spec": geom})

nmap = {int(n["id"]): (float(n.get("x", 0)), float(n.get("y", 0)), float(n.get("z", 0))) for n in geom["nodes"]}

def bar_len(b):
    i = nmap[int(b["n1"])]; j = nmap[int(b["n2"])]
    return math.sqrt((j[0]-i[0])**2 + (j[1]-i[1])**2 + (j[2]-i[2])**2)

deck, arch = [], []
for b in geom["bars"]:
    if not str(b.get("section")).startswith("IPE 500"):
        continue
    z1, z2 = nmap[int(b["n1"])][2], nmap[int(b["n2"])][2]
    (deck if (abs(z1) < 1e-9 and abs(z2) < 1e-9) else arch).append(b)
print(f"IPE-500 bars: deck={len(deck)} arch={len(arch)} (total IPE500={len(deck)+len(arch)})")

print("\nbar_id | type | spec_section | live_section | length_m | match")
problems = []
for b in sorted(deck + arch, key=lambda x: (x["id"])):
    bid = int(b["id"])
    spec_sec = str(b["section"])
    try:
        bar = ex.robot.structure.Bars.Get(bid)
        lbl = bar.GetLabel(RobotEnum.I_LT_BAR_SECTION)
        live_sec = str(lbl.Name)
    except Exception as exc:
        live_sec = f"ERR:{type(exc).__name__}"
    typ = "DECK" if b in deck else "ARCH"
    match = "OK" if (live_sec.strip().upper() == spec_sec.strip().upper() and spec_sec.strip().upper() == "IPE 500") else "<<< DIFF"
    if match != "OK":
        problems.append((bid, typ, spec_sec, live_sec))
    print(f"{bid:>6} | {typ:>4} | {spec_sec:<10} | {live_sec:<14} | {bar_len(b):5.2f} | {match}")

print("\n" + ("ALL MATCH: every chord bar is IPE 500 in BOTH spec and live COM" if not problems else f"DIFFS FOUND ({len(problems)}): {problems}"))
ex.robot.close()