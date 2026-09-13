"""Shared test fixtures: temporary configuration files."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def valid_config_yaml(tmp_path: Path) -> Path:
    """A minimal, valid configuration file pointing at the tmp data root.

    Every filesystem-writing path (all database_path values, audit log,
    tool roots) is pinned under tmp_path. Never rely on loader defaults
    here: they resolve to the shared C:/GREATSAGE/data root and tests must
    not touch it (shared-dev-DB incident: a stray migration against the
    shared tasks.db broke every runtime boot).
    """
    path = tmp_path / "jarvis.yaml"
    d = str(tmp_path).replace("\\", "/")
    path.write_text(
        f"""
core:
  name: "J.A.R.V.I.S. Test"
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
  enabled: true
  database_path: "{d}/data/sage-memory.db"
  auto_save_conversations: false
  default_confidence: 0.8
  retention_days: 365
tools:
  working_directory: "{d}/workspaces"
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "LOW_WRITE"
  browser:
    default_risk: "READ"
workspace:
  enabled: true
  max_scan_depth: 3
  max_entries: 500
  scan_timeout_seconds: 10.0
  database_path: "{d}/data/sage-workspace.db"
planning:
  enabled: true
  max_plan_steps: 25
  database_path: "{d}/data/sage-plans.db"
task:
  enabled: true
  max_steps: 25
  per_step_timeout_seconds: 30.0
  total_timeout_seconds: 600.0
  database_path: "{d}/data/sage-tasks.db"
scheduler:
  enabled: true
  max_schedules: 50
  database_path: "{d}/data/sage-scheduler.db"
security:
  mode: "normal"
  default_mode: "ask"
  allow_auto_approve_read: true
  destructive_confirm: true
  audit_log: "{d}/data/sage-audit.log"
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def invalid_config_yaml(tmp_path: Path) -> Path:
    """A configuration file that fails validation."""
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
logging:
  level: "VERBOSE"
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def malformed_config_yaml(tmp_path: Path) -> Path:
    """A file that is not valid YAML."""
    path = tmp_path / "malformed.yaml"
    path.write_text("logging: [unclosed\n  level: ", encoding="utf-8")
    return path
