"""Agent orchestrator: the bounded loop, limits, events, and safety (spec §7-§24)."""

from __future__ import annotations

import json

from greatsage.agent.approval import AgentApprovalProvider
from greatsage.agent.cancellation import CancellationToken
from greatsage.agent.models import (
    AgentContext,
    AgentLimits,
    AgentResult,
    AgentRunStatus,
    AgentState,
    AgentTask,
)
from greatsage.agent.orchestrator import AgentOrchestrator
from greatsage.events.models import (
    AGENT_COMPLETED,
    AGENT_FAILED,
    AGENT_LIMIT_REACHED,
    AGENT_STARTED,
    AGENT_STEP_COMPLETED,
    AGENT_STEP_STARTED,
    AGENT_TOOL_CALL_COMPLETED,
    AGENT_TOOL_CALL_REQUESTED,
)
from greatsage.exceptions import ProviderError
from greatsage.intelligence.models import Message
from tests.unit.agent._fakes import (
    FakeIntelligence,
    FakeTools,
    text_handler,
    tool_handler,
)


def _run(
    intelligence: FakeIntelligence,
    tools: FakeTools,
    *,
    limits: AgentLimits | None = None,
    token: CancellationToken | None = None,
    publisher=None,
) -> tuple[AgentResult, AgentRunStatus, ContextRecorder | None]:
    task = AgentTask(prompt="test task")
    context = AgentContext(
        session_id=None,
        task_id=task.task_id,
        prompt="test task",
        conversation=(Message.user("test task"),),
        system_note="You are J.A.R.V.I.S.",
    )
    status = AgentRunStatus(task_id=task.task_id)
    recorder = ContextRecorder(intelligence)
    result = AgentOrchestrator(
        intelligence, tools, publisher=publisher
    ).run(task, context, limits or AgentLimits(), status, token=token)
    return result, status, recorder


class ContextRecorder:
    """Records tool-schema exposure and message history per AI request."""

    def __init__(self, intelligence: FakeIntelligence) -> None:
        self.intelligence = intelligence

    def tool_names(self, request_index: int = 0) -> list[str]:
        tools = self.intelligence.requests[request_index].tools or []
        return [tool.name for tool in tools]

    def last_message_text(self, request_index: int = -1) -> str:
        message = self.intelligence.requests[request_index].messages[-1]
        return message.content if isinstance(message.content, str) else ""


def test_final_response_completes() -> None:
    intelligence = FakeIntelligence([text_handler("hello world")], keep_last=False)
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.final_text == "hello world"
    assert result.tool_calls == 0
    assert len(result.steps) == 1
    assert status.state is AgentState.COMPLETED
    assert status.steps == 1
    assert status.provider == "fake"
    assert status.model == "fake-model"
    assert status.request_id == intelligence.requests[0].request_id
    assert status.elapsed_ms >= 0
    assert tools.calls == []


def test_single_tool_call_then_final() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list"), text_handler("listed")], keep_last=False
    )
    tools = FakeTools(outputs={"filesystem.list": {"names": ["a.txt", "b.txt"]}})
    result, status, recorder = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.steps[-1].note == "final response (no tool calls)"
    assert result.tool_calls == 1
    assert len(result.steps) == 2
    assert len(tools.calls) == 1
    assert tools.calls[0].source == "agent"
    assert tools.calls[0].tool_id == "filesystem.list"
    assert tools.calls[0].arguments == {}
    assert '"names"' in recorder.last_message_text(-1)


def test_tool_schemas_exposed_to_ai() -> None:
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools()
    _run(intelligence, tools)
    assert intelligence.requests[0].tools is not None
    names = [tool.name for tool in intelligence.requests[0].tools or []]
    assert "filesystem.list" in names
    assert all(tool.description for tool in intelligence.requests[0].tools or [])


def test_multiple_tool_calls_in_one_response_execute_in_order() -> None:
    intelligence = FakeIntelligence(
        [tool_handler(count=2), text_handler("done")], keep_last=False
    )
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools, limits=AgentLimits(max_single_tool_calls=2))
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 2
    assert [call.tool_id for call in tools.calls] == ["filesystem.list"] * 2
    assert len(result.steps) == 2
    assert status.steps == 2


def test_tool_failure_is_returned_to_ai_once() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list"), text_handler("recovered")], keep_last=False
    )
    tools = FakeTools()
    tools.mark_failure("filesystem.list", error="boom")
    result, _, recorder = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1
    assert "error: boom" in recorder.last_message_text(-1)


def test_unknown_tool_failure_fed_to_ai() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("no.such.tool"), text_handler("done")], keep_last=False
    )
    tools = FakeTools()
    tools.mark_unknown("no.such.tool")
    result, _, recorder = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1
    assert "unknown tool" in recorder.last_message_text(-1)


