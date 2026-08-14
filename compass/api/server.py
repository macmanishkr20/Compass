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
import time
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


class DesignCreate(BaseModel):
    name: str = ""
    template: str = "blank"
    prompt: str = ""
    design_system: str = ""
    design_systems: list[str] = []


class DesignPatch(BaseModel):
    name: str | None = None
    html: str | None = None
    prompt: str | None = None
    starred: bool | None = None
    design_system: str | None = None
    design_systems: list[str] | None = None
    turns: list[dict] | None = None


@app.get("/v1/design/templates")
async def design_templates(user: str = Depends(require_user)) -> dict:
    from compass.services.design import TEMPLATES

    return {"templates": TEMPLATES}


@app.get("/v1/design/projects")
async def design_projects(user: str = Depends(require_user)) -> dict:
    from compass.services.design import get_design_store

    return {"projects": await get_design_store().list()}


class DesignClarify(BaseModel):
    prompt: str
    template: str = "blank"
    answers: str = ""      # what the first round already settled
    followup: bool = False  # ask the next round rather than the first


@app.post("/v1/design/clarify")
async def design_clarify(body: DesignClarify, user: str = Depends(require_user)) -> dict:
    """Is this brief enough to design from? If not, what should we ask?"""
    import json as _json

    from compass.services.design import (
        CLARIFY_PROMPT,
        FOLLOWUP_FALLBACK,
        FOLLOWUP_PROMPT,
        TEMPLATES,
        normalize_clarify,
    )

    prompt = body.prompt.strip()
    stem = next(
        (t.get("stem", "") for t in TEMPLATES if t["id"] == body.template), ""
    ).strip()
    # A prompt that is still just the template's opening words, or barely more
    # than that, is the case worth asking about. Anything fuller goes straight
    # through — nobody wants a form in front of a clear request.
    without_stem = prompt[len(stem):].strip() if stem and prompt.startswith(stem) else prompt
    if not body.followup and len(without_stem) >= 25:
        return {"ready": True}

    from compass.gateway.azure_client import get_model_client

    asked = f"Template: {body.template}\nRequest: {prompt or '(empty)'}"
    if body.answers.strip():
        asked += f"\n\nAlready answered:\n{body.answers.strip()}"

    try:
        raw = await get_model_client().complete_utility(
            FOLLOWUP_PROMPT if body.followup else CLARIFY_PROMPT,
            asked,
            max_tokens=4_000,
            prefer_main=True,
        )
    except Exception:  # noqa: BLE001 - never block designing on this
        return {"ready": True}

    raw = raw.strip()
    if "```" in raw:
        import re as _re

        m = _re.search(r"```(?:json)?\s*\n(.*?)```", raw, _re.S)
        if m:
            raw = m.group(1).strip()
    try:
        parsed = _json.loads(raw)
    except ValueError:
        parsed = {}
    form = normalize_clarify(parsed) if parsed.get("fields") else {"fields": []}
    if parsed.get("ready") or not form["fields"]:
        # Being asked for another round is itself the answer: someone who
        # pressed the button wants questions, not "nothing to ask".
        return dict(FOLLOWUP_FALLBACK) if body.followup else {"ready": True}
    return form


@app.post("/v1/design/projects")
async def design_create(body: DesignCreate, user: str = Depends(require_user)) -> dict:
    from compass.services.design import get_design_store

    return await get_design_store().create(
        name=body.name or (body.prompt[:60] if body.prompt else "Untitled"),
        template=body.template,
        prompt=body.prompt,
        design_system=body.design_system,
        design_systems=body.design_systems,
    )


