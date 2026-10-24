"""OpenCode v2 adapter tests against an in-process fake HTTP server.

Covers /api/info health (version shape), /openapi.json spec fetch, session
creation (``{data: {id}}`` envelope + v2 model refs), async prompt
(``POST /api/session/:id/prompt`` with ``{text}``) followed by message
timeline polling until ``idle``, malformed / timeout / server-error paths,
interrupt, capabilities, and the delegation surface (poll-based events,
permission reply decisions, diff envelope). No real OpenCode server,
no internet.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from greatsage.delegation.models import DelegationEventKind
from greatsage.exceptions import ProviderCapabilityError, ProviderError, ProviderUnavailableError
from greatsage.intelligence.models import AIRequest, Message, ToolDefinition
from greatsage.intelligence.opencode import OpenCodeProvider
from greatsage.intelligence.provider import Capability, ProviderState

OPENCODE_INFO = {"version": "2.0.29", "pid": 1234, "urls": [], "paths": {"tmp": "/tmp"}}
OPENCODE_SPEC = {"openapi": "3.1.0", "info": {"title": "opencode HttpApi"}}
SESSION_ID = "ses-123"
ASSISTANT_MESSAGE = {
    "id": "msg-a1",
    "time": {"created": 1},
    "type": "assistant",
    "agent": "build",
    "model": {"id": "opencode-model", "providerID": "opencode"},
    "content": [{"type": "text", "text": "code answer"}],
    "finish": "stop",
    "tokens": {"input": 11, "output": 3},
}
IDLE_MESSAGE = {"id": "msg-idle", "time": {"created": 2}, "type": "idle", "outcome": "succeeded"}
TIMELINE_DONE = {"data": [IDLE_MESSAGE, ASSISTANT_MESSAGE], "cursor": {}}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def opencode(fake_server) -> OpenCodeProvider:
    fake_server.route("GET", "/api/info", 200, OPENCODE_INFO)
    fake_server.route("GET", "/openapi.json", 200, OPENCODE_SPEC)
    provider = OpenCodeProvider(base_url=fake_server.url)
    return provider


def _routes_for_generate(fake_server) -> None:
    fake_server.route("POST", "/api/session", 200, {"data": {"id": SESSION_ID}})
    fake_server.route("POST", f"/api/session/{SESSION_ID}/prompt", 200, {"data": {}})
    fake_server.route("GET", f"/api/session/{SESSION_ID}/message", 200, TIMELINE_DONE)
    fake_server.route("DELETE", f"/api/session/{SESSION_ID}", 204, "")


def test_health_and_capabilities(opencode: OpenCodeProvider, fake_server) -> None:
    health = opencode.health()
    assert health.ok
    assert health.state is ProviderState.READY
    assert "2.0.29" in health.detail
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
    fake_server.route("GET", "/api/info", 200, "not-json")
    provider = OpenCodeProvider(base_url=fake_server.url)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.DEGRADED


def test_health_missing_version_is_degraded(fake_server) -> None:
    fake_server.route("GET", "/api/info", 200, {"ok": True})
    provider = OpenCodeProvider(base_url=fake_server.url)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.DEGRADED
    # Internal state must agree with the reported health (no silent drift).
    assert provider.state is ProviderState.DEGRADED


def test_health_unauthorized_is_degraded(fake_server) -> None:
    fake_server.route("GET", "/api/info", 401, {"error": "unauthorized"})
    provider = OpenCodeProvider(base_url=fake_server.url)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.DEGRADED


def test_generate_prompts_then_polls(opencode: OpenCodeProvider, fake_server) -> None:
    _routes_for_generate(fake_server)
    opencode.init()
    response = opencode.generate(AIRequest(messages=[Message.user("write code")]))
    assert response.provider == "opencode"
    assert response.content == "code answer"
    assert response.model == "opencode/opencode-model"
    assert response.finish_reason.value == "stop"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 3
    assert response.usage.total_tokens == 14
    methods = [req["method"] for req in fake_server.requests]
    assert "POST" in methods
    assert "DELETE" in methods
    prompt_request = next(r for r in fake_server.requests if r["path"].endswith("/prompt"))
    assert '"text": "write code"' in prompt_request["body"]
    assert '"model"' not in prompt_request["body"]  # model goes to session create
    assert '"system"' not in prompt_request["body"]  # v2 prompt takes text only


def test_generate_system_prompt_folded_into_text(
    opencode: OpenCodeProvider, fake_server
) -> None:
    _routes_for_generate(fake_server)
    opencode.init()
    opencode.generate(
        AIRequest(
            messages=[Message.user("hi")],
            system_prompt="you are a code assistant",
            model="opencode/muse-spark-1.3-contributor-free#xhigh",
        )
    )
    prompt_request = next(r for r in fake_server.requests if r["path"].endswith("/prompt"))
    assert "system: you are a code assistant" in prompt_request["body"]


def test_chat_generate_denies_all_tools_at_session_boundary(
    opencode: OpenCodeProvider, fake_server
) -> None:
    _routes_for_generate(fake_server)
    opencode.init()
    opencode.generate(
        AIRequest(
            messages=[Message.user("hello")],
            metadata={"source": "chat"},
        )
    )
    create_request = next(
        r for r in fake_server.requests if r["path"] == "/api/session"
    )
    body = json.loads(create_request["body"])
    assert body["permissions"] == [
        {"action": "*", "resource": "*", "effect": "deny"}
    ]


def test_non_chat_generate_keeps_normal_session_permissions(
    opencode: OpenCodeProvider, fake_server
) -> None:
    _routes_for_generate(fake_server)
    opencode.init()
    opencode.generate(AIRequest(messages=[Message.user("do work")]))
    create_request = next(
        r for r in fake_server.requests if r["path"] == "/api/session"
    )
    body = json.loads(create_request["body"])
    assert "permissions" not in body


def test_generate_model_ref_in_session_create(
    opencode: OpenCodeProvider, fake_server
) -> None:
    _routes_for_generate(fake_server)
    opencode.init()
    opencode.generate(
        AIRequest(
            messages=[Message.user("hi")],
            model="opencode/muse-spark-1.3-contributor-free#xhigh",
        )
    )
    create_request = next(
        r for r in fake_server.requests if r["path"].endswith("/api/session")
    )
    assert '"providerID": "opencode"' in create_request["body"]
    assert '"id": "muse-spark-1.3-contributor-free"' in create_request["body"]
    assert '"variant": "xhigh"' in create_request["body"]


def test_generate_usage_absent_is_none(opencode: OpenCodeProvider, fake_server) -> None:
    assistant = {
        "id": "m",
        "time": {"created": 1},
        "type": "assistant",
        "content": [{"type": "text", "text": "no usage here"}],
    }
    fake_server.route("POST", "/api/session", 200, {"data": {"id": SESSION_ID}})
    fake_server.route("POST", f"/api/session/{SESSION_ID}/prompt", 200, {"data": {}})
    fake_server.route(
        "GET", f"/api/session/{SESSION_ID}/message", 200, {"data": [IDLE_MESSAGE, assistant]}
    )
    fake_server.route("DELETE", f"/api/session/{SESSION_ID}", 204, "")
    opencode.init()
    response = opencode.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.usage is None
    assert response.content == "no usage here"


def test_generate_partial_usage_never_fabricates_total(
    opencode: OpenCodeProvider, fake_server
) -> None:
    # Only the input count is reported: the total must stay None, never a
    # fabricated sum (TokenUsage contract).
    assistant = {
        "id": "m",
        "time": {"created": 1},
        "type": "assistant",
        "content": [{"type": "text", "text": "partial"}],
        "tokens": {"input": 7},
    }
    fake_server.route("POST", "/api/session", 200, {"data": {"id": SESSION_ID}})
    fake_server.route("POST", f"/api/session/{SESSION_ID}/prompt", 200, {"data": {}})
    fake_server.route(
        "GET", f"/api/session/{SESSION_ID}/message", 200, {"data": [IDLE_MESSAGE, assistant]}
    )
    fake_server.route("DELETE", f"/api/session/{SESSION_ID}", 204, "")
    opencode.init()
    response = opencode.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.usage is not None
    assert response.usage.prompt_tokens == 7
    assert response.usage.completion_tokens is None
    assert response.usage.total_tokens is None


def test_generate_skips_reasoning_blobs(opencode: OpenCodeProvider, fake_server) -> None:
    assistant = {
        "id": "m",
        "time": {"created": 1},
        "type": "assistant",
        "content": [
            {"type": "reasoning", "text": "ENCRYPTED-BLOB"},
            {"type": "text", "text": "final"},
        ],
    }
    fake_server.route("POST", "/api/session", 200, {"data": {"id": SESSION_ID}})
    fake_server.route("POST", f"/api/session/{SESSION_ID}/prompt", 200, {"data": {}})
    fake_server.route(
        "GET", f"/api/session/{SESSION_ID}/message", 200, {"data": [IDLE_MESSAGE, assistant]}
    )
    fake_server.route("DELETE", f"/api/session/{SESSION_ID}", 204, "")
    opencode.init()
    response = opencode.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.content == "final"


def test_generate_executor_error(opencode: OpenCodeProvider, fake_server) -> None:
    assistant = {
        "id": "m",
        "time": {"created": 1},
        "type": "assistant",
        "content": [],
        "error": {"message": "model blew up"},
    }
    fake_server.route("POST", "/api/session", 200, {"data": {"id": SESSION_ID}})
    fake_server.route("POST", f"/api/session/{SESSION_ID}/prompt", 200, {"data": {}})
    fake_server.route(
        "GET", f"/api/session/{SESSION_ID}/message", 200, {"data": [IDLE_MESSAGE, assistant]}
    )
    fake_server.route("DELETE", f"/api/session/{SESSION_ID}", 204, "")
    opencode.init()
    with pytest.raises(ProviderError, match="model blew up"):
        opencode.generate(AIRequest(messages=[Message.user("hi")]))


def test_generate_failed_idle_outcome_fails_fast(
    opencode: OpenCodeProvider, fake_server
) -> None:
    # A non-succeeded idle outcome means no further assistant message will
    # arrive: fail fast with the session outcome instead of polling to timeout.
    aborted = {"id": "i2", "time": {"created": 2}, "type": "idle", "outcome": "aborted"}
    fake_server.route("POST", "/api/session", 200, {"data": {"id": SESSION_ID}})
    fake_server.route("POST", f"/api/session/{SESSION_ID}/prompt", 200, {"data": {}})
    fake_server.route(
        "GET", f"/api/session/{SESSION_ID}/message", 200, {"data": [aborted]}
    )
    fake_server.route("DELETE", f"/api/session/{SESSION_ID}", 204, "")
    opencode.init()
    with pytest.raises(ProviderError, match="session ended: aborted"):
        opencode.generate(AIRequest(messages=[Message.user("hi")]))


def test_default_model_fallback(fake_server) -> None:
    fake_server.route("GET", "/api/info", 200, OPENCODE_INFO)
    fake_server.route("GET", "/openapi.json", 200, OPENCODE_SPEC)
    _routes_for_generate(fake_server)
    provider = OpenCodeProvider(
        base_url=fake_server.url,
        model="opencode/muse-spark-1.3-contributor-free#medium",
    )
    provider.init()
    response = provider.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.model == "opencode/muse-spark-1.3-contributor-free#medium"
    create_request = next(
        r for r in fake_server.requests if r["path"].endswith("/api/session")
    )
    assert '"id": "muse-spark-1.3-contributor-free"' in create_request["body"]
    assert '"variant": "medium"' in create_request["body"]


def test_split_model_shapes() -> None:
    from greatsage.intelligence.opencode import OpenCodeProvider as P

    assert P._split_model(None) is None
    assert P._split_model("") is None
    assert P._split_model("opencode/m#xhigh") == (
        {"providerID": "opencode", "id": "m", "variant": "xhigh"}
    )
    assert P._split_model("opencode/m") == ({"providerID": "opencode", "id": "m"})
    assert P._split_model("m") == ({"providerID": "opencode", "id": "m"})


def test_generate_session_creation_failure(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/api/session", 500, {"error": "boom"})
    opencode.init()
    with pytest.raises(ProviderError, match="session"):
        opencode.generate(AIRequest(messages=[Message.user("hi")]))


def test_generate_missing_session_id(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/api/session", 200, {"data": {}})
    opencode.init()
    with pytest.raises(ProviderError, match="session id"):
        opencode.generate(AIRequest(messages=[Message.user("hi")]))


def test_generate_unreachable(fake_server) -> None:
    provider = OpenCodeProvider(base_url="http://127.0.0.1:1", timeout_seconds=1.0)
    provider.init()
    with pytest.raises(ProviderUnavailableError):
        provider.generate(AIRequest(messages=[Message.user("hi")]))


def test_generate_prompt_server_error(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/api/session", 200, {"data": {"id": SESSION_ID}})
    fake_server.route("POST", f"/api/session/{SESSION_ID}/prompt", 500, {"error": "x"})
    fake_server.route("DELETE", f"/api/session/{SESSION_ID}", 204, "")
    opencode.init()
    with pytest.raises(ProviderError, match="HTTP 500"):
        opencode.generate(AIRequest(messages=[Message.user("hi")]))


def test_tools_unsupported_explicit_error(opencode: OpenCodeProvider, fake_server) -> None:
    _routes_for_generate(fake_server)
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


def test_basic_auth_from_env(fake_server, monkeypatch) -> None:
    fake_server.route("GET", "/api/info", 200, OPENCODE_INFO)
    monkeypatch.setenv("GREATSAGE_OPENCODE_TOKEN", "sk-test")
    provider = OpenCodeProvider(
        base_url=fake_server.url, api_key_env="GREATSAGE_OPENCODE_TOKEN"
    )
    provider.init()
    headers = provider._auth_headers()
    assert headers["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(headers["Authorization"][len("Basic "):]).decode()
    assert decoded == "opencode:sk-test"


def test_basic_auth_username_override(fake_server, monkeypatch) -> None:
    fake_server.route("GET", "/api/info", 200, OPENCODE_INFO)
    monkeypatch.setenv("GREATSAGE_OPENCODE_TOKEN", "sk-test")
    monkeypatch.setenv("GREATSAGE_OPENCODE_USER", "sage")
    provider = OpenCodeProvider(
        base_url=fake_server.url,
        api_key_env="GREATSAGE_OPENCODE_TOKEN",
        username_env="GREATSAGE_OPENCODE_USER",
    )
    provider.init()
    headers = provider._auth_headers()
    decoded = base64.b64decode(headers["Authorization"][len("Basic "):]).decode()
    assert decoded == "sage:sk-test"


def test_no_auth_headers_without_env(opencode: OpenCodeProvider) -> None:
    assert opencode._auth_headers() == {}


def test_spec_fetch_retries_before_giving_up(fake_server) -> None:
    # A failed /openapi.json fetch must not be written off forever: the
    # provider retries a bounded number of times across health probes.
    fake_server.route("GET", "/api/info", 200, OPENCODE_INFO)
    fake_server.route("GET", "/openapi.json", 500, {"error": "transient"})
    provider = OpenCodeProvider(base_url=fake_server.url)
    provider.health()
    provider.health()
    assert provider._spec_attempts == 2  # still retrying
    fake_server.route("GET", "/openapi.json", 200, OPENCODE_SPEC)
    provider.health()
    assert provider._spec_fetched  # recovered on a later probe
    attempts = provider._spec_attempts
    provider.health()
    assert provider._spec_attempts == attempts  # fetched once, then cached


def test_spec_fetch_gives_up_after_cap(fake_server) -> None:
    fake_server.route("GET", "/api/info", 200, OPENCODE_INFO)
    fake_server.route("GET", "/openapi.json", 500, {"error": "down"})
    provider = OpenCodeProvider(base_url=fake_server.url)
    for _ in range(6):
        provider.health()
    assert not provider._spec_fetched
    assert provider._spec_attempts == 3  # bounded, never hammering


def test_timeout_hits(fake_server) -> None:
    fake_server.route("GET", "/api/info", 200, OPENCODE_INFO)
    fake_server.slow("/api/info")
    provider = OpenCodeProvider(base_url=fake_server.url, timeout_seconds=0.5)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.UNAVAILABLE


# --- delegation surface -----------------------------------------------


def test_capabilities_include_delegation(opencode: OpenCodeProvider) -> None:
    caps = opencode.capabilities()
    assert caps.supports(Capability.DELEGATION)


def test_create_session(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/api/session", 200, {"data": {"id": "sess-delegated"}})
    session_id = opencode.create_session()
    assert session_id == "sess-delegated"


def test_send_delegation_prompt_ok(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/api/session/sess-1/prompt", 200, {"data": {}})
    opencode.init()
    opencode.send_delegation_prompt("sess-1", "do the work", "/tmp/project")
    prompt_request = next(r for r in fake_server.requests if r["path"].endswith("/prompt"))
    assert '"text": "do the work"' in prompt_request["body"]
    assert "working_directory" not in prompt_request["body"]


def test_send_delegation_prompt_omits_cwd_when_absent(
    opencode: OpenCodeProvider, fake_server
) -> None:
    # The v2 prompt API carries no directory field: scoping stays a
    # Great Sage-side permission decision, never a wire field.
    fake_server.route("POST", "/api/session/sess-1/prompt", 200, {"data": {}})
    opencode.init()
    opencode.send_delegation_prompt("sess-1", "hello")
    prompt_request = next(r for r in fake_server.requests if r["path"].endswith("/prompt"))
    assert "working_directory" not in prompt_request["body"]


def test_send_delegation_prompt_failure(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/api/session/sess-1/prompt", 500, {"error": "boom"})
    opencode.init()
    with pytest.raises(ProviderError, match="prompt"):
        opencode.send_delegation_prompt("sess-1", "do it")


def _poll_routes(fake_server, messages, permissions=None) -> None:
    fake_server.route(
        "GET", "/api/session/sess-7/message", 200, {"data": messages, "cursor": {}}
    )
    fake_server.route(
        "GET", "/api/session/sess-7/permission", 200, {"data": permissions or []}
    )


def test_iter_session_events_translates_kinds(opencode, fake_server) -> None:
    _poll_routes(
        fake_server,
        [
            {"id": "u1", "type": "user", "text": "do it"},
            {
                "id": "a1",
                "type": "assistant",
                "content": [{"type": "text", "text": "first thought"}],
            },
            {"id": "i1", "type": "idle", "outcome": "succeeded", "summary": "all done"},
        ],
        [
            {
                "id": "perm-7",
                "action": "write",
                "path": "/tmp/a.txt",
                "description": "allow write",
            }
        ],
    )
    events = list(opencode.iter_session_events("sess-7"))
    assert [e.kind for e in events] == [
        DelegationEventKind.PROGRESS,
        DelegationEventKind.COMPLETED,
        DelegationEventKind.PERMISSION_REQUESTED,
    ]
    progress = events[0]
    assert progress.message == "first thought"
    permission = events[2]
    assert permission.metadata["permission_id"] == "perm-7"
    assert permission.metadata["action"] == "write"
    assert permission.metadata["path"] == "/tmp/a.txt"
    assert permission.message == "allow write"
    completed = events[1]
    assert completed.message == "all done"


def test_iter_session_events_quiet_sweep_heartbeats(opencode, fake_server) -> None:
    # A sweep with nothing new must heartbeat (NOTE), never go quiet:
    # quiet is normal mid-run; only transport failures end the stream.
    _poll_routes(fake_server, [{"id": "u1", "type": "user", "text": "hi"}])
    events = list(opencode.iter_session_events("sess-7"))
    assert len(events) == 1
    assert events[0].kind is DelegationEventKind.NOTE


def test_iter_session_events_dedupes_seen(opencode, fake_server) -> None:
    _poll_routes(fake_server, [IDLE_MESSAGE, ASSISTANT_MESSAGE])
    first = list(opencode.iter_session_events("sess-7"))
    assert len(first) == 2
    second = list(opencode.iter_session_events("sess-7"))
    assert len(second) == 1
    assert second[0].kind is DelegationEventKind.NOTE


def test_iter_session_events_failed_outcome(opencode, fake_server) -> None:
    _poll_routes(
        fake_server, [{"id": "i9", "type": "idle", "outcome": "aborted"}]
    )
    events = list(opencode.iter_session_events("sess-7"))
    assert [e.kind for e in events] == [DelegationEventKind.FAILED]
    assert "aborted" in (events[0].message or "")


def test_iter_session_events_assistant_error(opencode, fake_server) -> None:
    _poll_routes(
        fake_server,
        [
            {
                "id": "a9",
                "type": "assistant",
                "content": [],
                "error": {"message": "invalid token"},
            }
        ],
    )
    events = list(opencode.iter_session_events("sess-7"))
    assert [e.kind for e in events] == [DelegationEventKind.FAILED]
    assert events[0].message == "invalid token"


def test_iter_session_events_unknown_type_is_note(opencode, fake_server) -> None:
    _poll_routes(fake_server, [{"id": "c1", "type": "compaction"}])
    events = list(opencode.iter_session_events("sess-7"))
    assert len(events) == 1
    assert events[0].kind is DelegationEventKind.NOTE


def test_respond_permission(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/api/session/sess-1/permission/perm-7/reply", 204, "")
    fake_server.route("POST", "/api/session/sess-1/permission/perm-8/reply", 204, "")
    opencode.init()
    opencode.respond_permission("sess-1", "perm-7", True)
    opencode.respond_permission("sess-1", "perm-8", False, remember=True)
    bodies = [r["body"] for r in fake_server.requests if r["path"].endswith("/reply")]
    assert '"decision": "once"' in bodies[0]
    assert '"decision": "reject"' in bodies[1]


def test_respond_permission_remember_maps_to_always(
    opencode: OpenCodeProvider, fake_server
) -> None:
    fake_server.route("POST", "/api/session/sess-1/permission/perm-9/reply", 204, "")
    opencode.init()
    opencode.respond_permission("sess-1", "perm-9", True, remember=True)
    bodies = [r["body"] for r in fake_server.requests if r["path"].endswith("/reply")]
    assert '"decision": "always"' in bodies[0]


def test_interrupt_session_never_raises(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("POST", "/api/session/sess-1/interrupt", 200, {"ok": True})
    fake_server.route("POST", "/api/session/missing/interrupt", 404, {"error": "nope"})
    opencode.init()
    opencode.abort_session("sess-1")
    opencode.abort_session("missing")  # must not raise


def test_get_session_diff(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route(
        "GET",
        "/api/session/sess-1/diff",
        200,
        {"data": [{"file": "/tmp/a.txt", "status": "modified"}]},
    )
    assert opencode.get_session_diff("sess-1") == {
        "files": [{"file": "/tmp/a.txt", "status": "modified"}]
    }


def test_get_session_diff_scopes_to_prompt_turn(
    opencode: OpenCodeProvider, fake_server
) -> None:
    fake_server.route(
        "POST", "/api/session/sess-1/prompt", 200, {"data": {"id": "msg-u1"}}
    )
    fake_server.route(
        "GET",
        "/api/session/sess-1/diff?from=msg-u1",
        200,
        {"data": [{"file": "/tmp/a.txt", "status": "added"}]},
    )
    opencode.init()
    opencode.send_delegation_prompt("sess-1", "do it")
    diff = opencode.get_session_diff("sess-1")
    assert diff is not None
    assert diff["files"] == [{"file": "/tmp/a.txt", "status": "added"}]


def test_get_session_diff_unavailable_is_none(opencode, fake_server) -> None:
    fake_server.route("GET", "/api/session/sess-1/diff", 500, {"error": "x"})
    assert opencode.get_session_diff("sess-1") is None


def test_dispose_session_deletes(opencode: OpenCodeProvider, fake_server) -> None:
    fake_server.route("DELETE", "/api/session/sess-1", 204, "")
    opencode.init()
    opencode.dispose_session("sess-1")
    methods = [r["method"] for r in fake_server.requests]
    assert "DELETE" in methods


def test_iter_session_events_http_error_wrapped(opencode, fake_server) -> None:
    # A non-2xx on the poll endpoints must surface as a ProviderError so the
    # DelegationManager can treat it as connection loss — never an unhandled
    # HTTPErrorStatus escaping to the caller.
    fake_server.route("GET", "/api/session/sess-11/message", 500, {"error": "boom"})
    fake_server.route("GET", "/api/session/sess-11/permission", 200, {"data": []})
    opencode.init()
    with pytest.raises(ProviderError, match="event poll"):
        list(opencode.iter_session_events("sess-11"))


def test_iter_session_events_unreachable_wrapped(opencode, fake_server) -> None:
    # A connection failure on the poll endpoints maps to ProviderUnavailableError.
    provider = OpenCodeProvider(base_url="http://127.0.0.1:1", timeout_seconds=0.5)
    provider.init()
    with pytest.raises(ProviderUnavailableError, match="event poll"):
        list(provider.iter_session_events("sess-12"))
