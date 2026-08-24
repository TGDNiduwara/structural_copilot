"""Tool schemas (OpenAI function-calling JSON Schema format).

[FIX 06] Extracted verbatim from agent/tool_registry.py.
"""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
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
                    "enum": ["fixed", "pinned", "roller_x", "roller_z", "spring"],
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
        "description": "Applies a uniformly distributed load (kN/m) along a bar within a given load case. LIVE-VERIFIED SAFETY: if the current model contains COINCIDENT-but-distinct nodes (an arch springing node sharing a deck-end node's coordinate — what create_arch_truss / a compose twin-arch produce), Robot's solver silently under-transfers bar-uniform records to reactions (verified 6.9-20% shortfall live), so this tool transparently substitutes the statically equivalent NODAL loads (q*L/2 per end node — exact equilibrium, verified 0.00%) and reports method='nodal_lumped' with a warning. On models without coincident nodes it writes a true uniform record (verified exact). The returned 'method' tells you which was applied; 'warning' explains why when substitution occurred.",
        "parameters": {
            "type": "object",
            "properties": {
                "bar_id": {"type": "integer"},
                "case_id": {"type": "integer"},
                "value_kn_m": {
                    "type": "number",
                    "description": "Load magnitude in kN/m (negative = downward for gravity loads in Z).",
                },
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
                "divisions": {
                    "type": "integer",
                    "default": 5,
                    "description": "Number of divisions per bar (1-100).",
                },
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
                "file_name": {
                    "type": "string",
                    "description": "Output filename, e.g. 'Frame_Results.xlsx'.",
                },
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
                "file_name": {
                    "type": "string",
                    "description": "Output filename, e.g. 'Frame_Report.docx'.",
                },
                "project_title": {"type": "string", "default": "Untitled Project"},
                "engineer_name": {"type": "string", "default": "Structural Multi-App Agent"},
                "summary_text": {
                    "type": "string",
                    "description": "A short narrative engineering summary of the analysis and its key findings.",
                },
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
                "file_name": {
                    "type": "string",
                    "description": "Output filename, e.g. 'Frame_Presentation.pptx'.",
                },
                "project_title": {"type": "string", "default": "Untitled Project"},
                "engineer_name": {"type": "string", "default": "Structural Multi-App Agent"},
                "summary_text": {
                    "type": "string",
                    "description": "A short narrative engineering summary of the analysis and its key findings.",
                },
                "include_diagrams": {"type": "boolean", "default": True},
            },
            "required": ["file_name", "summary_text"],
        },
    },
    {
        "name": "create_structure_from_spec",
        "description": 'Builds a complete structure in one call from a JSON spec: project type, nodes, bars (sections), supports, load cases, and loads (uniform / concentrated / nodal). Prefer this tool for large or complex models. Example: {"project":"3D","nodes":[{"id":1,"x":0,"y":0,"z":0}],"bars":[{"id":1,"n1":1,"n2":2,"section":"IPE 300"}],"supports":[{"node":1,"type":"pinned"}],"cases":[{"id":1,"name":"DL","nature":"permanent"}],"loads":[{"kind":"bar_uniform","bar":1,"case":1,"direction":"Z","value":-10}]}. RELIABILITY: if the spec would exceed ~20 bars, DO NOT hand-type one giant JSON block - build INCREMENTS with smaller sub-specs (create_structure_from_spec for each sub-model\'s nodes/bars, or create_node/create_bar in loops). Long hand-typed single-shot JSON is the #1 reliability ceiling: one missing \':\' delimiter fails the whole call. Before using any non-IPE/HEA/HEB section name, call list_available_sections to get an exact catalog name. For ANY shape that is not one of the named templates (create_truss / create_arch_truss / create_braced_frame / create_rectangular_grid_frame / create_cylindrical_tank), use compose_structure instead of hand-writing node/bar JSON: it builds twin arches, twin trusses, cable-stayed decks and other assemblies from verified primitives, and it auto-numbers nodes/bars so you never compute ids.',
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
                "column_section": {
                    "type": "string",
                    "description": "Optional explicit column section; if omitted an H-family section is auto-sized from the storey height.",
                },
                "beam_x_section": {
                    "type": "string",
                    "description": "Optional explicit X-beam section; if omitted an IPE is auto-sized from bay_width_x.",
                },
                "beam_y_section": {
                    "type": "string",
                    "description": "Optional explicit Y-beam section; if omitted an IPE is auto-sized from bay_width_y.",
                },
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
                "top_section": {
                    "type": "string",
                    "description": "Optional explicit top-chord section; if omitted it is auto-sized from the span.",
                },
                "bottom_section": {
                    "type": "string",
                    "description": "Optional explicit bottom-chord section; if omitted it is auto-sized from the span.",
                },
                "web_section": {
                    "type": "string",
                    "description": "Optional explicit web (vertical/diagonal) section; if omitted a light angle is auto-sized. For any NON-IPE/HEA/HEB section, FIRST call list_available_sections(family='L') to get the exact catalog spelling (e.g. 'L 50x50x5'), then pass that here - guessing an angle name like 'L 120x120x5' from memory is what caused catalog-miss failures.",
                },
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
                "column_section": {
                    "type": "string",
                    "description": "Optional explicit column section; if omitted auto-sized from the height.",
                },
                "beam_section": {
                    "type": "string",
                    "description": "Optional explicit beam section; if omitted auto-sized from the width.",
                },
                "brace_section": {
                    "type": "string",
                    "description": "Optional explicit brace section; if omitted auto-sized from the diagonal length. For any NON-IPE/HEA/HEB section, FIRST call list_available_sections(family='L') to get the exact catalog spelling, then pass that here.",
                },
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
                "rise": {
                    "type": "number",
                    "default": 5.0,
                    "description": "Arch rise at mid-span in meters (span/rise ~ 4-8 typical).",
                },
                "panels": {
                    "type": "integer",
                    "default": 10,
                    "description": "Number of panels along each chord.",
                },
                "top_section": {
                    "type": "string",
                    "description": "Optional explicit top-chord section; if omitted auto-sized from the span.",
                },
                "bottom_section": {
                    "type": "string",
                    "description": "Optional explicit bottom-chord section; if omitted auto-sized from the span.",
                },
                "web_section": {
                    "type": "string",
                    "description": "Optional explicit web section; if omitted a light angle is auto-sized. For any NON-IPE/HEA/HEB section, FIRST call list_available_sections(family='L') to get the exact catalog spelling (e.g. 'L 50x50x5') - guessing angle names from memory caused catalog-miss failures.",
                },
                "arch_chord": {
                    "type": "string",
                    "enum": ["top", "bottom"],
                    "default": "top",
                    "description": "'top' = bowstring (arch on top, straight deck at z=0); 'bottom' = arch below with a straight deck above at z=rise.",
                },
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
                "spec": {
                    "type": "object",
                    "description": 'Structure spec dict: {"nodes": [{"id","x","y","z"}], "bars": [{"id","n1","n2","section"}]}.',
                },
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
                "lcr_y": {
                    "type": "number",
                    "description": "Major-axis buckling length (m); default = full bar length (conservative).",
                },
                "lcr_z": {
                    "type": "number",
                    "description": "Minor-axis buckling length (m); default = full bar length (conservative).",
                },
                "lcr_lt": {
                    "type": "number",
                    "description": "Lateral-torsional unbraced length (m); default = longest sub-span from brace_points, else full bar length.",
                },
                "brace_points": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Intermediate bracing positions as fractions of bar length in [0,1], e.g. [0.5] for a mid-span purlin. Shortens lcr_lt only.",
                },
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
                "bar_id": {
                    "type": "integer",
                    "description": "Optional bar id; omit to list all bars.",
                },
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
                "case_id": {
                    "type": "integer",
                    "default": 1,
                    "description": "Solved case/combination id.",
                },
                "bar_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional bar ids to check; omit to check all bars.",
                },
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
                "joint_end": {
                    "type": "string",
                    "enum": ["start", "end"],
                    "default": "end",
                    "description": "Which bar end the connection is at.",
                },
                "connection_type": {
                    "type": "string",
                    "enum": ["fin_plate", "double_angle", "end_plate"],
                    "default": "fin_plate",
                },
                "bolt_grade": {
                    "type": "string",
                    "enum": ["4.6", "5.6", "8.8", "10.9"],
                    "default": "8.8",
                },
                "bolt_diameter": {
                    "type": "number",
                    "default": 20,
                    "description": "Bolt diameter in mm.",
                },
                "bolt_rows": {
                    "type": "integer",
                    "default": 2,
                    "description": "Number of bolts in the vertical line.",
                },
                "pitch_mm": {
                    "type": "number",
                    "default": 60,
                    "description": "p1 vertical bolt pitch (mm).",
                },
                "edge_dist_mm": {
                    "type": "number",
                    "default": 30,
                    "description": "e2 side edge distance (mm).",
                },
                "end_dist_mm": {
                    "type": "number",
                    "default": 30,
                    "description": "e1 end distance (mm).",
                },
                "plate_thickness": {
                    "type": "number",
                    "default": 10,
                    "description": "Fin/end plate thickness (mm).",
                },
                "plate_grade": {
                    "type": "string",
                    "enum": ["S235", "S275", "S355", "S460"],
                    "default": "S275",
                },
                "weld_leg_mm": {
                    "type": "number",
                    "description": "Fillet weld leg size (mm) if the connection is welded; omitted = bolted.",
                },
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
                "bar_id": {
                    "type": "integer",
                    "description": "Existing bar id with a defined connection.",
                },
                "joint_end": {"type": "string", "enum": ["start", "end"], "default": "end"},
                "case_id": {
                    "type": "integer",
                    "default": 1,
                    "description": "Solved case/combination id.",
                },
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
                "case_id": {
                    "type": "integer",
                    "default": 1,
                    "description": "Solved case/combination id.",
                },
                "bar_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional bar ids to check; omit to check all bars.",
                },
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
                "section_name": {
                    "type": "string",
                    "description": "New catalog section, e.g. 'HEB 200'.",
                },
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
                "support_type": {
                    "type": "string",
                    "enum": ["fixed", "pinned", "roller_x", "roller_z"],
                },
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
                "code": {
                    "type": "string",
                    "description": "Python source to execute in the sandbox.",
                },
                "purpose": {
                    "type": "string",
                    "description": "One-line description of what this script does.",
                },
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
                "name": {
                    "type": "string",
                    "description": "snake_case tool name, e.g. 'create_arch_bridge'.",
                },
                "description": {
                    "type": "string",
                    "description": "What the tool does (shown in the tool list).",
                },
                "parameters": {
                    "type": "object",
                    "description": "JSON-schema 'parameters' object; properties are injected as script variables.",
                },
                "code": {
                    "type": "string",
                    "description": "Python source. Use the declared parameter names as variables; also has robot/RobotEnum/math/json/pd.",
                },
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
                "file_path": {
                    "type": "string",
                    "description": "Absolute destination path (.rtd appended if missing).",
                },
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
                "radius": {
                    "type": "number",
                    "default": 2.5,
                    "description": "Tank radius in meters (diameter = 2*radius).",
                },
                "height": {
                    "type": "number",
                    "default": 5.0,
                    "description": "Tank height in meters.",
                },
                "segments": {
                    "type": "integer",
                    "default": 16,
                    "description": "Number of polygon segments around the circle (16-32 for a smooth cylinder).",
                },
                "ring_levels": {
                    "type": "integer",
                    "default": 2,
                    "description": "Number of horizontal rings including base and top (add mid rings for tall tanks).",
                },
                "section_vertical": {
                    "type": "string",
                    "description": "Optional explicit section for the vertical columns; if omitted auto-sized from the tank height.",
                },
                "section_ring": {
                    "type": "string",
                    "description": "Optional explicit section for the circumferential ring beams; if omitted auto-sized from the tank diameter.",
                },
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
                "sheets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Which sheets to include, e.g. ['member_forces','reactions','displacements','stresses','boq'].",
                },
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
                "normal": {
                    "type": "string",
                    "enum": ["X", "Y", "Z"],
                    "default": "Y",
                    "description": "Panel plane normal: Y=horizontal slab (X-Z plane), X or Z = wall.",
                },
                "divisions_x": {"type": "integer", "default": 4},
                "divisions_z": {"type": "integer", "default": 4},
                "section": {
                    "type": "string",
                    "description": "Optional explicit grillage-bar section; if omitted auto-sized from the panel's smaller plan dimension.",
                },
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
                "thickness_m": {
                    "type": "number",
                    "description": "Plate thickness in meters, e.g. 0.2.",
                },
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
                "node_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "All vertex node numbers of the solid.",
                },
                "face_groups": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "integer"}},
                    "description": "Bounding faces as ordered closed loops of node numbers.",
                },
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
                    "type": "array",
                    "items": {"type": "integer"},
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
                    "description": 'Map of load-case id to factor, e.g. {"1": 1.2, "2": 1.6}.',
                },
                "combination_type": {
                    "type": "string",
                    "default": "ULS",
                    "enum": ["ULS", "SLS", "ALS", "ACC", "SPC"],
                },
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
        "description": '[P7 BATCH OPTIMIZER] Validates a design-space spec and estimates the run WITHOUT starting it. Translate the user\'s natural-language brief into the DesignSpace JSON schema: {"geometry": {...same as create_structure_from_spec\'s spec: nodes/bars with section/supports/cases/loads...}, "variable_groups": [{"group_name": "columns", "bar_ids": [1,3], "candidate_sections": ["HEA 200","HEA 220"]}, ...], "load_cases": [{"id":1,"name":"DL","nature":"permanent"}], "analysis_types": ["static"], "objective": {"minimize": "weight", "constraint": "max_utilization <= 1.0 AND buckling_pass == True"}}. HARD RULE: this tool NEVER starts a run - not ever, under ANY phrasing. It only validates + returns the candidate count, time estimate and a run_config_id. Do NOT call confirm_and_start_optimization_run in this same response, even if the user said \'just run it\', \'go ahead\', \'start it\', \'yes do it\', or anything that sounds like permission. A batch run consumes Robot license time, so confirmation ALWAYS requires a SEPARATE, LATER message from the user AFTER they have seen and approved this estimate. Your next reply after this tool must present the count + estimate to the user and ask for explicit confirmation - then STOP and wait for their next message. If you find yourself wanting to call confirm_and_start_optimization_run in this same turn, STOP: that is a violation. For LARGE design spaces (hundreds to thousands of candidates) where an exhaustive grid would be too slow, use start_surrogate_search_run instead. When the user wants to OPTIMIZE AN ALREADY-BUILT model, call export_structure_spec FIRST and use its returned \'geometry\' verbatim as spec.geometry (do NOT retype geometry from memory).',
        "parameters": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": 'DesignSpace JSON. Example: {"geometry":{"project":"2D","nodes":[{"id":1,"x":0,"z":0},{"id":2,"x":0,"z":3},{"id":3,"x":6,"z":3},{"id":4,"x":6,"z":0}],"bars":[{"id":1,"n1":1,"n2":2,"section":"HEA 200"},{"id":2,"n1":2,"n2":3,"section":"IPE 300"},{"id":3,"n1":3,"n2":4,"section":"HEA 200"}],"supports":[{"node":1,"type":"pinned"},{"node":4,"type":"pinned"}],"cases":[{"id":1,"name":"DL","nature":"permanent"}],"loads":[{"kind":"bar_uniform","bar":2,"case":1,"direction":"Z","value":-3}]},"variable_groups":[{"group_name":"columns","bar_ids":[1,3],"candidate_sections":["HEA 200","HEA 220","HEA 240","HEB 200"]},{"group_name":"beam","bar_ids":[2],"candidate_sections":["IPE 270","IPE 300","IPE 330"]}],"load_cases":[{"id":1,"name":"DL","nature":"permanent"}],"analysis_types":["static"],"objective":{"minimize":"weight","constraint":"max_utilization <= 1.0 AND buckling_pass == True"}}',
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
                "run_config_id": {
                    "type": "string",
                    "description": "The run_config_id returned by start_optimization_run.",
                },
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
                "run_id": {
                    "type": "integer",
                    "description": "The run_id returned by confirm_and_start_optimization_run.",
                },
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
                "budget": {
                    "type": "integer",
                    "description": "Max Robot calls the surrogate search may spend (default 300).",
                    "default": 300,
                },
                "patience": {
                    "type": "integer",
                    "description": "Stop after this many consecutive proposals that fail to improve the Pareto frontier (default 10).",
                    "default": 10,
                },
                "acquisition": {
                    "type": "string",
                    "enum": ["ucb", "ehvi"],
                    "description": "Acquisition function: 'ucb' (upper confidence bound on utilization - the current default) or 'ehvi' (expected hypervolume improvement over the Pareto frontier).",
                    "default": "ucb",
                },
                "kappa": {
                    "type": "number",
                    "description": "Exploration weight for the 'ucb' acquisition (default 2.0).",
                    "default": 2.0,
                },
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
                "run_config_id": {
                    "type": "string",
                    "description": "The run_config_id returned by start_surrogate_search_run.",
                },
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
                "run_id": {
                    "type": "integer",
                    "description": "The run_id of a completed optimization run.",
                },
                "file_name": {
                    "type": "string",
                    "description": "Output file name (e.g. 'winner' or 'winner.rtd'); saved into the generated/ directory with .rtd appended if missing.",
                },
                "frontier_index": {
                    "type": "integer",
                    "description": "Index into the Pareto frontier sorted by weight ascending; 0 = lightest passing (default).",
                    "default": 0,
                },
                "visible": {
                    "type": "boolean",
                    "description": "Open Robot visibly while building/solving/saving (default true - the user wants to look at it).",
                    "default": True,
                },
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
        "description": "eturns valid catalog section names (e.g. 'IPE 300', 'HEA 200') that the geometry templates and optimizer draw from, optionally filtered by family (IPE / HEA / HEB / HEM / IPN / UPN / UPE / L / CHS / RHS / SHS). Catalog-only and fast - no Robot solve needed. Use this to pick realistic candidate_sections for an optimization design space or a section for a bar. IMPORTANT for family='L' (angles): the list returns FULL resolvable equal-angle names like 'L 60x60x6' - always use the '<leg>x<leg>x<thickness>' form; thin webs are not available on every leg (L 120x120x5 does NOT exist). IMPORTANT for family='CHS'/'RHS'/'SHS' (hollow sections, VERIFIED in Robot's UKST catalog; EURO/AISC/DIN/ARCLR/CISC/CHINA/JAPAN do not carry these forms): names use the UK convention 'CHS <outer_d>x<wall_t>', 'RHS <b>x<h>x<t>', 'SHS <b>x<b>x<t>', e.g. 'CHS 139.7x5', 'CHS 114.3x4', 'RHS 150x100x6', 'SHS 100x100x5'. Use exactly the names the family filter returns - they are the ones that resolve live",
        "parameters": {
            "type": "object",
            "properties": {
                "family": {
                    "type": "string",
                    "description": "Optional section family filter (case-insensitive), e.g. 'IPE', 'HEA', 'HEB', 'L'. Omit for all families.",
                },
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
                "case_id": {
                    "type": "integer",
                    "description": "The load case to apply self-weight into (must already exist).",
                },
                "density": {
                    "type": "number",
                    "description": "Steel density in kg/m3 (default 7850).",
                    "default": 7850,
                },
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
                "file_name": {
                    "type": "string",
                    "description": "Output PNG file name (default 'structure_geometry.png').",
                    "default": "structure_geometry.png",
                },
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
                "combination_set": {
                    "type": "string",
                    "enum": ["ULS_SLS_basic", "ULS_only", "SLS_only"],
                    "description": "Which combinations to generate (default 'ULS_SLS_basic' = ULS + SLS characteristic).",
                    "default": "ULS_SLS_basic",
                },
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
                "variants": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "generator": {
                                "type": "string",
                                "enum": [
                                    "create_truss",
                                    "create_arch_truss",
                                    "create_braced_frame",
                                    "create_rectangular_grid_frame",
                                ],
                            },
                            "generator_args": {"type": "object"},
                        },
                    },
                    "description": "List of {name, generator, generator_args} variants to size and compare.",
                },
                "load_spec": {
                    "type": "object",
                    "description": "The SAME load spec applied to every variant: {cases: [{id,name,nature}], loads: [{kind,bar,case,direction,value}...]} (the geometry spec's cases/loads keys).",
                },
                "objective": {
                    "type": "object",
                    "description": "Optional DesignSpace objective dict (default: minimize weight with max_utilization <= 1.0 AND buckling_pass == True).",
                },
                "budget": {
                    "type": "integer",
                    "description": "Optional Robot-call budget for large variant grids (default 300).",
                },
            },
            "required": ["variants", "load_spec"],
        },
    },
]
