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
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from compass.common.auth import require_user
from compass.common.config import get_settings
from compass.common.ownership import owned, owner_for, visible_to
from compass.pipelines import credentials, oauth
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
    estimate_id: str | None = None


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
    #: The credential type's fields — client id, secret, token. Write-only in
    #: the same way, and the shape `credentials.py` declares.
    values: dict[str, Any] = Field(default_factory=dict)


class ConnectionPatch(BaseModel):
    """Editing a credential without recreating it, so the connection id the
    graph already points at survives a rotated token."""

    name: str | None = None
    values: dict[str, Any] | None = None


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
    # Built from the credential types, not from the HTTP connectors. They were
    # the same list until SQL Server arrived, which is a connection kind with
    # no HTTP connector behind it — and because this list drives the
    # Connections panel, a kind missing from it is a connection nobody can
    # create and a node that can never be set up.
    notes = {c.kind: c.note for c in connectors.CONNECTORS}
    return {
        "node_types": [t.summary() for t in types.values()],
        "capabilities": list(CAPABILITIES),
        "edge_conditions": list(EDGE_CONDITIONS),
        "connection_kinds": [
            {"kind": c.kind, "label": c.label, "auth": c.auth,
             "note": notes.get(c.kind) or c.setup_note}
            for c in credentials.all_types()
        ],
    }


# --------------------------------------------------------------------------- pipelines


async def _owned_pipeline(pipeline_id: str, user: str):
    """The pipeline, if this user may see it — one place, so the ownership
    check cannot be forgotten on a route added later.

    Someone else's pipeline is reported as missing rather than forbidden: a
    403 on a guessed id confirms the id exists, which is the one thing the
    guess was trying to learn.
    """
    pipeline = await pstore.pipelines.get(pipeline_id)
    if not pipeline or not visible_to(user, pipeline.owner):
        raise HTTPException(status_code=404, detail="no such pipeline")
    return pipeline


@router.get("/v1/pipelines")
async def list_pipelines(user: str = Depends(require_user)) -> dict:
    items = owned(await pstore.pipelines.list(), user)
    items.sort(key=lambda p: p.updated_at, reverse=True)
    return {"pipelines": [p.to_dict() for p in items]}


@router.post("/v1/pipelines")
async def create_pipeline(body: PipelineCreate,
                          user: str = Depends(require_user)) -> dict:
    pipeline = await pstore.pipelines.create(name=body.name.strip(),
                                             owner=owner_for(user))
    return pipeline.to_dict()


@router.get("/v1/pipelines/{pipeline_id}")
async def get_pipeline(pipeline_id: str,
                       user: str = Depends(require_user)) -> dict:
    pipeline = await _owned_pipeline(pipeline_id, user)
    return pipeline.to_dict()


@router.patch("/v1/pipelines/{pipeline_id}")
async def patch_pipeline(pipeline_id: str, body: PipelinePatch,
                         user: str = Depends(require_user)) -> dict:
    pipeline = await _owned_pipeline(pipeline_id, user)

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
    if body.estimate_id is not None:
        # Trimmed and length-capped, not validated against the Estimate store:
        # the two modules switch independently, and a pipeline that refuses to
        # load because a costing module is off would be a worse bug than a
        # link that resolves to nothing. "" clears it.
        pipeline.estimate_id = body.estimate_id.strip()[:64]

    await pstore.pipelines.save(pipeline, bump=graph_changed)
    return pipeline.to_dict()


@router.delete("/v1/pipelines/{pipeline_id}")
async def delete_pipeline(pipeline_id: str,
                          user: str = Depends(require_user)) -> dict:
    await _owned_pipeline(pipeline_id, user)
    return {"deleted": await pstore.pipelines.delete(pipeline_id)}


