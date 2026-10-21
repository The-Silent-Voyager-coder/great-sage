# ruff: noqa: E501
"""SQLite task repository (Phase 6)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from greatsage.exceptions import TaskValidationError
from greatsage.task.models import StepResult, TaskRecord, TaskState
from greatsage.task.repository import TaskHealth, TaskRepository

log = logging.getLogger("greatsage.task.sqlite")

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, list[str]] = {
    0: [
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            goal TEXT NOT NULL,
            state TEXT NOT NULL,
            current_step INTEGER NOT NULL,
            total_steps INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS step_results (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            tool_id TEXT NOT NULL,
            success INTEGER NOT NULL,
            output TEXT,
            error TEXT,
            duration_ms REAL NOT NULL,
            artifacts TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)",
        "CREATE INDEX IF NOT EXISTS idx_step_results_task ON step_results(task_id)",
    ],
}


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=str(row["id"]),
        plan_id=str(row["plan_id"]),
        goal=str(row["goal"]),
        state=TaskState(str(row["state"])),
        current_step=int(row["current_step"]),
        total_steps=int(row["total_steps"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        completed_at=datetime.fromisoformat(str(row["completed_at"])) if row["completed_at"] else None,
        error=row["error"],
        metadata=json.loads(str(row["metadata"] or "{}")),
    )


def _row_to_result(row: sqlite3.Row) -> StepResult:
    return StepResult(
        step_id=str(row["step_id"]),
        sequence=int(row["sequence"]),
        tool_id=str(row["tool_id"]),
        success=bool(row["success"]),
        output=json.loads(str(row["output"])) if row["output"] else None,
        error=row["error"],
        duration_ms=float(row["duration_ms"]),
        artifacts=json.loads(str(row["artifacts"] or "[]")),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


class SqliteTaskRepository(TaskRepository):
    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # One connection guarded by an RLock: the control plane runs
                # task work in a worker thread, and HTTP reads arrive on
                # handler threads. Serialized access keeps this safe.
                conn = sqlite3.connect(
                    str(self._path), timeout=5.0, check_same_thread=False
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA foreign_keys=ON")
                self._conn = conn
                self._migrate(conn)
            except sqlite3.DatabaseError as exc:
                self._close()
                raise TaskValidationError(f"task db corrupted: {self._path} ({exc})") from exc
            except OSError as exc:
                self._close()
                raise TaskValidationError(f"task db not accessible: {self._path} ({exc})") from exc

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.DatabaseError:
                pass
            self._conn = None

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
        current = 0
        if cur is not None:
            row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            current = int(row["value"]) if row else 0
        if current > SCHEMA_VERSION:
            raise TaskValidationError(f"task db schema {current} > {SCHEMA_VERSION}")
        for version in range(current, SCHEMA_VERSION):
            for stmt in MIGRATIONS.get(version, []):
                conn.execute(stmt)
            conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)", (str(version + 1),))
            conn.commit()

    def _conn_or_raise(self) -> sqlite3.Connection:
        if self._conn is None:
            raise TaskValidationError("task repository not initialized")
        return self._conn

    def create_task(self, record: TaskRecord) -> None:
        record.validate()
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                conn.execute(
                    """
                    INSERT INTO tasks
                    (id, plan_id, goal, state, current_step, total_steps, created_at, updated_at, completed_at, error, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.plan_id,
                        record.goal,
                        record.state.value,
                        record.current_step,
                        record.total_steps,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                        record.completed_at.isoformat() if record.completed_at else None,
                        record.error,
                        json.dumps(record.metadata),
                    ),
                )

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn_or_raise().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def update_task(self, record: TaskRecord) -> None:
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                cur = conn.execute(
                    """
                    UPDATE tasks SET
                        state = ?, current_step = ?, updated_at = ?, completed_at = ?, error = ?, metadata = ?
                    WHERE id = ?
                    """,
                    (
                        record.state.value,
                        record.current_step,
                        record.updated_at.isoformat(),
                        record.completed_at.isoformat() if record.completed_at else None,
                        record.error,
                        json.dumps(record.metadata),
                        record.id,
                    ),
                )
                if cur.rowcount == 0:
                    raise TaskValidationError(f"task not found: {record.id}")

    def list_tasks(self) -> list[TaskRecord]:
        with self._lock:
            rows = self._conn_or_raise().execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [_row_to_task(r) for r in rows]

    def save_step_result(self, task_id: str, result: StepResult) -> None:
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO step_results
                    (id, task_id, step_id, sequence, tool_id, success, output, error, duration_ms, artifacts, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{task_id}_{result.step_id}",
                        task_id,
                        result.step_id,
                        result.sequence,
                        result.tool_id,
                        1 if result.success else 0,
                        json.dumps(result.output) if result.output is not None else None,
                        result.error,
                        result.duration_ms,
                        json.dumps(result.artifacts),
                        result.created_at.isoformat(),
                    ),
                )

    def get_step_results(self, task_id: str) -> list[StepResult]:
        with self._lock:
            rows = self._conn_or_raise().execute("SELECT * FROM step_results WHERE task_id = ? ORDER BY sequence ASC", (task_id,)).fetchall()
        return [_row_to_result(r) for r in rows]

    def health(self) -> TaskHealth:
        try:
            with self._lock:
                conn = self._conn_or_raise()
                cur = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                version = int(cur["value"]) if cur else 0
                valid = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone() is not None
                current = version == SCHEMA_VERSION
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ROLLBACK")
                    writable = True
                except sqlite3.DatabaseError:
                    writable = False
                return TaskHealth(True, valid, current, writable, version, "", str(self._path))
        except Exception as exc:  # pragma: no cover
            return TaskHealth(False, False, False, False, 0, str(exc)[:200], str(self._path))
