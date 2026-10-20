"""CLI tools integration tests: list/info/health/execute through the pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, main


def write_tools_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    path = tmp_path / "tools-cli.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Tools CLI"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "DEBUG"
  retention_days: 7
memory:
  enabled: false
  database_path: "{d}/data/memory.db"
  auto_save_conversations: false
  default_confidence: 0.8
  retention_days: 365
security:
  mode: "normal"
  allow_auto_approve_read: true
tools:
  working_directory: "{d}/workspace"
  execution_timeout_seconds: 10.0
  max_output_bytes: 65536
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "SYSTEM"
  browser:
    default_risk: "FORBIDDEN"
""",
        encoding="utf-8",
    )
    return path


def test_tools_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_tools_config(tmp_path)
    code = main(["tools", "list", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Tools" in out
    assert "filesystem.list" in out
    assert "shell.execute" in out
    assert "filesystem.delete" not in out


def test_tools_list_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_tools_config(tmp_path)
    code = main(["tools", "list", "--json", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"id": "filesystem.list"' in out
    assert '"risk_level": "safe"' in out
    assert '"input_schema"' in out


def test_tools_info(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_tools_config(tmp_path)
    code = main(["tools", "info", "system.info", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Tool: system.info" in out
    assert "risk_level   safe" in out
    assert "output_schema" in out


def test_tools_info_unknown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_tools_config(tmp_path)
    code = main(["tools", "info", "bogus.tool", "--config", str(config)])
    assert code == EXIT_INVALID
    assert "unknown tool" in capsys.readouterr().err


def test_tools_health(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_tools_config(tmp_path)
    code = main(["tools", "health", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Tool Health" in out
    assert "status    healthy" in out
    assert "mode      normal" in out
    assert "tools     21" in out


def test_tools_health_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_tools_config(tmp_path)
    code = main(["tools", "health", "--json", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"available": true' in out
    assert '"tool_count": 21' in out


def test_tools_execute_safe_tool(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_tools_config(tmp_path)
    code = main(["tools", "execute", "system.info", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Tool: system.info" in out
    assert "Result: success" in out
    assert '"platform"' in out


def test_tools_execute_safe_tool_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    code = main(
        ["tools", "execute", "system.info", "--json", "--config", str(config)]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"success": true' in out
    assert '"tool_id": "system.info"' in out


def test_tools_execute_invalid_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    code = main(
        ["tools", "execute", "filesystem.read", "--config", str(config)]
    )
    assert code == EXIT_INVALID
    assert "missing required argument" in capsys.readouterr().err


def test_tools_execute_unknown_tool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    code = main(
        ["tools", "execute", "bogus.tool", "--config", str(config)]
    )
    assert code == EXIT_INVALID
    assert "unknown tool" in capsys.readouterr().err


def test_tools_execute_medium_risk_needs_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    target = tmp_path / "written.txt"
    code = main(
        [
            "tools", "execute", "filesystem.write",
            f"path={target}",
            "content=hello",
            "--config", str(config),
        ]
    )
    assert code == EXIT_FAILURE
    assert "no approval provider configured" in capsys.readouterr().err
    assert not target.exists()


def test_tools_execute_medium_risk_with_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    target = tmp_path / "approved.txt"
    code = main(
        [
            "tools", "execute", "filesystem.write",
            f"path={target}",
            "content=approved",
            "--approve",
            "--config", str(config),
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Result: success" in out
    assert '"existed_before": false' in out
    assert target.read_text(encoding="utf-8") == "approved"


def test_tools_execute_shell_needs_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    python_exe = sys.executable.replace("\\", "/")
    code = main(
        [
            "tools", "execute", "shell.execute",
            f'command=["{python_exe}", "--version"]',
            "--config", str(config),
        ]
    )
    assert code == EXIT_FAILURE
    assert "no approval provider configured" in capsys.readouterr().err


def test_tools_execute_shell_with_approval_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    python_exe = sys.executable.replace("\\", "/")
    code = main(
        [
"tools", "execute", "shell.execute",
            f'command=["{python_exe}", "--version"]',
            "--approve",
            "--config", str(config),
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Result: success" in out
    assert '"exit_code": 0' in out
    assert "Python" in out


def test_tools_execute_dangerous_command_denied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    code = main(
        [
            "tools", "execute", "shell.execute",
            'command=["del", "x.txt"]',
            "--approve",
            "--config", str(config),
        ]
    )
    assert code == EXIT_FAILURE
    assert "dangerous" in capsys.readouterr().err


def test_tools_execute_path_outside_roots_denied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    outside = tmp_path.parent / "sage-outside-root"
    outside.mkdir(exist_ok=True)
    code = main(
        [
            "tools", "execute", "filesystem.read",
            f"path={outside / 'sneaky.txt'}",
            "--config", str(config),
        ]
    )
    assert code == EXIT_FAILURE
    assert "allowed roots" in capsys.readouterr().err


def test_tools_execute_session_id_propagated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_tools_config(tmp_path)
    code = main(
        [
            "tools", "execute", "system.info",
            "--session-id", "sess-cli-1",
            "--json",
            "--config", str(config),
        ]
    )
    assert code == EXIT_OK
    assert '"success": true' in capsys.readouterr().out
