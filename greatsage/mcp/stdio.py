"""Newline-delimited JSON-RPC stdio transport for the MCP server."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

from greatsage.mcp.protocol import McpToolServer

log = logging.getLogger("greatsage.mcp.stdio")


def serve_stdio(
    server: McpToolServer,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Serve MCP messages until EOF.

    Stdout is reserved for JSON-RPC responses. Logs and diagnostics go to
    stderr through the normal logging setup, never to the protocol stream.
    A malformed line is answered with a JSON-RPC error and does not terminate
    the session.
    """
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    try:
        for line in source:
            if not line.strip():
                continue
            response: dict[str, Any] | None
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }
            else:
                response = server.handle(payload)
            if response is None:
                continue
            sink.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
            sink.write("\n")
            sink.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


def run_stdio_server(
    server: McpToolServer,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the transport and log a clean shutdown (stderr only)."""
    log.info("mcp stdio server started", extra={"component": "mcp"})
    try:
        return serve_stdio(server, stdin=stdin, stdout=stdout)
    finally:
        log.info("mcp stdio server stopped", extra={"component": "mcp"})
