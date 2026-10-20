# ruff: noqa: E501
"""Integration: workspace scan → plan → task execution (Phase 6)."""

from __future__ import annotations

from pathlib import Path

from greatsage.configuration.loader import load_config
from greatsage.planning.planner import Planner
from greatsage.task.executor import TaskExecutor
from greatsage.task.models import TaskRecord, TaskState
from greatsage.task.sqlite_repository import SqliteTaskRepository
from greatsage.tools.approval import DeterministicApprovalProvider
from greatsage.tools.models import ApprovalOutcome
from greatsage.tools.service import ToolService
from greatsage.workspace.scanner import WorkspaceScanner


def write_config(tmp_path: Path) -> object:
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
    return load_config(p).config


def test_scan_plan_execute_three_steps(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("hello world", encoding="utf-8")
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    cfg = write_config(tmp_path)
    # Scanner
    scanner = WorkspaceScanner(
        allowed_roots=tuple(cfg.tools.allowed_roots),
        denied_roots=tuple(cfg.tools.denied_roots),
        working_directory=cfg.tools.working_directory,
        max_scan_depth=cfg.workspace.max_scan_depth,
        max_entries=cfg.workspace.max_entries,
        scan_timeout_seconds=cfg.workspace.scan_timeout_seconds,
    )
    info = scanner.scan(ws)
    assert info.project_type.value == "python"
    assert info.structure is not None

    # Planner
    planner = Planner(max_plan_steps=cfg.planning.max_plan_steps)
    plan = planner.create_plan("list files and read file and write file", info)
    assert len(plan.steps) == 3
    # Ensure steps map to expected tools
    assert plan.steps[0].tool_id == "filesystem.list"
    assert plan.steps[1].tool_id == "filesystem.read"
    assert plan.steps[2].tool_id == "filesystem.write"

    # Adjust plan steps to use real paths inside workspace
    from greatsage.planning.models import Step

    steps = (
        Step(sequence=1, description=plan.steps[0].description, tool_id="filesystem.list", arguments={"path": str(ws)}, risk_estimate="low", acceptance_criteria="ok"),
        Step(sequence=2, description=plan.steps[1].description, tool_id="filesystem.read", arguments={"path": str(ws / "README.md")}, risk_estimate="low", acceptance_criteria="ok"),
        Step(sequence=3, description=plan.steps[2].description, tool_id="filesystem.write", arguments={"path": str(ws / "out.txt"), "content": "from plan"}, risk_estimate="medium", acceptance_criteria="ok"),
    )
    plan = plan.__class__(id=plan.id, goal=plan.goal, workspace_id=info.id, workspace_root=ws, steps=steps, status=plan.status, created_at=plan.created_at, updated_at=plan.updated_at, metadata=plan.metadata)

    # Executor
    tools = ToolService()
    tools.start(cfg)
    tools.approval = DeterministicApprovalProvider(ApprovalOutcome.APPROVED)  # type: ignore[attr-defined]
    repo = SqliteTaskRepository(cfg.task.database_path)
    repo.initialize()
    executor = TaskExecutor(tools=tools, repository=repo)
    task = TaskRecord(id="task_integ", plan_id=plan.id, goal=plan.goal, state=TaskState.PENDING, total_steps=3)
    repo.create_task(task)
    report = executor.execute(plan, task, per_step_timeout=cfg.task.per_step_timeout_seconds, total_timeout=cfg.task.total_timeout_seconds)
    assert report.state == TaskState.COMPLETED
    assert report.completed_steps == 3
    assert report.failed_steps == 0
    assert (ws / "out.txt").read_text(encoding="utf-8") == "from plan"
    # DB state
    stored = repo.get_task("task_integ")
    assert stored is not None
    assert stored.state == TaskState.COMPLETED
    results = repo.get_step_results("task_integ")
    assert len(results) == 3
    assert all(r.success for r in results)
    # TaskReport
    assert report.task_id == "task_integ"
    assert report.plan_id == plan.id
    assert report.summary

    repo.close()
    tools.shutdown()
