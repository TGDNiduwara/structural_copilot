"""DialogWatcher tests: known-pattern auto-dismiss, unknown fallback, Interactive=0 primary."""

import sys
import time

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")
from batch.headless_driver import (
    HeadlessSession,
    SolverInstabilityError,
    UnknownDialogError,
    _robot_pids,
)


def check(tag, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {tag} {detail}", flush=True)
    if not cond:
        raise SystemExit(f"assertion failed: {tag}")


BASE = {
    "project": "2D",
    "nodes": [{"id": 1, "x": 0.0, "y": 0.0, "z": 0.0}, {"id": 2, "x": 6.0, "y": 0.0, "z": 0.0}],
    "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "IPE 300"}],
    "supports": [{"node": 1, "type": "pinned"}, {"node": 2, "type": "pinned"}],
    "cases": [{"id": 1, "name": "Dead"}],
    "loads": [{"kind": "bar_uniform", "bar": 1, "case": 1, "direction": "Z", "value": 10.0}],
}


def build_unstable(s):
    s.new_2d_frame()
    b = s.bridge
    b.create_node(1, 0.0, 0.0, 0.0)
    b.create_node(2, 0.0, 0.0, 5.0)
    b.create_bar(1, 1, 2, "HEA100")
    b.set_support(1, "fixed")
    b.create_load_case(1, "C")
    b.apply_nodal_load(2, 1, fx_kn=0.0, fz_kn=-300.0, my_knm=0.0)


base_count = len(_robot_pids())

# ---- (c) Interactive=0 remains the primary defense ----
with HeadlessSession(visible=False) as s:
    check("(c) Interactive=0 set by connect()", int(s.bridge.robot_app.Interactive) == 0)

# ---- (a) known instability dialog WITHOUT Interactive=0: auto-dismiss ----
s = HeadlessSession(visible=False)
s.connect()
pid_before = sorted(s._owned_pids)
s.bridge.robot_app.Interactive = 1  # dialog-capable; watcher must answer it
build_unstable(s)
try:
    s.solve_all(["static"])
    check("(a) SolverInstabilityError raised", False, "no error")
except SolverInstabilityError as exc:
    check("(a) clean instability failure", "instability" in str(exc).lower(), str(exc)[:130])
except Exception as exc:  # noqa: BLE001
    check("(a) expected SolverInstabilityError", False, f"{type(exc).__name__}: {exc}")
# same session must survive (we clicked, not killed) and solve a valid model
s.bridge.robot_app.Interactive = 0
s.build_from_spec(BASE)
try:
    r = s.solve_all(["static"])
    check("(a) same session solves valid model after", r["static"]["status"] == "ok")
except Exception as exc:  # noqa: BLE001
    check("(a) same session solves valid model after", False, f"{type(exc).__name__}: {exc}")
pid_after = sorted(s._owned_pids)
check("(a) no relaunch (same PID)", pid_before == pid_after, f"{pid_before} vs {pid_after}")
s.close()

# ---- (b) unrecognized dialog fallback: force-kill + logged title ----
s2 = HeadlessSession(visible=False)
s2.connect()
s2.dialog_patterns = {"__no_such_popup__": {"action": "click", "button_text": "No"}}
s2.bridge.robot_app.Interactive = 1
build_unstable(s2)
try:
    s2.solve_all(["static"])
    check("(b) UnknownDialogError raised", False, "no error")
except UnknownDialogError as exc:
    check(
        "(b) unknown fallback (kill + logged title)",
        "instability" in str(exc).lower(),
        str(exc)[:150],
    )
except Exception as exc:  # noqa: BLE001
    check("(b) expected UnknownDialogError", False, f"{type(exc).__name__}: {exc}")
time.sleep(2)
now = len(_robot_pids())
check("(b) unknown-dialog process force-killed", now == base_count, f"now={now} base={base_count}")
s2.close()

print("\nDIALOG WATCHER TESTS PASSED", flush=True)
