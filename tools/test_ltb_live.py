"""
tools/test_ltb_live.py
======================
[EUROCODE Phase C] LIVE end-to-end LTB validation (needs a connected Robot).

Builds a 6 m IPE 300 beam (two 3 m bars) with a midspan NODAL point load
and solves, then runs check_lateral_torsional_buckling through the real
pipeline: live section props (probe-verified GetValue map), live material
RE, exported forces, bracing side-table.

KNOWN BUILD DEFECTS that shape this test (separately tracked — NOT fixed
here, eurocode_scope.md §6):
  * a STALE attached Robot session returns zero results — a FRESH instance
    (new_instance=True) solves correctly;
  * PINNED supports return zero results even on a fresh instance (verified:
    pinned-pinned and pinned+roller beams -> all-zero reactions/forces,
    while fixed-fixed and fixed+roller solve correctly). The test therefore
    uses a FIXED + ROLLER_Z propped-cantilever configuration; the fixed end
    is stiffer than the assumed simple support, so the simply-supported
    closed-form Mcr is CONSERVATIVE (safe direction).

The assertions are structural (pipeline correctness), not hand-numbered:
the exact Mcr/chi_LT/Mb,Rd numerics are covered by test_ltb_check.py.

Run:  python tools/test_ltb_live.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.ltb_check import check_lateral_torsional_buckling
from tools.robot_tool import RobotBridge


def _build_beam(bridge, p_kn: float) -> None:
    bridge.new_2d_frame()
    bridge.create_node(1, 0.0, 0.0, 0.0)
    bridge.create_node(2, 3.0, 0.0, 0.0)
    bridge.create_node(3, 6.0, 0.0, 0.0)
    bridge.create_bar(1, 1, 2, "IPE 300")
    bridge.create_bar(2, 2, 3, "IPE 300")
    bridge.set_support(1, "fixed")
    bridge.set_support(3, "roller_z")
    bridge.create_load_case(1, "DL")
    bridge.apply_nodal_load(2, 1, fz_kn=-p_kn)


def main() -> int:
    print("=" * 72)
    print("EUROCODE Phase C — LIVE LTB end-to-end validation")
    print("=" * 72)
    bridge = RobotBridge()
    bridge.connect(visible=True, new_instance=True)

    # 1) PASS case: P=50 kN -> governing MEd = 56.25 kNm at the fixed end
    _build_beam(bridge, 50.0)
    bridge.solve(timeout_s=180)
    res = check_lateral_torsional_buckling(bridge, 1)
    assert len(res["bars"]) == 2, res
    for b in res["bars"]:
        assert b["status"] == "PASS", b
        assert b["section"] == "IPE 300", b
        assert "end moments" in b["c1_case"], b["c1_case"]
        assert b["lcr_lt_source"] == "defaulted"
        assert len(b["warnings"]) >= 1, "defaulted Lcr must carry a warning"
        assert b["utilization"] < 1.0
    gov = max(res["bars"], key=lambda r: r["m_ed_knm"])
    assert abs(gov["m_ed_knm"] - 56.25) < 1.0, gov["m_ed_knm"]
    print(
        f"  [OK] P=50: PASS (governing MEd={gov['m_ed_knm']:.2f} kNm "
        f"at fixed end, Mb,Rd={gov['mb_rd_knm']:.1f} kNm; "
        f"lcr_lt defaulted+warning)"
    )

    # 2) explicit lcr_lt via set_bracing -> higher Mb,Rd, lower utilization
    for bar in (1, 2):
        bridge.set_bar_bracing(bar, lcr_lt=1.5)
    res = check_lateral_torsional_buckling(bridge, 1)
    for b in res["bars"]:
        assert b["lcr_lt_source"] == "explicit", b
        assert abs(b["lcr_lt_m"] - 1.5) < 1e-6
    before = gov["utilization"]
    after = max(b["utilization"] for b in res["bars"])
    assert after < before, f"bracing must reduce utilization ({before}->{after})"
    print(f"  [OK] lcr_lt=1.5 m explicit: util {before} -> {after} (source='explicit')")

    # 3) FAIL case: P=150 kN -> MEd = 168.75 kNm > Mb,Rd
    bridge.bracing.clear()
    _build_beam(bridge, 150.0)
    bridge.solve(timeout_s=180)
    res = check_lateral_torsional_buckling(bridge, 1)
    b = max(res["bars"], key=lambda r: r.get("m_ed_knm", 0.0))
    assert b["status"] == "FAIL", b
    assert b["utilization"] > 1.0
    print(
        f"  [OK] P=150: FAIL util={b['utilization']} "
        f"(MEd={b['m_ed_knm']:.1f} vs Mb,Rd={b['mb_rd_knm']:.1f})"
    )

    print("LIVE LTB VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
