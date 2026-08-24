"""SQLite-backed conversation persistence.

[FIX 10] Messages are persisted per conversation so a session can be resumed
(e.g. after a Streamlit rerun or container restart).  The system prompt is
never persisted (it is always loaded from prompts/system_prompt_v1.md).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any


class ConversationStore:
    """Simple, thread-safe-enough SQLite store for chat transcripts."""

    def __init__(self, db_path: str = "conversations.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT NOT NULL,
                    message_order  INTEGER NOT NULL,
                    role           TEXT NOT NULL,
                    content        TEXT,
                    metadata       TEXT,
                    created_at     TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_cid ON conversations(conversation_id)"
            )

    def save_message(
        self,
        conversation_id: str,
        role: str,
        content: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist one message.  The system prompt is ignored by design."""
        if role == "system":
            return
        if not isinstance(content, str):
            content = json.dumps(content, default=str)
        order = int(datetime.now(timezone.utc).timestamp() * 1000)
        with self._conn:
            self._conn.execute(
                "INSERT INTO conversations "
                "(conversation_id, message_order, role, content, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    order,
                    role,
                    content,
                    json.dumps(metadata) if metadata else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_conversation(self, conversation_id: str) -> list[dict[str, str]]:
        """Return the persisted messages for a conversation, in order."""
        rows = self._conn.execute(
            "SELECT role, content FROM conversations "
            "WHERE conversation_id = ? ORDER BY message_order",
            (conversation_id,),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent conversations (id, message count, last activity)."""
        rows = self._conn.execute(
            "SELECT conversation_id, COUNT(*) AS n, MAX(created_at) AS last "
            "FROM conversations GROUP BY conversation_id "
            "ORDER BY last DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def __del__(self):
        with contextlib.suppress(Exception):
            self.close()


def default_db_path() -> str:
    """Project-root conversations.db (gitignored)."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "conversations.db",
    )
