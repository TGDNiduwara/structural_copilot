"""Deep-compare grid vs surrogate raw results for the SAME candidate ids,
to determine whether the surrogate's util=0.0 rows are genuine (unloaded
design) or a zero-results artifact (solve returned no forces)."""
import sys, json
sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")
from batch.storage import Storage

g = Storage("batch/live_val_results/grid_runs.db")
s = Storage("batch/live_val_results/surrogate_runs.db")
grid = g.get_all_results(1)
surr = s.get_all_results(1)

for cid in (49, 68):
    gr = grid[grid["candidate_id"] == cid]
    sr = surr[surr["candidate_id"] == cid]
    print(f"\n===== candidate {cid} =====")
    for label, df in (("GRID", gr), ("SURR", sr)):
        if df.empty:
            print(f"  {label}: NOT PRESENT")
            continue
        row = df.iloc[0]
        print(f"  {label}: w={row.get('weight_kg')} util={row.get('max_utilization')} "
              f"status={row.get('candidate_status')} pass={row.get('pass_fail')}")
        print(f"    design_vars: {row.get('design_vars_json')}")
        raw = row.get("raw_results_json")
        try:
            raw = json.loads(raw) if isinstance(raw, str) else raw
            print(f"    raw keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")
            if isinstance(raw, dict):
                for k in ("weight", "utilization", "buckling_status"):
                    if k in raw:
                        v = raw[k]
                        print(f"      {k}: {str(v)[:200]}")
        except Exception as e:
            print(f"    raw parse err: {e}  raw[:150]={str(raw)[:150]}")
