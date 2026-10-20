"""Small real stdio MCP server used by the broker integration tests."""

import asyncio
import os
from pathlib import Path
import sys

from mcp.server.mcpserver import MCPServer
from mcp_types import ListToolsResult


marker = os.environ.get("MCP_FIXTURE_MARKER")
if marker:
    Path(marker).write_text(str(os.getpid()), encoding="utf-8")
if os.environ.get("MCP_FIXTURE_SPAM_STDERR") == "1":
    print(os.environ.get("MCP_FIXTURE_SECRET", "") * 1000, file=sys.stderr)

class TestServer(MCPServer):
    async def _handle_list_tools(self, ctx, params):
        tools = await self.list_tools()
        schema_marker = os.environ.get("MCP_FIXTURE_SCHEMA_MARKER")
        if schema_marker and Path(schema_marker).exists():
            tools = [
                tool.model_copy(update={"input_schema": {
                    "type": "object", "properties": {"value": {"type": "integer"}},
                    "required": ["value"], "additionalProperties": False,
                }}) if tool.name == "read_value" else tool
                for tool in tools
            ]
        if os.environ.get("MCP_FIXTURE_PAGED") == "1":
            if params and params.cursor == "page-2":
                return ListToolsResult(tools=tools[3:])
            return ListToolsResult(tools=tools[:3], next_cursor="page-2")
        return ListToolsResult(tools=tools)


server = TestServer("harness-test")


@server.tool()
def read_value(value: str) -> str:
    """Return a string for read-only integration testing."""
    return f"value:{value}"


@server.tool()
def read_secret() -> str:
    """Exercise server-only credential forwarding and redaction."""
    return os.environ.get("MCP_FIXTURE_SECRET", "missing")


@server.tool()
def write_value(value: str) -> str:
    """A deliberately disallowed tool."""
    marker = os.environ.get("MCP_FIXTURE_WRITE_MARKER")
    if marker:
        Path(marker).write_text(value, encoding="utf-8")
    return value


@server.tool()
async def slow_read() -> str:
    """Exercise broker timeout and cancellation."""
    marker = os.environ.get("MCP_FIXTURE_CALL_MARKER")
    if marker:
        path = Path(marker)
        count = int(path.read_text()) if path.exists() else 0
        path.write_text(str(count + 1), encoding="utf-8")
    await asyncio.sleep(5)
    return "late"


@server.tool()
def large_read() -> str:
    """Return an oversized result."""
    return "x" * 100_000


@server.tool()
def failing_read() -> str:
    """Return a tool exception without killing the server."""
    raise ValueError("fixture failure")


@server.tool()
def exit_read() -> str:
    """Exit abruptly after a tool call begins."""
    os._exit(17)


if __name__ == "__main__":
    print("fixture stdio server starting", file=sys.stderr)
    server.run(transport="stdio")
