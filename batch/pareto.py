"""
batch/pareto.py
===============
[PHASE 6] Pareto frontier computation for the batch optimizer.

Given a DataFrame of evaluated candidates (the shape returned by
storage.get_all_results()), compute the non-dominated set across the chosen
objectives, subject to a HARD constraint gate.

Design constraints (from the design_space objective, e.g.
"max_utilization <= 1.0 AND buckling_pass == True"):
  * pass_fail == "PASS" is a hard filter - a candidate that FAILS the
    elastic-utilization / buckling constraints is NOT a valid point on the
    Pareto frontier, no matter how light it is. It is excluded entirely,
    not traded off against weight.
  * strength_margin is derived from what is already computed:
      util_margin     = 1 - max_utilization
      buckling_margin = parsed from buckling_status (N vs Pcr) when present
      strength_margin = min(util_margin, buckling_margin or +inf)

Same caveat discipline as everywhere else in this pipeline: utilization is
an ELASTIC STRESS check and buckling is a BASIC EULER screening - neither
is full code compliance. The frontier is a screening-level ranking tool.

Synthetic-data validation (see test_pareto.py) is the gate before trusting
this on real results.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.result_store import _df_to_markdown

logger = logging.getLogger("structural_copilot.batch.pareto")

#: Matches the runner's buckling_status string, e.g.
#: "FAIL: worst bar 2 (IPE 270): N=30.0 kN vs Pcr=3080.0 kN, KL/r=..."
_BUCKLING_RE = re.compile(r"N=([0-9.]+)\s*kN vs Pcr=([0-9.]+)\s*kN")


# --------------------------------------------------------------------------- #
# strength margin
# --------------------------------------------------------------------------- #

def buckling_margin_from_status(status: Optional[str]) -> Optional[float]:
    """Buckling reserve = 1 - N/Pcr parsed from the runner's buckling_status
    string, or None when the status carries no numeric margin (e.g.
    "PASS (no compression members)")."""
    if not status:
        return None
    m = _BUCKLING_RE.search(str(status))
    if not m:
        return None
    try:
        n = float(m.group(1))
        pcr = float(m.group(2))
    except (TypeError, ValueError):
        return None
    if pcr <= 0.0:
        return None
    return 1.0 - n / pcr


def strength_margin_of(max_utilization: Any,
                       buckling_status: Optional[str]) -> Optional[float]:
    """Min(utilization margin, buckling margin). Returns None when
    utilization is missing (cannot certify)."""
    if max_utilization is None:
        return None
    try:
        u = float(max_utilization)
    except (TypeError, ValueError):
        return None
    util_margin = 1.0 - u
    bm = buckling_margin_from_status(buckling_status)
    if bm is None:
        return util_margin
    return min(util_margin, bm)


def add_strength_margin(df: pd.DataFrame) -> pd.DataFrame:
    """Adds a strength_margin column (in place) from max_utilization and
    buckling_status when it does not already exist."""
    if "strength_margin" in df.columns:
        return df
    margins: List[float] = []
    for _, row in df.iterrows():
        margins.append(strength_margin_of(
            row.get("max_utilization"), row.get("buckling_status")))
    df["strength_margin"] = margins
    return df


# --------------------------------------------------------------------------- #
# hard constraint gate
# --------------------------------------------------------------------------- #

def _passes_constraints(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Keeps only candidates that satisfy the design-space hard constraints:
    evaluated, pass_fail == PASS, and having weight + utilization values."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    ok = pd.Series(True, index=df.index)
    if "pass_fail" in df.columns:
        ok &= df["pass_fail"].astype(str).str.upper().eq("PASS")
    if "candidate_status" in df.columns:
        ok &= df["candidate_status"].astype(str).eq("evaluated")
    if "weight_kg" in df.columns:
        ok &= pd.to_numeric(df["weight_kg"], errors="coerce").notna()
    if "max_utilization" in df.columns:
        ok &= pd.to_numeric(df["max_utilization"], errors="coerce").notna()
    return df[ok]


# --------------------------------------------------------------------------- #
# pareto dominance
# --------------------------------------------------------------------------- #

def _dominates(a: pd.Series, b: pd.Series,
               minimize: List[str], maximize: List[str]) -> bool:
    """True if a dominates b: a is >= as good on every objective and
    strictly better on at least one."""
    better = False
    for col in minimize:
        av, bv = float(a[col]), float(b[col])
        if av > bv + 1e-9:
            return False
        if av < bv - 1e-9:
            better = True
    for col in maximize:
        av, bv = float(a[col]), float(b[col])
        if av < bv - 1e-9:
            return False
        if av > bv + 1e-9:
            better = True
    return better


def compute_pareto_frontier(
    results_df: pd.DataFrame,
    minimize: Optional[List[str]] = None,
    maximize: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Filters a results DataFrame to the non-dominated candidates only.

    Hard constraint gate first (pass_fail == PASS etc.), then standard
    Pareto dominance over the given objectives.

    Parameters
    ----------
    results_df : pd.DataFrame
        Evaluated candidates, shape of storage.get_all_results().
    minimize : list[str]
        Objectives to minimize (default ["weight_kg"]).
    maximize : list[str]
        Objectives to maximize (default ["strength_margin"]).

    Returns the non-dominated subset (empty if no candidate passes the
    constraint gate). The caller can attach summary info to .attrs.
    """
    minimize = list(minimize) if minimize else ["weight_kg"]
    maximize = list(maximize) if maximize else ["strength_margin"]

    passed = _passes_constraints(results_df)
    total = len(results_df)
    if passed.empty:
        out = passed.copy()
        out.attrs["total"] = total
        out.attrs["passed"] = 0
        out.attrs["frontier"] = 0
        out.attrs["note"] = (
            "No candidate passes the hard constraint gate "
            "(evaluated + pass_fail == PASS + weight/util present) - "
            "the Pareto set is empty.")
        return out

    work = add_strength_margin(passed.copy())
    obj_cols = [c for c in minimize + maximize if c in work.columns]
    if not obj_cols:
        raise ValueError(
            "None of the requested objective columns exist in the results "
            f"(have: {list(work.columns)})")
    work = work.dropna(subset=obj_cols)

    idx = list(work.index)
    nondom: List[Any] = []
    for i in idx:
        dominated = False
        for j in idx:
            if i == j:
                continue
            if _dominates(work.loc[j], work.loc[i], minimize, maximize):
                dominated = True
                break
        if not dominated:
            nondom.append(i)

    out = work.loc[nondom].copy()
    out.attrs["total"] = total
    out.attrs["passed"] = len(passed)
    out.attrs["frontier"] = len(out)
    return out


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def _design_label(row: pd.Series) -> str:
    """Human-readable design summary from design_vars_json (group_choices)."""
    import json
    try:
        dv = json.loads(row.get("design_vars_json") or "{}")
        gc = dv.get("group_choices") or {}
        return " | ".join(f"{k}={v}" for k, v in gc.items()) or "(no groups)"
    except Exception:  # noqa: BLE001
        return "(unknown)"


