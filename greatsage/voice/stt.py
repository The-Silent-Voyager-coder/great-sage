"""Speech-to-text abstraction (Phase 6 + real offline engines).

Backends: `mock` (deterministic echo, always available), `vosk` (real
offline transcription of 16 kHz mono PCM WAV via the Vosk model under
`{models_dir}/vosk/`), `faster-whisper` (accepted name, unavailable on
hosts without the package — explicit, never a silent fallback).
"""

from __future__ import annotations

import hashlib
import logging
import struct
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from greatsage.exceptions import VoiceTimeoutError, VoiceValidationError
from greatsage.voice.limits import VoiceLimits, default_limits
from greatsage.voice.models import Transcript, VoiceBackend, new_utterance_id, transcript_digest

log = logging.getLogger("greatsage.voice.stt")

STT_MOCK_PREFIX = "[mock-stt]"
WAV_MIN_BYTES = 44


def is_wav_bytes(data: bytes) -> bool:
    """Accept RIFF/WAVE headers only; everything else is rejected."""
    return len(data) >= WAV_MIN_BYTES and data[0:4] == b"RIFF" and data[8:12] == b"WAVE"


class STTBackend(ABC):
    """Pluggable transcription source; callers never touch engines directly."""

    name: str = "abstract"
    backend: VoiceBackend = VoiceBackend.MOCK

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def transcribe_text(self, text: str) -> tuple[str, dict[str, Any]]:
        """Transcribe caller-provided text (dictation stub); returns (text, meta)."""
        ...

    @abstractmethod
    def transcribe_audio(self, audio: bytes) -> tuple[str, dict[str, Any]]:
        """Transcribe WAV bytes; returns (text, meta)."""
        ...


