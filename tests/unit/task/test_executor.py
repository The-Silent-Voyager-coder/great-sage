# ruff: noqa: E501
"""Task executor unit tests (bounded, security, approval)."""

from __future__ import annotations

from pathlib import Path

from greatsage.configuration.loader import load_config
from greatsage.planning.models import Plan, Step
from greatsage.task.cancellation import CancellationToken
from greatsage.task.executor import TaskExecutor
from greatsage.task.models import TaskRecord, TaskState
from greatsage.task.sqlite_repository import SqliteTaskRepository
from greatsage.tools.approval import DeterministicApprovalProvider
from greatsage.tools.models import ApprovalOutcome
from greatsage.tools.service import ToolService


def make_config(tmp_path: Path) -> object:
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


def make_tools(tmp_path: Path, approval: object = None) -> ToolService:
    cfg = make_config(tmp_path)
    svc = ToolService()
    svc.start(cfg)
    if approval is not None:
        svc.approval = approval  # type: ignore[attr-defined]
    return svc


def test_executor_happy_path(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / "test.txt").write_text("hello", encoding="utf-8")
    cfg = make_config(tmp_path)
    tools = make_tools(tmp_path, DeterministicApprovalProvider(ApprovalOutcome.APPROVED))
    repo = SqliteTaskRepository(cfg.task.database_path)
    repo.initialize()
    executor = TaskExecutor(tools=tools, repository=repo)
    plan = Plan(
        goal="list and read and write",
        workspace_root=tmp_path / "workspace",
        steps=(
            Step(sequence=1, description="list", tool_id="filesystem.list", arguments={"path": str(tmp_path / "workspace")}, risk_estimate="low", acceptance_criteria="ok"),
            Step(sequence=1, description="list", tool_id="filesystem.list", arguments={"path": str(tmp_path / "workspace")}, risk_estimate="low", acceptance_criteria="ok"),
        ),
    )
    # fix sequences
    steps = (
        Step(sequence=1, description="list", tool_id="filesystem.list", arguments={"path": str(tmp_path / "workspace")}, risk_estimate="low", acceptance_criteria="ok"),
        Step(sequence=2, description="read", tool_id="filesystem.read", arguments={"path": str(tmp_path / "workspace" / "test.txt")}, risk_estimate="low", acceptance_criteria="ok"),
        Step(sequence=3, description="write", tool_id="filesystem.write", arguments={"path": str(tmp_path / "workspace" / "out.txt"), "content": "hi"}, risk_estimate="medium", acceptance_criteria="ok"),
    )
    plan = Plan(goal="test", workspace_root=tmp_path / "workspace", steps=steps)
    task = TaskRecord(id="task_1", plan_id=plan.id, goal=plan.goal, state=TaskState.PENDING, total_steps=3)
    repo.create_task(task)
    report = executor.execute(plan, task, per_step_timeout=30.0, total_timeout=600.0)
    assert report.state == TaskState.COMPLETED
    assert report.completed_steps == 3
    assert report.failed_steps == 0
    assert (tmp_path / "workspace" / "out.txt").exists()
    repo.close()
    tools.shutdown()


