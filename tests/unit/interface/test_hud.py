"""Unit tests for the Phase 10 HUD (greatsage/interface)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from greatsage import __version__
from greatsage.core.health import HealthReport, HealthStatus
from greatsage.exceptions import HudValidationError
from greatsage.interface.formatting import format_dashboard, format_status
from greatsage.interface.models import VALID_SECTIONS
from greatsage.interface.service import HudService


@dataclass
class _StubHealth:
    reports: list[HealthReport]
    overall: HealthStatus


class _StubService:
    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def _run(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._payload


class _StubMemory(_StubService):
    def stats(self) -> Any:
        return self._run()


class _StubAgent(_StubService):
    def health(self) -> Any:
        return self._run()


class _StubDelegation(_StubService):
    def list_tasks(self, limit: int = 5) -> Any:
        assert limit >= 1
        return self._run()


class _StubWorkspace(_StubService):
    def list(self) -> Any:
        return self._run()


class _StubPlanning(_StubService):
    def list(self) -> Any:
        return self._run()


class _StubTask(_StubService):
    def list(self, limit: int = 5) -> Any:
        assert limit >= 1
        return self._run()


class _StubRuntime:
    def __init__(self) -> None:
        self._reports = [
            HealthReport(component="core", status=HealthStatus.HEALTHY, detail="ok"),
            HealthReport(component="memory", status=HealthStatus.HEALTHY, detail="ok"),
        ]
        self.memory = _StubMemory(
            {"total": 2, "by_type": {"long_term": 2}, "expired": 0,
             "deleted": 0, "fts_enabled": True, "schema_version": 3}
        )
        self.agent = _StubAgent({"available": True, "status": "healthy",
                                 "detail": "ready", "current": {}})
        self.delegation = _StubDelegation(
            [{"task_id": "del_1", "state": "completed",
              "diff": "DIFF-BYTES-MUST-NOT-SHOW", "diff_available": True}]
        )
        self.workspace = _StubWorkspace([{"id": "ws_1", "root": "C:/ws"}])
        self.planning = _StubPlanning([{"plan_id": "plan_1", "goal": "demo"}])
        self.task = _StubTask([{"id": "task_1", "state": "completed"}])

    def health_report(self) -> list[HealthReport]:
        return list(self._reports)

    def overall_health(self) -> HealthStatus:
        return HealthStatus.HEALTHY


def test_snapshot_collects_all_sections() -> None:
    snapshot = HudService().snapshot(_StubRuntime())  # type: ignore[arg-type]
    assert snapshot.version == __version__
    assert snapshot.overall == "HEALTHY"
    assert {entry["component"] for entry in snapshot.components} == {"core", "memory"}
    assert set(snapshot.sections) == set(VALID_SECTIONS)
    assert all(section.available for section in snapshot.sections.values())
    assert snapshot.sections["memory"].data["total"] == 2
    json.dumps(snapshot.to_dict())  # JSON-serializable


def test_snapshot_degrades_failed_section() -> None:
    runtime = _StubRuntime()
    runtime.task = _StubTask(error=RuntimeError("db gone"))  # type: ignore[assignment]
    snapshot = HudService().snapshot(runtime)  # type: ignore[arg-type]
    assert snapshot.sections["task"].available is False
    assert "db gone" in (snapshot.sections["task"].error or "")
    assert snapshot.sections["memory"].available is True


def test_snapshot_never_carries_memory_content_or_diffs() -> None:
    runtime = _StubRuntime()
    snapshot = HudService().snapshot(runtime)  # type: ignore[arg-type]
    blob = json.dumps(snapshot.to_dict())
    assert "DIFF-BYTES-MUST-NOT-SHOW" not in blob
    assert "diff" not in snapshot.sections["delegation"].data["tasks"][0]


def test_snapshot_rejects_unknown_section_and_bad_limit() -> None:
    service = HudService()
    with pytest.raises(HudValidationError):
        service.snapshot(_StubRuntime(), sections=["nope"])  # type: ignore[arg-type]
    with pytest.raises(HudValidationError):
        service.snapshot(_StubRuntime(), limit=0)  # type: ignore[arg-type]
    with pytest.raises(HudValidationError):
        service.snapshot(_StubRuntime(), limit=51)  # type: ignore[arg-type]


def test_snapshot_section_filter_and_limit() -> None:
    snapshot = HudService().snapshot(
        _StubRuntime(), sections=["memory", "task"]  # type: ignore[arg-type]
    )
    assert set(snapshot.sections) == {"memory", "task"}


def test_status_is_compact() -> None:
    payload = HudService().status(_StubRuntime())  # type: ignore[arg-type]
    assert payload["overall"] == "HEALTHY"
    assert len(payload["components"]) == 2
    assert payload["health_available"] is True


def test_formatters_render_headers() -> None:
    snapshot = HudService().snapshot(_StubRuntime())  # type: ignore[arg-type]
    assert "Great Sage Status" in format_status(snapshot)
    dashboard = format_dashboard(snapshot)
    assert "Great Sage Status" in dashboard
    for section in ("memory", "agent", "delegation", "workspace", "planning", "task"):
        assert f"[{section}]" in dashboard


def test_formatters_render_degraded_section() -> None:
    runtime = _StubRuntime()
    runtime.agent = _StubAgent(error=RuntimeError("down"))  # type: ignore[assignment]
    snapshot = HudService().snapshot(runtime)  # type: ignore[arg-type]
    assert "unavailable: down" in format_dashboard(snapshot)
