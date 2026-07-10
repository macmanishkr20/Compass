"""The Tool contract and ToolUseContext — port of Tool.ts.

Everything the scheduler and the permission gate need lives on this one
interface. The two predicates take the *parsed input*, not just the tool:
`bash` is concurrency-safe for `git status` and serial for `rm -rf`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar

from pydantic import BaseModel

from compass.gateway.cost_tracker import CostTracker
from compass.policy.permissions import Behavior, PermissionDecision
from compass.tools.shell_session import ShellState


def _new_shell_state() -> ShellState:
    return ShellState()


@dataclass
class Progress:
    """Intermediate output streamed while a tool runs (bash stdout, subagent
    activity). Rendered live by surfaces, never sent to the model."""

    data: str


@dataclass
class ToolOutput:
    """Terminal yield of Tool.call — becomes the tool_result message."""

    content: str
    is_error: bool = False


ToolYield = Progress | ToolOutput


class PermissionBroker:
    """Bridges 'ask' verdicts to whatever surface is attached.

    The engine yields a PermissionRequest event and awaits resolve(); the
    FastAPI surface exposes resolve() as a REST endpoint, a CLI would wire it
    to stdin, tests auto-grant. This is the canUseTool seam."""

    def __init__(self, *, policy: str = "interactive") -> None:
        # policy: interactive | auto_grant | auto_deny (headless agents)
        self.policy = policy
        self._pending: dict[str, asyncio.Future[bool]] = {}

    def create(self, request_id: str) -> None:
        self._pending[request_id] = asyncio.get_running_loop().create_future()

    def resolve(self, request_id: str, allow: bool) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(allow)
        return True

    async def wait(self, request_id: str, timeout: float) -> bool | None:
        """True=granted, False=denied, None=timed out."""
        if self.policy == "auto_grant":
            self._pending.pop(request_id, None)
            return True
        if self.policy == "auto_deny":
            self._pending.pop(request_id, None)
            return False
        future = self._pending[request_id]
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(request_id, None)


@dataclass
class ToolUseContext:
    """Carried through the whole generator chain — port of ToolUseContext."""

    session_id: str
    tools: list["Tool"]
    broker: PermissionBroker
    cost_tracker: CostTracker
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    permission_mode: str | None = None
    # Root directory this session's file tools and shell are scoped to. When
    # None, tools fall back to the global workspace (get_settings). Set from
    # the session's selected workspace.
    workspace_root: "Path | None" = None
    agent_id: str | None = None  # set only inside subagent sidechains
    depth: int = 0
    # path -> mtime at last read; enforces read-before-edit and staleness
    # detection (fileStateCache analog).
    file_state: dict[str, float] = field(default_factory=dict)
    # session-scoped todo list (TodoWriteTool state)
    todos: list[dict[str, Any]] = field(default_factory=list)
    # persistent shell working directory (Shell.ts cwd-tracking analog); a `cd`
    # in one bash call is visible to the next. Owned by the Session so it
    # survives across turns.
    shell_state: "ShellState" = field(default_factory=lambda: _new_shell_state())
    # chainId/depth telemetry (queryTracking analog)
    chain_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)

    def child_for_subagent(self, tools: list["Tool"]) -> "ToolUseContext":
        """Subagent context: fresh sidechain identity, inherited trust plumbing
        (broker, abort, cost tracker) so permission prompts bubble to the
        parent surface and cancellation propagates down. The subagent gets its
        own shell cwd (isolated from the parent's), seeded at the workspace
        root."""
        return ToolUseContext(
            session_id=self.session_id,
            tools=tools,
            broker=self.broker,
            cost_tracker=self.cost_tracker,
            abort_event=self.abort_event,
            permission_mode=self.permission_mode,
            workspace_root=self.workspace_root,
            agent_id=str(uuid.uuid4())[:8],
            depth=self.depth + 1,
            chain_id=self.chain_id,
        )

    def effective_root(self) -> Path:
        from compass.config import get_settings

        return self.workspace_root or get_settings().workspace_root


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]

    def validate_input(self, arguments: dict[str, Any]) -> Any:
        """Parse raw arguments into the shape call() expects. Raises
        pydantic.ValidationError (or ValueError) on bad input. MCP tools
        override this — their schema is opaque JSON Schema, validated by
        the server itself."""
        return self.input_model.model_validate(arguments)

    def is_read_only(self, inp: BaseModel) -> bool:
        return False

    def is_concurrency_safe(self, inp: BaseModel) -> bool:
        # Same default as Claude Code: read-only implies parallelizable;
        # tools override for finer grain (bash inspects the command).
        try:
            return self.is_read_only(inp)
        except Exception:  # noqa: BLE001 — conservative on predicate failure
            return False

    def check_tool_permissions(
        self, inp: BaseModel, ctx: ToolUseContext
    ) -> PermissionDecision | None:
        """Tool-specific hard verdicts that pre-empt the rule engine
        (e.g. path escapes the workspace). None defers to rules/modes."""
        return None

    @abstractmethod
    def call(self, inp: BaseModel, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        """Async generator: zero or more Progress, then exactly one ToolOutput."""
        ...

    def to_openai_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


def find_tool(tools: list[Tool], name: str) -> Tool | None:
    return next((t for t in tools if t.name == name), None)


def deny(reason: str) -> PermissionDecision:
    return PermissionDecision(Behavior.DENY, reason)
