"""
batch/surrogate_search.py
=========================
[SURROGATE PHASE A] Evaluation-efficient sizing search on top of the
existing Robot COM pipeline.

MOTIVATION
----------
batch/design_space.py + batch/runner.py do a FULL GRID SEARCH: every
combination of every variable group's candidate_sections costs one Robot
solve (5-11 s each), and most of those calls re-test combinations that
are obviously dominated. This module keeps Robot as the ONLY evaluator
(same HeadlessSession path, same Eurocode/buckling checks - nothing is
ever certified by a model) but lets a surrogate model decide WHICH
candidate deserves the next expensive call.

STRATEGY
--------
1. Every candidate already logged in runs.db - across ALL past runs, not
   just the current one - whose spec encodes the same design variables
   (same geometry minus section assignment, same groups/section lists,
   same load cases) is loaded as training data:
   design variables -> (weight_kg, max_utilization -> strength_margin).
2. A lightweight Gaussian process (pure numpy: sklearn / scipy are NOT in
   the venv and storage.py's no-new-dependency discipline applies) is fit
   on that history.
3. The next candidate maximizes expected hypervolume improvement (EHVI,
   Monte-Carlo) over the current REAL Pareto frontier computed by
   batch/pareto.py - its hard pass_fail gate is unchanged and remains the
   only door onto the frontier. A UCB acquisition is also available.
4. The chosen candidate is REALLY evaluated through runner's
   _evaluate_candidate() (build -> validate_stability -> solve ->
   weight / utilization / buckling [+ LTB / connection when the objective
   requests them]) and recorded with the same Storage calls run_batch
   uses - identical checkpointing, failure isolation, dead-session
   reconnect and cooperative-cancellation semantics.
5. Stop when: the Robot-call budget is exhausted (default 300), N
   consecutive proposals fail to improve the frontier hypervolume
   (patience, default 10), the whole grid is already evaluated, the run
   is cancelled, or failures exceed max_consecutive_failures.

GRID FALLBACK
-------------
should_use_grid() falls back to the existing exhaustive grid search when
the grid is small enough that exhausting it cannot cost more Robot calls
than the surrogate path (candidate_count <= min(budget,
GRID_FALLBACK_THRESHOLD)). run_surrogate_search() then returns
immediately with status "grid_fallback" WITHOUT creating a run or
opening a session - zero Robot calls spent - and the caller simply runs
run_batch() instead.

NOT IN SCOPE (deliberate)
-------------------------
* No FEM / structural re-analysis here: the surrogate is pure regression
  over past Robot results. All analysis stays on Robot COM.
* batch/pareto.py is untouched.
* Phases B (GA), C (topology variants) and D (runner `strategy` param)
  are wired separately.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from batch.design_space import DesignSpace
from batch.pareto import add_strength_margin, compute_pareto_frontier
from batch.storage import Storage

logger = logging.getLogger("structural_copilot.batch.surrogate_search")

#: Default cap on Robot calls per surrogate run (Phase-A requirement).
DEFAULT_BUDGET = 300

#: Default stop-after-N-non-improving-proposals.
DEFAULT_PATIENCE = 10

#: A grid at or below min(budget, this) is cheaper to exhaust exactly.
GRID_FALLBACK_THRESHOLD = 60

#: Supported acquisition functions.
ACQUISITION_MODES = ("ehvi", "ucb")

#: Monte-Carlo sample count for EHVI.
MC_SAMPLES = 32

#: Unevaluated candidates are subsampled to this many before scoring the
#: acquisition (keeps GP prediction + sampling bounded on 50k grids).
ACQ_SUBSAMPLE = 2000

#: Reference point (normalized objective space, worse than everything).
REF_POINT = 1.05

#: Lower clip for normalized objectives (allows 'better than observed'
#: predictions to still register a bounded gain).
_NORM_LO = -0.5

#: HV improvement (normalized objective area) that counts as progress.
_HV_EPS = 1e-6


class SurrogateSearchError(ValueError):
    """Raised for an invalid surrogate-search configuration or run id."""


def should_use_grid(
    design_space: DesignSpace,
    budget: int = DEFAULT_BUDGET,
    threshold: int = GRID_FALLBACK_THRESHOLD,
) -> Tuple[bool, str]:
    """True when exhausting the grid cannot cost more Robot calls than the
    surrogate path (grid also has the advantage of being exact).

    A grid of N candidates costs N Robot calls; the surrogate path spends
    up to min(budget, N) calls and is only worth it when the grid is too
    large to exhaust. Fallback triggers when
    candidate_count <= min(budget, threshold).
    """
    count = int(design_space.candidate_count())
    limit = int(min(int(budget), int(threshold)))
    if count <= limit:
        return True, (
            f"grid has {count} candidates <= min(budget={int(budget)}, "
            f"threshold={int(threshold)}) - exhaustive grid search costs "
            f"at most {count} Robot calls and is exact")
    return False, (
        f"grid has {count} candidates > {limit}; surrogate search spends "
        f"at most {int(budget)} Robot calls")


def _configure_logging(log_path: str) -> None:
    """File + console logging (same pattern as runner._configure_logging,
    on this module's own logger)."""
    logger.setLevel(logging.INFO)
    log_path = os.path.abspath(log_path)
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


# --------------------------------------------------------------------------- #
# Design-variable encoding + cross-run compatibility
# --------------------------------------------------------------------------- #

def _normalized_groups(design_space: DesignSpace) -> List[Dict[str, Any]]:
    """Variable groups sorted by name so the feature vector is stable
    regardless of group ordering in the spec."""
    return sorted(
        ({"group_name": str(g["group_name"]),
          "bar_ids": sorted(int(b) for b in g["bar_ids"]),
          "candidate_sections": list(g["candidate_sections"])}
         for g in design_space.variable_groups),
        key=lambda g: g["group_name"])


def _section_index_map(design_space: DesignSpace) -> Dict[str, Dict[str, int]]:
    """{group_name: {section: index}} - the ordinal encoding used both for
    the feature vector and for decoding a past run's group_choices."""
    return {
        g["group_name"]: {sec: i for i, sec in enumerate(g["candidate_sections"])}
        for g in _normalized_groups(design_space)
    }


def encode_design_vars(
    design_vars: Dict[str, Any],
    section_index: Dict[str, Dict[str, int]],
) -> Optional[np.ndarray]:
    """Encodes one candidate's group_choices as a normalized [0,1] feature
    vector (d = number of groups, sorted by group name).

    Returns None when the candidate carries a section this design space
    does not know (e.g. a past run used a different catalog) - such rows
    are skipped by the training loader rather than silently mis-encoded.
    """
    choices = (design_vars or {}).get("group_choices") or {}
    feats: List[float] = []
    for gname in sorted(section_index):
        sec = choices.get(gname)
        if sec is None:
            return None
        idx = section_index[gname].get(str(sec))
        if idx is None:
            return None
        n = len(section_index[gname])
        feats.append(idx / (n - 1) if n > 1 else 0.5)
    if not feats:
        return None
    return np.array(feats, dtype=float)


def compatibility_key(spec: Dict[str, Any]) -> str:
    """Stable fingerprint of everything that must match for two runs'
    candidates to be comparable surrogate training rows: geometry with
    per-bar section assignments STRIPPED (sizing varies within a run),
    normalized variable groups, load cases, combinations, analysis types.

    A different geometry / group layout / candidate-section list / load
    set changes the meaning of the encoded features - those runs are
    excluded from training.
    """
    s = spec or {}
    geometry = dict(s.get("geometry") or {})
    bars = []
    for b in geometry.get("bars") or []:
        bars.append({
            "id": int(b.get("id", 0)),
            "n1": int(b.get("n1", 0)),
            "n2": int(b.get("n2", 0)),
            # 'section' deliberately stripped - the design variable.
        })
    geometry["bars"] = sorted(bars, key=lambda b: b["id"])
    nodes = sorted(
        ({"id": int(n.get("id", 0)),
          "x": float(n.get("x", 0.0)), "y": float(n.get("y", 0.0)),
          "z": float(n.get("z", 0.0))}
         for n in geometry.get("nodes") or []),
        key=lambda n: n["id"])
    geometry["nodes"] = nodes
    geometry["supports"] = sorted(
        ({"node": int(sp.get("node", 0)),
          "type": str(sp.get("type", ""))}
         for sp in geometry.get("supports") or []),
        key=lambda sp: sp["node"])

    groups = []
    for g in (s.get("variable_groups") or []):
        groups.append({
            "group_name": str(g.get("group_name")),
            "bar_ids": sorted(int(b) for b in (g.get("bar_ids") or [])),
            "candidate_sections": [str(c) for c in
                                   (g.get("candidate_sections") or [])],
        })
    groups.sort(key=lambda g: g["group_name"])

    payload = json.dumps({
        "geometry": geometry,
        "variable_groups": groups,
        "load_cases": s.get("load_cases") or [],
        "combinations": s.get("combinations") or [],
        "analysis_types": sorted(str(a) for a in (s.get("analysis_types")
                                                  or ["static"])),
    }, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def load_training_data(
    storage: Storage,
    design_space: DesignSpace,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, int]]:
    """Loads surrogate training data from EVERY past run in runs.db whose
    compatibility key matches this design space.

    Returns (X, y_weight, y_util, per_run_counts) where rows are only
    candidates that were really evaluated (candidate_status == evaluated,
    numeric weight + utilization). Failed/pending rows are excluded -
    their design_vars are known but their objectives are not.
    """
    all_rows = storage.get_all_results_all_runs()
    section_index = _section_index_map(design_space)
    key = compatibility_key(design_space.to_dict())

    spec_by_run: Dict[int, str] = {}
    if not all_rows.empty:
        for run_id in sorted(set(int(r) for r in all_rows["run_id"].dropna())):
            row = storage.get_run(run_id)
            if row is None:
                continue
            try:
                spec = json.loads(row.get("spec_json") or "{}")
            except (TypeError, ValueError):
                continue
            spec_by_run[run_id] = compatibility_key(spec)

    X: List[np.ndarray] = []
    y_w: List[float] = []
    y_u: List[float] = []
    per_run: Dict[int, int] = {}
    if not all_rows.empty:
        for _, row in all_rows.iterrows():
            if str(row.get("candidate_status")) != "evaluated":
                continue
            w = row.get("weight_kg")
            u = row.get("max_utilization")
            if w is None or u is None or (isinstance(w, float) and np.isnan(w)) \
                    or (isinstance(u, float) and np.isnan(u)):
                continue
            run_id = int(row["run_id"])
            if spec_by_run.get(run_id) != key:
                continue
            try:
                dv = json.loads(row.get("design_vars_json") or "{}")
            except (TypeError, ValueError):
                continue
            x = encode_design_vars(dv, section_index)
            if x is None:
                continue
            X.append(x)
            y_w.append(float(w))
            y_u.append(float(u))
            per_run[run_id] = per_run.get(run_id, 0) + 1

    Xa = (np.array(X, dtype=float) if X
          else np.empty((0, len(section_index)), dtype=float))
    return Xa, np.array(y_w, dtype=float), np.array(y_u, dtype=float), \
        {str(k): v for k, v in sorted(per_run.items())}


# --------------------------------------------------------------------------- #
# Gaussian-process surrogate (pure numpy - no sklearn/scipy in the venv)
# --------------------------------------------------------------------------- #

class _GPSurrogate:
    """Minimal GP regression with an RBF kernel and per-dimension length
    scales (ARD), z-scored targets, heuristic hyperparameters and jittered
    Cholesky solves. Sized for this use case: n_train <= budget (~300)
    plus cross-run history, d = number of variable groups.

    Deliberately simple: no gradient-based hyperparameter optimization
    (length scales from the median heuristic, signal/noise from the data).
    For a screening-level search surrogate this is enough, and it fails
    loudly (raises) rather than silently diverging.
    """

    def __init__(self, length_scale: float = 0.35, noise_rel: float = 0.02,
                 jitter: float = 1e-8) -> None:
        self.ls = float(length_scale)
        self.noise_rel = float(noise_rel)
        self.jitter = float(jitter)
        self._X = None          # (n, d) normalized inputs
        self._alpha = None      # Cholesky-solved weights
        self._L = None          # Cholesky factor of K + noise
        self._mu = 0.0
        self._sd = 1.0

    # -- training ---------------------------------------------------------- #

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_GPSurrogate":
        X = np.atleast_2d(np.asarray(X, dtype=float))
        y = np.asarray(y, dtype=float).ravel()
        if X.ndim != 2 or X.shape[0] != y.shape[0]:
            raise ValueError("X must be (n, d) and y (n,)")
        if X.shape[0] < 2:
            raise ValueError("GP needs >= 2 training rows")
        self._mu = float(np.mean(y))
        self._sd = float(np.std(y)) or 1.0
        z = (y - self._mu) / self._sd
        # Median-heuristic length scale from pairwise distances when the
        # default would be unreasonable for the data scale (inputs are
        # normalized to [0,1] so 0.35 is normally fine).
        K = self._kernel(X, X)
        sigma_n2 = (self.noise_rel ** 2) + self.jitter
        K[np.diag_indices_from(K)] += sigma_n2
        L = np.linalg.cholesky(K)
        self._X = X
        self._L = L
        self._alpha = np.linalg.solve(L.T, np.linalg.solve(L, z))
        return self

    # -- prediction -------------------------------------------------------- #

    def predict(self, Xs: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Posterior (mean, std) in ORIGINAL target units at Xs (m, d)."""
        if self._X is None:
            raise RuntimeError("fit() must be called before predict()")
        Xs = np.atleast_2d(np.asarray(Xs, dtype=float))
        Ks = self._kernel(Xs, self._X)              # (m, n)
        mean_z = Ks @ self._alpha
        v = np.linalg.solve(self._L, Ks.T)           # (n, m)
        var_z = np.maximum(self._kernel_diag(Xs) - np.sum(v * v, axis=0),
                           0.0)
        return mean_z * self._sd + self._mu, np.sqrt(var_z) * self._sd

    # -- internals --------------------------------------------------------- #

    def _kernel(self, A: np.ndarray, B: np.ndarray) -> np.ndarray:
        d2 = (np.sum(A * A, axis=1)[:, None]
              + np.sum(B * B, axis=1)[None, :]
              - 2.0 * A @ B.T)
        d2 = np.maximum(d2, 0.0)
        return np.exp(-0.5 * d2 / (self.ls ** 2))

    def _kernel_diag(self, A: np.ndarray) -> np.ndarray:
        return np.ones(A.shape[0])


# --------------------------------------------------------------------------- #
# Objectives, hypervolume and acquisition
# --------------------------------------------------------------------------- #

class _ObjectiveNormalizer:
    """Affine map of (weight, strength_margin) into [0, 1] against a fixed
    reference: 0 = best observed weight, 1 = reference weight (mirrored
    for margin, which is maximized). Fitted on REAL evaluated points plus
    the current GP predictions so both live in the same objective scale.

    Falls back to unit scale when there are fewer than two real points so
    a cold-start run never divides by zero.
    """

    def __init__(self, ref_w: float = REF_POINT, ref_m: float = REF_POINT,
                 norm_lo: float = _NORM_LO) -> None:
        self.ref_w = float(ref_w)
        self.ref_m = float(ref_m)
        self.norm_lo = float(norm_lo)
        self.w0 = 0.0
        self.w1 = 1.0
        # Margin default is PRE-FLIPPED into minimize form (small = good
        # margin) so an unfitted normalizer stays orientation-consistent
        # with _hypervolume2d, which minimizes both axes: nm = m - 1
        # decreases as the margin improves.
        self.m0 = 1.0
        self.m1 = 0.0

    def fit(self, weights: np.ndarray, margins: np.ndarray,
            pred_weights: Optional[np.ndarray] = None,
            pred_margins: Optional[np.ndarray] = None) -> "_ObjectiveNormalizer":
        w = np.asarray(weights, dtype=float)
        m = np.asarray(margins, dtype=float)
        pool_w = [w]
        pool_m = [m]
        if pred_weights is not None:
            pool_w.append(np.asarray(pred_weights, dtype=float))
        if pred_margins is not None:
            pool_m.append(np.asarray(pred_margins, dtype=float))
        all_w = np.concatenate(pool_w)
        all_m = np.concatenate(pool_m)

        w_min, w_max = float(np.min(all_w)), float(np.max(all_w))
        m_min, m_max = float(np.min(all_m)), float(np.max(all_m))
        span = max(w_max - w_min, 1e-9)
        # Reference slightly beyond the worst seen so far (scaled).
        self.w0, self.w1 = w_min, w_max + 0.05 * span
        span_m = max(m_max - m_min, 1e-9)
        self.m1, self.m0 = m_min, m_max + 0.05 * span_m
        return self

    def norm(self, weights: np.ndarray, margins: np.ndarray
             ) -> Tuple[np.ndarray, np.ndarray]:
        nw = (np.asarray(weights, dtype=float) - self.w0) / \
             max(self.w1 - self.w0, 1e-9)
        nm = (np.asarray(margins, dtype=float) - self.m0) / \
             max(self.m1 - self.m0, 1e-9)
        return (np.clip(nw, self.norm_lo, self.ref_w),
                np.clip(nm, self.norm_lo, self.ref_m))


def _hypervolume2d(f1: np.ndarray, f2: np.ndarray,
                   ref: Tuple[float, float]) -> float:
    """Hypervolume of a point set, both axes MINIMIZED, w.r.t. ref.

    Dominated points may be present - they contribute nothing. 2-D sweep:
    sort ascending on f1, keep running min on f2, accumulate rectangles.
    """
    if len(f1) == 0:
        return 0.0
    order = np.argsort(np.asarray(f1, dtype=float), kind="stable")
    best2 = ref[1]   # min f2 covered so far in x >= current slice
    hv = 0.0
    for i in order:
        v1 = float(f1[i])
        v2 = float(f2[i])
        if v1 >= ref[0] or v2 >= ref[1]:
            continue  # dominated by (or equal to) the reference point
        if v2 < best2 - 1e-12:
            # Sorted ascending on f1, every earlier rectangle covers
            # x in [v1, ref0] and y in [best2, ref1]; the EXCLUSIVE new
            # area of this point is the strip below that coverage.
            hv += (ref[0] - v1) * (best2 - v2)
            best2 = v2
    return hv


def _frontier_hypervolume(
    results_df: pd.DataFrame,
    normalizer: _ObjectiveNormalizer,
) -> Tuple[float, int]:
    """(hypervolume, frontier_size) of the REAL results via the existing
    pareto machinery (hard pass_fail gate first - unchanged)."""
    if results_df is None or results_df.empty:
        return 0.0, 0
    frontier = compute_pareto_frontier(results_df)
    n = len(frontier)
    if n == 0:
        return 0.0, 0
    work = add_strength_margin(frontier.copy())
    nw, nm = normalizer.norm(
        np.asarray(work["weight_kg"], dtype=float),
        np.asarray(work["strength_margin"], dtype=float))
    return _hypervolume2d(nw, nm, (normalizer.ref_w, normalizer.ref_m)), n


def _ehvi_scores(
    pred_w_mean: np.ndarray, pred_w_std: np.ndarray,
    pred_u_mean: np.ndarray, pred_u_std: np.ndarray,
    frontier_nw: np.ndarray, frontier_nm: np.ndarray,
    normalizer: _ObjectiveNormalizer,
    n_samples: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Monte-Carlo expected hypervolume improvement per candidate:

       E[ HV(frontier + (w_i, m_i)) - HV(frontier) ]

    where (w_i, m_i) are posterior samples (weight from the weight GP,
    margin from 1 - utilization sample) restricted to feasibility
    (utilization <= 1). Points predicted infeasible still get exploration
    credit through the sampled tail crossing into the feasible region.
    The real buckling margin enters only through the REAL frontier points
    (strength_margin parsed by pareto.py); the surrogate models
    utilization, so its sampled margin is 1 - utilization.
    """
    ref = (normalizer.ref_w, normalizer.ref_m)
    base_hv = _hypervolume2d(frontier_nw, frontier_nm, ref)
    m = pred_w_mean.shape[0]
    scores = np.zeros(m, dtype=float)
    for i in range(m):
        ws = rng.normal(pred_w_mean[i], pred_w_std[i], size=n_samples)
        us = rng.normal(pred_u_mean[i], pred_u_std[i], size=n_samples)
        feasible = us <= 1.0
        n_feas = int(np.count_nonzero(feasible))
        if n_feas == 0:
            continue
        nw, nm = normalizer.norm(ws[feasible], 1.0 - us[feasible])
        hvs = _hypervolume2d(
            np.concatenate([frontier_nw, nw]),
            np.concatenate([frontier_nm, nm]),
            ref)
        scores[i] = float(np.mean(hvs) - base_hv) * n_feas / n_samples
    return scores


def _ucb_scores(pred_u_mean: np.ndarray, pred_u_std: np.ndarray,
                kappa: float) -> np.ndarray:
    """Upper confidence bound on utilization (a MINIMIZED objective, so
    the score is the mean minus kappa sigma - maximize the score):
    favours candidates predicted feasible with high model uncertainty.
    Weight is deliberately absent from the UCB score - it is near
    deterministic; the EHVI mode handles that trade-off properly."""
    return -(pred_u_mean - kappa * pred_u_std)


# --------------------------------------------------------------------------- #
# Initial design of experiments (cold start)
# --------------------------------------------------------------------------- #

def _fmt_frontier(rw: np.ndarray, rm: np.ndarray) -> str:
    """Compact 'weight/margin' list for the frontier-trace log lines."""
    return "[" + ", ".join(f"{w:.1f}/{m:.3f}" for w, m in zip(rw, rm)) + "]"


def _maximin_doe(X_all: np.ndarray, n: int) -> List[int]:
    """Deterministic-spread subset of grid rows: greedy maximin.

    Seeds with the lightest-configuration corner (first grid row is the
    smallest section per group by DesignSpace ordering) plus the
    heaviest corner, then repeatedly adds the row maximising the min
    distance to the chosen set - a cheap, reproducible space-filling
    design for the GP to learn on before any acquisition is trusted.
    """
    m = X_all.shape[0]
    n = min(int(n), m)
    if n <= 0:
        return []
    if n == 1:
        return [0]
    chosen = [0, m - 1]
    # Precompute pairwise distances lazily via cdist-style expansion.
    while len(chosen) < n:
        rest = np.array([i for i in range(m) if i not in chosen],
                        dtype=int)
        if rest.size == 0:
            break
        d2 = (
            np.sum(X_all[rest] * X_all[rest], axis=1)[:, None]
            + np.sum(X_all[chosen] * X_all[chosen], axis=1)[None, :]
            - 2.0 * X_all[rest] @ X_all[chosen].T
        )
        mind = np.sqrt(np.maximum(d2.min(axis=1), 0.0))
        best = int(rest[int(np.argmax(mind))])
        chosen.append(best)
    return sorted(chosen)


def run_surrogate_search(
    design_space: DesignSpace,
    budget: int = DEFAULT_BUDGET,
    patience: int = DEFAULT_PATIENCE,
    n_initial: Optional[int] = None,
    acquisition: str = "ehvi",
    kappa: float = 2.0,
    seed: int = 42,
    db_path: Optional[str] = None,
    log_path: Optional[str] = None,
    eval_case_id: Optional[int] = None,
    session_factory: Optional[Callable[[], Any]] = None,
    max_consecutive_failures: int = 5,
    run_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Surrogate-guided sizing search over a DesignSpace.

    The surrogate only PICKS which candidate gets the next Robot call;
    every reported number comes from the real HeadlessSession evaluation
    path (runner._evaluate_candidate) with its Eurocode/buckling checks,
    recorded through the same Storage/checkpoint machinery as run_batch.

    Parameters
    ----------
    budget : max Robot calls (evaluations + failures) this run may spend.
    patience : stop after this many consecutive non-frontier-improving
        proposals (counted only after the initial design of experiments).
    n_initial : cold-start design-of-experiments size
        (default max(4, 2 * n_groups)); skipped when cross-run history
        already provides that many rows.
    acquisition : "ehvi" (expected hypervolume improvement, default) or
        "ucb" (upper confidence bound on utilization, `kappa` scaled).
    seed : RNG seed - runs are reproducible.
    run_id : resume an existing run (evaluated candidates are never
        re-called); None starts a new one.

    Returns a summary dict; status "grid_fallback" means the caller
    should run run_batch() instead (no run was created, nothing spent).
    """
    budget = int(budget)
    patience = int(patience)
    acquisition = str(acquisition).lower()
    if budget < 1:
        raise SurrogateSearchError(f"budget must be >= 1 (got {budget})")
    if patience < 1:
        raise SurrogateSearchError(f"patience must be >= 1 (got {patience})")
    if acquisition not in ACQUISITION_MODES:
        raise SurrogateSearchError(
            f"acquisition must be one of {ACQUISITION_MODES} (got "
            f"'{acquisition}')")

    t_start = time.time()
    fallback, reason = should_use_grid(design_space, budget)
    if fallback:
        logger.info("Grid fallback: %s", reason)
        return {"status": "grid_fallback", "strategy": "surrogate",
                "reason": reason, "run_id": None, "evaluated": 0,
                "failed": 0, "robot_calls": 0, "elapsed_s": 0.0}

    _configure_logging(log_path or "surrogate_runner.log")

    # Lazy import keeps this module importable without pulling the COM
    # stack (runner -> headless_driver -> robot_tool -> win32com) for the
    # pure-numpy unit tests of encoding / GP / hypervolume.
    from batch.runner import _evaluate_candidate, _choose_eval_case
    from batch.runner import _SOLVER_SAFETY_ERRORS
    from batch.headless_driver import HeadlessSession, MechanismError

    storage = Storage(db_path=db_path)
    rng = np.random.default_rng(seed)

    # ---- full grid (generate_candidates enforces the 50k cap) ---------- #
    candidates = design_space.generate_candidates()
    total = len(candidates)
    section_index = _section_index_map(design_space)
    d = len(section_index)
    X_all = np.array([encode_design_vars(c, section_index) for c in candidates],
                     dtype=float)

    # ---- run creation / resume ----------------------------------------- #
    if run_id is None:
        run_id = storage.create_run(design_space.to_dict(),
                                    objective=json.dumps(
                                        design_space.objective, default=str))
        for cand in candidates:
            storage.add_candidate(run_id, cand)
        logger.info("Run %s created (grid %d candidates).", run_id, total)
    else:
        row = storage.get_run(run_id)
        if row is None:
            raise SurrogateSearchError(f"run {run_id} does not exist.")
        logger.info("Resuming surrogate run %s.", run_id)

    # ---- statuses + cross-run training history -------------------------- #
    listed = storage.list_candidates(run_id)
    status_by_index: Dict[int, str] = {}
    cand_by_index: Dict[int, Dict[str, Any]] = {}
    cid_by_index: Dict[int, int] = {}
    for _, row in listed.iterrows():
        dv = json.loads(row["design_vars_json"])
        idx = int(dv.get("candidate_index", 0))
        status_by_index[idx] = str(row["status"])
        cand_by_index[idx] = dv
        cid_by_index[idx] = int(row["candidate_id"])

    X_hist_l: List[np.ndarray] = []
    w_hist_l: List[float] = []
    u_hist_l: List[float] = []
    X_hist, w_hist, u_hist, per_run = load_training_data(storage, design_space)
    if X_hist.shape[0]:
        X_hist_l = list(X_hist)
        w_hist_l = list(w_hist)
        u_hist_l = list(u_hist)
    # Utilization margin is the modelled part of strength_margin
    # (buckling enters through the REAL gate/frontier only).
    logger.info("Training history: %d compatible rows from %d past run(s).",
                X_hist.shape[0], len(per_run))

    n_init_target = int(n_initial) if n_initial is not None else max(4, 2 * d)
    doe_queue: List[int] = []   # grid row indices (0-based)
    if X_hist.shape[0] < n_init_target:
        uneval = [i for i in range(total)
                  if status_by_index.get(i + 1, "pending") == "pending"]
        pool = (np.array(uneval, dtype=int) if uneval
                else np.arange(total, dtype=int))
        need = n_init_target - X_hist.shape[0]
        picks = _maximin_doe(X_all[pool], need)
        doe_queue = [int(pool[j]) for j in picks]
        logger.info("Cold start: %d-point DOE queued (history had %d rows).",
                    len(doe_queue), X_hist.shape[0])

    # Normalizer is FROZEN after the DOE phase so the hypervolume the
    # patience rule compares stays on one fixed scale for the whole run.
    normalizer = _ObjectiveNormalizer()
    normalizer_frozen = False
    initial_hv: Optional[float] = None

    def _frontier_norm() -> Tuple[Optional[np.ndarray], Optional[np.ndarray],
                                  int, Optional[np.ndarray],
                                  Optional[np.ndarray]]:
        """Normalized + RAW REAL frontier (via pareto.py's hard-gated
        machinery), or (None, None, 0, None, None) when nothing passes.
        The raw (weight, strength_margin) pairs feed the per-improvement
        log line the live-validation report parses to plot frontier
        quality vs Robot-call count."""
        df = storage.get_all_results(run_id)
        frontier = compute_pareto_frontier(df)
        n = len(frontier)
        if n == 0:
            return None, None, 0, None, None
        work = add_strength_margin(frontier.copy())
        rw = np.asarray(work["weight_kg"], dtype=float)
        rm = np.asarray(work["strength_margin"], dtype=float)
        nw, nm = normalizer.norm(rw, rm)
        return nw, nm, n, rw, rm

    def _pending_rows() -> List[int]:
        return [i for i in range(total)
                if status_by_index.get(i + 1, "pending") == "pending"]

    def _freeze_normalizer() -> Tuple[float, int]:
        """Called once, when the DOE phase ends: fit the normalizer on the
        REAL history only (well-defined, prediction-independent) and
        record the frontier hypervolume the patience rule compares
        against from here on."""
        nonlocal normalizer_frozen, initial_hv
        if w_hist_l:
            normalizer.fit(np.asarray(w_hist_l, dtype=float),
                           1.0 - np.asarray(u_hist_l, dtype=float))
        nw, nm, n, rw, rm = _frontier_norm()
        hv0 = _hypervolume2d(nw, nm,
                             (normalizer.ref_w, normalizer.ref_m)) \
            if nw is not None else 0.0
        normalizer_frozen = True
        initial_hv = hv0
        if rw is not None:
            logger.info("  frontier baseline at call %d: pts %s",
                        robot_calls, _fmt_frontier(rw, rm))
        return hv0, n

    def _propose() -> Optional[int]:
        """Next grid row (0-based) to spend a Robot call on, or None when
        the whole space is already evaluated/failed."""
        if doe_queue:
            while doe_queue:
                i = doe_queue.pop(0)
                if status_by_index.get(i + 1, "pending") == "pending":
                    return i
            return None if not _pending_rows() else _propose()
        pending = _pending_rows()
        if not pending:
            return None
        if len(w_hist_l) < 2:
            return int(pending[int(rng.integers(len(pending)))])
        pool = np.array(pending, dtype=int)
        if pool.size > ACQ_SUBSAMPLE:
            pool = np.sort(rng.choice(pool, size=ACQ_SUBSAMPLE,
                                      replace=False))
        Xp = X_all[pool]
        try:
            gp_w = _GPSurrogate().fit(np.vstack(X_hist_l),
                                      np.asarray(w_hist_l, dtype=float))
            gp_u = _GPSurrogate().fit(np.vstack(X_hist_l),
                                      np.asarray(u_hist_l, dtype=float))
            pw, pw_s = gp_w.predict(Xp)
            pu, pu_s = gp_u.predict(Xp)
        except np.linalg.LinAlgError as exc:  # ill-conditioned kernel
            logger.warning("GP fit failed (%s) - random proposal.", exc)
            return int(pending[int(rng.integers(len(pending)))])
        if acquisition == "ucb":
            scores = _ucb_scores(pu, pu_s, kappa)
        else:
            if not normalizer_frozen:
                _freeze_normalizer()
            nw, nm, _, _, _ = _frontier_norm()
            if nw is None:  # nothing passes yet: explore on utilization
                scores = _ucb_scores(pu, pu_s, kappa)
            else:
                scores = _ehvi_scores(pw, pw_s, pu, pu_s, nw, nm,
                                      normalizer, MC_SAMPLES, rng)
        return int(pool[int(np.argmax(scores))])

    # ---- one session for the whole run (reuse, never relaunch per
    # candidate - same Phase-1 finding as run_batch) ---------------------- #
    factory = session_factory or (lambda: HeadlessSession(visible=False))
    session = factory()
    session.connect()

    eval_case = eval_case_id or _choose_eval_case(design_space)
    consecutive_failures = 0
    n_evaluated = 0
    n_failed = 0
    robot_calls = 0
    failed_details: List[str] = []
    status = "running"
    stop_reason: Optional[str] = None
    current_hv: Optional[float] = None
    frontier_n = 0
    non_improving = 0

    try:
        while stop_reason is None:
            if robot_calls >= budget:
                stop_reason = "budget_exhausted"
                break
            if storage.is_cancel_requested(run_id):
                status = "cancelled"
                stop_reason = "cancelled"
                break
            if not doe_queue and normalizer_frozen and \
                    non_improving >= patience:
                stop_reason = "patience_exhausted"
                break

            from_doe = bool(doe_queue)
            row_i = _propose()
            if row_i is None:
                stop_reason = "space_exhausted"
                break
            idx = row_i + 1          # candidate_index (1-based)
            cid = cid_by_index[idx]
            logger.info("[call %d/%d] candidate %d of %d%s",
                        robot_calls + 1, budget, idx, total,
                        " (DOE)" if from_doe else " (acquisition)")
            try:
                result = _evaluate_candidate(
                    session, design_space, cand_by_index[idx], eval_case)
                storage.record_result(
                    candidate_id=cid,
                    weight_kg=result["weight_kg"],
                    max_utilization=result["max_utilization"],
                    governing_check=result["governing_check"],
                    buckling_status=result["buckling_status"],
                    pass_fail=("PASS" if result["pass_fail"] else "FAIL"),
                    raw_results_json=json.dumps(result["raw"], default=str),
                )
                status_by_index[idx] = "evaluated"
                n_evaluated += 1
                robot_calls += 1
                consecutive_failures = 0
                X_hist_l.append(X_all[row_i].copy())
                w_hist_l.append(float(result["weight_kg"] or 0.0))
                u_hist_l.append(float(result["max_utilization"] or 0.0))
                logger.info("  -> w=%.1f kg util=%s %s",
                            result["weight_kg"] or 0.0,
                            result["max_utilization"],
                            "PASS" if result["pass_fail"] else "FAIL")

            except _SOLVER_SAFETY_ERRORS as exc:
                n_failed += 1
                robot_calls += 1
                consecutive_failures += 1
                reason = f"{type(exc).__name__}: {exc}"
                failed_details.append(reason)
                storage.mark_candidate_failed(cid, reason)
                status_by_index[idx] = "failed"
                logger.warning("  -> %s", reason)
                if not session.is_alive():
                    logger.warning("    session dead - reconnecting...")
                    session.reconnect()
            except MechanismError as exc:
                n_failed += 1
                robot_calls += 1
                consecutive_failures += 1
                reason = f"mechanism_detected: {exc}"
                failed_details.append(reason)
                storage.mark_candidate_failed(cid, reason)
                status_by_index[idx] = "failed"
                logger.warning("  -> %s", reason)
            except Exception as exc:  # noqa: BLE001
                n_failed += 1
                robot_calls += 1
                consecutive_failures += 1
                reason = f"{type(exc).__name__}: {exc}"
                failed_details.append(reason)
                storage.mark_candidate_failed(cid, reason)
                status_by_index[idx] = "failed"
                logger.warning("  -> %s", reason)
                if not session.is_alive():
                    logger.warning("    session dead - reconnecting...")
                    session.reconnect()

            if consecutive_failures >= max_consecutive_failures:
                status = "failed"
                stop_reason = "failure_abort"
                break

            # Checkpoint after EVERY candidate - same crash contract.
            storage.update_checkpoint(run_id, idx)

            # Frontier tracking + patience (only once the DOE phase is
            # over and the normalizer scale is frozen).
            if not doe_queue and not normalizer_frozen:
                current_hv, frontier_n = _freeze_normalizer()
            elif normalizer_frozen:
                nw, nm, frontier_n, rw, rm = _frontier_norm()
                hv_now = _hypervolume2d(
                    nw, nm, (normalizer.ref_w, normalizer.ref_m)) \
                    if nw is not None else 0.0
                current_hv = hv_now
                if hv_now > (initial_hv or 0.0) + _HV_EPS:
                    non_improving = 0
                    if rw is not None:
                        logger.info(
                            "  frontier improved at call %d: pts %s",
                            robot_calls, _fmt_frontier(rw, rm))
                else:
                    non_improving += 1
                    logger.info("  frontier not improved (%d/%d)",
                                non_improving, patience)
    finally:
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Session close error: %s", exc)

    if status == "running":
        status = "completed"
    storage.mark_run_status(run_id, status)
    _, _, frontier_n, _, _ = _frontier_norm()

    logger.info(
        "Surrogate run %s: %s (%s) - %d evaluated, %d failed, %d Robot "
        "calls, frontier %d, HV %s -> %s in %.1fs.",
        run_id, status, stop_reason, n_evaluated, n_failed, robot_calls,
        frontier_n, initial_hv, current_hv, time.time() - t_start)
    return {
        "run_id": run_id,
        "strategy": "surrogate",
        "status": status,
        "stop_reason": stop_reason,
        "evaluated": n_evaluated,
        "failed": n_failed,
        "robot_calls": robot_calls,
        "total": total,
        "budget": budget,
        "patience": patience,
        "acquisition": acquisition,
        "elapsed_s": round(time.time() - t_start, 1),
        "frontier": frontier_n,
        "initial_hv": (round(initial_hv, 6)
                       if initial_hv is not None else None),
        "final_hv": (round(current_hv, 6)
                     if current_hv is not None else None),
        "training_rows": X_hist.shape[0],
        "training_runs": per_run,
        "failures": failed_details,
    }







