"""tools/test_chs_sections.py - CHS/RHS/SHS offline tests (no COM).

Covers the Phase-1 requirements that do not need a live Robot:
  1. catalog name resolution (verified-name tables, no bare-name guesses)
  2. self-weight / unit-mass: static cache matches hand calc A = pi*t*(D-t)
  3. classification: Table 5.2 sheet 3 (circular) + sheet 1 (rect) classes,
     including the D7 Class-4 gate
  4. LTB: closed hollow sections explicitly NOT_CHECKABLE (no I-section logic)
Run: python tools/test_chs_sections.py
"""
from __future__ import annotations
import math
import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.section_sizing import (available_sections, section_families,
                                 section_depth_mm)
from tools.section_data import shape_kind, has_full_dims
from tools.section_classification import classify_section
from tools.ltb_check import check_ltb_member
from tools.robot_tool import RobotBridge


def chs_props(D_mm, t_mm, fy_mpa=None):
    D = float(D_mm) / 1000.0
    t = float(t_mm) / 1000.0
    A = math.pi * t * (D - t)
    return {
        "shape_type": 36, "shape_kind": "circular_hollow", "complete": True,
        "area_m2": A, "iy_m4": math.pi/64.0*(D**4-(D-2*t)**4),
        "iz_m4": math.pi/64.0*(D**4-(D-2*t)**4),
        "h_m": D, "b_m": D, "tw_m": t, "tf_m": t, "r_m": 0.0,
        "wy_m3": math.pi/32.0*(D**4-(D-2*t)**4)/D, "wpl_y_m3": 0.0,
    }


def rhs_props(h_mm, b_mm, t_mm):
    h = h_mm/1000.0; b = b_mm/1000.0; t = t_mm/1000.0
    return {
        "shape_type": 47, "shape_kind": "rect_hollow", "complete": True,
        "area_m2": 2*t*(h+b-2*t), "iy_m4": 1e-5, "iz_m4": 1e-6,
        "h_m": h, "b_m": b, "tw_m": t, "tf_m": t, "r_m": 0.006,
        "wy_m3": 1e-4, "wpl_y_m3": 1e-4,
    }


def test_catalog_names():
    assert "CHS" in section_families() and "RHS" in section_families()
    assert "SHS" in section_families()
    chs = available_sections("CHS")
    assert len(chs) == 12, f"CHS count={len(chs)}"
    for want in ("CHS 139.7x5", "CHS 114.3x4", "CHS 48.3x3.2",
                 "CHS 219.1x8"):
        assert want in chs, want
    assert "CHS 120" not in chs, "bare size must not be advertised"
    assert "CHS 139.7x5" in available_sections()
    assert "RHS 150x100x6" in available_sections("RHS")
    assert "SHS 100x100x5" in available_sections("SHS")
    assert len(available_sections("SHS")) == 5
    try:
        available_sections("ZZZ")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown family must raise")
    print("  OK: CHS/RHS/SHS verified-name resolution (12/5/5), no bare guesses")


def test_section_depth_parsing():
    assert section_depth_mm("CHS 139.7x5") == 139.7
    assert section_depth_mm("CHS 48.3x3.2") == 48.3
    assert section_depth_mm("RHS 150x100x6") == 150.0
    assert section_depth_mm("SHS 100x100x5") == 100.0
    assert section_depth_mm("IPE 300") == 300.0
    print("  OK: section_depth_mm parses decimal CHS diameters + RHS first dim")


def test_unit_mass_hand_calc():
    b = RobotBridge()  # no COM needed for the static-table path
    cases = [("CHS 139.7x5", 139.7, 5.0), ("CHS 88.9x4", 88.9, 4.0),
             ("CHS 48.3x3.2", 48.3, 3.2)]
    for name, D, t in cases:
        hand = math.pi * t * (D - t) * 1e-6 * 7850.0
        got = b._lookup_unit_mass(name, 7850.0)
        err = abs(got - hand) / hand * 100
        assert err < 1.0, f"{name}: {got} vs hand {hand} ({err:.2f}%)"
        print(f"  OK: {name} mass {got:.3f} kg/m vs hand {hand:.3f} ({err:.2f}%)")


