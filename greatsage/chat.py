"""Conversational voice chat loop.

Record from microphone -> transcribe (Vosk) -> generate (local LLM) ->
speak (Piper) -> play audio -> repeat.

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
    "You are speaking to your operator through voice. "
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
SILENCE_THRESHOLD = 500  # RMS threshold for silence detection
SILENCE_TIMEOUT = 1.5  # seconds of silence to stop recording


def _record_chunk() -> bytes:
    """Record one chunk of audio from the microphone. Returns raw PCM bytes."""
    import sounddevice as sd

    duration = CHUNK_SECONDS
    frames = int(SAMPLE_RATE * duration)
    audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=CHANNELS,
                   dtype="int16")
    sd.wait()
    return audio.tobytes()


def _rms(data: bytes) -> float:
    """Compute root-mean-square of 16-bit PCM audio."""
    if len(data) < 2:
        return 0.0
    count = len(data) // 2
    samples = struct.unpack(f"<{count}h", data[:count * 2])
    return (sum(s * s for s in samples) / count) ** 0.5


def record_from_mic() -> bytes | None:
    """Record from microphone until silence. Returns WAV bytes or None if too short."""

    print("  listening...", end="", flush=True)
    all_pcm = bytearray()
    silence_start = None
    started = time.monotonic()

    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed > MAX_RECORD_SECONDS:
                break

            chunk = _record_chunk()
            all_pcm.extend(chunk)

            rms = _rms(chunk)
            if rms < SILENCE_THRESHOLD:
                if silence_start is None:
                    silence_start = time.monotonic()
                elif time.monotonic() - silence_start > SILENCE_TIMEOUT:
                    break
            else:
                silence_start = None
    except KeyboardInterrupt:
        print("\n  (interrupted)")
        return None

    print(" done")

    total_seconds = len(all_pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    if total_seconds < 0.3:
        print("  (too short, try again)")
        return None

    # Build WAV manually for compatibility
    pcm_bytes = bytes(all_pcm)
    wav_buf = bytearray()
    data_size = len(pcm_bytes)
    file_size = 36 + data_size

    # RIFF header
    wav_buf.extend(b"RIFF")
    wav_buf.extend(struct.pack("<I", file_size))
    wav_buf.extend(b"WAVE")

    # fmt chunk
    wav_buf.extend(b"fmt ")
    wav_buf.extend(struct.pack("<I", 16))  # chunk size
    wav_buf.extend(struct.pack("<H", 1))  # PCM format
    wav_buf.extend(struct.pack("<H", CHANNELS))
    wav_buf.extend(struct.pack("<I", SAMPLE_RATE))
    wav_buf.extend(struct.pack("<I", SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH))  # byte rate
    wav_buf.extend(struct.pack("<H", CHANNELS * SAMPLE_WIDTH))  # block align
    wav_buf.extend(struct.pack("<H", 16))  # bits per sample

    # data chunk
    wav_buf.extend(b"data")
    wav_buf.extend(struct.pack("<I", data_size))
    wav_buf.extend(pcm_bytes)

    return bytes(wav_buf)


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
        chunk_id = audio[pos:pos + 4]
        chunk_size = struct.unpack("<I", audio[pos + 4:pos + 8])[0]
        if chunk_id == b"data":
            data = audio[pos + 8:pos + 8 + chunk_size]
            break
        pos += 8 + chunk_size

    if data is None:
        raise VoiceValidationError("no data chunk in WAV")

    # Parse fmt info from header
    channels = struct.unpack("<H", audio[22:24])[0]
    sample_rate = struct.unpack("<I", audio[24:28])[0]
    bits = struct.unpack("<H", audio[34:36])[0]

    # Convert to numpy array
    if bits == 16:
        samples = np.frombuffer(data, dtype=np.int16)
    else:
        raise VoiceValidationError(f"unsupported bit depth: {bits}")

    # Normalize to float32 for playback
    audio_float = samples.astype(np.float32) / 32768.0

    # Reshape for channels
    if channels > 1:
        audio_float = audio_float.reshape(-1, channels)

    sd.play(audio_float, samplerate=sample_rate)
    sd.wait()


class ChatSession:
    """Manages a conversational voice chat loop."""

    def __init__(
        self,
        config: JarvisConfig,
        voice: VoiceService,
        intelligence: IntelligenceService,
    ) -> None:
        self._config = config
        self._voice = voice
        self._intelligence = intelligence
        self._history: list[Message] = [
            Message.system(SYSTEM_PROMPT),
        ]
        self._session_id = f"chat_{uuid.uuid4().hex[:12]}"
        self._turn = 0
        # Temp dir for speech audio files
        self._chat_dir = config.core.data_dir / "voice" / "chat"
        self._chat_dir.mkdir(parents=True, exist_ok=True)

    def chat_turn(self) -> bool:
        """One listen-think-speak cycle. Returns False to stop."""
        self._turn += 1

        # 1. Record from mic
        wav_bytes = record_from_mic()
        if wav_bytes is None:
            return True  # skip short recordings

        # 2. Transcribe
        try:
            transcript = self._voice.listen(
                audio=wav_bytes, session_id=self._session_id,
            )
        except VoiceValidationError as exc:
            print(f"  (transcription failed: {exc})")
            return True

        user_text = transcript.text.strip()
        if not user_text:
            print("  (no speech detected)")
            return True

        print(f"  you: {user_text}")

        # 3. Check for exit commands
        lower = user_text.lower().strip()
        if lower in ("exit", "quit", "goodbye", "bye", "stop", "shut down"):
            print("  great sage: Goodbye!")
            return False

        # 4. Generate response
        self._history.append(Message.user(user_text))
        request = AIRequest(
            request_id=f"chat_{uuid.uuid4().hex[:8]}",
            messages=list(self._history),
            metadata={"source": "voice_chat", "session_id": self._session_id},
        )
        try:
            response = self._intelligence.generate(request)
        except JarvisError as exc:
            print(f"  (generation failed: {exc})")
            self._history.pop()  # remove failed user message
            return True

        reply = response.content.strip()
        if not reply:
            print("  great sage: (no response)")
            return True

        print(f"  great sage: {reply}")
        self._history.append(Message.assistant(reply))

        # 5. Speak response and play audio
        audio_file = self._chat_dir / f"turn_{self._turn:04d}.wav"
        try:
            self._voice.speak(
                reply, session_id=self._session_id, output_path=str(audio_file),
            )
        except VoiceValidationError as exc:
            print(f"  (speech synthesis failed: {exc})")
            return True

        # 6. Play the audio
        try:
            if audio_file.is_file():
                play_audio(audio_file.read_bytes())
        except Exception as exc:
            log.debug("audio playback failed: %s", exc)

        return True

    def run(self) -> None:
        """Run the chat loop until exit."""
        print("Great Sage Voice Chat")
        print(f"  session: {self._session_id}")
        print("  speak to talk, say 'exit' to quit, Ctrl+C to interrupt")
        print()

        try:
            while self.chat_turn():
                pass
        except KeyboardInterrupt:
            print("\n  (session ended)")

        print(f"\n  session {self._session_id} ended after {self._turn} turns")
