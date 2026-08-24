"""
batch/validate_surrogate_live.py
================================
LIVE Robot validation for Phase A (surrogate-guided sizing search).

Design space (step 1): the SAME 2D portal-frame geometry batch/test_runner.py
runs live (2 x 3 m HEA columns + 6 m IPE beam, pinned bases, UDL on the
beam) with the candidate_sections widened to 10 x 10 = 100 candidates -
small enough to exhaustively grid-search as ground truth.

LIVE DIAGNOSTIC FINDINGS (2026-08-22, this build):
  * project "2D" produces an INCOHERENT model via build_structure_from_spec
    on this Robot build (columns carry exactly zero force; beam reactions
    exceed the applied load; moments scale with the beam's own section).
    The Phase-5 live tests never asserted column forces, so this was
    silently present there. => This validator uses project "3D" (the mode
    every robot_tool template uses); the 3D probe reproduced exact portal
    statics (axial 2x30 = wL, M_mid = wL^2/8 - M_end).
  * UDL = -25 kN/m (the README example's canonical load): linear scaling
    puts the light corner at util ~1.24 (FAIL) and the heavy corner at
    ~0.17 (PASS) - a real feasibility boundary for the Pareto gate.

Stages (sequential only - one Robot seat; never run in parallel):
  preflight  license + catalog probe: build/solve the lightest and
             heaviest corners of the grid, record utilization variety.
  grid       run_batch() exhaustive ground truth over all 100 candidates.
  surrogate  cold-start run_surrogate_search(acquisition="ucb",
             budget=40) in a FRESH runs.db (cannot see the grid run).
  resume     subprocess hard-kill mid-run + resume with the same run_id;
             duplicate-call check from the combined runner log.
  reconnect  live dead-session recovery: Robot is force-killed at the
             3rd solve; the loop must reconnect and finish the budget.
  crossrun   second ucb run (new run_id) in the SAME runs.db - the
             cross-run training path against real history.
  ehvi       cold-start ehvi run in a FRESH runs.db, same budget.
  report     hypervolume ratios vs the grid truth, call fractions,
             cross-run before/after calls-to-quality, ucb vs ehvi.

Run:      python batch/validate_surrogate_live.py <stage>
Artifacts in batch/live_val_results/<stage>.json (+ .log/.db files).
A stage refuses to run twice (delete its .json to re-run) so an
accidental re-invocation cannot silently burn Robot calls.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback

import numpy as np

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.design_space import DesignSpace
from batch.pareto import add_strength_margin, compute_pareto_frontier
from batch.storage import Storage
from batch.surrogate_search import (
    _hypervolume2d,
    _ObjectiveNormalizer,
    run_surrogate_search,
)

ROOT = r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
VAL_DIR = os.path.join(ROOT, "batch", "live_val_results")
GRID_DB = os.path.join(VAL_DIR, "grid_runs.db")
SUR_DB = os.path.join(VAL_DIR, "surrogate_runs.db")
EHVI_DB = os.path.join(VAL_DIR, "ehvi_runs.db")
RESUME_DB = os.path.join(VAL_DIR, "resume_runs.db")
RECONNECT_DB = os.path.join(VAL_DIR, "reconnect_runs.db")

#: 10 x 10 = 100 candidates - exhaustive ground truth is affordable.
COLUMNS = [
    "HEA 160",
    "HEA 180",
    "HEA 200",
    "HEA 220",
    "HEA 240",
    "HEB 160",
    "HEB 180",
    "HEB 200",
    "HEB 220",
    "HEB 240",
]
BEAMS = [
    "IPE 200",
    "IPE 220",
    "IPE 240",
    "IPE 270",
    "IPE 300",
    "IPE 330",
    "IPE 360",
    "IPE 400",
    "IPE 450",
    "IPE 500",
]

#: Surrogate budget = 40% of the grid (step 3: "well under" the grid).
BUDGET = 40
PATIENCE = 15


def make_spec():
    """test_runner.py's portal geometry as project "3D" (2D mode is broken
    on this build - see module docstring), UDL -25 kN/m (README example
    load) so the grid spans a real FAIL/PASS boundary, with 10x10
    candidate_sections."""
    return {
        "geometry": {
            "project": "3D",
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
            "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
            "loads": [
                {"kind": "bar_uniform", "bar": 2, "case": 1, "direction": "Z", "value": -25.0}
            ],
        },
        "variable_groups": [
            {"group_name": "columns", "bar_ids": [1, 3], "candidate_sections": list(COLUMNS)},
            {"group_name": "beam", "bar_ids": [2], "candidate_sections": list(BEAMS)},
        ],
        "load_cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
        "analysis_types": ["static"],
        "objective": {
            "minimize": "weight",
            "constraint": "max_utilization <= 1.0 AND buckling_pass == True",
        },
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _robot_pids() -> set:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq robot.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        return {
            int(ln.split()[1])
            for ln in out.splitlines()
            if "robot.exe" in ln.lower() and len(ln.split()) > 1
        }
    except Exception:
        return set()


def _taskkill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
    except Exception:
        pass


def _baseline_pids() -> set:
    """Robot PIDs recorded by the preflight stage (untouchable)."""
    try:
        with open(os.path.join(VAL_DIR, "preflight.json"), encoding="utf-8") as fh:
            return set(json.load(fh).get("baseline_robot_pids") or [])
    except Exception:
        return set()


def _kill_orphans() -> list:
    """Kill robot.exe processes NOT in the preflight baseline (own runs'
    orphans). Never touches an interactive user's instance."""
    orphans = sorted(_robot_pids() - _baseline_pids())
    for pid in orphans:
        _taskkill(pid)
    return orphans


