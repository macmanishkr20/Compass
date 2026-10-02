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

from compass.common.gateway.cost_tracker import CostTracker
from compass.common.policy.permissions import Behavior, PermissionDecision
from compass.common.tools.shell_session import ShellState


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


#: How many expired request ids a broker remembers. Enough that a person who
#: walked away comes back to a straight answer, small enough that a long
#: session does not accumulate them. The ids are all that is kept.
EXPIRED_MEMORY = 64


class _Expiring:
    """Remembers request ids whose deadline passed, so a late answer can be
    told apart from a made-up one.

    Without this, a question that timed out and a request id that never
    existed are the same 404. They are not the same thing: one means "you took
    longer than five minutes and the turn went on without you", which a person
    can act on, and the other means "that id is not real". The turn cannot be
    resumed either way — it has already finished — but saying which happened is
    the difference between an explanation and a dead end.
    """

    def __init__(self) -> None:
        self._ids: list[str] = []

    def note(self, request_id: str) -> None:
        self._ids.append(request_id)
        if len(self._ids) > EXPIRED_MEMORY:
            del self._ids[: len(self._ids) - EXPIRED_MEMORY]

    def __contains__(self, request_id: str) -> bool:
        return request_id in self._ids


class PermissionBroker:
    """Bridges 'ask' verdicts to whatever surface is attached.

    The engine yields a PermissionRequest event and awaits resolve(); the
    FastAPI surface exposes resolve() as a REST endpoint, a CLI would wire it
    to stdin, tests auto-grant. This is the canUseTool seam."""

    def __init__(self, *, policy: str = "interactive") -> None:
        # policy: interactive | auto_grant | auto_deny (headless agents)
        self.policy = policy
        self._pending: dict[str, asyncio.Future[bool]] = {}
        # request_id -> (tool_name, primary_arg), so "allow always" can build a
        # session rule for the right tool/command.
        self._meta: dict[str, tuple[str, str]] = {}
        #: Ids whose deadline passed. Consulted only to answer a late caller.
        self.expired = _Expiring()
        # Session-scoped allow rules added by the user's "Always allow" choice;
        # consulted by check_permissions so matching calls stop prompting.
        self.session_rules: list = []

    def create(self, request_id: str, tool_name: str = "", primary: str = "") -> None:
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        self._meta[request_id] = (tool_name, primary)

    def resolve(self, request_id: str, allow: bool) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(allow)
        self._meta.pop(request_id, None)
        return True

    def outcome(self, request_id: str, allow: bool) -> str:
        """resolve(), but saying which of three things happened.

        "resumed" — the turn was waiting and has been answered.
        "late"    — the deadline had passed; the turn moved on without it.
        "unknown" — no such request, which stays a 404.
        """
        if self.resolve(request_id, allow):
            return "resumed"
        return "late" if request_id in self.expired else "unknown"

    def allow_always(self, request_id: str) -> bool:
        """Grant this request AND remember the choice for the session: future
        calls to the same tool (for bash, the same command prefix) auto-allow —
        claude.ai's 'Yes, and don't ask again' option."""
        from compass.common.config import PermissionRule

        tool_name, primary = self._meta.get(request_id, ("", ""))
        if tool_name:
            if tool_name == "bash" and primary.strip():
                pattern = primary.strip().split()[0] + " *"
            else:
                pattern = "*"
            self.session_rules.append(
                PermissionRule(tool=tool_name, pattern=pattern, action="allow")
            )
        return self.resolve(request_id, True)

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
            self.expired.note(request_id)
            return None
        finally:
            self._pending.pop(request_id, None)