class MockSTTBackend(STTBackend):
    """Deterministic stub; always available, zero-cost, honest about it."""

    name = "mock"
    backend = VoiceBackend.MOCK

    def is_available(self) -> bool:
        return True

    def transcribe_text(self, text: str) -> tuple[str, dict[str, Any]]:
        cleaned = " ".join(text.split())
        return (f"{STT_MOCK_PREFIX} {cleaned}", {"source": "text"})

    def transcribe_audio(self, audio: bytes) -> tuple[str, dict[str, Any]]:
        if not is_wav_bytes(audio):
            raise VoiceValidationError("STT audio must be WAV (RIFF/WAVE) bytes")
        digest = hashlib.sha256(audio).hexdigest()[:12]
        seconds = max(1, len(audio) // 32000)
        text = f"{STT_MOCK_PREFIX} audio frame {len(audio)} bytes ~{seconds}s id={digest}"
        return (text, {"source": "wav", "audio_bytes": len(audio)})


class OfflineSTTBackend(MockSTTBackend):
    """Offline-engine slot: same deterministic behavior until a local engine lands."""

    name = "offline"
    backend = VoiceBackend.OFFLINE

    def is_available(self) -> bool:
        return False


class FasterWhisperSTTBackend(MockSTTBackend):
    """Accepted engine name for hosts with faster-whisper installed.

    The package is unavailable on this host (Python 3.13 Windows wheels are
    missing), so this backend reports unavailable with an explicit reason
    instead of silently falling back. Use `vosk` for real transcription.
    """

    name = "faster-whisper"
    backend = VoiceBackend.OFFLINE

    def is_available(self) -> bool:
        # No reliable Windows/py3.13 wheels for faster-whisper; the backend is
        # accepted by config validation but can never report available
        # (docs/DEPENDENCY_POLICY.md). Use `vosk` for real transcription.
        return False


def parse_wav_pcm(audio: bytes) -> tuple[int, int, int, bytes]:
    """Parse WAV bytes; returns (sample_rate, channels, bits, pcm).

    Raises VoiceValidationError for non-WAV input or non-PCM layouts.
    """
    if not is_wav_bytes(audio):
        raise VoiceValidationError("STT audio must be WAV (RIFF/WAVE) bytes")
    if len(audio) < 44:
        raise VoiceValidationError("STT audio is shorter than a WAV header")
    try:
        (fmt_tag, channels, sample_rate, _, _, bits) = struct.unpack("<HHIIHH", audio[20:36])
    except struct.error as exc:
        raise VoiceValidationError(f"STT audio has an unreadable fmt chunk: {exc}") from exc
    if fmt_tag != 1:
        raise VoiceValidationError(f"STT audio must be PCM, got format tag {fmt_tag}")
    marker = audio.find(b"data")
    if marker < 0 or marker + 8 > len(audio):
        raise VoiceValidationError("STT audio has no data chunk")
    return sample_rate, channels, bits, audio[marker + 8 :]


class VoskSTTBackend(STTBackend):
    """Real offline transcription via a local Vosk model directory."""

    name = "vosk"
    backend = VoiceBackend.OFFLINE

    def __init__(self, model_dir: str | Path) -> None:
        self._model_dir = Path(model_dir)
        self._model: Any = None

    def is_available(self) -> bool:
        try:
            import vosk  # noqa: F401
        except ImportError:
            return False
        return self._model_dir.is_dir()

    def _load(self) -> Any:
        if self._model is None:
            try:
                from vosk import Model
            except ImportError as exc:
                raise VoiceValidationError(
                    "vosk engine needs the 'vosk' package (pip install vosk)"
                ) from exc
            if not self._model_dir.is_dir():
                raise VoiceValidationError(
                    f"vosk model not found: {self._model_dir} "
                    "(download the small-en model into {models_dir}/vosk/)"
                )
            try:
                self._model = Model(str(self._model_dir))
            except Exception as exc:
                raise VoiceValidationError(f"vosk model failed to load: {exc}") from exc
        return self._model

    def transcribe_text(self, text: str) -> tuple[str, dict[str, Any]]:
        raise VoiceValidationError("vosk transcribes audio, not text (use --audio-path)")

    def transcribe_audio(self, audio: bytes) -> tuple[str, dict[str, Any]]:
        import json

        sample_rate, channels, bits, pcm = parse_wav_pcm(audio)
        if sample_rate != 16000 or channels != 1 or bits != 16:
            raise VoiceValidationError(
                "vosk needs 16 kHz mono 16-bit PCM WAV "
                f"(got {sample_rate} Hz, {channels} ch, {bits}-bit)"
            )
        try:
            from vosk import KaldiRecognizer
        except ImportError as exc:
            raise VoiceValidationError(
                "vosk engine needs the 'vosk' package (pip install vosk)"
            ) from exc
        model = self._load()
        recognizer = KaldiRecognizer(model, 16000)
        for offset in range(0, len(pcm), 4000):
            recognizer.AcceptWaveform(pcm[offset : offset + 4000])
        try:
            text = " ".join(json.loads(recognizer.FinalResult()).get("text", "").split())
        except (ValueError, AttributeError) as exc:
            raise VoiceValidationError(f"vosk returned an unreadable result: {exc}") from exc
        if not text:
            raise VoiceValidationError("vosk recognized no speech in this audio")
        return (text, {"source": "vosk", "sample_rate": sample_rate, "audio_bytes": len(audio)})


class STTManager:
    """Bounded transcription: text/audio caps plus a wall-clock timeout."""

    def __init__(
        self,
        limits: VoiceLimits | None = None,
        backend: STTBackend | None = None,
    ) -> None:
        self._limits = limits or default_limits()
        self._backend = backend or MockSTTBackend()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def backend_available(self) -> bool:
        try:
            return self._backend.is_available()
        except Exception:
            return False

    def _finish(self, text: str, meta: dict[str, Any], started: float) -> Transcript:
        elapsed = time.perf_counter() - started
        if elapsed > self._limits.listen_timeout_seconds:
            raise VoiceTimeoutError(
                f"listen exceeded {self._limits.listen_timeout_seconds}s (took {elapsed:.2f}s)"
            )
        if len(text) > self._limits.max_text_chars:
            raise VoiceValidationError(
                f"transcript is {len(text)} chars, over the "
                f"{self._limits.max_text_chars}-char limit"
            )
        record = Transcript(
            id=new_utterance_id(),
            backend=self._backend.backend,
            text=text,
            text_chars=len(text),
            sha256=transcript_digest(text),
            duration_ms=round(elapsed * 1000, 3),
            metadata={k: v for k, v in meta.items() if k != "text"},
        )
        record.validate()
        log.debug(
            "transcribed utterance",
            extra={"component": "voice", "utterance_id": record.id},
        )
        return record

    def listen_text(self, text: str) -> Transcript:
        """Transcribe caller-provided text through the bounded mock pipeline."""
        if not isinstance(text, str) or not text.strip():
            raise VoiceValidationError("listen text must be a non-empty string")
        if len(text) > self._limits.max_text_chars:
            raise VoiceValidationError(
                f"listen text is {len(text)} chars, over the "
                f"{self._limits.max_text_chars}-char limit"
            )
        if not self.backend_available:
            raise VoiceValidationError(f"STT backend {self._backend.name!r} is not available")
        started = time.perf_counter()
        out, meta = self._backend.transcribe_text(text.strip())
        return self._finish(out, meta, started)

    def listen_audio(self, audio: bytes) -> Transcript:
        """Transcribe WAV bytes through the bounded mock pipeline."""
        if not isinstance(audio, bytes) or not audio:
            raise VoiceValidationError("listen audio must be non-empty bytes")
        if len(audio) > self._limits.max_audio_bytes:
            raise VoiceValidationError(
                f"listen audio is {len(audio)} bytes, over the "
                f"{self._limits.max_audio_bytes}-byte limit"
            )
        if not self.backend_available:
            raise VoiceValidationError(f"STT backend {self._backend.name!r} is not available")
        started = time.perf_counter()
        out, meta = self._backend.transcribe_audio(audio)
        return self._finish(out, meta, started)