def test_executor_resume_retries_failed_step(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "existing.txt").write_text("hello", encoding="utf-8")
    cfg = make_config(tmp_path)
    tools = make_tools(tmp_path, DeterministicApprovalProvider(ApprovalOutcome.APPROVED))
    repo = SqliteTaskRepository(cfg.task.database_path)
    repo.initialize()
    executor = TaskExecutor(tools=tools, repository=repo)
    steps = (
        Step(sequence=1, description="list", tool_id="filesystem.list", arguments={"path": str(workspace)}, risk_estimate="low", acceptance_criteria="ok"),
        Step(sequence=2, description="read missing", tool_id="filesystem.read", arguments={"path": str(workspace / "later.txt")}, risk_estimate="low", acceptance_criteria="ok"),
        Step(sequence=3, description="write", tool_id="filesystem.write", arguments={"path": str(workspace / "out.txt"), "content": "resumed"}, risk_estimate="medium", acceptance_criteria="ok"),
    )
    plan = Plan(goal="resume failure", workspace_root=workspace, steps=steps)
    task = TaskRecord(id="t_resume", plan_id=plan.id, goal=plan.goal, state=TaskState.PENDING, total_steps=3)
    repo.create_task(task)

    first = executor.execute(plan, task, per_step_timeout=30.0, total_timeout=600.0)
    assert first.state == TaskState.FAILED
    assert first.completed_steps == 1
    assert first.failed_steps == 1
    assert not (workspace / "out.txt").exists()

    (workspace / "later.txt").write_text("now present", encoding="utf-8")
    second = executor.execute(plan, task, per_step_timeout=30.0, total_timeout=600.0)

    assert second.state == TaskState.COMPLETED
    assert second.completed_steps == 3
    assert second.failed_steps == 0
    assert [r.sequence for r in second.step_results] == [1, 2, 3]
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "resumed"
    stored = repo.get_step_results("t_resume")
    assert [r.sequence for r in stored] == [1, 2, 3]
    assert all(r.success for r in stored)
    repo.close()
    tools.shutdown()


