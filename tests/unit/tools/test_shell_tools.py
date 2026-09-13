"""Shell tool tests (spec §25-29, §31, §39): timeout, limits, env, cwd."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from greatsage.exceptions import ToolExecutionError, ToolValidationError
from greatsage.tools.models import ToolContext
from greatsage.tools.shell_tools import ShellExecuteTool

PYTHON = sys.executable


def make_context(
    tmp_path: Path,
    timeout: float = 30.0,
    max_bytes: int = 65536,
) -> ToolContext:
    cwd = tmp_path / "workspace"
    cwd.mkdir(exist_ok=True)
    return ToolContext(
        working_directory=cwd,
        environment={"PATH": "C:/Windows/System32"},
        timeout_seconds=timeout,
        max_output_bytes=max_bytes,
    )


def run(command: list[str], **kwargs: object) -> dict:
    context = kwargs.pop("context", None)
    assert context is not None
    result = ShellExecuteTool().execute({"command": command, **kwargs}, context)
    return {"success": result.success, "output": result.output, "error": result.error}


def test_successful_command(tmp_path: Path) -> None:
    info = run([PYTHON, "--version"], context=make_context(tmp_path))
    assert info["success"] is True
    assert info["output"]["exit_code"] == 0
    assert "Python" in info["output"]["stdout"]


def test_nonzero_exit_code_is_failure(tmp_path: Path) -> None:
    info = run([PYTHON, "-c", "import sys; sys.exit(3)"], context=make_context(tmp_path))
    assert info["success"] is False
    assert "exit code 3" in (info["error"] or "")
    assert info["output"]["exit_code"] == 3


def test_timeout_kills_and_reports(tmp_path: Path) -> None:
    info = run(
        [PYTHON, "-c", "import time; time.sleep(60)"],
        context=make_context(tmp_path, timeout=1.0),
    )
    assert info["success"] is False
    assert info["output"]["timed_out"] is True
    assert "timed out" in (info["error"] or "")


def test_stdout_truncated_at_limit(tmp_path: Path) -> None:
    context = make_context(tmp_path, max_bytes=200)
    info = run([PYTHON, "-c", "print('x' * 10000)"], context=context)
    assert info["success"] is True
    assert info["output"]["stdout_truncated"] is True
    assert len(info["output"]["stdout"]) <= 200


def test_small_output_not_truncated(tmp_path: Path) -> None:
    info = run([PYTHON, "-c", "print('tiny')"], context=make_context(tmp_path))
    assert info["output"]["stdout_truncated"] is False
    assert "tiny" in info["output"]["stdout"]


def test_env_additions_allowed(tmp_path: Path) -> None:
    info = run(
        [PYTHON, "-c", "import os; print(os.environ.get('GREATSAGE_TEST_VAR'))"],
        env={"GREATSAGE_TEST_VAR": "visible"},
        context=make_context(tmp_path),
    )
    assert info["success"] is True
    assert "visible" in info["output"]["stdout"]


def test_env_secret_names_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolValidationError, match="not allowed"):
        ShellExecuteTool().execute(
            {"command": [PYTHON, "--version"], "env": {"OPENAI_API_KEY": "sk-x"}},
            make_context(tmp_path),
        )
    with pytest.raises(ToolValidationError, match="not allowed"):
        ShellExecuteTool().execute(
            {"command": [PYTHON, "--version"], "env": {"db_password": "pw"}},
            make_context(tmp_path),
        )


def test_env_non_string_value_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolValidationError, match="must be a string"):
        ShellExecuteTool().execute(
            {"command": [PYTHON, "--version"], "env": {"FOO": 5}},
            make_context(tmp_path),
        )


def test_cwd_explicit_and_enforced(tmp_path: Path) -> None:
    info = run(
        [PYTHON, "-c", "import os; print(os.getcwd())"],
        cwd=str(tmp_path / "workspace"),
        context=make_context(tmp_path),
    )
    assert info["success"] is True
    assert str(tmp_path / "workspace") in info["output"]["stdout"]


def test_cwd_missing_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="working directory does not exist"):
        ShellExecuteTool().execute(
            {"command": [PYTHON, "--version"], "cwd": str(tmp_path / "gone")},
            make_context(tmp_path),
        )


def test_unstartable_command_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="could not start"):
        ShellExecuteTool().execute(
            {"command": ["definitely-not-a-real-binary-xyz"]},
            make_context(tmp_path),
        )


def test_always_vector_shell_false(tmp_path: Path) -> None:
    # spec §27: never shell=True — verify by executing a command that would
    # only work through a shell metacharacter, and confirm it fails.
    info = run(
        [PYTHON, "-c", "print('a')", "&&", "print('b')"],
        context=make_context(tmp_path),
    )
    # the python -c receives 'print(a) && print(b)' as argv[1]? No: args after
    # -c are sys.argv; here "&&" would break argv only through a shell, which
    # we do not use. python -c "print('a')" runs fine with extra argv.
    assert info["success"] is True


def test_duration_reported(tmp_path: Path) -> None:
    info = run([PYTHON, "--version"], context=make_context(tmp_path))
    assert info["output"]["duration_ms"] >= 0.0


def test_output_schema_required_fields_present(tmp_path: Path) -> None:
    info = run([PYTHON, "--version"], context=make_context(tmp_path))
    for field in ("exit_code", "stdout", "stderr", "timed_out",
                  "stdout_truncated", "stderr_truncated", "duration_ms",
                  "cwd"):
        assert field in info["output"]
