"""The pipeline, the connection, the run — and where each is kept.

Three dataclasses and three stores. The stores write JSON under the data
directory, matching how the rest of Compass persists when Cosmos is not
configured; a Cosmos-backed variant would slot in behind the same methods, as
`persistence/factory.py` already does for transcripts.

Two shapes here are deliberate rather than incidental.

An edge carries `when`. Connecting two nodes means choosing which outcome the
arrow follows — success, failure, completion, skip — so failure handling is
not a feature bolted on later but the thing an arrow already is. It also means
the engine never needs a special "on error" table.

A run stores its state. Not held in a process: an approval node can leave a
run waiting for hours, and a restart in the middle must not lose it. The walk
is load, execute what is ready, persist, stop — which is also what makes the
resume endpoint possible at all.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from compass.common.config import get_settings

logger = logging.getLogger("compass.pipelines")

#: What an edge can follow. The same four Fabric uses, for the same reason:
#: between them they cover error paths, cleanup steps and "either way".
EDGE_CONDITIONS = ("success", "failure", "completion", "skip")

#: Capabilities a pipeline can be granted at edit time. A node declares what
#: it needs; the engine refuses to run it unless the pipeline holds it. This
#: is what stops a scheduled graph inheriting a blanket bypass.
CAPABILITIES = ("shell", "write", "network", "agent")


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Node:
    """One step, with the settings every step shares.

    The common fields mirror the properties pane in Fabric, which is worth
    copying because they are the fields that turn out to be needed on every
    node regardless of what it does — a timeout, a retry policy, a way to keep
    a payload out of the log, and a way to switch a node off without deleting
    it.
    """

    id: str
    type: str  # a NodeType id
    name: str = ""
    description: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    connection_id: str = ""
    position: dict[str, float] = field(default_factory=lambda: {"x": 0, "y": 0})

    timeout_s: int = 43_200  # 12 hours
    retries: int = 0
    retry_interval_s: int = 30
    #: Keep this side out of the run log. Governs logging, never authority —
    #: what a node is allowed to do is the pipeline's capability set.
    secure_input: bool = False
    secure_output: bool = False
    #: Pinned output. When set, a run in `mock` mode returns this instead of
    #: calling anything — so a freshly built pipeline can be shown working
    #: before a single account is connected, which is the moment someone is
    #: least willing to authorize one. Kept on the node rather than on a run
    #: because it is part of the design, not part of one execution.
    mock: dict[str, Any] | None = None
    state: str = "active"  # active | deactivated
    #: When deactivated, what to pretend happened, so downstream branches
    #: still exercise while the node itself never runs.
    mark_as: str = "success"  # success | failure | skip


@dataclass
class Edge:
    source: str
    target: str
    when: str = "success"  # one of EDGE_CONDITIONS
    port: str = "out"  # the source port this leaves from


@dataclass
class Pipeline:
    id: str
    name: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    #: Fixed for the duration of a run, so a run is reproducible.
    parameters: dict[str, Any] = field(default_factory=dict)
    #: Mutable during a run, by an explicit set-variable node.
    variables: dict[str, Any] = field(default_factory=dict)
    #: What this pipeline may do. Empty means it can only run harmless nodes.
    capabilities: list[str] = field(default_factory=list)
    triggers: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    version: int = 1
    #: Set once a manual run has succeeded. Scheduling is refused until then
    #: when `require_manual_first_run` is on.
    proven_at: float | None = None
    #: An estimate this pipeline was costed against, or "".
    #:
    #: An opaque string, and deliberately nothing more. Pipelines does not
    #: import the Estimate module and does not validate this against it: the
    #: two are separately switchable, and a foreign key between them would
    #: mean a pipeline that fails to load on a box where costing is off. The
    #: UI resolves the id when the section exists and shows nothing when it
    #: does not, which is the same contract the nav entry already uses.
    estimate_id: str = ""
    #: Who this pipeline belongs to. Empty is legacy and stays visible; see
    #: `compass.common.ownership`.
    owner: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Pipeline":
        nodes = [Node(**n) for n in d.get("nodes", [])]
        edges = [Edge(**e) for e in d.get("edges", [])]
        known = {
            k: d[k]
            for k in cls.__dataclass_fields__
            if k in d and k not in ("nodes", "edges")
        }
        return cls(**known, nodes=nodes, edges=edges)


@dataclass
class Connection:
    """An endpoint and how to authenticate to it — never the credential.

    `secret_ref` points at the secret store. Keeping the value out of here is
    what lets a pipeline be exported, diffed and shared without leaking, lets
    a connection be re-authorized without touching the pipelines that use it,
    and makes revoking access one delete instead of a search across graphs.
    """

    id: str
    kind: str  # "rest" | "gmail" | "outlook" | "postgres" | …
    name: str
    auth: str = "none"  # none | api_key | basic | oauth2 | managed_identity
    config: dict[str, Any] = field(default_factory=dict)  # endpoint, scopes…
    secret_ref: str = ""
    #: Who may use this connection. A credential is the one record where
    #: sharing by default is a leak rather than an inconvenience, so this is
    #: scoped even though the endpoint it points at may be innocuous.
    owner: str = ""
    created_at: float = field(default_factory=_now)

    def redacted(self) -> dict[str, Any]:
        """The API view: everything except the pointer to the secret."""
        d = asdict(self)
        d.pop("secret_ref", None)
        d.pop("owner", None)
        d["has_secret"] = bool(self.secret_ref)
        return d


@dataclass
class NodeRun:
    node_id: str
    status: str = "pending"
    # pending | running | waiting | done | failed | skipped | inactive
    #: The settings this node actually ran with — after expressions were
    #: resolved, so `@nodes('x').data.id` appears as the value it became.
    #: That difference is the whole reason to record it: a node fails far
    #: more often because a reference resolved to something unexpected than
    #: because the handler is wrong, and the resolved value is the only place
    #: that shows. Emptied when the node asks for `secure_input`.
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    port: str = "out"
    error: str = ""
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class PipelineRun:
    id: str
    pipeline_id: str
    pipeline_name: str
    #: Pinned, so editing a pipeline never rewrites the history of what ran.
    pipeline_version: int
    trigger: str = "manual"  # manual | scheduled | webhook | api
    #: "live" calls the world; "mock" returns pinned or stubbed data and
    #: touches nothing outside. Recorded on the run because a green mock run
    #: and a green live run mean very different things, and a log that did
    #: not say which is a log that misleads.
    mode: str = "live"
    status: str = "running"  # running | waiting | done | failed | cancelled
    parameters: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, NodeRun] = field(default_factory=dict)
    #: Loop bodies, kept apart from `nodes` because a node inside a For each
    #: has one state per item rather than one state. Keyed by the loop's node
    #: id; each entry is that iteration's node states, in item order. The
    #: canvas still shows one box per node — it summarises across the list —
    #: while the run panel can open a single iteration.
    iterations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: The token a waiting node is parked on, so /resume can find it.
    waiting_on: str = ""
    #: Inherited from the pipeline, not from whoever pressed Run: a scheduled
    #: run has no request behind it, and a run must resolve the same
    #: connections whether a person or a timer started it.
    owner: str = ""
    started_at: float = field(default_factory=_now)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["nodes"] = {k: asdict(v) for k, v in self.nodes.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PipelineRun":
        nodes = {k: NodeRun(**v) for k, v in (d.get("nodes") or {}).items()}
        known = {
            k: d[k] for k in cls.__dataclass_fields__ if k in d and k != "nodes"
        }
        return cls(**known, nodes=nodes)


# --------------------------------------------------------------------------- stores


class _JsonStore:
    """One collection of records, local or in Cosmos.

    Was one JSON file per record under the data directory, and still is on
    the local backend — same folder, same filenames, so nothing migrates.
    The reads and writes now go through a Collection, which is what lets the
    same store answer from Cosmos when the backend says so.

    `_read`/`_write`/`_all`/`_delete` stay sync-looking in name only: every
    caller already awaited the public methods, so they became awaitable
    without a ripple.
    """

    def __init__(self, folder: str, *, container: str | None = None,
                 partition_field: str | None = None,
                 partition_source: str | None = None) -> None:
        self._folder = folder
        from compass.common.persistence.catalog import CONTAINER, Collection

        self._items = Collection(
            folder.rstrip("s") or folder, folder, shape="dir",
            container=container or CONTAINER,
            partition_field=partition_field,
            partition_source=partition_source,
        )

    async def _write(self, record_id: str, payload: dict[str, Any]) -> None:
        await self._items.put({**payload, "id": record_id})

    async def _read(self, record_id: str) -> dict[str, Any] | None:
        return await self._items.get(record_id)

    async def _all(self) -> list[dict[str, Any]]:
        return await self._items.all()

    async def _delete(self, record_id: str) -> bool:
        return await self._items.remove(record_id)


class PipelineStore(_JsonStore):
    def __init__(self) -> None:
        super().__init__("pipelines")

    async def create(self, name: str, **kwargs: Any) -> Pipeline:
        pipeline = Pipeline(id=_new_id("pl"), name=name, **kwargs)
        await self._write(pipeline.id, pipeline.to_dict())
        return pipeline

    async def get(self, pipeline_id: str) -> Pipeline | None:
        raw = await self._read(pipeline_id)
        return Pipeline.from_dict(raw) if raw else None

    async def list(self) -> list[Pipeline]:
        return [Pipeline.from_dict(r) for r in await self._all()]

    async def save(self, pipeline: Pipeline, *, bump: bool = True) -> Pipeline:
        """Persist, raising the version when the graph itself changed.

        Runs pin the version they started with, so a bump is what keeps a
        finished run's history describing the pipeline that actually ran
        rather than the one it has since become.
        """
        if bump:
            pipeline.version += 1
        pipeline.updated_at = _now()
        await self._write(pipeline.id, pipeline.to_dict())
        return pipeline

    async def delete(self, pipeline_id: str) -> bool:
        return await self._delete(pipeline_id)


class ConnectionStore(_JsonStore):
    def __init__(self) -> None:
        super().__init__("connections")

    async def create(self, **kwargs: Any) -> Connection:
        conn = Connection(id=_new_id("cn"), **kwargs)
        await self._write(conn.id, asdict(conn))
        return conn

    async def get(self, connection_id: str) -> Connection | None:
        raw = await self._read(connection_id)
        return Connection(**raw) if raw else None

    async def list(self) -> list[Connection]:
        return [Connection(**r) for r in await self._all()]

    async def delete(self, connection_id: str) -> bool:
        return await self._delete(connection_id)


class RunStore(_JsonStore):
    def __init__(self) -> None:
        # Run history lives with the other run histories, partitioned by the
        # pipeline it belongs to: append-heavy and unbounded, unlike the
        # definitions, so one pipeline's history is one partition.
        super().__init__(
            "pipeline_runs", container="runs",
            partition_field="parentId", partition_source="pipeline_id",
        )

    async def create(self, pipeline: Pipeline, trigger: str,
                     parameters: dict[str, Any],
                     mode: str = "live") -> PipelineRun:
        run = PipelineRun(
            id=_new_id("run"),
            pipeline_id=pipeline.id,
            pipeline_name=pipeline.name,
            pipeline_version=pipeline.version,
            trigger=trigger,
            mode=mode,
            parameters={**pipeline.parameters, **parameters},
            variables=dict(pipeline.variables),
            nodes={n.id: NodeRun(node_id=n.id) for n in pipeline.nodes},
            owner=pipeline.owner,
        )
        await self._write(run.id, run.to_dict())
        return run

    async def get(self, run_id: str) -> PipelineRun | None:
        raw = await self._read(run_id)
        return PipelineRun.from_dict(raw) if raw else None

    async def list(self, pipeline_id: str = "", limit: int = 50
                   ) -> list[PipelineRun]:
        runs = [PipelineRun.from_dict(r) for r in await self._all()]
        if pipeline_id:
            runs = [r for r in runs if r.pipeline_id == pipeline_id]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    async def save(self, run: PipelineRun) -> None:
        await self._write(run.id, run.to_dict())

    async def delete(self, run_id: str) -> bool:
        """Drop one run. Needed because deleting a pipeline deletes its
        history, and until now there was no way to remove a run at all."""
        return await self._delete(run_id)


pipelines = PipelineStore()
connections = ConnectionStore()
runs = RunStore()
