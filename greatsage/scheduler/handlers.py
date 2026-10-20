"""Tick job handlers (roadmap Phase C).

Factories build the per-kind executors the runtime wires into
`SchedulerService`. Handlers return `(ok, summary)`; they never raise —
errors become failed runs. All disk writes stay inside the configured
allowed roots.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from greatsage.memory.models import MemoryType
from greatsage.scheduler.models import Schedule
from greatsage.tools.models import ToolRequest
from greatsage.tools.pathsecurity import canonicalize, is_protected_path, is_within

log = logging.getLogger("greatsage.scheduler.handlers")


def make_tool_handler(tool_service: Any) -> Callable[[Schedule], tuple[bool, str]]:
    """Execute `tool` schedules through the Phase 4 security pipeline."""

    def handle(schedule: Schedule) -> tuple[bool, str]:
        payload = schedule.payload
        tool_id = str(payload.get("tool_id", "")).strip()
        arguments = payload.get("arguments", {})
        if not tool_id:
            return False, "tool schedule missing payload.tool_id"
        if not isinstance(arguments, dict):
            return False, "tool schedule payload.arguments must be an object"
        request = ToolRequest(
            request_id=f"sch_{schedule.id}_{uuid.uuid4().hex[:8]}",
            tool_id=tool_id,
            arguments=arguments,
            source="scheduler",
        )
        try:
            result = tool_service.execute(request)
        except Exception as exc:
            return False, f"tool execution error: {exc}"[:200]
        if result.success:
            return True, f"tool {tool_id} ok"
        return False, f"tool {tool_id} failed: {result.error or 'unknown'}"[:200]

    return handle


def make_briefing_handler(
    *,
    memory: Any,
    task: Any,
    planning: Any,
    overall_health: Callable[[], str],
    allowed_roots: tuple[Path, ...],
    denied_roots: tuple[Path, ...],
    working_directory: Path,
) -> Callable[[Schedule], tuple[bool, str]]:
    """Snapshot health + episodic + open tasks/plans; optionally write JSON."""

    def handle(schedule: Schedule) -> tuple[bool, str]:
        try:
            days = int(schedule.payload.get("days", 1))
        except (TypeError, ValueError):
            return False, "briefing payload.days must be an integer"
        days = max(1, min(days, 30))
        try:
            cutoff = datetime.now(UTC) - timedelta(days=days)
            # Only the episode count is reported: fetch a small bounded page.
            episodes = memory.retrieve(
                memory_type=MemoryType.EPISODIC, created_after=cutoff, limit=10
            )
            ep_total = episodes.total
            tasks = task.list(limit=50)
            open_tasks = [t for t in tasks if t.get("state") in ("pending", "running", "paused")]
            plans = planning.list()
            open_plans = [p for p in plans if p.get("status") in ("draft", "ready", "running")]
            snapshot = {
                "schedule_id": schedule.id,
                "name": schedule.name,
                "days": days,
                "overall": overall_health(),
                "episodic_count": ep_total,
                "open_tasks": len(open_tasks),
                "open_plans": len(open_plans),
            }
            out = schedule.payload.get("out")
            if out:
                _write_snapshot(str(out), snapshot, allowed_roots, denied_roots, working_directory)
            summary = (
                f"briefing: {ep_total} episodic, {len(open_tasks)} tasks, "
                f"{len(open_plans)} plans open"
            )
            return True, summary
        except Exception as exc:
            return False, f"briefing error: {exc}"[:200]

    return handle


def _write_snapshot(
    out: str,
    snapshot: dict[str, Any],
    allowed_roots: tuple[Path, ...],
    denied_roots: tuple[Path, ...],
    working_directory: Path,
) -> None:
    """Write the briefing JSON inside allowed roots (deny otherwise)."""
    if not out.strip():
        raise ValueError("empty briefing out path")
    target = canonicalize(out, working_directory)
    if any(is_within(target, root) for root in denied_roots):
        raise ValueError(f"briefing out path inside denied root: {target}")
    if not any(is_within(target, root) for root in allowed_roots):
        raise ValueError(f"briefing out path outside allowed roots: {target}")
    if is_protected_path(target):
        raise ValueError(f"briefing out path is protected: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
