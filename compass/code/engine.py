"""Session-level orchestration — port of QueryEngine.ts ask().

ask() owns the conversation: user_prompt_submit hooks, system prompt
assembly, transcript persistence, and delegation to query() for the agentic
turn. It also owns conversation lifecycle — rename/group/archive metadata and
transcript checkpoints (fork / edit-and-resend / regenerate / delete). It has
no idea what surface is consuming it — REPL, SSE, or a test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from compass.common.agent.query_loop import query
from compass.common.config import get_settings
from compass.code.system_prompt import build_system_prompt
from compass.common.gateway.cost_tracker import CostTracker
from compass.common.models import events
from compass.common.models.messages import Message, user_message
from compass.common.persistence.base import TranscriptStore
from compass.common.persistence.factory import get_transcript_store
from compass.common.persistence.session_meta import SessionMeta, get_meta_store
from compass.common.policy.hooks import HookEvent, get_hook_registry
from compass.common.attachments import build_user_message, transcribe_attachments
from compass.common.tools.base import PermissionBroker, ToolUseContext, QuestionBroker
from compass.code.tools.registry import get_all_tools
from compass.common.tools.shelf import Shelf
from compass.common.tools.shell_session import ShellState


@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[Message] = field(default_factory=list)
    broker: PermissionBroker = field(default_factory=PermissionBroker)
    # Questions the model puts to the person. Owned by the session for the
    # same reason the permission broker is: the answer arrives on a different
    # request from the one that is waiting for it.
    questions: QuestionBroker = field(default_factory=QuestionBroker)
    cost_tracker: CostTracker = field(default_factory=CostTracker)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    permission_mode: str | None = None
    effort: str | None = None  # minimal|low|medium|high; None = server default
    model: str | None = None  # deployment override; None = server default
    workspace_id: str | None = None
    workspace_root: "Path | None" = None
    shell_state: ShellState = field(default_factory=ShellState)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Which held-back tools have been found by searching. Owned here rather
    # than rebuilt per turn: a tool found once should stay found.
    shelf: "Shelf | None" = None
    #: Run this session with a different tool set and prompt. Both default to
    #: None, which is the Code agent exactly as it was — these exist so the
    #: Pipelines builder can borrow the loop without inheriting a workspace
    #: agent's tools, which would let it edit files while it means to edit a
    #: graph.
    tool_override: list | None = None
    prompt_override: str | None = None
    #: Who this conversation belongs to, carried from the request that created
    #: it so the metadata written mid-turn can be stamped without threading a
    #: user through the engine. Empty means unowned; see
    #: `compass.common.ownership`.
    owner: str = ""

    def make_context(self) -> ToolUseContext:
        from compass.code.mcp.manager import get_mcp_manager
        from compass.common.tools.shelf import attach

        if self.workspace_root is not None:
            self.shell_state.root = str(self.workspace_root)
        base = (self.tool_override if self.tool_override is not None
                else [*get_all_tools(), *get_mcp_manager().tools])
        catalogue, shelf = attach(base)
        if self.shelf is None:
            self.shelf = shelf
        else:
            # Keep what earlier turns found; the search tool built above is
            # bound to this turn's catalogue, which may have gained or lost
            # an MCP server since.
            shelf.found = self.shelf.found
            self.shelf = shelf
        return ToolUseContext(
            session_id=self.id,
            tools=catalogue,
            shelf=self.shelf,
            broker=self.broker,
            questions=self.questions,
            cost_tracker=self.cost_tracker,
            abort_event=self.abort_event,
            permission_mode=self.permission_mode,
            workspace_root=self.workspace_root,
            shell_state=self.shell_state,
        )


def _title_from(text: str) -> str:
    clean = " ".join(text.split())
    return clean[:60] + ("…" if len(clean) > 60 else "")


logger = logging.getLogger("compass.code")


@dataclass
class MissionOffer:
    """"This looks like a build — start it as a mission?"

    Local to this surface rather than added to the shared events, the same
    reasoning as Home's Work IQ event: a client that does not know the type
    ignores it, and nothing about any other stream changes. Carries the
    person's own words as the goal, because they will see them again on the
    card and in the mission, and a paraphrase invites an argument about what
    they meant.
    """

    goal: str
    reason: str

    def to_sse(self) -> str:
        payload = json.dumps({"type": "mission_offer", "goal": self.goal,
                              "reason": self.reason})
        return f"event: mission_offer\ndata: {payload}\n\n"


def _mission_offer(prompt: str) -> "MissionOffer | None":
    """An offer, or None — and None whenever the module is off, so a build
    without Missions never mentions it."""
    if not get_settings().missions.enabled:
        return None
    try:
        from compass.missions.suggest import goal_from, looks_like_a_mission

        worth_it, reason = looks_like_a_mission(prompt)
        return MissionOffer(goal=goal_from(prompt), reason=reason) if worth_it else None
    except Exception:  # noqa: BLE001 — a suggestion must never cost a turn
        logger.warning("could not check for a mission offer", exc_info=True)
        return None


class QueryEngine:
    def __init__(self, store: TranscriptStore | None = None) -> None:
        self.store = store or get_transcript_store()
        self.meta = get_meta_store()

    # -- session lifecycle ---------------------------------------------------

    async def resume(self, session_id: str, **kwargs) -> Session:
        session = Session(id=session_id, **kwargs)
        session.messages = await self.store.load(session_id)
        meta = await self.meta.get(session_id)
        if meta is not None:
            session.permission_mode = kwargs.get("permission_mode") or meta.mode
            session.effort = kwargs.get("effort") or meta.effort
            session.model = kwargs.get("model") or meta.model or None
            session.workspace_id = kwargs.get("workspace_id") or meta.workspace or None
            session.owner = meta.owner
        await self._attach_workspace(session)
        return session

    async def _attach_workspace(self, session: Session) -> None:
        """Resolve the session's workspace id to an absolute root the tools
        can be scoped to."""
        from compass.common.workspaces import get_workspace_registry

        session.workspace_root = await get_workspace_registry().resolve_root(
            session.workspace_id
        )

    async def ensure_meta(self, session_id: str, owner: str = "") -> SessionMeta:
        meta = await self.meta.get(session_id)
        if meta is None:
            # Stamped at creation only. Re-stamping here would let whoever
            # opened a conversation next take it over.
            meta = SessionMeta(id=session_id, owner=owner)
            await self.meta.upsert(meta)
        return meta

    async def update_meta(self, session_id: str, **fields) -> SessionMeta:
        meta = await self.ensure_meta(session_id)
        for key, value in fields.items():
            if value is not None and hasattr(meta, key):
                setattr(meta, key, value)
        meta.updated_at = time.time()
        await self.meta.upsert(meta)
        return meta

    async def delete_session(self, session_id: str) -> None:
        await self.store.delete(session_id)
        await self.meta.delete(session_id)

    async def _bump_meta(self, session: Session, first_prompt: str | None) -> None:
        meta = await self.ensure_meta(session.id, session.owner)
        if first_prompt and not meta.title:
            meta.title = _title_from(first_prompt)
        meta.message_count = sum(1 for m in session.messages if m.role == "user")
        meta.mode = session.permission_mode or meta.mode
        meta.effort = session.effort or meta.effort
        if session.model:
            meta.model = session.model
        if session.workspace_id:
            meta.workspace = session.workspace_id
        meta.updated_at = time.time()
        await self.meta.upsert(meta)

    # -- running turns -------------------------------------------------------

    async def ask(
        self,
        session: Session,
        user_input: str,
        attachments: list[dict] | None = None,
    ) -> AsyncIterator[events.Event]:
        async with session.turn_lock:
            session.abort_event.clear()
            hooks = get_hook_registry()
            if not session.messages:
                await hooks.run(HookEvent.SESSION_START, {"session_id": session.id})
            outcome = await hooks.run(
                HookEvent.USER_PROMPT_SUBMIT,
                {"session_id": session.id, "prompt": user_input},
            )
            if outcome.blocked:
                yield events.ErrorEvent(message=f"prompt blocked by hook: {outcome.reason}")
                yield events.TurnComplete(reason="error", detail="prompt blocked")
                return

            is_first = not session.messages
            # Uploaded files (images -> gpt-5 vision, PDF/DOCX/ZIP/text ->
            # inlined) are folded into the user message by the shared builder;
            # with no attachments this is exactly user_message(user_input).
            message = (
                build_user_message(user_input, await transcribe_attachments(attachments))
                if attachments
                else user_message(user_input)
            )
            session.messages.append(message)
            self.store.append(session.id, message)
            await self._bump_meta(session, user_input if is_first else None)

            # A brief that reads like hours of work is offered to the
            # Missions harness rather than quietly becoming one. Yielded
            # before the turn so the card sits with the request it is about,
            # and the turn runs regardless: the offer is a second option, not
            # a fork in the road.
            if offer := _mission_offer(user_input):
                yield offer

            async for event in self._run_turn(session):
                yield event

    async def _run_turn(self, session: Session) -> AsyncIterator[events.Event]:
        """Shared turn runner — assumes session.messages already ends with the
        input to respond to. Used by ask(), edit, and regenerate."""
        ctx = session.make_context()
        # Memory: this project's entries (plus the global ones) are prepended so
        # the model starts the turn already knowing what it has learned.
        from compass.common.memory import GLOBAL_SCOPE, memory_prompt

        mem = await memory_prompt(session.workspace_id or GLOBAL_SCOPE)
        base_prompt = session.prompt_override or build_system_prompt(
            role="main", workspace_root=session.workspace_root
        )
        try:
            async for event in query(
                session.messages,
                ctx,
                system_prompt=f"{base_prompt}\n\n{mem}" if mem else base_prompt,
                effort=session.effort,
                model=session.model,
                on_message=lambda m: self.store.append(session.id, m),
            ):
                yield event
        finally:
            await self.store.flush()
            await self._bump_meta(session, None)

    # -- checkpoints: edit / regenerate / fork -------------------------------

    async def edit_message(
        self, session: Session, message_uuid: str, new_text: str
    ) -> AsyncIterator[events.Event]:
        """Edit a past user message: truncate the transcript to everything
        before it, then resend the edited text as a fresh prompt. The removed
        tail (old answer + anything after) is discarded — a checkpoint."""
        idx = next(
            (i for i, m in enumerate(session.messages) if m.uuid == message_uuid),
            None,
        )
        if idx is None:
            yield events.ErrorEvent(message="message not found")
            yield events.TurnComplete(reason="error", detail="unknown message")
            return
        session.messages = session.messages[:idx]
        await self.store.overwrite(session.id, session.messages)
        async for event in self.ask(session, new_text):
            yield event

    async def regenerate(self, session: Session) -> AsyncIterator[events.Event]:
        """Re-run the most recent user turn: drop everything after the last
        user message (the previous answer and its tool calls), then resend."""
        last_user = next(
            (i for i in range(len(session.messages) - 1, -1, -1)
             if session.messages[i].role == "user"
             and not session.messages[i].meta.get("synthetic")),
            None,
        )
        if last_user is None:
            yield events.ErrorEvent(message="nothing to regenerate")
            yield events.TurnComplete(reason="error", detail="no user message")
            return
        async with session.turn_lock:
            session.abort_event.clear()
            session.messages = session.messages[: last_user + 1]
            await self.store.overwrite(session.id, session.messages)
            async for event in self._run_turn(session):
                yield event

    async def fork(self, session_id: str, up_to_uuid: str | None = None) -> str:
        """Branch a conversation into a new session, copying history up to and
        including `up_to_uuid` (or the whole thing). Returns the new id."""
        messages = await self.store.load(session_id)
        if up_to_uuid is not None:
            idx = next(
                (i for i, m in enumerate(messages) if m.uuid == up_to_uuid), None
            )
            if idx is not None:
                messages = messages[: idx + 1]
        new_id = str(uuid.uuid4())
        await self.store.overwrite(new_id, messages)
        src = await self.meta.get(session_id)
        base_title = (src.title if src else "") or "Conversation"
        await self.meta.upsert(
            SessionMeta(
                id=new_id,
                title=f"Fork · {base_title}"[:60],
                group=src.group if src else "",
                mode=src.mode if src else "default",
                effort=src.effort if src else "medium",
                message_count=sum(1 for m in messages if m.role == "user"),
                owner=src.owner if src else "",
            )
        )
        return new_id
