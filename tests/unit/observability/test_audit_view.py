"""Read-only audit log views (tail/search/redaction)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatsage.observability.audit_view import (
    AuditLogError,
    describe,
    search_audit,
    tail_audit,
)

_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"


def _write(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _entry(event: str, **extra: object) -> dict:
    return {
        "timestamp": f"2026-10-22T00:00:0{len(event) % 10}+00:00",
        "event": event,
        "source": "test",
        "payload": {},
        **extra,
    }


def test_tail_returns_newest_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    _write(path, [_entry("A"), _entry("B"), _entry("C")])

    entries = tail_audit(path, lines=2)

    assert [entry["event"] for entry in entries] == ["B", "C"]


def test_tail_empty_file_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    path.write_text("", encoding="utf-8")
    assert tail_audit(path) == []
    with pytest.raises(AuditLogError, match="not found"):
        tail_audit(tmp_path / "missing.log")


def test_tail_redacts_secret_values(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    _write(path, [{"timestamp": "t", "event": "ToolFailed", "error": _SECRET}])

    entries = tail_audit(path)

    assert _SECRET not in json.dumps(entries)
    assert "[REDACTED:api_key]" in entries[0]["error"]


def test_tail_invalid_json_is_returned_as_redacted_raw(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    path.write_text(f"not-json {_SECRET}\n", encoding="utf-8")

    entries = tail_audit(path)

    assert _SECRET not in entries[0]["raw"]
    assert "[REDACTED:api_key]" in entries[0]["raw"]


def test_search_is_case_insensitive_and_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    _write(
        path,
        [
            _entry("Alpha"),
            _entry("beta"),
            _entry("ALPHA"),
            _entry("gamma"),
        ],
    )

    entries = search_audit(path, "alpha", limit=2)

    assert [entry["event"] for entry in entries] == ["ALPHA", "Alpha"]


def test_search_validates_query_and_limit(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    _write(path, [_entry("A")])

    with pytest.raises(AuditLogError, match="query"):
        search_audit(path, "  ")
    with pytest.raises(AuditLogError, match="limit"):
        search_audit(path, "A", limit=0)
    with pytest.raises(AuditLogError, match="limit"):
        search_audit(path, "A", limit=501)


def test_tail_validates_line_bounds(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    path.write_text("", encoding="utf-8")

    with pytest.raises(AuditLogError, match="lines"):
        tail_audit(path, lines=0)
    with pytest.raises(AuditLogError, match="lines"):
        tail_audit(path, lines=201)


def test_describe_includes_operational_fields_and_bounds_errors() -> None:
    entry = _entry(
        "ToolDenied",
        session_id="s1",
        task_id="t1",
        decision="deny",
        risk_level="high",
        error="x" * 500,
    )

    text = describe(entry)

    assert "ToolDenied" in text
    assert "session=s1" in text
    assert "task=t1" in text
    assert "decision=deny" in text
    assert "risk=high" in text
    assert len(text) < 500
