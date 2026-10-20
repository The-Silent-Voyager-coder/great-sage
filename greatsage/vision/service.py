"""Vision service facade (Phase 8).

Owns capture + OCR-free grounding + file persistence behind one bounded,
auditable surface. No new configuration section: the store lives under
`core.data_dir/vision` and output-path scoping reuses `tools.allowed_roots`
/ `denied_roots`, so path policy stays in exactly one place.

Events and logs carry ids/dimensions only — never pixel bytes, never
paths outside the store, never secrets.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import VISION_CAPTURED, VISION_DESCRIBED, VISION_FAILED, Event
from greatsage.exceptions import VisionUnavailableError, VisionValidationError
from greatsage.tools.redaction import redact_secrets
from greatsage.vision.capture import CaptureManager, StubCaptureBackend
from greatsage.vision.file_repository import FileVisionRepository
from greatsage.vision.grounding import GroundingStub, OllamaGrounding
from greatsage.vision.limits import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    VisionLimits,
    default_limits,
)
from greatsage.vision.models import (
    VisionBackend,
    VisionCapture,
    VisionDescription,
    new_capture_id,
    sanitize_output_path,
)
from greatsage.vision.repository import VisionRepository

log = logging.getLogger("greatsage.vision.service")


def _scoped_path(
    raw: str | Path, *, allowed_roots: tuple[Path, ...], denied_roots: tuple[Path, ...]
) -> Path:
    """Resolve an explicit output path inside the configured roots."""
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise VisionValidationError(f"capture output path must be absolute, got {raw!r}")
    # Canonicalize without requiring existence of the file itself.
    try:
        canonical = candidate.resolve()
    except OSError as exc:
        raise VisionValidationError(f"unresolvable output path: {exc}") from exc
    for denied in denied_roots:
        try:
            if canonical == denied.resolve() or canonical.is_relative_to(denied.resolve()):
                raise VisionValidationError(f"capture output path is under a denied root: {denied}")
        except OSError:
            continue
    if allowed_roots:
        inside = False
        for root in allowed_roots:
            try:
                resolved_root = root.resolve()
            except OSError:
                continue
            if canonical == resolved_root or canonical.is_relative_to(resolved_root):
                inside = True
                break
        if not inside:
            raise VisionValidationError("capture output path is outside the allowed roots")
    return canonical


class VisionService:
    """Facade over capture manager + local grounding + file repository."""

    def __init__(
        self,
        *,
        limits: VisionLimits | None = None,
        repository: VisionRepository | None = None,
        capture: CaptureManager | None = None,
        grounding: GroundingStub | OllamaGrounding | None = None,
    ) -> None:
        self._limits = limits or default_limits()
        self._repository = repository
        self._capture = capture or CaptureManager(limits=self._limits, backend=StubCaptureBackend())
        self._grounding = grounding or GroundingStub(limits=self._limits)
        self.publisher: Any = None  # callable(event) -> None, wired by runtime
        self.availability: str = "unavailable"
        self.detail: str = "vision service not started"
        self._config: SageConfig | None = None
        self._allowed_roots: tuple[Path, ...] = ()
        self._denied_roots: tuple[Path, ...] = ()
        self._session_counts: dict[str, int] = {}

    # --- lifecycle -----------------------------------------------------

    def start(self, config: SageConfig | None = None) -> None:
        self._config = config
        if config is None:
            self.availability = "unavailable"
            self.detail = "no configuration provided"
            return
        try:
            store_dir = config.core.data_dir / "vision"
            if self._repository is None:
                self._repository = FileVisionRepository(store_dir)
            self._repository.initialize()
            self._allowed_roots = tuple(config.tools.allowed_roots)
            self._denied_roots = tuple(config.tools.denied_roots)
            self.availability = "healthy"
            self.detail = (
                f"vision ready (backend={self._capture.backend_name}, "
                f"grounding={getattr(self._grounding, 'name', 'stub')}, "
                f"max={self._limits.max_width}x{self._limits.max_height}, "
                f"bytes<={self._limits.max_image_bytes})"
            )
            log.info("vision service started", extra={"component": "vision"})
        except Exception as exc:
            self.availability = "unavailable"
            self.detail = f"vision start failed: {exc}"
            log.error(
                "vision service failed to start",
                exc_info=exc,
                extra={"component": "vision"},
            )

    def shutdown(self) -> None:
        if self._repository is not None:
            try:
                self._repository.close()
            except Exception:
                pass
        self.availability = "disabled"
        self.detail = "vision service stopped"
        log.info("vision service stopped", extra={"component": "vision"})

    def _require_available(self) -> VisionRepository:
        if self.availability != "healthy" or self._repository is None:
            raise VisionUnavailableError(self.detail or "vision service not available")
        return self._repository

    # --- operations ----------------------------------------------------

    def capture(
        self,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        output_path: str | Path | None = None,
    ) -> VisionCapture:
        """Capture one bounded frame and persist it; returns metadata only."""
        repository = self._require_available()
        if session_id is not None:
            used = self._session_counts.get(session_id, 0)
            if used >= self._limits.max_captures_per_session:
                raise VisionValidationError(
                    f"session capture budget exhausted ({self._limits.max_captures_per_session})"
                )
        scoped: Path | None = None
        if output_path is not None:
            candidate = sanitize_output_path(output_path)
            scoped = _scoped_path(
                candidate,
                allowed_roots=self._allowed_roots,
                denied_roots=self._denied_roots,
            )
            if scoped.parent != scoped and not scoped.parent.exists():
                raise VisionValidationError(
                    "capture output directory does not exist; "
                    "create it first through the file tools"
                )
        try:
            image, meta = self._capture.capture(width, height)
        except Exception as exc:
            self._publish(
                VISION_FAILED,
                {"reason": redact_secrets(str(exc)[:200])},
                session_id=session_id,
                task_id=task_id,
            )
            raise
        digest = hashlib.sha256(image).hexdigest()
        record = VisionCapture(
            id=new_capture_id(),
            backend=VisionBackend(self._capture.backend_name),
            width=int(meta["width"]),
            height=int(meta["height"]),
            format="bmp",
            sha256=digest,
            size_bytes=len(image),
            output_path=str(scoped) if scoped is not None else None,
            metadata={"duration_ms": meta["duration_ms"]},
        )
        record.validate()
        repository.save(record, image)
        if scoped is not None:
            scoped.write_bytes(image)
        if session_id is not None:
            self._session_counts[session_id] = self._session_counts.get(session_id, 0) + 1
        self._publish(
            VISION_CAPTURED,
            {
                "capture_id": record.id,
                "backend": record.backend.value,
                "width": record.width,
                "height": record.height,
                "size_bytes": record.size_bytes,
            },
            session_id=session_id,
            task_id=task_id,
        )
        log.info(
            "vision capture stored",
            extra={"component": "vision", "capture_id": record.id},
        )
        return record

    def describe(
        self,
        capture_id: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        max_regions: int | None = None,
        max_chars: int | None = None,
    ) -> VisionDescription:
        """Describe a stored capture (real local VLM when wired, else stub)."""
        repository = self._require_available()
        found = repository.get(capture_id)
        if found is None:
            raise VisionValidationError(f"unknown capture id: {capture_id!r}")
        record, image = found
        description = self._grounding.describe(
            record.id,
            record.width,
            record.height,
            image=image,
            max_regions=max_regions,
            max_chars=max_chars,
        )
        self._publish(
            VISION_DESCRIBED,
            {
                "capture_id": record.id,
                "regions": len(description.regions),
                "truncated": description.truncated,
            },
            session_id=session_id,
            task_id=task_id,
        )
        return description

    def get(self, capture_id: str) -> VisionCapture | None:
        repository = self._require_available()
        found = repository.get(capture_id)
        return found[0] if found is not None else None

    def latest(self) -> VisionCapture | None:
        repository = self._require_available()
        items = repository.list(limit=1)
        return items[0] if items else None

    def list(self, limit: int = 50) -> list[VisionCapture]:
        return self._require_available().list(limit=limit)

    # --- health --------------------------------------------------------

    def health(self) -> dict[str, Any]:
        repo_health: dict[str, object] = {}
        if self._repository is not None:
            try:
                repo_health = self._repository.health().to_dict()
            except Exception as exc:
                repo_health = {"accessible": False, "detail": str(exc)[:200]}
        return {
            "available": self.availability == "healthy",
            "status": self.availability,
            "enabled": True,
            "detail": self.detail,
            "backend": self._capture.backend_name,
            "backend_available": self._capture.backend_available,
            "grounding": getattr(self._grounding, "name", "stub"),
            "repository": repo_health,
        }

    def register_health_check(self, health_registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self.availability == "disabled":
                return HealthStatus.HEALTHY
            if self.availability == "healthy":
                return HealthStatus.HEALTHY
            return HealthStatus.UNHEALTHY

        health_registry.register("vision", checker, "vision subsystem (bounded)")

    # --- internals -----------------------------------------------------

    def _publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(
                Event(
                    type=event_type,
                    source="vision",
                    session_id=session_id,
                    task_id=task_id,
                    payload=payload,
                )
            )
        except Exception as exc:
            log.warning("vision event publish failed: %s", exc, extra={"component": "vision"})
