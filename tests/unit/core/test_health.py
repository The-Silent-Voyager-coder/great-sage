"""Health registry tests: component checks and overall aggregation."""

from __future__ import annotations

from greatsage.core.health import HealthRegistry, HealthStatus


def test_overall_healthy_when_all_healthy() -> None:
    registry = HealthRegistry()
    registry.register("a", lambda: HealthStatus.HEALTHY)
    registry.register("b", lambda: HealthStatus.HEALTHY)
    assert registry.overall() is HealthStatus.HEALTHY


def test_overall_degraded_prefers_degraded() -> None:
    registry = HealthRegistry()
    registry.register("a", lambda: HealthStatus.HEALTHY)
    registry.register("b", lambda: HealthStatus.DEGRADED)
    assert registry.overall() is HealthStatus.DEGRADED


def test_overall_unhealthy_wins() -> None:
    registry = HealthRegistry()
    registry.register("a", lambda: HealthStatus.HEALTHY)
    registry.register("b", lambda: HealthStatus.DEGRADED)
    registry.register("c", lambda: HealthStatus.UNHEALTHY)
    assert registry.overall() is HealthStatus.UNHEALTHY


def test_check_returns_report_with_detail() -> None:
    registry = HealthRegistry()
    registry.register("storage", lambda: HealthStatus.HEALTHY, "root writable")
    report = registry.check("storage")
    assert report.component == "storage"
    assert report.status is HealthStatus.HEALTHY
    assert report.detail == "root writable"
    assert report.checked_at is not None


def test_checker_exception_becomes_unhealthy() -> None:
    registry = HealthRegistry()

    def broken() -> HealthStatus:
        raise RuntimeError("probe boom")

    registry.register("broken", broken, "probe detail")
    report = registry.check("broken")
    assert report.status is HealthStatus.UNHEALTHY
    assert "probe boom" in report.detail


def test_check_all_continues_after_checker_exception() -> None:
    registry = HealthRegistry()

    def broken() -> HealthStatus:
        raise RuntimeError("probe boom")

    registry.register("broken", broken)
    registry.register("healthy", lambda: HealthStatus.HEALTHY)
    reports = registry.check_all()
    assert [report.component for report in reports] == ["broken", "healthy"]
    assert reports[0].status is HealthStatus.UNHEALTHY
    assert reports[1].status is HealthStatus.HEALTHY
    assert registry.overall(reports) is HealthStatus.UNHEALTHY


def test_components_sorted() -> None:
    registry = HealthRegistry()
    registry.register("zulu", lambda: HealthStatus.HEALTHY)
    registry.register("alpha", lambda: HealthStatus.HEALTHY)
    assert registry.components == ("alpha", "zulu")
