"""The agent loop — port of query.ts queryLoop().

One iteration = one model sample + one round of tool execution. Explicit
state, typed Continue/Terminal transitions, and the same recovery paths:

  * tool calls present            -> Continue("tool_results")
  * finish_reason == "length"     -> Continue("max_output_recovery"), bounded
  * ContextOverflowError          -> Continue("reactive_compact"), once
  * Stop hook blocks termination  -> Continue("stop_hook"), once per turn-end
  * otherwise                     -> Terminal

The context pipeline (budget -> microcompact -> autocompact) runs before
every sample. Nothing here buffers: every event is yielded the moment it
exists, and the caller is free to be a terminal, an SSE stream, or a parent
agent consuming a subagent sidechain.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Callable

from compass.config import get_settings
from compass.context.compaction import (
    apply_tool_result_budget,
    autocompact_if_needed,
    microcompact,
)
from compass.core.tool_orchestration import run_tools
from compass.gateway.azure_client import (
    CompletionResult,
    ContextOverflowError,
    StreamDelta,
    get_model_client,
)
from compass.models import events
from compass.models.messages import (
    Message,
    ToolCall,
    messages_after_compact_boundary,
    user_message,
)
from compass.models.transitions import Continue, Terminal
from compass.policy.hooks import HookEvent, get_hook_registry
from compass.services.telemetry import log_event
from compass.tools.base import ToolUseContext

logger = logging.getLogger("compass.loop")

MAX_OUTPUT_RECOVERY_PROMPT = (
    "Your previous response was cut off at the output token limit. "
    "Continue the output from the exact character where it stopped. "
    "Do NOT add any preamble, explanation, or phrases like 'continuing where I "
    "left off'. Do NOT repeat any content already emitted. If you were inside a "
    "fenced code block or an artifact, do NOT re-open the fence — just continue "
    "the raw content so the two halves concatenate seamlessly."
)

OnMessage = Callable[[Message], None]


async def query(
    messages: list[Message],
    ctx: ToolUseContext,
    *,
    system_prompt: str,
    on_message: OnMessage | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    model: str | None = None,
) -> AsyncIterator[events.Event]:
    settings = get_settings()
    client = get_model_client()
    max_turns = max_turns or settings.loop.max_turns

    def append(message: Message) -> None:
        messages.append(message)
        if on_message is not None:
            on_message(message)

    # -- loop state (the State record from query.ts)
    turn = 0
    recovery_count = 0
    reactive_attempted = False
    stop_hook_fired = False
    transition: Continue | None = None  # why the previous iteration continued

    while True:
        turn += 1
        if turn > max_turns:
            yield _complete(Terminal("max_turns"), turn, ctx)
            return
        if ctx.abort_event.is_set():
            yield _complete(Terminal("aborted"), turn, ctx)
            return

        # ---- context pipeline: budget -> microcompact -> autocompact
        for report in (await apply_tool_result_budget(messages), microcompact(messages)):
            if report.changed:
                log_event("compaction", stage=report.stage,
                          tokens_before=report.tokens_before,
                          tokens_after=report.tokens_after)
                yield _compaction(report, ctx)
        new_messages, auto_report = await autocompact_if_needed(messages, client)
        if auto_report is not None:
            _adopt(messages, new_messages, on_message)
            yield _compaction(auto_report, ctx)

        visible = messages_after_compact_boundary(messages)
        api_messages = [{"role": "system", "content": system_prompt}] + [
            m.to_openai() for m in visible
        ]
        tool_schemas = [t.to_openai_schema() for t in ctx.tools]

        yield events.StreamRequestStart(
            turn=turn, model=settings.azure.deployment, agent_id=ctx.agent_id
        )

        # ---- sample the model, streaming
        result: CompletionResult | None = None
        try:
            async for item in client.stream_chat(
                api_messages, tool_schemas, effort=effort, deployment=model
            ):
                if isinstance(item, StreamDelta):
                    yield events.TextDelta(text=item.text, agent_id=ctx.agent_id)
                else:
                    result = item
        except ContextOverflowError:
            if reactive_attempted:
                yield events.ErrorEvent(
                    message="prompt too long even after emergency compaction",
                    agent_id=ctx.agent_id,
                )
                yield _complete(Terminal("error", "context overflow"), turn, ctx)
                return
            reactive_attempted = True
            new_messages, report = await autocompact_if_needed(
                messages, client, force=True
            )
            _adopt(messages, new_messages, on_message)
            if report is not None:
                yield _compaction(report, ctx)
            transition = Continue("reactive_compact")
            continue

        if result is None:
            yield _complete(Terminal("error", "model returned no completion"), turn, ctx)
            return

        ctx.cost_tracker.record(
            result.model,
            result.prompt_tokens,
            result.completion_tokens,
            result.cached_prompt_tokens,
        )

        # Stamp the real API usage onto the assistant message. This is the
        # anchor the context counter reads back (port of getTokenUsage): the
        # next request's prompt size is this response's (prompt+completion)
        # tokens plus a rough estimate of only the tool_results appended after
        # it — far more accurate than char-estimating the whole history.
        usage_meta = {
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "cached_prompt_tokens": result.cached_prompt_tokens,
            }
        }
        assistant = Message(
            role="assistant",
            content=result.content or None,
            tool_calls=[
                ToolCall(id=d.id, name=d.name, arguments=d.arguments)
                for d in result.tool_calls
            ],
            meta={**usage_meta, "agent_id": ctx.agent_id}
            if ctx.agent_id
            else usage_meta,
        )
        append(assistant)
        yield events.AssistantMessage(
            uuid=assistant.uuid,
            content=assistant.content,
            tool_calls=[tc.to_openai() for tc in assistant.tool_calls],
            finish_reason=result.finish_reason,
            agent_id=ctx.agent_id,
        )

        # ---- transition selection
        if assistant.tool_calls:
            async for item in run_tools(assistant.tool_calls, ctx):
                if isinstance(item, Message):
                    append(item)
                else:
                    yield item
            # Computer-use vision: if the browser tool captured screenshots this
            # turn, feed them to the model as a user image message AFTER all the
            # tool results (keeping the tool_call→tool_result ordering valid), so
            # the agent actually sees the page it's driving.
            if ctx.pending_vision:
                shots = ctx.pending_vision[-2:]  # only the most recent views
                ctx.pending_vision.clear()
                # Stale page views bloat context (images are heavy) and only the
                # CURRENT view matters — replace prior screenshots with a stub.
                for prior in messages:
                    if prior.meta.get("_vision") and isinstance(prior.content, list):
                        prior.content = "[earlier browser screenshot — superseded]"
                meta: dict = {"synthetic": True, "_vision": True}
                if ctx.agent_id:
                    meta["agent_id"] = ctx.agent_id
                append(
                    Message(
                        role="user",
                        content=[
                            {"type": "text", "text": "Current browser view(s):"},
                            *(
                                {"type": "image_url", "image_url": {"url": uri}}
                                for uri in shots
                            ),
                        ],
                        meta=meta,
                    )
                )
            transition = Continue("tool_results")
            continue

        if (
            result.finish_reason == "length"
            and recovery_count < settings.loop.max_output_tokens_recovery_limit
        ):
            recovery_count += 1
            append(user_message(MAX_OUTPUT_RECOVERY_PROMPT, synthetic=True))
            transition = Continue(
                "max_output_recovery", f"attempt {recovery_count}"
            )
            continue

        # Natural stop: give Stop hooks one chance per turn-end to push back
        # (stopHookActive guard from query.ts — prevents hook-driven livelock).
        if not stop_hook_fired:
            outcome = await get_hook_registry().run(
                HookEvent.SUBAGENT_STOP if ctx.agent_id else HookEvent.STOP,
                {"session_id": ctx.session_id, "agent_id": ctx.agent_id, "turns": turn},
            )
            if outcome.blocked:
                stop_hook_fired = True
                append(
                    user_message(
                        f"A stop hook prevented completion: {outcome.reason}",
                        synthetic=True,
                    )
                )
                transition = Continue("stop_hook", outcome.reason)
                continue

        yield events.UsageReport(
            prompt_tokens=sum(
                u.prompt_tokens for u in ctx.cost_tracker.by_model.values()
            ),
            completion_tokens=sum(
                u.completion_tokens for u in ctx.cost_tracker.by_model.values()
            ),
            cached_prompt_tokens=sum(
                u.cached_prompt_tokens for u in ctx.cost_tracker.by_model.values()
            ),
            cost_usd=ctx.cost_tracker.total_cost_usd(),
            agent_id=ctx.agent_id,
        )
        yield _complete(Terminal("end_turn"), turn, ctx)
        return


def _adopt(
    messages: list[Message], new_messages: list[Message], on_message: OnMessage | None
) -> None:
    """Adopt a compacted message list in place, persisting only the appended
    boundary/tail records (history itself is never rewritten on disk)."""
    appended = new_messages[len(messages) :]
    messages.extend(appended)
    if on_message is not None:
        for m in appended:
            on_message(m)


def _compaction(report, ctx: ToolUseContext) -> events.Compaction:
    return events.Compaction(
        stage=report.stage,
        tokens_before=report.tokens_before,
        tokens_after=report.tokens_after,
        agent_id=ctx.agent_id,
    )


def _complete(terminal: Terminal, turn: int, ctx: ToolUseContext) -> events.TurnComplete:
    log_event(
        "turn_complete",
        reason=terminal.reason,
        turns=turn,
        is_subagent=bool(ctx.agent_id),
        depth=ctx.depth,
        cost_usd=ctx.cost_tracker.total_cost_usd(),
        cache_hit_rate=ctx.cost_tracker.cache_hit_rate(),
    )
    return events.TurnComplete(
        reason=terminal.reason, detail=terminal.detail, turns=turn, agent_id=ctx.agent_id
    )