def test_invalid_arguments_failure_fed_to_ai() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list"), text_handler("done")], keep_last=False
    )
    tools = FakeTools()
    tools.mark_invalid("filesystem.list")
    result, _, recorder = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1
    assert "invalid arguments" in recorder.last_message_text(-1)


def test_security_denial_fed_to_ai() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list"), text_handler("done")], keep_last=False
    )
    tools = FakeTools(approval=None)
    tools.mark_requires_approval("filesystem.list")
    result, _, recorder = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1
    assert "denied" in recorder.last_message_text(-1)


def test_approval_pause_approve_and_executes() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list"), text_handler("done")], keep_last=False
    )
    tools = FakeTools(approval=AgentApprovalProvider())
    tools.mark_requires_approval("filesystem.list")
    tools.approval.approve("call_1")
    result, status, _ = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1
    assert len(tools.calls) == 1
    assert status.state is AgentState.COMPLETED


def test_approval_rejection_fed_to_ai() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list"), text_handler("done")], keep_last=False
    )
    tools = FakeTools(approval=AgentApprovalProvider())
    tools.mark_requires_approval("filesystem.list")
    tools.approval.reject("call_1")
    result, _, recorder = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1
    assert "approval denied" in recorder.last_message_text(-1)


def test_approval_wait_expiry_denies_gracefully() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list"), text_handler("done")], keep_last=False
    )
    tools = FakeTools(approval=AgentApprovalProvider(wait_seconds=0.05))
    tools.mark_requires_approval("filesystem.list")
    result, _, recorder = _run(intelligence, tools)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1
    assert "approval denied" in recorder.last_message_text(-1)


def test_provider_error_fails_run() -> None:
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=False)
    intelligence.raise_exc = ProviderError("forced provider error")
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools)
    assert result.state is AgentState.FAILED
    assert "forced provider error" in (result.error or "")
    assert status.state is AgentState.FAILED


def test_unexpected_exception_fails_run_safely() -> None:
    intelligence = FakeIntelligence([text_handler("x")], keep_last=False)
    intelligence.raise_exc = RuntimeError("boom")
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools)
    assert result.state is AgentState.FAILED
    assert "unexpected agent failure" in (result.error or "")
    assert status.reason == "failure"


def test_tool_crash_fails_run() -> None:
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools()
    tools.mark_crash("filesystem.list")
    result, _, _ = _run(intelligence, tools)
    assert result.state is AgentState.FAILED
    assert "crashed" in (result.error or "")


def test_validation_error_fails_run() -> None:
    intelligence = FakeIntelligence([tool_handler("", allow_empty=True)], keep_last=False)
    tools = FakeTools()
    result, _, _ = _run(intelligence, tools)
    assert result.state is AgentState.FAILED
    assert result.reason == "validation"


def test_cancelled_before_start() -> None:
    token = CancellationToken()
    token.cancel()
    intelligence = FakeIntelligence([text_handler("x")], keep_last=False)
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools, token=token)
    assert result.state is AgentState.CANCELLED
    assert result.reason == "cancelled"
    assert status.state is AgentState.CANCELLED
    assert tools.calls == []


def test_cancelled_during_tool_loop() -> None:
    token = CancellationToken()
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools()

    def cancel_next(request):  # noqa: ANN001
        response = tool_handler("filesystem.list")(request)
        token.cancel()
        return response

    intelligence._queue = type(intelligence._queue)([cancel_next])
    result, status, _ = _run(intelligence, tools, token=token)
    assert result.state is AgentState.CANCELLED
    assert status.reason == "cancelled"


def test_wall_clock_timeout() -> None:
    # Distinct calls per step so loop detection can never fire first: the
    # only possible outcome on any machine speed is the wall clock.
    import time

    def timing_handler(index: int):
        base = tool_handler("filesystem.list", {"path": f"/wall-clock-{index}"})

        def handle(request):
            time.sleep(0.002)
            return base(request)

        return handle

    handlers = [timing_handler(index) for index in range(20)]
    intelligence = FakeIntelligence(handlers, keep_last=False)
    tools = FakeTools()
    result, status, _ = _run(
        intelligence, tools, limits=AgentLimits(max_wall_time_seconds=0.0001)
    )
    assert result.state is AgentState.TIMED_OUT
    assert result.reason == "wall_clock"
    assert status.state is AgentState.TIMED_OUT


def test_max_steps_limit() -> None:
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools, limits=AgentLimits(max_steps=2))
    assert result.state is AgentState.LIMIT_REACHED
    assert result.reason == "max_steps"
    assert result.steps
    assert status.steps == len(result.steps) and result.tool_calls >= 1


