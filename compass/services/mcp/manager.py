"""MCPConnectionManager — long-lived connections over stdio / HTTP / SSE.

Started once at app startup (FastAPI lifespan), stopped at shutdown. A
server that fails to connect is reported and skipped — one broken server
never blocks the session (same resilience stance as the original manager).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack

from compass.services.mcp.config import MCPServerConfig, load_mcp_configs
from compass.services.mcp.tool_wrapper import MCPTool
from compass.services.telemetry import log_event

logger = logging.getLogger("compass.mcp")

CONNECT_TIMEOUT_SECONDS = 30


class MCPManager:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.tools: list[MCPTool] = []
        self.status: dict[str, str] = {}
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        configs = load_mcp_configs()
        if not configs:
            return
        for config in configs.values():
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                    await self._connect(config)
                self.status[config.name] = "connected"
            except Exception as err:  # noqa: BLE001 — isolate per-server failures
                self.status[config.name] = f"failed: {err}"
                logger.error("mcp server %r failed to connect: %s", config.name, err)
            log_event(
                "mcp_server_connection",
                server=config.name,
                transport=config.type,
                ok=self.status[config.name] == "connected",
            )

    async def _connect(self, config: MCPServerConfig) -> None:
        from mcp import ClientSession

        if config.type == "stdio":
            import os

            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            if not config.command:
                raise ValueError("stdio server needs a 'command'")
            params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env={**os.environ, **config.env},
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
        elif config.type == "http":
            from mcp.client.streamable_http import streamablehttp_client

            read, write, _ = await self._stack.enter_async_context(
                streamablehttp_client(config.url, headers=config.headers or None)
            )
        elif config.type == "sse":
            from mcp.client.sse import sse_client

            read, write = await self._stack.enter_async_context(
                sse_client(config.url, headers=config.headers or None)
            )
        else:
            raise ValueError(f"unknown MCP transport type: {config.type}")

        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listing = await session.list_tools()
        for tool_def in listing.tools:
            self.tools.append(MCPTool(config.name, session, tool_def))
        logger.info(
            "mcp server %r connected: %d tools", config.name, len(listing.tools)
        )

    async def stop(self) -> None:
        try:
            await self._stack.aclose()
        except Exception as err:  # noqa: BLE001 — best-effort shutdown
            logger.debug("mcp shutdown noise: %s", err)
        self.tools.clear()
        self._started = False


_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager
