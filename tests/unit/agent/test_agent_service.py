"""AgentService facade tests: availability, gating, limits, approval wiring,
memory retrieval, cancellation, and health."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from greatsage.agent.approval import AgentApprovalProvider
from greatsage.agent.approval_broker import ApprovalBroker
from greatsage.agent.history import AgentHistoryError
from greatsage.agent.models import AgentState
from greatsage.agent.service import AgentService
from greatsage.configuration.loader import load_config
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.delegation.models import (
    DelegationEvent,
    DelegationEventKind,
    DelegationRequest,
    DelegationState,
)
from greatsage.exceptions import (
    AgentUnavailableError,
    AgentValidationError,
    DelegationUnavailableError,
    ProviderCapabilityError,
    ProviderUnavailableError,
)
from greatsage.intelligence.provider import (
    Capability,
    ProviderCapabilities,
    ProviderState,
)
from greatsage.tools.approval import DeterministicApprovalProvider
from greatsage.tools.models import ApprovalOutcome
from tests.unit.agent._fakes import (
    FakeIntelligence,
    FakeProvider,
    FakeProviderRegistry,
    FakeTools,
    gate_handler,
    text_handler,
    tool_handler,
)


class FakeIntelligenceService(FakeIntelligence):
    """FakeIntelligence plus the provider registry the service gate needs."""

    def __init__(
        self,
        registry: FakeProviderRegistry,
        handlers: list | None = None,
        *,
        keep_last: bool = True,
    ) -> None:
        super().__init__(handlers, keep_last=keep_last)
        self.registry = registry


class _MemoryItem:
    def __init__(self, memory_id: str, content: str = "reference") -> None:
        self.id = memory_id
        self.content = content
        self.confidence = 0.9
        self.memory_type = _MemoryType.LONG_TERM


class _MemoryType:
    LONG_TERM = type("_MemoryTypeValue", (), {"value": "long_term"})()


class _Ranked:
    def __init__(self, memory) -> None:
        self.memory = memory


class _Retrieval:
    def __init__(self, items: list) -> None:
        self.items = items


class FakeMemoryService:
    def __init__(self, *, items: list | None = None, fail: bool = False) -> None:
        self._items = items or []
        self._fail = fail
        self.calls: list[tuple] = []

    def retrieve(self, query=None, *, session_id=None, limit=None, **kwargs):
        self.calls.append((query, session_id, limit))
        if self._fail:
            raise RuntimeError("memory database corrupted")
        return _Retrieval([_Ranked(item) for item in self._items])


def _config(tmp_path: Path, *, agent_enabled: bool = True) -> object:
    path = tmp_path / "sage.yaml"
    d = str(tmp_path).replace("\\", "/")
    path.write_text(
        f"""
core:
  name: "Great Sage Test"
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
agent:
  enabled: {"true" if agent_enabled else "false"}
  max_steps: 12
  max_tool_calls: 8
  max_wall_time_seconds: 300.0
  max_single_tool_calls: 3
  max_total_tool_output_bytes: 2097152
  loop_detection_threshold: 3
