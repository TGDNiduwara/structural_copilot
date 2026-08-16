"""
batch/test_runner.py
====================
[PHASE 5] Batch runner tests - five required scenarios:

  1. FULL RUN      - 12-candidate grid: all evaluated, results in SQLite,
                     weight spot-checks vs known section unit weights,
                     total runtime logged to file.
  2. REUSE         - only ONE Robot process launched for the whole run
                     (session_factory called exactly once).
  3. KILL-RESUME   - fresh 12-candidate run, KILL the Python process after
                     candidate 6; re-run with same run_id resumes at 7.
  4. FAILURE ISOL  - one injected failing (mechanism) candidate in the
                     middle; run continues; others complete.
  5. DEAD-SESSION  - reconnect-after-DialogWatcher-kill path works.

Run: python batch/test_runner.py   (needs a live Robot COM server)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.design_space import DesignSpace
from batch.runner import run_batch
from batch.storage import Storage
from batch.headless_driver import (
    HeadlessSession,
    SolverInstabilityError,
)

# --------------------------------------------------------------------------- #
# Shared test spec (SPEC A from Phase 4, plus load cases/loads so the model
# actually solves). 2D portal frame: 2 columns + 1 beam, pinned base.
# 4 column options x 3 beam options = 12 candidates.
# --------------------------------------------------------------------------- #

SPEC = {
    "geometry": {
        "project": "2D",
        "nodes": [
            {"id": 1, "x": 0, "z": 0},
            {"id": 2, "x": 0, "z": 3},
            {"id": 3, "x": 6, "z": 3},
            {"id": 4, "x": 6, "z": 0},
        ],
        "bars": [
            {"id": 1, "n1": 1, "n2": 2, "section": "HEA 200"},
            {"id": 2, "n1": 2, "n2": 3, "section": "IPE 300"},
            {"id": 3, "n1": 3, "n2": 4, "section": "HEA 200"},
        ],
        "supports": [
            {"node": 1, "type": "pinned"},
            {"node": 4, "type": "pinned"},
        ],
        # A UDL on the beam gives real forces/stresses.
        "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
        "loads": [
            {"kind": "bar_uniform", "bar": 2, "case": 1,
             "direction": "Z", "value": -10.0},
        ],
    },
    "variable_groups": [
        {"group_name": "columns", "bar_ids": [1, 3],
         "candidate_sections": ["HEA 200", "HEA 220", "HEA 240", "HEB 200"]},
        {"group_name": "beam", "bar_ids": [2],
         "candidate_sections": ["IPE 270", "IPE 300", "IPE 330"]},
    ],
    "load_cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
    "analysis_types": ["static"],
    "objective": {
        "minimize": "weight",
        "constraint": "max_utilization <= 1.0 AND buckling_pass == True",
    },
}

#: Unit masses from RobotBridge._SECTION_UNIT_MASS_TABLE (kg/m) - used for
#: manual weight spot-checks against the runner's recorded weights.
UNIT_MASS = {
    "HEA 200": 42.3, "HEA 220": 50.5, "HEA 240": 60.3, "HEB 200": 61.3,
    "IPE 270": 36.1, "IPE 300": 42.2, "IPE 330": 49.1,
}


def _expected_weight(columns: str, beam: str) -> float:
    """Manual weight calc: 2 columns x 3 m + 1 beam x 6 m."""
    return 2 * 3 * UNIT_MASS[columns] + 6 * UNIT_MASS[beam]


def _make_session_factory():
    """Returns (factory, counter_dict) where counter['sessions'] counts how
    many HeadlessSession instances the runner created (== Robot launches)."""
    counter = {"sessions": 0}
    def factory():
        counter["sessions"] += 1
        return HeadlessSession(visible=False)
    return factory, counter

def _spot_check_results(results_df, n_spots=3) -> None:
    """Manually verify weight for a few candidates against unit masses."""
    checked = 0
    for _, row in results_df.iterrows():
        if checked >= n_spots:
            break
        dv = json.loads(row["design_vars_json"])
        choices = dv.get("group_choices", {})
        columns = choices.get("columns")
        beam = choices.get("beam")
        if not columns or not beam:
            continue
        expected = _expected_weight(columns, beam)
        actual = float(row["weight_kg"])
        tol = 0.02 * expected
        assert abs(actual - expected) <= tol, (
            f"weight mismatch for {columns}/{beam}: expected ~{expected:.1f} "
            f"kg, got {actual:.1f} kg")
        assert float(row["max_utilization"]) > 0.0, "utilization must be > 0"
        assert row["buckling_status"] not in (None, ""), "buckling status set"
        assert row["pass_fail"] in ("PASS", "FAIL"), "pass_fail set"
        checked += 1
    assert checked >= 1, "could not spot-check any candidate"
    print(f"  spot-checked {checked} candidate weights vs unit-mass calc OK")


def _run_full_12(db_path, log_path):
    """Runs the full 12-candidate SPEC. Returns (summary, storage)."""
    ds = DesignSpace(SPEC)
    assert ds.candidate_count() == 12, ds.candidate_count()
    summary = run_batch(ds, db_path=db_path, log_path=log_path)
    storage = Storage(db_path=db_path)
    return summary, storage


def test_full_run_and_reuse() -> None:
    """1) Full run + 2) single-process reuse confirmation."""
    print("\n=== TEST 1+2: full 12-candidate run + single-session reuse ===")
    tmpdir = tempfile.mkdtemp(prefix="batch_runner_")
    db = os.path.join(tmpdir, "runs.db")
    log = os.path.join(tmpdir, "runner.log")

    factory, counter = _make_session_factory()
    ds = DesignSpace(SPEC)
    summary = run_batch(ds, db_path=db, log_path=log,
                        session_factory=factory)

    assert summary["status"] == "completed", summary
    assert summary["evaluated"] == 12, summary
    assert summary["failed"] == 0, summary
    assert counter["sessions"] == 1, (
        f"expected ONE Robot launch for the whole run, got "
        f"{counter['sessions']}")

    storage = Storage(db_path=db)
    df = storage.get_all_results(summary["run_id"])
    assert len(df) == 12, f"expected 12 result rows, got {len(df)}"
    evaluated = df[df["candidate_status"] == "evaluated"]
    assert len(evaluated) == 12, f"expected 12 evaluated, got {len(evaluated)}"
    _spot_check_results(evaluated)

    assert os.path.exists(log), "runner.log must exist"
    with open(log, encoding="utf-8") as fh:
        log_text = fh.read()
    assert "ETA ~" in log_text, "progress ETA must be logged"
    assert "[12/12]" in log_text or "checkpoint" in log_text, "progress logged"
    print(f"  run_id={summary['run_id']} elapsed={summary['elapsed_s']}s")
    print(f"  log file has progress/ETA lines: OK")
    print("  ALL 12 evaluated; single Robot session confirmed")

class _MechanismSpec(DesignSpace):
    """Design space whose candidate #6 gets an isolated, unsupported node
    (no bars, no support) - a guaranteed 2D mechanism that the runner's
    pre-solve validate_stability() must catch BEFORE Calculate()."""

    def apply_to_geometry(self, design_vars):
        geom = super().apply_to_geometry(design_vars)
        if isinstance(design_vars, dict):
            idx = design_vars.get("candidate_index")
            if idx == 6:
                geom.setdefault("nodes", []).append(
                    {"id": 999, "x": 50.0, "y": 0.0, "z": 50.0})
        return geom


def test_failure_isolation() -> None:
    """3) One injected mechanism candidate; run continues; others complete."""
    print("\n=== TEST 3: failure isolation (injected mechanism) ===")
    tmpdir = tempfile.mkdtemp(prefix="batch_isolation_")
    db = os.path.join(tmpdir, "runs.db")
    log = os.path.join(tmpdir, "runner.log")

    ds = _MechanismSpec(SPEC)
    summary = run_batch(ds, db_path=db, log_path=log)
    storage = Storage(db_path=db)
    df = storage.get_all_results(summary["run_id"])

    assert summary["status"] == "completed", summary
    assert summary["failed"] == 1, summary
    assert summary["evaluated"] == 11, summary

    failed_rows = df[df["candidate_status"] == "failed"]
    assert len(failed_rows) == 1, len(failed_rows)
    raw = json.loads(failed_rows.iloc[0]["raw_results_json"] or "{}")
    reason = str(raw.get("failure_reason", ""))
    assert "mechanism" in reason.lower(), reason
    print(f"  failed candidate #{failed_rows.iloc[0]['candidate_id']}: "
          f"reason={reason}")
    evaluated = df[df["candidate_status"] == "evaluated"]
    assert len(evaluated) == 11
    print(f"  {len(evaluated)} other candidates evaluated; run completed - OK")

class _KillAfterSecondSolveSession(HeadlessSession):
    """Wraps a real HeadlessSession but force-terminates the OWNED Robot
    process after the SECOND solve_all() call, then raises
    SolverInstabilityError - simulating what the DialogWatcher does when it
    force-kills on an unknown dialog. Lets us deterministically exercise the
    runner's dead-session reconnect path (i)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._solve_count = 0

    def solve_all(self, analysis_types):
        self._solve_count += 1
        if self._solve_count == 2:
            # Simulate DialogWatcher force-terminating the Robot process.
            for pid in list(self._owned_pids):
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=15)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1.0)
            raise SolverInstabilityError(
                "simulated DialogWatcher unknown-dialog force-kill")
        return super().solve_all(analysis_types)


