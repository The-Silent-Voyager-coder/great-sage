"""MemoryService tests: business rules, events, ranking, failure isolation."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from greatsage.configuration.loader import load_config
from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import (
    MEMORY_CREATED,
    MEMORY_DELETED,
    MEMORY_EXPIRED,
    MEMORY_RETRIEVED,
    MEMORY_UPDATED,
    Event,
)
from greatsage.exceptions import (
    MemoryNotFoundError,
    MemoryUnavailableError,
    MemoryValidationError,
)
from greatsage.memory.models import MemoryType, Provenance, utcnow
from greatsage.memory.service import MemoryService


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [event.type for event in self.events]

    def payload(self, event_type: str) -> dict[str, Any] | None:
        for event in self.events:
            if event.type == event_type:
                return event.payload
        return None


@pytest.fixture
def config(valid_config_yaml: Path) -> SageConfig:
    return load_config(valid_config_yaml).config


@pytest.fixture
def service(config: SageConfig) -> MemoryService:
    svc = MemoryService()
    svc.start(config)
    yield svc
    svc.shutdown()


@pytest.fixture
def publisher(service: MemoryService) -> RecordingPublisher:
    recorder = RecordingPublisher()
    service.publisher = recorder
    return recorder


def test_start_healthy_creates_database(config: SageConfig) -> None:
    svc = MemoryService()
    svc.start(config)
    assert svc.availability == "healthy"
    assert svc.detail == "memory database ready"
    assert config.memory.database_path.exists()
    svc.shutdown()
    assert svc.availability == "disabled"


def test_start_with_none_config_unavailable() -> None:
    svc = MemoryService()
    svc.start(None)
    assert svc.availability == "unavailable"
    with pytest.raises(MemoryUnavailableError):
        svc.remember("nope")


def test_start_disabled_config(valid_config_yaml: Path) -> None:
    disabled_path = valid_config_yaml.parent / "disabled.yaml"
    disabled_path.write_text(
        valid_config_yaml.read_text(encoding="utf-8").replace(
            "memory:\n  enabled: true",
            "memory:\n  enabled: false",
        ),
        encoding="utf-8",
    )
    svc = MemoryService()
    svc.start(load_config(disabled_path).config)
    registry = HealthRegistry()
    svc.register_health_check(registry)
    report = registry.check("memory")
    assert svc.availability == "disabled"
    assert report.status is HealthStatus.HEALTHY  # disabled is intentional -> green
    with pytest.raises(MemoryUnavailableError):
        svc.remember("nope")
    svc.shutdown()


def test_start_corrupt_database_unavailable(
    config: SageConfig, tmp_path: Path
) -> None:
    from dataclasses import replace

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a real sqlite file.........")
    svc = MemoryService()
    svc.start(replace(config, memory=replace(config.memory, database_path=corrupt)))
    assert svc.availability == "unavailable"
    assert svc.health()["status"] == "unavailable"
    with pytest.raises(MemoryUnavailableError):
        svc.remember("nope")
    svc.shutdown()


def test_remember_defaults(service: MemoryService, publisher: RecordingPublisher) -> None:
    memory = service.remember("prefer dark mode")
    assert memory.memory_type is MemoryType.LONG_TERM
    assert memory.source == "user"
    assert memory.provenance == Provenance.USER_EXPLICIT.value
    assert memory.confidence == 0.8  # config default_confidence
    assert memory.expires_at is None  # LONG_TERM persistent
    assert memory.id.startswith("mem_")
    assert publisher.types() == [MEMORY_CREATED]
    payload = publisher.payload(MEMORY_CREATED)
    assert payload["memory_id"] == memory.id
    assert "content" not in payload  # events never carry content


def test_remember_explicit_values(service: MemoryService) -> None:
    memory = service.remember(
        "espresso only",
        memory_type=MemoryType.SEMANTIC,
        source="manual import",
        provenance=Provenance.IMPORT.value,
        confidence=0.95,
        session_id="s9",
        metadata={"imported": True},
    )
    assert memory.confidence == 0.95
    assert memory.metadata == {"imported": True}
    assert memory.session_id == "s9"
    assert memory.expires_at is None  # SEMANTIC persistent


def test_remember_episodic_working_get_default_expiry(
    service: MemoryService,
) -> None:
    episodic = service.remember(
        "ran a backup",
        memory_type=MemoryType.EPISODIC,
        source="system",
    )
    assert episodic.expires_at is not None
    working = service.remember(
        "session scratch",
        memory_type=MemoryType.WORKING,
        source="user",
    )
    assert working.expires_at is not None


def test_remember_validation_errors(service: MemoryService) -> None:
    with pytest.raises(MemoryValidationError, match="confidence"):
        service.remember("x", confidence=1.5)
    with pytest.raises(MemoryValidationError, match="expires_at"):
        service.remember("x", expires_at=utcnow().replace(tzinfo=None))
    with pytest.raises(MemoryValidationError, match="source"):
        service.remember("x", source="  ")


def test_record_episode(service: MemoryService) -> None:
    memory = service.record_episode("executed terraform plan", action="terraform-plan")
    assert memory.memory_type is MemoryType.EPISODIC
    assert memory.metadata["action"] == "terraform-plan"
    assert memory.provenance == Provenance.SYSTEM_EVENT.value


def test_add_semantic(service: MemoryService) -> None:
    memory = service.add_semantic("company = {name: ACME}", source="intake doc")
    assert memory.memory_type is MemoryType.SEMANTIC
    assert memory.source == "intake doc"
    assert memory.provenance == Provenance.DOCUMENT.value
    assert memory.expires_at is None


def test_retrieve_ranking_order(service: MemoryService) -> None:
    service.remember("coffee preference", confidence=0.9)
    service.remember("coffee machine", confidence=0.4)
    service.remember("coffee beans origin", confidence=0.7)
    result = service.retrieve(query="coffee")
    assert result.total == 3
    scores = [item.score for item in result.items]
    assert scores == sorted(scores, reverse=True)
    assert [item.match_reason for item in result.items] == ["query match: coffee"] * 3
    assert result.items[0].memory.confidence == 0.9


def test_retrieve_list_filters_and_pagination(service: MemoryService) -> None:
    for i in range(7):
        service.remember(
            f"note {i}",
            memory_type=MemoryType.LONG_TERM,
            source=f"src{i % 2}",
        )
    result = service.retrieve(source="src0", limit=2, offset=0)
    assert len(result.items) == 2
    assert result.total == 4  # total counts the full match, not the page
    assert result.items[0].match_reason == "listed"


def test_retrieve_search_finds_structured_content(service: MemoryService) -> None:
    service.remember({"kind": "preference", "value": "low latency"})
    result = service.retrieve(query="latency")
    assert result.total == 1
    assert result.items[0].memory.content["kind"] == "preference"


def test_retrieve_excludes_expired_by_default(service: MemoryService) -> None:
    service.remember(
        "ephemeral fact",
        memory_type=MemoryType.WORKING,
        expires_at=utcnow() - timedelta(days=1),
    )
    assert service.retrieve(query="ephemeral").total == 0
    assert service.retrieve(query="ephemeral", include_expired=True).total == 1


def test_retrieve_deleted_by_default(service: MemoryService) -> None:
    memory = service.remember("disappear me")
    service.forget(memory.id)
    assert service.retrieve(query="disappear").total == 0
    assert service.retrieve(query="disappear", include_deleted=True).total == 1


def test_retrieve_session_filter(service: MemoryService) -> None:
    service.remember("session local", session_id="alpha")
    service.remember("global fact")
    assert service.retrieve(session_id="alpha").total == 2
    assert service.retrieve(session_id="beta").total == 1


def test_retrieve_validation(service: MemoryService) -> None:
    with pytest.raises(MemoryValidationError, match="query"):
        service.retrieve(query="   ")
    with pytest.raises(MemoryValidationError, match="minimum_confidence"):
        service.retrieve(minimum_confidence=2.0)
    with pytest.raises(MemoryValidationError, match="limit"):
        service.retrieve(limit=-1)
    with pytest.raises(MemoryValidationError, match="offset"):
        service.retrieve(offset=-1)


def test_retrieve_emits_event(service: MemoryService, publisher: RecordingPublisher) -> None:
    service.remember("searchable phrase")
    service.retrieve(query="phrase", session_id="s1")
    payload = publisher.payload(MEMORY_RETRIEVED)
    assert payload is not None
    assert payload["query"] == "phrase"
    assert payload["count"] == 1
    assert payload["total"] == 1
    assert len(payload["ids"]) == 1
    assert payload["session_id"] == "s1"


def test_get_and_update(service: MemoryService, publisher: RecordingPublisher) -> None:
    memory = service.remember("original note", confidence=0.7, metadata={"v": 1})
    loaded = service.get(memory.id)
    assert loaded.content == "original note"
    updated = service.update(
        memory.id,
        content="revised note",
        confidence=0.99,
        metadata={"v": 2},
    )
    assert updated.content == "revised note"
    assert updated.confidence == 0.99
    assert updated.metadata == {"v": 2}
    # immutable identity fields
    assert updated.source == memory.source
    assert updated.provenance == memory.provenance
    assert updated.memory_type is memory.memory_type
    assert updated.session_id == memory.session_id
    assert updated.created_at == memory.created_at
    assert updated.updated_at >= memory.updated_at
    assert publisher.types()[-1] == MEMORY_UPDATED
    assert "content" not in publisher.payload(MEMORY_UPDATED)


def test_update_unknown_or_deleted_raises(service: MemoryService) -> None:
    with pytest.raises(MemoryNotFoundError):
        service.update("mem_absent", content="x")
    memory = service.remember("delete me first")
    service.forget(memory.id)
    with pytest.raises(MemoryNotFoundError):
        service.update(memory.id, content="x")


def test_update_invalid_confidence_raises(service: MemoryService) -> None:
    memory = service.remember("stable")
    with pytest.raises(MemoryValidationError, match="confidence"):
        service.update(memory.id, confidence=3.0)


def test_forget_soft_delete_and_events(
    service: MemoryService, publisher: RecordingPublisher
) -> None:
    memory = service.remember("canary")
    service.forget(memory.id)
    assert service.get(memory.id) is None
    assert service.get(memory.id, include_deleted=True) is not None
    assert publisher.types()[-1] == MEMORY_DELETED
    payload = publisher.payload(MEMORY_DELETED)
    assert payload["memory_id"] == memory.id
    assert "content" not in payload
    with pytest.raises(MemoryNotFoundError):
        service.forget(memory.id)


def test_expire_sweep_emits_events(service: MemoryService, publisher: RecordingPublisher) -> None:
    service.remember(
        "stale episode",
        memory_type=MemoryType.EPISODIC,
        expires_at=utcnow() - timedelta(days=1),
    )
    assert service.expire() == 1
    assert publisher.types()[-1] == MEMORY_EXPIRED
    assert service.retrieve(query="stale").total == 0
    assert service.expire() == 0


def test_working_memory_via_service(service: MemoryService) -> None:
    session = service.working("srv-session")
    item_id = session.add("hot context")
    assert session.get(item_id).content == "hot context"
    assert item_id.startswith("wm_")
    assert service.drop_working_session("srv-session") is True
    assert service.working("srv-session").size == 0


def test_stats_and_health(service: MemoryService) -> None:
    service.remember("stat counter")
    stats = service.stats()
    assert stats["total"] == 1
    assert stats["by_type"]["long_term"] == 1
    health = service.health()
    assert health["available"] is True
    assert health["status"] == "healthy"
    assert health["accessible"] is True
    assert health["schema_valid"] is True
    assert health["migrations_current"] is True
    assert health["database_path"].endswith("memory.db")


def test_health_check_registry_mapping(service: MemoryService) -> None:
    registry = HealthRegistry()
    service.register_health_check(registry)
    assert registry.check("memory").status is HealthStatus.HEALTHY


def test_unavailable_maps_to_unhealthy() -> None:
    svc = MemoryService()
    svc.start(None)
    registry = HealthRegistry()
    svc.register_health_check(registry)
    assert registry.check("memory").status is HealthStatus.UNHEALTHY
    assert svc.health()["available"] is False


def test_publisher_failure_is_swallowed(service: MemoryService) -> None:
    def broken(event: Event) -> None:
        raise RuntimeError("bus is down")

    service.publisher = broken
    memory = service.remember("still stored")
    assert service.get(memory.id) is not None


def test_events_never_carry_content(service: MemoryService, publisher: RecordingPublisher) -> None:
    memory = service.remember("top secret phrase", session_id="ssn")
    service.update(memory.id, content="changed secret")
    for event in publisher.events:
        assert "top secret phrase" not in json.dumps(event.payload)
        assert "changed secret" not in json.dumps(event.payload)
        assert event.source == "memory"
