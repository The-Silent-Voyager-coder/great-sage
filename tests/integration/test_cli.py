"""CLI integration tests: help, version, config validate, health, exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest

from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, main


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: greatsage" in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "greatsage 0.3.0" in capsys.readouterr().out


def test_config_validate_valid(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["config", "validate", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Configuration valid." in out
    assert str(valid_config_yaml) in out


def test_config_validate_defaults(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GREATSAGE_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        "greatsage.configuration.loader.DEFAULT_CONFIG_PATH",
        tmp_path / "absent.yaml",
    )
    code = main(["config", "validate"])
    assert code == EXIT_OK
    assert "built-in defaults" in capsys.readouterr().out


def test_config_validate_invalid(
    invalid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["config", "validate", "--config", str(invalid_config_yaml)])
    assert code == EXIT_INVALID
    err = capsys.readouterr().err
    assert "Configuration error:" in err
    assert "logging.level" in err


def test_config_validate_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["config", "validate", "--config", str(tmp_path / "absent.yaml")])
    assert code == EXIT_INVALID
    assert "configuration file not found" in capsys.readouterr().err


def test_health_command(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["health", "--config", str(valid_config_yaml)])
    captured = capsys.readouterr()
    out = captured.out
    assert "Great Sage Health" in out
    assert "Core" in out and "HEALTHY" in out
    # Routine lifecycle logs stay off the console; file logs remain JSON.
    assert "runtime started" not in captured.err
    assert '"logger": "greatsage.core.runtime"' not in captured.err
    assert "Configuration" in out
    assert "Event Bus" in out
    assert "Service Registry" in out
    assert "Storage" in out
    assert "Overall" in out
    # Provider-dependent: without a live Ollama the intelligence check (and
    # therefore Overall) is UNHEALTHY — the exit code must match the report,
    # not assume a provider is running (CI runners have none).
    expected = EXIT_OK if "Overall          HEALTHY" in out else EXIT_FAILURE
    assert code == expected


def test_health_with_invalid_config(
    invalid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["health", "--config", str(invalid_config_yaml)])
    assert code == EXIT_INVALID
    assert "Configuration error:" in capsys.readouterr().err


def test_health_unhealthy_storage_returns_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # data_dir collides with an existing file: the runtime cannot start.
    collide = tmp_path / "data-blocked"
    collide.write_text("nope", encoding="utf-8")
    config_path = tmp_path / "bad-storage.yaml"
    d = str(tmp_path).replace("\\", "/")
    config_path.write_text(
        f"core:\n  data_dir: \"{d}/data-blocked\"\n  logs_dir: \"{d}/logs\"\n",
        encoding="utf-8",
    )
    code = main(["health", "--config", str(config_path)])
    assert code == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "runtime startup failed" in err


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    assert code == EXIT_OK
    assert "usage: greatsage" in capsys.readouterr().out


def test_unknown_subcommand_errors() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["config", "frobnicate"])
    assert exc.value.code == EXIT_INVALID


def test_ai_benchmark_command(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["ai", "benchmark"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Hardware benchmark" in out
    assert "platform" in out
    assert "cpu" in out


def test_ai_benchmark_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["ai", "benchmark", "--json"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"platform"' in out


def test_ai_health_command_reports_providers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = str(tmp_path).replace("\\", "/")
    config_path = tmp_path / "ai-config.yaml"
    config_path.write_text(
        f"""
core:
  name: "ai cli test"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "INFO"
  retention_days: 1
ai:
  default_provider: "local"
  providers:
    local:
      type: "local"
      enabled: true
      base_url: "http://127.0.0.1:1"
      model: ""
      timeout_seconds: 1
    opencode:
      type: "opencode"
      enabled: false
      base_url: "http://127.0.0.1:4096"
      api_key_env: ""
      model: ""
      timeout_seconds: 1
""",
        encoding="utf-8",
    )
    code = main(["ai", "health", "--config", str(config_path)])
    assert code == EXIT_FAILURE  # configured provider is unreachable
    out = capsys.readouterr().out
    assert "AI Provider Health" in out
    assert "local" in out


def test_ai_providers_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = str(tmp_path).replace("\\", "/")
    config_path = tmp_path / "ai-config.yaml"
    config_path.write_text(
        f"""
