"""SQL Server, as node types.

Separate from `connectors.py` because the shape is genuinely different: those
are declared HTTP requests sharing one handler, and this is a driver
handshake, a cursor, and a transaction. Forcing a database through the HTTP
connector's declaration would have meant a `base_url` that is not a URL and an
`auth` that is not a header.

The design decision that matters here is parameterisation, and it is worth
being explicit because getting it wrong is the difference between a working
integration and a hole. Compass resolves expressions in a node's settings
*before* the handler runs. So a query field containing
`@nodes('mail').data.subject` arrives at this module as a string with the
subject already substituted into it — and a subject reading
`'; DROP TABLE invoices; --` would arrive substituted too.

Two fields, therefore, never one. `query` is the SQL and is meant to be
static, with `?` where a value goes. `parameters` is a list, and that is where
expressions belong; the values are handed to the driver separately and never
become SQL text. `Insert` takes a table and an object of columns to values and
builds the statement itself, quoting identifiers and binding every value, so
the common case needs no placeholder discipline from the author at all.

The driver is imported lazily and is optional. `pyodbc` needs Microsoft's ODBC
driver installed on the host, which is a real piece of setup, and a Compass
that refuses to start because a database nobody is using has no driver would
be worse than one that says so when the step runs. Mock mode never touches the
driver, so a graph containing these nodes can be built, dry run and shown
working on a machine with no database at all — which is the whole point of the
sample data these types declare.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from compass.pipelines.types import NodeContext, NodeResult, NodeType, Port

logger = logging.getLogger("compass.pipelines")

#: How long a statement may take. Past this a query is a problem for a person,
#: not something to keep a run open for.
TIMEOUT_S = 60

#: Rows returned to the run. A SELECT with no TOP against a real table will
#: happily return millions; the run log and every downstream node would carry
#: all of them, so it is capped and the cap is reported rather than silent.
MAX_ROWS = 1_000

#: A SQL identifier Compass is willing to build a statement out of. Anything
#: else has to go through `query`, where the author is stating that they know
#: what they are writing.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(raw: str, what: str) -> str:
    """A table or column name, or a refusal.

    Bracket-quoting alone is not enough — `[a]]b]` closes the quote — so the
    name is validated against a conservative pattern first and quoted second.
    A name this rejects is not a name Compass will assemble SQL from; it can
    still be used in `query`, written by hand, where the author is the one
    making the claim.
    """
    parts = raw.strip().split(".")
    if not parts or not all(_IDENT.match(p or "") for p in parts):
        raise RuntimeError(
            f"{what} {raw!r} is not a plain identifier. Use letters, digits "
            "and underscores (optionally schema.table), or write the "
            "statement yourself in a Query step."
        )
    return ".".join(f"[{p}]" for p in parts)


def _connect(conn: dict[str, Any]):
    """Open a connection, or say exactly what is missing.

    Driver errors are the single most common failure with SQL Server and the
    least self-explanatory, so they are translated once here rather than
    reaching a run log as a stack trace nobody can act on.
    """
    try:
        import pyodbc  # noqa: PLC0415 — optional, and only when a node runs
    except ImportError as err:
        raise RuntimeError(
            "SQL Server steps need the `pyodbc` package and Microsoft's ODBC "
            "driver. Install the driver for your platform, then "
            "`pip install pyodbc`. Until then this pipeline still runs in "
            "mock mode, which returns sample rows and connects to nothing."
        ) from err

    host = str(conn.get("host") or "").strip()
    database = str(conn.get("database") or "").strip()
    if not host or not database:
        raise RuntimeError(
            "This SQL Server connection has no server or database set. Open "
            "it on the Connections panel and fill both in."
        )
    port = str(conn.get("port") or "1433").strip()
    encrypt = "yes" if str(conn.get("encrypt", "yes")).lower() not in (
        "no", "false", "0") else "no"

    parts = [
        "DRIVER={ODBC Driver 18 for SQL Server}",
        f"SERVER={host},{port}",
        f"DATABASE={database}",
        f"UID={conn.get('user', '')}",
        f"PWD={conn.get('password', '')}",
        f"Encrypt={encrypt}",
        "TrustServerCertificate=yes" if encrypt == "no" else "",
        f"Connection Timeout={TIMEOUT_S}",
    ]
    try:
        return pyodbc.connect(";".join(p for p in parts if p), timeout=TIMEOUT_S)
    except Exception as err:  # noqa: BLE001 — every driver failure is one answer
        raise RuntimeError(f"Could not connect to {host}: {err}") from err


async def _credential(ctx: NodeContext) -> dict[str, Any]:
    if ctx.connection is None:
        raise RuntimeError("This step needs a connection and none was given.")
    conn = await ctx.connection("")
    if not conn:
        raise RuntimeError(
            "No connection chosen. Pick a SQL Server connection in this "
            "step's settings, or run the pipeline in mock mode to see it work "
            "without a database."
        )
    return conn


def _rows(cursor) -> tuple[list[dict[str, Any]], bool]:
    """Rows as dicts, capped. Returns the rows and whether more were left."""
    if cursor.description is None:
        return [], False
    columns = [c[0] for c in cursor.description]
    out: list[dict[str, Any]] = []
    truncated = False
    for row in cursor:
        if len(out) >= MAX_ROWS:
            truncated = True
            break
        # Dates, decimals and UUIDs do not survive JSON; the run is persisted,
        # so anything not a primitive is stringified here rather than blowing
        # up when the run is written.
        out.append({
            name: value if isinstance(value, (str, int, float, bool, type(None)))
            else str(value)
            for name, value in zip(columns, row)
        })
    return out, truncated


async def _run(sql: str, params: list[Any], conn: dict[str, Any], *,
               commit: bool) -> tuple[list[dict[str, Any]], int, bool]:
    """One statement, in a thread — the driver is blocking."""
    import asyncio

    def work():
        connection = _connect(conn)
        try:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            rows, truncated = _rows(cursor)
            affected = cursor.rowcount
            if commit:
                connection.commit()
            return rows, affected, truncated
        finally:
            connection.close()

    return await asyncio.to_thread(work)


async def test(values: dict[str, Any]) -> tuple[bool, str]:
    """Prove a SQL Server credential by opening a connection and running
    SELECT 1.

    The HTTP credential test cannot serve this one — there is no URL to GET —
    so the route dispatches here. Same contract: never raises, and the
    driver's own message is passed through because it is the actionable one
    ("Login failed for user" and "server was not found" need different fixes
    and only the driver knows which happened).
    """
    import asyncio

    def work() -> None:
        connection = _connect(values)
        try:
            connection.cursor().execute("SELECT 1")
        finally:
            connection.close()

    try:
        await asyncio.to_thread(work)
    except RuntimeError as err:  # our own translated errors, already readable
        return False, str(err)
    except Exception as err:  # noqa: BLE001
        return False, f"SQL Server refused the connection: {err}"
    return True, "SQL Server answered. The connection works."


# --------------------------------------------------------------------------- handlers


async def _query(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    conn = await _credential(ctx)
    sql = str(config.get("query") or "").strip()
    if not sql:
        raise RuntimeError("This step has no SQL to run.")
    params = config.get("parameters") or []
    if not isinstance(params, list):
        raise RuntimeError("Parameters must be a list, one per ? in the query.")
    placeholders = sql.count("?")
    if placeholders != len(params):
        raise RuntimeError(
            f"The query has {placeholders} placeholder(s) and "
            f"{len(params)} parameter(s). They have to match, or the driver "
            "will bind the wrong values to the wrong columns."
        )

    rows, affected, truncated = await _run(sql, list(params), conn, commit=True)
    data: dict[str, Any] = {
        "rows": rows, "items": rows, "count": len(rows),
        "affected": affected,
    }
    if truncated:
        data["truncated"] = True
    summary = f"{len(rows)} row(s)" if rows else f"{max(affected, 0)} row(s) affected"
    if truncated:
        summary += f" (capped at {MAX_ROWS})"
    return NodeResult(data=data, text=summary)


async def _insert(config: dict[str, Any], ctx: NodeContext) -> NodeResult:
    conn = await _credential(ctx)
    table = _identifier(str(config.get("table") or ""), "Table")
    values = config.get("values")
    if not isinstance(values, dict) or not values:
        raise RuntimeError(
            "Insert needs a Values object — column names to the values to "
            "write, where an expression like @nodes('x').data.subject is the "
            "value rather than part of the SQL."
        )
    columns = [_identifier(name, "Column") for name in values]
    marks = ", ".join("?" for _ in values)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"
    rows, affected, _ = await _run(sql, list(values.values()), conn, commit=True)
    return NodeResult(
        data={"affected": max(affected, 0), "table": config.get("table", "")},
        text=f"Inserted into {config.get('table', '')}",
    )


# --------------------------------------------------------------------------- types


_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "title": "Query",
            "description": "The SQL to run. Put ? where a value goes and list "
                           "the values under Parameters — a value pasted into "
                           "the SQL itself is how an injection happens.",
        },
        "parameters": {
            "type": "array",
            "title": "Parameters",
            "description": "One per ? in the query, in order. Expressions "
                           "belong here.",
            "items": {"type": "string"},
        },
    },
    "required": ["query"],
}

_INSERT_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {
            "type": "string",
            "title": "Table",
            "description": "Table name, or schema.table.",
        },
        "values": {
            "type": "object",
            "title": "Values",
            "description": "Column name to value. Every value is bound as a "
                           "parameter, so expressions are safe here.",
        },
    },
    "required": ["table", "values"],
}


def provider() -> list[NodeType]:
    return [
        NodeType(
            id="mssql.query",
            label="SQL Server · query",
            category="connector",
            handler=_query,
            description="Run SQL against SQL Server. Rows come back on "
                        "`rows` and on `items`, so a For each can be wired "
                        "straight onto the result.",
            config_schema=_QUERY_SCHEMA,
            connection_kind="mssql",
            inputs=(Port("in"),),
            outputs=(Port("out", "items", "Rows"),),
            requires="network",
            concurrency_safe=False,
            sample={"rows": [
                {"id": 41, "client": "Northwind Trading",
                 "project": "Aurora Mobile Redesign", "budget": 85000,
                 "deadline": "2026-09-06"},
                {"id": 42, "client": "Confident Meadows",
                 "project": "Billing portal", "budget": 24000,
                 "deadline": "2026-10-01"},
            ], "items": [
                {"id": 41, "client": "Northwind Trading",
                 "project": "Aurora Mobile Redesign", "budget": 85000,
                 "deadline": "2026-09-06"},
                {"id": 42, "client": "Confident Meadows",
                 "project": "Billing portal", "budget": 24000,
                 "deadline": "2026-10-01"},
            ], "count": 2, "affected": -1},
        ),
        NodeType(
            id="mssql.insert",
            label="SQL Server · insert",
            category="connector",
            handler=_insert,
            description="Insert one row. Columns and values are given "
                        "separately and every value is bound as a parameter, "
                        "so an expression can be a value safely.",
            config_schema=_INSERT_SCHEMA,
            connection_kind="mssql",
            inputs=(Port("in"),),
            outputs=(Port("out", "json", "Result"),),
            requires="network",
            concurrency_safe=False,
            sample={"affected": 1, "table": "dbo.projects", "simulated": True},
        ),
    ]
