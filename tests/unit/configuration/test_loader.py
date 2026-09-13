"""Configuration loader tests: sources, precedence, env overrides, secrets."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from greatsage.configuration.loader import load_config
from greatsage.configuration.model import SecurityMode
from greatsage.exceptions import ConfigurationError


def test_defaults_load_without_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GREATSAGE_CONFIG_PATH", raising=False)
    # Hermetic against an operator-owned config/sage.yaml (documented setup
    # step): point the repo-root probe at a path that cannot exist.
    monkeypatch.setattr(
        "greatsage.configuration.loader.DEFAULT_CONFIG_PATH",
        tmp_path / "absent.yaml",
    )
    loaded = load_config()
    assert loaded.config_path is None
    assert loaded.source == "built-in defaults"
    assert loaded.config.core.name == "Great Sage"
    assert loaded.config.security.default_mode is SecurityMode.ASK
    assert loaded.config.ai.opencode.base_url == "http://127.0.0.1:4096"


def test_valid_file_overrides_defaults(valid_config_yaml: Path) -> None:
    loaded = load_config(valid_config_yaml)
    assert loaded.config_path is not None
    assert loaded.config.core.name == "J.A.R.V.I.S. Test"
    assert loaded.config.logging.level == "DEBUG"


def test_missing_explicit_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="configuration file not found"):
        load_config(tmp_path / "nope.yaml")


def test_malformed_yaml_fails(malformed_config_yaml: Path) -> None:
    with pytest.raises(ConfigurationError, match="malformed YAML"):
        load_config(malformed_config_yaml)


def test_invalid_type_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("events:\n  queue_maxsize: \"many\"\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_config(path)
    message = str(exc.value)
    assert "events.queue_maxsize" in message
    assert "Expected:" in message


def test_partial_provider_entry_completed_by_defaults(tmp_path: Path) -> None:
    # Merging happens against the built-in defaults: a provider entry with only
    # some fields is completed, not rejected.
    path = tmp_path / "partial.yaml"
    path.write_text(
        "ai:\n  providers:\n    opencode:\n      base_url: \"http://127.0.0.1:4321\"\n",
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert loaded.config.ai.opencode.base_url == "http://127.0.0.1:4321"
    assert loaded.config.ai.opencode.timeout_seconds == 300.0
    assert loaded.config.ai.opencode.enabled is False  # default preserved


def test_invalid_port_in_url_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "ai:\n  providers:\n    opencode:\n      base_url: \"http://127.0.0.1:99999\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as exc:
        load_config(path)
    assert "base_url" in str(exc.value)


def test_unknown_section_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("hud:\n  enabled: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_config(path)
    assert "hud" in str(exc.value)


def test_environment_override(valid_config_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREATSAGE_EVENTS__QUEUE_MAXSIZE", "42")
    loaded = load_config(valid_config_yaml)
    assert loaded.config.events.queue_maxsize == 42


def test_environment_beats_yaml(valid_config_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREATSAGE_LOGGING__LEVEL", "WARNING")
    loaded = load_config(valid_config_yaml)
    assert loaded.config.logging.level == "WARNING"
    assert loaded.config.core.name == "J.A.R.V.I.S. Test"  # yaml still applies elsewhere


def test_environment_beats_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GREATSAGE_CONFIG_PATH", raising=False)
    monkeypatch.setenv("GREATSAGE_SECURITY__DEFAULT_MODE", "deny")
    loaded = load_config()
    assert loaded.config.security.default_mode is SecurityMode.DENY


def test_bad_environment_bool_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREATSAGE_SECURITY__ALLOW_AUTO_APPROVE_READ", "maybe")
    with pytest.raises(ConfigurationError, match="GREATSAGE_SECURITY__ALLOW_AUTO_APPROVE_READ"):
        load_config()


def test_secrets_never_logged(
    valid_config_yaml: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GREATSAGE_AI__PROVIDERS__OPENCODE__BASE_URL", "http://user:hunter2@127.0.0.1:4096")
    with caplog.at_level(logging.DEBUG):
        loaded = load_config(valid_config_yaml)
    assert loaded.config.ai.opencode.base_url == "http://user:hunter2@127.0.0.1:4096"
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "hunter2" not in joined


def test_load_config_does_not_emit_logs(
    valid_config_yaml: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        load_config(valid_config_yaml)
    assert not caplog.records
