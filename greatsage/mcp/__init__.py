"""Local MCP (Model Context Protocol) server for Great Sage tools.

The stdio server is a transport adapter only: all execution is delegated to
``greatsage.tools.ToolService``. It is intentionally not used by ``chat``;
normal conversation never receives tool definitions.
"""

from greatsage.mcp.protocol import McpToolServer, mcp_tool_name
from greatsage.mcp.stdio import run_stdio_server, serve_stdio

__all__ = [
    "McpToolServer",
    "mcp_tool_name",
    "run_stdio_server",
    "serve_stdio",
]
