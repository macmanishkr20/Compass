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

from compass.common.config import get_settings
from compass.common.agent.compaction import (
    apply_tool_result_budget,
    autocompact_if_needed,
    count_context_tokens,
    microcompact,
)
from compass.common.agent.steering import answer_in, budget_note, threshold_guidance
from compass.common.tools.shelf import visible as shelf_visible
from compass.common.agent.tool_orchestration import run_tools
from compass.common.gateway.azure_client import (
    CompletionResult,
    ContextOverflowError,
    RefusedError,
    StreamDelta,
    get_model_client,
)
from compass.common.gateway import hosted, limits
from compass.common.gateway.responses import (
    REASONING_META_KEY,
    ThinkingDelta,
    ToolArgsDelta,
)
from compass.common.models import events
from compass.common.models.messages import (
    Message,
    ToolCall,
    messages_after_compact_boundary,
    user_message,
)
from compass.common.models.transitions import Continue, Terminal
from compass.common.policy.hooks import HookEvent, get_hook_registry
from compass.common.telemetry import log_event
from compass.common.tools.base import ToolUseContext

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


def with_missing_tool_results(messages: list[dict]) -> list[dict]:
    """Give every unanswered tool call a result, so the request is sendable.

    A call with no matching result is refused outright — "No tool output found
    for function call call_…" — and because the offending pair stays in the
    history, every later turn in that session is refused too. One interrupted
    call kills the conversation permanently.

    Calls are left unanswered whenever a turn stops between making one and
    recording its result: the client disconnects, the process is killed, or a
    tool that was waiting for a person is abandoned. Repairing on the way out
    rather than at teardown is deliberate — teardown is exactly the moment
    that does not reliably get to run — and it also heals sessions that were
    already broken before this existed.

    The synthetic result says the call was interrupted rather than pretending
    it succeeded. A model told a command ran when it did not will build on
    something that never happened.
    """
    answered = {
        m.get("tool_call_id") for m in messages if m.get("role") == "tool"
    }
    out: list[dict] = []
    for entry in messages:
        out.append(entry)
        if entry.get("role") != "assistant":
            continue
        for call in entry.get("tool_calls") or []:
            if call.get("id") in answered:
                continue
            out.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": "This call was interrupted before it finished and "
                           "has no result. Do not assume it succeeded; run it "
                           "again if the work still needs doing.",
            })
    return out


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
        # Only what is worth describing this turn. Below the threshold that is
        # everything, which is today's behaviour unchanged; above it the tools
        # from MCP servers are found by searching instead of listed, because a
        # hundred descriptions crowd out the conversation and make choosing
        # between them harder.
        offered = shelf_visible(
            ctx.tools, ctx.shelf, threshold=settings.tools.search_above
        )
        tool_schemas = [t.to_openai_schema() for t in offered]

        # A standing instruction about when thinking earns its latency, when
        # one is configured. Appended rather than prepended so it reads as a
        # note on the brief, not a replacement for it.
        notes = [
            threshold_guidance(settings.thinking.posture),
            answer_in(settings.thinking.response_language),
            # Empty unless the hosted interpreter is on, so the default
            # configuration's prompts stay byte-for-byte what they were.
            hosted.where_code_runs(),
        ]
        prompt_text = "\n\n".join([system_prompt, *(n for n in notes if n)])

        # The request, and the reasoning behind each earlier assistant turn,
        # built together so the positions cannot drift apart. That reasoning
        # is handed back so the model resumes it rather than working it out
        # again after every tool result; it is opaque and travels verbatim.
        # `to_openai()` cannot carry it, because the plain chat-completions
        # path would then send a field the API does not know.
        api_messages: list[dict] = [{"role": "system", "content": prompt_text}]
        reasoning_by_index: dict[int, list[dict]] = {}
        for message in visible:
            if message.role == "assistant" and message.meta.get(REASONING_META_KEY):
                reasoning_by_index[len(api_messages)] = message.meta[REASONING_META_KEY]
            api_messages.append(message.to_openai())

        api_messages = with_missing_tool_results(api_messages)

        # How much room is left, said out loud once it is worth saying. Sent
        # rather than stored: it describes this request, and a transcript full
        # of stale usage lines would be worse than one with none. Appended
        # last, after every real message, so it cannot shift the positions
        # `reasoning_by_index` was just keyed on.
        budget = limits.effective_window(settings.context.context_window_tokens)
        if note := budget_note(count_context_tokens(visible), budget):
            api_messages.append({"role": "system", "content": note})

        yield events.StreamRequestStart(
            turn=turn, model=settings.azure.deployment, agent_id=ctx.agent_id
        )

        # ---- sample the model, streaming
        result: CompletionResult | None = None
        # What has been streamed so far, kept so an abort mid-answer can save
        # the part the reader already watched arrive.
        streamed: list[str] = []
        aborted_mid_stream = False
        try:
            async for item in client.stream_chat(
                api_messages, tool_schemas, effort=effort, deployment=model,
                reasoning_by_index=reasoning_by_index,
            ):
                # Stop *inside* the stream, not only between turns. The check
                # at the top of the loop is the only one there used to be, and
                # a plain chat answer is one model call with no tool loop — so
                # it had already been passed before the first token, and Stop
                # did nothing at all: the turn ran to completion, was billed in
                # full, and was written to the transcript, while the API had
                # already answered {"aborted": true}. Breaking here closes the
                # upstream response, which is what actually stops generation.
                if ctx.abort_event.is_set():
                    aborted_mid_stream = True
                    break
                if isinstance(item, ThinkingDelta):
                    yield events.ThinkingDelta(
                        text=item.text,
                        starts_part=item.starts_part,
                        agent_id=ctx.agent_id,
                    )
                elif isinstance(item, StreamDelta):
                    streamed.append(item.text)
                    yield events.TextDelta(text=item.text, agent_id=ctx.agent_id)
                elif isinstance(item, ToolArgsDelta):
                    yield events.ToolArguments(
                        tool_call_id=item.call_id,
                        tool_name=item.name,
                        delta=item.delta,
                        agent_id=ctx.agent_id,
                    )
                elif isinstance(item, CompletionResult):
                    result = item
                # Anything else is dropped rather than mistaken for the
                # result: the bare `else` this replaces would have let a new
                # stream item silently become the turn's outcome.
        except RefusedError as declined:
            # Nothing was generated, so there is no turn to keep. Saying so is
            # the whole point: without this the surface shows a stack trace
            # about a 400 and the reader has no idea they were declined.
            yield events.Refused(
                message=declined.refusal.message(),
                category=declined.refusal.category,
                partial=False,
                agent_id=ctx.agent_id,
            )
            yield _complete(Terminal("refused", declined.refusal.category), turn, ctx)
            return
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

        if aborted_mid_stream:
            # Keep what the reader saw. The turn stopped in the middle of an
            # answer, and throwing that away would make Stop destructive as
            # well as slow — the text was on screen, so it belongs in the
            # thread the next turn re-sends.
            partial = "".join(streamed)
            if partial:
                assistant = Message(
                    role="assistant",
                    content=partial,
                    meta={"aborted": True, "agent_id": ctx.agent_id}
                    if ctx.agent_id
                    else {"aborted": True},
                )
                append(assistant)
                yield events.AssistantMessage(
                    uuid=assistant.uuid,
                    content=assistant.content,
                    tool_calls=[],
                    finish_reason="aborted",
                    agent_id=ctx.agent_id,
                )
            yield _complete(Terminal("aborted"), turn, ctx)
            return

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
                "reasoning_tokens": result.reasoning.tokens,
            }
        }
        # Reported before the reasoning summary, because it happened first:
        # the search is what the thinking was about.
        for item in result.hosted:
            if detail := hosted.describe(item):
                yield events.ServerToolUsed(
                    tool=item.get("type", "").removesuffix("_call"),
                    detail=detail,
                    sources=hosted.sources(item),
                    agent_id=ctx.agent_id,
                )
        if result.reasoning:
            # Stored on the message, so it is written to the transcript with
            # everything else and survives a resume. Nothing reads it but the
            # request builder, which passes it straight back.
            usage_meta[REASONING_META_KEY] = result.reasoning.items
            usage_meta["thinking_summary"] = result.reasoning.summary
            yield events.ThinkingComplete(
                tokens=result.reasoning.tokens,
                summary_chars=len(result.reasoning.summary),
                agent_id=ctx.agent_id,
            )
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

        # A declined turn ends here. It is not retried: the same prompt gets
        # the same answer, and the recovery path below would otherwise ask the
        # model to continue something it was stopped from writing.
        if result.refusal is not None:
            yield events.Refused(
                message=result.refusal.message(),
                category=result.refusal.category,
                partial=result.refusal.partial,
                agent_id=ctx.agent_id,
            )
            log_event("refused", category=result.refusal.category,
                      partial=result.refusal.partial)
            yield _complete(Terminal("refused", result.refusal.category), turn, ctx)
            return

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
        ok=getattr(report, "ok", True),
        detail=getattr(report, "detail", ""),
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
