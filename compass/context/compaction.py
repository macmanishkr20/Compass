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
from dataclasses import dataclass

from compass.config import get_settings
from compass.gateway.azure_client import ModelClient
from compass.persistence.artifact_store import save_artifact
from compass.models.messages import (
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
    threshold = int(
        settings.context.context_window_tokens * settings.context.autocompact_threshold
    )
    if not force and before < threshold:
        return messages, None

    transcript = _render_for_summary(visible)
    try:
        summary = await client.complete_utility(SUMMARY_PROMPT, transcript)
    except Exception as err:  # noqa: BLE001 — degrade, don't die mid-turn
        logger.error("autocompact summarization failed: %s", err)
        summary = "Summarization failed; history was truncated for length."

    boundary = compact_boundary_message(summary)
    # Keep the trailing slice (most recent exchange) after the boundary so
    # in-flight work isn't summarized out from under the model.
    tail = _protected_tail(visible)
    new_messages = messages + [boundary] + tail
    stage = "reactive" if force else "autocompact"
    report = StageReport(stage, before, estimate_tokens([boundary] + tail))
    return new_messages, report


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
