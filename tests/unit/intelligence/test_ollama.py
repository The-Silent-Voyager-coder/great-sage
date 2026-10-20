"""Ollama adapter tests against an in-process fake HTTP server.

Covers: healthy availability + model discovery, generation, streaming,
unavailable endpoint, malformed responses, server errors, timeout, and
capability gating. No real Ollama, no internet.
"""

from __future__ import annotations

import asyncio

import pytest

from greatsage.exceptions import ProviderCapabilityError, ProviderError, ProviderUnavailableError
from greatsage.intelligence.models import (
    AIRequest,
    Message,
    Role,
    ToolCallPart,
    ToolDefinition,
)
from greatsage.intelligence.ollama import OllamaProvider
from greatsage.intelligence.provider import Capability, ProviderState

OLLAMA_TAGS = {
    "models": [
        {"name": "llama3:latest", "size": 100},
        {"name": "llava:latest", "size": 200},
    ]
}

OLLAMA_CHAT = {
    "model": "llama3:latest",
    "message": {"role": "assistant", "content": "hello from ollama"},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 7,
    "eval_count": 5,
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ollama(fake_server) -> OllamaProvider:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    provider = OllamaProvider(base_url=fake_server.url, model="llama3:latest")
    return provider


def test_health_and_model_discovery(ollama: OllamaProvider, fake_server) -> None:
    health = ollama.health()
    assert health.ok
    assert health.state is ProviderState.READY
    assert health.model_loaded == "llama3:latest"
    assert ollama.capabilities().model_ids == ("llama3:latest", "llava:latest")
    assert ollama.capabilities().supports(Capability.STREAMING)
    assert ollama.capabilities().supports(Capability.LOCAL)


def test_init_transitions_ready(ollama: OllamaProvider) -> None:
    ollama.init()
    assert ollama.state is ProviderState.READY


def test_health_recovers_to_ready(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    provider = OllamaProvider(base_url=fake_server.url, model="llama3:latest")
    provider._set_state(ProviderState.UNAVAILABLE, "probe failed earlier")
    health = provider.health()
    assert health.ok is True
    assert provider.state is ProviderState.READY


def test_generate(ollama: OllamaProvider, fake_server) -> None:
    fake_server.route("POST", "/api/chat", 200, OLLAMA_CHAT)
    ollama.init()
    response = ollama.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.provider == "local"
    assert response.content == "hello from ollama"
    assert response.model == "llama3:latest"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 7
    assert response.usage.completion_tokens == 5
    assert response.finish_reason.value == "stop"
    sent = fake_server.requests[-1]
    assert "/api/chat" in sent["path"]
    assert '"stream": false' in sent["body"]
    assert '"role": "user"' in sent["body"]


def test_generate_selects_chat_model_when_unconfigured(fake_server) -> None:
    fake_server.route(
        "GET",
        "/api/tags",
        200,
        {
            "models": [
                {"name": "nomic-embed-text:latest", "size": 100},
                {"name": "llama3:latest", "size": 200},
            ]
        },
    )
    fake_server.route("POST", "/api/chat", 200, OLLAMA_CHAT)
    provider = OllamaProvider(base_url=fake_server.url, model="")
    provider.init()
    response = provider.generate(AIRequest(messages=[Message.user("hi")]))
    assert response.model == "llama3:latest"
    assert '"model": "llama3:latest"' in fake_server.requests[-1]["body"]


def test_health_degraded_without_models(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, {"models": []})
    provider = OllamaProvider(base_url=fake_server.url, model="")
    provider.init()
    assert provider.state is ProviderState.DEGRADED
    with pytest.raises(ProviderUnavailableError):
        provider.generate(AIRequest(messages=[Message.user("hi")]))


def test_generate_system_prompt_and_fields(ollama: OllamaProvider, fake_server) -> None:
    fake_server.route("POST", "/api/chat", 200, OLLAMA_CHAT)
    ollama.init()
    ollama.generate(
        AIRequest(
            messages=[Message.user("hi")],
            system_prompt="be brief",
            temperature=0.4,
            model="llama3:latest",
        )
    )
    sent = fake_server.requests[-1]
    assert '"system"' in sent["body"]
    assert '"temperature": 0.4' in sent["body"]
    assert '"model": "llama3:latest"' in sent["body"]


def test_stream_chunks(ollama: OllamaProvider, fake_server) -> None:
    stream_body = (
        '{"model":"llama3:latest","message":{"role":"assistant","content":"hel"},"done":false}\n'
        '{"model":"llama3:latest","message":{"role":"assistant","content":"lo"},"done":false}\n'
        '{"model":"llama3:latest","message":{"role":"assistant","content":""},"done":true,'
        '"done_reason":"stop","prompt_eval_count":7,"eval_count":5}\n'
    )
    fake_server.route("POST", "/api/chat", 200, stream_body)
    ollama.init()

    async def collect():
        request = AIRequest(messages=[Message.user("hi")])
        return [chunk async for chunk in ollama.stream(request)]

    chunks = run(collect())
    text = "".join(c.text for c in chunks if c.kind == "text")
    assert text == "hello"
    assert chunks[-1].kind == "completion"
    assert chunks[-1].finish_reason.value == "stop"
    assert chunks[-1].usage is not None
    sent = fake_server.requests[-1]
    assert '"stream": true' in sent["body"]


def test_stream_error_line_emits_error_chunk(ollama: OllamaProvider, fake_server) -> None:
    fake_server.route(
        "POST", "/api/chat", 200, '{"error":"model not found"}\n'
    )
    ollama.init()

    async def collect():
        request = AIRequest(messages=[Message.user("hi")])
        return [chunk async for chunk in ollama.stream(request)]

    chunks = run(collect())
    assert chunks[-1].kind == "error"
    assert "model not found" in chunks[-1].error


def test_stream_malformed_line_raises(ollama: OllamaProvider, fake_server) -> None:
    fake_server.route("POST", "/api/chat", 200, "{not json\n")
    ollama.init()

    async def collect():
        request = AIRequest(messages=[Message.user("hi")])
        return [chunk async for chunk in ollama.stream(request)]

    with pytest.raises(ProviderError, match="malformed stream line"):
        run(collect())


def test_unavailable_endpoint_health(fake_server) -> None:
    provider = OllamaProvider(base_url="http://127.0.0.1:1", timeout_seconds=1.0)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.UNAVAILABLE
    assert provider.state is ProviderState.UNAVAILABLE
    with pytest.raises(ProviderUnavailableError):
        provider.generate(AIRequest(messages=[Message.user("hi")]))


def test_server_error_raises_provider_error(ollama: OllamaProvider, fake_server) -> None:
    fake_server.route("POST", "/api/chat", 500, {"error": "internal"})
    ollama.init()
    with pytest.raises(ProviderError, match="HTTP 500"):
        ollama.generate(AIRequest(messages=[Message.user("hi")]))


def test_malformed_generate_raises(ollama: OllamaProvider, fake_server) -> None:
    fake_server.route("POST", "/api/chat", 200, "not-json")
    ollama.init()
    with pytest.raises(ProviderError, match="malformed response"):
        ollama.generate(AIRequest(messages=[Message.user("hi")]))


def test_timeout_hits(fake_server) -> None:
    fake_server.slow("/api/tags")
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    provider = OllamaProvider(base_url=fake_server.url, timeout_seconds=0.5)
    health = provider.health()
    assert health.ok is False
    assert health.state is ProviderState.UNAVAILABLE


def test_tool_calling_not_advertised(ollama: OllamaProvider, fake_server) -> None:
    fake_server.route("POST", "/api/chat", 200, OLLAMA_CHAT)
    ollama.init()
    request = AIRequest(
        messages=[Message.user("hi")],
        tools=[ToolDefinition(name="f", description="d")],
    )
    with pytest.raises(ProviderCapabilityError, match="tool_calling"):
        ollama.generate(request)


def test_generate_failure_degrades_state(ollama: OllamaProvider, fake_server) -> None:
    fake_server.route("POST", "/api/chat", 500, {"error": "boom"})
    ollama.init()
    with pytest.raises(ProviderError):
        ollama.generate(AIRequest(messages=[Message.user("hi")]))
    assert ollama.state in (ProviderState.READY, ProviderState.DEGRADED)


def test_shutdown_before_generate(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    provider = OllamaProvider(base_url=fake_server.url)
    provider.init()
    provider.shutdown()
    assert provider.state is ProviderState.STOPPED
    with pytest.raises(ProviderUnavailableError):
        provider.generate(AIRequest(messages=[Message.user("hi")]))


# --- tool calling (model capability gated) -----------------------------


def _show(capabilities: list[str]) -> dict:
    return {"capabilities": capabilities, "model_info": {}}


def test_tool_capability_advertised_when_model_supports_tools(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    fake_server.route("POST", "/api/show", 200, _show(["completion", "tools"]))
    provider = OllamaProvider(base_url=fake_server.url, model="llama3:latest")
    provider.init()
    assert provider.capabilities().supports(Capability.TOOL_CALLING)


def test_tool_capability_absent_without_tools_capability(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    fake_server.route("POST", "/api/show", 200, _show(["completion"]))
    provider = OllamaProvider(base_url=fake_server.url, model="llama3:latest")
    provider.init()
    assert not provider.capabilities().supports(Capability.TOOL_CALLING)


def test_tool_capability_absent_without_configured_model(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    provider = OllamaProvider(base_url=fake_server.url, model="")
    provider.init()
    assert not provider.capabilities().supports(Capability.TOOL_CALLING)


def test_tool_capability_probed_on_fallback_model(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    fake_server.route("POST", "/api/show", 200, _show(["completion", "tools"]))
    provider = OllamaProvider(base_url=fake_server.url, model="")
    provider.init()
    assert provider.capabilities().supports(Capability.TOOL_CALLING)
    assert provider.health().model_loaded == "llama3:latest"


def test_tool_capability_probe_failure_is_absent(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    fake_server.route("POST", "/api/show", 500, {"error": "old server"})
    provider = OllamaProvider(base_url=fake_server.url, model="llama3:latest")
    provider.init()
    assert provider.state is ProviderState.READY  # health unaffected
    assert not provider.capabilities().supports(Capability.TOOL_CALLING)


TOOL_CHAT = {
    "model": "llama3:latest",
    "message": {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "system.info", "arguments": {"verbose": True}}}
        ],
    },
    "done": True,
    "done_reason": "stop",
}


def _tool_provider(fake_server) -> OllamaProvider:
    fake_server.route("GET", "/api/tags", 200, OLLAMA_TAGS)
    fake_server.route("POST", "/api/show", 200, _show(["completion", "tools"]))
    provider = OllamaProvider(base_url=fake_server.url, model="llama3:latest")
    provider.init()
    return provider


def test_generate_parses_message_tool_calls(fake_server) -> None:
    provider = _tool_provider(fake_server)
    fake_server.route("POST", "/api/chat", 200, TOOL_CHAT)
    request = AIRequest(
        messages=[Message.user("check")],
        tools=[ToolDefinition(name="system.info", description="d")],
    )
    response = provider.generate(request)
    assert response.tool_calls is not None
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "system.info"
    assert call.arguments == {"verbose": True}
    assert call.id == "call_1"
    assert response.finish_reason.value == "tool_call"


def test_generate_parses_string_tool_arguments(fake_server) -> None:
    provider = _tool_provider(fake_server)
    chat = {
        "model": "llama3:latest",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "filesystem.list", "arguments": '{"path": "."}'}}
            ],
        },
        "done": True,
    }
    fake_server.route("POST", "/api/chat", 200, chat)
    response = provider.generate(
        AIRequest(
            messages=[Message.user("list")],
            tools=[ToolDefinition(name="filesystem.list", description="d")],
        )
    )
    assert response.tool_calls is not None
    assert response.tool_calls[0].arguments == {"path": "."}


def test_chat_body_think_metadata_controls_field(fake_server) -> None:
    # Explicit per-request thinking control: a boolean in request metadata
    # becomes Ollama's `think` field; absent means model default.
    provider = _tool_provider(fake_server)
    fake_server.route("POST", "/api/chat", 200, OLLAMA_CHAT)
    provider.generate(
        AIRequest(messages=[Message.user("hi")], metadata={"think": False})
    )
    assert '"think": false' in fake_server.requests[-1]["body"]
    provider.generate(AIRequest(messages=[Message.user("hi")]))
    assert '"think"' not in fake_server.requests[-1]["body"]


def test_chat_body_renders_native_tool_protocol(fake_server) -> None:
    provider = _tool_provider(fake_server)
    fake_server.route("POST", "/api/chat", 200, OLLAMA_CHAT)
    request = AIRequest(
        messages=[
            Message.user("check"),
            Message(
                role=Role.ASSISTANT,
                content=[ToolCallPart(id="call_1", name="system.info", arguments={})],
            ),
            Message.tool("call_1", '{"ok": true}'),
        ],
        tools=[ToolDefinition(name="system.info", description="d")],
    )
    provider.generate(request)
    body = fake_server.requests[-1]["body"]
    assert '"tool_calls"' in body
    assert '"role": "tool"' in body
    assert "tool_call_id" not in body  # Ollama has no id field on tool results
    assert '"parameters"' in body  # tool definitions rendered
