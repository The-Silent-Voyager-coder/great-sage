"""CLI wiring for read-only audit inspection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, main

_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"


def _audit_path(valid_config_yaml: Path) -> Path:
    return valid_config_yaml.parent / "data" / "sage-audit.log"


def _write_audit(valid_config_yaml: Path) -> Path:
    path = _audit_path(valid_config_yaml)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "timestamp": "2026-10-22T00:00:01+00:00",
            "event": "ToolRequested",
            "source": "tools",
            "task_id": "task_1",
            "error": None,
        },
        {
            "timestamp": "2026-10-22T00:00:02+00:00",
            "event": "ToolDenied",
            "source": "tools",
            "decision": "deny",
            "error": _SECRET,
        },
        {
            "timestamp": "2026-10-22T00:00:03+00:00",
            "event": "AgentCompleted",
            "source": "agent",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )
    return path


def test_audit_tail_json_returns_newest_entries(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_audit(valid_config_yaml)

    code = main(
        ["audit", "tail", "--lines", "2", "--config", str(valid_config_yaml), "--json"]
    )
    captured = capsys.readouterr()
    entries = json.loads(captured.out)

    assert code == EXIT_OK
    assert [entry["event"] for entry in entries] == ["ToolDenied", "AgentCompleted"]
    assert _SECRET not in captured.out


def test_audit_tail_text_is_bounded_and_operational(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_audit(valid_config_yaml)

    code = main(
        ["audit", "tail", "--lines", "1", "--config", str(valid_config_yaml)]
    )
    captured = capsys.readouterr()

    assert code == EXIT_OK
    assert "AgentCompleted" in captured.out
    assert "source=agent" in captured.out


def test_audit_search_filters_and_redacts(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_audit(valid_config_yaml)

    code = main(
        [
            "audit",
            "search",
            "deny",
            "--limit",
            "1",
            "--config",
            str(valid_config_yaml),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    entries = json.loads(captured.out)

    assert code == EXIT_OK
    assert len(entries) == 1
    assert entries[0]["event"] == "ToolDenied"
    assert _SECRET not in captured.out
    assert "[REDACTED:api_key]" in entries[0]["error"]


def test_audit_search_no_matches_is_success(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_audit(valid_config_yaml)

    code = main(
        ["audit", "search", "no-such-event", "--config", str(valid_config_yaml)]
    )
    captured = capsys.readouterr()

    assert code == EXIT_OK
    assert "No audit entries matched." in captured.out


def test_audit_tail_missing_log_fails_without_creating_it(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _audit_path(valid_config_yaml)
    if path.exists():
        path.unlink()

    code = main(["audit", "tail", "--config", str(valid_config_yaml)])

    assert code == EXIT_FAILURE
    assert "audit log not found" in capsys.readouterr().err
    assert not path.exists()


def test_audit_tail_rejects_bad_line_count(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["audit", "tail", "--lines", "0", "--config", str(valid_config_yaml)])

    assert code == EXIT_INVALID
    assert "--lines must be 1..200" in capsys.readouterr().err


def test_audit_search_rejects_empty_query(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["audit", "search", "", "--config", str(valid_config_yaml)])

    assert code == EXIT_INVALID
    assert "query must be 1..200" in capsys.readouterr().err


def test_audit_search_rejects_bad_limit(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["audit", "search", "x", "--limit", "0", "--config", str(valid_config_yaml)]
    )

    assert code == EXIT_INVALID
    assert "--limit must be 1..500" in capsys.readouterr().err


def test_audit_requires_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["audit"])

    assert exc.value.code == EXIT_INVALID
    assert "audit requires a subcommand" in capsys.readouterr().err