def _stage_path(stage: str) -> str:
    return os.path.join(VAL_DIR, f"{stage}.json")


def _finish(stage: str, payload: dict) -> None:
    with open(_stage_path(stage), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"[{stage}] DONE -> {_stage_path(stage)}", flush=True)


def _refuse_if_done(stage: str) -> None:
    if os.path.exists(_stage_path(stage)):
        raise RuntimeError(
            f"{_stage_path(stage)} already exists - delete it to re-run "
            f"(guard against burning Robot calls twice)."
        )


def _single_run(storage: Storage) -> int:
    df = storage.get_all_results_all_runs()
    if df.empty:
        raise RuntimeError("no runs in db")
    return int(df["run_id"].iloc[0])


def _frontier_of(storage: Storage, run_id: int):
    df = storage.get_all_results(run_id)
    f = compute_pareto_frontier(df)
    return add_strength_margin(f.copy()) if len(f) else f


def _hv_of(frontier, norm: _ObjectiveNormalizer) -> float:
    if frontier is None or len(frontier) == 0:
        return 0.0
    nw, nm = norm.norm(
        np.asarray(frontier["weight_kg"], dtype=float),
        np.asarray(frontier["strength_margin"], dtype=float),
    )
    return _hypervolume2d(nw, nm, (norm.ref_w, norm.ref_m))


def _grid_normalizer():
    """Objective normalizer FROZEN from the grid ground truth, so every
    run's hypervolume is computed on one identical scale."""
    storage = Storage(db_path=GRID_DB)
    run_id = _single_run(storage)
    f = _frontier_of(storage, run_id)
    norm = _ObjectiveNormalizer().fit(
        np.asarray(f["weight_kg"], dtype=float), np.asarray(f["strength_margin"], dtype=float)
    )
    return norm, f, run_id


def _pairs(frontier) -> list:
    if frontier is None or len(frontier) == 0:
        return []
    return [
        [round(float(w), 2), round(float(m), 4)]
        for w, m in zip(frontier["weight_kg"], frontier["strength_margin"])
    ]


_TRACE_RX = re.compile(r"frontier (?:baseline|improved) at call (\d+): pts \[([^\]]*)\]")


def _trace(log_path: str) -> list:
    """[(call, [(w, m), ...]), ...] frontier snapshots from the log, in
    call order (the quality-vs-calls curve of a run)."""
    out = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            m = _TRACE_RX.search(line)
            if not m:
                continue
            pairs = []
            for tok in m.group(2).split(","):
                tok = tok.strip()
                if not tok:
                    continue
                w, _, mg = tok.partition("/")
                pairs.append((float(w), float(mg)))
            out.append((int(m.group(1)), pairs))
    out.sort(key=lambda t: t[0])
    return out


def _hv_pairs(pairs, norm) -> float:
    if not pairs:
        return 0.0
    nw, nm = norm.norm(np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs]))
    return _hypervolume2d(nw, nm, (norm.ref_w, norm.ref_m))


