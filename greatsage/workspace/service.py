"""Workspace service facade (Phase 6)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import WORKSPACE_SCAN_FAILED, WORKSPACE_SCANNED, Event
from greatsage.exceptions import WorkspaceUnavailableError, WorkspaceValidationError
from greatsage.workspace.repository import WorkspaceRepository
from greatsage.workspace.scanner import WorkspaceScanner
from greatsage.workspace.sqlite_repository import SqliteWorkspaceRepository

log = logging.getLogger("greatsage.workspace.service")


class WorkspaceService:
    """Facade over workspace scanner + persistence."""

    def __init__(
        self,
        *,
        tools: Any | None = None,
    ) -> None:
        self._tools = tools
        self._config: SageConfig | None = None
        self._scanner: WorkspaceScanner | None = None
        self._repository: WorkspaceRepository | None = None
        self._availability: str = "unavailable"
        self._detail: str = "workspace service not started"
        self.publisher: Any = None

    def start(self, config: SageConfig | None = None) -> None:
        self._config = config
        if config is None:
            self._availability = "unavailable"
            self._detail = "no configuration provided"
            return
        if not config.workspace.enabled:
            self._availability = "disabled"
            self._detail = "workspace subsystem disabled by configuration"
            return
        try:
            # Scanner uses tool path security roots
            self._scanner = WorkspaceScanner(
                allowed_roots=tuple(config.tools.allowed_roots),
                denied_roots=tuple(config.tools.denied_roots),
                working_directory=config.tools.working_directory,
                max_scan_depth=config.workspace.max_scan_depth,
                max_entries=config.workspace.max_entries,
                scan_timeout_seconds=config.workspace.scan_timeout_seconds,
            )
            repo = SqliteWorkspaceRepository(config.workspace.database_path)
            repo.initialize()
            self._repository = repo
            self._availability = "healthy"
            self._detail = f"workspace ready (scan_depth<={config.workspace.max_scan_depth}, entries<={config.workspace.max_entries})"  # noqa: E501
            log.info("workspace service started", extra={"component": "workspace"})
        except Exception as exc:  # pragma: no cover
            self._availability = "unavailable"
            self._detail = f"workspace start failed: {exc}"
            log.error("workspace start failed", exc_info=exc, extra={"component": "workspace"})

    def shutdown(self) -> None:
        if self._repository is not None:
            try:
                self._repository.close()
            except Exception:
                pass
        self._repository = None
        self._scanner = None
        self._availability = "disabled"
        self._detail = "workspace service stopped"
        log.info("workspace service stopped", extra={"component": "workspace"})

    def _require_available(self) -> None:
        if self._availability != "healthy":
            raise WorkspaceUnavailableError(self._detail or "workspace service not available")
        assert self._scanner is not None
        assert self._repository is not None

    def scan(self, path: str | Path | None = None) -> dict[str, Any]:
        self._require_available()
        assert self._scanner is not None
        assert self._repository is not None
        try:
            info = self._scanner.scan(path)
            info.validate()
            self._repository.save(info)
            self._publish(WORKSPACE_SCANNED, {"workspace_id": info.id, "root": str(info.root), "project_type": info.project_type.value})  # noqa: E501
            return info.to_dict()
        except WorkspaceValidationError as exc:
            self._publish(WORKSPACE_SCAN_FAILED, {"error": str(exc)[:200], "path": str(path or "")})
            raise
        except Exception as exc:
            self._publish(WORKSPACE_SCAN_FAILED, {"error": str(exc)[:200]})
            raise WorkspaceValidationError(str(exc)) from exc

    def info(self, workspace_id: str | None = None, root: str | Path | None = None) -> dict[str, Any] | None:  # noqa: E501
        self._require_available()
        assert self._repository is not None
        if workspace_id:
            info = self._repository.get(workspace_id)
            return info.to_dict() if info else None
        if root is not None:
            from greatsage.tools.pathsecurity import canonicalize

            assert self._config is not None
            canonical = canonicalize(str(root), self._config.tools.working_directory)
            info = self._repository.get_by_root(canonical)
            return info.to_dict() if info else None
        # latest
        items = self._repository.list()
        return items[0].to_dict() if items else None

    def list(self) -> list[dict[str, Any]]:
        self._require_available()
        assert self._repository is not None
        return [info.to_dict() for info in self._repository.list()]

    def health(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "available": self._availability == "healthy",
            "status": self._availability,
            "detail": self._detail,
            "enabled": bool(self._config is not None and self._config.workspace.enabled),
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

        registry.register("workspace", checker, "workspace scanner (bounded)")

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(Event(type=event_type, source="workspace", payload=payload))
        except Exception as exc:  # pragma: no cover
            log.warning("workspace event publish failed: %s", exc, extra={"component": "workspace"})
