"""The routes that are nobody's feature in particular.

The web UI itself, liveness, the model list, speech, screenshots, memory,
the recap and the Customize panel: every one of them is reached from more
than one section of Compass, so none of them belongs inside one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from compass.common.auth import require_user
from compass.common.config import effort_levels_for, get_settings
from compass.code.mcp.manager import get_mcp_manager

logger = logging.getLogger("compass.common")

router = APIRouter()


class SpeechRequest(BaseModel):
    text: str
    voice: str | None = None


def _tts_voices() -> list[str]:
    from compass.common.speech import AVAILABLE_VOICES

    return AVAILABLE_VOICES


def _sandbox_status() -> dict:
    from compass.common import sandbox

    settings = get_settings()
    state = sandbox.availability()
    return {
        "enabled": settings.sandbox.enabled,
        "backend": state.backend,
        "enforced": settings.sandbox.enabled and state.ok,
        "network": settings.sandbox.network,
        "reason": "" if state.ok else state.reason,
    }


@router.get("/healthz")
async def healthz() -> dict:
    settings = get_settings()
    manager = get_mcp_manager()
    return {
        "status": "ok",
        "mock_model": settings.mock_model,
        "deployment": settings.azure.deployment,
        "models": settings.azure.model_options,
        # Beside the models, because the effort a picker may offer depends on
        # which model is selected — see /v1/models. Sent here too because this
        # is what the UI reads at boot, and a picker that had to wait for a
        # second call would show the wrong levels until it arrived.
        "efforts": {m: list(effort_levels_for(m))
                    for m in settings.azure.model_options},
        "github": settings.github.enabled,
        "storage_backend": settings.storage.backend,
        "telemetry": settings.telemetry.enabled,
        "auth": settings.auth.enabled,
        "tts": bool(settings.azure.tts_deployment),
        "tts_voice": settings.azure.tts_voice,
        "tts_voices": _tts_voices(),
        "mcp_servers": manager.status,
        "mcp_tools": [t.name for t in manager.tools],
        # Whether the Pipelines section exists at all. The UI reads this
        # rather than assuming, so a build with the module off shows three
        # sections and never a nav entry leading to routes that are not
        # mounted.
        "pipelines": settings.pipelines.enabled,
        # Same contract as `pipelines`: the UI reads whether the section exists
        # rather than assuming it, so a build with Estimate off shows no nav
        # entry leading to routes that are not mounted.
        "estimate": settings.estimate.enabled,
        # And Missions, on the same contract: the nav asks rather than
        # assuming, so a build without it shows no entry into routes that
        # were never mounted.
        "missions": settings.missions.enabled,
        # Whether shell commands run inside an OS-enforced boundary, and if
        # not, why. Reported rather than logged because "your agent is running
        # with the server's privileges" is a thing an operator should be able
        # to see without reading a log — particularly on Windows, where no
        # boundary exists and Compass carries on regardless.
        "sandbox": _sandbox_status(),
        "workspace": str(settings.workspace_root),
    }


# ---- models ---------------------------------------------------------------


@router.get("/v1/models")
async def list_models(user: str = Depends(require_user)) -> dict:
    """The deployments to choose from, and what each one will think at.

    The efforts come with the models because they are a property of the model
    and not of the product: the two families deployed here accept overlapping
    but different ladders, so a picker with one list built into it offers, for
    one of them, a level the API answers with a 400. The UI asks rather than
    knows, and a family added in config needs no release to be offered
    correctly.
    """
    settings = get_settings()
    models = settings.azure.model_options
    return {
        "models": models,
        "default": settings.azure.deployment,
        "efforts": {m: list(effort_levels_for(m)) for m in models},
    }


# ---- text-to-speech (read aloud) ------------------------------------------


@router.post("/v1/speech")
async def speech(body: SpeechRequest, user: str = Depends(require_user)) -> Response:
    """Synthesize expressive speech for a response. 503 when TTS isn't
    configured — the client then falls back to the browser voice."""
    from compass.common.speech import SpeechDisabledError, synthesize

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


class ScreenshotRequest(BaseModel):
    url: str
    full_page: bool = False


@router.post("/v1/screenshot")
async def take_screenshot(
    body: ScreenshotRequest, user: str = Depends(require_user)
) -> dict:
    """Headless-browser screenshot of a URL, returned as a data: URI."""
    from compass.common.screenshot import capture_data_uri

    try:
        image = await capture_data_uri(body.url, full_page=body.full_page)
    except RuntimeError as err:
        raise HTTPException(status_code=422, detail=str(err))
    except Exception as err:  # noqa: BLE001 - surface a readable message
        raise HTTPException(status_code=502, detail=f"screenshot failed: {err}")
    return {"image": image}


@router.get("/v1/customize")
async def get_customize(user: str = Depends(require_user)) -> dict:
    """One place listing what Compass can do and what it's connected to —
    the port of Claude's Customize section (skills / plugins / connectors)."""
    # This route is the one place that reports on all of Compass at once, so
    # it is also the one place in common that reaches into a section. Deferred,
    # so importing common cannot pull the agent in behind it.
    from compass.code.tools.registry import get_all_tools

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
        from compass.code.routines import store as routine_store

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


@router.get("/v1/recap")
async def get_recap(days: int = 30, user: str = Depends(require_user)) -> dict:
    """"How you've been working with Compass" — topics, busiest day, peak hour."""
    from compass.common.recap import build_recap

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


@router.get("/v1/memory")
async def list_memory(scope: str | None = None, user: str = Depends(require_user)) -> dict:
    """Everything Compass remembers, grouped by category in the UI."""
    from compass.common.memory import CATEGORIES, get_memory_store

    entries = await get_memory_store().list(scope)
    return {"entries": entries, "categories": CATEGORIES}


@router.post("/v1/memory")
async def add_memory(body: MemoryCreate, user: str = Depends(require_user)) -> dict:
    from compass.common.memory import get_memory_store

    return await get_memory_store().add(
        scope=body.scope,
        category=body.category,
        summary=body.summary,
        details=body.details,
    )


@router.patch("/v1/memory/{entry_id}")
async def patch_memory(
    entry_id: str, body: MemoryPatch, user: str = Depends(require_user)
) -> dict:
    from compass.common.memory import get_memory_store

    row = await get_memory_store().update(
        entry_id, summary=body.summary, details=body.details, category=body.category
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no such memory entry")
    return row


@router.delete("/v1/memory/{entry_id}")
async def delete_memory(entry_id: str, user: str = Depends(require_user)) -> dict:
    from compass.common.memory import get_memory_store

    return {"deleted": await get_memory_store().delete(entry_id)}


@router.get("/v1/screenshot-cache/{shot_id}")
async def screenshot_cache(shot_id: str) -> Response:
    """Serve a cached agent screenshot (referenced by screenshot://<id>)."""
    from compass.common.screenshot import get_cached

    png = get_cached(shot_id)
    if png is None:
        raise HTTPException(status_code=404, detail="screenshot expired")
    return Response(content=png, media_type="image/png")
