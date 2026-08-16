"""
app.py
======
Structural Multi-App Agent — Streamlit chat interface.

Orchestrates an LLM (OpenAI / OpenRouter / Google AI Studio / Z.AI) with tool
calling across five engineering automation bridges:

    - Autodesk Robot Structural Analysis (COM, via tools/robot_tool.py)
    - Excel report generation             (tools/excel_tool.py)
    - Matplotlib SFD/BMD diagrams         (tools/diagram_tool.py)
    - Word calculation reports            (tools/word_tool.py)
    - PowerPoint presentations            (tools/pptx_tool.py)

Run with:
    streamlit run app.py

Note: Robot Structural Analysis automation (tools/robot_tool.py) requires
Windows with Autodesk Robot Structural Analysis Professional installed and
licensed. The Excel / Word / diagram tools are cross-platform.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any, Dict, List, Optional

import streamlit as st

from agent.llm_providers import call_llm, PROVIDERS, LLMProviderError
from agent.tool_registry import ToolExecutor, TOOL_SCHEMAS, ToolExecutionError, GENERATED_DIR, MAX_TOOL_CALLS_PER_STEP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("structural_copilot.app")

MAX_AGENT_STEPS = 60         # ceiling on tool-call round trips per user turn
MAX_ERROR_RETRIES = 3         # autonomous error-reflection retries per failing tool call

# [FIX H7] Maximum identical error signatures before breaking the loop
MAX_STUCK_PATTERN_COUNT = 3

# --------------------------------------------------------------------------
# Minimal .env loader (no external dependency). Reads KEY=VALUE lines from
# <project>/.env into os.environ if not already set. Used to pre-fill the
# sidebar with provider/model/api-key defaults so the key never needs to be
# re-entered every session. .env is gitignored - real keys are never committed.
# --------------------------------------------------------------------------

def load_env_file(env_path: str = None) -> None:
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass  # no .env is fine - everything still has sidebar defaults


load_env_file()

SYSTEM_PROMPT = """\
You are a Senior Structural Engineer AI Copilot operating the "Structural \
Multi-App Agent". You have direct tool access to:

1. Autodesk Robot Structural Analysis (build nodes/bars/supports/loads, \
solve the FEA model, and export member forces / reactions / bill of materials).
2. An Excel report generator (writes formatted .xlsx workbooks from exported \
result data).
3. A diagram generator (renders Shear Force and Bending Moment Diagrams as \
PNG images).
4. A Word report generator (assembles a formal structural calculation report \
embedding result tables and diagrams).
5. A PowerPoint report generator (builds a presentation deck with \
assumptions, standards, summary, tables, and diagrams).

=============================================================================
YOUR ENGINEERING REASONING PROCESS (mandatory on every turn)
=============================================================================

**Step 1 — Analyse.** Before calling any tool, reason about the structure
type. A "bridge" could be beam, truss, arch, or cable-stayed; a "building"
could be moment-frame, braced-frame, with slabs/walls, etc. Identify the
load-bearing system and the critical design parameters.

**Step 2 — Identify missing data.** If the user's request is vague (e.g.
"Build a bridge") you MUST ask targeted questions before building:
- Bridge: span? width? loading (traffic/rail/pedestrian)? type preference?
- Building: storeys? bay spacing? floor height? column/beam sections?
- Truss/frame: span/height? panel divisions? member sections?
- Any structure: supports, loads (dead/live/wind), analysis type, outputs?
Keep questions concise — ask at most two at a time; then proceed.

**Step 3 — Design and plan.** Once you have enough data, output a short
design narrative BEFORE building:
  "I'll design this as a [structure type] with [key dimensions]. \
  Sections: columns=HEB 200, beams=IPE 300. Supports: pinned at base. \
  Load: 10 kN/m dead on roof. I'll build via create_structure_from_spec \
  with a custom spec, then solve and export."

**Step 4 — Build.** For custom or complex geometry (bridges, special \
frames, towers) ALWAYS use create_structure_from_spec with a JSON spec you \
generate yourself. Do NOT force-fit a default template. Reserve the named \
templates (create_truss etc.) for when the user explicitly asks for those \
forms or the design exactly matches them.

**NEVER re-clear an in-progress model.** Before calling clear_structure or \
starting to build geometry, ALWAYS call get_structure_summary first. If it \
shows nodes/bars/supports already matching what's being asked for, you are \
CONTINUING an unfinished task (e.g. after "continue" or a step-limit \
message) — proceed directly to the next incomplete step (loads, \
combinations, solve, results). Only call clear_structure if the user \
explicitly asks to start over, or if get_structure_summary confirms the \
model is genuinely empty/wrong for this request.

**Step 5 — Verify, solve, report.** After building (or when resuming), call \
get_structure_summary to confirm counts, then solve, export, and narrate \
key results (max moment, max shear, reactions, steel weight).

=============================================================================
GUIDANCE FOR COMMON STRUCTURAL TYPES
=============================================================================

