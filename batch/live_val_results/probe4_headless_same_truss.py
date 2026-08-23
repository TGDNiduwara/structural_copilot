"""probe4_headless_same_truss.py - runs the SAME 20m/3m/6-panel Pratt truss
with L 50x50x5 web + self-weight through the HEADLESS batch path and
checks reactions vs applied load. Isolates whether the zero-results bug is
interactive-app-only or physics-wide."""
from __future__ import annotations
import sys
ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
sys.path.insert(0, ROOT)
from batch.headless_driver import HeadlessSession
from tools.robot_tool import RobotBridge, RobotEnum

with HeadlessSession(visible=False) as s:
    b = s.bridge
    geom = RobotBridge.truss_spec(span=20.0, height=3.0, panels=6,
                                  web_section="L 50x50x5")
    s.clear_structure("3D")
    s.build_from_spec(geom)
    print("bars:", len(s.bridge._bar_endpoints))
    s.bridge.create_load_case(1, "SW",
                              RobotEnum.I_CN_PERMANENT)
    summ = s.bridge.apply_self_weight(1)
    total = summ["total_self_weight_kn"]
    print("applied total self-weight (kN):", total)
    r_stab = s.validate_stability()
    print("stability ok:", r_stab["ok"], r_stab["message"])
    s.solve_all(["static"])
    reac = b.export_reactions(case_id=1)
    print(reac.to_string())
    sum_fz = float(reac["FZ_kN"].abs().sum())
    print("sum|FZ|:", round(sum_fz, 4), "vs applied:", round(total, 4),
          "rel_err:", round(abs(sum_fz - total) / total * 100, 3), "%")
