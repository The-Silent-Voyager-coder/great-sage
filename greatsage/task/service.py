"""Task service facade (Phase 6)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.exceptions import TaskUnavailableError, TaskValidationError
from greatsage.planning.models import Plan, PlanStatus
from greatsage.planning.sqlite_repository import SqlitePlanningRepository
from greatsage.task.cancellation import CancellationToken
from greatsage.task.executor import TaskExecutor
from greatsage.task.models import TaskRecord, TaskReport, TaskState
from greatsage.task.sqlite_repository import SqliteTaskRepository
from greatsage.tools.approval import ApprovalProvider
from greatsage.tools.models import ApprovalOutcome, Tool, ToolRequest

log = logging.getLogger("greatsage.task.service")


class _TaskScopedApprovalProvider:
    """Approve ASK calls only for one explicitly approved task run.

    ``task run --approve`` is a human approval for that run, not a global
    auto-approval switch. Requests carrying another task/session fall back to
    the previously installed provider (or deny when there is none).
    """

    def __init__(
        self, task_id: str, fallback: ApprovalProvider | None = None
    ) -> None:
        self._task_id = task_id
        self._fallback = fallback

    def request_approval(
        self, request: ToolRequest, tool: Tool, reason: str
    ) -> ApprovalOutcome:
        if request.task_id == self._task_id:
            return ApprovalOutcome.APPROVED
        if self._fallback is not None:
            return self._fallback.request_approval(request, tool, reason)
        return ApprovalOutcome.DENIED


class TaskService:
    """Facade over task persistence + bounded executor."""

    def __init__(
        self,
        *,
        tools: Any | None = None,
        memory: Any | None = None,
    ) -> None:
        self._tools = tools
        self._memory = memory
        self._config: SageConfig | None = None
        self._repository: SqliteTaskRepository | None = None
        self._planning_repo: SqlitePlanningRepository | None = None
        self._executor: TaskExecutor | None = None
        self._availability: str = "unavailable"
        self._detail: str = "task service not started"
        self._tokens: dict[str, CancellationToken] = {}
        self.publisher: Any = None

    def start(self, config: SageConfig | None = None) -> None:
        self._config = config
        if config is None:
            self._availability = "unavailable"
            self._detail = "no configuration provided"
            return
        if not config.task.enabled:
            self._availability = "disabled"
            self._detail = "task subsystem disabled by configuration"
            return
        if self._tools is None:
            self._availability = "unavailable"
            self._detail = "task service dependencies not wired (tools)"
            return
        try:
            repo = SqliteTaskRepository(config.task.database_path)
            repo.initialize()
            self._repository = repo
            # Planning reads always use the planning database path (its own
            # connection, even when both sections share one file).
            planning_repo = SqlitePlanningRepository(config.planning.database_path)
            planning_repo.initialize()
            self._planning_repo = planning_repo
            self._executor = TaskExecutor(tools=self._tools, repository=repo, publisher=self.publisher, memory=self._memory)  # noqa: E501
            # Sync publisher if set after start
            if self.publisher is not None and self._executor is not None:
                self._executor._publisher = self.publisher
            self._availability = "healthy"
            self._detail = f"task ready (max_steps<={config.task.max_steps}, total_timeout<={config.task.total_timeout_seconds}s)"  # noqa: E501
            log.info("task service started", extra={"component": "task"})
        except Exception as exc:  # pragma: no cover
            self._availability = "unavailable"
            self._detail = f"task start failed: {exc}"
            log.error("task start failed", exc_info=exc, extra={"component": "task"})

    def shutdown(self) -> None:
        if self._repository is not None:
            try:
                self._repository.close()
            except Exception:
                pass
        if self._planning_repo is not None:
            try:
                self._planning_repo.close()
            except Exception:
                pass
        self._repository = None
        self._planning_repo = None
        self._executor = None
        self._availability = "disabled"
        self._detail = "task service stopped"
        log.info("task service stopped", extra={"component": "task"})

    def _require_available(self) -> None:
        if self._availability != "healthy":
            raise TaskUnavailableError(self._detail or "task service not available")
        assert self._repository is not None
        assert self._planning_repo is not None
        assert self._executor is not None
        assert self._config is not None
        assert self._tools is not None

    def _load_plan(self, plan_ref: str | Path | dict[str, Any]) -> Plan:
        # plan_ref may be path to yaml/json, or dict, or plan_id
        if isinstance(plan_ref, dict):
            return Plan.from_dict(plan_ref)
        if isinstance(plan_ref, (str, Path)) and Path(str(plan_ref)).exists():
            path = Path(str(plan_ref))
            raw = path.read_text(encoding="utf-8")
            data: dict[str, Any]
            if path.suffix.lower() in (".yaml", ".yml"):
                data = yaml.safe_load(raw) or {}
            else:
                data = json.loads(raw)
            return Plan.from_dict(data)
        # Assume plan_id
        assert self._planning_repo is not None
        plan = self._planning_repo.get(str(plan_ref))
        if plan is None:
            raise TaskValidationError(f"plan not found: {plan_ref}")
        return plan

    def run(
        self,
        plan: str | Path | dict[str, Any] | Plan,
        task_id: str | None = None,
        *,
        approved: bool = False,
    ) -> TaskReport:  # noqa: E501
        self._require_available()
        assert self._repository is not None
        assert self._config is not None
        assert self._executor is not None
        assert self._tools is not None
        # Resolve plan
        if isinstance(plan, Plan):
            plan_obj = plan
        else:
            plan_obj = self._load_plan(plan)
        plan_obj.validate()
        if plan_obj.status is not PlanStatus.READY:
            raise TaskValidationError(
                f"plan {plan_obj.id} is {plan_obj.status.value}; "
                "approve it before execution (status must be ready)"
            )
        assert self._planning_repo is not None
        try:
            # Persist the exact plan so a failed task can be resumed later,
            # including plans supplied as a file rather than a stored id.
            self._planning_repo.save(plan_obj)
        except Exception as exc:
            raise TaskValidationError(f"could not persist plan for resume: {exc}") from exc
        if len(plan_obj.steps) > self._config.task.max_steps:
            raise TaskValidationError(f"plan steps {len(plan_obj.steps)} exceeds max {self._config.task.max_steps}")  # noqa: E501
        # Create task record (human approval is recorded, never assumed)
        tid = task_id or f"task_{uuid.uuid4().hex}"
        record = TaskRecord(
            id=tid,
            plan_id=plan_obj.id,
            goal=plan_obj.goal,
            state=TaskState.PENDING,
            current_step=0,
            total_steps=len(plan_obj.steps),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            metadata={"workspace_root": str(plan_obj.workspace_root) if plan_obj.workspace_root else None, "human_approved": bool(approved)},  # noqa: E501
        )
        self._repository.create_task(record)
        token = CancellationToken()
        self._tokens[tid] = token
        # Ensure executor publisher is current
        if self.publisher is not None:
            self._executor._publisher = self.publisher
        previous_approval = getattr(self._tools, "approval", None)
        if approved:
            self._tools.approval = _TaskScopedApprovalProvider(tid, previous_approval)
        try:
            report = self._executor.execute(
                plan_obj,
                record,
                per_step_timeout=self._config.task.per_step_timeout_seconds,
                total_timeout=self._config.task.total_timeout_seconds,
                cancellation=token,
            )
            return report
        finally:
            if approved:
                self._tools.approval = previous_approval
            self._tokens.pop(tid, None)

    def resume(self, task_id: str, *, approved: bool = False) -> TaskReport:
        self._require_available()
        assert self._repository is not None
        assert self._planning_repo is not None
        assert self._executor is not None
        assert self._config is not None
        assert self._tools is not None
        record = self._repository.get_task(task_id)
        if record is None:
            raise TaskValidationError(f"task not found: {task_id}")
        plan = self._planning_repo.get(record.plan_id)
        if plan is None:
            raise TaskValidationError(f"plan not found for task: {record.plan_id}")
        if record.state in (TaskState.COMPLETED, TaskState.CANCELLED):
            # Completed/cancelled tasks stay terminal; return the stored report.
            results = self._repository.get_step_results(task_id)
            return TaskReport(
                task_id=record.id,
                plan_id=record.plan_id,
                state=record.state,
                goal=record.goal,
                total_steps=record.total_steps,
                completed_steps=sum(1 for r in results if r.success),
                failed_steps=sum(1 for r in results if not r.success),
                duration_ms=0.0,
                step_results=tuple(results),
                summary=f"task {record.state.value} (resumed terminal)",
            )
        if record.state in (TaskState.FAILED, TaskState.TIMED_OUT):
            record.reopen()
            self._repository.update_task(record)
        if record.state not in (TaskState.PAUSED, TaskState.RUNNING, TaskState.PENDING):
            raise TaskValidationError(f"task not resumable in state {record.state.value}")
        previous_approval = getattr(self._tools, "approval", None)
        if approved:
            record.metadata["human_approved"] = True
            self._tools.approval = _TaskScopedApprovalProvider(task_id, previous_approval)
        token = CancellationToken()
        self._tokens[task_id] = token
        # Ensure publisher sync
        if self.publisher is not None:
            self._executor._publisher = self.publisher
        try:
            # Mark running
            record.transition(TaskState.RUNNING)
            self._repository.update_task(record)
            report = self._executor.execute(
                plan,
                record,
                per_step_timeout=self._config.task.per_step_timeout_seconds,
                total_timeout=self._config.task.total_timeout_seconds,
                cancellation=token,
            )
            return report
        finally:
            if approved:
                self._tools.approval = previous_approval
            self._tokens.pop(task_id, None)

    def verify_plan(self, plan: str | Path | dict[str, Any] | Plan) -> dict[str, Any]:
        """Statically verify a plan without executing it (Phase 7 gate).

        Accepts the same references as :meth:`run` (file path, plan id,
        dict, or Plan). Never executes a tool.
        """
        from greatsage.planning.verify import verify_plan as _verify

        self._require_available()
        assert self._config is not None
        plan_obj = plan if isinstance(plan, Plan) else self._load_plan(plan)
        return _verify(plan_obj, max_steps=self._config.task.max_steps).to_dict()

    def cancel(self, task_id: str) -> dict[str, Any]:
        self._require_available()
        token = self._tokens.get(task_id)
        if token is not None:
            token.cancel()
            return {"task_id": task_id, "cancelling": True}
        # Check if task exists and is running/paused
        assert self._repository is not None
        record = self._repository.get_task(task_id)
        if record is None:
            raise TaskValidationError(f"unknown task: {task_id}")
        if record.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMED_OUT):  # noqa: E501
            raise TaskValidationError(f"task {task_id} already finished ({record.state.value})")
        # Mark cancelled directly if not running
        try:
            record.transition(TaskState.CANCELLED)
            record.error = "cancelled via API"
            self._repository.update_task(record)
        except Exception:
            pass
        return {"task_id": task_id, "cancelling": True, "state": record.state.value}

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        self._require_available()
        assert self._repository is not None
        records = self._repository.list_tasks()
        # Enrich with step counts
        result = []
        for rec in records[:limit]:
            results = self._repository.get_step_results(rec.id)
            d = rec.to_dict()
            d["completed_steps"] = sum(1 for r in results if r.success)
            d["failed_steps"] = sum(1 for r in results if not r.success)
            result.append(d)
        return result

    def get(self, task_id: str) -> dict[str, Any]:
        self._require_available()
        assert self._repository is not None
        record = self._repository.get_task(task_id)
        if record is None:
            raise TaskValidationError(f"unknown task: {task_id}")
        results = self._repository.get_step_results(task_id)
        d = record.to_dict()
        d["step_results"] = [r.to_dict() for r in results]
        return d

    def health(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "available": self._availability == "healthy",
            "status": self._availability,
            "detail": self._detail,
            "enabled": bool(self._config is not None and self._config.task.enabled),
        }
        if self._repository is not None:
            h = self._repository.health()
            base.update({
                "accessible": h.accessible,
                "schema_valid": h.schema_valid,
                "migrations_current": h.migrations_current,
                "writable": h.writable,
                "schema_version": h.schema_version,
                "database_path": h.database_path,
                "active_tasks": len(self._tokens),
            })
        return base

    def register_health_check(self, registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self._availability == "disabled":
                return HealthStatus.HEALTHY
            if self._availability == "healthy":
                return HealthStatus.HEALTHY
            return HealthStatus.UNHEALTHY

        registry.register("task", checker, "task executor (bounded multi-step)")
