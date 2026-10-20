"""SQLite memory repository tests: lifecycle, CRUD, filters, FTS, schema."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from greatsage.exceptions import MemoryDatabaseError, MemoryNotFoundError
from greatsage.memory.models import (
    Memory,
    MemoryFilter,
    MemoryType,
    Provenance,
    new_memory_id,
    utcnow,
)
from greatsage.memory.sqlite_repository import SCHEMA_VERSION, SqliteMemoryRepository


def _memory(content: str = "hello world", **overrides) -> Memory:
    fields = {
        "id": new_memory_id(),
        "memory_type": MemoryType.LONG_TERM,
        "content": content,
        "source": "user",
        "provenance": Provenance.USER_EXPLICIT.value,
        "confidence": 0.8,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    fields.update(overrides)
    return Memory(**fields)


@pytest.fixture
def repo(tmp_path: Path) -> SqliteMemoryRepository:
    repository = SqliteMemoryRepository(tmp_path / "memory.db")
    repository.initialize()
    yield repository
    repository.close()


def test_initialize_creates_schema(tmp_path: Path) -> None:
    repository = SqliteMemoryRepository(tmp_path / "fresh.db")
    repository.initialize()
    assert repository.fts_enabled is True
    health = repository.health()
    assert health.accessible is True
    assert health.schema_valid is True
    assert health.migrations_current is True
    assert health.schema_version == SCHEMA_VERSION
    assert health.writable is True
    assert health.fts_enabled is True
    assert (tmp_path / "fresh.db").exists()
    repository.close()


def test_create_and_get_round_trip(repo: SqliteMemoryRepository) -> None:
    memory = _memory(content="the quick brown fox")
    repo.create(memory)
    loaded = repo.get(memory.id)
    assert loaded is not None
    assert loaded.id == memory.id
    assert loaded.content == "the quick brown fox"
    assert loaded.memory_type is MemoryType.LONG_TERM
    assert loaded.confidence == 0.8
    assert loaded.metadata == {}
    assert loaded.session_id is None
    assert loaded.deleted_at is None
    assert loaded.created_at == memory.created_at


def test_structured_content_round_trip(repo: SqliteMemoryRepository) -> None:
    memory = _memory(content={"kind": "preference", "value": "dark mode"}, metadata={"a": 1})
    repo.create(memory)
    loaded = repo.get(memory.id)
    assert loaded.content == {"kind": "preference", "value": "dark mode"}
    assert loaded.metadata == {"a": 1}


def test_get_unknown_returns_none(repo: SqliteMemoryRepository) -> None:
    assert repo.get("mem_absent") is None


def test_get_excludes_expired_by_default(repo: SqliteMemoryRepository) -> None:
    expired = _memory(
        content="old timer",
        expires_at=utcnow() - timedelta(seconds=1),
        memory_type=MemoryType.EPISODIC,
    )
    repo.create(expired)
    assert repo.get(expired.id) is None
    assert repo.get(expired.id, include_expired=True) is not None


def test_get_excludes_deleted_by_default(repo: SqliteMemoryRepository) -> None:
    memory = _memory(content="to be removed")
    repo.create(memory)
    assert repo.delete(memory.id, utcnow()) is True
    assert repo.get(memory.id) is None
    loaded = repo.get(memory.id, include_deleted=True)
    assert loaded is not None
    assert loaded.deleted_at is not None


def test_update_replaces_row(repo: SqliteMemoryRepository) -> None:
    memory = _memory(content="before")
    repo.create(memory)
    updated = _memory(
        id=memory.id,
        content="after",
        confidence=0.95,
        created_at=memory.created_at,
        metadata={"edited": True},
    )
    repo.update(updated)
    loaded = repo.get(memory.id)
    assert loaded.content == "after"
    assert loaded.confidence == 0.95
    assert loaded.metadata == {"edited": True}


def test_update_unknown_raises(repo: SqliteMemoryRepository) -> None:
    with pytest.raises(MemoryNotFoundError, match="mem_absent"):
        repo.update(_memory(id="mem_absent"))


def test_delete_soft_and_idempotent(repo: SqliteMemoryRepository) -> None:
    memory = _memory(content="gone soon")
    repo.create(memory)
    assert repo.delete(memory.id, utcnow()) is True
    assert repo.delete(memory.id, utcnow()) is False  # already deleted
    assert repo.get(memory.id) is None


def test_list_orders_by_created_at_desc(repo: SqliteMemoryRepository) -> None:
    now = utcnow()
    first = _memory(content="first", created_at=now)
    middle = _memory(content="middle", created_at=now + timedelta(seconds=1))
    last = _memory(content="last", created_at=now + timedelta(seconds=2))
    repo.create(middle)
    repo.create(first)
    repo.create(last)
    contents = [m.content for m in repo.list(MemoryFilter())]
    assert contents == ["last", "middle", "first"]


def test_list_excludes_expired_and_deleted(repo: SqliteMemoryRepository) -> None:
    repo.create(_memory(content="live one"))
    repo.create(
        _memory(content="past it", expires_at=utcnow() - timedelta(seconds=1))
    )
    doomed = _memory(content="doomed")
    repo.create(doomed)
    repo.delete(doomed.id, utcnow())
    assert [m.content for m in repo.list(MemoryFilter())] == ["live one"]


def test_list_filter_by_type_source_provenance(repo: SqliteMemoryRepository) -> None:
    repo.create(_memory(content="long", memory_type=MemoryType.LONG_TERM))
    repo.create(
        _memory(
            content="episode",
            memory_type=MemoryType.EPISODIC,
            source="system",
            provenance=Provenance.SYSTEM_EVENT.value,
        )
    )
    assert [m.content for m in repo.list(MemoryFilter(memory_type=MemoryType.EPISODIC))] == [
        "episode"
    ]
    assert [m.content for m in repo.list(MemoryFilter(source="system"))] == ["episode"]
    provenance_filter = MemoryFilter(provenance=Provenance.SYSTEM_EVENT.value)
    assert [m.content for m in repo.list(provenance_filter)] == ["episode"]
    assert [m.content for m in repo.list(MemoryFilter(source="nobody"))] == []


def test_list_minimum_confidence(repo: SqliteMemoryRepository) -> None:
    repo.create(_memory(content="low", confidence=0.3))
    repo.create(_memory(content="high", confidence=0.9))
    assert [m.content for m in repo.list(MemoryFilter(minimum_confidence=0.8))] == ["high"]


def test_session_filter_semantics(repo: SqliteMemoryRepository) -> None:
    repo.create(_memory(content="global"))
    repo.create(_memory(content="sessioned", session_id="s1"))
    assert [m.content for m in repo.list(MemoryFilter(session_id="s1"))] == [
        "sessioned",
        "global",
    ]
    assert [m.content for m in repo.list(MemoryFilter(session_id="s2"))] == ["global"]


def test_search_fts_phrase_match(repo: SqliteMemoryRepository) -> None:
    repo.create(_memory(content="the quick brown fox jumps"))
    repo.create(_memory(content="a lazy dog sleeps"))
    hits = repo.search("brown fox", MemoryFilter())
    assert [m.content for m in hits] == ["the quick brown fox jumps"]
    assert repo.search("giraffe", MemoryFilter()) == []


def test_search_case_insensitive(repo: SqliteMemoryRepository) -> None:
    repo.create(_memory(content="Great Sage LOVES COFFEE"))
    assert len(repo.search("coffee", MemoryFilter())) == 1


def test_search_matches_source(repo: SqliteMemoryRepository) -> None:
    repo.create(_memory(content="whatever", source="archive-42"))
    assert len(repo.search("archive-42", MemoryFilter())) == 1


def test_search_excludes_deleted(repo: SqliteMemoryRepository) -> None:
    memory = _memory(content="vanishing text")
    repo.create(memory)
    repo.delete(memory.id, utcnow())
    assert repo.search("vanishing", MemoryFilter()) == []


def test_search_excludes_expired_by_default(repo: SqliteMemoryRepository) -> None:
    repo.create(
        _memory(
            content="expired needle",
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )
    assert repo.search("needle", MemoryFilter()) == []
    hits = repo.search("needle", MemoryFilter(include_expired=True))
    assert len(hits) == 1


def test_expire_sweeps_only_expired(repo: SqliteMemoryRepository) -> None:
    expired = _memory(content="done deal", expires_at=utcnow() - timedelta(seconds=1))
    live = _memory(content="still fresh", expires_at=utcnow() + timedelta(days=1))
    repo.create(expired)
    repo.create(live)
    affected = repo.expire(utcnow())
    assert set(affected) == {expired.id}
    assert repo.get(expired.id, include_expired=True, include_deleted=True).deleted_at is not None
    assert repo.get(live.id) is not None


def test_count_matches_list(repo: SqliteMemoryRepository) -> None:
    for i in range(5):
        repo.create(_memory(content=f"item {i}", memory_type=MemoryType.SEMANTIC))
    filters = MemoryFilter(memory_type=MemoryType.SEMANTIC)
    assert repo.count(filters) == 5
    assert len(repo.list(filters)) == 5
    assert repo.count(MemoryFilter(memory_type=MemoryType.EPISODIC)) == 0


def test_stats(repo: SqliteMemoryRepository) -> None:
    repo.create(_memory(content="one", memory_type=MemoryType.LONG_TERM))
    repo.create(_memory(content="two", memory_type=MemoryType.EPISODIC))
    repo.create(
        _memory(
            content="three",
            memory_type=MemoryType.EPISODIC,
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )
    stats = repo.stats()
    assert stats["total"] == 3
    assert stats["by_type"]["long_term"] == 1
    assert stats["by_type"]["episodic"] == 2
    assert stats["expired"] == 1
    assert stats["deleted"] == 0
    assert stats["fts_enabled"] is True
    assert stats["schema_version"] == SCHEMA_VERSION
    assert stats["database_path"].endswith("memory.db")


def test_transaction_rolls_back_on_failure(repo: SqliteMemoryRepository) -> None:
    memory = _memory(content="duplicate me")
    repo.create(memory)
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(memory)  # duplicate primary key
    assert repo.count(MemoryFilter()) == 1
    repo.create(_memory(content="still works after failure"))
    assert repo.count(MemoryFilter()) == 2


def test_corrupted_database_raises_and_keeps_file(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a sqlite database at all........")
    repository = SqliteMemoryRepository(path)
    with pytest.raises(MemoryDatabaseError, match="kept as-is"):
        repository.initialize()
    assert path.exists()  # never deleted to repair
    quarantined = list((tmp_path / "corrupt.quarantine").glob("corrupt-*.db.corrupt"))
    assert len(quarantined) == 1  # timestamped copy kept for recovery
    assert quarantined[0].read_bytes() == path.read_bytes()


def test_newer_schema_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '99')")
    conn.commit()
    conn.close()
    repository = SqliteMemoryRepository(path)
    with pytest.raises(MemoryDatabaseError, match="refusing to touch"):
        repository.initialize()


def test_version_zero_database_migrates(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '0')")
    conn.commit()
    conn.close()
    repository = SqliteMemoryRepository(path)
    repository.initialize()
    health = repository.health()
    assert health.schema_version == SCHEMA_VERSION
    assert health.migrations_current is True
    assert health.schema_valid is True
    repository.create(_memory(content="after migration"))
    assert repo_has(repository, "after migration")
    repository.close()


def repo_has(repository: SqliteMemoryRepository, content: str) -> bool:
    return any(m.content == content for m in repository.list(MemoryFilter()))


def test_unwritable_path_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file", encoding="utf-8")
    repository = SqliteMemoryRepository(blocker / "memory.db")
    with pytest.raises(MemoryDatabaseError, match="not accessible"):
        repository.initialize()


def test_like_fallback_when_fts_disabled(repo: SqliteMemoryRepository, tmp_path: Path) -> None:
    fallback = SqliteMemoryRepository(tmp_path / "fallback.db", enable_fts=False)
    fallback.initialize()
    assert fallback.fts_enabled is False
    fallback.create(_memory(content="progress: 100% ready"))
    fallback.create(_memory(content="alpha beta"))
    assert len(fallback.search("100%", MemoryFilter())) == 1
    assert len(fallback.search("%", MemoryFilter())) == 1
    assert len(fallback.search("alpha", MemoryFilter())) == 1
    assert fallback.search("omega", MemoryFilter()) == []
    fallback.close()


def test_operations_after_close_raise(repo: SqliteMemoryRepository) -> None:
    repo.close()
    with pytest.raises(MemoryDatabaseError, match="not initialized"):
        repo.list(MemoryFilter())
    repo.close()  # idempotent
