"""Extract the full Phase-A report numbers from report.json (offline)."""
import json
import os

VAL = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot\batch\live_val_results"
d = json.load(open(os.path.join(VAL, "report.json"), encoding="utf-8"))

print("== GRID (ground truth, 100-candidate run) ==")
g = d["grid"]
print("  evaluated:", g["evaluated"], "| failed:", g["failed"], "| wall_s:", round(g["wall_s"], 1))
pts = sorted(set(tuple(p) for p in g["frontier_pts"]))
pareto = []
for w, u in pts:
    if not any(w2 <= w and u2 >= u and (w2 < w or u2 > u) for w2, u2 in pareto):
        pareto = [(w2, u2) for w2, u2 in pareto if not (w <= w2 and u >= u2)]
        pareto.append((w, u))
print("  Pareto frontier (weight_kg, performance=1-util):")
for w, u in sorted(pareto):
    print(f"    {w:.1f} kg  perf {u:.4f}  (util {1-u:.4f})")
print("  frontier_n:", len(pareto))

print()
print("== SURROGATE (UCB, budget 40) ==")
s = d["surrogate"]
print("  calls:", s["calls"], "| call_fraction:", s["call_fraction"])
print("  hv:", round(s["hv"], 4), "| hv_grid:", round(s["hv_grid"], 4),
      "| hv_ratio:", round(s["hv_ratio"], 4))
print("  frontier_n:", s["frontier_n"], "| final frontier:", s["frontier_pts"])
print("  training_rows:", s["summary"]["training_rows"],
      "| training_runs:", s["summary"]["training_runs"])
print("  failures:", s["summary"]["failures"])

print()
print("== RESUME (kill + resume same run) ==")
r = d["resume"]
print("  killed_after_evaluated:", r["killed_after_evaluated"])
print("  orphan_robots_killed:", r["orphan_robots_killed"])
print("  candidates_sent_total:", r["candidates_sent_total"],
      "| unique:", r["candidates_sent_unique"])
print("  duplicate_candidates:", r["duplicate_candidates"])
print("  final_evaluated:", r["final_evaluated"], "| ok:", r["ok"])
print("  note:", r["note"])
print("  phase2_wall_s:", r["phase2_wall_s"])

print()
print("== RECONNECT ==")
rc = d["reconnect"]
print("  robot_launches:", rc.get("robot_launches"),
      "| reconnect_logged:", rc.get("reconnect_logged"),
      "| wall_s:", round(rc.get("wall_s", 0), 1))
print("  summary failures:", rc.get("summary", {}).get("failures"))

print()
print("== CROSSRUN (cold vs warm cross-run training) ==")
c = d["crossrun"]
print("  calls:", c["calls"], "| training_rows:", c["training_rows"],
      "| training_runs:", c["training_runs"])
print("  hv:", round(c["hv"], 4), "| hv_ratio:", round(c["hv_ratio"], 4))
print("  cold_run1:", c["cold_run1"])
print("  warm_run2:", c["warm_run2"])
print("  frontier_n:", c["frontier_n"])

print()
print("== EHVI vs UCB ==")
e = d["ehvi"]
print("  EHVI: calls:", e["calls"], "| hv:", round(e["hv"], 4),
      "| hv_ratio:", round(e["hv_ratio"], 4),
      "| call_fraction:", e["call_fraction"])
print("  UCB : calls:", s["calls"], "| hv:", round(s["hv"], 4),
      "| hv_ratio:", round(s["hv_ratio"], 4),
      "| call_fraction:", s["call_fraction"])
print("  EHVI frontier:", e["frontier_pts"])
