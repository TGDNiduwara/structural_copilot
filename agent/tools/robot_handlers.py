"""Robot model-building tool handlers (nodes, bars, supports, loads, solve,
materials, panels/solids, modal).

[FIX 06] Extracted verbatim from agent/tool_registry.py - dispatch binds
these functions onto ToolExecutor as `_tool_*` methods, so the public API
and behaviour are unchanged.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agent.tools._shared import GENERATED_DIR, ToolExecutionError, safe_output_path
from tools.diagram_tool import plot_structure_wireframe
from tools.robot_tool import RobotBridge


def tool_new_2d_frame(self) -> dict:
    self._ensure_robot()
    self.robot.new_2d_frame()
    return {"status": "ok", "message": "New 2D frame project created."}


def tool_new_3d_frame(self) -> dict:
    self._ensure_robot()
    self.robot.new_3d_frame()
    return {"status": "ok", "message": "New 3D frame project created."}


def tool_create_node(self, node_id: int, x: float, z: float, y: float = 0.0) -> dict:
    self._ensure_robot()
    self.robot.create_node(node_id, x, y, z)
    return {"status": "ok", "node_id": node_id, "x": x, "y": y, "z": z}


def tool_create_bar(self, bar_id: int, start_node: int, end_node: int, section_name: str) -> dict:
    self._ensure_robot()
    self.robot.create_bar(bar_id, start_node, end_node, section_name)
    return {"status": "ok", "bar_id": bar_id, "section": section_name}


def tool_set_support(
    self, node_id: int, support_type: str = "fixed", spring_stiffness: dict = None
) -> dict:
    self._ensure_robot()
    if support_type == "spring" and not spring_stiffness:
        raise ToolExecutionError(
            "support_type='spring' requires spring_stiffness, e.g. "
            "{'UZ': 100000.0} (UX/UY/UZ in kN/m, RX/RY/RZ in kNm/rad)."
        )
    self.robot.set_support(node_id, support_type, spring_stiffness=spring_stiffness)
    return {
        "status": "ok",
        "node_id": node_id,
        "support_type": support_type,
        "spring_stiffness": spring_stiffness,
    }


def tool_create_load_case(
    self, case_id: int, case_name: str = "Dead Load", nature: str = "permanent"
) -> dict:
    self._ensure_robot()
    nature_int = 0 if nature == "permanent" else 1
    self.robot.create_load_case(case_id, case_name, nature=nature_int)
    return {"status": "ok", "case_id": case_id, "case_name": case_name}


def tool_apply_bar_load(
    self, bar_id: int, case_id: int, value_kn_m: float, direction: str = "Z"
) -> dict:
    self._ensure_robot()
    r = self.robot.apply_bar_load(bar_id, case_id, value_kn_m, direction)
    out = {"status": "ok", "bar_id": bar_id, "case_id": case_id, "value_kn_m": value_kn_m}
    # [LIVE-FIX 2026-08-23] the bridge may have substituted an exact
    # nodal-lumped equivalent (coincident-node models) - surface the
    # method + warning so the caller SEES it instead of assuming a
    # uniform record was written.
    if isinstance(r, dict):
        out.update({k: v for k, v in r.items() if k != "status"})
    return out


def tool_apply_nodal_load(
    self, node_id: int, case_id: int, fx_kn: float = 0.0, fz_kn: float = 0.0, my_knm: float = 0.0
) -> dict:
    self._ensure_robot()
    self.robot.apply_nodal_load(node_id, case_id, fx_kn, fz_kn, my_knm)
    return {"status": "ok", "node_id": node_id, "case_id": case_id}


def tool_solve(self, timeout_s: int = 120) -> dict:
    self._ensure_robot()
    self.robot.solve(timeout_s=timeout_s)
    out = {"status": "ok", "message": "Solver run completed successfully."}
    warn = getattr(self.robot, "_last_instability_warning", None)
    if warn:
        # [INSTABILITY] NEVER silent: force the LLM/user to see that the
        # solver reported a suspected mechanism and the solve continued.
        out["status"] = "ok_with_warning"
        out["warning"] = (
            "Robot reported an INSTABILITY during the solve and the "
            f"dialog was auto-answered 'Yes' (continue): {warn!r}. "
            "Results may only be valid for the stable planes. Run "
            "check_model_stability to confirm the model is not a "
            "mechanism, and fix supports/geometry if it is."
        )
    return out


def tool_get_utilization_ratios(
    self,
    case_id: int = 1,
    bar_ids: list = None,
    divisions: int = 5,
) -> dict:
    self._ensure_robot()
    df = self.robot.get_utilization_ratios(case_id=case_id, bar_ids=bar_ids, divisions=divisions)
    self.utilization_df = df
    summary: dict[str, Any] = {  # [FIX 09] mixed int/float values
        "bars": len(df),
        "pass": int((df.get("Status") == "PASS").sum()) if not df.empty else 0,
        "fail": int((df.get("Status") == "FAIL").sum()) if not df.empty else 0,
        "not_checkable": int((df.get("Status") == "NOT_CHECKABLE").sum()) if not df.empty else 0,
    }
    try:
        u = pd.to_numeric(df["Utilization"], errors="coerce")
        if u.notna().any():
            summary["max_utilization"] = round(float(u.max()), 4)
    except Exception:
        pass
    return {
        "status": "ok",
        "summary": summary,
        "preview": df.head(15).to_dict(orient="records"),
        "note": "Analytical elastic check (RobotOM v27 has no code-check "
        "server — verified). Custom materials need fy via "
        "set_material(..., fy_mpa=...) to be checkable.",
    }


def tool_define_combination(
    self,
    name: str,
    case_factors: dict,
    combination_type: str = "ULS",
) -> dict:
    self._ensure_robot()
    result = self.robot.define_combination(
        name=name,
        case_factors={int(k): float(v) for k, v in case_factors.items()},
        combination_type=combination_type,
    )
    return {"status": "ok", **result}


def tool_list_combinations(self) -> dict:
    self._ensure_robot()
    combos = self.robot.list_combinations()
    return {"status": "ok", "count": len(combos), "combinations": combos}


def tool_solve_combination(self, name: str = None) -> dict:
    self._ensure_robot()
    result = self.robot.solve_combination(name=name)
    return {"status": "ok", **result}


def tool_get_governing_combination(
    self,
    bar_id: int,
    component: str = "MY",
    divisions: int = 5,
) -> dict:
    self._ensure_robot()
    result = self.robot.get_governing_combination(
        bar_id=bar_id, component=component, divisions=divisions
    )
    return {"status": "ok", **result}


def tool_apply_self_weight(self, case_id: int, density: float = 7850.0) -> dict:
    """Applies every bar's self-weight (global -Z) into the case."""
    self._ensure_robot()
    summary = self.robot.apply_self_weight(int(case_id), density=float(density))
    return {"status": "ok", **summary}


