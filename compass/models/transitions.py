"""Typed loop transitions — the port of query/transitions.ts.

Every way the agent loop can re-iterate is a `Continue` with a named reason;
every way it can end is a `Terminal`. Tests and telemetry assert on these
instead of inspecting message contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ContinueReason = Literal[
    "tool_results",           # model called tools; results appended
    "stop_hook",              # a Stop hook blocked termination
    "max_output_recovery",    # response truncated at max_output_tokens
    "reactive_compact",       # prompt-too-long -> emergency compact
    "fallback_model",         # primary deployment failed; retrying on fallback
]

TerminalReason = Literal[
    "end_turn",
    "max_turns",
    "aborted",
    "error",
]


@dataclass(frozen=True)
class Continue:
    reason: ContinueReason
    detail: str = ""


@dataclass(frozen=True)
class Terminal:
    reason: TerminalReason
    detail: str = ""
