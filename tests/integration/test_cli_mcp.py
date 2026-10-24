"""CLI tests for the local MCP server surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from greatsage.cli import EXIT_INVALID, EXIT_OK, main


def test_mcp_tools_lists_read_only_allowlist(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["mcp", "tools", "--config", str(valid_config_yaml)])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "Great Sage MCP Tools" in captured.out
    assert "greatsage_system_info" in captured.out
    assert "greatsage_filesystem_read" in captured.out


def test_mcp_tools_json(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["mcp", "tools", "--json", "--config", str(valid_config_yaml)])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert '"name": "greatsage_system_info"' in captured.out


def test_mcp_tools_disabled(
    valid_config_yaml: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    disabled = tmp_path / "mcp-disabled.yaml"
    disabled.write_text(
        valid_config_yaml.read_text(encoding="utf-8") + "\nmcp:\n  enabled: false\n",
        encoding="utf-8",
    )
    code = main(["mcp", "tools", "--config", str(disabled)])
    captured = capsys.readouterr()
    assert code != EXIT_OK
    assert "disabled" in captured.err


def test_mcp_serve_requires_stdio(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["mcp", "serve", "--config", str(valid_config_yaml)])
    captured = capsys.readouterr()
    assert code == EXIT_INVALID
    assert "only --stdio" in captured.err
