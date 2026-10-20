# ruff: noqa: E501
"""Workspace scanner unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from greatsage.configuration.loader import load_config
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.exceptions import WorkspaceValidationError
from greatsage.workspace.models import ProjectType
from greatsage.workspace.scanner import WorkspaceScanner
from greatsage.workspace.service import WorkspaceService


def write_config(tmp_path: Path, **overrides: object) -> Path:
    d = str(tmp_path).replace("\\", "/")
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    # Write minimal config with workspace enabled
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
  level: "DEBUG"
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
    return p


def test_scan_detects_python(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    cfg = load_config(write_config(tmp_path)).config
    scanner = WorkspaceScanner(
        allowed_roots=tuple(cfg.tools.allowed_roots),
        denied_roots=tuple(cfg.tools.denied_roots),
        working_directory=cfg.tools.working_directory,
        max_scan_depth=cfg.workspace.max_scan_depth,
        max_entries=cfg.workspace.max_entries,
        scan_timeout_seconds=cfg.workspace.scan_timeout_seconds,
    )
    info = scanner.scan(tmp_path / "workspace")
    assert info.project_type == ProjectType.PYTHON
    assert info.root == (tmp_path / "workspace").resolve()
    assert info.structure is not None
    assert info.structure.total_files >= 1


def test_scan_detects_node(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    cfg = load_config(write_config(tmp_path)).config
    scanner = WorkspaceScanner(
        allowed_roots=tuple(cfg.tools.allowed_roots),
        denied_roots=tuple(cfg.tools.denied_roots),
        working_directory=cfg.tools.working_directory,
    )
    info = scanner.scan(tmp_path / "workspace")
    assert info.project_type == ProjectType.NODE


def test_scan_outside_allowed_denied(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path)).config
    scanner = WorkspaceScanner(
        allowed_roots=tuple(cfg.tools.allowed_roots),
        denied_roots=(tmp_path / "workspace",),
        working_directory=cfg.tools.working_directory,
    )
    with pytest.raises(WorkspaceValidationError, match="denied root"):
        scanner.scan(tmp_path / "workspace")


def test_scan_outside_allowed_root(tmp_path: Path) -> None:
    load_config(write_config(tmp_path))
    scanner = WorkspaceScanner(
        allowed_roots=(tmp_path / "workspace",),
        denied_roots=(),
        working_directory=tmp_path / "workspace",
    )
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    with pytest.raises(WorkspaceValidationError, match="outside allowed"):
        scanner.scan(outside)


def test_scan_entry_points_and_structure_depth_limited(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("hello", encoding="utf-8")
    (ws / "a" / "b" / "c").mkdir(parents=True)
    (ws / "a" / "b" / "c" / "deep.txt").write_text("deep", encoding="utf-8")
    cfg = load_config(write_config(tmp_path)).config
    scanner = WorkspaceScanner(
        allowed_roots=tuple(cfg.tools.allowed_roots),
        denied_roots=(),
        working_directory=cfg.tools.working_directory,
        max_scan_depth=2,
        max_entries=100,
        scan_timeout_seconds=5.0,
    )
    info = scanner.scan(ws)
    assert info.structure is not None
    # Depth limited to 2, so deep.txt at depth 3 should not be in entries
    assert all(e["depth"] <= 2 for e in info.structure.entries)


def test_scan_with_workspace_config_override(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    (ws / ".sage").mkdir(parents=True)
    (ws / ".sage" / "workspace.yaml").write_text("name: custom\nproject_type: go\n", encoding="utf-8")
    (ws / "go.mod").write_text("module x\n", encoding="utf-8")
    cfg = load_config(write_config(tmp_path)).config
    scanner = WorkspaceScanner(
        allowed_roots=tuple(cfg.tools.allowed_roots),
        denied_roots=(),
        working_directory=cfg.tools.working_directory,
    )
    info = scanner.scan(ws)
    assert info.name == "custom"
    assert info.project_type == ProjectType.GO


def test_workspace_service_health(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path)).config
    svc = WorkspaceService(tools=None)
    svc.start(cfg)
    health = svc.health()
    assert health["available"] is True
    assert health["enabled"] is True
    svc.shutdown()
    assert svc.health()["status"] == "disabled"

    # Health check registration
    svc2 = WorkspaceService(tools=None)
    svc2.start(cfg)
    reg = HealthRegistry()
    svc2.register_health_check(reg)
    assert reg.check("workspace").status is HealthStatus.HEALTHY
    svc2.shutdown()
