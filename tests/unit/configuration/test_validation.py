"""Validation unit tests: types, enums, structural rules."""

from __future__ import annotations

import pytest

from greatsage.configuration.validation import apply_env, coerce_env, validate


def test_valid_default_raw_has_no_problems() -> None:
    problems = validate(
        {
            "core": {"name": "x", "data_dir": "C:/GREATSAGE/data"},
            "logging": {"level": "INFO"},
        }
    )
    assert problems == []


def test_invalid_log_level() -> None:
    problems = validate({"logging": {"level": "VERBOSE"}})
    assert len(problems) == 1
    assert problems[0].section == "logging"
    assert problems[0].field == "level"
    assert problems[0].value == "VERBOSE"
    assert "DEBUG" in problems[0].expected


def test_invalid_boolean() -> None:
    problems = validate({"memory": {"auto_save_conversations": "true"}})
    assert len(problems) == 1
    assert problems[0].field == "auto_save_conversations"


def test_auto_save_conversations_true_refused() -> None:
    problems = validate({"memory": {"auto_save_conversations": True}})
    assert len(problems) == 1
    assert problems[0].field == "auto_save_conversations"
    assert problems[0].value is True
    assert "false" in problems[0].expected


def test_auto_save_conversations_false_accepted() -> None:
    problems = validate({"memory": {"auto_save_conversations": False}})
    assert problems == []


def test_invalid_security_mode() -> None:
    problems = validate({"security": {"default_mode": "always"}})
    assert problems[0].field == "default_mode"


def test_invalid_tool_security_mode() -> None:
    problems = validate({"security": {"mode": "chaotic"}})
    assert problems[0].field == "mode"
    assert "lockdown" in problems[0].expected


def test_valid_tool_security_modes() -> None:
    for mode in ("lockdown", "normal", "development"):
        assert validate({"security": {"mode": mode}}) == []


def test_invalid_working_directory() -> None:
    problems = validate({"tools": {"working_directory": "relative"}})
    assert problems[0].field == "working_directory"


def test_invalid_execution_timeout() -> None:
    problems = validate({"tools": {"execution_timeout_seconds": 0}})
    assert problems[0].field == "execution_timeout_seconds"


def test_invalid_max_output_bytes() -> None:
    problems = validate({"tools": {"max_output_bytes": -1}})
    assert problems[0].field == "max_output_bytes"


def test_allowed_roots_rejects_non_list() -> None:
    problems = validate({"tools": {"allowed_roots": "C:/GREATSAGE"}})
    assert problems[0].field == "allowed_roots"


def test_allowed_roots_rejects_relative_item() -> None:
    problems = validate({"tools": {"allowed_roots": ["C:/GREATSAGE", "relative/path"]}})
    assert problems[0].field == "allowed_roots"


def test_allowed_roots_accepts_absolute_list() -> None:
    assert validate({"tools": {"allowed_roots": ["C:/GREATSAGE", "D:/work"]}}) == []


def test_denied_roots_accepts_empty_list() -> None:
    assert validate({"tools": {"denied_roots": []}}) == []


def test_coerce_env_path_list() -> None:
    assert coerce_env("path_list", "C:/a;C:/b") == ["C:/a", "C:/b"]
    with pytest.raises(ValueError):
        coerce_env("path_list", ";;")


def test_invalid_risk_level() -> None:
    problems = validate({"tools": {"terminal": {"default_risk": "ALWAYS"}}})
    assert problems[0].field == "terminal.default_risk"


def test_negative_retention() -> None:
    problems = validate({"logging": {"retention_days": -5}})
    assert problems[0].field == "retention_days"


def test_zero_worker_count() -> None:
    problems = validate({"events": {"worker_count": 0}})
    assert problems[0].field == "worker_count"


def test_relative_path_rejected() -> None:
    problems = validate({"core": {"data_dir": "relative/path"}})
    assert problems[0].field == "data_dir"


def test_unknown_provider_name() -> None:
    problems = validate({"ai": {"providers": {"gpt": {"type": "local"}}}})
    assert any("providers.gpt" in p.dotted_path for p in problems)


def test_unknown_provider_field() -> None:
    problems = validate({"ai": {"providers": {"local": {"model": "x", "weird": 1}}}})
    assert any(p.field == "weird" for p in problems)


def test_provider_missing_field() -> None:
    problems = validate({"ai": {"providers": {"opencode": {"base_url": "http://x"}}}})
    assert any("providers.opencode.type" in p.dotted_path for p in problems)


def test_unknown_top_level_section() -> None:
    problems = validate({"futuristic_hud": {}})
    assert problems[0].section == ""


def test_unknown_nested_key() -> None:
    problems = validate({"core": {"name": "x", "planet": "earth"}})
    assert problems[0].field == "planet"


def test_yaml_dict_where_scalar_expected() -> None:
    problems = validate({"logging": {"level": {"nested": True}}})
    assert problems[0].field == "level"


def test_format_problems_identifies_field_value_expected() -> None:
    from greatsage.configuration.validation import format_problems

    problems = validate({"logging": {"level": 5}})
    rendered = format_problems(problems)
    assert "Configuration error:" in rendered
    assert "logging.level" in rendered
    assert "Expected:" in rendered


def test_coerce_env_types() -> None:
    assert coerce_env("bool", "true") is True
    assert coerce_env("bool", "0") is False
    assert coerce_env("positive_int", "12") == 12
    assert coerce_env("positive_number", "1.5") == 1.5
    assert coerce_env("nonempty_str", "x") == "x"
    with pytest.raises(ValueError):
        coerce_env("bool", "perhaps")
    with pytest.raises(ValueError):
        coerce_env("positive_int", "abc")


def test_apply_env_recognized_only() -> None:
    raw = {"logging": {"level": "INFO"}}
    apply_env(raw, {"GREATSAGE_LOGGING__LEVEL": "ERROR", "GREATSAGE_UNRELATED__X": "1"})
    assert raw["logging"]["level"] == "ERROR"

    raw2 = {"ai": {"providers": {"opencode": {"base_url": "http://127.0.0.1:4096"}}}}
    apply_env(raw2, {"GREATSAGE_AI__PROVIDERS__OPENCODE__BASE_URL": "http://127.0.0.1:7777"})
    assert raw2["ai"]["providers"]["opencode"]["base_url"] == "http://127.0.0.1:7777"
