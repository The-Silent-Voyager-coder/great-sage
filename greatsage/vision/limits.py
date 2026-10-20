"""Vision limits: conservative defaults and absolute safety ceilings.

Phase 8 captures are bounded in dimensions, bytes, description length,
region count, and per-session capture count. Overrides below the defaults
are honored; anything above a ceiling is clamped down to it (the smaller
limit wins — a runtime invariant, mirroring greatsage.agent.limits).
"""

from __future__ import annotations

from dataclasses import dataclass

CAPTURE_TIMEOUT_SECONDS_DEFAULT = 10.0
CAPTURE_TIMEOUT_SECONDS_CEILING = 60.0

MAX_IMAGE_BYTES_DEFAULT = 1024 * 1024  # 1 MiB
MAX_IMAGE_BYTES_CEILING = 4 * 1024 * 1024  # 4 MiB

MAX_DESCRIPTION_CHARS_DEFAULT = 4000
MAX_DESCRIPTION_CHARS_CEILING = 16000

MAX_REGIONS_DEFAULT = 8
MAX_REGIONS_CEILING = 16

MAX_CAPTURES_PER_SESSION_DEFAULT = 25
MAX_CAPTURES_PER_SESSION_CEILING = 100

DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 200
MAX_WIDTH = 800
MAX_HEIGHT = 600


def check_bounded(name: str, value: int | float, ceiling: int | float) -> None:
    """Raise ValueError when a limit exceeds its absolute safety ceiling."""
    if value > ceiling:
        raise ValueError(f"{name} must be at most {ceiling}, got {value}")


@dataclass(frozen=True)
class VisionLimits:
    """Effective bounded limits for one vision service instance."""

    capture_timeout_seconds: float = CAPTURE_TIMEOUT_SECONDS_DEFAULT
    max_image_bytes: int = MAX_IMAGE_BYTES_DEFAULT
    max_description_chars: int = MAX_DESCRIPTION_CHARS_DEFAULT
    max_regions: int = MAX_REGIONS_DEFAULT
    max_captures_per_session: int = MAX_CAPTURES_PER_SESSION_DEFAULT
    max_width: int = MAX_WIDTH
    max_height: int = MAX_HEIGHT


def default_limits() -> VisionLimits:
    """Conservative defaults; safe to use without configuration."""
    return VisionLimits()


def resolve_limits(
    *,
    capture_timeout_seconds: float | None = None,
    max_image_bytes: int | None = None,
    max_description_chars: int | None = None,
    max_regions: int | None = None,
    max_captures_per_session: int | None = None,
) -> VisionLimits:
    """Resolve overrides against ceilings; the smaller limit always wins."""
    defaults = default_limits()
    return VisionLimits(
        capture_timeout_seconds=min(
            capture_timeout_seconds
            if capture_timeout_seconds is not None
            else defaults.capture_timeout_seconds,
            CAPTURE_TIMEOUT_SECONDS_CEILING,
        ),
        max_image_bytes=min(
            max_image_bytes if max_image_bytes is not None else defaults.max_image_bytes,
            MAX_IMAGE_BYTES_CEILING,
        ),
        max_description_chars=min(
            max_description_chars
            if max_description_chars is not None
            else defaults.max_description_chars,
            MAX_DESCRIPTION_CHARS_CEILING,
        ),
        max_regions=min(
            max_regions if max_regions is not None else defaults.max_regions,
            MAX_REGIONS_CEILING,
        ),
        max_captures_per_session=min(
            max_captures_per_session
            if max_captures_per_session is not None
            else defaults.max_captures_per_session,
            MAX_CAPTURES_PER_SESSION_CEILING,
        ),
    )
