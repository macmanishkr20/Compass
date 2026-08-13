"""Home/Chat REST surface — a self-contained router, mounted by server.py.

Entirely separate from the agent console's /v1/sessions/* endpoints: its own
engine (ChatEngine, tool-free), its own in-memory session registry, and its
own transcript namespace. Nothing here touches the console code paths.

    POST /v1/chat/sessions                     create or resume a chat thread
    POST /v1/chat/sessions/{sid}/messages      send input, stream SSE
    POST /v1/chat/sessions/{sid}/abort         cancel the running turn
    GET  /v1/chat/sessions/{sid}/transcript    replay the stored thread
    GET  /v1/chat/sessions                     list stored chat threads
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from compass.api.auth import require_user
from compass.core.chat_engine import ChatEngine, ChatSession
from compass.models.events import ErrorEvent

logger = logging.getLogger("compass.chat")

router = APIRouter(prefix="/v1/chat", tags=["chat"])
chat_engine = ChatEngine()
chat_sessions: dict[str, ChatSession] = {}


class CreateChatRequest(BaseModel):
    effort: str | None = Field(default=None, description="minimal | low | medium | high")
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


def _sse(gen) -> StreamingResponse:
    async def stream():
        try:
            async for event in gen:
                yield event.to_sse()
        except Exception as err:  # noqa: BLE001 — the stream must end with an event
            logger.exception("chat turn failed")
            yield ErrorEvent(message=str(err)).to_sse()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions")
async def create_chat_session(
    body: CreateChatRequest, user: str = Depends(require_user)
) -> dict:
    if body.resume and body.session_id and await chat_engine.store.exists(body.session_id):
        session = await chat_engine.resume(
            body.session_id, effort=body.effort, model=body.model
        )
    else:
        session = ChatSession(effort=body.effort, model=body.model)
        if body.session_id:
            session.id = body.session_id
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


@router.post("/sessions/{session_id}/messages")
async def send_chat_message(
    session_id: str, body: ChatMessageRequest, user: str = Depends(require_user)
) -> StreamingResponse:
    session = _get_chat_session(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    attachments = [a.model_dump() for a in body.attachments]
    return _sse(chat_engine.ask(session, body.content, attachments, body.work_iq))


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
    session = _get_chat_session(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    return _sse(chat_engine.regenerate(session, body.work_iq))


@router.post("/sessions/{session_id}/edit")
async def edit_chat(
    session_id: str, body: ChatEditRequest, user: str = Depends(require_user)
) -> StreamingResponse:
    session = _get_chat_session(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    return _sse(chat_engine.edit(session, body.index, body.content, body.work_iq))


@router.post("/sessions/{session_id}/abort")
async def abort_chat_turn(session_id: str, user: str = Depends(require_user)) -> dict:
    session = _get_chat_session(session_id)
    chat_engine.abort(session)
    return {"aborted": True}


@router.get("/sessions/{session_id}/transcript")
async def chat_transcript(session_id: str, user: str = Depends(require_user)) -> dict:
    if not await chat_engine.store.exists(session_id):
        raise HTTPException(status_code=404, detail="unknown chat session")
    messages = await chat_engine.store.load(session_id)
    return {"session_id": session_id, "messages": [m.to_record() for m in messages]}


@router.get("/work-iq")
async def work_iq_status(user: str = Depends(require_user)) -> dict:
    """Whether Work IQ (Azure AI Search) is configured — drives the Home toggle."""
    from compass.services import work_iq

    return {"configured": work_iq.configured()}


VOICE_INSTRUCTIONS = (
    "You are Compass, a warm, concise voice assistant. Speak naturally and "
    "conversationally, keep answers brief and to the point, and ask a short "
    "follow-up when it helps. You are talking out loud, so avoid code blocks, "
    "long lists, or reading URLs aloud."
)


@router.get("/voice")
async def voice_status(user: str = Depends(require_user)) -> dict:
    """Whether realtime voice mode is available (a realtime deployment set)."""
    from compass.config import get_settings

    return {"available": get_settings().azure.realtime_configured}


@router.post("/voice/session")
async def voice_session(user: str = Depends(require_user)) -> dict:
    """Mint a short-lived ephemeral key for the browser's WebRTC realtime
    session (keeps the Azure api-key server-side), per the Azure OpenAI GA
    Realtime WebRTC flow. Returns the token + the WebRTC calls URL."""
    import httpx

    from compass.config import get_settings

    az = get_settings().azure
    if not az.realtime_configured:
        raise HTTPException(status_code=400, detail="realtime voice not configured")

    base = az.endpoint.rstrip("/")
    session = {
        "type": "realtime",
        "model": az.realtime_deployment,
        "instructions": VOICE_INSTRUCTIONS,
        "audio": {
            "output": {"voice": az.realtime_voice},
            "input": {"transcription": {"model": "whisper-1"}},
        },
    }
    headers = {"api-key": az.api_key, "content-type": "application/json"}
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
    return {"sessions": await chat_engine.store.list_cards()}


class ChatForkRequest(BaseModel):
    index: int | None = None


@router.post("/sessions/{session_id}/fork")
async def fork_chat(
    session_id: str, body: ChatForkRequest, user: str = Depends(require_user)
) -> dict:
    """Branch a Home thread at a message into a new conversation."""
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
    await chat_engine.store.set_meta(session_id, title=body.title, pinned=body.pinned)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_chat_session(session_id: str, user: str = Depends(require_user)) -> dict:
    await chat_engine.store.delete(session_id)
    chat_sessions.pop(session_id, None)
    return {"deleted": session_id}
