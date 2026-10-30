"""Minimal MCP tool server over the Great Sage security pipeline.

The transport is JSON-RPC 2.0. There is intentionally no direct tool
execution here: every ``tools/call`` request becomes a ``ToolRequest`` and
goes through ``ToolService.execute`` (policy, path checks, approval, bounded
execution, audit events).

Security posture:

- only the configured ``mcp.allowed_tools`` are advertised;
- tool names are namespaced (``greatsage_<id>``) to avoid client collisions;
- ``ASK`` requests fail closed because the stdio server installs no approval
  provider;
- protocol errors never expose tracebacks or credentials.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from greatsage import __version__
from greatsage.exceptions import SageError, ToolNotFoundError
from greatsage.tools.models import Tool, ToolRequest
from greatsage.tools.redaction import redact_secrets
from greatsage.tools.service import ToolService

log = logging.getLogger("greatsage.mcp")

JSONRPC_VERSION = "2.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
TOOL_PREFIX = "greatsage_"


class McpProtocolError(Exception):
    """A JSON-RPC request is malformed or unsupported."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def mcp_tool_name(tool_id: str) -> str:
    """Map a Great Sage tool id to a stable, collision-resistant MCP name."""
    return TOOL_PREFIX + tool_id.replace(".", "_")


class McpToolServer:
    """Protocol handler for a bounded, read-only-by-default tool surface."""

    def __init__(
        self,
        tools: ToolService,
        allowed_tools: tuple[str, ...],
        *,
        server_name: str = "great-sage",
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    ) -> None:
        self._tools = tools
        self._allowed_tools = tuple(dict.fromkeys(allowed_tools))
        self._server_name = server_name
        self._protocol_version = protocol_version
        self._session_id = f"mcp_{uuid.uuid4().hex[:12]}"
        self._catalog: dict[str, Tool] | None = None

    # --- catalog -------------------------------------------------------

    def _build_catalog(self) -> dict[str, Tool]:
        if self._catalog is not None:
            return self._catalog
        catalog: dict[str, Tool] = {}
        for tool_id in self._allowed_tools:
            try:
                tool = self._tools.registry().get(tool_id)
            except ToolNotFoundError as exc:
                log.warning(
                    "mcp: configured tool %r is unavailable: %s",
                    tool_id,
                    exc,
                    extra={"component": "mcp"},
                )
                continue
            name = mcp_tool_name(tool_id)
            existing = catalog.get(name)
            if existing is not None:
                log.warning(
                    "mcp: tool name collision for %r and %r",
                    existing.id,
                    tool.id,
                    extra={"component": "mcp"},
                )
                continue
            catalog[name] = tool
        self._catalog = catalog
        return catalog

    def tool_names(self) -> tuple[str, ...]:
        """Names advertised to the client (stable order)."""
        return tuple(sorted(self._build_catalog()))

    def describe(self) -> list[dict[str, Any]]:
        """MCP ``tools/list`` declarations without executing anything."""
        catalog = self._build_catalog()
        return [
            {
                "name": name,
                "description": catalog[name].description,
                "inputSchema": catalog[name].input_schema,
            }
            for name in sorted(catalog)
        ]

    # --- protocol ------------------------------------------------------

    def handle(self, payload: object) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC message.

        Returns ``None`` for notifications (no response is allowed).
        """
        if not isinstance(payload, dict):
            return _error_response(None, -32600, "invalid request")
        request_id = payload.get("id")
        if payload.get("jsonrpc") != JSONRPC_VERSION:
            return _error_response(request_id, -32600, "jsonrpc must be '2.0'")
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            return _error_response(request_id, -32600, "method must be a string")

        is_notification = "id" not in payload
        try:
            result = self._dispatch(method, payload.get("params"))
        except McpProtocolError as exc:
            return None if is_notification else _error_response(
                request_id, exc.code, exc.message
            )
        except Exception as exc:  # protocol boundary: never leak a traceback
            log.warning(
                "mcp: request failed: %s",
                exc,
                extra={"component": "mcp"},
            )
            return None if is_notification else _error_response(
                request_id, -32603, "internal error"
            )
        if is_notification:
            return None
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "result": result,
        }

    def _dispatch(self, method: str, params: object) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.describe()}
        if method == "tools/call":
            return self._tools_call(params)
        if method.startswith("notifications/"):
            return {}
        raise McpProtocolError(-32601, f"method not found: {method}")

    def _initialize(self, params: object) -> dict[str, Any]:
        requested = self._protocol_version
        if isinstance(params, Mapping):
            version = params.get("protocolVersion")
            if isinstance(version, str) and version.strip():
                # Negotiate by echoing the client's version. Capabilities are
                # limited to tools, so a newer transport does not widen access.
                requested = version.strip()
        return {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self._server_name, "version": __version__},
            "instructions": (
                "Great Sage exposes a small, permission-checked tool surface. "
                "Use it only for the current coding/agent task, never for "
                "ordinary conversation. Every call is policy-checked."
            ),
        }

    def _tools_call(self, params: object) -> dict[str, Any]:
        if not isinstance(params, Mapping):
            raise McpProtocolError(-32602, "params must be a mapping")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise McpProtocolError(-32602, "tool name must be a string")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise McpProtocolError(-32602, "tool arguments must be a mapping")

        catalog = self._build_catalog()
        tool = catalog.get(name)
        if tool is None:
            raise McpProtocolError(-32602, f"tool not available: {name}")

        request = ToolRequest(
            request_id=f"mcp_{uuid.uuid4().hex}",
            tool_id=tool.id,
            arguments=dict(arguments),
            source="mcp",
            session_id=self._session_id,
        )
        try:
            result = self._tools.execute(request)
        except SageError as exc:
            return _tool_error(redact_secrets(str(exc)))
        except Exception:
            log.exception("mcp: unexpected tool execution failure")
            return _tool_error("tool execution failed")

        if not result.success:
            return _tool_error(redact_secrets(result.error or "tool failed"))
        text = _render_output(result.output)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }


def _render_output(output: object) -> str:
    if output is None:
        return "Tool completed successfully."
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=True, sort_keys=True, default=str)


def _tool_error(message: str | None) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message or "tool failed"}],
        "isError": True,
    }


def _error_response(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }
