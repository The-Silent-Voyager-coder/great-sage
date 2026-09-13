"""System.info redaction tests (spec §23-24, §39): never expose secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path

from greatsage.tools.models import ToolContext
from greatsage.tools.system_tools import SystemInfoTool


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        working_directory=tmp_path,
        environment={},
        timeout_seconds=30.0,
        max_output_bytes=65536,
    )


def test_system_info_shape(tmp_path: Path) -> None:
    result = SystemInfoTool().execute({}, make_context(tmp_path))
    assert result.success
    output = result.output
    for key in ("sage_version", "platform", "cpu", "memory", "storage", "gpu",
                "gpu_detectable"):
        assert key in output
    assert output["platform"]["system"] == "Windows"
    assert isinstance(output["cpu"]["logical_cpus"], int)
    assert output["memory"]["total_bytes"] > 0
    assert isinstance(output["storage"], list)


def test_system_info_never_exposes_secrets_or_env(tmp_path: Path) -> None:
    os.environ["GREATSAGE_TEST_SECRET_MARKER"] = "hunter2-unique"
    try:
        result = SystemInfoTool().execute({}, make_context(tmp_path))
        serialized = json.dumps(result.output).lower()
        assert "greatsage_test_secret_marker" not in serialized
        assert "hunter2-unique" not in serialized
        assert "api_key" not in serialized
        assert "token" not in serialized
    finally:
        os.environ.pop("GREATSAGE_TEST_SECRET_MARKER", None)
