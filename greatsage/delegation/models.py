"""Provider-neutral delegation models (Phase 5B spec §4-§21).

Nothing here is OpenCode-specific: DelegationRequest, DelegationTask,
DelegationResult, DelegationState, DelegationLimits, DelegationEvent, and
DelegationPermission form the vocabulary of controlled delegation. The
DelegationManager is the only place that transitions DelegationState values;
OpenCode-specific wire formats stay inside the provider adapter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any

from greatsage.delegation.limits import (
    MAX_DELEGATION_DEPTH_CEILING,
    MAX_DELEGATION_DEPTH_DEFAULT,
    MAX_OUTPUT_BYTES_CEILING,
    MAX_OUTPUT_BYTES_DEFAULT,
    MAX_PERMISSION_REQUESTS_CEILING,
    MAX_PERMISSION_REQUESTS_DEFAULT,
    MAX_SESSION_COUNT_CEILING,
    MAX_SESSION_COUNT_DEFAULT,
    MAX_WALL_TIME_SECONDS_CEILING,
    MAX_WALL_TIME_SECONDS_DEFAULT,
    check_bounded,
)
from greatsage.exceptions import DelegationStateError, DelegationValidationError


class DelegationState(StrEnum):
    """Valid delegation task states (spec §5). Transitions are validated."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @classmethod
    def can_transition(cls, current: DelegationState, target: DelegationState) -> bool:
        return target in _TRANSITIONS[current]


_TERMINAL_STATES: frozenset[DelegationState] = frozenset(
    {
        DelegationState.COMPLETED,
        DelegationState.FAILED,
        DelegationState.CANCELLED,
        DelegationState.TIMED_OUT,
    }
)

_TRANSITIONS: dict[DelegationState, frozenset[DelegationState]] = {
    DelegationState.CREATED: frozenset({DelegationState.STARTING}),
    DelegationState.STARTING: frozenset(
        {
            DelegationState.RUNNING,
            DelegationState.COMPLETED,
            DelegationState.FAILED,
            DelegationState.CANCELLED,
            DelegationState.TIMED_OUT,
        }
    ),
    DelegationState.RUNNING: frozenset(
        {
            DelegationState.WAITING_FOR_PERMISSION,
            DelegationState.COMPLETING,
            DelegationState.COMPLETED,
            DelegationState.FAILED,
            DelegationState.CANCELLED,
            DelegationState.TIMED_OUT,
        }
    ),
    DelegationState.WAITING_FOR_PERMISSION: frozenset(
        {
            DelegationState.RUNNING,
            DelegationState.FAILED,
            DelegationState.CANCELLED,
            DelegationState.TIMED_OUT,
        }
    ),
    DelegationState.COMPLETING: frozenset(
        {
            DelegationState.COMPLETED,
            DelegationState.FAILED,
            DelegationState.CANCELLED,
            DelegationState.TIMED_OUT,
        }
    ),
    DelegationState.COMPLETED: frozenset(),
    DelegationState.FAILED: frozenset(),
    DelegationState.CANCELLED: frozenset(),
    DelegationState.TIMED_OUT: frozenset(),
}


def transition_state(current: DelegationState, target: DelegationState) -> DelegationState:
    """Validate and perform a state transition; raise otherwise."""
    if target == current:
        return current
    if not DelegationState.can_transition(current, target):
        raise DelegationStateError(
            f"invalid delegation state transition: {current.value} -> {target.value}"
        )
    return target


class DelegationEventKind(StrEnum):
    """Provider-neutral event kinds emitted by a delegation-capable adapter."""

    PERMISSION_REQUESTED = "permission_requested"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOTE = "note"


