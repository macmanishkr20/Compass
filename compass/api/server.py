"""FastAPI surface — the port of Claude Code's REPL/SDK boundary.

The engine yields events; this module turns them into Server-Sent Events.
Permission "ask" verdicts surface as permission_request events on the
stream and are resolved out-of-band via REST — the same shape as the
remote permission bridge. A minimal web UI is served at /.

    GET  /                                             web UI
    POST /v1/sessions                                  create or resume a session
    POST /v1/sessions/{sid}/messages                   send input, stream SSE
    POST /v1/sessions/{sid}/permissions/{request_id}   resolve an ask verdict
    POST /v1/sessions/{sid}/abort                      cancel the running turn
    GET  /v1/sessions/{sid}/transcript                 replay the stored record
    GET  /v1/sessions                                  list stored sessions
    GET  /healthz                                      liveness + config summary
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from compass.api.auth import require_user
from compass.api.auth import router as auth_router
from compass.config import get_settings
from compass.core.query_engine import QueryEngine, Session
from compass.models.events import ErrorEvent
from compass.persistence.factory import get_transcript_store
from compass.persistence.session_meta import SessionMeta
from compass.services.mcp.manager import get_mcp_manager
from compass.services.telemetry import log_event, setup_telemetry

logger = logging.getLogger("compass.api")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry()
    manager = get_mcp_manager()
    await manager.start()
    if manager.status:
        logger.info("mcp servers: %s", manager.status)
    log_event("server_started", mcp_servers=len(manager.status))
    yield
    await manager.stop()
    store = get_transcript_store()
    close = getattr(store, "close", None)
    if close is not None:
        await close()


app = FastAPI(title="Compass", version="0.2.0", lifespan=lifespan)
app.include_router(auth_router)
engine = QueryEngine()
sessions: dict[str, Session] = {}


class CreateSessionRequest(BaseModel):
    permission_mode: str | None = Field(
        default=None, description="default | accept_edits | plan | bypass"
    )
    effort: str | None = Field(default=None, description="minimal | low | medium | high")
    model: str | None = Field(default=None, description="Azure deployment to use")
    workspace_id: str | None = Field(default=None, description="Workspace to operate in")
    resume: bool = Field(default=False, description="Reload transcript if it exists")
    session_id: str | None = None


class SendMessageRequest(BaseModel):
    content: str


class ResolvePermissionRequest(BaseModel):
    behavior: str = Field(description="allow | deny")


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    group: str | None = None
    mode: str | None = None
    effort: str | None = None
    model: str | None = None
    workspace: str | None = None


class WorkspaceFolderRequest(BaseModel):
    path: str | None = None  # register an existing local folder
    name: str | None = None  # or create a new folder by this name


class GitHubCloneRequest(BaseModel):
    full_name: str  # "owner/repo"
    branch: str | None = None


class SpeechRequest(BaseModel):
    text: str
    voice: str | None = None


class EditMessageRequest(BaseModel):
    content: str


class ForkRequest(BaseModel):
    up_to_uuid: str | None = None


def _get_session(session_id: str) -> Session:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return session


def _tts_voices() -> list[str]:
    from compass.services.speech import AVAILABLE_VOICES

    return AVAILABLE_VOICES


@app.get("/")
async def ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/healthz")
async def healthz() -> dict:
    settings = get_settings()
    manager = get_mcp_manager()
    return {
        "status": "ok",
        "mock_model": settings.mock_model,
        "deployment": settings.azure.deployment,
        "models": settings.azure.model_options,
        "github": settings.github.enabled,
        "storage_backend": settings.storage.backend,
        "telemetry": settings.telemetry.enabled,
        "auth": settings.auth.enabled,
        "tts": bool(settings.azure.tts_deployment),
        "tts_voice": settings.azure.tts_voice,
        "tts_voices": _tts_voices(),
        "mcp_servers": manager.status,
        "mcp_tools": [t.name for t in manager.tools],
        "workspace": str(settings.workspace_root),
    }


@app.post("/v1/sessions")
async def create_session(
    body: CreateSessionRequest, user: str = Depends(require_user)
) -> dict:
    if body.resume and body.session_id and await engine.store.exists(body.session_id):
        session = await engine.resume(
            body.session_id,
            permission_mode=body.permission_mode,
            effort=body.effort,
            model=body.model,
            workspace_id=body.workspace_id,
        )
    else:
        session = Session(
            permission_mode=body.permission_mode,
            effort=body.effort,
            model=body.model,
            workspace_id=body.workspace_id,
        )
        if body.session_id:
            session.id = body.session_id
        await engine._attach_workspace(session)
    sessions[session.id] = session
    return {
        "session_id": session.id,
        "resumed_messages": len(session.messages),
        "workspace_id": session.workspace_id,
        "workspace_root": str(session.workspace_root) if session.workspace_root else None,
        "model": session.model,
    }


async def _session_for_mutation(session_id: str) -> Session:
    """Return the live session, resuming from store if the server doesn't hold
    it in memory (e.g. after a restart). Used by edit/regenerate."""
    session = sessions.get(session_id)
    if session is None:
        if not await engine.store.exists(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        session = await engine.resume(session_id)
        sessions[session_id] = session
    return session


def _sse(gen) -> StreamingResponse:
    async def stream():
        try:
            async for event in gen:
                yield event.to_sse()
        except Exception as err:  # noqa: BLE001 — the stream must end with an event
            logger.exception("turn failed")
            yield ErrorEvent(message=str(err)).to_sse()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/sessions/{session_id}/messages")
async def send_message(
    session_id: str, body: SendMessageRequest, user: str = Depends(require_user)
) -> StreamingResponse:
    session = _get_session(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    return _sse(engine.ask(session, body.content))


@app.post("/v1/sessions/{session_id}/messages/{message_uuid}/edit")
async def edit_message(
    session_id: str,
    message_uuid: str,
    body: EditMessageRequest,
    user: str = Depends(require_user),
) -> StreamingResponse:
    """Edit a past user prompt and re-run from that checkpoint."""
    session = await _session_for_mutation(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    return _sse(engine.edit_message(session, message_uuid, body.content))


@app.post("/v1/sessions/{session_id}/regenerate")
async def regenerate(
    session_id: str, user: str = Depends(require_user)
) -> StreamingResponse:
    """Re-run the last user turn (discarding the previous answer)."""
    session = await _session_for_mutation(session_id)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409, detail="a turn is already running")
    return _sse(engine.regenerate(session))


@app.post("/v1/sessions/{session_id}/fork")
async def fork_session(
    session_id: str, body: ForkRequest, user: str = Depends(require_user)
) -> dict:
    if not await engine.store.exists(session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    new_id = await engine.fork(session_id, body.up_to_uuid)
    return {"session_id": new_id}


@app.patch("/v1/sessions/{session_id}")
async def update_session(
    session_id: str, body: UpdateSessionRequest, user: str = Depends(require_user)
) -> dict:
    meta = await engine.update_meta(
        session_id,
        title=body.title,
        pinned=body.pinned,
        archived=body.archived,
        group=body.group,
        mode=body.mode,
        effort=body.effort,
        model=body.model,
        workspace=body.workspace,
    )
    # Reflect settings onto a live session immediately.
    live = sessions.get(session_id)
    if live is not None:
        if body.mode is not None:
            live.permission_mode = body.mode
        if body.effort is not None:
            live.effort = body.effort
        if body.model is not None:
            live.model = body.model or None
        if body.workspace is not None:
            live.workspace_id = body.workspace or None
            await engine._attach_workspace(live)
            live.shell_state.cwd = ""  # reset shell cwd to the new workspace
    return meta.to_dict()


@app.delete("/v1/sessions/{session_id}")
async def delete_session(
    session_id: str, user: str = Depends(require_user)
) -> dict:
    await engine.delete_session(session_id)
    sessions.pop(session_id, None)
    return {"deleted": session_id}


@app.post("/v1/sessions/{session_id}/permissions/{request_id}")
async def resolve_permission(
    session_id: str,
    request_id: str,
    body: ResolvePermissionRequest,
    user: str = Depends(require_user),
) -> dict:
    session = _get_session(session_id)
    if body.behavior not in ("allow", "deny"):
        raise HTTPException(status_code=422, detail="behavior must be allow or deny")
    resolved = session.broker.resolve(request_id, body.behavior == "allow")
    if not resolved:
        raise HTTPException(status_code=404, detail="no pending request with that id")
    return {"request_id": request_id, "behavior": body.behavior}


@app.post("/v1/sessions/{session_id}/abort")
async def abort_turn(session_id: str, user: str = Depends(require_user)) -> dict:
    session = _get_session(session_id)
    session.abort_event.set()
    return {"aborted": True}


@app.get("/v1/sessions/{session_id}/transcript")
async def transcript(
    session_id: str,
    include_sidechains: bool = False,
    user: str = Depends(require_user),
) -> dict:
    if not await engine.store.exists(session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    messages = await engine.store.load(
        session_id, include_sidechains=include_sidechains
    )
    return {"session_id": session_id, "messages": [m.to_record() for m in messages]}


@app.get("/v1/sessions")
async def list_sessions(
    include_archived: bool = True, user: str = Depends(require_user)
) -> dict:
    """Rich conversation list: every transcript that exists, joined with its
    metadata (title, pin, archive, group, mode, effort, timestamps). Sort and
    group-by are computed client-side from these fields."""
    ids = set(await engine.store.list_sessions())
    metas = {m.id: m for m in await engine.meta.list_all()}
    cards = []
    for sid in ids:
        meta = metas.get(sid) or SessionMeta(id=sid)
        if not include_archived and meta.archived:
            continue
        cards.append(meta.to_dict())
    cards.sort(key=lambda c: c["updated_at"], reverse=True)
    return {"sessions": cards}


# ---- models ---------------------------------------------------------------


@app.get("/v1/models")
async def list_models(user: str = Depends(require_user)) -> dict:
    settings = get_settings()
    return {
        "models": settings.azure.model_options,
        "default": settings.azure.deployment,
    }


# ---- text-to-speech (read aloud) ------------------------------------------


@app.post("/v1/speech")
async def speech(body: SpeechRequest, user: str = Depends(require_user)) -> Response:
    """Synthesize expressive speech for a response. 503 when TTS isn't
    configured — the client then falls back to the browser voice."""
    from compass.services.speech import SpeechDisabledError, synthesize

    if not body.text.strip():
        raise HTTPException(status_code=422, detail="text is required")
    try:
        audio = await synthesize(body.text, voice=body.voice)
    except SpeechDisabledError as err:
        raise HTTPException(status_code=503, detail=str(err))
    except Exception as err:  # noqa: BLE001 — surface TTS/API failures cleanly
        raise HTTPException(status_code=502, detail=f"TTS failed: {err}")
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


# ---- workspaces -----------------------------------------------------------


@app.get("/v1/workspaces")
async def list_workspaces(user: str = Depends(require_user)) -> dict:
    from compass.services.workspaces import get_workspace_registry

    ws = await get_workspace_registry().list()
    return {"workspaces": [w.to_dict() for w in ws]}


@app.post("/v1/workspaces/folder")
async def add_folder_workspace(
    body: WorkspaceFolderRequest, user: str = Depends(require_user)
) -> dict:
    from compass.services.workspaces import get_workspace_registry

    reg = get_workspace_registry()
    try:
        if body.path:
            ws = await reg.add_local(body.path, name=body.name)
        elif body.name:
            ws = await reg.create_folder(body.name)
        else:
            raise HTTPException(status_code=422, detail="path or name required")
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return ws.to_dict()


@app.delete("/v1/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str, user: str = Depends(require_user)
) -> dict:
    from compass.services.workspaces import get_workspace_registry

    try:
        await get_workspace_registry().delete(workspace_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"deleted": workspace_id}


# ---- github ---------------------------------------------------------------


@app.get("/v1/github/repos")
async def github_repos(user: str = Depends(require_user)) -> dict:
    from compass.services.github import GitHubDisabledError, list_repos

    try:
        repos = await list_repos()
    except GitHubDisabledError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:  # noqa: BLE001 — surface API/auth failures cleanly
        raise HTTPException(status_code=502, detail=f"GitHub API error: {err}")
    return {"repos": [r.__dict__ for r in repos]}


@app.post("/v1/github/clone")
async def github_clone(
    body: GitHubCloneRequest, user: str = Depends(require_user)
) -> dict:
    from compass.services.github import GitHubDisabledError, clone_repo

    try:
        ws = await clone_repo(body.full_name, body.branch)
    except GitHubDisabledError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(err))
    return ws.to_dict()