def tool_preview_structure_geometry(
    self,
    file_name: str = "structure_geometry.png",
) -> dict:
    """Renders a wireframe of the in-memory geometry (no Robot COM)."""
    geometry = self.robot.get_model_geometry()
    if not geometry.get("nodes"):
        raise ToolExecutionError(
            "No geometry to preview yet - build or load a model first "
            "(create_structure_from_spec / create_node / create_bar)."
        )
    try:
        path = safe_output_path(str(file_name), GENERATED_DIR)
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc
    if not path.lower().endswith(".png"):
        path += ".png"
    plot_structure_wireframe(geometry["nodes"], geometry["bars"], path)
    return {
        "status": "ok",
        "file_path": path,
        "project": geometry.get("project"),
        "nodes": len(geometry["nodes"]),
        "bars": len(geometry["bars"]),
    }


def tool_check_model_stability(self) -> dict:
    """The mechanism pre-solve check on the current model."""
    self._ensure_robot()
    r = self.robot.validate_stability()
    return {"status": "ok", **r}


def tool_robot_session_status(self) -> dict:
    """[DIAG] Authoritative Robot session picture (pid, attach/launch,
    seat owner, live robot.exe processes). Does NOT require an existing
    connection - it reports from the seat registry and tasklist even
    when this process is not connected, so it is safe to call at any
    time (including before the first connect)."""
    from tools.robot_seat import seat_status

    summary = {
        "connected": bool(self.robot._connected),
        "connected_pid": self.robot.connected_pid,
    }
    try:
        status = self.robot.robot_session_status()
    except Exception as exc:  # noqa: BLE001
        status = {"error": str(exc), "seat": seat_status()}
        status["summary"] = f"robot_session_status failed: {exc}"
    summary["detail"] = status
    summary["status"] = "ok"
    return summary


