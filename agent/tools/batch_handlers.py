"""Optimization / surrogate-search / best-design export tool handlers.

[FIX 06] Extracted verbatim from agent/tool_registry.py - dispatch binds
these functions onto ToolExecutor as `_tool_*` methods, so the public API
and behaviour are unchanged.
"""

from __future__ import annotations

import json
import time
from typing import Any

from agent.tools._shared import GENERATED_DIR, ToolExecutionError, safe_output_path
from batch.design_space import DesignSpace, DesignSpaceError
from batch.export_candidate import export_best_from_run
from batch.pareto import pareto_summary
from batch.storage import Storage
from batch.surrogate_search import ACQUISITION_MODES, SurrogateSearchError, should_use_grid
from batch.surrogate_search import DEFAULT_BUDGET as SURROGATE_DEFAULT_BUDGET
from batch.surrogate_search import DEFAULT_PATIENCE as SURROGATE_DEFAULT_PATIENCE


def tool_start_optimization_run(self, spec: dict) -> dict:
    """Validates a DesignSpace spec and estimates the run WITHOUT starting
    it. Stores the validated spec under a generated run_config_id so the
    run only starts after explicit user confirmation via
    confirm_and_start_optimization_run. NEVER starts here."""
    if not spec or not isinstance(spec, dict):
        raise ToolExecutionError(
            "start_optimization_run requires a DesignSpace JSON 'spec' "
            "object (geometry + variable_groups + load_cases + "
            "analysis_types + objective). See the schema description."
        )
    try:
        ds = DesignSpace(spec)
        n_candidates = ds.candidate_count()
        ds.generate_candidates()  # validates grid <= cap (Phase 4 errors)
    except DesignSpaceError as exc:
        raise ToolExecutionError(f"Invalid design space: {exc}. Fix the spec and retry.") from exc
    # Conservative estimate from Phase-1 T1 timing (5-11 s/candidate).
    lo_s, hi_s = n_candidates * 5, n_candidates * 11
    cfg_id = f"cfg_{int(time.time())}"
    self._optimization_configs[cfg_id] = {"spec": spec, "created": time.time()}
    return {
        "status": "not_started",
        "run_config_id": cfg_id,
        "candidate_count": n_candidates,
        "estimate_seconds_min": lo_s,
        "estimate_seconds_max": hi_s,
        "estimate": (
            f"{n_candidates} candidates, roughly "
            f"{lo_s // 60}-{hi_s // 60} min (5-11 s/candidate, "
            "reused Robot session)"
        ),
        "message": (
            "Run NOT started. Show the user the candidate count "
            "and time estimate, get explicit confirmation, then "
            "call confirm_and_start_optimization_run with this "
            "run_config_id."
        ),
    }


def tool_confirm_and_start_optimization_run(self, run_config_id: str) -> dict:
    """Starts a staged batch run in a background thread and returns
    immediately with the run_id. Only staged configs (from
    start_optimization_run) can be started.

    The run + candidate rows are pre-created SYNCHRONOUSLY here (pure
    SQLite, no Robot) so the run_id is known immediately; the thread
    then executes the batch with that run_id."""
    cfg = self._optimization_configs.pop(run_config_id, None)
    if cfg is None or cfg.get("kind") == "surrogate":
        raise ToolExecutionError(
            f"run_config_id '{run_config_id}' is not a staged grid-run "
            "config (call start_optimization_run first; surrogate runs "
            "use confirm_and_start_surrogate_search_run)."
        )
    ds = DesignSpace(cfg["spec"])
    # Pre-create run + candidates (fast, no Robot) so run_id is immediate.
    st = Storage(db_path=self._batch_db_path)
    try:
        run_id = st.create_run(ds.to_dict(), objective=json.dumps(ds.objective, default=str))
        for cand in ds.generate_candidates():
            st.add_candidate(run_id, cand)
    except Exception as exc:  # noqa: BLE001
        raise ToolExecutionError(f"Could not stage batch run: {exc}") from exc
    finally:
        st.close()

    # Background thread: the runner opens its OWN Robot instance
    # (HeadlessSession, new_instance=True) and its own Storage connection,
    # so it never touches the interactive app's Robot or session state.
    import threading

    holder: dict[str, Any] = {"run_id": run_id, "error": None}
    t = threading.Thread(
        target=self._run_optimization_worker,
        args=(ds, run_id, holder),
        name="batch-optimizer",
        daemon=True,
    )
    t.start()
    self._optimization_runs[run_id] = {"thread": t, "started": time.time()}
    return {
        "status": "started",
        "run_id": run_id,
        "message": (
            "Batch optimization started in the background. Poll "
            "check_optimization_status; then get_optimization_results "
            "once it is 'completed'."
        ),
    }


