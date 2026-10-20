"""ToolService pipeline tests (spec §2, §32-34, §39): the security gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from greatsage.configuration.loader import load_config
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import (
    TOOL_ALLOWED,
    TOOL_APPROVAL_REQUESTED,
    TOOL_APPROVED,
    TOOL_COMPLETED,
    TOOL_DENIED,
    TOOL_FAILED,
    TOOL_REJECTED,
    TOOL_REQUESTED,
    TOOL_STARTED,
)
from greatsage.exceptions import (
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolUnavailableError,
    ToolValidationError,
)
from greatsage.tools.models import ApprovalOutcome, ToolRequest
from greatsage.tools.service import ToolService
from tests.unit.tools.stub_tools import EventRecorder, RecordingApprovalProvider

SEQ_ALLOWED = [TOOL_REQUESTED, TOOL_ALLOWED, TOOL_STARTED, TOOL_COMPLETED]
SEQ_ASKED = [TOOL_REQUESTED, TOOL_APPROVAL_REQUESTED]


def write_config(tmp_path: Path, **overrides: object) -> Path:
    d = str(tmp_path).replace("\\", "/")
    workspace = f"{d}/workspace"
    Path(workspace).mkdir(exist_ok=True)
    mode = str(overrides.pop("security_mode", "normal"))
    allow_low = bool(overrides.pop("allow_auto_approve_read", True))
    max_bytes = int(overrides.pop("max_output_bytes", 65536))
    timeout = float(overrides.pop("execution_timeout_seconds", 10.0))
    path = tmp_path / "sage.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Tools Test"
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
  mode: "{mode}"
  allow_auto_approve_read: {str(allow_low).lower()}
tools:
  working_directory: "{workspace}"
  execution_timeout_seconds: {timeout}
  max_output_bytes: {max_bytes}
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


def make_service(tmp_path: Path, **overrides: object) -> tuple[ToolService, EventRecorder]:
    config = load_config(write_config(tmp_path, **overrides)).config
    recorder = EventRecorder()
    service = ToolService()
    service.publisher = recorder
    service.start(config)
    return service, recorder


def request(tool_id: str, **arguments: object) -> ToolRequest:
    return ToolRequest(
        request_id="req-1",
        tool_id=tool_id,
        arguments=arguments,
        source="test",
        session_id="sess-1",
        task_id="task-1",
    )


def test_start_reports_healthy_with_tool_count(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    assert service.availability == "healthy"
    assert service.health()["tool_count"] >= 9
    assert service.health()["status"] == "healthy"
    service.shutdown()
    assert service.availability == "disabled"


def test_start_without_config_unavailable(tmp_path: Path) -> None:
    service = ToolService()
    service.start(None)
    assert service.availability == "unavailable"
    with pytest.raises(ToolUnavailableError):
        service.execute(request("system.info"))


def test_unstarted_service_raises_tool_unavailable(tmp_path: Path) -> None:
    service = ToolService()
    with pytest.raises(ToolUnavailableError):
        service.execute(request("system.info"))


def test_safe_tool_allowed_without_approval(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    result = service.execute(request("system.info"))
    assert result.success
    assert recorder.types == SEQ_ALLOWED
    event = recorder.events[0]
    assert event.payload["request_id"] == "req-1"
    assert event.session_id == "sess-1"
    assert event.task_id == "task-1"
    assert event.source == "tools"


def test_events_never_contain_arguments(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    service.execute(request("system.info"))
    for event in recorder.events:
        assert "arguments" not in json.dumps(event.payload)


def test_unknown_tool_fails_and_audits(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    with pytest.raises(ToolNotFoundError, match="unknown tool"):
        service.execute(request("not.a.tool"))
    assert recorder.types == [TOOL_REQUESTED, TOOL_FAILED]


def test_invalid_arguments_fail_and_audit(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    with pytest.raises(ToolValidationError, match="missing required argument"):
        service.execute(request("filesystem.read"))
    assert recorder.types[0] == TOOL_REQUESTED
    assert recorder.types[-1] == TOOL_FAILED


def test_medium_risk_without_approval_denied(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    target = tmp_path / "out.txt"
    with pytest.raises(ToolPermissionDeniedError, match="approval"):
        service.execute(request("filesystem.write", path=str(target), content="x"))
    assert recorder.types == SEQ_ASKED + [TOOL_DENIED]
    assert recorder.count(TOOL_DENIED) == 1


def test_low_risk_auto_allowed_in_normal(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    target = tmp_path / "readme.md"
    target.write_text("hello")
    result = service.execute(request("filesystem.read", path=str(target)))
    assert result.success
    assert recorder.types == SEQ_ALLOWED


def test_low_risk_asks_when_auto_approve_disabled(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path, allow_auto_approve_read=False)
    target = tmp_path / "readme.md"
    target.write_text("hello")
    with pytest.raises(ToolPermissionDeniedError):
        service.execute(request("filesystem.read", path=str(target)))
    assert recorder.types == SEQ_ASKED + [TOOL_DENIED]


def test_approved_execution_runs_post_policy(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    provider = RecordingApprovalProvider(ApprovalOutcome.APPROVED)
    service.approval = provider  # type: ignore[assignment]
    target = tmp_path / "approved.txt"
    result = service.execute(
        request("filesystem.write", path=str(target), content="approved")
    )
    assert result.success
    assert target.read_text(encoding="utf-8") == "approved"
    assert provider.decisions[0].tool_id == "filesystem.write"
    assert recorder.types == [
        TOOL_REQUESTED, TOOL_APPROVAL_REQUESTED, TOOL_APPROVED,
        TOOL_ALLOWED, TOOL_STARTED, TOOL_COMPLETED,
    ]


def test_approval_rejection_denies_and_audits(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    service.approval = RecordingApprovalProvider(  # type: ignore[assignment]
        ApprovalOutcome.DENIED
    )
    target = tmp_path / "rejected.txt"
    with pytest.raises(ToolPermissionDeniedError, match="approval denied"):
        service.execute(request("filesystem.write", path=str(target), content="x"))
    assert recorder.types == SEQ_ASKED + [TOOL_REJECTED, TOOL_DENIED]


def test_approval_timeout_denies(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    service.approval = RecordingApprovalProvider(  # type: ignore[assignment]
        ApprovalOutcome.TIMEOUT
    )
    target = tmp_path / "timedout.txt"
    with pytest.raises(ToolPermissionDeniedError, match="approval timeout"):
        service.execute(request("filesystem.write", path=str(target), content="x"))


def test_policy_path_denial_with_reason(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    with pytest.raises(ToolPermissionDeniedError, match="allowed roots"):
        service.execute(
            request("filesystem.read", path=str(tmp_path.parent / "sneaky.txt"))
        )
    assert recorder.count(TOOL_DENIED) == 1
    denied = [e for e in recorder.events if e.type == TOOL_DENIED][0]
    assert "allowed roots" in denied.payload["reason"]


def test_protected_file_denied_via_policy(tmp_path: Path) -> None:
    target = tmp_path / "memory.db"
    target.write_text("secrets")
    service, recorder = make_service(tmp_path)
    with pytest.raises(ToolPermissionDeniedError, match="protected file"):
        service.execute(request("filesystem.read", path=str(target)))
    assert recorder.count(TOOL_DENIED) == 1


def test_shell_dangerous_command_denied_before_execution(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    with pytest.raises(ToolPermissionDeniedError, match="dangerous"):
        service.execute(request("shell.execute", command=["del", "x.txt"]))
    assert recorder.count(TOOL_DENIED) == 1


def test_shell_execute_requires_approval_in_normal_mode(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    with pytest.raises(ToolPermissionDeniedError, match="approval"):
        service.execute(
            request("shell.execute", command=[sys.executable, "--version"])
        )
    assert recorder.types == SEQ_ASKED + [TOOL_DENIED]


def test_execution_failure_reported_and_audited(tmp_path: Path) -> None:
    service, recorder = make_service(tmp_path)
    result = service.execute(
        request("filesystem.read", path=str(tmp_path / "missing.txt"))
    )
    assert result.success is False
    assert "does not exist" in (result.error or "")
    assert recorder.types == [TOOL_REQUESTED, TOOL_ALLOWED, TOOL_STARTED, TOOL_FAILED]
    assert recorder.count(TOOL_COMPLETED) == 0


def test_output_limit_truncates_result(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, max_output_bytes=100)
    result = service.execute(request("system.info"))
    assert result.success
    assert result.metadata.get("output_truncated") is True
    assert result.output.get("_truncated") is True


def test_health_check_registration(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    registry = HealthRegistry()
    service.register_health_check(registry)
    assert "tools" in registry.components
    assert registry.check("tools").status is HealthStatus.HEALTHY
    service.shutdown()
    assert registry.check("tools").status is HealthStatus.HEALTHY


def test_health_check_unhealthy_when_unavailable(tmp_path: Path) -> None:
    service = ToolService()
    registry = HealthRegistry()
    service.register_health_check(registry)
    assert registry.check("tools").status is HealthStatus.UNHEALTHY


def test_publisher_never_raises(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)

    def boom(_event: object) -> None:
        raise RuntimeError("boom")

    service.publisher = boom
    result = service.execute(request("system.info"))
    assert result.success


def test_registry_access_and_defaults(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    ids = service.registry().list_ids()
    for tool_id in ("filesystem.list", "filesystem.stat", "filesystem.read",
                    "filesystem.mkdir", "filesystem.write", "process.list",
                    "process.info", "system.info", "shell.execute",
                    "voice.listen", "voice.speak", "voice.wake",
                    "vision.capture", "vision.describe"):
        assert tool_id in ids
    assert "filesystem.delete" not in ids
    assert "process.terminate" not in ids
    assert service.health()["mode"] == "normal"
