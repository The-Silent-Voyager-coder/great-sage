"""Read-only views over the append-only audit log.

The audit sink writes one redacted JSON object per line. These helpers only
read: they never rotate, truncate, rewrite, or create the audit file. Output is
redacted again as defense in depth for logs written by older versions.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from greatsage.tools.redaction import redact_secrets

TAIL_DEFAULT = 20
TAIL_MAX = 200
SEARCH_DEFAULT = 50
SEARCH_MAX = 500
QUERY_MAX = 200


class AuditLogError(RuntimeError):
    """The audit log could not be read."""


def _path(path: str | Path) -> Path:
    return Path(path)


def _redact(line: str) -> str:
    return redact_secrets(line) or ""


def _parse(line: str) -> dict[str, Any]:
    raw = _redact(line.strip())
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": raw}
    return value if isinstance(value, dict) else {"raw": raw}


def _tail_lines(path: Path, limit: int) -> list[str]:
    """Read the last ``limit`` lines without loading the whole log."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        if end == 0:
            return []
        chunk_size = 64 * 1024
        data = b""
        position = end
        while position > 0 and data.count(b"\n") <= limit:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + data
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-limit:]


def tail_audit(path: str | Path, *, lines: int = TAIL_DEFAULT) -> list[dict[str, Any]]:
    if lines < 1 or lines > TAIL_MAX:
        raise AuditLogError(f"lines must be 1..{TAIL_MAX}")
    log_path = _path(path)
    if not log_path.is_file():
        raise AuditLogError(f"audit log not found: {log_path}")
    try:
        raw_lines = _tail_lines(log_path, lines)
    except OSError as exc:
        raise AuditLogError(f"could not read audit log: {exc}") from exc
    return [_parse(line) for line in raw_lines if line.strip()]


def search_audit(
    path: str | Path, query: str, *, limit: int = SEARCH_DEFAULT
) -> list[dict[str, Any]]:
    if not query or not query.strip():
        raise AuditLogError("search query must not be empty")
    if len(query) > QUERY_MAX:
        raise AuditLogError(f"search query must be at most {QUERY_MAX} characters")
    if limit < 1 or limit > SEARCH_MAX:
        raise AuditLogError(f"limit must be 1..{SEARCH_MAX}")
    log_path = _path(path)
    if not log_path.is_file():
        raise AuditLogError(f"audit log not found: {log_path}")
    needle = query.casefold()
    matches: deque[str] = deque(maxlen=limit)
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if needle in _redact(line).casefold():
                    matches.append(line)
    except OSError as exc:
        raise AuditLogError(f"could not read audit log: {exc}") from exc
    # Newest matches first; the deque keeps only the most recent ones.
    return [_parse(line) for line in reversed(matches) if line.strip()]


def describe(entry: dict[str, Any]) -> str:
    """One bounded text line for CLI display."""
    if "raw" in entry:
        return str(entry["raw"])[:400]
    parts = [
        str(entry.get("timestamp", "-")),
        str(entry.get("event", entry.get("action", "-"))),
        f"source={entry.get('source', '-')}",
    ]
    if entry.get("session_id"):
        parts.append(f"session={entry['session_id']}")
    if entry.get("task_id"):
        parts.append(f"task={entry['task_id']}")
    if entry.get("decision"):
        parts.append(f"decision={entry['decision']}")
    if entry.get("risk_level"):
        parts.append(f"risk={entry['risk_level']}")
    if entry.get("error"):
        parts.append(f"error={str(entry['error'])[:160]}")
    return "  ".join(parts)
