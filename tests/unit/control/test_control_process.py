"""Background control-plane process management (offline)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from greatsage.control.process import (
    ServeProcessError,
    ServeState,
    clear_state,
    load_state,
    pid_alive,
    probe,
    save_state,
    start_background,
    state_path,
    status,
    stop_background,
)


def _state(pid: int = 1234, port: int = 8765) -> ServeState:
    return ServeState(
        pid=pid,
        host="127.0.0.1",
        port=port,
        url=f"http://127.0.0.1:{port}/",
        started_at="2026-10-22T00:00:00+00:00",
        config=None,
        log_path="C:/GREATSAGE/runtime/greatsage-serve.log",
    )


def test_state_roundtrip(tmp_path: Path) -> None:
    save_state(tmp_path, _state())

    restored = load_state(tmp_path)

    assert restored == _state()
    clear_state(tmp_path)
    assert load_state(tmp_path) is None


def test_pid_alive_for_current_process_and_invalid_pid() -> None:
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_probe_rejects_non_http_and_unreachable_targets() -> None:
    assert probe("https://127.0.0.1:1/") is False
    assert probe("file:///tmp/page") is False
    assert probe("http://127.0.0.1:1/", timeout=0.05) is False


def test_status_reports_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_state(tmp_path, _state())
    monkeypatch.setattr("greatsage.control.process.pid_alive", lambda _pid: True)
    monkeypatch.setattr("greatsage.control.process.probe", lambda _url: True)

    result = status(tmp_path)

    assert result["status"] == "running"
    assert result["state"]["pid"] == 1234


def test_status_clears_stale_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_state(tmp_path, _state())
    monkeypatch.setattr("greatsage.control.process.pid_alive", lambda _pid: False)

    result = status(tmp_path)

    assert result["status"] == "stale"
    assert not state_path(tmp_path).exists()


def test_stop_refuses_unresponsive_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_state(tmp_path, _state())
    monkeypatch.setattr("greatsage.control.process.pid_alive", lambda _pid: True)
    monkeypatch.setattr("greatsage.control.process.probe", lambda _url: False)
    terminated: list[int] = []
    monkeypatch.setattr(
        "greatsage.control.process._terminate_pid",
        lambda pid, **_kwargs: terminated.append(pid),
    )

    with pytest.raises(ServeProcessError, match="unresponsive"):
        stop_background(tmp_path)

    assert terminated == []
    assert state_path(tmp_path).exists()


def test_stop_terminates_verified_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_state(tmp_path, _state())
    monkeypatch.setattr("greatsage.control.process.pid_alive", lambda _pid: True)
    monkeypatch.setattr("greatsage.control.process.probe", lambda _url: True)
    terminated: list[int] = []
    monkeypatch.setattr(
        "greatsage.control.process._terminate_pid",
        lambda pid, **_kwargs: terminated.append(pid),
    )

    stopped = stop_background(tmp_path)

    assert stopped.pid == 1234
    assert terminated == [1234]
    assert not state_path(tmp_path).exists()


def test_start_refuses_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_state(tmp_path, _state())
    monkeypatch.setattr("greatsage.control.process.pid_alive", lambda _pid: True)
    monkeypatch.setattr("greatsage.control.process.probe", lambda _url: True)

    with pytest.raises(ServeProcessError, match="already running"):
        start_background(
            runtime_dir=tmp_path,
            host="127.0.0.1",
            port=8765,
            config_path=None,
            cwd=tmp_path,
        )


def test_start_spawns_detached_child_and_saves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 4321

        def poll(self) -> None:
            return None

    def fake_popen(command, **kwargs):  # noqa: ANN001
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("greatsage.control.process.subprocess.Popen", fake_popen)
    monkeypatch.setattr("greatsage.control.process.probe", lambda _url: True)

    started = start_background(
        runtime_dir=tmp_path,
        host="127.0.0.1",
        port=8765,
        config_path=tmp_path / "sage.yaml",
        cwd=tmp_path,
    )

    assert started.pid == 4321
    assert load_state(tmp_path) == started
    command = captured["command"]
    assert command[:3] == [sys.executable, "-m", "greatsage"]
    assert "serve" in command and "--host" in command and "--port" in command
    assert "--config" in command
    assert captured["kwargs"]["stdin"] is not None


def test_start_reports_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DeadProcess:
        pid = 4321

        def poll(self) -> int:
            return 7

    monkeypatch.setattr(
        "greatsage.control.process.subprocess.Popen",
        lambda *_args, **_kwargs: DeadProcess(),
    )

    with pytest.raises(ServeProcessError, match="exited with code 7"):
        start_background(
            runtime_dir=tmp_path,
            host="127.0.0.1",
            port=8765,
            config_path=None,
            cwd=tmp_path,
        )
