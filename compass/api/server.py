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

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from compass.api.auth import require_user
from compass.api.auth import router as auth_router
from compass.api.chat_routes import router as chat_router
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
    from compass.services.routines import start_scheduler

    start_scheduler(engine)
    yield
    await manager.stop()
    store = get_transcript_store()
    close = getattr(store, "close", None)
    if close is not None:
        await close()


app = FastAPI(title="Compass", version="0.2.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(chat_router)  # Home/Chat — isolated, tool-free workflow
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


class MessageAttachment(BaseModel):
    """A raw uploaded file for the Agent Console — same shape/handling as the
    chat surface (services.attachments classifies + extracts)."""

    name: str = ""
    mime: str = ""
    data_url: str | None = None
    text: str | None = None


class SendMessageRequest(BaseModel):
    content: str
    attachments: list[MessageAttachment] = []


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


class TriggerModel(BaseModel):
    type: str = "daily"  # once|hourly|daily|weekdays|weekly|custom
    time: str = "09:00"
    days: list[int] = Field(default_factory=list)
    cron: str = ""
    date: str = ""


class RoutineRequest(BaseModel):
    name: str = ""
    prompt: str
    triggers: list[TriggerModel] = Field(default_factory=list)
    target: str = "local"  # local | cloud
    model: str = ""
    repository: str = ""
    connectors: list[str] = Field(default_factory=list)
    behavior: dict = Field(default_factory=lambda: {"auto_fix_prs": False})
    notifications: dict = Field(
        default_factory=lambda: {"enabled": True, "push": True, "email": False, "slack": False}
    )


class RoutinePatchRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    triggers: list[TriggerModel] | None = None
    target: str | None = None
    model: str | None = None
    repository: str | None = None
    connectors: list[str] | None = None
    behavior: dict | None = None
    notifications: dict | None = None
    enabled: bool | None = None


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
    attachments = [a.model_dump() for a in body.attachments]
    return _sse(engine.ask(session, body.content, attachments))


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
    if body.behavior not in ("allow", "deny", "allow_always"):
        raise HTTPException(
            status_code=422, detail="behavior must be allow, deny, or allow_always"
        )
    if body.behavior == "allow_always":
        resolved = session.broker.allow_always(request_id)
    else:
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


@app.get("/v1/workspaces/{workspace_id}/git")
async def workspace_git(
    workspace_id: str, user: str = Depends(require_user)
) -> dict:
    """Working-tree summary (branch, diff stats, ahead) for the status bar."""
    from compass.services.workspaces import get_workspace_registry, git_summary

    root = await get_workspace_registry().resolve_root(workspace_id)
    return git_summary(root)


class ScreenshotRequest(BaseModel):
    url: str
    full_page: bool = False


@app.post("/v1/screenshot")
async def take_screenshot(
    body: ScreenshotRequest, user: str = Depends(require_user)
) -> dict:
    """Headless-browser screenshot of a URL, returned as a data: URI."""
    from compass.services.screenshot import capture_data_uri

    try:
        image = await capture_data_uri(body.url, full_page=body.full_page)
    except RuntimeError as err:
        raise HTTPException(status_code=422, detail=str(err))
    except Exception as err:  # noqa: BLE001 - surface a readable message
        raise HTTPException(status_code=502, detail=f"screenshot failed: {err}")
    return {"image": image}


SUGGEST_PROMPT = (
    "You suggest the ONE most likely next thing the user will ask, based on the "
    "exchange below. Reply with that single instruction, phrased the way the "
    "user would type it: imperative, lower-case, under 8 words, no quotes, no "
    "trailing period. It must be a concrete follow-up action that clearly "
    "follows from what just happened — e.g. after merging and pushing a branch: "
    "delete the feat/x branch. If nothing obvious follows, reply with exactly: "
    "NONE"
)


@app.post("/v1/sessions/{session_id}/suggest")
async def suggest_next(session_id: str, user: str = Depends(require_user)) -> dict:
    """The next-step suggestion pre-filled in the composer after a turn."""
    session = sessions.get(session_id)

    def _text(c) -> str:
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                p.get("text", "")
                for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return ""

    # Fall back to the stored transcript: a resumed conversation (or one from
    # before a restart) is not in the in-memory registry, and the suggestion
    # should still work there.
    messages = session.messages if session is not None else []
    if not messages:
        try:
            messages = await get_transcript_store().load(session_id)
        except Exception:  # noqa: BLE001
            messages = []

    turns = [
        f"{m.role}: {_text(m.content)[:600]}"
        for m in messages[-6:]
        if m.role in ("user", "assistant") and _text(m.content).strip()
    ]
    if not turns:
        return {"suggestion": ""}
    try:
        from compass.gateway.azure_client import get_model_client

        out = (await get_model_client().complete_utility(
            SUGGEST_PROMPT, "\n\n".join(turns)
        )).strip()
    except Exception:  # noqa: BLE001 — a suggestion is never worth an error
        return {"suggestion": ""}
    # Normalise: single line, drop quotes/trailing period, ignore refusals.
    out = out.splitlines()[0].strip().strip('"“”').rstrip(".")
    if not out or out.upper().startswith("NONE") or len(out) > 80:
        return {"suggestion": ""}
    return {"suggestion": out}


@app.get("/v1/customize")
async def get_customize(user: str = Depends(require_user)) -> dict:
    """One place listing what Compass can do and what it's connected to —
    the port of Claude's Customize section (skills / plugins / connectors)."""
    from compass.tools.registry import get_all_tools

    settings = get_settings()
    manager = get_mcp_manager()

    tools = [
        {"name": t.name, "description": (t.description or "").split(". ")[0] + "."}
        for t in get_all_tools()
    ]

    connectors: list[dict] = [
        {
            "name": "Azure OpenAI",
            "detail": settings.azure.deployment or "not configured",
            "connected": bool(settings.azure.endpoint and settings.azure.api_key),
        },
        {
            "name": "Work IQ (Azure AI Search)",
            "detail": settings.ai_search.index or "not configured",
            "connected": settings.ai_search.configured,
        },
        {
            "name": "GitHub",
            "detail": "pull requests and repo access",
            "connected": settings.github.enabled,
        },
        {
            "name": "Read-aloud (TTS)",
            "detail": settings.azure.tts_voice or "not configured",
            "connected": bool(settings.azure.tts_deployment),
        },
        {
            "name": "Voice mode (Realtime)",
            "detail": settings.azure.realtime_deployment or "not configured",
            "connected": settings.azure.realtime_configured,
        },
        {
            "name": "Cosmos DB",
            "detail": settings.storage.cosmos_database
            if settings.storage.cosmos_configured
            else "local files",
            "connected": settings.storage.cosmos_configured,
        },
        {
            "name": "Blob storage",
            "detail": settings.storage.blob_container
            if settings.storage.blob_configured
            else "local disk",
            "connected": settings.storage.blob_configured,
        },
    ]

    mcp = [
        {"name": name, "detail": state, "connected": state == "connected"}
        for name, state in (manager.status or {}).items()
    ]

    routines: list[dict] = []
    try:
        from compass.services.routines import store as routine_store

        routines = [
            {"name": r.name, "detail": f"{len(r.triggers)} trigger(s)"}
            for r in await routine_store.list()
        ]
    except Exception:  # noqa: BLE001 — the panel must render regardless
        pass

    return {
        "tools": tools,
        "connectors": connectors,
        "mcp_servers": mcp,
        "mcp_tools": [t.name for t in manager.tools],
        "routines": routines,
    }


@app.get("/v1/recap")
async def get_recap(days: int = 30, user: str = Depends(require_user)) -> dict:
    """"How you've been working with Compass" — topics, busiest day, peak hour."""
    from compass.services.recap import build_recap

    return await build_recap(days)


class MemoryCreate(BaseModel):
    scope: str = "home"
    category: str = "Context"
    summary: str
    details: str = ""


class MemoryPatch(BaseModel):
    summary: str | None = None
    details: str | None = None
    category: str | None = None


@app.get("/v1/memory")
async def list_memory(scope: str | None = None, user: str = Depends(require_user)) -> dict:
    """Everything Compass remembers, grouped by category in the UI."""
    from compass.services.memory import CATEGORIES, get_memory_store

    entries = await get_memory_store().list(scope)
    return {"entries": entries, "categories": CATEGORIES}


@app.post("/v1/memory")
async def add_memory(body: MemoryCreate, user: str = Depends(require_user)) -> dict:
    from compass.services.memory import get_memory_store

    return await get_memory_store().add(
        scope=body.scope,
        category=body.category,
        summary=body.summary,
        details=body.details,
    )


@app.patch("/v1/memory/{entry_id}")
async def patch_memory(
    entry_id: str, body: MemoryPatch, user: str = Depends(require_user)
) -> dict:
    from compass.services.memory import get_memory_store

    row = await get_memory_store().update(
        entry_id, summary=body.summary, details=body.details, category=body.category
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no such memory entry")
    return row


@app.delete("/v1/memory/{entry_id}")
async def delete_memory(entry_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.memory import get_memory_store

    return {"deleted": await get_memory_store().delete(entry_id)}


@app.websocket("/v1/browser/ws")
async def browser_ws(ws: WebSocket) -> None:
    """Interactive remote browser: streams a live server-side Chromium into the
    Compass browser pane and drives it from the client's mouse/keyboard — so any
    site renders and is interactive, like claude.ai (no iframe embedding limits).

    Client → server JSON: nav/reload/back/forward, move/down/up/wheel, type/key,
    resize. Server → client JSON: {t:'frame',data} JPEG frames and {t:'nav',...}.
    """
    import json

    from compass.services.remote_browser import RemoteBrowserSession, handle_command

    await ws.accept()
    # Serialise sends so screencast frames and nav events never interleave on
    # the wire, and a send after disconnect can't crash the reader loop.
    send_lock = asyncio.Lock()

    async def emit(event: dict) -> None:
        async with send_lock:
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps(event))

    sess = RemoteBrowserSession(emit)
    try:
        try:
            await sess.start()
        except Exception as err:  # Playwright/Chromium missing or failed
            await emit({"t": "error", "message": f"remote browser unavailable: {err}"})
            await ws.close()
            return
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            await handle_command(sess, msg)
    except WebSocketDisconnect:
        pass
    except Exception as err:  # noqa: BLE001
        logger.warning("browser_ws error: %s", err)
    finally:
        await sess.close()


@app.get("/v1/screenshot-cache/{shot_id}")
async def screenshot_cache(shot_id: str) -> Response:
    """Serve a cached agent screenshot (referenced by screenshot://<id>)."""
    from compass.services.screenshot import get_cached

    png = get_cached(shot_id)
    if png is None:
        raise HTTPException(status_code=404, detail="screenshot expired")
    return Response(content=png, media_type="image/png")


@app.get("/v1/workspaces/{workspace_id}/diff")
async def workspace_diff(
    workspace_id: str, user: str = Depends(require_user)
) -> dict:
    """Unified working-tree diff vs HEAD, for the composer's diff viewer."""
    from compass.services.workspaces import get_workspace_registry, git_diff

    root = await get_workspace_registry().resolve_root(workspace_id)
    return {"diff": git_diff(root)}


class CreatePrRequest(BaseModel):
    draft: bool = False
    manual: bool = False


@app.post("/v1/workspaces/{workspace_id}/pr")
async def workspace_create_pr(
    workspace_id: str,
    body: CreatePrRequest = CreatePrRequest(),
    user: str = Depends(require_user),
) -> dict:
    """Push the branch and open a GitHub PR (gh CLI), optionally as a draft, or
    return the compare URL for manual creation."""
    from compass.services.workspaces import (
        create_pull_request,
        get_workspace_registry,
    )

    root = await get_workspace_registry().resolve_root(workspace_id)
    try:
        return create_pull_request(root, draft=body.draft, manual=body.manual)
    except RuntimeError as err:
        raise HTTPException(status_code=422, detail=str(err))


@app.post("/v1/pick-folder")
async def pick_folder(user: str = Depends(require_user)) -> dict:
    """Open the host's native folder chooser (macOS Finder, Windows folder
    dialog, or Linux zenity) and return the selected absolute path; empty
    string if the user cancelled. Runs off the event loop (it blocks on a GUI)."""
    from compass.services.workspaces import choose_folder

    try:
        path = await asyncio.to_thread(choose_folder)
        return {"path": path}
    except RuntimeError as err:
        raise HTTPException(status_code=422, detail=str(err))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(err))


@app.post("/v1/workspaces/{workspace_id}/reveal")
async def reveal_workspace(
    workspace_id: str, user: str = Depends(require_user)
) -> dict:
    """Reveal the workspace folder in the host's file manager (Finder)."""
    from compass.services.workspaces import (
        get_workspace_registry,
        reveal_in_file_manager,
    )

    root = await get_workspace_registry().resolve_root(workspace_id)
    try:
        return {"opened": reveal_in_file_manager(root)}
    except RuntimeError as err:
        raise HTTPException(status_code=422, detail=str(err))


@app.post("/v1/workspaces/{workspace_id}/terminal")
async def open_workspace_terminal(
    workspace_id: str, user: str = Depends(require_user)
) -> dict:
    """Open a terminal at the workspace folder on the host."""
    from compass.services.workspaces import (
        get_workspace_registry,
        open_in_terminal,
    )

    root = await get_workspace_registry().resolve_root(workspace_id)
    try:
        return {"opened": open_in_terminal(root)}
    except RuntimeError as err:
        raise HTTPException(status_code=422, detail=str(err))


@app.post("/v1/workspaces/{workspace_id}/open-in-vscode")
async def open_workspace_in_vscode(
    workspace_id: str, user: str = Depends(require_user)
) -> dict:
    """Open the workspace folder in VS Code on the host running this backend."""
    from compass.services.workspaces import (
        get_workspace_registry,
        open_in_vscode,
    )

    root = await get_workspace_registry().resolve_root(workspace_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="workspace path not found")
    try:
        cmd = open_in_vscode(root)
    except RuntimeError as err:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not launch VS Code on the server host. Install the 'code' "
                f"command (VS Code → Shell Command: Install 'code' in PATH). {err}"
            ),
        )
    return {"opened": str(root), "command": cmd}


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


