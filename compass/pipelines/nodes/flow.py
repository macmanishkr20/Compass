"""Control flow: the nodes that make a graph more than a straight line.

These are the only node types the engine has any special relationship with,
and even that is kept to a minimum — a handler returns a `port`, and the
engine follows edges leaving that port without knowing which node types
branch. An If is not a special case in the walk; it is a node that returns
"true" or "false".

Fan-out is the one that earns its keep. Without ForEach a pipeline processes
exactly one thing, which rules out most of what anyone actually wants: every
message, every file, every row. It is also the node with the most engine
support behind it, so it is marked here and expanded in the engine rather
than pretending to run inside a handler.
"""

from __future__ import annotations

import asyncio
from typing import Any

from compass.pipelines.types import NodeContext, NodeResult, NodeType, Port


async def _start(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    return NodeResult(data={"parameters": ctx.parameters}, text="Run started")


async def _if(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    """Branch on a resolved value's truthiness.

    The condition arrives already resolved — `@nodes('x').data.count` is a
    number by the time it gets here — so this only has to decide what counts
    as true, and it uses Python's own answer rather than inventing one.
    """
    value = config.get("condition")
    truthy = bool(value) and value not in ("false", "False", "0")
    return NodeResult(
        data={"condition": truthy},
        text=f"Condition was {truthy}",
        port="true" if truthy else "false",
    )


async def _foreach(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    """Announce a collection to fan out over.

    The iteration itself belongs to the engine: each item becomes its own
    execution of everything downstream of the `each` port, and the engine is
    the only thing that can schedule those and join them again. This handler's
    job is to produce the list and refuse anything that is not one.
    """
    items = config.get("items")
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        return NodeResult(
            text=f"Expected a list to iterate, got {type(items).__name__}",
            data={"items": []},
            port="out",
        )
    limit = int(config.get("max_items") or 0)
    if limit > 0:
        items = items[:limit]
    return NodeResult(data={"items": items, "count": len(items)},
                      text=f"Fanning out over {len(items)} item(s)",
                      port="each")


async def _filter(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    items = config.get("items") or []
    if not isinstance(items, list):
        items = [items]
    field = str(config.get("field") or "")
    equals = config.get("equals")
    kept = [
        item for item in items
        if (item.get(field) if isinstance(item, dict) and field else item) == equals
    ]
    return NodeResult(data={"items": kept, "count": len(kept)},
                      text=f"Kept {len(kept)} of {len(items)}")


async def _set_variable(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    """Write a run variable.

    Mutation is deliberately a node rather than something any node may do:
    parameters are fixed so a run is reproducible, and every change to that
    reproducibility is visible on the canvas as a step somebody placed.
    """
    name = str(config.get("name") or "").strip()
    if not name:
        # Raise rather than return. A node that reports "done" while doing
        # nothing sends the run down the success branch and fails later
        # somewhere else — the downstream expression is what breaks, and the
        # error names the wrong node. Failing here says what is actually
        # wrong and where.
        raise ValueError(
            "Set variable has no name, so there is nothing to write. Give it "
            "the name of the variable to set."
        )
    ctx.variables[name] = config.get("value")
    return NodeResult(data={"name": name, "value": config.get("value")},
                      text=f"Set {name}")


async def _wait(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    seconds = max(0.0, min(float(config.get("seconds") or 0), 900.0))
    await asyncio.sleep(seconds)
    return NodeResult(data={"seconds": seconds}, text=f"Waited {seconds:g}s")


async def _fail(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    raise RuntimeError(str(config.get("message") or "Pipeline failed here"))


async def _approval(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    """Stop and wait for a person.

    Returning `waiting_on` parks the run: the engine persists and stops, and
    the resume endpoint restarts the walk with the answer in hand. This is the
    same shape as the permission gate and `ask_user` — stop on a decision, let
    a surface render it, resolve over REST — and it is the reason a run's
    state lives in the store instead of in a process.
    """
    return NodeResult(
        data={
            "question": config.get("question") or "Approve this run?",
            "options": config.get("options") or ["Approve", "Reject"],
        },
        text=str(config.get("question") or "Waiting for approval"),
        waiting_on=f"approval:{ctx.node_id}",
    )


def _schema(properties: dict[str, Any], required: list[str] | None = None
            ) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def provider() -> list[NodeType]:
    return [
        NodeType(
            id="flow.start",
            label="Start",
            category="flow",
            description="Where a run begins. Carries the run's parameters.",
            handler=_start,
            config_schema=_schema({}),
            inputs=(),
        ),
        NodeType(
            id="flow.if",
            label="If condition",
            category="flow",
            description="Branch on a value. Downstream nodes hang off the "
                        "true or false port.",
            handler=_if,
            config_schema=_schema(
                {"condition": {"type": ["string", "boolean", "number"],
                               "title": "Condition",
                               "description": "Usually an expression, e.g. "
                                              "@nodes('search').data.count"}},
                ["condition"],
            ),
            outputs=(Port("true", "signal", "True"),
                     Port("false", "signal", "False")),
        ),
        NodeType(
            id="flow.foreach",
            label="For each",
            category="flow",
            description="Run everything downstream once per item.",
            handler=_foreach,
            config_schema=_schema(
                {
                    "items": {"type": ["array", "string"], "title": "Items",
                              "description": "A list, or an expression "
                                             "resolving to one."},
                    "max_items": {"type": "integer", "title": "Maximum items",
                                  "description": "0 for no limit.",
                                  "default": 0},
                },
                ["items"],
            ),
            outputs=(Port("each", "json", "Each item"),
                     Port("out", "items", "All items")),
            concurrency_safe=False,
        ),
        NodeType(
            id="flow.filter",
            label="Filter",
            category="flow",
            description="Keep the items whose field equals a value.",
            handler=_filter,
            config_schema=_schema(
                {
                    "items": {"type": ["array", "string"], "title": "Items"},
                    "field": {"type": "string", "title": "Field"},
                    "equals": {"title": "Equals"},
                },
                ["items"],
            ),
            outputs=(Port("out", "items"),),
        ),
        NodeType(
            id="flow.set_variable",
            label="Set variable",
            category="flow",
            description="Change a run variable.",
            handler=_set_variable,
            config_schema=_schema(
                {"name": {"type": "string", "title": "Name"},
                 "value": {"title": "Value"}},
                ["name"],
            ),
        ),
        NodeType(
            id="flow.wait",
            label="Wait",
            category="flow",
            description="Pause for a number of seconds.",
            handler=_wait,
            config_schema=_schema(
                {"seconds": {"type": "number", "title": "Seconds",
                             "default": 5, "maximum": 900}},
            ),
        ),
        NodeType(
            id="flow.fail",
            label="Fail",
            category="flow",
            description="Stop the run with a message. Useful on an "
                        "error branch.",
            handler=_fail,
            config_schema=_schema(
                {"message": {"type": "string", "title": "Message"}},
            ),
            outputs=(),
        ),
        NodeType(
            id="flow.approval",
            label="Approval",
            category="flow",
            description="Wait for a person to approve or reject before "
                        "continuing.",
            handler=_approval,
            config_schema=_schema(
                {
                    "question": {"type": "string", "title": "Question"},
                    "options": {"type": "array", "title": "Options",
                                "items": {"type": "string"},
                                "default": ["Approve", "Reject"]},
                },
            ),
            outputs=(Port("out", "json", "Answer"),),
            concurrency_safe=False,
        ),
    ]
