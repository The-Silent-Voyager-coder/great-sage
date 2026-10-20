"""Typed configuration model.

Mirrors config/sage.example.yaml, the Phase 0 source of truth.
All records are immutable: values are validated before construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SecurityMode(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolSecurityMode(StrEnum):
    """Tool-system security modes (docs/SECURITY_MODEL.md §tools)."""

    LOCKDOWN = "lockdown"
    NORMAL = "normal"
    DEVELOPMENT = "development"


class RiskLevel(StrEnum):
    READ = "READ"
    LOW_WRITE = "LOW_WRITE"
    HIGH_WRITE = "HIGH_WRITE"
    SYSTEM = "SYSTEM"
    FORBIDDEN = "FORBIDDEN"


@dataclass(frozen=True)
class CoreConfig:
    name: str
    data_dir: Path
    cache_dir: Path
    logs_dir: Path
    runtime_dir: Path
    workspaces_dir: Path
    models_dir: Path
    backups_dir: Path
    timezone: str

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.data_dir,
            self.cache_dir,
            self.logs_dir,
            self.runtime_dir,
            self.workspaces_dir,
            self.models_dir,
            self.backups_dir,
        )


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    format: str
    retention_days: int


@dataclass(frozen=True)
class EventsConfig:
    queue_maxsize: int
    worker_count: int


@dataclass(frozen=True)
class LocalProviderConfig:
    type: str
    enabled: bool
    base_url: str
    model: str
    timeout_seconds: float


@dataclass(frozen=True)
class OpenCodeProviderConfig:
    type: str
    enabled: bool
    base_url: str
    api_key_env: str
    username_env: str
    model: str
    timeout_seconds: float


@dataclass(frozen=True)
class AIConfig:
    default_provider: str
    local: LocalProviderConfig
    opencode: OpenCodeProviderConfig

    @property
    def provider_names(self) -> tuple[str, ...]:
        return ("local", "opencode")


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool
    database_path: Path
    auto_save_conversations: bool
    default_confidence: float
    retention_days: int
    embeddings_enabled: bool
    embedding_model: str
    embedding_base_url: str


@dataclass(frozen=True)
class TasksConfig:
    max_iterations: int
    default_timeout_minutes: int
    persist_interval_seconds: int


@dataclass(frozen=True)
class WorkspaceConfig:
    """Workspace discovery settings (Phase 6)."""

    enabled: bool
    max_scan_depth: int
    max_entries: int
    scan_timeout_seconds: float
    database_path: Path


@dataclass(frozen=True)
class PlanningConfig:
    """Planner settings (Phase 6 deterministic templates)."""

    enabled: bool
    max_plan_steps: int
    database_path: Path


@dataclass(frozen=True)
class TaskConfig:
    """Task execution settings (Phase 6 bounded multi-step)."""

    enabled: bool
    max_steps: int
    per_step_timeout_seconds: float
    total_timeout_seconds: float
    database_path: Path


@dataclass(frozen=True)
class SchedulerConfig:
    """Scheduler settings (roadmap Phase C bounded ticks)."""

    enabled: bool
    max_schedules: int
    database_path: Path


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram bridge settings (remote chat, laptop stays home)."""

    enabled: bool
    token_env: str
    allowed_chat_ids: tuple[int, ...]
    poll_timeout_seconds: float
    max_listen_seconds: float


@dataclass(frozen=True)
class ToolDefaultsConfig:
    default_risk: RiskLevel


@dataclass(frozen=True)
class ToolsConfig:
    """Tool-system settings (Phase 4 secure tool layer)."""

    working_directory: Path
    execution_timeout_seconds: float
    max_output_bytes: int
    allowed_roots: tuple[Path, ...]
    denied_roots: tuple[Path, ...]
    terminal: ToolDefaultsConfig
    browser: ToolDefaultsConfig


@dataclass(frozen=True)
class AgentConfig:
    """Agent-loop settings (Phase 5A bounded tool calling).

    Limits are validated against absolute safety ceilings at load time
    (greatsage.agent.limits); the orchestrator re-validates every run.
    """

    enabled: bool
    max_steps: int
    max_tool_calls: int
    max_wall_time_seconds: float
    max_single_tool_calls: int
    max_total_tool_output_bytes: int
    loop_detection_threshold: int


@dataclass(frozen=True)
class DelegationConfig:
    """Delegation settings (Phase 5B controlled OpenCode execution).

    Limits are validated against absolute safety ceilings at load time
    (sage.delegation.limits); the manager re-validates every run.
    """

    enabled: bool
    default_provider: str
    max_wall_time_seconds: float
    max_output_bytes: int
    max_permission_requests: int
    max_session_count: int
    max_delegation_depth: int


@dataclass(frozen=True)
class SecurityConfig:
    default_mode: SecurityMode
    mode: ToolSecurityMode
    allow_auto_approve_read: bool
    destructive_confirm: bool
    audit_log: Path


@dataclass(frozen=True)
class WakeWordConfig:
    enabled: bool
    model: str


@dataclass(frozen=True)
class SttConfig:
    engine: str
    model: str
    language: str


@dataclass(frozen=True)
class TtsConfig:
    engine: str
    voice: str


@dataclass(frozen=True)
class VoiceConfig:
    wake_word: WakeWordConfig
    stt: SttConfig
    tts: TtsConfig


@dataclass(frozen=True)
class SageConfig:
    """Root immutable configuration object."""
    core: CoreConfig
    logging: LoggingConfig
    events: EventsConfig
    ai: AIConfig
    memory: MemoryConfig
    tasks: TasksConfig
    tools: ToolsConfig
    agent: AgentConfig
    delegation: DelegationConfig
    workspace: WorkspaceConfig
    planning: PlanningConfig
    task: TaskConfig
    scheduler: SchedulerConfig
    telegram: TelegramConfig
    security: SecurityConfig
    voice: VoiceConfig


# Previous-generation alias (past life: Jarvis). New code must use SageConfig.
JarvisConfig = SageConfig
