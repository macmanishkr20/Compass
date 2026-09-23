"""The context engine — port of services/compact/*.

Four escalating stages, cheapest first, each blind to the others' internals,
run before EVERY model call inside the query loop:

  1. tool-result budget  — oversized single results are truncated; the full
                           content is spilled to disk and the stub says where.
  2. microcompact        — tool results older than the last N are stubbed.
                           Operates purely on position/metadata, never parses
                           content, so it composes with stage 1.
  3. autocompact         — when estimated prompt tokens cross the threshold,
                           an LLM summary replaces the visible history behind
                           a compact-boundary message.
  4. reactive            — not run here; the loop invokes force_autocompact()
                           when the API rejects the prompt as too long.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace

from compass.common.config import get_settings
from compass.common.gateway import limits
from compass.common.gateway.azure_client import ModelClient
from compass.common.persistence.artifact_store import save_artifact
from compass.common.models.messages import (
    Message,
    compact_boundary_message,
    messages_after_compact_boundary,
)

logger = logging.getLogger("compass.compact")

SUMMARY_PROMPT = (
    "You are summarizing an agent session so it can continue in a fresh "
    "context window. Preserve: the user's goal, decisions made, files "
    "created or modified (with paths), commands run and their outcomes, "
    "unresolved errors, and the immediate next step. Be dense and factual."
)

MICROCOMPACT_STUB = (
    "[tool result compacted to save context — re-run the tool if you need "
    "this output again]"
)


def estimate_tokens(messages: list[Message]) -> int:
    """chars/4 heuristic — cheap, slightly pessimistic. Used for stage
    before/after deltas (how much a compactor freed), where a uniform
    char-based measure is exactly what's wanted."""
    total = 0
    for m in messages:
        total += len(m.content or "") // 4
        for tc in m.tool_calls:
            total += (len(tc.arguments) + len(tc.name)) // 4
        total += 8  # per-message envelope overhead
    return total


def count_context_tokens(messages: list[Message]) -> int:
    """Accurate prompt-size estimate — port of tokenCountWithEstimation.

    Walk back to the most recent assistant message carrying real API usage.
    Its (prompt + completion) tokens are exactly what was in context when the
    server produced it; only the messages appended *after* it (tool_results
    not yet seen by the API) need char-estimation. Falls back to a full
    char-estimate before the first response (fresh session)."""
    for i in range(len(messages) - 1, -1, -1):
        usage = messages[i].meta.get("usage")
        if usage:
            anchor = int(usage.get("prompt_tokens", 0)) + int(
                usage.get("completion_tokens", 0)
            )
            return anchor + estimate_tokens(messages[i + 1 :])
    return estimate_tokens(messages)


@dataclass
class StageReport:
    stage: str
    tokens_before: int
    tokens_after: int
    #: False when the stage degraded — a summary that could not be
    #: written, images dropped to fit. The turn continues either way, but
    #: the person is told rather than left to find it in the transcript.
    ok: bool = True
    detail: str = ""

    @property
    def changed(self) -> bool:
        return self.tokens_after < self.tokens_before


