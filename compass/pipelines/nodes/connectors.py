"""Connectors: the outside world, as node types.

A connector is declared, not written. Each one names a base URL, how it
authenticates, and its operations; every operation becomes a node type with a
generated settings form and one shared handler that does the HTTP. Adding
Gmail's "list labels" is a line in a tuple, not a new module — which is the
only way a catalogue of this shape stays maintainable once it has more than
three services in it.

Credentials never appear here. An operation names the `connection_kind` it
needs, the person creates a connection of that kind with the secret, and the
handler asks for it at the moment of the call. That is what keeps a pipeline
exportable and shareable: the graph holds a connection id, never a token.

A word on authentication, because the honest version matters more than the
convenient one. GitHub takes a personal access token, which is a value you
paste once and which keeps working. Gmail and Microsoft Graph take OAuth
access tokens, which expire in about an hour — so `oauth.py` runs the
authorization-code flow and renews them from a refresh token at the moment of
each call, which is what makes a scheduled pipeline survive the night.

What that flow cannot do is skip registration. The one-click "Sign in with
Google" in n8n's own demos is n8n *Cloud*, which operates a Google-verified
OAuth application; their documentation says self-hosted users must register an
app and supply a client id and secret, and Compass is self-hosted. So the
first setup asks for those two values, once, and `credentials.py` reads a
registered Compass application from the environment if there is ever one.

MCP is the other route to a connector and often the better one: a server that
speaks Gmail becomes node types through `tools.mcp_provider` with no code
here at all. These exist for the services worth having built in, and for the
generic HTTP node, which is the escape hatch that stops the catalogue being a
cage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from compass.pipelines.types import NodeContext, NodeResult, NodeType, Port

logger = logging.getLogger("compass.pipelines")

#: How long one call may take. Generous enough for a slow API, short enough
#: that a hung endpoint does not hold a run open until its node timeout.
TIMEOUT_S = 45.0

#: Response bodies are truncated here. A node's output is handed to every
#: downstream node and persisted with the run; a 40MB attachment listing would
#: make the run file unreadable and the transcript unusable.
MAX_BODY = 400_000


@dataclass(frozen=True)
class Field:
    """One setting on an operation, and where it goes in the request."""

    name: str
    where: str  # "path" | "query" | "body" | "header"
    title: str = ""
    description: str = ""
    required: bool = False
    kind: str = "string"  # JSON Schema type
    default: Any = None


@dataclass(frozen=True)
class Operation:
    id: str
    label: str
    method: str
    path: str
    description: str = ""
    fields: tuple[Field, ...] = ()
    #: Where the useful list lives in the response, so a fan-out can be wired
    #: straight onto it — "messages", "value", "" for the whole body.
    items_at: str = ""
    #: What a mocked run returns for this operation. Shaped like the real
    #: response, because the point of a mocked run is to show the graph doing
    #: its job before an account is connected — and a downstream expression
    #: like @nodes('x').data.items[0].subject has to resolve against it or
    #: the mocked run proves less than it appears to.
    sample: dict[str, Any] | None = None


@dataclass(frozen=True)
class Connector:
    kind: str
    label: str
    base_url: str
    auth: str  # "bearer" | "token" | "none"
    operations: tuple[Operation, ...]
    note: str = ""


CONNECTORS: tuple[Connector, ...] = (
    Connector(
        kind="github",
        label="GitHub",
        base_url="https://api.github.com",
        auth="bearer",
        note="Authenticates with a personal access token, which does not "
             "expire on a timer — paste one into a GitHub connection.",
        operations=(
            Operation(
                id="list_issues", label="GitHub · list issues", method="GET",
                path="/repos/{owner}/{repo}/issues",
                description="Open issues on a repository. The list is on "
                            "`items`, so a For each can fan out over it.",
                items_at="",
                fields=(
                    Field("owner", "path", "Owner", required=True),
                    Field("repo", "path", "Repository", required=True),
                    Field("state", "query", "State",
                          description="open, closed or all.", default="open"),
                    Field("labels", "query", "Labels",
                          description="Comma-separated."),
                    Field("per_page", "query", "How many", kind="integer",
                          default=30),
                ),
            ),
            Operation(
                id="create_issue", label="GitHub · create issue", method="POST",
                path="/repos/{owner}/{repo}/issues",
                description="Opens an issue.",
                fields=(
                    Field("owner", "path", "Owner", required=True),
                    Field("repo", "path", "Repository", required=True),
                    Field("title", "body", "Title", required=True),
                    Field("body", "body", "Body"),
                ),
            ),
            Operation(
                id="list_pulls", label="GitHub · list pull requests",
                method="GET", path="/repos/{owner}/{repo}/pulls",
                description="Pull requests on a repository.",
                fields=(
                    Field("owner", "path", "Owner", required=True),
                    Field("repo", "path", "Repository", required=True),
                    Field("state", "query", "State", default="open"),
                ),
            ),
        ),
    ),
    Connector(
        kind="gmail",
        label="Gmail",
        base_url="https://gmail.googleapis.com/gmail/v1",
        auth="bearer",
        note="Sign in on the connection and Compass keeps the access token "
             "renewed from its refresh token, so a scheduled pipeline does "
             "not stop working overnight.",
        operations=(
            Operation(
                id="list_messages", label="Gmail · list messages",
                method="GET", path="/users/me/messages",
                description="Message ids matching a query. Gmail returns ids "
                            "only — follow with 'get message' to read one.",
                items_at="messages",
                sample={"messages": [
                    {"id": "18f2a9c1b7e40a11", "threadId": "18f2a9c1b7e40a11"},
                    {"id": "18f2a7d4c0193b02", "threadId": "18f2a7d4c0193b02"},
                ], "resultSizeEstimate": 2},
                fields=(
                    Field("q", "query", "Search",
                          description="Gmail search syntax, e.g. "
                                      "is:unread from:billing@"),
                    Field("maxResults", "query", "How many", kind="integer",
                          default=25),
                ),
            ),
            Operation(
                id="get_message", label="Gmail · get message", method="GET",
                path="/users/me/messages/{id}",
                description="One message in full.",
                sample={
                    "id": "18f2a9c1b7e40a11",
                    "threadId": "18f2a9c1b7e40a11",
                    "labelIds": ["INBOX", "UNREAD"],
                    "snippet": "Your September invoice is ready. The total "
                               "due is 1,240.00 and payment is expected by "
                               "the 30th.",
                    "payload": {"headers": [
                        {"name": "From",
                         "value": "Billing <billing@example.com>"},
                        {"name": "To", "value": "you@example.com"},
                        {"name": "Subject", "value": "Invoice INV-2043 is ready"},
                        {"name": "Date", "value": "Tue, 2 Sep 2026 09:14:02 +0000"},
                    ]},
                },
                fields=(
                    Field("id", "path", "Message id", required=True,
                          description="From a list step: "
                                      "@item().id inside a For each."),
                    Field("format", "query", "Format", default="full"),
                ),
            ),
            Operation(
                id="send", label="Gmail · send", method="POST",
                path="/users/me/messages/send",
                description="Sends a message. `raw` must be base64url of an "
                            "RFC 2822 message.",
                sample={"id": "18f2b0117c5d9e44",
                        "threadId": "18f2b0117c5d9e44",
                        "labelIds": ["SENT"],
                        "simulated": True},
                fields=(Field("raw", "body", "Raw message", required=True),),
            ),
        ),
    ),
    Connector(
        kind="outlook",
        label="Outlook",
        base_url="https://graph.microsoft.com/v1.0",
        auth="bearer",
        note="Microsoft Graph. Sign in on the connection and Compass keeps "
             "the access token renewed from its refresh token, so a "
             "scheduled pipeline does not stop working overnight.",
        operations=(
            Operation(
                id="list_messages", label="Outlook · list messages",
                method="GET", path="/me/messages",
                description="Messages from the signed-in mailbox. The list is "
                            "on `value`, which is where a For each points.",
                items_at="value",
                sample={"value": [
                    {"id": "AAMkAGI2T",
                     "subject": "Invoice INV-2043 is ready",
                     "from": {"emailAddress": {"name": "Billing",
                                               "address": "billing@example.com"}},
                     "receivedDateTime": "2026-09-02T09:14:02Z",
                     "isRead": False,
                     "bodyPreview": "Your September invoice is ready. The "
                                    "total due is 1,240.00."},
                    {"id": "AAMkAGI2U",
                     "subject": "Standup moved to 10:15",
                     "from": {"emailAddress": {"name": "Priya Nair",
                                               "address": "priya@example.com"}},
                     "receivedDateTime": "2026-09-02T07:41:55Z",
                     "isRead": True,
                     "bodyPreview": "Pushing today's standup by fifteen "
                                    "minutes."},
                ]},
                fields=(
                    Field("$search", "query", "Search",
                          description='Graph search, e.g. "invoice".'),
                    Field("$filter", "query", "Filter",
                          description="OData filter, e.g. isRead eq false."),
                    Field("$top", "query", "How many", kind="integer",
                          default=25),
                ),
            ),
            Operation(
                id="get_message", label="Outlook · get message", method="GET",
                path="/me/messages/{id}",
                description="One message in full.",
                fields=(Field("id", "path", "Message id", required=True,
                              description="From a list step: @item().id"),),
            ),
            Operation(
                id="send", label="Outlook · send mail", method="POST",
                path="/me/sendMail",
                description="Sends a message. `message` is a Graph message "
                            "object; `saveToSentItems` defaults to true.",
                fields=(
                    Field("message", "body", "Message", kind="object",
                          required=True),
                    Field("saveToSentItems", "body", "Save to Sent",
                          kind="boolean", default=True),
                ),
            ),
        ),
    ),
)


def _schema(operation: Operation) -> dict[str, Any]:
    """The settings form for an operation, as JSON Schema."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in operation.fields:
        entry: dict[str, Any] = {
            "type": field.kind,
            "title": field.title or field.name,
        }
        if field.description:
            entry["description"] = field.description
        if field.default is not None:
            entry["default"] = field.default
        properties[field.name] = entry
        if field.required:
            required.append(field.name)
    return {"type": "object", "properties": properties, "required": required}


