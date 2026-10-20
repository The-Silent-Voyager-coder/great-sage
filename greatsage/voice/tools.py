"""Permissioned voice tools (Phase 6).

`voice.listen` / `voice.speak` / `voice.wake` run through the standard
ToolService security pipeline like every other tool: registry → policy
→ approval → execution → audit events. Risk is SAFE (READ-class for
listen/wake, LOW_WRITE-class output for speak capped at SAFE since
audio writes stay inside allowed roots); path arguments are declared
in PATH_ARGUMENTS so allowed/denied roots are enforced by policy
before execution. Outputs are metadata only — audio bytes and full
transcript text never flow through tool results, events, or logs.

Wire-up (for integration-qa): `register_voice_tools(registry)`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from greatsage.exceptions import VoiceError
from greatsage.tools.models import BaseTool, ToolCategory, ToolContext, ToolResult, ToolRisk
from greatsage.tools.registry import ToolRegistry
from greatsage.voice.limits import VoiceLimits, default_limits
from greatsage.voice.models import sanitize_audio_path
from greatsage.voice.stt import STTManager
from greatsage.voice.tts import TTSManager
from greatsage.voice.wakeword import KeywordWakeDetector, WakeWordDetector


def _failure(tool_id: str, error: str) -> ToolResult:
    return ToolResult(request_id="", tool_id=tool_id, success=False, error=error[:500])


class VoiceListenTool(BaseTool):
    """Transcribe one bounded turn; returns metadata, never audio."""

    id = "voice.listen"
    name = "Voice listen"
    description = "Transcribe one bounded voice turn (mock STT, text or WAV)."
    version = "1.0.0"
    risk_level = ToolRisk.SAFE
    category = ToolCategory.SYSTEM
    capabilities: tuple[str, ...] = ("observe",)
    PATH_ARGUMENTS: tuple[str, ...] = ("audio_path",)
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "audio_path": {"type": "string"},
        },
        "required": [],
    }
    output_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "utterance_id": {"type": "string"},
            "backend": {"type": "string"},
            "text_chars": {"type": "integer"},
            "sha256": {"type": "string"},
        },
        "required": ["utterance_id", "backend", "text_chars", "sha256"],
    }

    def __init__(self, limits: VoiceLimits | None = None, stt: STTManager | None = None) -> None:
        self._limits = limits or default_limits()
        self._stt = stt

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        _ = context
        manager = self._stt or STTManager(limits=self._limits)
        text = arguments.get("text")
        raw_path = arguments.get("audio_path")
        if (text is None) == (raw_path is None):
            return _failure(self.id, "provide exactly one of text or audio_path")
        try:
            if raw_path is not None:
                if not isinstance(raw_path, str) or not raw_path:
                    return _failure(self.id, "audio_path must be a non-empty string")
                path = sanitize_audio_path(raw_path)
                try:
                    audio = Path(path).read_bytes()
                except OSError as exc:
                    return _failure(self.id, f"cannot read audio_path: {exc}")
                record = manager.listen_audio(audio)
            else:
                if not isinstance(text, str) or not text.strip():
                    return _failure(self.id, "text must be a non-empty string")
                record = manager.listen_text(text)
        except VoiceError as exc:
            return _failure(self.id, str(exc))
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={
                "utterance_id": record.id,
                "backend": record.backend.value,
                "text_chars": record.text_chars,
                "sha256": record.sha256,
            },
        )


class VoiceSpeakTool(BaseTool):
    """Synthesize one bounded utterance; returns metadata, never audio."""

    id = "voice.speak"
    name = "Voice speak"
    description = "Synthesize one bounded utterance (mock TTS, WAV)."
    version = "1.0.0"
    risk_level = ToolRisk.SAFE
    category = ToolCategory.SYSTEM
    capabilities: tuple[str, ...] = ("speak",)
    PATH_ARGUMENTS: tuple[str, ...] = ("output_path",)
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "output_path": {"type": "string"},
        },
        "required": ["text"],
    }
    output_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "utterance_id": {"type": "string"},
            "backend": {"type": "string"},
            "text_chars": {"type": "integer"},
            "size_bytes": {"type": "integer"},
            "output_path": {"type": "string"},
        },
        "required": ["utterance_id", "backend", "text_chars", "size_bytes"],
    }

    def __init__(self, limits: VoiceLimits | None = None, tts: TTSManager | None = None) -> None:
        self._limits = limits or default_limits()
        self._tts = tts

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        _ = context
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return _failure(self.id, "text must be a non-empty string")
        manager = self._tts or TTSManager(limits=self._limits)
        try:
            audio, record = manager.speak(text)
        except VoiceError as exc:
            return _failure(self.id, str(exc))
        output: dict[str, Any] = {
            "utterance_id": record.id,
            "backend": record.backend.value,
            "text_chars": record.text_chars,
            "size_bytes": record.size_bytes,
        }
        raw_path = arguments.get("output_path")
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path:
                return _failure(self.id, "output_path must be a non-empty string")
            try:
                target = sanitize_audio_path(str(raw_path))
            except VoiceError as exc:
                return _failure(self.id, str(exc))
            if not target.is_absolute():
                return _failure(self.id, "output_path must be absolute")
            if not target.parent.exists():
                return _failure(
                    self.id,
                    "output directory does not exist; create it first through the file tools",
                )
            try:
                target.write_bytes(audio)
            except OSError as exc:
                return _failure(self.id, f"cannot write output_path: {exc}")
            output["output_path"] = str(target)
        return ToolResult(request_id="", tool_id=self.id, success=True, output=output)


class VoiceWakeTool(BaseTool):
    """Check one bounded text window for the wake keyword."""

    id = "voice.wake"
    name = "Voice wake-word check"
    description = "Check text for the wake keyword (stub matcher, no audio)."
    version = "1.0.0"
    risk_level = ToolRisk.SAFE
    category = ToolCategory.SYSTEM
    capabilities: tuple[str, ...] = ("observe",)
    PATH_ARGUMENTS: tuple[str, ...] = ()
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    }
    output_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "detected": {"type": "boolean"},
            "keyword": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["detected", "keyword", "confidence"],
    }

    def __init__(
        self,
        limits: VoiceLimits | None = None,
        wake: WakeWordDetector | None = None,
    ) -> None:
        self._limits = limits or default_limits()
        self._wake = wake

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        _ = context
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return _failure(self.id, "text must be a non-empty string")
        try:
            detector = self._wake or KeywordWakeDetector(
                self._limits.wake_keyword, limits=self._limits
            )
            result = detector.check(text)
        except VoiceError as exc:
            return _failure(self.id, str(exc))
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={
                "detected": result.detected,
                "keyword": result.keyword,
                "confidence": result.confidence,
            },
        )


def register_voice_tools(
    registry: ToolRegistry,
    *,
    limits: VoiceLimits | None = None,
    stt: STTManager | None = None,
    tts: TTSManager | None = None,
    wake: WakeWordDetector | None = None,
) -> None:
    """Register Phase 6 voice tools into `registry` (duplicates raise).

    Injected managers/backends are used as-is (the runtime passes the
    configured engines); omitted pieces fall back to mock defaults so a bare
    `register_voice_tools(registry)` stays honest (`backend: mock`).
    """
    registry.register(VoiceListenTool(limits=limits, stt=stt))
    registry.register(VoiceSpeakTool(limits=limits, tts=tts))
    registry.register(VoiceWakeTool(limits=limits, wake=wake))
