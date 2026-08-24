"""
batch/storage.py
================
SQLite persistence for the batch optimization engine (Phase 2).

Uses only Python''s built-in sqlite3 -- no new dependency. One row per
candidate/results/checkpoint so a crashed run can be resumed: the runner
checkpoints after every candidate, and `get_resume_point()` reports where to
continue after a process restart.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger("structural_copilot.batch.storage")

RUN_STATUSES = ("running", "completed", "failed", "cancelled")
CANDIDATE_STATUSES = ("pending", "evaluated", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    spec_json   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    objective   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    design_vars_json TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS results (
    candidate_id    INTEGER PRIMARY KEY
                    REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    weight_kg       REAL,
    max_utilization REAL,
    governing_check TEXT,
    buckling_status TEXT,
    pass_fail       TEXT,
    raw_results_json TEXT,
    evaluated_at    TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id                      INTEGER PRIMARY KEY
                                REFERENCES runs(run_id) ON DELETE CASCADE,
    last_completed_candidate_index INTEGER,
    updated_at                  TEXT
);
CREATE TABLE IF NOT EXISTS run_cancellations (
    run_id                      INTEGER PRIMARY KEY
                                REFERENCES runs(run_id) ON DELETE CASCADE,
    requested_at                TEXT NOT NULL,
    reason                      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_results_candidate ON results(candidate_id);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str)


class Storage:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs.db")
        self.db_path = os.path.abspath(db_path)
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Storage:
        self._connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------- runs ----------------

    def create_run(self, spec: dict[str, Any], objective: str = "") -> int:
        conn = self._connect()
        cur = conn.execute(
            "INSERT INTO runs (created_at, spec_json, status, objective) "
            "VALUES (?, ?, 'running', ?)",
            (_now(), _j(spec), str(objective or "")),
        )
        conn.commit()
        return int(cur.lastrowid)

    def mark_run_status(self, run_id: int, status: str) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run status '{status}'; allowed: {RUN_STATUSES}")
        conn = self._connect()
        conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, int(run_id)))
        conn.commit()

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT run_id, created_at, spec_json, status, objective FROM runs WHERE run_id = ?",
            (int(run_id),),
        ).fetchone()
        return dict(row) if row else None

    # ---------------- candidates ----------------

    def add_candidate(self, run_id: int, design_vars: dict[str, Any]) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO candidates (run_id, created_at, "
                "design_vars_json, status) VALUES (?, ?, ?, ?)",
                (int(run_id), _now(), _j(design_vars), "pending"),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"run {run_id} does not exist -- create it with create_run first."
            ) from exc
        conn.commit()
        return int(cur.lastrowid)

    def mark_candidate_status(self, candidate_id: int, status: str) -> None:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status '{status}'; allowed: {CANDIDATE_STATUSES}")
        conn = self._connect()
        conn.execute(
            "UPDATE candidates SET status = ? WHERE candidate_id = ?", (status, int(candidate_id))
        )
        conn.commit()

    def mark_candidate_failed(self, candidate_id: int, reason: str) -> None:
        """Marks a candidate as failed with a human-readable reason.

        Used by the batch runner for per-candidate failures (mechanism
        detected, solver dialog, timeout, unexpected COM error). The reason is
        stored in the results table's raw_results_json so it survives a crash
        and shows up in get_all_results()."""
        conn = self._connect()
        payload = _j({"failure_reason": str(reason)})
        conn.execute(
            "INSERT INTO results (candidate_id, raw_results_json, "
            "evaluated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET "
            "raw_results_json = excluded.raw_results_json, "
            "evaluated_at = excluded.evaluated_at",
            (int(candidate_id), payload, _now()),
        )
        conn.execute(
            "UPDATE candidates SET status = 'failed' WHERE candidate_id = ?", (int(candidate_id),)
        )
        conn.commit()

    def list_candidates(self, run_id: int) -> pd.DataFrame:
        conn = self._connect()
        df = pd.read_sql_query(
            "SELECT candidate_id, run_id, created_at, design_vars_json, "
            "status FROM candidates WHERE run_id = ? ORDER BY candidate_id",
            conn,
            params=(int(run_id),),
        )
        return df

    # ---------------- results ----------------

    def record_result(
        self,
        candidate_id: int,
        weight_kg: float | None,
        max_utilization: float | None,
        governing_check: str | None = None,
        buckling_status: str | None = None,
        pass_fail: str | None = None,
        raw_results_json: str | None = None,
    ) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT INTO results (candidate_id, weight_kg, max_utilization, "
            "governing_check, buckling_status, pass_fail, raw_results_json, "
            "evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET "
            "weight_kg = excluded.weight_kg, "
            "max_utilization = excluded.max_utilization, "
            "governing_check = excluded.governing_check, "
            "buckling_status = excluded.buckling_status, "
            "pass_fail = excluded.pass_fail, "
            "raw_results_json = excluded.raw_results_json, "
            "evaluated_at = excluded.evaluated_at",
            (
                int(candidate_id),
                None if weight_kg is None else float(weight_kg),
                None if max_utilization is None else float(max_utilization),
                governing_check,
                buckling_status,
                pass_fail,
                raw_results_json,
                _now(),
            ),
        )
        conn.execute(
            "UPDATE candidates SET status = 'evaluated' WHERE candidate_id = ?",
            (int(candidate_id),),
        )
        conn.commit()

    def get_all_results(self, run_id: int) -> pd.DataFrame:
        conn = self._connect()
        df = pd.read_sql_query(
            "SELECT c.candidate_id, c.run_id, c.status AS candidate_status, "
            "       c.design_vars_json, "
            "       r.weight_kg, r.max_utilization, r.governing_check, "
            "       r.buckling_status, r.pass_fail, r.raw_results_json, "
            "       r.evaluated_at "
            "FROM candidates c "
            "LEFT JOIN results r ON r.candidate_id = c.candidate_id "
            "WHERE c.run_id = ? ORDER BY c.candidate_id",
            conn,
            params=(int(run_id),),
        )
        return df

    def get_all_results_all_runs(self) -> pd.DataFrame:
        """[SURROGATE PHASE A] Same join as get_all_results but across EVERY
        run in the database (no WHERE clause), ordered by run then candidate.

        Read-only: used to train the surrogate model on all past evaluations
        so history accumulates across batch runs. Rows keep their run_id so
        callers can group/filter by run; candidate design_vars are only
        comparable across runs whose specs encode the same design variables
        (the surrogate applies its own compatibility filter on top).
        """
        conn = self._connect()
        df = pd.read_sql_query(
            "SELECT c.candidate_id, c.run_id, c.status AS candidate_status, "
            "       c.design_vars_json, "
            "       r.weight_kg, r.max_utilization, r.governing_check, "
            "       r.buckling_status, r.pass_fail, r.raw_results_json, "
            "       r.evaluated_at "
            "FROM candidates c "
            "LEFT JOIN results r ON r.candidate_id = c.candidate_id "
            "ORDER BY c.run_id, c.candidate_id",
            conn,
        )
        return df

    # ---------------- checkpoints ----------------

    def update_checkpoint(self, run_id: int, index: int) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT INTO checkpoints (run_id, last_completed_candidate_index, "
            "updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "last_completed_candidate_index = excluded."
            "last_completed_candidate_index, "
            "updated_at = excluded.updated_at",
            (int(run_id), int(index), _now()),
        )
        conn.commit()

    # ---------------- cancellation (Phase 7) ----------------

    def request_cancel(self, run_id: int, reason: str = "") -> None:
        """Sets the cancellation flag for a run. The runner checks this
        BETWEEN candidates (never mid-candidate) and stops cleanly after
        finishing + checkpointing the current one."""
        conn = self._connect()
        conn.execute(
            "INSERT INTO run_cancellations (run_id, requested_at, reason) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET reason = excluded.reason",
            (int(run_id), _now(), str(reason or "")),
        )
        conn.commit()

    def is_cancel_requested(self, run_id: int) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM run_cancellations WHERE run_id = ?", (int(run_id),)
        ).fetchone()
        return row is not None

    def clear_cancel(self, run_id: int) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM run_cancellations WHERE run_id = ?", (int(run_id),))
        conn.commit()

    def get_resume_point(self, run_id: int) -> int | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT last_completed_candidate_index FROM checkpoints WHERE run_id = ?",
            (int(run_id),),
        ).fetchone()
        return None if row is None else int(row[0])
