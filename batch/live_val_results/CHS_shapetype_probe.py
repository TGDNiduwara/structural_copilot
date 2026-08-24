import sys, traceback
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor
from win32com.client import CastTo
from tools.robot_tool import RobotEnum

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
print("before:", b.robot_session_status()["summary"], file=sys.stderr)
b.new_3d_frame()
names = ["CHS 139.7x5", "CHS 88.9x4", "CHS 48.3x3.2", "CHS 219.1x8",
         "RHS 150x100x6", "SHS 100x100x5"]
for n in names:
    try:
        b.create_node(1,0,0,0); b.create_node(2,1,0,0)
        b.create_bar(1, 1, 2, n)
        lab = b.structure.Labels.Get(RobotEnum.I_LT_BAR_SECTION, n)
        st = int(lab.Data.ShapeType)
        a = float(lab.Data.GetValue(0))
        # probe h/b/tw/tf/r indexes for CHS (12=h,13=b,14=tw,15=tf,16=r)
        vals = {}
        for k in (12,13,14,15,16):
            try: vals[k] = float(lab.Data.GetValue(k))
            except Exception: vals[k] = None
        print(f"{n:<16} ShapeType={st}  A_m2={a:.6f}  h/b/tw/tf/r={vals}")
        b.new_3d_frame()
    except Exception as e:
        print(n, "ERR", e)
print("after:", b.robot_session_status()["summary"], file=sys.stderr)
