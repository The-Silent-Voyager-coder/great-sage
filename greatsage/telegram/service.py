"""Telegram bridge service (remote chat, laptop stays home)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import TELEGRAM_RECEIVED, TELEGRAM_REPLIED, Event
from greatsage.telegram.bridge import TelegramBridge
from greatsage.telegram.client import TelegramClient, TelegramError, extract_message

log = logging.getLogger("greatsage.telegram.service")


class TelegramService:
    """Owns the bot client + bridge; `listen` runs a bounded poll loop."""

    def __init__(self) -> None:
        self._config: SageConfig | None = None
        self._client: TelegramClient | None = None
        self._bridge: TelegramBridge | None = None
        self._offset: int | None = None
        self._availability: str = "unavailable"
        self._detail: str = "telegram service not started"
        self.publisher: Any = None

    def start(self, config: SageConfig | None = None) -> None:
        self._config = config
        self._client = None
        self._bridge = None
        if config is None:
            self._availability = "unavailable"
            self._detail = "no configuration provided"
            return
        telegram = config.telegram
        if not telegram.enabled:
            self._availability = "disabled"
            self._detail = "telegram bridge disabled by configuration"
            return
        token = (os.environ.get(telegram.token_env) or "").strip()
        if not token:
            self._availability = "unavailable"
            self._detail = f"telegram token missing (set {telegram.token_env})"
            return
        allowed = frozenset(telegram.allowed_chat_ids)
        if not allowed:
            self._availability = "unavailable"
            self._detail = "telegram has no allowed chats configured"
            return
        try:
            self._client = TelegramClient(token, timeout_seconds=telegram.poll_timeout_seconds)
        except TelegramError as exc:
            self._availability = "unavailable"
            self._detail = str(exc)[:200]
            return
        self._bridge = TelegramBridge(allowed_chat_ids=allowed)
        self._availability = "healthy"
        self._detail = f"telegram ready ({len(allowed)} allowed chat(s))"
        log.info("telegram service started", extra={"component": "telegram"})

    def shutdown(self) -> None:
        self._client = None
        self._bridge = None
        self._availability = "disabled"
        self._detail = "telegram service stopped"
        log.info("telegram service stopped", extra={"component": "telegram"})

    def _require_bridge(self) -> tuple[TelegramClient, TelegramBridge]:
        if (
            self._availability != "healthy"
            or self._client is None
            or self._bridge is None
        ):
            raise TelegramError(self._detail or "telegram service not available")
        return self._client, self._bridge

    def listen(
        self,
        *,
        once: bool = False,
        for_seconds: float = 300.0,
        handlers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Poll Telegram and answer allowlisted chats. Always terminates.

        `handlers` maps bridge getter names (briefing/status) to zero-arg
        callables returning reply text; the CLI wires them from the runtime.
        """
        client, bridge = self._require_bridge()
        if handlers:
            from greatsage.telegram.bridge import TelegramBridge as Bridge

            bridge = Bridge(allowed_chat_ids=bridge.allowed_chat_ids, getters=handlers)
        assert self._config is not None
        budget = max(1.0, min(for_seconds, self._config.telegram.max_listen_seconds))
        deadline = time.monotonic() + budget
        received = replied = ignored = 0
        errors = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                updates = client.get_updates(
                    offset=self._offset,
                    long_poll_seconds=min(20, max(1, int(remaining))),
                )
            except TelegramError as exc:
                errors += 1
                log.warning("telegram poll failed: %s", exc, extra={"component": "telegram"})
                time.sleep(min(5.0, max(0.5, remaining / 10)))
                if once:
                    break
                continue
            if not updates and once:
                break
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._offset = max(self._offset or 0, update_id + 1)
                parsed = extract_message(update)
                if parsed is None:
                    continue
                chat_id, message_id, text = parsed
                received += 1
                self._publish(TELEGRAM_RECEIVED, {"chat_id": chat_id, "message_id": message_id})
                reply = bridge.handle(chat_id, text)
                if reply is None:
                    ignored += 1
                    continue
                try:
                    if isinstance(reply, dict):
                        photo = reply.get("photo")
                        caption = reply.get("caption", "")
                        if not isinstance(photo, (bytes, bytearray)):
                            raise TelegramError("bad photo reply from handler")
                        if not isinstance(caption, str):
                            raise TelegramError("bad photo caption from handler")
                        client.send_photo(chat_id, bytes(photo), caption)
                    else:
                        client.send_message(chat_id, reply)
                    replied += 1
                    self._publish(TELEGRAM_REPLIED, {"chat_id": chat_id, "message_id": message_id})
                except TelegramError as exc:
                    errors += 1
                    log.warning("telegram reply failed: %s", exc, extra={"component": "telegram"})
            if once:
                break
        return {"received": received, "replied": replied, "ignored": ignored, "errors": errors}

    def health(self) -> dict[str, Any]:
        return {
            "available": self._availability == "healthy",
            "status": self._availability,
            "detail": self._detail,
            "enabled": bool(self._config is not None and self._config.telegram.enabled),
            "allowed_chats": len(self._bridge.allowed_chat_ids) if self._bridge else 0,
        }

    def register_health_check(self, registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self._availability == "disabled":
                return HealthStatus.HEALTHY
            if self._availability == "healthy":
                return HealthStatus.HEALTHY
            return HealthStatus.UNHEALTHY

        registry.register("telegram", checker, "telegram bridge (bounded polls)")

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(Event(type=event_type, source="telegram", payload=payload))
        except Exception as exc:  # pragma: no cover
            log.warning("telegram event publish failed: %s", exc, extra={"component": "telegram"})
