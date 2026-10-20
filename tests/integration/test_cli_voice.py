"""CLI voice integration tests: health/listen/speak through real config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatsage.cli import EXIT_INVALID, EXIT_OK, main


def write_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    path = tmp_path / "voice-cli.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Voice CLI Test"
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


def test_voice_health(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["voice", "health", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Voice Health" in out
    assert "mock" in out


def test_voice_health_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["voice", "health", "--json", "--config", str(config)])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["available"] is True
    assert data["stt_backend"] == "mock"


def test_voice_listen_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["voice", "listen", "--text", "hello sage", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Voice Transcript" in out
    assert "hello sage" in out


def test_voice_listen_requires_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["voice", "listen", "--config", str(config)])
    assert code == EXIT_INVALID


def test_voice_listen_audio(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from greatsage.voice.tts import encode_wav

    config = write_config(tmp_path)
    audio, _ = encode_wav("hi")
    frame = tmp_path / "in.wav"
    frame.write_bytes(audio)
    code = main(["voice", "listen", "--audio-path", str(frame), "--config", str(config)])
    assert code == EXIT_OK
    assert "Voice Transcript" in capsys.readouterr().out


def test_voice_speak(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["voice", "speak", "--text", "hello", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Voice Speech" in out


def test_voice_speak_outside_roots_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    code = main(
        [
            "voice",
            "speak",
            "--text",
            "hi",
            "--out",
            "C:/Windows/Temp/x.wav",
            "--config",
            str(config),
        ]
    )
    assert code == EXIT_INVALID


def test_voice_speak_writes_wav(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    target = tmp_path / "workspace" / "speech.wav"
    code = main(["voice", "speak", "--text", "hi", "--out", str(target), "--config", str(config)])
    assert code == EXIT_OK
    assert target.exists()
    assert target.read_bytes()[0:4] == b"RIFF"


def test_voice_requires_subcommand(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    with pytest.raises(SystemExit):
        main(["voice", "--config", str(config)])
