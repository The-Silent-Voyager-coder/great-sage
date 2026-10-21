"""Loopback-only HTTP control plane.

This is a presentation/control surface, not a new execution path: agent runs
go through ``AgentService`` and every tool call still crosses the Phase 4
security pipeline. Mutating API calls require a per-server token; the default
bind is loopback only.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import unquote, urlparse

from greatsage.control.state import (
    ControlBusyError,
    ControlNotFoundError,
    ControlState,
    ControlUnavailableError,
    ControlValidationError,
)
from greatsage.control.web import INDEX_HTML

log = logging.getLogger("greatsage.control.server")

MAX_BODY_BYTES = 64 * 1024
_APPROVAL_PATH = re.compile(r"^/api/approvals/([A-Za-z0-9_-]+)/(approve|reject)$")
_PLAN_PATH = re.compile(r"^/api/plans/([A-Za-z0-9_-]+)$")
_PLAN_APPROVE_PATH = re.compile(r"^/api/plans/([A-Za-z0-9_-]+)/approve$")
_PLAN_RUN_PATH = re.compile(r"^/api/plans/([A-Za-z0-9_-]+)/run$")
_TASK_PATH = re.compile(r"^/api/tasks/([A-Za-z0-9_-]+)$")
_TASK_CANCEL_PATH = re.compile(r"^/api/tasks/([A-Za-z0-9_-]+)/cancel$")
_TASK_RESUME_PATH = re.compile(r"^/api/tasks/([A-Za-z0-9_-]+)/resume$")


class ControlServer:
    """A local HTTP server bound to loopback by default."""

    def __init__(
        self,
        runtime: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str | None = None,
    ) -> None:
        if not _is_loopback(host):
            raise ControlValidationError(
                "the control plane only binds to loopback (127.0.0.1 or localhost)"
            )
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ControlValidationError("port must be an integer 0..65535")
        self.state = ControlState(runtime, token=token or secrets.token_urlsafe(32))
        self._httpd = _ControlHTTPServer((host, port), _Handler, self.state)
        self.host = str(self._httpd.server_address[0])
        self.port = int(self._httpd.server_address[1])
        self._serving = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def token(self) -> str:
        return self.state.token

    def serve_forever(self) -> None:
        self._serving = True
        try:
            self._httpd.serve_forever(poll_interval=0.2)
        finally:
            self._serving = False

    def stop(self) -> None:
        try:
            if self._serving:
                self._httpd.shutdown()
        finally:
            self._httpd.server_close()


class _ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        state: ControlState,
    ) -> None:
        self.state = state
        super().__init__(server_address, handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "GreatSageControl/1"
    _body = b""

    @property
    def _state(self) -> ControlState:
        return cast(_ControlHTTPServer, self.server).state

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler name)
        if not self._valid_host():
            return
        path = urlparse(self.path).path
        if path == "/":
            page = INDEX_HTML.replace(
                "__GREATSAGE_TOKEN__", html.escape(self._state.token, quote=True)
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/state":
            if not self._authorized():
                return
            try:
                payload = self._state.snapshot()
            except Exception as exc:
                log.warning(
                    "control-plane state failed: %s", exc, extra={"component": "control"}
                )
                self._send_json(500, {"error": "state unavailable"})
                return
            self._send_json(200, payload)
            return
        if path in ("/api/plans", "/api/tasks"):
            if not self._authorized():
                return
            self._handle_list(path)
            return
        match = _PLAN_PATH.match(path)
        if match:
            if not self._authorized():
                return
            self._handle_plan_detail(unquote(match.group(1)))
            return
        match = _TASK_PATH.match(path)
        if match:
            if not self._authorized():
                return
            self._handle_task_detail(unquote(match.group(1)))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler name)
        if not self._valid_host():
            return
        # Drain the body before auth/path checks: closing with unread bytes
        # makes Windows clients see a connection reset instead of our status.
        if not self._read_body():
            return
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path == "/api/agent":
            self._handle_start()
            return
        if path in ("/api/agent/cancel", "/api/run/cancel"):
            self._state.cancel()
            self._send_json(200, self._state.run_snapshot())
            return
        match = _APPROVAL_PATH.match(path)
        if match:
            request_id = unquote(match.group(1))
            approve = match.group(2) == "approve"
            accepted = self._state.decide(request_id, approve=approve)
            if not accepted:
                self._send_json(404, {"error": "approval is not pending"})
                return
            self._send_json(200, self._state.run_snapshot())
            return
        match = _PLAN_APPROVE_PATH.match(path)
        if match:
            self._handle_plan_approve(unquote(match.group(1)))
            return
        match = _PLAN_RUN_PATH.match(path)
        if match:
            self._handle_plan_run(unquote(match.group(1)))
            return
        match = _TASK_CANCEL_PATH.match(path)
        if match:
            self._handle_task_cancel(unquote(match.group(1)))
            return
        match = _TASK_RESUME_PATH.match(path)
        if match:
            self._handle_task_resume(unquote(match.group(1)))
            return
        self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:  # noqa: N802 (stdlib handler name)
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_start(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        try:
            run = self._state.start_agent(
                payload.get("prompt"),
                session_id=payload.get("session_id"),
                provider=payload.get("provider"),
                model=payload.get("model"),
                max_steps=payload.get("max_steps"),
            )
        except ControlBusyError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        except ControlValidationError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            log.warning(
                "control-plane start failed: %s", exc, extra={"component": "control"}
            )
            self._send_json(500, {"error": "could not start agent run"})
            return
        self._send_json(202, run)

    def _handle_list(self, path: str) -> None:
        try:
            if path == "/api/plans":
                self._send_json(200, {"plans": self._state.list_plans()})
            else:
                self._send_json(200, {"tasks": self._state.list_tasks()})
        except Exception as exc:
            self._send_state_error(exc)

    def _handle_plan_detail(self, plan_id: str) -> None:
        try:
            plan = self._state.get_plan(plan_id)
        except Exception as exc:
            self._send_state_error(exc)
            return
        self._send_json(200, {"plan": plan})

    def _handle_task_detail(self, task_id: str) -> None:
        try:
            task = self._state.get_task(task_id)
        except Exception as exc:
            self._send_state_error(exc)
            return
        self._send_json(200, {"task": task})

    def _handle_plan_approve(self, plan_id: str) -> None:
        if not self._read_approved():
            return
        try:
            plan = self._state.approve_plan(plan_id)
        except Exception as exc:
            self._send_state_error(exc)
            return
        self._send_json(200, {"plan": plan})

    def _handle_plan_run(self, plan_id: str) -> None:
        if not self._read_approved():
            return
        try:
            run = self._state.start_task(plan_id)
        except Exception as exc:
            self._send_state_error(exc)
            return
        self._send_json(202, run)

    def _handle_task_cancel(self, task_id: str) -> None:
        try:
            result = self._state.cancel_task(task_id)
        except Exception as exc:
            self._send_state_error(exc)
            return
        self._send_json(200, result)

    def _handle_task_resume(self, task_id: str) -> None:
        if not self._read_approved():
            return
        try:
            run = self._state.start_resume(task_id)
        except Exception as exc:
            self._send_state_error(exc)
            return
        self._send_json(202, run)

    def _read_approved(self) -> bool:
        payload = self._read_json()
        if payload is None:
            return False
        if payload.get("approved") is not True:
            self._send_json(400, {"error": "explicit approval required"})
            return False
        return True

    def _send_state_error(self, exc: Exception) -> None:
        if isinstance(exc, ControlNotFoundError):
            self._send_json(404, {"error": str(exc)})
            return
        if isinstance(exc, ControlBusyError):
            self._send_json(409, {"error": str(exc)})
            return
        if isinstance(exc, ControlUnavailableError):
            self._send_json(503, {"error": str(exc)})
            return
        if isinstance(exc, ControlValidationError):
            self._send_json(400, {"error": str(exc)})
            return
        log.warning(
            "control-plane request failed: %s", exc, extra={"component": "control"}
        )
        self._send_json(500, {"error": "control request failed"})

    def _read_body(self) -> bool:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._body = b""
            return True
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return False
        if length < 0:
            self._send_json(400, {"error": "invalid Content-Length"})
            return False
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return False
        self._body = self.rfile.read(length) if length else b""
        return True

    def _read_json(self) -> dict[str, Any] | None:
        if not self._body:
            self._send_json(400, {"error": "JSON body required"})
            return None
        try:
            parsed = json.loads(self._body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(parsed, dict):
            self._send_json(400, {"error": "JSON object required"})
            return None
        return parsed

    def _valid_host(self) -> bool:
        # DNS-rebinding defense: only answer requests addressed to this
        # loopback server, not an attacker-controlled name resolving to it.
        port = int(cast(_ControlHTTPServer, self.server).server_address[1])
        supplied = self.headers.get("Host", "")
        allowed = {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        }
        if port == 80:
            allowed.update({"127.0.0.1", "localhost", "[::1]"})
        if supplied in allowed:
            return True
        self._send_json(403, {"error": "forbidden host"})
        return False

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-GreatSage-Token")
        if supplied is None or not secrets.compare_digest(supplied, self._state.token):
            self._send_json(403, {"error": "forbidden"})
            return False
        return True

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            )
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug(
            "control http %s - %s",
            self.address_string(),
            format % args,
            extra={"component": "control"},
        )


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
