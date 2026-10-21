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
