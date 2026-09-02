"""The REST surface for Pipelines.

Mounted only when the module is enabled, which is why the import in
`api/server.py` sits inside the conditional: an unfinished module should not
appear in the route table, and the route table is compared against a recorded
snapshot, so leakage would be caught rather than merely unlikely.

The endpoints follow the shape the other modules use — list, create, get,
patch, delete, plus the verbs this module needs (`run`, `resume`, `cancel`).
`/node-types` is the one that carries the design: the palette and the settings
pane are both rendered from what it returns, so a node type added by a
provider needs no frontend change to become usable.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from compass.common.auth import require_user
from compass.common.config import get_settings
from compass.pipelines import store as pstore
from compass.pipelines.engine import engine
from compass.pipelines.expressions import references
from compass.pipelines.secrets import get_secret_store
from compass.pipelines.store import CAPABILITIES, EDGE_CONDITIONS, Edge, Node
from compass.pipelines.types import get_registry

logger = logging.getLogger("compass.pipelines")

router = APIRouter()


# --------------------------------------------------------------------------- bodies


class PipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PipelinePatch(BaseModel):
    name: str | None = None
    nodes: list[dict[str, Any]] | None = None
    edges: list[dict[str, Any]] | None = None
    parameters: dict[str, Any] | None = None
    variables: dict[str, Any] | None = None
    capabilities: list[str] | None = None
    triggers: list[dict[str, Any]] | None = None
    enabled: bool | None = None


class RunStart(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    trigger: str = "manual"
    #: "live" calls the world; "mock" returns pinned or stubbed data and
    #: touches nothing outside, which is how a pipeline is verified before
    #: anyone connects an account to it.
    mode: str = "live"


class StepRun(BaseModel):
    """Run one node on its own, with `seed` standing in for its upstream."""

    seed: dict[str, Any] = Field(default_factory=dict)
    mode: str = "live"


class BuildMessage(BaseModel):
    """One turn of the builder conversation."""

    content: str = Field(min_length=1, max_length=8000)
    effort: str = "medium"
    model: str = ""


class ResumeBody(BaseModel):
    answer: dict[str, Any] = Field(default_factory=dict)


class ConnectionCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=120)
    auth: str = "none"
    config: dict[str, Any] = Field(default_factory=dict)
    #: Write-only. Stored behind a reference and never returned.
    secret: str = ""


# --------------------------------------------------------------------------- catalogue


@router.get("/v1/pipelines/node-types")
async def node_types(user: str = Depends(require_user)) -> dict:
    """Every node type currently available, with its settings schema.

    Rebuilt per request rather than cached: MCP servers connect and
    disconnect, so a palette that needed a restart to show a node the user
    just connected would be lying.
    """
    from compass.pipelines.nodes import connectors

    types = get_registry().all()
    return {
        "node_types": [t.summary() for t in types.values()],
        "capabilities": list(CAPABILITIES),
        "edge_conditions": list(EDGE_CONDITIONS),
        # The connection kinds the built-in connectors expect, so the
        # Connections panel can offer them rather than asking someone to
        # guess a string that has to match exactly.
        "connection_kinds": connectors.kinds(),
    }


# --------------------------------------------------------------------------- pipelines


@router.get("/v1/pipelines")
async def list_pipelines(user: str = Depends(require_user)) -> dict:
    items = await pstore.pipelines.list()
    items.sort(key=lambda p: p.updated_at, reverse=True)
    return {"pipelines": [p.to_dict() for p in items]}


@router.post("/v1/pipelines")
async def create_pipeline(body: PipelineCreate,
                          user: str = Depends(require_user)) -> dict:
    pipeline = await pstore.pipelines.create(name=body.name.strip())
    return pipeline.to_dict()


@router.get("/v1/pipelines/{pipeline_id}")
async def get_pipeline(pipeline_id: str,
                       user: str = Depends(require_user)) -> dict:
    pipeline = await pstore.pipelines.get(pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="no such pipeline")
    return pipeline.to_dict()


@router.patch("/v1/pipelines/{pipeline_id}")
async def patch_pipeline(pipeline_id: str, body: PipelinePatch,
                         user: str = Depends(require_user)) -> dict:
    pipeline = await pstore.pipelines.get(pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="no such pipeline")

    graph_changed = body.nodes is not None or body.edges is not None
    if body.name is not None:
        pipeline.name = body.name.strip() or pipeline.name
    if body.nodes is not None:
        pipeline.nodes = [Node(**n) for n in body.nodes]
    if body.edges is not None:
        pipeline.edges = [Edge(**e) for e in body.edges]
    if body.parameters is not None:
        pipeline.parameters = body.parameters
    if body.variables is not None:
        pipeline.variables = body.variables
    if body.capabilities is not None:
        unknown = set(body.capabilities) - set(CAPABILITIES)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unknown capability: {', '.join(sorted(unknown))}",
            )
        pipeline.capabilities = body.capabilities
    if body.triggers is not None:
        pipeline.triggers = body.triggers
    if body.enabled is not None:
        pipeline.enabled = body.enabled

    await pstore.pipelines.save(pipeline, bump=graph_changed)
    return pipeline.to_dict()


@router.delete("/v1/pipelines/{pipeline_id}")
async def delete_pipeline(pipeline_id: str,
                          user: str = Depends(require_user)) -> dict:
    return {"deleted": await pstore.pipelines.delete(pipeline_id)}


@router.post("/v1/pipelines/{pipeline_id}/validate")
async def validate_pipeline(pipeline_id: str,
                            user: str = Depends(require_user)) -> dict:
    """What is wrong with this graph, while someone is looking at it.

    Cheaper to say now than at 3am when a schedule fires, which is the whole
    argument for validating at edit time rather than only on the way in.
    """
    pipeline = await pstore.pipelines.get(pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="no such pipeline")

    types = get_registry().all()
    ids = {n.id for n in pipeline.nodes}
    problems: list[dict[str, str]] = []

    for node in pipeline.nodes:
        node_type = types.get(node.type)
        if node_type is None:
            problems.append({"node": node.id,
                             "problem": f"unknown node type {node.type!r}"})
            continue
        if node_type.requires and node_type.requires not in pipeline.capabilities:
            problems.append({
                "node": node.id,
                "problem": f"needs the '{node_type.requires}' capability, "
                           f"which this pipeline does not hold",
            })
        if node_type.connection_kind and not node.connection_id:
            problems.append({"node": node.id,
                             "problem": f"needs a "
                                        f"{node_type.connection_kind} connection"})
        for missing in references(node.config) - ids:
            problems.append({"node": node.id,
                             "problem": f"refers to node {missing!r}, "
                                        f"which is not in this pipeline"})

    for edge in pipeline.edges:
        if edge.source not in ids or edge.target not in ids:
            problems.append({"node": edge.source,
                             "problem": "edge points at a node that is gone"})
        if edge.when not in EDGE_CONDITIONS:
            problems.append({"node": edge.source,
                             "problem": f"unknown edge condition {edge.when!r}"})

    if _has_cycle(pipeline):
        problems.append({"node": "", "problem": "the graph contains a cycle"})

    return {"ok": not problems, "problems": problems}


def _has_cycle(pipeline) -> bool:
    """Depth-first cycle check.

    A cycle would leave the walk with nothing ever ready, which surfaces as a
    run that quietly does nothing — a far worse failure than being told.
    """
    outgoing: dict[str, list[str]] = {}
    for edge in pipeline.edges:
        outgoing.setdefault(edge.source, []).append(edge.target)
    seen: set[str] = set()
    stack: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in stack:
            return True
        if node_id in seen:
            return False
        seen.add(node_id)
        stack.add(node_id)
        for nxt in outgoing.get(node_id, ()):
            if visit(nxt):
                return True
        stack.discard(node_id)
        return False

    return any(visit(n.id) for n in pipeline.nodes)


# --------------------------------------------------------------------------- runs


@router.post("/v1/pipelines/{pipeline_id}/run")
async def run_pipeline(pipeline_id: str, body: RunStart,
                       user: str = Depends(require_user)) -> dict:
    pipeline = await pstore.pipelines.get(pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="no such pipeline")
    try:
        run = await engine.start(pipeline, trigger=body.trigger,
                                 parameters=body.parameters, mode=body.mode)
    except PermissionError as err:
        raise HTTPException(status_code=409, detail=str(err))
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))
    return run.to_dict()


@router.post("/v1/pipelines/{pipeline_id}/nodes/{node_id}/run")
async def run_node(pipeline_id: str, node_id: str, body: StepRun,
                   user: str = Depends(require_user)) -> dict:
    """Execute one step, for the node view's "Execute step".

    Goes through the same walk as a full run rather than a simpler executor,
    so a step cannot pass here and fail in a real run over a difference
    between two implementations.
    """
    pipeline = await pstore.pipelines.get(pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="no such pipeline")
    try:
        run = await engine.start(pipeline, trigger="manual", mode=body.mode,
                                 only=node_id, seed=body.seed)
    except PermissionError as err:
        raise HTTPException(status_code=409, detail=str(err))
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))
    return run.to_dict()


@router.get("/v1/pipelines/{pipeline_id}/runs")
async def list_runs(pipeline_id: str, limit: int = 50,
                    user: str = Depends(require_user)) -> dict:
    items = await pstore.runs.list(pipeline_id, limit)
    return {"runs": [r.to_dict() for r in items]}


@router.get("/v1/pipelines/{pipeline_id}/export")
async def export_pipeline(pipeline_id: str,
                          user: str = Depends(require_user)) -> dict:
    """The pipeline as a diagram, an architecture note and a Python package.

    A pipeline is a design, and a design is worth more if it can leave. This
    is the whole bundle in one response rather than a file at a time: it is
    small, the caller almost always wants all of it, and a zip would make the
    contents unreadable in the browser, which is where most of it gets looked
    at first.
    """
    pipeline = await pstore.pipelines.get(pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="No such pipeline")
    from compass.pipelines import export as pexport

    return pexport.bundle(pipeline)


@router.get("/v1/pipeline-runs/{run_id}")
async def get_run(run_id: str, user: str = Depends(require_user)) -> dict:
    run = await pstore.runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    return run.to_dict()


@router.post("/v1/pipeline-runs/{run_id}/resume")
async def resume_run(run_id: str, body: ResumeBody,
                     user: str = Depends(require_user)) -> dict:
    run = await engine.resume(run_id, body.answer)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    return run.to_dict()


@router.post("/v1/pipeline-runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: str = Depends(require_user)) -> dict:
    run = await engine.cancel(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    return run.to_dict()


# --------------------------------------------------------------------------- connections


@router.get("/v1/pipeline-connections")
async def list_connections(user: str = Depends(require_user)) -> dict:
    items = await pstore.connections.list()
    return {"connections": [c.redacted() for c in items]}


@router.post("/v1/pipeline-connections")
async def create_connection(body: ConnectionCreate,
                            user: str = Depends(require_user)) -> dict:
    """Create a connection, putting any credential behind a reference.

    The secret arrives once and is never returned. That is what lets a
    pipeline be exported and shared without leaking, and makes revoking
    access one delete rather than a search across every graph.
    """
    conn = await pstore.connections.create(
        kind=body.kind.strip(), name=body.name.strip(),
        auth=body.auth.strip() or "none", config=body.config,
    )
    if body.secret:
        conn.secret_ref = f"{conn.id}/secret"
        await get_secret_store().set(conn.secret_ref, body.secret)
        pstore.connections._write(conn.id, conn.__dict__)
    return conn.redacted()


@router.delete("/v1/pipeline-connections/{connection_id}")
async def delete_connection(connection_id: str,
                            user: str = Depends(require_user)) -> dict:
    conn = await pstore.connections.get(connection_id)
    if conn and conn.secret_ref:
        await get_secret_store().delete(conn.secret_ref)
    return {"deleted": await pstore.connections.delete(connection_id)}


# --------------------------------------------------------------------------- builder

#: One live builder session per pipeline. Held in memory rather than stored:
#: a conversation about a graph is worth keeping only while the graph is open,
#: and the graph itself — the thing that matters — is persisted at every edit.
_sessions: dict[str, Any] = {}


@router.post("/v1/pipelines/{pipeline_id}/build")
async def build(pipeline_id: str, body: BuildMessage,
                user: str = Depends(require_user)) -> StreamingResponse:
    """Describe a change; the graph on the canvas changes.

    Streams the same events the Code console renders — thinking, tool calls,
    text — because it is the same loop with a different tool set. The canvas
    is refreshed by the client when the turn ends: every edit was already
    written to the store as it happened, so there is nothing to apply.
    """
    from compass.code.routes import _sse
    from compass.code.routes import engine as code_engine
    from compass.pipelines.builder import build_session

    pipeline = await pstore.pipelines.get(pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="no such pipeline")

    session = _sessions.get(pipeline_id)
    if session is None:
        session = build_session(pipeline_id, model=body.model,
                                effort=body.effort)
        _sessions[pipeline_id] = session
        # Tag the transcript so it is not listed as a Code conversation. The
        # builder borrows that loop, which persists every turn the same way —
        # without this, asking a pipeline to add a step puts "Add a Wait step"
        # in the Code console's history.
        meta = await code_engine.ensure_meta(session.id)
        meta.pipeline_id = pipeline_id
        meta.title = f"⚙ {pipeline.name}"
        await code_engine.meta.upsert(meta)
    if session.turn_lock.locked():
        raise HTTPException(status_code=409,
                            detail="the builder is already working")
    session.effort = body.effort or session.effort
    return _sse(code_engine.ask(session, body.content))


@router.delete("/v1/pipelines/{pipeline_id}/build")
async def reset_build(pipeline_id: str,
                      user: str = Depends(require_user)) -> dict:
    """Forget the conversation, keep the graph."""
    _sessions.pop(pipeline_id, None)
    return {"cleared": True}
