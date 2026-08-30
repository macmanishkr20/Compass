"""Taking a pipeline out of Compass: a diagram, a design, and working code.

Scheduling is one way to use a pipeline and not the only one, and for a lot
of them it is not the interesting one. A pipeline drawn here is a design, and
the design is worth more if it can leave — dropped into another application as
a library, or handed to someone as an architecture they can implement.

So this produces three things from one graph.

A *diagram* — Mermaid, because it renders in the places a design actually gets
read: a README, a wiki, a pull request, a Compass artifact. Edge conditions
and loop bodies are drawn, since those are the parts a reader cannot infer.

An *architecture note* — what the pipeline does, step by step, what it needs
from its host, and what could not be exported. That last section is the one
that matters: it is the honest list of what the receiving application has to
supply before this runs.

A *Python package* — installable, dependency-free, with a `Pipeline` class you
can `run()`. Control flow is exported complete. Anything that needed Compass's
workspace, its model gateway or a stored credential becomes a named
registration point rather than a silent gap.

The runtime is a second implementation of the walk rather than a shared one,
which is a real cost and the right trade: Compass's engine is async, persists
every step, resolves secrets and can park for hours, and none of that belongs
in a library dropped into someone else's application. `scripts/check_export.py`
runs both against the same graph to prove they still agree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from pprint import pformat
from typing import Any

from compass.pipelines.engine import loop_body
from compass.pipelines.store import Pipeline
from compass.pipelines.types import get_registry

TEMPLATES = Path(__file__).parent / "templates"

#: Node types the exported package implements itself. Everything else becomes
#: a registration point — see `templates/handlers.py`.
_PORTABLE = {
    "flow.start", "flow.set_variable", "flow.if", "flow.foreach",
    "flow.filter", "flow.wait", "flow.fail",
    "tool.file_read", "tool.file_write", "tool.glob", "tool.web_fetch",
}


def package_name(pipeline: Pipeline) -> str:
    """A Python-safe module name from the pipeline's own name."""
    slug = re.sub(r"[^a-z0-9]+", "_", pipeline.name.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug) or "pipeline"
    if slug[0].isdigit():
        slug = f"p_{slug}"
    return slug


def _label(node: Any) -> str:
    return (node.name or node.type).replace('"', "'")


def mermaid(pipeline: Pipeline) -> str:
    """The graph as a Mermaid flowchart.

    Conditions are drawn on the arrows and loop bodies get their own subgraph,
    because those two are exactly what a reader cannot recover from a picture
    of boxes: which way failure goes, and what runs more than once.
    """
    lines = ["flowchart LR"]
    bodies: dict[str, list[str]] = {}
    for node in pipeline.nodes:
        node_type = get_registry().get(node.type)
        if node_type and any(p.name == "each" for p in node_type.outputs):
            body = loop_body(pipeline, node.id)
            if body:
                bodies[node.id] = body

    claimed = {nid for body in bodies.values() for nid in body}
    for node in pipeline.nodes:
        if node.id in claimed:
            continue
        lines.append(f'  {node.id}["{_label(node)}"]')

    for loop_id, body in bodies.items():
        lines.append(f'  subgraph loop_{loop_id}["once per item"]')
        for nid in body:
            node = next(n for n in pipeline.nodes if n.id == nid)
            lines.append(f'    {nid}["{_label(node)}"]')
        lines.append("  end")

    for edge in pipeline.edges:
        tag = ""
        if edge.when != "success":
            tag = edge.when
        elif edge.port not in ("out", ""):
            tag = edge.port
        arrow = f'-- "{tag}" -->' if tag else "-->"
        lines.append(f"  {edge.source} {arrow} {edge.target}")
    return "\n".join(lines)


