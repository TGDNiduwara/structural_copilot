
import sys, json
sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")
from batch.design_space import DesignSpace
from batch.validate_surrogate_live import make_spec
from batch.surrogate_search import run_surrogate_search
run_id = int(sys.argv[1]) if sys.argv[1] != "0" else None
budget = int(sys.argv[2])
s = run_surrogate_search(DesignSpace(make_spec()), run_id=run_id,
                         budget=budget, patience=15, acquisition="ucb",
                         db_path=sys.argv[3], log_path=sys.argv[4])
with open(sys.argv[5], "w") as fh:
    json.dump(s, fh, indent=2, default=str)
print("DRIVER DONE", s["status"], s["robot_calls"], flush=True)