@router.post("/v1/pipelines/{pipeline_id}/validate")
async def validate_pipeline(pipeline_id: str,
                            user: str = Depends(require_user)) -> dict:
    """What is wrong with this graph, while someone is looking at it.

    Cheaper to say now than at 3am when a schedule fires, which is the whole
    argument for validating at edit time rather than only on the way in.
    """
    pipeline = await _owned_pipeline(pipeline_id, user)

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

    # Where the graph begins. n8n makes this structural — a workflow starts at
    # a trigger and the editor will not let you forget one — and the reason
    # showed up here as a question nobody should have to ask: "where is the
    # start point, from where the first arrow comes from?" A graph with three
    # unconnected entry points runs all three at once, which is legal, rarely
    # meant, and invisible until it happens.
    if pipeline.nodes:
        targeted = {e.target for e in pipeline.edges}
        entries = [n for n in pipeline.nodes if n.id not in targeted]
        if not entries:
            problems.append({"node": "", "problem":
                             "no starting step — every node is downstream of "
                             "another, so nothing can run first"})
        elif len(entries) > 1:
            names = ", ".join(n.name or n.id for n in entries[:4])
            problems.append({"node": entries[0].id, "problem":
                             f"{len(entries)} steps start at once ({names}). "
                             "If that is deliberate this is fine; if not, "
                             "wire them in sequence or add a Start step."})

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
    pipeline = await _owned_pipeline(pipeline_id, user)
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
    pipeline = await _owned_pipeline(pipeline_id, user)
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
    await _owned_pipeline(pipeline_id, user)
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
    pipeline = await _owned_pipeline(pipeline_id, user)
    from compass.pipelines import export as pexport

    return pexport.bundle(pipeline)


async def _owned_run(run_id: str, user: str):
    run = await pstore.runs.get(run_id)
    if not run or not visible_to(user, run.owner):
        raise HTTPException(status_code=404, detail="no such run")
    return run


@router.get("/v1/pipeline-runs/{run_id}")
async def get_run(run_id: str, user: str = Depends(require_user)) -> dict:
    run = await pstore.runs.get(run_id)
    if not run or not visible_to(user, run.owner):
        raise HTTPException(status_code=404, detail="no such run")
    return run.to_dict()


@router.post("/v1/pipeline-runs/{run_id}/resume")
async def resume_run(run_id: str, body: ResumeBody,
                     user: str = Depends(require_user)) -> dict:
    await _owned_run(run_id, user)  # checked before it acts, not after
    run = await engine.resume(run_id, body.answer)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    return run.to_dict()


