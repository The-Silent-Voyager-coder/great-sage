"""Deterministic conversational-intent guard: detection plus orchestrator wiring."""

from __future__ import annotations

import pytest

from greatsage.agent.intent import is_conversational
from greatsage.agent.models import (
    AgentContext,
    AgentLimits,
    AgentRunStatus,
    AgentState,
    AgentTask,
)
from greatsage.agent.orchestrator import AgentOrchestrator
from greatsage.intelligence.models import Message
from tests.unit.agent._fakes import (
    FakeIntelligence,
    FakeTools,
    text_handler,
    tool_handler,
)

CONVERSATIONAL = [
    "hello",
    "Hi!",
    "hey there",
    "good morning",
    "thanks",
    "thank you",
    "ok",
    "who are you?",
    "what's your name?",
    "what can you do?",
    "reply hello",
    "say hi",
    "respond with hello",
]

ACTION_PROMPTS = [
    "list files",
    "read notes.txt",
    "hello, list files",
    "who are you? open the file",
    "what's my cpu usage?",
    "write a file",
    "click the button",
    "type hello",
    "open hello.txt",
    "search for hello",
    "hello, can you read this?",
    "git status",
    "install a package",
    "hello " * 40,
    "",
    "tell me a joke",
]


@pytest.mark.parametrize("prompt", CONVERSATIONAL)
def test_conversational_prompts_are_detected(prompt: str) -> None:
    assert is_conversational(prompt) is True


@pytest.mark.parametrize("prompt", ACTION_PROMPTS)
def test_action_or_long_prompts_keep_tools(prompt: str) -> None:
    assert is_conversational(prompt) is False


def _run(
    intelligence: FakeIntelligence,
    tools: FakeTools,
    *,
    tools_enabled: bool,
) -> tuple[object, AgentRunStatus]:
    task = AgentTask(prompt="hello")
    context = AgentContext(
        session_id=None,
        task_id=task.task_id,
        prompt="hello",
        conversation=(Message.user("hello"),),
        system_note="You are Great Sage",
        tools_enabled=tools_enabled,
    )
    status = AgentRunStatus(task_id=task.task_id)
    result = AgentOrchestrator(intelligence, tools).run(
        task, context, AgentLimits(), status
    )
    return result, status


def test_conversational_context_hides_tool_catalog() -> None:
    intelligence = FakeIntelligence([text_handler("hello")], keep_last=False)
    result, status = _run(intelligence, FakeTools(), tools_enabled=False)
    request = intelligence.requests[0]
    assert request.tools is None
    assert "No tools are available" in (request.system_prompt or "")
    assert status.state is AgentState.COMPLETED
    assert result.final_text == "hello"  # type: ignore[attr-defined]


def test_conversational_context_ignores_stray_tool_calls() -> None:
    intelligence = FakeIntelligence(
        [tool_handler("gui.click", {"x": 1, "y": 2})], keep_last=False
    )
    tools = FakeTools()
    result, status = _run(intelligence, tools, tools_enabled=False)
    assert tools.calls == []
    assert status.state is AgentState.COMPLETED
    assert status.tool_calls == 0
    text = result.final_text or ""  # type: ignore[attr-defined]
    assert "tool call was not executed" in text
    assert result.steps[-1].note.startswith("conversational")  # type: ignore[attr-defined]