def test_dead_session_recovery() -> None:
    """4) After the safety net kills Robot, the runner reconnects and the
    run continues without relaunching from scratch."""
    print("\n=== TEST 4: dead-session recovery after DialogWatcher kill ===")
    tmpdir = tempfile.mkdtemp(prefix="batch_dead_")
    db = os.path.join(tmpdir, "runs.db")
    log = os.path.join(tmpdir, "runner.log")

    launched = {"n": 0}

    def factory():
        # Count Robot LAUNCHES: the runner calls connect() on the initial
        # session and reconnect() (which re-launches) after the kill.
        s = _KillAfterSecondSolveSession(visible=False)
        orig_connect = s.connect

        def counting_connect():
            launched["n"] += 1
            orig_connect()

        s.connect = counting_connect
        return s

    ds = DesignSpace(SPEC)
    summary = run_batch(ds, db_path=db, log_path=log,
                        session_factory=factory)

    assert summary["status"] == "completed", summary
    # Candidate 2 (the kill target) is recorded failed; the other 11 succeed
    # on the reconnected session.
    assert summary["evaluated"] == 11, summary
    assert summary["failed"] == 1, summary
    assert launched["n"] >= 2, (
        f"expected >=2 Robot launches (initial + reconnect), "
        f"got {launched['n']}")
    assert any("SolverInstabilityError" in f for f in summary["failures"]), \
        summary["failures"]

    storage = Storage(db_path=db)
    df = storage.get_all_results(summary["run_id"])
    evaluated = df[df["candidate_status"] == "evaluated"]
    failed = df[df["candidate_status"] == "failed"]
    assert len(evaluated) == 11, len(evaluated)
    assert len(failed) == 1, len(failed)
    # The reconnect must NOT have restarted the run: candidate 1 and 3..12
    # all have results (the failed one is exactly candidate 2).
    ev_ids = sorted(int(r["candidate_id"]) for _, r in evaluated.iterrows())
    print(f"  evaluated candidate_ids={ev_ids} (failed={list(failed['candidate_id'])})")
    assert 1 in ev_ids and 12 in ev_ids, "run did not restart from scratch"
    print("  dead-session reconnect verified: 11 evaluated, 1 killed candidate")

