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
from tools.section_sizing import check_section_proportions
from tools.ltb_check import check_lateral_torsional_buckling
from tools.eurocode_members import check_eurocode_members
from tools.word_tool import WordReporter
from tools.pptx_tool import PowerPointReporter
from tools.result_store import ResultStore
from tools.section_sizing import available_sections, section_families
from tools.diagram_tool import plot_structure_wireframe
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
from batch.surrogate_search import (
    run_surrogate_search,
    should_use_grid,
    SurrogateSearchError,
    DEFAULT_BUDGET as SURROGATE_DEFAULT_BUDGET,
    DEFAULT_PATIENCE as SURROGATE_DEFAULT_PATIENCE,
    ACQUISITION_MODES,
)
from batch.export_candidate import export_best_from_run

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

ALLOWED_EXTENSIONS = {".xlsx", ".docx", ".pptx", ".png", ".pdf", ".rtd"}


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
                    "description": "Catalog section name, e.g. 'IPE 300', 'HEA 200', 'HEB 300', 'W 12X26'. Unspaced forms like 'IPE300' are auto-corrected. SCALE-AWARE: pick a depth ~= span/18 for beams and ~= height/25 for columns; prefer the create_* template tools, which auto-size sections from the span when none is given.",
                },
            },
            "required": ["bar_id", "start_node", "end_node", "section_name"],
        },
    },
    {
        "name": "set_support",
        "description": "Applies a boundary condition / support to a node. Types: fixed / pinned / roller_x / roller_z (unchanged behaviour), plus 'spring' — an elastic-linear spring support that requires spring_stiffness, a dict of DOF -> stiffness ({UX/UY/UZ} in kN/m, {RX/RY/RZ} in kNm/rad), e.g. {\"UZ\": 100000.0}.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer"},
                "support_type": {
                    "type": "string",
                    "enum": ["fixed", "pinned", "roller_x", "roller_z",
                             "spring"],
                    "default": "fixed",
                },
                "spring_stiffness": {
                    "type": "object",
                    "description": "Required when support_type='spring': DOF -> stiffness (UX/UY/UZ in kN/m, RX/RY/RZ in kNm/rad). Ignored for the other types.",
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
        "description": "Runs the Robot FEA solver on the current model. Must be called before exporting any results. For any MANUALLY-BUILT (non-template) structure, call check_model_stability FIRST to confirm the model is not a mechanism before solving (a mechanism triggers Robot's instability modal).",
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
        "description": "Builds a complete structure in one call from a JSON spec: project type, nodes, bars (sections), supports, load cases, and loads (uniform / concentrated / nodal). Prefer this tool for large or complex models. Example: {\"project\":\"3D\",\"nodes\":[{\"id\":1,\"x\":0,\"y\":0,\"z\":0}],\"bars\":[{\"id\":1,\"n1\":1,\"n2\":2,\"section\":\"IPE 300\"}],\"supports\":[{\"node\":1,\"type\":\"pinned\"}],\"cases\":[{\"id\":1,\"name\":\"DL\",\"nature\":\"permanent\"}],\"loads\":[{\"kind\":\"bar_uniform\",\"bar\":1,\"case\":1,\"direction\":\"Z\",\"value\":-10}]}. RELIABILITY: if the spec would exceed ~20 bars, DO NOT hand-type one giant JSON block - build INCREMENTS with smaller sub-specs (create_structure_from_spec for each sub-model's nodes/bars, or create_node/create_bar in loops). Long hand-typed single-shot JSON is the #1 reliability ceiling: one missing ':' delimiter fails the whole call. Before using any non-IPE/HEA/HEB section name, call list_available_sections to get an exact catalog name. For ANY shape that is not one of the named templates (create_truss / create_arch_truss / create_braced_frame / create_rectangular_grid_frame / create_cylindrical_tank), use compose_structure instead of hand-writing node/bar JSON: it builds twin arches, twin trusses, cable-stayed decks and other assemblies from verified primitives, and it auto-numbers nodes/bars so you never compute ids.",
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
        "name": "compose_structure",
        "description": "Builds an ARBITRARY structure by composing verified geometry primitives (no hand-written node/bar JSON, no per-shape bespoke tool). State persists ACROSS calls in this session: call it once per step, then action='finish' to get the assembled geometry (then pass it to create_structure_from_spec). Ops: chord (straight|arc), web (pratt|warren between two chains), bracing (cross|transverse BETWEEN two parallel planes - the twin-arch/twin-truss case), copy (mirror a chain into a second plane via y_shift), support (pinned/fixed/roller on a chain's ends or explicit nodes). Each op validates immediately (unknown chain names, mismatched panel counts, bad y_shift, duplicate names all fail with an actionable error, NOT deferred to the end). RELIABILITY: for assemblies with more than ~5-6 steps, call this tool ONCE PER STEP (action='step', step={...}) - do NOT pack a giant steps array into one call (hand-typed JSON has a reliability ceiling: one missing ':' fails the whole call). For small assemblies (<=5-6 steps) action='batch' with steps=[...] is fine. Example twin-arch: step1 chord name=arch_a kind=arc span=30 rise=5 panels=10 section='IPE 500'; step2 chord name=deck_a kind=straight span=30 panels=10 elevation=0 section='IPE 500'; step3 web top=arch_a bottom=deck_a pattern=pratt web_section='L 60x60x6'; step4 copy source=arch_a name=arch_b y_shift=6; step5 copy source=deck_a name=deck_b y_shift=6; step6 web top=arch_b bottom=deck_b pattern=pratt web_section='L 60x60x6'; step7 bracing plane_a=arch_a plane_b=arch_b pattern=cross section='L 50x50x5'; step8 bracing plane_a=deck_a plane_b=deck_b pattern=cross section='L 50x50x5'; step9 support chain=deck_a type=pinned; step10 support chain=deck_b type=pinned; action='finish'. Before using any non-IPE/HEA/HEB section name, call list_available_sections first.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["step", "batch", "finish", "reset"],
                    "description": "'step' = apply ONE step (one per call for >5-6 step assemblies); 'batch' = apply a small steps list in one call; 'finish' = assemble the full geometry + run integrity checks and return it (clears the session registry); 'reset' = discard the in-progress composition.",
                },
                "step": {
                    "type": "object",
                    "description": "Required when action='step': {op: 'chord'|'web'|'bracing'|'copy'|'support', ...} — see the tool description for per-op fields.",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Required when action='batch': a SMALL list (<=5-6) of step dicts.",
                },
            },
            "required": ["action"],
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
        "description": "Builds a 3D rectangular grid moment frame: multi-level, multi-bay columns with floor beams and pinned column bases. SCALE-AWARE: sections AUTO-SIZE from the bay widths / storey height when not specified (beams ~span/18, columns ~height/25) — do NOT reuse sections from other models with different spans.",
        "parameters": {
            "type": "object",
            "properties": {
                "levels": {"type": "integer", "default": 2},
                "bays_x": {"type": "integer", "default": 2},
                "bays_y": {"type": "integer", "default": 2},
                "bay_width_x": {"type": "number", "default": 5.0},
                "bay_width_y": {"type": "number", "default": 5.0},
                "level_height": {"type": "number", "default": 3.5},
                "column_section": {"type": "string", "description": "Optional explicit column section; if omitted an H-family section is auto-sized from the storey height."},
                "beam_x_section": {"type": "string", "description": "Optional explicit X-beam section; if omitted an IPE is auto-sized from bay_width_x."},
                "beam_y_section": {"type": "string", "description": "Optional explicit Y-beam section; if omitted an IPE is auto-sized from bay_width_y."},
            },
            "required": [],
        },
    },
    {
        "name": "create_truss",
        "description": "Builds a planar Pratt truss (top/bottom chords, verticals, diagonals) pinned at both ends. SCALE-AWARE: sections AUTO-SIZE from the span when not specified (chords ~span/18, light angle web) — a 1m truss should NOT use the same sections as a 30m truss.",
        "parameters": {
            "type": "object",
            "properties": {
                "span": {"type": "number", "default": 12.0},
                "height": {"type": "number", "default": 2.0},
                "panels": {"type": "integer", "default": 6},
                "top_section": {"type": "string", "description": "Optional explicit top-chord section; if omitted it is auto-sized from the span."},
                "bottom_section": {"type": "string", "description": "Optional explicit bottom-chord section; if omitted it is auto-sized from the span."},
                "web_section": {"type": "string", "description": "Optional explicit web (vertical/diagonal) section; if omitted a light angle is auto-sized. For any NON-IPE/HEA/HEB section, FIRST call list_available_sections(family='L') to get the exact catalog spelling (e.g. 'L 50x50x5'), then pass that here - guessing an angle name like 'L 120x120x5' from memory is what caused catalog-miss failures."},
            },
            "required": [],
        },
    },
    {
        "name": "create_braced_frame",
        "description": "Builds a single-bay braced frame (two columns, one beam, one diagonal brace) with pinned bases. SCALE-AWARE: sections AUTO-SIZE when not specified (columns ~height/25, beam ~span/18, brace on the diagonal length).",
        "parameters": {
            "type": "object",
            "properties": {
                "height": {"type": "number", "default": 6.0},
                "width": {"type": "number", "default": 6.0},
                "column_section": {"type": "string", "description": "Optional explicit column section; if omitted auto-sized from the height."},
                "beam_section": {"type": "string", "description": "Optional explicit beam section; if omitted auto-sized from the width."},
                "brace_section": {"type": "string", "description": "Optional explicit brace section; if omitted auto-sized from the diagonal length. For any NON-IPE/HEA/HEB section, FIRST call list_available_sections(family='L') to get the exact catalog spelling, then pass that here."},
            },
            "required": [],
        },
    },
    {
        "name": "create_arch_truss",
        "description": "Builds a planar arch truss in the X-Z plane (bowstring: arched top chord + straight bottom chord, or inverted: arched bottom chord + straight top deck) with circular-arc geometry and a Pratt web, pinned at both ends. SCALE-AWARE: sections AUTO-SIZE from the span when not specified (chords ~span/18, light angle web).",
        "parameters": {
            "type": "object",
            "properties": {
                "span": {"type": "number", "default": 30.0, "description": "Arch span in meters."},
                "rise": {"type": "number", "default": 5.0, "description": "Arch rise at mid-span in meters (span/rise ~ 4-8 typical)."},
                "panels": {"type": "integer", "default": 10, "description": "Number of panels along each chord."},
                "top_section": {"type": "string", "description": "Optional explicit top-chord section; if omitted auto-sized from the span."},
                "bottom_section": {"type": "string", "description": "Optional explicit bottom-chord section; if omitted auto-sized from the span."},
                "web_section": {"type": "string", "description": "Optional explicit web section; if omitted a light angle is auto-sized. For any NON-IPE/HEA/HEB section, FIRST call list_available_sections(family='L') to get the exact catalog spelling (e.g. 'L 50x50x5') - guessing angle names from memory caused catalog-miss failures."},
                "arch_chord": {"type": "string", "enum": ["top", "bottom"], "default": "top", "description": "'top' = bowstring (arch on top, straight deck at z=0); 'bottom' = arch below with a straight deck above at z=rise."},
            },
            "required": [],
        },
    },
    {
        "name": "check_section_proportions",
        "description": "Pure offline sanity check (no Robot needed): flags bars whose span/depth ratio is far outside structural norms (beams/chords ~10-25, columns ~8-40). Pass any structure spec dict (the same {nodes, bars} shape the create_* template tools accept) — useful to validate a hand-built spec BEFORE building it, or to review the auto-sized sections of a just-built model.",
        "parameters": {
            "type": "object",
            "properties": {
                "spec": {"type": "object", "description": "Structure spec dict: {\"nodes\": [{\"id\",\"x\",\"y\",\"z\"}], \"bars\": [{\"id\",\"n1\",\"n2\",\"section\"}]}."},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "set_bracing",
        "description": "[EUROCODE] Defines member unbraced lengths / bracing points for a bar (Robot has no such property — this is an explicit engineer-input layer). lcr_y / lcr_z / lcr_lt are buckling lengths in meters. When unset they default to the FULL bar length, which is a CONSERVATIVE assumption and is explicitly warned — a default is not a verified bracing condition. brace_points are intermediate bracing positions as fractions of the bar length in [0,1] (e.g. [0.5] = a purlin at mid-span) and shorten lcr_lt only. Negative lengths are rejected; lengths > 2.5x the bar length are flagged as suspicious K-factors.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer", "description": "Existing bar id."},
                "lcr_y": {"type": "number", "description": "Major-axis buckling length (m); default = full bar length (conservative)."},
                "lcr_z": {"type": "number", "description": "Minor-axis buckling length (m); default = full bar length (conservative)."},
                "lcr_lt": {"type": "number", "description": "Lateral-torsional unbraced length (m); default = longest sub-span from brace_points, else full bar length."},
                "brace_points": {"type": "array", "items": {"type": "number"}, "description": "Intermediate bracing positions as fractions of bar length in [0,1], e.g. [0.5] for a mid-span purlin. Shortens lcr_lt only."},
            },
            "required": ["bar_id"],
        },
    },
    {
        "name": "get_bracing",
        "description": "[EUROCODE] Returns the resolved bracing/unbraced-length data for one bar (or all bars): each Lcr value with its source ('explicit', 'brace_points', or 'defaulted' = conservative full-length assumption) plus any warnings, so results are traceable to what was actually specified vs assumed.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer", "description": "Optional bar id; omit to list all bars."},
            },
            "required": [],
        },
    },
    {
        "name": "check_lateral_torsional_buckling",
        "description": "[EUROCODE] Per-member lateral-torsional buckling + beam-column interaction check (EN 1993-1-1 §6.3.2.2 general method + §6.3.3 eqs. 6.61/6.62 with Annex B factors) for a SOLVED case. Doubly-symmetric rolled I-sections only (ShapeType-verified); It/Iw computed from live geometry; C1 from the exported moment shape (ENV Annex F); §6.3.2.3 NOT implemented. Class 4 / non-I sections return NOT_CHECKABLE with a stated reason. Lcr_LT (and Lcr_y/z for the interaction part) come from set_bracing; unset lengths default to the FULL bar length with an explicit warning — a default is not a verified bracing condition.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1, "description": "Solved case/combination id."},
                "bar_ids": {"type": "array", "items": {"type": "integer"}, "description": "Optional bar ids to check; omit to check all bars."},
            },
            "required": [],
        },
    },
    {
        "name": "define_connection",
        "description": "[EUROCODE] Defines a SIMPLE SHEAR connection at a bar end (Robot has no connection-design server — this is an explicit engineer-input layer). Types: fin_plate (single shear) / double_angle (double shear) / end_plate (single shear). EN 1993-1-8 bolt (Table 3.4), block shear (§3.10.2) and fillet-weld (§4.5.3) checks apply when check_connection_capacity runs. v1 supports a single column of bolts (bolt_columns=1) and no moment connections / base plates.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer", "description": "Existing bar id."},
                "joint_end": {"type": "string", "enum": ["start", "end"], "default": "end", "description": "Which bar end the connection is at."},
                "connection_type": {"type": "string", "enum": ["fin_plate", "double_angle", "end_plate"], "default": "fin_plate"},
                "bolt_grade": {"type": "string", "enum": ["4.6", "5.6", "8.8", "10.9"], "default": "8.8"},
                "bolt_diameter": {"type": "number", "default": 20, "description": "Bolt diameter in mm."},
                "bolt_rows": {"type": "integer", "default": 2, "description": "Number of bolts in the vertical line."},
                "pitch_mm": {"type": "number", "default": 60, "description": "p1 vertical bolt pitch (mm)."},
                "edge_dist_mm": {"type": "number", "default": 30, "description": "e2 side edge distance (mm)."},
                "end_dist_mm": {"type": "number", "default": 30, "description": "e1 end distance (mm)."},
                "plate_thickness": {"type": "number", "default": 10, "description": "Fin/end plate thickness (mm)."},
                "plate_grade": {"type": "string", "enum": ["S235", "S275", "S355", "S460"], "default": "S275"},
                "weld_leg_mm": {"type": "number", "description": "Fillet weld leg size (mm) if the connection is welded; omitted = bolted."},
            },
            "required": ["bar_id"],
        },
    },
    {
        "name": "check_connection_capacity",
        "description": "[EUROCODE] Checks a DEFINED simple shear connection (define_connection first) against the solved end shear at the joint (EN 1993-1-8 §3 bolts, §3.10.2 block shear, §4.5.3 fillet welds). Reports the governing failure mode (bolt shear / bearing / block shear / weld) with PASS/FAIL/NOT_CHECKABLE. Bearing on the beam web uses the live section web thickness; web-bearing is skipped honestly if the member grade is not an EN grade.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer", "description": "Existing bar id with a defined connection."},
                "joint_end": {"type": "string", "enum": ["start", "end"], "default": "end"},
                "case_id": {"type": "integer", "default": 1, "description": "Solved case/combination id."},
            },
            "required": ["bar_id"],
        },
    },
    {
        "name": "check_eurocode_members",
        "description": "[EUROCODE Phase E] Worst-governing per-bar verdict across ALL checks for a SOLVED case: elastic utilization, minor-axis Euler buckling, lateral-torsional buckling (§6.3.2.2 + §6.3.3 interaction), and defined simple-shear connections (EN 1993-1-8). Each bar reports overall_status (FAIL > NOT_CHECKABLE > PASS) with the governing check named, and the four individual sub-results. NOT_CHECKABLE means 'not certified' (never a silent pass).",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "default": 1, "description": "Solved case/combination id."},
                "bar_ids": {"type": "array", "items": {"type": "integer"}, "description": "Optional bar ids to check; omit to check all bars."},
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
                "section_vertical": {"type": "string", "description": "Optional explicit section for the vertical columns; if omitted auto-sized from the tank height."},
                "section_ring": {"type": "string", "description": "Optional explicit section for the circumferential ring beams; if omitted auto-sized from the tank diameter."},
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
                "section": {"type": "string", "description": "Optional explicit grillage-bar section; if omitted auto-sized from the panel's smaller plan dimension."},
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
        "description": "[P7 BATCH OPTIMIZER] Validates a design-space spec and estimates the run WITHOUT starting it. Translate the user's natural-language brief into the DesignSpace JSON schema: {\"geometry\": {...same as create_structure_from_spec's spec: nodes/bars with section/supports/cases/loads...}, \"variable_groups\": [{\"group_name\": \"columns\", \"bar_ids\": [1,3], \"candidate_sections\": [\"HEA 200\",\"HEA 220\"]}, ...], \"load_cases\": [{\"id\":1,\"name\":\"DL\",\"nature\":\"permanent\"}], \"analysis_types\": [\"static\"], \"objective\": {\"minimize\": \"weight\", \"constraint\": \"max_utilization <= 1.0 AND buckling_pass == True\"}}. HARD RULE: this tool NEVER starts a run - not ever, under ANY phrasing. It only validates + returns the candidate count, time estimate and a run_config_id. Do NOT call confirm_and_start_optimization_run in this same response, even if the user said 'just run it', 'go ahead', 'start it', 'yes do it', or anything that sounds like permission. A batch run consumes Robot license time, so confirmation ALWAYS requires a SEPARATE, LATER message from the user AFTER they have seen and approved this estimate. Your next reply after this tool must present the count + estimate to the user and ask for explicit confirmation - then STOP and wait for their next message. If you find yourself wanting to call confirm_and_start_optimization_run in this same turn, STOP: that is a violation. For LARGE design spaces (hundreds to thousands of candidates) where an exhaustive grid would be too slow, use start_surrogate_search_run instead. When the user wants to OPTIMIZE AN ALREADY-BUILT model, call export_structure_spec FIRST and use its returned 'geometry' verbatim as spec.geometry (do NOT retype geometry from memory).",
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
    {
        "name": "start_surrogate_search_run",
        "description": "[P7 BATCH OPTIMIZER - SURROGATE] Validates a design-space spec for SURROGATE-GUIDED sizing search (batch/surrogate_search.py) and estimates the run WITHOUT starting it. Use this INSTEAD of start_optimization_run when the design space is LARGE (hundreds to thousands of candidates) and an exhaustive grid search would be too slow: the surrogate spends at most 'budget' Robot calls (default 300), and every proposed candidate is still really built/solved/checked in Robot through the same HeadlessSession path with the same Eurocode/buckling gates - only the SELECTION is model-guided (a Gaussian process trained on past runs). Same DesignSpace JSON 'spec' schema as start_optimization_run. If the grid is small enough that exhaustive search is cheaper, this tool says so and you MUST use start_optimization_run instead. HARD RULE: this tool NEVER starts a run - not ever, under ANY phrasing. It only validates + returns the candidate count, Robot-call budget estimate and a run_config_id. Do NOT call confirm_and_start_surrogate_search_run in this same response, even if the user said 'just run it' - a batch run consumes Robot license time, so confirmation ALWAYS requires a SEPARATE, LATER message from the user after they have seen and approved the estimate.",
        "parameters": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": "DesignSpace JSON - same schema as start_optimization_run (geometry + variable_groups + load_cases + analysis_types + objective).",
                },
                "budget": {"type": "integer", "description": "Max Robot calls the surrogate search may spend (default 300).", "default": 300},
                "patience": {"type": "integer", "description": "Stop after this many consecutive proposals that fail to improve the Pareto frontier (default 10).", "default": 10},
                "acquisition": {"type": "string", "enum": ["ucb", "ehvi"], "description": "Acquisition function: 'ucb' (upper confidence bound on utilization - the current default) or 'ehvi' (expected hypervolume improvement over the Pareto frontier).", "default": "ucb"},
                "kappa": {"type": "number", "description": "Exploration weight for the 'ucb' acquisition (default 2.0).", "default": 2.0},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "confirm_and_start_surrogate_search_run",
        "description": "[P7 BATCH OPTIMIZER - SURROGATE] STARTS a surrogate-guided batch optimization in the background (does not block the chat). Only call this AFTER the user has explicitly confirmed the candidate count + Robot-call budget estimate returned by start_surrogate_search_run - never start a batch run without explicit user confirmation (it consumes Robot license time). Pass the run_config_id returned by start_surrogate_search_run. Returns the run_id immediately; poll with check_optimization_status and read results with get_optimization_results (the same tools used for grid runs - they work for any run_id).",
        "parameters": {
            "type": "object",
            "properties": {
                "run_config_id": {"type": "string", "description": "The run_config_id returned by start_surrogate_search_run."},
            },
            "required": ["run_config_id"],
        },
    },
    {
        "name": "export_best_design",
        "description": "[P7 BATCH OPTIMIZER] Materializes the lightest PASSING candidate of a COMPLETED optimization run as a real Robot project (.rtd) saved into the generated/ directory, so the user can open and inspect the winning design in Robot. The run was previously started via confirm_and_start_optimization_run or confirm_and_start_surrogate_search_run, and check_optimization_status shows 'completed'. 'frontier_index' picks a different Pareto-frontier candidate (0 = the lightest passing one - the default; frontier is ranked by weight ascending with the same hard pass_fail gate as get_optimization_results). The candidate is built, solved and saved in its OWN Robot instance (HeadlessSession, visible by default) - note the one-seat license caveat if an interactive Robot is already open.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer", "description": "The run_id of a completed optimization run."},
                "file_name": {"type": "string", "description": "Output file name (e.g. 'winner' or 'winner.rtd'); saved into the generated/ directory with .rtd appended if missing."},
                "frontier_index": {"type": "integer", "description": "Index into the Pareto frontier sorted by weight ascending; 0 = lightest passing (default).", "default": 0},
                "visible": {"type": "boolean", "description": "Open Robot visibly while building/solving/saving (default true - the user wants to look at it).", "default": True},
            },
            "required": ["run_id", "file_name"],
        },
    },
    {
        "name": "export_structure_spec",
        "description": "Reads the LIVE model (nodes, bars with sections, supports, load cases, loads) and returns it as the same JSON 'geometry' object that create_structure_from_spec / start_optimization_run accept — the reverse of building from a spec. Use this BEFORE start_optimization_run when optimizing an already-built model: call export_structure_spec and pass its output verbatim as spec.geometry instead of retyping geometry from memory.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_available_sections",
        "description": "Returns valid catalog section names (e.g. 'IPE 300', 'HEA 200') that the geometry templates and optimizer draw from, optionally filtered by family (IPE / HEA / HEB / HEM / IPN / UPN / UPE / L). Catalog-only and fast — no Robot solve needed. Use this to pick realistic candidate_sections for an optimization design space or a section for a bar. IMPORTANT for family='L' (angles): the list returns LEG sizes (e.g. 'L 120'); Robot's catalog resolves the FULL equal-angle name 'L <leg>x<leg>x<thickness>', e.g. 'L 60x60x6' — a thin t=5 is not available on every leg (120x120x5 does NOT exist). Prefer the template auto web sizing (create_truss without web_section) or build the full name from a leg here with a standard thickness (5 for legs<=60, 6 for <=100, 8 for <=120, 10/12 above) and verify.",
        "parameters": {
            "type": "object",
            "properties": {
                "family": {"type": "string", "description": "Optional section family filter (case-insensitive), e.g. 'IPE', 'HEA', 'HEB', 'L'. Omit for all families."},
            },
            "required": [],
        },
    },
    {
        "name": "apply_self_weight",
        "description": "Applies every bar's self-weight in the given load case (global -Z) in one call — each bar's weight (section unit mass x length x g) lumped 50/50 to its two end nodes (classic truss lumping). VERIFIED: sum(FZ) reactions equals the reported total exactly (0.00% on the live 138-bar 3D twin-arch; the previous per-bar uniform-load write silently under-applied ~15.7% on 3D assemblies and was replaced). One call instead of manually computing N loads.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "description": "The load case to apply self-weight into (must already exist)."},
                "density": {"type": "number", "description": "Steel density in kg/m3 (default 7850).", "default": 7850},
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "preview_structure_geometry",
        "description": "Renders a simple wireframe image (PNG in generated/) of the current node/bar geometry — axonometric for 3D models, dominant X-Z plane for planar ones — so a person can sanity-check span/height/panel count BEFORE committing to a solve or an optimization run. Uses the in-memory geometry only (no Robot COM needed).",
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "Output PNG file name (default 'structure_geometry.png').", "default": "structure_geometry.png"},
            },
            "required": [],
        },
    },
    {
        "name": "check_model_stability",
        "description": "Runs the mechanism pre-solve check on the CURRENT model - the same 2D rank check batch/runner.py runs before every candidate solve. Call any time after nodes/bars/supports are built and BEFORE solve. Returns ok/mechanism-detected with the rank info and the nodes/DOFs involved in any nullspace. If mechanism=True, fix supports/geometry first - solving a mechanism triggers Robot's instability modal.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "robot_session_status",
        "description": "[DIAG] Returns the authoritative Robot session picture: which robot.exe PID this bridge is connected to, HOW it connected (attach vs fresh launch), who owns the cross-process seat, and which robot.exe processes are live on the machine. Call this FIRST whenever Robot behaves oddly - stale bar ids ('Bar N not found' right after a build), RPC drops, phantom dialogs - because a SPLIT SESSION (two live COM handles on one robot.exe) shows up here immediately instead of taking a dozen failing tool calls to diagnose.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "generate_code_combinations",
        "description": "Auto-generates the standard EN 1990 load combination set from the currently defined load cases (using their 'nature': permanent / imposed). ULS = 1.35 x permanent + 1.5 x leading variable + 1.05 (1.5*0.7) x each other variable (one ULS per variable case as leading); SLS characteristic = 1.0 x all. Calls the existing define_combination for each - manual define_combination is unchanged; this is a convenience layer on top. Requires at least one simple load case.",
        "parameters": {
            "type": "object",
            "properties": {
                "combination_set": {"type": "string", "enum": ["ULS_SLS_basic", "ULS_only", "SLS_only"], "description": "Which combinations to generate (default 'ULS_SLS_basic' = ULS + SLS characteristic).", "default": "ULS_SLS_basic"},
            },
            "required": [],
        },
    },
    {
        "name": "compare_topologies",
        "description": "Sizes SEVERAL named topology variants (truss / arch truss / braced frame / rectangular grid frame) under the SAME load spec through the existing optimizer machinery and returns a ranked comparison - lightest passing design per topology, one call instead of manually repeating the build/optimize workflow per candidate. Each variant runs as its own batch run (run_id returned); may take minutes (each variant is a small grid search). Use export_best_design to materialize a winner afterwards.",
        "parameters": {
            "type": "object",
            "properties": {
                "variants": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "generator": {"type": "string", "enum": ["create_truss", "create_arch_truss", "create_braced_frame", "create_rectangular_grid_frame"]}, "generator_args": {"type": "object"}}}, "description": "List of {name, generator, generator_args} variants to size and compare."},
                "load_spec": {"type": "object", "description": "The SAME load spec applied to every variant: {cases: [{id,name,nature}], loads: [{kind,bar,case,direction,value}...]} (the geometry spec's cases/loads keys)."},
                "objective": {"type": "object", "description": "Optional DesignSpace objective dict (default: minimize weight with max_utilization <= 1.0 AND buckling_pass == True)."},
                "budget": {"type": "integer", "description": "Optional Robot-call budget for large variant grids (default 300)."},
            },
            "required": ["variants", "load_spec"],
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

        # [COMPOSE] Session-scoped geometry-composition state (verified
        # primitives only; see _tool_compose_structure). Persists across
        # chat turns via the ToolExecutor held in st.session_state.
        self._compose_chains: Dict[str, dict] = {}
        self._compose_bars: List[dict] = []
        self._compose_supports: List[dict] = []
        self._compose_next_id: int = 1

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
        self, bar_id: int, start_node: int, end_node: int, section_name: str
    ) -> dict:
        self._ensure_robot()
        self.robot.create_bar(bar_id, start_node, end_node, section_name)
        return {"status": "ok", "bar_id": bar_id, "section": section_name}

    def _tool_set_support(self, node_id: int, support_type: str = "fixed",
                          spring_stiffness: dict = None) -> dict:
        self._ensure_robot()
        if support_type == "spring" and not spring_stiffness:
            raise ToolExecutionError(
                "support_type='spring' requires spring_stiffness, e.g. "
                "{'UZ': 100000.0} (UX/UY/UZ in kN/m, RX/RY/RZ in kNm/rad).")
        self.robot.set_support(node_id, support_type,
                               spring_stiffness=spring_stiffness)
        return {"status": "ok", "node_id": node_id,
                "support_type": support_type,
                "spring_stiffness": spring_stiffness}

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
                "mechanism, and fix supports/geometry if it is.")
        return out

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
        if cfg is None or cfg.get("kind") == "surrogate":
            raise ToolExecutionError(
                f"run_config_id '{run_config_id}' is not a staged grid-run "
                "config (call start_optimization_run first; surrogate runs "
                "use confirm_and_start_surrogate_search_run).")
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

    def _tool_start_surrogate_search_run(
        self, spec: dict,
        budget: int = SURROGATE_DEFAULT_BUDGET,
        patience: int = SURROGATE_DEFAULT_PATIENCE,
        acquisition: str = "ucb",
        kappa: float = 2.0,
    ) -> dict:
        """Validates a DesignSpace spec for surrogate-guided sizing search
        and estimates the run WITHOUT starting it (same staged-confirmation
        discipline as start_optimization_run). NEVER starts here."""
        if not spec or not isinstance(spec, dict):
            raise ToolExecutionError(
                "start_surrogate_search_run requires a DesignSpace JSON "
                "'spec' object (geometry + variable_groups + load_cases + "
                "analysis_types + objective). See the schema description.")
        try:
            budget = int(budget)
            patience = int(patience)
            kappa = float(kappa)
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError(
                f"budget/patience/kappa must be numeric: {exc}") from exc
        acquisition = str(acquisition or "ucb").lower()
        try:
            if budget < 1 or patience < 1 or kappa < 0.0:
                raise SurrogateSearchError(
                    "budget >= 1, patience >= 1, kappa >= 0 required")
            if acquisition not in ACQUISITION_MODES:
                raise SurrogateSearchError(
                    f"acquisition must be one of {ACQUISITION_MODES}")
            ds = DesignSpace(spec)
            ds.generate_candidates()  # validates grid <= cap (Phase 4 errors)
        except (DesignSpaceError, SurrogateSearchError) as exc:
            raise ToolExecutionError(
                f"Invalid surrogate-search design space or parameters: "
                f"{exc}. Fix the spec and retry.") from exc

        # The surrogate auto-falls back to exhaustive grid search when the
        # grid is small enough to be cheaper - refuse to stage a run that
        # would immediately do that; grid search is exact and preferable.
        use_grid, reason = should_use_grid(ds, budget)
        if use_grid:
            return {
                "status": "grid_recommended",
                "candidate_count": ds.candidate_count(),
                "budget": budget,
                "reason": reason,
                "message": (
                    "This design space is small enough that exhaustive grid "
                    "search is cheaper and exact. Do NOT call "
                    "confirm_and_start_surrogate_search_run - use "
                    "start_optimization_run / confirm_and_start_optimization_run "
                    "instead for this spec."),
            }

        # Conservative estimate from Phase-1 T1 timing (5-11 s/candidate);
        # the surrogate spends at most `budget` Robot calls.
        lo_s, hi_s = budget * 5, budget * 11
        cfg_id = f"surr_cfg_{int(time.time())}"
        self._optimization_configs[cfg_id] = {
            "kind": "surrogate", "spec": spec,
            "budget": budget, "patience": patience,
            "acquisition": acquisition, "kappa": kappa,
            "created": time.time(),
        }
        return {
            "status": "not_started",
            "run_config_id": cfg_id,
            "candidate_count": ds.candidate_count(),
            "budget": budget,
            "patience": patience,
            "acquisition": acquisition,
            "kappa": kappa,
            "estimate_seconds_min": lo_s,
            "estimate_seconds_max": hi_s,
            "estimate": (f"up to {budget} Robot calls, roughly "
                         f"{lo_s // 60}-{hi_s // 60} min (5-11 s/call, "
                         f"reused Robot session)"),
            "message": ("Run NOT started. Show the user the estimate, get "
                        "explicit confirmation, then call "
                        "confirm_and_start_surrogate_search_run with this "
                        "run_config_id. HARD RULE: never start in this same "
                        "turn."),
        }

    def _tool_confirm_and_start_surrogate_search_run(
        self, run_config_id: str,
    ) -> dict:
        """Starts a staged surrogate search in a background thread and
        returns immediately with the run_id (same shape as the grid path).
        Only surrogate configs (kind == 'surrogate') can be started."""
        cfg = self._optimization_configs.pop(run_config_id, None)
        if cfg is None or cfg.get("kind") != "surrogate":
            raise ToolExecutionError(
                f"run_config_id '{run_config_id}' is not a staged surrogate "
                "config (call start_surrogate_search_run first).")
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
                f"Could not stage surrogate run: {exc}") from exc
        finally:
            st.close()

        import threading

        holder: Dict[str, Any] = {"run_id": run_id, "error": None}
        t = threading.Thread(
            target=self._run_surrogate_worker,
            args=(ds, run_id, cfg, holder),
            name="batch-surrogate-optimizer",
            daemon=True,
        )
        t.start()
        self._optimization_runs[run_id] = {"thread": t, "started": time.time()}
        return {
            "status": "started",
            "run_id": run_id,
            "strategy": "surrogate",
            "budget": cfg["budget"],
            "message": ("Surrogate-guided optimization started in the "
                        "background. Poll check_optimization_status; then "
                        "get_optimization_results once it is 'completed'."),
        }

    def _run_surrogate_worker(self, ds: DesignSpace, run_id: int,
                              cfg: Dict[str, Any],
                              holder: Dict[str, Any]) -> None:
        """Runs run_surrogate_search on the background thread for the
        pre-created run (its own Robot instance, never the interactive
        session's)."""
        try:
            summary = run_surrogate_search(
                ds, run_id=run_id,
                budget=cfg["budget"], patience=cfg["patience"],
                acquisition=cfg["acquisition"], kappa=cfg["kappa"],
                db_path=self._batch_db_path)
            holder["run_id"] = summary.get("run_id", run_id)
            holder["status"] = summary.get("status")
            if summary.get("status") == "grid_fallback":
                # The start-tool pre-check should have prevented this; mark
                # the pre-created run completed so it never dangles as
                # 'running' with zero results.
                try:
                    st = Storage(db_path=self._batch_db_path)
                    st.mark_run_status(run_id, "completed")
                    st.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.error("Surrogate optimizer worker failed: %s", exc)
            holder["error"] = str(exc)

    def _tool_export_best_design(
        self, run_id: int, file_name: str, frontier_index: int = 0,
        visible: bool = True,
    ) -> dict:
        """Exports the lightest passing candidate of a COMPLETED run as a
        real Robot project (.rtd) so it can be opened in Robot. Builds +
        solves + saves in its own visible HeadlessSession."""
        if not file_name or not str(file_name).strip():
            raise ToolExecutionError(
                "export_best_design requires a 'file_name'.")
        st = Storage(db_path=self._batch_db_path)
        try:
            run = st.get_run(int(run_id))
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
        finally:
            st.close()

        try:
            path = safe_output_path(str(file_name), GENERATED_DIR)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc
        if not path.lower().endswith(".rtd"):
            path += ".rtd"
        try:
            t0 = time.time()
            saved = export_best_from_run(
                int(run_id), path, frontier_index=int(frontier_index),
                db_path=self._batch_db_path, visible=bool(visible))
            elapsed_s = round(time.time() - t0, 1)
        except Exception as exc:  # noqa: BLE001
            raise ToolExecutionError(
                f"Could not export the best design from run {run_id}: "
                f"{exc}") from exc
        return {
            "status": "ok",
            "run_id": int(run_id),
            "frontier_index": int(frontier_index),
            "file_path": saved,
            "elapsed_s": elapsed_s,
            "message": ("Design built, solved and saved. Open the .rtd in "
                        "Robot to inspect it. Note: this opened its OWN "
                        "Robot instance (one-seat license caveat)."),
        }

    def _tool_export_structure_spec(self) -> dict:
        """Reverse of build_structure_from_spec: the LIVE model as the
        'geometry' JSON object the optimizer / create_structure_from_spec
        accept."""
        self._ensure_robot()
        spec = self.robot.export_structure_spec()
        return {
            "status": "ok",
            "geometry": spec,
            "counts": {k: len(v) for k, v in spec.items()
                       if isinstance(v, list)},
        }

    def _tool_list_available_sections(self, family: str = None) -> dict:
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

    def _tool_apply_self_weight(self, case_id: int,
                                density: float = 7850.0) -> dict:
        """Applies every bar's self-weight (global -Z) into the case."""
        self._ensure_robot()
        summary = self.robot.apply_self_weight(int(case_id),
                                               density=float(density))
        return {"status": "ok", **summary}

    def _tool_preview_structure_geometry(
        self, file_name: str = "structure_geometry.png",
    ) -> dict:
        """Renders a wireframe of the in-memory geometry (no Robot COM)."""
        geometry = self.robot.get_model_geometry()
        if not geometry.get("nodes"):
            raise ToolExecutionError(
                "No geometry to preview yet - build or load a model first "
                "(create_structure_from_spec / create_node / create_bar).")
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

    def _tool_check_model_stability(self) -> dict:
        """The mechanism pre-solve check on the current model."""
        self._ensure_robot()
        r = self.robot.validate_stability()
        return {"status": "ok", **r}

    def _tool_robot_session_status(self) -> dict:
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

    def _tool_generate_code_combinations(
        self, combination_set: str = "ULS_SLS_basic",
    ) -> dict:
        """EN 1990 combination set from the currently defined simple cases
        (manual define_combination untouched - this is a convenience layer)."""
        self._ensure_robot()
        cases = []
        for num, obj in self.robot._iter_all_cases():
            try:
                if self.robot._as_combination(obj) is not None:
                    continue   # combinations are not simple cases
                nat = int(obj.Nature)
            except Exception:  # noqa: BLE001
                continue
            nature = next((k for k, v in RobotBridge._NATURE_MAP.items()
                           if v == nat), None)
            if nature is None:
                continue
            cases.append((int(num), nature))
        if not cases:
            raise ToolExecutionError(
                "generate_code_combinations needs at least one simple load "
                "case with nature permanent/imposed (create_load_case "
                "first).")
        try:
            plans = RobotBridge.eurocode_combination_factors(cases,
                                                             combination_set)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc
        created = []
        for plan in plans:
            res = self.robot.define_combination(
                plan["name"], plan["case_factors"], plan["combination_type"])
            created.append({
                "name": plan["name"], "case_factors": plan["case_factors"],
                "combination_type": plan["combination_type"], "result": res,
            })
        return {"status": "ok", "combination_set": combination_set,
                "count": len(created), "created": created}

    def _tool_compare_topologies(self, variants: list, load_spec: dict,
                                 objective: dict = None,
                                 budget: int = None) -> dict:
        """Sizes several topology variants under the same load spec and
        ranks them by lightest passing design (batch/topology_compare)."""
        from batch.topology_compare import compare_topologies
        try:
            result = compare_topologies(
                variants, load_spec, objective=objective, budget=budget,
                db_path=self._batch_db_path)
        except Exception as exc:  # noqa: BLE001
            raise ToolExecutionError(
                f"compare_topologies failed: {exc}") from exc
        return {"status": "ok", **result}

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

    # ------------------------------------------------------------------ #
    # [COMPOSE] Arbitrary shapes from verified geometry primitives
    # ------------------------------------------------------------------ #

    def _compose_reset(self) -> dict:
        self._compose_chains = {}
        self._compose_bars = []
        self._compose_supports = []
        self._compose_next_id = 1
        return {"status": "ok", "message": "composition reset"}

    def _compose_apply_step(self, step: dict) -> dict:
        """Applies ONE compose step with IMMEDIATE per-op validation (never
        deferred to finish). Raises ToolExecutionError on any bad input."""
        from tools.geometry_primitives import (
            generate_straight_chord, generate_arc_chord, connect_web_pattern,
            connect_bracing, apply_support_pattern)
        op = str(step.get("op") or "").lower()
        if not op:
            raise ToolExecutionError(
                "compose_structure step is missing 'op' "
                "(chord | web | bracing | copy | support).")

        def _require(fields):
            for f in fields:
                if f not in step or step[f] in (None, ""):
                    raise ToolExecutionError(
                        f"compose_structure op '{op}' requires '{f}'.")

        def _chain(name):
            if name not in self._compose_chains:
                raise ToolExecutionError(
                    f"compose_structure: unknown chain '{name}' - define it "
                    f"first with op='chord' (known: {sorted(self._compose_chains)}).")
            return self._compose_chains[name]

        if op == "chord":
            _require(["name"])
            name = str(step["name"])
            if name in self._compose_chains:
                raise ToolExecutionError(
                    f"compose_structure: chain name '{name}' already exists "
                    "- pick a unique name.")
            kind = str(step.get("kind") or "straight").lower()
            if kind not in ("straight", "arc"):
                raise ToolExecutionError(
                    f"compose_structure op 'chord': kind must be "
                    f"'straight' or 'arc', got {kind!r}.")
            _require(["span", "n_panels"])
            try:
                span = float(step["span"])
                n_panels = int(step["n_panels"])
            except (TypeError, ValueError) as exc:
                raise ToolExecutionError(
                    f"compose_structure op 'chord': span/n_panels must be "
                    f"numbers, got {step.get('span')!r}/{step.get('n_panels')!r}.") from exc
            if span <= 0.0:
                raise ToolExecutionError("compose_structure op 'chord': span must be > 0.")
            if n_panels < 2:
                raise ToolExecutionError("compose_structure op 'chord': n_panels must be >= 2.")
            try:
                y_shift = float(step.get("plane") or 0.0)
            except (TypeError, ValueError):
                raise ToolExecutionError(
                    f"compose_structure op 'chord': plane must be a number, "
                    f"got {step.get('plane')!r}.")
            section = str(step.get("section") or "IPE 200")
            if kind == "arc":
                try:
                    rise = float(step.get("rise") or 0.0)
                except (TypeError, ValueError):
                    raise ToolExecutionError(
                        f"compose_structure op 'chord' (arc): rise must be a "
                        f"number, got {step.get('rise')!r}.")
                chain = generate_arc_chord(
                    span, rise, n_panels, elevation=float(step.get("elevation") or 0.0),
                    plane=y_shift, arch=str(step.get("arch") or "up"),
                    section=section, start_id=self._compose_next_id)
            else:
                chain = generate_straight_chord(
                    span, n_panels, elevation=float(step.get("elevation") or 0.0),
                    plane=y_shift, section=section,
                    start_id=self._compose_next_id)
            self._compose_chains[name] = chain
            self._compose_next_id = max(
                self._compose_next_id, chain["last"] + 1,
                (chain["bars"][-1]["id"] + 1) if chain["bars"] else 1)
            return {"status": "ok", "message": f"chain '{name}' added",
                    "nodes": len(chain["nodes"]), "bars": len(chain["bars"])}

        if op == "web":
            _require(["top", "bottom"])
            top = _chain(str(step["top"]))
            bottom = _chain(str(step["bottom"]))
            if len(top["ids"]) != len(bottom["ids"]):
                raise ToolExecutionError(
                    f"compose_structure op 'web': chains "
                    f"'{step['top']}' ({len(top['ids'])} nodes) and "
                    f"'{step['bottom']}' ({len(bottom['ids'])} nodes) have "
                    "different panel counts - regenerate with matching n_panels.")
            pattern = str(step.get("pattern") or "pratt").lower()
            if pattern not in ("pratt", "warren"):
                raise ToolExecutionError(
                    f"compose_structure op 'web': pattern must be 'pratt' or "
                    f"'warren', got {pattern!r}.")
            all_bars = connect_web_pattern(
                top, bottom, pattern,
                web_section=str(step.get("web_section") or "IPE 200"),
                start_id=self._compose_next_id)
            # connect_web_pattern also emits the two chord runs; the chains
            # ALREADY carry their own chord bars, so keep only the web bars.
            n = len(top["ids"]) - 1
            web = all_bars[2 * n:]
            # [COMPOSE] A web member whose two endpoints are COINCIDENT is a
            # degenerate zero-length bar (e.g. the arch springs from z=0 at
            # the deck ends -> the end vertical arch_a[0]-deck_a[0] has length
            # 0). Robot keeps such bars but every downstream consumer
            # (apply_self_weight skips length<=0, the solver sees a singular
            # element) treats them as garbage. The two chains ARE connected at
            # those points through the adjacent diagonals, so dropping the
            # zero-length web bar is the correct, non-degenerate geometry.
            coords = {nd["id"]: (nd["x"], nd["y"], nd["z"])
                      for nd in top["nodes"] + bottom["nodes"]}
            dropped = [br["id"] for br in web
                       if coords[br["n1"]] == coords[br["n2"]]]
            bars = [br for br in web if br["id"] not in set(dropped)]
            self._compose_bars.extend(bars)
            self._compose_next_id = max(
                self._compose_next_id,
                (all_bars[-1]["id"] + 1) if all_bars else self._compose_next_id)
            msg = f"web {pattern} added between '{step['top']}' and '{step['bottom']}'"
            if dropped:
                msg += f" (dropped {len(dropped)} zero-length end bar(s): {dropped})"
            return {"status": "ok", "message": msg, "bars": len(bars)}

        if op == "bracing":
            _require(["plane_a", "plane_b"])
            pa = _chain(str(step["plane_a"]))
            pb = _chain(str(step["plane_b"]))
            if len(pa["ids"]) != len(pb["ids"]):
                raise ToolExecutionError(
                    f"compose_structure op 'bracing': planes "
                    f"'{step['plane_a']}' ({len(pa['ids'])} nodes) and "
                    f"'{step['plane_b']}' ({len(pb['ids'])} nodes) have "
                    "different panel counts - bracing needs matching n_panels.")
            pattern = str(step.get("pattern") or "cross").lower()
            if pattern not in ("cross", "transverse"):
                raise ToolExecutionError(
                    f"compose_structure op 'bracing': pattern must be "
                    f"'cross' or 'transverse', got {pattern!r}.")
            bars = connect_bracing(
                pa, pb, pattern,
                section=str(step.get("section") or "IPE 200"),
                start_id=self._compose_next_id)
            self._compose_bars.extend(bars)
            self._compose_next_id = max(
                self._compose_next_id,
                (bars[-1]["id"] + 1) if bars else self._compose_next_id)
            return {"status": "ok",
                    "message": f"bracing {pattern} added between "
                               f"'{step['plane_a']}' and '{step['plane_b']}'",
                    "bars": len(bars)}

        if op == "copy":
            _require(["source", "name", "y_shift"])
            try:
                y_shift = float(step["y_shift"])
            except (TypeError, ValueError):
                raise ToolExecutionError(
                    f"compose_structure op 'copy': y_shift must be a number, "
                    f"got {step.get('y_shift')!r}.")
            import math as _math
            if not _math.isfinite(y_shift):
                raise ToolExecutionError(
                    "compose_structure op 'copy': y_shift must be finite.")
            src = _chain(str(step["source"]))
            name = str(step["name"])
            if name in self._compose_chains:
                raise ToolExecutionError(
                    f"compose_structure op 'copy': chain name '{name}' already "
                    "exists - pick a unique name.")
            shift = int(self._compose_next_id) - src["first"]
            id_map = {}
            new_nodes = []
            for nd in src["nodes"]:
                new_id = int(nd["id"]) + shift
                id_map[int(nd["id"])] = new_id
                new_nodes.append({
                    "id": new_id,
                    "x": nd["x"],
                    "y": round(float(nd["y"]) + y_shift, 6),
                    "z": nd["z"],
                })
            new_bars = [
                {"id": int(b["id"]) + shift,
                 "n1": id_map[int(b["n1"])], "n2": id_map[int(b["n2"])],
                 "section": b["section"]}
                for b in src["bars"]
            ]
            self._compose_chains[name] = {
                "nodes": new_nodes, "bars": new_bars,
                "section": src["section"],
                "first": new_nodes[0]["id"], "last": new_nodes[-1]["id"],
                "ids": [n["id"] for n in new_nodes],
            }
            self._compose_next_id = max(
                self._compose_next_id, self._compose_chains[name]["last"] + 1)
            return {"status": "ok",
                    "message": f"chain '{name}' copied from '{step['source']}' "
                               f"with y_shift={y_shift}",
                    "nodes": len(new_nodes), "bars": len(new_bars)}

        if op == "support":
            _require(["type"])
            stype = str(step["type"]).lower()
            if stype not in ("pinned", "fixed", "roller_x", "roller_z", "spring"):
                raise ToolExecutionError(
                    f"compose_structure op 'support': unknown support_type "
                    f"{stype!r} (pinned | fixed | roller_x | roller_z | spring).")
            chain_ref = step.get("chain")
            explicit = step.get("nodes")
            if chain_ref:
                chain = _chain(str(chain_ref))
                if str(step.get("ends_only", "true")).lower() in ("true", "1", "yes"):
                    node_ids = [chain["first"], chain["last"]]
                else:
                    node_ids = chain["ids"]
            elif explicit:
                node_ids = []
                for n in explicit:
                    try:
                        node_ids.append(int(n))
                    except (TypeError, ValueError):
                        raise ToolExecutionError(
                            f"compose_structure op 'support': nodes must be "
                            f"ints, got {n!r}.")
            else:
                raise ToolExecutionError(
                    "compose_structure op 'support': provide 'chain' (ends "
                    "supported) or 'nodes' (explicit ids).")
            self._compose_supports.extend(
                apply_support_pattern(node_ids, stype))
            return {"status": "ok",
                    "message": f"support '{stype}' applied to {len(node_ids)} node(s)"}

        raise ToolExecutionError(
            f"compose_structure: unknown op '{op}' "
            f"(chord | web | bracing | copy | support).")

    def _tool_compose_structure(
        self, action: str = "step", step: dict = None, steps: list = None,
    ) -> dict:
        """Arbitrary-shape composition from verified geometry primitives
        (see the compose_structure schema for the full contract)."""
        action = str(action or "step").lower()
        if action == "reset":
            return self._compose_reset()
        if action == "finish":
            return self._compose_finish()
        if action == "batch":
            if not steps:
                raise ToolExecutionError(
                    "compose_structure action='batch' requires a 'steps' list "
                    "(keep it SMALL: <=5-6 steps; larger assemblies should use "
                    "one action='step' call per step).")
            last = None
            for st in steps:
                last = self._compose_apply_step(st or {})
            return {"status": "ok", "message": f"applied {len(steps)} step(s)",
                    "last": last, "chain_count": len(self._compose_chains),
                    "bars_so_far": len(self._compose_bars),
                    "supports_so_far": len(self._compose_supports)}
        if action == "step":
            if not step:
                raise ToolExecutionError(
                    "compose_structure action='step' requires a 'step' dict.")
            res = self._compose_apply_step(step)
            res["chain_count"] = len(self._compose_chains)
            res["bars_so_far"] = len(self._compose_bars)
            res["supports_so_far"] = len(self._compose_supports)
            return res
        raise ToolExecutionError(
            f"compose_structure: unknown action '{action}' "
            f"(step | batch | finish | reset).")

    def _compose_finish(self) -> dict:
        """Merges every registered chain + accumulated bars + supports into
        ONE spec, runs the integrity pre-flight, and clears the registry."""
        nodes: List[dict] = []
        bars: List[dict] = list(self._compose_bars)
        for name in sorted(self._compose_chains):
            chain = self._compose_chains[name]
            nodes.extend(chain["nodes"])
            bars.extend(chain["bars"])
        spec = {
            "project": "3D",
            "nodes": nodes,
            "bars": bars,
            "supports": list(self._compose_supports),
            "__composed": True,
        }
        issues = self.robot.spec_integrity_issues(spec)
        counts = {"nodes": len(nodes), "bars": len(bars),
                  "supports": len(self._compose_supports)}
        message = (
            f"assembled {counts['nodes']} nodes / {counts['bars']} bars / "
            f"{counts['supports']} supports")
        if issues:
            self._compose_reset()
            raise ToolExecutionError(
                f"compose_structure finish: spec integrity FAILED: "
                f"{'; '.join(issues)}")
        self._compose_reset()
        return {"status": "ok", "message": message,
                "geometry": spec, "counts": counts}

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
        level_height: float = 3.5, column_section: str = None,
        beam_x_section: str = None, beam_y_section: str = None,
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
        top_section: str = None, bottom_section: str = None,
        web_section: str = None,
    ) -> dict:
        self._ensure_robot()  # [WP1 fix]
        summary = self.robot.create_truss(
            span=span, height=height, panels=panels, top_section=top_section,
            bottom_section=bottom_section, web_section=web_section,
        )
        return {"status": "ok", **summary}

    def _tool_create_braced_frame(
        self, height: float = 6.0, width: float = 6.0,
        column_section: str = None, beam_section: str = None,
        brace_section: str = None,
    ) -> dict:
        self._ensure_robot()  # [WP1 fix]
        summary = self.robot.create_braced_frame(
            height=height, width=width, column_section=column_section,
            beam_section=beam_section, brace_section=brace_section,
        )
        return {"status": "ok", **summary}

    def _tool_create_arch_truss(
        self, span: float = 30.0, rise: float = 5.0, panels: int = 10,
        top_section: str = None, bottom_section: str = None,
        web_section: str = None, arch_chord: str = "top",
    ) -> dict:
        self._ensure_robot()  # [Part A] bowstring / arch truss template
        summary = self.robot.create_arch_truss(
            span=span, rise=rise, panels=panels, top_section=top_section,
            bottom_section=bottom_section, web_section=web_section,
            arch_chord=arch_chord,
        )
        return {"status": "ok", **summary}

    def _tool_check_section_proportions(self, spec: dict) -> dict:
        # [Part B] Pure offline check — no Robot connection required.
        warnings = check_section_proportions(spec or {})
        return {"status": "ok", "warning_count": len(warnings),
                "section_proportion_warnings": warnings}

    def _tool_set_bracing(
        self, bar_id: int, lcr_y: Optional[float] = None,
        lcr_z: Optional[float] = None, lcr_lt: Optional[float] = None,
        brace_points: Optional[list] = None,
    ) -> dict:
        # [EUROCODE Phase A] Explicit unbraced-length input layer.
        self._ensure_robot()
        resolved = self.robot.set_bar_bracing(
            bar_id=bar_id, lcr_y=lcr_y, lcr_z=lcr_z, lcr_lt=lcr_lt,
            brace_points=brace_points)
        return {"status": "ok", "bracing": resolved}

    def _tool_get_bracing(self, bar_id: Optional[int] = None) -> dict:
        # [EUROCODE Phase A] Read back resolved bracing data (defaults
        # tagged, so the engineer sees what was assumed).
        self._ensure_robot()
        return {"status": "ok",
                "bracing": self.robot.get_bar_bracing(bar_id=bar_id)}

    def _tool_check_lateral_torsional_buckling(
        self, case_id: int = 1, bar_ids: Optional[list] = None,
    ) -> dict:
        # [EUROCODE Phase C] §6.3.2.2 LTB + §6.3.3 Annex B interaction.
        self._ensure_robot()
        result = check_lateral_torsional_buckling(self.robot, case_id, bar_ids)
        return {"status": "ok", **result}

    def _tool_define_connection(
        self, bar_id: int, joint_end: str = "end",
        connection_type: str = "fin_plate", bolt_grade: str = "8.8",
        bolt_diameter: float = 20, bolt_rows: int = 2,
        pitch_mm: float = 60, edge_dist_mm: float = 30,
        end_dist_mm: float = 30, plate_thickness: float = 10,
        plate_grade: str = "S275", weld_leg_mm: Optional[float] = None,
    ) -> dict:
        # [EUROCODE Phase D] Simple-shear connection input layer.
        self._ensure_robot()
        result = self.robot.define_connection(
            bar_id=bar_id, joint_end=joint_end,
            connection_type=connection_type, bolt_grade=bolt_grade,
            bolt_diameter=bolt_diameter, bolt_rows=bolt_rows,
            pitch_mm=pitch_mm, edge_dist_mm=edge_dist_mm,
            end_dist_mm=end_dist_mm, plate_thickness=plate_thickness,
            plate_grade=plate_grade, weld_leg_mm=weld_leg_mm)
        return {"status": "ok", **result}

    def _tool_check_connection_capacity(
        self, bar_id: int, joint_end: str = "end", case_id: int = 1,
    ) -> dict:
        # [EUROCODE Phase D] EN 1993-1-8 simple shear connection check.
        self._ensure_robot()
        result = self.robot.check_connection_capacity(
            bar_id=bar_id, joint_end=joint_end, case_id=case_id)
        return {"status": "ok", **result}

    def _tool_check_eurocode_members(
        self, case_id: int = 1, bar_ids: Optional[list] = None,
    ) -> dict:
        # [EUROCODE Phase E] Worst-governing across all four checks.
        self._ensure_robot()
        result = check_eurocode_members(self.robot, case_id, bar_ids)
        # Cache the worst per-bar verdicts so store_result can include the
        # LTB / connection status in its one-line snapshot (Phase E.3).
        bars = result.get("bars") or []
        worst_ltb = "FAIL" if any(
            b.get("checks", {}).get("ltb", {}).get("status") == "FAIL"
            for b in bars) else ("NOT_CHECKABLE" if any(
            b.get("checks", {}).get("ltb", {}).get("status") == "NOT_CHECKABLE"
            for b in bars) else "PASS")
        worst_conn = "FAIL" if any(
            b.get("checks", {}).get("connection", {}).get("status") == "FAIL"
            for b in bars) else ("NOT_CHECKABLE" if any(
            b.get("checks", {}).get("connection", {}).get("status")
            == "NOT_CHECKABLE" for b in bars) else "PASS")
        self._eurocode_member_summary = {
            "ltb_status": worst_ltb, "connection_status": worst_conn,
        }
        return {"status": "ok", **result}

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
            ltb_status=(self._eurocode_member_summary or {}).get("ltb_status"),
            connection_status=(self._eurocode_member_summary or {})
                              .get("connection_status"),
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
        section_vertical: str = None, section_ring: str = None,
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
        section: str = None, diagonals: bool = False,
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
