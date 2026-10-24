"""Protocol and pipeline tests for the local MCP server."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, cast

from greatsage.configuration.loader import load_config
from greatsage.exceptions import ToolNotFoundError, ToolPermissionDeniedError
from greatsage.mcp import McpToolServer, mcp_tool_name, serve_stdio
from greatsage.tools.models import ToolCategory, ToolResult
from greatsage.tools.service import ToolService
from tests.unit.tools.stub_tools import StubProbe


class FakeRegistry:
    def __init__(self, tools: list[StubProbe]) -> None:
        self._tools = {tool.id: tool for tool in tools}

    def get(self, tool_id: str) -> StubProbe:
        try:
            return self._tools[tool_id]
        except KeyError as exc:
            raise ToolNotFoundError(f"unknown tool: {tool_id}") from exc


class FakeToolService:
    def __init__(
        self,
        registry: FakeRegistry,
        *,
        result: ToolResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._registry = registry
        self._result = result or ToolResult("", "probe.echo", True, output={"ok": True})
        self._error = error
        self.requests: list[Any] = []

    def registry(self) -> FakeRegistry:
        return self._registry

    def execute(self, request: Any) -> ToolResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._result


def _request(method: str, request_id: object = 1, params: object = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def test_initialize_echoes_client_protocol_version() -> None:
    service = FakeToolService(
        FakeRegistry([StubProbe("probe.echo", "echo", ToolCategory.FILESYSTEM)])
    )
    server = McpToolServer(cast(ToolService, service), ("probe.echo",))

    response = server.handle(
        _request("initialize", params={"protocolVersion": "2025-06-18"})
    )

    assert response is not None
    result = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"]["listChanged"] is False


def test_tools_list_is_allowlisted_and_namespaced() -> None:
    service = FakeToolService(
        FakeRegistry([StubProbe("probe.echo", "echo", ToolCategory.FILESYSTEM)])
    )
    server = McpToolServer(
        cast(ToolService, service), ("probe.echo", "missing.tool")
    )

    response = server.handle(_request("tools/list"))

    assert response is not None
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["greatsage_probe_echo"]
    assert tools[0]["inputSchema"]["properties"]["value"]["type"] == "integer"


def test_tools_call_uses_tool_service_pipeline() -> None:
    service = FakeToolService(
        FakeRegistry([StubProbe("probe.echo", "echo", ToolCategory.FILESYSTEM)])
    )
    server = McpToolServer(cast(ToolService, service), ("probe.echo",))

    response = server.handle(
        _request(
            "tools/call",
            params={"name": "greatsage_probe_echo", "arguments": {"value": 7}},
        )
    )

    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"ok": True}
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.tool_id == "probe.echo"
    assert request.source == "mcp"
    assert request.session_id is not None
    assert request.arguments == {"value": 7}


def test_tools_call_denial_is_reported_as_tool_error() -> None:
    service = FakeToolService(
        FakeRegistry([StubProbe("probe.echo", "echo", ToolCategory.FILESYSTEM)]),
        error=ToolPermissionDeniedError("no approval provider configured"),
    )
    server = McpToolServer(cast(ToolService, service), ("probe.echo",))

    response = server.handle(
        _request("tools/call", params={"name": "greatsage_probe_echo"})
    )

    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert "no approval provider" in result["content"][0]["text"]


def test_unknown_tool_is_a_protocol_error_and_never_executes() -> None:
    service = FakeToolService(FakeRegistry([]))
    server = McpToolServer(cast(ToolService, service), ("probe.echo",))

    response = server.handle(
        _request("tools/call", params={"name": "greatsage_probe_echo"})
    )

    assert response is not None
    assert response["error"]["code"] == -32602
    assert service.requests == []


def test_notification_gets_no_response() -> None:
    service = FakeToolService(FakeRegistry([]))
    server = McpToolServer(cast(ToolService, service), ())

    response = server.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )

    assert response is None


def test_stdio_transport_handles_bad_json_and_requests() -> None:
    service = FakeToolService(
        FakeRegistry([StubProbe("probe.echo", "echo", ToolCategory.FILESYSTEM)])
    )
    server = McpToolServer(cast(ToolService, service), ("probe.echo",))
    source = io.StringIO(
        "\n"
        + json.dumps(_request("initialize", request_id=1))
        + "\n"
        + "not-json\n"
        + json.dumps(
            _request(
                "tools/call",
                request_id=2,
                params={"name": "greatsage_probe_echo"},
            )
        )
        + "\n"
    )
    sink = io.StringIO()

    assert serve_stdio(server, stdin=source, stdout=sink) == 0

    responses = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, None, 2]
    assert responses[1]["error"]["code"] == -32700


def test_real_tool_pipeline_serves_system_info(valid_config_yaml: Path) -> None:
    config = load_config(valid_config_yaml).config
    service = ToolService()
    service.start(config)
    try:
        server = McpToolServer(service, ("system.info",))
        list_response = server.handle(_request("tools/list"))
        call_response = server.handle(
            _request(
                "tools/call",
                params={"name": "greatsage_system_info", "arguments": {}},
            )
        )
    finally:
        service.shutdown()

    assert list_response is not None
    assert list_response["result"]["tools"][0]["name"] == "greatsage_system_info"
    assert call_response is not None
    result = call_response["result"]
    assert result["isError"] is False
    assert "sage_version" in result["content"][0]["text"]


def test_real_pipeline_ask_fails_closed(
    valid_config_yaml: Path, tmp_path: Path
) -> None:
    config = load_config(valid_config_yaml).config
    service = ToolService()
    service.start(config)
    target = tmp_path / "workspaces" / "write-probe.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        server = McpToolServer(service, ("filesystem.write",))
        response = server.handle(
            _request(
                "tools/call",
                params={
                    "name": "greatsage_filesystem_write",
                    "arguments": {"path": str(target), "content": "probe"},
                },
            )
        )
    finally:
        service.shutdown()

    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert "approval" in result["content"][0]["text"]
    assert not target.exists()


def test_tool_name_mapping_is_stable() -> None:
    assert mcp_tool_name("filesystem.read") == "greatsage_filesystem_read"
    assert mcp_tool_name("system.info") == "greatsage_system_info"
