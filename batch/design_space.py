"""
batch/design_space.py
=====================
Design-space schema + full grid-search candidate generator (Phase 4).

A DesignSpace is defined from a plain dict spec (the same shape the LLM
emits in Phase 7) describing a FIXED geometry plus one or more variable
groups whose candidate_sections are swept exhaustively.

IMPORTANT - deliberate scope boundary
--------------------------------------
generate_candidates() ONLY produces DESIGN VARIABLES for each candidate
(i.e. which section goes on which bar). It does NOT build or validate the
resulting structure. Mechanism / instability screening
(HeadlessSession.validate_stability(), built during the dialog-watcher
work) happens per-candidate at BUILD time in batch/runner.py. Some
section-swap combinations can theoretically still produce an unstable
structure depending on how supports / releases interact with the
goometry, so the runner must not assume every generated candidate is
automatically buildable / solvable just because this module generated
its spec.

Design-space spec shape
-----------------------
{
  "geometry": { ...same shape as HeadlessSession.build_from_spec spec... },
  "variable_groups": [
     {"group_name": "columns", "bar_ids": [1, 3],
      "candidate_sections": ["HEA200", "HEA220", "HEA240", "HEB200"]},
     {"group_name": "beam", "bar_ids": [2],
      "candidate_sections": ["IPE270", "IPE300", "IPE330"]}
  ],
  "load_cases": [...],         # optional, carried through unchanged
  "combinations": [...],       # optional, carried through unchanged
  "analysis_types": ["static"], # optional, default ["static"]
  "objective": {"minimize": "weight",
                 "constraint": "max_utilization <= 1.0 AND buckling_pass == True"}
}

generate_candidates() is a FULL GRID SEARCH: every combination of every
group's candidate_sections. The total is the cartesian product of the
group option counts; it is capped (default 50,000) and exceeding the cap
raises DesignSpaceError so the caller sees the combinatorics instead of
silently launching a multi-day unattended run.

Only grid search for now - the genetic-algorithm / pymoo path is
DELIBERATELY not implemented (Phase 6 of the build track) and only
becomes relevant if group counts make grid search impractical in practice.
"""

from __future__ import annotations

import copy
import itertools
import logging
import math
from typing import Any

logger = logging.getLogger("structural_copilot.batch.design_space")

#: Hard cap on generated candidate count (Phase 4 requirement).
DEFAULT_MAX_CANDIDATES = 50_000


class DesignSpaceError(ValueError):
    """Raised for an invalid design-space spec or an over-limit grid."""


