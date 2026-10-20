"""Structured JSON logging with correlation IDs and secret redaction.

All Great Sage logs are machine-readable JSON on stdout and to
`<logs_dir>/sage.log` (rotating). Correlation IDs (session/task/event) are
carried in context variables so logs produced inside a task are correlated
automatically. Secret-shaped values are masked before serialization as a
defense in depth — logging code must still never log credentials.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from greatsage.configuration.model import LoggingConfig

LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|password|passwd|token|secret|credential|authorization)"
)
_URL_USERINFO_RE = re.compile(r"(://)([^/@\s]+)(@)")
_MASK = "***"

# LogRecord attributes that are always present; anything else on the record is
# treated as user-supplied extra context.
_BASE_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)
_CONTEXT_ATTRS = frozenset({"component", "event_id", "session_id", "task_id"})

# --- correlation context -----------------------------------------------

_session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sage_session_id", default=None
)
_task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sage_task_id", default=None
)
_event_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sage_event_id", default=None
)


@dataclass(frozen=True)
class CorrelationIds:
    session_id: str | None = None
    task_id: str | None = None
    event_id: str | None = None


def current_correlation() -> CorrelationIds:
    return CorrelationIds(
        session_id=_session_id_var.get(),
        task_id=_task_id_var.get(),
        event_id=_event_id_var.get(),
    )


@contextmanager
def correlation(
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    event_id: str | None = None,
) -> Iterator[None]:
    """Bind correlation IDs for the duration of a block; logs inside inherit them."""
    tokens: list[tuple[contextvars.ContextVar[Any], contextvars.Token[Any]]] = []
    for var, value in (
        (_session_id_var, session_id),
        (_task_id_var, task_id),
        (_event_id_var, event_id),
    ):
        if value is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)


# --- redaction ---------------------------------------------------------


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _MASK if _SECRET_KEY_RE.search(str(key)) else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _URL_USERINFO_RE.sub(r"\1" + _MASK + r"\3", value)
    return value


def _scrub(record: logging.LogRecord) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _BASE_ATTRS or key in _CONTEXT_ATTRS:
            continue
        if isinstance(value, str) and _SECRET_KEY_RE.search(key):
            result[key] = _MASK
        else:
            result[key] = _sanitize(value)
    return result


# --- JSON formatter ----------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Serialize log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        correlation_ids = current_correlation()
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "component": record.__dict__.get("component") or self._component_from_name(record.name),
            "event_id": record.__dict__.get("event_id") or correlation_ids.event_id,
            "session_id": record.__dict__.get("session_id") or correlation_ids.session_id,
            "task_id": record.__dict__.get("task_id") or correlation_ids.task_id,
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        entry.update(_scrub(record))
        return json.dumps(entry, ensure_ascii=True, default=str)

    @staticmethod
    def _component_from_name(name: str) -> str:
        if name.startswith("greatsage."):
            return name[len("greatsage.") :]
        return name


# --- setup -------------------------------------------------------------


def setup_logging(
    cfg: LoggingConfig,
    logs_dir: Path | None = None,
    *,
    console: bool = True,
) -> None:
    """Configure the `greatsage` logger: JSON console + rotating JSON file."""
    logger = logging.getLogger("greatsage")
    logger.setLevel(LOG_LEVELS.get(cfg.level, logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(JsonFormatter())
        logger.addHandler(stream)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        backup_count = max(1, cfg.retention_days)
        file_handler = RotatingFileHandler(
            logs_dir / "sage.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)


def flush_logging() -> None:
    """Flush every handler attached to the `greatsage` logger."""
    logger = logging.getLogger("greatsage")
    for handler in logger.handlers:
        handler.flush()
