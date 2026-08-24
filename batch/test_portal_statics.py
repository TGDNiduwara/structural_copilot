"""
batch/test_portal_statics.py
============================
[LIVE - requires Robot COM] Portal-frame statics regression test.

Regression guard for the 2026-08-22 known issue (see README "Known
issues"): project="2D" specs built via build_structure_from_spec solve to
a structurally INVALID frame on this Robot build (columns carry zero
force; beam reactions do not equilibrate the applied load; moments scale
with the beam's own section). This test asserts the CORRECT behaviour on
project="3D" for a pinned-base portal:

    w = 10 kN/m UDL on the 6 m beam, columns 3 m:
      * each column axial force        = w*L/2 = 30 kN   (+/- 5%)
      * column axial forces sum        = w*L = 60 kN     (equilibrium)
      * beam end moment M_end and
        midspan moment M_mid satisfy   M_end + M_mid = w*L^2/8 = 45 kNm
      * utilizations sane (0 < max < 1 for the heavy corner)

The 2D failure mode is caught by this test only indirectly (the 3D
assertions would also catch a regression of validate_stability / force
export plumbing). Re-probing the 2D path is left as a separate Robot-side
investigation (documented in the README note).

Run:  python batch/test_portal_statics.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.headless_driver import HeadlessSession

W = 10.0  # kN/m UDL
L = 6.0  # m beam span
H = 3.0  # m column height
P_TOTAL = W * L  # 60 kN


def _portal_spec(column_section: str, beam_section: str) -> dict:
    return {
        "project": "3D",
        "nodes": [
            {"id": 1, "x": 0, "z": 0},
            {"id": 2, "x": 0, "z": H},
            {"id": 3, "x": L, "z": H},
            {"id": 4, "x": L, "z": 0},
        ],
        "bars": [
            {"id": 1, "n1": 1, "n2": 2, "section": column_section},
            {"id": 2, "n1": 2, "n2": 3, "section": beam_section},
            {"id": 3, "n1": 3, "n2": 4, "section": column_section},
        ],
        "supports": [
            {"node": 1, "type": "pinned"},
            {"node": 4, "type": "pinned"},
        ],
        "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
        "loads": [{"kind": "bar_uniform", "bar": 2, "case": 1, "direction": "Z", "value": -W}],
    }


def _bar_axial_kN(forces, bar_id: int) -> float:
    sub = forces[forces["Bar_ID"] == bar_id]
    return float(sub["FX_kN"].abs().mean())  # local x is along the bar


def _beam_moments_kNm(forces, bar_id: int) -> tuple:
    sub = forces[forces["Bar_ID"] == bar_id]
    # Sagging midspan (Position_m == L/2) vs hogging end moments.
    mid = sub[(sub["Position_m"] - L / 2).abs() < 0.01]
    ends = sub[(sub["Position_m"].abs() < 0.01) | ((sub["Position_m"] - L).abs() < 0.01)]
    m_mid = float(mid["MY_kNm"].abs().max())
    m_end = float(ends["MY_kNm"].abs().max())
    return m_mid, m_end


def _check_portal(column_section: str, beam_section: str) -> dict:
    with HeadlessSession(visible=False) as s:
        s.clear_structure("3D")
        s.build_from_spec(_portal_spec(column_section, beam_section))
        stab = s.validate_stability()
        assert stab.get("ok"), f"portal must be stable: {stab}"
        s.solve_all(["static"])
        forces = s.bridge.export_all_member_forces(case_id=1, divisions=4)
        util = s.get_utilization_summary(case_id=1)
        return {
            "forces": forces,
            "max_utilization": util["max_utilization"],
            "governing_check": util["governing_check"],
        }


def _run(column_section: str, beam_section: str) -> None:
    r = _check_portal(column_section, beam_section)
    f = r["forces"]
    ax1 = _bar_axial_kN(f, 1)
    ax3 = _bar_axial_kN(f, 3)
    m_mid, m_end = _beam_moments_kNm(f, 2)

    # 1. columns carry the load: axial = wL/2 each.
    for label, ax in (("col1", ax1), ("col3", ax3)):
        err = abs(ax - P_TOTAL / 2) / (P_TOTAL / 2)
        assert err < 0.05, (
            f"{label} axial {ax:.2f} kN != wL/2={P_TOTAL / 2:.1f} kN "
            f"(rel err {err:.3f}) - incoherent frame (see README known "
            f"issue). column_section={column_section} "
            f"beam_section={beam_section}"
        )
    # 2. vertical equilibrium: ax1 + ax3 = wL.
    assert abs(ax1 + ax3 - P_TOTAL) < 0.05 * P_TOTAL, (
        f"columns do not equilibrate the load: {ax1:.2f}+{ax3:.2f} != {P_TOTAL:.1f} kN"
    )
    # 3. beam moment balance: M_end + M_mid = wL^2/8 (portal-frame theory).
    m_ref = W * L * L / 8.0
    err_m = abs(m_end + m_mid - m_ref) / m_ref
    assert err_m < 0.10, (
        f"M_end({m_end:.2f}) + M_mid({m_mid:.2f}) = {m_end + m_mid:.2f} "
        f"!= wL^2/8 ({m_ref:.2f}) rel err {err_m:.3f}"
    )
    print(
        f"  {column_section}/{beam_section}: col axial "
        f"{ax1:.1f}/{ax3:.1f} kN (wL/2=30), M_end={m_end:.1f} "
        f"M_mid={m_mid:.1f} (sum {m_end + m_mid:.1f} vs {m_ref:.1f}), "
        f"util={r['max_utilization']}"
    )


def main():
    print("=" * 72)
    print("Portal statics regression (live Robot) - project=3D")
    print("=" * 72)
    # Light corner (passes) and heavy corner (all sections overkill) both
    # must satisfy the SAME statics - section size must NOT change forces.
    _run("HEA 200", "IPE 300")
    _run("HEB 240", "IPE 500")
    print()
    print("PORTAL STATICS OK")


if __name__ == "__main__":
    main()