core:
  name: "ai cli test"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "INFO"
  retention_days: 1
ai:
  default_provider: "local"
  providers:
    local:
      type: "local"
      enabled: true
      base_url: "http://127.0.0.1:1"
      model: ""
      timeout_seconds: 1
    opencode:
      type: "opencode"
      enabled: true
      base_url: "http://127.0.0.1:1"
      api_key_env: ""
      model: ""
      timeout_seconds: 1
""",
        encoding="utf-8",
    )
    code = main(["ai", "providers", "--config", str(config_path)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "local" in out
    assert "opencode" in out
    assert "text_generation" in out


def test_ai_health_with_invalid_config(
    invalid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ai", "health", "--config", str(invalid_config_yaml)])
    assert code == EXIT_INVALID
    assert "Configuration error:" in capsys.readouterr().err


# --- memory -------------------------------------------------------------


def _seed_memories(config_path: Path) -> dict[str, str]:
    from greatsage.configuration.loader import load_config
    from greatsage.memory.service import MemoryService

    service = MemoryService()
    service.start(load_config(config_path).config)
    first = service.remember("the quick brown fox", source="cli-seed")
    second = service.remember(
        "preferred coffee is espresso",
        source="cli-seed",
        provenance="user_explicit",
        confidence=0.95,
    )
    service.shutdown()
    return {"fox": first.id, "coffee": second.id}


def test_memory_health_command(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["memory", "health", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Memory Health" in out
    assert "status" in out
    assert "healthy" in out
    assert "sage-memory.db" in out


def test_memory_health_json(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["memory", "health", "--config", str(valid_config_yaml), "--json"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"available": true' in out
    assert '"status": "healthy"' in out


def test_memory_health_with_invalid_config(
    invalid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["memory", "health", "--config", str(invalid_config_yaml)])
    assert code == EXIT_INVALID
    assert "Configuration error:" in capsys.readouterr().err


def test_memory_stats_empty(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["memory", "stats", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Memory Stats" in out
    assert "total" in out
    assert "long_term" in out


def test_memory_list_empty(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["memory", "list", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    assert "(none)" in capsys.readouterr().out


def test_memory_get_unknown(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["memory", "get", "mem_absent", "--config", str(valid_config_yaml)])
    assert code == EXIT_FAILURE
    assert "not found" in capsys.readouterr().err


def test_memory_delete_unknown(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["memory", "delete", "mem_absent", "--config", str(valid_config_yaml)])
    assert code == EXIT_FAILURE
    assert "not found" in capsys.readouterr().err


def test_memory_delete_requires_id_or_filter(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["memory", "delete", "--config", str(valid_config_yaml)])
    assert code == EXIT_INVALID
    assert "at least one filter" in capsys.readouterr().err


def test_memory_bulk_delete_requires_yes(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["memory", "delete", "--type", "long_term", "--config", str(valid_config_yaml)]
    )
    assert code == EXIT_INVALID
    assert "--yes" in capsys.readouterr().err


def test_memory_crud_via_cli(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ids = _seed_memories(valid_config_yaml)

    code = main(["memory", "list", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "cli-seed" not in out  # content hidden by default
    assert ids["coffee"][:24] in out
    assert "conf=0.95" in out

    code = main(["memory", "search", "espresso", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert ids["coffee"][:24] in out
    assert "query match: espresso" in out
    assert "preferred coffee" not in out  # content hidden by default

    code = main(["memory", "search", "espresso", "--content", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "preferred coffee is espresso" in out

    code = main(["memory", "get", ids["coffee"], "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "hidden; pass --content to show" in out
    assert "espresso" not in out

    code = main(["memory", "get", ids["coffee"], "--content", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "preferred coffee is espresso" in out

    code = main(["memory", "delete", ids["fox"], "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    assert "Deleted 1 memory" in capsys.readouterr().out

    code = main(["memory", "get", ids["fox"], "--config", str(valid_config_yaml)])
    assert code == EXIT_FAILURE


def test_memory_bulk_delete_with_yes(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_memories(valid_config_yaml)
    code = main(
        [
            "memory", "delete",
            "--source", "cli-seed",
            "--yes",
            "--config", str(valid_config_yaml),
        ]
    )
    assert code == EXIT_OK
    assert "Deleted 2 memory" in capsys.readouterr().out
    code = main(["memory", "list", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    assert "(none)" in capsys.readouterr().out


def test_memory_list_json(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_memories(valid_config_yaml)
    code = main(["memory", "list", "--json", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"total": 2' in out
    assert '"items"' in out
    assert "content" not in out  # hidden in JSON without --content


def _seed_episodes(config_path: Path) -> None:
    from greatsage.configuration.loader import load_config
    from greatsage.memory.service import MemoryService

    service = MemoryService()
    service.start(load_config(config_path).config)
    service.record_episode("ran the full test suite", action="test", session_id="sess-a")
    service.record_episode("fixed a typo in the docs", action="edit", session_id="sess-a")
    service.record_episode("reviewed the roadmap", action="review", session_id="sess-b")
    service.shutdown()


def test_memory_digest_empty(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["memory", "digest", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Memory Digest" in out
    assert "(none)" in out


def test_memory_digest_groups_by_day_without_content(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_episodes(valid_config_yaml)
    code = main(["memory", "digest", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Memory Digest" in out
    assert "3 episodic" in out
    assert "ran the full test suite" not in out  # content hidden by default


def test_memory_digest_content_and_session_filter(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_episodes(valid_config_yaml)
    code = main(
        ["memory", "digest", "--content", "--config", str(valid_config_yaml)]
    )
    assert code == EXIT_OK
    assert "ran the full test suite" in capsys.readouterr().out

    code = main(
        ["memory", "digest", "--session", "sess-b", "--config", str(valid_config_yaml)]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "1 episodic" in out
    assert "sess-b" in out


def test_memory_digest_json_redacts_without_content(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_episodes(valid_config_yaml)
    code = main(
        ["memory", "digest", "--json", "--config", str(valid_config_yaml)]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"total": 3' in out
    assert '"days"' in out
    assert "content" not in out

    code = main(
        ["memory", "digest", "--json", "--content", "--config", str(valid_config_yaml)]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "ran the full test suite" in out


def test_memory_digest_rejects_bad_days(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["memory", "digest", "--days", "0", "--config", str(valid_config_yaml)])
    assert code == EXIT_INVALID
    assert "--days" in capsys.readouterr().err


def _embedding_config(tmp_path: Path, *, enabled: bool) -> Path:
    d = str(tmp_path).replace("\\", "/")
    path = tmp_path / "emb-cli.yaml"
    path.write_text(
        f"""
