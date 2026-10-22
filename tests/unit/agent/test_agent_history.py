"""Persistent agent-run history: metadata, ordering, retention, isolation."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from greatsage.agent.history import (
    MAX_RUNS,
    AgentHistoryError,
    AgentRunStore,
)
from greatsage.agent.models import AgentResult, AgentState

_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"


def _result(
    task_id: str = "task_1",
    *,
    state: AgentState = AgentState.COMPLETED,
    final_text: str = "done",
    error: str | None = None,
) -> AgentResult:
    return AgentResult(
        task_id=task_id,
        session_id="session_1",
        state=state,
        tool_calls=2,
        failed_tool_calls=1,
        tool_output_bytes=42,
        duration_ms=12.5,
        final_text=final_text,
        provider="local",
        model="llama3.2:1b",
        error=error,
        reason="finished",
        memory_references=1,
    )


def _times(offset: int = 0) -> tuple[datetime, datetime]:
    start = datetime(2026, 10, 22, tzinfo=UTC) + timedelta(minutes=offset)
    return start, start + timedelta(seconds=1)


def _store(tmp_path: Path) -> AgentRunStore:
    store = AgentRunStore(tmp_path / "agent-runs.db")
    store.initialize()
    return store


def test_record_list_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started, finished = _times()
    store.record(
        _result(),
        source="control",
        prompt_length=17,
        started_at=started,
        finished_at=finished,
    )

    listed = store.list()
    loaded = store.get("task_1")

    assert listed == [loaded]
    assert loaded is not None
    assert loaded["task_id"] == "task_1"
    assert loaded["source"] == "control"
    assert loaded["state"] == "completed"
    assert loaded["steps"] == 0
    assert loaded["tool_calls"] == 2
    assert loaded["failed_tool_calls"] == 1
    assert loaded["content_length"] == len("done")
    assert loaded["prompt_length"] == 17
    assert loaded["provider"] == "local"
    assert loaded["model"] == "llama3.2:1b"
    store.close()


def test_prompt_and_model_text_are_not_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started, finished = _times()
    final_text = f"the token is {_SECRET}"
    store.record(
        _result(final_text=final_text),
        source="cli",
        prompt_length=99,
        started_at=started,
        finished_at=finished,
    )

    encoded = json.dumps(store.list())
    assert _SECRET not in encoded
    assert '"final_text":' not in encoded
    assert '"prompt":' not in encoded
    assert f'"content_length": {len(final_text)}' in encoded
    store.close()


def test_errors_and_reasons_are_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started, finished = _times()
    store.record(
        _result(state=AgentState.FAILED, final_text=None, error=f"failed: {_SECRET}"),
        source="agent",
        prompt_length=1,
        started_at=started,
        finished_at=finished,
    )

    entry = store.list()[0]
    assert _SECRET not in json.dumps(entry)
    assert "[REDACTED:api_key]" in (entry["error"] or "")
    store.close()


def test_newest_first_and_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("greatsage.agent.history.MAX_RUNS", 2)
    store = _store(tmp_path)
    for index in range(3):
        started, finished = _times(index)
        store.record(
            _result(f"task_{index}"),
            source="agent",
            prompt_length=1,
            started_at=started,
            finished_at=finished,
        )

    entries = store.list()
    assert [entry["task_id"] for entry in entries] == ["task_2", "task_1"]
    assert store.count() == 2
    store.close()


def test_limit_validation_and_uninitialized_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AgentHistoryError, match="limit"):
        store.list(0)
    with pytest.raises(AgentHistoryError, match="limit"):
        store.list(201)
    store.close()
    with pytest.raises(AgentHistoryError, match="not initialized"):
        store.list()


def test_corrupt_database_is_kept_and_reported(tmp_path: Path) -> None:
    path = tmp_path / "agent-runs.db"
    path.write_text("not a sqlite database", encoding="utf-8")

    store = AgentRunStore(path)
    with pytest.raises(AgentHistoryError, match="corrupted"):
        store.initialize()

    assert path.read_text(encoding="utf-8") == "not a sqlite database"


def test_v1_database_migrates_failed_tool_calls_column(tmp_path: Path) -> None:
    path = tmp_path / "agent-runs.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');
        CREATE TABLE agent_runs (
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
        );
        INSERT INTO agent_runs
        (task_id, session_id, state, source, steps, tool_calls,
         tool_output_bytes, duration_ms, provider, model, error, reason,
         memory_references, content_length, prompt_length, started_at, finished_at)
        VALUES ('old_task', NULL, 'completed', 'cli', 1, 1, 0, 1.0,
                'local', 'm', NULL, NULL, 0, 0, 1,
                '2026-10-22T00:00:00+00:00', '2026-10-22T00:00:01+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = AgentRunStore(path)
    store.initialize()

    entry = store.list()[0]
    assert entry["task_id"] == "old_task"
    assert entry["failed_tool_calls"] == 0
    assert store.health().schema_version == 2
    store.close()


def test_health_reports_schema_and_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started, finished = _times()
    store.record(
        _result(),
        source="agent",
        prompt_length=1,
        started_at=started,
        finished_at=finished,
    )

    health = store.health()

    assert health.available is True
    assert health.schema_version == 2
    assert health.runs == 1
    assert health.database_path.endswith("agent-runs.db")
    store.close()
    assert store.health().available is False


def test_retention_constant_is_bounded() -> None:
    assert MAX_RUNS == 500