class QuestionBroker:
    """Bridges a question the model wants answered to whoever is watching.

    The same seam as PermissionBroker and for the same reason: the loop stops
    on a future, a surface shows the question, and an endpoint resolves it.
    Kept apart from permissions because they are different acts — one asks may
    I, the other asks which — and a surface may well want to render them
    differently, as claude.ai does.

    `policy` is what to do when nobody is watching. A headless run cannot
    answer, and the honest response there is to say so rather than to invent a
    preference: `unattended` returns nothing and the model is told the question
    went unanswered, so it proceeds on its own judgement instead of stalling.
    """

    def __init__(self, *, policy: str = "interactive") -> None:
        self.policy = policy
        self._pending: dict[str, asyncio.Future[dict[str, Any] | None]] = {}
        #: Ids whose deadline passed. Consulted only to answer a late caller.
        self.expired = _Expiring()

    def create(self, request_id: str) -> None:
        self._pending[request_id] = asyncio.get_running_loop().create_future()

    def answer(self, request_id: str, reply: dict[str, Any] | None) -> bool:
        """Resolve a pending question. `None` is a deliberate skip."""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(reply)
        return True

    def outcome(self, request_id: str, reply: dict[str, Any] | None) -> str:
        """answer(), but saying whether the turn was still waiting for it.

        "resumed" — answered in time; the turn continues.
        "late"    — the question had already timed out and been skipped.
        "unknown" — no such question, which stays a 404.
        """
        if self.answer(request_id, reply):
            return "resumed"
        return "late" if request_id in self.expired else "unknown"

    async def wait(
        self, request_id: str, timeout: float
    ) -> dict[str, Any] | None:
        """The chosen answer, or None for skipped, unattended or timed out."""
        if self.policy != "interactive":
            self._pending.pop(request_id, None)
            return None
        future = self._pending[request_id]
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.expired.note(request_id)
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
    # Who this conversation belongs to. Carried so that anything a tool
    # stores can be found again by the person who made it, from another
    # browser or another machine — see compass/common/media_index.py.
    owner: str = ""
    agent_id: str | None = None  # set only inside subagent sidechains
    depth: int = 0
    # path -> mtime at last read; enforces read-before-edit and staleness
    # detection (fileStateCache analog).
    file_state: dict[str, float] = field(default_factory=dict)
    # Screenshots (data: URIs) the browser tool captured this turn; the loop
    # feeds them back to the model as a vision user-message after the tool
    # results, so the agent can SEE the page it's driving (computer-use).
    pending_vision: list[str] = field(default_factory=list)
    # session-scoped todo list (TodoWriteTool state)
    todos: list[dict[str, Any]] = field(default_factory=list)
    # Which held-back tools have been found by searching. Only used when the
    # catalogue is large enough that not all of it is described each request;
    # owned by the Session so a tool found on one turn is still there on the
    # next. See compass.common.tools.shelf.
    shelf: Any = None
    # Where a question to the person goes. Defaulted rather than required so
    # every existing caller that builds a context keeps working untouched.
    questions: QuestionBroker = field(default_factory=QuestionBroker)
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
            # The subagent is working for the same person. Without this, a
            # picture it draws or a screenshot it takes is filed under
            # nobody — and a sidechain is exactly where the long, tool-heavy
            # work happens, so it is most of what there would be to lose.
            owner=self.owner,
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
        from compass.common.config import get_settings

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

    def wants_answer(self, inp: BaseModel) -> dict[str, Any] | None:
        """A question to put to the person, or None to just run.

        Returning a payload makes the executor stop, show the question and
        wait, exactly as it does for a permission — the tool itself never
        runs. A hook rather than a name check in the executor, so asking is
        something any tool could do rather than a special case for one.
        """
        return None

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
        from compass.common.config import get_settings

        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        function: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": schema,
        }
        # Strict mode constrains the model's sampling to the schema, so a tool
        # call cannot arrive with a missing field or the wrong type. It is not
        # free: the schema has to be reshaped to a stricter subset, and the
        # model then sends an explicit null for anything it is not supplying —
        # which `_without_nulls` turns back into "not supplied" before the
        # input model sees it.
        if get_settings().tools.strict_schemas:
            function["parameters"] = strict_schema(schema)
            function["strict"] = True
        return {"type": "function", "function": function}



def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Reshape a JSON Schema into the subset strict mode accepts.

    Two rules, applied at every level: an object refuses properties it did not
    declare, and every property it declares is required. An optional field
    cannot simply be left out of `required`, so it becomes nullable instead —
    "not supplied" is expressed as null rather than as absence.

    The original schema is left alone; this returns a copy, because the
    unmodified one is still what validates the arguments that come back.
    """
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k != "title"}

    for key in ("items", "additionalItems"):
        if isinstance(out.get(key), dict):
            out[key] = strict_schema(out[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(out.get(key), list):
            out[key] = [strict_schema(x) for x in out[key]]
    if isinstance(out.get("$defs"), dict):
        out["$defs"] = {k: strict_schema(v) for k, v in out["$defs"].items()}

    properties = out.get("properties")
    if not isinstance(properties, dict):
        return out

    required = list(out.get("required") or [])
    rebuilt: dict[str, Any] = {}
    for name, spec in properties.items():
        spec = strict_schema(spec) if isinstance(spec, dict) else spec
        if name not in required and isinstance(spec, dict):
            spec = _nullable(spec)
        rebuilt[name] = spec
    out["properties"] = rebuilt
    out["required"] = list(properties)
    out["additionalProperties"] = False
    return out


def _nullable(spec: dict[str, Any]) -> dict[str, Any]:
    """Let a value be null, however the schema happens to state its type."""
    spec = dict(spec)
    # A default is meaningless once the field is always sent; the null carries
    # the same meaning and the input model supplies the default.
    spec.pop("default", None)
    if "type" in spec:
        kinds = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        if "null" not in kinds:
            spec["type"] = [*kinds, "null"]
    elif any(k in spec for k in ("anyOf", "oneOf")):
        key = "anyOf" if "anyOf" in spec else "oneOf"
        if not any(x.get("type") == "null" for x in spec[key] if isinstance(x, dict)):
            spec[key] = [*spec[key], {"type": "null"}]
    else:
        spec["type"] = ["string", "null"]
    return spec


def without_nulls(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is null, so the input model applies its defaults.

    Under strict mode the model must send every property, and says "nothing
    here" with null. An input model that declares `replace_all: bool = False`
    rejects null outright, so the null has to be removed rather than passed
    through. Harmless when strict mode is off: a null arriving for a field
    with a default was going to fail validation either way.
    """
    return {k: v for k, v in arguments.items() if v is not None}

def find_tool(tools: list[Tool], name: str) -> Tool | None:
    return next((t for t in tools if t.name == name), None)


def deny(reason: str) -> PermissionDecision:
    return PermissionDecision(Behavior.DENY, reason)
