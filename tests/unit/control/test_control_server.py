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
from greatsage.exceptions import PlanningValidationError, TaskValidationError
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
            "failed_tool_calls": 0 if self._state == "completed" else 1,
            "duration_ms": 1.0,
            "final_text": "finished" if self._state == "completed" else None,
            "error": None if self._state == "completed" else "approval denied",
        }


class _FakeAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cancelled = False
        self.history_runs: list[dict[str, Any]] = [
            {
                "task_id": "task_control_0",
                "session_id": "s0",
                "state": "completed",
                "source": "control",
                "steps": 2,
                "tool_calls": 1,
                "failed_tool_calls": 1,
                "duration_ms": 12.0,
                "provider": "fake",
                "model": "fake-model",
                "error": None,
                "reason": None,
                "content_length": 8,
                "started_at": "2026-10-21T00:00:00+00:00",
                "finished_at": "2026-10-21T00:00:01+00:00",
            }
        ]

    def health(self) -> dict[str, Any]:
        return {
            "available": True,
            "status": "healthy",
            "enabled": True,
            "detail": "",
            "current": {},
            "history": {"available": True, "runs": 1},
        }

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.history_runs[:limit]

    def history_get(self, task_id: str) -> dict[str, Any] | None:
        return next(
            (run for run in self.history_runs if run["task_id"] == task_id),
            None,
        )

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        max_steps: int | None = None,
        approval: Any = None,
        source: str = "agent",
    ) -> _FakeResult:
        self.calls.append(
            {
                "prompt": prompt,
                "session_id": session_id,
                "max_steps": max_steps,
                "source": source,
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


class _FakePlanning:
    def __init__(self) -> None:
        self.plans: dict[str, dict[str, Any]] = {
            "plan_ready": {
                "id": "plan_ready",
                "goal": "inspect the workspace",
                "status": "ready",
                "workspace_root": "C:/GREATSAGE/workspaces/demo",
                "created_at": "2026-10-21T00:00:00+00:00",
                "updated_at": "2026-10-21T00:00:00+00:00",
                "steps": [
                    {
                        "id": "step_1",
                        "sequence": 1,
                        "description": "list files",
                        "tool_id": "filesystem.list",
                        "arguments": {"path": "C:/GREATSAGE/workspaces/demo"},
                        "risk_estimate": "low",
                        "acceptance_criteria": "listing returned",
                        "depends_on": [],
                    }
                ],
            },
            "plan_draft": {
                "id": "plan_draft",
                "goal": "draft plan",
                "status": "draft",
                "workspace_root": None,
                "created_at": "2026-10-21T00:00:00+00:00",
                "updated_at": "2026-10-21T00:00:00+00:00",
                "steps": [],
            },
        }
        self.approved: list[str] = []

    def list(self) -> list[dict[str, Any]]:
        return list(self.plans.values())

    def get(self, plan_id: str) -> dict[str, Any] | None:
        return self.plans.get(plan_id)

    def approve(self, plan_id: str) -> dict[str, Any]:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise PlanningValidationError(f"plan not found: {plan_id}")
        if plan["status"] != "draft":
            raise PlanningValidationError("only draft plans can be approved")
        approved = dict(plan, status="ready")
        self.plans[plan_id] = approved
        self.approved.append(plan_id)
        return approved

    def health(self) -> dict[str, Any]:
        return {"available": True, "status": "healthy", "detail": ""}


class _FakeTaskReport:
    def __init__(self, task_id: str, plan_id: str) -> None:
        self.task_id = task_id
        self.plan_id = plan_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "state": "completed",
            "goal": "inspect the workspace",
            "total_steps": 1,
            "completed_steps": 1,
            "failed_steps": 0,
            "duration_ms": 1.0,
            "step_results": [
                {
                    "step_id": "step_1",
                    "sequence": 1,
                    "tool_id": "filesystem.list",
                    "success": True,
                    "output": {"secret": "sk-ABCDEFGHIJKLMNOP"},
                    "error": None,
                    "duration_ms": 0.5,
                    "artifacts": [],
                }
            ],
            "artifacts": [],
            "summary": "task completed: 1/1 steps completed",
        }


class _FakeTask:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.tasks: dict[str, dict[str, Any]] = {
            "task_1": {
                "id": "task_1",
                "plan_id": "plan_ready",
                "goal": "inspect the workspace",
                "state": "failed",
                "current_step": 1,
                "total_steps": 1,
                "completed_steps": 0,
                "failed_steps": 1,
                "created_at": "2026-10-21T00:00:00+00:00",
                "updated_at": "2026-10-21T00:00:00+00:00",
                "completed_at": None,
                "error": "step failed",
                "step_results": [],
            },
            "task_2": {
                "id": "task_2",
                "plan_id": "plan_ready",
                "goal": "retry the workspace read",
                "state": "failed",
                "current_step": 1,
                "total_steps": 1,
                "completed_steps": 0,
                "failed_steps": 1,
                "created_at": "2026-10-21T00:00:00+00:00",
                "updated_at": "2026-10-21T00:00:00+00:00",
                "completed_at": None,
                "error": "step failed",
                "step_results": [],
            },
        }

    def run(
        self, plan: str, task_id: str | None = None, *, approved: bool = False
    ) -> _FakeTaskReport:
        task_id = task_id or "task_default"
        self.calls.append({"plan": plan, "task_id": task_id, "approved": approved})
        self.tasks[task_id] = {
            "id": task_id,
            "plan_id": plan,
            "goal": "inspect the workspace",
            "state": "completed",
            "current_step": 1,
            "total_steps": 1,
            "completed_steps": 1,
            "failed_steps": 0,
            "created_at": "2026-10-21T00:00:00+00:00",
            "updated_at": "2026-10-21T00:00:00+00:00",
            "completed_at": "2026-10-21T00:00:01+00:00",
            "error": None,
            "step_results": [],
        }
        return _FakeTaskReport(task_id, plan)

    def resume(self, task_id: str, *, approved: bool = False) -> _FakeTaskReport:
        if task_id not in self.tasks:
            raise TaskValidationError(f"unknown task: {task_id}")
        self.resume_calls.append({"task_id": task_id, "approved": approved})
        return _FakeTaskReport(task_id, self.tasks[task_id]["plan_id"])

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.tasks.values())[:limit]

    def get(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskValidationError(f"unknown task: {task_id}")
        return task

    def cancel(self, task_id: str) -> dict[str, Any]:
        if task_id not in self.tasks:
            raise TaskValidationError(f"unknown task: {task_id}")
        self.cancelled.append(task_id)
        return {"task_id": task_id, "cancelling": True}

    def health(self) -> dict[str, Any]:
        return {"available": True, "status": "healthy", "detail": ""}


class _FakeRuntime:
    def __init__(self) -> None:
        self.agent = _FakeAgent()
        self.planning = _FakePlanning()
        self.task = _FakeTask()
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
    assert b"Plans" in body
    assert b"Tasks" in body
    assert b"Recent agent runs" in body

    status, _ = _request(server, "GET", "/api/state", token=None)
    assert status == 403
    status, _ = _request(server, "GET", "/api/plans", token=None)
    assert status == 403
    status, _ = _request(server, "GET", "/api/runs", token=None)
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
    assert {plan["id"] for plan in state["plans"]} == {"plan_ready", "plan_draft"}
    assert state["tasks"][0]["id"] == "task_1"
    assert state["runs"][0]["task_id"] == "task_control_0"
    assert state["planning"]["available"] is True
    assert state["task"]["available"] is True


def test_agent_run_history_views(control_server: Any) -> None:
    server, _ = control_server
    status, body = _request(server, "GET", "/api/runs")
    assert status == 200
    runs = json.loads(body)["runs"]
    assert runs[0]["task_id"] == "task_control_0"
    assert runs[0]["source"] == "control"
    assert runs[0]["failed_tool_calls"] == 1
    assert "final_text" not in runs[0]

    status, body = _request(server, "GET", "/api/runs/task_control_0")
    assert status == 200
    run = json.loads(body)["run"]
    assert run["state"] == "completed"
    assert run["content_length"] == 8
    assert "prompt" not in run

    status, _ = _request(server, "GET", "/api/runs/missing")
    assert status == 404


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


def test_plan_and_task_read_views(control_server: Any) -> None:
    server, _ = control_server
    status, body = _request(server, "GET", "/api/plans")
    assert status == 200
    plans = json.loads(body)["plans"]
    assert {item["id"] for item in plans} == {"plan_ready", "plan_draft"}

    status, body = _request(server, "GET", "/api/plans/plan_ready")
    assert status == 200
    plan = json.loads(body)["plan"]
    assert plan["status"] == "ready"
    assert plan["steps"][0]["tool_id"] == "filesystem.list"
    assert "arguments_preview" in plan["steps"][0]

    status, body = _request(server, "GET", "/api/tasks")
    assert status == 200
    assert json.loads(body)["tasks"][0]["id"] == "task_1"

    status, body = _request(server, "GET", "/api/tasks/task_1")
    assert status == 200
    task = json.loads(body)["task"]
    assert task["state"] == "failed"
    assert task["error"] == "step failed"

    status, _ = _request(server, "GET", "/api/plans/missing")
    assert status == 404
    status, _ = _request(server, "GET", "/api/tasks/missing")
    assert status == 404


def test_ready_plan_requires_explicit_approval_and_runs_through_task_service(
    control_server: Any,
) -> None:
    server, runtime = control_server
    status, _ = _request(
        server,
        "POST",
        "/api/plans/plan_ready/run",
        payload={},
    )
    assert status == 400

    status, _ = _request(
        server,
        "POST",
        "/api/plans/plan_draft/run",
        payload={"approved": True},
    )
    assert status == 400

    status, body = _request(
        server,
        "POST",
        "/api/plans/plan_ready/run",
        payload={"approved": True},
    )
    assert status == 202
    run = json.loads(body)
    assert run["kind"] == "task"
    assert run["plan_id"] == "plan_ready"
    assert run["task_id"].startswith("task_control_")

    result = _wait_for(
        lambda: json.loads(_request(server, "GET", "/api/state")[1])["run"]["result"]
    )
    assert result["state"] == "completed"
    assert result["plan_id"] == "plan_ready"
    assert "output" not in result["step_results"][0]
    assert runtime.task.calls[0]["plan"] == "plan_ready"
    assert runtime.task.calls[0]["approved"] is True


def test_draft_plan_can_be_approved_then_has_ready_status(
    control_server: Any,
) -> None:
    server, runtime = control_server
    status, _ = _request(
        server,
        "POST",
        "/api/plans/plan_draft/approve",
        payload={},
    )
    assert status == 400

    status, body = _request(
        server,
        "POST",
        "/api/plans/plan_draft/approve",
        payload={"approved": True},
    )
    assert status == 200
    assert json.loads(body)["plan"]["status"] == "ready"
    assert runtime.planning.approved == ["plan_draft"]


def test_task_cancel_and_resume_use_task_service(control_server: Any) -> None:
    server, runtime = control_server
    status, _ = _request(
        server, "POST", "/api/tasks/task_1/cancel", payload={}
    )
    assert status == 200
    assert runtime.task.cancelled == ["task_1"]

    status, _ = _request(
        server,
        "POST",
        "/api/tasks/task_2/resume",
        payload={},
    )
    assert status == 400

    status, body = _request(
        server,
        "POST",
        "/api/tasks/task_2/resume",
        payload={"approved": True},
    )
    assert status == 202
    assert json.loads(body)["kind"] == "task"
    _wait_for(lambda: runtime.task.resume_calls)
    assert runtime.task.resume_calls[0] == {
        "task_id": "task_2",
        "approved": True,
    }


def test_plan_run_reports_unavailable_task_service(control_server: Any) -> None:
    server, runtime = control_server
    runtime.task.health = lambda: {  # type: ignore[method-assign]
        "available": False,
        "status": "unavailable",
        "detail": "task subsystem disabled",
    }
    status, body = _request(
        server,
        "POST",
        "/api/plans/plan_ready/run",
        payload={"approved": True},
    )
    assert status == 503
    assert b"task subsystem disabled" in body


def test_agent_and_task_share_one_active_execution_slot(control_server: Any) -> None:
    server, _ = control_server
    status, _ = _request(
        server, "POST", "/api/agent", payload={"prompt": "hold the slot"}
    )
    assert status == 202
    _wait_for(
        lambda: json.loads(_request(server, "GET", "/api/state")[1])["run"][
            "approvals"
        ]
    )
    status, body = _request(
        server,
        "POST",
        "/api/plans/plan_ready/run",
        payload={"approved": True},
    )
    assert status == 409
    assert b"already active" in body
    _request(server, "POST", "/api/approvals/req_control_1/approve", payload={})
