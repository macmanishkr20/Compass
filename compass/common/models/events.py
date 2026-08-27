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
class ErrorEvent(Event):
    message: str = ""


_TYPE_NAMES = {
    StreamRequestStart: "stream_request_start",
    TextDelta: "text_delta",
    ThinkingDelta: "thinking_delta",
    ThinkingComplete: "thinking_complete",
    AssistantMessage: "assistant_message",
    ToolCallStarted: "tool_call_started",
    ToolProgress: "tool_progress",
    ToolResult: "tool_result",
    PermissionRequest: "permission_request",
    PermissionResolved: "permission_resolved",
    Compaction: "compaction",
    UsageReport: "usage_report",
    TurnComplete: "turn_complete",
    ErrorEvent: "error",
}
