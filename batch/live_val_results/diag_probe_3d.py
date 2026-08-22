"""
diag_probe_3d.py - decisive probe: same portal as project="3D" (2 calls).
Checks: columns carry load, beam moment diagram is frame-like, utilizations
sane, and per-candidate timing (for grid-run planning).
"""
import sys
import time
sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.design_space import DesignSpace
from batch.headless_driver import HeadlessSession
from validate_diag_spec import make_spec

spec = make_spec()
spec["geometry"]["project"] = "3D"
ds = DesignSpace(spec)
cands = ds.generate_candidates()

with HeadlessSession(visible=False) as s:
    for name, cand in (("light", cands[0]), ("heavy", cands[-1])):
        geom = ds.apply_to_geometry(cand)
        t0 = time.time()
        s.clear_structure("3D")
        s.build_from_spec(geom)
        stab = s.validate_stability()
        print(f"--- {name} stability: ok={stab.get('ok')} "
              f"{stab.get('message', '')[:80]}", flush=True)
        s.solve_all(["static"])
        w = s.get_weight()
        t_solve = time.time() - t0
        df = s.bridge.get_utilization_ratios(case_id=1)
        print(f"=== {name}: {cand['group_choices']} w={w['weight_kg']}kg "
              f"({t_solve:.1f}s incl build+solve) ===", flush=True)
        print(df.to_string(index=False), flush=True)
        forces = s.bridge.export_all_member_forces(case_id=1, divisions=2)
        print(forces.to_string(index=False), flush=True)
print("DIAG3D DONE", flush=True)
