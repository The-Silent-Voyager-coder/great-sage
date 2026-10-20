"""Control-plane HTTP tests (loopback, token-gated, broker-backed)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest

from greatsage.control.server import ControlServer
from greatsage.control.state import ControlValidationError
from greatsage.tools.models import ApprovalOutcome, ToolRequest, ToolRisk


class _FakeTool:
    name = "Execute a shell command"
    risk_level = ToolRisk.HIGH


class _FakeResult:
    def __init__(self, state: str) -> None:
        self._state = state

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": "task_control_1",
            "state": self._state,
            "provider": "fake",
            "model": "fake-model",
            "tool_calls": 1,
            "duration_ms": 1.0,
            "final_text": "finished" if self._state == "completed" else None,
            "error": None if self._state == "completed" else "approval denied",
        }


class _FakeAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cancelled = False

    def health(self) -> dict[str, Any]:
        return {
            "available": True,
            "status": "healthy",
            "enabled": True,
            "detail": "",
            "current": {},
        }

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        max_steps: int | None = None,
        approval: Any = None,
    ) -> _FakeResult:
        self.calls.append(
            {
                "prompt": prompt,
                "session_id": session_id,
                "max_steps": max_steps,
            }
        )
        request = ToolRequest(
            request_id="req_control_1",
            tool_id="shell.execute",
            arguments={"command": ["python", "--version"]},
            source="ai",
            session_id=session_id,
        )
        outcome = approval.request_approval(request, _FakeTool(), "high risk")
        return _FakeResult(
            "completed" if outcome is ApprovalOutcome.APPROVED else "failed"
        )

    def cancel(self) -> None:
        self.cancelled = True


class _FakeRuntime:
    def __init__(self) -> None:
        self.agent = _FakeAgent()
        self.config = SimpleNamespace(
            agent=SimpleNamespace(max_wall_time_seconds=10.0)
        )

    def health_report(self) -> list[Any]:
        return []

    def overall_health(self) -> Any:
        return SimpleNamespace(value="healthy")


@pytest.fixture()
def control_server() -> Any:
    runtime = _FakeRuntime()
    server = ControlServer(runtime, host="127.0.0.1", port=0, token="test-token")
    thread = threading.Thread(target=server.serve_forever, name="control-test", daemon=True)
    thread.start()
    try:
        yield server, runtime
    finally:
        server.stop()
        thread.join(timeout=2)


def _request(
    server: ControlServer,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = "test-token",
    host: str | None = None,
) -> tuple[int, bytes]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-GreatSage-Token"] = token
    if host is not None:
        headers["Host"] = host
    request = urllib.request.Request(
        server.url.rstrip("/") + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def test_control_page_and_token_gate(control_server: Any) -> None:
    server, _ = control_server
    status, body = _request(server, "GET", "/", token=None)
    assert status == 200
    assert b"greatsage-token" in body
    assert b"test-token" in body

    status, _ = _request(server, "GET", "/api/state", token=None)
    assert status == 403
    status, _ = _request(
        server, "GET", "/", token=None, host="attacker.example"
    )
    assert status == 403

    status, body = _request(server, "GET", "/api/state")
    assert status == 200
    state = json.loads(body)
    assert state["status"]["overall"] == "HEALTHY"
    assert state["run"]["active"] is False


def test_agent_run_pauses_then_approves(control_server: Any) -> None:
    server, runtime = control_server
    status, body = _request(
        server,
        "POST",
        "/api/agent",
        payload={"prompt": "inspect the workspace", "session_id": "s1"},
    )
    assert status == 202
    assert json.loads(body)["active"] is True

    pending = _wait_for(
        lambda: json.loads(_request(server, "GET", "/api/state")[1])["run"][
            "approvals"
        ]
    )
    assert pending[0]["request_id"] == "req_control_1"
    assert pending[0]["risk_level"] == "high"

    status, _ = _request(
        server, "POST", "/api/approvals/req_control_1/approve", payload={}
    )
    assert status == 200
    result = _wait_for(
        lambda: json.loads(_request(server, "GET", "/api/state")[1])["run"]["result"]
    )
    assert result["state"] == "completed"
    assert runtime.agent.calls[0]["session_id"] == "s1"
    stale, _ = _request(
        server, "POST", "/api/approvals/req_control_1/approve", payload={}
    )
    assert stale == 404


def test_agent_run_rejects_approval(control_server: Any) -> None:
    server, _ = control_server
    status, _ = _request(
        server,
        "POST",
        "/api/agent",
        payload={"prompt": "try a denied action"},
    )
    assert status == 202
    _wait_for(
        lambda: json.loads(_request(server, "GET", "/api/state")[1])["run"][
            "approvals"
        ]
    )
    status, _ = _request(
        server, "POST", "/api/approvals/req_control_1/reject", payload={}
    )
    assert status == 200
    result = _wait_for(
        lambda: json.loads(_request(server, "GET", "/api/state")[1])["run"]["result"]
    )
    assert result["state"] == "failed"


def test_second_run_is_rejected_while_active(control_server: Any) -> None:
    server, _ = control_server
    first, _ = _request(
        server, "POST", "/api/agent", payload={"prompt": "first"}
    )
    assert first == 202
    _wait_for(
        lambda: json.loads(_request(server, "GET", "/api/state")[1])["run"][
            "approvals"
        ]
    )
    second, body = _request(
        server, "POST", "/api/agent", payload={"prompt": "second"}
    )
    assert second == 409
    assert b"already active" in body
    _request(server, "POST", "/api/approvals/req_control_1/approve", payload={})


def test_control_rejects_non_loopback_and_bad_prompt(control_server: Any) -> None:
    server, _ = control_server
    with pytest.raises(ControlValidationError):
        ControlServer(_FakeRuntime(), host="0.0.0.0", port=0)
    status, _ = _request(server, "POST", "/api/agent", payload={"prompt": ""})
    assert status == 400
    status, _ = _request(
        server, "POST", "/api/agent", payload={"prompt": "x", "max_steps": 99}
    )
    assert status == 400
    status, _ = _request(
        server, "POST", "/api/agent", payload={"prompt": "x"}, token="wrong"
    )
    assert status == 403