@router.post("/v1/pipeline-runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: str = Depends(require_user)) -> dict:
    await _owned_run(run_id, user)  # checked before it acts, not after
    run = await engine.cancel(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    return run.to_dict()


# --------------------------------------------------------------------------- connections


def _connection_view(conn, values: dict[str, Any] | None = None) -> dict:
    """The API view of a connection: never a stored value, always the state.

    What a person needs to know about a credential is whether it is complete
    and whether it works — not what is in it. `configured` and `signed_in`
    answer that without the API ever handing a token back, which is the rule
    the whole credential store exists to keep.
    """
    view = conn.redacted()
    spec = credentials.get_type(conn.kind)
    values = values or {}
    required = [f.name for f in (spec.fields if spec else ())
                if f.required and not f.from_oauth]
    view["configured"] = all(values.get(name) for name in required)
    view["signed_in"] = bool(values.get("access_token"))
    view["needs_sign_in"] = bool(spec and spec.auth == "oauth2"
                                 and not values.get("access_token"))
    expires_at = values.get("expires_at")
    view["expires_at"] = expires_at
    view["expired"] = bool(expires_at and float(expires_at) <= time.time())
    view["auth_kind"] = spec.auth if spec else conn.auth
    return view


async def _connection_values(conn) -> dict[str, Any]:
    raw = await get_secret_store().get(conn.secret_ref) if conn.secret_ref else ""
    return oauth.load_values(raw)


async def _owned_connection(connection_id: str, user: str):
    conn = await pstore.connections.get(connection_id)
    if not conn or not visible_to(user, conn.owner):
        raise HTTPException(status_code=404, detail="no such connection")
    return conn


@router.get("/v1/pipeline-connection-types")
async def connection_types(user: str = Depends(require_user)) -> dict:
    """What each kind of connection needs, so the form is the credential's own.

    n8n's credential classes declare their fields, how they authenticate and
    how they are tested; the browser renders from that rather than carrying a
    hand-written form per service, which is what keeps the two from drifting.
    """
    return {"types": [c.public() for c in credentials.all_types()]}


@router.get("/v1/pipeline-connections")
async def list_connections(user: str = Depends(require_user)) -> dict:
    items = owned(await pstore.connections.list(), user)
    out = []
    for conn in items:
        out.append(_connection_view(conn, await _connection_values(conn)))
    return {"connections": out}


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
        owner=owner_for(user),
    )
    values = dict(body.values)
    if body.secret and not values:
        # The pre-credential-types shape: one opaque secret. Read as an access
        # token so an older client keeps working.
        values = {"access_token": body.secret}
    if values:
        conn.secret_ref = f"{conn.id}/secret"
        await get_secret_store().set(conn.secret_ref, oauth.dump_values(values))
        pstore.connections._write(conn.id, conn.__dict__)
    return _connection_view(conn, values)


@router.patch("/v1/pipeline-connections/{connection_id}")
async def patch_connection(connection_id: str, body: ConnectionPatch,
                           user: str = Depends(require_user)) -> dict:
    """Edit a credential in place.

    In place because the graph points at the connection id: rotating a token
    by deleting and recreating would silently unwire every node that used it,
    and the person would find out at the next run.
    """
    conn = await _owned_connection(connection_id, user)
    values = await _connection_values(conn)
    if body.name is not None and body.name.strip():
        conn.name = body.name.strip()
        pstore.connections._write(conn.id, conn.__dict__)
    if body.values is not None:
        # Merged, not replaced: the form never sends back a password it was
        # never given, and a blank field must not wipe a stored secret.
        values.update({k: v for k, v in body.values.items() if v != ""})
        ref = conn.secret_ref or f"{conn.id}/secret"
        await get_secret_store().set(ref, oauth.dump_values(values))
        if not conn.secret_ref:
            conn.secret_ref = ref
            pstore.connections._write(conn.id, conn.__dict__)
    return _connection_view(conn, values)


@router.post("/v1/pipeline-connections/{connection_id}/test")
async def test_connection(connection_id: str,
                          user: str = Depends(require_user)) -> dict:
    """Prove the credential now, against the provider.

    The single most valuable thing n8n does with credentials, and the reason
    is scheduling: without a test, whether a credential works is discovered
    when a pipeline runs, which may be at 3am and is certainly not while the
    person who can fix it is looking at the form. A read-only call to the
    smallest endpoint the provider offers turns that into an answer now.

    Never raises. A failed test is an answer, not a server error, and the
    provider's own words are passed through because they are usually the
    actionable ones.
    """
    import httpx

    conn = await _owned_connection(connection_id, user)
    spec = credentials.get_type(conn.kind)
    if spec is None:
        return {"ok": False, "message": f"No credential type for {conn.kind}."}

    values = oauth.with_client_defaults(conn.kind, await _connection_values(conn))

    # A database credential is a driver handshake, not a request, so it has
    # its own tester rather than a URL to GET.
    if conn.kind == "mssql":
        from compass.pipelines.nodes import database

        passed, message = await database.test(values)
        return {"ok": passed, "message": message}

    url = spec.test_url or str(values.get("test_url") or "")
    if not url:
        return {"ok": False, "message":
                "This kind of connection has nothing to test against. Add a "
                "test URL and try again."}

    try:
        values = await oauth.fresh_values(conn, values)
    except ValueError as err:
        return {"ok": False, "message": str(err)}

    headers = credentials.apply_auth(conn.kind, values)
    if not headers and spec.auth != "none":
        return {"ok": False, "message":
                "There is no credential stored on this connection yet."}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(spec.test_method, url,
                                            headers={"Accept": "application/json",
                                                     **headers})
    except Exception as err:  # noqa: BLE001 — a network failure is a result
        return {"ok": False, "message": f"Could not reach {url}: {err}"}

    if response.status_code < 300:
        return {"ok": True, "message": f"{spec.label} answered. The "
                                       "connection works."}
    return {"ok": False, "status": response.status_code,
            "message": f"{spec.label} returned {response.status_code}: "
                       f"{oauth._provider_error(response.text, response.status_code)}"}


@router.post("/v1/pipeline-connections/{connection_id}/sign-in")
async def sign_in(connection_id: str,
                  user: str = Depends(require_user)) -> dict:
    """Where to send the person to authorize this connection."""
    conn = await _owned_connection(connection_id, user)
    values = await _connection_values(conn)
    try:
        return {"url": oauth.start(conn.id, conn.kind, values)}
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))


@router.get("/v1/pipeline-connections/oauth/callback")
async def oauth_callback(code: str = "", state: str = "",
                         error: str = "") -> Any:
    """Where the provider sends the person back.

    Deliberately unauthenticated: the browser arriving here is coming from
    the provider, not from Compass, and may not carry the session cookie
    depending on how the redirect is made. The `state` is the guard — it is
    unguessable, single-use, short-lived, and is what ties this callback to a
    sign-in that Compass itself started for a connection it owns.
    """
    from fastapi.responses import HTMLResponse

    def page(title: str, detail: str, ok: bool) -> HTMLResponse:
        # Plain and self-closing: this window exists for two seconds and its
        # only job is to say which of the two things happened.
        tint = "#177245" if ok else "#a3352c"
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8>"
            f"<title>{title}</title>"
            f"<body style='font:15px/1.5 system-ui;margin:14vh auto;max-width:32rem;"
            f"padding:0 1.5rem;color:#222'>"
            f"<h1 style='font-size:1.15rem;color:{tint}'>{title}</h1>"
            f"<p>{detail}</p>"
            f"<p style='color:#777;font-size:.9rem'>You can close this window "
            f"and go back to Compass.</p>"
            f"<script>setTimeout(()=>window.close(),2500)</script>",
            status_code=200 if ok else 400,
        )

    if error:
        return page("Sign-in was refused", f"The provider said: {error}", False)
    if not code or not state:
        return page("That link is incomplete",
                    "No authorization code came back. Start the sign-in "
                    "again from the Connections panel.", False)
    try:
        await oauth.exchange(state, code)
    except ValueError as err:
        return page("Sign-in did not complete", str(err), False)
    except Exception as err:  # noqa: BLE001
        logger.exception("oauth callback failed")
        return page("Sign-in did not complete", str(err), False)
    return page("Connected", "Compass has an access token and a refresh "
                             "token for this account.", True)


@router.delete("/v1/pipeline-connections/{connection_id}/sign-in")
async def sign_out(connection_id: str,
                   user: str = Depends(require_user)) -> dict:
    """Forget the tokens, keep the connection and its client id.

    Separate from deleting the connection because they are different
    intentions: signing out revokes this box's access, deleting unwires every
    node that pointed at it.
    """
    conn = await _owned_connection(connection_id, user)
    values = await _connection_values(conn)
    for key in ("access_token", "refresh_token", "expires_at"):
        values.pop(key, None)
    if conn.secret_ref:
        await get_secret_store().set(conn.secret_ref, oauth.dump_values(values))
    return _connection_view(conn, values)