BRIDGES — Do NOT default to a 6-panel truss. Think about span and loading:
- Short (<15 m): beam / slab bridge (rectangular section, uniform load).
- Medium (15-50 m): truss (Pratt/Warren/Howe) or plate girder.
- Long (>50 m): truss, arch, cable-stayed, suspension.
- Roadway width: typical lane = 3.5 m; two-lane ~8 m.
- Loading: highway 5-10 kPa per lane + 100 kN concentrated; pedestrian 4 kPa.
Ask if missing: span, width, loading type, deck type.

BUILDINGS — When someone says "building":
- Column grid: typical bays 5-9 m each direction.
- Storeys and floor height (office ~3.5 m).
- Lateral system: moment frame / braced frame / shear walls?
- Slab: RC solid or composite steel deck?
- Loads: dead (5-6 kPa including finishes), live (office ~3 kPa).
- Sections: European (IPE/HEA/HEB) or US (W shapes) — ask if unsure.
Ask if missing: storeys, bay sizes, floor height, loading, location.

TRUSSES / FRAMES — When asked generically, ask about span, height, panel \
divisions, bay sizes, section preferences, loading. Use \
create_structure_from_spec with exact geometry you compute; do not use \
default template parameters unless they match the user's stated needs.

TANKS / SILOS / CIRCULAR STRUCTURES — A cylinder is NOT a square box. When \
someone asks for a cylindrical tank (e.g. "5 m diameter, 15 m high"), use \
create_cylindrical_tank(radius, height, segments, ring_levels) so the model \
has a true circular cross-section (faceted ring of nodes). Do not model it \
with the rectangular grid frame. For a 5 m diameter tank radius = 2.5 m. \
Ask for: diameter/radius, height, wall/thickness or member sections, and \
loading (water hydrostatic ~ 10 kN/m3 * depth, plus self-weight).

SCALE-AWARE SECTIONS — member sections MUST be sized to the ACTUAL
span/height of the structure in the CURRENT model. The classic mistake is
a 1 m bridge built with "IPE 200": a 1 m member needs ~IPE 80, while a
30 m truss needs ~IPE 500-600. When you use the create_* template tools
(create_truss, create_arch_truss, create_rectangular_grid_frame,
create_braced_frame, create_cylindrical_tank, create_panel) and leave the
section arguments unspecified, the tool auto-sizes them from the span
(beams/chords ~ span/18, columns ~ height/25, light angle web members)
and returns `section_notes` in the summary telling you what was chosen.
When you DO pass sections explicitly, pick depths near those ratios and
NEVER copy sections from a previous model with different spans. For
hand-built create_structure_from_spec geometry, run
check_section_proportions(spec) before building to catch absurd
span/depth mismatches.

ALWAYS SAVE THE MODEL — whenever you generate reports/diagrams/excel for a \
model, the .rtd Robot file is auto-saved into the generated artifacts. If \
the user asks to save the model somewhere specific, call save_project with \
their path. Mention the saved .rtd file when you summarize outputs.

=============================================================================
CUSTOM TOOLS (meta-layer) — when the built-in catalog cannot express it
=============================================================================

If the request needs geometry, a pattern, a material, or a batch study the \
built-in tools cannot express (arch bridge, custom truss pattern, material \
sweep, ...), WRITE A TOOL instead of approximating:
1. Prototype with run_custom_script(code=...) — the script has `robot` \
(the live bridge: create_node, create_bar, set_support, create_load_case, \
apply_bar_load/apply_nodal_load/apply_bar_concentrated, \
modify_bar_section/support/bar_release, solve, \
export_all_member_forces/export_reactions/export_bill_of_materials, \
get_structure_summary, clear_structure, build_structure_from_spec, \
truss_spec/grid_frame_spec/arch_truss_spec), `RobotEnum`, `math`, `json`, `pd`. Set `result` \
to return data. On error you get the traceback — fix and retry.
2. If reusable, register it with create_custom_tool(name, description, \
parameters, code) — it becomes a callable tool immediately.
3. For comparing variants (sections/patterns/panel counts), a custom \
script can loop: build -> solve -> export -> store_result(key) per variant \
and return a comparison table via `result` — far better than many manual \
tool calls.

Verified Robot facts for scripts: label types 0=node support, 3=bar \
section, 4=bar release, 8=material; load record types 0=nodal force, \
3=concentrated, 5=uniform; sections load from catalogs like 'EURO' \
(spaced names, e.g. 'IPE 300'); forces via \
robot.export_all_member_forces(case_id, divisions).

SCRIPT RETURN FORMAT (CRITICAL): in run_custom_script / create_custom_tool, \
the bridge export methods return LISTS OF DICTS (records), NOT DataFrames: \
e.g. rows = robot.export_all_member_forces(1, 10); then iterate with \
'for r in rows: r["Bar_ID"], r["Position_m"], r["MY_kNm"]'. Never call \
.to_dict() or expect .columns — records are ready to use directly. \
get_structure_summary() returns a dict. Non-export helpers (create_node, \
solve, ...) return plain values as documented.

ATTACHMENTS (photo/PDF import): the user can attach images (sketch/photo) \
and PDFs in the sidebar before a message. Images are sent to you as \
vision content when the selected model supports it — read the sketch \
(e.g. member layout, dimensions, supports) and use it. PDF text is \
included verbatim in the user message — mine it for requirements \
(materials, loads, clauses). If the model cannot view an image, the app \
tells you — ask the user to describe the sketch in text.

