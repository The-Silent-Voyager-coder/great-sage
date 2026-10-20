"""Task executor — bounded step-by-step via ToolService (Phase 6)."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from greatsage.events.models import (
    TASK_CANCELLED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_STARTED,
    TASK_STEP_COMPLETED,
    TASK_STEP_FAILED,
    TASK_STEP_STARTED,
    TASK_TIMED_OUT,
    Event,
)
from greatsage.exceptions import (
    TaskCancelledError,
    TaskLimitError,
    TaskTimeoutError,
    TaskValidationError,
)
from greatsage.planning.graph import topological_order
from greatsage.planning.models import Plan, Step
from greatsage.task.cancellation import CancellationToken
from greatsage.task.limits import MAX_STEPS_CEILING
from greatsage.task.models import StepResult, TaskRecord, TaskReport, TaskState
from greatsage.task.repository import TaskRepository
from greatsage.tools.models import ToolRequest
from greatsage.tools.redaction import redact_secrets

log = logging.getLogger("greatsage.task.executor")


class TaskExecutor:
    """Executes a Plan through ToolService, bounded and persistently tracked."""

    def __init__(
        self,
        tools: Any,
        repository: TaskRepository,
        *,
        publisher: Any = None,
        memory: Any | None = None,
    ) -> None:
        self._tools = tools
        self._repository = repository
        self._publisher = publisher
        self._memory = memory

    def execute(
        self,
        plan: Plan,
        task: TaskRecord,
        *,
        per_step_timeout: float = 30.0,
        total_timeout: float = 600.0,
        cancellation: CancellationToken | None = None,
    ) -> TaskReport:
        cancellation = cancellation or CancellationToken()
        started = time.monotonic()
        started_wall = datetime.now(UTC)
        task.state = TaskState.RUNNING
        task.total_steps = len(plan.steps)
        self._repository.update_task(task)
        self._publish_task_started(task, plan)

        step_results: list[StepResult] = []
        artifacts: list[str] = []
        failed = 0
        completed = 0

        # Load existing results for resume. Only successful steps are skipped;
        # failed steps must be retried on an explicit resume (a failed result
        # from the previous attempt is replaced, not duplicated).
        existing = self._repository.get_step_results(task.id)
        completed_ids: set[str] = set()
        if existing:
            step_results = [r for r in existing if r.success]
            completed_ids = {r.step_id for r in step_results}
            completed = len(step_results)
            failed = 0
            task.current_step = len(step_results)

        # Phase 7 task-graph: execute in dependency order. Linear Phase 6
        # plans (empty depends_on) come back in sequence order unchanged.
        try:
            ordered_steps = topological_order(plan.steps)
        except Exception as exc:
            task.error = f"task graph invalid: {exc}"
            task.transition(TaskState.FAILED)
            self._repository.update_task(task)
            raise TaskValidationError(task.error) from exc

        for step in ordered_steps:
            # Skip already completed steps for resume (id-based: topo order
            # may differ from sequence order for DAG plans).
            if step.id in completed_ids:
                continue
            # Bounds checks
            if len(step_results) >= MAX_STEPS_CEILING:
                task.error = f"max steps reached: {MAX_STEPS_CEILING}"
                task.transition(TaskState.FAILED)
                self._repository.update_task(task)
                raise TaskLimitError(task.error, kind="max_steps")
            if cancellation.cancelled:
                task.transition(TaskState.CANCELLED)
                self._repository.update_task(task)
                raise TaskCancelledError("task cancelled before step")

            elapsed = time.monotonic() - started
            if elapsed >= total_timeout:
                task.transition(TaskState.TIMED_OUT)
                task.error = f"total timeout {elapsed:.1f}s >= {total_timeout:.1f}s"
                self._repository.update_task(task)
                raise TaskTimeoutError(task.error)

            self._publish_step_started(task, step)
            result = self._execute_step(task, plan, step, cancellation, per_step_timeout, started_wall)  # noqa: E501
            step_results.append(result)
            self._repository.save_step_result(task.id, result)
            # Count-based progress (sequence-based breaks under topo order).
            task.current_step = len(step_results)
            task.updated_at = datetime.now(UTC)
            # Persist after each step (resumable)
            self._repository.update_task(task)

            if result.success:
                completed += 1
                # Collect artifacts if output contains path-like strings
                if result.output and isinstance(result.output, dict):
                    for key in ("path", "file", "artifact"):
                        if key in result.output and isinstance(result.output[key], str):
                            artifacts.append(str(result.output[key]))
                self._publish_step_completed(task, step, result)
            else:
                failed += 1
                self._publish_step_failed(task, step, result)
                # On failure, include rollback hint and fail task (do not continue)
                task.error = result.error or f"step {step.sequence} failed"
                # Optionally invoke rollback hint: just log, don't auto-execute tool
                if step.rollback_hint:
                    log.info("rollback hint for step %s: %s", step.id, step.rollback_hint, extra={"component": "task", "task_id": task.id})  # noqa: E501
                task.transition(TaskState.FAILED)
                self._repository.update_task(task)
                break
            # Check cancellation after step
            if cancellation.cancelled:
                task.transition(TaskState.CANCELLED)
                self._repository.update_task(task)
                raise TaskCancelledError("task cancelled")

            # Check total timeout again
            if time.monotonic() - started >= total_timeout:
                task.transition(TaskState.TIMED_OUT)
                task.error = "total timeout exceeded after step"
                self._repository.update_task(task)
                raise TaskTimeoutError(task.error)

        # Determine final state if not already terminal
        if task.state not in (TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMED_OUT):
            if failed == 0 and completed == len(plan.steps):
                task.transition(TaskState.COMPLETED)
            elif failed > 0:
                task.transition(TaskState.FAILED)
            else:
                task.transition(TaskState.COMPLETED)
            self._repository.update_task(task)

        step_results.sort(key=lambda result: (result.sequence, result.step_id))
        duration = (time.monotonic() - started) * 1000.0
        report = TaskReport(
            task_id=task.id,
            plan_id=plan.id,
            state=task.state,
            goal=plan.goal,
            total_steps=len(plan.steps),
            completed_steps=completed,
            failed_steps=failed,
            duration_ms=duration,
            step_results=tuple(step_results),
            artifacts=artifacts,
            summary=f"task {task.state.value}: {completed}/{len(plan.steps)} steps completed",
        )
        # Optionally persist to memory if requested via metadata? Not automatic.
        # The executor does not auto-save; TaskService may do explicit memory persist.
        self._publish_task_completed(task, report)
        return report

    def _execute_step(
        self,
        task: TaskRecord,
        plan: Plan,
        step: Step,
        cancellation: CancellationToken,
        per_step_timeout: float,
        started_wall: datetime,
    ) -> StepResult:
        step_start = time.perf_counter()
        cancellation.raise_if_cancelled()
        # Fail closed: unknown tool → deny
        # Check tool exists via registry if available
        try:
            # Check if tool is registered (if tools has registry)
            if hasattr(self._tools, "registry"):
                registry = self._tools.registry()
                if step.tool_id not in registry.list_ids():
                    return StepResult(
                        step_id=step.id,
                        sequence=step.sequence,
                        tool_id=step.tool_id,
                        success=False,
                        error=f"unknown tool: {step.tool_id}",
                        duration_ms=(time.perf_counter() - step_start) * 1000.0,
                    )
        except Exception:
            pass

        # Build ToolRequest
        request = ToolRequest(
            request_id=f"{task.id}_{step.id}_{uuid.uuid4().hex[:6]}",
            tool_id=step.tool_id,
            arguments=dict(step.arguments),
            source="task",
            session_id=task.id,
            task_id=task.id,
        )
        try:
            # ToolService handles approval, path security, timeout
            result = self._tools.execute(request)
            duration = (time.perf_counter() - step_start) * 1000.0
            # Per-step timeout check (advisory, tool already bounded)
            if duration > per_step_timeout * 1000.0:
                return StepResult(
                    step_id=step.id,
                    sequence=step.sequence,
                    tool_id=step.tool_id,
                    success=False,
                    error=f"step timeout {duration:.0f}ms > {per_step_timeout*1000:.0f}ms",
                    duration_ms=duration,
                )
            if result.success:
                return StepResult(
                    step_id=step.id,
                    sequence=step.sequence,
                    tool_id=step.tool_id,
                    success=True,
                    output=result.output,
                    duration_ms=duration,
                )
            else:
                return StepResult(
                    step_id=step.id,
                    sequence=step.sequence,
                    tool_id=step.tool_id,
                    success=False,
                    error=result.error or "tool execution failed",
                    duration_ms=duration,
                )
        except TaskCancelledError:
            raise
        except Exception as exc:
            duration = (time.perf_counter() - step_start) * 1000.0
            return StepResult(
                step_id=step.id,
                sequence=step.sequence,
                tool_id=step.tool_id,
                success=False,
                error=str(exc)[:300],
                duration_ms=duration,
            )

    def _publish_task_started(self, task: TaskRecord, plan: Plan) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher(Event(type=TASK_STARTED, source="task", task_id=task.id, payload={"plan_id": plan.id, "goal": redact_secrets(plan.goal[:120]), "total_steps": len(plan.steps), "approved": bool(task.metadata.get("human_approved", False))}))  # noqa: E501
        except Exception:
            pass

    def _publish_step_started(self, task: TaskRecord, step: Step) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher(Event(type=TASK_STEP_STARTED, source="task", task_id=task.id, payload={"step_id": step.id, "sequence": step.sequence, "tool_id": step.tool_id}))  # noqa: E501
        except Exception:
            pass

    def _publish_step_completed(self, task: TaskRecord, step: Step, result: StepResult) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher(Event(type=TASK_STEP_COMPLETED, source="task", task_id=task.id, payload={"step_id": step.id, "sequence": step.sequence, "success": result.success, "duration_ms": result.duration_ms}))  # noqa: E501
        except Exception:
            pass

    def _publish_step_failed(self, task: TaskRecord, step: Step, result: StepResult) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher(Event(type=TASK_STEP_FAILED, source="task", task_id=task.id, payload={"step_id": step.id, "sequence": step.sequence, "error": redact_secrets((result.error or "")[:200])}))  # noqa: E501
        except Exception:
            pass

    def _publish_task_completed(self, task: TaskRecord, report: TaskReport) -> None:
        if self._publisher is None:
            return
        try:
            evt_type = TASK_COMPLETED if report.state == TaskState.COMPLETED else TASK_FAILED
            if report.state == TaskState.CANCELLED:
                evt_type = TASK_CANCELLED
            elif report.state == TaskState.TIMED_OUT:
                evt_type = TASK_TIMED_OUT
            self._publisher(Event(type=evt_type, source="task", task_id=task.id, payload={"plan_id": report.plan_id, "state": report.state.value, "completed_steps": report.completed_steps}))  # noqa: E501
        except Exception:
            pass
