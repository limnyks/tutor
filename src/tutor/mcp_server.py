"""The tutor connector: Moodle tools and memory tools in one MCP server (stdio)."""

import functools

# mcp 2 runs synchronous tools in a worker thread, which Playwright's sync API needs.
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .memory import tools as memory_tools
from .moodle import mcp_server as moodle


def _explained(fn):
    """Expected failures (unknown course or topic, no data yet) reach Claude with their message,
    so it can correct itself; the connector would otherwise report only 'Error executing tool'."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


def make_server(name: str, fns) -> MCPServer:
    server = MCPServer(name)
    for fn in fns:
        server.add_tool(_explained(fn))
    return server


def main() -> None:
    make_server("tutor", [*moodle.TOOLS, *memory_tools.ALL]).run()