from batch.headless_driver import _robot_pids

_DRIVER_TEMPLATE = r"""
import json
import sys

sys.path.insert(0, {root!r})
from batch.design_space import DesignSpace
from batch.runner import run_batch

spec = json.load(open(sys.argv[1]))
run_id = int(sys.argv[2])
db = sys.argv[3]
log = sys.argv[4]
run_batch(DesignSpace(spec), run_id=run_id, db_path=db, log_path=log)
"""


def test_kill_and_resume() -> None:
    """5) Real process kill after candidate 6; re-run resumes at 7."""
    print("\n=== TEST 5: kill-and-resume (real subprocess kill) ===")
    tmpdir = tempfile.mkdtemp(prefix="batch_resume_")
    db = os.path.join(tmpdir, "runs.db")
    log = os.path.join(tmpdir, "runner.log")
    spec_path = os.path.join(tmpdir, "spec.json")
    driver_path = os.path.join(tmpdir, "driver.py")
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(SPEC, fh)

    root = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER_TEMPLATE.format(root=root))

    # Pre-create the run + candidates (parent) so the subprocess resumes the
    # SAME run_id - matches the contract run_batch(run_id=...) uses.
    ds = DesignSpace(SPEC)
    storage = Storage(db_path=db)
    run_id = storage.create_run(ds.to_dict(),
                                objective=json.dumps(ds.objective, default=str))
    for cand in ds.generate_candidates():
        storage.add_candidate(run_id, cand)

    robots_before = _robot_pids()
    proc = subprocess.Popen(
        [sys.executable, driver_path, spec_path, str(run_id), db, log],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # Poll the DB for checkpoint >= 6 (candidate 6 completed + checkpointed).
    deadline = time.time() + 300
    checkpoint = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out = (proc.stdout.read() if proc.stdout else "") or ""
            raise AssertionError(f"subprocess exited before candidate 6: {out}")
        poll_storage = Storage(db_path=db)
        rp = poll_storage.get_resume_point(run_id)
        poll_storage.close()
        if rp is not None and rp >= 6:
            checkpoint = rp
            break
        time.sleep(2)
    assert checkpoint is not None, "checkpoint 6 never reached"
    print(f"  candidate 6 checkpointed; killing subprocess (pid {proc.pid})...")
    proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)

    # Re-run with the same run_id -> must resume from candidate 7.
    summary = run_batch(ds, run_id=run_id, db_path=db, log_path=log)
    assert summary["status"] == "completed", summary
    assert summary["resumed_from"] == 6, summary
    assert summary["evaluated"] == 6, summary  # candidates 7..12

    storage = Storage(db_path=db)
    df = storage.get_all_results(run_id)
    assert len(df) == 12, f"expected 12 result rows, got {len(df)}"
    evaluated = df[df["candidate_status"] == "evaluated"]
    assert len(evaluated) == 12, f"expected all 12 evaluated, got {len(evaluated)}"
    _spot_check_results(evaluated)
    print(f"  resumed at 7; all 12 recorded; spot-checked weights OK")

    # Cleanup: the killed subprocess orphaned its Robot. Kill only the PIDs
    # that appeared AFTER we started (never an interactive instance).
    for pid in _robot_pids() - robots_before:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass

def main() -> None:
    print("=" * 72)
    print("Phase 5 - batch runner tests (live Robot COM)")
    print("=" * 72)
    robots_before = _robot_pids()
    try:
        test_full_run_and_reuse()
        test_failure_isolation()
        test_dead_session_recovery()
        test_kill_and_resume()
    finally:
        # Clean up any Robot processes this suite launched (orphans from the
        # kill test); never touch pre-existing interactive instances.
        for pid in _robot_pids() - robots_before:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15)
            except Exception:  # noqa: BLE001
                pass
    print()
    print("ALL PHASE 5 RUNNER TESTS PASSED")


if __name__ == "__main__":
    main()
