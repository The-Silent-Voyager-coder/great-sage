"""Tool service facade: the single security pipeline (spec §2).

    AI → ToolRequest → ToolRegistry → SecurityPolicy → Permission Decision
        → Approval → Tool Execution → ToolResult → Audit Events

There is no bypass path: CLI execution goes through this exact service.
Every attempt is recorded as events; denied attempts are never discarded.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from typing import Any

from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import (
    TOOL_ALLOWED,
    TOOL_APPROVAL_REQUESTED,
    TOOL_APPROVED,
    TOOL_COMPLETED,
    TOOL_DENIED,
    TOOL_FAILED,
    TOOL_REJECTED,
    TOOL_REQUESTED,
    TOOL_STARTED,
    Event,
)
from greatsage.exceptions import (
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolUnavailableError,
    ToolValidationError,
)
from greatsage.tools.approval import ApprovalProvider
from greatsage.tools.defaults import register_default_tools
from greatsage.tools.environment import scrub_environment
from greatsage.tools.models import (
    ApprovalOutcome,
    Tool,
    ToolContext,
    ToolDecision,
    ToolRequest,
    ToolResult,
    validate_arguments,
)
from greatsage.tools.policy import SecurityPolicy
from greatsage.tools.redaction import redact_secrets
from greatsage.tools.registry import ToolRegistry

log = logging.getLogger("greatsage.tools.service")

DEFAULT_MAX_OUTPUT_BYTES = 65536


def _common_payload(
    request: ToolRequest, tool: Tool, **extra: Any
) -> dict[str, Any]:
    payload = {
        "request_id": request.request_id,
        "tool_id": tool.id,
        "risk_level": tool.risk_level.value,
        "source": request.source,
        "session_id": request.session_id,
        "task_id": request.task_id,
    }
    payload.update(extra)
    return payload


class ToolService:
    """Owns the registry, policy, approval provider, and audit events."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        policy: SecurityPolicy | None = None,
        approval: ApprovalProvider | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self.approval = approval  # public; CLI/tests may install providers
        self.publisher: Any = None  # callable(event) -> None, wired by runtime
        self.availability: str = "unavailable"
        self.detail: str = "tool service not started"
        self._config: SageConfig | None = None
        self._environment: dict[str, str] = {}

    # --- lifecycle -----------------------------------------------------

    def start(self, config: SageConfig | None = None) -> None:
        """Initialize registry, policy, and environment from config.

        Failures never raise: the service reports 'unavailable' and the
        runtime keeps working (Phase 3 §37 failure-isolation pattern).
        """
        self._config = config
        if config is None:
            self.availability = "unavailable"
            self.detail = "no configuration provided"
            return
        try:
            registry = self._registry or ToolRegistry()
            register_default_tools(registry)
            self._register_media_tools(registry, config)
            policy = self._policy or SecurityPolicy.from_config(config)
        except Exception as exc:
            self.availability = "unavailable"
            self.detail = str(exc)[:300]
            log.error(
                "tool service failed to initialize: %s",
                exc,
                exc_info=True,
                extra={"component": "tools"},
            )
            return
        self._registry = registry
        self._policy = policy
        self._environment = scrub_environment(os.environ)
        self.availability = "healthy"
        self.detail = (
            f"tool registry ready: {len(registry.list_ids())} tools, "
            f"mode {config.security.mode.value}"
        )
        log.info(
            "tool service started",
            extra={
                "component": "tools",
                "tool_count": len(registry.list_ids()),
                "mode": config.security.mode.value,
            },
        )

    def shutdown(self) -> None:
        self._registry = None
        self._policy = None
        self.availability = "disabled"
        self.detail = "tool service stopped"
        log.info("tool service stopped", extra={"component": "tools"})

    def _register_media_tools(self, registry: ToolRegistry, config: SageConfig) -> None:
        """Register the voice and vision toolsets (Phase 6/8 wiring).

        Voice tools are built from the configured engines (the same backends
        ``greatsage voice`` uses); if the configured engines cannot be built
        they stay unregistered rather than silently substituting mocks —
        registered mocks always report ``backend: mock`` in their output.
        Vision tools use the bounded stub backend by design (Phase 8).
        """
        from greatsage.vision.capture import build_capture_manager
        from greatsage.vision.grounding import build_grounding
        from greatsage.vision.tools import register_vision_tools

        register_vision_tools(
            registry,
            capture=build_capture_manager(),
            grounding=build_grounding(base_url=config.ai.local.base_url),
        )
        try:
            from greatsage.voice.backends import (
                build_stt_manager,
                build_tts_manager,
                build_wake_detector,
            )
            from greatsage.voice.limits import default_limits as voice_limits
            from greatsage.voice.tools import register_voice_tools

            limits = voice_limits()
            register_voice_tools(
                registry,
                limits=limits,
                stt=build_stt_manager(config, limits),
                tts=build_tts_manager(config, limits),
                wake=build_wake_detector(config, limits),
            )
        except Exception as exc:  # voice stays optional; never fails the service
            log.warning(
                "voice tools not registered: %s",
                exc,
                extra={"component": "tools"},
            )

    # --- public API ----------------------------------------------------

    def execute(self, request: ToolRequest) -> ToolResult:
        """Run one request through the full security pipeline (spec §2)."""
        self._require_available()
        registry = self._registry
        assert registry is not None
        try:
            tool = registry.get(request.tool_id)
        except ToolNotFoundError as exc:
            self._publish(
                TOOL_REQUESTED,
                {
                    "request_id": request.request_id,
                    "tool_id": request.tool_id,
                    "source": request.source,
                    "session_id": request.session_id,
                    "task_id": request.task_id,
                },
            )
            self._publish(
                TOOL_FAILED,
                {
                    "request_id": request.request_id,
                    "tool_id": request.tool_id,
                    "source": request.source,
                    "session_id": request.session_id,
                    "task_id": request.task_id,
                    "error": str(exc),
                },
            )
            raise
        self._publish(TOOL_REQUESTED, _common_payload(request, tool))
        try:
            validate_arguments(request.arguments, tool.input_schema)
        except ToolValidationError as exc:
            self._publish(
                TOOL_FAILED,
                _common_payload(request, tool, error=redact_secrets(str(exc))),
            )
            raise
        self._enforce_policy(request, tool)
        context = self._build_context()
        self._publish(TOOL_STARTED, _common_payload(request, tool))
        started = time.perf_counter()
        try:
            result = tool.execute(dict(request.arguments), context)
        except ToolPermissionDeniedError as exc:
            self._publish(TOOL_DENIED, _common_payload(request, tool, reason=str(exc)))
            raise
        except ToolExecutionError as exc:
            result = ToolResult(request.request_id, tool.id, False, error=str(exc))
        except Exception as exc:
            result = ToolResult(
                request.request_id,
                tool.id,
                False,
                error=f"unexpected tool failure: {exc}",
            )
        duration_ms = (time.perf_counter() - started) * 1000
        result = replace(
            result,
            request_id=request.request_id,
            duration_ms=round(duration_ms, 3),
        )
        # Phase 9: tool stderr/exceptions can echo credential material
        # (e.g. a failing curl with an embedded token); audit payloads and
        # the returned result carry only the redacted form.
        if result.error:
            result = replace(result, error=redact_secrets(result.error))
        result = self._enforce_output_limit(result)
        if result.success:
            self._publish(
                TOOL_COMPLETED,
                _common_payload(
                    request,
                    tool,
                    decision=ToolDecision.ALLOW.value,
                    duration_ms=result.duration_ms,
                ),
            )
        else:
            self._publish(
                TOOL_FAILED,
                _common_payload(
                    request,
                    tool,
                    duration_ms=result.duration_ms,
                    error=result.error,
                ),
            )
        return result

    def registry(self) -> ToolRegistry:
        self._require_available()
        assert self._registry is not None
        return self._registry

    def health(self) -> dict[str, Any]:
        registry_health: dict[str, Any] = {}
        if self.availability == "healthy" and self._registry is not None:
            registry_health = self._registry.health()
        mode = (
            self._config.security.mode.value
            if self._config is not None
            else None
        )
        return {
            "available": self.availability == "healthy",
            "status": self.availability,
            "detail": self.detail,
            "mode": mode,
            **registry_health,
        }

    def register_health_check(self, health_registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self.availability == "disabled":
                return HealthStatus.HEALTHY
            if self.availability == "healthy":
                return HealthStatus.HEALTHY
            if self.availability == "unavailable":
                return HealthStatus.UNHEALTHY
            return HealthStatus.DEGRADED  # pragma: no cover - future states

        health_registry.register("tools", checker, "tool subsystem (secure)")

    # --- internals -------------------------------------------------------

    def _require_available(self) -> None:
        if self.availability != "healthy":
            raise ToolUnavailableError(self.detail or "tool service is not available")

    def _enforce_policy(self, request: ToolRequest, tool: Tool) -> None:
        assert self._policy is not None
        decision, reason = self._policy.evaluate(request, tool)
        if decision is ToolDecision.ALLOW:
            self._publish(TOOL_ALLOWED, _common_payload(request, tool))
            return
        if decision is ToolDecision.DENY:
            self._publish(TOOL_DENIED, _common_payload(request, tool, reason=reason))
            raise ToolPermissionDeniedError(reason)
        # ASK — approval required (spec §12-13)
        self._publish(
            TOOL_APPROVAL_REQUESTED, _common_payload(request, tool, reason=reason)
        )
        if self.approval is None:
            self._publish(
                TOOL_DENIED,
                _common_payload(
                    request, tool, reason="no approval provider configured"
                ),
            )
            raise ToolPermissionDeniedError(
                "no approval provider configured; request denied"
            )
        outcome = self.approval.request_approval(request, tool, reason)
        if outcome is ApprovalOutcome.APPROVED:
            self._publish(TOOL_APPROVED, _common_payload(request, tool))
            self._publish(TOOL_ALLOWED, _common_payload(request, tool))
            return
        self._publish(TOOL_REJECTED, _common_payload(request, tool, outcome=outcome.value))
        self._publish(
            TOOL_DENIED, _common_payload(request, tool, reason=f"approval {outcome.value}")
        )
        raise ToolPermissionDeniedError(f"approval {outcome.value}")

    def _build_context(self) -> ToolContext:
        assert self._config is not None
        tools = self._config.tools
        return ToolContext(
            working_directory=tools.working_directory,
            environment=dict(self._environment),
            timeout_seconds=float(tools.execution_timeout_seconds),
            max_output_bytes=int(tools.max_output_bytes),
        )

    def _enforce_output_limit(self, result: ToolResult) -> ToolResult:
        if result.output is None:
            return result
        limit = (
            int(self._config.tools.max_output_bytes)
            if self._config is not None
            else DEFAULT_MAX_OUTPUT_BYTES
        )
        if len(json.dumps(result.output)) <= limit:
            return result
        return replace(
            result,
            output={
                "_truncated": True,
                "note": f"output exceeded the {limit}-byte limit",
            },
            metadata={**result.metadata, "output_truncated": True},
        )

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        # Phase 9: belt-and-braces — free-text audit fields never carry
        # credential material, even if a future caller forgets to redact.
        for key in ("error", "reason"):
            value = payload.get(key)
            if isinstance(value, str):
                payload[key] = redact_secrets(value)
        if self.publisher is None:
            return
        try:
            self.publisher(
                Event(
                    type=event_type,
                    source="tools",
                    session_id=payload.get("session_id"),
                    task_id=payload.get("task_id"),
                    payload=payload,
                )
            )
        except Exception as exc:
            log.warning("tool event publish failed: %s", exc, extra={"component": "tools"})
