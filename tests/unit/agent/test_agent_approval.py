"""Agent approval provider: recorded decisions, bounded wait, cancel-aware (spec §9)."""

from __future__ import annotations

import threading
import time

from greatsage.agent.approval import AgentApprovalProvider
from greatsage.agent.cancellation import CancellationToken
from greatsage.tools.models import ApprovalOutcome, ToolRequest


def _request(request_id: str = "r1") -> ToolRequest:
    return ToolRequest(
        request_id=request_id, tool_id="filesystem.list", arguments={}, source="agent"
    )


def test_recorded_approve_is_used() -> None:
    provider = AgentApprovalProvider()
    provider.approve("r1")
    assert provider.request_approval(_request("r1"), None, "ask") is ApprovalOutcome.APPROVED


def test_recorded_reject_is_used() -> None:
    provider = AgentApprovalProvider()
    provider.reject("r1")
    assert provider.request_approval(_request("r1"), None, "ask") is ApprovalOutcome.DENIED


def test_waiting_decision_from_other_thread() -> None:
    provider = AgentApprovalProvider(wait_seconds=5.0)
    decided: list[str] = []

    def waiter() -> None:
        outcome = provider.request_approval(_request(), None, "ask")
        decided.append(outcome.value)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    provider.approve("r1")
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert decided == ["approved"]


def test_wait_expiry_denies_without_decision() -> None:
    provider = AgentApprovalProvider(wait_seconds=0.05)
    began = time.monotonic()
    outcome = provider.request_approval(_request(), None, "ask")
    assert outcome is ApprovalOutcome.DENIED
    assert time.monotonic() - began < 5.0


def test_cancelled_token_denies_immediately() -> None:
    token = CancellationToken()
    token.cancel()
    provider = AgentApprovalProvider(wait_seconds=30.0, cancel_token=token)
    outcome = provider.request_approval(_request(), None, "ask")
    assert outcome is ApprovalOutcome.DENIED


def test_not_cancelled_token_allows_wait() -> None:
    token = CancellationToken()
    provider = AgentApprovalProvider(wait_seconds=0.05, cancel_token=token)
    assert provider.request_approval(_request(), None, "ask") is ApprovalOutcome.DENIED


def test_pending_callback_invoked() -> None:
    seen: list[str] = []
    provider = AgentApprovalProvider(on_pending=lambda req: seen.append(req.request_id))
    provider.request_approval(_request("r9"), None, "ask")
    assert seen == ["r9"]


def test_decisions_are_single_use() -> None:
    provider = AgentApprovalProvider(wait_seconds=0.05)
    provider.approve("r1")
    assert provider.request_approval(_request("r1"), None, "ask") is ApprovalOutcome.APPROVED
    assert provider.request_approval(_request("r1"), None, "ask") is ApprovalOutcome.DENIED


def test_has_pending_reflects_activity() -> None:
    provider = AgentApprovalProvider(wait_seconds=0.02)
    assert not provider.has_pending()
    provider.approve("r2")
    assert provider.has_pending()  # recorded decision not yet consumed
    provider.request_approval(_request("r2"), None, "ask")
    assert not provider.has_pending()  # consumed → nothing outstanding
