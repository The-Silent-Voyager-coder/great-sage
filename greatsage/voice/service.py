"""Voice service facade (Phase 6).

Owns wake-word stub + STT/TTS managers + file persistence behind one
bounded, auditable surface. No new configuration section: the store
lives under `core.data_dir/voice` and output-path scoping reuses
`tools.allowed_roots` / `denied_roots`, so path policy stays in
exactly one place.

Events and logs carry ids/lengths/counts only — never audio bytes,
never transcript text, never secrets.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from greatsage.configuration.model import JarvisConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import (
    SPEECH_TRANSCRIBED,
    VOICE_FAILED,
    VOICE_SPOKEN,
    VOICE_WAKE_DETECTED,
    Event,
)
from greatsage.exceptions import (
    VoiceUnavailableError,
    VoiceValidationError,
)
from greatsage.tools.redaction import redact_secrets
from greatsage.voice.file_repository import FileVoiceRepository
from greatsage.voice.limits import VoiceLimits, default_limits
from greatsage.voice.models import (
    SpeechResult,
    Transcript,
    WakeResult,
    sanitize_audio_path,
)
from greatsage.voice.repository import VoiceRepository
from greatsage.voice.stt import STTManager
from greatsage.voice.tts import TTSManager
from greatsage.voice.wakeword import KeywordWakeDetector, WakeWordDetector

log = logging.getLogger("greatsage.voice.service")


def _scoped_path(
    raw: str | Path, *, allowed_roots: tuple[Path, ...], denied_roots: tuple[Path, ...]
) -> Path:
    """Resolve an explicit audio output path inside the configured roots."""
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise VoiceValidationError(f"audio output path must be absolute, got {raw!r}")
    try:
        canonical = candidate.resolve()
    except OSError as exc:
        raise VoiceValidationError(f"unresolvable audio output path: {exc}") from exc
    for denied in denied_roots:
        try:
            resolved_denied = denied.resolve()
        except OSError:
            continue
        if canonical == resolved_denied or canonical.is_relative_to(resolved_denied):
            raise VoiceValidationError(f"audio output path is under a denied root: {denied}")
    if allowed_roots:
        inside = False
        for root in allowed_roots:
            try:
                resolved_root = root.resolve()
            except OSError:
                continue
            if canonical == resolved_root or canonical.is_relative_to(resolved_root):
                inside = True
                break
        if not inside:
            raise VoiceValidationError("audio output path is outside the allowed roots")
    return canonical


class VoiceService:
    """Facade over wake-word stub + STT/TTS managers + file repository."""

    def __init__(
        self,
        *,
        limits: VoiceLimits | None = None,
        repository: VoiceRepository | None = None,
        stt: STTManager | None = None,
        tts: TTSManager | None = None,
        wake: WakeWordDetector | None = None,
    ) -> None:
        self._limits = limits or default_limits()
        self._repository = repository
        self._stt = stt or STTManager(limits=self._limits)
        self._tts = tts or TTSManager(limits=self._limits)
        self._wake = wake or KeywordWakeDetector(self._limits.wake_keyword, limits=self._limits)
        self._stt_injected = stt is not None
        self._tts_injected = tts is not None
        self._wake_injected = wake is not None
        self.publisher: Any = None  # callable(event) -> None, wired by runtime
        self.availability: str = "unavailable"
        self.detail: str = "voice service not started"
        self._allowed_roots: tuple[Path, ...] = ()
        self._denied_roots: tuple[Path, ...] = ()
        self._session_counts: dict[str, int] = {}

    # --- lifecycle -----------------------------------------------------

    def start(self, config: JarvisConfig | None = None) -> None:
        if config is None:
            self.availability = "unavailable"
            self.detail = "no configuration provided"
            return
        try:
            store_dir = config.core.data_dir / "voice"
            if self._repository is None:
                self._repository = FileVoiceRepository(store_dir)
            self._repository.initialize()
            self._allowed_roots = tuple(config.tools.allowed_roots)
            self._denied_roots = tuple(config.tools.denied_roots)
            if not self._stt_injected:
                from greatsage.voice.backends import build_stt_manager

                self._stt = build_stt_manager(config, self._limits)
            if not self._tts_injected:
                from greatsage.voice.backends import build_tts_manager

                self._tts = build_tts_manager(config, self._limits)
            if not self._wake_injected:
                from greatsage.voice.backends import build_wake_detector

                self._wake = build_wake_detector(config, self._limits)
            self.availability = "healthy"
            self.detail = (
                f"voice ready (stt={self._stt.backend_name}, "
                f"tts={self._tts.backend_name}, wake={self._wake.name})"
            )
            log.info("voice service started", extra={"component": "voice"})
        except Exception as exc:
            self.availability = "unavailable"
            self.detail = f"voice start failed: {exc}"
            log.error(
                "voice service failed to start",
                exc_info=exc,
                extra={"component": "voice"},
            )

    def shutdown(self) -> None:
        if self._repository is not None:
            try:
                self._repository.close()
            except Exception:
                pass
        self.availability = "disabled"
        self.detail = "voice service stopped"
        log.info("voice service stopped", extra={"component": "voice"})

    def _require_available(self) -> VoiceRepository:
        if self.availability != "healthy" or self._repository is None:
            raise VoiceUnavailableError(self.detail or "voice service not available")
        return self._repository

    def _check_budget(self, session_id: str | None) -> None:
        if session_id is None:
            return
        used = self._session_counts.get(session_id, 0)
        if used >= self._limits.max_turns_per_session:
            raise VoiceValidationError(
                f"session voice budget exhausted ({self._limits.max_turns_per_session})"
            )

    def _spend_budget(self, session_id: str | None) -> None:
        if session_id is not None:
            self._session_counts[session_id] = self._session_counts.get(session_id, 0) + 1

    # --- operations ----------------------------------------------------

    def detect_wake(
        self,
        text: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> WakeResult:
        """Check one bounded text window for the wake keyword."""
        self._require_available()
        try:
            result = self._wake.check(text)
        except Exception as exc:
            self._publish(
                VOICE_FAILED,
                {"operation": "wake", "reason": redact_secrets(str(exc)[:200])},
                session_id=session_id,
                task_id=task_id,
            )
            raise
        if result.detected:
            self._publish(
                VOICE_WAKE_DETECTED,
                {"keyword": result.keyword, "confidence": result.confidence},
                session_id=session_id,
                task_id=task_id,
            )
        return result

    def listen(
        self,
        text: str | None = None,
        *,
        audio: bytes | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> Transcript:
        """Transcribe one bounded turn and persist it; returns the transcript."""
        repository = self._require_available()
        if (text is None) == (audio is None):
            raise VoiceValidationError("listen needs exactly one of text or audio")
        self._check_budget(session_id)
        try:
            if audio is not None:
                record = self._stt.listen_audio(audio)
            else:
                assert text is not None
                record = self._stt.listen_text(text)
        except Exception as exc:
            self._publish(
                VOICE_FAILED,
                {"operation": "listen", "reason": redact_secrets(str(exc)[:200])},
                session_id=session_id,
                task_id=task_id,
            )
            raise
        repository.save_transcript(record)
        self._spend_budget(session_id)
        self._publish(
            SPEECH_TRANSCRIBED,
            {
                "utterance_id": record.id,
                "backend": record.backend.value,
                "text_chars": record.text_chars,
            },
            session_id=session_id,
            task_id=task_id,
        )
        log.info(
            "voice turn transcribed",
            extra={"component": "voice", "utterance_id": record.id},
        )
        return record

    def speak(
        self,
        text: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        output_path: str | Path | None = None,
    ) -> SpeechResult:
        """Synthesize one bounded utterance, persist audio + metadata."""
        repository = self._require_available()
        self._check_budget(session_id)
        scoped: Path | None = None
        if output_path is not None:
            candidate = sanitize_audio_path(output_path)
            scoped = _scoped_path(
                candidate,
                allowed_roots=self._allowed_roots,
                denied_roots=self._denied_roots,
            )
            if not scoped.parent.exists():
                raise VoiceValidationError(
                    "audio output directory does not exist; create it first through the file tools"
                )
        try:
            audio, record = self._tts.speak(text)
        except Exception as exc:
            self._publish(
                VOICE_FAILED,
                {"operation": "speak", "reason": redact_secrets(str(exc)[:200])},
                session_id=session_id,
                task_id=task_id,
            )
            raise
        if scoped is not None:
            record = SpeechResult(
                id=record.id,
                backend=record.backend,
                text_chars=record.text_chars,
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                format=record.format,
                duration_ms=record.duration_ms,
                output_path=str(scoped),
                created_at=record.created_at,
                metadata=dict(record.metadata),
            )
            record.validate()
        repository.save_speech(record, audio)
        if scoped is not None:
            scoped.write_bytes(audio)
        self._spend_budget(session_id)
        self._publish(
            VOICE_SPOKEN,
            {
                "utterance_id": record.id,
                "backend": record.backend.value,
                "text_chars": record.text_chars,
                "size_bytes": record.size_bytes,
            },
            session_id=session_id,
            task_id=task_id,
        )
        log.info(
            "voice turn synthesized",
            extra={"component": "voice", "utterance_id": record.id},
        )
        return record

    def get_transcript(self, utterance_id: str) -> Transcript | None:
        return self._require_available().get_transcript(utterance_id)

    def list_transcripts(self, limit: int = 50) -> list[Transcript]:
        return self._require_available().list_transcripts(limit=limit)

    # --- health --------------------------------------------------------

    def health(self) -> dict[str, Any]:
        repo_health: dict[str, object] = {}
        if self._repository is not None:
            try:
                repo_health = self._repository.health().to_dict()
            except Exception as exc:
                repo_health = {"accessible": False, "detail": str(exc)[:200]}
        return {
            "available": self.availability == "healthy",
            "status": self.availability,
            "enabled": True,
            "detail": self.detail,
            "stt_backend": self._stt.backend_name,
            "stt_available": self._stt.backend_available,
            "tts_backend": self._tts.backend_name,
            "tts_available": self._tts.backend_available,
            "wake_backend": self._wake.name,
            "repository": repo_health,
        }

    def register_health_check(self, health_registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self.availability == "disabled":
                return HealthStatus.HEALTHY
            if self.availability == "healthy":
                return HealthStatus.HEALTHY
            return HealthStatus.UNHEALTHY

        health_registry.register("voice", checker, "voice subsystem (bounded)")

    # --- internals -----------------------------------------------------

    def _publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(
                Event(
                    type=event_type,
                    source="voice",
                    session_id=session_id,
                    task_id=task_id,
                    payload=payload,
                )
            )
        except Exception as exc:
            # In CLI context there's no event loop — events are best-effort
            log.debug("voice event publish skipped: %s", exc, extra={"component": "voice"})
