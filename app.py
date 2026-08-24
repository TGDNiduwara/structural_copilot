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
from typing import Any

import streamlit as st
import structlog

from agent.conversation_store import ConversationStore, default_db_path  # [FIX 10]
from agent.history import compact_messages  # [FIX 12]
from agent.llm_providers import PROVIDERS, LLMProviderError, call_llm
from agent.token_tracker import TokenTracker  # [FIX 11]
from agent.tool_registry import ToolExecutionError, ToolExecutor
from config import (  # [FIX 03] centralized config (was hardcoded literals)
    MAX_AGENT_STEPS,
    MAX_ERROR_RETRIES,
    MAX_STUCK_PATTERN_COUNT,
    MAX_TOOL_CALLS_PER_STEP,
    MAX_TOOL_RESULT_CHARS,
)

# [FIX 08] Structured logging via structlog.  Migrated modules render JSON
# lines (add_log_level + iso timestamp + JSON renderer).  Non-migrated stdlib
# loggers keep the basicConfig handler below so nothing is lost.
logging.basicConfig(level=logging.INFO)
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logger = structlog.get_logger("structural_copilot.app")

# [FIX 13] Sentry error tracking (opt-in via SENTRY_DSN env var).
if os.environ.get("SENTRY_DSN"):
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=0.1)
        logger.info("Sentry enabled")
    except Exception as _sentry_exc:  # noqa: BLE001 - never break startup
        logger.warning("Sentry init failed: %s", _sentry_exc)

# --------------------------------------------------------------------------
# Minimal .env loader (no external dependency). Reads KEY=VALUE lines from
# <project>/.env into os.environ if not already set. Used to pre-fill the
# sidebar with provider/model/api-key defaults so the key never needs to be
# re-entered every session. .env is gitignored - real keys are never committed.
# --------------------------------------------------------------------------


def load_env_file(env_path: str | None = None) -> None:
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
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

# [FIX 07] SYSTEM_PROMPT externalized to prompts/system_prompt_v1.md (was an
# inline ~240-line constant). Override via STRUCTURAL_AGENT_SYSTEM_PROMPT.
_PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "prompts", "system_prompt_v1.md"
)


def _load_prompt() -> str:
    """Loads the system prompt from prompts/system_prompt_v1.md.
    [FIX 07] Prompt externalized so it can be edited without touching code.
    """
    env_override = os.environ.get("STRUCTURAL_AGENT_SYSTEM_PROMPT")
    if env_override:
        return env_override
    with open(_PROMPT_FILE, encoding="utf-8") as fh:
        return fh.read()


SYSTEM_PROMPT = _load_prompt()


# --------------------------------------------------------------------------
# Session state initialization
# --------------------------------------------------------------------------


def init_session_state():
    # [FIX M4] Use simple assignment instead of PEP 526 type annotations
    # on session_state, which can cause serialization issues in some
    # Streamlit versions.
    if "messages" not in st.session_state:
        st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
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
    # [FIX 10] SQLite conversation persistence + stable per-session id.
    if "conversation_store" not in st.session_state:
        st.session_state.conversation_store = ConversationStore(db_path=default_db_path())
    if "conversation_id" not in st.session_state:
        import uuid

        st.session_state.conversation_id = str(uuid.uuid4())
    # [FIX 10] Opt-in resume: set STRUCTURAL_AGENT_CONVERSATION_ID to load a saved transcript.
    resume_id = os.environ.get("STRUCTURAL_AGENT_CONVERSATION_ID")
    if resume_id and len(st.session_state.messages) == 1:
        loaded = st.session_state.conversation_store.get_conversation(resume_id)
        if loaded:
            st.session_state.messages = st.session_state.messages[:1] + loaded
            st.session_state.conversation_id = resume_id
    # [FIX 11] Daily token/cost budget tracker for LLM calls.
    if "token_tracker" not in st.session_state:
        st.session_state.token_tracker = TokenTracker()


