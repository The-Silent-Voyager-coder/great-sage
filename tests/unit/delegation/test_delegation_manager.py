"""DelegationManager unit tests against a scripted fake provider.

Offline and deterministic: the fake DelegationProvider is pure in-process
(no network, no real OpenCode server). It exercises session creation,
prompting, permission routing and policy evaluation, output/limit enforcement,
timeout, cancellation, diff collection, cleanup, and event hygiene.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from greatsage.configuration.loader import load_config
from greatsage.delegation.limits import (
    MAX_DELEGATION_DEPTH_DEFAULT,
    POLL_INTERVAL_SECONDS,
)
from greatsage.delegation.manager import DelegationManager, DelegationProvider
from greatsage.delegation.models import (
    DelegationEvent,
    DelegationEventKind,
    DelegationRequest,
    DelegationState,
)
from greatsage.events.models import (
    DELEGATION_COMPLETED,
    DELEGATION_FAILED,
    DELEGATION_PERMISSION_REQUESTED,
    DELEGATION_PERMISSION_RESOLVED,
    DELEGATION_REQUESTED,
    DELEGATION_STARTED,
    TOOL_ALLOWED,
    TOOL_APPROVAL_REQUESTED,
    TOOL_APPROVED,
    TOOL_DENIED,
    TOOL_REJECTED,
    Event,
)
from greatsage.exceptions import (
    DelegationValidationError,
    ProviderCapabilityError,
    ProviderError,
    ProviderUnavailableError,
)
from greatsage.intelligence.models import AIRequest, AIResponse
from greatsage.intelligence.provider import (
    AIProvider,
    Capability,
    ProviderCapabilities,
    ProviderHealth,
    ProviderState,
)
from greatsage.intelligence.registry import ProviderRegistry
from greatsage.tools.approval import DeterministicApprovalProvider
from greatsage.tools.models import ApprovalOutcome


class FakeDelegationProvider(AIProvider, DelegationProvider):
    """In-process provider implementing both AIProvider and DelegationProvider surfaces."""

    def __init__(
        self,
        *,
        provider_id: str = "opencode-fake",
        fail_create: bool = False,
        fail_prompt: bool = False,
        events: list[DelegationEvent] | None = None,
        diff: dict[str, Any] | None = None,
        session_ids: list[str] | None = None,
    ) -> None:
        super().__init__(provider_id)
        self._set_state(ProviderState.READY)
        self._events = events or []
        self._diff = diff
        self._session_ids = list(session_ids or ["sess-fake-1"])
        self.fail_create = fail_create
        self.fail_prompt = fail_prompt
        self.created: list[str] = []
        self.prompted: list[tuple[str, str, str | None]] = []
        self.responded: list[tuple[str, str, bool]] = []
        self.aborted: list[str] = []
        self.diff_fetched: list[str] = []
        self.disposed: list[str] = []
        self._reads = 0

    # --- AIProvider surface ---

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            capabilities=frozenset({Capability.TEXT_GENERATION, Capability.DELEGATION}),
            model_ids=("fake-model",),
        )

    def _probe_health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self._provider_id,
            ok=True,
            state=ProviderState.READY,
        )

    def generate(self, request: AIRequest) -> AIResponse:
        raise AssertionError("fake delegation provider is not used for generation")

    # --- DelegationProvider surface ---

    def create_session(self) -> str:
        if self.fail_create:
            raise ProviderError("session creation failed")
        session_id = self._session_ids[len(self.created)]
        self.created.append(session_id)
        return session_id

    def send_delegation_prompt(
        self, session_id: str, prompt: str, working_directory: str | None = None
    ) -> None:
        if self.fail_prompt:
            raise ProviderError("prompt send failed")
        self.prompted.append((session_id, prompt, working_directory))

    def iter_session_events(self, session_id: str) -> Iterator[DelegationEvent]:
        self._reads += 1
        return iter(self._events)

    def respond_permission(
        self, session_id: str, permission_id: str, approved: bool, remember: bool | None = None
    ) -> None:
        self.responded.append((session_id, permission_id, approved))

    def abort_session(self, session_id: str) -> None:
        self.aborted.append(session_id)

    def get_session_diff(self, session_id: str) -> dict[str, Any] | None:
        self.diff_fetched.append(session_id)
        return self._diff

    def dispose_session(self, session_id: str) -> None:
        self.disposed.append(session_id)


def write_delegation_config(tmp_path: Path, **overrides: Any) -> Path:
    d = str(tmp_path).replace("\\", "/")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    fields = {
        "enabled": True,
        "default_provider": "opencode-fake",
        "max_wall_time_seconds": 30.0,
        "max_output_bytes": 10000,
        "max_permission_requests": 50,
        "max_session_count": 3,
        "max_delegation_depth": MAX_DELEGATION_DEPTH_DEFAULT,
    }
    fields.update(overrides)
    lines = "\n".join(
        f"    {name}: {value}" for name, value in fields.items()
    )
    path = tmp_path / "sage.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Delegation Test"
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
  default_mode: "ask"
  allow_auto_approve_read: true
tools:
  working_directory: "{d}/workspace"
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "SYSTEM"
  browser:
    default_risk: "FORBIDDEN"
delegation:
{lines}
""",
        encoding="utf-8",
    )
    return path


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)


