"""Delegation service facade (Phase 5B).

Owns the delegation lifecycle: availability, health, limit derivation from
configuration, provider readiness, approval-provider synchronization, and
cancellation. Execution runs through the DelegationManager, which in turn
only talks to a provider that advertises Capability.DELEGATION and the
Phase 4 security pipeline — the CLI and the agent use exactly the same path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.delegation.manager import DelegationManager
from greatsage.delegation.models import (
    DelegationRequest,
    DelegationResult,
)
from greatsage.exceptions import DelegationUnavailableError

if TYPE_CHECKING:
    from greatsage.intelligence.service import IntelligenceService
    from greatsage.tools.service import ToolService

log = logging.getLogger("greatsage.delegation.service")


class DelegationService:
    """Facade over the delegation manager and its dependencies."""

    def __init__(
        self,
        intelligence: IntelligenceService | None = None,
        tools: ToolService | None = None,
    ) -> None:
        self._intelligence = intelligence
        self._tools = tools
        self._manager: DelegationManager | None = None
        self.availability: str = "unavailable"
        self.detail: str = "delegation service not started"
        self._config: SageConfig | None = None
        self.publisher: Any = None  # wired by Runtime to EventBus.publish_nowait

    # --- lifecycle -----------------------------------------------------

    def start(self, config: SageConfig | None = None) -> None:
        self._config = config
        if config is None:
            self.availability = "unavailable"
            self.detail = "no configuration provided"
            return
        if not config.delegation.enabled:
            self.availability = "disabled"
            self.detail = "delegation subsystem disabled by configuration"
            return
        if self._intelligence is None or self._tools is None:
            self.availability = "unavailable"
            self.detail = "delegation service dependencies are not wired"
            return
        self._manager = DelegationManager(
            config=config,
            registry=self._intelligence.registry,
            publisher=self.publisher,
        )
        self.availability = "healthy"
        self.detail = (
            f"delegation ready (provider<={config.delegation.default_provider}, "
            f"wall<={config.delegation.max_wall_time_seconds}s, "
            f"sessions<={config.delegation.max_session_count}, "
            f"depth<={config.delegation.max_delegation_depth})"
        )
        log.info(
            "delegation service started",
            extra={
                "component": "delegation",
                "default_provider": config.delegation.default_provider,
                "max_wall_time_seconds": config.delegation.max_wall_time_seconds,
                "max_session_count": config.delegation.max_session_count,
            },
        )

    def shutdown(self) -> None:
        self._manager = None
        self.availability = "disabled"
        self.detail = "delegation service stopped"
        log.info("delegation service stopped", extra={"component": "delegation"})

    # --- public API ----------------------------------------------------

    def delegate(self, request: DelegationRequest) -> DelegationResult:
        """Run one controlled delegation to a terminal state (blocks)."""
        self._require_available()
        assert self._manager is not None
        self._sync_approval()
        return self._manager.delegate(request)

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Request cancellation of a running delegation (best-effort)."""
        self._require_available()
        assert self._manager is not None
        return self._manager.cancel(task_id)

    def get(self, task_id: str) -> dict[str, Any]:
        """Snapshot of a running or finished delegation task."""
        self._require_available()
        assert self._manager is not None
        return self._manager.get(task_id)

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        """Running tasks first, then recent results (newest first)."""
        self._require_available()
        assert self._manager is not None
        return self._manager.list_tasks(limit)

    # --- health --------------------------------------------------------

    def health(self) -> dict[str, Any]:
        manager = self._manager
        base: dict[str, Any] = {
            "available": self.availability == "healthy",
            "status": self.availability,
            "detail": self.detail,
            "enabled": bool(self._config is not None and self._config.delegation.enabled),
        }
        if manager is not None:
            base.update(manager.health())
        return base

    def register_health_check(self, health_registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self.availability == "disabled":
                return HealthStatus.HEALTHY
            if self.availability == "healthy":
                return HealthStatus.HEALTHY
            return HealthStatus.UNHEALTHY  # pragma: no cover - start/shutdown keep it set

        health_registry.register("delegation", checker, "delegation manager (bounded runs)")

    # --- internals -------------------------------------------------------

    def _require_available(self) -> None:
        if self.availability != "healthy":
            raise DelegationUnavailableError(self.detail or "delegation service is not available")
        assert self._manager is not None

    def _sync_approval(self) -> None:
        # The approval decision source is the Phase 4 ToolService, so the CLI,
        # the agent, and delegation always resolve approvals identically.
        if self._tools is not None and self._manager is not None:
            self._manager.approval = self._tools.approval
        # Keep the manager's publisher in sync with the service (wired by Runtime).
        if self._manager is not None:
            self._manager.publisher = self.publisher
