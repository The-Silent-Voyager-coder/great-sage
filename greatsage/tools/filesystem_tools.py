"""Filesystem tools (spec §18-20): read/non-destructive by default.

There is intentionally NO delete tool in Phase 4 (spec §20); writes are
MEDIUM risk, honor the protected-file policy, and use atomic replacement.
All paths are canonicalized and root-checked by the security policy before
reaching these tools.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from greatsage.exceptions import ToolExecutionError
from greatsage.tools.models import BaseTool, ToolContext, ToolResult, ToolRisk

MAX_LIST_ENTRIES = 5000


def _iso(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def _resolve(arguments: dict[str, Any], context: ToolContext) -> Path:
    raw = str(arguments["path"])
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = context.working_directory / candidate
    return candidate.resolve()


def _entry(path: Path, is_dir: bool) -> dict[str, Any]:
    return {
        "name": path.name,
        "path": str(path),
        "is_dir": is_dir,
        "size_bytes": None if is_dir else path.stat().st_size,
        "modified_at": _iso(path.stat().st_mtime),
    }


class FilesystemListTool(BaseTool):
    id = "filesystem.list"
    name = "List directory entries"
    description = "List files and directories inside a path (optionally recursive)."
    version = "1.0.0"
    risk_level = ToolRisk.SAFE
    capabilities = ("list", "read_metadata")
    PATH_ARGUMENTS = ("path",)
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "directory to list"},
            "recursive": {
                "type": "boolean",
                "description": "recurse into subdirectories (default false)",
            },
        },
        "required": ["path"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "entries": {"type": "array"},
        },
        "required": ["path", "entries"],
    }

    def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        path = _resolve(arguments, context)
        if not path.exists():
            raise ToolExecutionError(f"path does not exist: {path}")
        if not path.is_dir():
            raise ToolExecutionError(f"path is not a directory: {path}")
        recursive = bool(arguments.get("recursive", False))
        entries: list[dict[str, Any]] = []
        if recursive:
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    entries.append(_entry(Path(root) / name, False))
                    if len(entries) >= MAX_LIST_ENTRIES:
                        break
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
        else:
            with os.scandir(path) as scanner:
                for item in sorted(scanner, key=lambda entry: entry.name):
                    entries.append(_entry(Path(item.path), item.is_dir()))
                    if len(entries) >= MAX_LIST_ENTRIES:
                        break
        metadata: dict[str, Any] = {}
        if len(entries) >= MAX_LIST_ENTRIES:
            metadata["truncated"] = True
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={"path": str(path), "entries": entries},
            metadata=metadata,
        )


class FilesystemStatTool(BaseTool):
    id = "filesystem.stat"
    name = "File metadata"
    description = "Report existence and metadata for a file or directory."
    version = "1.0.0"
    risk_level = ToolRisk.SAFE
    capabilities = ("read_metadata",)
    PATH_ARGUMENTS = ("path",)
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file or directory to stat"},
        },
        "required": ["path"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "exists": {"type": "boolean"},
        },
        "required": ["path", "exists"],
    }

    def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        path = _resolve(arguments, context)
        if not path.exists():
            return ToolResult(
                request_id="",
                tool_id=self.id,
                success=True,
                output={
                    "path": str(path),
                    "exists": False,
                    "is_dir": False,
                    "is_file": False,
                    "size_bytes": None,
                    "modified_at": None,
                },
            )
        stat = path.stat()
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={
                "path": str(path),
                "exists": True,
                "is_dir": path.is_dir(),
                "is_file": path.is_file(),
                "is_symlink": path.is_symlink(),
                "size_bytes": stat.st_size if path.is_file() else None,
                "modified_at": _iso(stat.st_mtime),
                "created_at": _iso(getattr(stat, "st_ctime", None)),
            },
        )


class FilesystemReadTool(BaseTool):
    id = "filesystem.read"
    name = "Read a text file"
    description = "Read a text file, bounded by the configured output limit."
    version = "1.0.0"
    risk_level = ToolRisk.LOW
    capabilities = ("read",)
    PATH_ARGUMENTS = ("path",)
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file to read"},
            "max_bytes": {
                "type": "integer",
                "description": "optional read cap (defaults to the tool limit)",
            },
        },
        "required": ["path"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "bytes_read": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
        "required": ["path", "content", "bytes_read", "truncated"],
    }

    def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        path = _resolve(arguments, context)
        if not path.exists():
            raise ToolExecutionError(f"path does not exist: {path}")
        if not path.is_file():
            raise ToolExecutionError(f"path is not a file: {path}")
        requested = arguments.get("max_bytes")
        if requested is None:
            limit = context.max_output_bytes
        else:
            try:
                requested_int = int(requested)
            except (TypeError, ValueError) as exc:
                raise ToolExecutionError("max_bytes must be a positive integer") from exc
            if requested_int <= 0:
                raise ToolExecutionError("max_bytes must be a positive integer")
            limit = min(requested_int, context.max_output_bytes)
        # Never let a bad context value turn read() into "read everything":
        # negative sizes are unbounded in Python's file API.
        limit = max(1, limit)
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if b"\x00" in data[:4096]:
            raise ToolExecutionError(f"refusing to read binary file: {path}")
        truncated = len(data) > limit
        content = data[:limit].decode("utf-8", errors="replace")
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={
                "path": str(path),
                "content": content,
                "bytes_read": len(data[:limit]),
                "truncated": truncated,
            },
        )


class FilesystemMkdirTool(BaseTool):
    id = "filesystem.mkdir"
    name = "Create a directory"
    description = "Create a directory tree (idempotent; never removes anything)."
    version = "1.0.0"
    risk_level = ToolRisk.LOW
    capabilities = ("write",)
    PATH_ARGUMENTS = ("path",)
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "directory to create"},
            "parents": {
                "type": "boolean",
                "description": "create missing parents (default false)",
            },
        },
        "required": ["path"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "created": {"type": "boolean"},
        },
        "required": ["path", "created"],
    }

    def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        path = _resolve(arguments, context)
        if path.exists():
            if path.is_dir():
                return ToolResult(
                    request_id="",
                    tool_id=self.id,
                    success=True,
                    output={"path": str(path), "created": False},
                )
            raise ToolExecutionError(f"path exists and is not a directory: {path}")
        parents = bool(arguments.get("parents", False))
        try:
            path.mkdir(parents=parents)
        except OSError as exc:
            raise ToolExecutionError(f"could not create directory {path}: {exc}") from exc
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={"path": str(path), "created": True},
        )


class FilesystemWriteTool(BaseTool):
    id = "filesystem.write"
    name = "Write a text file"
    description = "Write a text file (atomic replace by default; content capped)."
    version = "1.0.0"
    risk_level = ToolRisk.MEDIUM
    capabilities = ("write",)
    PATH_ARGUMENTS = ("path",)
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file to write"},
            "content": {"type": "string", "description": "file content"},
            "atomic": {
                "type": "boolean",
                "description": "write to a temp file and replace (default true)",
            },
        },
        "required": ["path", "content"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "bytes_written": {"type": "integer"},
            "existed_before": {"type": "boolean"},
        },
        "required": ["path", "bytes_written", "existed_before"],
    }

    def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        path = _resolve(arguments, context)
        content = str(arguments["content"])
        encoded = content.encode("utf-8")
        if len(encoded) > context.max_output_bytes:
            raise ToolExecutionError(
                f"content exceeds the {context.max_output_bytes}-byte tool limit "
                f"({len(encoded)} bytes)"
            )
        parent = path.parent
        if not parent.exists():
            raise ToolExecutionError(f"parent directory does not exist: {parent}")
        existed_before = path.exists()
        atomic = bool(arguments.get("atomic", True))
        import tempfile

        try:
            if atomic:
                with tempfile.NamedTemporaryFile(
                    dir=str(parent), prefix=".sage-write-", delete=False
                ) as handle:
                    handle.write(encoded)
                    temp_path = Path(handle.name)
                os.replace(temp_path, path)
            else:
                with path.open("wb") as handle:
                    handle.write(encoded)
        except OSError as exc:
            raise ToolExecutionError(f"could not write {path}: {exc}") from exc
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={
                "path": str(path),
                "bytes_written": len(encoded),
                "existed_before": existed_before,
            },
        )