def _calls_to_quality(trace: list, target_hv: float, norm) -> int:
    """First Robot-call count whose logged frontier reaches target HV
    (-1 when never reached)."""
    for call, pairs in trace:
        if _hv_pairs(pairs, norm) >= target_hv - 1e-9:
            return call
    return -1


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #


def stage_preflight():
    _refuse_if_done("preflight")
    os.makedirs(VAL_DIR, exist_ok=True)
    baseline = sorted(_robot_pids())
    from batch.headless_driver import HeadlessSession

    ds = DesignSpace(make_spec())
    cands = ds.generate_candidates()
    assert len(cands) == 100, len(cands)
    probes = {}
    with HeadlessSession(visible=False) as s:
        for name, cand in (("light", cands[0]), ("heavy", cands[-1])):
            geom = ds.apply_to_geometry(cand)
            t0 = time.time()
            s.clear_structure("3D")
            s.build_from_spec(geom)
            stab = s.validate_stability()
            assert stab.get("ok"), stab
            s.solve_all(["static"])
            w = s.get_weight()
            u = s.get_utilization_summary(case_id=1)
            probes[name] = {
                "sections": cand["group_choices"],
                "weight_kg": w["weight_kg"],
                "max_utilization": u["max_utilization"],
                "elapsed_s": round(time.time() - t0, 1),
            }
    leftovers = _kill_orphans()
    _finish(
        "preflight",
        {
            "baseline_robot_pids": baseline,
            "orphan_pids_killed": leftovers,
            "grid_candidates": 100,
            "budget": BUDGET,
            "probes": probes,
            "util_variety_expected": probes["light"]["max_utilization"]
            != probes["heavy"]["max_utilization"],
        },
    )


def stage_grid():
    _refuse_if_done("grid")
    from batch.runner import run_batch

    _kill_orphans()
    if os.path.exists(GRID_DB):
        raise RuntimeError(f"{GRID_DB} already exists - delete to re-run.")
    log = os.path.join(VAL_DIR, "grid.log")
    t0 = time.time()
    summary = run_batch(DesignSpace(make_spec()), db_path=GRID_DB, log_path=log)
    dt = round(time.time() - t0, 1)
    storage = Storage(db_path=GRID_DB)
    run_id = _single_run(storage)
    f = _frontier_of(storage, run_id)
    _finish(
        "grid",
        {
            "summary": summary,
            "wall_s": dt,
            "run_id": run_id,
            "calls": summary["evaluated"] + summary["failed"],
            "evaluated": summary["evaluated"],
            "failed": summary["failed"],
            "frontier_pts": _pairs(f),
            "frontier_n": len(f),
        },
    )


def stage_surrogate():
    _refuse_if_done("surrogate")
    if os.path.exists(SUR_DB):
        raise RuntimeError(f"{SUR_DB} already exists - delete to re-run.")
    log = os.path.join(VAL_DIR, "surrogate.log")
    t0 = time.time()
    s = run_surrogate_search(
        DesignSpace(make_spec()),
        budget=BUDGET,
        patience=PATIENCE,
        acquisition="ucb",
        db_path=SUR_DB,
        log_path=log,
    )
    dt = round(time.time() - t0, 1)
    norm, grid_f, _ = _grid_normalizer()
    storage = Storage(db_path=SUR_DB)
    f = _frontier_of(storage, s["run_id"])
    hv_grid = _hv_of(grid_f, norm)
    _finish(
        "surrogate",
        {
            "summary": s,
            "wall_s": dt,
            "log": log,
            "calls": s["robot_calls"],
            "frontier_pts": _pairs(f),
            "frontier_n": len(f),
            "hv": _hv_of(f, norm),
            "hv_grid": hv_grid,
            "hv_ratio": _hv_of(f, norm) / hv_grid,
            "call_fraction": s["robot_calls"] / 100.0,
        },
    )


_DRIVER = """
import sys, json
sys.path.insert(0, r"{root}")
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
"""