async def _call(connector: Connector, operation: Operation,
                config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    """One HTTP request, with the connection's secret applied."""
    import httpx

    if ctx.connection is None:
        raise RuntimeError("This node needs a connection and none was given.")
    conn = await ctx.connection("")  # the engine resolves the node's own
    # Two different mistakes with two different fixes, so they get two
    # different sentences: no connection chosen on the node, versus a
    # connection chosen that carries no credential.
    if not conn:
        raise RuntimeError(
            f"No connection chosen. Pick a {connector.kind} connection in "
            "this node's settings, or create one first. To see the graph "
            "produce output before connecting an account, run it in mock "
            "mode — every step returns sample data and nothing is called."
        )
    secret = str(conn.get("secret") or "")
    if not secret and connector.auth != "none":
        raise RuntimeError(
            f"The {connector.label} connection has no credential yet. Open it "
            "on the Connections panel and "
            + ("sign in." if connector.auth == "oauth2" else "paste a token.")
            + " Or run the pipeline in mock mode to see it work without one."
        )

    path = operation.path
    query: dict[str, Any] = {}
    body: dict[str, Any] = {}
    headers: dict[str, str] = {"Accept": "application/json"}

    for field in operation.fields:
        value = config.get(field.name, field.default)
        if value is None or value == "":
            if field.required:
                raise RuntimeError(f"{field.title or field.name} is required.")
            continue
        if field.where == "path":
            # Quoted, because an id from an upstream message is not something
            # to paste into a URL unescaped.
            from urllib.parse import quote

            path = path.replace("{" + field.name + "}", quote(str(value), safe=""))
        elif field.where == "query":
            query[field.name] = value
        elif field.where == "body":
            body[field.name] = value
        else:
            headers[field.name] = str(value)

    if "{" in path:
        missing = path[path.index("{") + 1: path.index("}")]
        raise RuntimeError(f"{missing} is required.")

    # The credential type says how it authenticates; this no longer assumes
    # every service takes a bearer token. `headers` is computed by the engine
    # when it resolves the connection, from the same declaration the settings
    # form and the credential test are built from — so a change in one place
    # cannot leave the three disagreeing.
    headers.update(conn.get("headers") or {})

    url = connector.base_url.rstrip("/") + path
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        response = await client.request(
            operation.method, url, params=query or None,
            json=body or None, headers=headers,
        )

    text = response.text[:MAX_BODY]
    if response.status_code >= 400:
        # The body carries the reason far more often than the status does,
        # so it is included rather than swallowed.
        raise RuntimeError(
            f"{connector.label} returned {response.status_code}: {text[:600]}"
        )

    try:
        payload: Any = json.loads(text) if text.strip() else {}
    except ValueError:
        payload = {"text": text}

    data: dict[str, Any] = {"status": response.status_code}
    if isinstance(payload, dict):
        data.update(payload)
    else:
        data["result"] = payload

    # A list is put on `items` as well as where the API left it, so a For each
    # can be wired straight on without the author learning each API's shape.
    found = payload
    if operation.items_at and isinstance(payload, dict):
        found = payload.get(operation.items_at)
    if isinstance(found, list):
        data["items"] = found
        data["count"] = len(found)

    summary = f"{operation.label}: {response.status_code}"
    if "count" in data:
        summary += f", {data['count']} item(s)"
    return NodeResult(data=data, text=summary)


def _make(connector: Connector, operation: Operation) -> NodeType:
    async def handler(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
        return await _call(connector, operation, config, ctx)

    description = operation.description
    if connector.note:
        description = f"{description} {connector.note}".strip()

    return NodeType(
        id=f"{connector.kind}.{operation.id}",
        label=operation.label,
        category="connector",
        handler=handler,
        description=description,
        config_schema=_schema(operation),
        connection_kind=connector.kind,
        inputs=(Port("in"),),
        outputs=(Port("out", "json", "Result"),),
        requires="network",
        concurrency_safe=operation.method == "GET",
        sample=operation.sample,
    )


# --------------------------------------------------------------------------- generic


_HTTP_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "title": "URL",
                "description": "Absolute http(s) URL."},
        "method": {"type": "string", "title": "Method",
                   "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                   "default": "GET"},
        "headers": {"type": "object", "title": "Headers",
                    "description": "JSON object. The connection's secret is "
                                   "added as a bearer token when one is set."},
        "body": {"type": "object", "title": "Body",
                 "description": "JSON body, for anything but GET."},
    },
    "required": ["url"],
}


async def _http(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    """Any endpoint, with an optional connection for the credential.

    The escape hatch. Every declared connector above is a convenience over
    this, and anything not declared can still be reached without waiting for
    someone to add it.
    """
    import httpx

    url = str(config.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise RuntimeError(f"Needs an http(s) URL, got {url!r}")
    method = str(config.get("method") or "GET").upper()
    headers = dict(config.get("headers") or {})
    headers.setdefault("Accept", "application/json")

    if ctx.connection is not None:
        try:
            conn = await ctx.connection("")
            if secret := str(conn.get("secret") or ""):
                headers.setdefault("Authorization", f"Bearer {secret}")
        except Exception:  # noqa: BLE001 — a node with no connection is fine
            pass

    body = config.get("body")
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as client:
        response = await client.request(
            method, url, headers=headers,
            json=body if body and method != "GET" else None,
        )
    text = response.text[:MAX_BODY]
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {url} returned {response.status_code}: "
                           f"{text[:600]}")
    try:
        payload: Any = json.loads(text) if text.strip() else {}
    except ValueError:
        payload = text
    data: dict[str, Any] = {"status": response.status_code}
    if isinstance(payload, dict):
        data.update(payload)
    elif isinstance(payload, list):
        data["items"] = payload
        data["count"] = len(payload)
    else:
        data["text"] = payload
    return NodeResult(data=data, text=f"{method} {url} → {response.status_code}")


def provider() -> list[NodeType]:
    """Every connector operation, plus the generic request node."""
    types = [_make(c, op) for c in CONNECTORS for op in c.operations]
    types.append(NodeType(
        id="connector.http",
        label="HTTP request",
        category="connector",
        handler=_http,
        description="Call any HTTP endpoint. Attach a connection to send its "
                    "secret as a bearer token. The escape hatch for a service "
                    "Compass has no connector for.",
        config_schema=_HTTP_SCHEMA,
        connection_kind="",
        inputs=(Port("in"),),
        outputs=(Port("out", "json", "Response"),),
        requires="network",
        concurrency_safe=True,
    ))
    return types


def kinds() -> list[dict[str, str]]:
    """The connection kinds these need, for the Connections panel."""
    return [{"kind": c.kind, "label": c.label, "auth": c.auth, "note": c.note}
            for c in CONNECTORS]