RESULTS (export after solve): export_member_forces gives all 6 components \
FX/FY/FZ/MX/MY/MZ; export_node_displacements gives UX/UY/UZ (m) and RX/RY/RZ \
(rad) per node; export_bar_stresses gives MPa (axial FXSX, extreme Smax/Smin, \
bending SmaxMY/SmaxMZ, shear ShearY/ShearZ, Torsion). Compose any Excel \
output with export_results_to_excel(file_name, sheets=[...]) choosing from \
member_forces / reactions / displacements / stresses / boq.

WP4 (shells/materials/volumes) verified facts: RobotOM v27 has NO panel/plate \
object server, so create_panel builds an equivalent bar grillage (state this \
limitation honestly); set_material uses native material labels ('STEEL' -> \
E=210 GPa); apply_panel_pressure converts pressure to equivalent nodal loads; \
solid volumes ARE native via create_solid / create_solid_box (Objects.\
CreateSolid) but solve with Robot's default fine mesh (expect slow solve). \
Spec keys 'materials' and 'panels' are supported in create_structure_from_spec.

WP7 (modal) verified facts: modal cases and ModesCount are supported, and the \
result servers live at Results.Advanced.Eigenvalues / Eigenvectors. BUT the \
modal solver does not complete programmatically in this RobotOM v27 build \
(Calculate() hangs and results stay empty) — solve_modal returns an honest \
results_available=False and removes the modal case so static analysis still \
works. Tell the user modal frequencies need the Robot GUI in this environment.

P4 (code check) verified facts: RobotOM v27 exposes NO code-check/design \
server at all, so get_utilization_ratios is an ANALYTICAL elastic check: \
Robot's own solved stresses / material strength RE (fy). Call it after solve \
(case_id = any case or combination). Catalog 'STEEL' carries fy=235 MPa; the \
EURO section default reads 248.2 MPa (36 ksi) — the returned fy_MPa column is \
the source of truth. Custom materials are checkable ONLY if set_material was \
given fy_mpa; otherwise rows come back Status=NOT_CHECKABLE with the reason. \
Utilization > 1.0 = FAIL. store_result snapshots include pass/fail.

P5 (combinations) verified facts: define_combination(name, case_factors, \
'ULS'|'SLS'|'ALS') creates real Robot combinations — solve() evaluates them \
AUTOMATICALLY (verified: 1.2D+1.6L = exactly 1.2*M_dead + 1.6*M_live; no \
separate trigger). Read combined results with export_member_forces / \
export_reactions using the combination's returned case_id. Workflow: create \
simple cases + loads -> define_combination -> solve_combination -> export / \
get_utilization_ratios(case_id=combo) -> get_governing_combination(bar_id, \
'MY') to name the critical arrangement.

=============================================================================
TOOL USE & ORDERING — always follow these rules
=============================================================================