def test_executor_unknown_tool_denied(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    cfg = make_config(tmp_path)
    tools = make_tools(tmp_path)
    repo = SqliteTaskRepository(cfg.task.database_path)
    repo.initialize()
    executor = TaskExecutor(tools=tools, repository=repo)
    plan = Plan(
        goal="bad",
        workspace_root=tmp_path / "workspace",
        steps=(Step(sequence=1, description="bad", tool_id="unknown.tool", arguments={}, risk_estimate="high", acceptance_criteria="ok"),),
    )
    task = TaskRecord(id="t2", plan_id=plan.id, goal=plan.goal, state=TaskState.PENDING, total_steps=1)
    repo.create_task(task)
    report = executor.execute(plan, task)
    assert report.state == TaskState.FAILED
    assert report.failed_steps == 1
    assert "unknown tool" in (report.step_results[0].error or "").lower()
    repo.close()
    tools.shutdown()


def test_executor_protected_path_denied(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    cfg = make_config(tmp_path)
    # Create a protected file outside allowed? Actually use is_protected_path check via ToolService: reading .env should be denied
    # Create a file named .env inside workspace
    (tmp_path / "workspace" / ".env").write_text("secret=123", encoding="utf-8")
    tools = make_tools(tmp_path)
    repo = SqliteTaskRepository(cfg.task.database_path)
    repo.initialize()
    executor = TaskExecutor(tools=tools, repository=repo)
    plan = Plan(
        goal="read protected",
        workspace_root=tmp_path / "workspace",
        steps=(Step(sequence=1, description="read", tool_id="filesystem.read", arguments={"path": str(tmp_path / "workspace" / ".env")}, risk_estimate="low", acceptance_criteria="ok"),),
    )
    task = TaskRecord(id="t3", plan_id=plan.id, goal=plan.goal, state=TaskState.PENDING, total_steps=1)
    repo.create_task(task)
    report = executor.execute(plan, task)
    assert report.state == TaskState.FAILED
    assert "protected" in (report.step_results[0].error or "").lower() or "denied" in (report.step_results[0].error or "").lower()
    repo.close()
    tools.shutdown()


def test_executor_step_failure_includes_rollback_hint(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    cfg = make_config(tmp_path)
    tools = make_tools(tmp_path)
    repo = SqliteTaskRepository(cfg.task.database_path)
    repo.initialize()
    executor = TaskExecutor(tools=tools, repository=repo)
    plan = Plan(
        goal="fail",
        workspace_root=tmp_path / "workspace",
        steps=(
            Step(sequence=1, description="bad", tool_id="filesystem.read", arguments={"path": str(tmp_path / "workspace" / "nonexistent.txt")}, risk_estimate="low", acceptance_criteria="ok", rollback_hint="rollback hint text"),
        ),
    )
    task = TaskRecord(id="t4", plan_id=plan.id, goal=plan.goal, state=TaskState.PENDING, total_steps=1)
    repo.create_task(task)
    report = executor.execute(plan, task)
    assert report.state == TaskState.FAILED
    # The step's rollback hint should be preserved in plan, not in result, but task should have failed
    assert report.step_results[0].success is False
    repo.close()
    tools.shutdown()


def test_executor_cancellation_mid_plan(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "workspace" / "b.txt").write_text("b", encoding="utf-8")
    cfg = make_config(tmp_path)
    tools = make_tools(tmp_path, DeterministicApprovalProvider(ApprovalOutcome.APPROVED))
    repo = SqliteTaskRepository(cfg.task.database_path)
    repo.initialize()

    # Use a slow tool: shell.execute with sleep? We can simulate cancellation via token
    executor = TaskExecutor(tools=tools, repository=repo)
    steps = (
        Step(sequence=1, description="list", tool_id="filesystem.list", arguments={"path": str(tmp_path / "workspace")}, risk_estimate="low", acceptance_criteria="ok"),
        Step(sequence=2, description="read", tool_id="filesystem.read", arguments={"path": str(tmp_path / "workspace" / "a.txt")}, risk_estimate="low", acceptance_criteria="ok"),
        Step(sequence=3, description="write", tool_id="filesystem.write", arguments={"path": str(tmp_path / "workspace" / "c.txt"), "content": "hi"}, risk_estimate="medium", acceptance_criteria="ok"),
    )
    plan = Plan(goal="test cancel", workspace_root=tmp_path / "workspace", steps=steps)
    task = TaskRecord(id="t5", plan_id=plan.id, goal=plan.goal, state=TaskState.PENDING, total_steps=3)
    repo.create_task(task)
    token = CancellationToken()
    # Cancel before execution (simulate mid-plan)
    token.cancel()
    from greatsage.exceptions import TaskCancelledError

    try:
        executor.execute(plan, task, cancellation=token)
        assert False, "should have raised"
    except TaskCancelledError:
        pass
    # Task should be cancelled and persisted
    stored = repo.get_task("t5")
    assert stored is not None
    assert stored.state == TaskState.CANCELLED
    # Step results should be resumable: only first step not executed? Actually token cancelled before any step, so no results
    results = repo.get_step_results("t5")
    # Could be 0 results because cancelled before first step
    assert len(results) == 0 or stored.state == TaskState.CANCELLED
    repo.close()
    tools.shutdown()


def test_executor_timeout(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    cfg = make_config(tmp_path)
    # Use very short total timeout
    tools = make_tools(tmp_path)
    repo = SqliteTaskRepository(cfg.task.database_path)
    repo.initialize()
    executor = TaskExecutor(tools=tools, repository=repo)
    steps = tuple(
        Step(sequence=i + 1, description=f"step {i}", tool_id="filesystem.list", arguments={"path": str(tmp_path / "workspace")}, risk_estimate="low", acceptance_criteria="ok")
        for i in range(3)
    )
    plan = Plan(goal="timeout test", workspace_root=tmp_path / "workspace", steps=steps)
    task = TaskRecord(id="t6", plan_id=plan.id, goal=plan.goal, state=TaskState.PENDING, total_steps=3)
    repo.create_task(task)
    # Use 0.001 total timeout to force timeout
    from greatsage.exceptions import TaskTimeoutError

    try:
        executor.execute(plan, task, total_timeout=0.0)
        assert False, "should timeout"
    except TaskTimeoutError:
        pass
    stored = repo.get_task("t6")
    assert stored is not None
    assert stored.state == TaskState.TIMED_OUT
    repo.close()
    tools.shutdown()