def make_manager(
    tmp_path: Path,
    provider: FakeDelegationProvider,
    *,
    publisher: Recorder | None = None,
    approval: DeterministicApprovalProvider | None = None,
) -> tuple[DelegationManager, ProviderRegistry]:
    config = load_config(write_delegation_config(tmp_path)).config
    registry = ProviderRegistry()
    registry.register(provider)
    manager = DelegationManager(config=config, registry=registry, publisher=publisher)
    manager.approval = approval
    return manager, registry


def approved_wd(tmp_path: Path) -> Path:
    """Working directory inside the test config's allowed root (base)."""
    return tmp_path / "workspace"


class BlockingStreamProvider(FakeDelegationProvider):
    """iter_session_events never terminates: zero-byte PROGRESS forever.

    Keeps the run alive in RUNNING until cancellation or the wall-clock
    deadline, without accumulating output bytes.
    """

    def iter_session_events(self, session_id: str) -> Iterator[DelegationEvent]:
        self._reads += 1
        while True:
            yield DelegationEvent(kind=DelegationEventKind.PROGRESS, message="")
            time.sleep(0.002)


def test_delegate_completed_happy_path(tmp_path: Path) -> None:
    recorder = Recorder()
    provider = FakeDelegationProvider(
        events=[
            DelegationEvent(
                kind=DelegationEventKind.PROGRESS,
                message="parsing types",
            ),
            DelegationEvent(
                kind=DelegationEventKind.COMPLETED,
                message="done",
            ),
        ],
        diff={
            "files_changed": 2,
            "diff": "--- a/x\n+++ b/x\n",
        },
    )
    manager, _ = make_manager(tmp_path, provider, publisher=recorder)
    request = DelegationRequest(
        prompt="refactor the greeting service",
        working_directory=approved_wd(tmp_path),
    )
    result = manager.delegate(request)

    assert result.state is DelegationState.COMPLETED
    assert result.provider == "opencode-fake"
    assert result.files_changed == 2
    assert result.diff_available is True
    assert result.diff == json.dumps(
        {"files_changed": 2, "diff": "--- a/x\n+++ b/x\n"}
    )
    assert result.summary == "done"
    assert provider.created == ["sess-fake-1"]
    assert provider.prompted and provider.prompted[0][0] == "sess-fake-1"
    assert provider.disposed == ["sess-fake-1"]
    assert result.permission_requests == 0

    types = [event.type for event in recorder.events]
    assert DELEGATION_REQUESTED in types  # manager publishes the request itself
    assert DELEGATION_STARTED in types
    assert DELEGATION_COMPLETED in types


