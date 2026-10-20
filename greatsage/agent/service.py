"""Agent service facade (Phase 5A).

Owns the agent loop lifecycle: availability, health, limit derivation from
configuration, provider capability gating, approval-provider installation,
memory retrieval wiring, and cancellation. Runs execute through the
AgentOrchestrator, which in turn only talks to the IntelligenceService and
the ToolService security pipeline — the CLI uses exactly the same path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from greatsage.agent.approval import AgentApprovalProvider, NotifyingApprovalProvider
from greatsage.agent.cancellation import CancellationToken
from greatsage.agent.models import (
    AgentContext,
    AgentLimits,
    AgentResult,
    AgentRunStatus,
    AgentState,
    AgentTask,
)
from greatsage.agent.orchestrator import AgentOrchestrator
from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.exceptions import (
    AgentUnavailableError,
    AgentValidationError,
    DelegationUnavailableError,
    ProviderCapabilityError,
    ProviderUnavailableError,
)
from greatsage.intelligence.models import Message
from greatsage.intelligence.provider import Capability, ProviderState

if TYPE_CHECKING:
    from greatsage.delegation.models import DelegationRequest, DelegationResult
    from greatsage.intelligence.service import IntelligenceService
    from greatsage.memory.service import MemoryService
    from greatsage.tools.approval import ApprovalProvider
    from greatsage.tools.service import ToolService

log = logging.getLogger("greatsage.agent.service")

_MEMORY_REFERENCE_LIMIT = 5


class AgentService:
    """Facade over the agent orchestrator and its dependencies."""

    def __init__(
        self,
        intelligence: IntelligenceService | None = None,
        tools: ToolService | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self._intelligence = intelligence
        self._tools = tools
        self._memory = memory
        self.publisher: Any = None  # callable(event) -> None, wired by runtime
        self.availability: str = "unavailable"
        self.detail: str = "agent service not started"
        self._config: SageConfig | None = None
        self._status: AgentRunStatus | None = None
        self._token: CancellationToken | None = None

    # --- lifecycle -----------------------------------------------------

    def start(self, config: SageConfig | None = None) -> None:
        self._config = config
        if config is None:
            self.availability = "unavailable"
            self.detail = "no configuration provided"
            return
        if not config.agent.enabled:
            self.availability = "disabled"
            self.detail = "agent subsystem disabled by configuration"
            return
        self.availability = "healthy"
        self.detail = (
            f"agent loop ready (steps<={config.agent.max_steps}, "
            f"tool calls<={config.agent.max_tool_calls}, "
            f"wall<={config.agent.max_wall_time_seconds}s, "
            f"loop threshold={config.agent.loop_detection_threshold})"
        )
        log.info(
            "agent service started",
            extra={
                "component": "agent",
                "max_steps": config.agent.max_steps,
                "max_tool_calls": config.agent.max_tool_calls,
                "max_wall_time_seconds": config.agent.max_wall_time_seconds,
            },
        )

    def shutdown(self) -> None:
        self._status = None
        self._token = None
        self.availability = "disabled"
        self.detail = "agent service stopped"
        log.info("agent service stopped", extra={"component": "agent"})

    # --- public API ----------------------------------------------------

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        max_steps: int | None = None,
        approval: ApprovalProvider | None = None,
    ) -> AgentResult:
        """Run one bounded agent task to a terminal state (blocks)."""
        self._require_available()
        if not prompt or not prompt.strip():
            raise AgentValidationError("agent run requires a non-empty prompt")
        assert self._tools is not None
        assert self._intelligence is not None
        provider_id = provider or self._default_provider()
        self._require_tool_calling(provider_id)
        limits = self._effective_limits(max_steps)
        limits.validate()
        task = AgentTask(
            task_id=task_id if task_id is not None else _new_task_id(),
            session_id=session_id,
            prompt=prompt,
            provider=provider_id,
            model=model,
        )
        context = AgentContext(
            session_id=session_id,
            task_id=task.task_id,
            prompt=prompt,
            conversation=(Message.user(prompt),),
            memory_references=self._retrieve_memory(prompt, session_id),
            system_note=(
                "You are Great Sage. You may call tools to gather facts, but you "
                "must never claim actions you did not perform."
            ),
        )
        status = AgentRunStatus(task_id=task.task_id, session_id=session_id)
        token = CancellationToken()
        self._status = status
        self._token = token
        previous_approval = self._tools.approval

        def _on_pending(request: Any) -> None:
            self._on_approval_pending(status, request)

        agent_approval: ApprovalProvider
        if approval is None:
            agent_approval = AgentApprovalProvider(
                on_pending=_on_pending,
                wait_seconds=limits.approval_wait_seconds,
                cancel_token=token,
            )
        else:
            # Keep the run observably paused even when a caller supplies a
            # custom provider (CLI broker, future UI provider).
            agent_approval = NotifyingApprovalProvider(approval, _on_pending)
        self._tools.approval = agent_approval
        try:
            result = AgentOrchestrator(
                self._intelligence, self._tools, publisher=self.publisher
            ).run(task, context, limits, status, token=token)
        finally:
            self._tools.approval = previous_approval
            self._status = None
            self._token = None
        return result

    def cancel(self) -> None:
        """Request cancellation of the in-flight run (best-effort)."""
        if self._token is not None:
            self._token.cancel()
        status = self._status
        if self._intelligence is not None and status is not None and status.request_id:
            try:
                self._intelligence.cancel(status.request_id)
            except Exception as exc:
                log.debug("agent cancel failed: %s", exc)

    def delegate(self, request: DelegationRequest) -> DelegationResult:
        """Delegate a coding task to an executor under Great Sage authority.

        Explicit path only — the agent never fabricates delegation from
        keywords. A depth-0 request is issued against the delegation manager,
        which enforces limits, permissions, path security, and cancellation.
        """
        from greatsage.delegation.manager import DelegationManager

        registry = self._require_delegation_registry()
        self._require_delegation_capability(request.provider)
        if self._config is None:
            raise DelegationUnavailableError("delegation requires a configured runtime")
        manager = DelegationManager(
            config=self._config,
            registry=registry,
            publisher=self.publisher,
        )
        if self._tools is not None:
            manager.approval = self._tools.approval
        if request.depth > 0:
            raise AgentValidationError(
                "agent may only start depth-0 delegations; recursive "
                "delegation is controlled by the delegation manager"
            )
        return manager.delegate(request)

    def _require_delegation_registry(self) -> Any:
        if self._intelligence is None:
            raise DelegationUnavailableError("delegation requires a wired intelligence service")
        return self._intelligence.registry

    def _require_delegation_capability(self, provider_id: str | None) -> None:
        registry = self._intelligence.registry if self._intelligence is not None else None
        selected = provider_id or (self._config.delegation.default_provider
                                   if self._config is not None else None)
        if registry is None or selected is None:
            raise DelegationUnavailableError("delegation is not configured")
        if not registry.has(selected):
            raise ProviderUnavailableError(
                f"delegation provider {selected!r} is not registered "
                f"(available: {', '.join(registry.ids()) or 'none'})"
            )
        provider = registry.get(selected)
        if not provider.capabilities().supports(Capability.DELEGATION):
            raise ProviderCapabilityError(
                f"provider {selected!r} does not support delegation; "
                "no silent fallback to a non-delegating provider"
            )

    # --- health --------------------------------------------------------

    def health(self) -> dict[str, Any]:
        status = self._status
        current: dict[str, Any] = {}
        if status is not None:
            current = {
                "state": status.state.value,
                "steps": status.steps,
                "tool_calls": status.tool_calls,
                "elapsed_ms": status.elapsed_ms,
                "provider": status.provider,
                "model": status.model,
                "reason": status.reason,
                "error": status.error,
            }
        return {
            "available": self.availability == "healthy",
            "status": self.availability,
            "detail": self.detail,
            "enabled": bool(self._config is not None and self._config.agent.enabled),
            "current": current,
        }

    def register_health_check(self, health_registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self.availability == "disabled":
                return HealthStatus.HEALTHY
            if self.availability == "healthy":
                return HealthStatus.HEALTHY
            if self.availability == "unavailable":
                return HealthStatus.UNHEALTHY
            return HealthStatus.DEGRADED  # pragma: no cover - future states

        health_registry.register("agent", checker, "agent orchestrator (bounded loop)")

    # --- internals -------------------------------------------------------

    def _require_available(self) -> None:
        if self.availability != "healthy":
            raise AgentUnavailableError(self.detail or "agent service is not available")
        if self._intelligence is None or self._tools is None:
            raise AgentUnavailableError("agent service dependencies are not wired")

    def _default_provider(self) -> str:
        if self._config is not None:
            return self._config.ai.default_provider
        raise AgentUnavailableError("agent service has no configured provider")

    def _require_tool_calling(self, provider_id: str) -> None:
        assert self._intelligence is not None
        registry = self._intelligence.registry
        if not registry.has(provider_id):
            raise ProviderUnavailableError(
                f"provider {provider_id!r} is not registered "
                f"(available: {', '.join(registry.ids()) or 'none'})"
            )
        provider = registry.get(provider_id)
        if provider.state is not ProviderState.READY:
            raise ProviderUnavailableError(
                f"provider {provider_id!r} is {provider.state.value}"
            )
        if not provider.capabilities().supports(Capability.TOOL_CALLING):
            raise ProviderCapabilityError(
                f"provider {provider_id!r} does not support tool calling "
                "(required for agent runs); no silent fallback to a text-only provider"
            )

    def _effective_limits(self, max_steps: int | None) -> AgentLimits:
        assert self._config is not None
        agent = self._config.agent
        return AgentLimits(
            max_steps=max_steps if max_steps is not None else agent.max_steps,
            max_tool_calls=agent.max_tool_calls,
            max_wall_time_seconds=float(agent.max_wall_time_seconds),
            max_single_tool_calls=agent.max_single_tool_calls,
            max_total_tool_output_bytes=agent.max_total_tool_output_bytes,
            loop_detection_threshold=agent.loop_detection_threshold,
        )

    def _retrieve_memory(self, prompt: str, session_id: str | None) -> tuple[dict[str, Any], ...]:
        """Explicit, bounded memory retrieval for context (spec §15-16).

        Never dumps the memory database and never auto-saves anything;
        retrieval failures degrade to no references instead of failing the run.
        """
        if self._memory is None:
            return ()
        try:
            result = self._memory.retrieve(
                query=prompt,
                session_id=session_id,
                limit=_MEMORY_REFERENCE_LIMIT,
            )
        except Exception as exc:
            log.debug("agent memory retrieval skipped: %s", exc, extra={"component": "agent"})
            return ()
        references = []
        for item in result.items:
            memory = item.memory
            content = memory.content if isinstance(memory.content, str) else ""
            references.append(
                {
                    "id": memory.id,
                    "type": memory.memory_type.value,
                    "confidence": memory.confidence,
                    "content": content[:200],
                }
            )
        return tuple(references)

    def _on_approval_pending(self, status: AgentRunStatus, request: Any) -> None:
        status.transition(AgentState.WAITING_FOR_APPROVAL)


def _new_task_id() -> str:
    import uuid

    return uuid.uuid4().hex
