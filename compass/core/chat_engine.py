"""Home/Chat workflow — a separate, tool-free conversational engine.

This is deliberately NOT the agent console. `QueryEngine` (query_engine.py)
owns the Code/Agent Console: all tools, the permission gate, workspace
scoping, routines, git/PR. `ChatEngine` here owns the Home/Chat section and
shares none of that — it reuses only the shared low-level `query()` streaming
loop, invoked with an **empty tool list** and a conversational system prompt.

Consequences of `tools=[]`:
  * `run_tools` never fires — the model can only produce text.
  * no `PermissionRequest` is ever emitted (nothing to approve).
  * no workspace root is attached — pure conversation, no file access.

Chat transcripts persist to their own `sessions_dir/chat/` namespace so they
never appear in the agent's Conversations list.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from compass.config import get_settings
from compass.core.query_loop import query
from compass.gateway.cost_tracker import CostTracker
from compass.models import events
from compass.models.messages import Message
from compass.services.attachments import build_user_message
from compass.tools.base import PermissionBroker, ToolUseContext

@dataclass
class _WorkIqSources:
    """A one-off SSE event carrying the Work IQ retrieved sources to the UI.
    Local to the chat surface (not in the shared events module) so nothing about
    the agent console changes."""

    sources: list[dict]

    def to_sse(self) -> str:
        payload = json.dumps({"type": "work_iq_sources", "sources": self.sources})
        return f"event: work_iq_sources\ndata: {payload}\n\n"


CHAT_SYSTEM_PROMPT = (
    "You are Compass Chat — a friendly, knowledgeable conversational assistant "
    "running on Azure OpenAI (gpt-5). This is a plain chat: your only tool is "
    "`memory` (to remember durable facts about the user). You have no file "
    "access and cannot run commands or make changes. Just talk with "
    "the user — answer questions, brainstorm, explain, draft, and reason things "
    "through in clear, well-structured Markdown. If a request genuinely needs "
    "running code, editing files, inspecting a repository, or executing tools, "
    "say so briefly and point the user to the Code (Agent Console) section, "
    "where Compass can act with tools and your approval. Be concise by default "
    "and expand when the user wants depth."
)


def _content_text(content: Any) -> str:
    """Plain text from a message's content (a string, or multimodal parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _first_user_title(path: "Path") -> str:
    """First user message's text, trimmed to a short title — read lazily so a
    transcript with big base64 image parts isn't loaded whole."""
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("role") == "user":
                    text = " ".join(_content_text(rec.get("content")).split())
                    return text[:60] + ("…" if len(text) > 60 else "")
    except Exception:  # noqa: BLE001
        pass
    return ""


