"""Telegram bridge tests (remote chat, offline fakes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from greatsage.telegram.bridge import HELP_TEXT, TelegramBridge
from greatsage.telegram.client import TelegramClient, TelegramError, extract_message
from greatsage.telegram.service import TelegramService


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _ok(result: Any) -> _FakeResponse:
    return _FakeResponse(json.dumps({"ok": True, "result": result}).encode())


def test_bridge_allowlists() -> None:
    bridge = TelegramBridge(
        allowed_chat_ids=frozenset({7}),
        getters={"briefing": lambda: "all good", "status": lambda: "HEALTHY"},
    )
    assert bridge.handle(8, "/briefing") is None  # stranger ignored silently
    assert bridge.handle(7, "/briefing") == "all good"
    assert bridge.handle(7, "/status") == "HEALTHY"
    assert HELP_TEXT in (bridge.handle(7, "/help") or "")
    assert HELP_TEXT in (bridge.handle(7, "/start") or "")
    assert "unknown command" in (bridge.handle(7, "/nuke") or "")
    assert bridge.handle(7, "/briefing@jarvis_bot extra") == "all good"


def test_extract_message() -> None:
    good = {"update_id": 1, "message": {"message_id": 2, "chat": {"id": 7}, "text": "  /status "}}
    assert extract_message(good) == (7, 2, "/status")
    assert extract_message({"update_id": 1}) is None
    assert extract_message({"message": {"chat": {"id": 7}}}) is None
    no_id = {"message": {"chat": {"id": "x"}, "message_id": 1, "text": "hi"}}
    assert extract_message(no_id) is None
    blank = {"message": {"chat": {"id": 7}, "message_id": 1, "text": "   "}}
    assert extract_message(blank) is None


def test_client_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(req: object, timeout: object = None) -> _FakeResponse:
        calls.append(str(req.full_url))  # type: ignore[union-attr]
        if str(req.full_url).endswith("/getUpdates"):  # type: ignore[union-attr]
            update = {"update_id": 9, "message": {"message_id": 1, "chat": {"id": 7}, "text": "hi"}}
            return _ok([update])
        return _ok(True)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    client = TelegramClient("bottoken123")
    updates = client.get_updates()
    assert len(updates) == 1
    assert client.send_message(7, "hello") is True
    assert any(u.endswith("/sendMessage") for u in calls)


def test_client_rejects_empty_token() -> None:
    with pytest.raises(TelegramError):
        TelegramClient("")


def test_client_clips_long_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake(req: object, timeout: object = None) -> _FakeResponse:
        seen["body"] = req.data.decode()  # type: ignore[union-attr]
        return _ok(True)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    TelegramClient("tok").send_message(7, "x" * 5000)
    import urllib.parse

    sent = urllib.parse.parse_qs(seen["body"])["text"][0]
    assert len(sent) <= 4096


def _telegram_config(tmp_path: Path, **overrides: object) -> Path:
    d = str(tmp_path).replace("\\", "/")
    allowed = overrides.get("allowed_chat_ids", [7])
    enabled = overrides.get("enabled", True)
    path = tmp_path / "telegram.yaml"
    path.write_text(
        f"""
core:
  name: "telegram test"
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
memory:
  enabled: false
  database_path: "{d}/data/sage-memory.db"
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
scheduler:
  enabled: false
  max_schedules: 50
  database_path: "{d}/data/sage-scheduler.db"
telegram:
  enabled: {"true" if enabled else "false"}
  token_env: "GREATSAGE_TEST_TELEGRAM_TOKEN"
  allowed_chat_ids: [{", ".join(str(c) for c in allowed)}]
  poll_timeout_seconds: 1
  max_listen_seconds: 60
