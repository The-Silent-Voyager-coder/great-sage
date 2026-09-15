"""GUI tools (roadmap: laptop control foundation).

`gui.screenshot` (MEDIUM — screenshots are secret-dense, always ASK-gated),
`gui.click` and `gui.type` (HIGH — they act on whatever is on screen).
Windows-only, standard library only (ctypes); every other platform fails
closed with a clear error instead of crashing.

Security posture (docs/SECURITY_MODEL.md §11):
- Screenshots and their pixels never enter audit events or long-term memory
  (only dimensions/byte counts); an `out` file copy requires an explicit
  path inside allowed roots and honors protected-file rules.
- Click/type go through the normal ASK gate like any HIGH tool; there is no
  silent auto-play path. Game automation additionally needs the operator's
  judgment about the game's own automation rules.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
from pathlib import Path
from typing import Any

from greatsage.tools.models import BaseTool, ToolCategory, ToolContext, ToolResult, ToolRisk

_MAX_TYPE_CHARS = 500


def _windows_only(tool_id: str) -> ToolResult | None:
    if sys.platform != "win32":
        return ToolResult(
            request_id="", tool_id=tool_id, success=False,
            error=f"{tool_id} requires Windows (current platform: {sys.platform})",
        )
    return None


def _capture_bmp() -> tuple[int, int, bytes]:
    """Capture the primary monitor; returns (width, height, BMP file bytes)."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise OSError("windll is not available on this platform")
    user32 = windll.user32
    gdi32 = windll.gdi32
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    if width <= 0 or height <= 0:
        raise OSError("could not determine screen dimensions")
    src_dc = user32.GetDC(None)
    if not src_dc:
        raise OSError("GetDC failed")
    try:
        mem_dc = gdi32.CreateCompatibleDC(src_dc)
        if not mem_dc:
            raise OSError("CreateCompatibleDC failed")
        try:
            bitmap = gdi32.CreateCompatibleBitmap(src_dc, width, height)
            if not bitmap:
                raise OSError("CreateCompatibleBitmap failed")
            try:
                old = gdi32.SelectObject(mem_dc, bitmap)
                try:
                    if not gdi32.BitBlt(mem_dc, 0, 0, width, height, src_dc, 0, 0, 0x00CC0020):
                        raise OSError("BitBlt failed")
                    stride = ((width * 3 + 3) // 4) * 4
                    size = stride * height
                    buffer = ctypes.create_string_buffer(size)

                    class _Info(ctypes.Structure):
                        _fields_ = [
                            ("size", ctypes.c_uint32),
                            ("width", ctypes.c_int32),
                            ("height", ctypes.c_int32),
                            ("planes", ctypes.c_uint16),
                            ("bit_count", ctypes.c_uint16),
                            ("compression", ctypes.c_uint32),
                            ("size_image", ctypes.c_uint32),
                            ("x_pels", ctypes.c_int32),
                            ("y_pels", ctypes.c_int32),
                            ("clr_used", ctypes.c_uint32),
                            ("clr_important", ctypes.c_uint32),
                        ]

                    info = _Info(ctypes.sizeof(_Info), width, -height, 1, 24, 0, size, 0, 0, 0, 0)
                    lines = gdi32.GetDIBits(
                        mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0
                    )
                    if not lines:
                        raise OSError("GetDIBits failed")
                    pixels = bytes(buffer.raw)
                finally:
                    gdi32.SelectObject(mem_dc, old)
            finally:
                gdi32.DeleteObject(bitmap)
        finally:
            gdi32.DeleteDC(mem_dc)
    finally:
        user32.ReleaseDC(None, src_dc)
    file_size = 14 + 40 + size
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 24, 0, size, 0, 0, 0, 0)
    return width, height, header + dib + pixels


def _click_at(x: int, y: int, right: bool) -> None:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise OSError("windll is not available on this platform")
    user32 = windll.user32
    if not user32.SetCursorPos(x, y):
        raise OSError(f"SetCursorPos({x}, {y}) failed")
    time.sleep(0.05)
    down, up = (0x0008, 0x0010) if right else (0x0002, 0x0004)
    user32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(up, 0, 0, 0, 0)