def test_classification_circular():
    # S235: eps=1 -> Class 1: D/t<=50, Class 2: <=70, Class 3: <=90
    c1 = classify_section(chs_props(139.7, 5), 235.0, "bending")
    assert c1["class"] == 1, c1
    assert abs(c1["web_c_t"] - 139.7/5.0) < 0.01
    assert "circular hollow" in c1["reason"].lower()
    # same D/t in compression (sheet 3 single series)
    c1c = classify_section(chs_props(139.7, 5), 235.0, "compression")
    assert c1c["class"] == 1, c1c
    # S355: e2 = 0.814^2 = 0.663; 50*e2 = 33.1 -> 88.9/3.2=27.8 still Class 1
    c355 = classify_section(chs_props(88.9, 3.2), 355.0, "bending")
    assert c355["class"] == 1, c355
    # Class 2 boundary: D/t = 55 -> between 50 and 70 (S235)
    c2 = classify_section(chs_props(110, 2), 235.0, "bending")
    assert c2["class"] == 2, c2
    # Class 3: D/t = 75
    c3 = classify_section(chs_props(150, 2), 235.0, "bending")
    assert c3["class"] == 3, c3
    # Class 4: D/t = 100 -> NOT_CHECKABLE gate
    c4 = classify_section(chs_props(200, 2), 235.0, "bending")
    assert c4["class"] == 4 and "NOT_CHECKABLE" in c4["reason"], c4
    print("  OK: CHS classification (sheet 3) Class 1/2/3/4 + NOT_CHECKABLE")


def test_classification_rect():
    # RHS 150x100x6 S235 bending: web (150-18)/6=22<=72, flange (100-18)/6=13.7
    r = classify_section(rhs_props(150, 100, 6), 235.0, "bending")
    assert r["class"] == 1, r
    # compression: web c/t = (200-15)/5 = 37 -> between 33 and 38 -> Class 2
    r2 = classify_section(rhs_props(200, 100, 5), 235.0, "compression")
    assert r2["class"] == 2, r2
    # slender: (300-15)/5 = 57 > 42 -> Class 4
    r4 = classify_section(rhs_props(300, 150, 5), 235.0, "compression")
    assert r4["class"] == 4 and "NOT_CHECKABLE" in r4["reason"], r4
    print("  OK: RHS classification (sheet 1 internal parts) 1/2/4")


def test_ltb_hollow_gate():
    for kind, props in (("circular_hollow", chs_props(139.7, 5)),
                        ("rect_hollow", rhs_props(150, 100, 6))):
        res = check_ltb_member(props, 355.0e6, 5.0, [])
        assert res["status"] == "NOT_CHECKABLE", res
        assert "closed hollow" in res["reason"].lower(), res["reason"]
        assert res.get("section_kind") == kind
    print("  OK: LTB explicitly NOT_CHECKABLE for closed hollow sections")


def test_shape_kind_map():
    assert shape_kind(36) == "circular_hollow"
    assert shape_kind(47) == "rect_hollow"
    assert has_full_dims({"complete": True, "shape_kind": "circular_hollow"})
    assert has_full_dims({"complete": True, "shape_kind": "rect_hollow"})
    print("  OK: ShapeType 36/47 -> circular_hollow / rect_hollow")


def main():
    print("=" * 72)
    print("CHS / RHS / SHS offline tests (Phase 1)")
    print("=" * 72)
    test_catalog_names()
    test_section_depth_parsing()
    test_unit_mass_hand_calc()
    test_classification_circular()
    test_classification_rect()
    test_ltb_hollow_gate()
    test_shape_kind_map()
    print("ALL CHS TESTS PASSED")


if __name__ == "__main__":
    main()
