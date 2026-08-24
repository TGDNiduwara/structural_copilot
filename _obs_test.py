"""
_obs_test.py - Multi-turn observability test (live LLM).

Drives the ACTUAL agent loop (SYSTEM_PROMPT, ToolExecutor, dispatch) across
multiple turns, capping steps per turn so a "continue" is required. Captures:
  * every chunk of reasoning text the LLM emits BEFORE tool calls
  * the Robot.exe PID before/after each turn
  * every clear_structure call and which turn it happened in

This tells us definitively whether the model tears down and rebuilds every
turn (same PID + repeated clear_structure) or actually relaunches Robot
(PID changes without a Reset).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, r"c:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot")

import app  # noqa: E402  (loads .env, SYSTEM_PROMPT)
from agent.llm_providers import API_KEY_ENV_VARS, call_llm  # noqa: E402
from agent.tool_registry import ToolExecutionError, ToolExecutor  # noqa: E402

TURN1 = (
    "Build a 2-bay 6 m portal frame in IPE 300 / HEA 200 with pinned bases, "
    "add a 10 kN/m dead load case, then solve it and export results."
)
TURN2 = "continue"
STEPS_PER_TURN = 8  # force a multi-turn build


def _build_executor() -> ToolExecutor:
    ex = ToolExecutor()
    return ex


def _run_turn(ex: ToolExecutor, messages, max_steps: int) -> dict:
    """Replicates run_agent_turn's loop (app.py). Returns transcript info."""
    provider = os.environ.get("STRUCTURAL_AGENT_PROVIDER", "OpenAI")
    model = os.environ.get("STRUCTURAL_AGENT_MODEL", "gpt-4o")
    key_env = API_KEY_ENV_VARS.get(provider, "")
    api_key = os.environ.get(key_env, "") if key_env else ""

    reasoning = []  # every non-empty response.content before tool calls
    tool_names = []
    lifecycle = []  # drained executor activity events
    final_text = ""

    for step in range(max_steps):
        response = call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            messages=messages,
            tool_schemas=ex.get_tool_schemas(),
            temperature=0.2,
            base_url=None,
            attachments=None,
        )
        lifecycle += ex.drain_activity()
        if (response.content or "").strip():
            reasoning.append(response.content.strip())
        if not response.tool_calls:
            final_text = (response.content or "").strip()
            break
        tcs = list(response.tool_calls[: app.MAX_TOOL_CALLS_PER_STEP])
        # [P7] confirmation gate (same as app.py)
        names = {tc.name for tc in tcs}
        if "start_optimization_run" in names and "confirm_and_start_optimization_run" in names:
            for tc in tcs:
                if tc.name == "confirm_and_start_optimization_run":
                    tc.name = "__blocked_confirm__"
                    tc.arguments = {}
        assistant_msg = {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in tcs
            ],
        }
        messages.append(assistant_msg)
        for tc in tcs:
            tool_names.append(tc.name)
            if tc.name == "__blocked_confirm__":
                result = json.dumps({"status": "blocked", "error": "not started"})
            else:
                try:
                    result = ex.dispatch(tc.name, tc.arguments)
                except ToolExecutionError as exc:
                    result = json.dumps({"status": "error", "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    result = json.dumps({"status": "error", "error": str(exc)})
            lifecycle += ex.drain_activity()
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": result}
            )

    lifecycle += ex.drain_activity()
    return {
        "reasoning": reasoning,
        "tool_names": tool_names,
        "lifecycle": lifecycle,
        "final": final_text,
    }


def main() -> int:
    ex = _build_executor()
    messages = [{"role": "system", "content": app.SYSTEM_PROMPT}]

    print("=" * 72)
    print("MULTI-TURN OBSERVABILITY TEST (live LLM, real agent loop)")
    print("=" * 72)

    # Capture PID at each turn boundary.
    def pid():
        r = ex.robot
        return r.pid if r._connected else None

    pids = []
    for turn_no, prompt in ((1, TURN1), (2, TURN2)):
        pids.append((f"before-turn-{turn_no}", pid()))
        messages.append({"role": "user", "content": prompt})
        print(f"\n===== TURN {turn_no}: {prompt!r} =====")
        out = _run_turn(ex, messages, STEPS_PER_TURN)
        pids.append((f"after-turn-{turn_no}", pid()))
        print("REASONING TEXT (before tool calls):")
        for i, r in enumerate(out["reasoning"], 1):
            print(f"  [{i}] {r[:400]}")
        print(f"TOOL CALLS: {out['tool_names']}")
        if out["lifecycle"]:
            print("LIFECYCLE EVENTS:")
            for ev in out["lifecycle"]:
                print(f"  {ev}")
        else:
            print("LIFECYCLE EVENTS: (none this turn)")
        if out["final"]:
            print(f"FINAL REPLY: {out['final'][:250]}")
        if turn_no == 1 and "maximum number" not in out["final"] and out["final"]:
            # it finished; still force a 2nd turn to observe PID continuity
            print("  (turn 1 finished; observing turn 2 PID anyway)")

    print("\n" + "=" * 72)
    print("PID READOUT ACROSS TURNS:")
    for label, p in pids:
        print(f"  {label}: {p}")
    # Compare only the CONNECTED PIDs (skip the initial pre-connection None).
    connected = [p for _, p in pids if p is not None]
    print()
    if len(connected) < 2:
        print("VERDICT: Robot never connected during the test - no PID evidence.")
    elif len(set(connected)) == 1:
        print(
            f"VERDICT: SAME PID ({connected[0]}) across all turns - Robot was "
            "NOT closed/relaunched. Check the lifecycle log for clear_structure "
            "calls: if the model clears every turn it is the clear-and-rebuild "
            "bug; if it never clears, the session is genuinely reused."
        )
    else:
        print(
            f"VERDICT: PID CHANGED across turns ({sorted(set(connected))}) - "
            "Robot process was actually closed and relaunched."
        )
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
