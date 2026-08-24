"""
tools/test_zero_results_regression.py
=======================================
LIVE regression test for the zero-results bug.

Builds a statically-determinate fixed-base cantilever (node 1 fixed, node 2
free, -50 kN nodal load at node 2) that MUST produce non-zero, analytically
correct results: base reaction FZ = +50 kN and base moment MY = 250 kNm
(50 kN x 5 m lever arm). This is the exact trivial case from the bug report.

RUN (on the Windows machine with Robot SA 2027 installed + licensed):

    cd c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot
    ./venv/Scripts/python.exe tools/test_zero_results_regression.py

Expect: "ZERO-RESULTS REGRESSION TEST PASSED" printed. The test fails loudly
if reactions are zero (the bug) or if the numbers are wrong (modeling error).

While this is failing, watch the console for the H2DIAG lines added to
create_load_case(): they tell you whether the case was renumbered or the
FIX-R4 fallback hit a T2 auto-created proxy case.
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.robot_tool import RobotBridge


def check(tag, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {tag} {detail}", flush=True)
    if not cond:
        raise SystemExit(f"assertion failed: {tag}")


def main() -> int:
    print("=" * 72)
    print("ZERO-RESULTS REGRESSION TEST (live COM cantilever)")
    print("=" * 72)

    bridge = RobotBridge()
    try:
        bridge.connect(visible=False)
    except Exception as exc:  # noqa: BLE001
        check("connect", False, f"{type(exc).__name__}: {exc}")

    try:
        bridge.new_2d_frame()
        # Node 1 fixed at origin; node 2 free at (0,0,5) - vertical cantilever.
        bridge.create_node(1, 0.0, 0.0, 0.0)
        bridge.create_node(2, 0.0, 0.0, 5.0)
        bridge.create_bar(1, 1, 2, "HEA 200")
        bridge.set_support(1, "fixed")
        bridge.create_load_case(1, "P", nature=0)
        bridge.apply_nodal_load(1, 1, fx_kn=0.0, fz_kn=-50.0, my_knm=0.0)
        bridge.solve()
    except Exception as exc:  # noqa: BLE001
        check("build + solve", False, f"{type(exc).__name__}: {exc}")

    # The bug: solve() reports success but every result is zero.
    try:
        reactions = bridge.export_reactions(case_id=1)
        forces = bridge.export_all_member_forces(case_id=1, divisions=2)
    except Exception as exc:  # noqa: BLE001
        check("export results", False, f"{type(exc).__name__}: {exc}")

    print("--- reactions ---")
    print(reactions.to_string() if reactions is not None else None)
    print("--- member forces (bar 1, all stations) ---")
    if forces is not None and not forces.empty:
        b1 = forces[forces["Bar_ID"] == 1]
        print(b1.to_string() if not b1.empty else "(no rows for bar 1)")
    else:
        print("(empty)")

    # Assertions: cantilever with -50 kN at node 2 -> base FZ=+50, MY=250.
    r = reactions if reactions is not None else None
    check("reactions non-empty", r is not None and not r.empty)
    if r is not None and not r.empty:
        fz = float(r.iloc[0]["FZ_kN"])
        my = float(r.iloc[0]["MY_kNm"])
        check("base FZ == +50 kN", abs(fz - 50.0) < 1.0, f"got {fz}")
        check("base MY == 250 kNm", abs(my - 250.0) < 5.0, f"got {my}")

    check("member forces non-empty", forces is not None and not forces.empty)
    if forces is not None and not forces.empty:
        b1 = forces[forces["Bar_ID"] == 1]
        check("bar 1 has force rows", not b1.empty)
        if not b1.empty:
            any_nonzero = bool(
                b1[["FX_kN", "FY_kN", "FZ_kN", "MX_kNm", "MY_kNm", "MZ_kNm"]].abs().max(axis=None)
                > 1e-6
            )
            check("bar 1 forces non-zero", any_nonzero, "(all-zero is the bug)")

    bridge.close()
    print("\nZERO-RESULTS REGRESSION TEST PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
