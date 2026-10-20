"""Great Sage-specific exception hierarchy.

Small by design (see docs/DEVELOPMENT_RULES.md — no exception sprawl).
Every error preserves useful context via its message.
"""


class SageError(Exception):
    """Base class for all Great Sage errors."""


class ConfigurationError(SageError):
    """Configuration could not be loaded or validated."""


class LifecycleError(SageError):
    """An invalid runtime state transition or lifecycle violation."""


class ServiceError(SageError):
    """Service registry registration, retrieval, or lifecycle failure."""


class EventError(SageError):
    """Event publication or bus lifecycle failure."""


class ValidationError(SageError):
    """Input did not pass validation."""


class ProviderError(SageError):
    """A provider adapter failed (network, malformed response, service error)."""


class ProviderUnavailableError(ProviderError):
    """The provider endpoint is unreachable or the provider is not ready."""


class ProviderCapabilityError(ProviderError):
    """The requested behavior is not supported by this provider's capabilities."""


class RoutingError(SageError):
    """No provider could be selected for the request (policy + availability)."""


class MemoryError(SageError):
    """The memory subsystem failed (database, repository, or service)."""


class MemoryValidationError(MemoryError):
    """A memory model or request did not pass validation."""


class MemoryNotFoundError(MemoryError):
    """The requested memory id does not exist."""


class MemoryDatabaseError(MemoryError):
    """The memory database is inaccessible, corrupted, or schema-mismatched."""


class MemoryUnavailableError(MemoryError):
    """The memory subsystem failed to start and cannot serve requests."""


class ToolError(SageError):
    """The tool subsystem failed (registry, policy, or execution)."""


class ToolNotFoundError(ToolError):
    """The requested tool id is not registered."""


class ToolValidationError(ToolError):
    """A tool request did not pass schema validation."""


class ToolPermissionDeniedError(ToolError):
    """The security policy denied the tool request."""


class ToolExecutionError(ToolError):
    """The tool executed but failed (runtime, OS, or timeout errors)."""


class ToolUnavailableError(ToolError):
    """The tool subsystem failed to start and cannot serve requests."""


class AgentError(SageError):
    """The agent subsystem failed (orchestration, limits, or lifecycle)."""


class AgentValidationError(AgentError):
    """An agent model or request did not pass validation."""


class AgentStateError(AgentError):
    """An invalid agent state transition was attempted."""


class AgentLimitError(AgentError):
    """An agent loop limit was reached (bounded execution enforced)."""

    def __init__(self, message: str, *, kind: str = "limit") -> None:
        super().__init__(message)
        self.kind = kind


class AgentCancelledError(AgentError):
    """The agent task was cancelled."""


class AgentTimeoutError(AgentError):
    """The agent task exceeded its wall-clock limit."""


class AgentUnavailableError(AgentError):
    """The agent subsystem failed to start and cannot serve requests."""


class DelegationError(SageError):
    """The delegation subsystem failed (lifecycle, limits, or transport)."""


class DelegationValidationError(DelegationError):
    """A delegation request did not pass validation."""


class DelegationStateError(DelegationError):
    """An invalid delegation state transition was attempted."""


class DelegationLimitError(DelegationError):
    """A delegation limit was reached (bounded execution enforced)."""

    def __init__(self, message: str, *, kind: str = "limit") -> None:
        super().__init__(message)
        self.kind = kind


class DelegationCancelledError(DelegationError):
    """The delegated task was cancelled."""


class DelegationTimeoutError(DelegationError):
    """The delegated task exceeded its wall-clock limit."""


class DelegationUnavailableError(DelegationError):
    """The delegation subsystem failed to start and cannot serve requests."""


class WorkspaceError(SageError):
    """Workspace discovery failed."""


class WorkspaceValidationError(WorkspaceError):
    """Workspace input did not pass validation."""


class WorkspaceUnavailableError(WorkspaceError):
    """Workspace subsystem not available."""


class PlanningError(SageError):
    """Planning failed."""


class PlanningValidationError(PlanningError):
    """Plan input did not pass validation."""


class PlanningUnavailableError(PlanningError):
    """Planning subsystem not available."""


class TaskError(SageError):
    """Task execution failed."""


class TaskValidationError(TaskError):
    """Task input did not pass validation."""


class TaskStateError(TaskError):
    """Invalid task state transition."""


class TaskLimitError(TaskError):
    """Task limit was reached (bounded execution)."""

    def __init__(self, message: str, *, kind: str = "limit") -> None:
        super().__init__(message)
        self.kind = kind


class TaskCancelledError(TaskError):
    """Task was cancelled."""


class TaskTimeoutError(TaskError):
    """Task exceeded its wall-clock limit."""


class TaskUnavailableError(TaskError):
    """Task subsystem not available."""


class HudError(SageError):
    """The HUD subsystem failed (read-only aggregation error)."""


class HudValidationError(HudError):
    """A HUD request did not pass validation (unknown section, bad limit)."""


class HudUnavailableError(HudError):
    """The HUD snapshot could not be assembled (runtime not started)."""


class VisionError(SageError):
    """The vision subsystem failed (capture, grounding, or storage)."""


class VisionValidationError(VisionError):
    """A vision model or request did not pass validation."""


class VisionTimeoutError(VisionError):
    """A capture exceeded its wall-clock limit."""


class VisionUnavailableError(VisionError):
    """The vision subsystem failed to start and cannot serve requests."""


class VoiceError(SageError):
    """The voice subsystem failed (wake word, STT, TTS, or storage)."""


class VoiceValidationError(VoiceError):
    """A voice model or request did not pass validation."""


class VoiceTimeoutError(VoiceError):
    """A listen/speak turn exceeded its wall-clock limit."""


class VoiceUnavailableError(VoiceError):
    """The voice subsystem failed to start and cannot serve requests."""


class SchedulerError(SageError):
    """The scheduler subsystem failed (schedules, ticks, or storage)."""


class SchedulerValidationError(SchedulerError):
    """A schedule or tick request did not pass validation."""


class SchedulerUnavailableError(SchedulerError):
    """The scheduler subsystem failed to start and cannot serve requests."""


# --- Previous-generation aliases (past life: Jarvis) ---
# Kept for backward compatibility; new code must use SageError.
JarvisError = SageError