def pareto_summary(
    results_df: pd.DataFrame,
    minimize: Optional[List[str]] = None,
    maximize: Optional[List[str]] = None,
    max_rows: int = 12,
) -> Dict[str, Any]:
    """Builds a report dict for the Pareto frontier:
       total / passed / frontier counts, note, and a markdown table ranking
       the frontier by weight with utilization + buckling margins shown.
    """
    frontier = compute_pareto_frontier(results_df, minimize, maximize)
    info: Dict[str, Any] = {
        "total": frontier.attrs.get("total", len(results_df)),
        "passed": frontier.attrs.get("passed", 0),
        "frontier": frontier.attrs.get("frontier", len(frontier)),
        "note": frontier.attrs.get("note", ""),
    }
    if frontier.empty:
        info["markdown"] = "_no candidates pass the design constraint_"
        return info

    rank = frontier.sort_values(
        ["weight_kg", "strength_margin"], ascending=[True, False])
    # Focused table: only the columns that matter for a ranking, not the raw
    # JSON blobs (design_vars_json / raw_results_json) the full DF carries.
    view = pd.DataFrame(index=rank.index)
    if "design_vars_json" in rank.columns:
        view["design"] = rank.apply(_design_label, axis=1)
    for c in ["candidate_id", "weight_kg", "max_utilization",
              "governing_check", "strength_margin", "pass_fail",
              "buckling_status"]:
        if c in rank.columns:
            view[c] = rank[c]
    # Compact buckling_status: drop the long explanatory suffix.
    if "buckling_status" in view.columns:
        view["buckling_status"] = view["buckling_status"].apply(
            lambda s: (str(s).split(":")[0] + ":") if s else "")
    info["markdown"] = (
        "**Pareto frontier (ranked by weight)**\n"
        + _df_to_markdown(view, max_rows=max_rows)
        + "\n_(elastic stress + basic Euler buckling screening only - "
          "not full code compliance)_")
    return info
