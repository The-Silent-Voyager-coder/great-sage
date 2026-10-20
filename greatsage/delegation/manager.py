"""DelegationManager (Phase 5B): controlled, bounded executor delegation.

Great Sage owns the delegation lifecycle end to end: it validates the
request, chooses and gates the provider by capability, enforces the working
directory through Great Sage path security, routes every executor permission
request through the Great Sage SecurityPolicy and Phase 4 approval provider,
monitors the executor in bounded poll sweeps (paused by the poll interval,
with bounded reconnects, no infinite loops), enforces
limits (wall time, output bytes, permission count, session count, depth),
aborts and cleans up on cancellation/timeout/limits, and publishes the
Delegation* + TOOL_* audit events. The executor only carries out the work.

This module never imports the OpenCode adapter: it talks to any provider
through the DelegationProvider protocol, so the core stays provider-neutral.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from greatsage.configuration.model import SageConfig
from greatsage.delegation.limits import POLL_INTERVAL_SECONDS, RECONNECT_LIMIT
from greatsage.delegation.models import (
    DelegationEvent,
    DelegationEventKind,
    DelegationLimits,
    DelegationPermission,
    DelegationRequest,
    DelegationResult,
    DelegationRunStatus,
    DelegationState,
    DelegationTask,
    transition_state,
)
from greatsage.events.models import (
    DELEGATION_CANCELLED,
    DELEGATION_COMPLETED,
    DELEGATION_FAILED,
    DELEGATION_PERMISSION_REQUESTED,
    DELEGATION_PERMISSION_RESOLVED,
    DELEGATION_PROGRESS,
    DELEGATION_REQUESTED,
    DELEGATION_STARTED,
    DELEGATION_TIMED_OUT,
    TOOL_ALLOWED,
    TOOL_APPROVAL_REQUESTED,
    TOOL_APPROVED,
    TOOL_DENIED,
    TOOL_REJECTED,
    Event,
)
from greatsage.exceptions import (
    DelegationLimitError,
    DelegationValidationError,
    ProviderCapabilityError,
    ProviderUnavailableError,
)
from greatsage.intelligence.provider import Capability, ProviderState
from greatsage.intelligence.registry import ProviderRegistry
from greatsage.tools.models import (
    ApprovalOutcome,
    ToolCategory,
    ToolContext,
    ToolDecision,
    ToolRequest,
    ToolResult,
    ToolRisk,
)
from greatsage.tools.pathsecurity import canonicalize, is_protected_path, is_within
from greatsage.tools.policy import SecurityPolicy
from greatsage.tools.redaction import redact_secrets

log = logging.getLogger("greatsage.delegation.manager")

_SUMMARY_MAX = 4000
_PROGRESS_EVENT_MAX = 200
_RESULTS_KEEP = 25
_ERROR_MAX = 300

_PERMISSION_CATEGORY: dict[str, ToolCategory] = {
    "read": ToolCategory.FILESYSTEM,
    "edit": ToolCategory.FILESYSTEM,
    "write": ToolCategory.FILESYSTEM,
    "bash": ToolCategory.SHELL,
    "webfetch": ToolCategory.NETWORK,
    "websearch": ToolCategory.NETWORK,
    "task": ToolCategory.SYSTEM,
    "skill": ToolCategory.SYSTEM,
    "project": ToolCategory.SYSTEM,
    "external_directory": ToolCategory.FILESYSTEM,
    "question": ToolCategory.SYSTEM,
}

_PERMISSION_RISK: dict[str, ToolRisk] = {
    "read": ToolRisk.SAFE,
    "edit": ToolRisk.MEDIUM,
    "write": ToolRisk.MEDIUM,
    "bash": ToolRisk.HIGH,
    "webfetch": ToolRisk.MEDIUM,
    "websearch": ToolRisk.MEDIUM,
    "task": ToolRisk.HIGH,
    "skill": ToolRisk.MEDIUM,
    "project": ToolRisk.MEDIUM,
    "external_directory": ToolRisk.MEDIUM,
    "question": ToolRisk.MEDIUM,
}

_UNKNOWN_PERMISSION_CATEGORY = ToolCategory.SYSTEM
_UNKNOWN_PERMISSION_RISK = ToolRisk.HIGH  # unknown permissions are never auto-allowed

_EMPTY_SCHEMA: dict[str, object] = {"type": "object", "properties": {}, "required": []}


@runtime_checkable
class DelegationProvider(Protocol):
    """The executor surface the manager delegates to (spec §3).

    Implemented by providers that advertise Capability.DELEGATION (e.g. the
    OpenCode adapter). Wire formats stay behind this protocol.
    """

    def provider_id(self) -> str: ...

    def create_session(self) -> str: ...

    def send_delegation_prompt(
        self,
        session_id: str,
        prompt: str,
        working_directory: str | None = None,
    ) -> None: ...

    def iter_session_events(self, session_id: str) -> Iterator[DelegationEvent]:
        """One bounded sweep of executor events for a session.

        Poll-based providers return after a single bounded sweep (yielding
        zero or more events; a heartbeat NOTE is allowed while quiet).
        Stream-based providers read one bounded window of their stream.
        Transport failures raise so the manager can apply its bounded
        reconnect policy.
        """
        ...

    def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        approved: bool,
        remember: bool | None = None,
    ) -> None: ...

    def abort_session(self, session_id: str) -> None: ...

    def get_session_diff(self, session_id: str) -> dict[str, Any] | None: ...

    def dispose_session(self, session_id: str) -> None: ...


@dataclass
class _SyntheticTool:
    """A minimal Tool-shaped object for Great Sage SecurityPolicy evaluation."""

    id: str
    name: str
    description: str
    version: str
    risk_level: ToolRisk
    category: ToolCategory
    capabilities: tuple[str, ...] = ()
    input_schema: dict[str, object] = field(default_factory=lambda: dict(_EMPTY_SCHEMA))
    output_schema: dict[str, object] = field(default_factory=lambda: dict(_EMPTY_SCHEMA))
    PATH_ARGUMENTS: tuple[str, ...] = ("path",)

    def execute(  # pragma: no cover
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        raise NotImplementedError("synthetic tools are policy-only, never executed")


@dataclass
class _RunningDelegation:
    """Mutable record of an in-flight delegation for cancel/observe."""

    task: DelegationTask
    status: DelegationRunStatus
    provider: DelegationProvider
    token: threading.Event
    provider_session_id: str


class DelegationManager:
    """Owns the delegation lifecycle; the only delegator in the system."""

    def __init__(
        self,
        *,
        config: SageConfig,
        registry: ProviderRegistry,
        policy: SecurityPolicy | None = None,
        publisher: Any = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._policy = policy or SecurityPolicy.from_config(config)
        self.publisher = publisher
        self.approval: Any = None  # Phase 4 ApprovalProvider; synced by the service
        #: Pause between executor event sweeps (seconds). Tests may set 0.0.
        self.poll_interval_seconds: float = POLL_INTERVAL_SECONDS
        self._running: dict[str, _RunningDelegation] = {}
        self._results: dict[str, DelegationResult] = {}
        self._lock = threading.RLock()

    # --- public API ----------------------------------------------------

    def delegate(self, request: DelegationRequest) -> DelegationResult:
        """Run one controlled delegation to a terminal state (blocks).

        Great Sage-enforced limits, permission routing, path security,
        cancellation, timeout, and cleanup cover the whole run.
        """
        request.validate()
        limits = request.limits or self._effective_limits()
        limits.validate()
        self._check_depth(request, limits)
        provider = self._select_provider(request)
        working = self._authorize_working_directory(request)
        task = DelegationTask(
            task_id=request.task_id if request.task_id else request.request_id,
            request_id=request.request_id,
            session_id=request.session_id,
            prompt=request.prompt,
            working_directory=working,
            provider=provider.provider_id(),
            depth=request.depth,
            limits=limits,
            requested_capabilities=request.requested_capabilities,
        )
        return self._run(task, provider, limits)

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Request cancellation of a running delegation (best-effort).

        Marks the task for cancellation and aborts the executor session so
        the monitor loop terminates promptly. The run thread finalizes the
        CANCELLED state; an abort failure is represented accurately.
        """
        with self._lock:
            record = self._running.get(task_id)
        if record is None:
            result = self._results.get(task_id)
            if result is not None and result.state.is_terminal:
                raise DelegationValidationError(
                    f"delegation task {task_id} already finished "
                    f"({result.state.value})"
                )
            raise DelegationValidationError(f"unknown delegation task: {task_id}")
        record.token.set()
        session_id = record.provider_session_id or (record.task.session_id or "")
        try:
            record.provider.abort_session(session_id)
        except Exception as exc:  # abort is best-effort; state stays accurate
            log.debug("delegation abort failed for %s: %s", task_id, exc)
        log.info(
            "delegation cancellation requested",
            extra={
                "component": "delegation",
                "task_id": task_id,
                "state": record.status.state.value,
            },
        )
        return {
            "task_id": task_id,
            "state": record.status.state.value,
            "cancelling": True,
        }

    def get(self, task_id: str) -> dict[str, Any]:
        """Snapshot of a running or finished task (ids/reasons only)."""
        with self._lock:
            record = self._running.get(task_id)
            result = self._results.get(task_id)
        if record is not None:
            return record.status.to_dict()
        if result is not None:
            return result.to_dict()
        raise DelegationValidationError(f"unknown delegation task: {task_id}")

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        """Running tasks first, then recent results (newest first)."""
        with self._lock:
            running = [record.status.to_dict() for record in self._running.values()]
            completed = [result.to_dict() for result in reversed(tuple(self._results.values()))]
        return (running + completed)[: max(0, limit)]

    def health(self) -> dict[str, Any]:
        default_provider = self._config.delegation.default_provider
        provider_state: str | None = None
        provider_ready = False
        try:
            if self._registry.has(default_provider):
                provider = self._registry.get(default_provider)
                provider_state = provider.state.value
                provider_ready = (
                    provider.state is ProviderState.READY
                    and provider.capabilities().supports(Capability.DELEGATION)
                )
        except Exception as exc:  # registry access must never fail health
            log.debug("delegation health provider lookup failed: %s", exc)
        with self._lock:
            active = len(self._running)
        return {
            "enabled": self._config.delegation.enabled,
            "default_provider": default_provider,
            "provider_state": provider_state,
            "provider_ready": provider_ready,
            "active_tasks": active,
            "limits": {
                "max_wall_time_seconds": self._config.delegation.max_wall_time_seconds,
                "max_output_bytes": self._config.delegation.max_output_bytes,
                "max_permission_requests": (
                    self._config.delegation.max_permission_requests
                ),
                "max_session_count": self._config.delegation.max_session_count,
                "max_delegation_depth": self._config.delegation.max_delegation_depth,
            },
        }

    # --- internals: setup ----------------------------------------------

    def _effective_limits(self) -> DelegationLimits:
        delegation = self._config.delegation
        return DelegationLimits(
            max_wall_time_seconds=float(delegation.max_wall_time_seconds),
            max_output_bytes=int(delegation.max_output_bytes),
            max_permission_requests=int(delegation.max_permission_requests),
            max_session_count=int(delegation.max_session_count),
            max_delegation_depth=int(delegation.max_delegation_depth),
        )

    def _check_depth(self, request: DelegationRequest, limits: DelegationLimits) -> None:
        # Depth 0 is the top-level call; any request at depth >= the configured
        # limit is recursion and is rejected outright (never unlimited).
        if request.depth >= limits.max_delegation_depth:
            raise DelegationValidationError(
                f"delegation depth {request.depth} exceeds the configured maximum "
                f"{limits.max_delegation_depth}; recursive delegation is not allowed"
            )

    def _select_provider(self, request: DelegationRequest) -> DelegationProvider:
        provider_id = request.provider or self._config.delegation.default_provider
        if not self._registry.has(provider_id):
            raise DelegationValidationError(
                f"delegation provider {provider_id!r} is not registered "
                f"(available: {', '.join(self._registry.ids()) or 'none'})"
            )
        provider = self._registry.get(provider_id)
        if provider.state is not ProviderState.READY:
            raise ProviderUnavailableError(
                f"delegation provider {provider_id!r} is {provider.state.value}"
            )
        if not provider.capabilities().supports(Capability.DELEGATION):
            raise ProviderCapabilityError(
                f"provider {provider_id!r} does not support delegation; "
                "no silent fallback to a non-delegating provider"
            )
        return provider  # type: ignore[return-value]  # runtime-checked by DelegationProvider

    def _authorize_working_directory(self, request: DelegationRequest) -> Any:
        base = self._config.tools.working_directory
        canonical = canonicalize(str(request.working_directory), base)
        denied_roots = tuple(self._config.tools.denied_roots)
        allowed_roots = tuple(self._config.tools.allowed_roots)
        for root in denied_roots:
            if is_within(canonical, root):
                raise DelegationValidationError(
                    f"working directory {canonical} is inside a denied root: {root}"
                )
        if is_protected_path(canonical):
            raise DelegationValidationError(
                f"working directory {canonical} targets a protected path"
            )
        if is_within(canonical, base):
            return canonical  # the configured Great Sage workspace — allowed
        if any(is_within(canonical, root) for root in allowed_roots):
            return canonical  # inside an explicit allowed root — allowed
        # Outside approved roots: explicit human permission is required.
        if self._ask_directory_approval(request, canonical):
            return canonical
        raise DelegationValidationError(
            f"working directory {canonical} is outside approved roots and was "
            "not approved; refusing to delegate there"
        )

    def _ask_directory_approval(self, request: DelegationRequest, path: Any) -> bool:
        arguments: dict[str, Any] = {"path": str(path)}
        tool = _SyntheticTool(
            id="delegation.working_directory",
            name="delegation working directory",
            description="delegation workspace outside approved roots",
            version="1.0.0",
            risk_level=ToolRisk.HIGH,
            category=ToolCategory.FILESYSTEM,
        )
        tool_request = ToolRequest(
            request_id=request.request_id,
            tool_id=tool.id,
            arguments=arguments,
            session_id=request.session_id,
            task_id=request.task_id,
            source="delegation",
        )
        reason = "workspace outside approved roots"
        self._publish_tool(TOOL_APPROVAL_REQUESTED, tool_request, tool, reason)
        if self.approval is None:
            self._publish_tool(
                TOOL_DENIED,
                tool_request,
                tool,
                "no approval provider configured",
            )
            return False
        outcome = self.approval.request_approval(tool_request, tool, reason)
        if outcome is ApprovalOutcome.APPROVED:
            self._publish_tool(TOOL_APPROVED, tool_request, tool)
            self._publish_tool(TOOL_ALLOWED, tool_request, tool)
            return True
        self._publish_tool(TOOL_REJECTED, tool_request, tool, outcome=outcome.value)
        self._publish_tool(TOOL_DENIED, tool_request, tool, f"approval {outcome.value}")
        return False

    # --- internals: run loop -------------------------------------------

    def _run(
        self,
        task: DelegationTask,
        provider: DelegationProvider,
        limits: DelegationLimits,
    ) -> DelegationResult:
        self._publish(
            DELEGATION_REQUESTED,
            {
                "request_id": task.request_id,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "provider": provider.provider_id(),
                "state": DelegationState.CREATED.value,
            },
        )
        status = DelegationRunStatus(
            task_id=task.task_id,
            request_id=task.request_id,
            session_id=task.session_id,
            provider=provider.provider_id(),
            started_at=datetime.now(UTC),
        )
        started = time.monotonic()
        result: DelegationResult | None = None
        session_id: str | None = None
        record: _RunningDelegation | None = None
        status.transition(DelegationState.STARTING)
        try:
            with self._lock:
                if len(self._running) >= limits.max_session_count:
                    raise DelegationLimitError(
                        "delegation session count limit reached",
                        kind="session_count",
                    )
            try:
                session_id = provider.create_session()
            except Exception as exc:  # provider failures -> FAILED result
                result = self._fail_result(
                    status, started, provider, task, session_id,
                    reason="session_create_failed", error=str(exc)[:_ERROR_MAX],
                )
                return result
            status.provider_session_id = session_id
            status.transition(DelegationState.RUNNING)
            try:
                provider.send_delegation_prompt(
                    session_id,
                    task.prompt,
                    working_directory=str(task.working_directory),
                )
            except Exception as exc:  # prompt failures -> FAILED result
                result = self._fail_result(
                    status, started, provider, task, session_id,
                    reason="prompt_send_failed", error=str(exc)[:_ERROR_MAX],
                )
                return result
            self._publish(
                DELEGATION_STARTED,
                {
                    "request_id": task.request_id,
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "provider": provider.provider_id(),
                    "state": status.state.value,
                },
            )
            token = threading.Event()
            record = _RunningDelegation(task, status, provider, token, session_id)
            with self._lock:
                self._running[task.task_id] = record
            try:
                result = self._monitor(task, status, provider, limits, record, started)
            except DelegationLimitError as exc:
                result = self._fail_result(
                    status, started, provider, task, session_id,
                    reason=exc.kind or "limit", error=str(exc)[:_ERROR_MAX],
                )
            return result
        except DelegationLimitError as exc:
            result = self._fail_result(
                status, started, provider, task, session_id,
                reason=exc.kind or "limit", error=str(exc)[:_ERROR_MAX],
            )
            return result
        finally:
            if record is not None:
                with self._lock:
                    self._running.pop(task.task_id, None)
            if session_id is not None:
                try:
                    provider.dispose_session(session_id)
                except Exception as exc:  # cleanup is best-effort
                    log.debug("delegation session cleanup failed: %s", exc)
            if result is not None:
                assert result.state.is_terminal
                with self._lock:
                    self._results[task.task_id] = result
                    while len(self._results) > _RESULTS_KEEP:
                        self._results.pop(next(iter(self._results)))
        log.info(
            "delegation finished",
            extra={
                "component": "delegation",
                "task_id": task.task_id,
                "provider": provider.provider_id(),
                "state": (result.state.value if result else "unknown"),
            },
        )

    def _monitor(
        self,
        task: DelegationTask,
        status: DelegationRunStatus,
        provider: DelegationProvider,
        limits: DelegationLimits,
        record: _RunningDelegation,
        started: float,
    ) -> DelegationResult:
        deadline = started + limits.max_wall_time_seconds
        reconnects = 0
        while True:
            if record.token.is_set():
                return self._cancel_result(task, status, provider, started)
            if time.monotonic() >= deadline:
                return self._timeout_result(task, status, provider, started)
            try:
                stream = provider.iter_session_events(record.provider_session_id)
                event_received = False
                for event in stream:
                    event_received = True
                    if record.token.is_set():
                        return self._cancel_result(task, status, provider, started)
                    if time.monotonic() >= deadline:
                        return self._timeout_result(task, status, provider, started)
                    if event.kind is DelegationEventKind.PERMISSION_REQUESTED:
                        try:
                            self._handle_permission(event, task, status, provider, limits)
                        except DelegationLimitError as exc:
                            return self._fail_result(
                                status, started, provider, task,
                                record.provider_session_id,
                                reason=exc.kind or "permission_limit",
                                error=str(exc)[:_ERROR_MAX],
                            )
                    elif event.kind is DelegationEventKind.PROGRESS:
                        status.output_bytes += len((event.message or "").encode("utf-8"))
                        self._publish(
                            DELEGATION_PROGRESS,
                            {
                                "request_id": task.request_id,
                                "task_id": task.task_id,
                                "session_id": task.session_id,
                                "provider": provider.provider_id(),
                                "state": status.state.value,
                                "output_bytes": status.output_bytes,
                                "progress": redact_secrets(
                                    (event.message or "")[:_PROGRESS_EVENT_MAX]
                                ),
                            },
                        )
                        if status.output_bytes > limits.max_output_bytes:
                            return self._fail_result(
                                status, started, provider, task,
                                record.provider_session_id,
                                reason="output_limit",
                                error="delegated output exceeded the byte limit",
                            )
                    elif event.kind is DelegationEventKind.COMPLETED:
                        status.transition(DelegationState.COMPLETING)
                        return self._complete_result(
                            task, status, provider, started, limits,
                            summary=(event.message or "")[:_SUMMARY_MAX],
                        )
                    elif event.kind is DelegationEventKind.FAILED:
                        return self._fail_result(
                            status, started, provider, task,
                            record.provider_session_id,
                            reason="executor_error",
                            error=(event.message or "")[:_ERROR_MAX] or "executor reported failure",
                        )
                    elif event.kind is DelegationEventKind.CANCELLED:
                        return self._cancel_result(task, status, provider, started)
                    # NOTE events are informational and ignored here.
                if not event_received:
                    # The stream ended without a single event.
                    if record.token.is_set():
                        return self._cancel_result(task, status, provider, started)
                    if time.monotonic() >= deadline:
                        return self._timeout_result(task, status, provider, started)
                    reconnects += 1
                    if reconnects > RECONNECT_LIMIT:
                        return self._fail_result(
                            status, started, provider, task,
                            record.provider_session_id,
                            reason="event_connection_lost",
                            error="executor event stream ended without a result",
                        )
            except Exception as exc:  # bounded reconnect for any failure
                if record.token.is_set():
                    return self._cancel_result(task, status, provider, started)
                if time.monotonic() >= deadline:
                    return self._timeout_result(task, status, provider, started)
                reconnects += 1
                if reconnects > RECONNECT_LIMIT:
                    return self._fail_result(
                        status, started, provider, task,
                        record.provider_session_id,
                        reason="event_connection_lost",
                        error=str(exc)[:_ERROR_MAX],
                    )
            # One sweep complete: pause before the next one so a poll-based
            # provider is never hammered back-to-back for the whole run.
            self._pause_between_sweeps()

    def _pause_between_sweeps(self) -> None:
        """Sleep the configured poll interval (tests may zero it out)."""
        interval = self.poll_interval_seconds
        if interval > 0:
            time.sleep(interval)

    # --- internals: permission routing ---------------------------------

    def _handle_permission(
        self,
        event: DelegationEvent,
        task: DelegationTask,
        status: DelegationRunStatus,
        provider: DelegationProvider,
        limits: DelegationLimits,
    ) -> None:
        status.permission_requests += 1
        if status.permission_requests > limits.max_permission_requests:
            raise DelegationLimitError(
                "permission request limit reached",
                kind="permission_limit",
            )
        permission = DelegationPermission(
            permission_id=str(event.metadata.get("permission_id") or "unknown"),
            session_id=status.session_id or status.task_id,
            action=str(event.metadata.get("action") or "unknown").lower(),
            path=event.metadata.get("path") or None,
            # Phase 9: executor messages can echo secrets; audit keeps the
            # redacted form. `path` stays verbatim — approvers need the exact
            # target to decide.
            description=redact_secrets((event.message or "")[:_PROGRESS_EVENT_MAX]) or None,
        )
        category, risk = _classify_permission(permission.action)
        arguments: dict[str, Any] = {}
        if permission.path:
            arguments["path"] = permission.path
        tool = _SyntheticTool(
            id=f"permission.{permission.action}",
            name=f"delegated {permission.action}",
            description="executor permission translated to Great Sage policy",
            version="1.0.0",
            risk_level=risk,
            category=category,
        )
        tool_request = ToolRequest(
            request_id=task.request_id,
            tool_id=tool.id,
            arguments=arguments,
            session_id=task.session_id,
            task_id=task.task_id,
            source="delegation",
        )
        self._publish(
            DELEGATION_PERMISSION_REQUESTED,
            {
                "request_id": task.request_id,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "provider": provider.provider_id(),
                "state": status.state.value,
                "permission_id": permission.permission_id,
                "action": permission.action,
                "risk": risk.value,
                "path": permission.path,
            },
        )
        decision, reason = self._policy.evaluate(tool_request, tool)
        if decision is ToolDecision.ALLOW:
            provider.respond_permission(
                status.provider_session_id or "", permission.permission_id, True
            )
            self._publish_tool(TOOL_ALLOWED, tool_request, tool)
            self._publish_resolved(task, provider, status, permission, "allow", reason)
            return
        if decision is ToolDecision.DENY:
            provider.respond_permission(
                status.provider_session_id or "", permission.permission_id, False
            )
            self._publish_tool(TOOL_DENIED, tool_request, tool, reason)
            self._publish_resolved(task, provider, status, permission, "deny", reason)
            return
        # ASK — pause for human approval; never auto-approve a delegated request.
        status.transition(DelegationState.WAITING_FOR_PERMISSION)
        self._publish_tool(TOOL_APPROVAL_REQUESTED, tool_request, tool, reason)
        outcome = ApprovalOutcome.DENIED
        if self.approval is not None:
            outcome = self.approval.request_approval(tool_request, tool, reason)
        status.transition(DelegationState.RUNNING)
        approved = outcome is ApprovalOutcome.APPROVED
        provider.respond_permission(
            status.provider_session_id or "", permission.permission_id, approved
        )
        if approved:
            self._publish_tool(TOOL_APPROVED, tool_request, tool)
            self._publish_tool(TOOL_ALLOWED, tool_request, tool)
            self._publish_resolved(task, provider, status, permission, "allow", reason)
        else:
            self._publish_tool(TOOL_REJECTED, tool_request, tool, outcome=outcome.value)
            self._publish_tool(TOOL_DENIED, tool_request, tool, f"approval {outcome.value}")
            self._publish_resolved(task, provider, status, permission, "deny", reason)

    def _publish_resolved(
        self,
        task: DelegationTask,
        provider: DelegationProvider,
        status: DelegationRunStatus,
        permission: DelegationPermission,
        decision: str,
        reason: str,
    ) -> None:
        self._publish(
            DELEGATION_PERMISSION_RESOLVED,
            {
                "request_id": task.request_id,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "provider": provider.provider_id(),
                "state": status.state.value,
                "permission_id": permission.permission_id,
                "action": permission.action,
                "decision": decision,
                "reason": (reason or "")[:_PROGRESS_EVENT_MAX],
            },
        )

    # --- internals: terminal results -----------------------------------

    def _complete_result(
        self,
        task: DelegationTask,
        status: DelegationRunStatus,
        provider: DelegationProvider,
        started: float,
        limits: DelegationLimits,
        summary: str | None,
    ) -> DelegationResult:
        status.transition(DelegationState.COMPLETED)
        diff_data = None
        try:
            diff_data = provider.get_session_diff(
                status.provider_session_id or ""
            )
        except Exception as exc:  # diff is review material; never fatal
            log.debug("delegation diff fetch failed: %s", exc)
        files_changed = 0
        diff_text: str | None = None
        if isinstance(diff_data, dict):
            files = diff_data.get("files")
            if isinstance(files, list):
                files_changed = len(files)
            else:
                files_changed = len(diff_data) - sum(1 for k in diff_data if k in ("files",))
                if files_changed < 0:
                    files_changed = 0
            remaining = limits.max_output_bytes - status.output_bytes
            encoded = json.dumps(diff_data)
            if remaining > 0:
                budget = min(len(encoded), remaining)
                diff_text = encoded[:budget]
                if len(diff_text) == budget and budget < len(encoded):
                    diff_text += "\n[diff truncated]"
            # Phase 9: delegated diffs can embed freshly written secrets
            # (e.g. a key the executor placed in a file); stored results and
            # audit payloads keep only the redacted form.
            if diff_text is not None:
                diff_text = redact_secrets(diff_text) or diff_text
        result = DelegationResult(
            task_id=task.task_id,
            request_id=task.request_id,
            session_id=task.session_id,
            provider=provider.provider_id(),
            state=DelegationState.COMPLETED,
            provider_session_id=status.provider_session_id,
            summary=redact_secrets(summary),
            duration_ms=(time.monotonic() - started) * 1000.0,
            permission_requests=status.permission_requests,
            output_bytes=status.output_bytes,
            files_changed=files_changed,
            diff_available=diff_text is not None,
            diff=diff_text,
        )
        self._publish(
            DELEGATION_COMPLETED,
            {
                "request_id": task.request_id,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "provider": provider.provider_id(),
                "state": result.state.value,
                "duration_ms": result.duration_ms,
                "files_changed": files_changed,
                "diff_available": result.diff_available,
                "permission_requests": status.permission_requests,
                "output_bytes": status.output_bytes,
            },
        )
        return result

    def _fail_result(
        self,
        status: DelegationRunStatus,
        started: float,
        provider: DelegationProvider,
        task: DelegationTask,
        session_id: str | None,
        reason: str,
        error: str | None,
    ) -> DelegationResult:
        transition_state(status.state, DelegationState.FAILED)
        # Phase 9: provider exceptions can echo secrets; store redacted form.
        error = redact_secrets(error)
        status.error = error
        status.reason = reason
        if session_id:
            provider.abort_session(session_id)
        result = DelegationResult(
            task_id=task.task_id,
            request_id=task.request_id,
            session_id=task.session_id,
            provider=provider.provider_id(),
            state=DelegationState.FAILED,
            provider_session_id=status.provider_session_id,
            error=error,
            reason=reason,
            duration_ms=(time.monotonic() - started) * 1000.0,
            permission_requests=status.permission_requests,
            output_bytes=status.output_bytes,
        )
        self._publish(
            DELEGATION_FAILED,
            {
                "request_id": task.request_id,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "provider": provider.provider_id(),
                "state": result.state.value,
                "reason": reason,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    def _cancel_result(
        self,
        task: DelegationTask,
        status: DelegationRunStatus,
        provider: DelegationProvider,
        started: float,
    ) -> DelegationResult:
        transition_state(status.state, DelegationState.CANCELLED)
        session_id = status.provider_session_id
        if session_id:
            provider.abort_session(session_id)
        result = DelegationResult(
            task_id=task.task_id,
            request_id=task.request_id,
            session_id=task.session_id,
            provider=provider.provider_id(),
            state=DelegationState.CANCELLED,
            provider_session_id=session_id,
            duration_ms=(time.monotonic() - started) * 1000.0,
            permission_requests=status.permission_requests,
            output_bytes=status.output_bytes,
        )
        self._publish(
            DELEGATION_CANCELLED,
            {
                "request_id": task.request_id,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "provider": provider.provider_id(),
                "state": result.state.value,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    def _timeout_result(
        self,
        task: DelegationTask,
        status: DelegationRunStatus,
        provider: DelegationProvider,
        started: float,
    ) -> DelegationResult:
        transition_state(status.state, DelegationState.TIMED_OUT)
        session_id = status.provider_session_id
        if session_id:
            provider.abort_session(session_id)
        result = DelegationResult(
            task_id=task.task_id,
            request_id=task.request_id,
            session_id=task.session_id,
            provider=provider.provider_id(),
            state=DelegationState.TIMED_OUT,
            provider_session_id=session_id,
            error="delegation exceeded the wall-clock limit",
            reason="timeout",
            duration_ms=(time.monotonic() - started) * 1000.0,
            permission_requests=status.permission_requests,
            output_bytes=status.output_bytes,
        )
        self._publish(
            DELEGATION_TIMED_OUT,
            {
                "request_id": task.request_id,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "provider": provider.provider_id(),
                "state": result.state.value,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    # --- internals: events ---------------------------------------------

    def _publish_tool(
        self,
        event_type: str,
        request: ToolRequest,
        tool: Any,
        reason: str | None = None,
        outcome: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "request_id": request.request_id,
            "tool_id": tool.id,
            "risk_level": tool.risk_level.value,
            "source": request.source,
            "session_id": request.session_id,
            "task_id": request.task_id,
            "permission_id": None,
        }
        if reason:
            redacted = redact_secrets(reason[:_PROGRESS_EVENT_MAX])
            payload["reason"] = redacted
        if outcome:
            payload["outcome"] = outcome
        self._publish(event_type, payload)

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(
                Event(
                    type=event_type,
                    source="delegation",
                    session_id=payload.get("session_id"),
                    task_id=payload.get("task_id"),
                    payload=payload,
                )
            )
        except Exception as exc:
            log.warning(
                "delegation event publish failed: %s",
                exc,
                extra={"component": "delegation"},
            )


def _classify_permission(action: str) -> tuple[ToolCategory, ToolRisk]:
    """Translate an executor permission action to Great Sage category/risk.

    Unknown actions default to SYSTEM/HIGH so they are asked about (or
    denied in lockdown) — never silently allowed.
    """
    normalized = action.casefold()
    return (
        _PERMISSION_CATEGORY.get(normalized, _UNKNOWN_PERMISSION_CATEGORY),
        _PERMISSION_RISK.get(normalized, _UNKNOWN_PERMISSION_RISK),
    )
