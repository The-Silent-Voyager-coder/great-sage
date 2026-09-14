"""Chat module tests: text mode, voice mode, exit, limits, error handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from greatsage.chat import (
    EXIT_COMMANDS,
    MAX_HISTORY_TURNS,
    SAMPLE_RATE,
    SYSTEM_PROMPT,
    ChatSession,
    _resample_mono,
    _rms,
)
from greatsage.configuration.loader import load_config
from greatsage.intelligence.models import AIResponse, FinishReason, Message
from greatsage.intelligence.service import IntelligenceService


def write_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    path = tmp_path / "chat.yaml"
    path.write_text(
        f"""
core:
  name: "Chat Test"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "DEBUG"
  retention_days: 7
memory:
  enabled: false
  database_path: "{d}/data/memory.db"
  auto_save_conversations: false
  default_confidence: 0.8
  retention_days: 365
security:
  mode: "normal"
  allow_auto_approve_read: true
tools:
  working_directory: "{d}/workspace"
  execution_timeout_seconds: 10.0
  max_output_bytes: 65536
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "SYSTEM"
  browser:
    default_risk: "FORBIDDEN"
""",
        encoding="utf-8",
    )
    return path


def make_mock_intelligence(reply: str = "Hello!") -> MagicMock:
    """Create a mock intelligence service that returns a fixed reply."""
    mock = MagicMock(spec=IntelligenceService)
    mock.generate.return_value = AIResponse(
        request_id="test",
        provider="mock",
        model="mock-model",
        content=reply,
        finish_reason=FinishReason.STOP,
        usage=None,
        tool_calls=None,
        metadata={},
    )
    return mock


def make_mock_voice() -> MagicMock:
    """Create a mock voice service."""
    mock = MagicMock()
    mock.availability = "healthy"
    return mock


class TestChatSessionInit:
    def test_text_mode_no_voice(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence()
        session = ChatSession(cfg, intel, text_mode=True)
        assert session._text_mode is True
        assert session._voice is None
        assert session._turn == 0

    def test_voice_mode(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence()
        voice = make_mock_voice()
        session = ChatSession(cfg, intel, voice=voice, text_mode=False)
        assert session._text_mode is False
        assert session._voice is voice

    def test_session_id_format(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        session = ChatSession(cfg, make_mock_intelligence(), text_mode=True)
        assert session._session_id.startswith("chat_")

    def test_history_starts_with_system_prompt(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        session = ChatSession(cfg, make_mock_intelligence(), text_mode=True)
        assert len(session._history) == 1
        assert session._history[0].role.value == "system"
        assert session._history[0].content == SYSTEM_PROMPT


class TestRMS:
    def test_empty(self) -> None:
        assert _rms(b"") == 0.0

    def test_silence(self) -> None:
        data = b"\x00\x00" * 100
        assert _rms(data) == 0.0

    def test_sine_like(self) -> None:
        import struct

        samples = [1000, -1000, 1000, -1000]
        data = struct.pack(f"<{len(samples)}h", *samples)
        rms = _rms(data)
        assert rms > 0


class TestResample:
    def test_empty(self) -> None:
        assert _resample_mono(b"", 44100, 1) == b""

    def test_passthrough_16k_mono(self) -> None:
        import struct

        samples = [1000, -1000, 2000, -2000]
        data = struct.pack(f"<{len(samples)}h", *samples)
        assert _resample_mono(data, SAMPLE_RATE, 1) == data

    def test_stereo_to_mono(self) -> None:
        import struct

        # L=1000, R=3000 -> mono avg = 2000
        data = struct.pack("<4h", 1000, 3000, 1000, 3000)
        out = _resample_mono(data, SAMPLE_RATE, 2)
        samples = struct.unpack(f"<{len(out) // 2}h", out)
        assert len(samples) == 2
        assert all(s == 2000 for s in samples)

    def test_44100_to_16k(self) -> None:
        import struct

        samples = [1000] * 4410  # 0.1s at 44.1kHz
        data = struct.pack(f"<{len(samples)}h", *samples)
        out = _resample_mono(data, 44100, 1)
        # 0.1s at 16kHz = 1600 samples
        assert len(out) // 2 == 1600


class TestExitCommands:
    def test_all_exit_commands(self) -> None:
        assert "exit" in EXIT_COMMANDS
        assert "quit" in EXIT_COMMANDS
        assert "goodbye" in EXIT_COMMANDS
        assert "bye" in EXIT_COMMANDS
        assert "stop" in EXIT_COMMANDS
        assert "shut down" in EXIT_COMMANDS

    def test_case_insensitive(self) -> None:
        for cmd in EXIT_COMMANDS:
            assert cmd.upper().lower() == cmd.lower()


class TestTextMode:
    def test_single_turn(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence("I am Great Sage.")
        session = ChatSession(cfg, intel, text_mode=True)

        with patch("builtins.input", side_effect=["hello"]):
            result = session._text_turn()

        assert result is True  # should continue
        assert len(session._history) == 3  # system + user + assistant

    def test_exit_command(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence()
        session = ChatSession(cfg, intel, text_mode=True)

        with patch("builtins.input", side_effect=["exit"]):
            result = session._text_turn()

        assert result is False  # should stop

    def test_empty_input_continues(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence()
        session = ChatSession(cfg, intel, text_mode=True)

        with patch("builtins.input", side_effect=["", "  ", "exit"]):
            assert session._text_turn() is True  # empty
            assert session._text_turn() is True  # whitespace
            assert session._text_turn() is False  # exit

    def test_eof_error_stops(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence()
        session = ChatSession(cfg, intel, text_mode=True)

        with patch("builtins.input", side_effect=EOFError):
            result = session._text_turn()

        assert result is False

    def test_generation_failure_continues(self, tmp_path: Path) -> None:
        from greatsage.exceptions import JarvisError

        cfg = load_config(write_config(tmp_path)).config
        intel = MagicMock(spec=IntelligenceService)
        intel.generate.side_effect = JarvisError("provider down")
        session = ChatSession(cfg, intel, text_mode=True)

        with patch("builtins.input", side_effect=["hello"]):
            result = session._text_turn()

        assert result is True  # continues despite failure
        # history should NOT have the failed user message
        assert len(session._history) == 1  # only system prompt


class TestVoiceMode:
    def test_voice_turn_processes_transcript(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence("Hello operator!")
        voice = make_mock_voice()
        session = ChatSession(cfg, intel, voice=voice, text_mode=False)

        # Mock record_from_mic to return fake WAV
        fake_wav = b"RIFF" + b"\x00" * 40 + b"WAVE" + b"\x00" * 100
        transcript = MagicMock()
        transcript.text = "Hello Great Sage"

        with patch("greatsage.chat.record_from_mic", return_value=fake_wav):
            voice.listen.return_value = transcript
            with patch("greatsage.chat.play_audio"):
                voice.speak.return_value = MagicMock()
                result = session._voice_turn()

        assert result is True
        intel.generate.assert_called_once()

    def test_voice_no_speech_continues(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence()
        voice = make_mock_voice()
        session = ChatSession(cfg, intel, voice=voice, text_mode=False)

        transcript = MagicMock()
        transcript.text = ""

        fake_wav = b"RIFF" + b"\x00" * 40 + b"WAVE" + b"\x00" * 100
        with patch("greatsage.chat.record_from_mic", return_value=fake_wav):
            voice.listen.return_value = transcript
            result = session._voice_turn()

        assert result is True
        intel.generate.assert_not_called()


class TestHistoryTrimming:
    def test_trims_to_max(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        session = ChatSession(cfg, make_mock_intelligence(), text_mode=True)

        # Add more history than MAX_HISTORY_TURNS
        for i in range(MAX_HISTORY_TURNS + 5):
            session._history.append(Message.user(f"q{i}"))
            session._history.append(Message.assistant(f"a{i}"))

        session._trim_history()

        # Should have system + MAX_HISTORY_TURNS * 2 messages
        expected = 1 + MAX_HISTORY_TURNS * 2
        assert len(session._history) == expected
        # System prompt should still be first
        assert session._history[0].role.value == "system"


class TestGenerate:
    def test_generate_appends_to_history(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence("test reply")
        session = ChatSession(cfg, intel, text_mode=True)

        reply = session._generate("hello")

        assert reply == "test reply"
        assert len(session._history) == 3  # system + user + assistant
        assert session._history[1].role.value == "user"
        assert session._history[2].role.value == "assistant"

    def test_generate_empty_response(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        intel = make_mock_intelligence("")
        session = ChatSession(cfg, intel, text_mode=True)

        reply = session._generate("hello")

        assert reply is None
        # Empty response should not be appended to history
        assert len(session._history) == 2  # system + user only


class TestProcessInput:
    def test_process_input_returns_false_on_exit(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        session = ChatSession(cfg, make_mock_intelligence(), text_mode=True)

        result = session._process_input("goodbye")
        assert result is False

    def test_process_input_returns_true_on_normal(self, tmp_path: Path) -> None:
        cfg = load_config(write_config(tmp_path)).config
        session = ChatSession(cfg, make_mock_intelligence("ok"), text_mode=True)

        result = session._process_input("hello")
        assert result is True
