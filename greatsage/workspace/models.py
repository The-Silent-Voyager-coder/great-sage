"""Workspace models (Phase 6).

Workspace discovery is local-first and bounded: it enumerates allowed roots,
detects project type via marker files, maps entry points, and summarizes
structure depth-limited. All paths are canonicalized against allowed roots.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from greatsage.exceptions import WorkspaceValidationError


class ProjectType(StrEnum):
    """Detected project kind via marker files."""

    PYTHON = "python"
    NODE = "node"
    RUST = "rust"
    GO = "go"
    JAVA = "java"
    GENERIC = "generic"
    UNKNOWN = "unknown"


_MARKER_MAP: dict[str, ProjectType] = {
    "pyproject.toml": ProjectType.PYTHON,
    "package.json": ProjectType.NODE,
    "Cargo.toml": ProjectType.RUST,
    "go.mod": ProjectType.GO,
    "pom.xml": ProjectType.JAVA,
    "build.gradle": ProjectType.JAVA,
}


@dataclass(frozen=True)
class EntryPoint:
    """A discovered entry point (e.g. CLI, main module)."""

    path: Path
    kind: str
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "kind": self.kind, "description": self.description}


@dataclass(frozen=True)
class StructureSummary:
    """Depth-limited directory summary."""

    root: Path
    depth: int
    total_files: int
    total_dirs: int
    entries: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "depth": self.depth,
            "total_files": self.total_files,
            "total_dirs": self.total_dirs,
            "entries": self.entries,
        }


@dataclass(frozen=True)
class WorkspaceConfigFile:
    """Optional per-workspace file at .sage/workspace.yaml (repo root)."""

    name: str | None = None
    project_type: ProjectType | None = None
    description: str | None = None
    extra_entry_points: tuple[Path, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceConfigFile:
        pt = data.get("project_type")
        project_type = None
        if isinstance(pt, str) and pt.strip():
            try:
                project_type = ProjectType(pt.strip().lower())
            except ValueError:
                project_type = ProjectType.UNKNOWN
        eps = data.get("entry_points") or []
        extra = tuple(Path(str(p)) for p in eps if isinstance(p, str) and p.strip())
        return cls(
            name=str(data["name"]).strip() if isinstance(data.get("name"), str) and data["name"].strip() else None,  # noqa: E501
            project_type=project_type,
            description=str(data["description"]).strip() if isinstance(data.get("description"), str) else None,  # noqa: E501
            extra_entry_points=extra,
        )


@dataclass(frozen=True)
class WorkspaceInfo:
    """Result of a workspace scan (Phase 6)."""

    id: str = field(default_factory=lambda: f"ws_{uuid.uuid4().hex}")
    root: Path = field(default_factory=lambda: Path("."))
    name: str = ""
    project_type: ProjectType = ProjectType.UNKNOWN
    entry_points: tuple[EntryPoint, ...] = ()
    structure: StructureSummary | None = None
    scanned_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not str(self.root).strip():
            raise WorkspaceValidationError("workspace root must not be empty")
        if not self.root.is_absolute():
            raise WorkspaceValidationError(f"workspace root must be absolute: {self.root}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "root": str(self.root),
            "name": self.name,
            "project_type": self.project_type.value,
            "entry_points": [ep.to_dict() for ep in self.entry_points],
            "structure": self.structure.to_dict() if self.structure else None,
            "scanned_at": self.scanned_at.isoformat(),
            "metadata": self.metadata,
        }


def detect_project_type(root: Path) -> ProjectType:
    """Detect project type by marker files in root (no recursion)."""
    for marker, ptype in _MARKER_MAP.items():
        if (root / marker).is_file():
            return ptype
    # Heuristics: check for common python files
    if (root / "setup.py").is_file() or (root / "requirements.txt").is_file():
        return ProjectType.PYTHON
    if (root / "src").is_dir() or (root / "greatsage").is_dir():
        # fallback: if we see python package
        return ProjectType.PYTHON
    return ProjectType.GENERIC
