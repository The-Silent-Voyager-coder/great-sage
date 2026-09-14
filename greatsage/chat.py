"""Conversational chat loop — voice or text mode.

Voice mode:  record from mic -> transcribe (Vosk) -> generate (LLM) ->
              speak (Piper) -> play audio -> repeat.
Text mode:   type input -> generate (LLM) -> print response -> repeat.

No new config section: reuses voice.* and ai.* from the existing config.
Events carry ids/lengths only, never audio bytes or transcript content.
"""

from __future__ import annotations

import logging
import struct
import time
import uuid

from greatsage.configuration.model import JarvisConfig
from greatsage.exceptions import JarvisError, VoiceValidationError
from greatsage.intelligence.models import AIRequest, Message
from greatsage.intelligence.service import IntelligenceService
from greatsage.voice.service import VoiceService

log = logging.getLogger("greatsage.chat")

SYSTEM_PROMPT = (
    "You are Great Sage, a personal AI assistant. "
    "Keep responses concise and conversational — aim for 1-3 sentences "
    "unless the user asks for detail. "
    "You can help with questions, tasks, memory, and general conversation. "
    "Be direct, accurate, and helpful."
)

# Audio recording parameters (matches Vosk expectations)
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
CHUNK_SECONDS = 0.1
MAX_RECORD_SECONDS = 30
SILENCE_THRESHOLD = 100  # RMS threshold for silence detection
SILENCE_TIMEOUT = 1.5  # seconds of silence to stop recording

# Session limits
MAX_HISTORY_TURNS = 20  # keep last N exchanges in context
MAX_TURNS = 100  # hard cap per session

EXIT_COMMANDS = frozenset({"exit", "quit", "goodbye", "bye", "stop", "shut down"})


def _record_chunk(device: int | None = None) -> bytes:
    """Record one chunk of audio from the microphone. Returns raw PCM bytes."""
    import sounddevice as sd

    frames = int(SAMPLE_RATE * CHUNK_SECONDS)
    kwargs: dict = {"samplerate": SAMPLE_RATE, "channels": CHANNELS, "dtype": "int16"}
    if device is not None:
        kwargs["device"] = device
    audio = sd.rec(frames, **kwargs)
    sd.wait()
    return audio.tobytes()


def _rms(data: bytes) -> float:
    """Compute root-mean-square of 16-bit PCM audio."""
    if len(data) < 2:
        return 0.0
    count = len(data) // 2
    samples = struct.unpack(f"<{count}h", data[: count * 2])
    return (sum(s * s for s in samples) / count) ** 0.5