class ChatStore:
    """A tiny JSONL transcript store isolated under sessions_dir/chat/, so chat
    threads live entirely apart from the agent console's transcripts."""

    def _dir(self) -> Path:
        d = get_settings().sessions_dir / "chat"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, session_id: str) -> Path:
        return self._dir() / f"{session_id}.jsonl"

    # -- per-thread metadata (title override + starred), a tiny JSON index ----
    def _meta_path(self) -> Path:
        return self._dir() / "_meta.json"

    def _read_meta(self) -> dict[str, dict]:
        p = self._meta_path()
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_meta(self, meta: dict[str, dict]) -> None:
        self._meta_path().write_text(json.dumps(meta))

    async def set_meta(
        self, session_id: str, *, title: str | None = None, pinned: bool | None = None
    ) -> None:
        meta = self._read_meta()
        entry = meta.setdefault(session_id, {})
        if title is not None:
            entry["title"] = title
        if pinned is not None:
            entry["pinned"] = pinned
        self._write_meta(meta)

    def append(self, session_id: str, message: Message) -> None:
        with self._path(session_id).open("a") as f:
            f.write(json.dumps(message.to_record(), default=str) + "\n")

    async def flush(self) -> None:
        return None

    async def load(self, session_id: str) -> list[Message]:
        path = self._path(session_id)
        if not path.is_file():
            return []
        out: list[Message] = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(Message.from_record(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
        return out

    async def exists(self, session_id: str) -> bool:
        return self._path(session_id).is_file()

    async def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in self._dir().glob("*.jsonl"))

    async def list_cards(self) -> list[dict]:
        """Conversation cards for the Home sidebar: id + a title (a manual
        rename overrides the one derived from the first user message) + starred
        flag + file timestamps (most-recent first)."""
        meta = self._read_meta()
        cards: list[dict] = []
        for p in self._dir().glob("*.jsonl"):
            st = p.stat()
            m = meta.get(p.stem, {})
            cards.append(
                {
                    "id": p.stem,
                    "title": m.get("title") or _first_user_title(p) or "New chat",
                    "pinned": bool(m.get("pinned")),
                    "updated_at": st.st_mtime,
                    "created_at": getattr(st, "st_birthtime", st.st_ctime),
                }
            )
        cards.sort(key=lambda c: c["updated_at"], reverse=True)
        return cards

    async def delete(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)
        meta = self._read_meta()
        if meta.pop(session_id, None) is not None:
            self._write_meta(meta)

    async def rewrite(self, session_id: str, messages: list[Message]) -> None:
        """Overwrite a transcript with `messages` — used by regenerate/edit,
        which truncate the thread before re-answering (the append-only log
        can't otherwise drop the messages that were rolled back)."""
        with self._path(session_id).open("w") as f:
            for m in messages:
                f.write(json.dumps(m.to_record(), default=str) + "\n")


def get_chat_store():
    """Pick the Home/Chat backend: Azure Cosmos DB when configured, else the
    local JSONL store — the same config-or-fallback contract the agent
    transcript store uses. Absent Cosmos credentials, nothing changes."""
    cfg = get_settings().storage
    if cfg.backend == "cosmos" and cfg.cosmos_configured:
        from compass.persistence.chat_cosmos import CosmosChatStore

        return CosmosChatStore()
    return ChatStore()


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[Message] = field(default_factory=list)
    cost_tracker: CostTracker = field(default_factory=CostTracker)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    effort: str | None = None
    model: str | None = None
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def make_context(self) -> ToolUseContext:
        """Home's context carries exactly ONE tool: `memory`, so Compass can
        save what it learns while you chat (Claude's memory behaviour). It
        touches no files, runs no commands and needs no approval — every other
        tool stays out, so Home remains pure conversation. The broker is still
        auto_deny as a belt-and-braces guard: `memory` is read-only so it is
        auto-allowed and the broker is never consulted, but anything that
        somehow asked for permission is refused rather than silently granted."""
        from compass.tools.memory import MemoryTool

        return ToolUseContext(
            session_id=self.id,
            tools=[MemoryTool()],
            broker=PermissionBroker(policy="auto_deny"),
            cost_tracker=self.cost_tracker,
            abort_event=self.abort_event,
            permission_mode=None,
            workspace_root=None,
        )


class ChatEngine:
    def __init__(self) -> None:
        self.store = get_chat_store()

    async def resume(self, session_id: str, **kwargs) -> ChatSession:
        session = ChatSession(id=session_id, **kwargs)
        session.messages = await self.store.load(session_id)
        return session

    async def ask(
        self,
        session: ChatSession,
        user_input: str,
        attachments: list[dict] | None = None,
        work_iq: bool = False,
    ) -> AsyncIterator[events.Event]:
        async with session.turn_lock:
            session.abort_event.clear()
            rollback = list(session.messages)
            message = build_user_message(user_input, attachments)
            session.messages.append(message)
            self.store.append(session.id, message)
            async for ev in self._answer(session, user_input, work_iq, rollback):
                yield ev

    async def regenerate(
        self, session: ChatSession, work_iq: bool = False
    ) -> AsyncIterator[events.Event]:
        """Re-answer the last user turn (claude.ai's Retry): drop the trailing
        assistant reply, then run the model again on the same prompt."""
        async with session.turn_lock:
            session.abort_event.clear()
            while session.messages and session.messages[-1].role == "assistant":
                session.messages.pop()
            if not session.messages or session.messages[-1].role != "user":
                return
            await self.store.rewrite(session.id, session.messages)
            rollback = list(session.messages)
            query_text = _content_text(session.messages[-1].content)
            async for ev in self._answer(session, query_text, work_iq, rollback):
                yield ev

    async def edit(
        self,
        session: ChatSession,
        index: int,
        new_text: str,
        work_iq: bool = False,
    ) -> AsyncIterator[events.Event]:
        """Edit a prior user message and resend (claude.ai's Edit): truncate the
        thread at that message, replace it, and re-answer from there. `index` is
        the message's position — Home chat is strictly alternating user/assistant
        so the client's bubble order maps 1:1 onto the stored messages."""
        async with session.turn_lock:
            session.abort_event.clear()
            if index < 0 or index >= len(session.messages):
                return
            if session.messages[index].role != "user":
                return
            del session.messages[index:]
            message = build_user_message(new_text, None)
            session.messages.append(message)
            await self.store.rewrite(session.id, session.messages)
            rollback = list(session.messages)
            async for ev in self._answer(session, new_text, work_iq, rollback):
                yield ev

    async def _answer(
        self,
        session: ChatSession,
        query_text: str,
        work_iq: bool,
        rollback: list[Message],
    ) -> AsyncIterator[events.Event]:
        """Run one model turn over the current history. `rollback` is the state
        to restore (and rewrite to disk) if the turn errors, so a failed turn
        never poisons the thread that later turns re-send in full."""
        # Work IQ (Home-only, opt-in): retrieve from Azure AI Search and ground
        # this turn. The context lives in the per-turn system prompt (not
        # persisted), so history stays clean; sources go to the UI.
        system_prompt = CHAT_SYSTEM_PROMPT
        if work_iq:
            from compass.services import work_iq as wiq

            if wiq.configured():
                docs = await wiq.hybrid_search(query_text)
                system_prompt = wiq.WORK_IQ_SYSTEM_PROMPT.format(
                    context=wiq.format_context(docs)
                )
                if docs:
                    yield _WorkIqSources(sources=wiq.sources_for_ui(docs))

        # Memory: appended last so it survives the Work IQ prompt swap above.
        # Home reads the global scope — what the user tells Compass here is
        # remembered across chats (Claude's memory behaviour).
        from compass.services.memory import GLOBAL_SCOPE, memory_prompt

        mem = await memory_prompt(GLOBAL_SCOPE)
        if mem:
            system_prompt = f"{system_prompt}\n\n{mem}"

        ctx = session.make_context()
        try:
            async for event in query(
                session.messages,
                ctx,
                system_prompt=system_prompt,
                effort=session.effort,
                model=session.model,
                on_message=lambda m: self.store.append(session.id, m),
            ):
                yield event
        except Exception as err:  # noqa: BLE001 — surface as an in-stream event
            session.messages[:] = rollback
            await self.store.rewrite(session.id, session.messages)
            yield events.ErrorEvent(message=str(err))
            yield events.TurnComplete(reason="error", detail="chat turn failed")
        finally:
            await self.store.flush()

    def abort(self, session: ChatSession) -> None:
        session.abort_event.set()
