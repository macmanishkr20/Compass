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
    "running on Azure OpenAI (gpt-5). This is a plain chat: you have no tools, "
    "no file access, and cannot run commands or make changes. Just talk with "
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
        """Conversation cards for the Home sidebar: id + a title derived from
        the first user message + file timestamps (most-recent first)."""
        cards: list[dict] = []
        for p in self._dir().glob("*.jsonl"):
            st = p.stat()
            cards.append(
                {
                    "id": p.stem,
                    "title": _first_user_title(p) or "New chat",
                    "updated_at": st.st_mtime,
                    "created_at": getattr(st, "st_birthtime", st.st_ctime),
                }
            )
        cards.sort(key=lambda c: c["updated_at"], reverse=True)
        return cards

    async def delete(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)


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
        """A tool-free context. The broker is auto_deny as a belt-and-braces
        guard — with no tools it can never be consulted, but should the loop
        ever try, nothing is silently granted."""
        return ToolUseContext(
            session_id=self.id,
            tools=[],
            broker=PermissionBroker(policy="auto_deny"),
            cost_tracker=self.cost_tracker,
            abort_event=self.abort_event,
            permission_mode=None,
            workspace_root=None,
        )


class ChatEngine:
    def __init__(self) -> None:
        self.store = ChatStore()

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
            message = build_user_message(user_input, attachments)
            session.messages.append(message)
            self.store.append(session.id, message)

            # Work IQ (Home-only, opt-in): retrieve from Azure AI Search and
            # ground this turn. The context lives in the per-turn system prompt
            # (not persisted), so history stays clean; sources go to the UI.
            system_prompt = CHAT_SYSTEM_PROMPT
            if work_iq:
                from compass.services import work_iq as wiq

                if wiq.configured():
                    docs = await wiq.hybrid_search(user_input)
                    system_prompt = wiq.WORK_IQ_SYSTEM_PROMPT.format(
                        context=wiq.format_context(docs)
                    )
                    if docs:
                        yield _WorkIqSources(sources=wiq.sources_for_ui(docs))

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
                # A rejected turn (e.g. an image Azure can't parse) must not
                # poison the thread: every later turn re-sends the whole
                # history. Drop the user message we just added so the next
                # turn starts clean, and report the failure inline.
                try:
                    session.messages.remove(message)
                except ValueError:
                    pass
                yield events.ErrorEvent(message=str(err))
                yield events.TurnComplete(reason="error", detail="chat turn failed")
            finally:
                await self.store.flush()

    def abort(self, session: ChatSession) -> None:
        session.abort_event.set()
