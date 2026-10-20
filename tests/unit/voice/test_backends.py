"""Wake-word + STT/TTS backend tests: bounds, determinism, honesty."""

from __future__ import annotations

import pytest

from greatsage.exceptions import VoiceValidationError
from greatsage.voice.limits import VoiceLimits
from greatsage.voice.stt import MockSTTBackend, OfflineSTTBackend, STTManager, is_wav_bytes
from greatsage.voice.tts import MockTTSBackend, OfflineTTSBackend, TTSManager, encode_wav
from greatsage.voice.wakeword import KeywordWakeDetector


def test_wake_detects_keyword() -> None:
    detector = KeywordWakeDetector("sage")
    assert detector.check("hey Sage, help me").detected
    assert not detector.check("hello there").detected


def test_wake_rejects_empty_and_oversize() -> None:
    detector = KeywordWakeDetector("sage")
    with pytest.raises(VoiceValidationError):
        detector.check("   ")
    tight = KeywordWakeDetector("sage", limits=VoiceLimits(max_wake_text_chars=5))
    with pytest.raises(VoiceValidationError):
        tight.check("hey sage hello world")


def test_wake_rejects_empty_keyword() -> None:
    with pytest.raises(VoiceValidationError):
        KeywordWakeDetector("  ")


def test_stt_text_prefix_and_bounds() -> None:
    manager = STTManager()
    record = manager.listen_text("hello world")
    assert record.text.startswith("[mock-stt]")
    assert "hello world" in record.text
    tight = STTManager(limits=VoiceLimits(max_text_chars=4))
    with pytest.raises(VoiceValidationError):
        tight.listen_text("way too long")


def test_stt_audio_requires_wav() -> None:
    assert not is_wav_bytes(b"not audio")
    with pytest.raises(VoiceValidationError):
        MockSTTBackend().transcribe_audio(b"not audio")


def test_stt_audio_round_trip() -> None:
    audio, _ = encode_wav("hi")
    record = STTManager().listen_audio(audio)
    assert record.text.startswith("[mock-stt]")


def test_offline_backends_unavailable() -> None:
    assert not OfflineSTTBackend().is_available()
    assert not OfflineTTSBackend().is_available()
    with pytest.raises(VoiceValidationError):
        STTManager(backend=OfflineSTTBackend()).listen_text("hi")
    with pytest.raises(VoiceValidationError):
        TTSManager(backend=OfflineTTSBackend()).speak("hi")


def test_tts_produces_wav_and_bounds() -> None:
    audio, record = TTSManager().speak("hello sage")
    assert is_wav_bytes(audio)
    assert record.format == "wav"
    assert record.size_bytes == len(audio)
    tight = TTSManager(limits=VoiceLimits(max_synth_chars=2))
    with pytest.raises(VoiceValidationError):
        tight.speak("way too long")


def test_tts_deterministic() -> None:
    first, _ = MockTTSBackend().synthesize("same text")
    second, _ = MockTTSBackend().synthesize("same text")
    assert first == second
