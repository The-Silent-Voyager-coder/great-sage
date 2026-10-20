"""Voice model tests: validation, redaction, path sanitation."""

from __future__ import annotations

import pytest

from greatsage.exceptions import VoiceValidationError
from greatsage.voice.models import (
    SpeechResult,
    Transcript,
    VoiceBackend,
    WakeResult,
    new_utterance_id,
    redact_text,
    sanitize_audio_path,
    transcript_digest,
)


def test_new_utterance_id_format() -> None:
    uid = new_utterance_id()
    assert uid.startswith("vtt_") and len(uid) == 36
    assert uid != new_utterance_id()


def test_wake_result_validation() -> None:
    WakeResult(detected=True, keyword="sage", confidence=1.0).validate()
    with pytest.raises(VoiceValidationError):
        WakeResult(detected=True, keyword="", confidence=1.0).validate()
    with pytest.raises(VoiceValidationError):
        WakeResult(detected=True, keyword="sage", confidence=2.0).validate()


def test_transcript_round_trip() -> None:
    text = "hello sage"
    record = Transcript(
        id=new_utterance_id(),
        backend=VoiceBackend.MOCK,
        text=text,
        text_chars=len(text),
        sha256=transcript_digest(text),
    )
    record.validate()
    data = record.to_dict()
    assert "text" not in data  # privacy: text excluded by default
    assert record.to_dict(include_text=True)["text"] == text


def test_transcript_rejects_bad_id() -> None:
    with pytest.raises(VoiceValidationError):
        Transcript(
            id="bad",
            backend=VoiceBackend.MOCK,
            text="hi",
            text_chars=2,
            sha256="0" * 64,
        ).validate()


def test_speech_result_validation() -> None:
    SpeechResult(
        id=new_utterance_id(),
        backend=VoiceBackend.MOCK,
        text_chars=5,
        sha256="a" * 64,
        size_bytes=100,
    ).validate()
    with pytest.raises(VoiceValidationError):
        SpeechResult(
            id=new_utterance_id(),
            backend=VoiceBackend.MOCK,
            text_chars=5,
            sha256="a" * 64,
            size_bytes=100,
            format="mp3",
        ).validate()


def test_redact_text_masks_secrets() -> None:
    assert "***" in redact_text("api_key: hunter2-secret")
    assert redact_text("hello sage") == "hello sage"


def test_sanitize_audio_path() -> None:
    assert sanitize_audio_path("out.wav").suffix == ".wav"
    with pytest.raises(VoiceValidationError):
        sanitize_audio_path("out.mp3")
