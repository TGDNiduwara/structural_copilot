"""Dump the tool schemas the regression driver will dispatch (offline)."""
import sys, json
sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")
from agent.tool_registry import TOOL_SCHEMAS
want = ("create_truss", "create_load_case", "apply_self_weight",
        "check_model_stability", "solve", "export_reactions",
        "export_all_member_forces", "robot_session_status")
for s in TOOL_SCHEMAS:
    if s["name"] in want:
        print(f"== {s['name']}")
        print(json.dumps(s.get("parameters", {}), indent=1))
