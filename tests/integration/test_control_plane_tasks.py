"""Control-plane task execution over the real runtime services (offline)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from greatsage.control.state import ControlState
from greatsage.core.runtime import Runtime


def _wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def test_control_state_runs_ready_plan_through_task_service(
    valid_config_yaml: Path,
) -> None:
    runtime = Runtime.create(str(valid_config_yaml))
    asyncio.run(runtime.start())
    try:
        plan = runtime.planning.create_plan("list files")
        plan_id = plan["id"]
        runtime.planning.approve(plan_id)

        state = ControlState(runtime, token="test-token")
        run = state.start_task(plan_id)
        assert run["kind"] == "task"
        assert run["plan_id"] == plan_id
        assert run["task_id"].startswith("task_control_")

        result = _wait_for(lambda: state.run_snapshot()["result"])
        assert result["state"] == "completed"
        assert result["plan_id"] == plan_id
        assert result["completed_steps"] == 1

        task = state.get_task(result["task_id"])
        assert task["state"] == "completed"
        assert task["step_results"][0]["success"] is True
        plan = state.get_plan(plan_id)
        assert plan["status"] == "completed"
        assert plan["task_id"] == result["task_id"]

        snapshot = state.snapshot()
        assert snapshot["run"]["result"]["state"] == "completed"
        assert snapshot["planning"]["available"] is True
        assert snapshot["task"]["available"] is True
        assert snapshot["status"]["overall"] in ("HEALTHY", "DEGRADED", "UNHEALTHY")

        from_thread: list[dict[str, object]] = []
        reader = threading.Thread(target=lambda: from_thread.append(state.snapshot()))
        reader.start()
        reader.join(timeout=5)
        assert not reader.is_alive()
        assert from_thread and from_thread[0]["tasks"]
    finally:
        asyncio.run(runtime.stop())