""",
        encoding="utf-8",
    )
    return load_config(path, environ={}).config


def _service(tmp_path: Path, *, enabled: bool = True) -> AgentService:
    registry = FakeProviderRegistry({"local": FakeProvider("local")})
    service = AgentService(
        intelligence=FakeIntelligenceService(registry),
        tools=FakeTools(),
    )
    service.start(_config(tmp_path, agent_enabled=enabled))
    return service


def test_run_before_start_raises_unavailable() -> None:
    service = AgentService()
    with pytest.raises(AgentUnavailableError, match="not started"):
        service.run("do something")


def test_unwired_dependencies_raise_unavailable(tmp_path: Path) -> None:
    service = AgentService()
    service.start(_config(tmp_path))
    with pytest.raises(AgentUnavailableError, match="not wired"):
        service.run("do something")


def test_disabled_by_config_raises_unavailable(tmp_path: Path) -> None:
    service = AgentService()
    service.start(_config(tmp_path, agent_enabled=False))
    with pytest.raises(AgentUnavailableError, match="disabled"):
        service.run("do something")


def test_empty_prompt_raises_validation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(AgentValidationError, match="non-empty prompt"):
        service.run("   ")


def test_unknown_provider_raises_unavailable(tmp_path: Path) -> None:
    service = AgentService(
        intelligence=FakeIntelligenceService(FakeProviderRegistry()),
        tools=FakeTools(),
    )
    service.start(_config(tmp_path))
    with pytest.raises(ProviderUnavailableError, match="not registered"):
        service.run("do something")


def test_unready_provider_raises_unavailable(tmp_path: Path) -> None:
    provider = FakeProvider("local", state=ProviderState.UNAVAILABLE)
    service = AgentService(
        intelligence=FakeIntelligenceService(FakeProviderRegistry({"local": provider})),
        tools=FakeTools(),
    )
    service.start(_config(tmp_path))
    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        service.run("do something")


def test_provider_without_tool_calling_raises_capability(tmp_path: Path) -> None:
    provider = FakeProvider("local", tool_calling=False)
    service = AgentService(
        intelligence=FakeIntelligenceService(FakeProviderRegistry({"local": provider})),
        tools=FakeTools(),
    )
    service.start(_config(tmp_path))
    with pytest.raises(ProviderCapabilityError, match="tool calling"):
        service.run("do something")


def test_plain_run_completes(tmp_path: Path) -> None:
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [text_handler("understood")],
        keep_last=False,
    )
    service = AgentService(intelligence=intelligence, tools=FakeTools())
    service.start(_config(tmp_path))
    result = service.run("remember this fact")
    assert result.state is AgentState.COMPLETED
    assert result.final_text == "understood"
    assert result.provider == "fake"
    assert result.model == "fake-model"
    assert result.tool_calls == 0
    assert result.steps
    assert service.health()["current"] == {}
    assert intelligence.requests
    history = service.history()
    assert len(history) == 1
    assert history[0]["task_id"] == result.task_id
    assert history[0]["state"] == "completed"
    assert "prompt" not in history[0]
    assert "final_text" not in history[0]
    service.shutdown()
    restarted = AgentService()
    restarted.start(_config(tmp_path))
    assert restarted.history()[0]["task_id"] == result.task_id
    restarted.shutdown()


def test_history_failure_disables_history_but_not_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self) -> None:  # noqa: ANN001
        raise AgentHistoryError("history database is locked")

    monkeypatch.setattr(
        "greatsage.agent.service.AgentRunStore.initialize", fail
    )
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [text_handler("still works")],
        keep_last=False,
    )
    service = AgentService(intelligence=intelligence, tools=FakeTools())
    service.start(_config(tmp_path))

    result = service.run("run anyway")

    assert result.state is AgentState.COMPLETED
    assert result.final_text == "still works"
    assert service.history() == []
    assert service.history_health()["available"] is False


def test_max_steps_override_is_applied(tmp_path: Path) -> None:
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [tool_handler("filesystem.list")],
        keep_last=True,
    )
    service = AgentService(intelligence=intelligence, tools=FakeTools())
    service.start(_config(tmp_path))
    result = service.run("keep calling", max_steps=2)
    assert result.state is AgentState.LIMIT_REACHED
    assert result.reason == "max_steps"
    assert result.tool_calls == 2


def test_approval_provider_is_installed_and_restored(tmp_path: Path) -> None:
    installed_before = DeterministicApprovalProvider(ApprovalOutcome.APPROVED)
    tools = FakeTools(approval=installed_before)
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [tool_handler("filesystem.list"), text_handler("done")],
        keep_last=False,
    )
    service = AgentService(intelligence=intelligence, tools=tools)
    service.start(_config(tmp_path))
    result = service.run("call the tool", max_steps=2)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1
    assert tools.approval is installed_before


def test_run_with_custom_approval_decides(tmp_path: Path) -> None:
    tools = FakeTools()
    tools.mark_requires_approval("filesystem.list")
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [tool_handler("filesystem.list"), text_handler("done")],
        keep_last=False,
    )
    service = AgentService(intelligence=intelligence, tools=tools)
    service.start(_config(tmp_path))
    approval = AgentApprovalProvider(wait_seconds=5.0)
    approval.approve("call_1")
    result = service.run("call the tool", max_steps=2, approval=approval)
    assert result.state is AgentState.COMPLETED
    assert result.tool_calls == 1


def test_broker_approval_marks_waiting_and_completes(tmp_path: Path) -> None:
    tools = FakeTools()
    tools.mark_requires_approval("filesystem.list")
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [tool_handler("filesystem.list"), text_handler("done")],
        keep_last=False,
    )
    service = AgentService(intelligence=intelligence, tools=tools)
    service.start(_config(tmp_path))
    broker = ApprovalBroker(wait_seconds=5.0)
    outcome: dict = {}

    def worker() -> None:
        outcome["result"] = service.run("call the tool", max_steps=2, approval=broker)

    thread = threading.Thread(target=worker)
    thread.start()
    pending = broker.wait_for_pending(timeout=5.0)
    assert pending is not None
    assert service.health()["current"]["state"] == AgentState.WAITING_FOR_APPROVAL.value
    assert broker.approve(pending.request_id) is True
    thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert outcome["result"].state is AgentState.COMPLETED
    assert service.health()["current"] == {}


def test_memory_retrieval_is_bounded_and_plugged(tmp_path: Path) -> None:
    memory = FakeMemoryService(items=[_MemoryItem("m1"), _MemoryItem("m2")])
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [text_handler("done")],
        keep_last=False,
    )
    service = AgentService(intelligence=intelligence, tools=FakeTools(), memory=memory)
    service.start(_config(tmp_path))
    result = service.run("what do you remember", session_id="s1")
    assert result.state is AgentState.COMPLETED
    assert memory.calls == [("what do you remember", "s1", 5)]


def test_memory_failure_degrades_silently(tmp_path: Path) -> None:
    service = AgentService(
        intelligence=FakeIntelligenceService(
            FakeProviderRegistry({"local": FakeProvider("local")}),
            [text_handler("done")],
            keep_last=False,
        ),
        tools=FakeTools(),
        memory=FakeMemoryService(fail=True),
    )
    service.start(_config(tmp_path))
    result = service.run("do it anyway")
    assert result.state is AgentState.COMPLETED


def test_cancel_during_tool_call(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [gate_handler(entered, release, tool_call=True)],
        keep_last=False,
    )
    service = AgentService(intelligence=intelligence, tools=FakeTools())
    service.start(_config(tmp_path))
    outcome: dict = {}

    def worker() -> None:
        outcome["result"] = service.run("blocked call")

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=5)
    service.cancel()
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    result = outcome["result"]
    assert result.state is AgentState.CANCELLED
    assert result.reason == "cancelled"
    assert service.health()["current"] == {}


def test_cancel_without_run_is_safe(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.cancel()
    assert service.health()["available"]


def test_health_lifecycle(tmp_path: Path) -> None:
    service = AgentService()
    before = service.health()
    assert before == {
        "available": False,
        "status": "unavailable",
        "detail": "agent service not started",
        "enabled": False,
        "current": {},
        "history": {
            "available": False,
            "schema_version": 0,
            "database_path": "",
            "runs": 0,
            "detail": "agent history not initialized",
        },
    }
    service.start(_config(tmp_path))
    healthy = service.health()
    assert healthy["available"] is True
    assert healthy["status"] == "healthy"
    assert "steps<=" in healthy["detail"]
    assert healthy["enabled"] is True
    assert healthy["current"] == {}
    assert healthy["history"]["available"] is True
    service.shutdown()
    stopped = service.health()
    assert stopped["status"] == "disabled"
    assert stopped["detail"] == "agent service stopped"


def test_health_during_run_reports_current(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    intelligence = FakeIntelligenceService(
        FakeProviderRegistry({"local": FakeProvider("local")}),
        [gate_handler(entered, release)],
        keep_last=False,
    )
    service = AgentService(intelligence=intelligence, tools=FakeTools())
    service.start(_config(tmp_path))
    outcome: dict = {}

    def worker() -> None:
        outcome["result"] = service.run("blocked")

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=5)
    current = service.health()["current"]
    assert current["state"] == AgentState.RUNNING.value
    assert current["steps"] == 0
    release.set()
    thread.join(timeout=10)
    result = outcome["result"]
    assert result.state is AgentState.COMPLETED
    assert service.health()["current"] == {}


def test_health_check_registration(tmp_path: Path) -> None:
    registry = HealthRegistry()
    service = _service(tmp_path)
    service.register_health_check(registry)
    assert registry.check("agent").status is HealthStatus.HEALTHY

    unstarted = AgentService()
    unstarted.register_health_check(registry)
    assert registry.check("agent").status is HealthStatus.UNHEALTHY


def test_shutdown_clears_state(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.shutdown()
    with pytest.raises(AgentUnavailableError):
        service.run("again")


class FakeDelegationProvider(FakeProvider):
    """Delegation-capable provider for the AgentService.delegate path."""

    def __init__(
        self,
        provider_id: str = "delegate-fake",
        *,
        state: ProviderState = ProviderState.READY,
    ) -> None:
        caps = {
            Capability.TEXT_GENERATION,
            Capability.DELEGATION,
        }
        self._capabilities = ProviderCapabilities(capabilities=frozenset(caps))
        self._state = state
        self._provider_id = provider_id
        self.created: list[str] = []
        self.disposed: list[str] = []
        self.prompted: list[tuple[str, str, str | None]] = []

    def create_session(self) -> str:
        session_id = "sess-agent-delegated"
        self.created.append(session_id)
        return session_id

    def send_delegation_prompt(
        self, session_id: str, prompt: str, working_directory: str | None = None
    ) -> None:
        self.prompted.append((session_id, prompt, working_directory))

    def iter_session_events(self, session_id: str):  # type: ignore[no-untyped-def]
        yield DelegationEvent(kind=DelegationEventKind.COMPLETED, message="done")

    def respond_permission(
        self, session_id: str, permission_id: str, approved: bool, remember: bool | None = None
    ) -> None:
        pass

    def abort_session(self, session_id: str) -> None:
        pass

    def get_session_diff(self, session_id: str):  # type: ignore[no-untyped-def]
        return None

    def dispose_session(self, session_id: str) -> None:
        self.disposed.append(session_id)


class _DelegationEvent:
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        self.metadata = {}


def _delegation_config(tmp_path: Path) -> object:
    path = tmp_path / "sage-delegation.yaml"
    d = str(tmp_path).replace("\\", "/")
    path.write_text(
        f"""
