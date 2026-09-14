"""Intelligence service facade.

Owns the provider registry + router, constructs adapters from configuration,
acts as the single entry point for generate/stream/cancel, and publishes the
AI* event stream (docs/INTERFACES.md §11). Provider failures degrade health
and surface events — they never crash the runtime or the bus.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from greatsage.configuration.model import AIConfig, JarvisConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import (
    AI_PROVIDER_SELECTED,
    AI_PROVIDER_UNAVAILABLE,
    AI_REQUEST_COMPLETED,
    AI_REQUEST_FAILED,
    AI_REQUEST_STARTED,
    AI_STREAM_COMPLETED,
    AI_STREAM_FAILED,
    AI_STREAM_STARTED,
    Event,
)
from greatsage.exceptions import ProviderError, RoutingError
from greatsage.intelligence.mock import MockProvider
from greatsage.intelligence.models import (
    AIRequest,
    AIResponse,
    StreamChunk,
    format_response,
)
from greatsage.intelligence.ollama import OllamaProvider
from greatsage.intelligence.opencode import OpenCodeProvider
from greatsage.intelligence.provider import ProviderHealth, ProviderState
from greatsage.intelligence.registry import ProviderRegistry
from greatsage.intelligence.router import Route, Router

log = logging.getLogger("greatsage.intelligence.service")


class IntelligenceService:
    """Facade over providers, registry, and router."""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self.registry = registry or ProviderRegistry()
        self.router: Router | None = None
        self.publisher: Any = None  # callable(event) -> None, wired by runtime
        self._default_provider: str | None = None

    # --- lifecycle -----------------------------------------------------

    def start(self, config: JarvisConfig | None = None) -> None:
        self._register_from_config(config)
        self.router = Router(
            self.registry, default_provider=self._default_provider
        )
        states = self.registry.initialize_all()
        for provider_id, state in states.items():
            if state is not ProviderState.READY:
                health = self.registry.health(provider_id)
                self._publish_unavailable(provider_id, health)
        log.info(
            "intelligence service started",
            extra={
                "component": "intelligence",
                "providers": list(self.registry.ids()),
                "states": {name: state.value for name, state in states.items()},
            },
        )

    def shutdown(self) -> None:
        self.registry.shutdown_all()
        self.router = None
        log.info("intelligence service stopped", extra={"component": "intelligence"})

    # --- configuration-driven construction -----------------------------

    def _register_from_config(self, config: JarvisConfig | None) -> None:
        if config is None:
            return
        ai: AIConfig = config.ai
        self._default_provider = (
            ai.default_provider if ai.default_provider else None
        )
        local = ai.local
        if local.enabled and not self.registry.has("local"):
            self.registry.register(
                OllamaProvider(
                    base_url=local.base_url,
                    model=local.model,
                    timeout_seconds=local.timeout_seconds,
                )
            )
        opencode = ai.opencode
        if opencode.enabled and not self.registry.has("opencode"):
            self.registry.register(
                OpenCodeProvider(
                    base_url=opencode.base_url,
                    api_key_env=opencode.api_key_env,
                    model=opencode.model,
                    timeout_seconds=opencode.timeout_seconds,
                )
            )

    def register_mock(
        self,
        provider_id: str = "mock",
        **kwargs: Any,
    ) -> None:
        """Explicit test/harness hook — never automatic."""
        self.registry.register(MockProvider(provider_id=provider_id, **kwargs))

    # --- public API ----------------------------------------------------

    def generate(self, request: AIRequest) -> AIResponse:
        request.validate()
        started = time.monotonic()
        self._publish(
            AI_REQUEST_STARTED,
            {
                "request_id": request.request_id,
                "model": request.model,
                "provider_hint": request.metadata.get("provider"),
                "task_id": request.metadata.get("task_id"),
            },
        )
        try:
            route = self._select(request)
            provider = self.registry.get(route.selected_provider or "")
            response = provider.generate(request)
        except Exception as exc:
            self._publish_failure(request, exc, started)
            raise
        self._publish(
            AI_REQUEST_COMPLETED,
            {
                "request_id": request.request_id,
                "provider": response.provider,
                "model": response.model,
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "finish_reason": response.finish_reason.value,
                "content": format_response(response)[:200],
                "task_id": request.metadata.get("task_id"),
            },
        )
        return response

    async def stream(self, request: AIRequest) -> Any:
        request.validate()
        started = time.monotonic()
        self._publish(
            AI_STREAM_STARTED,
            {
                "request_id": request.request_id,
                "model": request.model,
                "task_id": request.metadata.get("task_id"),
            },
        )
        chunk: StreamChunk | None = None
        text = ""
        try:
            route = self._select(request)
            provider = self.registry.get(route.selected_provider or "")
            async for item in provider.stream(request):
                chunk = item
                if chunk.kind == "text":
                    text += chunk.text
                yield chunk
                if chunk.kind in ("completion", "error"):
                    break
            if chunk is not None and chunk.kind == "completion":
                self._publish(
                    AI_STREAM_COMPLETED,
                    {
                        "request_id": request.request_id,
                        "provider": provider.provider_id(),
                        "duration_ms": (time.monotonic() - started) * 1000.0,
                        "content": text[:200],
                        "task_id": request.metadata.get("task_id"),
                    },
                )
            elif chunk is not None and chunk.kind == "error":
                self._publish_failure(
                    request,
                    ProviderError(f"stream ended with error chunk: {chunk.error}"),
                    started,
                    AI_STREAM_FAILED,
                )
        except Exception as exc:
            self._publish_failure(request, exc, started, AI_STREAM_FAILED)
            raise

    def cancel(self, request_id: str) -> None:
        for provider in self.registry.enumerate():
            if provider.state is ProviderState.READY:
                try:
                    provider.cancel(request_id)
                except Exception as exc:
                    log.debug("cancel failed on %s: %s", provider.provider_id(), exc)

    # --- health --------------------------------------------------------

    def health(self) -> dict[str, ProviderHealth]:
        return self.registry.health_all()

    def health_summary(self) -> str:
        """One-line status for the runtime health check."""
        states = [health.state.value for health in self.registry.health_all().values()]
        if not states:
            return "no AI providers configured"
        if all(state == ProviderState.READY.value for state in states):
            return "providers ready: " + ", ".join(states)
        return "providers: " + ", ".join(states)

    def register_health_check(self, health_registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            return self._health_status()

        health_registry.register("intelligence", checker, "AI provider layer")

    def _health_status(self) -> HealthStatus:
        if not self.registry.ids():
            return HealthStatus.UNHEALTHY
        for health in self.registry.health_all().values():
            if health.ok:
                return HealthStatus.HEALTHY
        return HealthStatus.UNHEALTHY

    # --- internals -----------------------------------------------------

    def _select(self, request: AIRequest) -> Route:
        if self.router is None:
            raise RoutingError("intelligence service not started")
        route = self.router.route(request)
        self._publish(
            AI_PROVIDER_SELECTED,
            {
                "request_id": request.request_id,
                "provider": route.selected_provider,
                "reason": route.reason,
                **route.to_dict(),
            },
        )
        return route

    def _publish_unavailable(self, provider_id: str, health: ProviderHealth) -> None:
        self._publish(
            AI_PROVIDER_UNAVAILABLE,
            {
                "provider": provider_id,
                "state": health.state.value,
                "detail": health.detail,
            },
        )

    def _publish_failure(
        self,
        request: AIRequest,
        exc: Exception,
        started: float,
        event_type: str = AI_REQUEST_FAILED,
    ) -> None:
        self._publish(
            event_type,
            {
                "request_id": request.request_id,
                "error": str(exc)[:200],
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "task_id": request.metadata.get("task_id"),
            },
        )

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(
                Event(
                    type=event_type,
                    source="intelligence",
                    session_id=payload.get("session_id"),
                    task_id=payload.get("task_id"),
                    payload=payload,
                )
            )
        except Exception as exc:
            # In CLI context there's no event loop — events are best-effort
            log.debug("event publish skipped: %s", exc, extra={"component": "intelligence"})
