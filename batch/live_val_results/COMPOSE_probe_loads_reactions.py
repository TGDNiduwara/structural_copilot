"""COMPOSE_probe_loads_reactions.py - why did reactions not balance?

Reads back, from the LIVE model: (a) every load record in case 1 with its
PZ value, (b) every support reaction. Verifies whether all 142 self-weight
uniform loads actually registered and whether all 4 supports were exported.
"""
from __future__ import annotations
import sys
sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")
from agent.tool_registry import ToolExecutor
from win32com.client import CastTo

ex = ToolExecutor(robot_visible=True)
ex._ensure_robot()
b = ex.robot

print("connected pid:", b.connected_pid, "cases:", end=" ")
try:
    print(b.structure.Cases.Count)
except Exception as e:
    print("ERR", e)

case = CastTo(b.structure.Cases.Get(1), "IRobotSimpleCase")
n_rec = int(case.Records.Count)
print("case 1 records:", n_rec)

# Load record types: 4 = I_LRT_BAR_UNIFORM, 1 = I_LRT_NODE_FORCE
from win32com.client import constants
sum_pz = 0.0
uniform = node_force = 0
for i in range(1, n_rec + 1):
    rec = case.Records.Get(i)
    t = int(rec.Type)
    if t == 4:
        uniform += 1
        v = CastTo(rec, "IRobotBarUniformRecord")
        pz = float(v.Values.GetValue(2))  # I_BURV_PZ
        objs = v.Objects.Text
        sum_pz += pz
    elif t == 1:
        node_force += 1
print(f"  uniform load records: {uniform}, node-force records: {node_force}")
print(f"  sum(PZ) over all uniform records = {sum_pz:.6f} kN/m (sum of values, "
      f"NOT weighted by length)")

# Now reactions
rx = ex.dispatch("export_reactions", {"case_id": 1})
df = ex.reactions_df
print("\nreactions dataframe shape:", df.shape)
print(df.to_string())
