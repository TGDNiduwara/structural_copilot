"""Tests for agent.conversation_store.ConversationStore (Step 10)."""

from __future__ import annotations

from agent.conversation_store import ConversationStore


def test_save_and_get(tmp_path):
    store = ConversationStore(db_path=str(tmp_path / "conv.db"))
    store.save_message("conv-a", "user", "hello")
    store.save_message("conv-a", "assistant", "hi there")
    store.save_message("conv-a", "system", "should be ignored")
    msgs = store.get_conversation("conv-a")
    assert len(msgs) == 2
    assert msgs[0] == {"role": "user", "content": "hello"}
    assert msgs[1] == {"role": "assistant", "content": "hi there"}
    store.close()


def test_list_conversations(tmp_path):
    store = ConversationStore(db_path=str(tmp_path / "conv.db"))
    store.save_message("c1", "user", "a")
    store.save_message("c2", "user", "b")
    rows = store.list_conversations()
    assert {r["conversation_id"] for r in rows} == {"c1", "c2"}
    store.close()


def test_get_missing_conversation(tmp_path):
    store = ConversationStore(db_path=str(tmp_path / "conv.db"))
    assert store.get_conversation("nope") == []
    store.close()
