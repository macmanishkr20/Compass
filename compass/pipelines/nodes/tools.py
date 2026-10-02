"""Compass's tools, adapted into node types.

This provider is why the catalogue is not empty on day one. Every tool in the
registry already declares what a node type needs — a name, a description, a
pydantic model that yields JSON Schema, and a predicate saying whether it is
safe to run beside a sibling — so the adaptation is genuine reuse rather than
a wrapper written per tool.

MCP tools come through the same door. `MCPTool` subclasses `Tool` and carries
the server's own `inputSchema`, so a server someone connects contributes nodes
without a line of code here. That is the whole reason the registry is rebuilt
on demand instead of cached at import.

What this deliberately does not do is smuggle the agent's permission model
into the pipeline. A tool's own `check_tool_permissions` still runs, but the
pipeline's capability set is checked first and independently: a graph running
unattended at 3am should be allowed to run a shell command only because a
person ticked that box on the pipeline, never because the tool would have been
allowed in an interactive session.
"""

from __future__ import annotations

import logging
from typing import Any

from compass.pipelines.types import NodeContext, NodeResult, NodeType, Port

logger = logging.getLogger("compass.pipelines")

#: Tools whose capability requirement is not obvious from the tool itself.
#: Anything not named here is treated as harmless, which is true of the
#: read-only majority (glob, grep, read).
_REQUIRES: dict[str, str] = {
    "bash": "shell",
    "bash_output": "shell",
    "file_write": "write",
    "file_edit": "write",
    "web_fetch": "network",
    "browser": "network",
    "screenshot": "network",
    "agent": "agent",
    "consult": "agent",
    "memory": "write",
}

#: Tools that make no sense as a pipeline step. `ask_user` and `todo_write`
#: exist to shape a conversation, and a pipeline is not one — approval is a
#: flow node with a surface behind it, which is the thing people actually
#: want here.
_SKIP = {"ask_user", "todo_write"}


def _handler_for(tool: Any):
    async def _run(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
        # Validate through the tool's own model, so a node's settings are held
        # to exactly the contract the tool documents.
        try:
            parsed = tool.validate_input(config)
        except Exception as err:  # noqa: BLE001 — surfaced as a node failure
            raise ValueError(f"invalid settings for {tool.name}: {err}") from err

        from compass.common.tools.base import (
            CostTracker,
            PermissionBroker,
            ToolOutput,
            ToolUseContext,
        )

        # A pipeline node runs with no conversation behind it, so the tool
        # context is minimal by construction. Permission was already settled
        # by the engine against the pipeline's capabilities.
        #
        # The four below are required and were not being passed, so building
        # this raised TypeError before the tool was ever called — every tool
        # node failed, whatever the tool. They are given the values the
        # absence implied: no catalogue to search, nobody to prompt, and a
        # cost tracker of its own because the run is not a conversation.
        tool_ctx = ToolUseContext(
            session_id=ctx.run_id,
            tools=[],
            broker=PermissionBroker(policy="auto_grant"),
            cost_tracker=CostTracker(),
            workspace_root=ctx.workspace_root or None,
            permission_mode="bypass",
            # There is no conversation, so the run stands in for one: anything
            # this node stores is filed under the person whose pipeline it is,
            # against the run that produced it.
            owner=ctx.owner,
        )

        text_parts: list[str] = []
        is_error = False
        async for chunk in tool.call(parsed, tool_ctx):
            if isinstance(chunk, ToolOutput):
                text_parts.append(chunk.content or "")
                is_error = chunk.is_error
            else:
                # Progress. Kept, but bounded: a shell command that prints a
                # megabyte should not put a megabyte into the run record.
                data = getattr(chunk, "data", "")
                if data and sum(len(p) for p in text_parts) < 20_000:
                    text_parts.append(data)

        text = "".join(text_parts)
        if is_error:
            raise RuntimeError(text[:2_000] or f"{tool.name} failed")
        # Tool output is a string today. `data` carries the structured half
        # once ToolOutput grows one; until then the text is the honest answer
        # and an extract-to-schema node is how it becomes fields.
        return NodeResult(data={"text": text}, text=text)

    return _run


def _config_schema(tool: Any) -> dict[str, Any]:
    """JSON Schema for the tool's settings, from whichever source it has."""
    try:
        schema = tool.input_model.model_json_schema()
    except Exception:  # noqa: BLE001 — MCP tools carry an opaque model
        schema = getattr(tool, "_schema", None) or {
            "type": "object", "properties": {}
        }
    if isinstance(schema, dict):
        schema.pop("title", None)
    return schema


def _node_type(tool: Any, category: str) -> NodeType | None:
    try:
        name = tool.name
    except Exception:  # noqa: BLE001 — a malformed tool is skipped, not fatal
        return None
    if name in _SKIP:
        return None
    description = (getattr(tool, "description", "") or "").strip()
    # The first sentence is the palette blurb; tool descriptions are long
    # because they are written for a model, not for a menu.
    blurb = description.split(". ")[0][:200]

    # An MCP tool arrives named `mcp__server__tool`, which is the wire name
    # and reads badly in a palette. Split it back into the server it came
    # from and the tool it is, so the id says where a node is from and the
    # label says what it does — `mcp.sample.add`, "Add", from "sample".
    node_id = f"{category}.{name}"
    label = name.replace("_", " ").capitalize()
    if category == "mcp" and name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            _, server, tool_name = parts
            node_id = f"mcp.{server}.{tool_name}"
            label = tool_name.replace("_", " ").capitalize()
            blurb = blurb.removeprefix(f"[MCP:{server}]").strip()
            blurb = f"{blurb} From the {server} MCP server.".strip()

    return NodeType(
        id=node_id,
        label=label,
        category=category,
        description=blurb,
        handler=_handler_for(tool),
        config_schema=_config_schema(tool),
        outputs=(Port("out", "text"),),
        requires=_REQUIRES.get(name, ""),
        # Without an input we cannot ask the predicate, so assume the
        # cautious answer. A node wrongly serialized is slow; a node wrongly
        # parallelized corrupts something.
        concurrency_safe=False,
    )


def provider() -> list[NodeType]:
    """Built-in tools as node types."""
    try:
        from compass.code.tools.registry import get_all_tools
    except Exception:  # noqa: BLE001 — the module must load without the agent
        logger.warning("tool registry unavailable", exc_info=True)
        return []
    found: list[NodeType] = []
    for tool in get_all_tools():
        if node_type := _node_type(tool, "tool"):
            found.append(node_type)
    return found


def mcp_provider() -> list[NodeType]:
    """Whatever the connected MCP servers currently expose.

    Live by design: a server connected after startup should appear in the
    palette without a restart, which is why the registry rebuilds rather than
    caching this.

    Their own category rather than being folded in with connectors, because
    the distinction is one a reader acts on: a built-in connector is Compass's
    to fix, an MCP node belongs to a server someone else configured and can
    disconnect. A palette that hid that would make a node vanishing look like
    a Compass bug.
    """
    try:
        from compass.code.mcp.manager import get_mcp_manager

        tools = get_mcp_manager().tools or []
    except Exception:  # noqa: BLE001 — a server that is down costs its own
        # nodes and nothing else
        return []
    found: list[NodeType] = []
    for tool in tools:
        if node_type := _node_type(tool, "mcp"):
            found.append(node_type)
    return found
