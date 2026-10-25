"""The app itself: what Estimator mounts, and what it starts and stops.

One module, so one router. Compass's own server mounts nine and decides
which of them the deployment switched on; there is nothing to decide here,
and the lifespan is correspondingly short — telemetry up, stores closed on
the way out, and nothing in between.

What is deliberately NOT here, because Estimate never used it: the MCP
manager, the routine scheduler, the pipeline trigger loop, the mission
scheduler, and the workspace exception handler. Each was started
unconditionally or behind a flag for a module that does not exist in this
application, and a loop polling for work nobody can create is worse than
no loop at all.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from estimator.common.auth import router as auth_router
from estimator.common.config import get_settings
from estimator.common.telemetry import log_event, setup_telemetry
from estimator.estimate.routes import router as estimate_router

logger = logging.getLogger("estimator.api")


def _make_estimator_audible() -> None:
    """Give our own loggers somewhere to write.

    Under uvicorn the root logger has no handler, so everything logged
    under `estimator.*` is discarded — including the line that says a call
    spent its whole output budget thinking, which is the one worth reading
    when an estimate comes back short. uvicorn's own handler is reused
    where there is one, so the format matches the rest of the output.

    Left alone entirely if something has already configured logging.
    """
    ours = logging.getLogger("estimator")
    ours.setLevel(os.environ.get("ESTIMATOR_LOG_LEVEL",
                                 os.environ.get("COMPASS_LOG_LEVEL",
                                                "INFO")).upper())
    if ours.handlers:
        return
    if logging.getLogger().handlers:
        return
    for handler in logging.getLogger("uvicorn").handlers or ():
        ours.addHandler(handler)
    if not ours.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s:     %(name)s — %(message)s")
        )
        ours.addHandler(handler)


#: The built web UI ships beside this file, and is served by the app rather
#: than by the one section of it.
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    _make_estimator_audible()
    setup_telemetry()
    log_event("server_started")
    yield
    # Every store that holds a connection. Kept even though this app opens
    # fewer of them: the list lives with the stores, and a store added
    # later is closed without anybody having to remember this file.
    from estimator.common.persistence.shutdown import close_all

    await close_all()


app = FastAPI(title="Estimator", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(estimate_router)


@app.get("/healthz")
async def healthz() -> dict:
    """Up, and what it is configured to reach.

    Says whether the model gateway has somewhere to call and whether the
    catalog is pinned, because an estimate that silently fell back to the
    offline catalog is a different answer from one that priced against
    Azure — and this is the cheapest place to find that out.
    """
    settings = get_settings()
    return {
        "ok": True,
        # WHETHER A SIGN-IN IS REQUIRED, which the browser needs before it
        # can decide what to render. Compass answered this from its own
        # much larger health endpoint and the shell read `auth` off it; the
        # shell here reads the same key off this one, because a browser
        # that cannot tell "no session yet" from "auth is off" either shows
        # a login nobody can satisfy or skips one that was required.
        "auth": settings.auth.enabled,
        "estimate_enabled": settings.estimate.enabled,
        "gateway_configured": bool(settings.azure.endpoint),
        "storage_backend": settings.storage.backend,
    }


@app.get("/{path:path}")
async def web_ui(path: str):
    """The built Angular app, with every unknown path falling to index.html.

    A single-page app owns its own routing, so a deep link the server has
    never heard of is the app's to resolve rather than a 404 to hand back.

    WITH NO BUILD PRESENT this used to raise, and the raise came out as a
    500 with a stack trace — on every mistyped API path, because this
    route catches them all. "File at path …/static/index.html does not
    exist" is a true sentence about a file and a useless one about what
    went wrong, which is either that the UI was never built or that the
    path was wrong. It says which now, and says it as a 404.
    """
    candidate = (STATIC_DIR / path).resolve()
    if path and candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
        return FileResponse(candidate)
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        status_code=404,
        content={
            "detail": (
                f"No route {path!r}, and no built UI to fall back to. "
                f"Build it with `cd frontend && npm run build`, or run "
                f"`npm start` and use the dev server on :4200."
            )
        },
    )
