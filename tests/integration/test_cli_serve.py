"""CLI wiring for the local control plane."""

from __future__ import annotations

from typing import Any

from greatsage.cli import EXIT_INVALID, EXIT_OK, main


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