async def apply_tool_result_budget(messages: list[Message]) -> StageReport:
    settings = get_settings()
    limit = settings.context.tool_result_max_chars
    before = estimate_tokens(messages)
    for m in messages:
        if m.role != "tool" or not m.content or len(m.content) <= limit:
            continue
        if m.meta.get("budget_truncated"):
            continue
        locator = await save_artifact(
            f"{m.tool_call_id or uuid.uuid4()}.txt", m.content
        )
        head, tail = m.content[: limit // 2], m.content[-limit // 2 :]
        m.content = (
            f"{head}\n\n[... truncated: result exceeded budget; "
            f"full output saved to {locator} ...]\n\n{tail}"
        )
        m.meta["budget_truncated"] = True
    return StageReport("tool_result_budget", before, estimate_tokens(messages))


def microcompact(messages: list[Message]) -> StageReport:
    settings = get_settings()
    before = estimate_tokens(messages)
    visible = messages_after_compact_boundary(messages)
    tool_results = [m for m in visible if m.role == "tool"]
    old = tool_results[: -settings.context.microcompact_keep_recent or None]
    for m in old:
        if m.meta.get("microcompacted") or not m.content:
            continue
        if len(m.content) >= settings.context.microcompact_min_chars:
            m.content = MICROCOMPACT_STUB
            m.meta["microcompacted"] = True
    return StageReport("microcompact", before, estimate_tokens(messages))


async def autocompact_if_needed(
    messages: list[Message],
    client: ModelClient,
    *,
    force: bool = False,
) -> tuple[list[Message], StageReport | None]:
    """Returns (new message list, report) — appends a compact boundary rather
    than mutating history: the full record stays for resume/rewind, only the
    model's view shrinks (getMessagesAfterCompactBoundary)."""
    settings = get_settings()
    visible = messages_after_compact_boundary(messages)
    # Threshold decision uses the accurate, usage-anchored count so it fires on
    # true prompt size, not a pessimistic char guess.
    before = count_context_tokens(visible)
    # Plan against what the deployment will actually take, not only what the
    # model could hold. On a resource whose quota is smaller than the context
    # window, a conversation gets refused long before it gets large — and a
    # threshold above that point never fires, so the conversation 429s instead
    # of compacting. `effective_window` is the configured window until a
    # response reports a smaller quota, so nothing changes on a resource
    # generous enough that this never binds.
    budget = limits.effective_window(settings.context.context_window_tokens)
    threshold = int(budget * settings.context.autocompact_threshold)
    if not force and before < threshold:
        return messages, None

    transcript = _render_for_summary(visible)
    ok = True
    detail = ""
    try:
        summary = await client.complete_utility(SUMMARY_PROMPT, transcript)
    except Exception as err:  # noqa: BLE001 — degrade, don't die mid-turn
        logger.error("autocompact summarization failed: %s", err)
        summary = "Summarization failed; history was truncated for length."
        ok = False
        detail = (
            "The history could not be summarised, so older turns were dropped "
            f"instead ({err.__class__.__name__}). Anything the model needs from "
            "earlier in this session may have to be repeated."
        )

    # Keep the trailing slice (most recent exchange) after the boundary so
    # in-flight work isn't summarized out from under the model.
    tail = _protected_tail(visible)
    # Images are the reason a session reaches this point at all: thirty photos
    # on one turn is tens of megabytes re-sent on every request afterwards,
    # which is what makes a prompt too large to summarise and a request large
    # enough to be dropped mid-flight. Past the boundary the model keeps a few
    # and is told the rest exist.
    tail, dropped = _cap_images(tail, MAX_IMAGES_PAST_BOUNDARY)
    if dropped:
        summary = (
            f"{summary}\n\n[{dropped} attached image"
            f"{'' if dropped == 1 else 's'} dropped from the model's view here "
            "to fit the context window. They remain in the transcript; if they "
            "matter, save them into the workspace and refer to them by path.]"
        )
        detail = detail or (
            f"{dropped} attached image{'' if dropped == 1 else 's'} dropped from "
            "the model's view to fit the context window — they are still in the "
            "transcript. Files in the workspace are read by path and cost nothing "
            "to keep."
        )

    boundary = compact_boundary_message(summary)
    new_messages = messages + [boundary] + tail
    stage = "reactive" if force else "autocompact"
    report = StageReport(stage, before, estimate_tokens([boundary] + tail),
                         ok=ok, detail=detail)
    return new_messages, report


#: How many images survive a compaction. Enough to keep a conversation about a
#: picture working; few enough that the prompt stops growing without bound.
MAX_IMAGES_PAST_BOUNDARY = 4

_OMITTED_IMAGES = (
    "[earlier attached image omitted here to fit the context window]"
)


def _cap_images(messages: list[Message], keep: int) -> tuple[list[Message], int]:
    """Keep the newest `keep` image parts; replace the rest with a line saying
    what was there. Returns the messages and how many were dropped.

    Newest-first because the image someone just attached is the one being
    discussed; the ones from twenty turns ago have already been described in
    the text that followed them.
    """
    seen = 0
    dropped = 0
    out: list[Message] = []
    for m in reversed(messages):
        content = m.content
        if not isinstance(content, list):
            out.append(m)
            continue
        parts = []
        for part in reversed(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                seen += 1
                if seen <= keep:
                    parts.append(part)
                else:
                    dropped += 1
                    continue
            else:
                parts.append(part)
        parts.reverse()
        if len(parts) == len(content):
            out.append(m)
            continue
        parts.append({"type": "text", "text": _OMITTED_IMAGES})
        # replace() so uuid, timestamp and is_error survive — these messages are
        # the same messages, with fewer pictures.
        out.append(replace(m, content=parts, meta={**m.meta, "images_capped": True}))
    out.reverse()
    return out, dropped


def _protected_tail(visible: list[Message]) -> list[Message]:
    """Last user message onward, kept verbatim past the boundary. Walks
    backward past any tool results so a tool_call/tool pair is never split
    (the same invariant Claude Code protects)."""
    idx = len(visible) - 1
    while idx > 0 and visible[idx].role == "tool":
        idx -= 1
    for j in range(idx, -1, -1):
        if visible[j].role == "user" and not visible[j].meta.get("compact_boundary"):
            return [
                Message(
                    role=m.role,
                    content=m.content,
                    tool_calls=m.tool_calls,
                    tool_call_id=m.tool_call_id,
                    meta={**m.meta, "post_compact_tail": True},
                )
                for m in visible[j:]
            ]
    return []


def _render_for_summary(messages: list[Message], max_chars: int = 120_000) -> str:
    parts = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            calls = ", ".join(f"{tc.name}({tc.arguments[:200]})" for tc in m.tool_calls)
            parts.append(f"assistant -> tools: {calls}")
        if m.content:
            parts.append(f"{m.role}: {m.content[:4_000]}")
    text = "\n".join(parts)
    return text[-max_chars:]
