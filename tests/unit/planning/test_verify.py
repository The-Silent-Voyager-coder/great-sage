# ruff: noqa: E501
"""Verification + approval unit tests (Phase 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from greatsage.configuration.loader import load_config
from greatsage.exceptions import PlanningValidationError
from greatsage.planning.models import Plan, PlanStatus, Step
from greatsage.planning.service import PlanningService
from greatsage.planning.verify import KNOWN_TOOLS, verify_plan
from greatsage.tools.service import ToolService


def test_known_tools_match_started_registry(tmp_path: Path, valid_config_yaml: Path) -> None:
    # Drift guard: verify's allowlist must equal the tools ToolService.start
    # actually registers (defaults + voice/vision toolsets).
    service = ToolService()
    service.start(load_config(str(valid_config_yaml)).config)
    assert set(KNOWN_TOOLS) == set(service.registry().list_ids())


def step(sid: str, seq: int, tool: str, risk: str = "low", **kw: object) -> Step:
    return Step(id=sid, sequence=seq, description=f"do {sid}", tool_id=tool, arguments={}, risk_estimate=risk, acceptance_criteria="ok", **kw)  # type: ignore[arg-type]


def test_verify_ok_linear() -> None:
    plan = Plan(id="p", goal="g", steps=(step("s1", 1, "filesystem.list"), step("s2", 2, "filesystem.read")), status=PlanStatus.DRAFT)
    report = verify_plan(plan)
    assert report.ok and not report.errors
    assert report.execution_order == ("s1", "s2")
    assert report.requires_approval == ()


def test_verify_flags_high_risk() -> None:
    plan = Plan(id="p", goal="g", steps=(step("s1", 1, "shell.execute", "high"),), status=PlanStatus.DRAFT)
    report = verify_plan(plan)
    assert report.ok  # flaggged, not failed
    assert report.requires_approval == ("s1",)
    assert report.warnings


def test_verify_rejects_unknown_tool() -> None:
    plan = Plan(id="p", goal="g", steps=(step("s1", 1, "unknown.tool"),), status=PlanStatus.DRAFT)
    report = verify_plan(plan)
    assert not report.ok
    assert any("unknown tool" in e for e in report.errors)


def test_verify_rejects_bad_sequence_and_empty_goal() -> None:
    plan = Plan(id="p", goal="  ", steps=(step("s1", 2, "filesystem.list"),), status=PlanStatus.DRAFT)
    report = verify_plan(plan)
    assert not report.ok
    assert any("goal" in e for e in report.errors)
    assert any("contiguous" in e for e in report.errors)


def test_verify_rejects_cycle() -> None:
    a = step("a", 1, "filesystem.list", depends_on=("b",))
    b = step("b", 2, "filesystem.read", depends_on=("a",))
    plan = Plan(id="p", goal="g", steps=(a, b), status=PlanStatus.DRAFT)
    assert not verify_plan(plan).ok


def _service(tmp_path: Path) -> PlanningService:
    d = str(tmp_path).replace("\\", "/")
    cfg_path = tmp_path / "sage.yaml"
    cfg_path.write_text(
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
  enabled: false
  max_scan_depth: 3
  max_entries: 500
  scan_timeout_seconds: 10.0
  database_path: "{d}/data/workspace.db"
planning:
  enabled: true
  max_plan_steps: 25
  database_path: "{d}/data/plans.db"
task:
  enabled: false
  max_steps: 25
  per_step_timeout_seconds: 30.0
  total_timeout_seconds: 600.0
  database_path: "{d}/data/tasks.db"
""",
        encoding="utf-8",
    )
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    loaded = load_config(str(cfg_path))
    svc = PlanningService()
    svc.start(loaded.config)
    return svc


def test_approve_flow(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    try:
        created = svc.create_plan("list files", None)
        assert created["status"] == "draft"
        verified = svc.verify(created["id"])
        assert verified["ok"] is True
        approved = svc.approve(created["id"])
        assert approved["status"] == "ready"
        # Re-approval rejected (only drafts).
        with pytest.raises(PlanningValidationError, match="only draft"):
            svc.approve(created["id"])
    finally:
        svc.shutdown()


def test_approve_rejects_invalid_plan(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    try:
        with pytest.raises(PlanningValidationError, match="not found"):
            svc.approve("missing")
        with pytest.raises(PlanningValidationError, match="not found"):
            svc.verify("missing")
    finally:
        svc.shutdown()
