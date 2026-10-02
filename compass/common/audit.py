"""What was deleted, by whom, and what went with it.

Deleting in Compass means deleting: the record leaves Cosmos, the bytes leave
blob storage, the working copy leaves the disk. That is the behaviour people
ask for and it is the behaviour they get. It also means that afterwards there
is nothing at all to say it ever existed — which is fine until somebody asks
why a conversation is missing, or whether a design was removed before or
after a review, or what a departing colleague took with them.

So a note is kept. Not the content: a conversation's text, a design's markup
and a drawn picture are all genuinely gone. What survives is the shape of the
event — who deleted what, from which module, which conversation it belonged
to, when, and a tally of what was removed alongside it. Enough to answer "was
this deleted, and by whom", and not enough to reconstruct anything.

The notes expire on their own. Each row carries a `ttl` and Cosmos drops it
when it elapses, so retention is a property of the data rather than a sweep
somebody has to remember to run. `COMPASS_AUDIT_RETENTION_MONTHS` sets it —
three months by default, `0` to keep them indefinitely.

Writing one must never cost a deletion. Every function here swallows its own
failures: a person who asked for something to be deleted has it deleted, and
an audit store that is unreachable is a problem for the next log line, not
for them.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from compass.common.config import get_settings

logger = logging.getLogger("compass.audit")

#: Average, not calendar. Retention here is "about three months", and a
#: number of seconds is what Cosmos wants; pretending to land on the same day
#: of the month would be false precision for a log that expires rows lazily
#: anyway.
_SECONDS_PER_MONTH = 30.44 * 24 * 60 * 60


def retention_seconds() -> int:
    """How long a note is kept, in seconds. 0 means forever."""
    months = get_settings().storage.audit_retention_months
    return int(months * _SECONDS_PER_MONTH) if months and months > 0 else 0


def _local_path():
    settings = get_settings()
    folder = settings.workspace_root / settings.data_dir
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "audit.jsonl"


async def _container():
    from compass.common.persistence.catalog import _container as container_for

    name = get_settings().storage.cosmos_audit_container
    # -1 enables expiry without imposing one, so each row's own `ttl` decides.
    return await container_for(name, "/owner", default_ttl=-1)


def _uses_cosmos() -> bool:
    cfg = get_settings().storage
    return cfg.backend == "cosmos" and cfg.cosmos_configured


async def note_deletion(
    *,
    module: str,
    kind: str,
    record_id: str,
    owner: str,
    session_id: str = "",
    title: str = "",
    removed: dict[str, Any] | None = None,
) -> str:
    """Write down one deletion. Returns the note's id, or "" if it failed.

    `module` is the part of Compass it happened in — home, code, design,
    pipelines, estimate, missions. `kind` is what sort of thing went, since
    one module deletes several ("project", "page", "system"). `removed` is
    the tally of everything that went with it: blobs, index rows, child
    records. `title` is the human label the thing had, kept because an id
    alone answers nothing three months later.
    """
    ttl = retention_seconds()
    note = {
        "id": uuid.uuid4().hex,
        "owner": owner or "",
        "module": module,
        "kind": kind,
        "record_id": record_id,
        "session_id": session_id or "",
        # A label, deliberately short. Not the content — see the module note.
        "title": (title or "")[:200],
        "removed": removed or {},
        "deleted_at": time.time(),
    }
    if ttl:
        note["ttl"] = ttl
    try:
        if _uses_cosmos():
            container = await _container()
            await container.upsert_item(note)
        else:
            # Local installs get the same record in a file. No expiry here:
            # nothing sweeps it, and silently dropping somebody's audit trail
            # would be worse than a file that grows slowly.
            with _local_path().open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(note) + "\n")
        return str(note["id"])
    except Exception as err:  # noqa: BLE001 — the deletion itself succeeded
        logger.warning(
            "could not record the deletion of %s %s: %s", kind, record_id, err
        )
        return ""


async def for_owner(owner: str, *, limit: int = 200) -> list[dict]:
    """This person's deletions, newest first."""
    rows: list[dict] = []
    try:
        if _uses_cosmos():
            container = await _container()
            items = container.query_items(
                query="SELECT * FROM c WHERE c.owner=@owner",
                parameters=[{"name": "@owner", "value": owner or ""}],
            )
            async for row in items:
                rows.append(row)
        else:
            path = _local_path()
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (row.get("owner") or "") == (owner or ""):
                        rows.append(row)
    except Exception as err:  # noqa: BLE001
        logger.warning("could not read the audit log: %s", err)
        return []
    rows.sort(key=lambda r: r.get("deleted_at", 0), reverse=True)
    return rows[:limit]