core:
  name: "emb cli test"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "INFO"
  retention_days: 1
memory:
  enabled: true
  database_path: "{d}/data/sage-memory.db"
  auto_save_conversations: false
  default_confidence: 0.8
  retention_days: 365
  embeddings_enabled: {"true" if enabled else "false"}
  embedding_model: "fake"
  embedding_base_url: "http://127.0.0.1:11434"
tools:
  working_directory: "{d}/workspace"
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "SYSTEM"
  browser:
    default_risk: "FORBIDDEN"
scheduler:
  enabled: false
  max_schedules: 50
  database_path: "{d}/data/sage-scheduler.db"
telegram:
  enabled: false
  token_env: "GREATSAGE_TEST_TELEGRAM_TOKEN"
  allowed_chat_ids: []
  poll_timeout_seconds: 1
  max_listen_seconds: 60
""",
        encoding="utf-8",
    )
    return path


def test_memory_search_semantic_needs_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _embedding_config(tmp_path, enabled=False)
    code = main(["memory", "search", "cat", "--semantic", "--config", str(cfg)])
    assert code == EXIT_FAILURE
    assert "embeddings_enabled" in capsys.readouterr().err
    code = main(["memory", "reindex", "--config", str(cfg)])
    assert code == EXIT_FAILURE
    assert "embeddings_enabled" in capsys.readouterr().err


def test_memory_search_semantic_and_reindex(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from greatsage.configuration.loader import load_config
    from greatsage.memory.service import MemoryService
    from tests.unit.memory.test_memory_embeddings import _FakeProvider

    cfg = _embedding_config(tmp_path, enabled=True)
    monkeypatch.setattr("greatsage.memory.service.OllamaEmbeddingProvider", _FakeProvider)
    service = MemoryService()
    service.start(load_config(cfg).config)
    try:
        service.remember("the cat sat on the mat")
        service.remember("dogs bark loudly at night")
    finally:
        service.shutdown()
    code = main(["memory", "search", "my cat", "--semantic", "--content", "--config", str(cfg)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "semantic match: my cat" in out
    assert "the cat sat on the mat" in out
    code = main(["memory", "reindex", "--json", "--config", str(cfg)])
    assert code == EXIT_OK
    assert '"embedded": 0' in capsys.readouterr().out  # saves already embedded


def test_briefing_empty(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["briefing", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Great Sage Briefing" in out
    assert "health" in out
    assert "0 in last 1 day(s)" in out
    assert "0 open" in out
    assert "delegation" in out


def test_briefing_with_episode_gates_content(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_episodes(valid_config_yaml)
    code = main(["briefing", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "3 in last 1 day(s)" in out
    assert "ran the full test suite" not in out  # content hidden by default

    code = main(["briefing", "--content", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    assert "ran the full test suite" in capsys.readouterr().out


def test_briefing_json(valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_episodes(valid_config_yaml)
    code = main(["briefing", "--json", "--config", str(valid_config_yaml)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    for key in ('"health"', '"episodes"', '"tasks"', '"plans"', '"delegation"'):
        assert key in out
    assert "content" not in out  # redacted without --content


def test_briefing_rejects_bad_days(
    valid_config_yaml: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["briefing", "--days", "0", "--config", str(valid_config_yaml)])
    assert code == EXIT_INVALID
    assert "--days" in capsys.readouterr().err


# --- delegation ---------------------------------------------------------


def _delegation_cli_config(tmp_path: Path) -> Path:
    d = str(tmp_path).replace("\\", "/")
    path = tmp_path / "delegation-config.yaml"
    path.write_text(
        f"""
