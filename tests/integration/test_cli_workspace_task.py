# ruff: noqa: E501
"""CLI tests for workspace and task (Phase 6)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, main


def write_cli_config(tmp_path: Path, security_mode: str = "development") -> Path:
    d = str(tmp_path).replace("\\", "/")
    p = tmp_path / "sage.yaml"
    p.write_text(
        f"""
core:
  name: "test"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "INFO"
  retention_days: 7
memory:
  enabled: false
  database_path: "{d}/data/memory.db"
  auto_save_conversations: false
  default_confidence: 0.8
  retention_days: 365
tools:
  working_directory: "{d}/workspace"
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "LOW_WRITE"
  browser:
    default_risk: "READ"
security:
  mode: "{security_mode}"
  default_mode: "ask"
  allow_auto_approve_read: true
  destructive_confirm: true
  audit_log: "{d}/data/audit.log"
workspace:
  enabled: true
  max_scan_depth: 3
  max_entries: 500
  scan_timeout_seconds: 10.0
  database_path: "{d}/data/workspace.db"
planning:
  enabled: true
  max_plan_steps: 25
  database_path: "{d}/data/plans.db"
task:
  enabled: true
  max_steps: 25
  per_step_timeout_seconds: 30.0
  total_timeout_seconds: 600.0
  database_path: "{d}/data/tasks.db"
