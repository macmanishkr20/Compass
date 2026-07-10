"""MCPTool — a discovered MCP tool dressed as a native Tool.

This is the whole trick from the original architecture: because MCP tools
implement the same contract, the scheduler, the permission gate, hooks, and
telemetry treat them identically to built-ins. Names follow the
mcp__{server}__{tool} convention, sanitized to OpenAI's function-name rules.
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator

from pydantic import BaseModel

from compass.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


class _OpaqueInput(BaseModel):
    """Placeholder — MCP inputs are validated by the server's own schema."""

    model_config = {"extra": "allow"}


def mcp_tool_name(server: str, tool: str) -> str:
    return _NAME_SAFE.sub("_", f"mcp__{server}__{tool}")[:64]


class MCPTool(Tool):
    input_model = _OpaqueInput

    def __init__(self, server_name: str, session, tool_def) -> None:
        self._session = session
        self.server_name = server_name
        self.remote_name = tool_def.name
        self.name = mcp_tool_name(server_name, tool_def.name)
        self.description = (
            f"[MCP:{server_name}] {tool_def.description or tool_def.name}"
        )[:1024]
        self._schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        annotations = getattr(tool_def, "annotations", None)
        self._read_only_hint = bool(
            annotations and getattr(annotations, "readOnlyHint", False)
        )

    def validate_input(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("MCP tool arguments must be a JSON object")
        return arguments

    def is_read_only(self, inp) -> bool:
        # Trust boundary note: readOnlyHint is the *server's* claim. It grants
        # parallel scheduling and read-only permission defaults, same stance
        # as the original. Deny rules still beat it.
        return self._read_only_hint

    async def call(self, inp: dict, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        result = await self._session.call_tool(self.remote_name, inp)
        parts: list[str] = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(f"[{getattr(block, 'type', 'unknown')} content omitted]")
        yield ToolOutput(
            "\n".join(parts) or "(empty result)",
            is_error=bool(getattr(result, "isError", False)),
        )

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._schema,
            },
        }
