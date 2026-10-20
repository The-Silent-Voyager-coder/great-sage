"""Backend factories: config engine names -> managers (real or mock).

Real engines: `vosk` (STT), `piper` (TTS), `fuzzy`/`keyword` wake matchers.
Placeholder engines: `mock` (deterministic), and the legacy `offline`
names — for STT it maps to the unavailable faster-whisper placeholder, for
TTS to the unavailable OfflineTTSBackend placeholder; both report
unavailable with an explicit reason (never a silent mock substitute).

Unknown engine names raise VoiceValidationError (explicit, never a silent
fallback). Missing packages or model files do NOT raise here — the backend
reports unavailable and the health detail names exactly what is missing.
"""

from __future__ import annotations

from pathlib import Path

from greatsage.configuration.model import SageConfig
from greatsage.exceptions import VoiceValidationError
from greatsage.voice.limits import VoiceLimits
from greatsage.voice.stt import (
    FasterWhisperSTTBackend,
    MockSTTBackend,
    STTManager,
    VoskSTTBackend,
)
from greatsage.voice.tts import MockTTSBackend, PiperTTSBackend, TTSManager
from greatsage.voice.wakeword import FuzzyWakeDetector, KeywordWakeDetector, WakeWordDetector

_VOSK_MODELS = {
    "small": "vosk-model-small-en-us-0.15",
    "small-en-us": "vosk-model-small-en-us-0.15",
}


def vosk_model_dir(models_dir: Path, model: str) -> Path:
    """Resolve an STT model name to its directory under {models_dir}/vosk/."""
    name = _VOSK_MODELS.get(model.strip().lower(), model.strip())
    return models_dir / "vosk" / name


def piper_voice_paths(models_dir: Path, voice: str) -> tuple[Path, Path | None]:
    """Resolve a TTS voice name to (.onnx, .onnx.json|None) under {models_dir}/piper/."""
    stem = voice.strip()
    onnx = models_dir / "piper" / f"{stem}.onnx"
    config = models_dir / "piper" / f"{stem}.onnx.json"
    return onnx, config if config.is_file() else None


def build_stt_manager(config: SageConfig, limits: VoiceLimits) -> STTManager:
    engine = config.voice.stt.engine.strip().lower()
    if engine == "mock":
        return STTManager(limits, MockSTTBackend())
    if engine == "vosk":
        directory = vosk_model_dir(config.core.models_dir, config.voice.stt.model)
        return STTManager(limits, VoskSTTBackend(directory))
    if engine in ("faster-whisper", "offline"):
        return STTManager(limits, FasterWhisperSTTBackend())
    raise VoiceValidationError(f"unknown STT engine: {config.voice.stt.engine!r}")


def build_tts_manager(config: SageConfig, limits: VoiceLimits) -> TTSManager:
    engine = config.voice.tts.engine.strip().lower()
    if engine == "mock":
        return TTSManager(limits, MockTTSBackend())
    if engine == "piper":
        onnx, voice_config = piper_voice_paths(config.core.models_dir, config.voice.tts.voice)
        return TTSManager(limits, PiperTTSBackend(onnx, voice_config))
    if engine == "offline":
        from greatsage.voice.tts import OfflineTTSBackend

        return TTSManager(limits, OfflineTTSBackend())
    raise VoiceValidationError(f"unknown TTS engine: {config.voice.tts.engine!r}")


def build_wake_detector(config: SageConfig, limits: VoiceLimits) -> WakeWordDetector:
    model = config.voice.wake_word.model.strip().lower()
    keyword = limits.wake_keyword
    if model in ("keyword", "mock"):
        return KeywordWakeDetector(keyword, limits=limits)
    if model in ("fuzzy", "openwakeword"):
        return FuzzyWakeDetector(keyword, limits=limits)
    raise VoiceValidationError(f"unknown wake-word model: {config.voice.wake_word.model!r}")