""",
        encoding="utf-8",
    )
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return p


def test_workspace_scan_and_info(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_cli_config(tmp_path)
    (tmp_path / "workspace" / "README.md").write_text("hi", encoding="utf-8")
    code = main(["workspace", "scan", str(tmp_path / "workspace"), "--config", str(cfg)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Workspace Scan" in out

    code = main(["workspace", "info", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["root"] == str((tmp_path / "workspace").resolve())

    code = main(["workspace", "health", "--config", str(cfg)])
    assert code == EXIT_OK
    assert "Workspace Health" in capsys.readouterr().out


def test_workspace_scan_outside_allowed_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_cli_config(tmp_path)
    outside = tmp_path.parent / "elsewhere_cli"
    outside.mkdir(exist_ok=True)
    code = main(["workspace", "scan", str(outside), "--config", str(cfg)])
    assert code == EXIT_INVALID


def test_task_run_list_get(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_cli_config(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "README.md").write_text("hello", encoding="utf-8")
    # Create a plan file
    plan_data = {
        "id": "plan_cli",
        "goal": "test task",
        "workspace_root": str(ws),
        "steps": [
            {"sequence": 1, "description": "list", "tool_id": "filesystem.list", "arguments": {"path": str(ws)}, "risk_estimate": "low", "acceptance_criteria": "ok"},
            {"sequence": 2, "description": "read", "tool_id": "filesystem.read", "arguments": {"path": str(ws / "README.md")}, "risk_estimate": "low", "acceptance_criteria": "ok"},
            {"sequence": 3, "description": "write", "tool_id": "filesystem.write", "arguments": {"path": str(ws / "cli_out.txt"), "content": "cli"}, "risk_estimate": "medium", "acceptance_criteria": "ok"},
        ],
        "status": "ready",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    import yaml

    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")
    code = main(["task", "run", str(plan_path), "--config", str(cfg)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Task Run" in out

    # list should contain the task
    code = main(["task", "list", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    items = json.loads(out)
    assert len(items) >= 1
    task_id = items[0]["id"]

    code = main(["task", "get", task_id, "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == task_id
    assert len(data["step_results"]) == 3

    # health
    code = main(["task", "health", "--config", str(cfg)])
    assert code == EXIT_OK


def test_task_run_requires_ready_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_cli_config(tmp_path)
    ws = tmp_path / "workspace"
    import yaml

    plan_data = {
        "id": "plan_draft",
        "goal": "draft task",
        "workspace_root": str(ws),
        "steps": [
            {
                "sequence": 1,
                "description": "list",
                "tool_id": "filesystem.list",
                "arguments": {"path": str(ws)},
                "risk_estimate": "low",
                "acceptance_criteria": "ok",
            }
        ],
        "status": "draft",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    plan_path = tmp_path / "draft.yaml"
    plan_path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")
    code = main(["task", "run", str(plan_path), "--config", str(cfg)])
    assert code == EXIT_INVALID
    assert "approve it before execution" in capsys.readouterr().err


def test_task_high_risk_requires_approve_and_runs_with_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_cli_config(tmp_path)
    ws = tmp_path / "workspace"
    import yaml

    plan_data = {
        "id": "plan_high_risk",
        "goal": "run a shell command",
        "workspace_root": str(ws),
        "steps": [
            {
                "sequence": 1,
                "description": "run python",
                "tool_id": "shell.execute",
                "arguments": {
                    "command": [sys.executable, "-c", "print('task-approved')"]
                },
                "risk_estimate": "high",
                "acceptance_criteria": "command exits zero",
            }
        ],
        "status": "ready",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    plan_path = tmp_path / "high-risk.yaml"
    plan_path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")

    code = main(["task", "run", str(plan_path), "--config", str(cfg)])
    assert code == EXIT_FAILURE
    capsys.readouterr()

    code = main(["task", "run", str(plan_path), "--approve", "--config", str(cfg)])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "state    completed" in captured.out


def test_task_resume_retries_failed_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_cli_config(tmp_path)
    ws = tmp_path / "workspace"
    import yaml

    plan_data = {
        "id": "plan_resume",
        "goal": "resume a failed read",
        "workspace_root": str(ws),
        "steps": [
            {
                "sequence": 1,
                "description": "list",
                "tool_id": "filesystem.list",
                "arguments": {"path": str(ws)},
                "risk_estimate": "low",
                "acceptance_criteria": "ok",
            },
            {
                "sequence": 2,
                "description": "read missing",
                "tool_id": "filesystem.read",
                "arguments": {"path": str(ws / "later.txt")},
                "risk_estimate": "low",
                "acceptance_criteria": "ok",
            },
            {
                "sequence": 3,
                "description": "write",
                "tool_id": "filesystem.write",
                "arguments": {"path": str(ws / "resumed.txt"), "content": "resumed"},
                "risk_estimate": "medium",
                "acceptance_criteria": "ok",
            },
        ],
        "status": "ready",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    plan_path = tmp_path / "resume.yaml"
    plan_path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")

    code = main(["task", "run", str(plan_path), "--config", str(cfg)])
    assert code == EXIT_FAILURE
    capsys.readouterr()

    code = main(["task", "list", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    tasks = json.loads(capsys.readouterr().out)
    task_id = tasks[0]["id"]

    code = main(["planning", "get", "plan_resume", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    failed_plan = json.loads(capsys.readouterr().out)
    assert failed_plan["status"] == "failed"
    assert failed_plan["metadata"]["task_id"] == task_id

    (ws / "later.txt").write_text("now present", encoding="utf-8")
    code = main(["task", "resume", task_id, "--config", str(cfg)])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "state    completed" in captured.out
    assert (ws / "resumed.txt").read_text(encoding="utf-8") == "resumed"

    code = main(["planning", "get", "plan_resume", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    completed_plan = json.loads(capsys.readouterr().out)
    assert completed_plan["status"] == "completed"
    assert completed_plan["metadata"]["task_id"] == task_id


def test_task_resume_approve_retries_medium_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_cli_config(tmp_path, security_mode="normal")
    ws = tmp_path / "workspace"
    import yaml

    plan_data = {
        "id": "plan_resume_approve",
        "goal": "write after approval",
        "workspace_root": str(ws),
        "steps": [
            {
                "sequence": 1,
                "description": "write",
                "tool_id": "filesystem.write",
                "arguments": {"path": str(ws / "approved.txt"), "content": "yes"},
                "risk_estimate": "medium",
                "acceptance_criteria": "ok",
            }
        ],
        "status": "ready",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    plan_path = tmp_path / "resume-approve.yaml"
    plan_path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")

    code = main(["task", "run", str(plan_path), "--config", str(cfg)])
    assert code == EXIT_FAILURE
    capsys.readouterr()

    code = main(["task", "list", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    task_id = json.loads(capsys.readouterr().out)[0]["id"]

    code = main(
        ["task", "resume", task_id, "--approve", "--config", str(cfg)]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "state    completed" in captured.out
    assert (ws / "approved.txt").read_text(encoding="utf-8") == "yes"


def test_task_unknown_tool_denied(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_cli_config(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "README.md").write_text("hi", encoding="utf-8")
    import yaml

    plan_data = {
        "id": "plan_bad",
        "goal": "bad",
        "workspace_root": str(ws),
        "steps": [
            {"sequence": 1, "description": "bad", "tool_id": "unknown.tool", "arguments": {}, "risk_estimate": "high", "acceptance_criteria": "ok"},
        ],
        "status": "ready",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(plan_data), encoding="utf-8")
    code = main(["task", "run", str(p), "--config", str(cfg)])
    # Task fails, CLI returns failure (1)
    assert code == EXIT_FAILURE


def test_task_protected_path_denied(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_cli_config(tmp_path)
    ws = tmp_path / "workspace"
    (ws / ".env").write_text("secret", encoding="utf-8")
    import yaml

    plan_data = {
        "id": "plan_prot",
        "goal": "protected",
        "workspace_root": str(ws),
        "steps": [
            {"sequence": 1, "description": "read", "tool_id": "filesystem.read", "arguments": {"path": str(ws / ".env")}, "risk_estimate": "low", "acceptance_criteria": "ok"},
        ],
        "status": "ready",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    p = tmp_path / "prot.yaml"
    p.write_text(yaml.safe_dump(plan_data), encoding="utf-8")
    code = main(["task", "run", str(p), "--config", str(cfg)])
    assert code == EXIT_FAILURE
