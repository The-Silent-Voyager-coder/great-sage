"""Bounded, redacted views for control-plane plan/task payloads.

The dashboard is a presentation surface, not a data export. Plan arguments
and task errors can contain paths or pasted credentials, so these views keep
only the fields the operator needs to make a decision, redact secret-shaped
material, and cap every free-text field.
"""

from __future__ import annotations

import json
from typing import Any

from greatsage.tools.redaction import redact_secrets

MAX_GOAL = 400
MAX_DESCRIPTION = 400
MAX_ARGUMENTS = 600
MAX_ERROR = 1000
MAX_SUMMARY = 1000
MAX_ARTIFACTS = 20


def safe_text(value: Any, limit: int) -> str:
    """Redact and truncate a free-text value for display."""
    text = redact_secrets("" if value is None else str(value)) or ""
    if len(text) > limit:
        return text[:limit] + "... [truncated]"
    return text


def _optional_text(value: Any, limit: int) -> str | None:
    text = safe_text(value, limit)
    return text or None


def _arguments_preview(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(value)
    return safe_text(raw, MAX_ARGUMENTS)


def _artifacts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [safe_text(item, MAX_ARGUMENTS) for item in value[:MAX_ARTIFACTS]]


def _step_view(step: dict[str, Any]) -> dict[str, Any]:
    raw_deps = step.get("depends_on")
    depends_on = [str(item) for item in raw_deps] if isinstance(raw_deps, list) else []
    return {
        "id": str(step.get("id", "")),
        "sequence": step.get("sequence"),
        "description": safe_text(step.get("description", ""), MAX_DESCRIPTION),
        "tool_id": str(step.get("tool_id", "")),
        "risk_estimate": str(step.get("risk_estimate", "")),
        "acceptance_criteria": safe_text(
            step.get("acceptance_criteria", ""), MAX_DESCRIPTION
        ),
        "rollback_hint": _optional_text(step.get("rollback_hint"), MAX_DESCRIPTION),
        "depends_on": depends_on,
        "arguments_preview": _arguments_preview(step.get("arguments")),
    }


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    steps = plan.get("steps")
    return {
        "id": str(plan.get("id", "")),
        "goal": safe_text(plan.get("goal", ""), MAX_GOAL),
        "status": str(plan.get("status", "")),
        "step_count": len(steps) if isinstance(steps, list) else 0,
        "updated_at": plan.get("updated_at"),
        "workspace_root": _optional_text(plan.get("workspace_root"), MAX_GOAL),
    }


def plan_detail(plan: dict[str, Any]) -> dict[str, Any]:
    view = plan_summary(plan)
    steps = plan.get("steps")
    view.update(
        {
            "workspace_id": plan.get("workspace_id"),
            "created_at": plan.get("created_at"),
            "steps": [
                _step_view(step)
                for step in (steps if isinstance(steps, list) else [])
                if isinstance(step, dict)
            ],
        }
    )
    return view


def task_summary(task: dict[str, Any]) -> dict[str, Any]:
    task_id = task.get("id") or task.get("task_id")
    return {
        "id": str(task_id or ""),
        "task_id": str(task_id or ""),
        "plan_id": str(task.get("plan_id", "")),
        "goal": safe_text(task.get("goal", ""), MAX_GOAL),
        "state": str(task.get("state", "")),
        "current_step": task.get("current_step"),
        "total_steps": task.get("total_steps"),
        "completed_steps": task.get("completed_steps"),
        "failed_steps": task.get("failed_steps"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "completed_at": task.get("completed_at"),
        "error": _optional_text(task.get("error"), MAX_ERROR),
    }


def _step_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    results = data.get("step_results")
    if not isinstance(results, list):
        return []
    view: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        view.append(
            {
                "step_id": str(result.get("step_id", "")),
                "sequence": result.get("sequence"),
                "tool_id": str(result.get("tool_id", "")),
                "success": bool(result.get("success")),
                "duration_ms": result.get("duration_ms"),
                "error": _optional_text(result.get("error"), MAX_ERROR),
                "artifacts": _artifacts(result.get("artifacts")),
            }
        )
    return view


def task_detail(task: dict[str, Any]) -> dict[str, Any]:
    view = task_summary(task)
    results = _step_results(task)
    view["step_results"] = results
    if view["completed_steps"] is None:
        view["completed_steps"] = sum(1 for item in results if item["success"])
    if view["failed_steps"] is None:
        view["failed_steps"] = sum(1 for item in results if not item["success"])
    return view


def task_report(report: dict[str, Any]) -> dict[str, Any]:
    view = task_summary(report)
    view.update(
        {
            "duration_ms": report.get("duration_ms"),
            "summary": safe_text(report.get("summary", ""), MAX_SUMMARY),
            "artifacts": _artifacts(report.get("artifacts")),
            "step_results": _step_results(report),
        }
    )
    return view