def _requirements(pipeline: Pipeline) -> tuple[list[dict], list[dict]]:
    """(exported, needs-the-host) for every node type used."""
    registry = get_registry()
    exported: list[dict] = []
    needed: list[dict] = []
    seen: set[str] = set()
    for node in pipeline.nodes:
        if node.type in seen:
            continue
        seen.add(node.type)
        node_type = registry.get(node.type)
        entry = {
            "type": node.type,
            "label": node_type.label if node_type else node.type,
            "requires": node_type.requires if node_type else "",
            "connection": node_type.connection_kind if node_type else "",
        }
        (exported if node.type in _PORTABLE else needed).append(entry)
    return exported, needed


def architecture(pipeline: Pipeline) -> str:
    """The design note: what it does, what it needs, what did not come."""
    exported, needed = _requirements(pipeline)
    connections = sorted({n.connection_id for n in pipeline.nodes if n.connection_id})
    out: list[str] = [
        f"# {pipeline.name}",
        "",
        f"An exported Compass pipeline: {len(pipeline.nodes)} step(s), "
        f"{len(pipeline.edges)} connection(s). Version {pipeline.version}.",
        "",
        "## Shape",
        "",
        "```mermaid",
        mermaid(pipeline),
        "```",
        "",
        "## Steps",
        "",
        "| Step | Type | Runs |",
        "| --- | --- | --- |",
    ]
    inside = set()
    for node in pipeline.nodes:
        node_type = get_registry().get(node.type)
        if node_type and any(p.name == "each" for p in node_type.outputs):
            inside.update(loop_body(pipeline, node.id))
    for node in pipeline.nodes:
        runs = "once per item" if node.id in inside else "once"
        if node.state == "deactivated":
            runs = f"never (deactivated, reports {node.mark_as})"
        out.append(f"| {_label(node)} | `{node.type}` | {runs} |")

    out += ["", "## What the host must provide", ""]
    if needed:
        out.append(
            "These steps could not be exported as working code. Each one is "
            "registered as a named stub that raises until you supply an "
            "implementation, so nothing fails silently:"
        )
        out.append("")
        for entry in needed:
            out.append(f"- **`{entry['type']}`** — {entry['label']}."
                       + (f" Needs the `{entry['requires']}` capability."
                          if entry["requires"] else ""))
        out += ["", "```python", "from " + package_name(pipeline) + " import Pipeline",
                "", "p = Pipeline()"]
        for entry in needed:
            out.append(f"p.register({entry['type']!r}, your_handler)")
        out += ["result = p.run()", "```"]
    else:
        out.append("Nothing. Every step in this pipeline exported as working "
                   "code, so it runs as-is.")

    if connections:
        out += [
            "", "## Credentials", "",
            "This pipeline referenced "
            f"{len(connections)} stored connection(s) in Compass. Credentials "
            "are deliberately **not** exported — they live in Compass's secret "
            "store behind a reference, and copying them into a package would "
            "put them in your source control. Supply them the way the "
            "receiving application already handles secrets.",
        ]

    out += [
        "", "## Semantics worth knowing", "",
        "- An arrow carries a condition: `success`, `failure`, `completion` "
        "or `skip`. Failure handling is the wiring, not a separate mechanism.",
        "- A `For each` runs everything reachable from its `each` port once "
        "per item, sequentially, with each iteration isolated — one item "
        "failing does not fail the others, and the loop still continues past.",
        "- Settings may hold expressions: `@pipeline().parameters.x`, "
        "`@variables('v')`, `@nodes('id').data.field`, and inside a loop "
        "`@item()` and `@index()`.",
        "- A node that is deactivated never runs but reports the outcome it "
        "was told to, so the branches below it still exercise.",
    ]
    return "\n".join(out) + "\n"


def _graph_payload(pipeline: Pipeline) -> dict[str, Any]:
    """The graph as plain data the runtime can walk.

    Only what running needs: canvas positions, timeouts meant for a server and
    the log-redaction flags are Compass's business, not the library's. `loop`
    is precomputed so the runtime does not have to consult a type registry it
    does not have.
    """
    registry = get_registry()
    nodes = []
    for node in pipeline.nodes:
        node_type = registry.get(node.type)
        nodes.append({
            "id": node.id,
            "type": node.type,
            "name": node.name,
            "config": node.config,
            "retries": node.retries,
            "retry_interval_s": node.retry_interval_s,
            "state": node.state,
            "mark_as": node.mark_as,
            "loop": bool(node_type and any(p.name == "each"
                                           for p in node_type.outputs)),
        })
    return {
        "name": pipeline.name,
        "version": pipeline.version,
        "nodes": nodes,
        "edges": [{"source": e.source, "target": e.target,
                   "when": e.when, "port": e.port} for e in pipeline.edges],
        "parameters": pipeline.parameters,
        "variables": pipeline.variables,
    }


