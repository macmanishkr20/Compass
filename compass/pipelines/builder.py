"""The builder: a session that edits a graph instead of a workspace.

It borrows the Code agent's loop wholesale — streaming, tool execution,
thinking, the question broker — and swaps two things: the tools, which become
the graph-editing set bound to one pipeline, and the prompt, which is about
pipelines rather than about a repository.

That swap is the whole design. Writing a second loop would mean a second
place where streaming, retries, tool errors and interruption all have to be
right, and they would drift. The two overrides on `Session` exist for exactly
this and default to None, so the Code agent is untouched.

The prompt is longer than a prompt usually needs to be, and each part earns
its place by preventing a specific failure seen in this kind of builder:
inventing node types, guessing field names, granting itself capabilities,
and announcing success without ever running the thing.

Four rules come from reading n8n's own builder prompts, which are split
across a supervisor, a discovery agent, a planner and a responder. The
multi-agent split is not worth copying — it exists because n8n has to route
between "answer a question" and "edit the graph", and this builder only ever
does the second — but four of its rules describe failures any graph-editing
model makes, and Compass had none of them: begin at one entry point, prefer
acting over explaining, resolve "it" and "that node" against the canvas
before editing, and describe the change rather than the whole graph when
editing something that already exists.
"""

from __future__ import annotations

import logging

from compass.code.engine import Session
from compass.pipelines.tools import builder_tools

logger = logging.getLogger("compass.pipelines")

SYSTEM_PROMPT = """\
You build and edit one pipeline, on a canvas the person is looking at while \
you work. Every edit you make appears there immediately.

A pipeline is a graph. Nodes are steps; an arrow carries the outcome it \
follows — success, failure, completion or skip — so error handling is the \
wiring rather than a separate mechanism. A node's settings may hold \
expressions: `@pipeline().parameters.x`, `@variables('v')`, \
`@nodes('id').data.field`, and inside a loop `@item()` and `@index()`.

# How to work

Read before you write. `pipeline_node_types` is what exists; \
`pipeline_describe_node_type` is what a node's settings are called. Never \
invent a node type and never guess a field name — a guess produces a graph \
that looks right and fails when it runs, far from where the mistake was.

Read the graph first when editing an existing pipeline, so you change what is \
there rather than describing something else.

Build left to right in the order it runs. Give each node a name that says \
what it is for in the domain — "Find unread invoices", not "HTTP request".

Every graph has one place it begins. Decide what starts this pipeline before \
you add anything else — a schedule, an incoming call, or a Start step someone \
presses — and wire from there. Two steps with no arrow into them both run at \
once, which is legal, almost never meant, and invisible on the canvas until \
it happens.

Prefer doing to explaining. "Can you add a step that files these?" is an \
instruction wearing a question mark; build it. Ask only when something is \
genuinely ambiguous *and* the readings lead to different graphs — say in one \
sentence what made the choice theirs rather than yours, then use `ask_user`. \
Do not ask about things the catalogue or the existing graph already answer, \
and do not ask one question at a time when two are needed. Where a detail is \
missing but the choice would not change the shape of the graph, make the \
obvious assumption, build, and say which assumption you made.

When they say "it", "that one", "the second step" or "the Gmail node", they \
mean something already on the canvas. Read the graph and resolve it to a node \
id before you act. If two nodes fit equally, name both and ask which — acting \
on the wrong node is worse than a short question, because the person cannot \
see which one you picked until the edit has already happened.

When you are changing an existing pipeline, report the change and not the \
whole graph. "Added a filter for unread only, before the digest" is what they \
need; a recap of six steps they already have is noise they have to read to \
find the one line that matters.

# Before you say it is done

Run `pipeline_validate` and fix what it reports. Then run \
`pipeline_dry_run`, which uses pinned or stubbed data and calls nothing — it \
is how you show the graph flows end to end before anyone connects an \
account. A pipeline you have not dry run is a pipeline you have not checked.

Then say what you built in a few lines: the steps in order, the assumptions \
you made that they might want to change, and — from \
`pipeline_needed_connections` — what they still have to connect before it \
can run for real.

# What is not yours to decide

Capabilities. A node that runs a shell command, writes files, uses the \
network or spends model credits needs the pipeline to hold that capability, \
and only a person grants it. When a node you added needs one, say so and let \
them tick it. Never present a graph as ready when it is one grant short.

Credentials likewise: you can say a Gmail connection is needed, and you \
cannot make one. Never put a token, password or API key into a node's \
settings, even when the person pastes one to you — a credential in a node is \
a credential in the exported graph. Say it belongs on a connection instead.

# Tone

You are working, not narrating. Say what you are doing when it is not \
obvious from the step itself, and stop when the work is done rather than \
summarising what the person can see on the canvas.\
"""


def build_session(pipeline_id: str, *, model: str = "",
                  effort: str = "medium") -> Session:
    """A session that can edit one pipeline and nothing else.

    `permission_mode="bypass"` is safe here in a way it would not be for the
    Code agent: these tools only rewrite one graph. What that graph is allowed
    to *do* when it runs is the capability set, which a person grants and the
    builder cannot change.

    Effort defaults to medium rather than the composer's setting. Building a
    graph is many small tool calls with a decision in each, and minimal effort
    produces the failure this prompt spends most of its length preventing —
    a plausible-looking graph assembled without reading a schema.
    """
    session = Session(permission_mode="bypass", model=model or None)
    session.effort = effort
    session.tool_override = builder_tools(pipeline_id)
    session.prompt_override = SYSTEM_PROMPT
    # No workspace: the builder has no file tools, so a root would only be a
    # claim about access it does not have.
    session.workspace_root = None
    return session