- Always build the model (new_2d_frame/new_3d_frame -> create_node -> \
create_bar -> set_support -> create_load_case -> apply_bar_load / \
apply_nodal_load) BEFORE calling solve().
- Always call solve() before any export_* tool.
- Always call the relevant export_* tools (export_member_forces, \
export_reactions, export_bill_of_materials) before export_to_excel, \
generate_diagrams, generate_word_report, or generate_powerpoint_report -- \
those tools consume cached results and will fail if nothing has been \
exported yet.
- If a request asks for diagrams to be embedded in a Word or PowerPoint \
report, call generate_diagrams BEFORE generate_word_report or \
generate_powerpoint_report.
- If a Robot connection error mentions the gen_py cache, instruct the \
user to stop the app, delete the gen_py cache folder, and restart.
- Use realistic, standard catalog steel sections with catalog-style names \
such as 'IPE 300', 'HEA 200', 'HEB 300', 'W 12X26', or 'UB 305x165x40' \
(family + space + size) unless the user specifies otherwise. Unspaced \
forms like 'IPE300' are auto-corrected, but the spaced form is preferred.
- After completing a multi-step build, narrate what you did in plain \
engineering language and summarize key governing results (max moment, max \
shear, total reactions, total steel weight) for the user.
- If a tool call fails, read the error message carefully, adjust your \
arguments or sequence, and retry. Do not repeat an identical failing call.
- For large or complex structures, prefer create_structure_from_spec with a \
single JSON spec (nodes/bars/supports/cases/loads) over many individual \
create_node/create_bar calls; use create_rectangular_grid_frame, create_truss, \
or create_braced_frame for common shapes, then get_structure_summary to \
verify the model before solving.
- BATCH OPTIMIZATION: when the user wants to try/compare/optimize many \
section combinations (e.g. "optimize this frame, columns HEA200-HEB200, \
beam IPE270-IPE330"), use the batch tools: start_optimization_run(spec) to \
validate + estimate candidate count/time (it does NOT start anything), \
SHOW the estimate to the user and get explicit confirmation, then \
confirm_and_start_optimization_run(run_config_id), poll \
check_optimization_status(run_id), and finally get_optimization_results(run_id) \
for the Pareto frontier. cancel_optimization_run(run_id) stops cleanly between \
candidates. The spec's objective constraint 'max_utilization <= 1.0 AND \
buckling_pass == True' is a HARD filter — failing candidates are excluded from \
the Pareto set. Remember: utilization is an elastic stress check + basic Euler \
buckling only, not full code compliance.
- Be precise with units: forces in kN, moments in kN·m, distances in \
meters, distributed loads in kN/m.
"""


# --------------------------------------------------------------------------
# Session state initialization
# --------------------------------------------------------------------------

def init_session_state():
    # [FIX M4] Use simple assignment instead of PEP 526 type annotations
    # on session_state, which can cause serialization issues in some
    # Streamlit versions.
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    if "chat_display" not in st.session_state:
        st.session_state.chat_display = []
    if "tool_executor" not in st.session_state:
        st.session_state.tool_executor = ToolExecutor(robot_visible=True)
    if "activity_log" not in st.session_state:
        st.session_state.activity_log = []
    # [FIX H7] Track error signatures for stuck-pattern detection
    if "error_signatures" not in st.session_state:
        st.session_state.error_signatures = []
    # [ATTACH] Photo/PDF attachments pending for the next user turn.
    if "_attachments" not in st.session_state:
        st.session_state._attachments = []


# --------------------------------------------------------------------------
# Agent loop: LLM turn -> tool calls -> error reflection -> repeat
# --------------------------------------------------------------------------

def run_agent_turn(
    provider: str,
    model: str,
    api_key: str,
    temperature: float,
    base_url: Optional[str] = None,
    attachments=None,
    live=None,
) -> str:
    """
    Drives the ReAct-style tool-calling loop for a single user turn.
    Returns the final assistant natural-language reply.
    `attachments` is the optional list of image attachments ({name, mime,
    bytes}) to send to the LLM as vision content.
    `live` is an optional st.status-like object with a .write() method. Every
    chunk of LLM reasoning text AND every lifecycle event (Robot connect /
    close / clear_structure) is streamed to it in real time, so the user can
    literally read "I'm about to do X" as it happens instead of only seeing
    tool names after the fact.
    """
    executor: ToolExecutor = st.session_state.tool_executor
    messages = st.session_state.messages

    def _log(msg: str) -> None:
        st.session_state.activity_log.append(msg)
        if live is not None:
            live.write(msg)

    def _drain_executor_activity() -> None:
        for ev in executor.drain_activity():
            _log(ev)

    final_text = ""
    empty_final_retries = 0

    for step in range(MAX_AGENT_STEPS):
        # [FIX H7] Check for stuck pattern
        if _is_stuck(st.session_state.error_signatures):
            last_sigs = ", ".join(dict.fromkeys(
                st.session_state.error_signatures[-3:])) or "unknown"
            final_text = (
                "I've encountered a persistent error pattern that I cannot "
                "resolve autonomously. Last failing tool(s): "
                f"`{last_sigs}`. Please rephrase your request, check the "
                "values you provided, or review the Activity Log for details."
            )
            messages.append({"role": "assistant", "content": final_text})
            break

        # [empty-final fix] Cap oversized tool-result payloads so the
        # transcript never overflows (the cause of blank final replies).
        messages = _compact_history(messages)

        try:
            response = call_llm(
                provider=provider,
                model=model,
                api_key=api_key,
                messages=messages,
                tool_schemas=executor.get_tool_schemas(),  # [WP1] + custom tools
                temperature=temperature,
                base_url=base_url,
                attachments=attachments,
            )
        except LLMProviderError as exc:
            if attachments and ("image" in str(exc).lower()
                                or "vision" in str(exc).lower()):
                # The endpoint rejected the image payload — honest fallback.
                return (
                    "I couldn't send the attached image to this model — it "
                    "may not support vision input. Please describe the sketch "
                    "in text (member layout, dimensions, supports) and I'll "
                    "continue."
                )
            error_msg = f"LLM provider error: {exc}"
            _log(f"❌ {error_msg}")
            return f"I couldn't reach the {provider} API: {exc}"

        _drain_executor_activity()

        # [OBS] Stream the model's reasoning text LIVE whenever it is non-empty,
        # even when it accompanies tool_calls (not just on the final reply).
        # This is the Step-3 design narrative: the user should read what the
        # model says it is about to do BEFORE the tool calls run.
        if response.tool_calls and (response.content or "").strip():
            chunk = response.content.strip()
            st.session_state.chat_display.append(
                {"role": "assistant", "content": chunk})
            if live is not None:
                live.write(f"💭 {chunk}")

        if not response.tool_calls:
            content = (response.content or "").strip()
            if content:
                final_text = content
                messages.append({"role": "assistant", "content": final_text})
                break
            # [empty-final fix] The model returned no text. Give it up to two
            # more chances on a compacted history; then fall back to a recap
            # so the user always receives a reply.
            if empty_final_retries < 2:
                empty_final_retries += 1
                messages = _compact_history(messages)
                _log("⚠️ LLM returned an empty final message; retrying once.")
                continue
            final_text = _summarize_activity()
            messages.append({"role": "assistant", "content": final_text})
            break

        # [FIX M6] Limit tool calls per step to prevent resource exhaustion
        tool_calls_to_execute = response.tool_calls[:MAX_TOOL_CALLS_PER_STEP]
        if len(response.tool_calls) > MAX_TOOL_CALLS_PER_STEP:
            st.session_state.activity_log.append(
                f"⚠️ LLM returned {len(response.tool_calls)} tool calls; "
                f"executing first {MAX_TOOL_CALLS_PER_STEP} only."
            )

        # [P7] Confirmation gate (HARD ENFORCEMENT, not just a prompt rule):
        # a batch run must NEVER start in the same turn it was staged. If the
        # model returns start_optimization_run AND confirm_and_start_optimization_run
        # together (e.g. because the user said "just run it"), we block the
        # confirm_* call, tell the LLM the run was NOT started, and force it to
        # present the estimate and wait for a separate user confirmation.
        names_in_response = {tc.name for tc in tool_calls_to_execute}
        if ("start_optimization_run" in names_in_response
                and "confirm_and_start_optimization_run" in names_in_response):
            st.session_state.activity_log.append(
                "🛑 Blocked confirm_and_start_optimization_run: a batch run "
                "cannot start in the same turn it was staged. Presenting the "
                "estimate and waiting for explicit user confirmation."
            )
            for tc in list(tool_calls_to_execute):
                if tc.name == "confirm_and_start_optimization_run":
                    tc.name = "__blocked_confirm__"  # renamed so it never dispatches
                    tc.arguments = {}
            names_in_response.discard("confirm_and_start_optimization_run")
            names_in_response.add("__blocked_confirm__")

        # Record the assistant's tool-call request in the transcript
        assistant_tool_msg = {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in tool_calls_to_execute
            ],
        }
        messages.append(assistant_tool_msg)

        for tool_call in tool_calls_to_execute:
            _log(f"🔧 Calling `{tool_call.name}` with args: {tool_call.arguments}")
            result_str = _execute_with_reflection(
                executor, tool_call.name, tool_call.arguments, messages
            )
            _drain_executor_activity()

            # [FIX H7] Track error signatures
            if '"status": "error"' in result_str:
                st.session_state.error_signatures.append(tool_call.name)
            else:
                # Reset on success — only consecutive failures count
                st.session_state.error_signatures = []

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.name,
                    "content": result_str,
                }
            )

        # Loop again so the LLM can react to tool results
    else:
        final_text = (
            "I reached the maximum number of automated tool-call steps for this "
            "request. Here is the current state — let me know if you'd like me "
            "to continue."
        )
        messages.append({"role": "assistant", "content": final_text})

    # [empty-final fix] Guarantee a non-empty reply under all exit paths.
    if not (final_text or "").strip():
        final_text = _summarize_activity()
        messages.append({"role": "assistant", "content": final_text})

    return final_text


def _is_stuck(error_signatures: List[str]) -> bool:
    """[FIX H7] Detects when the agent is stuck in a repeating error cycle."""
    if len(error_signatures) < MAX_STUCK_PATTERN_COUNT:
        return False
    # Check if the last N errors are all on the same tool
    recent = error_signatures[-MAX_STUCK_PATTERN_COUNT:]
    if len(set(recent)) == 1:
        return True
    # Also check if we have many different tools all failing
    if len(error_signatures) >= MAX_STUCK_PATTERN_COUNT * 2:
        last_n = error_signatures[-(MAX_STUCK_PATTERN_COUNT * 2):]
        unique_tools = len(set(last_n))
        if unique_tools <= 2 and len(last_n) >= 6:
            return True
    return False


def _execute_with_reflection(
    executor: ToolExecutor,
    tool_name: str,
    arguments: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> str:
    """
    Executes a tool call. On failure, the error is captured and returned as
    the tool result content (rather than raised), which lets the LLM see the
    error on its next turn and self-correct -- this IS the error-reflection
    mechanism, driven by the outer agent loop re-invoking the LLM after every
    tool result.
    """
    attempt = 0
    last_error = None

    # [P7] Confirmation gate special-case: this marker tool call never
    # dispatches (renamed by run_agent_turn when the model tried to start a
    # run in the same turn it staged it). Return a result that instructs the
    # LLM to present the estimate and wait for explicit confirmation.
    if tool_name == "__blocked_confirm__":
        return json.dumps({
            "status": "blocked",
            "error": ("The batch run was NOT started. start_optimization_run "
                      "stages the spec; confirm_and_start_optimization_run may "
                      "only be called in a LATER turn after the user has "
                      "explicitly approved the candidate count and time "
                      "estimate. Present the estimate now and stop."),
        })

    while attempt <= MAX_ERROR_RETRIES:
        try:
            result_str = executor.dispatch(tool_name, arguments)
            if attempt > 0:
                st.session_state.activity_log.append(
                    f"✅ `{tool_name}` succeeded after {attempt} retry(ies)."
                )
            return result_str
        except ToolExecutionError as exc:
            last_error = str(exc)
            st.session_state.activity_log.append(
                f"⚠️ `{tool_name}` failed (attempt {attempt + 1}): {last_error}"
            )
            transient = _looks_transient(last_error)
            if not transient:
                break
            attempt += 1

    error_payload = {
        "status": "error",
        "tool": tool_name,
        "message": last_error,
        "hint": (
            "Review the required call sequence: build geometry -> set "
            "supports -> create load case -> apply loads -> solve() -> "
            "export_* -> export_to_excel / generate_diagrams / "
            "generate_word_report. Adjust arguments or call order and "
            "retry."
        ),
    }
    return json.dumps(error_payload)


def _looks_transient(error_message: str) -> bool:
    """Heuristic: COM timing / RPC hiccups are worth a same-turn immediate
    retry; logical/argument errors are not (the LLM must fix those)."""
    transient_markers = ["RPC_E", "CoInitialize", "server is busy", "timeout", "CalcInProgress"]
    return any(marker.lower() in error_message.lower() for marker in transient_markers)


# --------------------------------------------------------------------------
# Non-empty reply guarantees (empty-final-message fix)
# --------------------------------------------------------------------------

MAX_TOOL_RESULT_CHARS = 600  # cap for per-tool result text fed back to the LLM

# Map tool names to short phrases for the activity-based closing recap.
TOOL_PHRASE = {
    "new_2d_frame": "created the 2D frame",
    "new_3d_frame": "created the 3D frame",
    "create_node": "added the nodes",
    "create_bar": "created the members/bars",
    "set_support": "applied the supports",
    "create_load_case": "set up the load case(s)",
    "apply_bar_load": "applied a distributed bar load",
    "apply_bar_concentrated": "applied a concentrated load",
    "apply_nodal_load": "applied nodal loads",
    "solve": "ran the Finite Element solve",
    "solve_modal": "ran a modal analysis",
    "solve_buckling": "ran a buckling analysis",
    "get_utilization_ratios": "computed utilization ratios",
    "define_combination": "defined the load combination",
    "list_combinations": "listed the load combinations",
    "solve_combination": "solved cases and combinations",
    "get_governing_combination": "identified the governing combination",
    "export_member_forces": "exported member forces",
    "export_reactions": "exported support reactions",
    "export_bill_of_materials": "exported the bill of materials",
    "export_node_displacements": "exported nodal displacements",
    "export_bar_stresses": "exported bar stresses",
    "export_results_to_excel": "built the results Excel workbook",
    "set_material": "set the material",
    "create_panel": "created the panel grillage",
    "set_panel_thickness": "set the panel thickness",
    "apply_panel_pressure": "applied the panel pressure",
    "create_solid": "created the solid volume",
    "create_solid_box": "created the solid box volume",
    "solve_modal": "attempted the modal analysis",
    "export_modal_frequencies": "exported the modal frequencies",
    "export_modal_mode_shapes": "exported the modal mode shape",
    "export_to_excel": "produced an Excel workbook",
    "generate_diagrams": "generated SFD/BMD diagrams",
    "generate_word_report": "generated the Word report",
    "generate_powerpoint_report": "generated the PowerPoint deck",
    "create_structure_from_spec": "built the structure from a model spec",
    "create_rectangular_grid_frame": "created a grid frame",
    "create_truss": "created a truss",
    "create_braced_frame": "created a braced frame",
    "get_structure_summary": "verified the model summary",
    "clear_structure": "cleared the previous model",
    "create_load_combination": "created a load combination",
}


def _compact_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bounds the LLM payload so the context never overflows.

    Every message is kept (preserving the OpenAI tool_call<->tool pairing),
    but long `tool` result payloads (e.g. exported DataFrames) are truncated
    to a safe length. This stops oversized transcripts from causing empty
    final answers, especially after large Milestone-A models.
    """
    capped: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            content = m["content"]
            if len(content) > MAX_TOOL_RESULT_CHARS:
                m = {**m, "content": content[:MAX_TOOL_RESULT_CHARS] + "...[truncated]"}
        capped.append(m)
    return capped