def python_package(pipeline: Pipeline) -> dict[str, str]:
    """The whole package, as a map of relative path to file content."""
    name = package_name(pipeline)
    graph = _graph_payload(pipeline)
    exported, needed = _requirements(pipeline)

    runtime = (TEMPLATES / "runtime.py").read_text(encoding="utf-8")
    handlers = (TEMPLATES / "handlers.py").read_text(encoding="utf-8")

    init = f'''"""{pipeline.name} — exported from Compass.

Generated. Re-exporting overwrites this package, so keep your own handlers in
your own module and attach them with `Pipeline.register`.
"""

from .runtime import Context, NodeResult, Pipeline as _Base, PipelineError
from .runtime import MissingHandler, RunResult
from .graph import GRAPH
from .handlers import HANDLERS

__all__ = ["Pipeline", "NodeResult", "Context", "RunResult",
           "PipelineError", "MissingHandler"]


class Pipeline(_Base):
    """{pipeline.name}, ready to run."""

    GRAPH = GRAPH
    HANDLERS = HANDLERS
'''

    # pformat, not json.dumps: this file is imported as Python, and JSON's
    # `true`/`false`/`null` are not Python literals. Caught by check_export
    # running the generated package rather than only reading it.
    graph_py = (
        '"""The pipeline as data. Generated — edit the pipeline, not this."""\n'
        "\n"
        "GRAPH = " + pformat(graph, indent=4, width=88, sort_dicts=False) + "\n"
    )

    lines = [
        f"# {pipeline.name}",
        "",
        "A pipeline exported from Compass as a standalone Python package. No "
        "third-party dependencies.",
        "",
        "## Install",
        "",
        "```bash",
        "pip install -e .",
        "```",
        "",
        "## Run",
        "",
        "```python",
        f"from {name} import Pipeline",
        "",
        "p = Pipeline()",
    ]
    for entry in needed:
        lines.append(f"p.register({entry['type']!r}, your_handler)"
                     f"  # {entry['label']}")
    lines += [
        "result = p.run()",
        'print(result.status, result.failed())',
        "```",
        "",
        f"{len(exported)} of {len(exported) + len(needed)} node type(s) "
        "exported as working code."
        + (" The rest raise until you register them — see ARCHITECTURE.md."
           if needed else ""),
    ]

    pyproject = f'''[project]
name = "{name.replace('_', '-')}"
version = "0.{pipeline.version}.0"
description = "{pipeline.name} — a pipeline exported from Compass"
requires-python = ">=3.10"
dependencies = []

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["{name}"]
'''

    return {
        f"{name}/__init__.py": init,
        f"{name}/runtime.py": runtime,
        f"{name}/handlers.py": handlers,
        f"{name}/graph.py": graph_py,
        f"{name}/graph.json": json.dumps(graph, indent=2) + "\n",
        "pyproject.toml": pyproject,
        "README.md": "\n".join(lines) + "\n",
        "ARCHITECTURE.md": architecture(pipeline),
    }


def bundle(pipeline: Pipeline) -> dict[str, Any]:
    """Everything an export offers, for one API response."""
    files = python_package(pipeline)
    _, needed = _requirements(pipeline)
    return {
        "name": pipeline.name,
        "package": package_name(pipeline),
        "mermaid": mermaid(pipeline),
        "architecture": files["ARCHITECTURE.md"],
        "files": [{"path": p, "content": c} for p, c in sorted(files.items())],
        "needs_host": needed,
    }
