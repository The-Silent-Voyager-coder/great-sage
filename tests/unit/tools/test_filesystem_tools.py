"""Filesystem tool tests (spec §18-20, §39)."""

from __future__ import annotations

from pathlib import Path

import pytest

from greatsage.exceptions import ToolExecutionError
from greatsage.tools.filesystem_tools import (
    FilesystemListTool,
    FilesystemMkdirTool,
    FilesystemReadTool,
    FilesystemStatTool,
    FilesystemWriteTool,
)
from greatsage.tools.models import ToolContext, ToolRisk


def make_context(tmp_path: Path, max_bytes: int = 65536) -> ToolContext:
    return ToolContext(
        working_directory=tmp_path,
        environment={"PATH": "C:/Windows/System32"},
        timeout_seconds=30.0,
        max_output_bytes=max_bytes,
    )


def test_list_non_recursive(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    result = FilesystemListTool().execute({"path": str(tmp_path)}, make_context(tmp_path))
    assert result.success
    names = [entry["name"] for entry in result.output["entries"]]
    assert names == ["a.txt", "sub"]
    sub = next(e for e in result.output["entries"] if e["name"] == "sub")
    assert sub["is_dir"] and sub["size_bytes"] is None


def test_list_recursive(tmp_path: Path) -> None:
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "x.txt").write_text("x")
    result = FilesystemListTool().execute(
        {"path": str(tmp_path), "recursive": True}, make_context(tmp_path)
    )
    names = [entry["name"] for entry in result.output["entries"]]
    assert "x.txt" in names


def test_list_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="does not exist"):
        FilesystemListTool().execute({"path": str(tmp_path / "nope")}, make_context(tmp_path))


def test_list_file_path_raises(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("a")
    with pytest.raises(ToolExecutionError, match="not a directory"):
        FilesystemListTool().execute({"path": str(target)}, make_context(tmp_path))


def test_stat_existing_and_missing(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello")
    stat = FilesystemStatTool()
    result = stat.execute({"path": str(target)}, make_context(tmp_path))
    assert result.output["exists"] is True
    assert result.output["is_file"] is True
    assert result.output["size_bytes"] == 5
    result = stat.execute({"path": str(tmp_path / "missing")}, make_context(tmp_path))
    assert result.output["exists"] is False


def test_read_text_file(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("hello world", encoding="utf-8")
    result = FilesystemReadTool().execute({"path": str(target)}, make_context(tmp_path))
    assert result.success
    assert result.output["content"] == "hello world"
    assert result.output["truncated"] is False


def test_read_respects_refusal_of_binary(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ToolExecutionError, match="binary"):
        FilesystemReadTool().execute({"path": str(target)}, make_context(tmp_path))


def test_read_truncates_to_limit(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("x" * 1000)
    result = FilesystemReadTool().execute(
        {"path": str(target)}, make_context(tmp_path, max_bytes=100)
    )
    assert result.output["truncated"] is True
    assert len(result.output["content"]) == 100


def test_read_rejects_non_positive_max_bytes(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("x" * 1000)
    for value in (0, -1, -1000):
        with pytest.raises(ToolExecutionError, match="positive integer"):
            FilesystemReadTool().execute(
                {"path": str(target), "max_bytes": value}, make_context(tmp_path)
            )


def test_read_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="does not exist"):
        FilesystemReadTool().execute({"path": str(tmp_path / "nope")}, make_context(tmp_path))


def test_mkdir_creates_and_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "new" / "dir"
    result = FilesystemMkdirTool().execute(
        {"path": str(target), "parents": True}, make_context(tmp_path)
    )
    assert result.success and result.output["created"] is True
    assert target.is_dir()
    result = FilesystemMkdirTool().execute(
        {"path": str(target)}, make_context(tmp_path)
    )
    assert result.success and result.output["created"] is False


def test_mkdir_without_parents_fails(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "dir"
    with pytest.raises(ToolExecutionError, match="could not create"):
        FilesystemMkdirTool().execute({"path": str(target)}, make_context(tmp_path))


def test_write_creates_file_atomically(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    result = FilesystemWriteTool().execute(
        {"path": str(target), "content": "data"}, make_context(tmp_path)
    )
    assert result.success
    assert result.output["bytes_written"] == 4
    assert result.output["existed_before"] is False
    assert target.read_text(encoding="utf-8") == "data"


def test_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old")
    result = FilesystemWriteTool().execute(
        {"path": str(target), "content": "new"}, make_context(tmp_path)
    )
    assert result.output["existed_before"] is True
    assert target.read_text(encoding="utf-8") == "new"


def test_write_non_atomic_path(tmp_path: Path) -> None:
    target = tmp_path / "plain.txt"
    result = FilesystemWriteTool().execute(
        {"path": str(target), "content": "x", "atomic": False}, make_context(tmp_path)
    )
    assert result.success
    assert target.read_text(encoding="utf-8") == "x"


def test_write_oversized_content_rejected(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    with pytest.raises(ToolExecutionError, match="tool limit"):
        FilesystemWriteTool().execute(
            {"path": str(target), "content": "y" * 70000},
            make_context(tmp_path, max_bytes=65536),
        )


def test_write_missing_parent_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="parent directory"):
        FilesystemWriteTool().execute(
            {"path": str(tmp_path / "nope" / "f.txt"), "content": "x"},
            make_context(tmp_path),
        )


def test_no_delete_tool_exists() -> None:
    from greatsage.tools.defaults import DEFAULT_TOOL_CLASSES

    ids = {cls.id for cls in DEFAULT_TOOL_CLASSES}
    assert not any("delete" in tool_id or "remove" in tool_id for tool_id in ids)


def test_risk_levels_match_design() -> None:
    assert FilesystemListTool.risk_level is ToolRisk.SAFE
    assert FilesystemStatTool.risk_level is ToolRisk.SAFE
    assert FilesystemReadTool.risk_level is ToolRisk.LOW
    assert FilesystemMkdirTool.risk_level is ToolRisk.LOW
    assert FilesystemWriteTool.risk_level is ToolRisk.MEDIUM


def test_list_cap_is_enforced_in_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("greatsage.tools.filesystem_tools.MAX_LIST_ENTRIES", 5)
    for index in range(10):
        (tmp_path / f"f{index}.txt").write_text("x")
    result = FilesystemListTool().execute({"path": str(tmp_path)}, make_context(tmp_path))
    assert len(result.output["entries"]) == 5
    assert result.metadata.get("truncated") is True
