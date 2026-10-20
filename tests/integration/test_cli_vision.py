"""CLI vision integration tests: health/capture/describe through real config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, main


def write_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    path = tmp_path / "vision-cli.yaml"
    path.write_text(
        f"""
core:
  name: "Great Sage Vision CLI Test"
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


def test_vision_health(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["vision", "health", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Vision Health" in out
    assert "stub" in out


def test_vision_health_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["vision", "health", "--json", "--config", str(config)])
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["available"] is True
    assert data["backend"] == "stub"


def test_vision_capture(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["vision", "capture", "--width", "32", "--height", "20", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Vision Capture" in out
    assert "32x20" in out


def test_vision_capture_outside_roots_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    code = main(["vision", "capture", "--out", "C:/Windows/Temp/x.bmp", "--config", str(config)])
    assert code == EXIT_INVALID


def test_vision_describe_latest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    assert main(["vision", "capture", "--config", str(config)]) == EXIT_OK
    capsys.readouterr()
    code = main(["vision", "describe", "--config", str(config)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Vision Description" in out
    assert "stub-no-ocr" in out


def test_vision_describe_empty_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    code = main(["vision", "describe", "--config", str(config)])
    assert code == EXIT_FAILURE
