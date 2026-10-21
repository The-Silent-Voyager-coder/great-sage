"""Approval policy for Telegram-triggered tool calls.

Telegram has no interactive approver at the keyboard, so its screenshot
command is covered by a deliberate standing opt-in (``listen --content``).
That opt-in is scoped to one tool and one source: it must never become a
global auto-approval switch for CLI or agent requests.
"""

from __future__ import annotations

from greatsage.tools.approval import ApprovalProvider
from greatsage.tools.models import ApprovalOutcome, Tool, ToolRequest


class TelegramScreenshotApprovalProvider:
    """Approve only Telegram screenshot requests; fall back for everything else."""

    def __init__(self, fallback: ApprovalProvider | None = None) -> None:
        self._fallback = fallback

    def request_approval(
        self, request: ToolRequest, tool: Tool, reason: str
    ) -> ApprovalOutcome:
        if request.source == "telegram" and tool.id == "gui.screenshot":
            return ApprovalOutcome.APPROVED
        if self._fallback is not None:
            return self._fallback.request_approval(request, tool, reason)
        return ApprovalOutcome.DENIED
