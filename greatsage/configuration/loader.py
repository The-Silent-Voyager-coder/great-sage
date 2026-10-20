"""Configuration loading: defaults → YAML file → environment variables.

Precedence (low → high): built-in defaults, YAML configuration file,
environment variables (GREATSAGE_*), explicit CLI overrides (passed as arguments).
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from greatsage.configuration.defaults import DEFAULTS
from greatsage.configuration.model import SageConfig
from greatsage.configuration.validation import apply_env, build_config, format_problems, validate
from greatsage.exceptions import ConfigurationError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "sage.yaml"
CONFIG_PATH_ENV = "GREATSAGE_CONFIG_PATH"


@dataclass(frozen=True)
class LoadedConfig:
    """Validated configuration plus its provenance (for CLI reporting)."""

    config: SageConfig
    source: str
    config_path: Path | None


def _merge_dict(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _resolve_path(config_path: str | Path | None, environ: Mapping[str, str]) -> Path | None:
    candidate: Path | None
    if config_path is not None:
        candidate = Path(config_path).expanduser()
        if not candidate.is_file():
            raise ConfigurationError(f"configuration file not found: {candidate}")
        return candidate
    env_path = environ.get(CONFIG_PATH_ENV)
    if env_path:
        candidate = Path(env_path).expanduser()
        if not candidate.is_file():
            raise ConfigurationError(
                f"{CONFIG_PATH_ENV} points to a missing file: {candidate}"
            )
        return candidate
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"malformed YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration file {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigurationError(
            f"configuration file {path} must contain a mapping, got {type(data).__name__}"
        )
    return data


def load_config(
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> LoadedConfig:
    """Load, merge, validate, and freeze the runtime configuration.

    Raises ConfigurationError with a precise, secret-free description on any
    invalid input (missing file, malformed YAML, schema violations).
    """
    environment: Mapping[str, str] = os.environ if environ is None else environ
    path = _resolve_path(config_path, environment)

    raw: dict[str, Any] = copy.deepcopy(DEFAULTS)
    if path is not None:
        raw = _merge_dict(raw, _load_yaml(path))
    try:
        apply_env(raw, dict(environment))
    except ValueError as exc:
        raise ConfigurationError(f"environment variable error: {exc}") from exc

    problems = validate(raw)
    if problems:
        raise ConfigurationError(format_problems(problems))

    config = build_config(raw)
    source = str(path.resolve()) if path is not None else "built-in defaults"
    return LoadedConfig(config=config, source=source, config_path=path)
