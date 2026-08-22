"""
tools/test_connection_check.py
==============================
[EUROCODE Phase D] Simple-shear connection tests — pure Python, no Robot
COM required.

KNOWN-ANSWER ORACLE (hand calc, EN 1993-1-8; gamma_M2 = 1.25):
fin plate, 2 x M20 8.8 bolts, single shear, 10 mm S275 plate,
e1 = e2 = 30 mm, p1 = 60 mm, d0 = 22 mm:
  fub = 800 MPa, fu(S275) = 430 MPa, fy(S275@10mm) = 275 MPa, A = pi*20^2/4
  bolt shear   Fv,Rd = 0.6*800*314.16/1.25 = 120.6 kN/bolt x2 = 241.3 kN
  bearing      alpha_b = min(30/66, 800/430, 1) = 0.4545
               k1 = min(2.8*30/22-1.7, 2.5) = 2.118
               Fb,Rd = 2.118*0.4545*430*20*10/1.25 = 66.2 kN/bolt x2
                      = 132.5 kN   <-- GOVERNS
  block shear  Ant = 10*(30-11) = 190 mm2
               Anv = 2*10*60 + 2*10*30 - 2*10*22 = 1360 mm2
               Veff = 430*190/1.25 + 275*1360/(sqrt3*1.0) = 281.3 kN
  weld (6 mm): a = 0.707*6 = 4.24 mm; L = 60+60 = 120 mm; both sides
               Fw,Rd = 430/(sqrt3*0.85*1.25) * 2*4.24*120 = 237.8 kN

D8 NOTE: the SCI "Green Book" simple-joint numbers are the agreed oracle;
this hand calc stands in until the published numbers are pasted (swap
point).

Run:  python tools/test_connection_check.py
"""

from __future__ import annotations

import math
import sys

sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")

from tools.connection_check import (
    ConnectionRegistry, CONNECTION_DEFAULTS,
    bolt_shear_resistance, bearing_resistance, block_shear_resistance,
    weld_resistance, check_simple_shear_connection)

GAMMA_M2 = 1.25
V_ED = 100.0e3  # 100 kN design shear


def _fin_plate_2m20():
    return dict(CONNECTION_DEFAULTS, connection_type="fin_plate",
                bolt_grade="8.8", bolt_diameter_mm=20, bolt_rows=2,
                pitch_mm=60.0, edge_dist_mm=30.0, end_dist_mm=30.0,
                plate_thickness_mm=10.0, plate_grade="S275", weld_leg_mm=None)


def test_hand_calc_resistances():
    # Independent re-derivation from the EN formulas (all units SI).
    d0 = 22.0
    fv = bolt_shear_resistance("8.8", 20, 2, 1, GAMMA_M2)
    expected_shear = 2 * 0.6 * 800e6 * (math.pi * 20e-3 ** 2 / 4) / GAMMA_M2
    assert abs(fv - expected_shear) / expected_shear < 1e-9
    assert abs(fv / 1e3 - 241.3) < 1.0, f"bolt shear {fv/1e3:.1f} vs 241.3"
    fb = bearing_resistance(430.0, 20, 10, 30, 30, 60, 60, 2, 800.0, GAMMA_M2)
    alpha_b = min(30 / (3 * d0), 800 / 430, 1.0)
    k1 = min(2.8 * 30 / d0 - 1.7, 2.5)
    expected_bearing = 2 * k1 * alpha_b * 430e6 * 20e-3 * 10e-3 / GAMMA_M2
    assert abs(fb - expected_bearing) / expected_bearing < 1e-9
    assert abs(fb / 1e3 - 132.5) < 1.0, f"bearing {fb/1e3:.1f} vs 132.5"
    bs = block_shear_resistance(275.0, 430.0, 10, 30, 60, 2, 20, GAMMA_M2, 1.0)
    ant = 10 * (30 - d0 / 2)
    anv = 2 * 10 * (2 - 1) * 60 + 2 * 10 * 30 - 2 * 10 * d0
    expected_bs = 430e6 * ant * 1e-6 / GAMMA_M2 \
        + 275e6 * anv * 1e-6 / (math.sqrt(3) * 1.0)
    assert abs(bs - expected_bs) / expected_bs < 1e-9
    assert abs(bs / 1e3 - 281.3) < 2.0, f"block shear {bs/1e3:.1f} vs 281.3"
    w = weld_resistance(430.0, 6, 120, "S275", GAMMA_M2)
    throat = 0.707 * 6
    expected_w = (430e6 / (math.sqrt(3) * 0.85 * GAMMA_M2)) \
        * (2 * throat * 120) * 1e-6
    assert abs(w - expected_w) / expected_w < 1e-9
    print("  OK: hand-calc resistances match "
          "(bolt 241.3 / bearing 132.5 / block 281.3 / weld 237.8 kN)")


