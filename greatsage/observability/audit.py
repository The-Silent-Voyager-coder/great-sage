"""Append-only audit sink (Phase 9 hardening, completed trust spine).

The security model promises an append-only audit file that cannot be
disabled by an agent. This module is the writer: the runtime subscribes it to
the event bus, and every event is appended as one JSON line with
secret-shaped strings redacted and prompt/content payloads removed.

Writes are synchronous and flushed before returning. The file is opened in
append mode and never truncated; rotation/archival is an operator concern.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from greatsage.events.models import Event
from greatsage.tools.redaction import redact_secrets

log = logging.getLogger("greatsage.observability.audit")

# Payload keys that carry prompt/content/diff data rather than audit metadata.
_DROP_KEYS = frozenset(
    {
        "arguments",
        "body",
        "content",
        "diff",
        "prompt",
        "raw",
        "response",
        "stderr",
        "stdout",
        "transcript",
    }
)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {
            str(key): _clean(item)
            for key, item in value.items()
            if str(key).lower() not in _DROP_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


class AuditLog:
    """Append-only JSONL audit writer safe to call from sync or async code."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        self._closed = False

    def write(self, event: Event) -> None:
        payload = _clean(event.payload)
        record: dict[str, Any] = {
            "timestamp": event.timestamp.isoformat(),
            "event_id": event.id,
            "event": event.type,
            "source": event.source,
            "session_id": event.session_id,
            "task_id": event.task_id,
            "action": event.type,
            "decision": payload.get("decision") if isinstance(payload, dict) else None,
            "risk_level": payload.get("risk_level") if isinstance(payload, dict) else None,
            "error": payload.get("error") if isinstance(payload, dict) else None,
            "duration_ms": payload.get("duration_ms") if isinstance(payload, dict) else None,
            "payload": payload,
        }
        line = json.dumps(record, ensure_ascii=True, default=str)
        with self._lock:
            if self._closed:
                return
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handle.close()
            log.debug("audit log closed", extra={"component": "observability"})