def stage_resume():
    _refuse_if_done("resume")
    for p in (RESUME_DB,):
        if os.path.exists(p):
            raise RuntimeError(f"{p} already exists - delete to re-run.")
    os.makedirs(VAL_DIR, exist_ok=True)
    log = os.path.join(VAL_DIR, "resume.log")
    driver = os.path.join(VAL_DIR, "resume_driver.py")
    with open(driver, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER.format(root=ROOT))

    # Phase 1: subprocess, hard-killed after >=6 candidates recorded.
    robots_before = _robot_pids()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            driver,
            "0",
            "12",
            RESUME_DB,
            log,
            os.path.join(VAL_DIR, "resume_phase1.json"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    t_kill = None
    deadline = time.time() + 900
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"phase-1 subprocess exited early:\n{out}")
        st = Storage(db_path=RESUME_DB)
        try:
            df = st.get_all_results_all_runs()
            done = 0 if df.empty else int((df["candidate_status"] == "evaluated").sum())
        finally:
            st.close()
        if done >= 6:
            t_kill = done
            break
        time.sleep(3)
    if t_kill is None:
        proc.kill()
        raise RuntimeError("phase-1 never reached 6 evaluated candidates")
    proc.kill()
    proc.wait(timeout=30)
    time.sleep(2)
    orphans = sorted(_robot_pids() - robots_before)
    for pid in orphans:
        _taskkill(pid)

    # Phase 2: resume the SAME run_id in-process, fresh budget of 12.
    st = Storage(db_path=RESUME_DB)
    run_id = _single_run(st)
    st.close()
    t0 = time.time()
    s2 = run_surrogate_search(
        DesignSpace(make_spec()),
        run_id=run_id,
        budget=12,
        patience=15,
        acquisition="ucb",
        db_path=RESUME_DB,
        log_path=log,
    )
    dt = round(time.time() - t0, 1)

    # Duplicate check: every 'candidate N of 100' line across BOTH phases.
    sent = []
    with open(log, encoding="utf-8") as fh:
        for line in fh:
            m = re.search(r"candidate (\d+) of 100", line)
            if m:
                sent.append(int(m.group(1)))
    dupes = sorted({c for c in sent if sent.count(c) > 1})
    st = Storage(db_path=RESUME_DB)
    df = st.get_all_results(run_id)
    final_eval = int((df["candidate_status"] == "evaluated").sum())
    st.close()
    _finish(
        "resume",
        {
            "run_id": run_id,
            "killed_after_evaluated": t_kill,
            "orphan_robots_killed": orphans,
            "phase2_summary": s2,
            "phase2_wall_s": dt,
            "candidates_sent_total": len(sent),
            "candidates_sent_unique": len(set(sent)),
            "duplicate_candidates": dupes,
            "note": "duplicates allowed ONLY for the one candidate in flight "
            "at kill time (never recorded -> re-sent on resume)",
            "final_evaluated": final_eval,
            "ok": len(dupes) <= 1 and final_eval == len(set(sent)),
        },
    )


def stage_reconnect():
    _refuse_if_done("reconnect")
    if os.path.exists(RECONNECT_DB):
        raise RuntimeError(f"{RECONNECT_DB} already exists - delete.")
    from batch.headless_driver import HeadlessSession, SolverInstabilityError

    class _KillAtThirdSolve(HeadlessSession):
        """Force-terminates the OWNED Robot process during the 3rd solve,
        then raises SolverInstabilityError - exactly what the DialogWatcher
        does on an unknown dialog. Exercises the live reconnect path."""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._n = 0

        def solve_all(self, analysis_types):
            self._n += 1
            if self._n == 3:
                for pid in list(self._owned_pids):
                    _taskkill(pid)
                time.sleep(1.0)
                raise SolverInstabilityError("live validation: simulated DialogWatcher force-kill")
            return super().solve_all(analysis_types)

    launches = {"n": 0}

    def factory():
        s = _KillAtThirdSolve(visible=False)
        orig = s.connect

        def counting():
            launches["n"] += 1
            return orig()

        s.connect = counting
        return s

    log = os.path.join(VAL_DIR, "reconnect.log")
    t0 = time.time()
    s = run_surrogate_search(
        DesignSpace(make_spec()),
        budget=9,
        patience=15,
        acquisition="ucb",
        db_path=RECONNECT_DB,
        log_path=log,
        session_factory=factory,
    )
    dt = round(time.time() - t0, 1)
    log_text = open(log, encoding="utf-8").read()
    _finish(
        "reconnect",
        {
            "summary": s,
            "wall_s": dt,
            "robot_launches": launches["n"],
            "reconnect_logged": "session dead - reconnecting" in log_text,
            "failed": s["failed"],
            "evaluated": s["evaluated"],
            "ok": (
                launches["n"] >= 2
                and s["failed"] == 1
                and "session dead - reconnecting" in log_text
                and s["robot_calls"] == 9
            ),
        },
    )


