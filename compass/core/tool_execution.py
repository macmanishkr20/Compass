"""run_tool_use — the single trust choke point. Port of toolExecution.ts.

Every tool call in the system, including subagent and future MCP calls,
passes through this gate, ordered cheap-to-expensive:

  resolve -> abort check -> parse -> validate -> pre-hooks -> tool hard
  verdict -> rules/modes -> ask-user -> execute -> post-hooks

Denials are never exceptions: they become tool_result messages with
is_error=True so the model can adapt. Every tool_call gets exactly one
tool_result — the API invariant this file defends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from pydantic import ValidationError

from compass.config import get_settings
from compass.models import events
from compass.models.messages import Message, ToolCall, tool_result_message
from compass.policy.hooks import HookEvent, get_hook_registry
from compass.policy.permissions import Behavior, check_permissions
from compass.services.telemetry import log_event
from compass.tools.base import (
    Progress,
    Tool,
    ToolOutput,
    ToolUseContext,
    find_tool,
)

logger = logging.getLogger("compass.tools")

INTERRUPTED_MESSAGE = "Tool execution was interrupted by the user."

# What run_tool_use yields: UI events plus, exactly once, the tool_result
# Message the loop must append to history.
ToolRunItem = events.Event | Message


async def run_tool_use(
    tool_call: ToolCall, ctx: ToolUseContext
) -> AsyncIterator[ToolRunItem]:
    started = time.monotonic()
    arguments = _parse_arguments(tool_call.arguments)
    yield events.ToolCallStarted(
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
        arguments=arguments if isinstance(arguments, dict) else {},
        agent_id=ctx.agent_id,
    )

    async def finish(content: str, *, is_error: bool = False, **meta: Any):
        log_event(
            "tool_used",
            tool_name=tool_call.name,
            is_error=is_error,
            duration_ms=int((time.monotonic() - started) * 1000),
            is_subagent=bool(ctx.agent_id),
        )
        yield events.ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            content=content[:2_000],
            is_error=is_error,
            duration_ms=int((time.monotonic() - started) * 1000),
            agent_id=ctx.agent_id,
        )
        yield tool_result_message(
            tool_call.id, content, is_error=is_error, agent_id=ctx.agent_id, **meta
        )

    # Gate 1: resolve
    tool = find_tool(ctx.tools, tool_call.name)
    if tool is None:
        async for item in finish(
            f"No such tool available: {tool_call.name}", is_error=True
        ):
            yield item
        return

    # Gate 2: abort — synthesize a result rather than dropping the call
    if ctx.abort_event.is_set():
        async for item in finish(INTERRUPTED_MESSAGE, is_error=True, synthetic=True):
            yield item
        return

    # Gate 3: parse + validate against the tool's schema
    if not isinstance(arguments, dict):
        async for item in finish(
            f"tool arguments were not valid JSON: {tool_call.arguments[:500]}",
            is_error=True,
        ):
            yield item
        return
    try:
        parsed = tool.validate_input(arguments)
    except (ValidationError, ValueError) as err:
        async for item in finish(f"invalid input: {err}", is_error=True):
            yield item
        return

    # Gate 4: pre_tool_use hooks (may block or rewrite input)
    hook_outcome = await get_hook_registry().run(
        HookEvent.PRE_TOOL_USE,
        {
            "tool_name": tool.name,
            "arguments": arguments,
            "session_id": ctx.session_id,
            "agent_id": ctx.agent_id,
        },
    )
    if hook_outcome.blocked:
        async for item in finish(
            f"blocked by pre_tool_use hook: {hook_outcome.reason}", is_error=True
        ):
            yield item
        return
    if hook_outcome.updated_input is not None:
        try:
            parsed = tool.validate_input(hook_outcome.updated_input)
            arguments = hook_outcome.updated_input
        except (ValidationError, ValueError) as err:
            async for item in finish(
                f"hook-updated input failed validation: {err}", is_error=True
            ):
                yield item
            return

    # Gate 5: tool-specific hard verdict (workspace escapes etc.)
    verdict = tool.check_tool_permissions(parsed, ctx)
    if verdict is None:
        # Gate 6: rules and permission mode
        verdict = check_permissions(
            tool.name,
            arguments,
            is_read_only=tool.is_read_only(parsed),
            permission_mode=ctx.permission_mode,
            extra_rules=getattr(ctx.broker, "session_rules", None),
        )
    if verdict.behavior is Behavior.DENY:
        async for item in finish(
            f"permission denied: {verdict.reason}", is_error=True
        ):
            yield item
        return

    # Gate 7: ask the human (canUseTool seam)
    if verdict.behavior is Behavior.ASK:
        request_id = str(uuid.uuid4())
        if ctx.broker.policy == "interactive":
            primary = str(
                arguments.get("command")
                or arguments.get("path")
                or arguments.get("file_path")
                or ""
            )
            ctx.broker.create(request_id, tool_name=tool.name, primary=primary)
        yield events.PermissionRequest(
            request_id=request_id,
            tool_call_id=tool_call.id,
            tool_name=tool.name,
            arguments=arguments,
            reason=verdict.reason,
            agent_id=ctx.agent_id,
        )
        allowed = await ctx.broker.wait(
            request_id, timeout=get_settings().loop.permission_timeout_seconds
        )
        log_event(
            "permission_decision",
            tool_name=tool.name,
            behavior="allow" if allowed else ("timeout" if allowed is None else "deny"),
        )
        yield events.PermissionResolved(
            request_id=request_id,
            behavior="allow" if allowed else ("timeout" if allowed is None else "deny"),
            agent_id=ctx.agent_id,
        )
        if not allowed:
            reason = "permission request timed out" if allowed is None else "user denied permission"
            async for item in finish(f"{reason} for {tool.name}", is_error=True):
                yield item
            return

    # Gate 8: execute, streaming progress
    output: ToolOutput | None = None
    try:
        async with asyncio.timeout(get_settings().loop.tool_timeout_seconds):
            async for item in tool.call(parsed, ctx):
                if isinstance(item, Progress):
                    yield events.ToolProgress(
                        tool_call_id=tool_call.id, data=item.data, agent_id=ctx.agent_id
                    )
                elif isinstance(item, events.Event):
                    yield item  # subagent sidechain events pass straight through
                elif isinstance(item, ToolOutput):
                    output = item
    except TimeoutError:
        output = ToolOutput(f"{tool.name} timed out", is_error=True)
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 — a tool crash must not kill the turn
        logger.exception("tool %s raised", tool.name)
        output = ToolOutput(f"error calling tool ({tool.name}): {err}", is_error=True)
    if output is None:
        output = ToolOutput(f"{tool.name} produced no output", is_error=True)

    # Gate 9: post hooks (observability/policy; cannot rewrite the result)
    await get_hook_registry().run(
        HookEvent.POST_TOOL_USE_FAILURE if output.is_error else HookEvent.POST_TOOL_USE,
        {
            "tool_name": tool.name,
            "arguments": arguments,
            "is_error": output.is_error,
            "session_id": ctx.session_id,
            "agent_id": ctx.agent_id,
        },
    )

    async for item in finish(output.content, is_error=output.is_error):
        yield item


def _parse_arguments(raw: str) -> Any:
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw
