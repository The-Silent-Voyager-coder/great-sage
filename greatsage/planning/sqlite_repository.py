# ruff: noqa: E501
"""SQLite planning repository (Phase 6)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from greatsage.exceptions import PlanningValidationError
from greatsage.planning.models import Plan
from greatsage.planning.repository import PlanningHealth, PlanningRepository

log = logging.getLogger("greatsage.planning.sqlite")

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
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            workspace_id TEXT,
            workspace_root TEXT,
            steps TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_plans_workspace ON plans(workspace_id)",
    ],
}


def _row_to_plan(row: sqlite3.Row) -> Plan:
    data = {
        "id": str(row["id"]),
        "goal": str(row["goal"]),
        "workspace_id": row["workspace_id"],
        "workspace_root": row["workspace_root"],
        "steps": json.loads(str(row["steps"])),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "metadata": json.loads(str(row["metadata"] or "{}")),
    }
    return Plan.from_dict(data)


class SqlitePlanningRepository(PlanningRepository):
    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # One connection guarded by an RLock: plan reads can arrive
                # from control-plane handler threads.
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
                raise PlanningValidationError(f"planning db corrupted: {self._path} ({exc})") from exc
            except OSError as exc:
                self._close()
                raise PlanningValidationError(f"planning db not accessible: {self._path} ({exc})") from exc

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
            raise PlanningValidationError(f"planning db schema {current} > {SCHEMA_VERSION}")
        for version in range(current, SCHEMA_VERSION):
            for stmt in MIGRATIONS.get(version, []):
                conn.execute(stmt)
            conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)", (str(version + 1),))
            conn.commit()

    def _conn_or_raise(self) -> sqlite3.Connection:
        if self._conn is None:
            raise PlanningValidationError("planning repository not initialized")
        return self._conn

    def save(self, plan: Plan) -> None:
        plan.validate()
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO plans
                    (id, goal, workspace_id, workspace_root, steps, status, created_at, updated_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan.id,
                        plan.goal,
                        plan.workspace_id,
                        str(plan.workspace_root) if plan.workspace_root else None,
                        json.dumps([s.to_dict() for s in plan.steps]),
                        plan.status.value,
                        plan.created_at.isoformat(),
                        plan.updated_at.isoformat(),
                        json.dumps(plan.metadata),
                    ),
                )

    def get(self, plan_id: str) -> Plan | None:
        with self._lock:
            row = self._conn_or_raise().execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return _row_to_plan(row) if row else None

    def list(self) -> list[Plan]:
        with self._lock:
            rows = self._conn_or_raise().execute("SELECT * FROM plans ORDER BY created_at DESC").fetchall()
        return [_row_to_plan(r) for r in rows]

    def health(self) -> PlanningHealth:
        try:
            with self._lock:
                conn = self._conn_or_raise()
                cur = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                version = int(cur["value"]) if cur else 0
                valid = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='plans'").fetchone() is not None
                current = version == SCHEMA_VERSION
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ROLLBACK")
                    writable = True
                except sqlite3.DatabaseError:
                    writable = False
                return PlanningHealth(True, valid, current, writable, version, "", str(self._path))
        except Exception as exc:  # pragma: no cover
            return PlanningHealth(False, False, False, False, 0, str(exc)[:200], str(self._path))
