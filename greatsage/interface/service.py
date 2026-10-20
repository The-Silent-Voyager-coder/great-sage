"""Great Sage HUD service (Phase 10).

Read-only aggregator over the existing subsystem facades. The HUD is a
leaf consumer (ARCHITECTURE.md §4): it calls only health/stats/list
read paths, never run/execute/scan/delete, and never bypasses the
security pipeline (it performs no actions at all).

Failure isolation: every section is collected independently. A degraded
subsystem yields ``available: False`` with an error note — it never
fails the whole snapshot.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from greatsage import __version__
from greatsage.exceptions import HudUnavailableError, HudValidationError
from greatsage.interface.models import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    VALID_SECTIONS,
    HudSnapshot,
    SectionSnapshot,
)

if TYPE_CHECKING:
    from greatsage.core.runtime import Runtime

log = logging.getLogger("greatsage.interface.service")

# Delegation entries may carry large diffs; the HUD shows metadata only.
_DELEGATION_KEEP = (
    "task_id",
    "request_id",
    "session_id",
    "provider",
    "state",
    "reason",
    "error",
    "elapsed_ms",
    "duration_ms",
    "output_bytes",
    "permission_requests",
    "files_changed",
    "diff_available",
)


def _project(item: dict[str, Any], keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    if keys is None:
        return dict(item)
    return {key: item[key] for key in keys if key in item}


class HudService:
    """Stateless read-only HUD aggregator (no lifecycle of its own)."""

    def snapshot(
        self,
        runtime: Runtime,
        *,
        limit: int = DEFAULT_LIMIT,
        sections: tuple[str, ...] | list[str] | None = None,
    ) -> HudSnapshot:
        """Assemble a dashboard snapshot from live subsystem read paths."""
        validated_limit = self._validate_limit(limit)
        wanted = self._validate_sections(sections)
        collected: dict[str, SectionSnapshot] = {}
        for name in wanted:
            reader = self._readers()[name]
            try:
                collected[name] = SectionSnapshot(
                    name=name, available=True, data=reader(runtime, validated_limit)
                )
            except Exception as exc:  # failure isolation: degrade, never raise
                log.warning(
                    "hud section degraded",
                    extra={"component": "interface", "section": name, "error": str(exc)},
                )
                collected[name] = SectionSnapshot(name=name, available=False, error=str(exc))
        overall = self._overall(collected)
        components = self._components(collected)
        return HudSnapshot(
            version=__version__, overall=overall, components=components, sections=collected
        )

    def status(self, runtime: Runtime) -> dict[str, Any]:
        """Compact status: overall health plus the component table only."""
        snapshot = self.snapshot(runtime, sections=["health"])
        section = snapshot.sections["health"]
        return {
            "version": snapshot.version,
            "overall": snapshot.overall,
            "components": snapshot.components,
            "health_available": section.available,
            "health_error": section.error,
        }

    # --- validation ----------------------------------------------------

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise HudValidationError(f"limit must be an integer 1..{MAX_LIMIT}")
        if not 1 <= limit <= MAX_LIMIT:
            raise HudValidationError(f"limit must be an integer 1..{MAX_LIMIT}")
        return limit

    @staticmethod
    def _validate_sections(
        sections: tuple[str, ...] | list[str] | None,
    ) -> tuple[str, ...]:
        if sections is None:
            return VALID_SECTIONS
        unknown = [name for name in sections if name not in VALID_SECTIONS]
        if unknown:
            raise HudValidationError(
                f"unknown hud section(s): {unknown} (expected subset of {list(VALID_SECTIONS)})"
            )
        seen: list[str] = []
        for name in sections:
            if name not in seen:
                seen.append(name)
        if not seen:
            raise HudValidationError("at least one hud section is required")
        return tuple(seen)

    # --- aggregation helpers -------------------------------------------

    @staticmethod
    def _readers() -> dict[str, Any]:
        return {
            "health": HudService._read_health,
            "memory": HudService._read_memory,
            "agent": HudService._read_agent,
            "delegation": HudService._read_delegation,
            "workspace": HudService._read_workspace,
            "planning": HudService._read_planning,
            "task": HudService._read_task,
        }

    @staticmethod
    def _overall(collected: dict[str, SectionSnapshot]) -> str:
        health = collected.get("health")
        if health is not None and health.available:
            overall = str(health.data.get("overall", "unknown")).upper()
            if overall in ("HEALTHY", "DEGRADED", "UNHEALTHY"):
                return overall
        degraded = [name for name, section in collected.items() if not section.available]
        if degraded:
            return "DEGRADED"
        return "HEALTHY"

    @staticmethod
    def _components(collected: dict[str, SectionSnapshot]) -> list[dict[str, str]]:
        health = collected.get("health")
        if health is None or not health.available:
            return []
        components = health.data.get("components", [])
        return [dict(entry) for entry in components]

    # --- section readers (read-only subsystem paths only) --------------

    @staticmethod
    def _read_health(runtime: Runtime, limit: int) -> dict[str, Any]:
        try:
            reports = runtime.health_report()
        except Exception as exc:
            raise HudUnavailableError(f"runtime health not available: {exc}") from exc
        overall = str(runtime.overall_health().value)
        return {
            "overall": overall,
            "components": [
                {
                    "component": report.component,
                    "status": report.status.value,
                    "detail": report.detail,
                }
                for report in reports
            ],
        }

    @staticmethod
    def _read_memory(runtime: Runtime, limit: int) -> dict[str, Any]:
        stats = runtime.memory.stats()
        summary: dict[str, Any] = {
            "total": stats.get("total", 0),
            "by_type": dict(stats.get("by_type", {})),
            "expired": stats.get("expired", 0),
            "deleted": stats.get("deleted", 0),
            "fts_enabled": stats.get("fts_enabled", False),
            "schema_version": stats.get("schema_version", 0),
        }
        return summary

    @staticmethod
    def _read_agent(runtime: Runtime, limit: int) -> dict[str, Any]:
        return dict(runtime.agent.health())

    @staticmethod
    def _read_delegation(runtime: Runtime, limit: int) -> dict[str, Any]:
        tasks = runtime.delegation.list_tasks(limit)
        return {
            "count": len(tasks),
            "tasks": [_project(task, _DELEGATION_KEEP) for task in tasks[:limit]],
        }

    @staticmethod
    def _read_workspace(runtime: Runtime, limit: int) -> dict[str, Any]:
        items = runtime.workspace.list()
        return {"count": len(items), "workspaces": [_project(item) for item in items[:limit]]}

    @staticmethod
    def _read_planning(runtime: Runtime, limit: int) -> dict[str, Any]:
        plans = runtime.planning.list()
        return {"count": len(plans), "plans": [_project(plan) for plan in plans[:limit]]}

    @staticmethod
    def _read_task(runtime: Runtime, limit: int) -> dict[str, Any]:
        tasks = runtime.task.list(limit=limit)
        return {"count": len(tasks), "tasks": [_project(task) for task in tasks[:limit]]}