def _summarize_activity() -> str:
    """Builds a short, non-empty closing recap from the session activity log,
    used as a fallback whenever the LLM replies with no text."""
    order: List[str] = []
    seen: set = set()
    for entry in st.session_state.activity_log:
        if "Calling `" in entry and "` with args" in entry:
            name = entry.split("Calling `", 1)[1].split("`", 1)[0]
            if name not in seen:
                seen.add(name)
                order.append(name)
    phrases = [TOOL_PHRASE.get(n, f"ran `{n}`") for n in order]
    if phrases:
        recap = "Done. I completed: " + "; ".join(phrases) + "."
    else:
        recap = (
            "Done. I've finished processing your request. See the Activity Log "
            "and the 'Generated Artifacts' panel in the sidebar for details."
        )
    recap += (
        " Let me know if you'd like me to adjust the model, change the analysis, "
        "or generate other reports (Excel / Word / PowerPoint)."
    )
    return recap


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

def render_sidebar() -> Dict[str, Any]:
    st.sidebar.title("⚙️ Agent Configuration")

    # [.env support] Pre-fill defaults from .env when present (still editable).
    from agent.llm_providers import API_KEY_ENV_VARS
    env_provider = os.environ.get("STRUCTURAL_AGENT_PROVIDER", "")
    env_model = os.environ.get("STRUCTURAL_AGENT_MODEL", "")
    provider_index = 0
    if env_provider in PROVIDERS:
        provider_index = list(PROVIDERS.keys()).index(env_provider)
    provider = st.sidebar.selectbox(
        "LLM Provider", options=list(PROVIDERS.keys()), index=provider_index)
    default_model = PROVIDERS[provider]["default_model"]
    model_value = env_model if (env_model and provider == env_provider) else default_model
    model = st.sidebar.text_input("Model", value=model_value)
    # Per-provider API key from .env (e.g. DEEPSEEK_API_KEY for DeepSeek).
    key_env = API_KEY_ENV_VARS.get(provider, "")
    env_key = os.environ.get(key_env, "") if key_env else ""
    api_key = st.sidebar.text_input(f"{provider} API Key", type="password",
                                    value=env_key)
    temperature = st.sidebar.slider("Temperature", 0.0, 1.0, 0.2, 0.05)

    # [DeepSeek / Custom] "Custom (OpenAI-compatible)" lets the user point the
    # agent at ANY OpenAI-style /chat/completions endpoint (Ollama, LM Studio,
    # vLLM, Together, Groq, Azure OpenAI, a corporate proxy...).
    custom_url = ""
    if provider == "Custom (OpenAI-compatible)":
        custom_url = st.sidebar.text_input(
            "OpenAI-compatible Base URL",
            value="http://localhost:11434/v1/chat/completions",
        )
        if not custom_url.strip():
            st.sidebar.warning("Enter the full endpoint URL, ending in /chat/completions.")
        if not model:
            st.sidebar.warning("Enter the model name in the Model field above.")

    st.sidebar.markdown("---")
    st.sidebar.subheader("📎 Attachments")
    uploaded = st.sidebar.file_uploader(
        "Sketches / photos / PDFs — used as context for your NEXT message "
        "(then cleared)",
        type=["png", "jpg", "jpeg", "pdf"],
        accept_multiple_files=True,
    )
    if uploaded:
        _ingest_attachments(uploaded)
    atts = list(st.session_state.get("_attachments") or [])
    if atts:
        for a in atts:
            st.sidebar.caption(
                f"{'🖼️' if a['kind'] == 'image' else '📄'} {a['name']} "
                f"({a['kind']}"
                + (f", {len(a.get('text') or '')} chars of text"
                   if a["kind"] == "pdf" else "")
                + ")")
        if st.sidebar.button("🗑️ Clear attachments"):
            st.session_state._attachments = []
            st.rerun()
    else:
        st.sidebar.caption("No attachments pending.")

    st.sidebar.markdown("---")
    st.sidebar.subheader("🖥️ Robot Session")
    # [OBS] Drain any pending lifecycle events (connect/close/clear) into the
    # Activity Log panel so they're visible without needing the terminal.
    for ev in st.session_state.tool_executor.drain_activity():
        st.session_state.activity_log.append(ev)
    robot = st.session_state.tool_executor.robot
    pid = robot.pid if robot._connected else None
    if pid is not None:
        st.sidebar.caption(
            f"🟢 Robot.exe PID: **{pid}**\n\n_If this number changes between "
            "turns without you clicking Reset, a NEW Robot process was "
            "launched (close+relaunch). If it stays the same, the SAME "
            "session is being cleared and rebuilt._")
    else:
        st.sidebar.caption("⚪ Robot not connected (PID will appear on first use).")
    robot_visible = st.sidebar.checkbox("Show Robot application window", value=True)
    # [FIX M5] Use public setter instead of directly accessing private attribute
    st.session_state.tool_executor.set_robot_visible(robot_visible)

    if st.sidebar.button("🔌 Reset Robot / Agent Session"):
        try:
            old_pid = st.session_state.tool_executor.robot.pid
            st.session_state.tool_executor.robot.close()
            st.session_state.activity_log.append(
                f"🔌 Robot closed by user-triggered Reset (was PID {old_pid}).")
        except Exception:
            pass
        st.session_state.tool_executor = ToolExecutor(robot_visible=robot_visible)
        st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        st.session_state.chat_display = []
        st.session_state.activity_log = []
        st.session_state.error_signatures = []
        st.session_state._attachments = []
        st.sidebar.success("Session reset.")
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.subheader("📁 Generated Artifacts")
    generated_files = st.session_state.tool_executor.generated_files
    if not generated_files:
        st.sidebar.caption("No files generated yet this session.")
    else:
        for name, path in generated_files.items():
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    st.sidebar.download_button(
                        label=f"⬇️ {name}",
                        data=f.read(),
                        file_name=name,
                        mime=_mime_for(name),
                        key=f"dl_{name}",
                    )

    st.sidebar.markdown("---")
    with st.sidebar.expander("🩺 Activity / Tool Log", expanded=False):
        for entry in st.session_state.activity_log[-50:]:
            st.write(entry)

    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "base_url": custom_url.strip(),
    }