def test_delegate_executor_failure(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(
        events=[
            DelegationEvent(kind=DelegationEventKind.FAILED, message="compile broke"),
        ]
    )
    manager, _ = make_manager(tmp_path, provider)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.FAILED
    assert result.reason == "executor_error"
    assert "compile broke" in (result.error or "")
    assert provider.aborted == ["sess-fake-1"]


def test_delegate_session_create_failure(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(fail_create=True)
    manager, _ = make_manager(tmp_path, provider)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.FAILED
    assert result.reason == "session_create_failed"
    assert provider.created == []


def test_delegate_prompt_failure(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(fail_prompt=True)
    manager, _ = make_manager(tmp_path, provider)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.FAILED
    assert result.reason == "prompt_send_failed"
    assert provider.aborted == ["sess-fake-1"]


def test_timeout_finalizes_timed_out(tmp_path: Path) -> None:
    # A provider that never emits a terminal event + tiny wall-clock limit -> timeout.
    provider = BlockingStreamProvider()
    config = load_config(
        write_delegation_config(tmp_path, max_wall_time_seconds=0.05)
    ).config
    registry = ProviderRegistry()
    registry.register(provider)
    manager = DelegationManager(config=config, registry=registry)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.TIMED_OUT
    assert result.reason == "timeout"
    assert provider.aborted == ["sess-fake-1"]
    assert provider.disposed == ["sess-fake-1"]


def test_output_byte_limit_enforced(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(
        events=[
            DelegationEvent(
                kind=DelegationEventKind.PROGRESS,
                message="a" * 5000,
            ),
        ]
    )
    config = load_config(
        write_delegation_config(tmp_path, max_output_bytes=1000)
    ).config
    registry = ProviderRegistry()
    registry.register(provider)
    manager = DelegationManager(config=config, registry=registry)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.FAILED
    assert result.reason == "output_limit"
    assert provider.aborted == ["sess-fake-1"]


def test_permission_allowed_by_policy(tmp_path: Path) -> None:
    # SAFE read permission with allow_auto_approve_read -> auto ALLOW, no approve call.
    recorder = Recorder()
    provider = FakeDelegationProvider(
        events=[
            DelegationEvent(
                kind=DelegationEventKind.PERMISSION_REQUESTED,
                metadata={
                    "permission_id": "perm-1",
                    "action": "read",
                    "path": str(approved_wd(tmp_path) / "notes.txt"),
                },
                message="read notes.txt",
            ),
            DelegationEvent(
                kind=DelegationEventKind.COMPLETED,
                message="done",
            ),
        ]
    )
    manager, _ = make_manager(
        tmp_path,
        provider,
        publisher=recorder,
        approval=DeterministicApprovalProvider(ApprovalOutcome.DENIED),
    )
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.COMPLETED
    assert provider.responded == [("sess-fake-1", "perm-1", True)]
    types = [event.type for event in recorder.events]
    assert DELEGATION_PERMISSION_REQUESTED in types
    assert DELEGATION_PERMISSION_RESOLVED in types
    assert TOOL_ALLOWED in types


def test_permission_denied_by_policy(tmp_path: Path) -> None:
    # HIGH bash permission in normal mode -> ASK -> approval denies.
    recorder = Recorder()
    provider = FakeDelegationProvider(
        events=[
            DelegationEvent(
                kind=DelegationEventKind.PERMISSION_REQUESTED,
                metadata={"permission_id": "perm-1", "action": "bash", "path": None},
                message="run a command",
            ),
            DelegationEvent(
                kind=DelegationEventKind.COMPLETED,
                message="done",
            ),
        ]
    )
    manager, _ = make_manager(
        tmp_path,
        provider,
        publisher=recorder,
        approval=DeterministicApprovalProvider(ApprovalOutcome.DENIED),
    )
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.COMPLETED
    assert provider.responded == [("sess-fake-1", "perm-1", False)]
    types = [event.type for event in recorder.events]
    assert TOOL_APPROVAL_REQUESTED in types
    assert TOOL_REJECTED in types
    assert TOOL_DENIED in types


def test_permission_approved_after_ask(tmp_path: Path) -> None:
    recorder = Recorder()
    provider = FakeDelegationProvider(
        events=[
            DelegationEvent(
                kind=DelegationEventKind.PERMISSION_REQUESTED,
                metadata={"permission_id": "perm-1", "action": "bash", "path": None},
                message="run a command",
            ),
            DelegationEvent(
                kind=DelegationEventKind.COMPLETED,
                message="done",
            ),
        ]
    )
    manager, _ = make_manager(
        tmp_path,
        provider,
        publisher=recorder,
        approval=DeterministicApprovalProvider(ApprovalOutcome.APPROVED),
    )
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.COMPLETED
    assert provider.responded == [("sess-fake-1", "perm-1", True)]
    types = [event.type for event in recorder.events]
    assert TOOL_APPROVED in types
    assert TOOL_ALLOWED in types


def test_no_approval_provider_denies_unknown_permission(tmp_path: Path) -> None:
    # Unknown/unknown-risk permission with no approval wired -> DENIED, fail-closed.
    recorder = Recorder()
    provider = FakeDelegationProvider(
        events=[
            DelegationEvent(
                kind=DelegationEventKind.PERMISSION_REQUESTED,
                metadata={"permission_id": "perm-1", "action": "totally_unknown", "path": None},
                message="something new",
            ),
            DelegationEvent(
                kind=DelegationEventKind.COMPLETED,
                message="done",
            ),
        ]
    )
    manager, _ = make_manager(tmp_path, provider, publisher=recorder, approval=None)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.COMPLETED
    assert provider.responded == [("sess-fake-1", "perm-1", False)]
    types = [event.type for event in recorder.events]
    assert TOOL_APPROVAL_REQUESTED in types


def test_permission_limit_enforced(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(
        events=[
            DelegationEvent(
                kind=DelegationEventKind.PERMISSION_REQUESTED,
                metadata={"permission_id": f"p{i}", "action": "bash", "path": None},
            )
            for i in range(5)
        ]
    )
    config = load_config(
        write_delegation_config(tmp_path, max_permission_requests=2)
    ).config
    registry = ProviderRegistry()
    registry.register(provider)
    manager = DelegationManager(config=config, registry=registry)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.FAILED
    assert result.reason == "permission_limit"


def test_session_count_limit_enforced(tmp_path: Path) -> None:
    # First run holds the single slot on a blocking stream until wall timeout.
    blocker = BlockingStreamProvider(session_ids=["sess-block"])
    config = load_config(
        write_delegation_config(
            tmp_path, max_session_count=1, max_wall_time_seconds=1.0
        )
    ).config
    registry = ProviderRegistry()
    registry.register(blocker)
    manager = DelegationManager(config=config, registry=registry)
    holder: list[Any] = []

    first = DelegationRequest(prompt="first", working_directory=approved_wd(tmp_path))
    t = threading.Thread(
        target=lambda: holder.append(manager.delegate(first)), daemon=True
    )
    t.start()
    time.sleep(0.1)  # let the first run reach the running table
    try:
        # The count check fails closed: a FAILED result, not an exception.
        second = manager.delegate(
            DelegationRequest(prompt="second", working_directory=approved_wd(tmp_path))
        )
        assert second.state is DelegationState.FAILED
        assert second.reason == "session_count"
    finally:
        t.join(timeout=5)

    result = holder[0]
    # The first run ends via the wall-clock deadline (no terminal event).
    assert result.state is DelegationState.TIMED_OUT


def test_depth_rejected_when_limit_was_reached(tmp_path: Path) -> None:
    provider = FakeDelegationProvider()
    manager, _ = make_manager(tmp_path, provider)
    request = DelegationRequest(
        prompt="fix", working_directory=approved_wd(tmp_path), depth=1
    )
    with pytest.raises(DelegationValidationError, match="depth"):
        manager.delegate(request)


def test_working_directory_outside_allowed_roots_denied_without_approval(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(
        events=[DelegationEvent(kind=DelegationEventKind.COMPLETED, message="x")]
    )
    manager, _ = make_manager(tmp_path, provider, approval=None)
    with pytest.raises(DelegationValidationError, match="not approved"):
        manager.delegate(
            DelegationRequest(
                prompt="fix", working_directory=tmp_path.parent / "elsewhere"
            )
        )
    assert provider.created == []
    assert provider.disposed == []


def test_working_directory_inside_allowed_roots_proceeds(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(
        events=[DelegationEvent(kind=DelegationEventKind.COMPLETED, message="x")]
    )
    manager, _ = make_manager(tmp_path, provider)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.COMPLETED
    assert provider.created == ["sess-fake-1"]


def test_cancel_running_task_and_retrieve_status(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    config = load_config(
        write_delegation_config(tmp_path, max_wall_time_seconds=0.5)
    ).config
    registry = ProviderRegistry()
    registry.register(provider)
    manager = DelegationManager(config=config, registry=registry)

    result_holder: list[Any] = []
    thread = threading.Thread(
        target=lambda: result_holder.append(
            manager.delegate(
                DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
            )
        ),
        daemon=True,
    )
    thread.start()
    time.sleep(0.05)
    tasks = manager.list_tasks(limit=10)
    assert any(task["state"] == "running" for task in tasks)
    running = next(task for task in tasks if task["state"] == "running")
    info = manager.cancel(running["task_id"])
    assert info["cancelling"] is True
    thread.join(timeout=5)
    result = result_holder[0]
    assert result.state is DelegationState.CANCELLED
    assert provider.aborted


def test_health_shape(tmp_path: Path) -> None:
    provider = FakeDelegationProvider()
    manager, _ = make_manager(tmp_path, provider)
    health = manager.health()
    assert health["enabled"] is True
    assert health["default_provider"] == "opencode-fake"
    assert health["provider_ready"] is True
    assert health["active_tasks"] == 0
    assert "max_delegation_depth" in health["limits"]


def test_cancel_unknown_task_raises(tmp_path: Path) -> None:
    provider = FakeDelegationProvider()
    manager, _ = make_manager(tmp_path, provider)
    with pytest.raises(DelegationValidationError, match="unknown"):
        manager.cancel("nope")


def test_get_unknown_task_raises(tmp_path: Path) -> None:
    provider = FakeDelegationProvider()
    manager, _ = make_manager(tmp_path, provider)
    with pytest.raises(DelegationValidationError, match="unknown"):
        manager.get("nope")


def test_get_completed_task(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(
        events=[DelegationEvent(kind=DelegationEventKind.COMPLETED, message="x")]
    )
    manager, _ = make_manager(tmp_path, provider)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    snapshot = manager.get(result.task_id)
    assert snapshot["state"] == "completed"


def test_provider_missing_capability_rejected(tmp_path: Path) -> None:
    class NoCapability(FakeDelegationProvider):
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                capabilities=frozenset({Capability.TEXT_GENERATION}),
                model_ids=("fake-model",),
            )

    no_cap = NoCapability(provider_id="opencode-no-cap")
    registry = ProviderRegistry()
    registry.register(no_cap)
    config = load_config(
        write_delegation_config(tmp_path, default_provider="opencode-no-cap")
    ).config
    manager = DelegationManager(config=config, registry=registry)
    with pytest.raises(ProviderCapabilityError, match="no silent fallback"):
        manager.delegate(
            DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
        )


def test_unavailable_provider_rejected(tmp_path: Path) -> None:
    provider = FakeDelegationProvider(provider_id="down-provider")
    provider._set_state(ProviderState.UNAVAILABLE)
    registry = ProviderRegistry()
    registry.register(provider)
    config = load_config(
        write_delegation_config(tmp_path, default_provider="down-provider")
    ).config
    manager = DelegationManager(config=config, registry=registry)
    with pytest.raises(ProviderUnavailableError):
        manager.delegate(
            DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
        )


def test_missing_provider_rejected(tmp_path: Path) -> None:
    provider = FakeDelegationProvider()
    manager, _ = make_manager(tmp_path, provider)
    request = DelegationRequest(
        prompt="fix",
        working_directory=approved_wd(tmp_path),
        provider="absent",
    )
    with pytest.raises(DelegationValidationError, match="not registered"):
        manager.delegate(request)


class DroppingStreamProvider(FakeDelegationProvider):
    """The event stream keeps ending without a terminal event (connection loss)."""

    def iter_session_events(self, session_id: str) -> Iterator[DelegationEvent]:
        self._reads += 1
        return iter([])


def test_connection_loss_empty_stream_fails_after_bounded_reconnects(
    tmp_path: Path,
) -> None:
    provider = DroppingStreamProvider()
    recorder = Recorder()
    manager, _ = make_manager(tmp_path, provider, publisher=recorder)
    manager.poll_interval_seconds = 0.0  # bounded reconnects, no real waiting
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.FAILED
    assert result.reason == "event_connection_lost"
    # Bounded: the manager must stop reconnecting after the limit, not loop forever.
    assert provider._reads <= 1 + 3
    assert provider.disposed == ["sess-fake-1"]
    # The failure surfaces as an audit event for the security bus.
    types = [event.type for event in recorder.events]
    assert DELEGATION_FAILED in types
    failed = next(event for event in recorder.events if event.type == DELEGATION_FAILED)
    assert failed.payload["reason"] == "event_connection_lost"


class ErroringStreamProvider(FakeDelegationProvider):
    """The event stream raises a transport error on every read (connection loss)."""

    def iter_session_events(self, session_id: str) -> Iterator[DelegationEvent]:
        self._reads += 1
        raise urllib.error.URLError("stream lost")


def test_connection_loss_transport_error_fails_after_bounded_reconnects(
    tmp_path: Path,
) -> None:
    provider = ErroringStreamProvider()
    manager, _ = make_manager(tmp_path, provider)
    manager.poll_interval_seconds = 0.0  # bounded reconnects, no real waiting
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.FAILED
    assert result.reason == "event_connection_lost"
    assert provider.aborted == ["sess-fake-1"]
    assert provider.disposed == ["sess-fake-1"]


def test_reconnect_recovers_after_transient_failure(tmp_path: Path) -> None:
    # One failed read, then a healthy stream with a terminal event.
    provider = FakeDelegationProvider(
        events=[DelegationEvent(kind=DelegationEventKind.COMPLETED, message="x")]
    )
    calls = 0
    original = provider.iter_session_events

    def flaky_iter(session_id: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("transient")
        return original(session_id)

    provider.iter_session_events = flaky_iter  # type: ignore[method-assign]
    manager, _ = make_manager(tmp_path, provider)
    manager.poll_interval_seconds = 0.0  # transient failure, no real waiting
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.COMPLETED
    assert calls >= 2


class EventuallyCompletingProvider(FakeDelegationProvider):
    """Two empty sweeps (quiet executor) then a terminal event."""

    def iter_session_events(self, session_id: str) -> Iterator[DelegationEvent]:
        self._reads += 1
        if self._reads < 3:
            return iter([])
        return iter([DelegationEvent(kind=DelegationEventKind.COMPLETED, message="done")])


def test_poll_interval_pauses_between_sweeps(tmp_path: Path) -> None:
    provider = EventuallyCompletingProvider()
    manager, _ = make_manager(tmp_path, provider)
    pauses: list[float] = []
    manager._pause_between_sweeps = lambda: pauses.append(manager.poll_interval_seconds)
    result = manager.delegate(
        DelegationRequest(prompt="fix", working_directory=approved_wd(tmp_path))
    )
    assert result.state is DelegationState.COMPLETED
    # A pause runs after every sweep that did not end the run.
    assert pauses == [POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS]
    assert POLL_INTERVAL_SECONDS > 0
