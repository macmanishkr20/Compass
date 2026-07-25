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
    return _sse(chat_engine.ask(session, body.content, attachments))


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


@router.get("/sessions")
async def list_chat_sessions(user: str = Depends(require_user)) -> dict:
    """Home conversation cards (id, title, timestamps), most-recent first."""
    return {"sessions": await chat_engine.store.list_cards()}


@router.delete("/sessions/{session_id}")
async def delete_chat_session(session_id: str, user: str = Depends(require_user)) -> dict:
    await chat_engine.store.delete(session_id)
    chat_sessions.pop(session_id, None)
    return {"deleted": session_id}
