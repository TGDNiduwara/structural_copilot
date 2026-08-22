"""
tools/test_bracing_registry.py
==============================
[EUROCODE Phase A] Bracing / unbraced-length registry tests — pure Python,
no Robot COM required.

Covers eurocode_scope.md §3 / decision D6:
  * default-and-warn: unspecified Lcr resolves to the full bar length with
    a warning and source="defaulted" (never silent).
  * explicit Lcr -> source="explicit".
  * brace_points -> lcr_lt = longest sub-span between braces,
    source="brace_points" (derived from explicit input).
  * validation: negative Lcr rejected; brace_points outside [0,1] rejected;
    Lcr > 2.5 x bar length flagged as a suspicious K-factor warning.
  * bridge + registry wiring: RobotBridge() carries a BracingRegistry and
    the two tools are registered with handlers.

Run:  python tools/test_bracing_registry.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.bracing_registry import BracingRegistry, SUSPICIOUS_K_FACTOR
from tools.robot_tool import RobotBridge


def test_defaults_are_explicitly_warned():
    reg = BracingRegistry()
    value, source, warnings = reg.lcr_lt_for(1, 6.0)
    assert value == 6.0, "default = full bar length"
    assert source == "defaulted"
    assert warnings, "a default MUST carry a warning"
    assert "CONSERVATIVE" in warnings[0]
    value, source, warnings = reg.lcr_z_for(1, 6.0)
    assert value == 6.0 and source == "defaulted" and warnings
    print("  OK: default-and-warn contract (full length + warning)")


def test_explicit_lcr():
    reg = BracingRegistry()
    reg.set_bracing(2, lcr_y=3.0, lcr_z=2.5, lcr_lt=4.0)
    v, s, w = reg.lcr_lt_for(2, 6.0)
    assert v == 4.0 and s == "explicit" and w == []
    v, s, w = reg.lcr_y_for(2, 6.0)
    assert v == 3.0 and s == "explicit"
    v, s, w = reg.lcr_z_for(2, 6.0)
    assert v == 2.5 and s == "explicit"
    print("  OK: explicit Lcr honored, source='explicit'")


def test_brace_points_shorten_lcr_lt():
    reg = BracingRegistry()
    reg.set_bracing(3, brace_points=[0.5], bar_length=10.0)
    v, s, w = reg.lcr_lt_for(3, 10.0)
    assert v == 5.0, "mid-span purlin halves the LTB unbraced length"
    assert s == "brace_points"
    assert w == []
    # two braces -> three sub-spans, longest governs
    reg.set_bracing(4, brace_points=[0.25, 0.5, 0.75])
    v, s, w = reg.lcr_lt_for(4, 8.0)
    assert v == 2.0, "longest sub-span = 0.25 x 8.0 m"
    assert s == "brace_points"
    # brace_points never shorten lcr_y / lcr_z (only lcr_lt)
    v, s, w = reg.lcr_z_for(4, 8.0)
    assert v == 8.0 and s == "defaulted"
    print("  OK: brace_points -> lcr_lt = longest sub-span (y/z untouched)")


def test_validation():
    reg = BracingRegistry()
    try:
        reg.set_bracing(5, lcr_y=-0.5)
        raise AssertionError("negative Lcr must be rejected")
    except ValueError:
        pass
    try:
        reg.set_bracing(5, brace_points=[1.5])
        raise AssertionError("brace_point outside [0,1] must be rejected")
    except ValueError:
        pass
    try:
        reg.set_bracing(5, brace_points=[-0.1])
        raise AssertionError("negative brace_point must be rejected")
    except ValueError:
        pass
    # suspicious K-factor: explicit Lcr > 2.5 x length -> warning, not silent
    reg.set_bracing(6, lcr_lt=12.0)
    v, s, w = reg.lcr_lt_for(6, 4.0)
    assert v == 12.0 and s == "explicit"
    assert any("suspicious" in msg for msg in w), \
        "Lcr > 2.5 x length must surface a suspicious-K warning"
    assert 12.0 > SUSPICIOUS_K_FACTOR * 4.0
    print("  OK: validation (negative rejected, K-factor warning)")


def test_lifecycle():
    reg = BracingRegistry()
    assert len(reg) == 0
    reg.set_bracing(1, lcr_lt=2.0)
    reg.set_bracing(2, lcr_lt=3.0)
    assert len(reg) == 2
    assert reg.all_bars() == [1, 2]
    assert reg.get(2)["lcr_lt"] == 3.0
    assert reg.remove(2) is True and reg.remove(99) is False
    assert len(reg) == 1
    assert reg.clear() == 1 and len(reg) == 0
    # resolve() bundles values + sources + warnings
    reg.set_bracing(7, brace_points=[0.5], bar_length=10.0)
    row = reg.resolve(7, 10.0)
    assert row["lcr_lt_m"] == 5.0
    assert row["lcr_lt_source"] == "brace_points"
    assert row["lcr_z_source"] == "defaulted"
    assert len(row["warnings"]) >= 1  # defaulted lcr_y + lcr_z
    print("  OK: lifecycle (add/remove/clear/resolve)")


def test_bridge_and_registry_wiring():
    # Pure attribute: RobotBridge() constructs without connecting and must
    # carry a BracingRegistry side-table.
    bridge = RobotBridge()
    assert isinstance(bridge.bracing, BracingRegistry)
    bridge.bracing.set_bracing(1, lcr_lt=2.5)
    assert bridge.bracing.lcr_lt_for(1, 5.0)[0] == 2.5
    from agent.tool_registry import TOOL_SCHEMAS, ToolExecutor
    names = {s["name"] for s in TOOL_SCHEMAS}
    assert "set_bracing" in names and "get_bracing" in names
    assert hasattr(ToolExecutor, "_tool_set_bracing")
    assert hasattr(ToolExecutor, "_tool_get_bracing")
    print("  OK: bridge side-table + tool registration")


def main():
    print("=" * 72)
    print("EUROCODE Phase A — bracing / unbraced-length registry tests")
    print("=" * 72)
    test_defaults_are_explicitly_warned()
    test_explicit_lcr()
    test_brace_points_shorten_lcr_lt()
    test_validation()
    test_lifecycle()
    test_bridge_and_registry_wiring()
    print("ALL PHASE A TESTS PASSED")


if __name__ == "__main__":
    main()
