"""Trigger nodes: the step a pipeline begins at when something happens.

Compass had schedules as a property of the pipeline and nothing else, which
left "when an email arrives" inexpressible and left the canvas unable to say
where a graph starts — the question that got asked here about a graph the
builder had just drawn. n8n makes the trigger a node, first in the row, and
that is the better model for one specific reason: a trigger has settings
(which mailbox, which search, how often) and settings belong on a node where
they can be seen, expressed and validated, not in a side panel about the
pipeline as a whole.

A trigger node runs in two situations and does something different in each,
which is worth stating plainly because it is the only unusual thing here:

  fired      `runner.py` polled, found messages, and started the run with
             them attached. The node emits what was delivered and calls
             nothing — the fetch already happened, outside the run.
  by hand    someone pressed Run, or Execute step. The node fetches now, so
             the graph can be built and tried before any schedule exists.
             This is n8n's "Fetch Test Event" button.

The cursor that stops the same message firing twice belongs to the runner,
not here: it is state about a subscription, not about one execution, and a
manual run must never move it or testing a graph would make the schedule skip
real mail.
"""

from __future__ import annotations

import logging
from typing import Any

from compass.pipelines.types import NodeContext, NodeResult, NodeType, Port

logger = logging.getLogger("compass.pipelines")

#: Where `runner.py` leaves what it polled. Read out of the run's parameters
#: rather than passed as config, because it belongs to this execution and not
#: to the design of the graph.
DELIVERY_KEY = "_trigger"

TIMEOUT_S = 30.0


async def _fetch_messages(ctx: NodeContext, query: str,
                          limit: int) -> list[dict[str, Any]]:
    """List, then read each one, because Gmail's list returns bare ids.

    A trigger whose output is a list of ids would make every graph start with
    the same two extra steps, so the read is done here and the node emits
    whole messages with a sender and a subject on them.
    """
    import httpx

    if ctx.connection is None:
        raise RuntimeError("This trigger needs a Gmail connection.")
    conn = await ctx.connection("")
    if not conn:
        raise RuntimeError(
            "No Gmail connection chosen. Pick one in this step's settings, "
            "or run the pipeline in mock mode to see it work without an "
            "account."
        )
    headers = {"Accept": "application/json", **(conn.get("headers") or {})}
    if len(headers) == 1:
        raise RuntimeError(
            "That Gmail connection is not signed in yet. Open it on the "
            "Connections panel and sign in."
        )

    base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        listing = await client.get(base, headers=headers, params={
            "q": query or "", "maxResults": max(1, min(int(limit or 10), 50)),
        })
        if listing.status_code >= 400:
            raise RuntimeError(
                f"Gmail returned {listing.status_code}: {listing.text[:300]}")
        ids = [m.get("id") for m in (listing.json().get("messages") or [])]

        out: list[dict[str, Any]] = []
        for message_id in ids:
            if not message_id:
                continue
            detail = await client.get(f"{base}/{message_id}", headers=headers,
                                      params={"format": "full"})
            if detail.status_code >= 400:
                continue
            out.append(_flatten(detail.json()))
    return out


def _flatten(message: dict[str, Any]) -> dict[str, Any]:
    """Gmail's shape, plus the four fields every graph reaches for.

    The headers are a list of {name, value} pairs, which means the obvious
    expression — `@item().from` — does not work against the raw payload. They
    are lifted onto the item, and the original is kept so nothing is lost.
    """
    headers = {}
    for header in ((message.get("payload") or {}).get("headers") or []):
        name = str(header.get("name", "")).lower()
        if name in ("from", "to", "subject", "date"):
            headers[name] = header.get("value", "")
    return {
        "id": message.get("id", ""),
        "threadId": message.get("threadId", ""),
        "labelIds": message.get("labelIds") or [],
        "snippet": message.get("snippet", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "payload": message.get("payload") or {},
    }


async def _gmail_trigger(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    delivered = (ctx.parameters or {}).get(DELIVERY_KEY)
    if isinstance(delivered, dict) and delivered.get("node_id") in (
            ctx.node_id, None):
        items = list(delivered.get("items") or [])
        return NodeResult(
            data={"items": items, "count": len(items)},
            text=f"{len(items)} message(s) from the trigger",
        )

    # Run by hand: fetch now, so a graph can be tried before it is scheduled.
    items = await _fetch_messages(ctx, str(config.get("query") or ""),
                                  int(config.get("limit") or 10))
    return NodeResult(
        data={"items": items, "count": len(items)},
        text=f"{len(items)} message(s) matching the search",
    )


_SAMPLE = {
    "items": [{
        "id": "18f2a9c1b7e40a11",
        "threadId": "18f2a9c1b7e40a11",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Here are the project details for the upcoming "
                   "engagement. Project: Aurora Mobile Redesign.",
        "from": "Sarah Mitchell <sarah.mitchell@northwindtrading.com>",
        "to": "projects@ourcompany.com",
        "subject": "Project Details - Aurora Mobile Redesign",
        "date": "Sun, 30 Aug 2026 18:40:19 +0000",
    }],
    "count": 1,
}


def provider() -> list[NodeType]:
    return [
        NodeType(
            id="gmail.trigger",
            label="Gmail trigger",
            # "connector", not "flow", and the distinction is load-bearing: the
            # engine runs flow nodes for real even in a mocked run, because
            # mocking an `if` would make the mocked graph a different graph.
            # A trigger calls Gmail, so mocking it is exactly right — and
            # categorising it as flow made a mocked run try to reach the
            # mailbox and fail, which is how this was found.
            category="connector",
            handler=_gmail_trigger,
            description="Starts this pipeline when mail matching a search "
                        "arrives. Emits whole messages — sender, subject, "
                        "snippet — so the next step does not have to fetch "
                        "each one. Run by hand it fetches immediately, which "
                        "is how you try the graph before scheduling it.",
            config_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "title": "Search",
                        "description": "Gmail search syntax, e.g. "
                                       "is:unread subject:\"project details\". "
                                       "Empty means every new message.",
                    },
                    "limit": {
                        "type": "integer",
                        "title": "Most per check",
                        "description": "Messages taken from one poll. A "
                                       "backlog is delivered over several.",
                        "default": 10,
                    },
                    "every_minutes": {
                        "type": "integer",
                        "title": "Check every (minutes)",
                        "description": "How often Compass looks. Gmail is "
                                       "polled; there is no push here.",
                        "default": 5,
                    },
                },
            },
            connection_kind="gmail",
            # No input port: a trigger is where the graph begins, and giving
            # it one would let someone wire into it and produce a graph with
            # no entry point at all.
            inputs=(),
            outputs=(Port("out", "items", "Messages"),),
            requires="network",
            concurrency_safe=False,
            sample=_SAMPLE,
        ),
    ]
