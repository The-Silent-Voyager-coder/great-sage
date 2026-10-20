"""Voice limits: conservative defaults and absolute safety ceilings.

Phase 6 turns are bounded in audio bytes, text length, listen duration,
synthesis length, and per-session turn count. Overrides below the
defaults are honored; anything above a ceiling is clamped down to it
(the smaller limit wins — a runtime invariant, mirroring
greatsage.agent.limits and greatsage.vision.limits).
"""

from __future__ import annotations

from dataclasses import dataclass

LISTEN_TIMEOUT_SECONDS_DEFAULT = 15.0
LISTEN_TIMEOUT_SECONDS_CEILING = 120.0

MAX_AUDIO_BYTES_DEFAULT = 1024 * 1024  # 1 MiB
MAX_AUDIO_BYTES_CEILING = 8 * 1024 * 1024  # 8 MiB

MAX_TEXT_CHARS_DEFAULT = 2000
MAX_TEXT_CHARS_CEILING = 8000

MAX_SYNTH_CHARS_DEFAULT = 2000
MAX_SYNTH_CHARS_CEILING = 8000

MAX_TURNS_PER_SESSION_DEFAULT = 25
MAX_TURNS_PER_SESSION_CEILING = 100

WAKE_KEYWORD_DEFAULT = "great sage"
MAX_WAKE_TEXT_CHARS_DEFAULT = 500
MAX_WAKE_TEXT_CHARS_CEILING = 4000

#: Synthesis wall-clock budget (piper on CPU is slow; separate from listen).
SYNTH_TIMEOUT_SECONDS_DEFAULT = 30.0
SYNTH_TIMEOUT_SECONDS_CEILING = 300.0


@dataclass(frozen=True)
class VoiceLimits:
    """Effective bounded limits for one voice service instance."""

    listen_timeout_seconds: float = LISTEN_TIMEOUT_SECONDS_DEFAULT
    max_audio_bytes: int = MAX_AUDIO_BYTES_DEFAULT
    max_text_chars: int = MAX_TEXT_CHARS_DEFAULT
    max_synth_chars: int = MAX_SYNTH_CHARS_DEFAULT
    max_turns_per_session: int = MAX_TURNS_PER_SESSION_DEFAULT
    wake_keyword: str = WAKE_KEYWORD_DEFAULT
    max_wake_text_chars: int = MAX_WAKE_TEXT_CHARS_DEFAULT
    synth_timeout_seconds: float = SYNTH_TIMEOUT_SECONDS_DEFAULT


def default_limits() -> VoiceLimits:
    """Conservative defaults; safe to use without configuration."""
    return VoiceLimits()


def resolve_limits(
    *,
    listen_timeout_seconds: float | None = None,
    max_audio_bytes: int | None = None,
    max_text_chars: int | None = None,
    max_synth_chars: int | None = None,
    max_turns_per_session: int | None = None,
    wake_keyword: str | None = None,
    max_wake_text_chars: int | None = None,
    synth_timeout_seconds: float | None = None,
) -> VoiceLimits:
    """Resolve overrides against ceilings; the smaller limit always wins."""
    defaults = default_limits()
    keyword = (wake_keyword or defaults.wake_keyword).strip().lower()
    if not keyword:
        keyword = defaults.wake_keyword
    return VoiceLimits(
        listen_timeout_seconds=min(
            listen_timeout_seconds
            if listen_timeout_seconds is not None
            else defaults.listen_timeout_seconds,
            LISTEN_TIMEOUT_SECONDS_CEILING,
        ),
        max_audio_bytes=min(
            max_audio_bytes if max_audio_bytes is not None else defaults.max_audio_bytes,
            MAX_AUDIO_BYTES_CEILING,
        ),
        max_text_chars=min(
            max_text_chars if max_text_chars is not None else defaults.max_text_chars,
            MAX_TEXT_CHARS_CEILING,
        ),
        max_synth_chars=min(
            max_synth_chars if max_synth_chars is not None else defaults.max_synth_chars,
            MAX_SYNTH_CHARS_CEILING,
        ),
        max_turns_per_session=min(
            max_turns_per_session
            if max_turns_per_session is not None
            else defaults.max_turns_per_session,
            MAX_TURNS_PER_SESSION_CEILING,
        ),
        wake_keyword=keyword,
        max_wake_text_chars=min(
            max_wake_text_chars
            if max_wake_text_chars is not None
            else defaults.max_wake_text_chars,
            MAX_WAKE_TEXT_CHARS_CEILING,
        ),
        synth_timeout_seconds=min(
            synth_timeout_seconds
            if synth_timeout_seconds is not None
            else defaults.synth_timeout_seconds,
            SYNTH_TIMEOUT_SECONDS_CEILING,
        ),
    )
