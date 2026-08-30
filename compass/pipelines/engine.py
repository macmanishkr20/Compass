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
from dataclasses import asdict
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


def loop_body(pipeline: Pipeline, loop_id: str) -> list[str]:
    """The nodes that belong inside a For each, in no particular order.

    Fabric makes ForEach a container and you drop activities into it. Compass
    infers the body from the wiring instead, because a container needs a
    canvas that can nest — dragging a node *into* a box — and that is a much
    larger piece of UI than a loop is worth right now.

    The rule: everything reachable from the loop's `each` port, minus anything
    reachable from its `out` port. The subtraction is what makes it
    unambiguous. `out` fires once, after the loop, so whatever hangs off it is
    "after", and a node reachable both ways is after — being downstream of the
    join wins over being downstream of the body.

    A node reachable from `each` but with no path back to the loop is still in
    the body; it simply runs once per item and contributes nothing to the
    join. That is the honest reading of what the author drew.
    """
    outgoing: dict[str, list[tuple[str, str]]] = {}
    for edge in pipeline.edges:
        outgoing.setdefault(edge.source, []).append((edge.target, edge.port))

    def reach(start_ports: set[str]) -> set[str]:
        seen: set[str] = set()
        stack = [t for t, p in outgoing.get(loop_id, ()) if p in start_ports]
        while stack:
            node_id = stack.pop()
            if node_id in seen or node_id == loop_id:
                continue
            seen.add(node_id)
            stack.extend(t for t, _ in outgoing.get(node_id, ()))
        return seen

    return sorted(reach({"each"}) - reach({"out"}))


