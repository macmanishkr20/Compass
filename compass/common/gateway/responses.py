"""The reasoning path: Azure's Responses API, where thinking is visible.

Chat completions will tell you how many tokens a model spent thinking and
nothing else. The Responses API returns the reasoning itself — a readable
summary, streamed as it is produced — and, when asked, an encrypted copy of
the full reasoning that can be handed back on the next request so the model
picks up where it left off instead of re-deriving everything after every tool
call. Those two things are what make thinking something a user can watch and
an agent can build on, so Compass talks to this API whenever the deployment
reasons at all.

Three facts about this API decide the shape of everything below, and all three
were measured against the configured resource rather than assumed:

  * Reasoning arrives as its own stream of `response.reasoning_summary_text
    .delta` events, separate from the answer's `response.output_text.delta`.
    They are different kinds of content and stay separate all the way to the
    UI.
  * The reasoning that matters is not the summary. `encrypted_content` is the
    real thing, opaque and only useful passed back verbatim — the same
    contract as a thinking block's signature.
  * The wire format is not chat completions. Messages become a flat list of
    items, the system prompt becomes `instructions`, a tool call and its
    result are two separate items rather than a field on a message, and tool
    schemas lose a level of nesting. Compass speaks chat completions
    everywhere else, so the translation lives here and nowhere else.

Reasoning is billed as output and shares the output cap with the answer. A cap
sized for a reply without thinking will truncate one with it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger("compass.gateway.reasoning")

#: Where an assistant message keeps the reasoning that produced it. Kept in
#: `meta`, which is already persisted with the transcript, so reasoning
#: survives a session resume without any new storage.
REASONING_META_KEY = "reasoning"


@dataclass
class ThinkingDelta:
    """A fragment of the model's reasoning summary, as it is produced."""

    text: str = ""
    #: True on the first fragment of a new reasoning part. The API emits
    #: several parts per turn; they read as paragraphs, not one run-on block.
    starts_part: bool = False


