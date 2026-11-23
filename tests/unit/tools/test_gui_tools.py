"""GUI tool tests (laptop-control foundation).

Live click/type actions are NEVER exercised here (they would hijack the
operator's mouse/keyboard); only validation paths and the platform guard
run. Screenshot capture runs live on Windows and skips cleanly elsewhere.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

from greatsage.tools.gui_tools import (
    GuiClickTool,
    GuiScreenshotTool,
    GuiTypeTool,
    capture_primary_monitor_bmp,
)
from greatsage.tools.models import ToolCategory, ToolContext, ToolRisk
from greatsage.vision.capture import encode_bmp

WIN32 = sys.platform == "win32"


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        working_directory=tmp_path,
        environment={},
        timeout_seconds=5.0,
        max_output_bytes=65536,
    )


def test_declarations() -> None:
    shot = GuiScreenshotTool()
    assert shot.category is ToolCategory.GUI
    assert shot.risk_level is ToolRisk.MEDIUM
    assert "out" in shot.PATH_ARGUMENTS  # path policy applies to saved copies
    click = GuiClickTool()
    assert click.risk_level is ToolRisk.HIGH
    assert GuiTypeTool().risk_level is ToolRisk.HIGH


def test_platform_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert GuiScreenshotTool().execute({}, _context(tmp_path)).success is False
    click_err = GuiClickTool().execute({"x": 1, "y": 1}, _context(tmp_path)).error or ""
    assert "requires Windows" in click_err
    type_err = GuiTypeTool().execute({"text": "hi"}, _context(tmp_path)).error or ""
    assert "requires Windows" in type_err


def test_capture_helper_requires_windll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctypes, "windll", None, raising=False)
    with pytest.raises(OSError, match="windll"):
        capture_primary_monitor_bmp()


@pytest.mark.skipif(not WIN32, reason="Windows-only validation paths")
def test_click_validation(tmp_path: Path) -> None:
    tool = GuiClickTool()
    assert tool.execute({"x": "a", "y": 1}, _context(tmp_path)).success is False
    assert tool.execute({"x": 1, "y": True}, _context(tmp_path)).success is False
    assert tool.execute({"x": -5, "y": 1}, _context(tmp_path)).success is False
    assert tool.execute({"x": 99999, "y": 1}, _context(tmp_path)).success is False
    assert tool.execute({"x": 1, "y": 1, "button": "middle"}, _context(tmp_path)).success is False


@pytest.mark.skipif(not WIN32, reason="Windows-only validation paths")
def test_type_validation(tmp_path: Path) -> None:
    tool = GuiTypeTool()
    assert tool.execute({"text": ""}, _context(tmp_path)).success is False
    assert tool.execute({"text": "x" * 501}, _context(tmp_path)).success is False
    assert tool.execute({"text": "hi", "enter": "yes"}, _context(tmp_path)).success is False


@pytest.mark.skipif(not WIN32, reason="Windows-only capture")
def test_screenshot_live(tmp_path: Path) -> None:
    tool = GuiScreenshotTool()
    try:
        result = tool.execute({}, _context(tmp_path))
    except OSError:
        pytest.skip("no desktop session for capture")
    assert result.success is True
    assert result.output is not None
    assert result.output["width"] > 0 and result.output["height"] > 0
    assert result.output["bytes"] > 1000
    assert "saved_to" not in result.output  # pixels never leak into output

    out = tmp_path / "shots" / "screen.bmp"
    result = tool.execute({"out": str(out)}, _context(tmp_path))
    assert result.success is True
    assert result.output is not None
    assert out.exists() and out.stat().st_size == result.output["bytes"]
    assert result.output["saved_to"] == str(out)


def _changed_frame(width: int = 16, height: int = 10) -> bytes:
    data = bytearray(encode_bmp(width, height))
    stride = ((width * 3 + 3) // 4) * 4
    # One pixel near the top-left in a bottom-up frame.
    offset = 54 + (height - 1) * stride
    data[offset : offset + 3] = b"\xc8\x64\x32"
    return bytes(data)


def _fake_captures(monkeypatch: pytest.MonkeyPatch, frames: list[bytes]):
    monkeypatch.setattr(sys, "platform", "win32")
    iterator = iter(frames)
    return lambda: (16, 10, next(iterator))


def test_click_verify_reports_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = encode_bmp(16, 10)
    after = _changed_frame()
    capture = _fake_captures(monkeypatch, [before, after])
    clicks: list[tuple[int, int, bool]] = []
    waits: list[float] = []
    tool = GuiClickTool(
        capture=capture,
        click=lambda x, y, right: clicks.append((x, y, right)),
        sleep=waits.append,
    )
    result = tool.execute(
        {"x": 5, "y": 6, "verify": True, "wait_ms": 120},
        _context(tmp_path),
    )
    assert result.success is True
    assert result.output is not None
    assert result.output["verified"] is True
    assert result.output["screen_changed"] is True
    assert result.output["change_ratio"] > 0
    assert clicks == [(5, 6, False)]
    assert waits == [0.12]


def test_click_verify_reports_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = encode_bmp(16, 10)
    capture = _fake_captures(monkeypatch, [frame, frame])
    tool = GuiClickTool(capture=capture, click=lambda x, y, right: None, sleep=lambda _s: None)
    result = tool.execute({"x": 1, "y": 1, "verify": True}, _context(tmp_path))
    assert result.success is True
    assert result.output is not None
    assert result.output["screen_changed"] is False
    assert result.output["summary"] == "NOTHING CHANGED"


def test_click_verify_before_capture_failure_does_not_click(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    clicks: list[tuple[int, int, bool]] = []

    def broken_capture():
        raise OSError("no desktop")

    tool = GuiClickTool(
        capture=broken_capture,
        click=lambda x, y, right: clicks.append((x, y, right)),
    )
    result = tool.execute({"x": 1, "y": 1, "verify": True}, _context(tmp_path))
    assert result.success is False
    assert "before action" in (result.error or "")
    assert clicks == []


def test_click_verify_after_capture_failure_is_honest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    calls = {"captures": 0}

    def flaky_capture():
        calls["captures"] += 1
        if calls["captures"] == 1:
            return (16, 10, encode_bmp(16, 10))
        raise OSError("display went away")

    tool = GuiClickTool(
        capture=flaky_capture,
        click=lambda x, y, right: None,
        sleep=lambda _s: None,
    )
    result = tool.execute({"x": 1, "y": 1, "verify": True}, _context(tmp_path))
    assert result.success is True
    assert result.output is not None
    assert result.output["verified"] is False
    assert "after action" in result.output["verify_error"]


def test_type_verify_reports_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture = _fake_captures(monkeypatch, [encode_bmp(16, 10), _changed_frame()])
    typed: list[tuple[str, bool]] = []
    tool = GuiTypeTool(
        capture=capture,
        type_text=lambda text, enter: typed.append((text, enter)),
        sleep=lambda _s: None,
    )
    result = tool.execute(
        {"text": "hello", "enter": True, "verify": True},
        _context(tmp_path),
    )
    assert result.success is True
    assert result.output is not None
    assert result.output["verified"] is True
    assert result.output["screen_changed"] is True
    assert typed == [("hello", True)]


def test_verify_options_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    tool = GuiClickTool(
        capture=lambda: (16, 10, encode_bmp(16, 10)),
        click=lambda x, y, right: None,
    )
    context = _context(tmp_path)
    assert not tool.execute({"x": 1, "y": 1, "verify": "yes"}, context).success
    assert not tool.execute(
        {"x": 1, "y": 1, "verify": True, "wait_ms": -1}, context
    ).success
    assert not tool.execute(
        {"x": 1, "y": 1, "verify": True, "wait_ms": True}, context
    ).success


def test_verify_is_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    def unexpected_capture():
        raise AssertionError("capture must not run without verify=true")

    tool = GuiClickTool(capture=unexpected_capture, click=lambda x, y, right: None)
    result = tool.execute({"x": 1, "y": 1}, _context(tmp_path))
    assert result.success is True
    assert result.output is not None
    assert "verified" not in result.output
