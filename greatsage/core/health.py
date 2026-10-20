"""Health abstraction: HEALTHY / DEGRADED / UNHEALTHY components.

Each component is registered with a synchronous checker callable returning a
HealthStatus. Provider-specific checks belong to later phases; Phase 1 covers
core, configuration, event bus, service registry, and storage.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class HealthStatus(Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


@dataclass(frozen=True)
class HealthReport:
    component: str
    status: HealthStatus
    detail: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "status": self.status.value,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat(),
        }


HealthChecker = Callable[[], HealthStatus]


class HealthRegistry:
    """Collect and run component health checks."""

    def __init__(self) -> None:
        self._checkers: dict[str, tuple[HealthChecker, str]] = {}

    def register(self, component: str, checker: HealthChecker, detail: str = "") -> None:
        self._checkers[component] = (checker, detail)

    @property
    def components(self) -> tuple[str, ...]:
        return tuple(sorted(self._checkers))

    def check(self, component: str) -> HealthReport:
        checker, detail = self._checkers[component]
        try:
            status = checker()
        except Exception as exc:
            # A single broken probe must not take down the whole health
            # report; report that component as unhealthy and keep going.
            return HealthReport(
                component=component,
                status=HealthStatus.UNHEALTHY,
                detail=f"health check failed: {exc}",
            )
        return HealthReport(component=component, status=status, detail=detail)

    def check_all(self) -> list[HealthReport]:
        return [self.check(component) for component in self.components]

    def overall(self, reports: list[HealthReport] | None = None) -> HealthStatus:
        source = reports if reports is not None else self.check_all()
        if any(report.status is HealthStatus.UNHEALTHY for report in source):
            return HealthStatus.UNHEALTHY
        if any(report.status is HealthStatus.DEGRADED for report in source):
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY
