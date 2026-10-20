"""Voice tool tests: registry, direct execution, policy pipeline."""

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
from greatsage.voice.limits import VoiceLimits
from greatsage.voice.tools import (
    VoiceListenTool,
    VoiceSpeakTool,
    VoiceWakeTool,
    register_voice_tools,
)
from greatsage.voice.tts import encode_wav


def write_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    path = tmp_path / "voice-tools.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Voice Tools Test"
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


def test_register_voice_tools() -> None:
    registry = ToolRegistry()
    register_voice_tools(registry)
    assert "voice.listen" in registry.list_ids()
    assert "voice.speak" in registry.list_ids()
    assert "voice.wake" in registry.list_ids()
    info = registry.describe("voice.listen")
    assert info["risk_level"] == "safe"
    assert "text" in info["input_schema"]["properties"]


def test_tools_declare_safe_risk() -> None:
    assert VoiceListenTool().risk_level is ToolRisk.SAFE
    assert VoiceSpeakTool().risk_level is ToolRisk.SAFE
    assert VoiceWakeTool().risk_level is ToolRisk.SAFE
    assert VoiceListenTool().PATH_ARGUMENTS == ("audio_path",)
    assert VoiceSpeakTool().PATH_ARGUMENTS == ("output_path",)


def test_listen_tool_metadata_only(tmp_path: Path) -> None:
    tool = VoiceListenTool()
    result = tool.execute({"text": "hello"}, make_context(tmp_path))
    assert result.success
    assert result.output is not None
    assert "text" not in result.output and "audio" not in result.output


def test_listen_tool_requires_exactly_one_input(tmp_path: Path) -> None:
    tool = VoiceListenTool()
    assert not tool.execute({}, make_context(tmp_path)).success
    assert not tool.execute({"text": "hi", "audio_path": "x.wav"}, make_context(tmp_path)).success


def test_listen_tool_reads_wav(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    frame = tmp_path / "workspace" / "in.wav"
    audio, _ = encode_wav("hi")
    frame.write_bytes(audio)
    result = VoiceListenTool().execute({"audio_path": str(frame)}, make_context(tmp_path))
    assert result.success


def test_listen_tool_rejects_non_wav(tmp_path: Path) -> None:
    result = VoiceListenTool().execute(
        {"audio_path": str(tmp_path / "in.mp3")}, make_context(tmp_path)
    )
    assert not result.success


def test_speak_tool_metadata_only(tmp_path: Path) -> None:
    tool = VoiceSpeakTool()
    result = tool.execute({"text": "hello"}, make_context(tmp_path))
    assert result.success
    assert result.output is not None
    assert result.output["size_bytes"] > 0
    assert "audio" not in result.output


def test_speak_tool_writes_absolute_wav(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    target = tmp_path / "workspace" / "out.wav"
    result = VoiceSpeakTool().execute(
        {"text": "hi", "output_path": str(target)}, make_context(tmp_path)
    )
    assert result.success
    assert target.exists()
    assert result.output is not None
    assert result.output["output_path"] == str(target)


def test_speak_tool_rejects_oversize() -> None:
    tool = VoiceSpeakTool(limits=VoiceLimits(max_synth_chars=2))
    assert not tool.execute(
        {"text": "way too long"},
        ToolContext(
            working_directory=Path("."),
            environment={},
            timeout_seconds=10.0,
            max_output_bytes=65536,
        ),
    ).success


def test_wake_tool(tmp_path: Path) -> None:
    result = VoiceWakeTool().execute({"text": "hey great sage"}, make_context(tmp_path))
    assert result.success
    assert result.output is not None
    assert result.output["detected"] is True
    quiet = VoiceWakeTool().execute({"text": "good morning"}, make_context(tmp_path))
    assert quiet.success
    assert quiet.output is not None
    assert quiet.output["detected"] is False


def test_policy_denies_speak_outside_roots(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path))).config
    policy = SecurityPolicy.from_config(config)
    tool = VoiceSpeakTool()
    request = ToolRequest(
        request_id="r1",
        tool_id="voice.speak",
        arguments={"text": "hi", "output_path": "C:/Windows/Temp/evil.wav"},
        source="test",
    )
    decision, _reason = policy.evaluate(request, tool)
    assert decision is ToolDecision.DENY


def test_service_pipeline_executes_voice_listen(tmp_path: Path) -> None:
    service = ToolService()
    service.start(load_config(str(write_config(tmp_path))).config)
    # Voice tools are registered by ToolService.start (Phase 6 wiring).
    assert "voice.listen" in service.registry().list_ids()
    request = ToolRequest(
        request_id=uuid.uuid4().hex,
        tool_id="voice.listen",
        arguments={"text": "hello"},
        source="test",
    )
    result = service.execute(request)
    assert result.success


def test_pipeline_denies_outside_roots(tmp_path: Path) -> None:
    service = ToolService()
    service.start(load_config(str(write_config(tmp_path))).config)
    assert "voice.speak" in service.registry().list_ids()
    service.approval = DeterministicApprovalProvider(ApprovalOutcome.APPROVED)
    request = ToolRequest(
        request_id=uuid.uuid4().hex,
        tool_id="voice.speak",
        arguments={"text": "hi", "output_path": "C:/Windows/x.wav"},
        source="test",
    )
    try:
        service.execute(request)
    except ToolPermissionDeniedError:
        return
    raise AssertionError("expected ToolPermissionDeniedError")


def test_normal_mode_allows_safe_voice() -> None:
    policy = SecurityPolicy(ToolSecurityMode.NORMAL)
    request = ToolRequest(request_id="r1", tool_id="voice.wake", arguments={}, source="test")
    assert policy.evaluate(request, VoiceWakeTool())[0] is ToolDecision.ALLOW