# --------------------------------------------------------------------------
# Agent loop: LLM turn -> tool calls -> error reflection -> repeat
# --------------------------------------------------------------------------


def run_agent_turn(
    provider: str,
    model: str,
    api_key: str,
    temperature: float,
    base_url: str | None = None,
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
    # [FIX 15] Resolve the API key through secret_manager - never session_state.
    from agent.secret_manager import resolve_llm_key

    api_key = resolve_llm_key(provider, api_key)
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

    # [FIX 11] Enforce the daily token/cost budget before any LLM call.
    tracker: TokenTracker = st.session_state.token_tracker
    if tracker.is_over_budget():
        summary = tracker.get_usage_summary()
        return (
            "⚠️ I've hit the configured daily token/cost budget "
            f"({summary['total_tokens']} tokens, ${summary['estimated_cost_usd']:.2f}). "
            "Review config.py / STRUCTURAL_AGENT_DAILY_TOKEN_BUDGET and retry later."
        )

    for _step in range(MAX_AGENT_STEPS):  # [FIX 09] loop var unused
        # [FIX H7] Check for stuck pattern
        if _is_stuck(st.session_state.error_signatures):
            last_sigs = (
                ", ".join(dict.fromkeys(st.session_state.error_signatures[-3:])) or "unknown"
            )
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
            if attachments and ("image" in str(exc).lower() or "vision" in str(exc).lower()):
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

        # [FIX 11] Record provider-reported token usage into the tracker.
        if getattr(response, "usage", None):
            _usage = response.usage or {}
            tracker.add_usage(
                prompt_tokens=_usage.get("prompt_tokens") or _usage.get("promptTokenCount") or 0,
                completion_tokens=_usage.get("completion_tokens")
                or _usage.get("candidatesTokenCount")
                or 0,
            )

        # [OBS] Stream the model's reasoning text LIVE whenever it is non-empty,
        # even when it accompanies tool_calls (not just on the final reply).
        # This is the Step-3 design narrative: the user should read what the
        # model says it is about to do BEFORE the tool calls run.
        if response.tool_calls and (response.content or "").strip():
            chunk = response.content.strip()
            st.session_state.chat_display.append({"role": "assistant", "content": chunk})
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
        if (
            "start_optimization_run" in names_in_response
            and "confirm_and_start_optimization_run" in names_in_response
        ):
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


def _is_stuck(error_signatures: list[str]) -> bool:
    """[FIX H7] Detects when the agent is stuck in a repeating error cycle."""
    if len(error_signatures) < MAX_STUCK_PATTERN_COUNT:
        return False
    # Check if the last N errors are all on the same tool
    recent = error_signatures[-MAX_STUCK_PATTERN_COUNT:]
    if len(set(recent)) == 1:
        return True
    # Also check if we have many different tools all failing
    if len(error_signatures) >= MAX_STUCK_PATTERN_COUNT * 2:
        last_n = error_signatures[-(MAX_STUCK_PATTERN_COUNT * 2) :]
        unique_tools = len(set(last_n))
        if unique_tools <= 2 and len(last_n) >= 6:
            return True
    return False


def _execute_with_reflection(
    executor: ToolExecutor,
    tool_name: str,
    arguments: dict[str, Any],
    messages: list[dict[str, Any]],
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
        return json.dumps(
            {
                "status": "blocked",
                "error": (
                    "The batch run was NOT started. start_optimization_run "
                    "stages the spec; confirm_and_start_optimization_run may "
                    "only be called in a LATER turn after the user has "
                    "explicitly approved the candidate count and time "
                    "estimate. Present the estimate now and stop."
                ),
            }
        )

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

# [FIX 03] MAX_TOOL_RESULT_CHARS now comes from config (was a 600 literal here)

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


def _compact_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bounds the LLM payload so the context never overflows.

    Every message is kept (preserving the OpenAI tool_call<->tool pairing),
    but long `tool` result payloads (e.g. exported DataFrames) are truncated
    to a safe length. This stops oversized transcripts from causing empty
    final answers, especially after large Milestone-A models.
    """
    # [FIX 12] delegate to agent/history.compact_messages (structured summary)
    return compact_messages(messages, max_tool_chars=MAX_TOOL_RESULT_CHARS)


def _summarize_activity() -> str:
    """Builds a short, non-empty closing recap from the session activity log,
    used as a fallback whenever the LLM replies with no text."""
    order: list[str] = []
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


def render_sidebar() -> dict[str, Any]:
    st.sidebar.title("⚙️ Agent Configuration")

    # [.env support] Pre-fill defaults from .env when present (still editable).
    from agent.llm_providers import API_KEY_ENV_VARS

    env_provider = os.environ.get("STRUCTURAL_AGENT_PROVIDER", "")
    env_model = os.environ.get("STRUCTURAL_AGENT_MODEL", "")
    provider_index = 0
    if env_provider in PROVIDERS:
        provider_index = list(PROVIDERS.keys()).index(env_provider)
    provider = st.sidebar.selectbox(
        "LLM Provider", options=list(PROVIDERS.keys()), index=provider_index
    )
    default_model = PROVIDERS[provider]["default_model"]
    model_value = env_model if (env_model and provider == env_provider) else default_model
    model = st.sidebar.text_input("Model", value=model_value)
    # Per-provider API key. [FIX 15] resolved via secret_manager (os.environ /
    # streamlit secrets / .env), never stored in session_state.
    from agent.secret_manager import get_secret

    key_env = API_KEY_ENV_VARS.get(provider, "")
    env_key = get_secret(key_env) or "" if key_env else ""
    key_source = st.sidebar.selectbox(
        "API key source",
        options=["Use configured key (.env / secrets)", "Enter custom key"],
    )
    if key_source == "Enter custom key":
        api_key = st.sidebar.text_input(f"{provider} API Key (custom)", type="password")
    else:
        api_key = env_key
        if api_key:
            st.sidebar.caption("🔑 Using configured key (not shown).")
        else:
            st.sidebar.caption("No configured key found — enter a custom key to use this provider.")
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
        "Sketches / photos / PDFs — used as context for your NEXT message (then cleared)",
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
                + (f", {len(a.get('text') or '')} chars of text" if a["kind"] == "pdf" else "")
                + ")"
            )
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
            "session is being cleared and rebuilt._"
        )
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
                f"🔌 Robot closed by user-triggered Reset (was PID {old_pid})."
            )
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
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "pdf": "application/pdf",
        }.get(ext, "application/octet-stream")
        text = _extract_pdf_text(data) if kind == "pdf" else ""
        atts.append({"name": name, "kind": kind, "mime": mime, "bytes": data, "text": text[:6000]})
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
            pdf_text = "\n\n".join(f"PDF '{a['name']}':\n{a.get('text', '')}" for a in pdfs)
            if pdf_text.strip():
                user_content += "\n\n--- Extracted PDF text ---\n" + pdf_text[:6000]

        st.session_state.chat_display.append(
            {"role": "user", "content": user_input, "attachments": pending}
        )
        st.session_state.messages.append({"role": "user", "content": user_content})
        # [FIX 10] persist the user turn for resume support
        st.session_state.conversation_store.save_message(
            st.session_state.conversation_id, "user", user_content
        )

        with st.chat_message("user"):
            st.markdown(user_input)
            for a in pending:
                if a["kind"] == "image":
                    st.image(a["bytes"], width=320)
                else:
                    st.caption(
                        f"📄 {a['name']} — {len(a.get('text') or '')} chars of text extracted"
                    )

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
        # [FIX 10] persist the assistant reply for resume support
        st.session_state.conversation_store.save_message(
            st.session_state.conversation_id, "assistant", reply
        )
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
