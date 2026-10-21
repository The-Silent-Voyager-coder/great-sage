"""IntelligenceService facade tests: config-driven construction, routing
through the facade, event publication, failure isolation, runtime wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from greatsage.configuration.loader import load_config
from greatsage.core.runtime import Runtime
from greatsage.events.models import (
    AI_PROVIDER_SELECTED,
    AI_PROVIDER_UNAVAILABLE,
    AI_REQUEST_COMPLETED,
    AI_REQUEST_FAILED,
    AI_REQUEST_STARTED,
    AI_STREAM_COMPLETED,
    AI_STREAM_STARTED,
    Event,
)
from greatsage.exceptions import ProviderError, RoutingError
from greatsage.intelligence.models import AIRequest, Message
from greatsage.intelligence.provider import ProviderState
from greatsage.intelligence.service import IntelligenceService


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def service() -> IntelligenceService:
    return IntelligenceService()


def test_start_without_config_registers_nothing(service: IntelligenceService) -> None:
    service.start(None)
    assert service.registry.ids() == ()
    assert service.router is not None


def test_start_with_mock(service: IntelligenceService) -> None:
    service.register_mock()
    service.start(None)
    assert service.registry.get("mock").state is ProviderState.READY
    health = service.health()
    assert health["mock"].ok


def test_generate_via_facade(service: IntelligenceService) -> None:
    events: list[str] = []
    service.publisher = _dropping_publisher("intelligence", events)
    service.register_mock()
    service.start(None)
    response = service.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.content == "mock:hi"
    assert AI_REQUEST_STARTED in events
    assert AI_REQUEST_COMPLETED in events
    assert AI_PROVIDER_SELECTED in events


def test_generate_failure_publishes_failure(service: IntelligenceService) -> None:
    events: list[str] = []
    service.publisher = _dropping_publisher("intelligence", events)
    service.register_mock()
    service.start(None)
    service.registry.get("mock").fail_on_generate = True
    with pytest.raises(ProviderError):
        service.generate(AIRequest(messages=[Message.user("hi")]))
    assert AI_REQUEST_FAILED in events


def test_generate_completion_event_has_no_content(
    service: IntelligenceService,
) -> None:
    events: list[Event] = []
    service.publisher = lambda event: events.append(event)
    service.register_mock()
    service.start(None)

    response = service.generate(AIRequest(messages=[Message.user("secret prompt")]))

    completed = next(event for event in events if event.type == AI_REQUEST_COMPLETED)
    assert "content" not in completed.payload
    assert completed.payload["content_length"] == len(response.content)
    assert "secret prompt" not in str(completed.payload)
    assert response.content not in str(completed.payload)


def test_generate_validation_rejected(service: IntelligenceService) -> None:
    service.register_mock()
    service.start(None)
    with pytest.raises(Exception, match="at least one message"):
        service.generate(AIRequest(messages=[]))


def test_unavailable_provider_emits_event(tmp_path: Path) -> None:
    """An unreachable configured provider publishes AIProviderUnavailable at
    startup without crashing the service."""
    events: list[str] = []
    config_path = tmp_path / "intelligence.yaml"
    config_path.write_text(_config_yaml(tmp_path), encoding="utf-8")
    loaded = load_config(config_path).config
    service = IntelligenceService()
    service.publisher = _dropping_publisher("intelligence", events)
    service.start(loaded)
    assert AI_PROVIDER_UNAVAILABLE in events
    health = service.health()
    assert health["local"].ok is False
    service.shutdown()


def test_health_check_registration(service: IntelligenceService) -> None:
    from greatsage.core.health import HealthRegistry

    registry = HealthRegistry()
    service.register_mock()
    service.start(None)
    service.register_health_check(registry)
    report = registry.check("intelligence")
    assert report.status.value == "HEALTHY"


def test_stream_via_facade(service: IntelligenceService) -> None:
    from greatsage.intelligence.provider import Capability

    events: list[str] = []
    service.publisher = _dropping_publisher("intelligence", events)
    service.register_mock(capabilities={Capability.STREAMING})
    service.start(None)

    async def scenario():
        request = AIRequest(messages=[Message.user("stream words")])
        chunks = [chunk async for chunk in service.stream(request)]
        return chunks

    chunks = run(scenario())
    text = "".join(c.text for c in chunks if c.kind == "text")
    assert text
    assert any(c.kind == "completion" for c in chunks)
    assert AI_STREAM_STARTED in events
    assert AI_STREAM_COMPLETED in events


def test_stream_completion_event_has_no_content(service: IntelligenceService) -> None:
    from greatsage.intelligence.provider import Capability

    events: list[Event] = []
    service.publisher = lambda event: events.append(event)
    service.register_mock(capabilities={Capability.STREAMING})
    service.start(None)

    async def scenario():
        request = AIRequest(messages=[Message.user("stream secret")])
        return [chunk async for chunk in service.stream(request)]

    chunks = run(scenario())
    text = "".join(c.text for c in chunks if c.kind == "text")
    completed = next(event for event in events if event.type == AI_STREAM_COMPLETED)
    assert "content" not in completed.payload
    assert completed.payload["content_length"] == len(text)
    assert "stream secret" not in str(completed.payload)
    assert text not in str(completed.payload)


def test_stop_marks_providers_stopped(service: IntelligenceService) -> None:
    service.register_mock()
    service.start(None)
    service.shutdown()
    assert service.registry.get("mock").state is ProviderState.STOPPED


def _dropping_publisher(source: str, collected: list[str]):
    def publish(event: Event) -> None:
        collected.append(event.type)
        assert event.source == source
        assert event.payload is not None

    return publish


# --- runtime integration ---------------------------------------------


def _config_yaml(tmp_path: Path) -> str:
    d = str(tmp_path).replace("\\", "/")
    return f"""
