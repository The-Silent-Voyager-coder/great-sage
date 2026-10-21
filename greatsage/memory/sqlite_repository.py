"""SQLite memory repository (Phase 3 persistence).

Concurrency model (documented, docs/MEMORY.md): one SQLite connection per
repository guarded by a re-entrant lock; WAL journal + busy_timeout for
multi-process readers; all writes are transactions. This is deliberately
small — a local single-user workload does not need a distributed store.

Schema versioning: `schema_meta(schema_version)` + a controlled list of
standard-library SQL migrations. A corrupted or newer-schema database raises
MemoryDatabaseError and is never deleted to repair.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from greatsage.exceptions import MemoryDatabaseError, MemoryNotFoundError
from greatsage.memory.models import Memory, MemoryFilter, MemoryType, utcnow
from greatsage.memory.repository import MemoryRepository, RepositoryHealth
from greatsage.storage.recovery import quarantine_corrupt_file

# Alias so the `list`/`search` method names do not shadow the builtin `list`
# inside their own class-scope annotations (mypy valid-type).
MemoryList = list[Memory]
MemoryIdList = list[str]
QueryParts = tuple[str, list[Any]]

log = logging.getLogger("greatsage.memory.repository")

SCHEMA_VERSION = 2

MIGRATIONS: dict[int, list[str]] = {
    # Bootstrap: schema version 0 -> 1 (fresh database).
    0: [
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE memories (
            id          TEXT PRIMARY KEY,
            memory_type TEXT NOT NULL,
            content     TEXT NOT NULL,
            source      TEXT NOT NULL,
            provenance  TEXT NOT NULL,
            confidence  REAL NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            expires_at  TEXT,
            metadata    TEXT NOT NULL DEFAULT '{}',
            session_id  TEXT,
            deleted_at  TEXT
        )
        """,
        "CREATE INDEX idx_memories_type ON memories(memory_type)",
        "CREATE INDEX idx_memories_created ON memories(created_at)",
        "CREATE INDEX idx_memories_expires ON memories(expires_at)",
        "CREATE INDEX idx_memories_source ON memories(source)",
        "CREATE INDEX idx_memories_provenance ON memories(provenance)",
        "CREATE INDEX idx_memories_session ON memories(session_id) WHERE session_id IS NOT NULL",
    ],
    # Roadmap Phase B: embedding vectors live beside their memories.
    1: [
        """
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
            model     TEXT NOT NULL,
            dim       INTEGER NOT NULL,
            vector    TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_embeddings_model ON memory_embeddings(model)",
    ],
}

FTS_OBJECTS: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content, source, provenance,
        content='memories', content_rowid='rowid'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, source, provenance)
        VALUES (new.rowid, new.content, new.source, new.provenance);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, source, provenance)
        VALUES ('delete', old.rowid, old.content, old.source, old.provenance);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, source, provenance)
        VALUES ('delete', old.rowid, old.content, old.source, old.provenance);
        INSERT INTO memories_fts(rowid, content, source, provenance)
        VALUES (new.rowid, new.content, new.source, new.provenance);
    END
    """,
)


def _encode_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _decode_json(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _encode_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decode_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=str(row["id"]),
        memory_type=MemoryType(str(row["memory_type"])),
        content=_decode_json(str(row["content"]), ""),
        source=str(row["source"]),
        provenance=str(row["provenance"]),
        confidence=float(row["confidence"]),
        created_at=_decode_dt(row["created_at"]) or datetime.now(),  # pragma: no cover
        updated_at=_decode_dt(row["updated_at"]) or datetime.now(),  # pragma: no cover
        expires_at=_decode_dt(row["expires_at"]),
        metadata=_decode_json(row["metadata"], {}),
        session_id=row["session_id"],
        deleted_at=_decode_dt(row["deleted_at"]),
    )