def record_from_mic(device: int | None = None) -> bytes | None:
    """Record from microphone until silence. Returns WAV bytes or None if too short."""
    print("  listening...", end="", flush=True)
    all_pcm = bytearray()
    silence_start = None
    started = time.monotonic()
    has_speech = False  # only apply silence timeout after first speech

    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed > MAX_RECORD_SECONDS:
                break

            chunk = _record_chunk(device)
            all_pcm.extend(chunk)

            rms = _rms(chunk)
            if rms >= SILENCE_THRESHOLD:
                has_speech = True
                silence_start = None
            elif has_speech:
                if silence_start is None:
                    silence_start = time.monotonic()
                elif time.monotonic() - silence_start > SILENCE_TIMEOUT:
                    break
    except KeyboardInterrupt:
        print("\n  (interrupted)")
        return None

    print(" done")

    total_seconds = len(all_pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    if total_seconds < 0.3:
        print("  (too short, try again)")
        return None

    # Build WAV manually
    pcm_bytes = bytes(all_pcm)
    data_size = len(pcm_bytes)
    file_size = 36 + data_size

    wav = bytearray()
    wav.extend(b"RIFF")
    wav.extend(struct.pack("<I", file_size))
    wav.extend(b"WAVE")
    wav.extend(b"fmt ")
    wav.extend(struct.pack("<I", 16))
    wav.extend(struct.pack("<H", 1))  # PCM format
    wav.extend(struct.pack("<H", CHANNELS))
    wav.extend(struct.pack("<I", SAMPLE_RATE))
    wav.extend(struct.pack("<I", SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH))
    wav.extend(struct.pack("<H", CHANNELS * SAMPLE_WIDTH))
    wav.extend(struct.pack("<H", 16))
    wav.extend(b"data")
    wav.extend(struct.pack("<I", data_size))
    wav.extend(pcm_bytes)

    return bytes(wav)


def play_audio(audio: bytes) -> None:
    """Play WAV bytes through the default output device."""
    import numpy as np
    import sounddevice as sd

    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise VoiceValidationError("invalid WAV data")

    # Find data chunk
    pos = 12
    data = None
    while pos < len(audio) - 8:
        chunk_id = audio[pos : pos + 4]
        chunk_size = struct.unpack("<I", audio[pos + 4 : pos + 8])[0]
        if chunk_id == b"data":
            data = audio[pos + 8 : pos + 8 + chunk_size]
            break
        pos += 8 + chunk_size

    if data is None:
        raise VoiceValidationError("no data chunk in WAV")

    channels = struct.unpack("<H", audio[22:24])[0]
    sample_rate = struct.unpack("<I", audio[24:28])[0]
    bits = struct.unpack("<H", audio[34:36])[0]

    if bits == 16:
        samples = np.frombuffer(data, dtype=np.int16)
    else:
        raise VoiceValidationError(f"unsupported bit depth: {bits}")

    audio_float = samples.astype(np.float32) / 32768.0
    if channels > 1:
        audio_float = audio_float.reshape(-1, channels)

    sd.play(audio_float, samplerate=sample_rate)
    sd.wait()


class ChatSession:
    """Manages a conversational chat loop (voice or text mode)."""

    def __init__(
        self,
        config: JarvisConfig,
        intelligence: IntelligenceService,
        *,
        voice: VoiceService | None = None,
        text_mode: bool = False,
    ) -> None:
        self._config = config
        self._intelligence = intelligence
        self._voice = voice
        self._text_mode = text_mode
        self._history: list[Message] = [Message.system(SYSTEM_PROMPT)]
        self._session_id = f"chat_{uuid.uuid4().hex[:12]}"
        self._turn = 0
        self._chat_dir = config.core.data_dir / "voice" / "chat"
        if not text_mode:
            self._chat_dir.mkdir(parents=True, exist_ok=True)

    def _generate(self, user_text: str) -> str | None:
        """Send user text to the LLM and return the reply, or None on error."""
        self._history.append(Message.user(user_text))
        request = AIRequest(
            request_id=f"chat_{uuid.uuid4().hex[:8]}",
            messages=list(self._history),
            metadata={"source": "chat", "session_id": self._session_id},
        )
        try:
            response = self._intelligence.generate(request)
        except JarvisError as exc:
            print(f"  (generation failed: {exc})")
            self._history.pop()
            return None

        reply = response.content.strip()
        if reply:
            self._history.append(Message.assistant(reply))
        return reply or None

    def _trim_history(self) -> None:
        """Trim conversation history to stay within context budget."""
        # Keep system prompt + last N turns (each turn = user + assistant)
        max_messages = 1 + MAX_HISTORY_TURNS * 2
        if len(self._history) > max_messages:
            system = self._history[0:1]
            recent = self._history[-(max_messages - 1) :]
            self._history = system + recent

    def _voice_turn(self) -> bool:
        """One voice cycle: record -> transcribe -> generate -> speak. Returns False to stop."""
        wav_bytes = record_from_mic()
        if wav_bytes is None:
            return True

        try:
            transcript = self._voice.listen(audio=wav_bytes, session_id=self._session_id)
        except VoiceValidationError as exc:
            print(f"  (transcription failed: {exc})")
            return True

        user_text = transcript.text.strip()
        if not user_text:
            print("  (no speech detected)")
            return True

        print(f"  you: {user_text}")
        return self._process_input(user_text)

    def _text_turn(self) -> bool:
        """One text cycle: read input -> generate -> print. Returns False to stop."""
        try:
            user_text = input("  you: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if not user_text:
            return True

        return self._process_input(user_text)

    def _process_input(self, user_text: str) -> bool:
        """Shared logic: check exit, generate, optionally speak. Returns False to stop."""
        lower = user_text.lower().strip()
        if lower in EXIT_COMMANDS:
            print("  great sage: Goodbye!")
            return False

        reply = self._generate(user_text)
        if reply is None:
            return True

        print(f"  great sage: {reply}")
        self._trim_history()

        # Speak response (voice mode only)
        if not self._text_mode and self._voice is not None:
            audio_file = self._chat_dir / f"turn_{self._turn:04d}.wav"
            try:
                self._voice.speak(
                    reply, session_id=self._session_id, output_path=str(audio_file)
                )
            except VoiceValidationError:
                pass  # text still printed, speech is best-effort
            else:
                try:
                    if audio_file.is_file():
                        play_audio(audio_file.read_bytes())
                except Exception as exc:
                    log.debug("audio playback failed: %s", exc)

        return True

    def run(self) -> None:
        """Run the chat loop until exit."""
        mode = "text" if self._text_mode else "voice"
        print(f"Great Sage Chat ({mode} mode)")
        print(f"  session: {self._session_id}")
        if self._text_mode:
            print("  type to talk, 'exit' to quit, Ctrl+C to interrupt")
        else:
            print("  speak to talk, say 'exit' to quit, Ctrl+C to interrupt")
        print()

        try:
            while self._turn < MAX_TURNS:
                self._turn += 1
                turn_fn = self._text_turn if self._text_mode else self._voice_turn
                if not turn_fn():
                    break
        except KeyboardInterrupt:
            print("\n  (session ended)")

        print(f"\n  session {self._session_id} ended after {self._turn} turns")
