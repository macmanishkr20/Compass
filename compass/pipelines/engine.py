"""Running a graph: ready-set walk, conditional edges, resumable state.

The walk is deliberately not recursive and does not hold the run in memory
between nodes. Each pass loads the state, finds every node whose upstream
edges are all settled, runs those, persists, and goes round again. When
nothing is ready and something is waiting, it stops and returns — the run
sits in the store until `resume` restarts it.

That shape is what makes an approval node possible without a process parked
for hours, and it is also why a restart mid-run loses nothing.

Three rules worth stating because they are easy to get subtly wrong.

*An edge is a condition, not just an ordering.* A node becomes ready when
every incoming edge is satisfied: the source finished, and its outcome
matches what the edge follows. An edge whose condition cannot be met marks
its target skipped, and skipping propagates — which is how a failure branch
avoids dragging the success branch along behind it.

*Capability is checked before the handler, not inside it.* A node declares
what it needs; the pipeline declares what it was granted at edit time by a
person. The engine refuses the node otherwise. Tools keep their own
permission checks underneath, but a graph running unattended must never
acquire authority just because it is composed of tools that would have been
allowed interactively.

*Fan-out is the engine's job.* A ForEach yields on `each`, and everything
downstream of that port runs once per item, with the item visible to the
expression resolver. Handlers cannot do this: only the walk can schedule the
copies and join them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from compass.common.config import get_settings
from compass.pipelines import store as pstore
from compass.pipelines.expressions import ExpressionError, Resolver
from compass.pipelines.secrets import get_secret_store
from compass.pipelines.store import Edge, Node, Pipeline, PipelineRun
from compass.pipelines.types import NodeContext, NodeResult, get_registry

logger = logging.getLogger("compass.pipelines")

#: Terminal node states — a node in one of these will not run again.
_SETTLED = ("done", "failed", "skipped", "inactive")


def _edge_satisfied(edge: Edge, source: pstore.NodeRun) -> bool | None:
    """Whether `edge` may be followed, or None while its source is unsettled.

    `completion` follows either way, which is what makes a cleanup step
    expressible. A `skip` edge follows a source that was itself skipped or
    switched off, which is how a deactivated node still exercises the branch
    someone left hanging off it.
    """
    if source.status not in _SETTLED:
        return None
    outcome = source.status
    if source.status == "inactive":
        outcome = {"success": "done", "failure": "failed",
                   "skip": "skipped"}.get(source.port, "done")
    if edge.when == "completion":
        return True
    if edge.when == "success":
        return outcome == "done" and _port_matches(edge, source)
    if edge.when == "failure":
        return outcome == "failed"
    if edge.when == "skip":
        return outcome == "skipped"
    return False


def _port_matches(edge: Edge, source: pstore.NodeRun) -> bool:
    """A success edge only follows the port the handler actually chose.

    This is the whole of branching: an If returns "true" or "false" and the
    engine follows the matching edges, without knowing that If exists.
    """
    return edge.port == (source.port or "out")


class PipelineEngine:
    """Executes pipelines. One instance is enough; it holds no run state."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # -- public API ---------------------------------------------------------

    async def start(self, pipeline: Pipeline, *, trigger: str = "manual",
                    parameters: dict[str, Any] | None = None) -> PipelineRun:
        """Create a run and drive it as far as it will go."""
        settings = get_settings()
        if (trigger == "scheduled"
                and settings.pipelines.require_manual_first_run
                and not pipeline.proven_at):
            raise PermissionError(
                "This pipeline has not completed a manual run yet, so it "
                "cannot be scheduled. Run it once by hand first."
            )
        run = await pstore.runs.create(pipeline, trigger, parameters or {})
        # Nodes switched off never execute; they are settled up front so the
        # branches hanging off them still resolve.
        for node in pipeline.nodes:
            if node.state == "deactivated":
                node_run = run.nodes[node.id]
                node_run.status = "inactive"
                node_run.port = node.mark_as
                node_run.text = "Deactivated"
        await pstore.runs.save(run)
        return await self._drive(pipeline, run)

    async def resume(self, run_id: str, answer: dict[str, Any] | None = None
                     ) -> PipelineRun | None:
        """Continue a run that stopped on a waiting node."""
        run = await pstore.runs.get(run_id)
        if not run or run.status != "waiting":
            return run
        pipeline = await pstore.pipelines.get(run.pipeline_id)
        if not pipeline:
            return run
        for node_run in run.nodes.values():
            if node_run.status != "waiting":
                continue
            node_run.status = "done"
            node_run.output = {**node_run.output, "answer": answer or {}}
            node_run.finished_at = time.time()
            # An explicit rejection takes the failure branch, which is what
            # makes an approval meaningful rather than a speed bump.
            if str((answer or {}).get("choice", "")).lower() in ("reject", "no"):
                node_run.status = "failed"
                node_run.error = "Rejected"
        run.waiting_on = ""
        run.status = "running"
        await pstore.runs.save(run)
        return await self._drive(pipeline, run)

    async def cancel(self, run_id: str) -> PipelineRun | None:
        run = await pstore.runs.get(run_id)
        if not run or run.status in ("done", "failed", "cancelled"):
            return run
        run.status = "cancelled"
        run.finished_at = time.time()
        await pstore.runs.save(run)
        return run

    # -- the walk -----------------------------------------------------------

    async def _drive(self, pipeline: Pipeline, run: PipelineRun) -> PipelineRun:
        budget = get_settings().pipelines.max_node_runs
        executed = sum(1 for n in run.nodes.values() if n.status in _SETTLED)

        while True:
            # Before looking for work, not after. A resume can re-enter with
            # nothing runnable — a rejected approval settles the only path —
            # and propagating only after a batch would leave the unreachable
            # nodes sitting at "pending" forever, which reads in the run log
            # as though they might still happen.
            await self._propagate_skips(pipeline, run)
            ready = self._ready(pipeline, run)
            if not ready:
                break
            if executed >= budget:
                run.status = "failed"
                for node_run in run.nodes.values():
                    if node_run.status == "pending":
                        node_run.status = "skipped"
                        node_run.text = "Run exceeded its node budget"
                break

            # Independent ready nodes run together unless one says not to.
            parallel = [n for n in ready if self._type_safe(n)]
            serial = [n for n in ready if n not in parallel]
            if parallel:
                await asyncio.gather(
                    *(self._run_node(pipeline, run, n) for n in parallel)
                )
            for node in serial:
                await self._run_node(pipeline, run, node)
            executed += len(ready)
            await pstore.runs.save(run)

            if any(n.status == "waiting" for n in run.nodes.values()):
                run.status = "waiting"
                run.waiting_on = next(
                    (n.output.get("waiting_on", "") or f"node:{n.node_id}")
                    for n in run.nodes.values() if n.status == "waiting"
                )
                await pstore.runs.save(run)
                return run

        if run.status == "running":
            failed = any(n.status == "failed" for n in run.nodes.values())
            run.status = "failed" if failed else "done"
            run.finished_at = time.time()
            # A manual run that succeeded is what unlocks scheduling.
            if run.status == "done" and run.trigger == "manual":
                fresh = await pstore.pipelines.get(pipeline.id)
                if fresh and not fresh.proven_at:
                    fresh.proven_at = time.time()
                    await pstore.pipelines.save(fresh, bump=False)
        await pstore.runs.save(run)
        return run

    def _ready(self, pipeline: Pipeline, run: PipelineRun) -> list[Node]:
        """Every pending node whose incoming edges are all satisfied."""
        by_id = {n.id: n for n in pipeline.nodes}
        ready: list[Node] = []
        for node in pipeline.nodes:
            node_run = run.nodes.get(node.id)
            if not node_run or node_run.status != "pending":
                continue
            incoming = [e for e in pipeline.edges if e.target == node.id]
            if not incoming:
                ready.append(node)  # a source node
                continue
            verdicts = [
                _edge_satisfied(e, run.nodes[e.source])
                for e in incoming
                if e.source in run.nodes and e.source in by_id
            ]
            if not verdicts or any(v is None for v in verdicts):
                continue  # something upstream has not settled
            if any(v for v in verdicts):
                ready.append(node)
        return ready

    def _type_safe(self, node: Node) -> bool:
        node_type = get_registry().get(node.type)
        return bool(node_type and node_type.concurrency_safe)

    async def _propagate_skips(self, pipeline: Pipeline,
                               run: PipelineRun) -> None:
        """Mark unreachable nodes skipped, repeatedly until nothing changes.

        A node whose every incoming edge has settled against it can never run,
        and leaving it pending would stall the walk forever waiting for a
        turn that is not coming.
        """
        changed = True
        while changed:
            changed = False
            for node in pipeline.nodes:
                node_run = run.nodes.get(node.id)
                if not node_run or node_run.status != "pending":
                    continue
                incoming = [e for e in pipeline.edges if e.target == node.id]
                if not incoming:
                    continue
                verdicts = [
                    _edge_satisfied(e, run.nodes[e.source])
                    for e in incoming if e.source in run.nodes
                ]
                if verdicts and all(v is False for v in verdicts):
                    node_run.status = "skipped"
                    node_run.text = "Not reached"
                    node_run.finished_at = time.time()
                    changed = True

    # -- one node -----------------------------------------------------------

    async def _run_node(self, pipeline: Pipeline, run: PipelineRun,
                        node: Node) -> None:
        node_run = run.nodes[node.id]
        node_type = get_registry().get(node.type)
        node_run.started_at = time.time()

        if node_type is None:
            node_run.status = "failed"
            node_run.error = f"Unknown node type {node.type!r}"
            node_run.finished_at = time.time()
            return

        # Capability first, before anything is resolved or run.
        if node_type.requires and node_type.requires not in pipeline.capabilities:
            node_run.status = "failed"
            node_run.error = (
                f"This pipeline is not allowed to {node_type.requires}. "
                f"Grant it on the pipeline before running this node."
            )
            node_run.finished_at = time.time()
            return

        node_run.status = "running"

        resolver = Resolver(
            parameters=run.parameters,
            variables=run.variables,
            nodes={
                nid: {"data": nr.output, "text": nr.text}
                for nid, nr in run.nodes.items()
                if nr.status in ("done", "inactive")
            },
            run={"id": run.id, "trigger": run.trigger,
                 "started_at": run.started_at},
        )
        try:
            config = resolver.config(node.config)
        except ExpressionError as err:
            node_run.status = "failed"
            node_run.error = str(err)
            node_run.finished_at = time.time()
            return

        ctx = NodeContext(
            run_id=run.id,
            node_id=node.id,
            upstream={nid: nr.output for nid, nr in run.nodes.items()},
            parameters=run.parameters,
            variables=run.variables,
            connection=self._connection_resolver(node.connection_id),
            allowed=frozenset(pipeline.capabilities),
            workspace_root=str(get_settings().workspace_root),
        )

        attempts = max(1, node.retries + 1)
        for attempt in range(1, attempts + 1):
            node_run.attempts = attempt
            try:
                result: NodeResult = await asyncio.wait_for(
                    node_type.handler(config, ctx),
                    timeout=max(1, node.timeout_s),
                )
            except asyncio.TimeoutError:
                node_run.error = f"Timed out after {node.timeout_s}s"
            except Exception as err:  # noqa: BLE001 — recorded on the node
                node_run.error = str(err)[:2_000]
                logger.warning("node %s failed: %s", node.id, err)
            else:
                node_run.status = "waiting" if result.waiting_on else "done"
                node_run.output = (
                    {} if node.secure_output else dict(result.data)
                )
                if result.waiting_on:
                    node_run.output["waiting_on"] = result.waiting_on
                node_run.text = "" if node.secure_output else result.text[:8_000]
                node_run.port = result.port
                node_run.error = ""
                node_run.finished_at = time.time()
                return
            if attempt < attempts:
                await asyncio.sleep(max(0, node.retry_interval_s))

        node_run.status = "failed"
        node_run.finished_at = time.time()

    def _connection_resolver(self, connection_id: str):
        """Fetches a connection with its secret, at the moment it is used.

        A callable rather than a value so the credential never sits in the
        run's persisted state — which is the point of `secret_ref` existing.
        """
        async def _resolve(requested: str = "") -> dict[str, Any]:
            target = requested or connection_id
            if not target:
                return {}
            conn = await pstore.connections.get(target)
            if not conn:
                raise ValueError(f"no connection {target!r}")
            secret = ""
            if conn.secret_ref:
                secret = await get_secret_store().get(conn.secret_ref)
            return {"kind": conn.kind, "auth": conn.auth,
                    **conn.config, "secret": secret}

        return _resolve


engine = PipelineEngine()
