"""Ollama provider adapter (local, HTTP).

Connects to a configured Ollama endpoint (default http://127.0.0.1:11434),
discovers availability + models, and implements health/capabilities/generate/
stream/cancel. It never downloads models or touches the Ollama disk cache —
model selection stays a config concern. When Ollama is not running the
provider reports UNAVAILABLE and the runtime remains functional.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from datetime import UTC, datetime
from typing import Any

from greatsage.exceptions import ProviderError, ProviderUnavailableError
from greatsage.intelligence import transport
from greatsage.intelligence.models import (
    AIRequest,
    AIResponse,
    FinishReason,
    Message,
    StreamChunk,
    TokenUsage,
    ToolCall,
)
from greatsage.intelligence.provider import (
    AIProvider,
    Capability,
    ProviderCapabilities,
    ProviderHealth,
    ProviderState,
)

log = logging.getLogger("greatsage.intelligence.ollama")

OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"


def _join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _map_finish_reason(reason: str | None) -> FinishReason:
    if reason == "length":
        return FinishReason.LENGTH
    if reason in ("tool_calls", "tool_call"):
        return FinishReason.TOOL_CALL
    if reason == "canceled":
        return FinishReason.CANCELLED
    return FinishReason.STOP


def _pick_default_model(models: tuple[str, ...]) -> str | None:
    """First non-embedding model, else the first model, else None."""
    for name in models:
        if "embed" not in name.lower():
            return name
    return models[0] if models else None


_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "description",
        "title",
        "default",
        "enum",
        "items",
        "oneOf",
        "anyOf",
        "allOf",
    }
)


def _schema_accepts(arguments: dict[str, Any], schema: dict[str, Any] | None) -> bool:
    """Cheap check that arguments look like an instance, not the schema itself."""
    if not isinstance(schema, dict):
        return bool(arguments)
    properties = schema.get("properties")
    required = schema.get("required")
    if isinstance(required, list):
        names = [name for name in required if isinstance(name, str)]
        if names:
            return all(name in arguments for name in names)
    if isinstance(properties, dict):
        return any(name in arguments for name in properties)
    return bool(arguments)


def _coerce_boolean_strings(
    arguments: dict[str, Any], schema: dict[str, Any] | None
) -> dict[str, Any]:
    """Repair the common `\"true\"`/`\"false\"` model output for boolean fields.

    Only values whose schema explicitly declares `type: boolean` are touched;
    every other string stays untouched. Argument validation still runs.
    """
    if not isinstance(schema, dict):
        return arguments
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return arguments
    result = dict(arguments)
    for name, spec in properties.items():
        if name not in result or not isinstance(spec, dict):
            continue
        if spec.get("type") != "boolean" or not isinstance(result[name], str):
            continue
        value = result[name].strip().lower()
        if value == "true":
            result[name] = True
        elif value == "false":
            result[name] = False
    return result


def _normalise_tool_arguments(
    arguments: Any, schema: dict[str, Any] | None
) -> dict[str, Any]:
    """Turn common model argument malformations into a flat instance object.

    Small local models sometimes emit the tool schema itself, or wrap the real
    arguments in `parameters`/`properties`/`arguments`. This only reshapes the
    payload; the tool layer still validates it and applies the full security
    policy, so normalization never grants an action.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    if not isinstance(arguments, dict):
        return {}

    # Explicit function-call wrapper: {type, function, parameters: {...}}.
    parameters = arguments.get("parameters")
    if (
        isinstance(parameters, dict)
        and "function" in arguments
        and isinstance(arguments.get("function"), str)
    ):
        arguments = parameters

    # Schema-shaped payload: {type: "object", properties: {...}, required: [...]}.
    if not _schema_accepts(arguments, schema):
        properties = schema.get("properties") if isinstance(schema, dict) else None
        for key in ("parameters", "properties", "arguments"):
            candidate = arguments.get(key)
            if not isinstance(candidate, dict) or not _schema_accepts(candidate, schema):
                continue
            merged = dict(candidate)
            if isinstance(properties, dict):
                for name, value in arguments.items():
                    if (
                        name in properties
                        and name not in merged
                        and name not in _SCHEMA_KEYWORDS
                    ):
                        merged[name] = value
            arguments = merged
            break

    return _coerce_boolean_strings(arguments, schema)


def _parse_tool_calls(
    raw: Any, schemas: dict[str, dict[str, Any]] | None = None
) -> list[ToolCall] | None:
    """Parse Ollama `message.tool_calls` (arguments may arrive as JSON text)."""
    if not isinstance(raw, list) or not raw:
        return None
    calls: list[ToolCall] = []
    for index, call in enumerate(raw):
        if not isinstance(call, dict):
            continue
        raw_function = call.get("function")
        function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
        name = str(function.get("name") or call.get("name") or "unknown")
        schema = (schemas or {}).get(name)
        arguments = _normalise_tool_arguments(function.get("arguments"), schema)
        calls.append(
            ToolCall(
                id=str(call.get("id") or f"call_{index + 1}"),
                name=name,
                arguments=arguments,
            )
        )
    return calls or None


