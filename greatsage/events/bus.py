"""In-process event bus.

Design (Phase 1 contract):

- Subscribers may be sync or async callables; the bus awaits them.
- Dispatch is strictly sequential and in subscription order, which preserves
  event ordering within a stream (TaskCreated → TaskStarted → ...).
- A failing subscriber is isolated: its error is logged and other subscribers
  still receive the event. The bus itself never raises from a handler.
- `close()` is graceful: it rejects further publishes and awaits nothing
  in flight (dispatch is synchronous, so a closed bus is quiescent).
- No external event framework; built on asyncio primitives.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from greatsage.events.models import Event
from greatsage.exceptions import EventError

log = logging.getLogger("greatsage.events.bus")

EventHandler = Callable[[Event], Awaitable[None] | None]


@dataclass(frozen=True)
class Subscription:
    id: str
    event_type: str | None  # None = receive all event types
    handler: EventHandler


class EventBus:
    """Publish/subscribe event bus with ordered, isolated dispatch."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, Subscription] = {}
        self._closed = False
        self._published: int = 0

    # --- subscription ---------------------------------------------------

    def subscribe(self, handler: EventHandler, event_type: str | None = None) -> Subscription:
        """Register a handler. `event_type=None` receives every event."""
        if self._closed:
            raise EventError("cannot subscribe: event bus is closed")
        sub = Subscription(id=uuid.uuid4().hex, event_type=event_type, handler=handler)
        self._subscriptions[sub.id] = sub
        return sub

    def unsubscribe(self, sub: Subscription | str) -> bool:
        """Remove a subscription (by object or id). Returns True if removed."""
        sub_id = sub.id if isinstance(sub, Subscription) else sub
        return self._subscriptions.pop(sub_id, None) is not None

    def clear(self) -> None:
        self._subscriptions.clear()

    # --- publication ----------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Dispatch an event to matching subscribers, preserving order."""
        if self._closed:
            raise EventError("cannot publish: event bus is closed")
        self._published += 1
        for sub in tuple(self._subscriptions.values()):
            if sub.event_type is not None and sub.event_type != event.type:
                continue
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # subscriber isolation
                log.error(
                    "event subscriber failed",
                    extra={"component": "events", "event_id": event.id, "event_type": event.type},
                )
                log.debug("subscriber error detail", exc_info=exc, extra={"component": "events"})

    def publish_nowait(self, event: Event) -> None:
        """Fire-and-forget publish from synchronous code (errors are logged).

        Inside an event loop this schedules a task. In a synchronous context
        (the CLI calls services after ``asyncio.run(runtime.start())`` has
        returned) there is no loop to schedule onto, so synchronous
        subscribers are dispatched inline. Async subscribers cannot be run
        without a loop and are skipped with a warning; dropping their work is
        preferable to dropping the whole event, especially for the audit
        sink. A closed bus drops the event with a debug log.
        """
        if self._closed:
            log.debug(
                "dropping event on closed bus",
                extra={"component": "events", "event_type": event.type},
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._publish_sync(event)
            return
        loop.create_task(self._publish_logged(event))

    def _publish_sync(self, event: Event) -> None:
        """Dispatch to sync subscribers without an event loop.

        This is the CLI path: services run synchronously after the runtime
        event loop has closed. Subscriber failures remain isolated, exactly as
        in the async path.
        """
        self._published += 1
        for sub in tuple(self._subscriptions.values()):
            if sub.event_type is not None and sub.event_type != event.type:
                continue
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    log.warning(
                        "skipping async event subscriber without a running loop",
                        extra={
                            "component": "events",
                            "event_id": event.id,
                            "event_type": event.type,
                        },
                    )
            except Exception as exc:  # subscriber isolation
                log.error(
                    "event subscriber failed",
                    extra={
                        "component": "events",
                        "event_id": event.id,
                        "event_type": event.type,
                    },
                )
                log.debug("subscriber error detail", exc_info=exc, extra={"component": "events"})

    async def _publish_logged(self, event: Event) -> None:
        try:
            await self.publish(event)
        except EventError as exc:
            log.debug(
                "dropping event on closed bus",
                extra={"component": "events", "event_type": event.type},
            )
            log.debug("publish race detail", exc_info=exc, extra={"component": "events"})
        except Exception as exc:  # never die loudly in a fire-and-forget task
            log.error(
                "fire-and-forget publish failed",
                extra={"component": "events", "event_type": event.type},
            )
            log.debug("publish error detail", exc_info=exc, extra={"component": "events"})

    # --- lifecycle ------------------------------------------------------

    async def close(self) -> None:
        """Reject further publishes/subscribes. Idempotent."""
        self._closed = True
        self._subscriptions.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    @property
    def published_count(self) -> int:
        return self._published
