"""
batch/live_val_results/diag_probe.py - one-off diagnostic (3 Robot calls).
Prints the FULL per-bar utilization table for light / mid / heavy corners
to find which bar+check governs and whether fy_MPa is sane.
"""
import sys
sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.design_space import DesignSpace
from batch.headless_driver import HeadlessSession
from validate_diag_spec import make_spec

ds = DesignSpace(make_spec())
cands = ds.generate_candidates()
probes = [("light", cands[0]), ("mid", cands[54]), ("heavy", cands[-1])]

with HeadlessSession(visible=False) as s:
    for name, cand in probes:
        geom = ds.apply_to_geometry(cand)
        s.clear_structure("2D")
        s.build_from_spec(geom)
        s.solve_all(["static"])
        w = s.get_weight()
        df = s.bridge.get_utilization_ratios(case_id=1)
        print(f"=== {name}: {cand['group_choices']} w={w['weight_kg']}kg ===",
              flush=True)
        print(df.to_string(index=False), flush=True)
        # Also raw bar forces for the governing bar sanity:
        forces = s.bridge.export_all_member_forces(case_id=1, divisions=4)
        print(forces.to_string(index=False), flush=True)
print("DIAG DONE", flush=True)
