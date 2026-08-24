"""Word / PowerPoint report tool handlers.

[FIX 06] Extracted verbatim from agent/tool_registry.py - dispatch binds
these functions onto ToolExecutor as `_tool_*` methods, so the public API
and behaviour are unchanged.
"""

from __future__ import annotations

from agent.tools._shared import GENERATED_DIR, ToolExecutionError, safe_output_path


def tool_generate_word_report(
    self,
    file_name: str,
    summary_text: str,
    project_title: str = "Untitled Project",
    engineer_name: str = "Structural Multi-App Agent",
    include_diagrams: bool = True,
) -> dict:
    if self.member_forces_df.empty and self.reactions_df.empty:
        raise ToolExecutionError(
            "No result data cached. Run export_member_forces and export_reactions "
            "before generate_word_report."
        )

    diagram_paths = list(self.diagram_paths.values()) if include_diagrams else []
    if include_diagrams and not diagram_paths:
        raise ToolExecutionError(
            "include_diagrams was True but no diagrams have been generated yet. "
            "Call generate_diagrams first."
        )

    # [FIX C2] Validate output path
    path = safe_output_path(file_name, GENERATED_DIR)
    self.word.generate_calculation_report(
        file_path=path,
        project_title=project_title,
        engineer_name=engineer_name,
        summary_text=summary_text,
        member_df=self.member_forces_df,
        reactions_df=self.reactions_df,
        diagram_paths=diagram_paths,
    )
    self.generated_files[file_name] = path
    self._save_robot_project_artifact(file_name)
    return {"status": "ok", "file_path": path}


def tool_generate_powerpoint_report(
    self,
    file_name: str,
    summary_text: str,
    project_title: str = "Untitled Project",
    engineer_name: str = "Structural Multi-App Agent",
    include_diagrams: bool = True,
) -> dict:
    if self.member_forces_df.empty and self.reactions_df.empty:
        raise ToolExecutionError(
            "No result data cached. Run export_member_forces and export_reactions "
            "before generate_powerpoint_report."
        )

    diagram_paths = list(self.diagram_paths.values()) if include_diagrams else []
    if include_diagrams and not diagram_paths:
        raise ToolExecutionError(
            "include_diagrams was True but no diagrams have been generated yet. "
            "Call generate_diagrams first."
        )

    # [FIX C2] Validate output path
    path = safe_output_path(file_name, GENERATED_DIR)
    self.pptx.generate_presentation(
        file_path=path,
        project_title=project_title,
        engineer_name=engineer_name,
        summary_text=summary_text,
        member_df=self.member_forces_df,
        reactions_df=self.reactions_df,
        diagram_paths=diagram_paths,
    )
    self.generated_files[file_name] = path
    self._save_robot_project_artifact(file_name)
    return {"status": "ok", "file_path": path}
