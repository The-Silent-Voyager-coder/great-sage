"""Audit log tests: append-only JSONL, redaction, and content stripping."""

from __future__ import annotations

import json
from pathlib import Path

from greatsage.events.models import TOOL_COMPLETED, Event
from greatsage.observability.audit import AuditLog


def test_audit_log_redacts_and_drops_content(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    secret = "sk-" + "a" * 24
    audit = AuditLog(path)
    audit.write(
        Event(
            type=TOOL_COMPLETED,
            source="tools",
            session_id="s1",
            task_id="t1",
            payload={
                "tool_id": "shell.execute",
                "risk_level": "high",
                "decision": "allow",
                "duration_ms": 1.5,
                "content": "prompt or output should not be audited",
                "prompt": "also omitted",
                "arguments": {"token": secret},
                "error": f"command failed with {secret}",
            },
        )
    )
    audit.close()

    text = path.read_text(encoding="utf-8")
    assert secret not in text
    record = json.loads(text.strip())
    assert record["event"] == TOOL_COMPLETED
    assert record["session_id"] == "s1"
    assert record["task_id"] == "t1"
    assert record["risk_level"] == "high"
    assert record["decision"] == "allow"
    assert "content" not in record["payload"]
    assert "prompt" not in record["payload"]
    assert "arguments" not in record["payload"]
    assert "[REDACTED" in record["error"]


def test_audit_log_appends_and_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    audit = AuditLog(path)
    audit.write(Event(type="First", source="test"))
    audit.write(Event(type="Second", source="test"))
    audit.close()
    audit.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["First", "Second"]