def tool_generate_code_combinations(
    self,
    combination_set: str = "ULS_SLS_basic",
) -> dict:
    """EN 1990 combination set from the currently defined simple cases
    (manual define_combination untouched - this is a convenience layer)."""
    self._ensure_robot()
    cases = []
    for num, obj in self.robot._iter_all_cases():
        try:
            if self.robot._as_combination(obj) is not None:
                continue  # combinations are not simple cases
            nat = int(obj.Nature)
        except Exception:  # noqa: BLE001
            continue
        nature = next((k for k, v in RobotBridge._NATURE_MAP.items() if v == nat), None)
        if nature is None:
            continue
        cases.append((int(num), nature))
    if not cases:
        raise ToolExecutionError(
            "generate_code_combinations needs at least one simple load "
            "case with nature permanent/imposed (create_load_case "
            "first)."
        )
    try:
        plans = RobotBridge.eurocode_combination_factors(cases, combination_set)
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc
    created = []
    for plan in plans:
        res = self.robot.define_combination(
            plan["name"], plan["case_factors"], plan["combination_type"]
        )
        created.append(
            {
                "name": plan["name"],
                "case_factors": plan["case_factors"],
                "combination_type": plan["combination_type"],
                "result": res,
            }
        )
    return {
        "status": "ok",
        "combination_set": combination_set,
        "count": len(created),
        "created": created,
    }


def tool_clear_structure(self, project_type: str = "3D") -> dict:
    self._ensure_robot()  # [WP1 fix]
    self.robot.clear_structure(project_type)
    self._log_activity(
        f"🗑️ clear_structure called (project_type={project_type}, "
        f"PID {self.robot.pid}) - LLM-requested model reset"
    )
    return {
        "status": "ok",
        "project_type": project_type,
        "message": f"Cleared; blank {project_type} model created.",
    }


def tool_modify_bar_section(self, bar_id: int, section_name: str) -> dict:
    self._ensure_robot()
    message = self.robot.modify_bar_section(bar_id, section_name)
    return {"status": "ok", "message": message, "bar_id": bar_id, "section": section_name}


def tool_modify_support(self, node_id: int, support_type: str) -> dict:
    self._ensure_robot()
    message = self.robot.modify_support(node_id, support_type)
    return {"status": "ok", "message": message, "node_id": node_id, "support_type": support_type}


def tool_modify_bar_release(self, bar_id: int, **release_flags) -> dict:
    self._ensure_robot()
    message = self.robot.modify_bar_release(bar_id, **release_flags)
    return {"status": "ok", "message": message, "bar_id": bar_id}


def tool_delete_bar(self, bar_id: int) -> dict:
    self._ensure_robot()
    message = self.robot.delete_bar(bar_id)
    return {"status": "ok", "message": message, "bar_id": bar_id}


def tool_delete_node(self, node_id: int) -> dict:
    self._ensure_robot()
    message = self.robot.delete_node(node_id)
    return {"status": "ok", "message": message, "node_id": node_id}