def test_max_tool_calls_limit() -> None:
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools()
    result, _, _ = _run(intelligence, tools, limits=AgentLimits(max_tool_calls=1))
    assert result.state is AgentState.LIMIT_REACHED
    assert result.reason == "max_tool_calls"
    assert result.tool_calls == 1


def test_max_single_tool_calls_limit() -> None:
    intelligence = FakeIntelligence([tool_handler(count=2)], keep_last=False)
    tools = FakeTools()
    result, _, _ = _run(intelligence, tools, limits=AgentLimits(max_single_tool_calls=1))
    assert result.state is AgentState.LIMIT_REACHED
    assert result.reason == "single_tool_calls"
    assert result.tool_calls == 0


def test_output_bytes_limit() -> None:
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools(outputs={"filesystem.list": {"data": "x" * 5000}})
    result, _, _ = _run(
        intelligence, tools, limits=AgentLimits(max_total_tool_output_bytes=100)
    )
    assert result.state is AgentState.LIMIT_REACHED
    assert result.reason == "output_bytes"


def test_loop_detection_limit() -> None:
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools()
    result, _, _ = _run(
        intelligence, tools, limits=AgentLimits(loop_detection_threshold=2)
    )
    assert result.state is AgentState.LIMIT_REACHED
    assert result.reason == "loop_detection"


def test_publisher_failure_does_not_break_run() -> None:
    def broken_publisher(_event):  # noqa: ANN001
        raise RuntimeError("bus is down")

    intelligence = FakeIntelligence([text_handler("done")], keep_last=False)
    tools = FakeTools()
    result, _, _ = _run(intelligence, tools, publisher=broken_publisher)
    assert result.state is AgentState.COMPLETED


def test_events_ordered_and_serializable() -> None:
    recorded: list[tuple[str, dict]] = []
    events = [AGENT_STARTED, AGENT_STEP_STARTED, AGENT_TOOL_CALL_REQUESTED,
              AGENT_TOOL_CALL_COMPLETED, AGENT_STEP_COMPLETED, AGENT_STEP_STARTED,
              AGENT_STEP_COMPLETED, AGENT_COMPLETED]

    def publisher(event):  # noqa: ANN001
        recorded.append((event.type, event.payload))
        json.dumps(event.payload, default=str)

    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list"), text_handler("done")], keep_last=False
    )
    tools = FakeTools()
    _run(intelligence, tools, publisher=publisher)
    assert [t for t, _ in recorded] == events
    assert recorded[0][1]["task_id"]
    assert recorded[1][1]["step"] == 1


def test_terminal_event_payload_shape() -> None:
    recorded: list[str] = []

    def publisher(event):  # noqa: ANN001
        recorded.append(event.type)
        json.dumps(event.payload, default=str)

    intelligence = FakeIntelligence([text_handler("bye")], keep_last=False)
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools, publisher=publisher)
    assert result.state is AgentState.COMPLETED
    assert recorded[-1] == AGENT_COMPLETED
    assert status.reason is None


def test_agent_limit_reached_event_payload_has_limit() -> None:
    recorded: list[tuple[str, dict]] = []

    def publisher(event):  # noqa: ANN001
        recorded.append((event.type, event.payload))

    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools()
    _run(
        intelligence,
        tools,
        limits=AgentLimits(loop_detection_threshold=2),
        publisher=publisher,
    )
    terminal = [p for t, p in recorded if t == AGENT_LIMIT_REACHED]
    assert terminal and terminal[0]["limit"] == "loop_detection"


def test_agent_failed_event_payload_has_error() -> None:
    recorded: list[tuple[str, dict]] = []

    def publisher(event):  # noqa: ANN001
        recorded.append((event.type, event.payload))

    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=False)
    intelligence.raise_exc = RuntimeError("boom")
    tools = FakeTools()
    _run(intelligence, tools, publisher=publisher)
    failed = [p for t, p in recorded if t == AGENT_FAILED]
    assert failed and "error" in failed[0]


def test_tool_calls_always_flow_through_execute_path() -> None:
    intelligence = FakeIntelligence([tool_handler("filesystem.list")], keep_last=True)
    tools = FakeTools()
    _run(intelligence, tools, limits=AgentLimits(max_steps=2))
    assert tools.calls
    assert all(call.source == "agent" for call in tools.calls)


def test_never_leaves_running_when_completed() -> None:
    intelligence = FakeIntelligence([text_handler("done")], keep_last=False)
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools)
    assert result.state is not AgentState.RUNNING
    assert status.state.is_terminal


def test_status_observability_tracked() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("filesystem.list", count=2), text_handler("done")], keep_last=False
    )
    tools = FakeTools()
    result, status, _ = _run(intelligence, tools, limits=AgentLimits(max_single_tool_calls=2))
    assert status.tool_calls == 2
    assert status.steps == 2
    assert status.model == "fake-model"
    assert result.provider == "fake"
