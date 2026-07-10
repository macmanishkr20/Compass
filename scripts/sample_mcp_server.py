"""Minimal stdio MCP server used by tests and as a wiring example.

Register it in .compass/mcp.json:

    {"sample": {"type": "stdio", "command": "python",
                "args": ["scripts/sample_mcp_server.py"]}}
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sample")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def reverse_text(text: str) -> str:
    """Reverse a string."""
    return text[::-1]


if __name__ == "__main__":
    mcp.run()  # stdio transport
