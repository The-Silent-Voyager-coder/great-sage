"""CLI integration tests: telegram health (disabled by default)."""

from __future__ import annotations

from pathlib import Path

import pytest

from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, main


def test_telegram_health_disabled_by_default(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["telegram", "health", "--config", str(valid_config_yaml)])
    assert code == EXIT_FAILURE  # disabled by default config -> available False
    out = capsys.readouterr().out
    assert "Great Sage Telegram Health" in out
    assert "disabled" in out


def test_telegram_health_json_disabled(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["telegram", "health", "--json", "--config", str(valid_config_yaml)])
    assert code == EXIT_FAILURE
    assert '"status": "disabled"' in capsys.readouterr().out


def test_telegram_listen_rejects_bad_budget(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["telegram", "listen", "--for", "0", "--config", str(valid_config_yaml)])
    assert code == EXIT_INVALID
    assert "--for" in capsys.readouterr().err


def test_telegram_listen_unconfigured_reports_failure(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["telegram", "listen", "--once", "--config", str(valid_config_yaml)]
    )
    assert code == EXIT_FAILURE
    assert "telegram" in capsys.readouterr().err


def test_telegram_health_with_invalid_config(
    invalid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["telegram", "health", "--config", str(invalid_config_yaml)])
    assert code == EXIT_INVALID


def test_telegram_screenshot_uses_tool_pipeline(
    valid_config_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from greatsage.cli import _telegram_screenshot_reply
    from greatsage.core.runtime import Runtime
    from greatsage.tools.approval import DeterministicApprovalProvider
    from greatsage.tools.models import ApprovalOutcome

    # Avoid touching the real screen and keep the test valid off Windows.
    monkeypatch.setattr("greatsage.tools.gui_tools._windows_only", lambda _: None)
    monkeypatch.setattr(
        "greatsage.tools.gui_tools.capture_primary_monitor_bmp",
        lambda: (4, 3, b"BMPIX"),
    )
    runtime = Runtime.create(str(valid_config_yaml))
    asyncio.run(runtime.start())
    previous = DeterministicApprovalProvider(ApprovalOutcome.DENIED)
    runtime.tools.approval = previous
    try:
        reply = _telegram_screenshot_reply(runtime, True)
        assert runtime.tools.approval is previous
        assert isinstance(reply, dict)
        assert reply["photo"] == b"BMPIX"
        assert reply["caption"] == "screen 4x3"
        audit = Path(runtime.config.security.audit_log).read_text(encoding="utf-8")
        assert "ToolApprovalRequested" in audit
        assert "ToolApproved" in audit
        assert "gui.screenshot" in audit
        assert "BMPIX" not in audit
    finally:
        asyncio.run(runtime.stop())
