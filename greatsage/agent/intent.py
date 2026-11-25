"""Deterministic conversational-intent guard for agent runs.

Small local models sometimes propose high-risk tool calls for pure
conversation ("reply hello" -> ``gui.click``). The guard is intentionally
narrow: only short, closed greeting/thanks/identity prompts with no action
marker run without a tool catalog. Any action word keeps the full tool loop
available, so the security pipeline is unchanged either way.
"""

from __future__ import annotations

import re

_MAX_CONVERSATIONAL_CHARS = 160

_CONVERSATIONAL_PATTERNS = (
    re.compile(
        r"^(hi|hello|hey|yo|howdy|good\s+(morning|afternoon|evening)|"
        r"thanks|thank\s+you|ok|okay|cool|nice|great)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(how\s+are\s+you|who\s+are\s+you|"
        r"what(?:'s|\s+is)\s+your\s+name|what\s+can\s+you\s+do)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(reply|respond|say|answer|tell(?:\s+me)?)\b"
        r"[^.?!]{0,60}\b(hello|hi|hey|thanks|ok)\b",
        re.IGNORECASE,
    ),
)

_ACTION_PATTERN = re.compile(
    r"\b(files?|folders?|director(?:y|ies)|paths?|read|write|edit|search|"
    r"find|list|open|close|run|exec(?:ute)?|commands?|shell|terminal|"
    r"process(?:es)?|system|cpu|memory|disk|network|https?|urls?|"
    r"browsers?|screens?|screenshots?|click|type|windows?|apps?|email|"
    r"telegram|schedule|remind(?:er)?|timers?|voice|listen|speak|home|"
    r"lights?|temperature|git|code|build|tests?|python|scripts?|install|"
    r"download|upload)\b",
    re.IGNORECASE,
)


def is_conversational(prompt: str) -> bool:
    """True for a short, closed conversational prompt with no action marker."""
    text = " ".join(prompt.strip().split())
    if not text or len(text) > _MAX_CONVERSATIONAL_CHARS:
        return False
    if _ACTION_PATTERN.search(text):
        return False
    return any(pattern.search(text) for pattern in _CONVERSATIONAL_PATTERNS)


__all__ = ["is_conversational"]
