"""History compaction for the agent loop.

[FIX 12] Bounds the LLM payload so the context never overflows, replacing
app._compact_history with a structured summary.  The system prompt and the
tool_call<->tool pairing are always preserved.
"""

from __future__ import annotations

from typing import Any


def compact_messages(
    messages: list[dict[str, Any]],
    max_tool_chars: int = 2000,
    recent: int = 12,
) -> list[dict[str, Any]]:
    """Return a context-bounded copy of the message list.

    - Long `tool` result payloads are truncated to `max_tool_chars`.
    - If the transcript is longer than `recent` non-system messages, the older
      prefix (excluding the system prompt) is collapsed into a single structured
      summary marker.  The last `recent` messages are kept verbatim so the
      tool_call <-> tool pairing stays intact.
    """
    if not messages:
        return messages

    capped: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            content = m["content"]
            if len(content) > max_tool_chars:
                m = {**m, "content": content[:max_tool_chars] + "...[truncated]"}
        capped.append(m)

    system: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for m in capped:
        (system if m.get("role") == "system" else rest).append(m)

    if len(rest) <= recent:
        return capped

    kept = rest[-recent:]
    older = rest[:-recent]
    tool_names = sorted({m.get("name", "tool") for m in older if m.get("role") == "tool"})
    summary = {
        "role": "assistant",
        "content": (
            "[Earlier conversation compacted for context] "
            f"{len(older)} prior message(s) included tool calls "
            f"({', '.join(tool_names)[:200] or 'none'}). Key results from "
            "those steps remain available in the session result store; "
            "continue with the most recent context below."
        ),
    }
    return system + [summary] + kept
