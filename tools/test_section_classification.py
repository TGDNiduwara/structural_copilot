"""
tools/test_section_classification.py
====================================
[EUROCODE Phase B] Cross-section classification tests — pure Python, no
Robot COM required.

Known-answer cases are hand-calculated from EN 1993-1-1 Table 5.2:
  * IPE 300, S235, pure bending  -> Class 1
    (flange c = (150-7.1)/2-15 = 56.45 mm, c/tf = 5.28 <= 9e; web
     c = 300-2*10.7-2*15 = 248.6 mm, c/tw = 35.0 <= 72e)   [e = 1.0]
  * IPE 300, S235, pure compression -> Class 2
    (web compression limits 33e/38e: 35.0 is between 33 and 38)
  * very slender built-up plate (h=800, b=80, tw=3, tf=4, r=0) -> Class 4
    (web c/tw = 264 > 124e) — the D7 NOT_CHECKABLE trigger
  * e scales with grade: S355 -> e = sqrt(235/355) = 0.814

Also exercises the full PURE extraction path: a fake GetValue callable
(probe-verified IPE 300 values) -> extract_section_props ->
classify_section, and the parametric-without-dims NOT_CHECKABLE path.

Run:  python tools/test_section_classification.py
"""

from __future__ import annotations

import math
import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.section_classification import classify_section
from tools.section_data import extract_section_props


def _ipe300_props():
    """Probe-verified live IPE 300 dimensions (tools/probe_section_data.py):
    A=0.005381, Iy=8.4e-5, Iz=6e-6, h=0.3, b=0.15, tw=0.0071, tf=0.0107,
    r=0.015, Wpl,y=0.000628.  ShapeType 20 = rolled I."""
    return {
        "shape_type": 20,
        "shape_kind": "i",
        "complete": True,
        "area_m2": 0.005381,
        "iy_m4": 8.4e-5,
        "iz_m4": 6e-6,
        "h_m": 0.3,
        "b_m": 0.15,
        "tw_m": 0.0071,
        "tf_m": 0.0107,
        "r_m": 0.015,
        "wy_m3": 0.000557,
        "wpl_y_m3": 0.000628,
    }


def test_ipe300_bending_class_1():
    res = classify_section(_ipe300_props(), 235.0, "bending")
    assert res["class"] == 1, f"IPE 300 S235 bending must be Class 1: {res}"
    assert res["web_class"] == 1 and res["flange_class"] == 1
    assert abs(res["eps"] - 1.0) < 1e-9
    assert abs(res["flange_c_t"] - (56.45 / 10.7)) < 0.01
    assert abs(res["web_c_t"] - (248.6 / 7.1)) < 0.01
    print("  OK: IPE 300 S235 bending -> Class 1 (flange 5.28<=9, web 35.0<=72)")


def test_ipe300_compression_class_2():
    res = classify_section(_ipe300_props(), 235.0, "compression")
    # web c/tw = 35.0 is between the Class-1 (33e) and Class-2 (38e)
    # compression limits -> web Class 2 -> overall Class 2.
    assert res["class"] == 2, f"IPE 300 S235 compression must be Class 2: {res}"
    assert res["web_class"] == 2 and res["flange_class"] == 1
    print("  OK: IPE 300 S235 compression -> Class 2 (web 35.0 in (33, 38])")


def test_combined_uses_conservative_web_limits():
    bending = classify_section(_ipe300_props(), 235.0, "bending")
    combined = classify_section(_ipe300_props(), 235.0, "combined")
    assert combined["class"] == 2 and combined["web_class"] == 2
    assert combined["class"] >= bending["class"], (
        "combined must not be LESS slender than pure bending"
    )
    print("  OK: combined state uses conservative pure-compression web limits")


def test_slender_plate_class_4():
    slender = {
        "shape_type": 20,
        "shape_kind": "i",
        "complete": True,
        "area_m2": 1e-3,
        "iy_m4": 1e-5,
        "iz_m4": 1e-6,
        "h_m": 0.8,
        "b_m": 0.08,
        "tw_m": 0.003,
        "tf_m": 0.004,
        "r_m": 0.0,
        "wy_m3": 1e-5,
        "wpl_y_m3": 1e-5,
    }
    res = classify_section(slender, 235.0, "bending")
    assert res["class"] == 4, f"slender web (c/tw=264) must be Class 4: {res}"
    assert res["web_class"] == 4
    assert "Class 4" in res["reason"] and "NOT_CHECKABLE" in res["reason"]
    print("  OK: slender built-up plate -> Class 4 (D7 NOT_CHECKABLE trigger)")


def test_grade_epsilon_scaling():
    res_s235 = classify_section(_ipe300_props(), 235.0, "bending")
    res_s355 = classify_section(_ipe300_props(), 355.0, "bending")
    assert abs(res_s355["eps"] - math.sqrt(235.0 / 355.0)) < 1e-4
    assert res_s355["eps"] < res_s235["eps"]
    # higher grade -> smaller e -> still Class 1 for IPE 300
    assert res_s355["class"] == 1
    print("  OK: epsilon scales with grade (e(S355)=0.814)")


def test_missing_dims_not_checkable():
    custom = {
        "shape_type": 99,
        "shape_kind": "other",
        "complete": False,
        "area_m2": 0.0,
        "iy_m4": 0.0,
        "iz_m4": 0.0,
        "h_m": 0.0,
        "b_m": 0.0,
        "tw_m": 0.0,
        "tf_m": 0.0,
        "r_m": 0.0,
        "wy_m3": 0.0,
        "wpl_y_m3": 0.0,
    }
    res = classify_section(custom, 235.0, "bending")
    assert res["class"] is None
    assert "NOT_CHECKABLE" in res["reason"]
    print("  OK: parametric/custom without live dims -> NOT_CHECKABLE")


def test_full_pure_extraction_path():
    # Probe-verified GetValue map for IPE 300 (units m / m2 / m3 / m4).
    probe = {
        0: 0.005381,
        4: 8.4e-5,
        5: 6e-6,
        12: 0.3,
        13: 0.15,
        14: 0.0071,
        15: 0.0107,
        16: 0.015,
        19: 0.000628,
    }
    props = extract_section_props(lambda i: probe.get(i, 0.0), shape_type=20)
    assert props["complete"] is True
    assert abs(props["wy_m3"] - 2 * 8.4e-5 / 0.3) < 1e-9
    res = classify_section(props, 235.0, "bending")
    assert res["class"] == 1
    print("  OK: pure extraction path (fake GetValue) -> Class 1")


def main():
    print("=" * 72)
    print("EUROCODE Phase B — cross-section classification tests")
    print("=" * 72)
    test_ipe300_bending_class_1()
    test_ipe300_compression_class_2()
    test_combined_uses_conservative_web_limits()
    test_slender_plate_class_4()
    test_grade_epsilon_scaling()
    test_missing_dims_not_checkable()
    test_full_pure_extraction_path()
    print("ALL PHASE B TESTS PASSED")


if __name__ == "__main__":
    main()