# ---- background tasks -----------------------------------------------------


@app.get("/v1/background-tasks")
async def list_background_tasks(user: str = Depends(require_user)) -> dict:
    from compass.services.background_tasks import registry

    tasks = [t.to_dict() for t in registry.list()]
    return {
        "tasks": tasks,
        "running": sum(1 for t in tasks if t["status"] == "running"),
        "finished": sum(1 for t in tasks if t["status"] != "running"),
    }


@app.get("/v1/background-tasks/{task_id}/logs")
async def background_task_logs(task_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.background_tasks import registry

    if not registry.get(task_id):
        raise HTTPException(status_code=404, detail="unknown task")
    return {"lines": registry.logs(task_id)}


@app.post("/v1/background-tasks/{task_id}/stop")
async def stop_background_task(task_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.background_tasks import registry

    ok = await registry.stop(task_id)
    if not ok and not registry.get(task_id):
        raise HTTPException(status_code=404, detail="unknown task")
    return {"stopped": ok}


@app.post("/v1/background-tasks/clear")
async def clear_background_tasks(user: str = Depends(require_user)) -> dict:
    from compass.services.background_tasks import registry

    return {"cleared": await registry.clear_finished()}


# ---- routines -------------------------------------------------------------


@app.get("/v1/routines")
async def list_routines(user: str = Depends(require_user)) -> dict:
    from compass.services.routines import (
        CONNECTOR_OPTIONS, SUGGESTIONS, TEMPLATES, store,
    )

    routines = [r.to_dict() for r in await store.list()]
    return {
        "routines": routines, "templates": TEMPLATES, "suggestions": SUGGESTIONS,
        "connectors": CONNECTOR_OPTIONS,
    }


@app.post("/v1/routines")
async def create_routine(
    body: RoutineRequest, user: str = Depends(require_user)
) -> dict:
    from compass.services.routines import store

    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="instructions are required")
    r = await store.create(
        name=body.name,
        prompt=body.prompt,
        triggers=[t.model_dump() for t in body.triggers],
        target=body.target,
        model=body.model,
        repository=body.repository,
        connectors=body.connectors,
        behavior=body.behavior,
        notifications=body.notifications,
    )
    return r.to_dict()


@app.get("/v1/routines/{routine_id}")
async def get_routine(routine_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.routines import store

    r = await store.get(routine_id)
    if not r:
        raise HTTPException(status_code=404, detail="unknown routine")
    return r.to_dict()


@app.patch("/v1/routines/{routine_id}")
async def update_routine(
    routine_id: str, body: RoutinePatchRequest, user: str = Depends(require_user)
) -> dict:
    from compass.services.routines import store

    patch = body.model_dump(exclude_none=True)
    if "triggers" in patch:
        patch["triggers"] = [t if isinstance(t, dict) else t for t in patch["triggers"]]
    r = await store.update(routine_id, patch)
    if not r:
        raise HTTPException(status_code=404, detail="unknown routine")
    return r.to_dict()


@app.delete("/v1/routines/{routine_id}")
async def delete_routine(routine_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.routines import store

    if not await store.delete(routine_id):
        raise HTTPException(status_code=404, detail="unknown routine")
    return {"deleted": routine_id}


@app.get("/v1/routines/{routine_id}/runs")
async def list_routine_runs(routine_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.routines import runs

    return {"runs": [r.to_dict() for r in await runs.list_for(routine_id)]}


@app.post("/v1/routines/{routine_id}/run")
async def run_routine_now(routine_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.routines import execute_routine, store

    routine = await store.get(routine_id)
    if not routine:
        raise HTTPException(status_code=404, detail="unknown routine")
    run = await execute_routine(routine, "manual", engine)
    return run.to_dict()


@app.get("/v1/routines/runs/{run_id}")
async def get_routine_run(run_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.routines import runs

    run = await runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="unknown run")
    return run.to_dict()


@app.get("/v1/routine-runs/recent")
async def recent_routine_runs(
    since: float = 0.0, user: str = Depends(require_user)
) -> dict:
    """Runs that finished after `since`, each joined with its routine's
    notification settings — the browser polls this to fire push notifications."""
    from compass.services.routines import runs, store

    routines_by_id = {r.id: r for r in await store.list()}
    out = []
    for run in await runs.finished_since(since):
        r = routines_by_id.get(run.routine_id)
        notif = (r.notifications if r else {}) or {}
        d = run.to_dict()
        d["notify_enabled"] = notif.get("enabled", True)
        d["notify_push"] = notif.get("push", False)
        d["notify_email"] = notif.get("email", False)
        out.append(d)
    return {"runs": out, "email_configured": _email_configured()}


def _email_configured() -> bool:
    from compass.services.notify import email_configured

    return email_configured()
