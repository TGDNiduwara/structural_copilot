"""
batch/test_tool_registry_batch.py
=================================
Offline tests for the LLM batch-optimizer tools added in tool_registry.py:
start_surrogate_search_run / confirm_and_start_surrogate_search_run /
export_best_design (plus the grid-fallback and config-kind guards).

NO Robot COM: these paths never launch Robot (start never runs, export
returns not_ready on a non-completed run, and the confirm guards reject
wrong config kinds before any thread starts).

Run:  python batch/test_tool_registry_batch.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

from agent.tool_registry import (
    ALLOWED_EXTENSIONS,
    TOOL_SCHEMAS,
    ToolExecutionError,
    ToolExecutor,
    _validate_tool_arguments,
)
from batch.storage import Storage

#: 2 candidates -> below the grid-fallback threshold.
SMALL_SPEC = {
    "geometry": {
        "project": "3D",
        "nodes": [{"id": 1, "x": 0, "z": 0}, {"id": 2, "x": 6, "z": 0}],
        "bars": [{"id": 1, "n1": 1, "n2": 2, "section": "IPE 300"}],
        "supports": [{"node": 1, "type": "pinned"}, {"node": 2, "type": "pinned"}],
    },
    "variable_groups": [
        {"group_name": "beam", "bar_ids": [1], "candidate_sections": ["IPE 300", "IPE 360"]},
    ],
}

#: 8 x 8 = 64 candidates -> above min(budget, 60) for any budget: the
#: surrogate search stages instead of recommending the grid.
LARGE_SPEC = {
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
        "supports": [{"node": 1, "type": "pinned"}, {"node": 4, "type": "pinned"}],
    },
    "variable_groups": [
        {
            "group_name": "columns",
            "bar_ids": [1, 3],
            "candidate_sections": [
                "HEA 160",
                "HEA 180",
                "HEA 200",
                "HEA 220",
                "HEA 240",
                "HEB 160",
                "HEB 180",
                "HEB 200",
            ],
        },
        {
            "group_name": "beam",
            "bar_ids": [2],
            "candidate_sections": [
                "IPE 200",
                "IPE 220",
                "IPE 240",
                "IPE 270",
                "IPE 300",
                "IPE 330",
                "IPE 360",
                "IPE 400",
            ],
        },
    ],
}


def test_schemas_and_handlers_registered():
    names = {s["name"] for s in TOOL_SCHEMAS}
    for name in (
        "start_surrogate_search_run",
        "confirm_and_start_surrogate_search_run",
        "export_best_design",
    ):
        assert name in names, f"schema missing: {name}"
    assert ".rtd" in ALLOWED_EXTENSIONS
    ex = ToolExecutor()
    for attr in (
        "_tool_start_surrogate_search_run",
        "_tool_confirm_and_start_surrogate_search_run",
        "_tool_export_best_design",
        "_run_surrogate_worker",
    ):
        assert hasattr(ex, attr), attr
    _validate_tool_arguments(
        "start_surrogate_search_run",
        {"spec": LARGE_SPEC, "budget": 40, "patience": 5, "acquisition": "ehvi", "kappa": 1.5},
    )
    _validate_tool_arguments(
        "confirm_and_start_surrogate_search_run", {"run_config_id": "surr_cfg_1"}
    )
    _validate_tool_arguments(
        "export_best_design", {"run_id": 3, "file_name": "winner.rtd", "frontier_index": 0}
    )
    print("REGISTRY: 3 new schemas + handlers present, args validate")


def test_start_small_grid_recommends_grid():
    ex = ToolExecutor()
    r = ex._tool_start_surrogate_search_run(SMALL_SPEC)
    assert r["status"] == "grid_recommended", r
    assert not ex._optimization_configs, "grid_recommended must not stage a config"
    print("START: small grid -> grid_recommended, nothing staged")


def test_start_large_stages_and_confirm_guard():
    ex = ToolExecutor()
    r = ex._tool_start_surrogate_search_run(LARGE_SPEC, budget=25, patience=3, acquisition="ucb")
    assert r["status"] == "not_started", r
    assert r["run_config_id"].startswith("surr_cfg_"), r
    assert r["candidate_count"] == 64 and r["budget"] == 25, r
    assert r["acquisition"] == "ucb", r
    cfg_id = r["run_config_id"]

    try:
        ex._tool_confirm_and_start_surrogate_search_run("bogus")
        raise AssertionError("bogus config must be rejected")
    except ToolExecutionError as exc:
        assert "not a staged surrogate" in str(exc), exc

    # A GRID config must not be startable as a surrogate run, and vice versa.
    g = ex._tool_start_optimization_run(LARGE_SPEC)
    assert g["status"] == "not_started", g
    try:
        ex._tool_confirm_and_start_surrogate_search_run(g["run_config_id"])
        raise AssertionError("grid config must not start surrogate")
    except ToolExecutionError as exc:
        assert "not a staged surrogate" in str(exc), exc
    try:
        ex._tool_confirm_and_start_optimization_run(cfg_id)
        raise AssertionError("surrogate config must not start a grid run")
    except ToolExecutionError as exc:
        assert "not a staged grid-run config" in str(exc), exc
    print(
        "START+CONFIRM: staged surrogate config; bogus/grid/surrogate cross-start guards all reject"
    )


def test_export_best_design_not_ready_and_missing():
    ex = ToolExecutor()
    tmp = tempfile.mkdtemp(prefix="toolreg_batch_")
    ex._batch_db_path = os.path.join(tmp, "runs.db")
    st = Storage(db_path=ex._batch_db_path)
    run_id = st.create_run({"geometry": {}, "variable_groups": []}, objective="")
    st.mark_run_status(run_id, "running")  # not completed yet
    st.close()

    r = ex._tool_export_best_design(run_id, "winner.rtd")
    assert r["status"] == "not_ready" and r["run_status"] == "running", r

    try:
        ex._tool_export_best_design(99999, "winner.rtd")
        raise AssertionError("missing run must be rejected")
    except ToolExecutionError as exc:
        assert "does not exist" in str(exc), exc
    print("EXPORT: non-completed run -> not_ready (no Robot); missing run raises cleanly")


def main():
    print("=" * 72)
    print("Batch-optimizer tool-registry tests (offline)")
    print("=" * 72)
    test_schemas_and_handlers_registered()
    test_start_small_grid_recommends_grid()
    test_start_large_stages_and_confirm_guard()
    test_export_best_design_not_ready_and_missing()
    print()
    print("ALL TOOL-REGISTRY BATCH TESTS PASSED")


if __name__ == "__main__":
    main()
