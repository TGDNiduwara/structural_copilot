"""CHS_mass_probe.py - live CHS area / unit-mass / self-weight check."""
from __future__ import annotations
import sys, math, traceback
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor
from tools.robot_tool import RobotEnum

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
print("before:", b.robot_session_status()["summary"], file=sys.stderr)
b.new_3d_frame()

sizes = [("CHS 139.7x5", 139.7, 5.0), ("CHS 88.9x4", 88.9, 4.0),
         ("CHS 48.3x3.2", 48.3, 3.2)]
print(f"{'section':<14}{'live_A_mm2':>12}{'live_mass':>12}{'hand_mass':>12}{'err%':>8}")
for name, D, t in sizes:
    try:
        b.create_node(1, 0, 0, 0); b.create_node(2, 1, 0, 0)
        b.create_bar(1, 1, 2, name)
    except Exception as e:
        print(name, "CREATE FAILED", e); continue
    lab = b.structure.Labels.Get(RobotEnum.I_LT_BAR_SECTION, name)
    a_m2 = float(lab.Data.GetValue(0))
    live_mass = a_m2 * 7850.0
    hand = math.pi * t * (D - t) * 1e-6 * 7850.0
    err = abs(live_mass - hand) / hand * 100
    print(f"{name:<14}{a_m2*1e6:>12.2f}{live_mass:>12.3f}{hand:>12.3f}{err:>7.2f}")
    b.new_3d_frame()

# apply_self_weight on a single 5 m CHS 139.7x5 bar (length known).
b.create_node(1, 0, 0, 0); b.create_node(2, 5, 0, 0)
b.create_bar(1, 1, 2, "CHS 139.7x5")
b.create_load_case(1, "SW", nature=RobotEnum.I_CN_PERMANENT)
sw = b.apply_self_weight(1)
print("\napply_self_weight on 5 m CHS 139.7x5:")
print("  reported total_self_weight_kn:", sw["total_self_weight_kn"])
print("  bars:", sw["bars"], "method:", sw["method"])
kn_m = 16.610 * 9.81 / 1000.0
hand_kn = kn_m * 5.0
print(f"  hand calc: {kn_m:.5f} kN/m * 5 m = {hand_kn:.5f} kN")
err2 = abs(sw["total_self_weight_kn"] - hand_kn) / hand_kn * 100
print(f"  error: {err2:.3f}%")
print("after:", b.robot_session_status()["summary"], file=sys.stderr)
