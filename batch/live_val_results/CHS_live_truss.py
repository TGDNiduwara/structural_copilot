"""CHS_live_truss.py - create_truss with explicit CHS sections, self-weight,
solve, reactions vs applied (the equilibrium gate, same as every other live
check tonight). Planar truss in a 3D project -> Y-free mechanism dialog is
auto-answered Yes; in-plane Z results are valid."""
from __future__ import annotations
import sys, json, traceback
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor
from tools.robot_tool import RobotEnum

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
print("before:", b.robot_session_status()["summary"], file=sys.stderr)

def run_tool(tool, args):
    return json.loads(ex.dispatch(tool, args))

run_tool("clear_structure", {"project_type": "3D"})
r = run_tool("create_truss", {"span": 12.0, "height": 2.0, "panels": 6,
                              "top_section": "CHS 88.9x4",
                              "bottom_section": "CHS 88.9x4",
                              "web_section": "CHS 48.3x3.2"})
print("create_truss:", r, file=sys.stderr)
run_tool("create_load_case", {"case_id": 1, "case_name": "SW",
                              "nature": "permanent"})
sw = run_tool("apply_self_weight", {"case_id": 1})
print("apply_self_weight:", {k: sw[k] for k in ("bars", "total_self_weight_kn", "method")})
st = run_tool("check_model_stability", {})
print("stability:", st.get("ok"), st.get("message"), file=sys.stderr)
t0 = __import__("time").time()
sol = run_tool("solve", {})
print(f"solve {sol.get('status')} ({__import__('time').time()-t0:.1f}s)", file=sys.stderr)
run_tool("export_reactions", {"case_id": 1})
df = ex.reactions_df
print(df.to_string())
got = float(df["FZ_kN"].sum())
exp = float(sw["total_self_weight_kn"])
print(f"  sum(FZ) = {got:.4f} kN vs applied {exp:.4f} kN => "
      f"{abs(abs(got)-exp)/exp*100:.3f}% error")
print("after:", b.robot_session_status()["summary"], file=sys.stderr)
