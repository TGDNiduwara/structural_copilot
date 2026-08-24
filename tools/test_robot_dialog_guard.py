"""
tools/test_robot_dialog_guard.py
=================================
LIVE integration test for the interactive-safe dialog guard around
RobotBridge's Project.New() (new_2d_frame / new_3d_frame / clear_structure)
and Calculate() (solve).

This is a LIVE script (same convention as batch/test_dialog_watcher.py): it
REALLY launches Robot via COM and REALLY solves a model, then exercises
clear_structure() to prove the guard returns instead of hanging on a
save-changes modal.

RUN (on the Windows machine with Robot Structural Analysis Professional
installed and licensed):

    cd c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot
    .\\venv\\Scripts\\python.exe tools\\test_robot_dialog_guard.py

Expect: "ROBOT DIALOG GUARD TESTS PASSED" printed. If a real
"Do you want to save changes to Structure?" modal appears and the guard's
"No" click pattern does NOT match the live text, the test will still complete
(non-hang is what it proves) but you will see a warning in the console from
watch_and_dismiss about an UNKNOWN dialog - capture that printed body text and
update SAVE_PROMPT_PATTERNS in tools/win_dialogs.py accordingly.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.robot_tool import RobotBridge


def check(tag, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {tag} {detail}", flush=True)
    if not cond:
        raise SystemExit(f"assertion failed: {tag}")


def node_count(bridge) -> int:
    coll = bridge.structure.Nodes.GetAll()
    return int(coll.Count) if coll is not None else 0


def bar_count(bridge) -> int:
    coll = bridge.structure.Bars.GetAll()
    return int(coll.Count) if coll is not None else 0


def main() -> int:
    print("=" * 72)
    print("ROBOT DIALOG GUARD TESTS (live COM)")
    print("=" * 72)

    bridge = RobotBridge()
    try:
        bridge.connect(visible=True)  # Interactive=1 (the app's normal mode)
    except Exception as exc:  # noqa: BLE001
        check("connect", False, f"{type(exc).__name__}: {exc}")

    # (a) build a small 2D frame and solve it so RESULTS exist
    try:
        bridge.new_2d_frame()
        bridge.create_node(1, 0.0, 0.0, 0.0)
        bridge.create_node(2, 6.0, 0.0, 0.0)
        bridge.create_bar(1, 1, 2, "IPE 300")
        bridge.set_support(1, "pinned")
        bridge.set_support(2, "pinned")
        bridge.create_load_case(1, "DL", nature=0)
        bridge.apply_bar_load(1, 1, -10.0, "Z")
        bridge.solve()
        check("(a) solve completed (results now exist)", True)
    except Exception as exc:  # noqa: BLE001
        check("(a) build + solve", False, f"{type(exc).__name__}: {exc}")

    # (b) clear_structure must RETURN within a few seconds, not hang
    start = time.time()
    try:
        bridge.clear_structure("2D")
        elapsed = time.time() - start
        check("(b) clear_structure returned", elapsed < 20.0, f"took {elapsed:.1f}s")
    except Exception as exc:  # noqa: BLE001
        elapsed = time.time() - start
        check(
            "(b) clear_structure returned",
            False,
            f"{type(exc).__name__} after {elapsed:.1f}s: {exc}",
        )

    # (c) the resulting project is genuinely empty (discard really completed)
    try:
        n = node_count(bridge)
        b = bar_count(bridge)
        check("(c) model empty after clear", n == 0 and b == 0, f"nodes={n} bars={b}")
    except Exception as exc:  # noqa: BLE001
        check("(c) model empty after clear", False, f"{type(exc).__name__}: {exc}")

    bridge.close()
    print("\nROBOT DIALOG GUARD TESTS PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
