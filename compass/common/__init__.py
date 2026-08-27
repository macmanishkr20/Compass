"""What more than one part of Compass needs.

Nothing in here belongs to Home, Code or Design in particular. A thing earns
its place by having callers in more than one of them, and that is a fact about
the imports rather than a matter of taste: `config`, the model gateway, the
message and event shapes, storage, permissions, the agent loop, the tool
protocol — and the services all three reach for, like workspaces and memory.

Common does not import from a section, with two deliberate exceptions, both of
which say so where they do it: `routes.get_customize` and `recap.build_recap`.
Summarising every section is the entire job of those two, so they reach into
each one — always inside the function, never at import time.
"""
