"""CHS_live_single_beam.py - simply-supported CHS beam vs closed form.
10-element FE beam, span 5 m, pin + roller, UDL 5 kN/m, CHS 139.7x5.
Closed forms: M_mid = wL2/8 ; delta_mid = 5wL4/(384EI).
"""
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

L = 5.0
N = 10
w = 5.0  # kN/m
sec = "CHS 139.7x5"
for i in range(N + 1):
    b.create_node(i + 1, i * L / N, 0.0, 0.0)
for i in range(N):
    b.create_bar(i + 1, i + 1, i + 2, sec)
b.set_support(1, "pinned")
b.set_support(N + 1, "roller_x")
b.set_material("STEEL", apply_to_bars=True)   # E = 210 GPa
b.create_load_case(1, "UDL", nature=RobotEnum.I_CN_PERMANENT)
for i in range(1, N + 1):
    b.apply_bar_load(i, 1, -w, "Z")

sol = b.solve()
print("solve:", sol)

# reactions
rx = b.export_reactions(1)
print(rx.to_string())
fz = float(rx["FZ_kN"].sum())
print(f"  sum(FZ) reactions = {fz:.3f} kN vs applied {w*L:.3f} kN")

# midspan moment: middle bar, station 0.5
mid_bar = N // 2
mdf = b.export_all_member_forces(case_id=1, divisions=4)
sub = mdf[mdf["Bar_ID"] == mid_bar]
row = sub.iloc[-1]  # station 1.0 of the middle bar = TRUE midspan x=L/2
m_mid = float(row["MY_kNm"])
hand_m = w * L * L / 8.0
print(f"  midspan MY = {m_mid:.4f} kNm vs wL2/8 = {hand_m:.4f} kNm "
      f"({abs(m_mid-hand_m)/hand_m*100:.2f}%)")

# midspan deflection
ndf = b.export_node_displacements(1)
n6 = ndf[ndf["Node_ID"] == N // 2 + 1]
uz = float(n6["UZ_mm"].iloc[0]) / 1000.0 if "UZ_mm" in n6.columns else None
if uz is None:
    uz = float(n6["UZ_m"].iloc[0])
# section I from live label
lab = b.structure.Labels.Get(RobotEnum.I_LT_BAR_SECTION, sec)
Iy = float(lab.Data.GetValue(4))
mat = b.set_material("STEEL", apply_to_bars=False)
E = float(mat["e_pa"])
hand_d = 5.0 * w * 1e3 * L**4 / (384.0 * E * Iy)
print(f"  Iy(live)={Iy:.6e} m4  E={E:.3g} Pa")
print(f"  midspan UZ = {uz*1000:.2f} mm vs 5wL4/384EI = {hand_d*1000:.2f} mm "
      f"({abs(abs(uz)-hand_d)/hand_d*100:.2f}%)")
print("after:", b.robot_session_status()["summary"], file=sys.stderr)