def tool_check_optimization_status(self, run_id: int) -> dict:
    st = Storage(db_path=self._batch_db_path)
    try:
        run = st.get_run(run_id)
        if run is None:
            raise ToolExecutionError(
                f"run_id {run_id} does not exist in batch storage ({self._batch_db_path})."
            )
        df = st.get_all_results(run_id)
        n_eval = int((df["candidate_status"] == "evaluated").sum())
        n_fail = int((df["candidate_status"] == "failed").sum())
        total = len(df)
    finally:
        st.close()
    status = str(run.get("status", "unknown"))
    # Estimate remaining from average elapsed-per-evaluated (like the
    # runner's ETA): created_at is stored in the runs row.
    from datetime import datetime

    elapsed_s = None
    remaining = None
    try:
        created = datetime.strptime(str(run["created_at"]), "%Y-%m-%d %H:%M:%S")
        elapsed_s = (datetime.now() - created).total_seconds()
        if n_eval > 0 and total > n_eval:
            per_c = elapsed_s / n_eval
            remaining = int(per_c * (total - n_eval))
    except Exception:  # noqa: BLE001
        elapsed_s, remaining = None, None
    return {
        "status": status,
        "run_id": run_id,
        "evaluated": n_eval,
        "failed": n_fail,
        "total": total,
        "elapsed_s": int(elapsed_s) if elapsed_s else None,
        "estimated_remaining_s": remaining,
    }


def tool_get_optimization_results(self, run_id: int) -> dict:
    st = Storage(db_path=self._batch_db_path)
    try:
        run = st.get_run(run_id)
        if run is None:
            raise ToolExecutionError(f"run_id {run_id} does not exist in batch storage.")
        status = str(run.get("status", "unknown"))
        if status != "completed":
            return {
                "status": "not_ready",
                "run_status": status,
                "message": (
                    f"Run {run_id} is '{status}', not 'completed'. "
                    "Results are NOT meaningful until the run "
                    "finishes - poll check_optimization_status."
                ),
            }
        df = st.get_all_results(run_id)
    finally:
        st.close()
    summ = pareto_summary(df)
    return {
        "status": "ok",
        "run_id": run_id,
        "total": summ["total"],
        "passed": summ["passed"],
        "frontier_size": summ["frontier"],
        "note": summ.get("note", ""),
        "markdown": summ["markdown"],
    }


def tool_cancel_optimization_run(self, run_id: int) -> dict:
    st = Storage(db_path=self._batch_db_path)
    try:
        run = st.get_run(run_id)
        if run is None:
            raise ToolExecutionError(f"run_id {run_id} does not exist in batch storage.")
        st.request_cancel(run_id, reason="user requested")
        df = st.get_all_results(run_id)
        n_eval = int((df["candidate_status"] == "evaluated").sum())
        total = len(df)
    finally:
        st.close()
    return {
        "status": "cancel_requested",
        "run_id": run_id,
        "message": (
            "Cancellation requested. The runner stops cleanly "
            "BETWEEN candidates (current candidate finishes and "
            "is checkpointed first, then it exits). Progress so "
            f"far: {n_eval}/{total} evaluated."
        ),
    }


def tool_start_surrogate_search_run(
    self,
    spec: dict,
    budget: int = SURROGATE_DEFAULT_BUDGET,
    patience: int = SURROGATE_DEFAULT_PATIENCE,
    acquisition: str = "ucb",
    kappa: float = 2.0,
) -> dict:
    """Validates a DesignSpace spec for surrogate-guided sizing search
    and estimates the run WITHOUT starting it (same staged-confirmation
    discipline as start_optimization_run). NEVER starts here."""
    if not spec or not isinstance(spec, dict):
        raise ToolExecutionError(
            "start_surrogate_search_run requires a DesignSpace JSON "
            "'spec' object (geometry + variable_groups + load_cases + "
            "analysis_types + objective). See the schema description."
        )
    try:
        budget = int(budget)
        patience = int(patience)
        kappa = float(kappa)
    except (TypeError, ValueError) as exc:
        raise ToolExecutionError(f"budget/patience/kappa must be numeric: {exc}") from exc
    acquisition = str(acquisition or "ucb").lower()
    try:
        if budget < 1 or patience < 1 or kappa < 0.0:
            raise SurrogateSearchError("budget >= 1, patience >= 1, kappa >= 0 required")
        if acquisition not in ACQUISITION_MODES:
            raise SurrogateSearchError(f"acquisition must be one of {ACQUISITION_MODES}")
        ds = DesignSpace(spec)
        ds.generate_candidates()  # validates grid <= cap (Phase 4 errors)
    except (DesignSpaceError, SurrogateSearchError) as exc:
        raise ToolExecutionError(
            f"Invalid surrogate-search design space or parameters: {exc}. Fix the spec and retry."
        ) from exc

    # The surrogate auto-falls back to exhaustive grid search when the
    # grid is small enough to be cheaper - refuse to stage a run that
    # would immediately do that; grid search is exact and preferable.
    use_grid, reason = should_use_grid(ds, budget)
    if use_grid:
        return {
            "status": "grid_recommended",
            "candidate_count": ds.candidate_count(),
            "budget": budget,
            "reason": reason,
            "message": (
                "This design space is small enough that exhaustive grid "
                "search is cheaper and exact. Do NOT call "
                "confirm_and_start_surrogate_search_run - use "
                "start_optimization_run / confirm_and_start_optimization_run "
                "instead for this spec."
            ),
        }

    # Conservative estimate from Phase-1 T1 timing (5-11 s/candidate);
    # the surrogate spends at most `budget` Robot calls.
    lo_s, hi_s = budget * 5, budget * 11
    cfg_id = f"surr_cfg_{int(time.time())}"
    self._optimization_configs[cfg_id] = {
        "kind": "surrogate",
        "spec": spec,
        "budget": budget,
        "patience": patience,
        "acquisition": acquisition,
        "kappa": kappa,
        "created": time.time(),
    }
    return {
        "status": "not_started",
        "run_config_id": cfg_id,
        "candidate_count": ds.candidate_count(),
        "budget": budget,
        "patience": patience,
        "acquisition": acquisition,
        "kappa": kappa,
        "estimate_seconds_min": lo_s,
        "estimate_seconds_max": hi_s,
        "estimate": (
            f"up to {budget} Robot calls, roughly "
            f"{lo_s // 60}-{hi_s // 60} min (5-11 s/call, "
            f"reused Robot session)"
        ),
        "message": (
            "Run NOT started. Show the user the estimate, get "
            "explicit confirmation, then call "
            "confirm_and_start_surrogate_search_run with this "
            "run_config_id. HARD RULE: never start in this same "
            "turn."
        ),
    }


