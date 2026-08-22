"""
batch/test_topology_compare.py
==============================
Offline test for the chat topology-compare tool (#3): a fake multi-variant
flow using the REAL pure template generators (RobotBridge.truss_spec /
braced_frame_spec) and a fake session (full runner._evaluate_candidate
surface) with synthetic weight/util responses, so compare_topologies runs
the real run_batch machinery end-to-end without Robot.

Run:  python batch/test_topology_compare.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

import pandas as pd

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.topology_compare import compare_topologies
from batch.storage import Storage


def _size_of(section: str) -> int:
    m = re.search(r"(\d+)", str(section))
    return int(m.group(1)) if m else 0


def _unit_mass(section: str) -> float:
    return 20.0 + _size_of(section) / 10.0   # kg/m, monotonic in size


class _FakeSession:
    """Full runner._evaluate_candidate surface; synthetic responses:
    utilization falls with section size (light FAIL / heavy PASS), so the
    optimizer has a real feasibility boundary to rank."""

    def __init__(self, visible=False):
        self.bridge = self
        self.calls = {"build": 0, "solve": 0}
        self._sections = {}

    # lifecycle
    def connect(self):
        return self

    def is_alive(self):
        return True

    def reconnect(self):
        pass

    def close(self):
        pass

    # _evaluate_candidate surface
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
        w, _ = self._response()
        return {"weight_kg": round(w, 2), "boq_rows": len(self._sections)}

    def get_utilization_summary(self, case_id=1):
        _, u = self._response()
        ok = u <= 1.0
        return {
            "max_utilization": round(u, 4),
            "governing_check": "synthetic_bending",
            "per_bar": [{"bar_id": bid, "utilization": round(u, 4),
                         "governing_check": "synthetic_bending",
                         "status": "OK" if ok else "FAIL"}
                        for bid in self._sections],
            "note": "synthetic offline response",
        }

    # bridge surface used by the buckling gate (tension -> not applicable)
    def export_all_member_forces(self, case_id=1, divisions=2):
        return pd.DataFrame([
            {"Bar_ID": bid, "Position_m": k * 1.0 / max(divisions - 1, 1),
             "FX_kN": 50.0}
            for bid in self._sections for k in range(max(divisions, 1))
        ])

    def _response(self):
        """(weight_kg, max_utilization) - pseudo lengths so weight scales
        with section size; utilization = 1.15 - 0.002*size (all default
        catalog sizes >= 80 pass, so the frontier is non-empty and the
        lightest passing design wins)."""
        weight = sum(3.0 * _unit_mass(sec) for sec in self._sections.values())
        worst = max((1.15 - 0.0020 * _size_of(sec))
                    for sec in self._sections.values()) or 1.15
        return weight, worst


def _load_spec():
    return {"cases": [{"id": 1, "name": "DL", "nature": "permanent"}],
            "loads": []}

def test_compare_topologies_fake_flow():
    tmpdir = tempfile.mkdtemp(prefix="topo_cmp_")
    db = os.path.join(tmpdir, "runs.db")
    log = os.path.join(tmpdir, "topo.log")

    holder = {"n": 0}

    def factory():
        holder["n"] += 1
        return _FakeSession()

    variants = [
        {"name": "truss", "generator": "create_truss",
         "generator_args": {"span": 6.0, "height": 1.5, "panels": 3}},
        {"name": "frame", "generator": "create_braced_frame",
         "generator_args": {"height": 3.0, "width": 6.0}},
    ]
    result = compare_topologies(variants, _load_spec(),
                                db_path=db, log_path=log,
                                session_factory=factory)

    assert result["status"] == "ok"
    ranked = result["variants"]
    assert len(ranked) == 2, ranked

    # One distinct run per variant; results actually in runs.db.
    st = Storage(db_path=db)
    runs = st.get_all_results_all_runs()
    run_ids = sorted(int(r) for r in runs["run_id"].unique())
    st.close()
    assert len(run_ids) == 2, run_ids
    assert {v["run_id"] for v in ranked} == set(run_ids)

    # Every variant found a lightest PASSING design (beams pass in the
    # synthetic world), ranked by weight ascending with sections reported.
    for v in ranked:
        assert v["weight_kg"] is not None, v
        assert v["max_utilization"] is not None and v["max_utilization"] <= 1.0
        assert isinstance(v["sections"], dict) and v["sections"], v
        assert v["grid_candidates"] > 0, v
    assert ranked[0]["weight_kg"] <= ranked[1]["weight_kg"]
    print(f"TOPOLOGY COMPARE: {len(ranked)} variants sized via real "
          f"run_batch + fake session; ranked lightest-first "
          f"({[(v['name'], v['weight_kg']) for v in ranked]}); 2 runs in db")

    # Unknown generator raises cleanly (no Robot).
    try:
        compare_topologies(
            [{"name": "x", "generator": "nope"}], _load_spec(),
            db_path=db, log_path=log, session_factory=factory)
        raise AssertionError("unknown generator must raise")
    except ValueError as exc:
        assert "unknown generator" in str(exc), exc
    print("  unknown generator rejected cleanly")


def main():
    print("=" * 72)
    print("Topology-compare tests (offline, fake multi-variant flow)")
    print("=" * 72)
    test_compare_topologies_fake_flow()
    print()
    print("ALL TOPOLOGY-COMPARE TESTS PASSED")


if __name__ == "__main__":
    main()

