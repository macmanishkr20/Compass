"""MCP server configuration — port of the mcp config + envExpansion layer.

Sources, merged in order (later wins on name clash):
  1. `.compass/mcp.json` in the workspace (or COMPASS_MCP_CONFIG path).
     Accepts either a bare mapping or Claude Code's {"mcpServers": {...}}.
  2. COMPASS_MCP_SERVERS env var — inline JSON, same shape.

Every string value supports ${VAR} expansion against the process
environment, so secrets (API tokens in headers, etc.) live in .env and the
checked-in config stays clean:

    {
      "github": {"type": "http", "url": "https://api.example.com/mcp",
                 "headers": {"Authorization": "Bearer ${GITHUB_MCP_TOKEN}"}},
      "sample": {"type": "stdio", "command": "python",
                 "args": ["scripts/sample_mcp_server.py"]}
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from compass.config import get_settings

logger = logging.getLogger("compass.mcp")

_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class MCPServerConfig:
    name: str
    type: str = "stdio"  # stdio | http | sse
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def _expand(value):
    if isinstance(value, str):
        return _VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _parse(raw: dict, source: str) -> dict[str, MCPServerConfig]:
    servers = raw.get("mcpServers", raw)
    configs: dict[str, MCPServerConfig] = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            logger.warning("mcp config %s: server %r is not an object", source, name)
            continue
        spec = _expand(spec)
        configs[name] = MCPServerConfig(
            name=name,
            type=spec.get("type", "stdio" if spec.get("command") else "http"),
            command=spec.get("command", ""),
            args=list(spec.get("args", [])),
            env=dict(spec.get("env", {})),
            url=spec.get("url", ""),
            headers=dict(spec.get("headers", {})),
        )
    return configs


def load_mcp_configs() -> dict[str, MCPServerConfig]:
    settings = get_settings()
    configs: dict[str, MCPServerConfig] = {}

    path = settings.workspace_root / settings.mcp_config_path
    if path.is_file():
        try:
            configs.update(_parse(json.loads(path.read_text()), str(path)))
        except (OSError, json.JSONDecodeError) as err:
            logger.error("could not load %s: %s", path, err)

    if settings.mcp_servers_inline:
        try:
            configs.update(
                _parse(json.loads(settings.mcp_servers_inline), "COMPASS_MCP_SERVERS")
            )
        except json.JSONDecodeError as err:
            logger.error("COMPASS_MCP_SERVERS is not valid JSON: %s", err)

    return configs