def _type_text(text: str, enter: bool) -> None:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise OSError("windll is not available on this platform")
    user32 = windll.user32
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    VK_RETURN = 0x0D
    for char in text:
        user32.keybd_event(0, 0, KEYEVENTF_UNICODE, ord(char))
        user32.keybd_event(0, 0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, ord(char))
    if enter:
        user32.keybd_event(VK_RETURN, 0, 0, 0)
        user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)


class GuiScreenshotTool(BaseTool):
    id = "gui.screenshot"
    name = "GUI screenshot"
    description = "Capture the primary monitor (metadata unless saved; ASK-gated)."
    version = "1.0.0"
    risk_level = ToolRisk.MEDIUM
    category = ToolCategory.GUI
    capabilities = ("observe",)
    PATH_ARGUMENTS = ("out",)
    input_schema = {
        "type": "object",
        "properties": {"out": {"type": "string"}},
        "required": [],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "bytes": {"type": "integer"},
            "saved_to": {"type": "string"},
        },
        "required": ["width", "height", "bytes"],
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gated = _windows_only(self.id)
        if gated is not None:
            return gated
        out = arguments.get("out")
        if out is not None and (not isinstance(out, str) or not out.strip()):
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error="gui.screenshot out must be a non-empty path")
        try:
            width, height, bmp = _capture_bmp()
        except OSError as exc:
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error=f"gui.screenshot capture failed: {exc}")
        output: dict[str, Any] = {"width": width, "height": height, "bytes": len(bmp)}
        if out:
            # Path policy (allowed/denied roots, protected files) is enforced
            # by the pipeline before execute(); resolve defensively anyway.
            target = Path(out.strip())
            if not target.is_absolute():
                target = context.working_directory / target
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bmp)
            except OSError as exc:
                return ToolResult(request_id="", tool_id=self.id, success=False,
                                  error=f"gui.screenshot could not save: {exc}")
            output["saved_to"] = str(target)
        return ToolResult(request_id="", tool_id=self.id, success=True, output=output)


class GuiClickTool(BaseTool):
    id = "gui.click"
    name = "GUI click"
    description = "Click at screen coordinates (acts on the machine; always ASK-gated)."
    version = "1.0.0"
    risk_level = ToolRisk.HIGH
    category = ToolCategory.GUI
    capabilities = ("act",)
    input_schema = {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string"},
        },
        "required": ["x", "y"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string"},
        },
        "required": ["x", "y", "button"],
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gated = _windows_only(self.id)
        if gated is not None:
            return gated
        x, y = arguments.get("x"), arguments.get("y")
        if isinstance(x, bool) or not isinstance(x, int):
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error="gui.click x must be an integer")
        if isinstance(y, bool) or not isinstance(y, int):
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error="gui.click y must be an integer")
        if not 0 <= x <= 7680 or not 0 <= y <= 4320:
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error="gui.click coordinates out of plausible range")
        button = str(arguments.get("button", "left")).strip().lower()
        if button not in ("left", "right"):
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error="gui.click button must be left or right")
        try:
            _click_at(x, y, right=button == "right")
        except OSError as exc:
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error=f"gui.click failed: {exc}")
        return ToolResult(
            request_id="", tool_id=self.id, success=True,
            output={"x": x, "y": y, "button": button},
        )


class GuiTypeTool(BaseTool):
    id = "gui.type"
    name = "GUI type"
    description = "Type text into the focused window (acts on the machine; always ASK-gated)."
    version = "1.0.0"
    risk_level = ToolRisk.HIGH
    category = ToolCategory.GUI
    capabilities = ("act",)
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "enter": {"type": "boolean"},
        },
        "required": ["text"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "typed_chars": {"type": "integer"},
            "enter": {"type": "boolean"},
        },
        "required": ["typed_chars", "enter"],
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gated = _windows_only(self.id)
        if gated is not None:
            return gated
        text = arguments.get("text")
        if not isinstance(text, str) or not text:
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error="gui.type requires non-empty text")
        if len(text) > _MAX_TYPE_CHARS:
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error=f"gui.type text capped at {_MAX_TYPE_CHARS} chars")
        enter = arguments.get("enter", False)
        if not isinstance(enter, bool):
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error="gui.type enter must be a boolean")
        try:
            _type_text(text, enter)
        except OSError as exc:
            return ToolResult(request_id="", tool_id=self.id, success=False,
                              error=f"gui.type failed: {exc}")
        return ToolResult(
            request_id="", tool_id=self.id, success=True,
            output={"typed_chars": len(text), "enter": enter},
        )
