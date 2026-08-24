"""CHS_discover_catalog.py - PROBE live catalogs for CHS/tube/pipe names.
Read-only discovery, NOT assumption: only names that resolve via
LoadFromDBase2 against a live catalog are reported."""
from __future__ import annotations
import sys, json, traceback
sys.path.insert(0, r"c:/Users/dinat/Downloads/structural_multi_app_agent/structural_copilot")
from agent.tool_registry import ToolExecutor
from win32com.client import CastTo
from tools.robot_tool import RobotEnum

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot
print("seat/session before:", b.robot_session_status()["summary"], file=sys.stderr)

cands = [
    "CHS 48.3x3.2", "CHS 60.3x3.2", "CHS 76.1x3.2", "CHS 88.9x4",
    "CHS 114.3x4", "CHS 139.7x5", "CHS 168.3x6", "CHS 219.1x8",
    "CHS 88.9x3.2", "CHS 114.3x5", "CHS 139.7x8", "CHS 42.4x3.2",
    "RO 48.3x3.2", "RO 60.3x3.2", "RO 76.1x3.2", "RO 88.9x4",
    "RO 114.3x4", "RO 139.7x5", "RO 168.3x6", "RO 219.1x8",
    "RO 88.9x3.2", "TUBE 88.9x4", "TUBE 60.3x3.2", "PIPE 88.9x4",
    "PIPE 114.3x4", "D 88.9x4", "D 114.3x4",
    "RHS 120x80x5", "RHS 150x100x6", "RHS 200x100x6",
    "RHS 100x50x4", "RHS 250x150x8",
    "SHS 100x100x5", "SHS 120x120x6", "SHS 150x150x8",
    "SHS 200x200x10", "SHS 80x80x4",
]
found = {}
for db in RobotEnum.SECTION_DATABASES:
    b.new_3d_frame()
    b._ensure_section_catalog_active(db)
    lab = b.structure.Labels.Create(RobotEnum.I_LT_BAR_SECTION, "__CHS_PROBE__")
    sd = CastTo(lab.Data, "IRobotBarSectionData")
    ok = []
    for n in cands:
        try:
            if sd.LoadFromDBase2(n, db):
                ok.append(n)
        except Exception:
            pass
    try:
        b.structure.Labels.Delete(RobotEnum.I_LT_BAR_SECTION, "__CHS_PROBE__")
    except Exception:
        pass
    if ok:
        found[db] = ok
    print(f"  {db:6s}: {len(ok)} resolved: {ok}")
print("== JSON ==")
print(json.dumps(found, indent=1))
print("seat/session after:", b.robot_session_status()["summary"], file=sys.stderr)