class OllamaProvider(AIProvider):
    """Adapter for a local Ollama server.

    Config fields (from ai.providers.local): base_url, model, enabled,
    timeout_seconds. The 'local' config slot is Phase 0/1 naming; the provider
    id stays 'local'.
    """

    def __init__(
        self,
        *,
        base_url: str = OLLAMA_DEFAULT_URL,
        model: str = "",
        timeout_seconds: float = 60.0,
        provider_id: str = "local",
    ) -> None:
        super().__init__(provider_id)
        self._base_url = base_url if base_url else OLLAMA_DEFAULT_URL
        self._configured_model = model
        self._timeout = timeout_seconds if timeout_seconds > 0 else 60.0
        self._models: tuple[str, ...] = ()
        self._tool_capable = False

    # --- public surface ------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        caps = {
            Capability.TEXT_GENERATION,
            Capability.STREAMING,
            Capability.LOCAL,
            Capability.CANCELLATION,
        }
        if self._models and any("vision" in name for name in self._models):
            caps.add(Capability.VISION)
        if self._tool_capable:
            caps.add(Capability.TOOL_CALLING)
        return ProviderCapabilities(
            capabilities=frozenset(caps),
            model_ids=self._models,
        )

    def _probe_health(self) -> ProviderHealth:
        try:
            data = transport.request_json(
                _join_url(self._base_url, "/api/tags"),
                timeout=min(self._timeout, 5.0),
                default_timeout=5.0,
            )
        except urllib.error.URLError:
            self._set_state(ProviderState.UNAVAILABLE, "connection failed")
            return ProviderHealth(
                provider_id=self._provider_id,
                ok=False,
                state=ProviderState.UNAVAILABLE,
                detail="connection failed",
            )
        except (transport.HTTPErrorStatus, ValueError) as exc:
            self._set_state(ProviderState.DEGRADED, "malformed response")
            return ProviderHealth(
                provider_id=self._provider_id,
                ok=False,
                state=ProviderState.DEGRADED,
                detail=f"malformed response: {str(exc)[:120]}",
            )
        models = tuple(
            str(item.get("name", ""))
            for item in data.get("models", [])
            if isinstance(item, dict) and item.get("name")
        )
        self._models = models
        if not models:
            self._set_state(ProviderState.DEGRADED, "no models available")
            return ProviderHealth(
                provider_id=self._provider_id,
                ok=False,
                state=ProviderState.DEGRADED,
                detail="no models available; pull a model with `ollama pull`",
            )
        loaded = (
            self._configured_model
            if self._configured_model in models
            else _pick_default_model(models)
        )
        self._tool_capable = self._probe_tool_capability(loaded)
        return ProviderHealth(
            provider_id=self._provider_id,
            ok=True,
            state=ProviderState.READY,
            detail=f"{len(models)} model(s) available",
            model_loaded=loaded,
        )

    def _probe_tool_capability(self, model: str | None) -> bool:
        """True when the configured model declares the `tools` capability.

        Ollama reports per-model capabilities via `/api/show`; the probe is
        best-effort (probe failures keep TOOL_CALLING unadvertised — the agent
        layer then fails closed with an explicit capability error).
        """
        if not model:
            return False
        try:
            data = transport.request_json(
                _join_url(self._base_url, "/api/show"),
                method="POST",
                body={"model": model},
                timeout=min(self._timeout, 5.0),
                default_timeout=5.0,
            )
        except Exception as exc:  # capability probe is best-effort
            log.debug("ollama /api/show probe failed: %s", exc)
            return False
        if not isinstance(data, dict):
            return False
        capabilities = data.get("capabilities")
        return isinstance(capabilities, list) and "tools" in capabilities

    def generate(self, request: AIRequest) -> AIResponse:
        self._require_ready()
        if request.tools:
            self._require_capability(Capability.TOOL_CALLING)
        started = datetime.now(UTC)
        try:
            data = transport.request_json(
                _join_url(self._base_url, "/api/chat"),
                method="POST",
                body=self._chat_body(request),
                timeout=request.timeout or self._timeout,
                default_timeout=self._timeout,
            )
        except urllib.error.URLError as exc:
            self._set_state(ProviderState.UNAVAILABLE, "connection failed")
            raise ProviderUnavailableError(
                f"ollama unreachable: {exc}"
            ) from exc
        except transport.HTTPErrorStatus as exc:
            raise ProviderError(f"ollama error: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"ollama malformed response: {exc}") from exc
        if not isinstance(data, dict):
            raise ProviderError("ollama returned a non-object response")
        usage = None
        prompt_tokens = data.get("prompt_eval_count")
        completion_tokens = data.get("eval_count")
        if prompt_tokens is not None or completion_tokens is not None:
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        raw_message = data.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        schemas = {tool.name: tool.input_schema for tool in (request.tools or [])}
        tool_calls = _parse_tool_calls(
            message.get("tool_calls") or data.get("tool_calls"), schemas
        )
        content = str(message.get("content") or "")
        finish_reason = _map_finish_reason(data.get("done_reason"))
        if tool_calls:
            finish_reason = FinishReason.TOOL_CALL
        elapsed = (datetime.now(UTC) - started).total_seconds() * 1000.0
        log.info(
            "ollama generate completed",
            extra={
                "component": "intelligence",
                "provider": self._provider_id,
                "duration_ms": elapsed,
                "model": data.get("model") or self._resolve_model(request.model),
            },
        )
        return AIResponse(
            request_id=request.request_id,
            provider=self._provider_id,
            model=str(data.get("model") or self._resolve_model(request.model)),
            content=content,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls,
        )

    async def stream(self, request: AIRequest) -> Any:
        self._require_ready()
        if request.tools:
            self._require_capability(Capability.TOOL_CALLING)
        schemas = {tool.name: tool.input_schema for tool in (request.tools or [])}
        try:
            response = transport.request_text(
                _join_url(self._base_url, "/api/chat"),
                method="POST",
                body=self._chat_body(request, streaming=True),
                timeout=request.timeout or self._timeout,
                default_timeout=self._timeout,
            )
        except urllib.error.URLError as exc:
            self._set_state(ProviderState.UNAVAILABLE, "connection failed")
            raise ProviderUnavailableError(f"ollama unreachable: {exc}") from exc
        except transport.HTTPErrorStatus as exc:
            raise ProviderError(f"ollama error: {exc}") from exc
        for line in response.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError as exc:
                raise ProviderError(f"ollama malformed stream line: {exc}") from exc
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                yield StreamChunk.error_chunk(str(data["error"]))
                return
            content = data.get("message", {}).get("content")
            if content:
                yield StreamChunk.text_delta(str(content))
            raw_message = data.get("message")
            message = raw_message if isinstance(raw_message, dict) else {}
            raw_calls = data.get("tool_calls") or message.get("tool_calls") or []
            for call in _parse_tool_calls(raw_calls, schemas) or []:
                yield StreamChunk.tool_call_delta(
                    tool_call_id=call.id,
                    name=call.name,
                    arguments=json.dumps(call.arguments),
                )
            if data.get("done"):
                prompt_tokens = data.get("prompt_eval_count")
                completion_tokens = data.get("eval_count")
                usage = None
                if prompt_tokens is not None or completion_tokens is not None:
                    usage = TokenUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                yield StreamChunk.completion(
                    _map_finish_reason(data.get("done_reason")), usage=usage
                )
                return

    def cancel(self, request_id: str) -> None:
        """Best-effort cancel; Ollama polls the client, so cancelling is a no-op.

        In-flight generations are interrupted by closing the connection; the
        adapter has no server-side handle. The capability is advertised so the
        service layer can record the intent without crashing.
        """
        log.info(
            "ollama cancel requested (no server-side handle)",
            extra={"component": "intelligence", "provider": self._provider_id},
        )

    # --- internals ------------------------------------------------------

    def _resolve_model(self, requested: str | None) -> str:
        """Pick the request model, the configured model, or a local chat model.

        The example configuration leaves ``model`` empty on purpose. Ollama
        still requires an explicit model on ``/api/chat``, so an empty config
        must not become an empty request field. Prefer the first non-embedding
        model when no model was configured.
        """
        model = (requested or self._configured_model or "").strip()
        if model:
            return model
        fallback = _pick_default_model(self._models)
        if fallback is not None:
            return fallback
        raise ProviderError(
            "no Ollama model is configured or available; "
            "set ai.providers.local.model or pull a model"
        )

    def _chat_body(self, request: AIRequest, streaming: bool = False) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for message in request.messages:
            messages.append(self._render_message(message))
        body: dict[str, Any] = {
            "model": self._resolve_model(request.model),
            "messages": messages,
            "stream": streaming,
        }
        # Explicit per-request thinking control (Ollama `think`): present only
        # when the caller set a boolean — thinking models (Spark-X2.5, qwen3)
        # otherwise use their model default. Chat disables it for snappy
        # replies; the agent loop keeps deliberation for tool planning.
        think = request.metadata.get("think")
        if isinstance(think, bool):
            body["think"] = think
        if request.temperature is not None:
            body["options"] = {"temperature": request.temperature}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        return body

    def _render_message(self, message: Message) -> dict[str, Any]:
        if isinstance(message.content, str):
            return {"role": message.role.value, "content": message.content}
        # Structured parts: assistant turns may carry tool calls alongside text
        # (rendered as the native Ollama tool_calls protocol, not as text).
        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for part in message.content:
            if part.kind == "text":
                texts.append(part.text)
            else:
                tool_calls.append(
                    {
                        "function": {
                            "name": part.name,
                            "arguments": dict(part.arguments),
                        }
                    }
                )
        rendered: dict[str, Any] = {
            "role": message.role.value,
            "content": "\n".join(texts),
        }
        if tool_calls:
            rendered["tool_calls"] = tool_calls
        return rendered

    def _on_shutdown(self) -> None:
        self._models = ()
        self._tool_capable = False
