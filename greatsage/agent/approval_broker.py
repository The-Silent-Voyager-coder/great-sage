"""Approval broker for human-in-the-loop tool decisions.

The broker owns the pending-approval queue and delegates the actual wait to
``AgentApprovalProvider``. It is deliberately transport-neutral: the CLI
prompts on stdin today, and a future local UI can poll ``pending()`` and call
``approve()``/``reject()`` without changing the agent or tool layers.

Security properties:

- A decision is accepted only for a request that is currently pending.
- Pending arguments are redacted and bounded before they are exposed to a
  presenter; the raw request is never retained by the broker.
- A request that times out is removed from the queue and denies by default.
- The broker never approves anything itself.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from greatsage.agent.approval import AgentApprovalProvider
from greatsage.agent.cancellation import CancellationToken
from greatsage.tools.models import ApprovalOutcome, Tool, ToolRequest
from greatsage.tools.redaction import redact_secrets

_ARGUMENTS_PREVIEW_LIMIT = 2000


@dataclass(frozen=True)
class PendingApproval:
    """One request waiting for a human decision (safe for display)."""

    request_id: str
    tool_id: str
    tool_name: str
    risk_level: str
    reason: str
    arguments_preview: str
    task_id: str | None = None
    session_id: str | None = None
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "arguments_preview": self.arguments_preview,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
        }


class ApprovalBroker:
    """Thread-safe pending queue + bounded decision wait for one or more runs."""

    def __init__(
        self,
        *,
        wait_seconds: float = 300.0,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self._provider = AgentApprovalProvider(
            wait_seconds=wait_seconds,
            cancel_token=cancel_token,
        )
        self._pending: dict[str, PendingApproval] = {}
        self._condition = threading.Condition()

    def request_approval(
        self, request: ToolRequest, tool: Tool | None, reason: str
    ) -> ApprovalOutcome:
        pending = _pending_from_request(request, tool, reason)
        with self._condition:
            self._pending[request.request_id] = pending
            self._condition.notify_all()
        try:
            return self._provider.request_approval(request, tool, reason)  # type: ignore[arg-type]
        finally:
            with self._condition:
                self._pending.pop(request.request_id, None)
                self._condition.notify_all()

    def pending(self) -> tuple[PendingApproval, ...]:
        """Snapshot outstanding requests, oldest first."""
        with self._condition:
            return tuple(
                sorted(self._pending.values(), key=lambda item: item.created_at)
            )

    def has_pending(self) -> bool:
        with self._condition:
            return bool(self._pending)

    def wait_for_pending(self, timeout: float | None = None) -> PendingApproval | None:
        """Wait for the next pending request, or return ``None`` on timeout."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._pending:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)
            return min(self._pending.values(), key=lambda item: item.created_at)

    def approve(self, request_id: str) -> bool:
        """Approve one pending request; false when it is no longer pending."""
        with self._condition:
            if request_id not in self._pending:
                return False
            self._provider.approve(request_id)
            return True

    def reject(self, request_id: str) -> bool:
        """Reject one pending request; false when it is no longer pending."""
        with self._condition:
            if request_id not in self._pending:
                return False
            self._provider.reject(request_id)
            return True

    def reject_all(self) -> int:
        """Reject every outstanding request (used on CLI cancellation)."""
        with self._condition:
            request_ids = tuple(self._pending)
            for request_id in request_ids:
                self._provider.reject(request_id)
            return len(request_ids)


def _pending_from_request(
    request: ToolRequest, tool: Tool | None, reason: str
) -> PendingApproval:
    risk = getattr(tool, "risk_level", "unknown")
    risk_level = str(getattr(risk, "value", risk))
    return PendingApproval(
        request_id=request.request_id,
        tool_id=request.tool_id,
        tool_name=str(getattr(tool, "name", request.tool_id)),
        risk_level=risk_level,
        reason=str(reason or ""),
        arguments_preview=_arguments_preview(request.arguments),
        task_id=request.task_id,
        session_id=request.session_id,
        created_at=time.time(),
    )


def _arguments_preview(arguments: dict[str, Any]) -> str:
    try:
        raw = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(arguments)
    raw = redact_secrets(raw) or ""
    if len(raw) > _ARGUMENTS_PREVIEW_LIMIT:
        return raw[:_ARGUMENTS_PREVIEW_LIMIT] + "... [truncated]"
    return raw
