"""OpenCode adapter tests against an in-process fake HTTP server.

Covers /global/health (both `ok` and `healthy` shapes), /doc spec fetch,
session creation, sync message prompt (`POST /session/:id/message` with
`model{providerID,modelID}` + `variant`), response parsing, malformed /
timeout / server-error paths, abort, capabilities, and the delegation
surface (sessions, SSE events, permissions). No real OpenCode server,
no internet.
"""

from __future__ import annotations

import asyncio

import pytest

from greatsage.delegation.models import DelegationEventKind
from greatsage.exceptions import ProviderCapabilityError, ProviderError, ProviderUnavailableError
from greatsage.intelligence.models import AIRequest, Message, ToolDefinition
from greatsage.intelligence.opencode import OpenCodeProvider
from greatsage.intelligence.provider import Capability, ProviderState

OPENCODE_HEALTH = {"ok": True, "service": "opencode", "version": "0.1.0"}
OPENCODE_SPEC = {"openapi": "3.1.0", "info": {"title": "opencode server"}}
SESSION_ID = "sess-123"
MESSAGE_RESPONSE = {
    "info": {"modelID": "opencode-model", "stopReason": "stop"},
    "parts": [
        {"type": "step-start"},
        {"type": "text", "text": "code answer"},
        {"type": "step-finish"},
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def opencode(fake_server) -> OpenCodeProvider:
    fake_server.route("GET", "/global/health", 200, OPENCODE_HEALTH)
    fake_server.route("GET", "/doc", 200, OPENCODE_SPEC)
    provider = OpenCodeProvider(base_url=fake_server.url)
    return provider


def test_health_and_capabilities(opencode: OpenCodeProvider, fake_server) -> None:
    health = opencode.health()
    assert health.ok
    assert health.state is ProviderState.READY
    caps = opencode.capabilities()
    assert caps.supports(Capability.REMOTE)
    assert caps.supports(Capability.CODE_EXECUTION)
    assert caps.supports(Capability.CANCELLATION)
    assert not caps.supports(Capability.STREAMING)


def test_health_unavailable(fake_server) -> None:
    provider = OpenCodeProvider(base_url="http://127.0.0.1:1", timeout_seconds=1.0)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.UNAVAILABLE


def test_health_malformed(fake_server) -> None:
    fake_server.route("GET", "/global/health", 200, "not-json")
    provider = OpenCodeProvider(base_url=fake_server.url)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.DEGRADED


def test_health_negative_ok_field(fake_server) -> None:
    fake_server.route("GET", "/global/health", 200, {"ok": False, "reason": "busy"})
    provider = OpenCodeProvider(base_url=fake_server.url)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.DEGRADED


def test_health_healthy_shape(fake_server) -> None:
    # Live servers (1.18+) answer {"healthy": true, ...} instead of {"ok": ...}.
    fake_server.route("GET", "/global/health", 200, {"healthy": True, "version": "1.18.30"})
    provider = OpenCodeProvider(base_url=fake_server.url)
    health = provider.health()
    assert health.ok
    assert health.state is ProviderState.READY


def test_generate_sessions_and_prompt(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 200, {"id": SESSION_ID})
    fake_server.route(
        "POST", f"/session/{SESSION_ID}/message", 200, MESSAGE_RESPONSE
    )
    fake_server.route("DELETE", f"/session/{SESSION_ID}", 200, {"ok": True})
    opencode.init()
    response = opencode.generate(AIRequest(messages=[Message.user("write code")]))
    assert response.provider == "opencode"
    assert response.content == "code answer"
    assert response.model == "opencode-model"
    assert response.finish_reason.value == "stop"
    assert response.usage is not None
    assert response.usage.total_tokens == 14
    methods = [req["method"] for req in fake_server.requests]
    assert "POST" in methods
    assert "DELETE" in methods
    prompt_request = next(
        r for r in fake_server.requests if r["path"].endswith("/message")
    )
    assert '"type": "text"' in prompt_request["body"]
    assert "write code" in prompt_request["body"]
    assert '"model"' not in prompt_request["body"]  # server default when unset


def test_generate_system_prompt(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 200, {"id": SESSION_ID})
    fake_server.route(
        "POST", f"/session/{SESSION_ID}/message", 200, MESSAGE_RESPONSE
    )
    fake_server.route("DELETE", f"/session/{SESSION_ID}", 200, {"ok": True})
    opencode.init()
    opencode.generate(
        AIRequest(
            messages=[Message.user("hi")],
            system_prompt="you are a code assistant",
            model="opencode/muse-spark-1.3-contributor-free#xhigh",
        )
    )
    prompt_request = next(r for r in fake_server.requests if r["path"].endswith("/message"))
    assert '"system": "you are a code assistant"' in prompt_request["body"]
    assert '"providerID": "opencode"' in prompt_request["body"]
    assert '"modelID": "muse-spark-1.3-contributor-free"' in prompt_request["body"]
    assert '"variant": "xhigh"' in prompt_request["body"]


def test_generate_usage_absent_is_none(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 200, {"id": SESSION_ID})
    fake_server.route(
        "POST",
        f"/session/{SESSION_ID}/message",
        200,
        {"info": {}, "parts": [{"type": "text", "text": "no usage here"}]},
    )
    fake_server.route("DELETE", f"/session/{SESSION_ID}", 200, {"ok": True})
    opencode.init()
    response = opencode.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.usage is None
    assert response.content == "no usage here"


def test_generate_ignores_non_text_parts(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 200, {"id": SESSION_ID})
    fake_server.route(
        "POST",
        f"/session/{SESSION_ID}/message",
        200,
        {
            "info": {"modelID": "m"},
            "parts": [
                {"type": "step-start"},
                {"type": "reasoning", "text": ""},
                {"type": "text", "text": "final"},
                {"type": "step-finish"},
            ],
        },
    )
    fake_server.route("DELETE", f"/session/{SESSION_ID}", 200, {"ok": True})
    opencode.init()
    response = opencode.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.content == "final"


def test_default_model_fallback(fake_server) -> None:
    fake_server.route("GET", "/global/health", 200, OPENCODE_HEALTH)
    fake_server.route("GET", "/doc", 200, OPENCODE_SPEC)
    fake_server.route("POST", "/session", 200, {"id": SESSION_ID})
    fake_server.route(
        "POST",
        f"/session/{SESSION_ID}/message",
        200,
        {"info": {"modelID": "m"}, "parts": [{"type": "text", "text": "hi"}]},
    )
    fake_server.route("DELETE", f"/session/{SESSION_ID}", 200, {"ok": True})
    provider = OpenCodeProvider(
        base_url=fake_server.url,
        model="opencode/muse-spark-1.3-contributor-free#medium",
    )
    provider.init()
    response = provider.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.model == "opencode/muse-spark-1.3-contributor-free#medium"
    prompt_request = next(r for r in fake_server.requests if r["path"].endswith("/message"))
    assert '"modelID": "muse-spark-1.3-contributor-free"' in prompt_request["body"]
    assert '"variant": "medium"' in prompt_request["body"]


def test_split_model_shapes() -> None:
    from greatsage.intelligence.opencode import OpenCodeProvider as P

    assert P._split_model(None) == (None, None)
    assert P._split_model("") == (None, None)
    assert P._split_model("opencode/m#xhigh") == (
        {"providerID": "opencode", "modelID": "m"}, "xhigh",
    )
    assert P._split_model("opencode/m") == ({"providerID": "opencode", "modelID": "m"}, None)
    assert P._split_model("m") == ({"providerID": "opencode", "modelID": "m"}, None)


def test_generate_session_creation_failure(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 500, {"error": "boom"})
    opencode.init()
    with pytest.raises(ProviderError, match="session"):
        opencode.generate(AIRequest(messages=[Message.user("hi")]))


def test_generate_missing_session_id(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 200, {})
    opencode.init()
    with pytest.raises(ProviderError, match="session id"):
        opencode.generate(AIRequest(messages=[Message.user("hi")]))


def test_generate_unreachable(fake_server) -> None:
    provider = OpenCodeProvider(base_url="http://127.0.0.1:1", timeout_seconds=1.0)
    provider.init()
    with pytest.raises(ProviderUnavailableError):
        provider.generate(AIRequest(messages=[Message.user("hi")]))


def test_generate_server_error(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 200, {"id": SESSION_ID})
    fake_server.route("POST", f"/session/{SESSION_ID}/message", 500, {"error": "x"})
    fake_server.route("DELETE", f"/session/{SESSION_ID}", 200, {"ok": True})
    opencode.init()
    with pytest.raises(ProviderError, match="HTTP 500"):
        opencode.generate(AIRequest(messages=[Message.user("hi")]))


def test_tools_unsupported_explicit_error(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 200, {"id": SESSION_ID})
    fake_server.route("POST", f"/session/{SESSION_ID}/message", 200, MESSAGE_RESPONSE)
    fake_server.route("DELETE", f"/session/{SESSION_ID}", 200, {"ok": True})
    opencode.init()
    request = AIRequest(
        messages=[Message.user("hi")],
        tools=[ToolDefinition(name="f", description="d")],
    )
    with pytest.raises(ProviderCapabilityError, match="tool_calling"):
        opencode.generate(request)


def test_stream_not_supported_explicit_error(opencode: OpenCodeProvider) -> None:
    opencode.init()

    async def collect():
        request = AIRequest(messages=[Message.user("hi")])
        return [chunk async for chunk in opencode.stream(request)]

    with pytest.raises(ProviderCapabilityError, match="streaming"):
        run(collect())


def test_cancel_noop(opencode: OpenCodeProvider) -> None:
    opencode.init()
    opencode.cancel("req-1")  # must not raise


def test_api_key_env_used(fake_server, monkeypatch) -> None:
    fake_server.route("GET", "/global/health", 200, OPENCODE_HEALTH)
    monkeypatch.setenv("GREATSAGE_OPENCODE_TOKEN", "sk-test")
    provider = OpenCodeProvider(
        base_url=fake_server.url, api_key_env="GREATSAGE_OPENCODE_TOKEN"
    )
    provider.init()
    provider._auth_headers()  # force load
    assert provider._api_key == "sk-test"


def test_timeout_hits(fake_server) -> None:
    fake_server.route("GET", "/global/health", 200, OPENCODE_HEALTH)
    fake_server.slow("/global/health")
    provider = OpenCodeProvider(base_url=fake_server.url, timeout_seconds=0.5)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.UNAVAILABLE


# --- delegation surface -----------------------------------------------


def test_capabilities_include_delegation(opencode: OpenCodeProvider) -> None:
    caps = opencode.capabilities()
    assert caps.supports(Capability.DELEGATION)


def test_create_session(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session", 200, {"id": "sess-delegated"})
    session_id = opencode.create_session()
    assert session_id == "sess-delegated"


def test_send_delegation_prompt_ok(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session/sess-1/prompt_async", 204, "")
    opencode.init()
    opencode.send_delegation_prompt("sess-1", "do the work", "/tmp/project")
    prompt_request = next(r for r in fake_server.requests if "prompt_async" in r["path"])
    assert '"type": "text"' in prompt_request["body"]
    assert '"text": "do the work"' in prompt_request["body"]


def test_send_delegation_prompt_omits_cwd_when_absent(
    opencode: OpenCodeProvider, fake_server
) -> None:
    # The 1.18 message API carries no directory field: scoping stays a
    # JARVIS-side permission decision, never a wire field.
    fake_server.route("POST", "/session/sess-1/prompt_async", 204, "")
    opencode.init()
    opencode.send_delegation_prompt("sess-1", "hello")
    prompt_request = next(r for r in fake_server.requests if "prompt_async" in r["path"])
    assert "working_directory" not in prompt_request["body"]


def test_send_delegation_prompt_failure(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session/sess-1/prompt_async", 500, {"error": "boom"})
    opencode.init()
    with pytest.raises(ProviderError, match="prompt"):
        opencode.send_delegation_prompt("sess-1", "do it")


SSE_EVENTS = "\n".join(
    [
        'event: session.run.started',
        'data: {"text": "first thought"}',
        "",
        'event: session.request.permission',
        'data: {"permissionID": "perm-7", "key": {"permissions": {"action": '
        '"write", "path": "/tmp/a.txt"}}, "description": "allow write"}',
        "",
        'event: session.run.completed',
        'data: {"status": "completed", "summary": "all done"}',
        "",
    ]
)


def test_iter_session_events_translates_kinds(opencode, fake_server) -> None:
    fake_server.route("GET", "/session/sess-7/event", 200, SSE_EVENTS)
    events = list(opencode.iter_session_events("sess-7"))
    assert [e.kind for e in events] == [
        DelegationEventKind.PROGRESS,
        DelegationEventKind.PERMISSION_REQUESTED,
        DelegationEventKind.COMPLETED,
    ]
    progress = events[0]
    assert progress.message == "first thought"
    permission = events[1]
    assert permission.metadata["permission_id"] == "perm-7"
    assert permission.metadata["action"] == "write"
    assert permission.metadata["path"] == "/tmp/a.txt"
    assert permission.message == "allow write"
    completed = events[2]
    assert completed.message == "all done"


def test_iter_session_events_error_and_cancel(opencode, fake_server) -> None:
    raw = "\n".join(
        [
            'event: session.error',
            'data: {"error": "invalid token"}',
            "",
            'event: session.cancelled',
            'data: {"cancelled": true}',
            "",
        ]
    )
    fake_server.route("GET", "/session/sess-8/event", 200, raw)
    events = list(opencode.iter_session_events("sess-8"))
    assert [e.kind for e in events] == [
        DelegationEventKind.FAILED,
        DelegationEventKind.CANCELLED,
    ]
    assert events[0].message == "invalid token"


def test_iter_session_events_malformed_skipped(opencode, fake_server) -> None:
    raw = "\n".join(
        [
            'event: session.thing',
            'data: not-json',
            "",
            'event: session.thing',
            'data: [1, 2, 3]',
            "",
        ]
    )
    fake_server.route("GET", "/session/sess-9/event", 200, raw)
    events = list(opencode.iter_session_events("sess-9"))
    assert len(events) == 1
    assert events[0].kind is DelegationEventKind.NOTE


def test_iter_session_events_non_dict_data_is_note(opencode, fake_server) -> None:
    raw = "\n".join(
        [
            'event: session.thing',
            'data: "just a string"',
            "",
        ]
    )
    fake_server.route("GET", "/session/sess-10/event", 200, raw)
    events = list(opencode.iter_session_events("sess-10"))
    assert len(events) == 1
    assert events[0].kind is DelegationEventKind.NOTE


def test_respond_permission(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session/sess-1/permissions/perm-7", 200, {"ok": True})
    fake_server.route("POST", "/session/sess-1/permissions/perm-8", 200, {"ok": True})
    opencode.init()
    opencode.respond_permission("sess-1", "perm-7", True)
    opencode.respond_permission("sess-1", "perm-8", False, remember=True)
    bodies = [r["body"] for r in fake_server.requests if "permissions" in r["path"]]
    assert '"response": true' in bodies[0]
    assert '"response": false' in bodies[1]
    assert '"remember": true' in bodies[1]


def test_abort_session_never_raises(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/session/sess-1/abort", 200, {"ok": True})
    fake_server.route("POST", "/session/missing/abort", 404, {"error": "nope"})
    opencode.init()
    opencode.abort_session("sess-1")
    opencode.abort_session("missing")  # must not raise


def test_get_session_diff(opencode: OpenCodeProvider, fake_server) -> None:
    diff = {"files": [{"path": "/tmp/a.txt", "added": 1}]}
    fake_server.route("GET", "/session/sess-1/diff", 200, diff)
    assert opencode.get_session_diff("sess-1") == diff


def test_get_session_diff_unavailable_is_none(opencode, fake_server) -> None:
    fake_server.route("GET", "/session/sess-1/diff", 500, {"error": "x"})
    assert opencode.get_session_diff("sess-1") is None


def test_dispose_session_deletes(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("DELETE", "/session/sess-1", 200, {"ok": True})
    opencode.init()
    opencode.dispose_session("sess-1")
    methods = [r["method"] for r in fake_server.requests]
    assert "DELETE" in methods


def test_iter_session_events_http_error_wrapped(opencode: OpenCodeProvider, fake_server) -> None:
    # A non-2xx on the event endpoint must surface as a ProviderError so the
    # DelegationManager can treat it as connection loss — never an unhandled
    # HTTPErrorStatus escaping to the caller.
    fake_server.route("GET", "/session/sess-11/event", 500, {"error": "boom"})
    opencode.init()
    with pytest.raises(ProviderError, match="event stream"):
        list(opencode.iter_session_events("sess-11"))


def test_iter_session_events_unreachable_wrapped(opencode: OpenCodeProvider, fake_server) -> None:
    # A connection failure on the event endpoint maps to ProviderUnavailableError.
    provider = OpenCodeProvider(base_url="http://127.0.0.1:1", timeout_seconds=0.5)
    provider.init()
    with pytest.raises(ProviderUnavailableError, match="event stream"):
        list(provider.iter_session_events("sess-12"))
