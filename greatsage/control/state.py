"""Local control-plane state.

The control plane is deliberately thin: it starts bounded agent runs through
``AgentService`` and explicitly approved plan/resume runs through
``TaskService``, exposes the pending-approval queue from
``ApprovalBroker``, and reports read-only plan/task state. It never executes
a tool or approves a request itself.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from greatsage.agent.approval_broker import ApprovalBroker
from greatsage.control.views import (
    plan_detail,
    plan_summary,
    task_detail,
    task_report,
    task_summary,
)
from greatsage.exceptions import (
    PlanningUnavailableError,
    PlanningValidationError,
    TaskUnavailableError,
    TaskValidationError,
)
from greatsage.interface.service import HudService
from greatsage.tools.redaction import redact_secrets

if TYPE_CHECKING:
    from greatsage.core.runtime import Runtime

log = logging.getLogger("greatsage.control.state")

_MAX_PROMPT = 8000
_MAX_IDENTIFIER = 128
_MAX_STEPS = 25
_MAX_FINAL_TEXT = 20_000
_MAX_ERROR_TEXT = 4_000
_MAX_PLANS = 25
_MAX_TASKS = 25
_HEALTH_CACHE_SECONDS = 2.0


class ControlValidationError(ValueError):
    """A control-plane request failed local validation."""


class ControlNotFoundError(ControlValidationError):
    """A requested plan or task does not exist."""


class ControlUnavailableError(RuntimeError):
    """The subsystem required for a control-plane request is unavailable."""


class ControlBusyError(RuntimeError):
    """Only one agent/task execution may be active at a time."""


class ControlState:
    """Thread-safe, transport-neutral state for one local control server."""

    def __init__(self, runtime: Runtime, *, token: str) -> None:
        self._runtime = runtime
        self._token = token
        self._lock = threading.Lock()
        self._broker: ApprovalBroker | None = None
        self._thread: threading.Thread | None = None
        self._kind: str | None = None
        self._plan_id: str | None = None
        self._task_id: str | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._health_lock = threading.Lock()
        self._health: dict[str, Any] = {}
        self._health_at = 0.0

    @property
    def token(self) -> str:
        return self._token

    # --- agent execution ------------------------------------------------

    def start_agent(
        self,
        prompt: Any,
        *,
        session_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any]:
        """Start one bounded agent run; 409-style errors are typed."""
        cleaned_prompt = _text(prompt, "prompt", _MAX_PROMPT)
        if not cleaned_prompt:
            raise ControlValidationError("prompt must not be empty")
        session_id = _optional_text(session_id, "session_id")
        provider = _optional_text(provider, "provider")
        model = _optional_text(model, "model")
        if max_steps is not None:
            if isinstance(max_steps, bool) or not isinstance(max_steps, int):
                raise ControlValidationError("max_steps must be an integer")
            if not 1 <= max_steps <= _MAX_STEPS:
                raise ControlValidationError(f"max_steps must be 1..{_MAX_STEPS}")

        broker = ApprovalBroker(wait_seconds=_approval_wait(self._runtime))
        self._start(
            "agent",
            target=self._agent_worker,
            args=(cleaned_prompt, session_id, provider, model, max_steps, broker),
            broker=broker,
        )
        return self.run_snapshot()

    # --- plan/task execution -------------------------------------------

    def start_task(self, plan_id: Any) -> dict[str, Any]:
        """Run a READY plan through TaskService with explicit approval."""
        identifier = _identifier(plan_id, "plan_id")
        plan = self._get_plan_dict(identifier)
        if plan.get("status") != "ready":
            raise ControlValidationError(
                f"plan {identifier} is {plan.get('status', 'unknown')}; "
                "approve it before execution"
            )
        self._require_task_available()
        task_id = f"task_control_{uuid.uuid4().hex}"
        self._start(
            "task",
            target=self._task_worker,
            args=(identifier, task_id),
            plan_id=identifier,
            task_id=task_id,
        )
        return self.run_snapshot()

    def start_resume(self, task_id: Any) -> dict[str, Any]:
        """Resume a failed/timed-out/paused task with explicit approval."""
        identifier = _identifier(task_id, "task_id")
        task = self._get_task_dict(identifier)
        state = str(task.get("state", ""))
        if state in ("completed", "cancelled", "running"):
            raise ControlValidationError(
                f"task {identifier} is {state}; it cannot be resumed"
            )
        self._require_task_available()
        plan_id = str(task.get("plan_id") or "")
        self._start(
            "task",
            target=self._resume_worker,
            args=(identifier,),
            plan_id=plan_id,
            task_id=identifier,
        )
        return self.run_snapshot()

    def cancel(self) -> bool:
        """Reject outstanding approvals and request cancellation (best-effort)."""
        with self._lock:
            broker = self._broker
            kind = self._kind
            task_id = self._task_id
        if broker is not None:
            broker.reject_all()
        try:
            if kind == "task" and task_id:
                self._runtime.task.cancel(task_id)
            else:
                self._runtime.agent.cancel()
        except Exception as exc:  # cancellation is best effort at this boundary
            log.warning(
                "control-plane cancellation failed: %s",
                exc,
                extra={"component": "control"},
            )
            return False
        return True

    def decide(self, request_id: str, *, approve: bool) -> bool:
        """Answer one pending approval; false when it is no longer pending."""
        with self._lock:
            broker = self._broker
        if broker is None:
            return False
        if approve:
            return broker.approve(request_id)
        return broker.reject(request_id)

    def run_snapshot(self) -> dict[str, Any]:
        """Return the current/last execution without probing subsystem health."""
        with self._lock:
            thread = self._thread
            active = thread is not None and thread.is_alive()
            broker = self._broker
            kind = self._kind
            started_at = self._started_at
            finished_at = self._finished_at
            plan_id = self._plan_id
            task_id = self._task_id
            result = dict(self._last_result) if self._last_result is not None else None
            error = self._last_error
        pending = [item.to_dict() for item in broker.pending()] if broker else []
        if result:
            task_id = result.get("task_id") or result.get("id") or task_id
            plan_id = result.get("plan_id") or plan_id
        return {
            "active": active,
            "kind": kind,
            "task_id": task_id,
            "plan_id": plan_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "result": result,
            "error": error,
            "approvals": pending,
        }

    def snapshot(self) -> dict[str, Any]:
        """One JSON-safe snapshot for the dashboard/API."""
        agent_health = self._safe_health(lambda: self._runtime.agent.health())
        planning_health = self._safe_health(lambda: self._runtime.planning.health())
        task_health = self._safe_health(lambda: self._runtime.task.health())
        try:
            plans = self.list_plans()
        except Exception:
            plans = []
        try:
            tasks = self.list_tasks()
        except Exception:
            tasks = []
        return {
            "status": self._health_snapshot(),
            "agent": agent_health,
            "planning": planning_health,
            "task": task_health,
            "run": self.run_snapshot(),
            "plans": plans,
            "tasks": tasks,
        }

    # --- plan/task read paths ------------------------------------------

    def list_plans(self) -> list[dict[str, Any]]:
        try:
            plans = self._runtime.planning.list()
        except PlanningUnavailableError as exc:
            raise ControlUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise ControlUnavailableError(f"planning unavailable: {exc}") from exc
        return [
            plan_summary(plan)
            for plan in plans[:_MAX_PLANS]
            if isinstance(plan, dict)
        ]

    def get_plan(self, plan_id: Any) -> dict[str, Any]:
        identifier = _identifier(plan_id, "plan_id")
        return plan_detail(self._get_plan_dict(identifier))

    def approve_plan(self, plan_id: Any) -> dict[str, Any]:
        """Run the human gate for a DRAFT plan; verification happens inside."""
        identifier = _identifier(plan_id, "plan_id")
        self._get_plan_dict(identifier)
        try:
            plan = self._runtime.planning.approve(identifier)
        except PlanningValidationError as exc:
            raise ControlValidationError(str(exc)) from exc
        except PlanningUnavailableError as exc:
            raise ControlUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise ControlUnavailableError(f"planning unavailable: {exc}") from exc
        return plan_detail(plan)

    def list_tasks(self) -> list[dict[str, Any]]:
        try:
            tasks = self._runtime.task.list(limit=_MAX_TASKS)
        except TaskUnavailableError as exc:
            raise ControlUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise ControlUnavailableError(f"task subsystem unavailable: {exc}") from exc
        return [
            task_summary(task)
            for task in tasks[:_MAX_TASKS]
            if isinstance(task, dict)
        ]

    def get_task(self, task_id: Any) -> dict[str, Any]:
        identifier = _identifier(task_id, "task_id")
        return task_detail(self._get_task_dict(identifier))

    def cancel_task(self, task_id: Any) -> dict[str, Any]:
        identifier = _identifier(task_id, "task_id")
        self._get_task_dict(identifier)
        try:
            return self._runtime.task.cancel(identifier)
        except TaskValidationError as exc:
            raise ControlValidationError(str(exc)) from exc
        except TaskUnavailableError as exc:
            raise ControlUnavailableError(str(exc)) from exc

    # --- internals -----------------------------------------------------

    def _start(
        self,
        kind: str,
        *,
        target: Any,
        args: tuple[Any, ...],
        plan_id: str | None = None,
        task_id: str | None = None,
        broker: ApprovalBroker | None = None,
    ) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ControlBusyError("an execution is already active")
            self._kind = kind
            self._plan_id = plan_id
            self._task_id = task_id
            self._broker = broker
            self._started_at = time.time()
            self._finished_at = None
            self._last_result = None
            self._last_error = None
            thread = threading.Thread(
                target=target,
                args=args,
                name=f"greatsage-control-{kind}",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def _agent_worker(
        self,
        prompt: str,
        session_id: str | None,
        provider: str | None,
        model: str | None,
        max_steps: int | None,
        broker: ApprovalBroker,
    ) -> None:
        try:
            result = self._runtime.agent.run(
                prompt=prompt,
                session_id=session_id,
                provider=provider,
                model=model,
                max_steps=max_steps,
                approval=broker,
            )
            data = result.to_dict()
            text = data.get("final_text")
            if isinstance(text, str) and len(text) > _MAX_FINAL_TEXT:
                data["final_text"] = text[:_MAX_FINAL_TEXT] + "... [truncated]"
            error = data.get("error")
            with self._lock:
                self._last_result = data
                self._last_error = _safe_text(error) if error else None
        except Exception as exc:
            log.warning(
                "control-plane agent run failed: %s",
                exc,
                extra={"component": "control"},
            )
            with self._lock:
                self._last_error = _safe_text(str(exc)) or type(exc).__name__
        finally:
            with self._lock:
                self._finished_at = time.time()

    def _task_worker(self, plan_id: str, task_id: str) -> None:
        try:
            report = self._runtime.task.run(plan_id, task_id=task_id, approved=True)
            data = task_report(report.to_dict())
            with self._lock:
                self._last_result = data
                self._last_error = None
        except Exception as exc:
            log.warning(
                "control-plane task run failed: %s",
                exc,
                extra={"component": "control"},
            )
            with self._lock:
                self._last_error = _safe_text(str(exc)) or type(exc).__name__
        finally:
            with self._lock:
                self._finished_at = time.time()

    def _resume_worker(self, task_id: str) -> None:
        try:
            report = self._runtime.task.resume(task_id, approved=True)
            data = task_report(report.to_dict())
            with self._lock:
                self._last_result = data
                self._last_error = None
        except Exception as exc:
            log.warning(
                "control-plane task resume failed: %s",
                exc,
                extra={"component": "control"},
            )
            with self._lock:
                self._last_error = _safe_text(str(exc)) or type(exc).__name__
        finally:
            with self._lock:
                self._finished_at = time.time()

    def _get_plan_dict(self, plan_id: str) -> dict[str, Any]:
        try:
            plan = self._runtime.planning.get(plan_id)
        except PlanningValidationError as exc:
            raise ControlValidationError(str(exc)) from exc
        except PlanningUnavailableError as exc:
            raise ControlUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise ControlUnavailableError(f"planning unavailable: {exc}") from exc
        if plan is None:
            raise ControlNotFoundError(f"plan not found: {plan_id}")
        return plan

    def _get_task_dict(self, task_id: str) -> dict[str, Any]:
        try:
            task = self._runtime.task.get(task_id)
        except TaskValidationError as exc:
            message = str(exc)
            if "unknown task" in message.lower() or "not found" in message.lower():
                raise ControlNotFoundError(f"task not found: {task_id}") from exc
            raise ControlValidationError(message) from exc
        except TaskUnavailableError as exc:
            raise ControlUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise ControlUnavailableError(f"task subsystem unavailable: {exc}") from exc
        if not task:
            raise ControlNotFoundError(f"task not found: {task_id}")
        return task

    def _require_task_available(self) -> None:
        health = self._safe_health(lambda: self._runtime.task.health())
        if not health.get("available"):
            detail = str(health.get("detail") or "task subsystem unavailable")
            raise ControlUnavailableError(detail)

    def _safe_health(self, provider: Any) -> dict[str, Any]:
        try:
            return dict(provider())
        except Exception as exc:
            return {
                "available": False,
                "status": "unavailable",
                "detail": _safe_text(str(exc)),
            }

    def _health_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._health_lock:
            if self._health and now - self._health_at < _HEALTH_CACHE_SECONDS:
                return dict(self._health)
        try:
            data = HudService().status(self._runtime)
        except Exception as exc:  # health is a report, never a crash
            data = {
                "overall": "UNKNOWN",
                "components": [],
                "health_available": False,
                "health_error": _safe_text(str(exc)),
            }
        with self._health_lock:
            self._health = data
            self._health_at = now
        return dict(data)


def _approval_wait(runtime: Runtime) -> float:
    try:
        configured = float(runtime.config.agent.max_wall_time_seconds)
    except Exception:
        configured = 300.0
    return max(1.0, min(300.0, configured))


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ControlValidationError(f"{name} must be a string")
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise ControlValidationError(f"{name} must be at most {limit} characters")
    return cleaned


def _identifier(value: Any, name: str) -> str:
    cleaned = _text(value, name, _MAX_IDENTIFIER)
    if not cleaned:
        raise ControlValidationError(f"{name} must not be empty")
    return cleaned


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    cleaned = _text(value, name, _MAX_IDENTIFIER)
    return cleaned or None


def _safe_text(value: Any) -> str:
    text = redact_secrets(str(value)) or ""
    if len(text) > _MAX_ERROR_TEXT:
        return text[:_MAX_ERROR_TEXT] + "... [truncated]"
    return text
