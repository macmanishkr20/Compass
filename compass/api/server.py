"""The app itself: what Compass mounts, and what it starts and stops.

The routes live with the section that owns them and are mounted here.

    compass.common.auth        signing in
    compass.home.routes     Home/Chat — tool-free, its own store
    compass.code.routes     Code/Agent — sessions, workspaces, routines
    compass.design.routes   Design — projects, generation, export
    compass.common.routes   the web UI, health, speech, memory, recap
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from compass.common.auth import router as auth_router
from compass.common.config import get_settings
from compass.code.routes import engine
from compass.common.workspaces import UnknownWorkspace
from compass.code.routes import router as code_router
from compass.common.routes import router as common_router
from compass.design.routes import router as design_router
from compass.home.routes import router as chat_router
from compass.common.persistence.factory import get_transcript_store
from compass.code.mcp.manager import get_mcp_manager
from compass.common.telemetry import log_event, setup_telemetry

logger = logging.getLogger("compass.api")


def _make_compass_audible() -> None:
    """Give Compass's own loggers somewhere to write.

    Under uvicorn the root logger has no handler, so everything logged under
    `compass.*` is discarded — including the line that says a call spent its
    whole output budget thinking, which is the one worth reading when a
    generation comes back short. uvicorn's own handler is reused where there
    is one, so the format matches the rest of the output.

    Left alone entirely if something has already configured logging.
    """
    ours = logging.getLogger("compass")
    # The level is set whatever else is true. Inheriting it from a root that
    # nobody configured leaves us at WARNING, which silences exactly the lines
    # this exists to show.
    ours.setLevel(os.environ.get("COMPASS_LOG_LEVEL", "INFO").upper())
    if ours.handlers:
        return
    # Somewhere to write, but only if there is nowhere already: a handler on
    # the root serves us through propagation, and adding a second would print
    # everything twice.
    if logging.getLogger().handlers:
        return
    for handler in logging.getLogger("uvicorn").handlers or ():
        ours.addHandler(handler)  # match the format of the rest of the output
    if not ours.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s:     %(name)s — %(message)s")
        )
        ours.addHandler(handler)


# The built web UI ships beside this file, and is served by the app rather than
# by any one section of it.
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    _make_compass_audible()
    setup_telemetry()
    manager = get_mcp_manager()
    await manager.start()
    if manager.status:
        logger.info("mcp servers: %s", manager.status)
    log_event("server_started", mcp_servers=len(manager.status))
    from compass.code.routines import start_scheduler

    start_scheduler(engine)
    # Only when the module is on. A trigger loop for a disabled module would
    # poll mailboxes for pipelines nobody can see.
    if get_settings().pipelines.enabled:
        from compass.pipelines import runner as pipeline_runner

        pipeline_runner.start()
    # Scheduled mission starts, on the same terms. Started here rather than
    # with `@app.on_event("startup")`, which is both deprecated and silently
    # ignored once an app has a lifespan — registered that way the loop never
    # ran at all, and nothing said so.
    #
    # Scheduled starts only: nothing that was running when the process died is
    # resumed on boot. A server that spends money on its own after a crash,
    # with nobody having asked it to, is not a feature.
    mission_scheduler = None
    if get_settings().missions.enabled:
        import asyncio

        from compass.missions.schedule import scheduler_loop

        mission_scheduler = asyncio.create_task(scheduler_loop())
    yield
    if mission_scheduler is not None:
        mission_scheduler.cancel()
    await manager.stop()
    store = get_transcript_store()
    close = getattr(store, "close", None)
    if close is not None:
        await close()


app = FastAPI(title="Compass", version="0.2.0", lifespan=lifespan)


@app.exception_handler(UnknownWorkspace)
async def _unknown_workspace(_request: Request, exc: UnknownWorkspace) -> JSONResponse:
    """A workspace id that does not resolve is a 404, everywhere at once.

    Twelve endpoints resolve a workspace root, and every one of them used to
    receive the server's own repo when the id was stale. Handling it here
    rather than at each call site means a new endpoint that resolves a
    workspace inherits the right answer instead of having to remember it.
    """
    return JSONResponse(status_code=404, content={"detail": str(exc)})


# Mounted in the order they were written out when they all lived here, so
# the order FastAPI matches paths in does not change.
app.include_router(auth_router)
app.include_router(chat_router)  # Home/Chat — isolated, tool-free workflow


@app.get("/")
async def ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.include_router(common_router)
app.include_router(code_router)
app.include_router(design_router)

# Pipelines is opt-in and mounted last, so the paths every other module
# matches are unchanged whether it is on or off. The import is inside the
# conditional rather than at the top of the file on purpose: a module that is
# switched off should cost nothing at all, and "nothing" includes import time
# and anything its imports would start.
if get_settings().pipelines.enabled:
    from compass.pipelines.routes import router as pipelines_router

    app.include_router(pipelines_router)
    logger.info("Pipelines enabled")

# Estimate, on the same terms and for the same reason — mounted last, imported
# inside the conditional, costing a build that does not want it nothing at all.
# Off by default, unlike Pipelines: see EstimateSettings for why a module that
# quotes money should be opted into rather than out of.
if get_settings().estimate.enabled:
    from compass.estimate.routes import router as estimate_router

    app.include_router(estimate_router)
    logger.info("Estimate enabled")

# Missions — long-running autonomous builds. Same contract again: opted into,
# imported inside the conditional, and absent entirely when off. A module that
# runs for hours unattended and spends money is the clearest case yet for
# being switched on deliberately.
if get_settings().missions.enabled:
    from compass.missions.routes import router as missions_router

    app.include_router(missions_router)
    logger.info("Missions enabled")

