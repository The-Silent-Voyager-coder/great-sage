"""Process tool tests (spec §21-22, §39)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from greatsage.exceptions import ToolExecutionError
from greatsage.tools.models import ToolContext
from greatsage.tools.process_tools import ProcessInfoTool, ProcessListTool


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        working_directory=tmp_path,
        environment={},
        timeout_seconds=30.0,
        max_output_bytes=65536,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="process tools require Windows (tasklist)")
def test_process_list_returns_entries(tmp_path: Path) -> None:
    result = ProcessListTool().execute({}, make_context(tmp_path))
    assert result.success
    assert "processes" in result.output
    assert any(process["pid"] == os.getpid() for process in result.output["processes"])
    for process in result.output["processes"]:
        assert "name" in process and "pid" in process and "memory_bytes" in process


@pytest.mark.skipif(sys.platform != "win32", reason="process tools require Windows (tasklist)")
def test_process_info_current_pid_running(tmp_path: Path) -> None:
    result = ProcessInfoTool().execute({"pid": os.getpid()}, make_context(tmp_path))
    assert result.success
    assert result.output["running"] is True
    assert result.output["pid"] == os.getpid()


@pytest.mark.skipif(sys.platform != "win32", reason="process tools require Windows (tasklist)")
def test_process_info_unknown_pid_not_running(tmp_path: Path) -> None:
    result = ProcessInfoTool().execute({"pid": 999999999}, make_context(tmp_path))
    assert result.success
    assert result.output["running"] is False


def test_process_info_negative_pid_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="invalid pid"):
        ProcessInfoTool().execute({"pid": -1}, make_context(tmp_path))


def test_no_terminate_tool_exists() -> None:
    from greatsage.tools.defaults import DEFAULT_TOOL_CLASSES

    assert not any("terminate" in cls.id for cls in DEFAULT_TOOL_CLASSES)
    assert not any("kill" in cls.id for cls in DEFAULT_TOOL_CLASSES)


def test_process_tools_declare_observational_capabilities() -> None:
    from greatsage.tools.process_tools import ProcessInfoTool, ProcessListTool

    assert ProcessListTool.capabilities == ("inspect",)
    assert ProcessInfoTool.capabilities == ("inspect",)


@pytest.mark.skipif(sys.platform != "win32", reason="tasklist is Windows-only")
def test_process_list_uses_tasklist_without_shell(tmp_path: Path) -> None:
    import greatsage.tools.process_tools as pt

    pt._list_processes()  # noqa: SLF001 - smoke test for the real command