@dataclass
class ReasoningTrace:
    """What one turn of thinking produced.

    `items` is the round-trip payload and is never interpreted — it goes back
    to the API exactly as it arrived. `summary` is the readable part, and is
    for people.
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    tokens: int = 0

    def __bool__(self) -> bool:
        return bool(self.items or self.summary)


# --------------------------------------------------------------------------
# chat completions -> Responses
# --------------------------------------------------------------------------


def split_instructions(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], int]:
    """Lift the leading system prompt out; the Responses API takes it apart.

    Only a leading run of system messages is lifted. A system message that
    appears mid-conversation is a deliberate marker (a compaction boundary,
    say) and stays where it is, as a normal item.

    The third value is how many messages were lifted. Callers index into the
    list they passed in, so anything keyed by position has to be shifted by
    this much — leaving it out silently drops the reasoning instead of
    failing, and the turn still looks fine.
    """
    head: list[str] = []
    rest = list(messages)
    while rest and rest[0].get("role") == "system":
        head.append(str(rest[0].get("content") or ""))
        rest.pop(0)
    return "\n\n".join(h for h in head if h), rest, len(messages) - len(rest)


def _text_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal parts
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text")
        )
    return "" if content is None else str(content)


def to_input(
    messages: list[dict[str, Any]],
    reasoning_by_index: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Turn chat-completions messages into Responses input items.

    `reasoning_by_index` carries the reasoning captured for an assistant
    message, keyed by its position in `messages`. Those items are re-emitted
    immediately before the tool calls they produced, which is the order the
    API expects and the reason the model can resume its own reasoning rather
    than starting over after each tool result.
    """
    reasoning_by_index = reasoning_by_index or {}
    items: list[dict[str, Any]] = []

    for index, message in enumerate(messages):
        role = message.get("role")

        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id") or "",
                "output": _text_content(message) or "",
            })
            continue

        if role == "assistant":
            # Reasoning first: it is what led to everything else in this turn.
            for item in reasoning_by_index.get(index, []):
                items.append(item)
            text = _text_content(message)
            if text:
                items.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue

        # user, and any system message that is not part of the leading run
        content = message.get("content")
        if isinstance(content, list):
            # Multimodal: images pass through, text is renamed to the input form.
            parts: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("text", "input_text"):
                    parts.append({"type": "input_text", "text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    parts.append({"type": "input_image", "image_url": url})
                elif part.get("type") == "input_image":
                    parts.append(part)
            items.append({"role": role or "user", "content": parts})
        else:
            items.append({
                "role": role or "user",
                "content": [{"type": "input_text", "text": _text_content(message)}],
            })

    return items


def to_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten chat-completions tool schemas into the Responses shape.

    Chat completions nests the definition under `function`; this API does not.
    """
    flattened: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function") if tool.get("type") == "function" else None
        if fn is None:  # already flat
            flattened.append(tool)
            continue
        flattened.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return flattened


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------


def build_request(
    *,
    deployment: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_output_tokens: int,
    effort: str | None,
    display: str,
    reasoning_by_index: dict[int, list[dict[str, Any]]] | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    """The request body, with reasoning asked for in the way this API wants.

    `store: false` with `include: ["reasoning.encrypted_content"]` is what
    makes the encrypted reasoning come back to us rather than being kept
    server-side under a response id: Compass owns its own transcripts, and a
    conversation has to survive a restart of anything.
    """
    instructions, rest, lifted = split_instructions(messages)
    # Positions were computed against the caller's list, before the system
    # prompt was lifted off the front of it.
    shifted = {i - lifted: items for i, items in (reasoning_by_index or {}).items()
               if i - lifted >= 0}

    reasoning: dict[str, Any] = {}
    if effort:
        reasoning["effort"] = effort
    if display != "omitted":
        reasoning["summary"] = "auto"

    body: dict[str, Any] = {
        "model": deployment,
        "input": to_input(rest, shifted),
        "max_output_tokens": max_output_tokens,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if instructions:
        body["instructions"] = instructions
    if reasoning:
        body["reasoning"] = reasoning
    if tools:
        body["tools"] = to_tools(tools)
        body["tool_choice"] = "auto"
    if stream:
        body["stream"] = True
    return body


# --------------------------------------------------------------------------
# the stream
# --------------------------------------------------------------------------


@dataclass
class ResponsesOutcome:
    """Everything one streamed response produced, once it has finished."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: ReasoningTrace = field(default_factory=ReasoningTrace)
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0


class ResponsesStreamError(RuntimeError):
    """The API reported the response itself as failed."""


async def consume(
    events: AsyncIterator[dict[str, Any]],
    outcome: ResponsesOutcome,
) -> AsyncIterator[ThinkingDelta | str]:
    """Fold the event stream into `outcome`, yielding what should be shown live.

    Yields `ThinkingDelta` for reasoning and plain `str` for answer text, so a
    caller can put each in front of the user the moment it arrives. Everything
    else — the reasoning items to round-trip, the tool calls, the usage — is
    accumulated on `outcome` and read once the stream ends.
    """
    part_open = False

    async for event in events:
        kind = event.get("type", "")

        # ---- reasoning, as it is produced
        if kind == "response.reasoning_summary_part.added":
            part_open = False  # the next delta opens a new part
            continue
        if kind == "response.reasoning_summary_text.delta":
            text = event.get("delta") or ""
            if not text:
                continue
            outcome.reasoning.summary += ("\n\n" if not part_open and
                                          outcome.reasoning.summary else "") + text
            yield ThinkingDelta(text=text, starts_part=not part_open)
            part_open = True
            continue

        # ---- the answer
        if kind == "response.output_text.delta":
            text = event.get("delta") or ""
            if text:
                outcome.text += text
                yield text
            continue

        # ---- completed items: reasoning to round-trip, and tool calls
        if kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "reasoning":
                # Kept whole and never edited: only the API can read it back.
                outcome.reasoning.items.append(item)
            elif item.get("type") == "function_call":
                outcome.tool_calls.append({
                    "id": item.get("call_id") or item.get("id") or "",
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "{}",
                })
            continue

        # ---- the end
        if kind in ("response.completed", "response.incomplete"):
            response = event.get("response") or {}
            usage = response.get("usage") or {}
            outcome.prompt_tokens = usage.get("input_tokens", 0) or 0
            outcome.completion_tokens = usage.get("output_tokens", 0) or 0
            outcome.cached_prompt_tokens = (
                (usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
            )
            outcome.reasoning.tokens = (
                (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
            )
            if kind == "response.incomplete":
                reason = (response.get("incomplete_details") or {}).get("reason", "")
                # The API's name for it is `max_output_tokens`; the rest of
                # Compass already reacts to the chat-completions name.
                outcome.finish_reason = (
                    "length" if "max_output_tokens" in reason else reason or "incomplete"
                )
            else:
                outcome.finish_reason = "tool_calls" if outcome.tool_calls else "stop"
            continue

        if kind in ("response.failed", "error"):
            response = event.get("response") or {}
            err = response.get("error") or event.get("error") or {}
            raise ResponsesStreamError(
                err.get("message") or "the model reported the response as failed"
            )


def parse_response(payload: dict[str, Any]) -> ResponsesOutcome:
    """Fold a non-streaming response into the same shape the stream produces.

    Design generates in one long call rather than a stream, but it wants the
    same three things out of it: the text, the reasoning to carry into the
    next step, and an honest reason when the answer was cut short. This is
    `consume` for a response that arrived all at once.
    """
    outcome = ResponsesOutcome()
    for item in payload.get("output") or []:
        kind = item.get("type")
        if kind == "reasoning":
            outcome.reasoning.items.append(item)
            parts = [p.get("text", "") for p in item.get("summary") or []]
            if parts:
                joined = "\n\n".join(p for p in parts if p)
                outcome.reasoning.summary += (
                    "\n\n" if outcome.reasoning.summary else "") + joined
        elif kind == "message":
            outcome.text += "".join(
                c.get("text", "") for c in item.get("content") or []
                if c.get("type") == "output_text"
            )
        elif kind == "function_call":
            outcome.tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "name": item.get("name") or "",
                "arguments": item.get("arguments") or "{}",
            })

    usage = payload.get("usage") or {}
    outcome.prompt_tokens = usage.get("input_tokens", 0) or 0
    outcome.completion_tokens = usage.get("output_tokens", 0) or 0
    outcome.cached_prompt_tokens = (
        (usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    )
    outcome.reasoning.tokens = (
        (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    )

    if payload.get("status") == "incomplete":
        reason = (payload.get("incomplete_details") or {}).get("reason", "")
        # Named the way the rest of Compass already reacts to it, rather than
        # left as this API's own spelling.
        outcome.finish_reason = (
            "length" if "max_output_tokens" in reason else reason or "incomplete"
        )
    else:
        outcome.finish_reason = "tool_calls" if outcome.tool_calls else "stop"
    return outcome


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Parse `data:` lines into event dicts, ignoring everything else."""
    async for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("unparseable event on the reasoning stream: %.120s", payload)
