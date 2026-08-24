"""Export tool handlers (Excel, diagrams, member forces, results).

[FIX 06] Extracted verbatim from agent/tool_registry.py - dispatch binds
these functions onto ToolExecutor as `_tool_*` methods, so the public API
and behaviour are unchanged.
"""

from __future__ import annotations

import os
import re

from agent.tools._shared import (
    GENERATED_DIR,
    ToolExecutionError,
    _ensure_generated_dir,
    safe_output_path,
)
from tools.section_sizing import available_sections, section_families


def tool_export_structure_spec(self) -> dict:
    """Reverse of build_structure_from_spec: the LIVE model as the
    'geometry' JSON object the optimizer / create_structure_from_spec
    accept."""
    self._ensure_robot()
    spec = self.robot.export_structure_spec()
    return {
        "status": "ok",
        "geometry": spec,
        "counts": {k: len(v) for k, v in spec.items() if isinstance(v, list)},
    }


def tool_list_available_sections(self, family: str = None) -> dict:
    """Catalog-only section names (no Robot solve)."""
    try:
        sections = available_sections(family)
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc
    return {
        "status": "ok",
        "family": family,
        "count": len(sections),
        "families": section_families(),
        "sections": sections,
    }


def tool_export_member_forces(self, case_id: int = 1, divisions: int = 5) -> dict:
    # [FIX M11] Clamp divisions to safe range
    divisions = max(1, min(divisions, 100))
    self._ensure_robot()
    df = self.robot.export_all_member_forces(case_id=case_id, divisions=divisions)
    self.member_forces_df = df
    return {
        "status": "ok",
        "rows": len(df),
        "preview": df.head(10).to_dict(orient="records"),
    }


def tool_export_reactions(self, case_id: int = 1) -> dict:
    self._ensure_robot()
    df = self.robot.export_reactions(case_id=case_id)
    self.reactions_df = df
    return {
        "status": "ok",
        "rows": len(df),
        "preview": df.to_dict(orient="records"),
    }


def tool_export_bill_of_materials(self) -> dict:
    self._ensure_robot()
    df = self.robot.export_bill_of_materials()
    self.boq_df = df
    return {
        "status": "ok",
        "rows": len(df),
        "preview": df.to_dict(orient="records"),
    }


def tool_export_to_excel(self, file_name: str, project_name: str = "Untitled Project") -> dict:
    if self.member_forces_df.empty and self.reactions_df.empty and self.boq_df.empty:
        raise ToolExecutionError(
            "No result data cached. Call export_member_forces, export_reactions, "
            "and export_bill_of_materials before export_to_excel."
        )
    # [FIX C2] Validate output path
    path = safe_output_path(file_name, GENERATED_DIR)
    self.excel.create_structural_workbook(
        file_path=path,
        project_name=project_name,
        member_forces_df=self.member_forces_df,
        reactions_df=self.reactions_df,
        boq_df=self.boq_df,
    )
    self.generated_files[file_name] = path
    self._save_robot_project_artifact(project_name)
    return {"status": "ok", "file_path": path}


def tool_generate_diagrams(self, base_name: str = "diagram") -> dict:
    if self.member_forces_df.empty:
        raise ToolExecutionError(
            "No member force data cached. Call export_member_forces before generate_diagrams."
        )
    # [FIX C2] Validate base_name (used to construct filenames)
    if not re.match(r"^[a-zA-Z0-9_\-]+$", base_name):
        raise ValueError(
            f"Invalid base_name '{base_name}'. Only alphanumeric characters, "
            "hyphens, and underscores are allowed."
        )
    _ensure_generated_dir()
    sfd_path = os.path.join(GENERATED_DIR, f"{base_name}_SFD.png")
    bmd_path = os.path.join(GENERATED_DIR, f"{base_name}_BMD.png")
    self.diagrams.plot_sfd(self.member_forces_df, sfd_path)
    self.diagrams.plot_bmd(self.member_forces_df, bmd_path)
    self.diagram_paths["sfd"] = sfd_path
    self.diagram_paths["bmd"] = bmd_path
    self._save_robot_project_artifact(base_name)
    return {"status": "ok", "sfd_path": sfd_path, "bmd_path": bmd_path}


def tool_export_node_displacements(self, case_id: int = 1) -> dict:
    self._ensure_robot()
    df = self.robot.export_node_displacements(case_id=case_id)
    self.displacements_df = df
    return {"status": "ok", "rows": len(df), "preview": df.head(10).to_dict(orient="records")}


def tool_export_bar_stresses(self, case_id: int = 1, divisions: int = 5) -> dict:
    self._ensure_robot()
    df = self.robot.export_bar_stresses(case_id=case_id, divisions=divisions)
    self.stresses_df = df
    return {"status": "ok", "rows": len(df), "preview": df.head(10).to_dict(orient="records")}


def tool_export_results_to_excel(
    self,
    file_name: str,
    sheets: list[str],
    case_id: int = 1,
) -> dict:
    self._ensure_robot()
    available = {
        "member_forces": self.member_forces_df,
        "reactions": self.reactions_df,
        "displacements": self.displacements_df,
        "stresses": self.stresses_df,
        "boq": self.boq_df,
        "modal": self.modal_frequencies_df,
    }
    # Gather anything requested but not yet cached.
    if "displacements" in sheets and self.displacements_df.empty:
        self.displacements_df = self.robot.export_node_displacements(case_id=case_id)
        available["displacements"] = self.displacements_df
    if "stresses" in sheets and self.stresses_df.empty:
        self.stresses_df = self.robot.export_bar_stresses(case_id=case_id)
        available["stresses"] = self.stresses_df
    if "member_forces" in sheets and self.member_forces_df.empty:
        self.member_forces_df = self.robot.export_all_member_forces(case_id=case_id)
        available["member_forces"] = self.member_forces_df
    if "reactions" in sheets and self.reactions_df.empty:
        self.reactions_df = self.robot.export_reactions(case_id=case_id)
        available["reactions"] = self.reactions_df
    if "modal" in sheets and self.modal_frequencies_df.empty:
        self.modal_frequencies_df = self.robot.export_modal_frequencies(case_id=case_id)
        available["modal"] = self.modal_frequencies_df

    picked = {}
    for s in sheets:
        df = available.get(s)
        if df is not None and not df.empty:
            picked[s.replace("_", " ").title()] = df
        else:
            raise ToolExecutionError(
                f"Requested sheet '{s}' has no data. Ensure the model is "
                "solved and relevant export_* tools have run first."
            )

    # [FIX C2] Validate output path
    path = safe_output_path(file_name, GENERATED_DIR)
    self.excel.build_workbook_from_sheets(file_path=path, sheets=picked)
    self.generated_files[file_name] = path
    self._save_robot_project_artifact(file_name)
    return {"status": "ok", "file_path": path, "sheets": list(picked.keys())}
