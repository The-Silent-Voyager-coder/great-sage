"""CLI agent integration tests: agent health and bounded agent runs.

The provider is replaced with the scripted MockProvider (offline; the real
provider registry is exercised everywhere else), so runs are deterministic:
one safe tool call, then a final answer. The rest of the path — CLI handler,
AgentService, AgentOrchestrator, ToolService security pipeline — is real.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import greatsage.cli as cli_module
from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, main
from greatsage.core.runtime import Runtime


def write_agent_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    path = tmp_path / "agent-cli.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Agent CLI"
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
ai:
  default_provider: "local"
  providers:
    local:
      type: "local"
      enabled: false
      base_url: "http://127.0.0.1:11434"
      model: ""
      timeout_seconds: 5.0
    opencode:
      type: "opencode"
      enabled: false
      base_url: "http://127.0.0.1:4096"
      api_key_env: ""
      model: ""
      timeout_seconds: 5.0
agent:
  enabled: true
  max_steps: 12
  max_tool_calls: 8
  max_wall_time_seconds: 300.0
  max_single_tool_calls: 3
  max_total_tool_output_bytes: 2097152
  loop_detection_threshold: 3
security:
  mode: "normal"
  default_mode: "ask"
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


def _start_runtime_with_mock(args) -> Runtime:
    runtime = Runtime.create(args.config)
    asyncio.run(runtime.start())
    runtime.intelligence.register_mock(
        provider_id="local",
        script=(
            {
                "tool_calls": [
                    {"id": "call_1", "name": "system.info", "arguments": {}}
                ]
            },
            {"content": "System checked via agent loop."},
        ),
    )
    return runtime


def _start_runtime_with_write_mock(workspace: Path):
    target = workspace / "approved.txt"

    def start(args) -> Runtime:
        runtime = Runtime.create(args.config)
        asyncio.run(runtime.start())
        runtime.intelligence.register_mock(
            provider_id="local",
            script=(
                {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "filesystem.write",
                            "arguments": {
                                "path": str(target),
                                "content": "approved",
                            },
                        }
                    ]
                },
                {"content": "write finished."},
            ),
        )
        return runtime

    return start


def test_agent_health_offline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_agent_config(tmp_path)
    code = main(["agent", "health", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Agent Health" in out
    assert "status    healthy" in out
    assert "available True" in out
    assert "steps<=" in out


def test_agent_health_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_agent_config(tmp_path)
    code = main(["agent", "health", "--json", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"available": true' in out
    assert '"status": "healthy"' in out
    assert '"current": {}' in out


def test_agent_run_safe_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    monkeypatch.setattr(cli_module, "_runtime_from_args", _start_runtime_with_mock)
    code = main(["agent", "run", "--prompt", "check the system", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Agent Run" in out
    assert "state        completed" in out
    assert "tool_calls   1" in out
    assert "failed_tools 0" in out
    assert "System checked via agent loop." in out


def test_agent_history_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    with monkeypatch.context() as context:
        context.setattr(cli_module, "_runtime_from_args", _start_runtime_with_mock)
        code = main(
            ["agent", "run", "--prompt", "check the system", "--config", str(config)]
        )
    assert code == EXIT_OK
    capsys.readouterr()

    code = main(["agent", "history", "--config", str(config)])

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "Great Sage Agent History" in captured.out
    assert "completed" in captured.out
    assert "source=cli" in captured.out
    assert "task=" in captured.out
    assert "failed_tools=0" in captured.out
    assert "system.info" not in captured.out


def test_agent_history_json_and_limit_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    with monkeypatch.context() as context:
        context.setattr(cli_module, "_runtime_from_args", _start_runtime_with_mock)
        assert (
            main(
                ["agent", "run", "--prompt", "check the system", "--config", str(config)]
            )
            == EXIT_OK
        )
    capsys.readouterr()

    code = main(
        ["agent", "history", "--json", "--limit", "1", "--config", str(config)]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert '"state": "completed"' in captured.out
    assert '"source": "cli"' in captured.out
    assert "final_text" not in captured.out

    code = main(["agent", "history", "--limit", "0", "--config", str(config)])
    assert code == EXIT_INVALID
    assert "--limit must be 1..200" in capsys.readouterr().err


def test_agent_history_empty_after_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    code = main(["agent", "history", "--config", str(config)])

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "No agent runs recorded." in captured.out


def test_agent_run_interactive_approves_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(
        cli_module, "_runtime_from_args", _start_runtime_with_write_mock(workspace)
    )
    monkeypatch.setattr("builtins.input", lambda: "y")
    code = main(
        [
            "agent", "run",
            "--prompt", "write the approved file",
            "--interactive",
            "--config", str(config),
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert (workspace / "approved.txt").read_text(encoding="utf-8") == "approved"
    assert "Approval required" in captured.err
    assert "risk       medium" in captured.err
    assert "state        completed" in captured.out
    assert "tool_calls   1" in captured.out
    assert "failed_tools 0" in captured.out


def test_agent_run_interactive_denies_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(
        cli_module, "_runtime_from_args", _start_runtime_with_write_mock(workspace)
    )
    monkeypatch.setattr("builtins.input", lambda: "n")
    code = main(
        [
            "agent", "run",
            "--prompt", "write the approved file",
            "--interactive",
            "--config", str(config),
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert not (workspace / "approved.txt").exists()
    assert "Approval required" in captured.err
    assert "denied" in captured.err
    assert "state        completed" in captured.out
    assert "tool_calls   1" in captured.out
    assert "failed_tools 1" in captured.out


def test_agent_run_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    monkeypatch.setattr(cli_module, "_runtime_from_args", _start_runtime_with_mock)
    code = main(
        [
            "agent", "run",
            "--prompt", "check the system",
            "--json", "--session-id", "cli-session-1",
            "--config", str(config),
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"state": "completed"' in out
    assert '"tool_calls": 1' in out
    assert '"session_id": "cli-session-1"' in out


def test_agent_run_empty_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    code = main(["agent", "run", "--prompt", "   ", "--config", str(config)])
    assert code == EXIT_INVALID
    assert "prompt must not be empty" in capsys.readouterr().err


def test_agent_run_offline_no_provider_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    code = main(["agent", "run", "--prompt", "do something", "--config", str(config)])
    assert code == EXIT_FAILURE
    assert "agent run:" in capsys.readouterr().err


def test_agent_run_limit_via_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_agent_config(tmp_path)
    monkeypatch.setattr(cli_module, "_runtime_from_args", _start_runtime_with_mock)
    code = main(
        [
            "agent", "run",
            "--prompt", "keep going",
            "--max-steps", "1",
            "--json",
            "--config", str(config),
        ]
    )
    assert code == EXIT_FAILURE
    out = capsys.readouterr().out
    assert '"state": "limit_reached"' in out
    assert '"reason": "max_steps"' in out
