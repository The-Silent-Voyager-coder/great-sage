"""Static plan verification (Phase 7).

``verify_plan`` checks a plan *before* execution without running any tool:
structure, known tool ids, risk flags that require human approval,
dependency validity, and bounded limits. Pure function, no I/O, so the
CLI ``planning verify`` and ``task run --verify-only`` paths share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from greatsage.planning import graph as task_graph
from greatsage.planning.limits import MAX_PLAN_STEPS_CEILING


#: Tool ids the secure registry can execute (default set + voice/vision
#: toolsets wired by ToolService.start). Plans referencing anything else
#: fail closed at verification time (and again in the executor).
def _known_tool_ids() -> frozenset[str]:
    from greatsage.tools.defaults import DEFAULT_TOOL_CLASSES
    from greatsage.vision.tools import VisionCaptureTool, VisionDescribeTool
    from greatsage.voice.tools import VoiceListenTool, VoiceSpeakTool, VoiceWakeTool

    classes = (
        *DEFAULT_TOOL_CLASSES,
        VoiceListenTool,
        VoiceSpeakTool,
        VoiceWakeTool,
        VisionCaptureTool,
        VisionDescribeTool,
    )
    ids: set[str] = set()
    for tool_class in classes:
        tool_id = getattr(tool_class, "id", None)
        if isinstance(tool_id, str) and tool_id:
            ids.add(tool_id)
    return frozenset(ids)


KNOWN_TOOLS = _known_tool_ids()

#: Risk estimates that always require explicit human approval.
APPROVAL_REQUIRED_RISKS = frozenset({"high", "critical"})

#: Risk estimates considered valid vocabulary.
VALID_RISKS = frozenset({"low", "medium", "high", "critical"})


@dataclass(frozen=True)
class VerificationReport:
    """Outcome of a static plan check."""

    plan_id: str
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    requires_approval: tuple[str, ...] = ()
    execution_order: tuple[str, ...] = ()
    max_level: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "requires_approval": list(self.requires_approval),
            "execution_order": list(self.execution_order),
            "max_level": self.max_level,
        }


def verify_plan(plan: Any, *, max_steps: int = MAX_PLAN_STEPS_CEILING) -> VerificationReport:
    """Statically verify ``plan`` and return a structured report.

    Never raises for content problems (they become ``errors``); only
    raises ``AttributeError``-style failures for a malformed plan object,
    which indicates a programming bug, not a user plan problem.
    """
    errors: list[str] = []
    warnings: list[str] = []
    requires_approval: list[str] = []
    order: list[str] = []
    max_level = 0

    plan_id = str(getattr(plan, "id", "unknown"))
    goal = str(getattr(plan, "goal", "") or "")
    if not goal.strip():
        errors.append("plan goal must not be empty")
    steps = list(getattr(plan, "steps", None) or [])
    if not steps:
        errors.append("plan must have at least one step")
        return VerificationReport(plan_id=plan_id, ok=False, errors=tuple(errors))
    if len(steps) > max_steps:
        errors.append(f"plan has {len(steps)} steps, limit is {max_steps}")

    seqs = [int(s.sequence) for s in steps]
    if seqs != list(range(1, len(seqs) + 1)):
        errors.append("step sequences must be 1..n contiguous")

    seen_ids: set[str] = set()
    for step in steps:
        sid = str(step.id)
        if sid in seen_ids:
            errors.append(f"duplicate step id: {sid}")
        seen_ids.add(sid)
        if not str(step.description or "").strip():
            errors.append(f"step {sid} has an empty description")
        if not str(step.tool_id or "").strip():
            errors.append(f"step {sid} has an empty tool_id")
        elif str(step.tool_id) not in KNOWN_TOOLS:
            errors.append(f"step {sid} references unknown tool: {step.tool_id}")
        risk = str(getattr(step, "risk_estimate", "low") or "low").lower()
        if risk not in VALID_RISKS:
            errors.append(f"step {sid} has invalid risk_estimate: {step.risk_estimate}")
        elif risk in APPROVAL_REQUIRED_RISKS:
            requires_approval.append(sid)
            warnings.append(
                f"step {sid} ({step.tool_id}) is {risk} risk and requires approval"
            )
        if not str(getattr(step, "acceptance_criteria", "") or "").strip():
            warnings.append(f"step {sid} has no acceptance criteria")

    missing = task_graph.find_missing_deps(steps)
    for sid, dep in missing:
        errors.append(f"step {sid} depends on unknown step {dep}")
    for sid in task_graph.find_self_deps(steps):
        errors.append(f"step {sid} depends on itself")
    if not missing:
        cycle = task_graph.find_cycle(steps)
        if cycle is not None:
            errors.append("dependency cycle: " + " -> ".join(cycle))

    if not errors:
        try:
            ordered = task_graph.topological_order(steps)
            order = [str(s.id) for s in ordered]
            depths = task_graph.levels(steps)
            max_level = max(depths.values(), default=0)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"graph ordering failed: {exc}")

    return VerificationReport(
        plan_id=plan_id,
        ok=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        requires_approval=tuple(requires_approval),
        execution_order=tuple(order),
        max_level=max_level,
    )
