"""System information tool (spec §23-24).

system.info reports OS / CPU / RAM / storage / GPU / Great Sage version using the
read-only collectors from the Phase 2 benchmark. It never exposes the
environment, user names, API keys, tokens, or any other credential.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from greatsage.intelligence.benchmark import (
    collect_cpu,
    collect_gpu,
    collect_memory,
    collect_platform,
)
from greatsage.tools.models import BaseTool, ToolCategory, ToolContext, ToolResult, ToolRisk


def _sage_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("great-sage")
    except PackageNotFoundError:  # pragma: no cover - non-installed checkout
        from greatsage import __version__

        return __version__


def _storage_summary() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    if sys.platform == "win32":
        import string

        for letter in string.ascii_uppercase:
            mount = f"{letter}:\\"
            try:
                if not Path(mount).exists():
                    continue
                usage = shutil.disk_usage(mount)
            except OSError:
                continue
            summaries.append(
                {
                    "mount": mount,
                    "total_bytes": usage.total,
                    "free_bytes": usage.free,
                }
            )
    else:
        try:
            usage = shutil.disk_usage("/")
            summaries.append(
                {
                    "mount": "/",
                    "total_bytes": usage.total,
                    "free_bytes": usage.free,
                }
            )
        except OSError:
            summaries = []
    return summaries


class SystemInfoTool(BaseTool):
    id = "system.info"
    name = "System information"
    description = "OS, CPU, RAM, storage, GPU, and Great Sage version (read-only)."
    version = "1.0.0"
    risk_level = ToolRisk.SAFE
    category = ToolCategory.SYSTEM
    capabilities = ("inspect",)
    input_schema = {"type": "object", "properties": {}, "required": []}
    output_schema = {
        "type": "object",
        "properties": {
            "sage_version": {"type": "string"},
            "platform": {"type": "object"},
            "cpu": {"type": "object"},
            "memory": {"type": "object"},
            "storage": {"type": "array"},
            "gpu": {"type": "array"},
        },
        "required": ["sage_version", "platform", "cpu", "memory", "gpu"],
    }

    def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        gpus = collect_gpu()
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={
                "sage_version": _sage_version(),
                "platform": collect_platform(),
                "cpu": collect_cpu(),
                "memory": collect_memory(),
                "storage": _storage_summary(),
                "gpu": gpus,
                "gpu_detectable": bool(gpus),
            },
        )
