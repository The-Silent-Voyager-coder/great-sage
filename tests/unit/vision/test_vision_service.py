"""Vision service tests: lifecycle, bounds, scoping, events, health."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from greatsage.configuration.loader import load_config
from greatsage.events.models import VISION_CAPTURED, VISION_DESCRIBED
from greatsage.exceptions import VisionUnavailableError, VisionValidationError
from greatsage.vision.limits import VisionLimits
from greatsage.vision.service import VisionService


def write_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    path = tmp_path / "vision.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Vision Test"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "DEBUG"
  retention_days: 7
memory:
  enabled: false
  database_path: "{d}/data/memory.db"
  auto_save_conversations: false
  default_confidence: 0.8
  retention_days: 365
security:
  mode: "normal"
  allow_auto_approve_read: true
tools:
  working_directory: "{d}/workspace"
  execution_timeout_seconds: 10.0
  max_output_bytes: 65536
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "SYSTEM"
  browser:
    default_risk: "FORBIDDEN"
""",
        encoding="utf-8",
    )
    return path


def make_service(tmp_path: Path, **kwargs: Any) -> VisionService:
    config = load_config(str(write_config(tmp_path))).config
    service = VisionService(**kwargs)
    service.start(config)
    return service


class EventSink:
    def __init__(self) -> None:
        self.types: list[str] = []
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, event: Any) -> None:
        self.types.append(event.type)
        self.payloads.append(dict(event.payload))


def test_start_without_config_unavailable() -> None:
    service = VisionService()
    service.start(None)
    assert service.availability == "unavailable"
    with pytest.raises(VisionUnavailableError):
        service.capture()


def test_capture_and_get(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    record = service.capture(64, 40, session_id="s1")
    assert record.width == 64
    assert service.get(record.id) is not None
    assert service.latest() is not None
    assert len(service.list()) == 1


def test_capture_events_carry_no_pixels(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    sink = EventSink()
    service.publisher = sink
    record = service.capture(32, 20, session_id="s1", task_id="t1")
    assert VISION_CAPTURED in sink.types
    payload = sink.payloads[sink.types.index(VISION_CAPTURED)]
    assert payload["capture_id"] == record.id
    assert "image" not in payload and "pixels" not in payload


def test_describe_stub(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    sink = EventSink()
    service.publisher = sink
    record = service.capture(64, 40)
    desc = service.describe(record.id, max_regions=4)
    assert desc.capture_id == record.id
    assert len(desc.regions) == 4
    assert VISION_DESCRIBED in sink.types


def test_describe_unknown_id(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    from greatsage.vision.models import new_capture_id

    with pytest.raises(VisionValidationError):
        service.describe(new_capture_id())


def test_session_budget_enforced(tmp_path: Path) -> None:
    service = make_service(tmp_path, limits=VisionLimits(max_captures_per_session=1))
    service.capture(16, 10, session_id="budget")
    with pytest.raises(VisionValidationError):
        service.capture(16, 10, session_id="budget")


def test_output_path_outside_roots_rejected(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(VisionValidationError):
        service.capture(16, 10, output_path="C:/Windows/Temp/frame.bmp")


def test_output_path_inside_roots_written(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    target = tmp_path / "workspace" / "frame.bmp"
    record = service.capture(16, 10, output_path=str(target))
    assert record.output_path is not None
    assert target.exists()


def test_output_path_missing_parent_rejected(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(VisionValidationError):
        service.capture(16, 10, output_path=str(tmp_path / "nope" / "frame.bmp"))


def test_health(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    data = service.health()
    assert data["available"]
    assert data["backend"] == "stub"
    assert data["repository"]["capture_count"] == 0