core:
  name: "delegation cli test"
  data_dir: "{d}/data"
  cache_dir: "{d}/cache"
  logs_dir: "{d}/logs"
  runtime_dir: "{d}/runtime"
  workspaces_dir: "{d}/workspaces"
  models_dir: "{d}/models"
  backups_dir: "{d}/backups"
  timezone: "UTC"
logging:
  level: "INFO"
  retention_days: 1
memory:
  enabled: false
  database_path: "{d}/data/sage-memory.db"
  auto_save_conversations: false
  default_confidence: 0.8
  retention_days: 365
tools:
  working_directory: "{d}/workspace"
  allowed_roots: ["{d}"]
  denied_roots: []
  terminal:
    default_risk: "SYSTEM"
  browser:
    default_risk: "FORBIDDEN"
delegation:
  enabled: false
  default_provider: "opencode"
  max_wall_time_seconds: 1800.0
  max_output_bytes: 4194304
  max_permission_requests: 50
  max_session_count: 3
  max_delegation_depth: 1
""",
        encoding="utf-8",
    )
    return path


def test_delegation_health_disabled_reports_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["delegation", "health", "--config", str(_delegation_cli_config(tmp_path))])
    assert code == EXIT_FAILURE  # disabled by config -> available False
    out = capsys.readouterr().out
    assert "Great Sage Delegation Health" in out
    assert "status          disabled" in out
    assert "available       False" in out
    assert "enabled         False" in out


def test_delegation_health_disabled_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "delegation", "health", "--json",
            "--config", str(_delegation_cli_config(tmp_path)),
        ]
    )
    assert code == EXIT_FAILURE
    out = capsys.readouterr().out
    assert '"status": "disabled"' in out
    assert '"available": false' in out


def test_delegation_list_when_unavailable_returns_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["delegation", "list", "--config", str(_delegation_cli_config(tmp_path))])
    assert code == EXIT_FAILURE
    assert "delegation subsystem" in capsys.readouterr().err


def test_delegation_get_when_unavailable_returns_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["delegation", "get", "task-1", "--config", str(_delegation_cli_config(tmp_path))]
    )
    assert code == EXIT_FAILURE
    assert "delegation subsystem" in capsys.readouterr().err


def test_delegation_cancel_when_unavailable_returns_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["delegation", "cancel", "task-1", "--config", str(_delegation_cli_config(tmp_path))]
    )
    assert code == EXIT_FAILURE
    assert "delegation subsystem" in capsys.readouterr().err
