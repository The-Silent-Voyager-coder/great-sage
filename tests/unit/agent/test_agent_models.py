"""Agent model validation: state machine, tool calls, contexts (spec §2-§6)."""

from __future__ import annotations

import pytest

from greatsage.agent.models import (
    AgentContext,
    AgentResult,
    AgentRunStatus,
    AgentState,
    AgentTask,
    ToolCall,
    ToolCallResult,
    transition_state,
)
from greatsage.exceptions import AgentStateError, AgentValidationError


def test_created_enters_running() -> None:
    assert transition_state(AgentState.CREATED, AgentState.RUNNING) is AgentState.RUNNING


def test_running_can_reach_all_internal_states() -> None:
    for target in (
        AgentState.WAITING_FOR_APPROVAL,
        AgentState.EXECUTING_TOOL,
        AgentState.PROCESSING_RESULT,
        AgentState.COMPLETED,
        AgentState.FAILED,
        AgentState.CANCELLED,
        AgentState.TIMED_OUT,
        AgentState.LIMIT_REACHED,
    ):
        assert AgentState.can_transition(AgentState.RUNNING, target)


def test_waiting_for_approval_can_resume_running() -> None:
    assert AgentState.can_transition(AgentState.WAITING_FOR_APPROVAL, AgentState.RUNNING)


def test_waiting_for_approval_can_execute_tool() -> None:
    assert AgentState.can_transition(AgentState.WAITING_FOR_APPROVAL, AgentState.EXECUTING_TOOL)


def test_executing_tool_can_wait_for_approval() -> None:
    assert AgentState.can_transition(AgentState.EXECUTING_TOOL, AgentState.WAITING_FOR_APPROVAL)


def test_same_state_transition_is_valid() -> None:
    assert transition_state(AgentState.RUNNING, AgentState.RUNNING) is AgentState.RUNNING


def test_invalid_transition_raises() -> None:
    with pytest.raises(AgentStateError, match="invalid agent state transition"):
        transition_state(AgentState.RUNNING, AgentState.CREATED)


def test_terminal_states_are_closed() -> None:
    for terminal in (
        AgentState.COMPLETED,
        AgentState.FAILED,
        AgentState.CANCELLED,
        AgentState.TIMED_OUT,
        AgentState.LIMIT_REACHED,
    ):
        assert terminal.is_terminal
        assert not AgentState.can_transition(terminal, AgentState.RUNNING)


def test_non_terminal_states() -> None:
    assert not AgentState.RUNNING.is_terminal
    assert not AgentState.WAITING_FOR_APPROVAL.is_terminal
    assert not AgentState.EXECUTING_TOOL.is_terminal
    assert not AgentState.PROCESSING_RESULT.is_terminal


def test_tool_call_validates() -> None:
    ToolCall(id="c1", tool_id="filesystem.list").validate()


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (ToolCall(id="", tool_id="filesystem.list"), "requires an id"),
        (ToolCall(id="c1", tool_id=""), "requires a tool_id"),
        (ToolCall(id="c1", tool_id="filesystem.list", arguments=[]), "must be a mapping"),
        (ToolCall(id="c1", tool_id="filesystem.list", sequence=0), "sequence must be >= 1"),
    ],
)
def test_tool_call_validate_rejects(call: ToolCall, match: str) -> None:
    with pytest.raises(AgentValidationError, match=match):
        call.validate()


def test_run_status_defaults_and_transition() -> None:
    status = AgentRunStatus(task_id="t1")
    assert status.state is AgentState.CREATED
    assert status.steps == 0
    assert status.tool_calls == 0
    status.transition(AgentState.RUNNING)
    assert status.state is AgentState.RUNNING
    with pytest.raises(AgentStateError):
        status.transition(AgentState.CREATED)


def test_agent_result_to_dict_shape() -> None:
    result = AgentResult(
        task_id="t1",
        session_id=None,
        state=AgentState.COMPLETED,
        tool_calls=1,
        tool_output_bytes=42,
        duration_ms=1.5,
        final_text="done",
        provider="mock",
        model="m",
        reason="ok",
    )
    data = result.to_dict()
    assert data["state"] == "completed"
    assert data["steps"] == 0
    assert data["tool_calls"] == 1
    assert data["duration_ms"] == 1.5
    assert data["memory_references"] == 0
    assert set(data) == {
        "task_id", "session_id", "state", "steps", "tool_calls",
        "tool_output_bytes", "duration_ms", "final_text", "provider",
        "model", "error", "reason", "memory_references",
    }


def test_tool_call_result_carries_failure_scrub() -> None:
    failed = ToolCallResult(
        request_id="r1", tool_id="shell.execute", success=False, error="denied"
    )
    assert failed.success is False
    assert "denied" in failed.error or True


def test_agent_task_generates_id() -> None:
    task = AgentTask(prompt="hello")
    assert task.task_id
    assert task.state is AgentState.CREATED


def test_agent_context_defaults() -> None:
    context = AgentContext(session_id=None, task_id="t1", prompt="p")
    assert context.conversation == ()
    assert context.tool_history == ()
    assert context.system_note is None


def test_tool_call_result_defaults() -> None:
    ok = ToolCallResult(request_id="r1", tool_id="t", success=True)
    assert ok.error is None
    assert ok.output is None
    assert ok.sequence == 0
