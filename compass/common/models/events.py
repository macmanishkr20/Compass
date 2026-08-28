"""Stream events yielded by the engine and forwarded to surfaces as SSE.

The generator chain (gateway -> query loop -> query engine -> API surface)
yields these; nothing in the chain buffers. `agent_id` is set when an event
originates inside a subagent sidechain.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Event:
    agent_id: str | None = field(default=None, kw_only=True)

    @property
    def type(self) -> str:
        return _TYPE_NAMES[self.__class__]

    def to_sse(self) -> str:
        data = asdict(self)
        data["type"] = self.type
        return f"event: {self.type}\ndata: {json.dumps(data, default=str)}\n\n"


@dataclass
class StreamRequestStart(Event):
    turn: int = 0
    model: str = ""


@dataclass
class TextDelta(Event):
    text: str = ""


@dataclass
class ThinkingDelta(Event):
    """A fragment of the model's reasoning, as it is produced.

    Kept apart from TextDelta all the way to the surface because it is a
    different kind of content: the model's working, not its answer. A turn the
    model answered directly carries none of these, which is normal — a
    reasoning model decides per request whether thinking helps.
    """

    text: str = ""
    #: Starts a new paragraph of reasoning rather than continuing one.
    starts_part: bool = False


@dataclass
class ToolArguments(Event):
    """A tool call's arguments arriving, before the call is complete.

    Display only. The text is unvalidated and may be cut off mid-string, so
    nothing may be executed from it — the call that runs is the finished one
    that arrives afterwards. This exists so a large parameter, a file being
    written or a document being composed, is visible while it happens instead
    of after it.
    """

    tool_call_id: str = ""
    tool_name: str = ""
    #: Raw JSON text to append to whatever arrived before it.
    delta: str = ""


@dataclass
class ServerToolUsed(Event):
    """Something Azure did inside the turn, rather than something Compass ran.

    A web search or a snippet of Python executes server-side and never comes
    back to the loop, so none of the tool events fire for it. Without this the
    surface cannot tell an answer that quietly rested on three web pages from
    one the model produced out of memory — and those are not the same claim.
    """

    #: "web_search" or "code_interpreter".
    tool: str = ""
    #: One line saying what it did, already written for a reader.
    detail: str = ""
    #: Pages actually opened, when it opened any.
    sources: list[str] = field(default_factory=list)


@dataclass
class ThinkingComplete(Event):
    """What the finished reasoning cost, once the turn's thinking is done.

    Reasoning is billed as output and shares the output cap with the answer,
    so `tokens` is the number that explains a turn that ran out of room.
    """

    tokens: int = 0
    #: Characters of summary shown. Never equal to what was billed: the
    #: summary is a readable account of the reasoning, not the reasoning.
    summary_chars: int = 0


@dataclass
class AssistantMessage(Event):
    uuid: str = ""
    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None


@dataclass
class ToolCallStarted(Event):
    tool_call_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolProgress(Event):
    tool_call_id: str = ""
    data: str = ""


@dataclass
class ToolResult(Event):
    tool_call_id: str = ""
    tool_name: str = ""
    content: str = ""
    is_error: bool = False
    duration_ms: int = 0


@dataclass
class PermissionRequest(Event):
    request_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class PermissionResolved(Event):
    request_id: str = ""
    behavior: str = ""  # "allow" | "deny" | "timeout"


@dataclass
class Compaction(Event):
    stage: str = ""  # "tool_result_budget" | "microcompact" | "autocompact" | "reactive"
    tokens_before: int = 0
    tokens_after: int = 0


@dataclass
class UsageReport(Event):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class TurnComplete(Event):
    reason: str = ""
    detail: str = ""
    turns: int = 0


@dataclass
class Refused(Event):
    """The model declined, rather than failed.

    A refusal arrives as a perfectly successful response, so it is not an
    error and is not reported as one: nothing went wrong with the request. It
    is its own event so a surface can say what happened and, when some of the
    answer was already shown, that what is on screen is only the part written
    before the turn was stopped.
    """

    message: str = ""
    #: What the filter flagged, when it says. Often empty.
    category: str = ""
    #: Text had already been streamed when the turn was stopped.
    partial: bool = False


@dataclass
class ErrorEvent(Event):
    message: str = ""


_TYPE_NAMES = {
    StreamRequestStart: "stream_request_start",
    TextDelta: "text_delta",
    ThinkingDelta: "thinking_delta",
    ThinkingComplete: "thinking_complete",
    ServerToolUsed: "server_tool_used",
    ToolArguments: "tool_arguments",
    AssistantMessage: "assistant_message",
    ToolCallStarted: "tool_call_started",
    ToolProgress: "tool_progress",
    ToolResult: "tool_result",
    PermissionRequest: "permission_request",
    PermissionResolved: "permission_resolved",
    Compaction: "compaction",
    UsageReport: "usage_report",
    Refused: "refused",
    TurnComplete: "turn_complete",
    ErrorEvent: "error",
}