def test_member_check_pass_fail_and_governing():
    res = check_simple_shear_connection(_fin_plate_2m20(), V_ED)
    assert res["status"] == "PASS", res
    assert res["governing"] == "bearing_plate", res
    assert abs(res["utilization"] - 100.0 / 132.5) < 0.01, res
    res_f = check_simple_shear_connection(_fin_plate_2m20(), 150.0e3)
    assert res_f["status"] == "FAIL", res_f
    assert res_f["governing"] == "bearing_plate"
    assert res_f["utilization"] > 1.0
    print(f"  OK: member PASS util={res['utilization']} "
          f"(bearing governs); FAIL at 150 kN util={res_f['utilization']}")

def test_double_angle_doubles_bolt_shear():
    conn = dict(_fin_plate_2m20(), connection_type="double_angle")
    res = check_simple_shear_connection(conn, V_ED)
    fs = bolt_shear_resistance("8.8", 20, 2, 2, GAMMA_M2)
    assert abs(fs - 2 * 241.3e3) < 100.0
    assert res["status"] == "PASS"
    print("  OK: double angle -> bolt shear x2 planes")


def test_weld_governs_when_added():
    conn = dict(_fin_plate_2m20(), weld_leg_mm=6.0)
    res = check_simple_shear_connection(conn, V_ED)
    assert "weld" in res["checks"]
    assert res["governing"] == "bearing_plate", res  # weld 238 kN > bearing
    thin = dict(_fin_plate_2m20(), weld_leg_mm=3.0)
    res_t = check_simple_shear_connection(thin, V_ED)
    assert res_t["governing"] == "weld", res_t  # 3 mm weld weakens below bearing
    print("  OK: weld check active; 3 mm leg becomes governing")


def test_not_checkable_and_registry():
    reg = ConnectionRegistry()
    reg.set_connection(1, "end")
    assert len(reg) == 1
    assert reg.get(1, "end")["bolt_grade"] == "8.8"
    try:
        reg.set_connection(2, "end", bolt_columns=2)
        raise AssertionError("multi-column must be rejected (v1)")
    except ValueError:
        pass
    try:
        reg.set_connection(2, "end", bolt_grade="12.9")
        raise AssertionError("unknown bolt grade must be rejected")
    except ValueError:
        pass
    assert reg.remove(1) is True and reg.clear() == 0
    res = check_simple_shear_connection({}, V_ED)
    assert res["status"] == "NOT_CHECKABLE"
    print("  OK: registry lifecycle + NOT_CHECKABLE gates")


def test_bridge_and_tool_wiring():
    from tools.robot_tool import RobotBridge
    bridge = RobotBridge()
    assert isinstance(bridge.connections, ConnectionRegistry)
    bridge.connections.set_connection(7, "end")
    assert bridge.connections.get(7, "end") is not None
    from agent.tool_registry import TOOL_SCHEMAS, ToolExecutor
    names = {s["name"] for s in TOOL_SCHEMAS}
    assert "define_connection" in names and "check_connection_capacity" in names
    assert hasattr(ToolExecutor, "_tool_define_connection")
    assert hasattr(ToolExecutor, "_tool_check_connection_capacity")
    print("  OK: bridge side-table + tool registration")


def main():
    print("=" * 72)
    print("EUROCODE Phase D — simple shear connection tests")
    print("=" * 72)
    test_hand_calc_resistances()
    test_member_check_pass_fail_and_governing()
    test_double_angle_doubles_bolt_shear()
    test_weld_governs_when_added()
    test_not_checkable_and_registry()
    test_bridge_and_tool_wiring()
    print("ALL PHASE D TESTS PASSED")


if __name__ == "__main__":
    main()