class DesignSpace:
    """Validated design space whose candidates are a full grid search."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = dict(spec or {})

        geometry = self._spec.get("geometry")
        if not isinstance(geometry, dict) or not geometry:
            raise DesignSpaceError("DesignSpace requires a non-empty 'geometry' dict")
        self.geometry = geometry

        groups = self._spec.get("variable_groups")
        if not isinstance(groups, list) or not groups:
            raise DesignSpaceError("DesignSpace requires a non-empty 'variable_groups' list")

        self.objective = self._spec.get("objective") or {
            "minimize": "weight",
            "constraint": "max_utilization <= 1.0 AND buckling_pass == True",
        }
        self.load_cases = list(self._spec.get("load_cases") or [])
        self.combinations = list(self._spec.get("combinations") or [])
        analysis = self._spec.get("analysis_types")
        if not analysis:
            analysis = ["static"]
        self.analysis_types = [str(a).lower() for a in analysis]
        for a in self.analysis_types:
            if a not in ("static", "modal"):
                raise DesignSpaceError(
                    f"Unsupported analysis_type '{a}' (supported: static, modal)"
                )
        self.max_candidates = int(self._spec.get("max_candidates", DEFAULT_MAX_CANDIDATES))

        bar_ids_in_geometry = {int(b["id"]) for b in (geometry.get("bars") or [])}
        seen_groups: set = set()
        seen_bar_ids: set = set()
        self.variable_groups: list[dict[str, Any]] = []

        for g in groups:
            name = str(g.get("group_name") or "")
            if not name:
                raise DesignSpaceError("Each variable_group requires a 'group_name'")
            if name in seen_groups:
                raise DesignSpaceError(f"Duplicate variable group name '{name}'")

            bar_ids = [int(b) for b in (g.get("bar_ids") or [])]
            if not bar_ids:
                raise DesignSpaceError(f"Group '{name}' has empty bar_ids")
            overlap = seen_bar_ids.intersection(bar_ids)
            if overlap:
                raise DesignSpaceError(
                    f"Group '{name}' bar_ids overlap another group: {sorted(overlap)}"
                )

            sections = [str(s) for s in (g.get("candidate_sections") or [])]
            if not sections:
                raise DesignSpaceError(f"Group '{name}' has empty candidate_sections")
            sections = list(dict.fromkeys(sections))  # dedupe, keep order

            missing = sorted(set(bar_ids) - bar_ids_in_geometry)
            if missing:
                raise DesignSpaceError(
                    f"Group '{name}' references bar_ids not present in geometry.bars: {missing}"
                )

            self.variable_groups.append(
                {
                    "group_name": name,
                    "bar_ids": bar_ids,
                    "candidate_sections": sections,
                }
            )
            seen_groups.add(name)
            seen_bar_ids.update(bar_ids)

    def candidate_count(self) -> int:
        """Cartesian product of each group's candidate_section count."""
        return math.prod(len(g["candidate_sections"]) for g in self.variable_groups)

    def generate_candidates(self) -> list[dict[str, Any]]:
        """Full grid search over every group's candidate sections.

        Each returned candidate is a dict:
          {
            "candidate_index": 1,
            "group_choices": {"columns": "HEA200", "beam": "IPE270"},
            "sections": {1: "HEA200", 3: "HEA200", 2: "IPE270"},
          }

        This produces DESIGN VARIABLES ONLY - it neither builds nor
        validates the structure (see module docstring). If the grid
        exceeds max_candidates, a DesignSpaceError is raised so the
        caller sees the combinatorics before launching a run.
        """
        total = self.candidate_count()
        if total > self.max_candidates:
            raise DesignSpaceError(
                f"Grid search would generate {total} candidates "
                f"(cap {self.max_candidates}). Reduce candidate_sections "
                "per group or raise max_candidates explicitly."
            )
        if total == 0:
            return []

        option_lists = [g["candidate_sections"] for g in self.variable_groups]
        candidates: list[dict[str, Any]] = []
        for idx, combo in enumerate(itertools.product(*option_lists), start=1):
            group_choices = {g["group_name"]: sec for g, sec in zip(self.variable_groups, combo)}
            section_map: dict[int, str] = {}
            for g, sec in zip(self.variable_groups, combo):
                for bar_id in g["bar_ids"]:
                    section_map[bar_id] = sec
            candidates.append(
                {
                    "candidate_index": idx,
                    "group_choices": group_choices,
                    "sections": section_map,
                }
            )
        return candidates

    def apply_to_geometry(self, design_vars: dict[str, Any]) -> dict[str, Any]:
        """Deep-copy ``geometry`` with each bar's ``section`` overwritten by
        ``design_vars['sections']`` (bar_ids not in the map are untouched).

        The returned dict is what batch/runner.py feeds to
        HeadlessSession.build_from_spec() for one candidate.

        ``design_vars`` may be a full candidate dict (with a "sections" key,
        as returned by generate_candidates) OR a bare {bar_id: section} map
        - both are handled.
        """
        geom = copy.deepcopy(self.geometry)
        if isinstance(design_vars, dict) and "sections" in design_vars:
            sections = design_vars.get("sections") or {}
        else:
            sections = design_vars or {}
        # JSON round-trip (storage) turns int bar-id keys into STRING keys -
        # normalize so `1 in sections` matches `{"1": ...}` as well.
        section_map = {}
        for k, v in (sections or {}).items():
            try:
                section_map[int(k)] = v
            except (TypeError, ValueError):
                section_map[k] = v
        for b in geom.get("bars") or []:
            bar_id = int(b["id"])
            if bar_id in section_map:
                b["section"] = section_map[bar_id]
        return geom

    def describe(self) -> str:
        """One-line-per-group human summary (candidate counts visible)."""
        lines = [
            f"DesignSpace: {self.candidate_count()} candidates",
            f"  analysis_types: {self.analysis_types}",
        ]
        for g in self.variable_groups:
            lines.append(
                f"  {g['group_name']}: bars {g['bar_ids']} in "
                f"{len(g['candidate_sections'])} options "
                f"{g['candidate_sections']}"
            )
        if self.objective:
            lines.append(f"  objective: {self.objective}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Return the normalized design-space spec (for persistence)."""
        return {
            "geometry": self.geometry,
            "variable_groups": self.variable_groups,
            "load_cases": self.load_cases,
            "combinations": self.combinations,
            "analysis_types": self.analysis_types,
            "objective": self.objective,
            "max_candidates": self.max_candidates,
        }
