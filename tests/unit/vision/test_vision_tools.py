"""Vision tool tests: registry, direct execution, policy pipeline."""

from __future__ import annotations

import uuid
from pathlib import Path

from greatsage.configuration.loader import load_config
from greatsage.configuration.model import ToolSecurityMode
from greatsage.exceptions import ToolPermissionDeniedError
from greatsage.tools.approval import DeterministicApprovalProvider
from greatsage.tools.models import (
    ApprovalOutcome,
    ToolContext,
    ToolDecision,
    ToolRequest,
    ToolRisk,
)
from greatsage.tools.policy import SecurityPolicy
from greatsage.tools.registry import ToolRegistry
from greatsage.tools.service import ToolService
from greatsage.vision.capture import encode_bmp
from greatsage.vision.tools import (
    VisionCaptureTool,
    VisionCompareTool,
    VisionDescribeTool,
    VisionLocateTool,
    register_vision_tools,
)


def write_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    path = tmp_path / "vision-tools.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Vision Tools Test"
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


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        working_directory=tmp_path / "workspace",
        environment={},
        timeout_seconds=10.0,
        max_output_bytes=65536,
    )


def test_register_vision_tools() -> None:
    registry = ToolRegistry()
    register_vision_tools(registry)
    assert "vision.capture" in registry.list_ids()
    assert "vision.describe" in registry.list_ids()
    assert "vision.locate" in registry.list_ids()
    assert "vision.compare" in registry.list_ids()
    info = registry.describe("vision.capture")
    assert info["risk_level"] == "safe"
    assert "output_path" in info["input_schema"]["properties"]


def test_tools_declare_safe_read_risk() -> None:
    assert VisionCaptureTool().risk_level is ToolRisk.SAFE
    assert VisionDescribeTool().risk_level is ToolRisk.SAFE
    assert VisionLocateTool().risk_level is ToolRisk.SAFE
    assert VisionCompareTool().risk_level is ToolRisk.SAFE
    assert VisionCaptureTool().PATH_ARGUMENTS == ("output_path",)
    assert VisionDescribeTool().PATH_ARGUMENTS == ("capture_path",)
    assert VisionLocateTool().PATH_ARGUMENTS == ("output_path",)
    assert VisionCompareTool().PATH_ARGUMENTS == ("before_path", "after_path")


def test_capture_tool_metadata_only(tmp_path: Path) -> None:
    tool = VisionCaptureTool()
    result = tool.execute({"width": 32, "height": 20}, make_context(tmp_path))
    assert result.success
    assert result.output is not None
    assert result.output["width"] == 32
    assert "image" not in result.output and "pixels" not in result.output


def test_capture_tool_rejects_oversize() -> None:
    from greatsage.vision.limits import VisionLimits

    tool = VisionCaptureTool(limits=VisionLimits(max_width=8, max_height=8))
    result = tool.execute({"width": 64, "height": 64}, make_context(Path(".")))
    assert not result.success


