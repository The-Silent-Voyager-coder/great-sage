"""Provider-neutral agent models (Phase 5A spec §2-§6).

Nothing here is OpenCode-specific. AgentTask, AgentStep, ToolCall,
ToolCallResult, AgentResult, AgentState, AgentLimits, and AgentContext form
the vocabulary of the bounded AI tool-calling loop; the orchestrator is the
only place that transitions AgentState values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from greatsage.agent.limits import (
    AGENT_APPROVAL_WAIT_SECONDS_CEILING,
    AGENT_APPROVAL_WAIT_SECONDS_DEFAULT,
    LOOP_DETECTION_THRESHOLD_CEILING,
    LOOP_DETECTION_THRESHOLD_DEFAULT,
    MAX_SINGLE_TOOL_CALLS_CEILING,
    MAX_SINGLE_TOOL_CALLS_DEFAULT,
    MAX_STEPS_CEILING,
    MAX_STEPS_DEFAULT,
    MAX_TOOL_CALLS_CEILING,
    MAX_TOOL_CALLS_DEFAULT,
    MAX_TOTAL_TOOL_OUTPUT_BYTES_CEILING,
    MAX_TOTAL_TOOL_OUTPUT_BYTES_DEFAULT,
    MAX_WALL_TIME_SECONDS_CEILING,
    MAX_WALL_TIME_SECONDS_DEFAULT,
    check_bounded,
)
from greatsage.exceptions import AgentStateError, AgentValidationError
from greatsage.intelligence.models import Message


class AgentState(StrEnum):
    """Valid agent task states (spec §3). Transitions are validated."""

    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    EXECUTING_TOOL = "executing_tool"
    PROCESSING_RESULT = "processing_result"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LIMIT_REACHED = "limit_reached"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @classmethod
    def can_transition(cls, current: AgentState, target: AgentState) -> bool:
        return target in _TRANSITIONS[current]


_TERMINAL_STATES: frozenset[AgentState] = frozenset(
    {
        AgentState.COMPLETED,
        AgentState.FAILED,
        AgentState.CANCELLED,
        AgentState.TIMED_OUT,
        AgentState.LIMIT_REACHED,
    }
)

_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.CREATED: frozenset({AgentState.RUNNING}),
    AgentState.RUNNING: frozenset(
        {
            AgentState.WAITING_FOR_APPROVAL,
            AgentState.EXECUTING_TOOL,
            AgentState.PROCESSING_RESULT,
            AgentState.COMPLETED,
            AgentState.FAILED,
            AgentState.CANCELLED,
            AgentState.TIMED_OUT,
            AgentState.LIMIT_REACHED,
        }
    ),
    AgentState.WAITING_FOR_APPROVAL: frozenset(
        {
            AgentState.RUNNING,
            AgentState.EXECUTING_TOOL,
            AgentState.PROCESSING_RESULT,
            AgentState.FAILED,
            AgentState.CANCELLED,
            AgentState.TIMED_OUT,
            AgentState.LIMIT_REACHED,
        }
    ),
    AgentState.EXECUTING_TOOL: frozenset(
        {
            AgentState.WAITING_FOR_APPROVAL,
            AgentState.PROCESSING_RESULT,
            AgentState.FAILED,
            AgentState.CANCELLED,
            AgentState.TIMED_OUT,
            AgentState.LIMIT_REACHED,
        }
    ),
    AgentState.PROCESSING_RESULT: frozenset(
        {
            AgentState.RUNNING,
            AgentState.WAITING_FOR_APPROVAL,
            AgentState.COMPLETED,
            AgentState.FAILED,
            AgentState.CANCELLED,
            AgentState.TIMED_OUT,
            AgentState.LIMIT_REACHED,
        }
    ),
    AgentState.COMPLETED: frozenset(),
    AgentState.FAILED: frozenset(),
    AgentState.CANCELLED: frozenset(),
    AgentState.TIMED_OUT: frozenset(),
    AgentState.LIMIT_REACHED: frozenset(),
}


def transition_state(current: AgentState, target: AgentState) -> AgentState:
    """Validate and perform a state transition; raise AgentStateError otherwise."""
    if target == current:
        return current
    if not AgentState.can_transition(current, target):
        raise AgentStateError(
            f"invalid agent state transition: {current.value} -> {target.value}"
        )
    return target


@dataclass(frozen=True)
class ToolCall:
    """A single tool call requested by the AI (spec §4).

    Arguments are always structured data, never an opaque shell command
    string. sequence is the 1-based position of the call within the run.
    """

    id: str
    tool_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    sequence: int = 1
    task_id: str | None = None
    session_id: str | None = None

    def validate(self) -> None:
        if not self.id:
            raise AgentValidationError("tool call requires an id")
        if not self.tool_id:
            raise AgentValidationError("tool call requires a tool_id")
        if not isinstance(self.arguments, dict):
            raise AgentValidationError("tool call arguments must be a mapping")
        if self.sequence < 1:
            raise AgentValidationError(f"tool call sequence must be >= 1, got {self.sequence}")


@dataclass(frozen=True)
class ToolCallResult:
    """Outcome of one tool call (spec §6).

    Carries request_id, tool_id, success, output, error, and duration.
    Never exposes internal security details: errors are already-scrubbed
    messages produced by the tool security pipeline.
    """

    request_id: str
    tool_id: str
    success: bool
    output: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0
    sequence: int = 0


@dataclass(frozen=True)
class AgentStep:
    """One AI turn of the loop: the request, its tool calls, and outcomes."""

    sequence: int
    state: AgentState
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float = 0.0
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_results: tuple[ToolCallResult, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class AgentLimits:
    """Effective run limits (spec §10-12). Validated against ceilings."""

    max_steps: int = MAX_STEPS_DEFAULT
    max_tool_calls: int = MAX_TOOL_CALLS_DEFAULT
    max_wall_time_seconds: float = MAX_WALL_TIME_SECONDS_DEFAULT
    max_single_tool_calls: int = MAX_SINGLE_TOOL_CALLS_DEFAULT
    max_total_tool_output_bytes: int = MAX_TOTAL_TOOL_OUTPUT_BYTES_DEFAULT
    loop_detection_threshold: int = LOOP_DETECTION_THRESHOLD_DEFAULT
    approval_wait_seconds: float = AGENT_APPROVAL_WAIT_SECONDS_DEFAULT

    def validate(self) -> None:
        try:
            check_bounded("agent.max_steps", self.max_steps, MAX_STEPS_CEILING)
            check_bounded("agent.max_tool_calls", self.max_tool_calls, MAX_TOOL_CALLS_CEILING)
            check_bounded(
                "agent.max_wall_time_seconds",
                self.max_wall_time_seconds,
                MAX_WALL_TIME_SECONDS_CEILING,
            )
            check_bounded(
                "agent.max_single_tool_calls",
                self.max_single_tool_calls,
                MAX_SINGLE_TOOL_CALLS_CEILING,
            )
            check_bounded(
                "agent.max_total_tool_output_bytes",
                self.max_total_tool_output_bytes,
                MAX_TOTAL_TOOL_OUTPUT_BYTES_CEILING,
            )
            check_bounded(
                "agent.loop_detection_threshold",
                self.loop_detection_threshold,
                LOOP_DETECTION_THRESHOLD_CEILING,
            )
            check_bounded(
                "agent.approval_wait_seconds",
                self.approval_wait_seconds,
                AGENT_APPROVAL_WAIT_SECONDS_CEILING,
            )
        except ValueError as exc:
            raise AgentValidationError(str(exc)) from exc
        if self.max_steps < 1:
            raise AgentValidationError("max_steps must be >= 1")
        if self.max_tool_calls < 1:
            raise AgentValidationError("max_tool_calls must be >= 1")
        if self.max_wall_time_seconds <= 0:
            raise AgentValidationError("max_wall_time_seconds must be positive")
        if self.max_single_tool_calls < 1:
            raise AgentValidationError("max_single_tool_calls must be >= 1")
        if self.max_total_tool_output_bytes < 1:
            raise AgentValidationError("max_total_tool_output_bytes must be >= 1")
        if self.loop_detection_threshold < 2:
            raise AgentValidationError("loop_detection_threshold must be >= 2")


@dataclass(frozen=True)
class AgentTask:
    """The unit of work an agent run performs."""

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str | None = None
    prompt: str = ""
    provider: str | None = None
    model: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    state: AgentState = AgentState.CREATED


@dataclass(frozen=True)
class AgentContext:
    """Context handed to the orchestrator (spec §15).

    Conversation, tool history, and memory references are explicit; the
    memory database is never dumped into context — retrieval happens only
    through MemoryService, and nothing is auto-saved as long-term memory.
    """

    session_id: str | None
    task_id: str
    prompt: str
    conversation: tuple[Message, ...] = ()
    tool_history: tuple[ToolCallResult, ...] = ()
    memory_references: tuple[dict[str, Any], ...] = ()
    system_note: str | None = None


@dataclass
class AgentRunStatus:
    """Mutable, observable status of a running agent task (spec §27).

    The orchestrator owns updates; AgentService.health() reads it for
    observability. Exposes state, step/tool counters, elapsed time, provider,
    model, and the reason/error of a terminal state. No secrets.
    """

    task_id: str
    session_id: str | None = None
    state: AgentState = AgentState.CREATED
    steps: int = 0
    tool_calls: int = 0
    failed_tool_calls: int = 0
    elapsed_ms: float = 0.0
    provider: str | None = None
    model: str | None = None
    request_id: str | None = None
    reason: str | None = None
    error: str | None = None

    def transition(self, target: AgentState) -> None:
        self.state = transition_state(self.state, target)


@dataclass(frozen=True)
class AgentResult:
    """Final outcome of an agent run (spec §2, §27)."""

    task_id: str
    session_id: str | None
    state: AgentState
    steps: tuple[AgentStep, ...] = ()
    tool_calls: int = 0
    failed_tool_calls: int = 0
    tool_output_bytes: int = 0
    duration_ms: float = 0.0
    final_text: str | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    reason: str | None = None
    memory_references: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "state": self.state.value,
            "steps": len(self.steps),
            "tool_calls": self.tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "tool_output_bytes": self.tool_output_bytes,
            "duration_ms": self.duration_ms,
            "final_text": self.final_text,
            "provider": self.provider,
            "model": self.model,
            "error": self.error,
            "reason": self.reason,
            "memory_references": self.memory_references,
        }
