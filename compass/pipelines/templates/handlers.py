"""Node implementations that survive the trip out of Compass.

Copied verbatim into an exported package. Two groups.

*Control flow* is pure logic — branching, looping, filtering, variables — so
it is exported complete and behaves exactly as it did in Compass.

*Everything else* is a stub that raises, and the stub is the honest answer
rather than a gap. A node that reads the workspace, runs a shell command,
calls a model or spends a stored credential cannot be handed to another
application by copying code: it needs that application's filesystem, its
model access, its secrets. So the export names each one, says what it needed,
and leaves a registration point:

    pipeline.register("tool.bash", lambda config, ctx: NodeResult(...))

Raising beats returning empty. A pipeline that silently skips the step which
sends the email looks like it worked, and that is the failure mode worth
spending an exception to avoid.
"""

from __future__ import annotations

import fnmatch
import time
from pathlib import Path
from typing import Any

from .runtime import Context, NodeResult, PipelineError

# --------------------------------------------------------------------------- flow


def start(config: dict, ctx: Context) -> NodeResult:
    return NodeResult(text="Run started")


def set_variable(config: dict, ctx: Context) -> NodeResult:
    name = str(config.get("name") or "").strip()
    if not name:
        raise PipelineError("Set variable needs a name")
    ctx.variables[name] = config.get("value")
    return NodeResult(data={"name": name, "value": config.get("value")},
                      text=f"Set {name}")


def if_condition(config: dict, ctx: Context) -> NodeResult:
    """Branches on `out` or `false`, so the graph carries the decision."""
    value = config.get("condition")
    truthy = bool(value) and value not in ("false", "False", "0", 0)
    return NodeResult(data={"result": truthy},
                      text=f"Condition was {truthy}",
                      port="out" if truthy else "false")


def foreach(config: dict, ctx: Context) -> NodeResult:
    items = config.get("items")
    if isinstance(items, str):
        raise PipelineError(
            "For each needs a list; got a string. Point it at an expression "
            "that resolves to one, such as @nodes('read').data.items"
        )
    items = list(items or [])
    limit = config.get("max_items")
    if limit:
        items = items[: int(limit)]
    return NodeResult(data={"items": items, "count": len(items)},
                      text=f"Fanning out over {len(items)} item(s)",
                      port="each")


def filter_items(config: dict, ctx: Context) -> NodeResult:
    items = list(config.get("items") or [])
    field = str(config.get("field") or "").strip()
    equals = config.get("equals")
    if not field:
        kept = [i for i in items if i]
    else:
        kept = [i for i in items
                if isinstance(i, dict) and i.get(field) == equals]
    return NodeResult(data={"items": kept, "count": len(kept)},
                      text=f"Kept {len(kept)} of {len(items)}")


def wait(config: dict, ctx: Context) -> NodeResult:
    seconds = float(config.get("seconds") or 0)
    time.sleep(max(0.0, min(seconds, 300.0)))
    return NodeResult(text=f"Waited {seconds}s")


def fail(config: dict, ctx: Context) -> NodeResult:
    raise PipelineError(str(config.get("message") or "Pipeline failed"))


def approval(config: dict, ctx: Context) -> NodeResult:
    """Approval cannot be exported, because there is nobody to ask.

    In Compass this parks the run and a person answers on screen. A library
    has no screen, so the host decides what approval means — a queue, a Slack
    message, an auto-approve in a test — and registers it.
    """
    raise PipelineError(
        "Approval needs a person. Register a handler that asks whoever should "
        "decide: pipeline.register('flow.approval', your_handler). It "
        "receives the question in config['question'] and should return "
        "NodeResult(port='out') to continue or port='false' to stop."
    )


# --------------------------------------------------------------------------- portable tools


def file_read(config: dict, ctx: Context) -> NodeResult:
    path = Path(str(config.get("path") or ""))
    text = path.read_text(encoding="utf-8", errors="replace")
    return NodeResult(data={"text": text, "path": str(path)},
                      text=f"Read {path}")


def file_write(config: dict, ctx: Context) -> NodeResult:
    path = Path(str(config.get("path") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    content = config.get("content")
    path.write_text("" if content is None else str(content), encoding="utf-8")
    return NodeResult(data={"path": str(path)}, text=f"Wrote {path}")


def glob_files(config: dict, ctx: Context) -> NodeResult:
    root = Path(str(config.get("path") or "."))
    pattern = str(config.get("pattern") or "*")
    hits = [str(p) for p in root.rglob("*") if fnmatch.fnmatch(p.name, pattern)]
    return NodeResult(data={"items": hits, "count": len(hits)},
                      text=f"Found {len(hits)} file(s)")


def web_fetch(config: dict, ctx: Context) -> NodeResult:
    """Stdlib only, so the package stays dependency-free.

    Deliberately plainer than Compass's: no redirect ceiling, no refusal of
    link-local and metadata addresses. Those protections belong to the host,
    which knows its own network — read the URL from somewhere you trust, or
    register a handler that uses your HTTP client and its policies.
    """
    import urllib.request

    url = str(config.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise PipelineError(f"web fetch needs an http(s) URL, got {url!r}")
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        body = response.read(2_000_000).decode("utf-8", errors="replace")
    return NodeResult(data={"body": body, "url": url},
                      text=f"Fetched {url}")


# --------------------------------------------------------------------------- registry


def _needs_host(node_type: str, why: str):
    def handler(config: dict, ctx: Context) -> NodeResult:
        raise PipelineError(
            f"{node_type} was not exported because {why}. Supply it with "
            f"pipeline.register({node_type!r}, your_handler)."
        )
    return handler


HANDLERS: dict[str, Any] = {
    "flow.start": start,
    "flow.set_variable": set_variable,
    "flow.if": if_condition,
    "flow.foreach": foreach,
    "flow.filter": filter_items,
    "flow.wait": wait,
    "flow.fail": fail,
    "flow.approval": approval,
    "tool.file_read": file_read,
    "tool.file_write": file_write,
    "tool.glob": glob_files,
    "tool.web_fetch": web_fetch,
    # Named rather than omitted, so the error says what is missing and why.
    "tool.bash": _needs_host("tool.bash", "it runs commands on a machine only "
                             "the host can choose"),
    "tool.bash_output": _needs_host("tool.bash_output", "it reads a shell "
                                    "session the host owns"),
    "tool.grep": _needs_host("tool.grep", "Compass's version searches an "
                             "indexed workspace"),
    "tool.file_edit": _needs_host("tool.file_edit", "it edits files in a "
                                  "workspace the host defines"),
    "tool.agent": _needs_host("tool.agent", "it needs a model gateway and "
                              "credentials"),
    "tool.consult": _needs_host("tool.consult", "it needs a model gateway"),
    "tool.memory": _needs_host("tool.memory", "it reads Compass's own memory"),
    "tool.browser": _needs_host("tool.browser", "it drives a browser Compass "
                                "hosts"),
    "tool.screenshot": _needs_host("tool.screenshot", "it captures the host "
                                   "machine's screen"),
}
