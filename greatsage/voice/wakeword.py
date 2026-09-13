"""Wake-word matching (Phase 6 + fuzzy variant).

`keyword`: exact substring match (stdlib, always available).
`fuzzy`: exact match plus difflib tolerance for STT mishearings
("jervis", "jarvis please" fragments) with the alias list below.
Neither needs audio DSP, models, or network.
"""

from __future__ import annotations

import difflib
import logging
import re
from abc import ABC, abstractmethod

from greatsage.exceptions import VoiceValidationError
from greatsage.voice.limits import VoiceLimits, default_limits
from greatsage.voice.models import VoiceBackend, WakeResult, redact_text

log = logging.getLogger("greatsage.voice.wakeword")

FUZZY_RATIO = 0.78
WAKE_ALIASES: tuple[str, ...] = (
    "grayt sage",
    "grate sage",
    "great stage",
    "great sedge",
    "great siege",
    "grey sage",
    "grate stage",
    "great sag",
    "great sage",
    "gray sage",
)


class WakeWordDetector(ABC):
    """Pluggable wake-word source; callers never touch audio directly."""

    name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def check(self, text: str) -> WakeResult:
        """Check one bounded text window for the wake keyword."""
        ...


class KeywordWakeDetector(WakeWordDetector):
    """Case-insensitive substring match; always available, zero-cost."""

    name = "keyword"

    def __init__(
        self,
        keyword: str = "great sage",
        *,
        limits: VoiceLimits | None = None,
    ) -> None:
        cleaned = (keyword or "").strip().lower()
        if not cleaned:
            raise VoiceValidationError("wake keyword must not be empty")
        self._keyword = cleaned
        self._limits = limits or default_limits()

    @property
    def keyword(self) -> str:
        return self._keyword

    def is_available(self) -> bool:
        return True

    def check(self, text: str) -> WakeResult:
        """Match the keyword inside a bounded window (chars + word boundary)."""
        if not isinstance(text, str):
            raise VoiceValidationError("wake input must be a string")
        if not text.strip():
            raise VoiceValidationError("wake input must not be empty")
        if len(text) > self._limits.max_wake_text_chars:
            raise VoiceValidationError(
                f"wake input is {len(text)} chars, over the "
                f"{self._limits.max_wake_text_chars}-char limit"
            )
        lowered = text.lower()
        detected = self._keyword in lowered
        result = WakeResult(
            detected=detected,
            keyword=self._keyword,
            confidence=1.0 if detected else 0.0,
            backend=VoiceBackend.MOCK,
        )
        result.validate()
        log.debug(
            "wake check: detected=%s",
            detected,
            extra={"component": "voice", "keyword": self._keyword},
        )
        _ = redact_text(text)
        return result


class FuzzyWakeDetector(WakeWordDetector):
    """Keyword match tolerant to STT mishearings (stdlib difflib)."""

    name = "fuzzy"

    def __init__(
        self,
        keyword: str = "great sage",
        *,
        limits: VoiceLimits | None = None,
        aliases: tuple[str, ...] = WAKE_ALIASES,
        ratio: float = FUZZY_RATIO,
    ) -> None:
        cleaned = (keyword or "").strip().lower()
        if not cleaned:
            raise VoiceValidationError("wake keyword must not be empty")
        self._keyword = cleaned
        self._aliases = tuple(a.strip().lower() for a in aliases if a.strip())
        self._ratio = ratio
        self._limits = limits or default_limits()

    @property
    def keyword(self) -> str:
        return self._keyword

    def is_available(self) -> bool:
        return True

    def check(self, text: str) -> WakeResult:
        """Exact match (1.0) else best alias/token fuzzy ratio above threshold."""
        if not isinstance(text, str):
            raise VoiceValidationError("wake input must be a string")
        if not text.strip():
            raise VoiceValidationError("wake input must not be empty")
        if len(text) > self._limits.max_wake_text_chars:
            raise VoiceValidationError(
                f"wake input is {len(text)} chars, over the "
                f"{self._limits.max_wake_text_chars}-char limit"
            )
        lowered = text.lower()
        clean = re.sub(r"[^a-z0-9 ]+", "", lowered)
        if self._keyword in clean:
            confidence = 1.0
        else:
            candidates = [self._keyword, *self._aliases]
            tokens = clean.split()
            best = 0.0
            for candidate in candidates:
                if candidate in clean:
                    best = 1.0
                    break
                for token in tokens:
                    score = difflib.SequenceMatcher(None, candidate, token).ratio()
                    if score > best:
                        best = score
            confidence = best if best >= self._ratio else 0.0
        result = WakeResult(
            detected=confidence > 0.0,
            keyword=self._keyword,
            confidence=round(confidence, 3),
            backend=VoiceBackend.OFFLINE,
        )
        result.validate()
        log.debug(
            "fuzzy wake check: detected=%s",
            result.detected,
            extra={"component": "voice", "keyword": self._keyword},
        )
        _ = redact_text(text)
        return result
