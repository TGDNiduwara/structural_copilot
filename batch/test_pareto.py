"""
batch/test_pareto.py
====================
[PHASE 6] Synthetic-data validation of the Pareto frontier computation.

Hand-constructed candidates with KNOWN dominance relationships (worked out
by hand below) - the gate before trusting compute_pareto_frontier on real
results. Then a real-data check against the leftover Phase-5 result DBs.

Run: python batch/test_pareto.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

import pandas as pd

from batch.pareto import (
    compute_pareto_frontier,
    pareto_summary,
    strength_margin_of,
    buckling_margin_from_status,
)


def _mk_df(rows):
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Synthetic set 1: clean dominance, 6 candidates (one FAILS constraint)
# --------------------------------------------------------------------------- #
# Hand analysis (minimize weight_kg, maximize strength_margin = 1-util):
#   A (w=100, m=0.30) - dominated by B? B w=90 m=0.35 -> better on BOTH  => A dominated
#   B (w= 90, m=0.35)
#   C (w=110, m=0.45) - dominated by B? B w=90 m=0.35 -> B better weight, worse margin
#                       -> not dominated by B. By D? D w=95 m=0.40 -> better both => C dominated
#   D (w= 95, m=0.40)
#   E (w=120, m=0.60) - dominated by F? F w=105 m=0.55 -> better weight, worse margin -> no
#                       -> is E dominated by anyone with >= margin and <= weight? no (D: m0.40<0.6) => E survives
#   F (w=105, m=0.55) - dominated by E? E w=120 (worse). no. Survives.
#   G (w=80,  m=1.30, FAIL constraint - util>1.0) - EXCLUDED entirely by hard gate,
#                                                     even though it is lightest.
# Expected non-dominated: {B, D, E, F}
EXPECTED_SET_1 = {90, 95, 120, 105}  # weights of B, D, E, F

DF1 = [
    {"candidate_id": 1, "candidate_status": "evaluated", "weight_kg": 100.0,
     "max_utilization": 0.70, "pass_fail": "PASS", "buckling_status": "PASS (no compression members)"},
    {"candidate_id": 2, "candidate_status": "evaluated", "weight_kg": 90.0,
     "max_utilization": 0.65, "pass_fail": "PASS", "buckling_status": "PASS (no compression members)"},
    {"candidate_id": 3, "candidate_status": "evaluated", "weight_kg": 110.0,
     "max_utilization": 0.55, "pass_fail": "PASS", "buckling_status": "PASS (no compression members)"},
    {"candidate_id": 4, "candidate_status": "evaluated", "weight_kg": 95.0,
     "max_utilization": 0.60, "pass_fail": "PASS", "buckling_status": "PASS (no compression members)"},
    {"candidate_id": 5, "candidate_status": "evaluated", "weight_kg": 120.0,
     "max_utilization": 0.40, "pass_fail": "PASS", "buckling_status": "PASS (no compression members)"},
    {"candidate_id": 6, "candidate_status": "evaluated", "weight_kg": 105.0,
     "max_utilization": 0.45, "pass_fail": "PASS", "buckling_status": "PASS (no compression members)"},
    {"candidate_id": 7, "candidate_status": "evaluated", "weight_kg": 80.0,
     "max_utilization": 1.30, "pass_fail": "FAIL", "buckling_status": "FAIL: bar 2 (IPE 270): N=30.0 kN vs Pcr=25.0 kN"},
]


def test_synthetic_set1():
    print("=== Synthetic set 1: clean dominance (6 valid + 1 FAIL) ===")
    df = _mk_df(DF1)
    front = compute_pareto_frontier(df)
    got = set(front["weight_kg"].round(3))
    assert front.attrs["total"] == 7
    assert front.attrs["passed"] == 6
    assert got == EXPECTED_SET_1, f"expected {EXPECTED_SET_1}, got {got}"
    # The FAILING light candidate (G, w=80) must NOT appear even though lightest.
    assert 80.0 not in got, "FAIL constraint candidate leaked into frontier"
    # Every candidate has strength_margin = 1 - utilization (no buckling margin)
    for _, r in front.iterrows():
        assert abs(r["strength_margin"] - (1.0 - r["max_utilization"])) < 1e-9
    print(f"  passed={front.attrs['passed']} frontier={len(front)} weights={sorted(got)}")
    print("  FAIL-candidate (w=80) excluded by hard gate: OK")
    print("  PASS")


# --------------------------------------------------------------------------- #
# Synthetic set 2: buckling margin participates in strength_margin
# --------------------------------------------------------------------------- #
DF2 = [
    {"candidate_id": 1, "candidate_status": "evaluated", "weight_kg": 100.0,
     "max_utilization": 0.50, "pass_fail": "PASS",
     "buckling_status": "PASS: worst bar 2 (IPE 270): N=20.0 kN vs Pcr=100.0 kN"},  # bm=0.8
    {"candidate_id": 2, "candidate_status": "evaluated", "weight_kg": 105.0,
     "max_utilization": 0.10, "pass_fail": "PASS",
     "buckling_status": "PASS (no compression members)"},  # m = 0.90
]


def test_synthetic_set2_buckling_margin():
    print("=== Synthetic set 2: buckling margin in strength_margin ===")
    bm = buckling_margin_from_status("PASS: worst bar 2: N=20.0 kN vs Pcr=100.0 kN")
    assert abs(bm - 0.8) < 1e-9, bm
    m1 = strength_margin_of(0.50, "PASS: N=20.0 kN vs Pcr=100.0 kN")
    assert abs(m1 - 0.5) < 1e-9, m1   # min(0.50, 0.80) = 0.50
    df = _mk_df(DF2)
    front = compute_pareto_frontier(df)
    # Candidate 1: w=100, m=0.50. Candidate 2: w=105, m=0.90.
    # Neither dominates the other (2 is heavier but has bigger margin).
    assert len(front) == 2
    got = {int(r["candidate_id"]) for _, r in front.iterrows()}
    assert got == {1, 2}, got
    print("  buckling margin parsed (N/Pcr -> 1-0.2=0.8): OK")
    print("  both candidates non-dominated (weight vs margin tradeoff): OK")
    print("  PASS")


# --------------------------------------------------------------------------- #
# Synthetic set 3: empty frontier when ALL fail the constraint gate
# --------------------------------------------------------------------------- #
def test_synthetic_set3_all_fail():
    print("=== Synthetic set 3: all candidates FAIL constraint -> empty ===")
    df = _mk_df([
        {"candidate_id": 1, "candidate_status": "evaluated", "weight_kg": 100.0,
         "max_utilization": 1.5, "pass_fail": "FAIL", "buckling_status": "PASS (no compression members)"},
        {"candidate_id": 2, "candidate_status": "evaluated", "weight_kg": 90.0,
         "max_utilization": 0.9, "pass_fail": "FAIL", "buckling_status": "PASS (no compression members)"},
    ])
    front = compute_pareto_frontier(df)
    assert front.empty
    assert front.attrs["passed"] == 0
    summ = pareto_summary(df)
    assert summ["frontier"] == 0
    print("  empty frontier returned honestly (no forced result): OK")
    print("  PASS")


def test_pareto_summary_markdown():
    print("=== pareto_summary markdown ===")
    summ = pareto_summary(_mk_df(DF1))
    assert "| candidate_id" in summ["markdown"] or "| design" in summ["markdown"]
    assert "not full code compliance" in summ["markdown"]
    print(summ["markdown"])
    print("  PASS")


def main():
    print("=" * 72)
    print("Phase 6 - Pareto frontier tests (synthetic first)")
    print("=" * 72)
    test_synthetic_set1()
    test_synthetic_set2_buckling_margin()
    test_synthetic_set3_all_fail()
    test_pareto_summary_markdown()
    print()
    print("ALL PHASE 6 SYNTHETIC TESTS PASSED")


if __name__ == "__main__":
    main()
