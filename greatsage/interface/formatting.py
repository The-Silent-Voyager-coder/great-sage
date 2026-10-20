"""Text renderers for the Great Sage HUD (Phase 10).

Plain-stdout tables matching the existing CLI style (``cli.py`` prints
``Great Sage <Subsystem>`` headers). JSON output is produced by
``HudSnapshot.to_dict`` directly; this module covers human-readable text.
"""

from __future__ import annotations

from greatsage.interface.models import HudSnapshot


def format_status(snapshot: HudSnapshot) -> str:
    """Compact status: overall plus the component table."""
    lines = ["Great Sage Status", f"  overall      {snapshot.overall}"]
    if not snapshot.components:
        lines.append("  components   (unavailable)")
    for entry in snapshot.components:
        lines.append(f"  {entry.get('component', '-'):<14} {entry.get('status', '?')}")
    health = snapshot.sections.get("health")
    if health is not None and not health.available:
        lines.append(f"  health note  degraded: {health.error}")
    return "\n".join(lines)


def format_dashboard(snapshot: HudSnapshot) -> str:
    """Full dashboard: status plus one block per collected section."""
    lines = [format_status(snapshot)]
    for name, section in snapshot.sections.items():
        if name == "health":
            continue
        lines.append(f"\n[{name}]")
        if not section.available:
            lines.append(f"  unavailable: {section.error}")
            continue
        lines.extend(_format_section(name, section.data))
    return "\n".join(lines)


def _format_section(name: str, data: dict[str, object]) -> list[str]:
    if name == "memory":
        assert isinstance(data, dict)
        lines = [f"  total        {data.get('total', 0)}"]
        by_type = data.get("by_type")
        if isinstance(by_type, dict):
            for key in sorted(by_type):
                lines.append(f"  {key:<12} {by_type[key]}")
        lines.append(f"  fts          {'enabled' if data.get('fts_enabled') else 'fallback'}")
        return lines
    if name == "agent":
        assert isinstance(data, dict)
        lines = [
            f"  status       {data.get('status', 'unknown')}",
            f"  detail       {data.get('detail') or ''}",
        ]
        current = data.get("current")
        if isinstance(current, dict) and current:
            lines.append(
                f"  current      state={current.get('state')} steps={current.get('steps')} "
                f"tool_calls={current.get('tool_calls')}"
            )
        return lines
    for plural, singular in (
        ("tasks", "task"),
        ("workspaces", "workspace"),
        ("plans", "plan"),
    ):
        if plural in data:
            assert isinstance(data, dict)
            items = data.get(plural)
            count = data.get("count", len(items) if isinstance(items, list) else 0)
            lines = [f"  {plural:<12} {count}"]
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        label = (
                            item.get("task_id")
                            or item.get("id")
                            or item.get("plan_id")
                            or item.get("name")
                            or "-"
                        )
                        state = item.get("state", item.get("status", "-"))
                        lines.append(f"    {label} [{state}]")
            return lines
    assert isinstance(data, dict)
    return [f"  {key:<12} {value}" for key, value in data.items()]
