"""Real voice backend tests (Vosk STT, Piper TTS, fuzzy wake-word).

Vosk/Piper tests need the optional packages + local model files
(`C:/GREATSAGE/models/...`); they skip cleanly without them (CI-safe).
The fuzzy matcher is stdlib and always runs.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from greatsage.exceptions import VoiceValidationError
from greatsage.voice.backends import piper_voice_paths, vosk_model_dir
from greatsage.voice.stt import VoskSTTBackend, parse_wav_pcm
from greatsage.voice.tts import PiperTTSBackend
from greatsage.voice.wakeword import FuzzyWakeDetector

MODELS_DIR = Path("C:/GREATSAGE/models")


def _vosk_backend() -> VoskSTTBackend:
    return VoskSTTBackend(vosk_model_dir(MODELS_DIR, "small"))


def _piper_backend() -> PiperTTSBackend:
    onnx, config = piper_voice_paths(MODELS_DIR, "en_GB-alan-medium")
    return PiperTTSBackend(onnx, config)


def _needs_vosk() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not _vosk_backend().is_available(), reason="vosk package/model missing"
    )


def _needs_piper() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not _piper_backend().is_available(), reason="piper package/voice missing"
    )


def _silent_wav_16k_mono(seconds: int = 1) -> bytes:
    frames = 16000 * seconds
    pcm = b"\x00\x00" * frames
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
        16000, 32000, 2, 16, b"data", len(pcm),
    )
    return header + pcm


def test_parse_wav_pcm_ok() -> None:
    rate, channels, bits, pcm = parse_wav_pcm(_silent_wav_16k_mono())
    assert (rate, channels, bits) == (16000, 1, 16)
    assert len(pcm) == 32000


def test_parse_wav_pcm_rejects_junk() -> None:
    with pytest.raises(VoiceValidationError):
        parse_wav_pcm(b"not audio at all" + b"\x00" * 64)


def test_fuzzy_exact_and_alias() -> None:
    detector = FuzzyWakeDetector("jarvis")
    assert detector.check("hey jarvis, lights on").detected is True
    assert detector.check("hey jarvis, lights on").confidence == 1.0
    assert detector.check("hey jervis, lights on").confidence == 1.0  # known alias
    misheard = detector.check("hey jarbis, lights on")
    assert misheard.detected is True
    assert 0.0 < misheard.confidence < 1.0
    assert detector.check("what time is it").detected is False
    with pytest.raises(VoiceValidationError):
        detector.check("   ")


@_needs_vosk()
def test_vosk_silence_yields_no_speech() -> None:
    backend = _vosk_backend()
    with pytest.raises(VoiceValidationError, match="no speech"):
        backend.transcribe_audio(_silent_wav_16k_mono())


@_needs_vosk()
def test_vosk_rejects_wrong_format() -> None:
    backend = _vosk_backend()
    with pytest.raises(VoiceValidationError, match="16 kHz mono"):
        backend.transcribe_audio(_make_8k_wav())
    with pytest.raises(VoiceValidationError, match="not text"):
        backend.transcribe_text("hello")


def _make_8k_wav() -> bytes:
    pcm = b"\x00\x00" * 8000
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
        8000, 16000, 2, 16, b"data", len(pcm),
    )
    return header + pcm


@_needs_piper()
def test_piper_synthesis() -> None:
    backend = _piper_backend()
    audio, meta = backend.synthesize("hello jarvis")
    assert audio[0:4] == b"RIFF" and audio[8:12] == b"WAVE"
    assert len(audio) > 1000
    assert meta["source"] == "piper"
    assert meta["voice"] == "en_GB-alan-medium"


def test_factories_reject_unknown_engines() -> None:
    import tempfile

    from greatsage.configuration.loader import load_config
    from greatsage.voice.backends import build_stt_manager, build_tts_manager, build_wake_detector
    from greatsage.voice.limits import default_limits

    d = tempfile.mkdtemp().replace("\\", "/")
    path = Path(d) / "v.yaml"
    path.write_text(
        f"""
voice:
  wake_word: {{enabled: false, model: "bogus"}}
  stt: {{engine: "bogus", model: "small", language: "en"}}
  tts: {{engine: "bogus", voice: "en_GB-alan-medium"}}
core:
  data_dir: "{d}/data"
""",
        encoding="utf-8",
    )
    config = load_config(path).config
    limits = default_limits()
    with pytest.raises(VoiceValidationError):
        build_stt_manager(config, limits)
    with pytest.raises(VoiceValidationError):
        build_tts_manager(config, limits)
    with pytest.raises(VoiceValidationError):
        build_wake_detector(config, limits)


def test_factories_build_known_engines() -> None:
    import tempfile

    from greatsage.configuration.loader import load_config
    from greatsage.voice.backends import build_stt_manager, build_tts_manager, build_wake_detector
    from greatsage.voice.limits import default_limits

    d = tempfile.mkdtemp().replace("\\", "/")
    path = Path(d) / "v.yaml"
    path.write_text(
        f"""
voice:
  wake_word: {{enabled: false, model: "fuzzy"}}
  stt: {{engine: "vosk", model: "small", language: "en"}}
  tts: {{engine: "piper", voice: "en_GB-alan-medium"}}
core:
  data_dir: "{d}/data"
""",
        encoding="utf-8",
    )
    config = load_config(path).config
    limits = default_limits()
    assert build_stt_manager(config, limits).backend_name == "vosk"
    assert build_tts_manager(config, limits).backend_name == "piper"
    assert build_wake_detector(config, limits).name == "fuzzy"