@app.get("/v1/design/projects/{project_id}")
async def design_get(project_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.design import get_design_store

    p = await get_design_store().get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@app.patch("/v1/design/projects/{project_id}")
async def design_patch(
    project_id: str, body: DesignPatch, user: str = Depends(require_user)
) -> dict:
    from compass.services.design import get_design_store

    p = await get_design_store().update(project_id, **body.model_dump())
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@app.delete("/v1/design/projects/{project_id}")
async def design_delete(project_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services import design_files
    from compass.services.design import get_design_store

    deleted = await get_design_store().delete(project_id)
    if deleted:
        design_files.delete_project(project_id)
    return {"deleted": deleted}


class DesignSystemCreate(BaseModel):
    name: str = ""
    source: str = "pasted"   # pasted | upload | url | repo
    text: str = ""           # a style guide, a stylesheet, or notes
    css: str = ""            # tokens to reproduce verbatim
    url: str = ""            # a brand or docs page to read
    workspace_id: str = ""   # a repo already registered as a workspace
    path: str = ""           # ...and the folder inside it to read
    distil: bool = True      # read the source into a system, rather than storing it raw


# What a repo import reads. Stylesheets and token files carry the system; the
# rest of a codebase is noise that would only dilute the distillation.
_STYLE_SUFFIXES = {".css", ".scss", ".sass", ".less", ".styl"}
_TOKEN_NAMES = {
    "tailwind.config.js", "tailwind.config.ts", "theme.ts", "theme.js",
    "tokens.json", "design-tokens.json", "styleguide.md", "style-guide.md",
}
_REPO_READ_LIMIT = 60_000  # characters handed to the model


def _read_repo_styles(root: Path, rel: str) -> tuple[str, list[str]]:
    """Concatenate the stylesheets and token files under `rel`, biggest first."""
    target = _safe_join(root, rel)
    if not target.exists():
        raise HTTPException(status_code=404, detail="no such path in that workspace")

    files: list[Path] = []
    if target.is_file():
        files = [target]
    else:
        for p in target.rglob("*"):
            if any(part in _SKIP_DIRS for part in p.parts) or not p.is_file():
                continue
            if p.suffix.lower() in _STYLE_SUFFIXES or p.name in _TOKEN_NAMES:
                files.append(p)
    if not files:
        raise HTTPException(
            status_code=404, detail="found no stylesheets or token files there"
        )

    # Biggest first, but capped per file — one enormous stylesheet would
    # otherwise eat the whole budget and hide the rest of the system.
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    per_file = max(4_000, _REPO_READ_LIMIT // 8)
    chunks, names, budget = [], [], _REPO_READ_LIMIT
    for p in files:
        if budget <= 0:
            break
        try:
            body = p.read_text(errors="replace")[: min(per_file, budget)]
        except OSError:
            continue
        budget -= len(body)
        names.append(str(p.relative_to(root)))
        chunks.append(f"/* {p.relative_to(root)} */\n{body}")
    return "\n\n".join(chunks), names


async def _fetch_page(url: str) -> str:
    """Read a page's own markup for distillation. The user names the URL — it is
    never taken from generated content."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="only http(s) URLs can be read")
    import httpx

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            r = await client.get(url, headers={"user-agent": "Compass Design"})
            r.raise_for_status()
            return r.text[:_REPO_READ_LIMIT]
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"could not read {url}: {err}")


@app.get("/v1/design/systems")
async def design_systems(user: str = Depends(require_user)) -> dict:
    from compass.services.design import BUILTIN_SYSTEMS, get_system_store

    return {"systems": await get_system_store().list(), "included": BUILTIN_SYSTEMS}


@app.post("/v1/design/systems")
async def design_system_create(
    body: DesignSystemCreate, user: str = Depends(require_user)
) -> dict:
    """Import a design system. With `distil`, the pasted source is read into a
    short system first — a whole stylesheet in the prompt would crowd out the
    actual design request."""
    from compass.services.design import EXTRACT_PROMPT, get_system_store, parse_extract

    text, origin = body.text.strip(), ""
    if body.url.strip():
        text = await _fetch_page(body.url.strip())
        origin = body.url.strip()
    elif body.workspace_id:
        from compass.services.workspaces import get_workspace_registry

        root = await get_workspace_registry().resolve_root(body.workspace_id)
        text, names = _read_repo_styles(root, body.path)
        origin = f"{body.workspace_id}/{body.path}".rstrip("/") + f" ({len(names)} files)"

    if not text and not body.css.strip():
        raise HTTPException(status_code=400, detail="nothing to import")

    name, notes, fonts, swatches = body.name.strip(), text, "", []
    if text and body.distil:
        from compass.gateway.azure_client import get_model_client

        try:
            notes = (
                await get_model_client().complete_utility(
                    EXTRACT_PROMPT, text[:40_000], max_tokens=8_000, prefer_main=True
                )
            ).strip() or text
        except Exception as err:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not read that: {err}")
        read_name, fonts, swatches = parse_extract(notes)
        name = name or read_name

    return await get_system_store().create(
        name=name,
        source=body.source,
        notes=notes,
        css=body.css,
        fonts=fonts,
        swatches=swatches,
        origin=origin,
    )


class SystemSetup(BaseModel):
    """The set-up form: who you are, and whatever design material you can hand
    over. Everything but the blurb is optional — the point is to take what a
    team already has rather than make them write a specification."""

    name: str = ""
    blurb: str = ""            # company and what it makes, or the system's name
    github: str = ""           # https://github.com/owner/repo
    workspace_id: str = ""     # a repo already registered here
    path: str = ""             # ...and a folder inside it
    files: list[dict] = []     # {name, text} read in the browser
    images: list[str] = []     # data: URLs — logos, screenshots, brand pages
    notes: str = ""            # anything else worth knowing
    css: str = ""              # tokens to reproduce verbatim


@app.post("/v1/design/systems/setup")
async def design_system_setup(
    body: SystemSetup, user: str = Depends(require_user)
) -> dict:
    """Build a design system from everything the form collected."""
    from compass.services.design import EXTRACT_PROMPT, get_system_store, parse_extract

    sources: list[str] = []
    origin_bits: list[str] = []

    if body.blurb.strip():
        sources.append("The company, in their words:\n" + body.blurb.strip())

    repo_workspace, repo_path = body.workspace_id.strip(), body.path.strip()
    if body.github.strip() and not repo_workspace:
        # Clone it, then read it the same way a registered repo is read.
        full_name = (
            body.github.strip()
            .replace("https://github.com/", "")
            .replace("http://github.com/", "")
            .rstrip("/")
            .removesuffix(".git")
        )
        try:
            from compass.services.github import clone_repo

            ws = await clone_repo(full_name)
            repo_workspace = ws.to_dict()["id"]
        except Exception as err:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"could not clone {full_name}: {err}. Clone it under Code, "
                "then point at the workspace instead.",
            )
        origin_bits.append(full_name)

    if repo_workspace:
        from compass.services.workspaces import get_workspace_registry

        root = await get_workspace_registry().resolve_root(repo_workspace)
        text, names = _read_repo_styles(root, repo_path)
        sources.append("Stylesheets and tokens from the codebase:\n" + text)
        origin_bits.append(f"{repo_workspace}/{repo_path}".rstrip("/") + f" ({len(names)} files)")

    for f in body.files[:40]:
        name = str(f.get("name", "file"))
        text = str(f.get("text", ""))[:20_000]
        if text.strip():
            sources.append(f"/* {name} */\n{text}")
    if body.files:
        origin_bits.append(f"{len(body.files)} uploaded files")

    if body.notes.strip():
        sources.append("Notes from the team:\n" + body.notes.strip())

    if not sources and not body.images:
        raise HTTPException(status_code=400, detail="nothing to build a system from")

    from compass.gateway.azure_client import get_model_client

    try:
        notes = (
            await get_model_client().complete_utility(
                EXTRACT_PROMPT,
                "\n\n".join(sources)[:60_000] or "Read the attached images.",
                max_tokens=8_000,
                prefer_main=True,
                images=body.images[:6],
            )
        ).strip()
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"could not read that: {err}")

    read_name, fonts, swatches = parse_extract(notes)
    if body.images:
        origin_bits.append(f"{len(body.images)} images")

    return await get_system_store().create(
        name=body.name.strip() or read_name,
        source="set up",
        notes=notes,
        css=body.css,
        fonts=fonts,
        swatches=swatches,
        origin=" · ".join(origin_bits),
    )


@app.post("/v1/design/systems/{system_id}/duplicate")
async def design_system_duplicate(system_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.design import get_system_store

    store = get_system_store()
    system = await store.get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    return await store.duplicate(system)


@app.get("/v1/design/systems/{system_id}/doc")
async def design_system_doc(system_id: str, user: str = Depends(require_user)) -> dict:
    """The system as a browsable project: its pages, its parameters, and the
    files a developer would receive."""
    from compass.services import design_docs
    from compass.services.design import get_system_store

    system = await get_system_store().get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    return {
        "system": {k: v for k, v in system.items() if k != "notes"},
        "name": system.get("name"),
        "notes": system.get("notes", ""),
        "sections": design_docs.tree(system),
        "params": design_docs.theme(system),
        "swatches": design_docs._ramp(system),
        "usage": system.get("usage") or {},
    }


@app.get("/v1/design/systems/{system_id}/page/{section_id}")
async def design_system_page(
    system_id: str, section_id: str, user: str = Depends(require_user)
) -> Response:
    """One section, as a standalone document — what the preview frames render."""
    from compass.services import design_docs
    from compass.services.design import get_system_store

    system = await get_system_store().get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    try:
        html = design_docs.page_html(system, section_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such page")
    return Response(content=html, media_type="text/html")


@app.get("/v1/design/systems/{system_id}/file")
async def design_system_file(
    system_id: str, path: str = "styles.css", user: str = Depends(require_user)
) -> Response:
    """A raw file from the system — the token sheet, the guide, the record."""
    from compass.services import design_docs
    from compass.services.design import get_system_store

    system = await get_system_store().get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    files = {
        "styles.css": (design_docs.styles_css, "text/css"),
        "readme.md": (design_docs.readme_md, "text/markdown"),
        "theme.json": (design_docs.theme_json, "application/json"),
    }
    if path not in files:
        raise HTTPException(status_code=404, detail="no such file")
    build, media = files[path]
    return Response(content=build(system), media_type=media)


@app.get("/v1/design/systems/{system_id}/export")
async def design_system_export(system_id: str, user: str = Depends(require_user)) -> Response:
    from compass.services import design_docs
    from compass.services.design import get_system_store

    system = await get_system_store().get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    stem = "".join(
        c for c in system.get("name", "design-system") if c.isalnum() or c in " -_"
    ).strip() or "design-system"
    return Response(
        content=design_docs.system_zip(system),
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="{stem}.zip"'},
    )


class SystemUsage(BaseModel):
    section: str
    note: str


@app.post("/v1/design/systems/{system_id}/usage")
async def design_system_usage(
    system_id: str, body: SystemUsage, user: str = Depends(require_user)
) -> dict:
    """Usage notes a team adds to a section. Only a system of the user's own can
    carry them — the included ones are read-only by design."""
    from compass.services.design import get_system_store

    store = get_system_store()
    rows = store._read()
    for r in rows:
        if r.get("id") == system_id:
            usage = dict(r.get("usage") or {})
            if body.note.strip():
                usage[body.section] = body.note.strip()
            else:
                usage.pop(body.section, None)
            r["usage"] = usage
            r["updated_at"] = time.time()
            store._write(rows)
            return {"usage": usage}
    raise HTTPException(
        status_code=404, detail="usage notes can only be added to your own systems"
    )


@app.delete("/v1/design/systems/{system_id}")
async def design_system_delete(system_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.design import get_system_store

    return {"deleted": await get_system_store().delete(system_id)}


class DesignHtml(BaseModel):
    html: str
    label: str = "Edited on canvas"


@app.post("/v1/design/projects/{project_id}/html")
async def design_save_html(
    project_id: str, body: DesignHtml, user: str = Depends(require_user)
) -> dict:
    """Store a design edited directly on the canvas, keeping the old one as a
    version. Separate from PATCH so canvas edits always enter history."""
    from compass.services.design import get_design_store

    p = await get_design_store().save_html(project_id, body.html, label=body.label)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@app.post("/v1/design/projects/{project_id}/open")
async def design_open(project_id: str, user: str = Depends(require_user)) -> dict:
    """Mark the project as viewed — backs the table's Last viewed column."""
    from compass.services.design import get_design_store

    p = await get_design_store().touch(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@app.post("/v1/design/projects/{project_id}/duplicate")
async def design_duplicate(project_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.design import get_design_store

    p = await get_design_store().duplicate(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


class PageCreate(BaseModel):
    name: str = ""


@app.get("/v1/design/projects/{project_id}/pages")
async def design_pages(project_id: str, user: str = Depends(require_user)) -> dict:
    from compass.services.design import get_design_store

    store = get_design_store()
    project = await store.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such design project")
    pages = await store.pages(project_id)
    return {"pages": pages, "active": project.get("active_page") or (pages[0]["id"] if pages else "")}


@app.post("/v1/design/projects/{project_id}/pages")
async def design_page_add(
    project_id: str, body: PageCreate, user: str = Depends(require_user)
) -> dict:
    from compass.services.design import get_design_store

    p = await get_design_store().add_page(project_id, body.name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@app.delete("/v1/design/projects/{project_id}/pages/{page_id}")
async def design_page_delete(
    project_id: str, page_id: str, user: str = Depends(require_user)
) -> dict:
    from compass.services.design import get_design_store

    p = await get_design_store().delete_page(project_id, page_id)
    if p is None:
        raise HTTPException(
            status_code=400, detail="a project keeps at least one page"
        )
    return p


@app.post("/v1/design/projects/{project_id}/pages/{page_id}/open")
async def design_page_open(
    project_id: str, page_id: str, user: str = Depends(require_user)
) -> dict:
    from compass.services.design import get_design_store

    p = await get_design_store().open_page(project_id, page_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such page")
    return p


# ---- a project's own files ------------------------------------------------


class ProjectFile(BaseModel):
    path: str
    text: str = ""
    data_url: str = ""


@app.get("/v1/design/projects/{project_id}/files")
async def design_files(
    project_id: str, path: str = "", user: str = Depends(require_user)
) -> dict:
    from compass.services import design_files

    try:
        return design_files.listing(project_id, path)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such folder")


@app.get("/v1/design/projects/{project_id}/files/read")
async def design_file_read(
    project_id: str, path: str, user: str = Depends(require_user)
) -> Response:
    from compass.services import design_files

    try:
        blob, media = design_files.read_bytes(project_id, path)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such file")
    return Response(content=blob, media_type=media)


@app.post("/v1/design/projects/{project_id}/files")
async def design_file_write(
    project_id: str, body: ProjectFile, user: str = Depends(require_user)
) -> dict:
    from compass.services import design_files

    try:
        if not body.text and not body.data_url:
            return design_files.make_folder(project_id, body.path)
        return design_files.write(
            project_id, body.path, text=body.text, data_url=body.data_url
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except IsADirectoryError:
        raise HTTPException(status_code=400, detail="that path is a folder")


@app.delete("/v1/design/projects/{project_id}/files")
async def design_file_delete(
    project_id: str, path: str, user: str = Depends(require_user)
) -> dict:
    from compass.services import design_files

    try:
        return {"deleted": design_files.remove(project_id, path)}
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))


@app.get("/v1/design/projects/{project_id}/versions")
async def design_versions(project_id: str, user: str = Depends(require_user)) -> dict:
    """The history list — html omitted, since a version can be tens of kilobytes."""
    from compass.services.design import get_design_store

    p = await get_design_store().get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return {
        "current": {"label": p.get("version_label") or "Current", "at": p.get("updated_at")},
        "versions": [
            {k: v for k, v in ver.items() if k != "html"} for ver in (p.get("versions") or [])
        ],
    }


@app.post("/v1/design/projects/{project_id}/versions/{version_id}/restore")
async def design_restore(
    project_id: str, version_id: str, user: str = Depends(require_user)
) -> dict:
    """Restore a past version. The design being replaced becomes a version of
    its own, so restoring is itself undoable."""
    from compass.services.design import get_design_store

    store = get_design_store()
    p = await store.get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    version = next((v for v in (p.get("versions") or []) if v.get("id") == version_id), None)
    if version is None:
        raise HTTPException(status_code=404, detail="no such version")
    return await store.save_html(project_id, version["html"], label="Restored") or p


class DesignComment(BaseModel):
    x: float = 0        # position as a fraction of the design's width
    y: float = 0        # ...and of its height, so pins survive a resize
    text: str = ""
    resolved: bool | None = None


@app.post("/v1/design/projects/{project_id}/comments")
async def design_comment_add(
    project_id: str, body: DesignComment, user: str = Depends(require_user)
) -> dict:
    import uuid as _uuid

    from compass.services.design import get_design_store

    store = get_design_store()
    p = await store.get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    comments = list(p.get("comments") or [])
    comments.append(
        {
            "id": _uuid.uuid4().hex[:12],
            "x": body.x,
            "y": body.y,
            "text": body.text,
            "author": user,
            "resolved": False,
            "at": time.time(),
        }
    )
    return await store.update(project_id, comments=comments) or p


@app.delete("/v1/design/projects/{project_id}/comments/{comment_id}")
async def design_comment_delete(
    project_id: str, comment_id: str, user: str = Depends(require_user)
) -> dict:
    from compass.services.design import get_design_store

    store = get_design_store()
    p = await store.get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    kept = [c for c in (p.get("comments") or []) if c.get("id") != comment_id]
    return await store.update(project_id, comments=kept) or p


@app.get("/v1/design/projects/{project_id}/thumbnail")
async def design_thumbnail(project_id: str, user: str = Depends(require_user)) -> Response:
    """A small render of the design, cached on disk until the design changes."""
    from compass.services.design import get_design_store

    p = await get_design_store().get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    html = p.get("html") or ""
    if not html:
        raise HTTPException(status_code=404, detail="no design yet")

    settings = get_settings()
    cache_dir = settings.workspace_root / settings.data_dir / "design_thumbs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{project_id}-{int(p.get('updated_at', 0))}.png"
    if not cached.is_file():
        from compass.services import design_export as ex

        try:
            png = await ex.to_thumbnail(html)
        except RuntimeError as err:
            raise HTTPException(status_code=501, detail=str(err))
        except Exception as err:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"thumbnail failed: {err}")
        cached.write_bytes(png)
        for stale in cache_dir.glob(f"{project_id}-*.png"):
            if stale != cached:
                stale.unlink(missing_ok=True)

    return Response(
        content=cached.read_bytes(),
        media_type="image/png",
        headers={"cache-control": "private, max-age=86400"},
    )


@app.get("/v1/design/projects/{project_id}/export")
async def design_export(
    project_id: str, format: str = "html", user: str = Depends(require_user)
) -> Response:
    """Export the design as html | pdf | png | zip | pptx."""
    from compass.services.design import get_design_store, get_system_store

    project = await get_design_store().get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such design project")
    html = project.get("html") or ""
    if not html:
        raise HTTPException(status_code=409, detail="this project has no design yet")

    stem = "".join(c for c in project.get("name", "design") if c.isalnum() or c in " -_").strip()
    stem = stem or "design"

    def send(body: bytes, media: str, ext: str) -> Response:
        return Response(
            content=body,
            media_type=media,
            headers={"content-disposition": f'attachment; filename="{stem}.{ext}"'},
        )

    if format == "html":
        return send(html.encode(), "text/html", "html")

    from compass.services import design_export as ex

    if format == "zip":
        system = await get_system_store().get(project.get("design_system") or "")
        return send(
            ex.to_zip(
                name=project.get("name", "Design"),
                html=html,
                prompt=project.get("prompt", ""),
                system_notes=(system or {}).get("notes", ""),
            ),
            "application/zip",
            "zip",
        )

    try:
        if format == "pdf":
            return send(await ex.to_pdf(html), "application/pdf", "pdf")
        if format == "png":
            return send(await ex.to_png(html), "image/png", "png")
        if format == "pptx":
            return send(
                await ex.to_pptx(html),
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "pptx",
            )
    except RuntimeError as err:  # an optional library is missing on this host
        raise HTTPException(status_code=501, detail=str(err))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"export failed: {err}")

    raise HTTPException(status_code=400, detail=f"unknown format: {format}")


class DesignGenerate(BaseModel):
    prompt: str
    template: str = ""  # "" = keep the project's own template
    design_system: str = ""
    model: str = ""     # deployment chosen in the composer; "" = the default
    images: list[str] = []  # data: URLs to design from


@app.post("/v1/design/projects/{project_id}/generate")
async def design_generate(
    project_id: str, body: DesignGenerate, user: str = Depends(require_user)
) -> dict:
    """Generate (or refine) the project's design and store the HTML."""
    from compass.services.design import (
        DESIGN_SYSTEM_PROMPT,
        TEMPLATE_PROMPTS,
        get_design_store,
        get_system_store,
        system_prompt_block,
    )

    store = get_design_store()
    project = await store.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such design project")

    template = body.template or project.get("template") or "blank"
    parts = [TEMPLATE_PROMPTS.get(template, "")]

    ids = (
        [body.design_system] if body.design_system
        else project.get("design_systems") or
        ([project["design_system"]] if project.get("design_system") else [])
    )
    if ids:
        store_s = get_system_store()
        parts.append(system_prompt_block(*[await store_s.get(i) for i in ids]))
    if project.get("html"):
        parts.append(
            "Refine the EXISTING design below; keep everything not mentioned "
            "unchanged.\n\n```html\n" + project["html"][:60_000] + "\n```"
        )
    parts.append("Request: " + body.prompt)

    try:
        from compass.gateway.azure_client import get_model_client

        # A full design needs a large budget: on reasoning models the thinking
        # is billed against the same cap, so a small one returns nothing.
        out = await get_model_client().complete_utility(
            DESIGN_SYSTEM_PROMPT,
            "\n\n".join(p for p in parts if p),
            max_tokens=32_000,
            prefer_main=True,
            model=body.model,
            images=body.images,
        )
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"design generation failed: {err}")

    html = out.strip()
    if "```" in html:  # pull the html out of the fenced block
        import re

        m = re.search(r"```(?:html)?\s*\n(.*?)```", html, re.S)
        if m:
            html = m.group(1).strip()
    if not html:
        raise HTTPException(status_code=502, detail="the model returned no design")

    # The seed prompt is the project's identity — refinements are appended to
    # the transcript instead, so reopening a project replays the conversation.
    turns = list(project.get("turns") or [])
    turns.append({"role": "user", "text": body.prompt})
    turns.append(
        {
            "role": "assistant",
            "text": "Here it is — tell me what to change.",
            "steps": ["Reading the brief", "Refining design" if project.get("html") else "Designing"],
            "file": f"{project.get('name', 'Design')}.html",
        }
    )
    await store.update(
        project_id, turns=turns, prompt=project.get("prompt") or body.prompt
    )
    label = "Refined" if project.get("html") else "First version"
    updated = await store.save_html(project_id, html, label=label)
    return updated or {"id": project_id, "html": html}


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


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".angular", "dist", ".next"}


def _safe_join(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, refusing anything that escapes the workspace."""
    target = (root / rel).resolve() if rel else root.resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes the workspace")
    return target


@app.get("/v1/workspaces/{workspace_id}/files")
async def list_files(
    workspace_id: str, path: str = "", user: str = Depends(require_user)
) -> dict:
    """One directory level, folders first — backs the Files tree."""
    from compass.services.workspaces import get_workspace_registry

    root = await get_workspace_registry().resolve_root(workspace_id)
    target = _safe_join(root, path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="not a directory")
    entries = []
    for p in target.iterdir():
        if p.name in _SKIP_DIRS:
            continue
        try:
            entries.append(
                {
                    "name": p.name,
                    "path": str(p.relative_to(root)),
                    "dir": p.is_dir(),
                    "size": 0 if p.is_dir() else p.stat().st_size,
                }
            )
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": path, "entries": entries}


@app.get("/v1/workspaces/{workspace_id}/file")
async def read_file(
    workspace_id: str, path: str, user: str = Depends(require_user)
) -> dict:
    """File contents for the Files viewer (text only, size-capped)."""
    from compass.services.workspaces import get_workspace_registry

    root = await get_workspace_registry().resolve_root(workspace_id)
    target = _safe_join(root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    if target.stat().st_size > 2_000_000:
        raise HTTPException(status_code=413, detail="file too large to preview")
    try:
        return {"path": path, "content": target.read_text(errors="replace")}
    except (OSError, UnicodeDecodeError):
        raise HTTPException(status_code=415, detail="not a text file")


@app.get("/v1/workspaces/{workspace_id}/files/search")
async def search_files(
    workspace_id: str, q: str, content: bool = False, user: str = Depends(require_user)
) -> dict:
    """Filter by filename, or (content=true, the '?text' syntax) grep contents."""
    from compass.services.workspaces import get_workspace_registry

    root = await get_workspace_registry().resolve_root(workspace_id)
    needle = q.lower()
    hits: list[dict] = []
    for p in root.rglob("*"):
        if len(hits) >= 200:
            break
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if content:
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    if needle in line.lower():
                        hits.append({"path": rel, "line": i, "text": line.strip()[:200]})
                        break
            except (OSError, UnicodeDecodeError):
                continue
        elif needle in rel.lower():
            hits.append({"path": rel, "line": 0, "text": ""})
    return {"query": q, "content": content, "hits": hits}


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
