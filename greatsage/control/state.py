"""Local control-plane state.

The control plane is deliberately thin: it starts bounded agent runs through
``AgentService`` (the same path as ``greatsage agent run``), exposes the
pending-approval queue from ``ApprovalBroker``, and reports the existing HUD
status. It never executes a tool or approves a request itself.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from greatsage.agent.approval_broker import ApprovalBroker
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
_HEALTH_CACHE_SECONDS = 2.0


class ControlValidationError(ValueError):
    """A control-plane request failed local validation."""


class ControlBusyError(RuntimeError):
    """Only one agent run may be active at a time."""


class ControlState:
    """Thread-safe, transport-neutral state for one local control server."""

    def __init__(self, runtime: Runtime, *, token: str) -> None:
        self._runtime = runtime
        self._token = token
        self._lock = threading.Lock()
        self._broker: ApprovalBroker | None = None
        self._thread: threading.Thread | None = None
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

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ControlBusyError("an agent run is already active")
            wait_seconds = _approval_wait(self._runtime)
            broker = ApprovalBroker(wait_seconds=wait_seconds)
            self._broker = broker
            self._started_at = time.time()
            self._finished_at = None
            self._last_result = None
            self._last_error = None
            thread = threading.Thread(
                target=self._worker,
                args=(cleaned_prompt, session_id, provider, model, max_steps, broker),
                name="greatsage-control-agent",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return self.run_snapshot()

    def cancel(self) -> bool:
        """Reject outstanding approvals and request cancellation (best-effort)."""
        with self._lock:
            broker = self._broker
        if broker is not None:
            broker.reject_all()
        try:
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
        """Return the current/last run without probing subsystem health."""
        with self._lock:
            thread = self._thread
            active = thread is not None and thread.is_alive()
            broker = self._broker
            started_at = self._started_at
            finished_at = self._finished_at
            result = dict(self._last_result) if self._last_result is not None else None
            error = self._last_error
        pending = [item.to_dict() for item in broker.pending()] if broker else []
        task_id = result.get("task_id") if result else None
        return {
            "active": active,
            "task_id": task_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "result": result,
            "error": error,
            "approvals": pending,
        }

    def snapshot(self) -> dict[str, Any]:
        """One JSON-safe snapshot for the dashboard/API."""
        agent_health: dict[str, Any]
        try:
            agent_health = dict(self._runtime.agent.health())
        except Exception as exc:
            agent_health = {"available": False, "status": "unavailable", "detail": str(exc)}
        return {
            "status": self._health_snapshot(),
            "agent": agent_health,
            "run": self.run_snapshot(),
        }

    # --- internals -----------------------------------------------------

    def _worker(
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