core:
  name: "intelligence test"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "INFO"
  retention_days: 1
ai:
  default_provider: "local"
  providers:
    local:
      type: "local"
      enabled: true
      base_url: "http://127.0.0.1:1"
      model: ""
      timeout_seconds: 1
    opencode:
      type: "opencode"
      enabled: false
      base_url: "http://127.0.0.1:4096"
      api_key_env: ""
      model: ""
      timeout_seconds: 1
"""


def _runtime(tmp_path: Path) -> Runtime:
    config_path = tmp_path / "intelligence.yaml"
    config_path.write_text(_config_yaml(tmp_path), encoding="utf-8")
    return Runtime(load_config(config_path).config)


def test_runtime_starts_with_unavailable_provider(tmp_path: Path) -> None:
    """Ollama pointed at a dead port must degrade, never crash startup."""
    runtime = _runtime(tmp_path)

    async def scenario() -> None:
        await runtime.start()
        assert runtime.state.value == "running"
        health = runtime.intelligence.health()
        assert "local" in health
        assert health["local"].ok is False
        await runtime.stop()

    run(scenario())


def test_runtime_health_includes_intelligence(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    async def scenario() -> None:
        await runtime.start()
        report = runtime.health_report()
        components = {r.component for r in report}
        assert "intelligence" in components
        await runtime.stop()

    run(scenario())


def test_runtime_generate_without_provider_raises_routing_error(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    async def scenario() -> None:
        await runtime.start()
        with pytest.raises(RoutingError):
            runtime.intelligence.generate(AIRequest(messages=[Message.user("hi")]))
        await runtime.stop()

    run(scenario())


def test_runtime_publishes_events_when_generating(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    received: list[str] = []

    async def scenario() -> None:
        await runtime.start()
        runtime.intelligence.register_mock()
        runtime.intelligence.registry.initialize("mock")
        runtime.bus.subscribe(lambda event: received.append(event.type))
        runtime.intelligence.generate(
            AIRequest(messages=[Message.user("hi")], metadata={"provider": "mock"})
        )
        await asyncio.sleep(0.1)  # let publish_nowait tasks dispatch
        await runtime.stop()

    run(scenario())
    assert AI_REQUEST_STARTED in received
    assert AI_REQUEST_COMPLETED in received
