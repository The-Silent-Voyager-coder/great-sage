"""Secret redaction for audit-safe strings (Phase 9 hardening).

Audit events, error messages, and summaries must never carry credential
material: a provider exception, a delegated diff, or a user goal can embed a
token that would otherwise land verbatim in ``audit.log`` or the event bus.
`redact_secrets` masks only high-confidence secret *formats* (provider key
prefixes, PEM private-key blocks, JWT-shaped tokens) so ordinary prose —
including words like "password" in documentation — passes through unchanged.

Key-shaped *names* are handled by `greatsage.tools.environment.is_secret_name`;
this module handles secret-shaped *values*.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", re.compile(r"sk-[A-Za-z0-9\-_]{16,}")),
    ("api_key", re.compile(r"sk-(live|test|proj)-[A-Za-z0-9\-_]{8,}")),
    ("token", re.compile(r"gh[pou]_[A-Za-z0-9]{20,}")),
    ("token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}")),
    ("token", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}"),
    ),
)


def redact_secrets(text: str | None) -> str | None:
    """Mask high-confidence secret formats in `text`.

    Returns the input unchanged when it is not a string or contains no
    secret-shaped material. Replacement markers name only the *kind*
    (``[REDACTED:api_key]``), never the matched value.
    """
    if not isinstance(text, str) or not text:
        return text
    redacted = text
    for kind, pattern in _PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{kind}]", redacted)
    return redacted


def looks_like_secret_value(text: str) -> bool:
    """True when `text` contains a high-confidence secret format."""
    if not isinstance(text, str) or not text:
        return False
    return any(pattern.search(text) is not None for _, pattern in _PATTERNS)
