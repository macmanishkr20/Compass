"""Home/Chat workflow — a separate conversational engine with almost no tools.

This is deliberately NOT the agent console. `QueryEngine` (query_engine.py)
owns the Code/Agent Console: all tools, the permission gate, workspace
scoping, routines, git/PR. `ChatEngine` here owns the Home/Chat section and
shares none of that — it reuses only the shared low-level `query()` streaming
loop, with a short tool list and a conversational system prompt.

The list is three, and what they have in common is that none of them can
reach anything of yours: `memory` remembers, `web_fetch` reads a page, and
`make_video` cuts a film out of the files attached to the conversation it is
running in (compass/home/video_tool.py).
  * no file, shell or workspace tool is ever offered, so Home cannot read or
    change anything on disk.
  * no `PermissionRequest` is ever emitted: the two read-only tools are
    auto-allowed, and `make_video` allows itself for its own directory, so
    the auto_deny broker is never consulted.
  * no workspace root is attached — conversation, plus what you hand it.

Chat transcripts persist to their own `sessions_dir/chat/` namespace so they
never appear in the agent's Conversations list; uploads live beside them
under `sessions_dir/chat_media/`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from compass.common.config import get_settings
from compass.common.agent.query_loop import query
from compass.common.gateway.cost_tracker import CostTracker
from compass.common.models import events
from compass.common.models.messages import Message
from compass.common.attachments import build_user_message, transcribe_attachments
from compass.common.tools.base import PermissionBroker, ToolUseContext
import logging

from compass.home import media

logger = logging.getLogger("compass.home")

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
    "running on Azure OpenAI (gpt-5). This is a plain chat, with four ways to "
    "reach beyond it: `memory` (to remember durable facts about the user), "
    "`web_fetch` (to read a web page when you have its address), web "
    "search (to find pages when you do not), and `make_video` (to cut a video "
    "from what the user has attached). Use them when the answer depends "
    "on something you cannot know — anything current, anything at a link the "
    "user gives you — and not otherwise; most questions want an answer, not a "
    "search. You have no file access and cannot run commands or make changes. "
    "Otherwise just talk with the user — answer questions, brainstorm, "
    "explain, draft, and reason things through in clear, well-structured "
    "Markdown. If a request genuinely needs running code, editing files, "
    "inspecting a repository, or executing tools, say so briefly and point the "
    "user to the Code (Agent Console) section, where Compass can act with "
    "tools and your approval. Be concise by default and expand when the user "
    "wants depth."
)

# Said twice, and in the second person, because saying it once in the first
# did not hold. Asked to build a video from uploaded photos, the model replied
# "I'm switching to the Agent Console to run the workflow… I'll start now and
# report back when the render completes" — and then, of course, nothing ran.
# The prompt above already said it cannot run commands; what it did not say is
# that it cannot *go anywhere else to run them either*, and "point the user to
# the Code section" is a short step from "I'll head over there myself".
CHAT_SYSTEM_PROMPT += (
    "\n\nYou cannot switch sections, open the Agent Console, start a job "
    "there, or watch one finish. You have no way to act outside this "
    "conversation and no way to come back to it later. Never say you are "
    "switching to the Agent Console, that you are starting a run, that work "
    "is under way, or that you will report back when something completes — "
    "the person waits for a thing that is never going to happen. When a "
    "request needs running code, editing files or inspecting a repository, "
    "say in one line that this chat cannot do it, then hand it over in a form "
    "that can be used: the exact command or steps, and a note to open the "
    "Code (Agent Console) tab — or the Agent toggle beside this box — where "
    "Compass runs tools with their approval."
)

# Video is the one thing in that list this chat *can* now do, so it is carved
# out explicitly — the paragraph above spent three sentences teaching the model
# to refuse this kind of request, and a tool it has been told it cannot use is
# a tool it will not call.
CHAT_SYSTEM_PROMPT += (
    "\n\nVideo is the exception. `make_video` renders a real MP4 — a teaser, "
    "a montage, a recap — from the photos, clips and audio attached to this "
    "conversation, and the file it produces is played in the chat. When "
    "someone asks for one and there are attachments to build it from, cut it: "
    "choose the order, the pacing, the captions and the music yourself the way "
    "an editor would, call the tool, then say in a sentence or two what you "
    "chose and offer to change it. Do not interview them shot by shot first, "
    "and do not send them to the Agent Console for it.\n"
    "It cuts on the beat of whatever music is attached, sets captions in "
    "the language they were spoken in — Kannada, Hindi, Tamil and the rest, "
    "with the English underneath — frames photographs into collages, and "
    "lays a vignette and drifting light over the whole thing. Use those: a "
    "teaser in someone's own language, cut to their own music, is the point "
    "of the feature.\n"
    "Two honest limits, said plainly when they bite. It edits what was "
    "uploaded; it cannot invent footage, generate imagery or animate a "
    "photograph into something that was never photographed — if that is what "
    "they want, say so rather than rendering a slideshow and calling it what "
    "they asked for. And it needs something to work with: with nothing "
    "attached, ask for the photos or clips instead of guessing."
)


#: What Home is for, said plainly, because the model's default is otherwise
#: to hand back the recipe instead of the dish.
#:
#: Asked for a weather poster, the model wrote a block of SVG. That is a
#: perfectly good answer in a tool that renders markup — the Design section
#: renders it, which is what Design is — and a useless one here, because Home
#: renders nothing. The person gets a wall of angle brackets and has to go
#: find something to open it in, and what they finally see is flat vector
#: shapes rather than the photograph they pictured.
#:
#: This is about *deliverables*, not about code in general. A question about
#: programming still gets a snippet: that is the answer to the question. The
#: rule is that when the thing asked for is an artefact, the artefact is what
#: comes back.
MAKE_THE_THING = (
    "\n\nWhen somebody asks you to MAKE something, make it — do not hand back "
    "the code that would make it. This chat renders nothing: a block of SVG, "
    "HTML or canvas script is not a poster, a chart or a logo here, it is a "
    "wall of markup the person then has to find somewhere to open. "
    "If you cannot produce the artefact itself, say so in one line and offer "
    "what you genuinely can — never substitute source code for the thing and "
    "present it as the thing.\n"
    "Code is still the right answer to a question about code: how something "
    "works, why it breaks, what to write. That is an explanation, not a "
    "deliverable, and nothing above changes it."
)

CHAT_SYSTEM_PROMPT += MAKE_THE_THING


def _drawing_clause() -> str:
    """What to add to the prompt when this install can draw.

    Appended per turn rather than baked into the constant, because whether
    there is an image deployment is a fact about the install and the prompt
    above is a module-level string. It also has to *correct* that prompt:
    three sentences up, the model is told plainly that it cannot generate
    imagery, which was true and is now conditional. Left uncorrected, the
    model declines to call a tool it has just been given.
    """
    from compass.common.gateway import images

    if not images.available():
        return ""
    return (
        "\n\nOne correction to the limits above: you can now draw. "
        "`generate_image` renders a real picture from a description and gives "
        "you a URL. So anything visual that is asked for is drawn and handed "
        "over as a picture — never as SVG, HTML or canvas code. A request to "
        "make something from nothing has an answer: draw the frames and cut "
        "them together with `make_video`, or draw the single image when that "
        "is all that was asked for.\n"
        "WRITE THE PROMPT PROPERLY. This is the whole difference between a "
        "picture somebody wanted and a flat diagram. The description you pass "
        "is not the request you were given — it is a brief for an "
        "illustrator, and you write it. Four or five sentences, not four or "
        "five words. Say the subject and what it is doing; the setting and "
        "the time of day; the medium and the finish — a photograph, its lens "
        "and depth of field, or an illustration and its technique; the light; "
        "the palette; the composition and where the eye goes; the mood. "
        "\"Make a weather poster for Bengaluru\" is the request. The brief is "
        "a dusk photograph of the Bengaluru skyline under heavy monsoon "
        "cloud, wet roads lit by traffic, warm window light against a "
        "blue-grey sky, shot wide on a 35mm lens, the upper third left open "
        "and uncluttered for a headline, cinematic and calm. Fill in what was "
        "not specified with a decision rather than leaving it vague: vague "
        "prompts are what produce flat, generic pictures.\n"
        "Any words that must be legible — a headline, a price, a date — go in "
        "the description in quotes, kept short, and say where they sit. "
        "Generated lettering is unreliable past a few words, so ask for a "
        "clear area and keep long copy out of the picture.\n"
        "The limit that still holds is about *their* material: a photograph "
        "somebody attached cannot be animated into footage that was never "
        "taken, and a drawn picture is a drawing rather than a photograph of "
        "their event. Say which one you are giving them."
    )


def _media_catalogue(session_id: str) -> str:
    """The photos, clips and recordings this thread is holding, as the model
    needs to see them: ids, because ids are what `make_video` takes."""
    files = media.listing(session_id)
    if not files:
        return ""
    lines = "\n".join(f"  {f.describe()}" for f in files)
    return (
        "Files attached to this conversation and kept for rendering "
        "(`make_video` takes these ids, exactly as written):\n" + lines +
        "\nThese are the only files you can use. Anything rendered earlier in "
        "this thread is in the list too, so a second version can be cut from "
        "the same material without asking for the uploads again.\n"
        "Each line says what the picture is of and which beat of a "
        "celebration it belongs to. Use that: order the film by what happens "
        "in it — arrival, ritual, the procession, the dancing — rather than "
        "by the order the files were uploaded, and close on something that "
        "reads as an ending. Keep upright photographs upright; they are the "
        "right shape for a reel already."
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
        self, session_id: str, *, title: str | None = None,
        pinned: bool | None = None, owner: str | None = None
    ) -> None:
        meta = self._read_meta()
        entry = meta.setdefault(session_id, {})
        if title is not None:
            entry["title"] = title
        if pinned is not None:
            entry["pinned"] = pinned
        if owner is not None:
            entry["owner"] = owner
        self._write_meta(meta)

    async def owner_of(self, session_id: str) -> str:
        """Who a thread belongs to. Empty for threads written before ownership
        existed — Home's index only holds a row once something has been
        renamed, pinned or created since, so absence here is normal and means
        legacy rather than unowned-and-hidden."""
        return self._read_meta().get(session_id, {}).get("owner", "") or ""

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

    async def load_page(
        self, session_id: str, *, limit: int, before_seq: int | None = None,
    ) -> tuple[list[Message], int | None]:
        """The last `limit` messages, oldest first, and where the page before
        it starts. Whole-file and sliced: there is no round trip to save on a
        local disk, and position in the file is the sequence number."""
        messages = await self.load(session_id)
        end = len(messages) if before_seq is None else max(0, int(before_seq))
        start = max(0, end - limit)
        return messages[start:end], (start if start > 0 else None)

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
                    "owner": m.get("owner", "") or "",
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


_chat_store = None


def get_chat_store():
    """Pick the Home/Chat backend: Azure Cosmos DB when configured, else the
    local JSONL store — the same config-or-fallback contract the agent
    transcript store uses. Absent Cosmos credentials, nothing changes.

    One instance for the process, like the transcript store. It used to build
    a fresh one per call, which on the Cosmos backend meant a new client and
    connection pool for every sidebar recap, none of them ever closed.
    """
    global _chat_store
    if _chat_store is not None:
        return _chat_store
    cfg = get_settings().storage
    if cfg.backend == "cosmos" and cfg.cosmos_configured:
        from compass.home.store import CosmosChatStore

        _chat_store = CosmosChatStore()
    else:
        _chat_store = ChatStore()
    return _chat_store


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
        """Home carries three tools, and the bar they clear is the same one:
        nothing on this machine is readable or runnable through them.

        `memory`, so Compass can save what it learns while you chat (Claude's
        memory behaviour). And `web_fetch`, because Home can already search the
        web — server tools ride on the request rather than on this list — and
        without it a link you paste is the one thing on the internet Home
        cannot read. Searching for a page you have the address of is a poor
        substitute for opening it.

        `make_video` is the exception that proves the rule, and it was added
        because the alternative was worse: asked to cut a teaser out of the
        photos somebody had just attached, Home's only honest answer was "go
        to the Agent Console", and its dishonest answer — the one it kept
        giving — was to promise a render that never happened. It takes no
        paths and no commands, only ids of files uploaded to this
        conversation, and writes only into that conversation's own folder. It
        is not read-only, so it allows itself explicitly (see its
        `check_tool_permissions`) rather than being waved through by the gate.

        Everything else stays out and Home remains conversation: no shell, no
        workspace, nothing of yours it can open. The broker is still auto_deny
        as a belt-and-braces guard — anything that asked for permission
        without having granted itself any is refused rather than silently
        allowed."""
        from compass.common.gateway import images
        from compass.common.tools.draw import DrawTool, EditImageTool
        from compass.common.tools.memory import MemoryTool
        from compass.common.tools.web_fetch import WebFetchTool
        from compass.home.video_tool import VideoTool

        tools = [MemoryTool(), WebFetchTool(), VideoTool()]
        # Drawing changes what Home can be asked for. `make_video` cuts a film
        # out of what was attached and says so — it cannot invent footage —
        # so a request to make something from nothing had no answer at all.
        # With this, the pictures can be drawn and then cut together.
        if images.available():
            tools += [DrawTool(), EditImageTool()]

        return ToolUseContext(
            session_id=self.id,
            tools=tools,
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
            # Anything this thread already has, brought back to disk first.
            # Normally there is nothing to do and this is one listing: the
            # files are where they were left. It matters when they are not —
            # a second machine, or a rebuilt container — because everything
            # below works on paths, and so does ffmpeg.
            await media.hydrate(session.id)
            # Photos, clips and recordings are written down before anything
            # else touches them: what the model is shown is a copy scaled for
            # vision or a transcript, and neither can be cut into a film. Kept
            # first so this happens whatever transcription does next.
            kept = media.keep(session.id, attachments)
            # Written through to storage, so the disk is a working copy rather
            # than the only one.
            if kept:
                await media.upload(session.id, kept)
            # Looked at once, here, so that a reel asked for three turns later
            # can be ordered by what is in the photographs rather than by
            # their filenames. Never fatal: undescribed pictures still work.
            try:
                from compass.home import vision

                await vision.describe_new(session.id)
            except Exception:  # noqa: BLE001
                logger.warning("could not describe the new photos", exc_info=True)
            # Audio is transcribed first; the model reads it, it cannot hear it.
            attachments = await transcribe_attachments(attachments)
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
                # Say so. A bare `return` here yielded an empty 200: the
                # client opened a stream, received zero events, and waited for
                # a completion that was never coming.
                yield events.ErrorEvent(
                    message="There is no answer to regenerate — this thread has "
                            "no user message to re-run."
                )
                yield events.TurnComplete(reason="error", detail="nothing to regenerate")
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
                yield events.ErrorEvent(
                    message=f"Cannot edit message {index}: this thread has "
                            f"{len(session.messages)} messages."
                )
                yield events.TurnComplete(reason="error", detail="edit index out of range")
                return
            if session.messages[index].role != "user":
                yield events.ErrorEvent(
                    message=f"Cannot edit message {index}: it is a "
                            f"{session.messages[index].role} message, and only a "
                            "user message can be edited and resent."
                )
                yield events.TurnComplete(reason="error", detail="edit target is not a user message")
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
        system_prompt = CHAT_SYSTEM_PROMPT + _drawing_clause()
        if work_iq:
            from compass.home import work_iq as wiq

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
        from compass.common.memory import GLOBAL_SCOPE, memory_prompt

        mem = await memory_prompt(GLOBAL_SCOPE)
        if mem:
            system_prompt = f"{system_prompt}\n\n{mem}"

        # What this thread has to work with, by id. In the per-turn prompt
        # rather than in the user's message, for two reasons: a note appended
        # to what somebody typed shows up in their own chat bubble, and the
        # list has to be right on every turn, not only on the turn the files
        # arrived. Costs one line when there are no uploads: nothing.
        catalogue = _media_catalogue(session.id)
        if catalogue:
            system_prompt = f"{system_prompt}\n\n{catalogue}"

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

    async def fork(self, session_id: str, index: int | None = None) -> str:
        """Branch a Home thread: copy it up to and including `index` into a new
        thread, so a different direction can be explored without losing the
        original. `index` is the message's position (Home alternates strictly)."""
        messages = await self.store.load(session_id)
        if index is not None:
            messages = messages[: index + 1]
        new_id = str(uuid.uuid4())
        await self.store.rewrite(new_id, messages)
        meta = self.store._read_meta().get(session_id, {}) if hasattr(
            self.store, "_read_meta"
        ) else {}
        title = meta.get("title")
        if title:
            await self.store.set_meta(new_id, title=title)
        return new_id

    def abort(self, session: ChatSession) -> None:
        session.abort_event.set()