def _mime_for(file_name: str) -> str:
    if file_name.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if file_name.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if file_name.endswith(".pptx"):
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if file_name.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


# --------------------------------------------------------------------------
# [ATTACH] Photo / PDF import helpers
# --------------------------------------------------------------------------

def _extract_pdf_text(data: bytes) -> str:
    """Best-effort PDF text extraction via pypdf. Returns "" on any failure
    (e.g. scanned / image-only PDFs, or pypdf not installed)."""
    try:
        import io as _io
        from pypdf import PdfReader
        reader = PdfReader(_io.BytesIO(data))
        parts = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("PDF text extraction skipped (%s).", exc)
        return ""


def _ingest_attachments(files) -> None:
    """Stores uploaded images/PDFs in session state for the next user turn."""
    atts = list(st.session_state.get("_attachments") or [])
    existing = {a["name"] for a in atts}
    for f in files or []:
        name = getattr(f, "name", "attachment")
        if name in existing:
            continue
        data = f.getvalue()
        lower = name.lower()
        kind = "pdf" if lower.endswith(".pdf") else "image"
        ext = lower.rsplit(".", 1)[-1]
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "pdf": "application/pdf"}.get(ext, "application/octet-stream")
        text = _extract_pdf_text(data) if kind == "pdf" else ""
        atts.append({"name": name, "kind": kind, "mime": mime,
                     "bytes": data, "text": text[:6000]})
        existing.add(name)
    st.session_state._attachments = atts


