"""Voice service tests: lifecycle, budgets, scoping, events, health."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from greatsage.configuration.loader import load_config
from greatsage.events.models import SPEECH_TRANSCRIBED, VOICE_SPOKEN, VOICE_WAKE_DETECTED
from greatsage.exceptions import VoiceUnavailableError, VoiceValidationError
from greatsage.voice.limits import VoiceLimits
from greatsage.voice.models import new_utterance_id
from greatsage.voice.service import VoiceService
from greatsage.voice.tts import encode_wav


def write_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    path = tmp_path / "voice.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Voice Test"
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
  allow_auto_approve_read: true
tools:
  working_directory: "{d}/workspace"
  execution_timeout_seconds: 10.0
  max_output_bytes: 65536
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


def make_service(tmp_path: Path, **kwargs: Any) -> VoiceService:
    config = load_config(str(write_config(tmp_path))).config
    service = VoiceService(**kwargs)
    service.start(config)
    return service


class EventSink:
    def __init__(self) -> None:
        self.types: list[str] = []
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, event: Any) -> None:
        self.types.append(event.type)
        self.payloads.append(dict(event.payload))


def test_start_without_config_unavailable() -> None:
    service = VoiceService()
    service.start(None)
    assert service.availability == "unavailable"
    with pytest.raises(VoiceUnavailableError):
        service.listen("hi")


def test_listen_text_and_get(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    record = service.listen("hello sage", session_id="s1")
    assert record.text_chars == len(record.text)
    assert service.get_transcript(record.id) is not None
    assert len(service.list_transcripts()) == 1


def test_listen_events_carry_no_text_or_audio(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    sink = EventSink()
    service.publisher = sink
    record = service.listen("hello", session_id="s1", task_id="t1")
    assert SPEECH_TRANSCRIBED in sink.types
    payload = sink.payloads[sink.types.index(SPEECH_TRANSCRIBED)]
    assert payload["utterance_id"] == record.id
    assert "text" not in payload and "audio" not in payload


def test_listen_audio(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    audio, _ = encode_wav("hi")
    record = service.listen(audio=audio)
    assert record.text.startswith("[mock-stt]")


def test_listen_requires_exactly_one_input(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    audio, _ = encode_wav("hi")
    with pytest.raises(VoiceValidationError):
        service.listen("hi", audio=audio)
    with pytest.raises(VoiceValidationError):
        service.listen()


def test_wake_detection_event(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    sink = EventSink()
    service.publisher = sink
    assert service.detect_wake("hey great sage, lights on").detected
    assert VOICE_WAKE_DETECTED in sink.types
    assert not service.detect_wake("good morning").detected


def test_speak_and_output_path(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    sink = EventSink()
    service.publisher = sink
    target = tmp_path / "workspace" / "speech.wav"
    record = service.speak("hello there", output_path=str(target))
    assert record.output_path is not None
    assert target.exists()
    assert VOICE_SPOKEN in sink.types
    payload = sink.payloads[sink.types.index(VOICE_SPOKEN)]
    assert payload["utterance_id"] == record.id
    assert "text" not in payload


def test_output_path_outside_roots_rejected(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(VoiceValidationError):
        service.speak("hi", output_path="C:/Windows/Temp/speech.wav")


def test_output_path_missing_parent_rejected(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(VoiceValidationError):
        service.speak("hi", output_path=str(tmp_path / "nope" / "s.wav"))


def test_session_budget_enforced(tmp_path: Path) -> None:
    service = make_service(tmp_path, limits=VoiceLimits(max_turns_per_session=1))
    service.listen("one", session_id="budget")
    with pytest.raises(VoiceValidationError):
        service.listen("two", session_id="budget")
    with pytest.raises(VoiceValidationError):
        service.speak("three", session_id="budget")


def test_get_unknown_id_returns_none(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assert service.get_transcript(new_utterance_id()) is None


def test_health(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    data = service.health()
    assert data["available"]
    assert data["stt_backend"] == "mock"
    assert data["tts_backend"] == "mock"
    assert data["repository"]["utterance_count"] == 0
