"""Vision models: captures, regions, descriptions (Phase 8).

Local-first and bounded by design: a capture carries metadata + a content
hash, never pixel bytes in events or logs. Grounding is OCR-free — regions
are deterministic layout stubs explicitly labeled as such, never claims of
real screen content.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from greatsage.exceptions import VisionValidationError


class VisionBackend(StrEnum):
    """Capture backends. Only local, zero-cost backends are permitted."""

    STUB = "stub"  # synthetic frames (fallback, hermetic tests)
    GDI = "gdi"  # real primary-monitor capture via Windows GDI (ctypes)
    OLLAMA = "ollama"  # descriptions from a local Ollama vision model


def new_capture_id() -> str:
    """Generate a non-sequential capture id."""
    return f"cap_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class VisionCapture:
    """Metadata for one bounded screen capture.

    Pixel bytes live in the repository / output file only; this record is
    what flows through events, logs, and CLI output.
    """

    id: str
    backend: VisionBackend
    width: int
    height: int
    format: str
    sha256: str
    size_bytes: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    output_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id.startswith("cap_") or len(self.id) != 36:
            raise VisionValidationError(f"invalid capture id: {self.id!r}")
        if self.width <= 0 or self.height <= 0:
            raise VisionValidationError(
                f"capture dimensions must be positive, got {self.width}x{self.height}"
            )
        if self.format != "bmp":
            raise VisionValidationError(f"unsupported capture format: {self.format!r}")
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise VisionValidationError("capture sha256 must be 64 lowercase hex chars")
        if self.size_bytes <= 0:
            raise VisionValidationError("capture size_bytes must be positive")
        if self.created_at.tzinfo is None:
            raise VisionValidationError("capture created_at must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backend": self.backend.value,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at.isoformat(),
            "output_path": self.output_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VisionCapture:
        try:
            capture = cls(
                id=str(data["id"]),
                backend=VisionBackend(str(data["backend"])),
                width=int(data["width"]),
                height=int(data["height"]),
                format=str(data["format"]),
                sha256=str(data["sha256"]),
                size_bytes=int(data["size_bytes"]),
                created_at=datetime.fromisoformat(str(data["created_at"])),
                output_path=data.get("output_path"),
                metadata=dict(data.get("metadata") or {}),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise VisionValidationError(f"invalid capture record: {exc}") from exc
        capture.validate()
        return capture


@dataclass(frozen=True)
class VisionRegion:
    """One OCR-free grounded region with normalized 0..1 coordinates."""

    label: str
    x: float
    y: float
    width: float
    height: float
    confidence: float

    def validate(self) -> None:
        if not self.label:
            raise VisionValidationError("region label must not be empty")
        for name, value in (
            ("x", self.x),
            ("y", self.y),
            ("width", self.width),
            ("height", self.height),
        ):
            if not 0.0 <= value <= 1.0:
                raise VisionValidationError(f"region {name} must be in 0..1, got {value}")
        if not 0.0 <= self.confidence <= 1.0:
            raise VisionValidationError(f"region confidence must be in 0..1, got {self.confidence}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class VisionDescription:
    """OCR-free description of a capture: stub summary + layout regions."""

    capture_id: str
    backend: VisionBackend
    summary: str
    regions: tuple[VisionRegion, ...] = ()
    truncated: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def validate(self) -> None:
        if not self.capture_id.startswith("cap_"):
            raise VisionValidationError(f"invalid capture id: {self.capture_id!r}")
        if not self.summary:
            raise VisionValidationError("description summary must not be empty")
        for region in self.regions:
            region.validate()
        if self.created_at.tzinfo is None:
            raise VisionValidationError("description created_at must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "backend": self.backend.value,
            "summary": self.summary,
            "regions": [region.to_dict() for region in self.regions],
            "truncated": self.truncated,
            "created_at": self.created_at.isoformat(),
        }


def sanitize_output_path(value: str | Path) -> Path:
    """Validate an explicit capture output path (no secrets, bmp only)."""
    path = Path(value)
    if path.suffix.lower() != ".bmp":
        raise VisionValidationError(f"capture output must be a .bmp path, got {path.suffix!r}")
    return path