def stage_crossrun():
    _refuse_if_done("crossrun")
    if not os.path.exists(os.path.join(VAL_DIR, "surrogate.json")):
        raise RuntimeError("run the 'surrogate' stage first.")
    log = os.path.join(VAL_DIR, "crossrun.log")
    t0 = time.time()
    s = run_surrogate_search(
        DesignSpace(make_spec()),
        budget=BUDGET,
        patience=PATIENCE,
        acquisition="ucb",
        db_path=SUR_DB,
        log_path=log,
    )  # NEW run, same db
    dt = round(time.time() - t0, 1)
    norm, grid_f, _ = _grid_normalizer()
    st = Storage(db_path=SUR_DB)
    f2 = _frontier_of(st, s["run_id"])
    with open(os.path.join(VAL_DIR, "surrogate.json"), encoding="utf-8") as fh:
        run1 = json.load(fh)
    f1_hv = run1["hv"]
    # calls-to-quality on both runs' traces under the grid normalizer
    tr1 = _trace(os.path.join(VAL_DIR, "surrogate.log"))
    tr2 = _trace(log)
    k_cold = _calls_to_quality(tr1, f1_hv, norm)
    k_warm = _calls_to_quality(tr2, f1_hv, norm)
    _finish(
        "crossrun",
        {
            "summary": s,
            "wall_s": dt,
            "log": log,
            "calls": s["robot_calls"],
            "training_rows": s["training_rows"],
            "training_runs": s["training_runs"],
            "frontier_pts": _pairs(f2),
            "frontier_n": len(f2),
            "hv": _hv_of(f2, norm),
            "hv_ratio": _hv_of(f2, norm) / run1["hv_grid"],
            "cold_run1": {
                "calls": run1["calls"],
                "final_hv": f1_hv,
                "calls_to_own_final_hv": k_cold,
            },
            "warm_run2": {"calls": s["robot_calls"], "calls_to_run1_final_hv": k_warm},
        },
    )


def stage_ehvi():
    _refuse_if_done("ehvi")
    if os.path.exists(EHVI_DB):
        raise RuntimeError(f"{EHVI_DB} already exists - delete to re-run.")
    log = os.path.join(VAL_DIR, "ehvi.log")
    t0 = time.time()
    s = run_surrogate_search(
        DesignSpace(make_spec()),
        budget=BUDGET,
        patience=PATIENCE,
        acquisition="ehvi",
        db_path=EHVI_DB,
        log_path=log,
    )
    dt = round(time.time() - t0, 1)
    norm, grid_f, _ = _grid_normalizer()
    st = Storage(db_path=EHVI_DB)
    f = _frontier_of(st, s["run_id"])
    _finish(
        "ehvi",
        {
            "summary": s,
            "wall_s": dt,
            "log": log,
            "calls": s["robot_calls"],
            "frontier_pts": _pairs(f),
            "frontier_n": len(f),
            "hv": _hv_of(f, norm),
            "hv_ratio": _hv_of(f, norm) / _hv_of(grid_f, norm),
            "call_fraction": s["robot_calls"] / 100.0,
        },
    )


def stage_report():
    """Aggregates every stage json into the final live-validation table."""
    out = {}
    for stage in ("preflight", "grid", "surrogate", "resume", "reconnect", "crossrun", "ehvi"):
        p = _stage_path(stage)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                out[stage] = json.load(fh)
    print(json.dumps(out, indent=2, default=str))
    with open(os.path.join(VAL_DIR, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)


STAGES = {
    "preflight": stage_preflight,
    "grid": stage_grid,
    "surrogate": stage_surrogate,
    "resume": stage_resume,
    "reconnect": stage_reconnect,
    "crossrun": stage_crossrun,
    "ehvi": stage_ehvi,
    "report": stage_report,
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in STAGES:
        print(__doc__)
        print("stages:", " | ".join(STAGES))
        sys.exit(2)
    os.makedirs(VAL_DIR, exist_ok=True)
    stage = sys.argv[1]
    try:
        STAGES[stage]()
    except Exception:  # noqa: BLE001
        err = os.path.join(VAL_DIR, f"{stage}.ERROR.txt")
        with open(err, "w", encoding="utf-8") as fh:
            fh.write(traceback.format_exc())
        print(traceback.format_exc(), flush=True)
        print(f"[{stage}] FAILED -> {err}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
