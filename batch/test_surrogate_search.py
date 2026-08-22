"""
batch/test_surrogate_search.py
==============================
[SURROGATE PHASE A] Offline tests for the surrogate-guided search.

NO Robot COM anywhere: a _FakeSession implements exactly the surface
runner._evaluate_candidate() touches (clear_structure / build_from_spec /
validate_stability / solve_all / get_weight / get_utilization_summary /
bridge.export_all_member_forces) with ANALYTIC synthetic responses whose
optimum is computable in closed form, so the search loop's decisions can
be validated against brute force over the same functions.

Test list
---------
  1. GRID FALLBACK   - small grid -> status grid_fallback, zero spend.
  2. CONFIG ERRORS   - bad budget / acquisition raise SurrogateSearchError.
  3. ENCODING + KEY  - encode_design_vars round-trip; compatibility_key
                       ignores bar sections but not geometry/groups/loads.
  4. TRAINING LOADER - temp runs.db seeded with two past runs (one
                       compatible, one different geometry): only the
                       compatible run's evaluated rows load, with correct
                       per-run counts (failed/pending rows excluded).
  5. GP ACCURACY     - interpolation at training points, bounded error at
                       test points, variance grows away from data.
  6. HYPERVOLUME     - _hypervolume2d equals an exact slicing brute force
                       on random sets (incl. dominated + ref-dominated).
  7. EHVI SANITY     - a candidate whose posterior dominates the frontier
                       scores > 0 and beats one deep in dominated space.
  8. END-TO-END      - 100-candidate grid, fake responses: budget honored,
                       results recorded through real Storage, frontier
                       consistent with pareto.py, brute-force frontier
                       recovered at a fraction of the grid cost.
  9. RESUME          - same run_id re-invoked with a spent budget adds no
                       duplicate evaluations.
 10. BUDGET / PATIENCE stop reasons fire.

Run:  python batch/test_surrogate_search.py
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.design_space import DesignSpace
from batch.pareto import add_strength_margin, compute_pareto_frontier
from batch.storage import Storage
from batch.surrogate_search import (
    ACQUISITION_MODES,
    DEFAULT_BUDGET,
    GRID_FALLBACK_THRESHOLD,
    SurrogateSearchError,
    _GPSurrogate,
    _ObjectiveNormalizer,
    _ehvi_scores,
    _hypervolume2d,
    _maximin_doe,
    compatibility_key,
    encode_design_vars,
    load_training_data,
    run_surrogate_search,
    should_use_grid,
)

# --------------------------------------------------------------------------- #
# Synthetic world: portal frame, 2 columns x 3 m + 1 beam x 6 m (the Phase-5
# test geometry). Weight is exact from unit masses; utilization is a smooth
# function of the normalized section indices with a real PASS/FAIL trade-off.
# --------------------------------------------------------------------------- #

UNIT_MASS = {
    "HEA 160": 30.4, "HEA 180": 35.5, "HEA 200": 42.3, "HEA 220": 50.5,
    "HEA 240": 60.3, "HEB 160": 42.6, "HEB 180": 51.2, "HEB 200": 61.3,
    "HEB 220": 71.5, "HEB 240": 83.2,
    "IPE 200": 22.4, "IPE 220": 26.2, "IPE 240": 30.7, "IPE 270": 36.1,
    "IPE 300": 42.2, "IPE 330": 49.1, "IPE 360": 57.1, "IPE 400": 66.3,
    "IPE 450": 77.6, "IPE 500": 90.7,
}

COLUMNS_10 = ["HEA 160", "HEA 180", "HEA 200", "HEA 220", "HEA 240",
              "HEB 160", "HEB 180", "HEB 200", "HEB 220", "HEB 240"]
BEAMS_10 = ["IPE 200", "IPE 220", "IPE 240", "IPE 270", "IPE 300",
            "IPE 330", "IPE 360", "IPE 400", "IPE 450", "IPE 500"]


def _geometry():
    """Fake-session portal spec. OFFLINE-ONLY (no Robot solve): project
    "2D" is kept for shape parity; live portal specs must use "3D" on this
    build (README "Known issues", 2026-08-22)."""
    return {
        "project": "2D",
        "nodes": [
            {"id": 1, "x": 0, "z": 0}, {"id": 2, "x": 0, "z": 3},
            {"id": 3, "x": 6, "z": 3}, {"id": 4, "x": 6, "z": 0},
        ],
        "bars": [
            {"id": 1, "n1": 1, "n2": 2, "section": "HEA 200"},
            {"id": 2, "n1": 2, "n2": 3, "section": "IPE 300"},
            {"id": 3, "n1": 3, "n2": 4, "section": "HEA 200"},
        ],
        "supports": [
            {"node": 1, "type": "pinned"}, {"node": 4, "type": "pinned"},
        ],
        "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
        "loads": [{"kind": "bar_uniform", "bar": 2, "case": 1,
                   "direction": "Z", "value": -10.0}],
    }


def synthetic_response(column_section: str, beam_section: str,
                       columns: list, beams: list):
    """(weight_kg, max_utilization, passes) for a section pair - the fake
    Robot. Utilization falls with stiffer sections (smooth, interacting)."""
    w = 6.0 * UNIT_MASS[column_section] + 6.0 * UNIT_MASS[beam_section]
    c = columns.index(column_section) / (len(columns) - 1)
    b = beams.index(beam_section) / (len(beams) - 1)
    u = 1.30 - 0.85 * c - 0.55 * b + 0.25 * c * b
    return w, u, (u <= 1.0)


def _spec(columns, beams):
    return {
        "geometry": _geometry(),
        "variable_groups": [
            {"group_name": "columns", "bar_ids": [1, 3],
             "candidate_sections": list(columns)},
            {"group_name": "beam", "bar_ids": [2],
             "candidate_sections": list(beams)},
        ],
        "load_cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
        "analysis_types": ["static"],
        "objective": {"minimize": "weight",
                      "constraint": "max_utilization <= 1.0 AND "
                                    "buckling_pass == True"},
    }


class _FakeSession:
    """Stands in for HeadlessSession against runner._evaluate_candidate.

    All members are tension (positive FX) so _buckling_for_candidate
    short-circuits to 'no compression members' without any COM surface.
    """

    def __init__(self, columns, beams, visible=False):
        self.columns = list(columns)
        self.beams = list(beams)
        self.calls = {"build": 0, "solve": 0, "reconnect": 0}
        self.bridge = self

    # -- session lifecycle ------------------------------------------------ #
    def connect(self):
        return self

    def is_alive(self):
        return True

    def reconnect(self):
        self.calls["reconnect"] += 1

    def close(self):
        pass

    # -- the _evaluate_candidate surface ----------------------------------- #
    def clear_structure(self, project):
        pass

    def build_from_spec(self, geometry):
        self.calls["build"] += 1
        self._sections = {int(b["id"]): str(b["section"])
                          for b in geometry.get("bars", [])}
        return {"status": "ok"}

    def validate_stability(self):
        return {"ok": True, "message": ""}

    def solve_all(self, analysis_types):
        self.calls["solve"] += 1
        return {"static": {"status": "ok", "elapsed_s": 0.01}}

    def get_weight(self):
        w, _, _ = self._response()
        return {"weight_kg": round(w, 2), "boq_rows": 3}

    def get_utilization_summary(self, case_id=1):
        _, u, ok = self._response()
        return {
            "max_utilization": round(u, 4),
            "governing_check": "fake_bending",
            "per_bar": [{"bar_id": i, "utilization": round(u, 4),
                         "governing_check": "fake_bending",
                         "status": "OK" if ok else "FAIL"}
                        for i in (1, 2, 3)],
            "note": "synthetic offline response",
        }

    # -- bridge surface used by the buckling gate -------------------------- #
    def export_all_member_forces(self, case_id=1, divisions=2):
        return pd.DataFrame([
            {"Bar_ID": bid, "Position_m": k * 1.0 / max(divisions - 1, 1),
             "FX_kN": 50.0}   # tension: buckling check not applicable
            for bid in (1, 2, 3) for k in range(max(divisions, 1))
        ])

    # -- internals --------------------------------------------------------- #
    def _response(self):
        return synthetic_response(self._sections[1], self._sections[2],
                                  self.columns, self.beams)


def _brute_force(columns, beams):
    """All (w, u, pass) over the grid + the brute-force Pareto frontier."""
    rows = []
    for cs, bs in itertools.product(columns, beams):
        w, u, ok = synthetic_response(cs, bs, columns, beams)
        rows.append({"columns": cs, "beam": bs, "weight_kg": w,
                     "max_utilization": u,
                     "pass_fail": "PASS" if ok else "FAIL"})
    df = pd.DataFrame(rows)
    return df, compute_pareto_frontier(df)

# --------------------------------------------------------------------------- #
# 1-2. fallback + config errors
# --------------------------------------------------------------------------- #

def test_grid_fallback():
    small = DesignSpace(_spec(["HEA 200", "HEA 220", "HEA 240"],
                              ["IPE 270", "IPE 300", "IPE 330"]))   # 9 grid
    use, why = should_use_grid(small)
    assert use, why
    assert "9 candidates" in why, why

    big = DesignSpace(_spec(COLUMNS_10, BEAMS_10))                  # 100 grid
    use2, why2 = should_use_grid(big)
    assert not use2, why2

    # Boundary: exactly at the threshold -> grid; one over -> surrogate.
    assert should_use_grid(big, budget=100, threshold=100)[0] is True
    assert should_use_grid(big, budget=99, threshold=100)[0] is False
    # Budget below grid size: surrogate (min(budget, threshold) rule).
    assert should_use_grid(big, budget=50, threshold=200)[0] is False

    # run_surrogate_search returns WITHOUT creating a run or a session.
    tmpdir = tempfile.mkdtemp(prefix="sur_fallback_")
    db = os.path.join(tmpdir, "runs.db")
    summary = run_surrogate_search(
        small, db_path=db, log_path=os.path.join(tmpdir, "log.txt"),
        session_factory=lambda: (_ for _ in ()).throw(
            AssertionError("no session may be created on fallback")))
    assert summary["status"] == "grid_fallback", summary
    assert summary["robot_calls"] == 0 and summary["run_id"] is None
    print("FALLBACK: 9-candidate grid short-circuits to grid search, "
          "zero Robot calls, no session")


def test_config_errors():
    ds = DesignSpace(_spec(COLUMNS_10, BEAMS_10))
    for kwargs in ({"budget": 0}, {"patience": 0}, {"acquisition": "pi"}):
        try:
            run_surrogate_search(ds, **kwargs)
            raise AssertionError(f"expected error for {kwargs}")
        except SurrogateSearchError:
            pass
    print("CONFIG: bad budget / patience / acquisition raise cleanly")


# --------------------------------------------------------------------------- #
# 3. encoding + compatibility key
# --------------------------------------------------------------------------- #

def test_encoding_and_key():
    ds = DesignSpace(_spec(COLUMNS_10, BEAMS_10))
    from batch.surrogate_search import _section_index_map
    simap = _section_index_map(ds)

    cand = ds.generate_candidates()[0]
    x = encode_design_vars(cand, simap)
    assert x is not None and x.shape == (2,), x
    # First candidate = lightest of each group -> normalized 0.0, 0.0.
    assert np.allclose(x, [0.0, 0.0]), x
    last = ds.generate_candidates()[-1]
    assert np.allclose(encode_design_vars(last, simap), [1.0, 1.0])

    # Unknown section in a past run's row -> None (row gets skipped).
    bad = {"group_choices": {"columns": "UB 305x165x40", "beam": "IPE 300"}}
    assert encode_design_vars(bad, simap) is None

    # Key: section swaps inside geometry must NOT change it...
    s1 = _spec(COLUMNS_10, BEAMS_10)
    s2 = _spec(COLUMNS_10, BEAMS_10)
    s2["geometry"]["bars"][0]["section"] = "HEB 240"
    assert compatibility_key(s1) == compatibility_key(s2)
    # ...but geometry, groups, sections list, loads and cases must.
    s3 = _spec(COLUMNS_10, BEAMS_10)
    s3["geometry"]["nodes"][3]["x"] = 7.0
    assert compatibility_key(s1) != compatibility_key(s3)
    s4 = _spec(COLUMNS_10, BEAMS_10)
    s4["variable_groups"][0]["candidate_sections"] = list(COLUMNS_10[:9])
    assert compatibility_key(s1) != compatibility_key(s4)
    s5 = _spec(COLUMNS_10, BEAMS_10)
    s5["geometry"]["loads"] = [{"kind": "bar_uniform", "bar": 2,
                                "case": 1, "direction": "Z", "value": -20.0}]
    assert compatibility_key(s1) != compatibility_key(s5)
    # Group order / bar order do not matter (normalized).
    s6 = _spec(COLUMNS_10, BEAMS_10)
    s6["variable_groups"] = list(reversed(s6["variable_groups"]))
    s6["geometry"]["bars"] = list(reversed(s6["geometry"]["bars"]))
    assert compatibility_key(s1) == compatibility_key(s6)
    print("ENCODING/KEY: features round-trip; key ignores sections, "
          "catches geometry/group/load changes, order-insensitive")

# --------------------------------------------------------------------------- #
# 4. cross-run training loader (the runs.db read)
# --------------------------------------------------------------------------- #

def test_training_loader():
    tmpdir = tempfile.mkdtemp(prefix="sur_train_")
    db = os.path.join(tmpdir, "runs.db")
    ds = DesignSpace(_spec(COLUMNS_10, BEAMS_10))

    # Seed run A (compatible): evaluate 5 candidates, fail 1, leave 1 pending.
    storage = Storage(db_path=db)
    run_a = storage.create_run(ds.to_dict(), objective="")
    cands = ds.generate_candidates()[:7]
    ids = [storage.add_candidate(run_a, c) for c in cands]
    for cid, c in zip(ids, cands):
        w, u, ok = synthetic_response(c["group_choices"]["columns"],
                                      c["group_choices"]["beam"],
                                      COLUMNS_10, BEAMS_10)
        if c["candidate_index"] == 3:
            storage.mark_candidate_failed(cid, "mechanism_detected: test")
        elif c["candidate_index"] == 7:
            pass  # stays pending
        else:
            storage.record_result(cid, weight_kg=w, max_utilization=u,
                                  pass_fail="PASS" if ok else "FAIL",
                                  buckling_status="PASS (no compression "
                                                  "members)")
    storage.mark_run_status(run_a, "completed")

    # Seed run B (same spec): 3 more evaluated rows (a second past run).
    run_b = storage.create_run(ds.to_dict(), objective="")
    for c in ds.generate_candidates()[7:10]:
        cid = storage.add_candidate(run_b, c)
        w, u, ok = synthetic_response(c["group_choices"]["columns"],
                                      c["group_choices"]["beam"],
                                      COLUMNS_10, BEAMS_10)
        storage.record_result(cid, weight_kg=w, max_utilization=u,
                              pass_fail="PASS" if ok else "FAIL",
                              buckling_status="PASS (no compression "
                                              "members)")
    storage.mark_run_status(run_b, "completed")

    # Seed run C (DIFFERENT geometry): must be excluded entirely.
    other = _spec(COLUMNS_10, BEAMS_10)
    other["geometry"]["nodes"][3]["x"] = 8.0
    ds_other = DesignSpace(other)
    run_c = storage.create_run(ds_other.to_dict(), objective="")
    for c in ds_other.generate_candidates()[:4]:
        cid = storage.add_candidate(run_c, c)
        storage.record_result(cid, weight_kg=1.0, max_utilization=0.5,
                              pass_fail="PASS")
    storage.mark_run_status(run_c, "completed")
    storage.close()

    storage2 = Storage(db_path=db)
    X, yw, yu, per_run = load_training_data(storage2, ds)
    # Run A contributes candidates 1,2,4,5,6 (3 failed, 7 pending excluded);
    # run B contributes 3 rows (indexes 8,9,10); run C contributes none.
    assert X.shape[0] == len(yw) == len(yu) == 8, X.shape
    assert set(per_run.keys()) == {str(run_a), str(run_b)}, per_run
    assert per_run[str(run_a)] == 5 and per_run[str(run_b)] == 3, per_run
    expected = sorted(
        [synthetic_response(c["group_choices"]["columns"],
                            c["group_choices"]["beam"],
                            COLUMNS_10, BEAMS_10)[0]
         for c in cands if c["candidate_index"] not in (3, 7)]
        + [synthetic_response(c["group_choices"]["columns"],
                              c["group_choices"]["beam"],
                              COLUMNS_10, BEAMS_10)[0]
           for c in ds.generate_candidates()[7:10]])
    assert np.allclose(sorted(yw.tolist()), expected), (yw, expected)
    assert X.min() >= 0.0 and X.max() <= 1.0
    assert len({tuple(np.round(r, 9)) for r in X}) == 8
    print(f"TRAINING LOADER: 8 compatible rows loaded (run A: 5, run B: 3); "
          f"different-geometry run C excluded; failed/pending skipped")

# --------------------------------------------------------------------------- #
# 5-7. GP accuracy, hypervolume brute force, EHVI sanity
# --------------------------------------------------------------------------- #

def test_gp_accuracy():
    rng = np.random.default_rng(7)
    X = np.sort(rng.uniform(0.0, 1.0, size=(10, 1)), axis=0)
    f = lambda t: np.sin(3.0 * t[:, 0])          # noqa: E731
    y = f(X)
    gp = _GPSurrogate().fit(X, y)

    mean_tr, std_tr = gp.predict(X)
    assert np.max(np.abs(mean_tr - y)) < 0.05 * (y.max() - y.min()), \
        np.max(np.abs(mean_tr - y))

    Xs = np.array([[0.15], [0.45], [0.85]])
    mean_te, std_te = gp.predict(Xs)
    err = np.abs(mean_te - f(Xs)) / (y.max() - y.min())
    assert np.max(err) < 0.20, (err, mean_te, f(Xs))

    # Uncertainty grows away from training data (far extrapolation vs
    # training points).
    far = X.max() + 0.5
    _, std_far = gp.predict(np.array([[far]]))
    assert std_far[0] > np.median(std_tr), (std_far, np.median(std_tr))

    # fit() argument validation raises cleanly.
    for bad in ((X[:1], y[:1]), (X, y[:-1])):
        try:
            _GPSurrogate().fit(*bad)
            raise AssertionError("expected shape error")
        except ValueError:
            pass
    print(f"GP: train-point error {np.max(np.abs(mean_tr - y)):.4f}, "
          f"test relative error {np.max(err):.3f}, variance grows off-data")


def _hv_brute_force(f1, f2, ref):
    """Exact hypervolume by x-slab union (independent implementation).

    Points at/eyond the reference on either axis contribute nothing and
    are removed BEFORE building slabs (a point beyond ref[0] must not
    create a slab outside the reference rectangle).
    """
    f1 = np.asarray(f1, dtype=float)
    f2 = np.asarray(f2, dtype=float)
    keep = (f1 < ref[0]) & (f2 < ref[1])
    f1, f2 = f1[keep], f2[keep]
    if f1.size == 0:
        return 0.0
    xs = np.unique(np.concatenate([f1, [ref[0]]]))
    area = 0.0
    for a, b in zip(xs[:-1], xs[1:]):
        mid = 0.5 * (a + b)
        sel = f1 <= mid + 1e-12
        if not np.any(sel):
            continue
        area += max(ref[1] - np.min(f2[sel]), 0.0) * (b - a)
    return area


def test_hypervolume():
    rng = np.random.default_rng(11)
    ref = (1.05, 1.05)
    for trial in range(30):
        n = int(rng.integers(1, 12))
        f1 = rng.uniform(-0.3, 1.2, size=n)
        f2 = rng.uniform(-0.3, 1.2, size=n)
        got = _hypervolume2d(f1, f2, ref)
        want = _hv_brute_force(f1, f2, ref)
        assert abs(got - want) < 1e-9, (trial, got, want, f1, f2)
    # Single dominating point: HV = full rectangle.
    assert abs(_hypervolume2d([0.2], [0.3], ref)
               - (1.05 - 0.2) * (1.05 - 0.3)) < 1e-12
    # Dominated points add nothing.
    hv_one = _hypervolume2d([0.2], [0.3], ref)
    hv_two = _hypervolume2d([0.2, 0.5, 0.9], [0.3, 0.6, 0.95], ref)
    assert abs(hv_one - hv_two) < 1e-12, (hv_one, hv_two)
    print("HYPERVOLUME: 30 random sets match exact slab-union brute force; "
          "dominated/ref-dominated points contribute nothing")


def test_ehvi_sanity():
    # Fitted normalizer (raw units: kg, margin) - fit() flips the margin
    # axis into minimize form exactly like the production path does.
    norm = _ObjectiveNormalizer().fit(np.array([600.0, 900.0]),
                                      np.array([0.5, 0.1]))
    frontier_nw, frontier_nm = norm.norm(np.array([600.0]),
                                         np.array([0.5]))
    # Candidate A: predicted to EXTEND the frontier (clearly lighter at
    # slightly less margin -> new hypervolume). Candidate B: heavier AND
    # less margin -> strictly dominated. Both near-certain (tiny sigma).
    pw = np.array([580.0, 850.0])
    pw_s = np.array([1e-3, 1e-3])
    pu = np.array([0.55, 0.90])     # u<=1 feasible; margin 1-u
    pu_s = np.array([1e-6, 1e-6])
    scores = _ehvi_scores(pw, pw_s, pu, pu_s, frontier_nw, frontier_nm,
                          norm, n_samples=64,
                          rng=np.random.default_rng(3))
    assert scores[0] > 0.0, scores
    assert scores[0] > scores[1] + 1e-6, scores
    assert scores[1] < 1e-6, scores
    # Near-certainly INFEASIBLE prediction (u >> 1) scores ~0.
    s2 = _ehvi_scores(np.array([550.0]), np.array([1e-3]),
                      np.array([2.0]), np.array([1e-6]),
                      frontier_nw, frontier_nm,
                      norm, 64, np.random.default_rng(3))
    assert s2[0] < 1e-6, s2
    print(f"EHVI: frontier-extending candidate scores {scores[0]:.4f} > "
          f"dominated {scores[1]:.6f}; infeasible scores ~0")

# --------------------------------------------------------------------------- #
# 8-10. end-to-end (fake session), resume, stop rules
# --------------------------------------------------------------------------- #

def _run_fake(columns, beams, budget, patience, tmpdir, run_id=None,
              **kwargs):
    ds = DesignSpace(_spec(columns, beams))
    session_holder = {"session": None}

    def factory():
        session_holder["session"] = _FakeSession(columns, beams)
        return session_holder["session"]

    summary = run_surrogate_search(
        ds, budget=budget, patience=patience,
        db_path=os.path.join(tmpdir, "runs.db"),
        log_path=os.path.join(tmpdir, "surrogate.log"),
        session_factory=factory, run_id=run_id, **kwargs)
    return ds, session_holder["session"], summary


def test_end_to_end():
    tmpdir = tempfile.mkdtemp(prefix="sur_e2e_")
    ds, fake, summary = _run_fake(COLUMNS_10, BEAMS_10, budget=45,
                                  patience=15, tmpdir=tmpdir)

    assert summary["status"] == "completed", summary
    assert summary["robot_calls"] == summary["evaluated"] \
        + summary["failed"], summary
    assert summary["robot_calls"] <= 45, summary
    assert summary["total"] == 100, summary
    assert summary["evaluated"] < 100, summary
    assert summary["training_rows"] == 0, summary   # cold start, fresh db
    assert fake.calls["solve"] == summary["robot_calls"], fake.calls

    # Results recorded through the real Storage, one row per candidate.
    storage = Storage(db_path=os.path.join(tmpdir, "runs.db"))
    df = storage.get_all_results(summary["run_id"])
    assert len(df) == 100, len(df)
    ev = df[df["candidate_status"] == "evaluated"]
    assert len(ev) == summary["evaluated"], (len(ev), summary)
    assert ev["weight_kg"].notna().all() and ev["max_utilization"].notna().all()

    # Every evaluated row matches the synthetic world exactly.
    for _, row in ev.iterrows():
        dv = json.loads(row["design_vars_json"])
        w, u, ok = synthetic_response(dv["group_choices"]["columns"],
                                      dv["group_choices"]["beam"],
                                      COLUMNS_10, BEAMS_10)
        assert abs(float(row["weight_kg"]) - w) < 0.01
        assert abs(float(row["max_utilization"]) - u) < 1e-3
        assert row["pass_fail"] == ("PASS" if ok else "FAIL")

    # Frontier consistency vs brute force over the SAME evaluated subset:
    # the recorded rows must reproduce exactly what pareto.py computes on
    # them (self-consistency of the stored data). Points non-dominated
    # among the evaluated subset MAY be dominated by unevaluated grid
    # points - that is expected, so the full-grid comparison is done via
    # hypervolume ratio below, not set inclusion.
    _, bf_front = _brute_force(COLUMNS_10, BEAMS_10)
    front = compute_pareto_frontier(df)
    ev_front = compute_pareto_frontier(ev)
    got_set = {(round(r.weight_kg, 3), round(r.max_utilization, 4))
               for r in front.itertuples()}
    ev_set = {(round(r.weight_kg, 3), round(r.max_utilization, 4))
              for r in ev_front.itertuples()}
    assert got_set == ev_set, (got_set, ev_set)

    lightest_pass = min(bf_front["weight_kg"])
    found_lightest = lightest_pass in front["weight_kg"].values
    bf_w = np.asarray(bf_front["weight_kg"], dtype=float)
    bf_u = np.asarray(bf_front["max_utilization"], dtype=float)
    norm = _ObjectiveNormalizer().fit(bf_w, 1.0 - bf_u)
    hv_target = _hypervolume2d(*norm.norm(bf_w, 1.0 - bf_u),
                               (norm.ref_w, norm.ref_m))
    fw = np.asarray(front["weight_kg"], dtype=float)
    fu = np.asarray(front["max_utilization"], dtype=float)
    hv_found = _hypervolume2d(*norm.norm(fw, 1.0 - fu),
                              (norm.ref_w, norm.ref_m))
    ratio = hv_found / hv_target if hv_target else 1.0
    assert ratio >= 0.80, (ratio, hv_found, hv_target)
    bf_set = {(round(r.weight_kg, 3), round(r.max_utilization, 4))
              for r in bf_front.itertuples()}
    print(f"END-TO-END: {summary['evaluated']}/100 evaluated "
          f"({summary['robot_calls']} calls, stop={summary['stop_reason']}), "
          f"frontier {len(got_set)} pts (full-grid brute force: "
          f"{len(bf_set)}), HV ratio {ratio:.3f}, "
          f"lightest PASS found: {found_lightest}")

def test_resume_no_duplicates():
    tmpdir = tempfile.mkdtemp(prefix="sur_resume_")
    db = os.path.join(tmpdir, "runs.db")
    ds = DesignSpace(_spec(COLUMNS_10, BEAMS_10))

    class _RecordingFake(_FakeSession):
        def __init__(self, log, **kwargs):
            super().__init__(COLUMNS_10, BEAMS_10, **kwargs)
            self._log = log

        def solve_all(self, analysis_types):
            self._log.append((self._sections[1], self._sections[2]))
            return super().solve_all(analysis_types)

    log1: list = []
    s1 = run_surrogate_search(
        ds, budget=12, patience=50, db_path=db,
        log_path=os.path.join(tmpdir, "surrogate.log"),
        session_factory=lambda: _RecordingFake(log1))
    assert s1["robot_calls"] == 12 == len(log1), s1

    # Resume the SAME run with a fresh per-invocation budget: continues
    # searching (never re-spends an evaluated design), and trains on the
    # first invocation's rows via the cross-run loader.
    log2: list = []
    s2 = run_surrogate_search(
        ds, budget=20, patience=50, db_path=db,
        log_path=os.path.join(tmpdir, "surrogate.log"), run_id=s1["run_id"],
        session_factory=lambda: _RecordingFake(log2))
    assert s2["robot_calls"] == len(log2) and s2["robot_calls"] > 0, s2
    combined = log1 + log2
    assert len(combined) == len(set(combined)), "a design was re-evaluated"
    assert s2["training_rows"] >= s1["evaluated"], s2
    assert str(s1["run_id"]) in (s2["training_runs"] or {}), s2

    storage = Storage(db_path=db)
    df = storage.get_all_results(s1["run_id"])
    assert len(df) == 100
    assert len(df[df["candidate_status"] == "evaluated"]) \
        == s1["evaluated"] + s2["evaluated"]
    print(f"RESUME: continued run spent {s2['robot_calls']} more calls, "
          f"0 duplicate designs across {len(combined)} evaluations, "
          f"trained on {s2['training_rows']} cross-run rows")


class _AllFailFake(_FakeSession):
    """Every candidate fails utilization -> the frontier can never grow,
    so patience must fire after DOE + `patience` non-improving calls."""

    def get_utilization_summary(self, case_id=1):
        return {
            "max_utilization": 2.0,
            "governing_check": "fake_bending",
            "per_bar": [{"bar_id": i, "utilization": 2.0,
                         "governing_check": "fake_bending",
                         "status": "FAIL"} for i in (1, 2, 3)],
            "note": "synthetic offline response (all FAIL)",
        }


def test_stop_rules():
    tmpdir = tempfile.mkdtemp(prefix="sur_stop_")
    ds, _, s = _run_fake(COLUMNS_10[:8], BEAMS_10[:9], budget=5, patience=50,
                         tmpdir=tmpdir)                    # 72-grid
    assert s["stop_reason"] == "budget_exhausted", s
    assert s["robot_calls"] == 5, s

    # Patience: a world where nothing can pass -> zero frontier growth
    # -> stop after the 4-point DOE + 3 non-improving proposals.
    tmpdir2 = tempfile.mkdtemp(prefix="sur_stop2_")
    ds2 = DesignSpace(_spec(COLUMNS_10, BEAMS_10))
    s2 = run_surrogate_search(
        ds2, budget=200, patience=3,
        db_path=os.path.join(tmpdir2, "runs.db"),
        log_path=os.path.join(tmpdir2, "surrogate.log"),
        session_factory=lambda: _AllFailFake(COLUMNS_10, BEAMS_10))
    assert s2["stop_reason"] == "patience_exhausted", s2
    assert s2["robot_calls"] <= 4 + 3 + 3, s2    # DOE + patience + slack
    assert s2["frontier"] == 0, s2
    print(f"STOP RULES: budget stop at {s['robot_calls']} calls; "
          f"patience stop after {s2['robot_calls']} calls in an "
          f"all-FAIL world (frontier stays empty)")


def main():
    print("=" * 72)
    print("Surrogate Phase A - surrogate-guided sizing search tests (offline)")
    print("=" * 72)
    test_grid_fallback()
    test_config_errors()
    test_encoding_and_key()
    test_training_loader()
    test_gp_accuracy()
    test_hypervolume()
    test_ehvi_sanity()
    test_end_to_end()
    test_resume_no_duplicates()
    test_stop_rules()
    print()
    print("ALL SURROGATE PHASE A TESTS PASSED")


if __name__ == "__main__":
    main()






