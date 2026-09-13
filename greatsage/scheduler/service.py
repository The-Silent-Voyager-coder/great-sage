"""Scheduler service facade (roadmap Phase C)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from greatsage.configuration.model import JarvisConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import (
    SCHEDULE_CREATED,
    SCHEDULE_FAILED,
    SCHEDULE_REMOVED,
    SCHEDULE_RUN,
    Event,
)
from greatsage.exceptions import SchedulerUnavailableError, SchedulerValidationError
from greatsage.scheduler.limits import MAX_SCHEDULES_DEFAULT
from greatsage.scheduler.models import Schedule, ScheduleKind
from greatsage.scheduler.repository import SchedulerRepository
from greatsage.scheduler.sqlite_repository import SqliteSchedulerRepository

log = logging.getLogger("greatsage.scheduler.service")

# Handler: schedule -> (ok, summary). Wired by the runtime/CLI per kind.
ScheduleHandler = Callable[[Schedule], tuple[bool, str]]


class SchedulerService:
    """Facade over schedule persistence + bounded tick execution."""

    def __init__(self) -> None:
        self._config: JarvisConfig | None = None
        self._repository: SchedulerRepository | None = None
        self._availability: str = "unavailable"
        self._detail: str = "scheduler service not started"
        self.publisher: Any = None
        self._handlers: dict[str, ScheduleHandler] = {}

    def start(self, config: JarvisConfig | None = None) -> None:
        self._config = config
        if config is None:
            self._availability = "unavailable"
            self._detail = "no configuration provided"
            return
        if not config.scheduler.enabled:
            self._availability = "disabled"
            self._detail = "scheduler subsystem disabled by configuration"
            return
        try:
            repo = SqliteSchedulerRepository(config.scheduler.database_path)
            repo.initialize()
            self._repository = repo
            self._availability = "healthy"
            self._detail = (
                f"scheduler ready (max_schedules<={config.scheduler.max_schedules})"
            )
            log.info("scheduler service started", extra={"component": "scheduler"})
        except Exception as exc:
            self._availability = "unavailable"
            self._detail = f"scheduler start failed: {exc}"
            log.error("scheduler start failed", exc_info=exc, extra={"component": "scheduler"})

    def shutdown(self) -> None:
        if self._repository is not None:
            try:
                self._repository.close()
            except Exception:
                pass
        self._repository = None
        self._availability = "disabled"
        self._detail = "scheduler service stopped"
        log.info("scheduler service stopped", extra={"component": "scheduler"})

    def register_handler(self, kind: ScheduleKind | str, handler: ScheduleHandler) -> None:
        """Register the executor for one schedule kind (runtime wires these)."""
        key = kind.value if isinstance(kind, ScheduleKind) else str(kind)
        self._handlers[key] = handler

    def _require_available(self) -> SchedulerRepository:
        if self._availability != "healthy" or self._repository is None:
            raise SchedulerUnavailableError(self._detail or "scheduler service not available")
        return self._repository

    def _max_schedules(self) -> int:
        if self._config is not None:
            return self._config.scheduler.max_schedules
        return MAX_SCHEDULES_DEFAULT

    def add(
        self,
        *,
        name: str,
        kind: ScheduleKind | str,
        interval_seconds: int,
        payload: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        repo = self._require_available()
        try:
            kind_enum = kind if isinstance(kind, ScheduleKind) else ScheduleKind(str(kind))
        except ValueError as exc:
            raise SchedulerValidationError(f"unknown schedule kind: {kind!r}") from exc
        if repo.count() >= self._max_schedules():
            raise SchedulerValidationError(
                f"schedule limit reached ({self._max_schedules()})"
            )
        now = datetime.now(UTC)
        schedule = Schedule(
            name=name,
            kind=kind_enum,
            interval_seconds=interval_seconds,
            payload=dict(payload or {}),
            enabled=enabled,
            created_at=now,
            next_run_at=now,
        )
        schedule.validate()
        repo.save(schedule)
        self._publish(SCHEDULE_CREATED, {
            "schedule_id": schedule.id, "name": schedule.name,
            "kind": schedule.kind.value,
        })
        return schedule.to_dict()

    def remove(self, schedule_id: str) -> dict[str, Any]:
        repo = self._require_available()
        if not schedule_id.strip():
            raise SchedulerValidationError("schedule id must not be empty")
        removed = repo.remove(schedule_id)
        if removed:
            self._publish(SCHEDULE_REMOVED, {"schedule_id": schedule_id})
        return {"removed": removed, "schedule_id": schedule_id}

    def list(self) -> list[dict[str, Any]]:
        repo = self._require_available()
        return [s.to_dict() for s in repo.list()]

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """Run every due, enabled schedule once. Bounded: one pass, no chains."""
        repo = self._require_available()
        at = now or datetime.now(UTC)
        ran: list[dict[str, Any]] = []
        for schedule in repo.list():
            if not schedule.enabled or schedule.next_run_at > at:
                continue
            handler = self._handlers.get(schedule.kind.value)
            if handler is None:
                status, summary = False, f"no handler for kind {schedule.kind.value}"
            else:
                try:
                    status, summary = handler(schedule)
                except Exception as exc:  # handler failure is a failed run, not a crash
                    status, summary = False, f"handler error: {exc}"[:200]
            updated = Schedule(
                id=schedule.id,
                name=schedule.name,
                kind=schedule.kind,
                interval_seconds=schedule.interval_seconds,
                payload=schedule.payload,
                enabled=schedule.enabled,
                created_at=schedule.created_at,
                last_run_at=at,
                next_run_at=at + timedelta(seconds=schedule.interval_seconds),
                last_status="ok" if status else "failed",
                last_summary=summary[:200],
            )
            repo.save(updated)
            run = {"schedule_id": schedule.id, "name": schedule.name, "ok": status}
            ran.append(run)
            self._publish(
                SCHEDULE_RUN if status else SCHEDULE_FAILED,
                {"schedule_id": schedule.id, "name": schedule.name,
                 "summary": summary[:200]},
            )
        return {"ran": ran, "ran_count": len(ran)}

    def health(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "available": self._availability == "healthy",
            "status": self._availability,
            "detail": self._detail,
            "enabled": bool(self._config is not None and self._config.scheduler.enabled),
        }
        if self._repository is not None:
            h = self._repository.health()
            base.update({
                "accessible": h.accessible,
                "schema_valid": h.schema_valid,
                "migrations_current": h.migrations_current,
                "writable": h.writable,
                "schema_version": h.schema_version,
                "database_path": h.database_path,
            })
        return base

    def register_health_check(self, registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self._availability == "disabled":
                return HealthStatus.HEALTHY
            if self._availability == "healthy":
                return HealthStatus.HEALTHY
            return HealthStatus.UNHEALTHY

        registry.register("scheduler", checker, "scheduler (bounded ticks)")

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(Event(type=event_type, source="scheduler", payload=payload))
        except Exception as exc:  # pragma: no cover
            log.warning("scheduler event publish failed: %s", exc, extra={"component": "scheduler"})


def default_database_path() -> Path:
    return Path("C:/GREATSAGE/data/sage-scheduler.db")
