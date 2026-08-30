"""Dynamic content: how a node's settings reach the rest of the run.

Without this a graph is just an ordering. With it, a node's configuration can
name a parameter, a variable, or what an earlier node produced — which is what
makes an arrow carry data.

The syntax follows Fabric's, because it is the one the user asked for and
because its choices are sound:

    @pipeline().parameters.name    a parameter, fixed for the run
    @variables('name')             a variable, mutable mid-run
    @nodes('id').data.field        an upstream node's structured output
    @nodes('id').text              its human-readable half
    @{ ... }                       interpolation inside a string
    @@                             a literal at-sign

One deliberate divergence. Fabric writes `@activity('Name')`, keyed by the
activity's display name; this keys by node id, because a name is editable and
a reference that breaks when someone renames a step is a trap. The node's name
is for reading, its id is for referring to.

Two rules that are easy to get wrong and matter:

A whole-string expression preserves type — `@nodes('x').data.count` yields the
number, not "42" — while interpolation always produces a string. That is what
lets a numeric setting be driven by an upstream count without the schema
rejecting it.

This is a resolver, not an evaluator. There are no operators, no arithmetic
and no function calls, and that is on purpose for a first version: every one
of those is a place where a hand-rolled parser grows a security problem. The
escape hatch is a Python node, which is sandboxed and explicit about what it
is. Functions can be added later, from a fixed table, without changing the
syntax.
"""

from __future__ import annotations

import re
from typing import Any

#: A whole-value expression: the entire string is one reference.
_WHOLE = re.compile(r"^@(?!@)(?!\{)(.+)$", re.DOTALL)
#: An interpolated reference inside a larger string.
_INTERP = re.compile(r"@\{([^}]*)\}")

_PARAM = re.compile(r"^pipeline\(\)\.parameters\.(.+)$")
_VAR = re.compile(r"^variables\(\s*'([^']*)'\s*\)$")
_NODE = re.compile(r"^nodes\(\s*'([^']*)'\s*\)(?:\.(.+))?$")
_RUN = re.compile(r"^run\(\)\.(id|trigger|started_at)$")


class ExpressionError(ValueError):
    """A reference that cannot be resolved against this run."""


def _walk(value: Any, path: str) -> Any:
    """Follow a dotted path, with [i] for list indexes."""
    if not path:
        return value
    current = value
    for part in re.split(r"\.(?![^\[]*\])", path):
        if not part:
            continue
        # Split "field[0][1]" into a name and its indexes.
        name, *indexes = re.split(r"\[(\d+)\]", part)
        if name:
            if not isinstance(current, dict) or name not in current:
                raise ExpressionError(f"no field {name!r} in {path!r}")
            current = current[name]
        for index in (i for i in indexes if i.isdigit()):
            try:
                current = current[int(index)]
            except (TypeError, KeyError, IndexError) as err:
                raise ExpressionError(f"index {index} out of range in {path!r}") from err
    return current


class Resolver:
    """Resolves references against one run's state."""

    def __init__(
        self,
        *,
        parameters: dict[str, Any],
        variables: dict[str, Any],
        nodes: dict[str, dict[str, Any]],
        run: dict[str, Any] | None = None,
    ) -> None:
        self.parameters = parameters
        self.variables = variables
        #: node id -> {"data": {...}, "text": "..."}
        self.nodes = nodes
        self.run = run or {}

    def reference(self, expr: str) -> Any:
        expr = expr.strip()
        if match := _PARAM.match(expr):
            key = match.group(1)
            if key not in self.parameters:
                raise ExpressionError(f"no parameter {key!r}")
            return self.parameters[key]
        if match := _VAR.match(expr):
            key = match.group(1)
            if key not in self.variables:
                raise ExpressionError(f"no variable {key!r}")
            return self.variables[key]
        if match := _NODE.match(expr):
            node_id, path = match.group(1), match.group(2) or ""
            if node_id not in self.nodes:
                raise ExpressionError(
                    f"node {node_id!r} has not produced a result yet"
                )
            return _walk(self.nodes[node_id], path)
        if match := _RUN.match(expr):
            return self.run.get(match.group(1), "")
        raise ExpressionError(f"unrecognised expression: {expr}")

    def value(self, raw: Any) -> Any:
        """Resolve one setting value, recursing through dicts and lists."""
        if isinstance(raw, dict):
            return {k: self.value(v) for k, v in raw.items()}
        if isinstance(raw, list):
            return [self.value(v) for v in raw]
        if not isinstance(raw, str):
            return raw

        if raw.startswith("@@"):
            # An escaped literal: "@@x" is the string "@x".
            return raw[1:]
        if match := _WHOLE.match(raw):
            # Whole-string form keeps the referenced type intact.
            return self.reference(match.group(1))

        def _sub(m: re.Match[str]) -> str:
            return str(self.reference(m.group(1)))

        return _INTERP.sub(_sub, raw)

    def config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Resolve a node's whole settings object."""
        return {k: self.value(v) for k, v in config.items()}


def references(config: Any) -> set[str]:
    """Every node id a settings object refers to.

    Used at edit time: a reference to a node that is not upstream is a broken
    pipeline, and saying so while someone is looking at the canvas beats
    failing at 3am when the schedule fires.
    """
    found: set[str] = set()
    if isinstance(config, dict):
        for value in config.values():
            found |= references(value)
    elif isinstance(config, list):
        for value in config:
            found |= references(value)
    elif isinstance(config, str):
        for match in re.finditer(r"nodes\(\s*'([^']*)'\s*\)", config):
            found.add(match.group(1))
    return found
