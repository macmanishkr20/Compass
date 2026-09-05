"""The builder's hands: tools that edit one pipeline.

Every tool here is bound to a single pipeline at construction, so the model
never names which graph it is editing and cannot reach another one. That is
the whole of the isolation: the builder for pipeline A holds tools that only
write pipeline A.

Two rules shape the set, and both are about avoiding a class of bug rather
than about convenience.

*Edits go through the store, the same way the canvas writes.* The graph is
never reconstructed from what the model said — it is changed by the same
operations a person's click performs. That is the difference between a canvas
that updates as you watch and one that shows a plan you then have to apply,
and it means a model edit and a human edit cannot diverge.

*The model picks from the catalogue and reads a schema before writing
settings.* `node_types` and `describe_node_type` exist so it does not guess.
A guessed field name produces a graph that validates in the model's head and
fails on the canvas, and the failure surfaces far from the cause.

The tools are deliberately small and close to the REST surface. Anything
clever — "build me the whole thing" — belongs in the prompt, not in a tool
that hides a dozen edits behind one call nobody can review.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from compass.common.ownership import owned
from compass.common.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield
from compass.pipelines import store as pstore
from compass.pipelines.store import Edge, Node
from compass.pipelines.types import get_registry

logger = logging.getLogger("compass.pipelines")

#: How many node types to describe in one search result. Past this the answer
#: stops being a menu and becomes a wall, and the model gets worse at picking
#: from it — the same recall problem the tool shelf exists for.
MAX_MATCHES = 25


class _Bound(Tool):
    """A tool that knows which pipeline it edits."""

    def __init__(self, pipeline_id: str) -> None:
        self.pipeline_id = pipeline_id

    async def _pipeline(self):
        pipeline = await pstore.pipelines.get(self.pipeline_id)
        if not pipeline:
            raise ValueError(f"pipeline {self.pipeline_id} is gone")
        return pipeline

    def is_read_only(self, inp: BaseModel) -> bool:
        return False

    def is_concurrency_safe(self, inp: BaseModel) -> bool:
        # Two edits to one graph at once would race on the stored version.
        return False

    def check_tool_permissions(self, inp: BaseModel, ctx: ToolUseContext):
        # Editing the graph someone opened the builder on needs no gate; what
        # the graph is *allowed to do* when it runs is the capability set,
        # which the engine checks and this cannot touch.
        return None


# --------------------------------------------------------------------------- reading


class SearchInput(BaseModel):
    search: str = Field(
        default="",
        description="Words to match against a node type's name and "
                    "description. Empty lists everything, grouped by kind.",
    )


class NodeTypesTool(_Bound):
    name = "pipeline_node_types"
    description = (
        "List the node types this pipeline can use. Always look here before "
        "adding a node: the catalogue is what exists, and a type you invent "
        "will fail. Search by what you want to do — 'email', 'github', "
        "'loop', 'shell'."
    )
    input_model = SearchInput

    def is_read_only(self, inp: SearchInput) -> bool:
        return True

    def is_concurrency_safe(self, inp: SearchInput) -> bool:
        return True

    async def call(self, inp: SearchInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        everything = sorted(get_registry().all().values(),
                            key=lambda t: (t.category, t.id))
        # Words, not one substring. "loop over items" has to reach `For each`,
        # and a single substring match cannot: the phrase appears nowhere.
        # Measured before this — searching "loop" returned nothing, and the
        # builder spent fifteen calls hunting synonyms and never built.
        words = [w for w in inp.search.strip().lower().split()
                 if w and w not in _FUNCTION_WORDS]
        # Ranked by how many of the words appear, not gated on all of them.
        # Requiring every word means one plural sinks a good query: "loop over
        # items" missed `For each`, whose description says "once per item".
        # Scoring keeps an exact phrase at the top without throwing away the
        # near miss underneath it.
        if words:
            scored = [(sum(_word_score(w, t) for w in words), t)
                      for t in everything]
            found = [t for score, t in
                     sorted((s for s in scored if s[0]),
                            key=lambda s: (-s[0], s[1].category, s[1].id))]
        else:
            found = everything

        if words and not found:
            # A dead end is what caused the thrash, so there is no dead end:
            # a search that matches nothing returns the whole catalogue and
            # says so. Better to read thirty lines than to guess again.
            yield ToolOutput("\n".join(
                [f"Nothing matches {inp.search!r}. The whole catalogue, so "
                 "you can pick rather than guess again:"]
                + [_line(t) for t in everything]))
            return

        lines = [f"{len(found)} node type(s)"
                 + (f" matching {inp.search!r}" if words else "")]
        lines += [_line(t) for t in found[:MAX_MATCHES]]
        if len(found) > MAX_MATCHES:
            lines.append(f"  … and {len(found) - MAX_MATCHES} more; search to "
                         "narrow it")
        yield ToolOutput("\n".join(lines))


class DescribeInput(BaseModel):
    type_id: str = Field(description="A node type id, e.g. 'flow.foreach'.")


class DescribeTypeTool(_Bound):
    name = "pipeline_describe_node_type"
    description = (
        "The settings schema for one node type. Read this before setting a "
        "node's config, so field names come from the schema rather than from "
        "a guess — a guessed name produces a graph that looks right and fails "
        "when it runs."
    )
    input_model = DescribeInput

    def is_read_only(self, inp: DescribeInput) -> bool:
        return True

    def is_concurrency_safe(self, inp: DescribeInput) -> bool:
        return True

    async def call(self, inp: DescribeInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        node_type = get_registry().get(inp.type_id.strip())
        if node_type is None:
            yield ToolOutput(f"No node type {inp.type_id!r}. Use "
                             "pipeline_node_types to see what exists.",
                             is_error=True)
            return
        summary = node_type.summary()
        yield ToolOutput(json.dumps({
            "id": summary["id"],
            "label": summary["label"],
            "description": summary["description"],
            "config_schema": summary["config_schema"],
            "outputs": summary["outputs"],
            "connection_kind": summary["connection_kind"],
            "requires": summary["requires"],
        }, indent=2))


class ReadInput(BaseModel):
    pass


class ReadGraphTool(_Bound):
    name = "pipeline_read"
    description = (
        "The pipeline as it stands: its nodes, how they are wired, and what "
        "it is allowed to do. Read before editing an existing graph so you "
        "change what is there rather than describing something else."
    )
    input_model = ReadInput

    def is_read_only(self, inp: ReadInput) -> bool:
        return True

    def is_concurrency_safe(self, inp: ReadInput) -> bool:
        return True

    async def call(self, inp: ReadInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        pipeline = await self._pipeline()
        lines = [f"{pipeline.name} — {len(pipeline.nodes)} node(s), "
                 f"{len(pipeline.edges)} edge(s), v{pipeline.version}",
                 f"capabilities: {', '.join(pipeline.capabilities) or 'none'}"]
        for node in pipeline.nodes:
            bits = [f"  {node.id}: {node.type}"]
            if node.name:
                bits.append(f'"{node.name}"')
            if node.config:
                bits.append(json.dumps(node.config))
            if node.connection_id:
                bits.append(f"connection={node.connection_id}")
            lines.append(" ".join(bits))
        for edge in pipeline.edges:
            tag = "" if edge.when == "success" else f" [{edge.when}]"
            port = "" if edge.port == "out" else f" .{edge.port}"
            lines.append(f"  {edge.source}{port} -> {edge.target}{tag}")
        yield ToolOutput("\n".join(lines))


# --------------------------------------------------------------------------- writing


class AddInput(BaseModel):
    type_id: str = Field(description="A node type id from the catalogue.")
    name: str = Field(default="", description="What this step is for, in a "
                                              "few words. Shown on the node.")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Settings, matching the type's schema. May hold "
                    "expressions such as @nodes('other').data.field.",
    )
    connection_id: str = Field(
        default="", description="For a node type that needs a connection.")


class AddNodeTool(_Bound):
    name = "pipeline_add_node"
    description = (
        "Add a node and return the id it was given. Lay a pipeline out left "
        "to right in the order it runs; positions are assigned for you."
    )
    input_model = AddInput

    async def call(self, inp: AddInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        node_type = get_registry().get(inp.type_id.strip())
        if node_type is None:
            yield ToolOutput(
                f"No node type {inp.type_id!r}. Use pipeline_node_types "
                "first — inventing a type produces a graph that cannot run.",
                is_error=True)
            return
        pipeline = await self._pipeline()

        # Validate against the type's own schema now rather than at run time,
        # so the model is corrected while it still remembers what it meant.
        if problem := _schema_problem(node_type, inp.config):
            yield ToolOutput(problem, is_error=True)
            return

        right = max((n.position.get("x", 0) for n in pipeline.nodes), default=-70)
        column = right + 260
        stacked = sum(1 for n in pipeline.nodes
                      if n.position.get("x") == column)
        node = Node(
            id=f"n_{len(pipeline.nodes) + 1}_{abs(hash(inp.type_id)) % 997:03d}",
            type=inp.type_id.strip(),
            name=inp.name.strip() or node_type.label,
            config=inp.config,
            connection_id=inp.connection_id.strip(),
            position={"x": column, "y": 40 + stacked * 96},
        )
        pipeline.nodes.append(node)
        await pstore.pipelines.save(pipeline)
        note = ""
        if node_type.requires and node_type.requires not in pipeline.capabilities:
            note = (f" This node needs the '{node_type.requires}' capability, "
                    "which the pipeline does not hold — say so rather than "
                    "granting it yourself.")
        yield ToolOutput(f"Added {node.id} ({node.type}).{note}")


class ConnectInput(BaseModel):
    source: str = Field(description="Node id the arrow leaves.")
    target: str = Field(description="Node id the arrow enters.")
    when: str = Field(
        default="success",
        description="Which outcome the arrow follows: success, failure, "
                    "completion or skip. Use failure for error handling — it "
                    "is what an arrow already is, not a separate mechanism.",
    )
    port: str = Field(
        default="out",
        description="Which output it leaves from. 'each' on a For each is the "
                    "loop body; 'false' on an If is the other branch.",
    )


class ConnectTool(_Bound):
    name = "pipeline_connect"
    description = "Wire one node to another."
    input_model = ConnectInput

    async def call(self, inp: ConnectInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        pipeline = await self._pipeline()
        ids = {n.id for n in pipeline.nodes}
        missing = [i for i in (inp.source, inp.target) if i not in ids]
        if missing:
            yield ToolOutput(f"No node {', '.join(missing)}. Existing: "
                             f"{', '.join(sorted(ids)) or 'none'}",
                             is_error=True)
            return
        if inp.when not in pstore.EDGE_CONDITIONS:
            yield ToolOutput(
                f"'{inp.when}' is not an edge condition. Use one of: "
                f"{', '.join(pstore.EDGE_CONDITIONS)}.", is_error=True)
            return
        edge = Edge(source=inp.source, target=inp.target,
                    when=inp.when, port=inp.port)
        if any(e.source == edge.source and e.target == edge.target
               and e.port == edge.port and e.when == edge.when
               for e in pipeline.edges):
            yield ToolOutput("That wire is already there.")
            return
        pipeline.edges.append(edge)
        await pstore.pipelines.save(pipeline)
        yield ToolOutput(f"Wired {inp.source} -> {inp.target} on {inp.when}.")


class ConfigInput(BaseModel):
    node_id: str
    config: dict[str, Any] = Field(
        description="Fields to set. Merged with what is there, so send only "
                    "what changes.")
    name: str = Field(default="", description="Rename the step, optionally.")


class SetConfigTool(_Bound):
    name = "pipeline_set_config"
    description = (
        "Change a node's settings. Merged, so send only the fields that "
        "change. Read the type's schema first if you are unsure of a name."
    )
    input_model = ConfigInput

    async def call(self, inp: ConfigInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        pipeline = await self._pipeline()
        node = next((n for n in pipeline.nodes if n.id == inp.node_id), None)
        if node is None:
            yield ToolOutput(f"No node {inp.node_id!r}.", is_error=True)
            return
        node_type = get_registry().get(node.type)
        merged = {**node.config, **inp.config}
        if node_type and (problem := _schema_problem(node_type, merged)):
            yield ToolOutput(problem, is_error=True)
            return
        node.config = merged
        if inp.name.strip():
            node.name = inp.name.strip()
        await pstore.pipelines.save(pipeline)
        yield ToolOutput(f"Set {inp.node_id}: {json.dumps(inp.config)}")


class RemoveInput(BaseModel):
    node_id: str


class RemoveNodeTool(_Bound):
    name = "pipeline_remove_node"
    description = ("Delete a node and every wire touching it.")
    input_model = RemoveInput

    async def call(self, inp: RemoveInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        pipeline = await self._pipeline()
        before = len(pipeline.nodes)
        pipeline.nodes = [n for n in pipeline.nodes if n.id != inp.node_id]
        if len(pipeline.nodes) == before:
            yield ToolOutput(f"No node {inp.node_id!r}.", is_error=True)
            return
        # Edges to a node that no longer exists would only surface as
        # validation problems later, so they go with it.
        pipeline.edges = [e for e in pipeline.edges
                          if e.source != inp.node_id and e.target != inp.node_id]
        await pstore.pipelines.save(pipeline)
        yield ToolOutput(f"Removed {inp.node_id} and its wires.")


# --------------------------------------------------------------------------- checking


class NoInput(BaseModel):
    pass


class ValidateTool(_Bound):
    name = "pipeline_validate"
    description = (
        "What is wrong with the graph as it stands. Run this after building "
        "and fix what it reports before telling anyone you are done."
    )
    input_model = NoInput

    def is_read_only(self, inp: NoInput) -> bool:
        return True

    async def call(self, inp: NoInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        from compass.pipelines.routes import validate_pipeline

        result = await validate_pipeline(self.pipeline_id, user="builder")
        problems = result.get("problems") or []
        if not problems:
            yield ToolOutput("No problems.")
            return
        lines = [f"{len(problems)} problem(s):"]
        lines += [f"  {p['node'] or 'graph'}: {p['problem']}" for p in problems]
        yield ToolOutput("\n".join(lines))


class DryRunTool(_Bound):
    name = "pipeline_dry_run"
    description = (
        "Run the pipeline with pinned or stubbed data. Calls nothing outside "
        "and needs no credentials, so it is how you check a graph you just "
        "built actually flows end to end. Do this before saying it is ready."
    )
    input_model = NoInput

    async def call(self, inp: NoInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        from compass.pipelines.engine import engine

        pipeline = await self._pipeline()
        run = await engine.start(pipeline, mode="mock")
        lines = [f"Dry run: {run.status}"]
        for node in pipeline.nodes:
            node_run = run.nodes.get(node.id)
            if not node_run:
                continue
            detail = node_run.error or node_run.text
            lines.append(f"  {node.id} ({node.name or node.type}): "
                         f"{node_run.status}"
                         + (f" — {detail[:160]}" if detail else ""))
        yield ToolOutput("\n".join(lines),
                         is_error=run.status == "failed")


class NeededConnectionsTool(_Bound):
    name = "pipeline_needed_connections"
    description = (
        "Which connections this graph needs before it can run for real, and "
        "which of them exist. Use it to tell the person what is left to set "
        "up rather than guessing."
    )
    input_model = NoInput

    def is_read_only(self, inp: NoInput) -> bool:
        return True

    async def call(self, inp: NoInput, ctx: ToolUseContext
                   ) -> AsyncIterator[ToolYield]:
        pipeline = await self._pipeline()
        registry = get_registry()
        have = {c.kind for c in owned(await pstore.connections.list(), pipeline.owner)}
        needed: dict[str, list[str]] = {}
        for node in pipeline.nodes:
            node_type = registry.get(node.type)
            if node_type and node_type.connection_kind:
                needed.setdefault(node_type.connection_kind, []).append(node.id)
        if not needed:
            yield ToolOutput("This pipeline needs no connections.")
            return
        lines = []
        for kind, nodes in sorted(needed.items()):
            state = "one exists" if kind in have else "none yet"
            lines.append(f"  {kind}: {state} — used by {', '.join(nodes)}")
        caps = [c for c in ("shell", "write", "network", "agent")
                if any((registry.get(n.type) or None) and
                       getattr(registry.get(n.type), "requires", "") == c
                       for n in pipeline.nodes)
                and c not in pipeline.capabilities]
        if caps:
            lines.append("  capabilities not yet granted: " + ", ".join(caps))
        yield ToolOutput("\n".join(["Connections this pipeline needs:"] + lines))


# --------------------------------------------------------------------------- helpers


#: Dropped from a search, because requiring every word means one preposition
#: sinks a reasonable query — "loop over items" found nothing while "loop"
#: found the right node. Only function words: anything that could name a step
#: stays, including "each".
_FUNCTION_WORDS = frozenset(
    "a an and the of for to in on over with from into at by as is are it this "
    "that then when node step".split()
)

#: Words a node type is also known by. Only where the label and description
#: genuinely miss the word someone would reach for: nothing in "For each /
#: Run everything downstream once per item" contains "loop", and that gap cost
#: a whole build. Kept small on purpose — a synonym table that tries to cover
#: every phrasing becomes its own thing to maintain and get wrong.
_ALSO_KNOWN_AS: dict[str, str] = {
    "flow.foreach": "loop iterate repeat each every batch",
    "flow.if": "branch condition conditional decide",
    "flow.filter": "where select narrow",
    "flow.wait": "delay pause sleep",
    "flow.approval": "human review approve confirm pause",
    "flow.set_variable": "store remember assign",
    "connector.http": "api rest request webhook url call",
    "tool.file_read": "open load read",
    "tool.file_write": "save write output",
    "tool.bash": "shell command run script terminal",
    "tool.agent": "llm model ai reason summarise summarize",
    "tool.consult": "llm model ai advice",
    "tool.web_fetch": "download url page scrape",
}


def _haystack(node_type: Any) -> str:
    return " ".join([
        node_type.id, node_type.label, node_type.description,
        node_type.category, _ALSO_KNOWN_AS.get(node_type.id, ""),
    ]).lower()


def _name_of(node_type: Any) -> str:
    """What this node is called, as opposed to what it says about itself."""
    return " ".join([node_type.id, node_type.label,
                     _ALSO_KNOWN_AS.get(node_type.id, "")]).lower()


def _word_score(word: str, node_type: Any) -> int:
    """A word in the name counts for more than a word in the prose.

    Without the weighting, `For each` lost the search for "loop" to nodes
    whose descriptions happen to mention items — one of which was a GitHub
    issue lister, purely because its blurb explains that its results arrive
    on `items`. A name is what a node *is*; a description is what it happens
    to talk about.
    """
    if word in _name_of(node_type):
        return 3
    return 1 if word in _haystack(node_type) else 0


def _line(node_type: Any) -> str:
    extra = []
    if node_type.connection_kind:
        extra.append(f"needs a {node_type.connection_kind} connection")
    if node_type.requires:
        extra.append(f"needs the '{node_type.requires}' capability")
    suffix = f" ({'; '.join(extra)})" if extra else ""
    return f"  {node_type.id} — {node_type.label}{suffix}"


def _schema_problem(node_type: Any, config: dict[str, Any]) -> str:
    """A readable complaint about settings, or "" when they are fine.

    Only unknown keys and missing required ones are checked. Full JSON Schema
    validation would reject expressions — `@nodes('x').data.n` is a string
    where the schema says integer — and those are exactly what a pipeline is
    made of, so the type of a field cannot be enforced here.
    """
    schema = node_type.config_schema or {}
    properties = schema.get("properties") or {}
    if not properties:
        return ""
    unknown = [k for k in config if k not in properties]
    if unknown:
        return (f"{node_type.id} has no setting(s) named "
                f"{', '.join(sorted(unknown))}. It takes: "
                f"{', '.join(sorted(properties))}.")
    missing = [k for k in (schema.get("required") or []) if k not in config]
    if missing:
        return (f"{node_type.id} needs {', '.join(sorted(missing))}, which "
                "is not set.")
    return ""


def builder_tools(pipeline_id: str) -> list[Tool]:
    """Every tool the builder gets, bound to one pipeline."""
    return [
        NodeTypesTool(pipeline_id),
        DescribeTypeTool(pipeline_id),
        ReadGraphTool(pipeline_id),
        AddNodeTool(pipeline_id),
        ConnectTool(pipeline_id),
        SetConfigTool(pipeline_id),
        RemoveNodeTool(pipeline_id),
        ValidateTool(pipeline_id),
        DryRunTool(pipeline_id),
        NeededConnectionsTool(pipeline_id),
    ]
