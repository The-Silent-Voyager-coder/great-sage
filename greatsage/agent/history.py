"""Persistent agent-run history (metadata only).

Every bounded agent run writes one local row after it reaches a terminal
state. The row carries operational metadata -- state, step/tool counts,
provider/model, timings, redacted error/reason, and output length -- but never
the prompt or the model's final text. That matches the rest of the system's
privacy boundary while making recent runs survive a restart.

The store is deliberately failure-isolated from the agent loop: a corrupt,
locked, or unavailable history database disables history, never the run.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from greatsage.agent.models import AgentResult
from greatsage.tools.redaction import redact_secrets

log = logging.getLogger("greatsage.agent.history")

SCHEMA_VERSION = 2
MAX_RUNS = 500
MAX_TEXT = 4000

MIGRATIONS: dict[int, list[str]] = {
    0: [
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            task_id TEXT PRIMARY KEY,
            session_id TEXT,
            state TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'agent',
            steps INTEGER NOT NULL,
            tool_calls INTEGER NOT NULL,
            tool_output_bytes INTEGER NOT NULL,
            duration_ms REAL NOT NULL,
            provider TEXT,
            model TEXT,
            error TEXT,
            reason TEXT,
            memory_references INTEGER NOT NULL,
            content_length INTEGER NOT NULL,
            prompt_length INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_finished "
        "ON agent_runs(finished_at DESC)",
    ],
    1: [
        "ALTER TABLE agent_runs ADD COLUMN failed_tool_calls "
        "INTEGER NOT NULL DEFAULT 0",
    ],
}


class AgentHistoryError(RuntimeError):
    """Agent-run history is unavailable (the agent run itself is unaffected)."""


@dataclass(frozen=True)
class AgentHistoryHealth:
    available: bool
    schema_version: int
    database_path: str
    runs: int
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "schema_version": self.schema_version,
            "database_path": self.database_path,
            "runs": self.runs,
            "detail": self.detail,
        }


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    cleaned = redact_secrets(str(value)) or ""
    if not cleaned:
        return None
    if len(cleaned) > limit:
        return cleaned[:limit] + "... [truncated]"
    return cleaned


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": str(row["task_id"]),
        "session_id": str(row["session_id"]) if row["session_id"] else None,
        "state": str(row["state"]),
        "source": str(row["source"]),
        "steps": int(row["steps"]),
        "tool_calls": int(row["tool_calls"]),
        "failed_tool_calls": int(row["failed_tool_calls"]),
        "tool_output_bytes": int(row["tool_output_bytes"]),
        "duration_ms": float(row["duration_ms"]),
        "provider": str(row["provider"]) if row["provider"] else None,
        "model": str(row["model"]) if row["model"] else None,
        "error": str(row["error"]) if row["error"] else None,
        "reason": str(row["reason"]) if row["reason"] else None,
        "memory_references": int(row["memory_references"]),
        "content_length": int(row["content_length"]),
        "prompt_length": int(row["prompt_length"]),
        "started_at": str(row["started_at"]),
        "finished_at": str(row["finished_at"]),
    }


class AgentRunStore:
    """SQLite-backed metadata history for terminal agent runs."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self._path), timeout=5.0, check_same_thread=False
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                self._conn = conn
                self._migrate(conn)
            except AgentHistoryError:
                self._close_locked()
                raise
            except sqlite3.DatabaseError as exc:
                self._close_locked()
                raise AgentHistoryError(
                    f"agent history database is corrupted (file kept as-is): "
                    f"{self._path} ({exc})"
                ) from exc
            except OSError as exc:
                self._close_locked()
                raise AgentHistoryError(
                    f"agent history database is not accessible: {self._path} ({exc})"
                ) from exc

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.DatabaseError:
                pass
            self._conn = None

    def _migrate(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        current = 0
        if row is not None:
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            current = int(version["value"]) if version else 0
        if current > SCHEMA_VERSION:
            raise AgentHistoryError(
                f"agent history schema {current} is newer than {SCHEMA_VERSION}"
            )
        for version in range(current, SCHEMA_VERSION):
            for statement in MIGRATIONS.get(version, []):
                conn.execute(statement)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES ('schema_version', ?)",
                (str(version + 1),),
            )
            conn.commit()

    def _conn_or_raise(self) -> sqlite3.Connection:
        if self._conn is None:
            raise AgentHistoryError("agent history store is not initialized")
        return self._conn

    def record(
        self,
        result: AgentResult,
        *,
        source: str,
        prompt_length: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """Insert one terminal result, retaining only metadata."""
        row = (
            result.task_id,
            result.session_id,
            result.state.value,
            _text(source, 40) or "agent",
            len(result.steps),
            int(result.tool_calls),
            int(result.failed_tool_calls),
            int(result.tool_output_bytes),
            float(result.duration_ms),
            _text(result.provider, 200),
            _text(result.model, 200),
            _text(result.error),
            _text(result.reason),
            int(result.memory_references),
            len(result.final_text or ""),
            max(0, int(prompt_length)),
            started_at.isoformat(),
            finished_at.isoformat(),
        )
        with self._lock:
            conn = self._conn_or_raise()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO agent_runs
                        (task_id, session_id, state, source, steps, tool_calls,
                         failed_tool_calls, tool_output_bytes, duration_ms, provider,
                         model, error, reason, memory_references, content_length,
                         prompt_length, started_at, finished_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        row,
                    )
                    conn.execute(
                        """
                        DELETE FROM agent_runs
                        WHERE task_id NOT IN (
                            SELECT task_id FROM agent_runs
                            ORDER BY finished_at DESC, rowid DESC LIMIT ?
                        )
                        """,
                        (MAX_RUNS,),
                    )
            except sqlite3.DatabaseError as exc:
                raise AgentHistoryError(f"could not record agent run: {exc}") from exc

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise AgentHistoryError("history limit must be 1..200")
        with self._lock:
            conn = self._conn_or_raise()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_runs
                    ORDER BY finished_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise AgentHistoryError(f"could not read agent history: {exc}") from exc
        return [_row_to_dict(row) for row in rows]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._conn_or_raise()
            try:
                row = conn.execute(
                    "SELECT * FROM agent_runs WHERE task_id = ?", (task_id,)
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise AgentHistoryError(f"could not read agent history: {exc}") from exc
        return _row_to_dict(row) if row is not None else None

    def count(self) -> int:
        with self._lock:
            conn = self._conn_or_raise()
            row = conn.execute("SELECT COUNT(*) AS n FROM agent_runs").fetchone()
        return int(row["n"]) if row is not None else 0

    def health(self) -> AgentHistoryHealth:
        try:
            with self._lock:
                conn = self._conn_or_raise()
                version_row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                version = int(version_row["value"]) if version_row else 0
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='agent_runs'"
                ).fetchone()
                count_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM agent_runs"
                ).fetchone()
                return AgentHistoryHealth(
                    available=table is not None and version == SCHEMA_VERSION,
                    schema_version=version,
                    database_path=str(self._path),
                    runs=int(count_row["n"]) if count_row is not None else 0,
                    detail="",
                )
        except Exception as exc:  # health is a report, never a crash
            return AgentHistoryHealth(
                available=False,
                schema_version=0,
                database_path=str(self._path),
                runs=0,
                detail=str(exc)[:200],
            )
