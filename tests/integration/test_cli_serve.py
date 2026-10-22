"""CLI wiring for the local control plane."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from greatsage.cli import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, main
from greatsage.control.process import (
    ServeProcessError,
    ServeState,
    save_state,
    state_path,
)


def test_serve_refuses_non_loopback_before_start() -> None:
    code = main(["serve", "--host", "0.0.0.0", "--port", "0"])
    assert code == EXIT_INVALID


def test_serve_starts_and_stops_cleanly(monkeypatch: Any, capsys: Any) -> None:
    stopped: list[bool] = []

    class FakeRuntime:
        async def stop(self) -> None:
            stopped.append(True)

    monkeypatch.setattr("greatsage.cli._runtime_from_args", lambda args: FakeRuntime())

    def interrupt(self: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "greatsage.control.server.ControlServer.serve_forever", interrupt
    )
    code = main(["serve", "--port", "0"])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "Great Sage Control Plane" in captured.out
    assert stopped == [True]


def _state() -> ServeState:
    return ServeState(
        pid=4321,
        host="127.0.0.1",
        port=8765,
        url="http://127.0.0.1:8765/",
        started_at="2026-10-22T00:00:00+00:00",
        config=None,
        log_path="C:/GREATSAGE/runtime/greatsage-serve.log",
    )


def test_serve_status_reports_stopped(
    valid_config_yaml: Path, capsys: Any
) -> None:
    code = main(["serve", "status", "--config", str(valid_config_yaml)])

    assert code == EXIT_FAILURE
    assert "status stopped" in capsys.readouterr().out


def test_serve_status_reports_running(
    valid_config_yaml: Path, capsys: Any, monkeypatch: Any
) -> None:
    from greatsage.control import process

    runtime_dir = valid_config_yaml.parent / "runtime"
    save_state(runtime_dir, _state())
    monkeypatch.setattr(process, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(process, "probe", lambda _url: True)

    code = main(["serve", "status", "--config", str(valid_config_yaml)])
    captured = capsys.readouterr()

    assert code == EXIT_OK
    assert "status running" in captured.out
    assert "pid    4321" in captured.out


def test_serve_background_reports_spawned_state(
    valid_config_yaml: Path, capsys: Any, monkeypatch: Any
) -> None:
    from greatsage.control import process

    monkeypatch.setattr(process, "start_background", lambda **_kwargs: _state())

    code = main(["serve", "--background", "--config", str(valid_config_yaml)])
    captured = capsys.readouterr()

    assert code == EXIT_OK
    assert "running in background" in captured.out
    assert "http://127.0.0.1:8765/" in captured.out


def test_serve_stop_reports_stopped(
    valid_config_yaml: Path, capsys: Any, monkeypatch: Any
) -> None:
    from greatsage.control import process

    monkeypatch.setattr(process, "stop_background", lambda _runtime_dir: _state())

    code = main(["serve", "stop", "--config", str(valid_config_yaml)])
    captured = capsys.readouterr()

    assert code == EXIT_OK
    assert "stopped (pid 4321)" in captured.out


def test_serve_stop_failure_is_reported(
    valid_config_yaml: Path, capsys: Any, monkeypatch: Any
) -> None:
    from greatsage.control import process

    def fail(_runtime_dir: Path) -> None:
        raise ServeProcessError("not running")

    monkeypatch.setattr(process, "stop_background", fail)

    code = main(["serve", "stop", "--config", str(valid_config_yaml)])

    assert code == EXIT_FAILURE
    assert "greatsage serve stop: not running" in capsys.readouterr().err


def test_serve_background_rejects_status_mode(
    valid_config_yaml: Path, capsys: Any
) -> None:
    code = main(
        ["serve", "status", "--background", "--config", str(valid_config_yaml)]
    )

    assert code == EXIT_INVALID
    assert "--background cannot be used with status" in capsys.readouterr().err


def test_serve_background_rejects_port_zero(
    valid_config_yaml: Path, capsys: Any
) -> None:
    code = main(
        ["serve", "--background", "--port", "0", "--config", str(valid_config_yaml)]
    )

    assert code == EXIT_INVALID
    assert "requires an explicit non-zero port" in capsys.readouterr().err


def test_serve_status_stale_state_is_cleared(
    valid_config_yaml: Path, capsys: Any, monkeypatch: Any
) -> None:
    from greatsage.control import process

    runtime_dir = valid_config_yaml.parent / "runtime"
    save_state(runtime_dir, _state())
    monkeypatch.setattr(process, "pid_alive", lambda _pid: False)

    code = main(["serve", "status", "--config", str(valid_config_yaml)])

    assert code == EXIT_FAILURE
    assert "status stale" in capsys.readouterr().out
    assert not state_path(runtime_dir).exists()
