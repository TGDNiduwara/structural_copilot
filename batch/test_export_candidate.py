"""
batch/test_export_candidate.py
==============================
Offline tests for materializing a winning candidate to a .rtd project.

NO Robot COM: a _FakeSession records every build_from_spec geometry and
save_project path, so we can assert
  1. the right design_vars are applied (the geometry Robot would build
     carries exactly the candidate's section map),
  2. solve_all is called once and save_project receives the requested
     output path,
  3. project="2D" specs surface a warning instead of silently saving
     (README "Known issues", 2026-08-22),
  4. export_best_from_run picks the lightest PASSING frontier candidate
     and exports it (plus empty-frontier / index-range errors).

Run:  python batch/test_export_candidate.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.design_space import DesignSpace
from batch.export_candidate import export_best_from_run, export_candidate
from batch.storage import Storage


def _portal_spec(project: str = "3D", load: float = -10.0) -> dict:
    """3-bar portal: 2 columns + 1 beam, pinned bases (test_runner SPEC
    shape). candidate_sections are nominal labels - the fake session never
    touches Robot, so no catalog is needed."""
    return {
        "geometry": {
            "project": project,
            "nodes": [
                {"id": 1, "x": 0, "z": 0}, {"id": 2, "x": 0, "z": 3},
                {"id": 3, "x": 6, "z": 3}, {"id": 4, "x": 6, "z": 0},
            ],
            "bars": [
                {"id": 1, "n1": 1, "n2": 2, "section": "HEA 160"},
                {"id": 2, "n1": 2, "n2": 3, "section": "IPE 200"},
                {"id": 3, "n1": 3, "n2": 4, "section": "HEA 160"},
            ],
            "supports": [
                {"node": 1, "type": "pinned"}, {"node": 4, "type": "pinned"},
            ],
            "cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
            "loads": [{"kind": "bar_uniform", "bar": 2, "case": 1,
                       "direction": "Z", "value": load}],
        },
        "variable_groups": [
            {"group_name": "columns", "bar_ids": [1, 3],
             "candidate_sections": ["HEA 160", "HEA 200", "HEA 240"]},
            {"group_name": "beam", "bar_ids": [2],
             "candidate_sections": ["IPE 200", "IPE 300"]},
        ],
        "load_cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
        "analysis_types": ["static"],
        "objective": {"minimize": "weight",
                      "constraint": "max_utilization <= 1.0 AND "
                                    "buckling_pass == True"},
    }

class _FakeSession:
    """Records the surface export_candidate touches, never touches Robot."""

    def __init__(self):
        self.bridge = self
        self.built_geometries: list = []
        self.saved_paths: list = []
        self.solve_calls = 0
        self.closes = 0

    # -- lifecycle -------------------------------------------------------- #
    def connect(self):
        return self

    def close(self):
        self.closes += 1

    # -- the _evaluate_candidate-style surface ----------------------------- #
    def clear_structure(self, project_type):
        self.cleared = project_type

    def build_from_spec(self, geometry):
        self.built_geometries.append(geometry)
        return {"status": "ok"}

    def validate_stability(self):
        return {"ok": True, "message": ""}

    def solve_all(self, analysis_types):
        self.solve_calls += 1
        return {"static": {"status": "ok", "elapsed_s": 0.01}}

    def save_project(self, path):
        self.saved_paths.append(path)


def _make_factory(fake_holder):
    def factory():
        fake_holder["fake"] = _FakeSession()
        return fake_holder["fake"]
    return factory


def test_export_candidate_applies_design_vars():
    ds = DesignSpace(_portal_spec())
    cands = ds.generate_candidates()
    cand = cands[0]
    tmpdir = tempfile.mkdtemp(prefix="exp_cand_")
    out = os.path.join(tmpdir, "design.rtd")

    holder = {}
    saved = export_candidate(ds, cand, out,
                             session_factory=_make_factory(holder))
    fake = holder["fake"]

    # Right design_vars applied: the geometry Robot would build carries
    # exactly this candidate's section map (apply_to_geometry semantics).
    assert len(fake.built_geometries) == 1
    geom = fake.built_geometries[0]
    got = {b["id"]: b["section"] for b in geom["bars"]}
    assert got == cand["sections"], (got, cand["sections"])
    assert fake.cleared == "3D"

    # Solved once, saved to the exact output path, session closed.
    assert fake.solve_calls == 1
    assert fake.saved_paths == [out], fake.saved_paths
    assert fake.closes == 1
    assert saved == os.path.abspath(out)

    # Bare {bar_id: section} maps are accepted too (apply_to_geometry path).
    holder2 = {}
    bare = {1: "HEA 240", 2: "IPE 300", 3: "HEA 240"}
    export_candidate(ds, bare, out, session_factory=_make_factory(holder2))
    got2 = {b["id"]: b["section"]
            for b in holder2["fake"].built_geometries[0]["bars"]}
    assert got2 == bare, got2
    print("EXPORT CANDIDATE: design_vars applied exactly, solved once, "
          "saved to requested path, session closed")


def test_export_candidate_2d_warns():
    """project='2D' must surface a warning, not silently save (README
    known issue - 2D frames solve invalid on this build)."""
    ds = DesignSpace(_portal_spec(project="2D"))
    cand = ds.generate_candidates()[0]
    tmpdir = tempfile.mkdtemp(prefix="exp_2d_")
    out = os.path.join(tmpdir, "design2d.rtd")

    holder = {}
    records = []
    h = logging.Handler()
    h.emit = lambda r: records.append(r)
    logger = logging.getLogger("structural_copilot.batch.export_candidate")
    logger.addHandler(h)
    try:
        export_candidate(ds, cand, out, session_factory=_make_factory(holder))
    finally:
        logger.removeHandler(h)
    assert any("2D" in r.getMessage() for r in records), \
        [r.getMessage() for r in records]
    # Still saved (the person opens it and sees the problem) - not silent.
    assert holder["fake"].saved_paths == [out]
    print("EXPORT 2D: warning surfaced (not silent), file still saved")

def test_export_best_from_run():
    tmpdir = tempfile.mkdtemp(prefix="exp_run_")
    db = os.path.join(tmpdir, "runs.db")
    ds = DesignSpace(_portal_spec())
    storage = Storage(db_path=db)
    run_id = storage.create_run(ds.to_dict(),
                                objective=json.dumps(ds.objective, default=str))

    weights = {1: 300.0, 2: 350.0, 3: 400.0, 4: 420.0, 5: 500.0, 6: 600.0}
    fail_candidates = {3}   # candidate 3 FAILS the gate
    for cand in ds.generate_candidates():
        cid = storage.add_candidate(run_id, cand)
        w = weights[cand["candidate_index"]]
        u = 0.9 - 0.05 * cand["candidate_index"]
        ok = cand["candidate_index"] not in fail_candidates
        storage.record_result(cid, weight_kg=w, max_utilization=u,
                              pass_fail="PASS" if ok else "FAIL",
                              buckling_status="PASS (no compression members)")
    storage.mark_run_status(run_id, "completed")
    storage.close()

    out = os.path.join(tmpdir, "best.rtd")
    holder = {}
    saved = export_best_from_run(run_id, out, db_path=db,
                                 session_factory=_make_factory(holder))
    fake = holder["fake"]

    # frontier[0] must be the LIGHTEST PASSING candidate (candidate 1 at
    # 300 kg; candidate 3 fails and must be excluded regardless of weight).
    geom = fake.built_geometries[0]
    sections = {b["id"]: b["section"] for b in geom["bars"]}
    assert sections == {1: "HEA 160", 3: "HEA 160", 2: "IPE 200"}, sections
    assert saved == os.path.abspath(out)
    assert fake.saved_paths == [out]

    # frontier_index=1 -> second lightest passing (candidate 2, HEA 160 +
    # IPE 300 - itertools.product order: columns outer, beam inner).
    holder2 = {}
    export_best_from_run(run_id, out, frontier_index=1, db_path=db,
                         session_factory=_make_factory(holder2))
    sections2 = {b["id"]: b["section"]
                 for b in holder2["fake"].built_geometries[0]["bars"]}
    assert sections2 == {1: "HEA 160", 3: "HEA 160", 2: "IPE 300"}, sections2

    # Errors: index out of range; no passing candidates at all.
    try:
        export_best_from_run(run_id, out, frontier_index=99, db_path=db,
                             session_factory=_make_factory(holder))
        raise AssertionError("expected out-of-range error")
    except ValueError:
        pass

    storage2 = Storage(db_path=db)
    all_fail = DesignSpace(_portal_spec(project="3D"))
    run2 = storage2.create_run(all_fail.to_dict(), objective="")
    for cand in all_fail.generate_candidates():
        cid = storage2.add_candidate(run2, cand)
        storage2.record_result(cid, weight_kg=100.0, max_utilization=2.0,
                               pass_fail="FAIL")
    storage2.close()
    try:
        export_best_from_run(run2, out, db_path=db,
                             session_factory=_make_factory(holder))
        raise AssertionError("expected empty-frontier error")
    except ValueError as exc:
        assert "no candidates passing" in str(exc), exc
    print("EXPORT BEST: frontier[0]=lightest PASSING (FAIL excluded), "
          "index selection works, errors raise cleanly")


def main():
    print("=" * 72)
    print("Export-candidate tests (offline, fake session)")
    print("=" * 72)
    test_export_candidate_applies_design_vars()
    test_export_candidate_2d_warns()
    test_export_best_from_run()
    print()
    print("ALL EXPORT-CANDIDATE TESTS PASSED")


if __name__ == "__main__":
    main()


