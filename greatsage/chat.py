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
    "You are Great Sage — the personal AI operating system of your Master. "
    "You are highly intelligent, precise, and efficient. "
    "You speak with quiet confidence and dry wit, not servile politeness. "
    "You refer to the user as 'Master' when it fits naturally, not forced. "
    "You are direct: short answers for simple questions, detailed when complexity demands it. "
    "You do not apologize unnecessarily or use filler like 'I'd be happy to help'. "
    "You state facts, solve problems, and move on. "
    "Your tone is calm, slightly formal, with occasional dry humor — "
    "like a hyper-competent butler who also happens to be the smartest entity in the room. "
    "You can discuss memory, tasks, files, and system capabilities. "
    "If asked who made you, say you were built by your Master's design."
)

# Audio recording parameters (matches Vosk expectations)
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
CHUNK_SECONDS = 0.1
MAX_RECORD_SECONDS = 30
SILENCE_THRESHOLD = 60  # RMS threshold for silence detection (mic is quiet)
SILENCE_TIMEOUT = 2.0  # seconds of silence to stop recording (longer to avoid cutoff)

# Session limits
MAX_HISTORY_TURNS = 20  # keep last N exchanges in context
MAX_TURNS = 100  # hard cap per session

EXIT_COMMANDS = frozenset({"exit", "quit", "goodbye", "bye", "stop", "shut down"})


def _native_rate(device: int | None) -> tuple[int, int]:
    """Return (sample_rate, channels) the device actually supports."""
    import sounddevice as sd

    info = sd.query_devices(device if device is not None else sd.default.device[0])
    return int(info["default_samplerate"]), min(2, int(info["max_input_channels"]))


def _resample_mono(pcm: bytes, src_rate: int, src_ch: int) -> bytes:
    """Convert arbitrary-rate multi-channel PCM to 16kHz mono (linear interp)."""
    import numpy as np

    if not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if src_ch > 1:
        samples = samples.reshape(-1, src_ch).mean(axis=1)
    if src_rate == SAMPLE_RATE:
        return samples.astype(np.int16).tobytes()
    # Linear-interpolation resample
    src_n = len(samples)
    dst_n = int(src_n * SAMPLE_RATE / src_rate)
    if dst_n == 0:
        return b""
    src_idx = np.linspace(0, src_n - 1, dst_n)
    lo = np.floor(src_idx).astype(np.int64)
    hi = np.minimum(lo + 1, src_n - 1)
    frac = (src_idx - lo).astype(np.float32)
    out = samples[lo] * (1 - frac) + samples[hi] * frac
    out = np.clip(out, -32768, 32767)
    return out.astype(np.int16).tobytes()


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
    """Record from microphone until silence. Returns WAV bytes or None if too short.

    Uses a single persistent InputStream at the device's native rate
    (open/close churn returns zeros on some drivers), then resamples
    to 16kHz mono for Vosk.
    """
    import sounddevice as sd

    print("  listening...", end="", flush=True)

    try:
        native_rate, native_ch = _native_rate(device)
    except Exception as exc:
        print(f"\n  (mic error: {exc})")
        return None

    block = max(1, int(native_rate * CHUNK_SECONDS))
    raw_chunks: list[bytes] = []
    silence_start = None
    has_speech = False  # only apply silence timeout after first speech
    started = time.monotonic()

    try:
        with sd.InputStream(
            samplerate=native_rate,
            channels=native_ch,
            dtype="int16",
            device=device,
            blocksize=block,
        ) as stream:
            while True:
                elapsed = time.monotonic() - started
                if elapsed > MAX_RECORD_SECONDS:
                    break

                data, _overflowed = stream.read(block)
                chunk = data.tobytes()
                raw_chunks.append(chunk)

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
    except Exception as exc:
        print(f"\n  (mic error: {exc})")
        return None

    print(" done")

    raw_pcm = b"".join(raw_chunks)
    pcm16 = _resample_mono(raw_pcm, native_rate, native_ch)

    total_seconds = len(pcm16) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    if total_seconds < 0.3:
        print("  (too short, try again)")
        return None

    # Build WAV manually (always 16kHz mono after resample)
    pcm_bytes = bytes(pcm16)
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


def _amplify_wav(wav: bytes) -> bytes:
    """Amplify quiet WAV audio so Vosk can actually understand it.

    Finds the peak sample and scales up to target peak (~8000).
    Clipping is avoided; silence is left alone.
    """
    if len(wav) < 44:
        return wav

    # Find data chunk
    pos = 12
    data_start = None
    data_size = 0
    while pos < len(wav) - 8:
        chunk_id = wav[pos : pos + 4]
        chunk_size = struct.unpack("<I", wav[pos + 4 : pos + 8])[0]
        if chunk_id == b"data":
            data_start = pos + 8
            data_size = chunk_size
            break
        pos += 8 + chunk_size

    if data_start is None or data_size == 0:
        return wav

    # Parse samples
    count = data_size // 2
    samples = list(struct.unpack(f"<{count}h", wav[data_start : data_start + count * 2]))

    peak = max((abs(s) for s in samples), default=0)
    if peak < 100:
        return wav  # too quiet to be speech

    target_peak = 8000
    gain = min(target_peak / peak, 30.0)  # cap gain at 30x

    amplified = struct.pack(
        f"<{count}h",
        *[max(-32768, min(32767, int(s * gain))) for s in samples],
    )

    return wav[:data_start] + amplified


class ChatSession:
    """Manages a conversational chat loop (voice or text mode)."""

    def __init__(
        self,
        config: JarvisConfig,
        intelligence: IntelligenceService,
        *,
        voice: VoiceService | None = None,
        text_mode: bool = False,
        device: int | None = None,
    ) -> None:
        self._config = config
        self._intelligence = intelligence
        self._voice = voice
        self._text_mode = text_mode
        self._device = device
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
        wav_bytes = record_from_mic(device=self._device)
        if wav_bytes is None:
            return True

        # Amplify weak audio before sending to Vosk
        wav_bytes = _amplify_wav(wav_bytes)

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
