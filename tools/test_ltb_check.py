"""
tools/test_ltb_check.py
=======================
[EUROCODE Phase C] Lateral-torsional buckling tests — pure Python, no Robot
COM required.

KNOWN-ANSWER ORACLE (independent hand calc, all intermediate values shown):
IPE 300, S275, simply supported, span 6 m, UDL, load at shear center.
Published section properties: Iz=6.04e-6 m4, It=2.01e-7 m4, Iw=1.259e-7
m6, Wy=5.571e-4 m3, A=5.381e-3 m2, Iy=8.356e-5 m4. E=210 GPa, G=81 GPa.

  Mcr   = 1.13 * (pi^2*210e9*6.04e-6 / 6^2)
          * sqrt(1.259e-7/6.04e-6 + 36*81e9*2.01e-7/(pi^2*210e9*6.04e-6))
        = 1.13 * 347,742 * sqrt(0.020844 + 0.046819) = 102.2 kNm
  lam_LT = sqrt(5.571e-4 * 275e6 / 102.2e3) = 1.224
  alpha_LT (h/b = 2.0 -> curve a) = 0.21
  Phi_LT = 0.5*(1 + 0.21*(1.224-0.4) + 0.75*1.224^2) = 1.149
  chi_LT = 1/(1.149 + sqrt(1.149^2 - 0.75*1.224^2)) = 0.629
  Mb,Rd  = 0.629 * 5.571e-4 * 275e6 / 1.0 = 96.3 kNm

D8 NOTE: the Designers' Guide to EN 1993-1-1 worked-example numbers are
the agreed oracle; this independent hand calc stands in until the
published numbers are pasted (the assertions below are the swap point).

Also validates: C1-from-moment-shape classification, lambda_LT plateau,
It/Iw closed forms vs published values, the NOT_CHECKABLE gates (non-I /
no dims / Class 4), and the effective-yield-strength EN-grade capping.

Run:  python tools/test_ltb_check.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.eurocode_params import effective_yield_strength, fy_for_grade
from tools.ltb_check import (
    c1_from_moment_shape,
    check_ltb_member,
    chi_lt_reduction,
    lt_slenderness,
    mb_rd,
    mcr_closed_form,
)
from tools.section_data import it_from_dims, iw_from_dims

# Published IPE 300 properties (m / m2 / m3 / m4 / m6).
IPE300 = {
    "shape_type": 20,
    "shape_kind": "i",
    "complete": True,
    "area_m2": 0.005381,
    "iy_m4": 8.356e-5,
    "iz_m4": 6.04e-6,
    "h_m": 0.3,
    "b_m": 0.15,
    "tw_m": 0.0071,
    "tf_m": 0.0107,
    "r_m": 0.015,
    "wy_m3": 5.571e-4,
    "wpl_y_m3": 6.284e-4,
}
E_PA, G_PA = 210e9, 81e9
FY_S275 = 275e6
L = 6.0


def _udl_moments(m_max, stations: int = 9):
    """M(frac) = Mmax * 4*frac*(1-frac) — simply-supported UDL shape."""
    out = []
    for i in range(stations):
        f = i / (stations - 1)
        out.append((f, m_max * 4.0 * f * (1.0 - f)))
    return out


def test_hand_calc_mcr_chi_mb():
    # Uses PUBLISHED It/Iw so the oracle is independent of our closed forms.
    mcr = mcr_closed_form(E_PA, G_PA, IPE300["iz_m4"], 2.01e-7, 1.259e-7, L, c1=1.13)
    assert abs(mcr / 1e3 - 102.2) < 1.0, f"Mcr {mcr / 1e3:.1f} kNm vs 102.2"
    lam = lt_slenderness(IPE300["wy_m3"], FY_S275, mcr)
    assert abs(lam - 1.224) < 0.02, f"lam_LT {lam:.3f} vs 1.224"
    chi = chi_lt_reduction(0.21, lam)
    assert abs(chi - 0.629) < 0.01, f"chi_LT {chi:.3f} vs 0.629"
    mb = mb_rd(chi, IPE300["wy_m3"], FY_S275) / 1e3
    assert abs(mb - 96.3) < 1.0, f"Mb,Rd {mb:.1f} kNm vs 96.3"
    print(
        f"  OK: Mcr={mcr / 1e3:.2f} kNm, lam_LT={lam:.3f}, "
        f"chi_LT={chi:.3f}, Mb,Rd={mb:.2f} kNm  (hand calc)"
    )
    return mcr, lam, chi, mb


def test_full_member_check_pass_and_fail():
    res = check_ltb_member(
        IPE300, FY_S275, L, _udl_moments(80e3), lcr_lt=6.0, lcr_lt_source="explicit"
    )
    assert res["status"] == "PASS", res
    assert res["c1_case"] == "distributed load (UDL)"
    assert abs(res["c1"] - 1.13) < 1e-6
    assert 0.80 < res["utilization"] < 0.85, res["utilization"]
    assert res["lcr_lt_source"] == "explicit"
    res_f = check_ltb_member(
        IPE300, FY_S275, L, _udl_moments(120e3), lcr_lt=6.0, lcr_lt_source="explicit"
    )
    assert res_f["status"] == "FAIL", res_f
    assert res_f["governing"] == "ltb"
    print(
        f"  OK: member PASS util={res['utilization']:.3f}; "
        f"FAIL util={res_f['utilization']:.3f} (>1.0)"
    )


def test_beam_column_interaction_governs():
    # Compression + UDL moment: eq(6.61)/(6.62) must govern over pure LTB.
    # Realistic case: 200 kN compression, minor axis braced at mid-height
    # (lcr_z = 3.0 m) -> still PASSES, but eq62 > util_ltb.
    res = check_ltb_member(
        IPE300,
        FY_S275,
        L,
        _udl_moments(40e3),
        axial_n=-200e3,
        lcr_lt=6.0,
        lcr_lt_source="explicit",
        lcr_y=6.0,
        lcr_z=3.0,
        lcr_z_source="explicit",
    )
    assert res["status"] == "PASS", res
    assert "eq61" in res and "eq62" in res
    assert res["utilization"] >= res["util_ltb"], "interaction must not reduce utilization"
    assert res["governing"] in ("eq61", "eq62")
    assert res["utilization"] == res["eq62"], "eq62 (weak-axis) governs here"
    print(
        f"  OK: beam-column interaction governs ({res['governing']}= "
        f"{res['utilization']}) over LTB util {res['util_ltb']}"
    )


def test_c1_from_moment_shape():
    uni = [(0.0, 50e3), (0.5, 50e3), (1.0, 50e3)]
    c1, case = c1_from_moment_shape(uni)
    assert c1 == 1.0 and "uniform" in case
    c1, case = c1_from_moment_shape(_udl_moments(80e3))
    assert abs(c1 - 1.13) < 1e-9 and "distributed" in case
    pts = [(i / 8, 2 * 80e3 * min(i / 8, 1 - i / 8)) for i in range(9)]
    c1, case = c1_from_moment_shape(pts)
    assert abs(c1 - 1.35) < 1e-9 and "concentrated" in case
    c1, case = c1_from_moment_shape([(0.0, 100e3), (0.5, 50e3), (1.0, 0.0)])
    assert abs(c1 - 1.77) < 1e-6 and "end moments" in case
    print("  OK: C1 classification (uniform=1.0, UDL=1.13, point=1.35, end psi=0 -> 1.77)")


def test_chi_plateau_and_closed_forms():
    assert chi_lt_reduction(0.21, 0.3) == 1.0, "lambda_LT <= 0.4 -> chi=1"
    it = it_from_dims(0.3, 0.15, 0.0071, 0.0107, 0.015)
    iw = iw_from_dims(6.04e-6, 0.3, 0.0107)
    assert 0.85 < it / 2.01e-7 < 0.95, f"It {it} vs 2.01e-7 (10% low ok)"
    assert 0.99 < iw / 1.259e-7 < 1.01, f"Iw {iw} vs 1.259e-7"
    print(f"  OK: chi plateau; It={it:.3e} (vs 2.01e-7), Iw={iw:.3e} (vs 1.259e-7)")


def test_not_checkable_gates():
    ch = dict(IPE300, shape_kind="channel", shape_type=38)
    res = check_ltb_member(ch, FY_S275, L, _udl_moments(80e3))
    assert res["status"] == "NOT_CHECKABLE"
    no_dims = dict(IPE300, complete=False, h_m=0.0, b_m=0.0)
    res = check_ltb_member(no_dims, FY_S275, L, _udl_moments(80e3))
    assert res["status"] == "NOT_CHECKABLE"
    slim = dict(IPE300, h_m=0.8, b_m=0.08, tw_m=0.003, tf_m=0.004, r_m=0.0)
    res = check_ltb_member(slim, FY_S275, L, _udl_moments(80e3))
    assert res["status"] == "NOT_CHECKABLE"
    assert "Class 4" in res.get("reason", "")
    print("  OK: NOT_CHECKABLE gates (non-I / no dims / Class 4)")


def test_defaulted_bracing_warning():
    res = check_ltb_member(
        IPE300,
        FY_S275,
        L,
        _udl_moments(80e3),
        lcr_lt_source="defaulted",
        warnings=["bar 1: no explicit lcr_lt set - defaulted (conservative)."],
    )
    assert res["lcr_lt_source"] == "defaulted"
    assert any("defaulted" in w.lower() for w in res["warnings"])
    print("  OK: defaulted Lcr_LT carried with its warning")


def test_effective_yield_strength():
    assert effective_yield_strength(355.0, "S355", 10.0)[0] == 355.0
    fy, src = effective_yield_strength(400.0, "S355", 25.0)
    assert fy == 345.0 and "EN 10025-2" in src, "RE=400 must be capped to 345"
    assert fy_for_grade("S355", 60.0) == 335.0
    assert effective_yield_strength(248.2, "STEEL", 10.0)[0] == 248.2
    print("  OK: effective fy (S355@25mm -> 345 cap; STEEL RE passthrough)")


def main():
    print("=" * 72)
    print("EUROCODE Phase C — lateral-torsional buckling tests")
    print("=" * 72)
    test_hand_calc_mcr_chi_mb()
    test_full_member_check_pass_and_fail()
    test_beam_column_interaction_governs()
    test_c1_from_moment_shape()
    test_chi_plateau_and_closed_forms()
    test_not_checkable_gates()
    test_defaulted_bracing_warning()
    test_effective_yield_strength()
    print("ALL PHASE C TESTS PASSED")


if __name__ == "__main__":
    main()
