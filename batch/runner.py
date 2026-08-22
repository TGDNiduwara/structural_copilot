"""
batch/runner.py
===============
[PHASE 5] Batch runner - the actual optimization loop.

Ties together every prior phase: the reused HeadlessSession (Phase 1), the
pre-solve stability validation + DialogWatcher + solve timeout (Step 2/3
work), the Euler buckling screening (Phase 3), the design-space grid
(Phase 4), and SQLite checkpointing (Phase 2).

run_batch() is fully standalone - no Streamlit, no LLM, no chat loop. It is
the compute core the Phase-7 LLM tools will drive.

Crash-recovery contract (Phase 2/5):
  * update_checkpoint() after EVERY candidate - a crash loses at most one
    candidate's work.
  * Resuming with the same run_id skips candidates up to and including the
    stored checkpoint index.
  * Per-candidate exceptions are caught, logged and the candidate marked
    'failed' with the reason - one bad candidate never aborts the run.
  * After max_consecutive_failures in a row (default 5), the run is
    aborted and marked 'failed' instead of grinding forever.
  * A session whose Robot process was killed by the DialogWatcher / solve
    timeout is detected via is_alive() and RECONNECTED (fresh instance)
    before the next candidate - the DialogWatcher never leaves a dead
    session behind.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from batch.buckling_check import check_euler_buckling, EULER_NOTE
from batch.design_space import DesignSpace, DesignSpaceError
from tools.ltb_check import check_lateral_torsional_buckling
from batch.headless_driver import (
    HeadlessSession,
    MechanismError,
    SolverInstabilityError,
    UnknownDialogError,
)
from batch.storage import Storage

logger = logging.getLogger("structural_copilot.batch.runner")

#: Exception classes that mean the safety net (DialogWatcher / timeout)
#: may have force-terminated the Robot process, requiring a reconnect.
_SOLVER_SAFETY_ERRORS = (SolverInstabilityError, UnknownDialogError,
                         TimeoutError)


def _configure_logging(log_path: str) -> None:
    """File + console logging for unattended runs. Safe to call multiple
    times (duplicate handlers are avoided)."""
    logger.setLevel(logging.INFO)
    log_path = os.path.abspath(log_path)
    # Deduplicate by PATH, not by handler type: a long-running test process
    # calls run_batch with several log files; each must get its own handler.
    existing_paths = {getattr(h, "baseFilename", None) for h in logger.handlers
                      if isinstance(h, logging.FileHandler)}
    if log_path not in existing_paths:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and
               not isinstance(h, logging.FileHandler)
               for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(sh)
def _buckling_for_candidate(
    bridge, util_summary: Dict[str, Any], eval_case_id: int
) -> Tuple[str, bool]:
    """Runs check_euler_buckling for every compression member of the
    candidate and returns (buckling_status_str, buckling_pass_bool).

    Uses the utilization summary's per-bar table to enumerate members, then
    pulls the single solved axial force for the case once (avoiding an
    export per bar) and evaluates each compression member. Tension members
    are skipped (check_euler_buckling would return "member in tension" -
    no number is reported for them, per the Phase-3 scope).

    NOTE: same discipline as the elastic utilization tool - this is a basic
    minor-axis Euler screening, NOT a full code-based buckling/interaction
    check. The caveat is embedded in the returned status string so a user
    cannot miss it.
    """
    per_bar = util_summary.get("per_bar", []) or []
    if not per_bar:
        return "PASS (no members)", True

    # One force export for the whole candidate - reuse across bars.
    try:
        df = bridge.export_all_member_forces(case_id=eval_case_id, divisions=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not export member forces for buckling: %s", exc)
        return "UNKNOWN (force export failed - %s)" % exc, False

    worst: Optional[Dict[str, Any]] = None  # worst = smallest margin Pcr/N
    for row in per_bar:
        bar_id = row.get("bar_id")
        if bar_id is None:
            continue
        sub = df[df["Bar_ID"] == int(bar_id)] if df is not None else None
        if sub is None or sub.empty:
            continue
        # Midspan axial force (approx. constant along a member).
        axial_kN = float(sub["FX_kN"].iloc[len(sub) // 2])
        if axial_kN >= 0.0:
            continue  # tension - not applicable (no misleading number)
        try:
            res = check_euler_buckling(
                bridge, int(bar_id), eval_case_id,
                axial_force_kn=axial_kN,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Buckling check failed for bar %s: %s", bar_id, exc)
            continue
        if not res.get("applies", False):
            continue
        margin = res["Pcr_kN"] / abs(res["applied_axial_kN"])
        if worst is None or margin < worst["_margin"]:
            res["_margin"] = margin
            worst = res

    if worst is None:
        return "PASS (no compression members)", True

    pcr = worst["Pcr_kN"]
    applied = abs(worst["applied_axial_kN"])
    ok = bool(worst["pass_fail"])
    status = "PASS" if ok else "FAIL"
    detail = (f"worst bar {worst['bar_id']} ({worst.get('section')}): "
              f"N={applied:.1f} kN vs Pcr={pcr:.1f} kN, "
              f"KL/r={worst['slenderness_KL_r']}, "
              f"E={worst.get('E_GPa')} GPa. "
              f"{EULER_NOTE}")
    return f"{status}: {detail}", ok


def _evaluate_candidate(
    session: HeadlessSession,
    design_space: DesignSpace,
    design_vars: Dict[str, Any],
    eval_case_id: int,
) -> Dict[str, Any]:
    """Builds + solves + scores one candidate. Returns a dict with keys
    weight_kg, max_utilization, governing_check, buckling_status,
    pass_fail, raw, or raises (caller catches)."""
    geometry = design_space.apply_to_geometry(design_vars)
    proj = str(geometry.get("project") or "3D")
    session.clear_structure(proj)
    session.build_from_spec(geometry)

    # Pre-solve stability validation: never call Calculate() on a mechanism.
    stability = session.validate_stability()
    if not stability.get("ok", True):
        raise MechanismError(stability.get("message", "mechanism detected"))

    session.solve_all(design_space.analysis_types)

    weight = session.get_weight()
    util = session.get_utilization_summary(case_id=eval_case_id)
    buckling_status, buckling_pass = _buckling_for_candidate(
        session.bridge, util, eval_case_id)

    # [EUROCODE Phase E] Optional LTB + connection integration when the
    # design-space objective constraint requests them, e.g.
    #   "max_utilization <= 1.0 AND buckling_pass == True AND
    #    ltb_pass == True AND connection_pass == True"
    objective = (design_space.objective or {}).get("constraint", "") or ""
    want_ltb = "ltb_pass" in str(objective)
    want_conn = "connection_pass" in str(objective)
    ltb_status, ltb_pass = "PASS (not requested)", True
    if want_ltb:
        try:
            ltb_res = check_lateral_torsional_buckling(
                session.bridge, eval_case_id)
            statuses = [r.get("status") for r in ltb_res.get("bars", [])]
            ltb_status = ("FAIL" if "FAIL" in statuses else
                          ("NOT_CHECKABLE" if "NOT_CHECKABLE" in statuses
                           else "PASS"))
            ltb_pass = "FAIL" not in statuses
        except Exception as exc:  # noqa: BLE001
            ltb_status = f"UNKNOWN ({exc})"
            ltb_pass = False
    conn_status, conn_pass = "PASS (not requested)", True
    if want_conn:
        defined = session.bridge.connections.all_connections()
        if not defined:
            conn_status = "PASS (no connections defined)"
        else:
            try:
                statuses = []
                for c in defined:
                    r = session.bridge.check_connection_capacity(
                        c["bar_id"], c["joint_end"], eval_case_id)
                    statuses.append(r.get("status"))
                conn_status = ("FAIL" if "FAIL" in statuses else
                               ("NOT_CHECKABLE" if "NOT_CHECKABLE" in statuses
                                else "PASS"))
                conn_pass = "FAIL" not in statuses
            except Exception as exc:  # noqa: BLE001
                conn_status = f"UNKNOWN ({exc})"
                conn_pass = False

    max_util = util.get("max_utilization")
    gov = util.get("governing_check")
    util_ok = (max_util is not None and max_util <= 1.0)
    pass_fail = bool(util_ok and buckling_pass and ltb_pass and conn_pass)
    return {
        "weight_kg": weight.get("weight_kg"),
        "max_utilization": max_util,
        "governing_check": gov,
        "buckling_status": buckling_status,
        "ltb_status": ltb_status,
        "connection_status": conn_status,
        "pass_fail": pass_fail,
        "raw": {
            "weight": weight,
            "utilization": util,
            "buckling_status": buckling_status,
            "ltb_status": ltb_status,
            "connection_status": conn_status,
            "note": "Elastic stress + Euler buckling screening; LTB "
                    "(§6.3.2.2) and connection (EN 1993-1-8) run only when "
                    "the objective constraint requests ltb_pass / "
                    "connection_pass.",
        },
    }
def _choose_eval_case(design_space: DesignSpace) -> int:
    """The load case used for utilization/buckling scoring.

    If the design space defines load_cases, score the FIRST one (or the
    caller can pass a case id explicitly later). Returns 1 when the design
    space carries no explicit load cases (matching the conversational app's
    default case numbering)."""
    cases = design_space.load_cases or []
    if cases:
        return int(cases[0].get("id", 1))
    return 1


def _load_pending(
    storage: Storage, run_id: int, resume_index: Optional[int]
) -> List[Dict[str, Any]]:
    """Loads candidate rows from storage and returns the pending ones
    (design_vars + storage candidate_id), ordered by generation order.

    On resume, candidates up to and including resume_index are skipped.
    Already-finished candidates (evaluated/failed) are always skipped so a
    crash/re-run never duplicates work.
    """
    df = storage.list_candidates(run_id)
    pending: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        cid = int(row["candidate_id"])
        if str(row["status"]) != "pending":
            continue
        dv = json.loads(row["design_vars_json"])
        idx = int(dv.get("candidate_index", 0))
        if resume_index is not None and idx <= resume_index:
            continue
        pending.append({"candidate_id": cid, "candidate_index": idx,
                        "design_vars": dv})
    pending.sort(key=lambda p: p["candidate_index"])
    return pending


def run_batch(
    design_space: DesignSpace,
    run_id: Optional[int] = None,
    max_consecutive_failures: int = 5,
    db_path: Optional[str] = None,
    log_path: Optional[str] = None,
    eval_case_id: Optional[int] = None,
    session_factory: Optional[Callable[[], HeadlessSession]] = None,
) -> Dict[str, Any]:
    """Runs a batch optimization over a design space, with checkpointing.

    Parameters
    ----------
    design_space : DesignSpace
        Validated design space (Phase 4).
    run_id : int | None
        None = start a new run (creates the run + candidates rows).
        Given = resume: skip candidates <= the stored checkpoint index.
    max_consecutive_failures : int
        Abort the run after this many consecutive candidate failures.
    db_path : str | None
        SQLite path (defaults to batch/runs.db).
    log_path : str | None
        Log file path (defaults to batch/runner.log).
    eval_case_id : int | None
        Load case used for utilization/buckling scoring. Defaults to the
        design space's first load case (or 1).
    session_factory : callable | None
        For tests: a factory returning a HeadlessSession. Defaults to
        HeadlessSession(visible=False).

    Returns a summary dict.
    """
    _configure_logging(log_path or "runner.log")
    storage = Storage(db_path=db_path)
    t_start = time.time()

    if run_id is None:
        run_id = storage.create_run(design_space.to_dict(),
                                    objective=json.dumps(
                                        design_space.objective, default=str))
        candidates = design_space.generate_candidates()
        for cand in candidates:
            storage.add_candidate(run_id, cand)
        logger.info("Run %s created with %d candidates.", run_id,
                    len(candidates))
    else:
        row = storage.get_run(run_id)
        if row is None:
            raise ValueError(f"run {run_id} does not exist in storage.")
        logger.info("Resuming run %s.", run_id)

    resume_index = storage.get_resume_point(run_id)
    pending = _load_pending(storage, run_id, resume_index)
    total = len(pending)
    if resume_index is not None:
        logger.info("Resume point: %s (skipping candidates <= it).",
                    resume_index)
    logger.info("Pending candidates: %d.", total)
    if total == 0:
        storage.mark_run_status(run_id, "completed")
        return {"run_id": run_id, "status": "completed",
                "evaluated": 0, "failed": 0, "total": 0,
                "elapsed_s": round(time.time() - t_start, 1),
                "resumed_from": resume_index}
    # One session for the whole run (Phase-1 T1 timing finding: reuse, don't
    # relaunch per candidate - relaunch is the FAILURE path only).
    factory = session_factory or (lambda: HeadlessSession(visible=False))
    session: Optional[HeadlessSession] = factory()
    session.connect()

    eval_case = eval_case_id or _choose_eval_case(design_space)
    consecutive_failures = 0
    n_evaluated = 0
    n_failed = 0
    failed_details: List[str] = []
    status = "running"

    try:
        for i, cand in enumerate(pending, start=1):
            cid = cand["candidate_id"]
            idx = cand["candidate_index"]
            logger.info("[%d/%d] evaluating candidate %d", i, total, idx)
            t_c = time.time()
            try:
                result = _evaluate_candidate(
                    session, design_space, cand["design_vars"], eval_case)
                storage.record_result(
                    candidate_id=cid,
                    weight_kg=result["weight_kg"],
                    max_utilization=result["max_utilization"],
                    governing_check=result["governing_check"],
                    buckling_status=result["buckling_status"],
                    pass_fail=("PASS" if result["pass_fail"] else "FAIL"),
                    raw_results_json=json.dumps(result["raw"], default=str),
                )
                n_evaluated += 1
                consecutive_failures = 0
                logger.info(
                    "  -> w=%.1f kg util=%s %s",
                    result["weight_kg"] or 0.0,
                    result["max_utilization"],
                    "PASS" if result["pass_fail"] else "FAIL",
                )
            except _SOLVER_SAFETY_ERRORS as exc:
                n_failed += 1
                consecutive_failures += 1
                reason = f"{type(exc).__name__}: {exc}"
                failed_details.append(reason)
                storage.mark_candidate_failed(cid, reason)
                logger.warning("  -> %s", reason)
                # The safety net may have force-terminated Robot: check and
                # reconnect a fresh session before the next candidate.
                if not session.is_alive():
                    logger.warning("    session dead - reconnecting...")
                    session.reconnect()
            except MechanismError as exc:
                n_failed += 1
                consecutive_failures += 1
                reason = f"mechanism_detected: {exc}"
                failed_details.append(reason)
                storage.mark_candidate_failed(cid, reason)
                logger.warning("  -> %s", reason)
                # Mechanism is caught pre-solve; the session stays healthy.
            except Exception as exc:  # noqa: BLE001
                n_failed += 1
                consecutive_failures += 1
                reason = f"{type(exc).__name__}: {exc}"
                failed_details.append(reason)
                storage.mark_candidate_failed(cid, reason)
                logger.warning("  -> %s", reason)
                if not session.is_alive():
                    logger.warning("    session dead - reconnecting...")
                    session.reconnect()

            # Checkpoint after EVERY candidate - a crash loses at most one.
            storage.update_checkpoint(run_id, idx)
            elapsed = time.time() - t_c
            # Progress: candidate i of total, elapsed, estimated remaining
            # (per-candidate average over candidates done so far).
            run_elapsed = time.time() - t_start
            avg = run_elapsed / i
            eta = avg * (total - i)
            logger.info(
                "  checkpoint %s after %.1fs | [%d/%d] run elapsed %.0fs, "
                "ETA ~%.0fs",
                idx, elapsed, i, total, run_elapsed, eta)


            # [PHASE 7] Cooperative cancellation: check BETWEEN candidates
            # (never mid-solve). The current candidate is already finished
            # and checkpointed above - stop cleanly, not abruptly.
            if storage.is_cancel_requested(run_id):
                logger.warning("Run %s cancellation requested - stopping",
                              run_id)
                storage.mark_run_status(run_id, "cancelled")
                status = "cancelled"
                break
            if consecutive_failures >= max_consecutive_failures:
                storage.mark_run_status(run_id, "failed")
                logger.error(
                    "Aborting run %s: %d consecutive failures >= %d.",
                    run_id, consecutive_failures, max_consecutive_failures)
                return {
                    "run_id": run_id, "status": "failed",
                    "evaluated": n_evaluated, "failed": n_failed,
                    "total": total,
                    "elapsed_s": round(time.time() - t_start, 1),
                    "resumed_from": resume_index,
                    "aborted": True,
                    "consecutive_failures": consecutive_failures,
                    "failures": failed_details,
                }

        if status != "cancelled":
            storage.mark_run_status(run_id, "completed")
            status = "completed"
    finally:
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Session close error: %s", exc)

    logger.info("Run %s completed: %d evaluated, %d failed in %.1fs.",
                run_id, n_evaluated, n_failed, time.time() - t_start)
    return {
        "run_id": run_id, "status": status,
        "evaluated": n_evaluated, "failed": n_failed,
        "total": total,
        "elapsed_s": round(time.time() - t_start, 1),
        "resumed_from": resume_index,
        "failures": failed_details,
    }

