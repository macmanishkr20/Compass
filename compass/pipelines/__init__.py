"""Pipelines: Compass's capabilities, wired together on a canvas.

The module is off by default. Nothing here is imported unless
`settings.pipelines.enabled` is true, which is why `compass/api/server.py`
imports the router inside a conditional rather than at the top of the file:
an unfinished module should cost nothing at all when it is switched off, and
"costs nothing" has to include import time and the route table.

The shape, in one paragraph. A *node type* declares what it needs (a JSON
Schema), what it produces (typed ports) and how to run it (an async handler).
Node types come from *providers* — the tool registry, the MCP manager,
connectors, control flow, the other modules — so the catalogue is open-ended
rather than a list somebody maintains. A *pipeline* is nodes and edges, where
an edge carries the condition it follows. A *run* walks that graph, keeping
its state in the store rather than in a process, because a node that waits
for a person can wait for hours.
"""
