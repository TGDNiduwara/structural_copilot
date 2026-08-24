"""COINC_AUDIT_BAR_DELETION.py - READ-ONLY: DELETE vs RENUMBER confirmation.

Rebuilds the twin-arch, reads the 8 end-chord bars, calls Calculate, then
enumerates ALL live bars POSITIONALLY (GetAll().Get(i)) and checks whether
the 8 pre-solve node-connections still exist under any live bar id. Captures
the Calculation Messages dialog BODY text during solve (fast poll thread).
Robot stays open until the end.
"""
from __future__ import annotations
import json
import math
import sys
import threading
import time

ROOT = r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot"
sys.path.insert(0, ROOT)

from win32com.client import CastTo

from agent.tool_registry import ToolExecutor
from tools.robot_tool import RobotEnum
from tools.win_dialogs import _enum_windows, _window_text
from tools.robot_seat import seat_status

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

TARGETS = [11, 20, 102, 111, 31, 40, 122, 131]
CAPTURED_DIALOGS: list[str] = []


def read_bar(b):
    """Live COM read: section, endpoint node ids + coords, length."""
    bar = CastTo(b.structure.Bars.Get(bid if False else 1), "IRobotBar")
    return bar


def bar_info(b, bid: int) -> dict:
    bar = CastTo(b.structure.Bars.Get(bid), "IRobotBar")
    sec = str(bar.GetLabel(RobotEnum.I_LT_BAR_SECTION).Name)
    n1, n2 = int(bar.StartNode), int(bar.EndNode)
    n1o = CastTo(b.structure.Nodes.Get(n1), "IRobotNode")
    n2o = CastTo(b.structure.Nodes.Get(n2), "IRobotNode")
    p1 = (round(float(n1o.X), 6), round(float(n1o.Y), 6), round(float(n1o.Z), 6))
    p2 = (round(float(n2o.X), 6), round(float(n2o.Y), 6), round(float(n2o.Z), 6))
    return {"id": bid, "section": sec, "n1": n1, "n2": n2, "p1": p1, "p2": p2,
            "conn": frozenset((p1, p2))}


def dialog_capture_thread(robot_pids, stop: threading.Event):
    while not stop.is_set():
        try:
            for hwnd, title, cls in _enum_windows(robot_pids):
                if (cls or "").lower() == "robobatrobot97":
                    continue
                txt = _window_text(hwnd)
                low = txt.lower()
                if "calculation messages" in low or "message" in low or "warn" in low or "merge" in low or "removed" in low or "deleted" in low or "duplicate" in low:
                    CAPTURED_DIALOGS.append((title, txt))
        except Exception:
            pass
        time.sleep(0.03)


ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
run = lambda t, a: json.loads(ex.dispatch(t, a))
b = ex.robot

run("clear_structure", {"project_type": "3D"})
for st in STEPS:
    run("compose_structure", {"action": "step", "step": st})
fin = run("compose_structure", {"action": "finish"})
geom = fin["geometry"]
print(f"compose finish: {fin['counts']} merged={geom.get('__merged_coincident_nodes')}")
run("create_structure_from_spec", {"spec": geom})

# pre-solve read of the 8 targets
pre = {t: bar_info(b, t) for t in TARGETS}
for t in TARGETS:
    print(f"PRE  bar {t:>4} {pre[t]['section']:<9} n{pre[t]['n1']}->n{pre[t]['n2']} {pre[t]['p1']} -> {pre[t]['p2']}")

run("create_load_case", {"case_id": 1, "case_name": "DECK", "nature": "permanent"})
nmap = {int(n["id"]): (float(n.get("x", 0)), float(n.get("y", 0)), float(n.get("z", 0))) for n in geom["nodes"]}
deck = [int(xb["id"]) for xb in geom["bars"]
        if str(xb.get("section")).startswith("IPE 500")
        and abs(nmap[int(xb["n1"])][2]) < 1e-9 and abs(nmap[int(xb["n2"])][2]) < 1e-9]
for bid in deck:
    r = run("apply_bar_load", {"bar_id": bid, "case_id": 1, "value_kn_m": -10.0, "direction": "Z"})
    assert r.get("method") == "bar_uniform_record", (bid, r)

robot_pids = list((seat_status().get("robot_pids") or []))
if b.connected_pid and b.connected_pid not in robot_pids:
    robot_pids.append(b.connected_pid)
print(f"dialog capture watching pids: {robot_pids}")
stop = threading.Event()
th = threading.Thread(target=dialog_capture_thread, args=(robot_pids, stop), daemon=True)
th.start()
t0 = time.time()
run("solve", {"timeout_s": 150})
print(f"solve {time.time()-t0:.0f}s")
stop.set()
th.join(timeout=5)

# ---- POST-SOLVE positional enumeration ------------------------------------
coll = b.structure.Bars.GetAll()
n_live = int(coll.Count)
print(f"POST-SOLVE live bar count: {n_live}")
live_bars = []
for i in range(1, n_live + 1):
    try:
        obj = coll.Get(i)
        bid = int(obj.Number)
        info = bar_info(b, bid)
        live_bars.append(info)
    except Exception as exc:
        live_bars.append({"id": f"GET_FAIL_{i}", "err": str(exc)[:50], "conn": None})

live_conns = {lb["conn"] for lb in live_bars if lb.get("conn")}
print(f"live connections (unordered node-pairs): {len(live_conns)}")

print("\nbar | pre_conn_exists_live | live_bar_id(s) | verdict")
for t in TARGETS:
    c = pre[t]["conn"]
    hits = [lb["id"] for lb in live_bars if lb.get("conn") == c]
    if hits:
        print(f"{t:>4} | YES                 | {hits}      | RENUMBERED/PRESENT")
    else:
        print(f"{t:>4} | NO                  | -              | DELETED")

print("\n=== CAPTURED DIALOGS (body text) ===")
seen = set()
for title, txt in CAPTURED_DIALOGS:
    key = (title, txt[:120])
    if key in seen:
        continue
    seen.add(key)
    print(f"--- title: {title!r}")
    print(txt[:1500])
print("(end captured dialogs)")

# reaction sum (Robot still open)
run("export_reactions", {"case_id": 1})
print(f"\nreaction sum(FZ) = {float(ex.reactions_df['FZ_kN'].sum()):.3f} kN")

ex.robot.close()
print("robot closed")