core:
  name: "Great Sage Agent Delegate Test"
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
tools:
  working_directory: "{d}/workspace"
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "SYSTEM"
  browser:
    default_risk: "FORBIDDEN"
delegation:
  enabled: true
  default_provider: "delegate-fake"
  max_wall_time_seconds: 30.0
  max_output_bytes: 10000
  max_permission_requests: 50
  max_session_count: 3
  max_delegation_depth: 1
""",
        encoding="utf-8",
    )
    return load_config(path, environ={}).config


def _delegate_service(tmp_path: Path) -> AgentService:
    registry = FakeProviderRegistry({"delegate-fake": FakeDelegationProvider()})
    service = AgentService(
        intelligence=FakeIntelligenceService(registry),
        tools=FakeTools(),
    )
    service.start(_delegation_config(tmp_path))
    return service


def test_delegate_unavailable_without_intelligence() -> None:
    service = AgentService()
    request = DelegationRequest(
        prompt="fix", working_directory=Path("C:/tmp/workspace")
    )
    with pytest.raises(DelegationUnavailableError, match="intelligence"):
        service.delegate(request)


def test_delegate_rejects_depth_above_zero(tmp_path: Path) -> None:
    service = _delegate_service(tmp_path)
    request = DelegationRequest(
        prompt="fix",
        working_directory=tmp_path / "workspace",
        depth=1,
    )
    with pytest.raises(AgentValidationError, match="depth-0"):
        service.delegate(request)


def test_delegate_rejects_provider_without_capability(tmp_path: Path) -> None:
    registry = FakeProviderRegistry({"local": FakeProvider("local")})
    service = AgentService(
        intelligence=FakeIntelligenceService(registry),
        tools=FakeTools(),
    )
    service.start(_delegation_config(tmp_path))
    request = DelegationRequest(
        prompt="fix",
        working_directory=tmp_path / "workspace",
        provider="local",
    )
    with pytest.raises(ProviderCapabilityError, match="no silent fallback"):
        service.delegate(request)


def test_delegate_happy_path(tmp_path: Path) -> None:
    service = _delegate_service(tmp_path)
    result = service.delegate(
        DelegationRequest(prompt="fix", working_directory=tmp_path / "workspace")
    )
    assert result.state is DelegationState.COMPLETED
    assert result.provider == "delegate-fake"


def test_delegate_unavailable_before_start(tmp_path: Path) -> None:
    service = AgentService()
    with pytest.raises(DelegationUnavailableError, match="intelligence"):
        service.delegate(
            DelegationRequest(prompt="fix", working_directory=tmp_path / "workspace")
        )
