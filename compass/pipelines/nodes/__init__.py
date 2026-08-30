"""The providers the registry starts with.

Order matters only for duplicates, and the registry keeps the first it sees:
flow first because those ids are reserved and should never be shadowed, then
Compass's own tools, then whatever MCP servers are currently connected.

Adding a provider is the whole extension story — a function returning
`list[NodeType]`, appended here or registered at runtime. A connector package
would land beside `flow` and `tools` and need no change anywhere else.
"""

from __future__ import annotations

from compass.pipelines.nodes import flow, tools
from compass.pipelines.types import Provider


def builtin_providers() -> list[Provider]:
    return [flow.provider, tools.provider, tools.mcp_provider]
