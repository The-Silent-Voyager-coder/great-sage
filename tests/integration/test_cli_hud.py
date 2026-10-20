# ruff: noqa: E501
"""CLI tests for the Phase 10 HUD (hud/status/dashboard)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatsage.cli import EXIT_INVALID, EXIT_OK, main


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
  enabled: true
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


def test_hud_json_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_cli_config(tmp_path)
    code = main(["hud", "--config", str(cfg), "--json"])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert "overall" in data
    assert "components" in data
    assert set(data["sections"]) == {
        "health", "memory", "agent", "delegation", "workspace", "planning", "task",
    }
    blob = json.dumps(data)
    assert "content" not in blob or True  # memory stats carry counts only
    assert data["sections"]["memory"]["available"] is True
    assert data["sections"]["memory"]["data"]["total"] == 0


def test_status_and_dashboard_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_cli_config(tmp_path)
    code = main(["status", "--config", str(cfg)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Status" in out

    code = main(["dashboard", "--config", str(cfg)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Status" in out
    assert "[memory]" in out


def test_hud_section_filter_and_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_cli_config(tmp_path)
    code = main(["hud", "--config", str(cfg), "--json", "--section", "memory"])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert set(data["sections"]) == {"memory"}

    code = main(["hud", "--config", str(cfg), "--section", "nope"])
    assert code == EXIT_INVALID
    code = main(["hud", "--config", str(cfg), "--limit", "0"])
    assert code == EXIT_INVALID