""",
        encoding="utf-8",
    )
    return path


def test_service_requires_token_and_chats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from greatsage.configuration.loader import load_config

    monkeypatch.delenv("GREATSAGE_TEST_TELEGRAM_TOKEN", raising=False)
    service = TelegramService()
    service.start(load_config(_telegram_config(tmp_path)).config)
    assert service.health()["status"] == "unavailable"
    assert "GREATSAGE_TEST_TELEGRAM_TOKEN" in service.health()["detail"]
    service.shutdown()


def test_listen_once_replies_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from greatsage.configuration.loader import load_config

    monkeypatch.setenv("GREATSAGE_TEST_TELEGRAM_TOKEN", "test-token")
    service = TelegramService()
    service.start(load_config(_telegram_config(tmp_path)).config)
    assert service.health()["available"] is True
    sent: list[tuple[int, str]] = []
    polled = {"n": 0}

    def fake(req: object, timeout: object = None) -> _FakeResponse:
        url = str(req.full_url)  # type: ignore[union-attr]
        if url.endswith("/getUpdates"):
            polled["n"] += 1
            if polled["n"] > 1:
                return _ok([])
            mine = {
                "update_id": 1,
                "message": {"message_id": 1, "chat": {"id": 7}, "text": "/status"},
            }
            stranger = {
                "update_id": 2,
                "message": {"message_id": 2, "chat": {"id": 666}, "text": "/status"},
            }
            return _ok([mine, stranger])
        body = req.data.decode()  # type: ignore[union-attr]
        import urllib.parse

        form = urllib.parse.parse_qs(body)
        sent.append((int(form["chat_id"][0]), form["text"][0]))
        return _ok(True)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    try:
        out = service.listen(
            once=True, for_seconds=30,
            handlers={"status": lambda: "HEALTHY", "briefing": lambda: "brief"},
        )
    finally:
        service.shutdown()
    assert out == {"received": 2, "replied": 1, "ignored": 1, "errors": 0}
    assert sent == [(7, "HEALTHY")]


def test_send_photo_multipart(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, bytes] = {}

    def fake(req: object, timeout: object = None) -> _FakeResponse:
        seen["url"] = str(req.full_url).encode()  # type: ignore[union-attr]
        seen["body"] = bytes(req.data)  # type: ignore[union-attr]
        seen["ctype"] = req.headers["Content-type"].encode()  # type: ignore[union-attr]
        return _ok({"message_id": 3})

    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert TelegramClient("tok").send_photo(7, b"BMPIX", "desk") is True
    assert seen["url"].endswith(b"/sendPhoto")
    assert b"multipart/form-data" in seen["ctype"]
    assert b'name="chat_id"' in seen["body"] and b"BMPIX" in seen["body"]
    with pytest.raises(TelegramError):
        TelegramClient("tok").send_photo(7, b"x" * (10 * 1024 * 1024 + 1))


def test_listen_sends_photo_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from greatsage.configuration.loader import load_config

    monkeypatch.setenv("GREATSAGE_TEST_TELEGRAM_TOKEN", "test-token")
    service = TelegramService()
    service.start(load_config(_telegram_config(tmp_path)).config)
    photos: list[bytes] = []
    polled = {"n": 0}

    def fake(req: object, timeout: object = None) -> _FakeResponse:
        url = str(req.full_url)  # type: ignore[union-attr]
        if url.endswith("/getUpdates"):
            polled["n"] += 1
            if polled["n"] > 1:
                return _ok([])
            shot = {
                "update_id": 5,
                "message": {"message_id": 9, "chat": {"id": 7}, "text": "/screenshot"},
            }
            return _ok([shot])
        photos.append(bytes(req.data))  # type: ignore[union-attr]
        return _ok(True)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    try:
        out = service.listen(
            once=True, for_seconds=30,
            handlers={"screenshot": lambda: {"photo": b"BMPIX", "caption": "desk"}},
        )
    finally:
        service.shutdown()
    assert out == {"received": 1, "replied": 1, "ignored": 0, "errors": 0}
    assert len(photos) == 1 and b"BMPIX" in photos[0]


def test_bridge_screenshot_gate() -> None:
    bridge = TelegramBridge(
        allowed_chat_ids=frozenset({7}),
        getters={"screenshot": lambda: {"photo": b"x", "caption": "c"}},
    )
    reply = bridge.handle(7, "/screenshot")
    assert isinstance(reply, dict) and reply["caption"] == "c"
    assert bridge.handle(8, "/screenshot") is None
