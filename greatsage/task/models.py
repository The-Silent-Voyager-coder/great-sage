"""Task models (Phase 6)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from greatsage.exceptions import TaskStateError, TaskValidationError


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


_TERMINAL: frozenset[TaskState] = frozenset({
    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMED_OUT
})

_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.RUNNING}),
    TaskState.RUNNING: frozenset({TaskState.PAUSED, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMED_OUT}),  # noqa: E501
    TaskState.PAUSED: frozenset({TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED, TaskState.TIMED_OUT}),  # noqa: E501
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.TIMED_OUT: frozenset(),
}


def can_transition(cur: TaskState, nxt: TaskState) -> bool:
    return nxt in _TRANSITIONS[cur] or cur == nxt


def transition_state(cur: TaskState, nxt: TaskState) -> TaskState:
    if cur == nxt:
        return cur
    if not can_transition(cur, nxt):
        raise TaskStateError(f"invalid task transition: {cur.value} -> {nxt.value}")
    return nxt


@dataclass(frozen=True)
class StepResult:
    """Result of a single step execution."""

    step_id: str
    sequence: int
    tool_id: str
    success: bool
    output: Any | None = None
    error: str | None = None
    duration_ms: float = 0.0
    artifacts: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "sequence": self.sequence,
            "tool_id": self.tool_id,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "artifacts": self.artifacts,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class TaskReport:
    """Final report after plan completion."""

    task_id: str
    plan_id: str
    state: TaskState
    goal: str
    total_steps: int
    completed_steps: int
    failed_steps: int
    duration_ms: float
    step_results: tuple[StepResult, ...] = ()
    artifacts: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "state": self.state.value,
            "goal": self.goal,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "duration_ms": self.duration_ms,
            "step_results": [r.to_dict() for r in self.step_results],
            "artifacts": self.artifacts,
            "summary": self.summary,
        }


@dataclass
class TaskRecord:
    """Persisted task row (mutable for executor)."""

    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex}")
    plan_id: str = ""
    goal: str = ""
    state: TaskState = TaskState.PENDING
    current_step: int = 0
    total_steps: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id.strip():
            raise TaskValidationError("task id must not be empty")
        if not self.plan_id.strip():
            raise TaskValidationError("task plan_id must not be empty")

    def transition(self, target: TaskState) -> None:
        self.state = transition_state(self.state, target)
        self.updated_at = datetime.now(UTC)
        if self.state in _TERMINAL:
            self.completed_at = datetime.now(UTC)

    def reopen(self) -> None:
        """Prepare a failed/timed-out task for an explicit retry.

        This is deliberately not a normal state transition: resuming is a
        human-initiated new attempt, and completed/cancelled tasks stay
        terminal.
        """
        if self.state not in (TaskState.FAILED, TaskState.TIMED_OUT):
            raise TaskStateError(
                f"task {self.id} cannot be reopened from {self.state.value}"
            )
        self.state = TaskState.PENDING
        self.completed_at = None
        self.error = None
        self.updated_at = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "goal": self.goal,
            "state": self.state.value,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRecord:
        return cls(
            id=str(data["id"]),
            plan_id=str(data["plan_id"]),
            goal=str(data.get("goal", "")),
            state=TaskState(str(data.get("state", "pending"))),
            current_step=int(data.get("current_step", 0)),
            total_steps=int(data.get("total_steps", 0)),
            created_at=datetime.fromisoformat(str(data["created_at"])) if data.get("created_at") else datetime.now(UTC),  # noqa: E501
            updated_at=datetime.fromisoformat(str(data["updated_at"])) if data.get("updated_at") else datetime.now(UTC),  # noqa: E501
            completed_at=datetime.fromisoformat(str(data["completed_at"])) if data.get("completed_at") else None,  # noqa: E501
            error=data.get("error"),
            metadata=dict(data.get("metadata", {})),
        )
