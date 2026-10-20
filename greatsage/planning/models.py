"""Planning models (Phase 6).

Plans are deterministic, rule/template based — no AI provider calls.
Steps are serializable and stored in the task DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from greatsage.exceptions import PlanningValidationError
from greatsage.planning.limits import MAX_PLAN_STEPS_CEILING


class PlanStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Step:
    """A single plan step (Phase 6 spec)."""

    id: str = field(default_factory=lambda: f"step_{uuid.uuid4().hex[:8]}")
    sequence: int = 0
    description: str = ""
    tool_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    risk_estimate: str = "low"
    acceptance_criteria: str = ""
    rollback_hint: str | None = None
    # Phase 7 task-graph: ids of steps that must complete before this one.
    # Empty (default) means sequential execution in ``sequence`` order,
    # which keeps every Phase 6 linear plan valid without modification.
    depends_on: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.description.strip():
            raise PlanningValidationError("step description must not be empty")
        if not self.tool_id.strip():
            raise PlanningValidationError("step tool_id must not be empty")
        if self.sequence < 1:
            raise PlanningValidationError("step sequence must be >= 1")
        for dep in self.depends_on:
            if not str(dep).strip():
                raise PlanningValidationError("step depends_on entries must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "description": self.description,
            "tool_id": self.tool_id,
            "arguments": self.arguments,
            "risk_estimate": self.risk_estimate,
            "acceptance_criteria": self.acceptance_criteria,
            "rollback_hint": self.rollback_hint,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Step:
        raw_deps = data.get("depends_on", ())
        deps = tuple(str(d) for d in raw_deps) if isinstance(raw_deps, (list, tuple)) else ()
        return cls(
            id=str(data.get("id", f"step_{uuid.uuid4().hex[:8]}")),
            sequence=int(data.get("sequence", 0)),
            description=str(data.get("description", "")),
            tool_id=str(data.get("tool_id", "")),
            arguments=dict(data.get("arguments", {})),
            risk_estimate=str(data.get("risk_estimate", "low")),
            acceptance_criteria=str(data.get("acceptance_criteria", "")),
            rollback_hint=data.get("rollback_hint"),
            depends_on=deps,
        )


@dataclass(frozen=True)
class Plan:
    """Ordered steps to achieve a goal within a workspace."""

    id: str = field(default_factory=lambda: f"plan_{uuid.uuid4().hex}")
    goal: str = ""
    workspace_id: str | None = None
    workspace_root: Path | None = None
    steps: tuple[Step, ...] = ()
    status: PlanStatus = PlanStatus.DRAFT
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.goal.strip():
            raise PlanningValidationError("plan goal must not be empty")
        if not self.steps:
            raise PlanningValidationError("plan must have at least one step")
        if len(self.steps) > MAX_PLAN_STEPS_CEILING:
            raise PlanningValidationError("plan exceeds max steps")
        for step in self.steps:
            step.validate()
        # sequences must be 1..n
        seqs = [s.sequence for s in self.steps]
        if seqs != list(range(1, len(seqs) + 1)):
            raise PlanningValidationError("step sequences must be 1..n contiguous")
        # Phase 7 task-graph: deps must reference known steps, no self-deps, no cycles.
        known_ids = {s.id for s in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in known_ids:
                    raise PlanningValidationError(
                        f"step {step.id} depends on unknown step {dep}"
                    )
                if dep == step.id:
                    raise PlanningValidationError(f"step {step.id} depends on itself")
        from greatsage.planning.graph import find_cycle

        cycle = find_cycle(self.steps)
        if cycle is not None:
            raise PlanningValidationError("dependency cycle: " + " -> ".join(cycle))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "workspace_id": self.workspace_id,
            "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        steps = tuple(Step.from_dict(s) for s in data.get("steps", []))
        ws_root = data.get("workspace_root")
        return cls(
            id=str(data.get("id", f"plan_{uuid.uuid4().hex}")),
            goal=str(data.get("goal", "")),
            workspace_id=data.get("workspace_id"),
            workspace_root=Path(ws_root) if isinstance(ws_root, str) and ws_root else None,
            steps=steps,
            status=PlanStatus(str(data.get("status", "draft"))),
            created_at=datetime.fromisoformat(str(data["created_at"])) if data.get("created_at") else datetime.now(UTC),  # noqa: E501
            updated_at=datetime.fromisoformat(str(data["updated_at"])) if data.get("updated_at") else datetime.now(UTC),  # noqa: E501
            metadata=dict(data.get("metadata", {})),
        )
