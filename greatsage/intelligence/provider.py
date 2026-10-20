"""AI provider abstraction: capability model, lifecycle states, health, interface.

The core and the router depend on this module only — never on Ollama,
OpenCode, or HTTP transports (docs/ARCHITECTURE.md §5). A provider failure is
a state/health problem here, never an exception that can crash the runtime.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from greatsage.core.health import HealthStatus
from greatsage.intelligence.models import AIRequest, AIResponse

log = logging.getLogger("greatsage.intelligence.provider")


class Capability(StrEnum):
    TEXT_GENERATION = "text_generation"
    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    VISION = "vision"
    STRUCTURED_OUTPUT = "structured_output"
    CANCELLATION = "cancellation"
    LOCAL = "local"
    REMOTE = "remote"
    CODE_EXECUTION = "code_execution"
    DELEGATION = "delegation"


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider advertises; the router filters on these."""

    capabilities: frozenset[Capability] = frozenset({Capability.TEXT_GENERATION})
    model_ids: tuple[str, ...] = ()
    context_window: int | None = None
    max_output_tokens: int | None = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def all_supports(self, required: frozenset[Capability]) -> bool:
        return required <= self.capabilities


class ProviderState(StrEnum):
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"

    def to_health(self) -> HealthStatus:
        if self is ProviderState.READY:
            return HealthStatus.HEALTHY
        if self in (ProviderState.DEGRADED, ProviderState.UNAVAILABLE):
            return HealthStatus.DEGRADED
        return HealthStatus.UNHEALTHY


@dataclass(frozen=True)
class ProviderHealth:
    """Health snapshot (docs/INTERFACES.md §8). Never blocks the runtime."""

    provider_id: str
    ok: bool
    state: ProviderState
    detail: str | None = None
    latency_ms: float | None = None
    model_loaded: str | None = None
    last_check: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "ok": self.ok,
            "state": self.state.value,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "model_loaded": self.model_loaded,
            "last_check": self.last_check.isoformat(),
        }


class AIProvider(ABC):
    """Provider-independent adapter interface.

    Implementations translate their vendor wire format into the core models.
    `generate` is synchronous (adapters may block on their HTTP call); `stream`
    is an async generator. `cancel` is best-effort and only meaningful when
    CANCELLATION is advertised.
    """

    def __init__(self, provider_id: str) -> None:
        self._provider_id = provider_id
        self._state = ProviderState.UNINITIALIZED
        self._detail: str | None = None

    # --- identity / capabilities ---------------------------------------

    def provider_id(self) -> str:
        return self._provider_id

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    # --- lifecycle ------------------------------------------------------

    @property
    def state(self) -> ProviderState:
        return self._state

    @property
    def detail(self) -> str | None:
        return self._detail

    def init(self) -> None:
        """Bring the provider up; unreachable targets end UNAVAILABLE, never raise."""
        if self._state not in (ProviderState.UNINITIALIZED, ProviderState.STOPPED):
            log.warning(
                "provider %s init requested from state %s",
                self._provider_id,
                self._state.value,
            )
            return
        self._set_state(ProviderState.INITIALIZING)
        try:
            health = self.health()
            if health.ok:
                self._set_state(ProviderState.READY)
            else:
                # Preserve DEGRADED vs UNAVAILABLE from the probe. A malformed
                # service is degraded (reachable but wrong); an unreachable one
                # is unavailable. Collapsing both hides the distinction.
                state = (
                    health.state
                    if health.state
                    in (ProviderState.DEGRADED, ProviderState.UNAVAILABLE, ProviderState.FAILED)
                    else ProviderState.UNAVAILABLE
                )
                self._set_state(state, health.detail)
        except Exception as exc:  # provider failure must never escape init
            log.error(
                "provider %s failed to initialize",
                self._provider_id,
                exc_info=exc,
                extra={"component": "intelligence", "provider": self._provider_id},
            )
            self._set_state(ProviderState.FAILED, str(exc))

    def shutdown(self) -> None:
        """Release provider resources; safe to call repeatedly."""
        if self._state in (ProviderState.STOPPED, ProviderState.STOPPING):
            return
        self._set_state(ProviderState.STOPPING)
        try:
            self._on_shutdown()
        except Exception as exc:  # shutdown must not propagate
            log.error(
                "provider %s shutdown reported an error",
                self._provider_id,
                exc_info=exc,
                extra={"component": "intelligence", "provider": self._provider_id},
            )
        self._set_state(ProviderState.STOPPED)

    def _on_shutdown(self) -> None:  # override point
        return None

    # --- health ---------------------------------------------------------

    def health(self) -> ProviderHealth:
        """Return the current health snapshot (fast, fail-safe)."""
        started = datetime.now(UTC)
        if self._state in (ProviderState.STOPPED, ProviderState.STOPPING):
            return ProviderHealth(
                provider_id=self._provider_id,
                ok=False,
                state=self._state,
                detail="provider is not running",
            )
        try:
            result = self._probe_health()
        except Exception as exc:
            self._set_state(ProviderState.UNAVAILABLE, str(exc))
            return ProviderHealth(
                provider_id=self._provider_id,
                ok=False,
                state=self._state,
                detail=str(exc),
                latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000.0,
            )
        if result.ok and self._state is not ProviderState.READY:
            # Recovery: a previously unavailable/degraded provider that now
            # probes healthy must become selectable again (the router filters
            # on internal state, not just the returned health object).
            self._set_state(ProviderState.READY, result.detail)
        if result.latency_ms is None:
            elapsed = (datetime.now(UTC) - started).total_seconds() * 1000.0
            result = ProviderHealth(
                provider_id=result.provider_id,
                ok=result.ok,
                state=result.state,
                detail=result.detail,
                latency_ms=elapsed,
                model_loaded=result.model_loaded,
                last_check=result.last_check,
            )
        return result

    @abstractmethod
    def _probe_health(self) -> ProviderHealth: ...

    # --- generation -----------------------------------------------------

    @abstractmethod
    def generate(self, request: AIRequest) -> AIResponse: ...

    def stream(self, request: AIRequest) -> Any:
        """Async generator of StreamChunk.

        Default implementation is the explicit capability gate: providers
        that do not advertise STREAMING must not be streamed, and the error
        is raised at first iteration, never silently ignored.
        """
        self._require_capability(Capability.STREAMING)
        raise NotImplementedError("stream() not implemented by this provider")

    def cancel(self, request_id: str) -> None:
        """Cancel an in-flight request; meaningful only with CANCELLATION."""
        raise NotImplementedError("cancel() not implemented by this provider")

    # --- helpers --------------------------------------------------------

    def _set_state(self, state: ProviderState, detail: str | None = None) -> None:
        self._state = state
        if detail is not None:
            self._detail = detail

    def _require_ready(self) -> None:
        if self.state is not ProviderState.READY:
            from greatsage.exceptions import ProviderUnavailableError

            raise ProviderUnavailableError(
                f"provider {self._provider_id} is {self.state.value}"
                + (f": {self._detail}" if self._detail else "")
            )

    def _require_capability(self, capability: Capability) -> None:
        if not self.capabilities().supports(capability):
            from greatsage.exceptions import ProviderCapabilityError

            raise ProviderCapabilityError(
                f"provider {self._provider_id} does not support {capability.value}"
            )
