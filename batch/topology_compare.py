"""
batch/topology_compare.py
=========================
[CHAT TOPOLOGY COMPARE] Sizes several named topology variants under the
SAME load spec through the existing optimizer machinery and ranks them by
lightest passing design — so a multi-topology comparison is one call
instead of manually repeating the bridge workflow per candidate.

compare_topologies() orchestrates ONLY existing pieces (no new optimizer):
  * the pure static template spec builders (RobotBridge.truss_spec /
    arch_truss_spec / braced_frame_spec / grid_frame_spec) for geometry;
  * DesignSpace + run_batch (grid) — or run_surrogate_search with its
    automatic grid fallback when a variant's grid would be large — for
    sizing;
  * pareto.compute_pareto_frontier for each variant's best design.
Every variant is sized in its OWN run_id (a fresh blank model per
variant — run_batch's own session satisfies the "build fresh / clear
between variants" semantics). runner.py's existing functions are
untouched.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List, Optional

from batch.design_space import DesignSpace
from batch.pareto import compute_pareto_frontier
from batch.runner import run_batch
from batch.storage import Storage
from batch.surrogate_search import run_surrogate_search, should_use_grid
from tools.robot_tool import RobotBridge

logger = logging.getLogger("structural_copilot.batch.topology_compare")

#: generator name -> pure static spec builder (no Robot COM needed).
SPEC_GENERATORS = {
    "create_truss": RobotBridge.truss_spec,
    "create_arch_truss": RobotBridge.arch_truss_spec,
    "create_braced_frame": RobotBridge.braced_frame_spec,
    "create_rectangular_grid_frame": RobotBridge.grid_frame_spec,
    "truss_spec": RobotBridge.truss_spec,
    "arch_truss_spec": RobotBridge.arch_truss_spec,
    "braced_frame_spec": RobotBridge.braced_frame_spec,
    "grid_frame_spec": RobotBridge.grid_frame_spec,
}

#: Default candidate_sections per bar orientation (subsets of the nominal
#: catalog series so each variant's grid stays small).
DEFAULT_SIZING = {
    "column": ["HEA 160", "HEA 200", "HEA 240"],
    "beam": ["IPE 270", "IPE 300", "IPE 330"],
    "brace": ["L 80", "L 100"],
}

_DEFAULT_OBJECTIVE = {
    "minimize": "weight",
    "constraint": "max_utilization <= 1.0 AND buckling_pass == True",
}


def _group_bars_by_orientation(geometry: Dict[str, Any]) -> Dict[str, List[int]]:
    """Columns = vertical bars (dx~dy~0), beams = horizontal bars (span in
    the x/y plane, z unchanged), everything else = brace."""
    nodes = {
        int(n["id"]): (float(n.get("x", 0.0)), float(n.get("y", 0.0)),
                       float(n.get("z", 0.0)))
        for n in geometry.get("nodes", [])
    }
    groups: Dict[str, List[int]] = {"column": [], "beam": [], "brace": []}
    for b in geometry.get("bars", []):
        n1, n2 = int(b["n1"]), int(b["n2"])
        if n1 not in nodes or n2 not in nodes:
            continue
        p1, p2 = nodes[n1], nodes[n2]
        horiz = abs(p2[0] - p1[0]) + abs(p2[1] - p1[1])
        dz = abs(p2[2] - p1[2])
        if horiz < 1e-9:
            groups["column"].append(int(b["id"]))
        elif dz < 1e-9:
            groups["beam"].append(int(b["id"]))
        else:
            groups["brace"].append(int(b["id"]))
    return groups


def _variant_design_space(base_geometry, load_spec, objective, sizing):
    """One DesignSpace per variant: same geometry + same load spec, with
    variable_groups derived from bar orientation."""
    geom = copy.deepcopy(base_geometry)
    geom["cases"] = list(load_spec.get("cases") or [])
    geom["loads"] = list(load_spec.get("loads") or [])
    groups = _group_bars_by_orientation(geom)
    variable_groups = []
    for kind in ("column", "beam", "brace"):
        bar_ids = groups[kind]
        if not bar_ids:
            continue
        sections = (sizing or {}).get(kind) or DEFAULT_SIZING[kind]
        variable_groups.append({
            "group_name": kind, "bar_ids": bar_ids,
            "candidate_sections": list(sections),
        })
    if not variable_groups:
        raise ValueError("variant has no bars to size")
    cases = load_spec.get("cases") or []
    return DesignSpace({
        "geometry": geom,
        "variable_groups": variable_groups,
        "load_cases": load_spec.get("load_cases")
                      or [c for c in cases if isinstance(c, dict)],
        "combinations": load_spec.get("combinations") or [],
        "analysis_types": ["static"],
        "objective": objective or dict(_DEFAULT_OBJECTIVE),
    })


def compare_topologies(
    variants: List[Dict[str, Any]],
    load_spec: Dict[str, Any],
    objective: Optional[Dict[str, Any]] = None,
    sizing: Optional[Dict[str, List[str]]] = None,
    budget: Optional[int] = None,
    db_path: Optional[str] = None,
    log_path: Optional[str] = None,
    session_factory=None,
) -> Dict[str, Any]:
    """Sizes each variant with the SAME load spec and ranks by lightest
    passing design.

    ``variants``: [{"name", "generator", "generator_args"}], generator in
    SPEC_GENERATORS (e.g. "create_truss"). ``load_spec``: a dict with
    "cases" and "loads" (the geometry spec keys). ``objective``: optional
    DesignSpace objective dict. ``sizing``: optional {orientation:
    candidate_sections} override.

    Returns {"status", "variants": [{name, run_id, grid_candidates,
    weight_kg, max_utilization, sections}]} sorted by weight ascending
    (all-fail variants at the end with weight_kg None).
    """
    ranked: List[Dict[str, Any]] = []
    for i, variant in enumerate(variants):
        name = str(variant.get("name") or f"variant_{i + 1}")
        gen = SPEC_GENERATORS.get(str(variant.get("generator") or ""))
        if gen is None:
            raise ValueError(
                f"unknown generator '{variant.get('generator')}'; options: "
                f"{sorted(SPEC_GENERATORS)}")
        base = gen(**dict(variant.get("generator_args") or {}))
        ds = _variant_design_space(base, load_spec, objective, sizing)

        if should_use_grid(ds, int(budget or 300))[0]:
            summary = run_batch(ds, db_path=db_path, log_path=log_path,
                                session_factory=session_factory)
            run_id = summary["run_id"]
        else:
            s = run_surrogate_search(
                ds, budget=int(budget or 200), db_path=db_path,
                log_path=log_path, session_factory=session_factory)
            run_id = s["run_id"]

        st = Storage(db_path=db_path)
        try:
            frontier = compute_pareto_frontier(st.get_all_results(run_id))
        finally:
            st.close()

        entry: Dict[str, Any] = {"name": name, "run_id": run_id,
                                 "grid_candidates": ds.candidate_count()}
        if frontier.empty:
            entry.update({"weight_kg": None, "max_utilization": None,
                          "sections": None})
        else:
            best = frontier.sort_values("weight_kg").iloc[0]
            sections = {}
            try:
                dv = json.loads(best.get("design_vars_json") or "{}")
                sections = dv.get("group_choices") or {}
            except (TypeError, ValueError):
                sections = {}
            entry.update({
                "weight_kg": round(float(best["weight_kg"]), 2),
                "max_utilization": round(float(best["max_utilization"]), 4),
                "sections": sections,
            })
        ranked.append(entry)
        logger.info("Topology '%s': best weight %s kg (run %s).",
                    name, entry.get("weight_kg"), run_id)

    ranked.sort(key=lambda e: (e["weight_kg"] is None,
                               e["weight_kg"] if e["weight_kg"] is not None
                               else float("inf")))
    return {"status": "ok", "variants": ranked,
            "objective": objective or dict(_DEFAULT_OBJECTIVE)}

