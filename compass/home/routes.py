"""Home/Chat REST surface — a self-contained router, mounted by server.py.

Entirely separate from the agent console's /v1/sessions/* endpoints: its own
engine (ChatEngine), its own in-memory session registry, and its
own transcript namespace. Nothing here touches the console code paths.

    POST /v1/chat/sessions                     create or resume a chat thread
    POST /v1/chat/sessions/{sid}/messages      send input, stream SSE
    POST /v1/chat/sessions/{sid}/abort         cancel the running turn
    GET  /v1/chat/sessions/{sid}/transcript    replay the stored thread
    GET  /v1/chat/sessions                     list stored chat threads
    GET  /v1/chat/sessions/{sid}/media/{name}  an upload, or a render made from one
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from compass.common.agent.steering import steer
from compass.common.gateway.responses import REASONING_META_KEY
from compass.common.auth import require_user
from compass.common.media_types import media_type_for
from compass.common.ownership import owned, owner_for, visible_to
from compass.common.sse import with_heartbeat
from compass.home import media
from compass.common.models.messages import Message
from compass.home.engine import ChatEngine, ChatSession
from compass.common.models.events import ErrorEvent

logger = logging.getLogger("compass.chat")

router = APIRouter(prefix="/v1/chat", tags=["chat"])


def for_the_browser(record: dict) -> dict:
    """A transcript record with the encrypted reasoning taken out.

    The sealed reasoning is meaningless outside the model and runs to
    kilobytes per turn; it belongs in the transcript on disk, where a resumed
    session reads it, not on the wire to a browser that cannot use it. The
    readable summary and what it cost do go, because those are for people.
    """
    meta = record.get("meta") or {}
    if REASONING_META_KEY not in meta:
        return record
    return {**record, "meta": {k: v for k, v in meta.items()
                               if k != REASONING_META_KEY}}
chat_engine = ChatEngine()
chat_sessions: dict[str, ChatSession] = {}


class CreateChatRequest(BaseModel):
    effort: str | None = Field(
        default=None, description="minimal | low | medium | high"
    )
    model: str | None = Field(default=None, description="Azure deployment to use")
    resume: bool = Field(default=False, description="Reload the thread if it exists")
    session_id: str | None = None


class ChatAttachment(BaseModel):
    """A raw uploaded file: base64 `data_url` for every type. The backend
    (services.attachments) classifies and extracts — images to gpt-5 vision,
    PDF/DOCX/ZIP/text to inlined text."""

    name: str = ""
    mime: str = ""
    data_url: str | None = None
    text: str | None = None  # optional pre-extracted text (compatibility)


class ChatMessageRequest(BaseModel):
    content: str
    attachments: list[ChatAttachment] = []
    work_iq: bool = False  # ground this turn in Azure AI Search (Home "Work IQ")
    # Steer thinking for this turn alone: "more" or "less". Effort is the
    # calibrated control, but it is part of the cached prefix — changing it
    # mid-conversation re-sends everything — so a single turn is steered with
    # words instead.
    think: str = ""


def _sse(gen) -> StreamingResponse:
    async def stream():
        try:
            async for event in gen:
                yield event.to_sse()
        except Exception as err:  # noqa: BLE001 — the stream must end with an event
            logger.exception("chat turn failed")
            yield ErrorEvent(message=str(err)).to_sse()

    # Pinged while quiet, so silence on the wire means the turn is gone
    # rather than merely thinking. See compass.common.sse.
    return StreamingResponse(
        with_heartbeat(stream()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions")
async def create_chat_session(
    body: CreateChatRequest, user: str = Depends(require_user)
) -> dict:
    if body.resume and body.session_id and await chat_engine.store.exists(body.session_id):
        await _owned_chat(body.session_id, user)
        session = await chat_engine.resume(
            body.session_id, effort=body.effort, model=body.model
        )
    else:
        session = ChatSession(effort=body.effort, model=body.model)
        if body.session_id:
            session.id = body.session_id
        if stamp := owner_for(user):
            await chat_engine.store.set_meta(session.id, owner=stamp)
    chat_sessions[session.id] = session
    return {
        "session_id": session.id,
        "resumed_messages": len(session.messages),
        "model": session.model,
    }


def _get_chat_session(session_id: str) -> ChatSession:
    session = chat_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown chat session")
    return session


async def _owned_chat(session_id: str, user: str) -> None:
    """Refuse a thread this user does not own, reported as unknown so a
    guessed id learns nothing."""
    if not visible_to(user, await chat_engine.store.owner_of(session_id)):
        raise HTTPException(status_code=404, detail="unknown chat session")


async def _known_chat(session_id: str) -> None:
    """404 unless this thread is real: a transcript on disk, or a live session
    created in this process that has not written a turn yet.

    Rename and delete used to answer 200 for any id at all — `{"deleted":
    "never-existed-at-all"}` — while `/transcript` and `/fork` correctly said
    404 for the same id. Worse than the inconsistency, a rename *wrote* a
    metadata entry keyed by the id, so posting to a made-up id left a row in
    the index for a thread that had never existed.

    The in-memory arm matters: a thread is created before its first turn, so
    for a moment it is real without being on disk, and refusing to rename it
    then would be a different bug from the one being fixed."""
    if session_id in chat_sessions:
        return
    if not await chat_engine.store.exists(session_id):
        raise HTTPException(status_code=404, detail="unknown chat session")


@router.post("/sessions/{session_id}/messages")
async def send_chat_message(
    session_id: str, body: ChatMessageRequest, user: str = Depends(require_user)
) -> StreamingResponse:
    await _owned_chat(session_id, user)
    session = _get_chat_session(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    attachments = [a.model_dump() for a in body.attachments]
    return _sse(
        chat_engine.ask(
            session, steer(body.content, body.think), attachments, body.work_iq
        )
    )


class ChatRegenRequest(BaseModel):
    work_iq: bool = False


class ChatEditRequest(BaseModel):
    index: int
    content: str
    work_iq: bool = False


@router.post("/sessions/{session_id}/regenerate")
async def regenerate_chat(
    session_id: str, body: ChatRegenRequest, user: str = Depends(require_user)
) -> StreamingResponse:
    await _owned_chat(session_id, user)
    session = _get_chat_session(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    return _sse(chat_engine.regenerate(session, body.work_iq))


@router.post("/sessions/{session_id}/edit")
async def edit_chat(
    session_id: str, body: ChatEditRequest, user: str = Depends(require_user)
) -> StreamingResponse:
    await _owned_chat(session_id, user)
    session = _get_chat_session(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    return _sse(chat_engine.edit(session, body.index, body.content, body.work_iq))


@router.post("/sessions/{session_id}/abort")
async def abort_chat_turn(session_id: str, user: str = Depends(require_user)) -> dict:
    await _owned_chat(session_id, user)
    session = _get_chat_session(session_id)
    chat_engine.abort(session)
    return {"aborted": True}


@router.get("/sessions/{session_id}/transcript")
async def chat_transcript(
    session_id: str,
    limit: int = 0,
    before_seq: int | None = None,
    user: str = Depends(require_user),
) -> dict:
    """A thread's messages, oldest first.

    `limit` asks for the end of the thread rather than all of it, and
    `before_seq` — taken from a previous response — asks for the page before
    that one. Without `limit`, the whole thread, as before.
    """
    # Together rather than one after another: three independent round trips to
    # another continent, and in sequence the browser waits for their sum.
    page = (
        chat_engine.store.load_page(session_id, limit=limit, before_seq=before_seq)
        if limit > 0
        else chat_engine.store.load(session_id)
    )
    _owned, exists, loaded = await asyncio.gather(
        _owned_chat(session_id, user),
        chat_engine.store.exists(session_id),
        page,
    )
    if not exists:
        raise HTTPException(status_code=404, detail="unknown chat session")
    messages, earlier = loaded if limit > 0 else (loaded, None)
    return {
        "session_id": session_id,
        "messages": [for_the_browser(m.to_record()) for m in messages],
        "before_seq": earlier,
    }


#: A spoken turn is two short strings; anything longer is not something a
#: person said into a microphone, and the cap is what stops a stuck client
#: writing a novel into somebody's transcript.
MAX_SPOKEN_CHARS = 8_000


class VoiceTurnRequest(BaseModel):
    turn_id: str = Field(
        description="Idempotency key for this exchange, minted by the client. "
                    "The same key twice stores one turn, so a retry after a "
                    "dropped response cannot double-write the conversation.",
    )
    heard: str = Field(default="", description="What the person said.")
    reply: str = Field(default="", description="What the assistant said back.")


def _voice_uuid(turn_id: str, role: str) -> str:
    """A stable id for one half of a spoken exchange.

    Derived from the client's key rather than random, which is what makes
    the write idempotent: the retry produces the same two ids, and the
    duplicate is recognised instead of appended.
    """
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"compass-voice/{turn_id}/{role}"))


@router.post("/sessions/{session_id}/voice-turn")
async def record_voice_turn(
    session_id: str, body: VoiceTurnRequest, user: str = Depends(require_user)
) -> dict:
    """Write one spoken exchange into the thread.

    Voice mode talks to Azure directly over WebRTC, so the server never sees
    the conversation — which is what makes it fast, and why nothing was being
    kept. Leaving voice mode used to discard the whole exchange: the thread
    showed no record that anything had been said. So the client reports each
    completed turn here, and a spoken conversation becomes a conversation.

    Marked `voice` in the message metadata rather than rewritten to look
    typed. It is a different kind of turn — transcribed, not composed — and a
    surface that wants to say so can, while one that does not simply reads it
    as text.
    """
    await _owned_chat(session_id, user)
    # A thread that has never been typed in has no stored messages, and voice
    # may be the first thing said in it — so `exists` alone would refuse
    # exactly the case this route is for. Being live in `chat_sessions` is
    # what proves the id came from `POST /sessions` rather than being made
    # up; ownership above is checked either way, and an unowned id that
    # matches neither is refused rather than quietly started.
    known = session_id in chat_sessions or await chat_engine.store.exists(session_id)
    if not known:
        raise HTTPException(status_code=404, detail="unknown chat session")

    turn_id = (body.turn_id or "").strip()[:128]
    if not turn_id:
        raise HTTPException(status_code=422, detail="turn_id is required")
    heard = (body.heard or "").strip()[:MAX_SPOKEN_CHARS]
    reply = (body.reply or "").strip()[:MAX_SPOKEN_CHARS]
    if not reply and not heard:
        raise HTTPException(status_code=422, detail="nothing was said")

    user_id = _voice_uuid(turn_id, "user")
    reply_id = _voice_uuid(turn_id, "assistant")

    existing = {m.uuid for m in await chat_engine.store.load(session_id)}
    if user_id in existing or reply_id in existing:
        # Already written. A retry says so plainly rather than pretending to
        # have done the work again.
        return {"stored": False, "reason": "already recorded", "turn_id": turn_id}

    # The question first, then the answer, so the thread reads in the order it
    # happened. `heard` can be empty when the deployment declined input
    # transcription: the exchange is still kept, and the gap is stated in the
    # metadata rather than filled in with words nobody said.
    chat_engine.store.append(session_id, Message(
        role="user", content=heard, uuid=user_id,
        meta={"voice": True, **({} if heard else {"transcript_unavailable": True})},
    ))
    if reply:
        chat_engine.store.append(session_id, Message(
            role="assistant", content=reply, uuid=reply_id, meta={"voice": True},
        ))
    return {"stored": True, "turn_id": turn_id}


@router.get("/sessions/{session_id}/media/{filename}")
async def chat_media(
    session_id: str, filename: str, user: str = Depends(require_user)
):
    """Serve one of this thread's files — an upload, or something rendered
    from them by `make_video`.

    Ownership is checked first and a miss is a 404 either way, so this cannot
    be used to find out whether a thread exists. `media.resolve` does the
    containment check: it takes the basename, joins it to this thread's
    directory and re-checks after resolving, which is what stops `..` and a
    symlink alike. FileResponse handles Range requests, which is what lets
    someone scrub a video rather than wait for the whole file.
    """
    await _owned_chat(session_id, user)
    path = media.resolve(session_id, filename)
    if path is None:
        # Nothing on disk under that name. Before saying so, bring down what
        # storage has for this thread: a conversation opened on a machine that
        # never held its uploads would otherwise show every image broken.
        # Only on a miss, so the normal path stays a single filesystem check.
        if await media.hydrate(session_id):
            path = media.resolve(session_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(
        path,
        media_type=media_type_for(path),
        # The name it was uploaded as, not the numbered one on disk, so a
        # download lands in someone's folder called what they called it.
        filename=path.name.split("-", 1)[-1],
        content_disposition_type="inline",
    )


@router.get("/work-iq")
async def work_iq_status(user: str = Depends(require_user)) -> dict:
    """Whether Work IQ (Azure AI Search) is configured — drives the Home toggle."""
    from compass.home import work_iq

    return {"configured": work_iq.configured()}


VOICE_INSTRUCTIONS = (
    "You are Compass, a warm, concise voice assistant. Speak naturally and "
    "conversationally, keep answers brief and to the point, and ask a short "
    "follow-up when it helps. You are talking out loud, so avoid code blocks, "
    "long lists, or reading URLs aloud."
)

#: Language names for the codes people actually configure. Only to write the
#: instruction in words the model reads easily; an unlisted code is passed
#: through as itself, which it also understands.
_LANGUAGE_NAMES = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "hi": "Hindi",
    "ar": "Arabic", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "ru": "Russian", "pl": "Polish", "tr": "Turkish", "sv": "Swedish",
}


def _voice_instructions(language: str) -> str:
    """The voice prompt, with the language said out loud.

    Belt and braces with the transcription hint below, and both are needed.
    The hint keeps Whisper from mis-hearing which language is being spoken;
    this keeps the model from answering in some other one anyway, which it
    will do if a single stray word looks foreign to it.
    """
    if not language:
        return VOICE_INSTRUCTIONS + (
            " Reply in whatever language the person is speaking."
        )
    name = _LANGUAGE_NAMES.get(language, language)
    return VOICE_INSTRUCTIONS + (
        f" The person is speaking {name}. Always reply in {name}, even if a "
        f"word or name in what you hear looks like another language."
    )


def _transcription(language: str) -> dict:
    """Input transcription config. The language is a hint to Whisper, not a
    filter: it stops the guessing, which is where the wrong language comes
    from. Omitted entirely when unset, which restores auto-detection."""
    cfg: dict = {"model": "whisper-1"}
    if language:
        cfg["language"] = language
    return cfg


@router.get("/voice")
async def voice_status(user: str = Depends(require_user)) -> dict:
    """Whether realtime voice mode is available (a realtime deployment set)."""
    from compass.common.config import get_settings

    return {"available": get_settings().azure.realtime_configured}


@router.post("/voice/session")
async def voice_session(user: str = Depends(require_user)) -> dict:
    """Mint a short-lived ephemeral key for the browser's WebRTC realtime
    session (keeps the Azure api-key server-side), per the Azure OpenAI GA
    Realtime WebRTC flow. Returns the token + the WebRTC calls URL."""
    import httpx

    from compass.common.config import get_settings

    az = get_settings().azure
    if not az.realtime_configured:
        raise HTTPException(status_code=400, detail="realtime voice not configured")

    # The audio resource, not the chat one — see AzureOpenAISettings.
    base = az.realtime_endpoint_effective.rstrip("/")
    session = {
        "type": "realtime",
        "model": az.realtime_deployment,
        "instructions": _voice_instructions(az.realtime_language),
        "audio": {
            "output": {"voice": az.realtime_voice},
            "input": {"transcription": _transcription(az.realtime_language)},
        },
    }
    headers = {
        "api-key": az.realtime_api_key_effective,
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{base}/openai/v1/realtime/client_secrets",
            headers=headers,
            json={"session": session},
        )
        # Input transcription may be rejected by some deployments — retry the
        # documented minimal session (voice-to-voice still works without it).
        if resp.status_code >= 400:
            session["audio"].pop("input", None)
            resp = await client.post(
                f"{base}/openai/v1/realtime/client_secrets",
                headers=headers,
                json={"session": session},
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"realtime session failed: {resp.text[:300]}")
        data = resp.json()

    token = data.get("value") or (data.get("client_secret") or {}).get("value")
    if not token:
        raise HTTPException(status_code=502, detail="no ephemeral token returned")
    return {"token": token, "webrtc_url": f"{base}/openai/v1/realtime/calls?webrtcfilter=on"}


@router.get("/sessions")
async def list_chat_sessions(user: str = Depends(require_user)) -> dict:
    """Home conversation cards (id, title, timestamps), most-recent first."""
    return {"sessions": owned(await chat_engine.store.list_cards(), user)}


class ChatForkRequest(BaseModel):
    index: int | None = None


@router.post("/sessions/{session_id}/fork")
async def fork_chat(
    session_id: str, body: ChatForkRequest, user: str = Depends(require_user)
) -> dict:
    """Branch a Home thread at a message into a new conversation."""
    await _owned_chat(session_id, user)
    if not await chat_engine.store.exists(session_id):
        raise HTTPException(status_code=404, detail="unknown chat session")
    return {"session_id": await chat_engine.fork(session_id, body.index)}


class ChatPatchRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None


@router.patch("/sessions/{session_id}")
async def patch_chat_session(
    session_id: str, body: ChatPatchRequest, user: str = Depends(require_user)
) -> dict:
    """Rename or star (pin) a Home chat — persisted to the chat meta index."""
    await _owned_chat(session_id, user)
    await _known_chat(session_id)
    await chat_engine.store.set_meta(session_id, title=body.title, pinned=body.pinned)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_chat_session(session_id: str, user: str = Depends(require_user)) -> dict:
    await _owned_chat(session_id, user)
    await _known_chat(session_id)
    # Read before it is gone: the audit note keeps the label, because an id
    # alone answers nothing three months later.
    title = next(
        (c.get("title", "") for c in await chat_engine.store.list_cards()
         if c.get("id") == session_id),
        "",
    )
    await chat_engine.store.delete(session_id)
    # The uploads go with the thread, which is what media.py has always said
    # and did not do. Here rather than in either store, so it happens once
    # whichever backend holds the transcript — and it clears both the stored
    # copy and the one on disk.
    await media.forget(session_id)
    # And the pictures, films and rows that described them — bytes included.
    # `forget_session` only dropped the index, which left every drawn picture
    # and screenshot in blob storage with nothing able to name it again.
    from compass.common import audit, media_index

    tally = await media_index.purge_session(session_id)
    chat_sessions.pop(session_id, None)
    await audit.note_deletion(
        module="home", kind="conversation", record_id=session_id,
        owner=owner_for(user), session_id=session_id, title=title,
        removed={"transcript": True, "uploads": True, **tally},
    )
    return {"deleted": session_id}


# ── the prompt library ──────────────────────────────────────────────────
#: Saved prompts are per-install rather than per-conversation: the point of
#: keeping one is that it outlives the chat it came from.


class SavePromptRequest(BaseModel):
    title: str = Field(default="", description="A label. Derived from the text when empty.")
    text: str = Field(description="The prompt itself.")
    session_id: str = Field(default="", description="Where it came from, if anywhere.")


class PromptOrderRequest(BaseModel):
    order: list[str] = Field(
        default_factory=list,
        description="Every row, in the order to show them. A saved prompt's "
                    "id, or the name of one of the client's built-in "
                    "starters; keys that match nothing are ignored when the "
                    "list is drawn.",
    )


class SharpenTurn(BaseModel):
    role: str = ""
    text: str = ""


class SharpenRequest(BaseModel):
    text: str = Field(description="The prompt as it was typed.")
    turns: list[SharpenTurn] = Field(
        default_factory=list,
        description="The conversation, oldest first. What the person asked "
                    "*after* the selected prompt usually carries the intent — "
                    "people open with something small and say what they want "
                    "next — so the whole thread goes, not just what preceded "
                    "it, and the model decides which of it is related.")
    context: str = Field(
        default="",
        description="The older shape: a little of what was on screen around "
                    "it. Kept so an out-of-date client still gets a rewrite.")


@router.get("/prompts")
async def list_prompts(user: str = Depends(require_user)) -> dict:
    from compass.home.prompts import ON_SCREEN, get_prompt_library

    library = get_prompt_library()
    rows = await library.list(owner_for(user))
    return {
        "prompts": [r.to_dict() for r in rows],
        "on_screen": ON_SCREEN,
        # The arrangement, if one was ever set. Sent alongside rather than
        # applied here: it also covers the client's own built-in starters,
        # which the server has never seen.
        "order": await library.order(owner_for(user)),
    }


@router.put("/prompts/order")
async def set_prompt_order(body: PromptOrderRequest,
                           user: str = Depends(require_user)) -> dict:
    """The order the prompts are shown in, saved so it outlives the tab.

    Takes the whole arrangement rather than a move, because a move is only
    meaningful against the list the browser was looking at, and two tabs
    disagreeing about that is how a list ends up shuffled. The last writer
    wins, which is the right answer for one person's own library.
    """
    from compass.home.prompts import get_prompt_library

    return {"order": await get_prompt_library().set_order(body.order, owner_for(user))}


@router.post("/prompts")
async def save_prompt(body: SavePromptRequest,
                      user: str = Depends(require_user)) -> dict:
    from compass.home.prompts import get_prompt_library

    try:
        row = await get_prompt_library().add(
            title=body.title, text=body.text, session_id=body.session_id,
            owner=owner_for(user))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return row.to_dict()


@router.patch("/prompts/{prompt_id}")
async def edit_prompt(prompt_id: str, body: SavePromptRequest,
                      user: str = Depends(require_user)) -> dict:
    from compass.home.prompts import get_prompt_library

    row = await get_prompt_library().update(
        prompt_id, title=body.title, text=body.text, owner=owner_for(user))
    if row is None:
        raise HTTPException(status_code=404, detail="no such prompt")
    return row.to_dict()


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(prompt_id: str, user: str = Depends(require_user)) -> dict:
    from compass.home.prompts import get_prompt_library

    from compass.common import audit

    gone = await get_prompt_library().delete(prompt_id, owner_for(user))
    if gone:
        await audit.note_deletion(
            module="home", kind="prompt", record_id=prompt_id,
            owner=owner_for(user), removed={"prompt": True},
        )
    return {"deleted": gone}


class RefineRequest(BaseModel):
    text: str = Field(description="The saved prompt, as it stands.")
    title: str = Field(default="", description="Its current title, if it has one.")


@router.post("/prompts/refine")
async def refine_prompt(body: RefineRequest,
                        user: str = Depends(require_user)) -> dict:
    """A better-written version of one saved prompt, from the prompt alone.

    The sibling of `/prompts/sharpen`, and deliberately a separate route
    rather than a flag on it. Sharpening merges a thread out of the
    conversation a prompt came from; that is the right offer while saving
    and a meaningless one afterwards, when the prompt is opened from the
    library and there is no conversation in front of it.

    Always answers: on any failure it returns the prompt it was given.
    """
    from compass.home.prompts import refine

    return await refine(body.text, body.title)


@router.post("/prompts/sharpen")
async def sharpen_prompt(body: SharpenRequest,
                         user: str = Depends(require_user)) -> dict:
    """A clearer, self-contained version of one prompt, and a title for it.

    Always answers: on any failure it returns the prompt it was given, because
    this sits behind an optional button and the person should be left with
    what they already had rather than an error.
    """
    from compass.home.prompts import sharpen

    return await sharpen(body.text, body.context,
                         [t.model_dump() for t in body.turns])


@router.get("/favicon")
async def source_favicon(url: str, user: str = Depends(require_user)):
    """The site icon for a cited source, fetched and cached by the server.

    Served from here rather than linked directly in the page so the browser
    never contacts the sites in a list of sources: which of them somebody is
    reading is private until they click one. A site with no usable icon gets
    a 404 and the page draws a lettered monogram instead.
    """
    from fastapi.responses import FileResponse, Response

    from compass.home import favicons

    host = favicons.host_of(url)
    if not host:
        raise HTTPException(status_code=400, detail="not a host")
    path = await favicons.fetch(host)
    if path is None:
        # 404 rather than a placeholder image: the page has a nicer fallback
        # than anything that could be sent here, and a cached 404 is cheap.
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=86400"})
    # The type is stated, not guessed. The extension was chosen from the
    # content-type the site sent when this was cached, so it is already known
    # — and leaving it to `mimetypes` puts a Windows registry between a PNG
    # and the browser drawing it.
    return FileResponse(
        path,
        media_type=media_type_for(path),
        headers={"Cache-Control": "public, max-age=604800"},
    )
