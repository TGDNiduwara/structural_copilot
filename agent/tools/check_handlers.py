"""Engineering check tool handlers (proportions, bracing, LTB, connections, Eurocode, topologies).

[FIX 06] Extracted verbatim from agent/tool_registry.py - dispatch binds
these functions onto ToolExecutor as `_tool_*` methods, so the public API
and behaviour are unchanged.
"""

from __future__ import annotations

from agent.tools._shared import ToolExecutionError
from tools.eurocode_members import check_eurocode_members
from tools.ltb_check import check_lateral_torsional_buckling
from tools.section_sizing import check_section_proportions


def tool_compare_topologies(
    self, variants: list, load_spec: dict, objective: dict = None, budget: int = None
) -> dict:
    """Sizes several topology variants under the same load spec and
    ranks them by lightest passing design (batch/topology_compare)."""
    from batch.topology_compare import compare_topologies

    try:
        result = compare_topologies(
            variants, load_spec, objective=objective, budget=budget, db_path=self._batch_db_path
        )
    except Exception as exc:  # noqa: BLE001
        raise ToolExecutionError(f"compare_topologies failed: {exc}") from exc
    return {"status": "ok", **result}


def tool_check_section_proportions(self, spec: dict) -> dict:
    # [Part B] Pure offline check — no Robot connection required.
    warnings = check_section_proportions(spec or {})
    return {"status": "ok", "warning_count": len(warnings), "section_proportion_warnings": warnings}


def tool_set_bracing(
    self,
    bar_id: int,
    lcr_y: float | None = None,
    lcr_z: float | None = None,
    lcr_lt: float | None = None,
    brace_points: list | None = None,
) -> dict:
    # [EUROCODE Phase A] Explicit unbraced-length input layer.
    self._ensure_robot()
    resolved = self.robot.set_bar_bracing(
        bar_id=bar_id, lcr_y=lcr_y, lcr_z=lcr_z, lcr_lt=lcr_lt, brace_points=brace_points
    )
    return {"status": "ok", "bracing": resolved}


def tool_get_bracing(self, bar_id: int | None = None) -> dict:
    # [EUROCODE Phase A] Read back resolved bracing data (defaults
    # tagged, so the engineer sees what was assumed).
    self._ensure_robot()
    return {"status": "ok", "bracing": self.robot.get_bar_bracing(bar_id=bar_id)}


def tool_check_lateral_torsional_buckling(
    self,
    case_id: int = 1,
    bar_ids: list | None = None,
) -> dict:
    # [EUROCODE Phase C] §6.3.2.2 LTB + §6.3.3 Annex B interaction.
    self._ensure_robot()
    result = check_lateral_torsional_buckling(self.robot, case_id, bar_ids)
    return {"status": "ok", **result}


def tool_define_connection(
    self,
    bar_id: int,
    joint_end: str = "end",
    connection_type: str = "fin_plate",
    bolt_grade: str = "8.8",
    bolt_diameter: float = 20,
    bolt_rows: int = 2,
    pitch_mm: float = 60,
    edge_dist_mm: float = 30,
    end_dist_mm: float = 30,
    plate_thickness: float = 10,
    plate_grade: str = "S275",
    weld_leg_mm: float | None = None,
) -> dict:
    # [EUROCODE Phase D] Simple-shear connection input layer.
    self._ensure_robot()
    result = self.robot.define_connection(
        bar_id=bar_id,
        joint_end=joint_end,
        connection_type=connection_type,
        bolt_grade=bolt_grade,
        bolt_diameter=bolt_diameter,
        bolt_rows=bolt_rows,
        pitch_mm=pitch_mm,
        edge_dist_mm=edge_dist_mm,
        end_dist_mm=end_dist_mm,
        plate_thickness=plate_thickness,
        plate_grade=plate_grade,
        weld_leg_mm=weld_leg_mm,
    )
    return {"status": "ok", **result}


def tool_check_connection_capacity(
    self,
    bar_id: int,
    joint_end: str = "end",
    case_id: int = 1,
) -> dict:
    # [EUROCODE Phase D] EN 1993-1-8 simple shear connection check.
    self._ensure_robot()
    result = self.robot.check_connection_capacity(
        bar_id=bar_id, joint_end=joint_end, case_id=case_id
    )
    return {"status": "ok", **result}


def tool_check_eurocode_members(
    self,
    case_id: int = 1,
    bar_ids: list | None = None,
) -> dict:
    # [EUROCODE Phase E] Worst-governing across all four checks.
    self._ensure_robot()
    result = check_eurocode_members(self.robot, case_id, bar_ids)
    # Cache the worst per-bar verdicts so store_result can include the
    # LTB / connection status in its one-line snapshot (Phase E.3).
    bars = result.get("bars") or []
    worst_ltb = (
        "FAIL"
        if any(b.get("checks", {}).get("ltb", {}).get("status") == "FAIL" for b in bars)
        else (
            "NOT_CHECKABLE"
            if any(
                b.get("checks", {}).get("ltb", {}).get("status") == "NOT_CHECKABLE" for b in bars
            )
            else "PASS"
        )
    )
    worst_conn = (
        "FAIL"
        if any(b.get("checks", {}).get("connection", {}).get("status") == "FAIL" for b in bars)
        else (
            "NOT_CHECKABLE"
            if any(
                b.get("checks", {}).get("connection", {}).get("status") == "NOT_CHECKABLE"
                for b in bars
            )
            else "PASS"
        )
    )
    self._eurocode_member_summary = {
        "ltb_status": worst_ltb,
        "connection_status": worst_conn,
    }
    return {"status": "ok", **result}
