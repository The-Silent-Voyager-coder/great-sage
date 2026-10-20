"""Task approval scoping tests (task run --approve is not global)."""

from __future__ import annotations

from greatsage.task.service import _TaskScopedApprovalProvider
from greatsage.tools.approval import DeterministicApprovalProvider
from greatsage.tools.models import ApprovalOutcome, ToolRequest


def _request(task_id: str | None) -> ToolRequest:
    return ToolRequest(
        request_id="r1",
        tool_id="shell.execute",
        arguments={"command": ["python", "--version"]},
        source="task",
        task_id=task_id,
    )


def test_task_scope_approves_only_matching_task() -> None:
    provider = _TaskScopedApprovalProvider("t1")
    assert (
        provider.request_approval(_request("t1"), None, "high risk")  # type: ignore[arg-type]
        is ApprovalOutcome.APPROVED
    )
    assert (
        provider.request_approval(_request("t2"), None, "high risk")  # type: ignore[arg-type]
        is ApprovalOutcome.DENIED
    )


def test_task_scope_falls_back_for_other_requests() -> None:
    fallback = DeterministicApprovalProvider(ApprovalOutcome.APPROVED)
    provider = _TaskScopedApprovalProvider("t1", fallback)
    assert (
        provider.request_approval(_request("t2"), None, "high risk")  # type: ignore[arg-type]
        is ApprovalOutcome.APPROVED
    )
