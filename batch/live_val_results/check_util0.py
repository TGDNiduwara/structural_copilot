"""Check the util=0.0 surrogate candidates against the grid ground truth.
If the grid evaluated the same section combo with NONZERO util, the
surrogate's 0.0 is a suspicious zero-results artifact; if the grid never
saw it (RPC-failed or different sections), it's a genuine discovery."""
import sys
sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")
from batch.storage import Storage

g = Storage("batch/live_val_results/grid_runs.db")
s = Storage("batch/live_val_results/surrogate_runs.db")
grid = g.get_all_results(1)
surr = s.get_all_results(1)
print("grid cols:", list(grid.columns))
print("surr cols:", list(surr.columns))

# surrogate util~0 rows
zero = surr[surr["max_utilization"].astype(float).abs() < 1e-9]
print("\nSURROGATE util~0 rows:", len(zero))
for _, r in zero.head(6).iterrows():
    w = r.get("weight_kg")
    dv = r.get("design_vars")
    cand = r.get("candidate_id") or r.get("candidate_index")
    print(f"  cand={cand} w={w} util={r.get('max_utilization')} vars={dv}")
    # find grid rows with same weight
    same_w = grid[grid["weight_kg"].astype(float).round(1) == round(float(w), 1)]
    print(f"    grid rows same weight: {len(same_w)}")
    for _, gr in same_w.head(3).iterrows():
        print(f"      grid: cand={gr.get('candidate_id') or gr.get('candidate_index')} "
              f"w={gr.get('weight_kg')} util={gr.get('max_utilization')} "
              f"status={gr.get('candidate_status')}")
