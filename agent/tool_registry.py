"""
agent/tool_registry.py
=======================
Declares the LLM-facing tool schema (OpenAI function-calling JSON Schema
format -- also reused/converted for Google AI Studio in llm_providers.py)
and implements the ToolExecutor, which dispatches a validated tool call to
the underlying RobotBridge / ExcelReporter / DiagramGenerator / WordReporter
instances and returns a JSON-serializable result (or a structured error
payload used for autonomous error-reflection retries).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.robot_tool import RobotBridge
from tools.excel_tool import ExcelReporter
from tools.diagram_tool import DiagramGenerator
from tools.word_tool import WordReporter
from tools.pptx_tool import PowerPointReporter
from tools.result_store import ResultStore
from tools.custom_tools import (
    CustomToolRegistry,
    run_sandboxed,
    ScriptRejected,
)
# [P7] Batch optimizer bookend tools. These dispatch into batch/ (runner,
# design_space, storage, pareto). batch/ is deliberately isolated: it never
# imports agent/tool_registry.py, so adding these imports creates no new
# coupling direction (tool_registry -> batch only).
from batch.design_space import DesignSpace, DesignSpaceError
from batch.runner import run_batch
from batch.storage import Storage
from batch.pareto import pareto_summary

logger = logging.getLogger("structural_copilot.tool_registry")
logger.setLevel(logging.INFO)

# [FIX H5] Use module-relative path instead of os.getcwd()
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)
GENERATED_DIR = os.environ.get(
    "STRUCTURAL_AGENT_GENERATED_DIR",
    os.path.join(_PROJECT_ROOT, "generated"),
)

# [FIX M9] Defer directory creation to first use instead of import time
_generated_dir_created = False


def _ensure_generated_dir():
    """Lazily creates the generated output directory on first use."""
    global _generated_dir_created
    if not _generated_dir_created:
        os.makedirs(GENERATED_DIR, exist_ok=True)
        _generated_dir_created = True


# --------------------------------------------------------------------------
# [FIX C2] Path traversal protection
# --------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {".xlsx", ".docx", ".pptx", ".png", ".pdf"}


def safe_output_path(file_name: str, base_dir: str = GENERATED_DIR) -> str:
    """
    Validates that file_name resolves to a path within base_dir after
    canonicalization, preventing path traversal attacks.
    Also validates the file extension.
    """
    _ensure_generated_dir()

    if not file_name or not file_name.strip():
        raise ValueError("file_name must not be empty.")

    # Strip any leading path separators
    file_name = file_name.lstrip(os.sep).lstrip("/")

    if os.path.isabs(file_name):
        raise ValueError(f"Absolute paths not allowed: {file_name}")

    # Check for path traversal patterns before resolution
    parts = file_name.replace("\\", "/").split("/")
    for part in parts:
        if part == ".." or part == ".":
            raise ValueError(f"Path traversal detected in file_name: {file_name}")

    # Resolve the full path and verify it stays within base_dir
    full_path = os.path.realpath(os.path.join(base_dir, file_name))
    base_real = os.path.realpath(base_dir)

    if not full_path.startswith(base_real + os.sep) and full_path != base_real:
        raise ValueError(f"Path traversal detected: {file_name} resolves to {full_path}")

    # Validate file extension
    ext = os.path.splitext(full_path)[1].lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Disallowed file extension '{ext}' in '{file_name}'. "
            f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )

    return full_path


# --------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling JSON Schema format)
# --------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "new_2d_frame",
        "description": "Starts a brand new, empty planar (2D) frame model in Robot Structural Analysis. Call this before creating any nodes or bars for a 2D structure.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "new_3d_frame",
        "description": "Starts a brand new, empty spatial (3D) frame model in Robot Structural Analysis.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "create_node",
        "description": "Creates a structural node at the given global coordinates (meters).",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Unique integer id for the node."},
                "x": {"type": "number", "description": "X coordinate in meters."},
                "y": {"type": "number", "description": "Y coordinate in meters (0 for 2D frames)."},
                "z": {"type": "number", "description": "Z (vertical) coordinate in meters."},
            },
            "required": ["node_id", "x", "z"],
        },
    },
    {
        "name": "create_bar",
        "description": "Creates a bar (beam/column) element between two existing nodes and assigns a steel section.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer"},
                "start_node": {"type": "integer"},
                "end_node": {"type": "integer"},
                "section_name": {
                    "type": "string",
                    "description": "Catalog section name, e.g. 'IPE 300', 'HEA 200', 'HEB 300', 'W 12X26'. Unspaced forms like 'IPE300' are auto-corrected.",
                    "default": "HEA 200",
                },
            },
            "required": ["bar_id", "start_node", "end_node"],
        },
    },
    {
        "name": "set_support",
        "description": "Applies a boundary condition / support to a node.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer"},
                "support_type": {
                    "type": "string",
                    "enum": ["fixed", "pinned", "roller_x", "roller_z"],
                    "default": "fixed",
                },
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "create_load_case",
        "description": "Creates a new static load case (e.g. Dead Load, Live Load) in the model.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer"},
                "case_name": {"type": "string", "default": "Dead Load"},
                "nature": {
                    "type": "string",
                    "enum": ["permanent", "imposed"],
                    "default": "permanent",
                },
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "apply_bar_load",
        "description": "Applies a uniformly distributed load (kN/m) along a bar within a given load case.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer"},
                "case_id": {"type": "integer"},
                "value_kn_m": {"type": "number", "description": "Load magnitude in kN/m (negative = downward for gravity loads in Z)."},
                "direction": {"type": "string", "enum": ["X", "Y", "Z"], "default": "Z"},
            },
            "required": ["bar_id", "case_id", "value_kn_m"],
        },
    },
    {
        "name": "apply_nodal_load",
        "description": "Applies a concentrated force/moment at a node within a given load case.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer"},
                "case_id": {"type": "integer"},
                "fx_kn": {"type": "number", "default": 0.0},
                "fz_kn": {"type": "number", "default": 0.0},
                "my_knm": {"type": "number", "default": 0.0},
            },
            "required": ["node_id", "case_id"],
        },
    },
    {
        "name": "solve",
        "description": "Runs the Robot FEA solver on the current model. Must be called before exporting any results.",
        "parameters": {
            "type": "object",
            "properties": {
                "timeout_s": {"type": "integer", "default": 120},
            },
        },
    },
    {
        "name": "export_member_forces",
        "description": "Extracts member internal forces (FX, FZ, MY) at evenly spaced stations along every bar, for a given load case. Caches the result for later use by diagrams/reports/exports.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1},
                "divisions": {"type": "integer", "default": 5, "description": "Number of divisions per bar (1-100)."},
            },
        },
    },
    {
        "name": "export_reactions",
        "description": "Extracts support reactions for every supported node, for a given load case. Caches the result for later use.",
        "parameters": {
            "type": "object",
            "properties": {"case_id": {"type": "integer", "default": 1}},
        },
    },
    {
        "name": "export_bill_of_materials",
        "description": "Computes the bill of quantities (steel weight per section type) for all bars currently in the model. Caches the result.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "export_to_excel",
        "description": "Writes the most recently exported member forces, reactions, and BOQ (call the export_* tools first) into a formatted Excel workbook.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "Output filename, e.g. 'Frame_Results.xlsx'."},
                "project_name": {"type": "string", "default": "Untitled Project"},
            },
            "required": ["file_name"],
        },
    },
    {
        "name": "generate_diagrams",
        "description": "Renders Shear Force Diagram (SFD) and Bending Moment Diagram (BMD) images from the most recently exported member forces. Must be called before generate_word_report if diagrams are requested.",
        "parameters": {
            "type": "object",
            "properties": {
                "base_name": {"type": "string", "default": "diagram"},
            },
        },
    },
    {
        "name": "generate_word_report",
        "description": "Generates a formal Word (.docx) structural calculation report, embedding the member-forces / reactions tables and any previously generated diagram images.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "Output filename, e.g. 'Frame_Report.docx'."},
                "project_title": {"type": "string", "default": "Untitled Project"},
                "engineer_name": {"type": "string", "default": "Structural Multi-App Agent"},
                "summary_text": {"type": "string", "description": "A short narrative engineering summary of the analysis and its key findings."},
                "include_diagrams": {"type": "boolean", "default": True},
            },
            "required": ["file_name", "summary_text"],
        },
    },
    {
        "name": "generate_powerpoint_report",
        "description": "Generates a PowerPoint (.pptx) presentation of the structural analysis: title slide, assumptions, design standards, executive summary, governing member-forces table, reactions table, and SFD/BMD diagram slides. Uses the most recently exported results.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "Output filename, e.g. 'Frame_Presentation.pptx'."},
                "project_title": {"type": "string", "default": "Untitled Project"},
                "engineer_name": {"type": "string", "default": "Structural Multi-App Agent"},
                "summary_text": {"type": "string", "description": "A short narrative engineering summary of the analysis and its key findings."},
                "include_diagrams": {"type": "boolean", "default": True},
            },
            "required": ["file_name", "summary_text"],
        },
    },
    {
        "name": "create_structure_from_spec",
        "description": "Builds a complete structure in one call from a JSON spec: project type, nodes, bars (sections), supports, load cases, and loads (uniform / concentrated / nodal). Prefer this tool for large or complex models. Example: {\"project\":\"3D\",\"nodes\":[{\"id\":1,\"x\":0,\"y\":0,\"z\":0}],\"bars\":[{\"id\":1,\"n1\":1,\"n2\":2,\"section\":\"IPE 300\"}],\"supports\":[{\"node\":1,\"type\":\"pinned\"}],\"cases\":[{\"id\":1,\"name\":\"DL\",\"nature\":\"permanent\"}],\"loads\":[{\"kind\":\"bar_uniform\",\"bar\":1,\"case\":1,\"direction\":\"Z\",\"value\":-10}]}",
        "parameters": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": "The full model spec (see tool description for the shape).",
                }
            },
            "required": ["spec"],
        },
    },
    {
        "name": "get_structure_summary",
        "description": "Returns a compact summary of the current Robot model: node/bar/case counts, bounding box, and sections in use.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "clear_structure",
        "description": "Resets the current Robot project to a blank model of the given type (3D or 2D), discarding the current model.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_type": {"type": "string", "description": "'3D' or '2D'", "default": "3D"},
            },
            "required": ["project_type"],
        },
    },
    {
        "name": "create_rectangular_grid_frame",
        "description": "Builds a 3D rectangular grid moment frame: multi-level, multi-bay columns with floor beams and pinned column bases.",
        "parameters": {
            "type": "object",
            "properties": {
                "levels": {"type": "integer", "default": 2},
                "bays_x": {"type": "integer", "default": 2},
                "bays_y": {"type": "integer", "default": 2},
                "bay_width_x": {"type": "number", "default": 5.0},
                "bay_width_y": {"type": "number", "default": 5.0},
                "level_height": {"type": "number", "default": 3.5},
                "column_section": {"type": "string", "default": "HEB 200"},
                "beam_x_section": {"type": "string", "default": "IPE 300"},
                "beam_y_section": {"type": "string", "default": "IPE 300"},
            },
            "required": [],
        },
    },
    {
        "name": "create_truss",
        "description": "Builds a planar Pratt truss (top/bottom chords, verticals, diagonals) pinned at both ends.",
        "parameters": {
            "type": "object",
            "properties": {
                "span": {"type": "number", "default": 12.0},
                "height": {"type": "number", "default": 2.0},
                "panels": {"type": "integer", "default": 6},
                "top_section": {"type": "string", "default": "IPE 200"},
                "bottom_section": {"type": "string", "default": "IPE 200"},
                "web_section": {"type": "string", "default": "L 50x50x5"},
            },
            "required": [],
        },
    },
    {
        "name": "create_braced_frame",
        "description": "Builds a single-bay braced frame (two columns, one beam, one diagonal brace) with pinned bases.",
        "parameters": {
            "type": "object",
            "properties": {
                "height": {"type": "number", "default": 6.0},
                "width": {"type": "number", "default": 6.0},
                "column_section": {"type": "string", "default": "HEB 200"},
                "beam_section": {"type": "string", "default": "IPE 360"},
                "brace_section": {"type": "string", "default": "IPE 200"},
            },
            "required": [],
        },
    },
    {
        "name": "modify_bar_section",
        "description": "Changes the section of an EXISTING bar (e.g. for design optimization / comparing variants) without deleting or recreating it. Geometry, loads and releases are preserved. Use catalog names like 'IPE 300' or 'HEB 200'. Re-run solve afterwards to refresh results.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer", "description": "ID of the existing bar to modify."},
                "section_name": {"type": "string", "description": "New catalog section, e.g. 'HEB 200'."},
            },
            "required": ["bar_id", "section_name"],
        },
    },
    {
        "name": "modify_support",
        "description": "Changes the support condition of an EXISTING node (e.g. pinned -> fixed when optimizing support conditions). The node and all connected bars are preserved. Re-run solve afterwards.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "ID of the existing supported node."},
                "support_type": {"type": "string", "enum": ["fixed", "pinned", "roller_x", "roller_z"]},
            },
            "required": ["node_id", "support_type"],
        },
    },
    {
        "name": "modify_bar_release",
        "description": "Sets end releases (connection fixity) on an EXISTING bar. Each flag: 1 = released (hinged in that DOF), 0 = fixed (continuous). Example: a pin at the start = start_rx=1, start_ry=1, start_rz=1. Defaults (all 0) = fully rigid both ends. Re-run solve afterwards.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer", "description": "ID of the existing bar."},
                "start_ux": {"type": "integer", "default": 0},
                "start_uy": {"type": "integer", "default": 0},
                "start_uz": {"type": "integer", "default": 0},
                "start_rx": {"type": "integer", "default": 0},
                "start_ry": {"type": "integer", "default": 0},
                "start_rz": {"type": "integer", "default": 0},
                "end_ux": {"type": "integer", "default": 0},
                "end_uy": {"type": "integer", "default": 0},
                "end_uz": {"type": "integer", "default": 0},
                "end_rx": {"type": "integer", "default": 0},
                "end_ry": {"type": "integer", "default": 0},
                "end_rz": {"type": "integer", "default": 0},
            },
            "required": ["bar_id"],
        },
    },
    {
        "name": "store_result",
        "description": "Saves a named snapshot of the CURRENT model + its most recently exported results (member forces, reactions, bill of materials) under a variant key, e.g. 'HEB200' or 'pinned-base'. Use this to compare design variants during optimization: build/solve/export variant A, store_result('A'), then modify_bar_section / modify_support / modify_bar_release, re-solve, re-export, store_result('B'), then list_stored_results to compare. Requires the export_* tools to have been run this session (otherwise the snapshot has no result data).",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Variant name, e.g. 'IPE300-variant'."},
            },
            "required": ["key"],
        },
    },
    {
        "name": "retrieve_result",
        "description": "Returns a previously stored result snapshot as readable markdown tables (model summary, member forces, reactions, bill of materials).",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The stored variant name."},
            },
            "required": ["key"],
        },
    },
    {
        "name": "list_stored_results",
        "description": "Lists all stored design-variant snapshots: key, timestamp, bar count, total steel weight, max bending moment — for quick comparison.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "clear_stored_results",
        "description": "Empties the stored result snapshots (the Robot model itself is NOT affected).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_custom_script",
        "description": "[meta] Executes a short Python script you write, giving you direct access to the live Robot bridge when the built-in tools cannot express the request (custom geometry patterns, custom materials, batch sweeps/comparisons, etc.). Available in the script: `robot` (the live RobotBridge — call its methods like robot.create_node(...), robot.solve(), robot.export_all_member_forces(...)), `RobotEnum` (verified Robot constants), `math`, `json`, `pd` (pandas), and `print()`. Set a `result` variable to return structured data. No filesystem/network/imports beyond math/json/statistics/itertools/pandas. On error you receive the traceback — fix the script and retry. Typical flow: prototype here, then register it with create_custom_tool for reuse.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute in the sandbox."},
                "purpose": {"type": "string", "description": "One-line description of what this script does."},
            },
            "required": ["code"],
        },
    },
    {
        "name": "create_custom_tool",
        "description": "[meta] Registers a script you wrote as a NAMED, parameterized tool callable for the rest of the session (it appears in your tool list immediately). Prefer this over re-running scripts manually when a pattern will be reused (e.g. create_arch_bridge(span, rise)). `parameters` is a JSON-schema object; its declared properties become variables inside the script.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case tool name, e.g. 'create_arch_bridge'."},
                "description": {"type": "string", "description": "What the tool does (shown in the tool list)."},
                "parameters": {"type": "object", "description": "JSON-schema 'parameters' object; properties are injected as script variables."},
                "code": {"type": "string", "description": "Python source. Use the declared parameter names as variables; also has robot/RobotEnum/math/json/pd."},
            },
            "required": ["name", "description", "code"],
        },
    },
    {
        "name": "list_custom_tools",
        "description": "[meta] Lists custom tools registered this session (name, parameters, description).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_custom_tool",
        "description": "[meta] Removes a previously registered custom tool.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "delete_bar",
        "description": "Deletes a bar from the current model (e.g. to remove an element the user deleted or wants gone). Use get_structure_summary afterwards to confirm the live model state.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer"},
            },
            "required": ["bar_id"],
        },
    },
    {
        "name": "delete_node",
        "description": "Deletes a node from the current model. Fails (with guidance) if bars are still attached — delete those bars first.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "save_project",
        "description": "Saves the current Robot model as a .rtd project file to an absolute path of the user's choice (e.g. 'D:/projects/bridge.rtd' or 'C:/Users/me/Desktop/tower.rtd'). Parent folders are created automatically. Use when the user asks to save the model anywhere.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute destination path (.rtd appended if missing)."},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "create_cylindrical_tank",
        "description": "Builds a 3D FACETED CYLINDRICAL water-tank frame: a circular ring of nodes at each height level, connected by vertical columns and circumferential ring beams, with pinned base supports. Use this for cylindrical tanks / silos / towers with circular cross-sections — NOT the rectangular grid frame. After building, apply water/hydrostatic loads and solve.",
        "parameters": {
            "type": "object",
            "properties": {
                "radius": {"type": "number", "default": 2.5, "description": "Tank radius in meters (diameter = 2*radius)."},
                "height": {"type": "number", "default": 5.0, "description": "Tank height in meters."},
                "segments": {"type": "integer", "default": 16, "description": "Number of polygon segments around the circle (16-32 for a smooth cylinder)."},
                "ring_levels": {"type": "integer", "default": 2, "description": "Number of horizontal rings including base and top (add mid rings for tall tanks)."},
                "section_vertical": {"type": "string", "default": "IPE 200", "description": "Section for the vertical columns."},
                "section_ring": {"type": "string", "default": "IPE 200", "description": "Section for the circumferential ring beams."},
            },
            "required": ["radius", "height"],
        },
    },
    {
        "name": "export_node_displacements",
        "description": "Exports nodal displacements for a load case: UX/UY/UZ (meters) and RX/RY/RZ (radians) with node coordinates. Caches the result for export_results_to_excel.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1},
            },
            "required": [],
        },
    },
    {
        "name": "export_bar_stresses",
        "description": "Exports bar stresses (MPa) at stations along each member: axial (FXSX), combined extreme (Smax/Smin), bending from MY and MZ, shear Y/Z, and torsion. Caches the result for export_results_to_excel.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1},
                "divisions": {"type": "integer", "default": 5},
            },
            "required": [],
        },
    },
    {
        "name": "export_results_to_excel",
        "description": "Builds one Excel workbook with exactly the sheets you ask for. `sheets` is a list drawn from: 'member_forces' (all 6 components FX/FY/FZ/MX/MY/MZ), 'reactions', 'displacements', 'stresses', 'boq', 'modal'. The needed data is gathered from Robot (solve + export first). Example: sheets=['member_forces','reactions','displacements','stresses','boq'].",
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "Output .xlsx filename."},
                "sheets": {"type": "array", "items": {"type": "string"}, "description": "Which sheets to include, e.g. ['member_forces','reactions','displacements','stresses','boq']."},
                "case_id": {"type": "integer", "default": 1},
            },
            "required": ["file_name", "sheets"],
        },
    },
    {
        "name": "set_material",
        "description": "Creates/reuses a material label (verified: 'STEEL' -> E=210 GPa, NU=0.3 via database). Custom E (MPa) and NU can override. Optionally reassigns all bars to the material.",
        "parameters": {
            "type": "object",
            "properties": {
                "material_name": {"type": "string", "default": "STEEL"},
                "e_mpa": {"type": "number", "description": "Optional Young's modulus in MPa."},
                "nu": {"type": "number", "description": "Optional Poisson ratio."},
                "apply_to_bars": {"type": "boolean", "default": True},
            },
            "required": [],
        },
    },
    {
        "name": "create_panel",
        "description": "[HONEST APPROXIMATION] RobotOM v27 exposes no plate/panel object server, so a panel is built as an equivalent bar grillage (dense grid of beams in the panel plane; plane perpendicular to `normal`). State this limitation when using it. Returns node/bar counts.",
        "parameters": {
            "type": "object",
            "properties": {
                "panel_id": {"type": "integer"},
                "x": {"type": "number", "default": 0.0},
                "y": {"type": "number", "default": 0.0},
                "z": {"type": "number", "default": 0.0},
                "width": {"type": "number", "default": 4.0},
                "height": {"type": "number", "default": 3.0},
                "normal": {"type": "string", "enum": ["X", "Y", "Z"], "default": "Y", "description": "Panel plane normal: Y=horizontal slab (X-Z plane), X or Z = wall."},
                "divisions_x": {"type": "integer", "default": 4},
                "divisions_z": {"type": "integer", "default": 4},
                "section": {"type": "string", "default": "IPE 100"},
                "diagonals": {"type": "boolean", "default": False},
            },
            "required": ["panel_id"],
        },
    },
    {
        "name": "set_panel_thickness",
        "description": "Re-sections every grillage bar of the panel to the nearest IPE depth for the requested plate thickness (approximate stiffness match).",
        "parameters": {
            "type": "object",
            "properties": {
                "panel_id": {"type": "integer"},
                "thickness_m": {"type": "number", "description": "Plate thickness in meters, e.g. 0.2."},
            },
            "required": ["panel_id", "thickness_m"],
        },
    },
    {
        "name": "apply_panel_pressure",
        "description": "Applies a uniform pressure (kPa, negative = downward) on a grillage panel as equivalent nodal loads at the grid nodes (tributary areas). Total force = pressure x panel area is conserved.",
        "parameters": {
            "type": "object",
            "properties": {
                "panel_id": {"type": "integer"},
                "case_id": {"type": "integer", "default": 1},
                "pressure_kpa": {"type": "number", "default": -1.0},
            },
            "required": ["panel_id"],
        },
    },
    {
        "name": "create_solid",
        "description": "[NATIVE, VERIFIED] Creates a 3D solid volume from existing nodes via Objects.CreateSolid. `face_groups` lists closed loops of node numbers (each face ordered consistently), e.g. [[1,2,3,4],[1,2,6,5],...]. CAUTION: solid volumes mesh with Robot's default fine mesh, so 'solve' can take several minutes; use sparingly and tell the user to expect a slow solve.",
        "parameters": {
            "type": "object",
            "properties": {
                "solid_id": {"type": "integer"},
                "node_ids": {"type": "array", "items": {"type": "integer"}, "description": "All vertex node numbers of the solid."},
                "face_groups": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}, "description": "Bounding faces as ordered closed loops of node numbers."},
            },
            "required": ["solid_id", "node_ids", "face_groups"],
        },
    },
    {
        "name": "create_solid_box",
        "description": "[NATIVE, VERIFIED] Convenience: creates a rectangular solid volume (box) at a given origin with the given dimensions. Generates its 8 corner nodes automatically. CAUTION: solid volumes mesh with Robot's default fine mesh, so 'solve' can take several minutes; use sparingly and tell the user to expect a slow solve.",
        "parameters": {
            "type": "object",
            "properties": {
                "solid_id": {"type": "integer"},
                "origin_x": {"type": "number", "default": 0.0},
                "origin_y": {"type": "number", "default": 0.0},
                "origin_z": {"type": "number", "default": 0.0},
                "size_x": {"type": "number", "default": 1.0},
                "size_y": {"type": "number", "default": 1.0},
                "size_z": {"type": "number", "default": 1.0},
            },
            "required": ["solid_id"],
        },
    },
    {
        "name": "solve_modal",
        "description": "[HONEST] Runs a modal (eigenvalue) analysis: creates/reuses a modal case (I_CAT_DYNAMIC_MODAL), sets the number of modes, and attempts the solve with a bounded timeout. IMPORTANT VERIFIED LIMITATION: RobotOM v27's modal solver does not complete programmatically in this environment (Calculate() hangs; results stay empty), so this tool reports results_available=False, removes the modal case, and recommends running modal analysis in the Robot GUI. Use it when the user explicitly asks for modal frequencies.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1},
                "n_modes": {"type": "integer", "default": 10},
                "timeout_s": {"type": "integer", "default": 150},
            },
            "required": [],
        },
    },
    {
        "name": "export_modal_frequencies",
        "description": "Exports natural frequencies (Hz), periods (s), pulsation, damping and participation for the given modal case from Results.Advanced.Eigenvalues. Returns an empty table with an honest note when no modal results exist (modal analysis did not complete).",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1},
                "n_modes": {"type": "integer", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "export_modal_mode_shapes",
        "description": "Exports the mode shape (eigenvector) for one mode at every node via Results.Advanced.Eigenvectors (UX/UY/UZ in m, RX/RY/RZ in rad). Empty when no modal results exist.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1},
                "mode_num": {"type": "integer", "default": 1},
            },
            "required": [],
        },
    },
    {
        "name": "get_utilization_ratios",
        "description": "[P4] Per-member utilization ratios (code check) for a SOLVED case. ANALYTICAL, clearly not Robot's design module: RobotOM v27 exposes no code-check server (verified live), so this divides Robot's own solved bar stresses by the material design strength (RE / fy). Returns per bar: governing Utilization, Governing_Check (combined_normal = axial + biaxial bending at the extreme fiber, axial, shear_y, shear_z, torsion), fy_MPa, Status PASS/FAIL (>1.0). Catalog 'STEEL' carries fy=235 MPa (S235); custom materials are ONLY checkable if set_material was given fy_mpa — otherwise the bar returns Status=NOT_CHECKABLE with the reason (never a silent wrong number). Run solve first; snapshot via store_result includes these ratios.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1},
                "bar_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "Optional subset of bar ids; omit for all bars.",
                },
                "divisions": {"type": "integer", "default": 5},
            },
            "required": [],
        },
    },
    {
        "name": "define_combination",
        "description": "[P5, VERIFIED] Creates (or redefines) a load combination, e.g. 1.2*Dead + 1.6*Live via case_factors {case_id: factor}. combination_type: 'ULS' (default), 'SLS', 'ALS'. Component cases must already exist (create_load_case). VERIFIED LIVE: solve() evaluates all combinations automatically — no separate trigger (1.2D+1.6L returned exactly 1.2*M_dead + 1.6*M_live). Read combined results with export_member_forces/export_reactions using the returned case_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "case_factors": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                    "description": "Map of load-case id to factor, e.g. {\"1\": 1.2, \"2\": 1.6}.",
                },
                "combination_type": {"type": "string", "default": "ULS",
                                     "enum": ["ULS", "SLS", "ALS", "ACC", "SPC"]},
            },
            "required": ["name", "case_factors"],
        },
    },
    {
        "name": "list_combinations",
        "description": "[P5] Lists every defined load combination: case_id, name, type (ULS/SLS/ALS/SPC) and its factors [{case, factor}].",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "solve_combination",
        "description": "[P5, VERIFIED] Runs the solver so all cases AND combinations get results. Robot's Calculate() evaluates combinations automatically (verified live — no separate trigger exists or is needed); passing name just validates that the named combination is defined. Afterwards read combined results with export_member_forces(case_id=<combination id>) or get_governing_combination.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Optional combination name to validate."},
            },
            "required": [],
        },
    },
    {
        "name": "start_optimization_run",
        "description": "[P7 BATCH OPTIMIZER] Validates a design-space spec and estimates the run WITHOUT starting it. Translate the user's natural-language brief into the DesignSpace JSON schema: {\"geometry\": {...same as create_structure_from_spec's spec: nodes/bars with section/supports/cases/loads...}, \"variable_groups\": [{\"group_name\": \"columns\", \"bar_ids\": [1,3], \"candidate_sections\": [\"HEA 200\",\"HEA 220\"]}, ...], \"load_cases\": [{\"id\":1,\"name\":\"DL\",\"nature\":\"permanent\"}], \"analysis_types\": [\"static\"], \"objective\": {\"minimize\": \"weight\", \"constraint\": \"max_utilization <= 1.0 AND buckling_pass == True\"}}. HARD RULE: this tool NEVER starts a run - not ever, under ANY phrasing. It only validates + returns the candidate count, time estimate and a run_config_id. Do NOT call confirm_and_start_optimization_run in this same response, even if the user said 'just run it', 'go ahead', 'start it', 'yes do it', or anything that sounds like permission. A batch run consumes Robot license time, so confirmation ALWAYS requires a SEPARATE, LATER message from the user AFTER they have seen and approved this estimate. Your next reply after this tool must present the count + estimate to the user and ask for explicit confirmation - then STOP and wait for their next message. If you find yourself wanting to call confirm_and_start_optimization_run in this same turn, STOP: that is a violation.",
        "parameters": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": "DesignSpace JSON. Example: {\"geometry\":{\"project\":\"2D\",\"nodes\":[{\"id\":1,\"x\":0,\"z\":0},{\"id\":2,\"x\":0,\"z\":3},{\"id\":3,\"x\":6,\"z\":3},{\"id\":4,\"x\":6,\"z\":0}],\"bars\":[{\"id\":1,\"n1\":1,\"n2\":2,\"section\":\"HEA 200\"},{\"id\":2,\"n1\":2,\"n2\":3,\"section\":\"IPE 300\"},{\"id\":3,\"n1\":3,\"n2\":4,\"section\":\"HEA 200\"}],\"supports\":[{\"node\":1,\"type\":\"pinned\"},{\"node\":4,\"type\":\"pinned\"}],\"cases\":[{\"id\":1,\"name\":\"DL\",\"nature\":\"permanent\"}],\"loads\":[{\"kind\":\"bar_uniform\",\"bar\":2,\"case\":1,\"direction\":\"Z\",\"value\":-3}]},\"variable_groups\":[{\"group_name\":\"columns\",\"bar_ids\":[1,3],\"candidate_sections\":[\"HEA 200\",\"HEA 220\",\"HEA 240\",\"HEB 200\"]},{\"group_name\":\"beam\",\"bar_ids\":[2],\"candidate_sections\":[\"IPE 270\",\"IPE 300\",\"IPE 330\"]}],\"load_cases\":[{\"id\":1,\"name\":\"DL\",\"nature\":\"permanent\"}],\"analysis_types\":[\"static\"],\"objective\":{\"minimize\":\"weight\",\"constraint\":\"max_utilization <= 1.0 AND buckling_pass == True\"}}",
                }
            },
            "required": ["spec"],
        },
    },
    {
        "name": "confirm_and_start_optimization_run",
        "description": "[P7 BATCH OPTIMIZER] STARTS a batch optimization run in the background (does not block the chat). Only call this AFTER the user has explicitly confirmed the candidate count + time estimate returned by start_optimization_run — never start a batch run without explicit user confirmation (it consumes Robot license time). Pass the run_config_id returned by start_optimization_run. Returns the run_id immediately; poll with check_optimization_status.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_config_id": {"type": "string", "description": "The run_config_id returned by start_optimization_run."},
            },
            "required": ["run_config_id"],
        },
    },
    {
        "name": "check_optimization_status",
        "description": "[P7 BATCH OPTIMIZER] Queries progress of a batch optimization run: status (running/completed/failed/cancelled), candidates evaluated / total, any failures, elapsed time and estimated remaining. No Robot interaction.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer", "description": "The run_id returned by confirm_and_start_optimization_run."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "get_optimization_results",
        "description": "[P7 BATCH OPTIMIZER] Returns the Pareto frontier of a COMPLETED batch run as a markdown table (ranked by weight, with utilization + buckling margins). Only meaningful once check_optimization_status shows 'completed' — otherwise it plainly says the run is not finished (never returns partial/misleading results). Includes the standing 'elastic stress + basic Euler buckling screening only, not full code compliance' caveat.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer", "description": "The run_id of a completed run."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "cancel_optimization_run",
        "description": "[P7 BATCH OPTIMIZER] Requests cancellation of a running batch optimization. The runner stops cleanly BETWEEN candidates (finishes + checkpoints the current candidate first, then exits) — never mid-solve. Returns the current progress.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer", "description": "The run_id to cancel."},
            },
            "required": ["run_id"],
        },
    },
]

# [FIX M10] Build a schema lookup for argument validation
_SCHEMA_LOOKUP: Dict[str, Dict[str, Any]] = {}
for _schema in TOOL_SCHEMAS:
    _SCHEMA_LOOKUP[_schema["name"]] = _schema


def _validate_tool_arguments(tool_name: str, arguments: Dict[str, Any]) -> None:
    """
    [FIX M10] Validates tool arguments against the declared JSON schema.
    Raises ToolExecutionError for missing required params or type mismatches.
    Also detects JSON decode errors injected by llm_providers.py [FIX H3].
    """
    # [FIX H3] Detect JSON decode error marker from llm_providers
    if arguments.get("_json_decode_error"):
        raise ToolExecutionError(
            f"Tool call '{tool_name}' received malformed JSON arguments from the LLM. "
            f"Error: {arguments.get('_error_message', 'unknown')}. "
            f"Raw arguments: {arguments.get('_raw_arguments', '')[:300]}. "
            "Please regenerate the tool call with valid JSON."
        )

    schema = _SCHEMA_LOOKUP.get(tool_name)
    if schema is None:
        return  # Unknown tool — let dispatch handle it

    params_def = schema.get("parameters", {})
    required = params_def.get("required", [])
    properties = params_def.get("properties", {})

    # Check required parameters
    missing = [r for r in required if r not in arguments]
    if missing:
        raise ToolExecutionError(
            f"Tool '{tool_name}' is missing required parameter(s): {missing}. "
            f"Provided arguments: {list(arguments.keys())}. "
            f"Required: {required}."
        )

    # Basic type checking
    type_map = {"integer": int, "number": (int, float), "string": str, "boolean": bool}
    for key, value in arguments.items():
        if key.startswith("_"):
            continue  # Skip internal markers
        prop_def = properties.get(key)
        if prop_def is None:
            continue  # Extra param — will be ignored by handler
        expected_type = prop_def.get("type")
        if expected_type and expected_type in type_map:
            if not isinstance(value, type_map[expected_type]):
                raise ToolExecutionError(
                    f"Tool '{tool_name}' parameter '{key}' expected type "
                    f"'{expected_type}' but got '{type(value).__name__}' "
                    f"with value {value!r}."
                )


# --------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------

class ToolExecutionError(RuntimeError):
    """Raised (and caught by the agent loop) when a tool call fails."""


# [FIX M6] Maximum number of tool calls allowed in a single LLM response
MAX_TOOL_CALLS_PER_STEP = 10


class ToolExecutor:
    """
    Owns live instances of the four engineering tool bridges plus the
    in-memory result cache (member forces / reactions / BOQ DataFrames and
    generated file paths), and dispatches named tool calls to them.

    One ToolExecutor is created per Streamlit session and stashed in
    st.session_state so state (the open Robot model, cached DataFrames,
    generated artifact paths) survives across chat turns.
    """

    def __init__(self, robot_visible: bool = True):
        self.robot = RobotBridge()
        self.excel = ExcelReporter()
        self.diagrams = DiagramGenerator()
        self.word = WordReporter()
        self.pptx = PowerPointReporter()
        # [PHASE 2] Session-scoped variant/result snapshot store.
        self.results = ResultStore()
        # [WP1 meta-layer] LLM-authored custom tools (session-scoped).
        self.custom_tools = CustomToolRegistry()
        import tools.custom_tools as _ct
        _ct._BUILTIN_TOOL_NAMES = {s["name"] for s in TOOL_SCHEMAS}

        # [P7] Batch optimizer state: staged (validated but NOT started)
        # design-space configs, plus handles to live background runs.
        self._optimization_configs: Dict[str, dict] = {}
        self._optimization_runs: Dict[int, dict] = {}  # run_id -> thread info
        # [P7] Default SQLite DB for batch runs, shared by all bookend tools.
        self._batch_db_path = os.path.join(_PROJECT_ROOT, "batch", "runs.db")
        # [OBS] Lifecycle event log (plain list - no Streamlit import here).
        # app.py drains this into the sidebar Activity Log panel each turn so
        # connect/close/clear_structure events are visible in the running app.
        self.activity_log: List[str] = []

        self._robot_connected = False
        self._robot_visible = robot_visible

        self.member_forces_df: pd.DataFrame = pd.DataFrame()
        self.reactions_df: pd.DataFrame = pd.DataFrame()
        self.boq_df: pd.DataFrame = pd.DataFrame()
        # [WP6] Additional cached result exports.
        self.displacements_df: pd.DataFrame = pd.DataFrame()
        self.stresses_df: pd.DataFrame = pd.DataFrame()
        # [WP7] Modal frequency cache (for the Excel 'modal' sheet).
        self.modal_frequencies_df: pd.DataFrame = pd.DataFrame()
        # [P4] Utilization cache (included in store_result snapshots).
        self.utilization_df: pd.DataFrame = pd.DataFrame()

        self.generated_files: Dict[str, str] = {}   # logical name -> abs path
        self.diagram_paths: Dict[str, str] = {}      # 'sfd' / 'bmd' -> abs path

    def _log_activity(self, entry: str) -> None:
        """Appends a timestamped lifecycle event to the executor's activity log
        (drained into the UI by app.py)."""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.activity_log.append(f"[{ts}] {entry}")

    def drain_activity(self) -> List[str]:
        """Returns and clears the pending lifecycle events."""
        out = list(self.activity_log)
        self.activity_log = []
        return out

    def get_tool_schemas(self) -> list:
        """
        [WP1] Base tool schemas + any custom tools registered this session.
        The agent loop passes this to the LLM so custom tools appear in the
        tool list immediately after registration.
        """
        return TOOL_SCHEMAS + self.custom_tools.schemas()

    # ------------------------------------------------------------------ #
    # Visibility setter (avoids direct private attribute access)
    # ------------------------------------------------------------------ #

    def set_robot_visible(self, visible: bool) -> None:
        """[FIX M5] Public setter for Robot visibility instead of direct _ access."""
        self._robot_visible = visible

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def dispatch(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        Executes a named tool with the given arguments. Returns a JSON string
        result on success. Raises ToolExecutionError (with a clear message
        suitable for feeding back to the LLM) on failure.
        """
        # [FIX M10] Validate arguments against schema before dispatching
        _validate_tool_arguments(tool_name, arguments)

        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            # [WP1 meta-layer] fall back to session-registered custom tools.
            if self.custom_tools.has(tool_name):
                return self._call_custom_tool(tool_name, arguments)
            raise ToolExecutionError(f"Unknown tool '{tool_name}'. No such handler is registered.")

        try:
            result = handler(**arguments)
        except ToolExecutionError:
            raise  # Re-raise as-is
        except TypeError as exc:
            # Convert Python TypeError (wrong arguments) to structured error
            raise ToolExecutionError(
                f"Tool '{tool_name}' argument error: {exc}. "
                f"Provided arguments: {list(arguments.keys())}."
            ) from exc
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            logger.error("Unhandled error in tool '%s': %s\n%s", tool_name, exc, tb)
            raise ToolExecutionError(
                f"Tool '{tool_name}' failed with an unexpected error: {exc}"
            ) from exc

        return json.dumps(result, default=str)

    # ------------------------------------------------------------------ #
    # Robot connection helper
    # ------------------------------------------------------------------ #

    def _ensure_robot(self):
        """
        Connects to Robot on first use; reconnects if the connection was
        genuinely lost (COM transport error).

        [FIX R2] connect() failures are converted into ToolExecutionError so
        the LLM receives actionable guidance instead of the agent silently
        looping. The RobotBridge launch circuit-breaker [FIX R3] guarantees
        no endless robot.exe spawning.
        """
        try:
            if not self._robot_connected:
                self.robot.connect(visible=self._robot_visible)
                self._robot_connected = True
                self._log_activity(
                    f"🔌 Robot connected (PID {self.robot.pid}) - first connection")
            elif not self.robot.is_alive():  # [FIX H8] Health check
                logger.warning("Robot connection lost; attempting reconnect...")
                self.robot.connect(visible=self._robot_visible)
                self._robot_connected = True
                self._log_activity(
                    f"🔌 Robot RECONNECTED (PID {self.robot.pid}) - health-check "
                    "reconnect after connection loss")
        except ToolExecutionError:
            raise
        except Exception as exc:
            self._robot_connected = False
            raise ToolExecutionError(
                f"Could not establish a Robot Structural Analysis connection: {exc}. "
                "Verify Robot is installed, licensed, and not blocked by a "
                "splash/license dialog; then retry the tool call."
            ) from exc

    # ------------------------------------------------------------------ #
    # Tool handlers
    # ------------------------------------------------------------------ #

    def _tool_new_2d_frame(self) -> dict:
        self._ensure_robot()
        self.robot.new_2d_frame()
        return {"status": "ok", "message": "New 2D frame project created."}

    def _tool_new_3d_frame(self) -> dict:
        self._ensure_robot()
        self.robot.new_3d_frame()
        return {"status": "ok", "message": "New 3D frame project created."}

    def _tool_create_node(self, node_id: int, x: float, z: float, y: float = 0.0) -> dict:
        self._ensure_robot()
        self.robot.create_node(node_id, x, y, z)
        return {"status": "ok", "node_id": node_id, "x": x, "y": y, "z": z}

    def _tool_create_bar(
        self, bar_id: int, start_node: int, end_node: int, section_name: str = "HEA 200"
    ) -> dict:
        self._ensure_robot()
        self.robot.create_bar(bar_id, start_node, end_node, section_name)
        return {"status": "ok", "bar_id": bar_id, "section": section_name}

    def _tool_set_support(self, node_id: int, support_type: str = "fixed") -> dict:
        self._ensure_robot()
        self.robot.set_support(node_id, support_type)
        return {"status": "ok", "node_id": node_id, "support_type": support_type}

    def _tool_create_load_case(
        self, case_id: int, case_name: str = "Dead Load", nature: str = "permanent"
    ) -> dict:
        self._ensure_robot()
        nature_int = 0 if nature == "permanent" else 1
        self.robot.create_load_case(case_id, case_name, nature=nature_int)
        return {"status": "ok", "case_id": case_id, "case_name": case_name}

    def _tool_apply_bar_load(
        self, bar_id: int, case_id: int, value_kn_m: float, direction: str = "Z"
    ) -> dict:
        self._ensure_robot()
        self.robot.apply_bar_load(bar_id, case_id, value_kn_m, direction)
        return {"status": "ok", "bar_id": bar_id, "case_id": case_id, "value_kn_m": value_kn_m}

    def _tool_apply_nodal_load(
        self, node_id: int, case_id: int, fx_kn: float = 0.0, fz_kn: float = 0.0, my_knm: float = 0.0
    ) -> dict:
        self._ensure_robot()
        self.robot.apply_nodal_load(node_id, case_id, fx_kn, fz_kn, my_knm)
        return {"status": "ok", "node_id": node_id, "case_id": case_id}

    def _tool_solve(self, timeout_s: int = 120) -> dict:
        self._ensure_robot()
        self.robot.solve(timeout_s=timeout_s)
        return {"status": "ok", "message": "Solver run completed successfully."}

    def _tool_get_utilization_ratios(
        self, case_id: int = 1, bar_ids: list = None, divisions: int = 5,
    ) -> dict:
        self._ensure_robot()
        df = self.robot.get_utilization_ratios(
            case_id=case_id, bar_ids=bar_ids, divisions=divisions)
        self.utilization_df = df
        summary = {
            "bars": len(df),
            "pass": int((df.get("Status") == "PASS").sum()) if not df.empty else 0,
            "fail": int((df.get("Status") == "FAIL").sum()) if not df.empty else 0,
            "not_checkable": int((df.get("Status") == "NOT_CHECKABLE").sum())
            if not df.empty else 0,
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

    def _tool_define_combination(
        self, name: str, case_factors: dict, combination_type: str = "ULS",
    ) -> dict:
        self._ensure_robot()
        result = self.robot.define_combination(
            name=name, case_factors={int(k): float(v)
                                     for k, v in case_factors.items()},
            combination_type=combination_type)
        return {"status": "ok", **result}

    def _tool_list_combinations(self) -> dict:
        self._ensure_robot()
        combos = self.robot.list_combinations()
        return {"status": "ok", "count": len(combos), "combinations": combos}

    def _tool_solve_combination(self, name: str = None) -> dict:
        self._ensure_robot()
        result = self.robot.solve_combination(name=name)
        return {"status": "ok", **result}

    def _tool_get_governing_combination(
        self, bar_id: int, component: str = "MY", divisions: int = 5,
    ) -> dict:
        self._ensure_robot()
        result = self.robot.get_governing_combination(
            bar_id=bar_id, component=component, divisions=divisions)
        return {"status": "ok", **result}

    # ------------------------------------------------------------------ #
    # [P7] Batch optimizer bookend tools (dispatch into batch/, not Robot)
    # ------------------------------------------------------------------ #

    def _tool_start_optimization_run(self, spec: dict) -> dict:
        """Validates a DesignSpace spec and estimates the run WITHOUT starting
        it. Stores the validated spec under a generated run_config_id so the
        run only starts after explicit user confirmation via
        confirm_and_start_optimization_run. NEVER starts here."""
        if not spec or not isinstance(spec, dict):
            raise ToolExecutionError(
                "start_optimization_run requires a DesignSpace JSON 'spec' "
                "object (geometry + variable_groups + load_cases + "
                "analysis_types + objective). See the schema description.")
        try:
            ds = DesignSpace(spec)
            n_candidates = ds.candidate_count()
            ds.generate_candidates()  # validates grid <= cap (Phase 4 errors)
        except DesignSpaceError as exc:
            raise ToolExecutionError(
                f"Invalid design space: {exc}. Fix the spec and retry.") from exc
        # Conservative estimate from Phase-1 T1 timing (5-11 s/candidate).
        lo_s, hi_s = n_candidates * 5, n_candidates * 11
        cfg_id = f"cfg_{int(time.time())}"
        self._optimization_configs[cfg_id] = {"spec": spec, "created": time.time()}
        return {
            "status": "not_started",
            "run_config_id": cfg_id,
            "candidate_count": n_candidates,
            "estimate_seconds_min": lo_s,
            "estimate_seconds_max": hi_s,
            "estimate": (f"{n_candidates} candidates, roughly "
                         f"{lo_s//60}-{hi_s//60} min (5-11 s/candidate, "
                         "reused Robot session)"),
            "message": ("Run NOT started. Show the user the candidate count "
                        "and time estimate, get explicit confirmation, then "
                        "call confirm_and_start_optimization_run with this "
                        "run_config_id."),
        }

    def _tool_confirm_and_start_optimization_run(self, run_config_id: str) -> dict:
        """Starts a staged batch run in a background thread and returns
        immediately with the run_id. Only staged configs (from
        start_optimization_run) can be started.

        The run + candidate rows are pre-created SYNCHRONOUSLY here (pure
        SQLite, no Robot) so the run_id is known immediately; the thread
        then executes the batch with that run_id."""
        cfg = self._optimization_configs.pop(run_config_id, None)
        if cfg is None:
            raise ToolExecutionError(
                f"run_config_id '{run_config_id}' is not a staged config "
                "(call start_optimization_run first).")
        ds = DesignSpace(cfg["spec"])
        # Pre-create run + candidates (fast, no Robot) so run_id is immediate.
        st = Storage(db_path=self._batch_db_path)
        try:
            run_id = st.create_run(ds.to_dict(),
                                   objective=json.dumps(ds.objective, default=str))
            for cand in ds.generate_candidates():
                st.add_candidate(run_id, cand)
        except Exception as exc:  # noqa: BLE001
            raise ToolExecutionError(
                f"Could not stage batch run: {exc}") from exc
        finally:
            st.close()

        # Background thread: the runner opens its OWN Robot instance
        # (HeadlessSession, new_instance=True) and its own Storage connection,
        # so it never touches the interactive app's Robot or session state.
        import threading

        holder: Dict[str, Any] = {"run_id": run_id, "error": None}
        t = threading.Thread(
            target=self._run_optimization_worker,
            args=(ds, run_id, holder),
            name="batch-optimizer",
            daemon=True,
        )
        t.start()
        self._optimization_runs[run_id] = {
            "thread": t, "started": time.time()}
        return {
            "status": "started",
            "run_id": run_id,
            "message": ("Batch optimization started in the background. Poll "
                        "check_optimization_status; then get_optimization_results "
                        "once it is 'completed'."),
        }

    def _run_optimization_worker(self, ds: DesignSpace, run_id: int,
                                 holder: Dict[str, Any]) -> None:
        """Runs run_batch on the background thread for the pre-created run."""
        try:
            summary = run_batch(ds, run_id=run_id, db_path=self._batch_db_path)
            holder["run_id"] = summary["run_id"]
        except Exception as exc:  # noqa: BLE001
            logger.error("Batch optimizer worker failed: %s", exc)
            holder["error"] = str(exc)

    def _tool_check_optimization_status(self, run_id: int) -> dict:
        st = Storage(db_path=self._batch_db_path)
        try:
            run = st.get_run(run_id)
            if run is None:
                raise ToolExecutionError(
                    f"run_id {run_id} does not exist in batch storage "
                    f"({self._batch_db_path}).")
            df = st.get_all_results(run_id)
            n_eval = int((df["candidate_status"] == "evaluated").sum())
            n_fail = int((df["candidate_status"] == "failed").sum())
            total = len(df)
        finally:
            st.close()
        status = str(run.get("status", "unknown"))
        # Estimate remaining from average elapsed-per-evaluated (like the
        # runner's ETA): created_at is stored in the runs row.
        from datetime import datetime
        elapsed_s = None
        remaining = None
        try:
            created = datetime.strptime(str(run["created_at"]),
                                        "%Y-%m-%d %H:%M:%S")
            elapsed_s = (datetime.now() - created).total_seconds()
            if n_eval > 0 and total > n_eval:
                per_c = elapsed_s / n_eval
                remaining = int(per_c * (total - n_eval))
        except Exception:  # noqa: BLE001
            elapsed_s, remaining = None, None
        return {
            "status": status,
            "run_id": run_id,
            "evaluated": n_eval,
            "failed": n_fail,
            "total": total,
            "elapsed_s": int(elapsed_s) if elapsed_s else None,
            "estimated_remaining_s": remaining,
        }

    def _tool_get_optimization_results(self, run_id: int) -> dict:
        st = Storage(db_path=self._batch_db_path)
        try:
            run = st.get_run(run_id)
            if run is None:
                raise ToolExecutionError(
                    f"run_id {run_id} does not exist in batch storage.")
            status = str(run.get("status", "unknown"))
            if status != "completed":
                return {
                    "status": "not_ready",
                    "run_status": status,
                    "message": (f"Run {run_id} is '{status}', not 'completed'. "
                                "Results are NOT meaningful until the run "
                                "finishes - poll check_optimization_status."),
                }
            df = st.get_all_results(run_id)
        finally:
            st.close()
        summ = pareto_summary(df)
        return {
            "status": "ok",
            "run_id": run_id,
            "total": summ["total"],
            "passed": summ["passed"],
            "frontier_size": summ["frontier"],
            "note": summ.get("note", ""),
            "markdown": summ["markdown"],
        }

    def _tool_cancel_optimization_run(self, run_id: int) -> dict:
        st = Storage(db_path=self._batch_db_path)
        try:
            run = st.get_run(run_id)
            if run is None:
                raise ToolExecutionError(
                    f"run_id {run_id} does not exist in batch storage.")
            st.request_cancel(run_id, reason="user requested")
            df = st.get_all_results(run_id)
            n_eval = int((df["candidate_status"] == "evaluated").sum())
            total = len(df)
        finally:
            st.close()
        return {
            "status": "cancel_requested",
            "run_id": run_id,
            "message": ("Cancellation requested. The runner stops cleanly "
                        "BETWEEN candidates (current candidate finishes and "
                        "is checkpointed first, then it exits). Progress so "
                        f"far: {n_eval}/{total} evaluated."),
        }

    def _tool_export_member_forces(self, case_id: int = 1, divisions: int = 5) -> dict:
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

    def _tool_export_reactions(self, case_id: int = 1) -> dict:
        self._ensure_robot()
        df = self.robot.export_reactions(case_id=case_id)
        self.reactions_df = df
        return {
            "status": "ok",
            "rows": len(df),
            "preview": df.to_dict(orient="records"),
        }

    def _tool_export_bill_of_materials(self) -> dict:
        self._ensure_robot()
        df = self.robot.export_bill_of_materials()
        self.boq_df = df
        return {
            "status": "ok",
            "rows": len(df),
            "preview": df.to_dict(orient="records"),
        }

    def _tool_export_to_excel(self, file_name: str, project_name: str = "Untitled Project") -> dict:
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

    def _save_robot_project_artifact(self, base_name: str) -> None:
        """
        [TANK-FIX] Saves the current Robot model (.rtd) into the generated
        artifacts directory so the user can download it alongside the
        reports/diagrams. Best-effort: never blocks artifact generation.
        """
        try:
            self._ensure_robot()
            stem = (os.path.splitext(os.path.basename(base_name))[0]
                    or "robot_model").strip()
            _ensure_generated_dir()
            rtd_path = os.path.join(GENERATED_DIR, f"{stem}.rtd")
            self.robot.save_project(rtd_path)
            self.generated_files[f"{stem}.rtd"] = rtd_path
        except Exception as exc:
            logger.warning("Could not auto-save the Robot model artifact: %s", exc)

    def _tool_generate_diagrams(self, base_name: str = "diagram") -> dict:
        if self.member_forces_df.empty:
            raise ToolExecutionError(
                "No member force data cached. Call export_member_forces before generate_diagrams."
            )
        # [FIX C2] Validate base_name (used to construct filenames)
        if not re.match(r'^[a-zA-Z0-9_\-]+$', base_name):
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

    def _tool_generate_word_report(
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

    def _tool_generate_powerpoint_report(
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

    # ------------------------------------------------------------------ #
    # Milestone A: model spec / summary / templates
    # ------------------------------------------------------------------ #

    def _tool_create_structure_from_spec(self, spec=None) -> dict:
        self._ensure_robot()  # [WP1 fix] connect before touching the bridge
        summary = self.robot.build_structure_from_spec(spec)
        logger.info("Built structure from spec: %s", summary)
        return {"status": "ok", **summary}

    def _tool_get_structure_summary(self) -> dict:
        self._ensure_robot()  # [WP1 fix]
        summary = self.robot.get_structure_summary()
        return {"status": "ok", **summary}

    def _tool_clear_structure(self, project_type: str = "3D") -> dict:
        self._ensure_robot()  # [WP1 fix]
        self.robot.clear_structure(project_type)
        self._log_activity(
            f"🗑️ clear_structure called (project_type={project_type}, "
            f"PID {self.robot.pid}) - LLM-requested model reset")
        return {"status": "ok", "project_type": project_type,
                "message": f"Cleared; blank {project_type} model created."}

    def _tool_create_rectangular_grid_frame(
        self, levels: int = 2, bays_x: int = 2, bays_y: int = 2,
        bay_width_x: float = 5.0, bay_width_y: float = 5.0,
        level_height: float = 3.5, column_section: str = "HEB 200",
        beam_x_section: str = "IPE 300", beam_y_section: str = "IPE 300",
    ) -> dict:
        self._ensure_robot()  # [WP1 fix]
        summary = self.robot.create_rectangular_grid_frame(
            levels=levels, bays_x=bays_x, bays_y=bays_y,
            bay_width_x=bay_width_x, bay_width_y=bay_width_y,
            level_height=level_height, column_section=column_section,
            beam_x_section=beam_x_section, beam_y_section=beam_y_section,
        )
        return {"status": "ok", **summary}

    def _tool_create_truss(
        self, span: float = 12.0, height: float = 2.0, panels: int = 6,
        top_section: str = "IPE 200", bottom_section: str = "IPE 200",
        web_section: str = "L 50x50x5",
    ) -> dict:
        self._ensure_robot()  # [WP1 fix]
        summary = self.robot.create_truss(
            span=span, height=height, panels=panels, top_section=top_section,
            bottom_section=bottom_section, web_section=web_section,
        )
        return {"status": "ok", **summary}

    def _tool_create_braced_frame(
        self, height: float = 6.0, width: float = 6.0,
        column_section: str = "HEB 200", beam_section: str = "IPE 360",
        brace_section: str = "IPE 200",
    ) -> dict:
        self._ensure_robot()  # [WP1 fix]
        summary = self.robot.create_braced_frame(
            height=height, width=width, column_section=column_section,
            beam_section=beam_section, brace_section=brace_section,
        )
        return {"status": "ok", **summary}

    # ------------------------------------------------------------------ #
    # Phase 1: element modification (iterative design)
    # ------------------------------------------------------------------ #

    def _tool_modify_bar_section(self, bar_id: int, section_name: str) -> dict:
        self._ensure_robot()
        message = self.robot.modify_bar_section(bar_id, section_name)
        return {"status": "ok", "message": message,
                "bar_id": bar_id, "section": section_name}

    def _tool_modify_support(self, node_id: int, support_type: str) -> dict:
        self._ensure_robot()
        message = self.robot.modify_support(node_id, support_type)
        return {"status": "ok", "message": message,
                "node_id": node_id, "support_type": support_type}

    def _tool_modify_bar_release(self, bar_id: int, **release_flags) -> dict:
        self._ensure_robot()
        message = self.robot.modify_bar_release(bar_id, **release_flags)
        return {"status": "ok", "message": message, "bar_id": bar_id}

    # ------------------------------------------------------------------ #
    # Phase 2: result store & variant comparison
    # ------------------------------------------------------------------ #

    def _tool_store_result(self, key: str) -> dict:
        # Snapshot uses the executor's cached export DataFrames (populated by
        # the export_* tools) plus a live (cheap, bookkeeping-based) model
        # summary — no redundant re-query of solved results.
        try:
            summary = self.robot.get_structure_summary()
        except Exception:
            summary = {"nodes": 0, "bars": 0, "sections": {}}
        message = self.results.store(
            key=key,
            summary=summary,
            member_forces=self.member_forces_df,
            reactions=self.reactions_df,
            boq=self.boq_df,
            utilization=self.utilization_df,
        )
        if self.member_forces_df.empty and self.reactions_df.empty:
            message += (" Note: no exported results were cached — run "
                        "export_member_forces / export_reactions (and solve "
                        "first) before storing to capture result data.")
        elif self.utilization_df.empty:
            message += (" Note: no utilization data cached — run "
                        "get_utilization_ratios before storing to capture "
                        "pass/fail information.")
        return {"status": "ok", "message": message, "key": key}

    def _tool_retrieve_result(self, key: str) -> dict:
        return {"status": "ok", "message": self.results.retrieve(key)}

    def _tool_list_stored_results(self) -> dict:
        return {"status": "ok", "message": self.results.list_results()}

    def _tool_clear_stored_results(self) -> dict:
        return {"status": "ok", "message": self.results.clear()}

    # ------------------------------------------------------------------ #
    # WP1 meta-layer: LLM-authored custom tools
    # ------------------------------------------------------------------ #

    def _tool_run_custom_script(self, code: str, purpose: str = "") -> dict:
        self._ensure_robot()  # scripts expect a live `robot`
        try:
            outcome = run_sandboxed(code, self.robot, timeout_s=120.0)
        except TimeoutError as exc:
            return {"status": "error", "message": str(exc)}
        except ScriptRejected as exc:
            return {"status": "error", "message": str(exc)}
        except RuntimeError as exc:
            # Script raised — return traceback so the LLM can self-correct.
            return {"status": "error", "message": str(exc),
                    "hint": "Fix the script and call run_custom_script again."}
        result = outcome["result"]
        if isinstance(result, pd.DataFrame):
            result = result.head(20).to_dict(orient="records")
        return {
            "status": "ok",
            "purpose": purpose,
            "result": result if result is not None else None,
            "stdout": outcome["stdout"][-40:],
        }

    def _tool_create_custom_tool(
        self, name: str, description: str, code: str,
        parameters: Optional[dict] = None,
    ) -> dict:
        message = self.custom_tools.register(
            name=name, description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            code=code,
        )
        ok = not message.startswith("Error")
        return {"status": "ok" if ok else "error", "message": message,
                "hint": ("Call it now with sample arguments to test it."
                         if ok else
                         "Adjust the name/code and retry.")}

    def _tool_list_custom_tools(self) -> dict:
        return {"status": "ok", "message": self.custom_tools.list_tools()}

    def _tool_delete_custom_tool(self, name: str) -> dict:
        message = self.custom_tools.delete(name)
        ok = not message.startswith("Error")
        return {"status": "ok" if ok else "error", "message": message}

    # ------------------------------------------------------------------ #
    # WP2/WP3: delete elements + save anywhere
    # ------------------------------------------------------------------ #

    def _tool_delete_bar(self, bar_id: int) -> dict:
        self._ensure_robot()
        message = self.robot.delete_bar(bar_id)
        return {"status": "ok", "message": message, "bar_id": bar_id}

    def _tool_delete_node(self, node_id: int) -> dict:
        self._ensure_robot()
        message = self.robot.delete_node(node_id)
        return {"status": "ok", "message": message, "node_id": node_id}

    def _tool_save_project(self, file_path: str) -> dict:
        self._ensure_robot()
        message = self.robot.save_project(file_path)
        return {"status": "ok", "message": message}

    def _tool_create_cylindrical_tank(
        self, radius: float = 2.5, height: float = 5.0,
        segments: int = 16, ring_levels: int = 2,
        section_vertical: str = "IPE 200", section_ring: str = "IPE 200",
    ) -> dict:
        self._ensure_robot()
        summary = self.robot.create_cylindrical_tank(
            radius=radius, height=height, segments=segments,
            ring_levels=ring_levels, section_vertical=section_vertical,
            section_ring=section_ring,
        )
        return {"status": "ok", **summary}

    # ------------------------------------------------------------------ #
    # WP6: rich result extraction (displacements / stresses / all-in-one Excel)
    # ------------------------------------------------------------------ #

    def _tool_export_node_displacements(self, case_id: int = 1) -> dict:
        self._ensure_robot()
        df = self.robot.export_node_displacements(case_id=case_id)
        self.displacements_df = df
        return {"status": "ok", "rows": len(df),
                "preview": df.head(10).to_dict(orient="records")}

    def _tool_export_bar_stresses(self, case_id: int = 1, divisions: int = 5) -> dict:
        self._ensure_robot()
        df = self.robot.export_bar_stresses(case_id=case_id, divisions=divisions)
        self.stresses_df = df
        return {"status": "ok", "rows": len(df),
                "preview": df.head(10).to_dict(orient="records")}

    def _tool_export_results_to_excel(
        self, file_name: str, sheets: List[str], case_id: int = 1,
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
            self.member_forces_df = self.robot.export_all_member_forces(
                case_id=case_id)
            available["member_forces"] = self.member_forces_df
        if "reactions" in sheets and self.reactions_df.empty:
            self.reactions_df = self.robot.export_reactions(case_id=case_id)
            available["reactions"] = self.reactions_df
        if "modal" in sheets and self.modal_frequencies_df.empty:
            self.modal_frequencies_df = self.robot.export_modal_frequencies(
                case_id=case_id)
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
        return {"status": "ok", "file_path": path,
                "sheets": list(picked.keys())}

    # ------------------------------------------------------------------ #
    # WP4: materials / panels (grillage approximation) / volumes
    # ------------------------------------------------------------------ #

    def _tool_set_material(
        self, material_name: str = "STEEL",
        e_mpa: Optional[float] = None, nu: Optional[float] = None,
        apply_to_bars: bool = True,
    ) -> dict:
        self._ensure_robot()
        return {"status": "ok", **self.robot.set_material(
            material_name=material_name, e_mpa=e_mpa, nu=nu,
            apply_to_bars=apply_to_bars)}

    def _tool_create_panel(
        self, panel_id: int, x: float = 0.0, y: float = 0.0, z: float = 0.0,
        width: float = 4.0, height: float = 3.0, normal: str = "Y",
        divisions_x: int = 4, divisions_z: int = 4,
        section: str = "IPE 100", diagonals: bool = False,
    ) -> dict:
        self._ensure_robot()
        return {"status": "ok", **self.robot.create_panel(
            panel_id=panel_id, x=x, y=y, z=z, width=width, height=height,
            normal=normal, divisions_x=divisions_x, divisions_z=divisions_z,
            section=section, diagonals=diagonals)}

    def _tool_set_panel_thickness(
        self, panel_id: int, thickness_m: float,
    ) -> dict:
        self._ensure_robot()
        return {"status": "ok", **self.robot.set_panel_thickness(
            panel_id=panel_id, thickness_m=thickness_m)}

    def _tool_apply_panel_pressure(
        self, panel_id: int, case_id: int = 1, pressure_kpa: float = -1.0,
    ) -> dict:
        self._ensure_robot()
        return {"status": "ok", **self.robot.apply_panel_pressure(
            panel_id=panel_id, case_id=case_id, pressure_kpa=pressure_kpa)}

    def _tool_create_solid(
        self, solid_id: int, node_ids: List[int],
        face_groups: List[List[int]],
    ) -> dict:
        self._ensure_robot()
        return {"status": "ok", **self.robot.create_solid(
            solid_id=solid_id, node_ids=node_ids, face_groups=face_groups)}

    def _tool_create_solid_box(
        self, solid_id: int, origin_x: float = 0.0, origin_y: float = 0.0,
        origin_z: float = 0.0, size_x: float = 1.0, size_y: float = 1.0,
        size_z: float = 1.0,
    ) -> dict:
        self._ensure_robot()
        return {"status": "ok", **self.robot.create_solid_box(
            solid_id=solid_id, origin_x=origin_x, origin_y=origin_y,
            origin_z=origin_z, size_x=size_x, size_y=size_y, size_z=size_z)}

    # ------------------------------------------------------------------ #
    # WP7: modal analysis
    # ------------------------------------------------------------------ #

    def _tool_solve_modal(
        self, case_id: int = 1, n_modes: int = 10, timeout_s: int = 150,
    ) -> dict:
        self._ensure_robot()
        return {"status": "ok", **self.robot.solve_modal(
            case_id=case_id, n_modes=n_modes, timeout_s=timeout_s)}

    def _tool_export_modal_frequencies(
        self, case_id: int = 1, n_modes: int = 10,
    ) -> dict:
        self._ensure_robot()
        df = self.robot.export_modal_frequencies(case_id=case_id,
                                                 n_modes=n_modes)
        self.modal_frequencies_df = df
        if df.empty:
            return {"status": "ok", "rows": 0,
                    "note": "No modal results exist yet — the RobotOM modal "
                            "solver does not complete programmatically in "
                            "this build. Run modal analysis in the Robot GUI "
                            "and retry, or solve_modal to see the honest "
                            "status."}
        return {"status": "ok", "rows": len(df),
                "preview": df.head(10).to_dict(orient="records")}

    def _tool_export_modal_mode_shapes(
        self, case_id: int = 1, mode_num: int = 1,
    ) -> dict:
        self._ensure_robot()
        df = self.robot.export_modal_mode_shapes(case_id=case_id,
                                                 mode_num=mode_num)
        return {"status": "ok", "rows": len(df),
                "preview": df.head(10).to_dict(orient="records")}

    def _call_custom_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Executes a registered custom tool via dispatch()."""
        try:
            self._ensure_robot()
            outcome = self.custom_tools.call(tool_name, arguments, self.robot,
                                             timeout_s=120.0)
        except TimeoutError as exc:
            return json.dumps({"status": "error", "tool": tool_name,
                               "message": str(exc)})
        except (RuntimeError, KeyError) as exc:
            return json.dumps({"status": "error", "tool": tool_name,
                               "message": str(exc),
                               "hint": "Fix the custom tool (delete + "
                                       "re-register) and retry."})
        result = outcome["result"]
        if isinstance(result, pd.DataFrame):
            result = {"preview": result.head(20).to_dict(orient="records"),
                      "rows": len(result)}
        payload = {"status": "ok", "tool": tool_name, "result": result}
        if outcome["stdout"]:
            payload["stdout_tail"] = outcome["stdout"][-15:]
        return json.dumps(payload, default=str)