@dataclass(frozen=True)
class DelegationEvent:
    """A single normalized event from the delegated executor (spec §18).

    The adapter translates its wire format into these kinds; the manager
    never sees raw provider payloads. `metadata` is JSON-serializable and
    carries permission ids, action names, and paths — never secrets.
    """

    kind: DelegationEventKind
    session_id: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DelegationPermission:
    """A permission request surfaced by the delegated executor (spec §8-10).

    `action` is the provider-reported permission name (e.g. 'edit', 'bash',
    'read'). The manager translates it into JARVIS tool category/risk and
    evaluates it with the JARVIS SecurityPolicy — never blanket-approving.
    """

    permission_id: str
    session_id: str
    action: str = "unknown"
    path: str | None = None
    description: str | None = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class DelegationLimits:
    """Effective limits for one delegated task (spec §13-14)."""

    max_wall_time_seconds: float = MAX_WALL_TIME_SECONDS_DEFAULT
    max_output_bytes: int = MAX_OUTPUT_BYTES_DEFAULT
    max_permission_requests: int = MAX_PERMISSION_REQUESTS_DEFAULT
    max_session_count: int = MAX_SESSION_COUNT_DEFAULT
    max_delegation_depth: int = MAX_DELEGATION_DEPTH_DEFAULT

    def validate(self) -> None:
        try:
            check_bounded(
                "delegation.max_wall_time_seconds",
                self.max_wall_time_seconds,
                MAX_WALL_TIME_SECONDS_CEILING,
            )
            check_bounded(
                "delegation.max_output_bytes",
                self.max_output_bytes,
                MAX_OUTPUT_BYTES_CEILING,
            )
            check_bounded(
                "delegation.max_permission_requests",
                self.max_permission_requests,
                MAX_PERMISSION_REQUESTS_CEILING,
            )
            check_bounded(
                "delegation.max_session_count",
                self.max_session_count,
                MAX_SESSION_COUNT_CEILING,
            )
            check_bounded(
                "delegation.max_delegation_depth",
                self.max_delegation_depth,
                MAX_DELEGATION_DEPTH_CEILING,
            )
        except ValueError as exc:
            raise DelegationValidationError(str(exc)) from exc
        if self.max_wall_time_seconds <= 0:
            raise DelegationValidationError("max_wall_time_seconds must be positive")
        if self.max_output_bytes < 1:
            raise DelegationValidationError("max_output_bytes must be >= 1")
        if self.max_permission_requests < 1:
            raise DelegationValidationError("max_permission_requests must be >= 1")
        if self.max_session_count < 1:
            raise DelegationValidationError("max_session_count must be >= 1")
        if self.max_delegation_depth < 1:
            raise DelegationValidationError("max_delegation_depth must be >= 1")


@dataclass(frozen=True)
class DelegationRequest:
    """The unit of delegation input (spec §4). Provider-neutral by design.

    `working_directory` is validated against JARVIS path security before any
    executor session is created; defaults resolve to the configured JARVIS
    workspace.
    """

    prompt: str
    working_directory: Path
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str | None = None
    session_id: str | None = None
    requested_capabilities: tuple[str, ...] = ()
    limits: DelegationLimits | None = None
    provider: str | None = None
    depth: int = 0

    def validate(self) -> None:
        if not self.prompt or not self.prompt.strip():
            raise DelegationValidationError("delegation request requires a non-empty prompt")
        if not str(self.working_directory).strip():
            raise DelegationValidationError("delegation request requires a working directory")
        is_abs = (
            self.working_directory.is_absolute()
            or PureWindowsPath(str(self.working_directory)).is_absolute()
        )
        if not is_abs:
            raise DelegationValidationError(
                f"working directory must be absolute: {self.working_directory}"
            )
        if self.depth < 0:
            raise DelegationValidationError(f"delegation depth must be >= 0, got {self.depth}")
        if not all(isinstance(item, str) and bool(item) for item in self.requested_capabilities):
            raise DelegationValidationError("requested capabilities must be non-empty strings")


@dataclass(frozen=True)
class DelegationTask:
    """The work item the manager runs (spec §4)."""

    task_id: str
    request_id: str
    session_id: str | None
    prompt: str
    working_directory: Path
    provider: str
    depth: int
    limits: DelegationLimits
    requested_capabilities: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    state: DelegationState = DelegationState.CREATED


@dataclass
class DelegationRunStatus:
    """Mutable, observable status of a running delegation (spec §27).

    The manager owns updates; DelegationService.health()/get()/list() read it.
    Carries ids, counters, and reasons — never prompts or secrets.
    """

    task_id: str
    request_id: str
    session_id: str | None = None
    provider: str | None = None
    provider_session_id: str | None = None
    state: DelegationState = DelegationState.CREATED
    permission_requests: int = 0
    output_bytes: int = 0
    elapsed_ms: float = 0.0
    started_at: datetime | None = None
    reason: str | None = None
    error: str | None = None

    def transition(self, target: DelegationState) -> None:
        self.state = transition_state(self.state, target)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "state": self.state.value,
            "permission_requests": self.permission_requests,
            "output_bytes": self.output_bytes,
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass(frozen=True)
class DelegationResult:
    """Final outcome of a delegated task (spec §20-21).

    `summary`, `error`, and `diff` are capped by the manager so result sizes
    stay bounded. The result never auto-applies or commits changes — diff
    review is a separate, explicit action.
    """

    task_id: str
    request_id: str
    session_id: str | None
    provider: str | None
    state: DelegationState
    provider_session_id: str | None = None
    summary: str | None = None
    error: str | None = None
    reason: str | None = None
    duration_ms: float = 0.0
    permission_requests: int = 0
    output_bytes: int = 0
    files_changed: int = 0
    diff_available: bool = False
    diff: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "state": self.state.value,
            "summary": self.summary,
            "error": self.error,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "permission_requests": self.permission_requests,
            "output_bytes": self.output_bytes,
            "files_changed": self.files_changed,
            "diff_available": self.diff_available,
            "diff": self.diff,
        }
