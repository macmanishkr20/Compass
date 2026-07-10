"""run_tools — the scheduler. Port of toolOrchestration.ts.

Same algorithm: partition the assistant's tool calls into batches where each
batch is either a run of consecutive concurrency-safe calls (executed in
parallel, results streamed as they arrive) or a single unsafe call (executed
serially, in order). Safety is judged per-input via Tool.is_concurrency_safe;
anything unparseable or throwing is treated as unsafe.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from compass.config import get_settings
from compass.core.tool_execution import ToolRunItem, run_tool_use
from compass.models.messages import ToolCall
from compass.tools.base import ToolUseContext, find_tool


@dataclass
class _Batch:
    concurrency_safe: bool
    calls: list[ToolCall] = field(default_factory=list)


def _is_concurrency_safe(tool_call: ToolCall, ctx: ToolUseContext) -> bool:
    tool = find_tool(ctx.tools, tool_call.name)
    if tool is None:
        return False
    try:
        parsed = tool.validate_input(json.loads(tool_call.arguments))
        return bool(tool.is_concurrency_safe(parsed))
    except Exception:  # noqa: BLE001 — conservative: unparseable means serial
        return False


def partition_tool_calls(
    tool_calls: list[ToolCall], ctx: ToolUseContext
) -> list[_Batch]:
    batches: list[_Batch] = []
    for tc in tool_calls:
        safe = _is_concurrency_safe(tc, ctx)
        if safe and batches and batches[-1].concurrency_safe:
            batches[-1].calls.append(tc)
        else:
            batches.append(_Batch(concurrency_safe=safe, calls=[tc]))
    return batches


async def run_tools(
    tool_calls: list[ToolCall], ctx: ToolUseContext
) -> AsyncIterator[ToolRunItem]:
    max_concurrency = get_settings().loop.max_tool_concurrency
    for batch in partition_tool_calls(tool_calls, ctx):
        if batch.concurrency_safe and len(batch.calls) > 1:
            for start in range(0, len(batch.calls), max_concurrency):
                window = batch.calls[start : start + max_concurrency]
                async for item in _merge(
                    [run_tool_use(tc, ctx) for tc in window]
                ):
                    yield item
        else:
            for tc in batch.calls:
                async for item in run_tool_use(tc, ctx):
                    yield item


async def _merge(
    generators: list[AsyncIterator[ToolRunItem]],
) -> AsyncIterator[ToolRunItem]:
    """Drain several tool generators into one stream, first-come order —
    the `all()` generator-merge utility Claude Code uses for parallel reads."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    sentinel = object()

    async def drain(gen: AsyncIterator[ToolRunItem]) -> None:
        try:
            async for item in gen:
                await queue.put(item)
        finally:
            await queue.put(sentinel)

    tasks = [asyncio.create_task(drain(g)) for g in generators]
    finished = 0
    try:
        while finished < len(tasks):
            item = await queue.get()
            if item is sentinel:
                finished += 1
            else:
                yield item  # type: ignore[misc]
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result
