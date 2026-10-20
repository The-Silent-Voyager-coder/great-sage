"""Approval broker tests: pending queue, decisions, timeout, redaction."""

from __future__ import annotations

import threading

from greatsage.agent.approval_broker import ApprovalBroker
from greatsage.tools.models import ApprovalOutcome, ToolRequest


def _request(request_id: str = "r1", arguments: dict | None = None) -> ToolRequest:
    return ToolRequest(
        request_id=request_id,
        tool_id="filesystem.write",
        arguments=arguments or {},
        source="agent",
        session_id="s1",
        task_id="t1",
    )


def test_broker_waits_and_approves() -> None:
    broker = ApprovalBroker(wait_seconds=5.0)
    outcomes: list[ApprovalOutcome] = []

    thread = threading.Thread(
        target=lambda: outcomes.append(
            broker.request_approval(_request(), None, "medium risk write")
        )
    )
    thread.start()
    pending = broker.wait_for_pending(timeout=2.0)
    assert pending is not None
    assert pending.request_id == "r1"
    assert pending.tool_id == "filesystem.write"
    assert pending.session_id == "s1"
    assert pending.task_id == "t1"
    assert broker.has_pending()
    assert broker.approve(pending.request_id) is True
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcomes == [ApprovalOutcome.APPROVED]
    assert broker.pending() == ()


def test_broker_rejects() -> None:
    broker = ApprovalBroker(wait_seconds=5.0)
    outcomes: list[ApprovalOutcome] = []
    thread = threading.Thread(
        target=lambda: outcomes.append(broker.request_approval(_request(), None, "ask"))
    )
    thread.start()
    pending = broker.wait_for_pending(timeout=2.0)
    assert pending is not None
    assert broker.reject(pending.request_id) is True
    thread.join(timeout=5.0)
    assert outcomes == [ApprovalOutcome.DENIED]


def test_broker_timeout_removes_pending() -> None:
    broker = ApprovalBroker(wait_seconds=0.05)
    outcome = broker.request_approval(_request(), None, "ask")
    assert outcome is ApprovalOutcome.DENIED
    assert broker.pending() == ()
    assert broker.wait_for_pending(timeout=0.01) is None


def test_broker_redacts_arguments_preview() -> None:
    broker = ApprovalBroker(wait_seconds=5.0)
    secret = "sk-" + "a" * 24
    thread = threading.Thread(
        target=lambda: broker.request_approval(
            _request(arguments={"token": secret, "path": "C:/tmp/x"}), None, "ask"
        )
    )
    thread.start()
    pending = broker.wait_for_pending(timeout=2.0)
    assert pending is not None
    assert secret not in pending.arguments_preview
    assert "C:/tmp/x" in pending.arguments_preview
    broker.reject(pending.request_id)
    thread.join(timeout=5.0)


def test_stale_decision_is_not_accepted() -> None:
    broker = ApprovalBroker(wait_seconds=0.05)
    assert broker.approve("missing") is False
    assert broker.reject("missing") is False


def test_reject_all_clears_queue() -> None:
    broker = ApprovalBroker(wait_seconds=5.0)
    outcomes: list[ApprovalOutcome] = []
    for request_id in ("r1", "r2"):
        threading.Thread(
            target=lambda rid=request_id: outcomes.append(
                broker.request_approval(_request(rid), None, "ask")
            )
        ).start()
    assert broker.wait_for_pending(timeout=2.0) is not None
    # Wait until both requests are registered (the worker threads race start).
    deadline = threading.Event()
    for _ in range(100):
        if len(broker.pending()) == 2:
            break
        deadline.wait(0.01)
    assert len(broker.pending()) == 2
    assert broker.reject_all() == 2
    for _ in range(100):
        if len(outcomes) == 2:
            break
        deadline.wait(0.01)
    assert outcomes == [ApprovalOutcome.DENIED, ApprovalOutcome.DENIED]