class SqliteMemoryRepository(MemoryRepository):
    """MemoryRepository backed by a local SQLite file."""

    def __init__(self, database_path: str | Path, *, enable_fts: bool = True) -> None:
        self._path = Path(database_path)
        self._enable_fts = enable_fts
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._fts_active = False

    # --- lifecycle -----------------------------------------------------

    def initialize(self) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # One connection guarded by an RLock: agent/memory reads may
                # arrive from the control plane's worker threads.
                conn = sqlite3.connect(
                    str(self._path), timeout=5.0, check_same_thread=False
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA foreign_keys=ON")
                self._conn = conn
                self._validate_or_repair_schema(conn)
                self._setup_fts(conn)
            except MemoryDatabaseError:
                self._close_conn()
                raise
            except sqlite3.DatabaseError as exc:
                self._close_conn()
                saved = quarantine_corrupt_file(self._path, reason=f"open failed: {exc}")
                hint = f" (quarantined copy: {saved})" if saved else ""
                raise MemoryDatabaseError(
                    f"memory database is corrupted or unreadable (file kept as-is): "
                    f"{self._path} ({exc}){hint}"
                ) from exc
            except OSError as exc:
                self._close_conn()
                raise MemoryDatabaseError(
                    f"memory database is not accessible: {self._path} ({exc})"
                ) from exc

    def close(self) -> None:
        with self._lock:
            self._close_conn()

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.DatabaseError:
                pass
            self._conn = None

    # --- schema --------------------------------------------------------

    def _validate_or_repair_schema(self, conn: sqlite3.Connection) -> None:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            saved = quarantine_corrupt_file(self._path, reason="integrity_check failed")
            hint = f" (quarantined copy: {saved})" if saved else ""
            raise MemoryDatabaseError(
                f"memory database failed integrity check (file kept as-is): {self._path}{hint}"
            )
        current = self._read_schema_version(conn)
        if current > SCHEMA_VERSION:
            raise MemoryDatabaseError(
                f"memory database schema version {current} is newer than supported "
                f"{SCHEMA_VERSION}; refusing to touch it: {self._path}"
            )
        if current < SCHEMA_VERSION:
            with conn:  # single transaction per migration step
                for version in range(current, SCHEMA_VERSION):
                    for statement in MIGRATIONS.get(version, []):
                        conn.execute(statement)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES ('schema_version', ?)",
                        (str(version + 1),),
                    )
            log.info(
                "memory schema migrated %s -> %s",
                current,
                SCHEMA_VERSION,
                extra={"component": "memory"},
            )
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        if table is None:
            raise MemoryDatabaseError(
                f"memory database schema is invalid (memories table missing), "
                f"file kept as-is: {self._path}"
            )

    def _read_schema_version(self, conn: sqlite3.Connection) -> int:
        meta = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if meta is None:
            return 0
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _setup_fts(self, conn: sqlite3.Connection) -> None:
        if not self._enable_fts:
            self._fts_active = False
            return
        try:
            for statement in FTS_OBJECTS:
                conn.execute(statement)
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('optimize')")
            conn.commit()
            self._fts_active = True
        except sqlite3.OperationalError as exc:
            log.warning(
                "FTS5 unavailable (%s); falling back to LIKE search",
                exc,
                extra={"component": "memory"},
            )
            self._fts_active = False

    # --- connection helper ----------------------------------------------

    @property
    def fts_enabled(self) -> bool:
        return self._fts_active

    def _conn_or_raise(self) -> sqlite3.Connection:
        if self._conn is None:
            raise MemoryDatabaseError("memory database is not initialized")
        return self._conn

    # --- CRUD ----------------------------------------------------------

    def create(self, memory: Memory) -> None:
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                conn.execute(
                    """
                    INSERT INTO memories (
                        id, memory_type, content, source, provenance, confidence,
                        created_at, updated_at, expires_at, metadata, session_id, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory.id,
                        memory.memory_type.value,
                        _encode_json(memory.content),
                        memory.source,
                        memory.provenance,
                        float(memory.confidence),
                        _encode_dt(memory.created_at),
                        _encode_dt(memory.updated_at),
                        _encode_dt(memory.expires_at),
                        _encode_json(dict(memory.metadata)),
                        memory.session_id,
                        _encode_dt(memory.deleted_at),
                    ),
                )

    def get(
        self,
        memory_id: str,
        *,
        include_expired: bool = False,
        include_deleted: bool = False,
    ) -> Memory | None:
        with self._lock:
            conn = self._conn_or_raise()
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
        memory = _row_to_memory(row)
        if not include_deleted and memory.deleted_at is not None:
            return None
        if not include_expired and memory.expires_at is not None and memory.expires_at <= utcnow():
            return None
        return memory

    def update(self, memory: Memory) -> None:
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE memories SET
                        memory_type = ?, content = ?, source = ?, provenance = ?,
                        confidence = ?, updated_at = ?, expires_at = ?, metadata = ?,
                        session_id = ?, deleted_at = ?
                    WHERE id = ?
                    """,
                    (
                        memory.memory_type.value,
                        _encode_json(memory.content),
                        memory.source,
                        memory.provenance,
                        float(memory.confidence),
                        _encode_dt(memory.updated_at),
                        _encode_dt(memory.expires_at),
                        _encode_json(dict(memory.metadata)),
                        memory.session_id,
                        _encode_dt(memory.deleted_at),
                        memory.id,
                    ),
                )
            if cursor.rowcount == 0:
                raise MemoryNotFoundError(f"memory not found: {memory.id}")

    def delete(self, memory_id: str, deleted_at: datetime) -> bool:
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                cursor = conn.execute(
                    "UPDATE memories SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (_encode_dt(deleted_at), memory_id),
                )
            return cursor.rowcount > 0

    # --- embeddings ------------------------------------------------------

    def save_embedding(self, memory_id: str, model: str, vector: list[float]) -> None:
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memory_embeddings
                    (memory_id, model, dim, vector, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id, model, len(vector),
                        _encode_json([float(v) for v in vector]),
                        _encode_dt(utcnow()),
                    ),
                )

    def get_embedding(self, memory_id: str) -> tuple[str, list[float]] | None:
        with self._lock:
            row = (
                self._conn_or_raise()
                .execute(
                    "SELECT model, vector FROM memory_embeddings WHERE memory_id = ?",
                    (memory_id,),
                )
                .fetchone()
            )
        if row is None:
            return None
        raw = _decode_json(str(row["vector"]), [])
        if not isinstance(raw, list):
            return None
        return str(row["model"]), [float(v) for v in raw]

    def delete_embedding(self, memory_id: str) -> None:
        with self._lock:
            conn = self._conn_or_raise()
            with conn:
                conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))

    def list_embeddings(self, model: str) -> dict[str, list[float]]:
        with self._lock:
            rows = (
                self._conn_or_raise()
                .execute(
                    "SELECT e.memory_id AS id, e.vector AS vector FROM memory_embeddings e "
                    "JOIN memories m ON m.id = e.memory_id "
                    "WHERE e.model = ? AND m.deleted_at IS NULL",
                    (model,),
                )
                .fetchall()
            )
        out: dict[str, list[float]] = {}
        for row in rows:
            raw = _decode_json(str(row["vector"]), [])
            if isinstance(raw, list) and raw:
                out[str(row["id"])] = [float(v) for v in raw]
        return out

    def missing_embeddings(self, model: str, limit: int = 500) -> list[str]:
        with self._lock:
            rows = (
                self._conn_or_raise()
                .execute(
                    "SELECT m.id AS id FROM memories m "
                    "LEFT JOIN memory_embeddings e ON e.memory_id = m.id AND e.model = ? "
                    "WHERE m.deleted_at IS NULL AND e.memory_id IS NULL "
                    "ORDER BY m.created_at DESC LIMIT ?",
                    (model, max(0, limit)),
                )
                .fetchall()
            )
        return [str(row["id"]) for row in rows]

    # --- retrieval ------------------------------------------------------

    def list(self, filters: MemoryFilter) -> MemoryList:
        with self._lock:
            rows = self._conn_or_raise().execute(
                *self._select_statement(filters)
            ).fetchall()
        return [_row_to_memory(row) for row in rows]

    def search(self, query: str, filters: MemoryFilter) -> MemoryList:
        with self._lock:
            conn = self._conn_or_raise()
            if self._fts_active:
                phrase = '"' + query.replace('"', '""') + '"'
                where, params = self._where_clause(filters)
                sql = (
                    "SELECT memories.* FROM memories "
                    "JOIN memories_fts ON memories_fts.rowid = memories.rowid "
                    f"WHERE memories_fts MATCH ? AND {where} "
                    "ORDER BY memories.created_at DESC, memories.id ASC"
                )
                rows = conn.execute(sql, [phrase, *params]).fetchall()
            else:
                pattern = f"%{_escape_like(query)}%"
                where, params = self._where_clause(filters)
                sql = (
                    "SELECT * FROM memories "
                    f"WHERE (content LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\' "
                    f"OR provenance LIKE ? ESCAPE '\\') AND {where} "
                    "ORDER BY created_at DESC, id ASC"
                )
                rows = conn.execute(sql, [pattern, pattern, pattern, *params]).fetchall()
        return [_row_to_memory(row) for row in rows]

    def expire(self, now: datetime) -> MemoryIdList:
        with self._lock:
            conn = self._conn_or_raise()
            rows = conn.execute(
                "SELECT id FROM memories WHERE deleted_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (_encode_dt(now),),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                with conn:
                    conn.execute(
                        "UPDATE memories SET deleted_at = ? WHERE deleted_at IS NULL "
                        "AND expires_at IS NOT NULL AND expires_at <= ?",
                        (_encode_dt(now), _encode_dt(now)),
                    )
        return ids

    def count(self, filters: MemoryFilter) -> int:
        with self._lock:
            conn = self._conn_or_raise()
            where, params = self._where_clause(filters)
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM memories WHERE {where}", params
            ).fetchone()
            return int(row["n"])

    def stats(self) -> dict[str, Any]:
        with self._lock:
            conn = self._conn_or_raise()
            rows = conn.execute(
                "SELECT memory_type, COUNT(*) AS n FROM memories WHERE deleted_at IS NULL "
                "GROUP BY memory_type"
            ).fetchall()
            by_type = {str(r["memory_type"]): int(r["n"]) for r in rows}
            expired = conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE deleted_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (_encode_dt(utcnow()),),
            ).fetchone()["n"]
            deleted = conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE deleted_at IS NOT NULL"
            ).fetchone()["n"]
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE deleted_at IS NULL"
            ).fetchone()["n"]
        return {
            "total": int(total),
            "by_type": by_type,
            "expired": int(expired),
            "deleted": int(deleted),
            "fts_enabled": self._fts_active,
            "schema_version": SCHEMA_VERSION,
            "database_path": str(self._path),
        }

    # --- SQL construction ------------------------------------------------

    def _select_statement(self, filters: MemoryFilter) -> QueryParts:
        where, params = self._where_clause(filters)
        return (
            f"SELECT * FROM memories WHERE {where} "
            "ORDER BY created_at DESC, id ASC",
            params,
        )

    def _where_clause(self, filters: MemoryFilter) -> QueryParts:
        clauses: list[str] = []
        params: list[Any] = []
        if filters.memory_type is not None:
            clauses.append("memory_type = ?")
            params.append(filters.memory_type.value)
        if filters.source is not None:
            clauses.append("source = ?")
            params.append(filters.source)
        if filters.provenance is not None:
            clauses.append("provenance = ?")
            params.append(filters.provenance)
        if filters.created_after is not None:
            clauses.append("created_at > ?")
            params.append(_encode_dt(filters.created_after))
        if filters.created_before is not None:
            clauses.append("created_at < ?")
            params.append(_encode_dt(filters.created_before))
        if filters.expires_before is not None:
            clauses.append("expires_at IS NOT NULL AND expires_at < ?")
            params.append(_encode_dt(filters.expires_before))
        if filters.minimum_confidence is not None:
            clauses.append("confidence >= ?")
            params.append(float(filters.minimum_confidence))
        if not filters.include_deleted:
            clauses.append("deleted_at IS NULL")
        if not filters.include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(_encode_dt(utcnow()))
        if filters.session_id is not None:
            clauses.append("(session_id IS NULL OR session_id = ?)")
            params.append(filters.session_id)
        if not clauses:
            return "1 = 1", params
        return " AND ".join(clauses), params

    # --- health ----------------------------------------------------------

    def health(self) -> RepositoryHealth:
        accessible = False
        schema_valid = False
        migrations_current = False
        writable = False
        detail = ""
        schema_version = 0
        try:
            with self._lock:
                conn = self._conn_or_raise()
                accessible = True
                version = self._read_schema_version(conn)
                schema_version = version
                migrations_current = version == SCHEMA_VERSION
                schema_valid = (
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
                    ).fetchone()
                    is not None
                )
                writable = self._probe_writable(conn)
        except Exception as exc:  # pragma: no cover - defensive
            detail = str(exc)[:200]
        return RepositoryHealth(
            accessible=accessible,
            schema_valid=schema_valid,
            migrations_current=migrations_current,
            writable=writable,
            fts_enabled=self._fts_active,
            schema_version=schema_version,
            detail=detail,
            database_path=str(self._path),
        )

    def _probe_writable(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True
        except sqlite3.DatabaseError:
            return False


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
