"""Quantify the phantom util=0.0 impact on the surrogate's reported HV.
Grid ground truth gives REAL strength_margin for candidates 49 & 68; the
surrogate logged util=0.0 (per-bar N/A) for them after the 19:08 reconnect.
Recompute the surrogate final-frontier HV with corrected margins."""
import sys, json
sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")
import numpy as np
import batch.validate_surrogate_live as vsl
from batch.storage import Storage

# Frozen normalizer from the grid ground truth
norm, grid_f, _ = vsl._grid_normalizer()

# Surrogate's reported final frontier (from report.json)
surr_frontier = [[399.0, 0.2599], [525.0, 0.574], [580.2, 0.6525],
                 [678.6, 0.7327], [651.6, 0.6887], [705.0, 1.0]]
hv_reported = vsl._hv_pairs(surr_frontier, norm)

# Real margins from the grid for the SAME candidates
g = Storage(vsl.GRID_DB).get_all_results(1)
real = {}
for cid in (49, 68):
    row = g[g["candidate_id"] == cid]
    if not row.empty:
        u = float(row.iloc[0]["max_utilization"])
        w = float(row.iloc[0]["weight_kg"])
        real[w] = 1.0 - u
        print(f"candidate {cid}: grid w={w} util={u} -> true margin {1-u:.4f}")

# Corrected frontier: swap phantom points for grid-truth margins, then Pareto-filter
pts = []
for w, m in surr_frontier:
    m_true = real.get(w, m)   # replace phantom 1.0 with grid truth where known
    pts.append((w, m_true))
# Pareto-filter (min weight, max margin)
pts = sorted(set(pts))
pareto = []
for w, m in pts:
    if not any(w2 <= w and m2 >= m and (w2 < w or m2 > m) for w2, m2 in pareto):
        pareto = [(w2, m2) for w2, m2 in pareto if not (w <= w2 and m >= m2)]
        pareto.append((w, m))
print("\nreported frontier:", surr_frontier)
print("corrected frontier:", sorted(pareto))
hv_corrected = vsl._hv_pairs(pareto, norm)
hv_grid = vsl._hv_of(grid_f, norm)
print(f"\nhv_reported  = {hv_reported:.4f}  (hv_ratio {hv_reported/hv_grid:.4f})")
print(f"hv_corrected = {hv_corrected:.4f}  (hv_ratio {hv_corrected/hv_grid:.4f})")
print(f"grid hv      = {hv_grid:.4f}")
