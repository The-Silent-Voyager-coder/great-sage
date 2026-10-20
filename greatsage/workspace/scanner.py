"""Workspace scanner (Phase 6).

Enumerates allowed roots, detects project type, maps entry points, summarizes
structure depth-limited. All paths are canonicalized and checked against
allowed/denied roots via the same pathsecurity used by tools.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml

from greatsage.exceptions import WorkspaceValidationError
from greatsage.tools.pathsecurity import canonicalize, is_within
from greatsage.workspace.models import (
    EntryPoint,
    ProjectType,
    StructureSummary,
    WorkspaceConfigFile,
    WorkspaceInfo,
    detect_project_type,
)

_DEFAULT_ENTRY_GLOBS: dict[ProjectType, list[str]] = {
    ProjectType.PYTHON: ["pyproject.toml", "setup.py", "greatsage/cli.py", "main.py", "app.py"],
    ProjectType.NODE: ["package.json", "src/index.js", "index.js"],
    ProjectType.RUST: ["Cargo.toml", "src/main.rs"],
    ProjectType.GO: ["go.mod", "main.go"],
    ProjectType.JAVA: ["pom.xml", "build.gradle", "src/main/java"],
    ProjectType.GENERIC: ["README.md", "Makefile"],
    ProjectType.UNKNOWN: ["README.md"],
}


class WorkspaceScanner:
    """Bounded scanner for a single workspace root."""

    def __init__(
        self,
        *,
        allowed_roots: tuple[Path, ...],
        denied_roots: tuple[Path, ...],
        working_directory: Path,
        max_scan_depth: int = 3,
        max_entries: int = 500,
        scan_timeout_seconds: float = 10.0,
    ) -> None:
        self._allowed = allowed_roots
        self._denied = denied_roots
        self._working = working_directory
        self._max_depth = max_scan_depth
        self._max_entries = max_entries
        self._timeout = scan_timeout_seconds

    def scan(self, path: str | Path | None = None) -> WorkspaceInfo:
        """Scan a workspace root and return WorkspaceInfo.

        Raises WorkspaceValidationError on disallowed paths or invalid input.
        """
        target = Path(path) if path is not None else self._working
        canonical = canonicalize(str(target), self._working)
        self._assert_allowed(canonical)
        if not canonical.exists():
            raise WorkspaceValidationError(f"workspace path does not exist: {canonical}")
        if not canonical.is_dir():
            raise WorkspaceValidationError(f"workspace path is not a directory: {canonical}")
        start = time.monotonic()
        project_type = detect_project_type(canonical)
        # Per-workspace config override at .sage/workspace.yaml (legacy
        # .greatsage/ falls back; see _load_workspace_config)
        config_file = self._load_workspace_config(canonical)
        if config_file.project_type is not None:
            project_type = config_file.project_type
        entry_points = self._map_entry_points(canonical, project_type, config_file)
        structure = self._summarize(canonical, start)
        name = config_file.name or canonical.name
        return WorkspaceInfo(
            root=canonical,
            name=name,
            project_type=project_type,
            entry_points=tuple(entry_points),
            structure=structure,
            metadata={"config_present": any(
                (canonical / loc / name).exists()
                for loc in (".sage", ".greatsage")
                for name in ("workspace.yaml", "workspace.yml")
            )},
        )

    def _load_workspace_config(self, root: Path) -> WorkspaceConfigFile:
        for candidate in (
            root / ".sage" / "workspace.yaml",
            root / ".sage" / "workspace.yml",
            # Legacy locations from previous generation; read-only fallback.
            root / ".greatsage" / "workspace.yaml",
            root / ".greatsage" / "workspace.yml",
        ):
            if candidate.is_file():
                cfg_path = candidate
                break
        else:
            return WorkspaceConfigFile()
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return WorkspaceConfigFile()
            return WorkspaceConfigFile.from_dict(data)
        except Exception:
            return WorkspaceConfigFile()

    def _map_entry_points(
        self, root: Path, ptype: ProjectType, cfg: WorkspaceConfigFile
    ) -> list[EntryPoint]:
        globs = list(_DEFAULT_ENTRY_GLOBS.get(ptype, []))
        entries: list[EntryPoint] = []
        seen: set[Path] = set()
        for pattern in globs:
            candidate = root / pattern
            if candidate.exists():
                if candidate not in seen:
                    entries.append(EntryPoint(path=candidate, kind=ptype.value, description=pattern))  # noqa: E501
                    seen.add(candidate)
        # Extra entry points from config file
        for ep in cfg.extra_entry_points:
            cand = (root / ep) if not ep.is_absolute() else ep
            cand = cand.resolve() if cand.exists() else cand
            if cand not in seen and str(cand).strip():
                entries.append(EntryPoint(path=cand, kind="custom", description="config entry_point"))  # noqa: E501
                seen.add(cand)
        # Fallback: at least root README if present
        if not entries and (root / "README.md").is_file():
            entries.append(EntryPoint(path=root / "README.md", kind="generic", description="README.md"))  # noqa: E501
        return entries

    def _summarize(self, root: Path, start: float) -> StructureSummary:
        total_files = 0
        total_dirs = 0
        entries: list[dict[str, Any]] = []
        # BFS depth-limited
        stack: list[tuple[Path, int]] = [(root, 0)]
        visited = 0
        while stack:
            current, depth = stack.pop(0)
            if time.monotonic() - start > self._timeout:
                break
            if depth > self._max_depth:
                continue
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if visited >= self._max_entries:
                            break
                        visited += 1
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        rel = Path(entry.path).relative_to(root) if Path(entry.path).is_relative_to(root) else Path(entry.name)  # noqa: E501
                        # skip hidden cache/venv
                        if entry.name in {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache"}:  # noqa: E501
                            continue
                        if is_dir:
                            total_dirs += 1
                            entries.append({"path": str(rel), "type": "dir", "depth": depth + 1})
                            if depth + 1 < self._max_depth:
                                stack.append((Path(entry.path), depth + 1))
                        else:
                            total_files += 1
                            # only include files at depth <= max_depth, limit entries size
                            if len(entries) < self._max_entries:
                                entries.append({"path": str(rel), "type": "file", "depth": depth + 1})  # noqa: E501
            except (OSError, PermissionError):
                continue
        return StructureSummary(
            root=root,
            depth=self._max_depth,
            total_files=total_files,
            total_dirs=total_dirs,
            entries=entries[: self._max_entries],
        )

    def _assert_allowed(self, path: Path) -> None:
        # Denied roots first
        for root in self._denied:
            if is_within(path, root):
                raise WorkspaceValidationError(f"workspace {path} is inside denied root: {root}")
        # Protected paths via pathsecurity (e.g. memory.db) not needed here; just scope check
        if is_within(path, self._working):
            return
        if any(is_within(path, r) for r in self._allowed):
            return
        raise WorkspaceValidationError(f"workspace {path} is outside allowed roots")
