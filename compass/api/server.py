"""The app itself: what Compass mounts, and what it starts and stops.

The routes live with the section that owns them and are mounted here.

    compass.api.auth        signing in
    compass.home.routes     Home/Chat — tool-free, its own store
    compass.code.routes     Code/Agent — sessions, workspaces, routines
    compass.design.routes   Design — projects, generation, export
    compass.common.routes   the web UI, health, speech, memory, recap
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from compass.api.auth import router as auth_router
from compass.code.routes import engine
from compass.code.routes import router as code_router
from compass.common.routes import router as common_router
from compass.design.routes import router as design_router
from compass.home.routes import router as chat_router
from compass.persistence.factory import get_transcript_store
from compass.services.mcp.manager import get_mcp_manager
from compass.services.telemetry import log_event, setup_telemetry

logger = logging.getLogger("compass.api")

# The built web UI ships beside this file, and is served by the app rather than
# by any one section of it.
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
