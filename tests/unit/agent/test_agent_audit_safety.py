"""Phase 9: agent audit events carry ids/reasons only — never prompt content."""

from __future__ import annotations

from greatsage.agent.models import (
    AgentContext,
    AgentLimits,
    AgentRunStatus,
    AgentTask,
)
from greatsage.agent.orchestrator import AgentOrchestrator
from greatsage.events.models import AGENT_STARTED
from greatsage.intelligence.models import Message
from tests.unit.agent._fakes import FakeIntelligence, FakeTools, text_handler


def test_phase9_agent_started_carries_no_prompt_content() -> None:
    recorded: list[tuple[str, dict]] = []

    def publisher(event) -> None:  # noqa: ANN001
        recorded.append((event.type, event.payload))

    secret_prompt = "summarize; credential sk-live-abcdefghij123456 inside"
    task = AgentTask(prompt=secret_prompt)
    context = AgentContext(
        session_id=None,
        task_id=task.task_id,
        prompt=secret_prompt,
        conversation=(Message.user(secret_prompt),),
        system_note="You are Great Sage",
    )
    status = AgentRunStatus(task_id=task.task_id)
    intelligence = FakeIntelligence([text_handler("done")], keep_last=False)
    AgentOrchestrator(intelligence, FakeTools(), publisher=publisher).run(
        task, context, AgentLimits(), status
    )
    started = [p for t, p in recorded if t == AGENT_STARTED]
    assert len(started) == 1
    payload = started[0]
    assert "prompt" not in payload
    assert payload["prompt_chars"] == len(secret_prompt)
    assert "sk-live" not in str(payload)
