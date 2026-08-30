"""What a node type has to declare, and where node types come from.

This is the extension point the whole module rests on. A node type is a
declaration, not a class to subclass: an id, a label, a JSON Schema for its
settings, its ports, and an async handler. Anything that can produce that
declaration can add nodes — and nothing else in the module needs to know what
the node actually does.

The reason for the indirection is the catalogue. Compass's own tools are one
source of node types; MCP servers are another and are unbounded; connectors,
control flow and the other modules are three more. If node types were
hard-coded, adding a Gmail connector would mean editing the engine. With a
provider they are registered, and the engine keeps not knowing.

Two deliberate choices worth naming.

`config_schema` is JSON Schema rather than a pydantic model, even though most
providers will generate it from one. The settings pane renders from the
schema, and a node type that arrives from an MCP server at runtime has only
a schema to offer — so the schema is the contract, and pydantic is an
implementation detail of the providers that happen to use it.

Ports are typed and named. The canvas needs to refuse a connection that could
never work, and control flow needs somewhere to put its second output: a
ForEach yields on `each`, an If on `true` and `false`. Without named ports,
branching has to be encoded in the edge, which puts knowledge of specific node
types back into the engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger("compass.pipelines")

#: Port payload kinds. Deliberately coarse — the point is to catch wiring that
#: is obviously wrong (a file where a number is wanted), not to build a type
#: system. `any` opts a port out, which is the honest description of a shell
#: command's output.
PORT_KINDS = ("any", "text", "json", "items", "file", "signal")


@dataclass(frozen=True)
class Port:
    """One socket on a node."""

    name: str = "out"
    kind: str = "any"
    label: str = ""

    def accepts(self, other: "Port") -> bool:
        """Whether `other`'s payload can flow into this port."""
        return "any" in (self.kind, other.kind) or self.kind == other.kind


@dataclass
class NodeResult:
    """What a handler returns.

    `data` is the structured half and is what downstream nodes read; `text` is
    the human half, shown in the run log. Both, because a node that greps a
    file has a useful string and no useful object, and a node that lists
    messages has the reverse.

    `port` lets a handler choose which way the run continues — an If returns
    "true" or "false" — without the engine knowing which node types branch.
    """

    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    port: str = "out"
    #: Set by a node that cannot finish now and expects to be resumed — an
    #: approval, a webhook callback. The run is persisted and stops; the
    #: token is what the resume endpoint quotes back.
    waiting_on: str = ""


@dataclass
class NodeContext:
    """What a handler is given besides its own settings.

    Everything a node needs to reach the outside world arrives here rather
    than being imported, so a handler is testable and so the engine keeps a
    single place to decide what a run is allowed to do.
    """

    run_id: str
    node_id: str
    #: Resolved upstream outputs, keyed by node id — the data half of every
    #: node that has already finished.
    upstream: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The run's parameters (fixed) and variables (mutable).
    parameters: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    #: Resolves a connection id to its config with the secret filled in.
    #: A callable rather than the value, so a credential is fetched at the
    #: moment it is used and never sits in the run's state.
    connection: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    #: What this pipeline was granted at edit time. Handlers that do something
    #: powerful check it; the engine checks it too, before calling them.
    allowed: frozenset[str] = frozenset()
    workspace_root: str = ""


Handler = Callable[[dict[str, Any], NodeContext], Awaitable[NodeResult]]


@dataclass(frozen=True)
class NodeType:
    """A kind of node the canvas can offer and the engine can run."""

    id: str
    label: str
    category: str  # connector | intelligence | code | flow | module | tool | io
    handler: Handler
    description: str = ""
    icon: str = ""
    #: JSON Schema for this node's settings. The properties pane renders from
    #: it, and the engine validates against it before a run starts.
    config_schema: dict[str, Any] = field(default_factory=dict)
    #: The kind of connection this node needs, if any ("gmail", "rest", …).
    #: Empty means it needs none.
    connection_kind: str = ""
    inputs: tuple[Port, ...] = (Port("in"),)
    outputs: tuple[Port, ...] = (Port("out"),)
    #: Capability this node requires the pipeline to hold — "shell", "write",
    #: "network". Checked before the handler runs. Empty means harmless.
    requires: str = ""
    #: False for anything that must not run beside a sibling. Mirrors the
    #: tool contract's own predicate, which is where most of these come from.
    concurrency_safe: bool = True

    def summary(self) -> dict[str, Any]:
        """The palette/inspector view of this type, for the API."""
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "icon": self.icon,
            "config_schema": self.config_schema,
            "connection_kind": self.connection_kind,
            "inputs": [{"name": p.name, "kind": p.kind, "label": p.label}
                       for p in self.inputs],
            "outputs": [{"name": p.name, "kind": p.kind, "label": p.label}
                        for p in self.outputs],
            "requires": self.requires,
        }


class Provider(Protocol):
    """Anything that contributes node types."""

    def __call__(self) -> list[NodeType]: ...


class Registry:
    """Every node type currently available, gathered from its providers.

    Rebuilt on demand rather than cached at import, because two of the
    providers are live: MCP servers connect and disconnect, and a connector
    appears when someone adds a connection. A palette that needs a restart to
    show a node the user just connected is a palette that lies.
    """

    def __init__(self) -> None:
        self._providers: list[Provider] = []

    def add_provider(self, provider: Provider) -> None:
        self._providers.append(provider)

    def all(self) -> dict[str, NodeType]:
        found: dict[str, NodeType] = {}
        for provider in self._providers:
            try:
                types = provider()
            except Exception:  # noqa: BLE001 — one bad provider must not
                # empty the palette; a connector whose server is down should
                # cost its own nodes and nothing else.
                logger.warning("node type provider failed", exc_info=True)
                continue
            for node_type in types:
                if node_type.id in found:
                    logger.warning(
                        "duplicate node type %s; keeping the first",
                        node_type.id,
                    )
                    continue
                found[node_type.id] = node_type
        return found

    def get(self, type_id: str) -> NodeType | None:
        return self.all().get(type_id)


_registry: Registry | None = None


def get_registry() -> Registry:
    """The process-wide registry, with the built-in providers attached."""
    global _registry
    if _registry is not None:
        return _registry
    registry = Registry()
    # Imported here rather than at module scope: `nodes` imports from this
    # file, and doing it at the top would be a cycle.
    from compass.pipelines.nodes import builtin_providers

    for provider in builtin_providers():
        registry.add_provider(provider)
    _registry = registry
    return registry


def reset_registry() -> None:
    """Forget the registry. For tests, and after connections change."""
    global _registry
    _registry = None
