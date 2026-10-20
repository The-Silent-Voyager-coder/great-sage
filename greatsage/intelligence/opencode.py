"""OpenCode provider adapter (remote code execution, server v2 API).

Connectivity/health via ``GET /api/info``, session lifecycle via
``POST /api/session`` + ``DELETE /api/session/{id}``, sync generation via
``POST /api/session/{id}/prompt`` (``{text}``) followed by polling
``GET /api/session/{id}/message`` until an ``idle`` message lands, async
delegation via the same prompt call plus polling the message timeline and
``GET /api/session/{id}/permission`` for pending approvals, abort via
``POST /api/session/{id}/interrupt``. No authority delegation, no agent
loop — the provider merely routes model calls to an OpenCode server.

Model selectors look like ``provider/model#variant``
(e.g. ``opencode/muse-spark-1.3-contributor-free#xhigh``); a bare model id
resolves to the ``opencode`` provider namespace. Variants are server-side
effort presets — unknown variants produce a server error, never a
silent downgrade.

Authentication is HTTP Basic (``username:password``). The username defaults
to ``opencode`` (the server default); the password comes from the
environment variable named by ``api_key_env`` and is never read from
source. Both names are configurable so server-side overrides
(``OPENCODE_SERVER_USERNAME`` / ``OPENCODE_SERVER_PASSWORD``) keep working.

The v2 OpenAPI spec lives at ``GET /openapi.json`` (server root); it is
fetched best-effort at init for metadata only.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from greatsage.delegation.models import DelegationEvent, DelegationEventKind
from greatsage.exceptions import ProviderError, ProviderUnavailableError
from greatsage.intelligence import transport
from greatsage.intelligence.models import (
    AIRequest,
    AIResponse,
    FinishReason,
    TokenUsage,
)
from greatsage.intelligence.provider import (
    AIProvider,
    Capability,
    ProviderCapabilities,
    ProviderHealth,
    ProviderState,
)

log = logging.getLogger("greatsage.intelligence.opencode")

OPENCODE_DEFAULT_URL = "http://127.0.0.1:4096"
OPENCODE_DEFAULT_USERNAME = "opencode"

#: The OpenAPI spec is metadata-only; a transient failure gets a few more
#: chances (each bounded) instead of being permanently written off.
_SPEC_FETCH_ATTEMPTS = 3

_POLL_SECONDS = 2.0


class OpenCodeProvider(AIProvider):
    """Adapter for an OpenCode v2 server (http://127.0.0.1:4096 by default).

    Config fields (from ai.providers.opencode): base_url, api_key_env
    (server password), username_env (server username, default ``opencode``),
    enabled, timeout_seconds. The provider id stays 'opencode'.
    """

    def __init__(
        self,
        *,
        base_url: str = OPENCODE_DEFAULT_URL,
        api_key_env: str = "",
        username_env: str = "",
        model: str = "",
        timeout_seconds: float = 60.0,
        provider_id: str = "opencode",
    ) -> None:
        super().__init__(provider_id)
        self._base_url = base_url if base_url else OPENCODE_DEFAULT_URL
        self._api_key: str | None = None
        self._api_key_env = api_key_env
        self._username: str | None = None
        self._username_env = username_env
        self._default_model = model.strip()
        self._timeout = timeout_seconds if timeout_seconds > 0 else 60.0
        self._spec_fetched = False
        self._spec_attempts = 0
        self._seen_message_ids: dict[str, set[str]] = {}
        self._known_permission_ids: dict[str, set[str]] = {}
        self._prompt_message_ids: dict[str, str] = {}

    # --- public surface ------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        caps = {
            Capability.TEXT_GENERATION,
            Capability.REMOTE,
            Capability.CANCELLATION,
            Capability.CODE_EXECUTION,
            Capability.DELEGATION,
        }
        return ProviderCapabilities(
            capabilities=frozenset(caps),
            model_ids=(),
        )

    def _probe_health(self) -> ProviderHealth:
        try:
            data = transport.request_json(
                f"{self._api_root()}/info",
                timeout=min(self._timeout, 5.0),
                default_timeout=5.0,
                headers=self._auth_headers(),
            )
        except urllib.error.URLError as exc:
            self._set_state(ProviderState.UNAVAILABLE, "connection failed")
            return ProviderHealth(
                provider_id=self._provider_id,
                ok=False,
                state=ProviderState.UNAVAILABLE,
                detail=f"connection failed: {str(exc)[:120]}",
            )
        except (transport.HTTPErrorStatus, ValueError) as exc:
            self._set_state(ProviderState.DEGRADED, "malformed response")
            return ProviderHealth(
                provider_id=self._provider_id,
                ok=False,
                state=ProviderState.DEGRADED,
                detail=f"malformed response: {str(exc)[:120]}",
            )
        self._maybe_fetch_spec()
        if not isinstance(data, dict) or not isinstance(data.get("version"), str):
            self._set_state(ProviderState.DEGRADED, "unexpected /api/info response (no version)")
            return ProviderHealth(
                provider_id=self._provider_id,
                ok=False,
                state=ProviderState.DEGRADED,
                detail="unexpected /api/info response (no version)",
            )
        return ProviderHealth(
            provider_id=self._provider_id,
            ok=True,
            state=ProviderState.READY,
            detail=f"opencode v2 server: {data.get('version')}",
        )

    def generate(self, request: AIRequest) -> AIResponse:
        self._require_ready()
        if request.tools:
            self._require_capability(Capability.TOOL_CALLING)
        started = datetime.now(UTC)
        session_id = self._create_session(model=request.model or self._default_model or None)
        try:
            self._send_prompt(session_id, self._prompt_text(request))
            assistant = self._await_assistant(
                session_id, timeout=request.timeout or self._timeout
            )
        except (ProviderUnavailableError, ProviderError):
            raise
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(f"opencode unreachable: {exc}") from exc
        except transport.HTTPErrorStatus as exc:
            raise ProviderError(f"opencode error: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"opencode malformed response: {exc}") from exc
        finally:
            self._cleanup_session(session_id)
        elapsed = (datetime.now(UTC) - started).total_seconds() * 1000.0
        log.info(
            "opencode generate completed",
            extra={
                "component": "intelligence",
                "provider": self._provider_id,
                "duration_ms": elapsed,
                "session_id": session_id,
            },
        )
        return self._response_from_assistant(request, assistant)

    def cancel(self, request_id: str) -> None:
        """Best-effort session abort is a no-op here (no in-flight handle)."""
        log.info(
            "opencode cancel requested (no in-flight handle)",
            extra={"component": "intelligence", "provider": self._provider_id},
        )

    # --- delegation surface (Phase 5B) --------------------------------

    def create_session(self) -> str:
        """Create a fresh OpenCode session and return its id."""
        return self._create_session()

    def send_delegation_prompt(
        self,
        session_id: str,
        prompt: str,
        working_directory: str | None = None,
    ) -> None:
        """Send the delegation prompt for asynchronous execution.

        `working_directory` has already passed Great Sage path security in the
        DelegationManager. The v2 prompt API carries no directory field, so
        scoping stays a Great Sage-side permission decision (every executor
        file/shell action is allow/ask/deny gated); it is not forwarded.
        """
        try:
            self._send_prompt(session_id, prompt)
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(f"opencode unreachable: {exc}") from exc
        except transport.HTTPErrorStatus as exc:
            raise ProviderError(f"opencode prompt error: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"opencode malformed prompt response: {exc}") from exc

    def iter_session_events(self, session_id: str) -> Iterator[DelegationEvent]:
        """Yield fresh delegation events from one poll sweep.

        The v2 server exposes a single global SSE bus; instead of holding a
        long-lived stream here, each call polls the session message timeline
        plus the pending-permission list and yields what is new since the
        previous sweep. The DelegationManager re-calls on every loop pass, so
        its deadline/reconnect policy applies unchanged. Transport failures
        raise (never yield partial state) so the manager can reconnect.
        """
        try:
            messages = self._session_messages(session_id)
            pending = self._pending_permissions(session_id)
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(
                f"opencode event poll unreachable: {exc}"
            ) from exc
        except transport.HTTPErrorStatus as exc:
            raise ProviderError(f"opencode event poll error: {exc}") from exc
        seen = self._seen_message_ids.setdefault(session_id, set())
        yielded = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or message_id in seen:
                continue
            seen.add(message_id)
            event = self._message_event(session_id, message)
            if event is not None:
                yielded = True
                yield event
        known = self._known_permission_ids.setdefault(session_id, set())
        for item in pending:
            if not isinstance(item, dict):
                continue
            permission_id = (
                _string(item.get("id"))
                or _string(item.get("requestID"))
                or _string(item.get("requestId"))
            )
            if permission_id is None or permission_id in known:
                continue
            known.add(permission_id)
            yielded = True
            yield DelegationEvent(
                kind=DelegationEventKind.PERMISSION_REQUESTED,
                session_id=session_id,
                message=_string(item.get("description"))
                or _string(item.get("message"))
                or _string(item.get("text")),
                metadata={
                    "permission_id": permission_id,
                    "action": (
                        _string(item.get("action"))
                        or _string(item.get("tool"))
                        or _string(item.get("command"))
                        or "unknown"
                    ),
                    "path": _string(item.get("path")) or "",
                },
            )
        if not yielded:
            # Quiet sweep: the executor is still working. Emit a heartbeat
            # so the manager does not mistake a healthy run for a lost
            # stream (NOTE events are informational and ignored there).
            yield DelegationEvent(
                kind=DelegationEventKind.NOTE,
                session_id=session_id,
                message=None,
                metadata={"poll": "quiet"},
            )

    def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        approved: bool,
        remember: bool | None = None,
    ) -> None:
        """Answer one permission request with the Great Sage decision."""
        decision = "reject"
        if approved:
            decision = "always" if remember else "once"
        try:
            transport.request_json(
                f"{self._api_root()}/session/"
                f"{urllib.parse.quote(session_id)}/permission/"
                f"{urllib.parse.quote(permission_id)}/reply",
                method="POST",
                body={"decision": decision},
                timeout=min(self._timeout, 10.0),
                default_timeout=10.0,
                headers=self._auth_headers(),
            )
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(f"opencode unreachable: {exc}") from exc
        except transport.HTTPErrorStatus as exc:
            raise ProviderError(f"opencode permission error: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"opencode malformed permission response: {exc}") from exc

    def abort_session(self, session_id: str) -> None:
        """Interrupt a delegated session (best-effort; never raises)."""
        try:
            transport.request_json(
                f"{self._api_root()}/session/"
                f"{urllib.parse.quote(session_id)}/interrupt",
                method="POST",
                body={},
                timeout=min(self._timeout, 10.0),
                default_timeout=10.0,
                headers=self._auth_headers(),
            )
        except Exception as exc:  # abort is best-effort
            log.debug("opencode session interrupt failed: %s", exc)

    def get_session_diff(self, session_id: str) -> dict[str, Any] | None:
        """Fetch the file-change diff of a session (best-effort)."""
        query = ""
        prompt_id = self._prompt_message_ids.get(session_id)
        if prompt_id:
            query = f"?from={urllib.parse.quote(prompt_id)}"
        try:
            data = transport.request_json(
                f"{self._api_root()}/session/"
                f"{urllib.parse.quote(session_id)}/diff{query}",
                timeout=min(self._timeout, 10.0),
                default_timeout=10.0,
                headers=self._auth_headers(),
            )
        except Exception as exc:  # diff is best-effort review material
            log.debug("opencode session diff unavailable: %s", exc)
            return None
        files: list[Any] = []
        if isinstance(data, dict):
            raw = data.get("data", data.get("files", []))
            if isinstance(raw, list):
                files = raw
        elif isinstance(data, list):
            files = data
        return {"files": [entry for entry in files if isinstance(entry, dict)]}

    def dispose_session(self, session_id: str) -> None:
        """Release a delegated session (best-effort)."""
        self._seen_message_ids.pop(session_id, None)
        self._known_permission_ids.pop(session_id, None)
        self._prompt_message_ids.pop(session_id, None)
        self._cleanup_session(session_id)

    # --- internals ------------------------------------------------------

    def _api_root(self) -> str:
        return f"{self._base_url.rstrip('/')}/api"

    def _message_event(
        self, session_id: str, message: dict[str, Any]
    ) -> DelegationEvent | None:
        kind = message.get("type")
        if kind == "user":
            return None  # our own prompt echo
        if kind == "idle":
            outcome = _string(message.get("outcome"))
            if outcome is not None and outcome != "succeeded":
                return DelegationEvent(
                    kind=DelegationEventKind.FAILED,
                    session_id=session_id,
                    message=f"executor session ended: {outcome}",
                    metadata={"outcome": outcome},
                )
            return DelegationEvent(
                kind=DelegationEventKind.COMPLETED,
                session_id=session_id,
                message=_string(message.get("summary")),
                metadata={"outcome": outcome or "succeeded"},
            )
        if kind == "assistant":
            error = message.get("error")
            if isinstance(error, dict):
                return DelegationEvent(
                    kind=DelegationEventKind.FAILED,
                    session_id=session_id,
                    message=_string(error.get("message")) or "executor reported failure",
                    metadata={},
                )
            text = _assistant_text(message)
            return DelegationEvent(
                kind=DelegationEventKind.PROGRESS,
                session_id=session_id,
                message=text if text else None,
                metadata={"message_id": str(message.get("id", ""))},
            )
        return DelegationEvent(
            kind=DelegationEventKind.NOTE,
            session_id=session_id,
            message=None,
            metadata={"message_type": str(kind)},
        )

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key_env:
            return {}
        password = self._load_api_key()
        if not password:
            return {}
        username = self._load_username()
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _load_api_key(self) -> str | None:
        import os

        if self._api_key is not None:
            return self._api_key
        self._api_key = os.environ.get(self._api_key_env) or None
        return self._api_key

    def _load_username(self) -> str:
        import os

        if self._username is not None:
            return self._username
        if self._username_env:
            self._username = os.environ.get(self._username_env) or OPENCODE_DEFAULT_USERNAME
        else:
            self._username = OPENCODE_DEFAULT_USERNAME
        return self._username

    def _maybe_fetch_spec(self) -> None:
        if self._spec_fetched or self._spec_attempts >= _SPEC_FETCH_ATTEMPTS:
            return
        self._spec_attempts += 1
        try:
            spec = transport.request_json(
                f"{self._base_url.rstrip('/')}/openapi.json",
                timeout=min(self._timeout, 5.0),
                default_timeout=5.0,
                headers=self._auth_headers(),
            )
            if isinstance(spec, dict) and str(spec.get("openapi", "")).startswith("3."):
                self._spec_fetched = True
                log.info(
                    "opencode spec fetched",
                    extra={
                        "component": "intelligence",
                        "provider": self._provider_id,
                        "openapi": str(spec.get("openapi")),
                    },
                )
        except Exception as exc:  # spec is optional
            log.debug(
                "opencode /openapi.json unavailable (attempt %d/%d): %s",
                self._spec_attempts,
                _SPEC_FETCH_ATTEMPTS,
                exc,
            )

    def _create_session(self, model: str | None = None) -> str:
        body: dict[str, Any] = {}
        ref = self._split_model(model or self._default_model or None)
        if ref is not None:
            body["model"] = ref
        try:
            data = transport.request_json(
                f"{self._api_root()}/session",
                method="POST",
                body=body,
                timeout=min(self._timeout, 10.0),
                default_timeout=10.0,
                headers=self._auth_headers(),
            )
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(f"opencode unreachable: {exc}") from exc
        except (transport.HTTPErrorStatus, ValueError) as exc:
            raise ProviderError(f"opencode session error: {exc}") from exc
        session_id = _session_id_from(data)
        if session_id is None:
            raise ProviderError("opencode did not return a session id")
        return session_id

    def _send_prompt(self, session_id: str, text: str) -> None:
        try:
            data = transport.request_json(
                f"{self._api_root()}/session/"
                f"{urllib.parse.quote(session_id)}/prompt",
                method="POST",
                body={"text": text},
                timeout=min(self._timeout, 30.0),
                default_timeout=30.0,
                headers=self._auth_headers(),
            )
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(f"opencode unreachable: {exc}") from exc
        except transport.HTTPErrorStatus as exc:
            raise ProviderError(f"opencode prompt error: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"opencode malformed prompt response: {exc}") from exc
        message_id = _prompt_id_from(data)
        if message_id is not None:
            self._prompt_message_ids[session_id] = message_id

    def _session_messages(self, session_id: str) -> list[Any]:
        data = transport.request_json(
            f"{self._api_root()}/session/"
            f"{urllib.parse.quote(session_id)}/message",
            timeout=min(self._timeout, 15.0),
            default_timeout=15.0,
            headers=self._auth_headers(),
        )
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return list(data["data"])
        if isinstance(data, list):
            return list(data)
        raise ProviderError("opencode returned an unexpected message timeline")

    def _pending_permissions(self, session_id: str) -> list[Any]:
        try:
            data = transport.request_json(
                f"{self._api_root()}/session/"
                f"{urllib.parse.quote(session_id)}/permission",
                timeout=min(self._timeout, 10.0),
                default_timeout=10.0,
                headers=self._auth_headers(),
            )
        except transport.HTTPErrorStatus as exc:
            if exc.status == 404:
                return []  # older servers / sessions without permission support
            raise
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return list(data["data"])
        if isinstance(data, list):
            return list(data)
        return []

    def _await_assistant(self, session_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(timeout, 1.0)
        last_error: str = "executor produced no assistant message"
        while True:
            try:
                messages = self._session_messages(session_id)
            except (urllib.error.URLError, transport.HTTPErrorStatus, ValueError) as exc:
                last_error = str(exc)[:200]
                messages = []
            idle_seen = False
            idle_outcome: str | None = None
            assistant: dict[str, Any] | None = None
            for message in messages:
                if not isinstance(message, dict):
                    continue
                kind = message.get("type")
                if kind == "idle":
                    idle_seen = True
                    idle_outcome = _string(message.get("outcome"))
                elif kind == "assistant":
                    assistant = message
            if assistant is not None:
                if idle_seen and idle_outcome not in (None, "succeeded"):
                    # Fail fast: the session ended without success and will
                    # never produce another assistant message.
                    raise ProviderError(
                        f"opencode executor session ended: {idle_outcome}"
                    )
                error = assistant.get("error")
                if isinstance(error, dict):
                    raise ProviderError(
                        f"opencode executor error: "
                        f"{_string(error.get('message')) or 'unknown'}"
                    )
                if idle_seen:
                    return assistant
            elif idle_seen and idle_outcome not in (None, "succeeded"):
                raise ProviderError(f"opencode executor session ended: {idle_outcome}")
            if time.monotonic() >= deadline:
                raise ProviderError(f"opencode timed out waiting for a reply: {last_error}")
            time.sleep(_POLL_SECONDS)

    def _response_from_assistant(
        self, request: AIRequest, assistant: dict[str, Any]
    ) -> AIResponse:
        content = _assistant_text(assistant) or None
        usage: TokenUsage | None = None
        tokens = assistant.get("tokens")
        if isinstance(tokens, dict):
            prompt = tokens.get("input")
            completion = tokens.get("output")
            prompt_n = prompt if isinstance(prompt, int) else None
            completion_n = completion if isinstance(completion, int) else None
            if prompt_n is not None or completion_n is not None:
                # Never fabricate counts: the total is only known when both
                # halves were reported (TokenUsage contract).
                total = (
                    prompt_n + completion_n
                    if prompt_n is not None and completion_n is not None
                    else None
                )
                usage = TokenUsage(
                    prompt_tokens=prompt_n,
                    completion_tokens=completion_n,
                    total_tokens=total,
                )
        model_info = assistant.get("model")
        model_echo = request.model or self._default_model or "opencode"
        if model_echo == "opencode" and isinstance(model_info, dict):
            model_id = model_info.get("id")
            if isinstance(model_id, str) and model_id:
                model_echo = f"{model_info.get('providerID', 'opencode')}/{model_id}"
        finish = _map_finish_reason(assistant.get("finish"))
        return AIResponse(
            request_id=request.request_id,
            provider=self._provider_id,
            model=model_echo,
            content=str(content) if content is not None else None,
            finish_reason=finish,
            usage=usage,
        )

    def _cleanup_session(self, session_id: str) -> None:
        try:
            transport.request_json(
                f"{self._api_root()}/session/{urllib.parse.quote(session_id)}",
                method="DELETE",
                timeout=min(self._timeout, 5.0),
                default_timeout=5.0,
                headers=self._auth_headers(),
            )
        except Exception as exc:  # cleanup is best-effort
            log.debug("opencode session cleanup failed: %s", exc)

    @staticmethod
    def _split_model(model: str | None) -> dict[str, str] | None:
        """Split ``provider/model#variant`` into a v2 model reference.

        A bare model id resolves to the ``opencode`` provider namespace; an
        empty model means "server default" (no model key is sent).
        """
        if not model or not model.strip():
            return None
        rest, _, variant = model.strip().partition("#")
        if "/" in rest:
            provider, _, model_id = rest.partition("/")
        else:
            provider, model_id = "opencode", rest
        if not model_id.strip():
            return None
        ref = {"providerID": provider.strip(), "id": model_id.strip()}
        if variant.strip():
            ref["variant"] = variant.strip()
        return ref

    def _prompt_text(self, request: AIRequest) -> str:
        utterances = [
            (message.role.value, self._render_content(message))
            for message in request.messages
            if message.role.value != "system"
        ]
        if len(utterances) == 1 and not request.system_prompt:
            return utterances[0][1]
        lines = []
        if request.system_prompt:
            lines.append(f"system: {request.system_prompt}")
        lines.extend(f"{role}: {content}" for role, content in utterances)
        return "\n".join(lines)

    def _render_content(self, message: Any) -> str:
        if isinstance(message.content, str):
            return message.content
        parts: list[str] = []
        for part in message.content:
            if part.kind == "text":
                parts.append(part.text)
            else:
                parts.append(
                    f"[tool_call name={part.name} args={json.dumps(part.arguments)}]"
                )
        return "\n".join(parts)

    def _on_shutdown(self) -> None:
        self._spec_fetched = False
        self._spec_attempts = 0


def _prompt_id_from(data: Any) -> str | None:
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("id"), str):
            return str(inner["id"])
        if isinstance(data.get("id"), str):
            return str(data["id"])
    return None


def _session_id_from(data: Any) -> str | None:
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("id"), str):
            return str(inner["id"])
        if isinstance(data.get("id"), str):
            return str(data["id"])
        if isinstance(data.get("sessionID"), str):
            return str(data["sessionID"])
    return None


def _assistant_text(assistant: dict[str, Any]) -> str:
    """Join non-empty text parts; reasoning/tool blobs are never included."""
    content = assistant.get("content")
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return "\n".join(texts)


def _map_finish_reason(reason: Any) -> FinishReason:
    if reason is None:
        return FinishReason.STOP
    normalized = str(reason).lower().replace("-", "")
    if "length" in normalized:
        return FinishReason.LENGTH
    if "tool" in normalized:
        return FinishReason.TOOL_CALL
    if "cancel" in normalized:
        return FinishReason.CANCELLED
    if "filter" in normalized:
        return FinishReason.CONTENT_FILTERED
    if "error" in normalized:
        return FinishReason.ERROR
    return FinishReason.STOP


def _string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
