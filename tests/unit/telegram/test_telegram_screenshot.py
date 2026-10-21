"""Telegram screenshot approval and tool-pipeline tests (offline)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from greatsage.cli import _telegram_screenshot_reply
from greatsage.telegram.approval import TelegramScreenshotApprovalProvider
from greatsage.tools.approval import DeterministicApprovalProvider
from greatsage.tools.filesystem_tools import FilesystemWriteTool
from greatsage.tools.gui_tools import GuiScreenshotTool
from greatsage.tools.models import ApprovalOutcome, ToolRequest, ToolResult


class _RecordingTools:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.approval = DeterministicApprovalProvider(ApprovalOutcome.DENIED)
        self.requests: list[ToolRequest] = []

    def execute(self, request: ToolRequest) -> ToolResult:
        self.requests.append(request)
        out = Path(request.arguments["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"BMPIX")
        return ToolResult(
            request_id=request.request_id,
            tool_id=request.tool_id,
            success=True,
            output={"width": 8, "height": 6, "saved_to": str(out)},
        )


def _runtime(tmp_path: Path) -> tuple[Any, _RecordingTools]:
    tools = _RecordingTools(tmp_path / "workspaces")
    config = SimpleNamespace(
        tools=SimpleNamespace(working_directory=tmp_path / "workspaces")
    )
    return SimpleNamespace(config=config, tools=tools), tools


def test_screenshot_uses_tool_service_and_restores_approval(tmp_path: Path) -> None:
    runtime, tools = _runtime(tmp_path)
    previous = tools.approval

    reply = _telegram_screenshot_reply(runtime, True)

    assert reply == {"photo": b"BMPIX", "caption": "screen 8x6"}
    assert len(tools.requests) == 1
    request = tools.requests[0]
    assert request.tool_id == "gui.screenshot"
    assert request.source == "telegram"
    assert tools.approval is previous


def test_screenshot_requires_explicit_content_opt_in(tmp_path: Path) -> None:
    runtime, tools = _runtime(tmp_path)

    reply = _telegram_screenshot_reply(runtime, False)

    assert isinstance(reply, str)
    assert "listen --content" in reply
    assert tools.requests == []


def test_provider_is_scoped_to_telegram_screenshots() -> None:
    provider = TelegramScreenshotApprovalProvider()
    screenshot = GuiScreenshotTool()
    other = FilesystemWriteTool()
    request = ToolRequest(
        request_id="req-1",
        tool_id="gui.screenshot",
        arguments={},
        source="telegram",
    )

    assert (
        provider.request_approval(request, screenshot, "ask")
        is ApprovalOutcome.APPROVED
    )
    assert (
        provider.request_approval(request, other, "ask")
        is ApprovalOutcome.DENIED
    )
    assert (
        provider.request_approval(
            ToolRequest(
                request_id="req-2",
                tool_id="gui.screenshot",
                arguments={},
                source="ai",
            ),
            screenshot,
            "ask",
        )
        is ApprovalOutcome.DENIED
    )


def test_provider_falls_back_for_other_requests() -> None:
    fallback = DeterministicApprovalProvider(ApprovalOutcome.APPROVED)
    provider = TelegramScreenshotApprovalProvider(fallback)
    request = ToolRequest(
        request_id="req-3",
        tool_id="filesystem.write",
        arguments={},
        source="telegram",
    )

    assert (
        provider.request_approval(request, FilesystemWriteTool(), "ask")
        is ApprovalOutcome.APPROVED
    )
