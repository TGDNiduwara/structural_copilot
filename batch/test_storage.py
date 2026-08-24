"""
batch/test_storage.py — Phase 2 validation.

Creates a fake run with 10 candidates and results, checkpoints at candidate
5, then CLOSES the connection entirely (simulating a real process restart),
reopens a FRESH Storage over the same file, and confirms:
  * get_resume_point returns 5 (resume at candidate 6),
  * all 10 candidates' data is still queryable and correct.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.storage import Storage  # noqa: E402


def check(tag, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {tag} {detail}", flush=True)
    if not cond:
        raise SystemExit(f"assertion failed: {tag}")


fd, db_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
try:
    # ---- phase A: create + populate (first "process") ----
    s = Storage(db_path)
    run_id = s.create_run(
        {"geometry": {"type": "2D"}, "objective": "minimize weight"},
        objective="minimize weight under ULS",
    )
    check("create_run returns int id", isinstance(run_id, int) and run_id >= 1, f"run_id={run_id}")

    candidate_ids = []
    for i in range(10):
        cid = s.add_candidate(run_id, {"beam": f"IPE{300 + i}", "columns": "HEA220"})
        candidate_ids.append(cid)
    check("10 candidates added", len(candidate_ids) == 10, candidate_ids)

    for i, cid in enumerate(candidate_ids):
        s.record_result(
            candidate_id=cid,
            weight_kg=100.0 + i,
            max_utilization=0.5 + 0.04 * i,
            governing_check="combined_normal",
            buckling_status=None,  # Phase-3 placeholder
            pass_fail="PASS" if i < 7 else "FAIL",
            raw_results_json=json.dumps({"bar": 1, "MY_kNm": -45.0 - i}),
        )

    s.update_checkpoint(run_id, 5)
    check("checkpoint set to 5", s.get_resume_point(run_id) == 5)

    # Bonus integrity check: FK enforcement on a bogus run id.
    try:
        s.add_candidate(999999, {"beam": "IPE100"})
        check("FK: bogus run rejected", False, "no error raised")
    except ValueError:
        check("FK: bogus run rejected", True)

    # ---- simulate a real process restart: close connection entirely ----
    s.close()
    check("connection closed (restart simulated)", s._conn is None, "")

    # ---- phase B: reopen FRESH (new Storage instance over same file) ----
    s2 = Storage(db_path)

    rp = s2.get_resume_point(run_id)
    check("get_resume_point == 5 after reopen", rp == 5, f"got {rp}")

    df = s2.get_all_results(run_id)
    check("get_all_results -> 10 rows", len(df) == 10, f"rows={len(df)}")
    check(
        "columns present",
        {
            "candidate_id",
            "weight_kg",
            "max_utilization",
            "governing_check",
            "buckling_status",
            "pass_fail",
            "design_vars_json",
            "evaluated_at",
        }
        <= set(df.columns),
        str(list(df.columns)),
    )

    ok = True
    for i, row in df.iterrows():
        if int(row["candidate_id"]) != candidate_ids[i]:
            ok = False
        if abs(float(row["weight_kg"]) - (100.0 + i)) > 1e-9:
            ok = False
        if abs(float(row["max_utilization"]) - (0.5 + 0.04 * i)) > 1e-9:
            ok = False
        if row["governing_check"] != "combined_normal":
            ok = False
        if row["pass_fail"] != ("PASS" if i < 7 else "FAIL"):
            ok = False
        if "MY_kNm" not in (row["raw_results_json"] or ""):
            ok = False
        if row["candidate_status"] != "evaluated":
            ok = False
        dv = json.loads(row["design_vars_json"])
        if dv["beam"] != f"IPE{300 + i}":
            ok = False
    check("all 10 candidate rows correct after reopen", ok)

    s2.mark_run_status(run_id, "completed")
    check("mark_run_status completed", s2.get_run(run_id)["status"] == "completed")

    s2.close()
    print("\nPHASE 2 STORAGE TEST PASSED", flush=True)
finally:
    try:
        os.remove(db_path)
    except OSError:
        pass