def tool_confirm_and_start_surrogate_search_run(
    self,
    run_config_id: str,
) -> dict:
    """Starts a staged surrogate search in a background thread and
    returns immediately with the run_id (same shape as the grid path).
    Only surrogate configs (kind == 'surrogate') can be started."""
    cfg = self._optimization_configs.pop(run_config_id, None)
    if cfg is None or cfg.get("kind") != "surrogate":
        raise ToolExecutionError(
            f"run_config_id '{run_config_id}' is not a staged surrogate "
            "config (call start_surrogate_search_run first)."
        )
    ds = DesignSpace(cfg["spec"])
    # Pre-create run + candidates (fast, no Robot) so run_id is immediate.
    st = Storage(db_path=self._batch_db_path)
    try:
        run_id = st.create_run(ds.to_dict(), objective=json.dumps(ds.objective, default=str))
        for cand in ds.generate_candidates():
            st.add_candidate(run_id, cand)
    except Exception as exc:  # noqa: BLE001
        raise ToolExecutionError(f"Could not stage surrogate run: {exc}") from exc
    finally:
        st.close()

    import threading

    holder: dict[str, Any] = {"run_id": run_id, "error": None}
    t = threading.Thread(
        target=self._run_surrogate_worker,
        args=(ds, run_id, cfg, holder),
        name="batch-surrogate-optimizer",
        daemon=True,
    )
    t.start()
    self._optimization_runs[run_id] = {"thread": t, "started": time.time()}
    return {
        "status": "started",
        "run_id": run_id,
        "strategy": "surrogate",
        "budget": cfg["budget"],
        "message": (
            "Surrogate-guided optimization started in the "
            "background. Poll check_optimization_status; then "
            "get_optimization_results once it is 'completed'."
        ),
    }


def tool_export_best_design(
    self,
    run_id: int,
    file_name: str,
    frontier_index: int = 0,
    visible: bool = True,
) -> dict:
    """Exports the lightest passing candidate of a COMPLETED run as a
    real Robot project (.rtd) so it can be opened in Robot. Builds +
    solves + saves in its own visible HeadlessSession."""
    if not file_name or not str(file_name).strip():
        raise ToolExecutionError("export_best_design requires a 'file_name'.")
    st = Storage(db_path=self._batch_db_path)
    try:
        run = st.get_run(int(run_id))
        if run is None:
            raise ToolExecutionError(f"run_id {run_id} does not exist in batch storage.")
        status = str(run.get("status", "unknown"))
        if status != "completed":
            return {
                "status": "not_ready",
                "run_status": status,
                "message": (
                    f"Run {run_id} is '{status}', not 'completed'. "
                    "Results are NOT meaningful until the run "
                    "finishes - poll check_optimization_status."
                ),
            }
    finally:
        st.close()

    try:
        path = safe_output_path(str(file_name), GENERATED_DIR)
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc
    if not path.lower().endswith(".rtd"):
        path += ".rtd"
    try:
        t0 = time.time()
        saved = export_best_from_run(
            int(run_id),
            path,
            frontier_index=int(frontier_index),
            db_path=self._batch_db_path,
            visible=bool(visible),
        )
        elapsed_s = round(time.time() - t0, 1)
    except Exception as exc:  # noqa: BLE001
        raise ToolExecutionError(
            f"Could not export the best design from run {run_id}: {exc}"
        ) from exc
    return {
        "status": "ok",
        "run_id": int(run_id),
        "frontier_index": int(frontier_index),
        "file_path": saved,
        "elapsed_s": elapsed_s,
        "message": (
            "Design built, solved and saved. Open the .rtd in "
            "Robot to inspect it. Note: this opened its OWN "
            "Robot instance (one-seat license caveat)."
        ),
    }
