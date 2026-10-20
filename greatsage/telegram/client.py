"""Minimal Telegram Bot API client (stdlib urllib only).

Covers exactly what the bridge needs: getUpdates (long-poll) + sendMessage.
The token travels in the URL path per Bot API convention and is never
logged, persisted, or included in errors returned to callers.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("greatsage.telegram.client")


class TelegramError(Exception):
    """Bot API call failed (network or Telegram-side error)."""


def _api(token: str, method: str, params: dict[str, Any], timeout: float) -> Any:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        data = urllib.parse.urlencode(params).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TelegramError(f"bad {method} params: {exc}") from exc
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": "GreatSage-local/1.0"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1048577)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 404):
            raise TelegramError("bot token rejected (401/404); check TELEGRAM_BOT_TOKEN") from exc
        raise TelegramError(f"telegram http error {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise TelegramError(f"telegram unreachable: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise TelegramError(f"telegram request failed: {exc}") from exc
    try:
        body = json.loads(raw[:1048576].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramError("telegram returned non-JSON content") from exc
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise TelegramError("telegram error response")
    return body.get("result")


class TelegramClient:
    """Thin wrapper holding the token in memory only."""

    def __init__(self, token: str, *, timeout_seconds: float = 30.0) -> None:
        if not token:
            raise TelegramError("telegram bot token is empty")
        self._token = token
        self._timeout = max(1.0, min(timeout_seconds, 60.0))

    def get_updates(
        self, *, offset: int | None = None, long_poll_seconds: int = 20
    ) -> list[dict[str, Any]]:
        """Long-poll for updates; returns the raw update list (may be empty)."""
        params: dict[str, Any] = {
            "timeout": max(0, min(long_poll_seconds, 50)),
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            result = _api(self._token, "getUpdates", params, self._timeout + long_poll_seconds)
        except TelegramError:
            raise
        return list(result) if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str) -> bool:
        """Send a text reply (capped at Telegram's 4096 chars)."""
        clipped = text if len(text) <= 4000 else text[:4000] + "… (truncated)"
        try:
            _api(self._token, "sendMessage", {"chat_id": chat_id, "text": clipped}, self._timeout)
        except TelegramError:
            raise
        return True

    def send_photo(self, chat_id: int, photo: bytes, caption: str = "") -> bool:
        """Send a BMP/PNG photo reply (multipart, capped at 10 MiB)."""
        if len(photo) > 10 * 1024 * 1024:
            raise TelegramError("photo too large for telegram send")
        import uuid as _uuid

        boundary = f"----sage{_uuid.uuid4().hex}".encode("ascii")
        clipped = caption if len(caption) <= 1000 else caption[:1000] + "…"
        body = bytearray()
        body.extend(
            b"--" + boundary + b'\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n'
            + str(chat_id).encode("ascii") + b"\r\n"
        )
        body.extend(
            b"--" + boundary + b'\r\nContent-Disposition: form-data; name="caption"\r\n\r\n'
            + clipped.encode("utf-8") + b"\r\n"
        )
        body.extend(
            b"--" + boundary + b'; name="photo"; filename="screenshot.bmp"\r\n'
            b"Content-Type: image/bmp\r\n\r\n" + photo + b"\r\n--" + boundary + b"--\r\n"
        )
        url = f"https://api.telegram.org/bot{self._token}/sendPhoto"
        request = urllib.request.Request(
            url,
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary.decode('ascii')}",
                "User-Agent": "GreatSage-local/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout + 30) as response:
                payload = json.loads(response.read(65537).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"telegram photo http error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TelegramError(f"telegram unreachable: {exc.reason}") from exc
        except (TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramError(f"telegram photo send failed: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramError("telegram photo error response")
        return True


def extract_message(update: dict[str, Any]) -> tuple[int, int, str] | None:
    """Pull (chat_id, message_id, text) from an update; None when unusable."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str) or not text.strip():
        return None
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None
    return chat_id, message_id, text.strip()[:2000]
