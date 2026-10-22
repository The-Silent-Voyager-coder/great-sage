"""Background process management for the local control plane.

The control plane is still a loopback-only presentation surface. This module
only owns the lifecycle of the ``greatsage serve`` child process: a small JSON
state file in ``core.runtime_dir``, a log file, liveness checks, and explicit
start/stop operations.

No page token, secret, or API key is written to the state file. ``serve stop``
only terminates a process after the loopback dashboard answers, which keeps a
stale/reused PID from turning into an arbitrary kill.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from greatsage.tools.redaction import redact_secrets

STATE_FILENAME = "greatsage-serve.json"
LOG_FILENAME = "greatsage-serve.log"
_READY_MARKER = b"Great Sage Control Plane"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class ServeProcessError(RuntimeError):
    """A background control-plane operation could not be completed."""


@dataclass(frozen=True)
class ServeState:
    pid: int
    host: str
    port: int
    url: str
    started_at: str
    config: str | None
    log_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServeState:
        pid = int(data["pid"])
        port = int(data["port"])
        host = str(data["host"])
        url = str(data["url"])
        if pid <= 0:
            raise ValueError("pid must be positive")
        if not 0 <= port <= 65535:
            raise ValueError("port must be 0..65535")
        if not host or not url:
            raise ValueError("host and url are required")
        return cls(
            pid=pid,
            host=host,
            port=port,
            url=url,
            started_at=str(data.get("started_at") or ""),
            config=str(data["config"]) if data.get("config") else None,
            log_path=str(data.get("log_path") or ""),
        )


def state_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / STATE_FILENAME


def log_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / LOG_FILENAME


def load_state(runtime_dir: str | Path) -> ServeState | None:
    path = state_path(runtime_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return ServeState.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None


def save_state(runtime_dir: str | Path, state: ServeState) -> None:
    path = state_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def clear_state(runtime_dir: str | Path) -> None:
    try:
        state_path(runtime_dir).unlink()
    except FileNotFoundError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        handle = windll.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == _STILL_ACTIVE
        finally:
            windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def probe(url: str, *, timeout: float = 1.0) -> bool:
    """Return True when ``url`` serves the Great Sage control page."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return False
    port = parts.port or 80
    connection = HTTPConnection(parts.hostname, port, timeout=timeout)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read(4096)
    except (OSError, ValueError):
        return False
    finally:
        connection.close()
    return response.status == 200 and _READY_MARKER in body


def status(runtime_dir: str | Path) -> dict[str, Any]:
    state = load_state(runtime_dir)
    if state is None:
        return {"status": "stopped", "detail": "no background server state"}
    if not pid_alive(state.pid):
        clear_state(runtime_dir)
        return {
            "status": "stale",
            "detail": "the recorded process is no longer running",
            "state": state.to_dict(),
        }
    if not probe(state.url):
        return {
            "status": "unresponsive",
            "detail": "the process is running but the dashboard is not responding",
            "state": state.to_dict(),
        }
    return {"status": "running", "detail": "dashboard is responding", "state": state.to_dict()}


def _tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return redact_secrets(tail) or ""


def start_background(
    *,
    runtime_dir: str | Path,
    host: str,
    port: int,
    config_path: str | Path | None = None,
    cwd: str | Path | None = None,
    timeout: float = 20.0,
) -> ServeState:
    """Start a detached ``greatsage serve`` child and wait until it answers."""
    runtime_path = Path(runtime_dir)
    existing = load_state(runtime_path)
    if existing is not None:
        if pid_alive(existing.pid):
            if probe(existing.url):
                raise ServeProcessError(
                    f"control plane already running at {existing.url} (pid {existing.pid})"
                )
            raise ServeProcessError(
                f"process {existing.pid} is alive but not responding; run `greatsage serve stop`"
            )
        clear_state(runtime_path)

    runtime_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path(runtime_path)
    command = [
        sys.executable,
        "-m",
        "greatsage",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    resolved_config: str | None = None
    if config_path is not None:
        resolved_config = str(Path(config_path).resolve())
        command.extend(["--config", resolved_config])

    creationflags = 0
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "DETACHED_PROCESS", 0)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    with log_file.open("ab") as output:
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **popen_kwargs,
        )

    url = f"http://{host}:{port}/"
    state = ServeState(
        pid=process.pid,
        host=host,
        port=port,
        url=url,
        started_at=datetime.now(UTC).isoformat(),
        config=resolved_config,
        log_path=str(log_file),
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            detail = _tail(log_file)
            raise ServeProcessError(
                f"background server exited with code {code}"
                + (f": {detail}" if detail else "")
            )
        if probe(url):
            save_state(runtime_path, state)
            return state
        time.sleep(0.2)

    _terminate_pid(process.pid, timeout=5.0)
    detail = _tail(log_file)
    raise ServeProcessError(
        f"background server did not become ready within {timeout:.0f}s"
        + (f": {detail}" if detail else "")
    )


def stop_background(
    runtime_dir: str | Path, *, timeout: float = 10.0
) -> ServeState:
    """Stop the verified background dashboard and clear its state file."""
    state = load_state(runtime_dir)
    if state is None:
        raise ServeProcessError("control plane is not running")
    if not pid_alive(state.pid):
        clear_state(runtime_dir)
        raise ServeProcessError("control plane is not running (stale state removed)")
    if not probe(state.url):
        raise ServeProcessError(
            "control plane process is alive but unresponsive; refusing to kill it"
        )
    _terminate_pid(state.pid, timeout=timeout)
    clear_state(runtime_dir)
    return state


def _terminate_pid(pid: int, *, timeout: float) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise ServeProcessError(f"permission denied stopping pid {pid}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    else:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return
    if pid_alive(pid):
        raise ServeProcessError(f"could not stop process {pid}")