def test_capture_tool_writes_absolute_bmp(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    target = tmp_path / "workspace" / "shot.bmp"
    tool = VisionCaptureTool()
    result = tool.execute(
        {"width": 16, "height": 10, "output_path": str(target)},
        make_context(tmp_path),
    )
    assert result.success
    assert target.exists()
    assert result.output is not None
    assert result.output["output_path"] == str(target)


def test_capture_tool_rejects_non_bmp_suffix(tmp_path: Path) -> None:
    tool = VisionCaptureTool()
    result = tool.execute(
        {"width": 16, "height": 10, "output_path": str(tmp_path / "shot.png")},
        make_context(tmp_path),
    )
    assert not result.success


def test_describe_tool_reads_bmp(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    frame = tmp_path / "workspace" / "frame.bmp"
    frame.write_bytes(encode_bmp(64, 40))
    tool = VisionDescribeTool()
    result = tool.execute({"capture_path": str(frame)}, make_context(tmp_path))
    assert result.success
    assert result.output is not None
    assert result.output["summary"].startswith("[stub-no-ocr]")
    assert len(result.output["regions"]) == 8


def test_describe_tool_uses_real_capture_id(tmp_path: Path) -> None:
    from greatsage.vision.tools import _capture_id_from_path

    real = "cap_" + "a" * 32
    assert _capture_id_from_path(tmp_path / f"{real}.bmp") == real
    assert _capture_id_from_path(tmp_path / "screenshot.bmp") == "cap_" + "0" * 32


def test_describe_tool_rejects_missing_file(tmp_path: Path) -> None:
    tool = VisionDescribeTool()
    result = tool.execute({"capture_path": str(tmp_path / "ghost.bmp")}, make_context(tmp_path))
    assert not result.success


def test_describe_tool_reports_clickable_centers(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    frame = tmp_path / "workspace" / "frame.bmp"
    frame.write_bytes(encode_bmp(64, 40))
    result = VisionDescribeTool().execute(
        {"capture_path": str(frame)}, make_context(tmp_path)
    )
    assert result.success
    assert result.output is not None
    first = result.output["regions"][0]
    assert first["index"] == 0
    assert 0 <= first["center_x"] < 64
    assert 0 <= first["center_y"] < 40


def test_locate_tool_captures_and_numbers_regions(tmp_path: Path) -> None:
    result = VisionLocateTool().execute(
        {"width": 32, "height": 20, "max_regions": 6},
        make_context(tmp_path),
    )
    assert result.success
    assert result.output is not None
    assert result.output["capture_id"].startswith("cap_")
    assert result.output["width"] == 32
    assert len(result.output["regions"]) == 6
    first = result.output["regions"][0]
    assert first["index"] == 0
    assert 0 <= first["center_x"] < 32
    assert 0 <= first["center_y"] < 20
    assert "image" not in result.output and "pixels" not in result.output


def test_locate_tool_saves_capture(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    target = tmp_path / "workspace" / "locate.bmp"
    result = VisionLocateTool().execute(
        {"width": 16, "height": 10, "output_path": str(target)},
        make_context(tmp_path),
    )
    assert result.success
    assert result.output is not None
    assert target.exists()
    assert result.output["output_path"] == str(target)


def test_locate_tool_rejects_invalid_output_and_regions(tmp_path: Path) -> None:
    tool = VisionLocateTool()
    context = make_context(tmp_path)
    assert not tool.execute({"output_path": "relative.bmp"}, context).success
    assert not tool.execute({"output_path": str(tmp_path / "shot.png")}, context).success
    assert not tool.execute({"max_regions": 0}, context).success
    assert not tool.execute({"max_regions": True}, context).success


def test_compare_tool_reports_change(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    before = tmp_path / "workspace" / "before.bmp"
    after = tmp_path / "workspace" / "after.bmp"
    before.write_bytes(encode_bmp(32, 20, seed=0))
    after.write_bytes(encode_bmp(32, 20, seed=1))
    result = VisionCompareTool().execute(
        {"before_path": str(before), "after_path": str(after)},
        make_context(tmp_path),
    )
    assert result.success
    assert result.output is not None
    assert result.output["changed"] is True
    assert result.output["change_ratio"] > 0
    assert result.output["regions"]


def test_compare_tool_reports_nothing_changed(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    frame = tmp_path / "workspace" / "frame.bmp"
    frame.write_bytes(encode_bmp(32, 20))
    result = VisionCompareTool().execute(
        {"before_path": str(frame), "after_path": str(frame)},
        make_context(tmp_path),
    )
    assert result.success
    assert result.output is not None
    assert result.output["changed"] is False
    assert result.output["summary"] == "NOTHING CHANGED"


def test_compare_tool_resolves_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    frame = tmp_path / "workspace" / "frame.bmp"
    frame.write_bytes(encode_bmp(16, 10))
    result = VisionCompareTool().execute(
        {"before_path": "frame.bmp", "after_path": "frame.bmp"},
        make_context(tmp_path),
    )
    assert result.success


def test_compare_tool_rejects_missing_or_non_bmp(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    text = tmp_path / "workspace" / "note.txt"
    text.write_text("not an image", encoding="utf-8")
    tool = VisionCompareTool()
    context = make_context(tmp_path)
    assert not tool.execute(
        {"before_path": "ghost.bmp", "after_path": "ghost.bmp"}, context
    ).success
    assert not tool.execute(
        {"before_path": str(text), "after_path": str(text)}, context
    ).success


def test_policy_denies_capture_outside_roots(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path))).config
    policy = SecurityPolicy.from_config(config)
    tool = VisionCaptureTool()
    request = ToolRequest(
        request_id="r1",
        tool_id="vision.capture",
        arguments={"output_path": "C:/Windows/Temp/evil.bmp"},
        source="test",
    )
    decision, _reason = policy.evaluate(request, tool)
    assert decision is ToolDecision.DENY


def test_service_pipeline_executes_vision_capture(tmp_path: Path) -> None:
    service = ToolService()
    service.start(load_config(str(write_config(tmp_path))).config)
    # Vision tools are registered by ToolService.start (Phase 8 wiring).
    assert "vision.capture" in service.registry().list_ids()
    request = ToolRequest(
        request_id=uuid.uuid4().hex,
        tool_id="vision.capture",
        arguments={"width": 32, "height": 20},
        source="test",
    )
    result = service.execute(request)
    assert result.success
    assert result.output is not None
    assert result.output["format"] == "bmp"


def test_pipeline_denies_outside_roots(tmp_path: Path) -> None:
    service = ToolService()
    service.start(load_config(str(write_config(tmp_path))).config)
    assert "vision.capture" in service.registry().list_ids()
    service.approval = DeterministicApprovalProvider(ApprovalOutcome.APPROVED)
    request = ToolRequest(
        request_id=uuid.uuid4().hex,
        tool_id="vision.capture",
        arguments={"width": 16, "height": 10, "output_path": "C:/Windows/x.bmp"},
        source="test",
    )
    try:
        service.execute(request)
    except ToolPermissionDeniedError:
        return
    raise AssertionError("expected ToolPermissionDeniedError")


def test_normal_mode_allows_safe_vision() -> None:
    policy = SecurityPolicy(ToolSecurityMode.NORMAL)
    request = ToolRequest(request_id="r1", tool_id="vision.capture", arguments={}, source="test")
    assert policy.evaluate(request, VisionCaptureTool())[0] is ToolDecision.ALLOW
