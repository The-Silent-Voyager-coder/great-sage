# ruff: noqa: E501
"""CLI tests for planning + task approval/verify flags (Phase 7)."""

from __future__ import annotations

import json
from pathlib import Path

from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, main


def write_cli_config(tmp_path: Path) -> Path:
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
  mode: "development"
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


def test_planning_create_verify_approve(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = write_cli_config(tmp_path)
    code = main(["planning", "create", "--goal", "list files, read file", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "draft"
    assert len(created["steps"]) == 2
    plan_id = created["id"]

    code = main(["planning", "verify", plan_id, "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True

    code = main(["planning", "approve", plan_id, "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "ready"

    # Re-approval rejected.
    code = main(["planning", "approve", plan_id, "--config", str(cfg)])
    assert code == EXIT_INVALID

    # List + get + health.
    code = main(["planning", "list", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    assert any(p["id"] == plan_id for p in json.loads(capsys.readouterr().out))
    code = main(["planning", "get", plan_id, "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    code = main(["planning", "health", "--config", str(cfg)])
    assert code == EXIT_OK


def test_task_run_verify_only_and_approve(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = write_cli_config(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "README.md").write_text("hello", encoding="utf-8")
    import yaml

    plan_data = {
        "id": "plan_p7",
        "goal": "verify me",
        "workspace_root": str(ws),
        "steps": [
            {"id": "s1", "sequence": 1, "description": "list", "tool_id": "filesystem.list", "arguments": {"path": str(ws)}, "risk_estimate": "low", "acceptance_criteria": "ok"},
        ],
        "status": "ready",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    plan_path = tmp_path / "p7.yaml"
    plan_path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")

    code = main(["task", "run", str(plan_path), "--verify-only", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # --approve installs a task-scoped provider; the plan must already be ready.
    code = main(["task", "run", str(plan_path), "--approve", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "completed"

    code = main(["task", "list", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    items = json.loads(capsys.readouterr().out)
    assert len(items) >= 1
    code = main(["task", "get", items[0]["id"], "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    assert json.loads(capsys.readouterr().out)["step_results"][0]["success"] is True


def test_task_run_verify_only_rejects_bad_plan(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = write_cli_config(tmp_path)
    import yaml

    plan_data = {
        "id": "plan_bad7",
        "goal": "bad",
        "steps": [
            {"id": "s1", "sequence": 1, "description": "bad", "tool_id": "unknown.tool", "arguments": {}, "risk_estimate": "high", "acceptance_criteria": "ok"},
        ],
        "status": "draft",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    p = tmp_path / "bad7.yaml"
    p.write_text(yaml.safe_dump(plan_data), encoding="utf-8")
    code = main(["task", "run", str(p), "--verify-only", "--config", str(cfg), "--json"])
    assert code == EXIT_FAILURE
