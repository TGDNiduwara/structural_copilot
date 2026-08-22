"""
tools/test_eurocode_members.py
==============================
[EUROCODE Phase E] Integration wrapper tests — pure ranking logic + tool
registration (the live four-check path is exercised by test_ltb_live.py).

Covers the worst-governing convention: FAIL > NOT_CHECKABLE > PASS, ties
break toward the earlier check in [elastic, buckling, ltb, connection].

Run:  python tools/test_eurocode_members.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.eurocode_members import worst_of, _CHECK_ORDER


def _per(elastic="PASS", buckling="N/A", ltb="PASS", connection="N/A"):
    return {"elastic": {"status": elastic},
            "buckling": {"status": buckling},
            "ltb": {"status": ltb},
            "connection": {"status": connection}}


def test_all_pass():
    gov, overall = worst_of(_per())
    assert overall == "PASS" and gov == "elastic"
    print("  OK: all PASS -> PASS (elastic governing)")


def test_ltb_fail_governs():
    gov, overall = worst_of(_per(ltb="FAIL"))
    assert overall == "FAIL" and gov == "ltb"
    print("  OK: LTB FAIL -> overall FAIL, governing='ltb'")


def test_not_checkable_over_pass():
    gov, overall = worst_of(_per(connection="NOT_CHECKABLE"))
    assert overall == "NOT_CHECKABLE" and gov == "connection"
    print("  OK: connection NOT_CHECKABLE -> 'not certified' over PASS")


def test_fail_beats_not_checkable():
    gov, overall = worst_of(_per(ltb="FAIL", connection="NOT_CHECKABLE"))
    assert overall == "FAIL" and gov == "ltb", "FAIL must beat NOT_CHECKABLE"
    print("  OK: FAIL beats NOT_CHECKABLE")


def test_tie_breaks_to_earlier_check():
    gov, _ = worst_of(_per(buckling="NOT_CHECKABLE", ltb="NOT_CHECKABLE"))
    assert gov == "buckling", "tie breaks to the earlier check"
    print("  OK: tie between NOT_CHECKABLE checks -> earlier in order")


def test_wiring():
    from tools.robot_tool import RobotBridge
    bridge = RobotBridge()
    assert hasattr(bridge, "bracing") and hasattr(bridge, "connections")
    from agent.tool_registry import TOOL_SCHEMAS, ToolExecutor
    names = {s["name"] for s in TOOL_SCHEMAS}
    for tool in ("set_bracing", "get_bracing",
                 "check_lateral_torsional_buckling",
                 "define_connection", "check_connection_capacity",
                 "check_eurocode_members"):
        assert tool in names, f"{tool} not registered"
    for handler in ("_tool_set_bracing", "_tool_get_bracing",
                    "_tool_check_lateral_torsional_buckling",
                    "_tool_define_connection",
                    "_tool_check_connection_capacity",
                    "_tool_check_eurocode_members"):
        assert hasattr(ToolExecutor, handler), f"{handler} missing"
    assert _CHECK_ORDER == ["elastic", "buckling", "ltb", "connection"]
    print("  OK: all six Eurocode tools registered with handlers")


def main():
    print("=" * 72)
    print("EUROCODE Phase E — integration wrapper tests")
    print("=" * 72)
    test_all_pass()
    test_ltb_fail_governs()
    test_not_checkable_over_pass()
    test_fail_beats_not_checkable()
    test_tie_breaks_to_earlier_check()
    test_wiring()
    print("ALL PHASE E TESTS PASSED")


if __name__ == "__main__":
    main()
