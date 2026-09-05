"""The providers the registry starts with.

Order matters only for duplicates, and the registry keeps the first it sees:
flow first because those ids are reserved and should never be shadowed, then
connectors, then Compass's own tools, then whatever MCP servers are currently
connected. MCP is last deliberately: a server that offers a `github` tool
should not shadow the built-in GitHub connector, and the registry keeps the
first id it sees.

Adding a provider is the whole extension story — a function returning
`list[NodeType]`, appended here or registered at runtime. A connector package
would land beside `flow` and `tools` and need no change anywhere else.
"""

from __future__ import annotations

from compass.pipelines.nodes import connectors, database, flow, tools, triggers
from compass.pipelines.types import Provider


def builtin_providers() -> list[Provider]:
    return [flow.provider, triggers.provider, connectors.provider,
            database.provider, tools.provider, tools.mcp_provider]
