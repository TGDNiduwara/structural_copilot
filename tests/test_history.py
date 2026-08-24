"""Tests for agent/history.compact_messages (Step 12)."""

from __future__ import annotations

from agent.history import compact_messages


def _msg(role, content="x" * 100, name=None):
    m = {"role": role, "content": content}
    if name:
        m["name"] = name
    return m


def test_short_transcript_unchanged():
    msgs = [_msg("system"), _msg("user"), _msg("assistant")]
    out = compact_messages(msgs, recent=12)
    assert out == msgs


def test_long_tool_payload_truncated():
    msgs = [_msg("user"), _msg("tool", "A" * 5000, name="export_reactions")]
    out = compact_messages(msgs, max_tool_chars=600)
    tool = [m for m in out if m["role"] == "tool"][0]
    assert len(tool["content"]) == 600 + len("...[truncated]")
    assert tool["content"].endswith("...[truncated]")


def test_system_prompt_always_preserved():
    msgs = [_msg("system"), _msg("user")] + [_msg("assistant")] * 20
    out = compact_messages(msgs, recent=5)
    assert out[0]["role"] == "system"
    # summary marker is an assistant message
    assert out[1]["role"] == "assistant"
    assert "compacted" in out[1]["content"]
    assert len(out) < len(msgs)


def test_recent_messages_kept_verbatim():
    msgs = [_msg("system")] + [_msg("assistant", f"msg {i}") for i in range(15)]
    out = compact_messages(msgs, recent=6)
    tail = [m["content"] for m in out[-6:]]
    assert tail == [f"msg {i}" for i in range(9, 15)]


def test_tool_call_pairing_preserved_in_recent():
    msgs = [
        _msg("system"),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "solve"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    out = compact_messages(msgs, recent=3)
    assert out[1]["tool_calls"][0]["id"] == "c1"
    assert out[2]["tool_call_id"] == "c1"
