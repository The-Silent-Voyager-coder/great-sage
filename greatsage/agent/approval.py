"""Approval provider for agent runs (spec §9).

When the security policy answers ASK, ToolService consults the installed
ApprovalProvider. The agent layer installs AgentApprovalProvider so that a
human (or future approval UI) can decide per request. Decisions can be
recorded before execution (deterministic tests) or while the orchestrator
waits; a pending request pauses the run in WAITING_FOR_APPROVAL and defaults
to DENIED after a bounded wait. Nothing is ever auto-approved.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from greatsage.agent.cancellation import CancellationToken
from greatsage.tools.approval import ApprovalProvider
from greatsage.tools.models import ApprovalOutcome, Tool, ToolRequest

log = logging.getLogger("greatsage.agent.approval")

PendingCallback = Callable[[ToolRequest], None]


class AgentApprovalProvider:
    """ApprovalProvider with per-request recorded decisions and a bounded wait."""

    def __init__(
        self,
        on_pending: PendingCallback | None = None,
        wait_seconds: float = 30.0,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self._on_pending = on_pending
        self._wait_seconds = max(0.0, wait_seconds)
        self._cancel_token = cancel_token
        self._decisions: dict[str, ApprovalOutcome] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def request_approval(self, request: ToolRequest, tool: Tool, reason: str) -> ApprovalOutcome:
        if self._on_pending is not None:
            try:
                self._on_pending(request)
            except Exception as exc:
                log.warning(
                    "agent approval pending callback failed: %s", exc, extra={"component": "agent"}
                )
        with self._lock:
            event = self._events.setdefault(request.request_id, threading.Event())
        deadline = time.monotonic() + self._wait_seconds
        try:
            while True:
                if self._cancel_token is not None and self._cancel_token.cancelled:
                    return ApprovalOutcome.DENIED
                with self._lock:
                    decision = self._decisions.get(request.request_id)
                if decision is not None:
                    return self._take_decision(request.request_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(
                        "agent approval wait expired for %s (denied)",
                        request.request_id,
                        extra={"component": "agent"},
                    )
                    return ApprovalOutcome.DENIED
                event.wait(timeout=min(0.05, remaining))
        finally:
            with self._lock:
                # This request is no longer waiting: drop its wait handle so
                # `has_pending` reflects only outstanding work.
                self._events.pop(request.request_id, None)

    def approve(self, request_id: str) -> None:
        self._decide(request_id, ApprovalOutcome.APPROVED)

    def reject(self, request_id: str) -> None:
        self._decide(request_id, ApprovalOutcome.DENIED)

    def has_pending(self) -> bool:
        """True while a request is waiting or a recorded decision is unconsumed."""
        with self._lock:
            return bool(self._decisions) or bool(self._events)

    def _decide(self, request_id: str, outcome: ApprovalOutcome) -> None:
        with self._lock:
            self._decisions[request_id] = outcome
            event = self._events.get(request_id)
        if event is not None:
            event.set()

    def _take_decision(self, request_id: str) -> ApprovalOutcome:
        with self._lock:
            return self._decisions.pop(request_id, ApprovalOutcome.DENIED)


class NotifyingApprovalProvider:
    """Wrap any approval provider and observe every pending request.

    ``AgentService`` uses this to keep its run status in
    ``WAITING_FOR_APPROVAL`` even when the caller supplies a custom provider
    (for example the CLI's broker or a UI-backed provider).
    """

    def __init__(
        self,
        inner: ApprovalProvider,
        on_pending: PendingCallback | None = None,
    ) -> None:
        self._inner = inner
        self._on_pending = on_pending

    def request_approval(
        self, request: ToolRequest, tool: Tool, reason: str
    ) -> ApprovalOutcome:
        if self._on_pending is not None:
            try:
                self._on_pending(request)
            except Exception as exc:
                log.warning(
                    "approval pending callback failed: %s",
                    exc,
                    extra={"component": "agent"},
                )
        return self._inner.request_approval(request, tool, reason)
