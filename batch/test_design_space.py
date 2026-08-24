"""
batch/test_design_space.py
==========================
Phase 4 tests: design-space schema validation, grid-search candidate
generation, candidate counts for test specs, cap enforcement, and the
apply_to_geometry section map.

Run:  python batch/test_design_space.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from batch.design_space import (
    DesignSpace,
    DesignSpaceError,
)


def _geometry():
    """A tiny 2-bar model spec (portal-ish) used by all test specs.

    NOTE: project "2D" here is OFFLINE-ONLY (no Robot solve) - kept for
    spec-shape parity. Live "2D" portal specs are structurally invalid on
    this build (see README "Known issues", 2026-08-22); live tests use
    project "3D".
    """
    return {
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
    }


SPEC_A = {
    "geometry": _geometry(),
    "variable_groups": [
        {
            "group_name": "columns",
            "bar_ids": [1, 3],
            "candidate_sections": ["HEA 200", "HEA 220", "HEA 240", "HEB 200"],
        },
        {
            "group_name": "beam",
            "bar_ids": [2],
            "candidate_sections": ["IPE 270", "IPE 300", "IPE 330"],
        },
    ],
    "load_cases": [
        {"id": 1, "name": "DL", "nature": "permanent"},
        {"id": 2, "name": "LL", "nature": "imposed"},
    ],
    "analysis_types": ["static"],
    "objective": {
        "minimize": "weight",
        "constraint": "max_utilization <= 1.0 AND buckling_pass == True",
    },
}

SPEC_B = {
    "geometry": _geometry(),
    "variable_groups": [
        {"group_name": "columns", "bar_ids": [1, 3], "candidate_sections": ["HEA 200", "HEB 200"]},
        {
            "group_name": "beam",
            "bar_ids": [2],
            "candidate_sections": ["IPE 270", "IPE 300", "IPE 330"],
        },
    ],
    "analysis_types": ["static", "modal"],
}


def test_spec_a_counts():
    ds = DesignSpace(SPEC_A)
    count = ds.candidate_count()
    cands = ds.generate_candidates()
    assert count == 12, count  # 4 columns x 3 beams
    assert len(cands) == 12, len(cands)
    print(f"SPEC A: 4 column options x 3 beam options = {count} candidates")
    print(ds.describe())
    # First candidate:
    c0 = cands[0]
    assert c0["candidate_index"] == 1
    assert c0["group_choices"] == {"columns": "HEA 200", "beam": "IPE 270"}
    assert c0["sections"] == {1: "HEA 200", 3: "HEA 200", 2: "IPE 270"}
    # Last candidate wraps both groups:
    last = cands[-1]
    assert last["group_choices"] == {"columns": "HEB 200", "beam": "IPE 330"}
    assert last["sections"] == {1: "HEB 200", 3: "HEB 200", 2: "IPE 330"}
    # Full sweep covers every combination:
    seen = {(c["group_choices"]["columns"], c["group_choices"]["beam"]) for c in cands}
    assert len(seen) == 12
    print("  OK: 12/12 combinations present, group_choices + sections correct")
    print()


def test_spec_b_counts():
    ds = DesignSpace(SPEC_B)
    count = ds.candidate_count()
    cands = ds.generate_candidates()
    assert count == 6, count  # 2 columns x 3 beams
    assert len(cands) == 6, len(cands)
    assert ds.analysis_types == ["static", "modal"]
    assert ds.load_cases == []  # optional, absent in SPEC_B
    print(f"SPEC B: 2 column options x 3 beam options = {count} candidates")
    print(ds.describe())
    print("  OK: count, analysis_types passthrough, load_cases default")
    print()


def test_cap_enforcement():
    # 3 groups x 5 sections = 125 would be fine; use a tiny cap to prove raise.
    spec = dict(SPEC_A)
    spec["max_candidates"] = 5
    ds = DesignSpace(spec)
    try:
        ds.generate_candidates()
        raise AssertionError("Expected DesignSpaceError for over-cap grid")
    except DesignSpaceError as e:
        assert "would generate 12 candidates" in str(e)
        assert "cap 5" in str(e)
        print("CAP: grid of 12 with cap 5 correctly raises:", str(e)[:80], "...")
        print()


def test_validation_errors():
    # duplicate group names
    spec = dict(SPEC_A)
    spec["variable_groups"] = [
        {"group_name": "cols", "bar_ids": [1], "candidate_sections": ["HEA 200"]},
        {"group_name": "cols", "bar_ids": [3], "candidate_sections": ["HEA 220"]},
    ]
    try:
        DesignSpace(spec)
        raise AssertionError("expected duplicate-group error")
    except DesignSpaceError as e:
        assert "Duplicate variable group" in str(e)

    # overlapping bar_ids between groups
    spec = dict(SPEC_A)
    spec["variable_groups"] = [
        {"group_name": "a", "bar_ids": [1, 2], "candidate_sections": ["HEA 200"]},
        {"group_name": "b", "bar_ids": [2, 3], "candidate_sections": ["HEA 220"]},
    ]
    try:
        DesignSpace(spec)
        raise AssertionError("expected overlap error")
    except DesignSpaceError as e:
        assert "overlap" in str(e)

    # bar_ids referencing nonexistent bars
    spec = dict(SPEC_A)
    spec["variable_groups"] = [
        {"group_name": "a", "bar_ids": [99], "candidate_sections": ["HEA 200"]},
    ]
    try:
        DesignSpace(spec)
        raise AssertionError("expected missing-bar error")
    except DesignSpaceError as e:
        assert "not present in geometry.bars" in str(e)

    # empty candidate_sections
    spec = dict(SPEC_A)
    spec["variable_groups"] = [
        {"group_name": "a", "bar_ids": [1], "candidate_sections": []},
    ]
    try:
        DesignSpace(spec)
        raise AssertionError("expected empty-sections error")
    except DesignSpaceError as e:
        assert "empty candidate_sections" in str(e)

    # unsupported analysis type
    spec = dict(SPEC_A)
    spec["analysis_types"] = ["response_spectrum"]
    try:
        DesignSpace(spec)
        raise AssertionError("expected unsupported-analysis error")
    except DesignSpaceError as e:
        assert "Unsupported analysis_type" in str(e)

    print(
        "VALIDATION: duplicate group / overlap / missing bar_id / "
        "empty sections / unsupported analysis all raise cleanly"
    )
    print()


def test_apply_to_geometry():
    ds = DesignSpace(SPEC_A)
    cand = ds.generate_candidates()[5]  # some middle candidate
    geom = ds.apply_to_geometry(cand)
    bars = {b["id"]: b["section"] for b in geom["bars"]}
    assert bars == cand["sections"], (bars, cand["sections"])
    # Original geometry untouched (deep copy):
    orig = {b["id"]: b["section"] for b in SPEC_A["geometry"]["bars"]}
    assert orig[1] == "HEA 200"
    # Nodes / supports preserved:
    assert len(geom["nodes"]) == 4
    assert len(geom["supports"]) == 2
    # 0-based index 5 == candidate_index 6 == (HEA 220, IPE 330)
    assert cand["group_choices"] == {"columns": "HEA 220", "beam": "IPE 330"}
    print("APPLY: apply_to_geometry maps sections correctly and deep-copies")
    print()


def test_roundtrip():
    ds = DesignSpace(SPEC_A)
    d = ds.to_dict()
    ds2 = DesignSpace(d)
    assert ds2.candidate_count() == 12
    assert ds2.analysis_types == ["static"]
    print("ROUNDTRIP: to_dict -> DesignSpace reproduces 12 candidates")
    print()


def main():
    print("=" * 72)
    print("Phase 4 - design-space / candidate generator tests")
    print("=" * 72)
    test_spec_a_counts()
    test_spec_b_counts()
    test_cap_enforcement()
    test_validation_errors()
    test_apply_to_geometry()
    test_roundtrip()
    print("ALL PHASE 4 TESTS PASSED")


if __name__ == "__main__":
    main()
