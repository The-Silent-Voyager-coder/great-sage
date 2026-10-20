"""Structured, serializable event envelope and canonical event types.

Event type constants follow the Phase 0 documented catalog. Payloads are
JSON-serializable dictionaries.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# --- Canonical event types (Phase 0 catalog + Phase 1 lifecycle events) ---

USER_MESSAGE_RECEIVED = "UserMessageReceived"
VOICE_WAKE_DETECTED = "VoiceWakeDetected"
SPEECH_TRANSCRIBED = "SpeechTranscribed"
AI_RESPONSE_STARTED = "AIResponseStarted"
AI_RESPONSE_COMPLETED = "AIResponseCompleted"
TOOL_REQUESTED = "ToolRequested"
TOOL_ALLOWED = "ToolAllowed"
TOOL_DENIED = "ToolDenied"
TOOL_APPROVAL_REQUESTED = "ToolApprovalRequested"
TOOL_APPROVED = "ToolApproved"
TOOL_REJECTED = "ToolRejected"
TOOL_STARTED = "ToolStarted"
TOOL_COMPLETED = "ToolCompleted"
TOOL_FAILED = "ToolFailed"
TASK_CREATED = "TaskCreated"
TASK_STARTED = "TaskStarted"
TASK_PAUSED = "TaskPaused"
TASK_COMPLETED = "TaskCompleted"
TASK_FAILED = "TaskFailed"
OPENCODE_CONNECTED = "OpenCodeConnected"
OPENCODE_DISCONNECTED = "OpenCodeDisconnected"
OPENCODE_EVENT_RECEIVED = "OpenCodeEventReceived"
MEMORY_CREATED = "MemoryCreated"
MEMORY_UPDATED = "MemoryUpdated"
MEMORY_DELETED = "MemoryDeleted"
MEMORY_EXPIRED = "MemoryExpired"
MEMORY_RETRIEVED = "MemoryRetrieved"
PERMISSION_REQUESTED = "PermissionRequested"
PERMISSION_GRANTED = "PermissionGranted"
PERMISSION_DENIED = "PermissionDenied"

RUNTIME_STARTED = "RuntimeStarted"
RUNTIME_STOPPING = "RuntimeStopping"
RUNTIME_STOPPED = "RuntimeStopped"

# Phase 2 — intelligence layer
AI_REQUEST_STARTED = "AIRequestStarted"
AI_REQUEST_COMPLETED = "AIRequestCompleted"
AI_REQUEST_FAILED = "AIRequestFailed"
AI_PROVIDER_SELECTED = "AIProviderSelected"
AI_PROVIDER_UNAVAILABLE = "AIProviderUnavailable"
AI_STREAM_STARTED = "AIStreamStarted"
AI_STREAM_COMPLETED = "AIStreamCompleted"
AI_STREAM_FAILED = "AIStreamFailed"

# Phase 5A — agent orchestration (bounded AI tool-calling loop)
AGENT_STARTED = "AgentStarted"
AGENT_STEP_STARTED = "AgentStepStarted"
AGENT_TOOL_CALL_REQUESTED = "AgentToolCallRequested"
AGENT_TOOL_CALL_COMPLETED = "AgentToolCallCompleted"
AGENT_STEP_COMPLETED = "AgentStepCompleted"
AGENT_COMPLETED = "AgentCompleted"
AGENT_FAILED = "AgentFailed"
AGENT_CANCELLED = "AgentCancelled"
AGENT_TIMED_OUT = "AgentTimedOut"
AGENT_LIMIT_REACHED = "AgentLimitReached"

# Phase 5B — delegation (controlled OpenCode execution under Great Sage authority)
DELEGATION_REQUESTED = "DelegationRequested"
DELEGATION_STARTED = "DelegationStarted"
DELEGATION_PROGRESS = "DelegationProgress"
DELEGATION_PERMISSION_REQUESTED = "DelegationPermissionRequested"
DELEGATION_PERMISSION_RESOLVED = "DelegationPermissionResolved"
DELEGATION_COMPLETED = "DelegationCompleted"
DELEGATION_FAILED = "DelegationFailed"
DELEGATION_CANCELLED = "DelegationCancelled"
DELEGATION_TIMED_OUT = "DelegationTimedOut"

# Phase 6 — workspace / planning / task (bounded project intelligence)
WORKSPACE_SCANNED = "WorkspaceScanned"
WORKSPACE_SCAN_FAILED = "WorkspaceScanFailed"
PLAN_CREATED = "PlanCreated"
PLAN_FAILED = "PlanFailed"
PLAN_VERIFIED = "PlanVerified"
PLAN_APPROVED = "PlanApproved"
TASK_STEP_STARTED = "TaskStepStarted"
TASK_STEP_COMPLETED = "TaskStepCompleted"
TASK_STEP_FAILED = "TaskStepFailed"
TASK_CANCELLED = "TaskCancelled"
TASK_TIMED_OUT = "TaskTimedOut"

# Phase 8 — vision (bounded local-first capture + OCR-free grounding)
VISION_CAPTURED = "VisionCaptured"
VISION_DESCRIBED = "VisionDescribed"
VISION_FAILED = "VisionFailed"

# Phase 6 — voice (bounded local-first wake word + STT/TTS)
VOICE_SPOKEN = "VoiceSpoken"
VOICE_FAILED = "VoiceFailed"

# Roadmap Phase C — scheduler (bounded local ticks, no daemon)
SCHEDULE_CREATED = "ScheduleCreated"
SCHEDULE_REMOVED = "ScheduleRemoved"
SCHEDULE_RUN = "ScheduleRun"
SCHEDULE_FAILED = "ScheduleFailed"

# Remote chat (bounded Telegram polls, allowlisted chats only)
TELEGRAM_RECEIVED = "TelegramReceived"
TELEGRAM_REPLIED = "TelegramReplied"

DOCUMENTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        USER_MESSAGE_RECEIVED,
        VOICE_WAKE_DETECTED,
        SPEECH_TRANSCRIBED,
        AI_RESPONSE_STARTED,
        AI_RESPONSE_COMPLETED,
        TOOL_REQUESTED,
        TOOL_ALLOWED,
        TOOL_DENIED,
        TOOL_APPROVAL_REQUESTED,
        TOOL_APPROVED,
        TOOL_REJECTED,
        TOOL_STARTED,
        TOOL_COMPLETED,
        TOOL_FAILED,
        TASK_CREATED,
        TASK_STARTED,
        TASK_PAUSED,
        TASK_COMPLETED,
        TASK_FAILED,
        OPENCODE_CONNECTED,
        OPENCODE_DISCONNECTED,
        OPENCODE_EVENT_RECEIVED,
        MEMORY_CREATED,
        MEMORY_UPDATED,
        MEMORY_DELETED,
        MEMORY_EXPIRED,
        MEMORY_RETRIEVED,
        PERMISSION_REQUESTED,
        PERMISSION_GRANTED,
        PERMISSION_DENIED,
        RUNTIME_STARTED,
        RUNTIME_STOPPING,
        RUNTIME_STOPPED,
        AI_REQUEST_STARTED,
        AI_REQUEST_COMPLETED,
        AI_REQUEST_FAILED,
        AI_PROVIDER_SELECTED,
        AI_PROVIDER_UNAVAILABLE,
        AI_STREAM_STARTED,
        AI_STREAM_COMPLETED,
        AI_STREAM_FAILED,
        AGENT_STARTED,
        AGENT_STEP_STARTED,
        AGENT_TOOL_CALL_REQUESTED,
        AGENT_TOOL_CALL_COMPLETED,
        AGENT_STEP_COMPLETED,
        AGENT_COMPLETED,
        AGENT_FAILED,
        AGENT_CANCELLED,
        AGENT_TIMED_OUT,
        AGENT_LIMIT_REACHED,
        DELEGATION_REQUESTED,
        DELEGATION_STARTED,
        DELEGATION_PROGRESS,
        DELEGATION_PERMISSION_REQUESTED,
        DELEGATION_PERMISSION_RESOLVED,
        DELEGATION_COMPLETED,
        DELEGATION_FAILED,
        DELEGATION_CANCELLED,
        DELEGATION_TIMED_OUT,
        WORKSPACE_SCANNED,
        WORKSPACE_SCAN_FAILED,
        PLAN_CREATED,
        PLAN_FAILED,
        PLAN_VERIFIED,
        PLAN_APPROVED,
        TASK_STEP_STARTED,
        TASK_STEP_COMPLETED,
        TASK_STEP_FAILED,
        TASK_CANCELLED,
        TASK_TIMED_OUT,
        VISION_CAPTURED,
        VISION_DESCRIBED,
        VISION_FAILED,
        VOICE_SPOKEN,
        VOICE_FAILED,
        SCHEDULE_CREATED,
        SCHEDULE_REMOVED,
        SCHEDULE_RUN,
        SCHEDULE_FAILED,
        TELEGRAM_RECEIVED,
        TELEGRAM_REPLIED,
    }
)


@dataclass(frozen=True)
class Event:
    """Immutable, JSON-serializable event envelope."""

    type: str
    source: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    session_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        return cls(
            id=str(data["id"]),
            type=str(data["type"]),
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
            source=str(data["source"]),
            session_id=data.get("session_id"),
            task_id=data.get("task_id"),
            payload=dict(data.get("payload") or {}),
        )
