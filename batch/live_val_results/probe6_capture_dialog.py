"""probe6_capture_dialog.py - during an interactive Calculate, dump the full
text of every robot-owned window to see EXACTLY what dialog appears and why
clicking 'No' on it yields an empty calculation."""
from __future__ import annotations
import sys, threading, time
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from agent.tool_registry import ToolExecutor
from tools.win_dialogs import _enum_windows, _window_text, _robot_pids

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot

print("== build truss + self weight ==")
print(ex.dispatch("create_truss", {"span": 20.0, "height": 3.0, "panels": 6,
                                   "web_section": "L 50x50x5"})[:100])
ex.dispatch("create_load_case", {"case_id": 1, "case_name": "SW",
                                 "nature": "permanent"})
print(ex.dispatch("apply_self_weight", {"case_id": 1})[:100])

pids = _robot_pids()
print("robot pids:", pids)

seen = set()
stop = threading.Event()

def dump_windows():
    while not stop.is_set():
        for hwnd, title, cls in _enum_windows(pids):
            key = (hwnd, title)
            if key in seen:
                continue
            seen.add(key)
            txt = _window_text(hwnd)
            print(f"[WIN] cls={cls!r} title={title!r}")
            print(f"      text={txt[:400]!r}")
        time.sleep(0.3)

t = threading.Thread(target=dump_windows, daemon=True)
t.start()
try:
    print("\n== solve (with window capture) ==")
    res = b.solve(timeout_s=120)
    print("solve returned:", res)
finally:
    stop.set()
    t.join(timeout=2)

print("\n== results after solve ==")
try:
    f = b.structure.Results.Bars.Forces.Value(1, 1, 0.5)
    print("bar1@0.5 FX,FZ,MY:", round(float(f.FX),3), round(float(f.FZ),3),
          round(float(f.MY),3))
except Exception as exc:
    print("force read ERR:", type(exc).__name__, str(exc)[:120])
try:
    print(b.export_reactions(case_id=1).to_string())
except Exception as exc:
    print("reactions ERR:", type(exc).__name__, str(exc)[:120])
