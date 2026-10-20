"""Great Sage HUD data models (Phase 10).

Read-only snapshot types for the local-first status/dashboard. The HUD
never carries memory content, prompts, secrets, or diffs — only counts,
states, and metadata already exposed by the per-subsystem health/list
commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID_SECTIONS: tuple[str, ...] = (
    "health",
    "memory",
    "agent",
    "delegation",
    "workspace",
    "planning",
    "task",
)

DEFAULT_LIMIT = 5
MAX_LIMIT = 50


@dataclass(frozen=True)
class SectionSnapshot:
    """One HUD section: either data or a degradation note, never both."""

    name: str
    available: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "available": self.available}
        if self.available:
            payload["data"] = self.data
        else:
            payload["error"] = self.error or "unavailable"
        return payload


@dataclass(frozen=True)
class HudSnapshot:
    """Full read-only dashboard snapshot."""

    version: str
    overall: str
    components: list[dict[str, str]] = field(default_factory=list)
    sections: dict[str, SectionSnapshot] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "overall": self.overall,
            "components": list(self.components),
            "sections": {name: section.to_dict() for name, section in self.sections.items()},
        }