@router.delete("/v1/pipeline-connections/{connection_id}")
async def delete_connection(connection_id: str,
                            user: str = Depends(require_user)) -> dict:
    conn = await pstore.connections.get(connection_id)
    if not conn or not visible_to(user, conn.owner):
        return {"deleted": False}
    if conn.secret_ref:
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

    pipeline = await _owned_pipeline(pipeline_id, user)

    session = _sessions.get(pipeline_id)
    if session is None:
        session = await _builder_session(pipeline, body.model, body.effort)
        _sessions[pipeline_id] = session
    if session.turn_lock.locked():
        raise HTTPException(status_code=409,
                            detail="the builder is already working")
    session.effort = body.effort or session.effort
    return _sse(code_engine.ask(session, body.content))


async def _find_builder_session(pipeline_id: str) -> str:
    """The session id this pipeline's builder has been using, if any."""
    from compass.code.routes import engine as code_engine

    for meta in await code_engine.meta.list_all():
        if meta.pipeline_id == pipeline_id:
            return meta.id
    return ""


async def _builder_session(pipeline, model: str, effort: str):
    """The builder's session, resumed where one exists.

    In-memory sessions do not survive a restart, and starting a fresh one
    would abandon the conversation that explains how the graph on screen came
    to be — which is the reason to keep it at all. The transcript is already
    on disk; this reattaches to it.
    """
    from compass.code.routes import engine as code_engine
    from compass.pipelines.builder import build_session, SYSTEM_PROMPT
    from compass.pipelines.tools import builder_tools

    existing = await _find_builder_session(pipeline.id)
    if existing:
        session = await code_engine.resume(existing, permission_mode="bypass")
        # `resume` rebuilds a plain Code session, so the two things that make
        # it a builder have to be put back or it would carry file tools and a
        # repository prompt into a conversation about a graph.
        session.tool_override = builder_tools(pipeline.id)
        session.prompt_override = SYSTEM_PROMPT
        session.workspace_root = None
        session.effort = effort or session.effort
        return session

    session = build_session(pipeline.id, model=model, effort=effort)
    # Tag the transcript so it is not listed as a Code conversation, and so
    # it can be found again after a restart.
    meta = await code_engine.ensure_meta(session.id)
    meta.pipeline_id = pipeline.id
    meta.title = f"⚙ {pipeline.name}"
    await code_engine.meta.upsert(meta)
    return session


@router.get("/v1/pipelines/{pipeline_id}/build")
async def build_history(pipeline_id: str,
                        user: str = Depends(require_user)) -> dict:
    """The builder conversation for this pipeline.

    Kept because it is the record of how the graph came to look the way it
    does — the questions asked, the assumptions stated, the steps rejected.
    A canvas shows what was decided and never why.
    """
    from compass.code.routes import engine as code_engine

    await _owned_pipeline(pipeline_id, user)
    session_id = await _find_builder_session(pipeline_id)
    if not session_id:
        return {"session_id": "", "messages": []}
    messages = await code_engine.store.load(session_id)
    out = []
    for message in messages:
        role = getattr(message, "role", "")
        if role not in ("user", "assistant"):
            continue  # tool traffic is the machinery, not the conversation
        content = getattr(message, "content", "") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        out.append({"role": role, "text": content})
    return {"session_id": session_id, "messages": out}


@router.delete("/v1/pipelines/{pipeline_id}/build")
async def reset_build(pipeline_id: str,
                      user: str = Depends(require_user)) -> dict:
    """Forget the conversation, keep the graph."""
    from compass.code.routes import engine as code_engine

    await _owned_pipeline(pipeline_id, user)
    _sessions.pop(pipeline_id, None)
    # Forget the transcript as well. Dropping only the in-memory session would
    # make Clear look like it worked and then bring the conversation back on
    # the next reload.
    session_id = await _find_builder_session(pipeline_id)
    if session_id:
        await code_engine.store.delete(session_id)
        await code_engine.meta.delete(session_id)
    return {"cleared": True}
