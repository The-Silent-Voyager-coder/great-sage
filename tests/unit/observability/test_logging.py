"""Observability tests: JSON format, correlation, redaction, file logging."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from greatsage.configuration.model import LoggingConfig
from greatsage.observability.logging import (
    JsonFormatter,
    correlation,
    current_correlation,
    flush_logging,
    setup_logging,
)


def _make_record(message: str, logger: str = "greatsage.test", **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name=logger,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_produces_json_with_required_fields() -> None:
    line = JsonFormatter().format(_make_record("hello world"))
    entry = json.loads(line)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "greatsage.test"
    assert entry["message"] == "hello world"
    assert entry["component"] == "test"
    assert entry["timestamp"]
    assert entry["event_id"] is None
    assert entry["session_id"] is None
    assert entry["task_id"] is None


def test_extra_component_and_context_attrs() -> None:
    line = JsonFormatter().format(
        _make_record("boot", component="core.runtime", event_id="e1", session_id="s1", task_id="t1")
    )
    entry = json.loads(line)
    assert entry["component"] == "core.runtime"
    assert entry["event_id"] == "e1"
    assert entry["session_id"] == "s1"
    assert entry["task_id"] == "t1"


def test_correlation_context_inherited_by_logs() -> None:
    logger = logging.getLogger("greatsage.correlate")
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler: logging.Handler = Capture()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        with correlation(session_id="sess-1", task_id="task-7", event_id="evt-3"):
            logger.info("inside task")
        logger.info("outside task")
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 2
    inside = json.loads(captured[0])
    assert inside["session_id"] == "sess-1"
    assert inside["task_id"] == "task-7"
    assert inside["event_id"] == "evt-3"
    outside = json.loads(captured[1])
    assert outside["session_id"] is None
    assert outside["task_id"] is None
    assert outside["event_id"] is None


def test_current_correlation_roundtrip() -> None:
    with correlation(task_id="abc"):
        ids = current_correlation()
        assert ids.task_id == "abc"
        assert ids.session_id is None
    assert current_correlation().task_id is None


def test_secret_keys_scrubbed_in_extras() -> None:
    line = JsonFormatter().format(
        _make_record("payload", api_key="sk-super-secret", password="hunter2", safe="visible")
    )
    entry = json.loads(line)
    assert entry["api_key"] == "***"
    assert entry["password"] == "***"
    assert entry["safe"] == "visible"


def test_secret_nested_values_scrubbed() -> None:
    line = JsonFormatter().format(
        _make_record(
            "nested",
            config={"token": "abc123", "something": "ok", "inner": {"password": "zzz"}},
        )
    )
    entry = json.loads(line)
    assert entry["config"]["token"] == "***"
    assert entry["config"]["something"] == "ok"
    entry = json.loads(line)
    assert entry["config"]["token"] == "***"
    assert entry["config"]["something"] == "ok"
    assert entry["config"]["inner"]["password"] == "***"


def test_url_userinfo_masked() -> None:
    line = JsonFormatter().format(_make_record("endpoint", endpoint="http://user:hunter2@127.0.0.1:4096/x"))
    entry = json.loads(line)
    assert "hunter2" not in json.dumps(entry)
    assert "***" in entry["endpoint"]


def test_exception_serialized() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="greatsage.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failure", args=(), exc_info=__import__("sys").exc_info(),
        )
    entry = json.loads(JsonFormatter().format(record))
    assert "boom" in entry["exception"]


def test_setup_logging_writes_json_file(tmp_path: Path) -> None:
    cfg = LoggingConfig(level="DEBUG", format="json", retention_days=2)
    setup_logging(cfg, tmp_path)
    logger = logging.getLogger("greatsage.filecheck")
    logger.info("wrote to file", extra={"component": "test"})
    flush_logging()

    log_file = tmp_path / "sage.log"
    assert log_file.is_file()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["message"] == "wrote to file"


def test_console_handler_added_and_level_applied(tmp_path: Path) -> None:
    setup_logging(LoggingConfig(level="WARNING", format="json", retention_days=1), tmp_path)
    logger = logging.getLogger("greatsage.consolecheck")
    assert logger.isEnabledFor(logging.WARNING)
    assert not logger.isEnabledFor(logging.INFO)
