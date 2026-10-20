"""Event bus tests: delivery, filtering, unsubscribe, isolation, order, shutdown."""

from __future__ import annotations

import asyncio

import pytest

from greatsage.events.bus import EventBus
from greatsage.events.models import TASK_STARTED, TOOL_COMPLETED, TOOL_STARTED, Event
from greatsage.exceptions import EventError


def run(coro):
    return asyncio.run(coro)


def test_publication_delivers_to_subscriber() -> None:
    async def scenario() -> None:
        bus = EventBus()
        received: list[Event] = []
        bus.subscribe(received.append, event_type=TASK_STARTED)
        await bus.publish(Event(type=TASK_STARTED, source="test"))
        assert len(received) == 1
        assert received[0].type == TASK_STARTED
        assert received[0].source == "test"
        assert received[0].id
        assert received[0].session_id is None

    run(scenario())


def test_type_filter_skips_others() -> None:
    async def scenario() -> None:
        bus = EventBus()
        received: list[str] = []
        bus.subscribe(lambda e: received.append(e.type), event_type=TOOL_STARTED)
        await bus.publish(Event(type=TOOL_COMPLETED, source="test"))
        assert received == []

    run(scenario())


def test_unsubscribe_stops_delivery() -> None:
    async def scenario() -> None:
        bus = EventBus()
        received: list[str] = []
        sub = bus.subscribe(lambda e: received.append(e.type), event_type=TOOL_COMPLETED)
        assert bus.unsubscribe(sub) is True
        await bus.publish(Event(type=TOOL_COMPLETED, source="test"))
        assert received == []
        assert bus.unsubscribe(sub.id) is False

    run(scenario())


def test_multiple_subscribers_all_receive() -> None:
    async def scenario() -> None:
        bus = EventBus()
        first: list[str] = []
        second: list[str] = []
        bus.subscribe(lambda e: first.append(e.type), event_type=TASK_STARTED)
        bus.subscribe(lambda e: second.append(e.type), event_type=TASK_STARTED)
        await bus.publish(Event(type=TASK_STARTED, source="test"))
        assert first == [TASK_STARTED]
        assert second == [TASK_STARTED]

    run(scenario())


def test_subscriber_failure_isolation() -> None:
    async def scenario() -> None:
        bus = EventBus()
        received: list[str] = []

        def broken(_event: Event) -> None:
            raise RuntimeError("subscriber boom")

        bus.subscribe(broken, event_type=TASK_STARTED)
        bus.subscribe(lambda e: received.append(e.type), event_type=TASK_STARTED)
        await bus.publish(Event(type=TASK_STARTED, source="test"))
        assert received == [TASK_STARTED]
        await bus.publish(Event(type=TASK_STARTED, source="test"))
        assert received == [TASK_STARTED, TASK_STARTED]

    run(scenario())


def test_ordering_preserved() -> None:
    async def scenario() -> None:
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe(lambda e: seen.append(e.type))
        for event_type in (TASK_STARTED, TOOL_STARTED, TOOL_COMPLETED):
            await bus.publish(Event(type=event_type, source="test"))
        assert seen == [TASK_STARTED, TOOL_STARTED, TOOL_COMPLETED]

    run(scenario())


def test_close_rejects_publish_and_subscribe() -> None:
    async def scenario() -> None:
        bus = EventBus()
        await bus.close()
        with pytest.raises(EventError, match="closed"):
            bus.subscribe(lambda e: None)
        with pytest.raises(EventError, match="closed"):
            await bus.publish(Event(type=TASK_STARTED, source="test"))

    run(scenario())


def test_async_subscriber_awaited() -> None:
    async def scenario() -> None:
        bus = EventBus()
        done = asyncio.Event()

        async def handler(_event: Event) -> None:
            await asyncio.sleep(0.01)
            done.set()

        bus.subscribe(handler, event_type=TASK_STARTED)
        await bus.publish(Event(type=TASK_STARTED, source="test"))
        assert done.is_set()

    run(scenario())


def test_publish_nowait() -> None:
    async def scenario() -> None:
        bus = EventBus()
        received: list[Event] = []
        bus.subscribe(received.append, event_type=TASK_STARTED)
        bus.publish_nowait(Event(type=TASK_STARTED, source="test"))
        await asyncio.sleep(0.01)
        assert len(received) == 1

    run(scenario())


def test_publish_nowait_without_loop_dispatches_sync_subscriber() -> None:
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append, event_type=TASK_STARTED)
    bus.publish_nowait(Event(type=TASK_STARTED, source="cli"))
    assert [event.type for event in received] == [TASK_STARTED]
    assert received[0].source == "cli"
    assert bus.published_count == 1


def test_publish_nowait_without_loop_skips_async_subscriber() -> None:
    bus = EventBus()
    called: list[Event] = []

    async def handler(event: Event) -> None:
        called.append(event)

    bus.subscribe(handler, event_type=TASK_STARTED)
    bus.publish_nowait(Event(type=TASK_STARTED, source="cli"))
    assert called == []


def test_publish_nowait_closed_bus_drops_silently() -> None:
    async def scenario() -> None:
        bus = EventBus()
        await bus.close()
        # Must not raise and must not leave a failing task behind.
        bus.publish_nowait(Event(type=TASK_STARTED, source="test"))
        await asyncio.sleep(0.01)

    run(scenario())


def test_event_serialization_roundtrip() -> None:
    event = Event(type=TASK_STARTED, source="test", session_id="s1", task_id="t1", payload={"a": 1})
    restored = Event.from_dict(event.to_dict())
    assert restored == event


def test_clear_removes_subscribers() -> None:
    async def scenario() -> None:
        bus = EventBus()
        received: list[str] = []
        bus.subscribe(lambda e: received.append(e.type))
        bus.clear()
        await bus.publish(Event(type=TASK_STARTED, source="test"))
        assert received == []

    run(scenario())