def render_chat():
    st.title("🏗️ Structural Multi-App Agent")
    st.caption(
        "A unified AI copilot controlling Autodesk Robot Structural Analysis, "
        "Excel, Word, and diagram generation for end-to-end structural "
        "engineering workflows."
    )

    for turn in st.session_state.chat_display:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            for a in turn.get("attachments") or []:
                if a["kind"] == "image":
                    st.image(a["bytes"], width=320)
                else:
                    st.caption(f"📄 {a['name']}")

    user_input = st.chat_input(
        "e.g. Build a 2-bay 6m frame, run 25 kN/m dead load, export to "
        "'Frame_Results.xlsx', and generate 'Frame_Report.docx' with moment diagrams."
    )

    if user_input:
        # [ATTACH] Consume pending attachments (one-shot per turn).
        pending = list(st.session_state.get("_attachments") or [])
        st.session_state._attachments = []
        image_attachments = [a for a in pending if a["kind"] == "image"]

        user_content = user_input
        if pending:
            imgs = [a["name"] for a in pending if a["kind"] == "image"]
            pdfs = [a for a in pending if a["kind"] == "pdf"]
            bits = []
            if imgs:
                bits.append(f"{len(imgs)} image(s): {', '.join(imgs)}")
            if pdfs:
                bits.append(f"{len(pdfs)} PDF(s): {', '.join(a['name'] for a in pdfs)}")
            user_content += "\n\n[User attached: " + "; ".join(bits) + "]"
            pdf_text = "\n\n".join(
                f"PDF '{a['name']}':\n{a.get('text', '')}" for a in pdfs)
            if pdf_text.strip():
                user_content += "\n\n--- Extracted PDF text ---\n" + pdf_text[:6000]

        st.session_state.chat_display.append({
            "role": "user", "content": user_input, "attachments": pending})
        st.session_state.messages.append({"role": "user", "content": user_content})

        with st.chat_message("user"):
            st.markdown(user_input)
            for a in pending:
                if a["kind"] == "image":
                    st.image(a["bytes"], width=320)
                else:
                    st.caption(f"📄 {a['name']} — "
                               f"{len(a.get('text') or '')} chars of text extracted")

        with st.chat_message("assistant"):
            config = st.session_state.get("_config", {})
            if not config.get("api_key"):
                reply = (
                    "Please enter your API key for the selected provider in the "
                    "sidebar before sending a request."
                )
            else:
                # [OBS] Live status: streams the LLM's reasoning text and every
                # tool call / lifecycle event in real time instead of only
                # showing tool names after the fact.
                with st.status(
                    "Working across Robot / Excel / Word / diagrams...",
                    expanded=True,
                ) as status:
                    try:
                        reply = run_agent_turn(
                            provider=config["provider"],
                            model=config["model"],
                            api_key=config["api_key"],
                            temperature=config["temperature"],
                            base_url=config.get("base_url"),
                            attachments=image_attachments,
                            live=status,
                        )
                    except Exception as exc:
                        tb = traceback.format_exc(limit=6)
                        logger.error("Unhandled agent error: %s\n%s", exc, tb)
                        reply = (
                            f"An unexpected error stopped the agent: `{exc}`. "
                            f"Check the Activity Log in the sidebar for details."
                        )
                status.update(label="Done", state="complete")
                st.markdown(reply)

        st.session_state.chat_display.append({"role": "assistant", "content": reply})
        st.rerun()


def main():
    st.set_page_config(
        page_title="Structural Multi-App Agent",
        page_icon="🏗️",
        layout="wide",
    )
    init_session_state()
    config = render_sidebar()
    st.session_state["_config"] = config
    render_chat()


if __name__ == "__main__":
    main()
