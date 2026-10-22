"""Control-plane plan/task projection tests (redaction + omission)."""

from __future__ import annotations

from greatsage.control.views import (
    agent_run_detail,
    agent_run_summary,
    plan_detail,
    task_detail,
    task_report,
)

_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"


def test_plan_detail_redacts_arguments_and_bounds_text() -> None:
    plan = {
        "id": "plan_1",
        "goal": "g" * 5000,
        "status": "ready",
        "metadata": {"task_id": "task_1"},
        "steps": [
            {
                "id": "step_1",
                "sequence": 1,
                "description": "d" * 5000,
                "tool_id": "filesystem.write",
                "risk_estimate": "medium",
                "arguments": {"content": _SECRET, "path": "out.txt"},
                "depends_on": [],
            }
        ],
    }

    view = plan_detail(plan)

    assert len(view["goal"]) <= 420
    assert view["task_id"] == "task_1"
    assert len(view["steps"][0]["description"]) <= 420
    assert _SECRET not in view["steps"][0]["arguments_preview"]
    assert "out.txt" in view["steps"][0]["arguments_preview"]


def test_task_detail_omits_step_output_and_redacts_errors() -> None:
    task = {
        "id": "task_1",
        "plan_id": "plan_1",
        "goal": "inspect",
        "state": "failed",
        "error": _SECRET,
        "step_results": [
            {
                "step_id": "step_1",
                "sequence": 1,
                "tool_id": "filesystem.read",
                "success": True,
                "output": {"secret": _SECRET},
                "error": None,
                "duration_ms": 1.0,
                "artifacts": [],
            }
        ],
    }

    view = task_detail(task)

    assert view["error"] is not None
    assert _SECRET not in view["error"]
    assert "output" not in view["step_results"][0]
    assert _SECRET not in str(view)


def test_agent_run_view_omits_prompts_and_model_text_and_redacts_errors() -> None:
    run = {
        "task_id": "task_1",
        "session_id": "s1",
        "state": "failed",
        "source": "control",
        "steps": 2,
        "tool_calls": 1,
        "duration_ms": 12.0,
        "provider": "local",
        "model": "llama3.2:1b",
        "error": f"failed: {_SECRET}",
        "reason": "provider error",
        "content_length": 100,
        "prompt_length": 25,
        "tool_output_bytes": 10,
        "memory_references": 1,
        "final_text": _SECRET,
        "prompt": _SECRET,
        "started_at": "2026-10-22T00:00:00+00:00",
        "finished_at": "2026-10-22T00:00:01+00:00",
    }

    summary = agent_run_summary(run)
    detail = agent_run_detail(run)

    assert summary["task_id"] == "task_1"
    assert summary["source"] == "control"
    assert summary["content_length"] == 100
    assert "final_text" not in summary
    assert "prompt" not in summary
    assert detail["prompt_length"] == 25
    assert _SECRET not in str(summary)
    assert _SECRET not in str(detail)


def test_task_report_omits_step_output() -> None:
    report = {
        "task_id": "task_1",
        "plan_id": "plan_1",
        "state": "completed",
        "goal": "inspect",
        "total_steps": 1,
        "completed_steps": 1,
        "failed_steps": 0,
        "duration_ms": 1.0,
        "step_results": [
            {
                "step_id": "step_1",
                "sequence": 1,
                "tool_id": "filesystem.read",
                "success": True,
                "output": {"secret": _SECRET},
                "error": None,
                "duration_ms": 1.0,
                "artifacts": [],
            }
        ],
        "artifacts": [],
        "summary": "done",
    }

    view = task_report(report)

    assert view["task_id"] == "task_1"
    assert "output" not in view["step_results"][0]
    assert _SECRET not in str(view)