def tool_save_project(self, file_path: str) -> dict:
    self._ensure_robot()
    message = self.robot.save_project(file_path)
    return {"status": "ok", "message": message}


def tool_set_material(
    self,
    material_name: str = "STEEL",
    e_mpa: float | None = None,
    nu: float | None = None,
    apply_to_bars: bool = True,
) -> dict:
    self._ensure_robot()
    return {
        "status": "ok",
        **self.robot.set_material(
            material_name=material_name, e_mpa=e_mpa, nu=nu, apply_to_bars=apply_to_bars
        ),
    }


def tool_create_panel(
    self,
    panel_id: int,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    width: float = 4.0,
    height: float = 3.0,
    normal: str = "Y",
    divisions_x: int = 4,
    divisions_z: int = 4,
    section: str = None,
    diagonals: bool = False,
) -> dict:
    self._ensure_robot()
    return {
        "status": "ok",
        **self.robot.create_panel(
            panel_id=panel_id,
            x=x,
            y=y,
            z=z,
            width=width,
            height=height,
            normal=normal,
            divisions_x=divisions_x,
            divisions_z=divisions_z,
            section=section,
            diagonals=diagonals,
        ),
    }


def tool_set_panel_thickness(
    self,
    panel_id: int,
    thickness_m: float,
) -> dict:
    self._ensure_robot()
    return {
        "status": "ok",
        **self.robot.set_panel_thickness(panel_id=panel_id, thickness_m=thickness_m),
    }


def tool_apply_panel_pressure(
    self,
    panel_id: int,
    case_id: int = 1,
    pressure_kpa: float = -1.0,
) -> dict:
    self._ensure_robot()
    return {
        "status": "ok",
        **self.robot.apply_panel_pressure(
            panel_id=panel_id, case_id=case_id, pressure_kpa=pressure_kpa
        ),
    }


def tool_create_solid(
    self,
    solid_id: int,
    node_ids: list[int],
    face_groups: list[list[int]],
) -> dict:
    self._ensure_robot()
    return {
        "status": "ok",
        **self.robot.create_solid(solid_id=solid_id, node_ids=node_ids, face_groups=face_groups),
    }


def tool_create_solid_box(
    self,
    solid_id: int,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
    origin_z: float = 0.0,
    size_x: float = 1.0,
    size_y: float = 1.0,
    size_z: float = 1.0,
) -> dict:
    self._ensure_robot()
    return {
        "status": "ok",
        **self.robot.create_solid_box(
            solid_id=solid_id,
            origin_x=origin_x,
            origin_y=origin_y,
            origin_z=origin_z,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
        ),
    }


def tool_solve_modal(
    self,
    case_id: int = 1,
    n_modes: int = 10,
    timeout_s: int = 150,
) -> dict:
    self._ensure_robot()
    return {
        "status": "ok",
        **self.robot.solve_modal(case_id=case_id, n_modes=n_modes, timeout_s=timeout_s),
    }


def tool_export_modal_frequencies(
    self,
    case_id: int = 1,
    n_modes: int = 10,
) -> dict:
    self._ensure_robot()
    df = self.robot.export_modal_frequencies(case_id=case_id, n_modes=n_modes)
    self.modal_frequencies_df = df
    if df.empty:
        return {
            "status": "ok",
            "rows": 0,
            "note": "No modal results exist yet — the RobotOM modal "
            "solver does not complete programmatically in "
            "this build. Run modal analysis in the Robot GUI "
            "and retry, or solve_modal to see the honest "
            "status.",
        }
    return {"status": "ok", "rows": len(df), "preview": df.head(10).to_dict(orient="records")}


def tool_export_modal_mode_shapes(
    self,
    case_id: int = 1,
    mode_num: int = 1,
) -> dict:
    self._ensure_robot()
    df = self.robot.export_modal_mode_shapes(case_id=case_id, mode_num=mode_num)
    return {"status": "ok", "rows": len(df), "preview": df.head(10).to_dict(orient="records")}