class PipelineEngine:
    """Executes pipelines. One instance is enough; it holds no run state."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # -- public API ---------------------------------------------------------

    async def start(self, pipeline: Pipeline, *, trigger: str = "manual",
                    parameters: dict[str, Any] | None = None,
                    mode: str = "live",
                    only: str | None = None,
                    seed: dict[str, Any] | None = None) -> PipelineRun:
        """Create a run and drive it as far as it will go.

        `mode="mock"` returns pinned or stubbed data instead of calling
        anything, which is what lets a freshly built pipeline be shown working
        before an account is connected — the moment someone is least willing
        to authorize one, because they have not seen it work yet.

        `only=` runs a single node and stops, with `seed` standing in for what
        an upstream step would have produced. That is what a node's own
        "Execute step" needs, and it deliberately shares the whole path with a
        full run rather than being a second, simpler executor that would drift.
        """
        settings = get_settings()
        if mode not in ("live", "mock"):
            raise ValueError(f"unknown run mode {mode!r}")
        if (trigger == "scheduled"
                and settings.pipelines.require_manual_first_run
                and not pipeline.proven_at):
            raise PermissionError(
                "This pipeline has not completed a manual run yet, so it "
                "cannot be scheduled. Run it once by hand first."
            )
        # A mocked run proves nothing about credentials or the network, so it
        # must never be what marks a pipeline ready to schedule.
        run = await pstore.runs.create(pipeline, trigger, parameters or {},
                                       mode=mode)
        if only:
            return await self._one(pipeline, run, only, seed or {})
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

    async def _one(self, pipeline: Pipeline, run: PipelineRun,
                   node_id: str, seed: dict[str, Any]) -> PipelineRun:
        """Run a single node, with `seed` standing in for its upstream.

        Every other node is marked skipped rather than left pending, so the
        log does not imply the rest of the graph is about to run.
        """
        node = next((n for n in pipeline.nodes if n.id == node_id), None)
        if node is None:
            raise ValueError(f"no node {node_id!r}")
        for other in pipeline.nodes:
            if other.id != node_id:
                run.nodes[other.id].status = "skipped"
                run.nodes[other.id].text = "Not part of this step run"
        # The seed is presented as an upstream result under a reserved id, so
        # `@nodes('input').data.x` reaches it and the node needs no rewriting
        # to be run alone.
        run.nodes.setdefault("input", pstore.NodeRun(node_id="input"))
        run.nodes["input"].status = "done"
        run.nodes["input"].output = dict(seed)
        run.nodes["input"].text = "Supplied for this step run"
        await self._run_node(pipeline, run, node)
        run.status = "failed" if run.nodes[node_id].status == "failed" else "done"
        run.finished_at = time.time()
        await pstore.runs.save(run)
        return run

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
            # A manual run that succeeded is what unlocks scheduling — and it
            # has to be a live one. A mocked run calls nothing, so treating it
            # as proof would let a pipeline be scheduled on the strength of a
            # run that never touched the thing it is supposed to touch.
            if (run.status == "done" and run.trigger == "manual"
                    and run.mode == "live"):
                fresh = await pstore.pipelines.get(pipeline.id)
                if fresh and not fresh.proven_at:
                    fresh.proven_at = time.time()
                    await pstore.pipelines.save(fresh, bump=False)
        await pstore.runs.save(run)
        return run

    def _in_a_loop(self, pipeline: Pipeline) -> set[str]:
        """Every node that belongs to some loop's body.

        The outer walk must leave these alone: they are run by the loop, once
        per item, and a body node picked up by the outer walk would run an
        extra time with no item in scope.
        """
        owned: set[str] = set()
        for node in pipeline.nodes:
            node_type = get_registry().get(node.type)
            if node_type and any(p.name == "each" for p in node_type.outputs):
                owned.update(loop_body(pipeline, node.id))
        return owned

    def _ready(self, pipeline: Pipeline, run: PipelineRun) -> list[Node]:
        """Every pending node whose incoming edges are all satisfied."""
        by_id = {n.id: n for n in pipeline.nodes}
        inside = self._in_a_loop(pipeline)
        ready: list[Node] = []
        for node in pipeline.nodes:
            if node.id in inside:
                continue
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
        inside = self._in_a_loop(pipeline)
        changed = True
        while changed:
            changed = False
            for node in pipeline.nodes:
                if node.id in inside:
                    continue  # the loop settles its own body
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
                        node: Node, *,
                        states: dict[str, pstore.NodeRun] | None = None,
                        item: Any = None, index: int | None = None) -> None:
        """Run one node against a state map.

        `states` is the run's own for an ordinary node, or one iteration's for
        a node inside a loop body. Passing it rather than reading `run.nodes`
        is what keeps an iteration isolated: item three failing must not mark
        the node failed for items four and five.
        """
        scope = states if states is not None else run.nodes
        node_run = scope[node.id]
        node_type = get_registry().get(node.type)
        node_run.started_at = time.time()

        if node_type is None:
            node_run.status = "failed"
            node_run.error = f"Unknown node type {node.type!r}"
            node_run.finished_at = time.time()
            return

        # Capability first, before anything is resolved or run — except in a
        # mocked run, which calls nothing and so has nothing to be permitted.
        # Checking it there would make verifying a pipeline require the very
        # grants the verification exists to justify asking for.
        if (run.mode != "mock" and node_type.requires
                and node_type.requires not in pipeline.capabilities):
            node_run.status = "failed"
            node_run.error = (
                f"This pipeline is not allowed to {node_type.requires}. "
                f"Grant it on the pipeline before running this node."
            )
            node_run.finished_at = time.time()
            return

        node_run.status = "running"

        # A node inside a loop can name both its siblings in this iteration
        # and anything that finished before the loop began. Siblings are
        # merged second so an id that appears in both resolves to this
        # iteration's value, which is the one the author means.
        visible = {
            nid: {"data": nr.output, "text": nr.text}
            for nid, nr in run.nodes.items()
            if nr.status in ("done", "inactive")
        }
        if states is not None:
            visible.update({
                nid: {"data": nr.output, "text": nr.text}
                for nid, nr in states.items()
                if nr.status in ("done", "inactive")
            })
        resolver = Resolver(
            parameters=run.parameters,
            variables=run.variables,
            nodes=visible,
            run={"id": run.id, "trigger": run.trigger,
                 "started_at": run.started_at},
            item=item,
            index=index,
        )
        try:
            config = resolver.config(node.config)
        except ExpressionError as err:
            node_run.status = "failed"
            node_run.error = str(err)
            node_run.finished_at = time.time()
            return

        # Recorded before the handler runs, so a node that fails or times out
        # still shows what it was asked to do. Recording it afterwards would
        # lose exactly the case the pane is most wanted for.
        node_run.input = {} if node.secure_input else dict(config)

        # In mock mode the handler is never called. Substituting here rather
        # than inside each handler is what makes it true of every node type,
        # including ones contributed by a provider that knows nothing about
        # mocking — which is the only way "touches nothing outside" can be a
        # property of the run rather than a hope about its nodes.
        # Control flow runs for real even in a mocked run. An `if` that took
        # an arbitrary branch, or a loop fanning out over stubs, would make
        # the mocked run a different graph from the live one — and a
        # verification of a different graph verifies nothing.
        if run.mode == "mock" and node_type.category != "flow":
            mocked = self._mock_result(node, node_type)
            node_run.status = "done"
            node_run.output = {} if node.secure_output else dict(mocked.data)
            node_run.text = mocked.text
            node_run.port = mocked.port
            node_run.finished_at = time.time()
            if mocked.port == "each":
                await self._run_loop(pipeline, run, node, node_run, mocked)
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
                # A loop runs its body here rather than leaving the outer walk
                # to do it: only one place can schedule the copies, hold the
                # per-item state and join them, and splitting that across the
                # walk and the handler would put half the loop in each.
                if result.port == "each":
                    await self._run_loop(pipeline, run, node, node_run, result)
                return
            if attempt < attempts:
                await asyncio.sleep(max(0, node.retry_interval_s))

        node_run.status = "failed"
        node_run.finished_at = time.time()

    def _mock_result(self, node: Node, node_type: Any) -> NodeResult:
        """What a node returns when the run is not allowed to touch anything.

        Pinned data first, because a person who took the trouble to paste a
        real payload wants exactly that. Otherwise a stub shaped by the node
        type's own output port: an empty object would verify the wiring and
        nothing else, and the difference between "four nodes ran" and "four
        nodes ran and produced plausible data" is most of the value of showing
        someone a mocked run at all.

        Control flow is never mocked. An `if` that always took the true branch
        or a loop that fanned out over a stub would make a mocked run a
        different graph from the live one, which defeats the point of running
        it — so flow nodes execute for real, and only the leaves are stubbed.
        """
        if node.mock is not None:
            return NodeResult(data=dict(node.mock), text="Pinned data",
                              port="out")
        kind = ""
        if node_type is not None and node_type.outputs:
            kind = node_type.outputs[0].kind
        label = node.name or node.type
        if kind == "items":
            return NodeResult(
                data={"items": [{"id": "mock-1"}, {"id": "mock-2"}],
                      "count": 2},
                text=f"{label}: two stub item(s)")
        if kind == "text":
            return NodeResult(data={"text": f"Mock output from {label}"},
                              text=f"{label}: stub text")
        return NodeResult(data={"mock": True, "from": node.id},
                          text=f"{label}: stub result")

    # -- fan-out --------------------------------------------------------------

    async def _run_loop(self, pipeline: Pipeline, run: PipelineRun,
                        node: Node, node_run: pstore.NodeRun,
                        result: NodeResult) -> None:
        """Run the loop's body once per item, then hand the results to `out`.

        Iterations are sequential. Running them at once would be faster and is
        the wrong default here: a body typically contains exactly the nodes
        that are not safe to parallelise — a shell command, a file write, an
        agent turn — and a loop over fifty messages firing fifty agent turns
        at a deployment with a per-minute token quota is a way to turn one
        mistake into a rate limit. Parallelism belongs on the loop as a
        setting, once there is a reason to want it.

        The body is isolated per item. Each iteration gets its own node
        states, so a node that fails on item three does not mark itself failed
        for items four and five, and `@item()` resolves to that iteration's
        item rather than to whatever ran last.
        """
        body = loop_body(pipeline, node.id)
        items = list(result.data.get("items") or [])
        if not body:
            node_run.text += " (nothing is wired to the 'each' port)"
            node_run.output = {**node_run.output, "results": []}
            node_run.port = "out"
            return

        by_id = {n.id: n for n in pipeline.nodes}
        scoped_edges = [
            e for e in pipeline.edges
            if e.source in body and e.target in body
        ]
        entry = [
            e.target for e in pipeline.edges
            if e.source == node.id and e.port == "each" and e.target in body
        ]

        iterations: list[dict[str, Any]] = []
        results: list[Any] = []
        failures = 0

        for index, item in enumerate(items):
            states: dict[str, pstore.NodeRun] = {
                nid: pstore.NodeRun(node_id=nid) for nid in body
            }
            await self._walk_body(
                pipeline, run, by_id, body, scoped_edges, entry, states,
                item=item, index=index,
            )
            iterations.append({nid: asdict(nr) for nid, nr in states.items()})
            if any(nr.status == "failed" for nr in states.values()):
                failures += 1
            # The join carries what the body's leaves produced — the nodes
            # nothing else in the body depends on, which is what "the result
            # of this iteration" means without asking the author to nominate
            # one.
            leaves = [
                nid for nid in body
                if not any(e.source == nid for e in scoped_edges)
            ]
            results.append({nid: states[nid].output for nid in leaves})

        run.iterations[node.id] = iterations
        self._summarise_body(run, body, iterations, len(items))
        node_run.output = {
            **node_run.output,
            "results": results,
            "failed_items": failures,
        }
        node_run.text = (
            f"Ran {len(items)} item(s)"
            + (f", {failures} failed" if failures else "")
        )
        # Whatever happened inside, the loop itself is done and the graph
        # continues past it. A body failure is visible on the loop and in the
        # iteration list; making the loop fail would strand every "after the
        # loop" step on one bad item, which is rarely what anyone wants.
        node_run.port = "out"

    def _summarise_body(self, run: PipelineRun, body: list[str],
                        iterations: list[dict[str, Any]], total: int) -> None:
        """Roll each body node's iterations up into its one canvas box.

        The canvas draws one box per node, not one per item, so a body node
        needs a single status. Failure wins over success: a node that failed
        on any item is worth showing as failed, because the alternative is a
        green graph with a failure hidden one click away.
        """
        for nid in body:
            states = [it.get(nid, {}) for it in iterations]
            statuses = [s.get("status", "pending") for s in states]
            summary = run.nodes.get(nid)
            if summary is None:
                continue
            failed = sum(1 for s in statuses if s == "failed")
            done = sum(1 for s in statuses if s == "done")
            summary.status = (
                "failed" if failed else "done" if done else "skipped"
            )
            summary.text = (
                f"{done}/{total} item(s)"
                + (f", {failed} failed" if failed else "")
            )
            summary.error = next(
                (s.get("error", "") for s in states if s.get("error")), ""
            )
            summary.finished_at = time.time()

    async def _walk_body(self, pipeline: Pipeline, run: PipelineRun,
                         by_id: dict[str, Node], body: list[str],
                         edges: list[Edge], entry: list[str],
                         states: dict[str, pstore.NodeRun],
                         *, item: Any, index: int) -> None:
        """One iteration: the same ready-set walk, over the body only."""
        budget = len(body) + 1
        while budget > 0:
            budget -= 1
            ready: list[Node] = []
            for nid in body:
                state = states[nid]
                if state.status != "pending":
                    continue
                incoming = [e for e in edges if e.target == nid]
                if not incoming:
                    # An entry node has its edge from the loop itself, which
                    # is outside the body and always satisfied by the time we
                    # are here.
                    if nid in entry:
                        ready.append(by_id[nid])
                    continue
                verdicts = [_edge_satisfied(e, states[e.source]) for e in incoming]
                if any(v is None for v in verdicts):
                    continue
                if any(verdicts):
                    ready.append(by_id[nid])
                elif all(v is False for v in verdicts):
                    state.status = "skipped"
                    state.text = "Not reached"
            if not ready:
                break
            for node in ready:
                await self._run_node(
                    pipeline, run, node, states=states, item=item, index=index,
                )

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
