"""What was made, who made it, and where the bytes are.

The bytes of a picture live in blob storage; this is the row that says what
they are. Without it the only record of a generated image is the URL sitting
inside a message's text, which is enough to show it again and not enough for
anything else: you cannot list what a person has made, cannot say which
conversation a picture came from, cannot tell a 2MB poster from a thumbnail
without fetching it, and cannot clean up after a deleted thread.

One row per thing, in the shared `catalog` container, partitioned by owner —
which is the shape of the question being asked of it. "Everything I have
made" is a single-partition read; "everything in this conversation" filters
that by session. Both work from any browser on any machine, because none of
it is on the machine that made it.

Recording is best-effort and never blocks. A picture whose row failed to
write is still drawn, still stored and still shown in the turn that asked
for it; the row is how it is found *later*, and losing it must not cost the
thing itself.
"""

from __future__ import annotations

import logging
import time
import uuid

from compass.common.persistence.catalog import Collection

logger = logging.getLogger("compass.media")

#: One row per stored artefact. In the shared catalog container rather than
#: one of its own: these are small records, read a page at a time, which is
#: exactly what that container is for.
_media = Collection("media", "media.json", shape="list")


async def record(
    *,
    blob_name: str,
    url: str,
    kind: str,
    session_id: str = "",
    owner: str = "",
    prompt: str = "",
    width: int = 0,
    height: int = 0,
    size_bytes: int = 0,
    source: str = "",
) -> str:
    """Write down one stored artefact. Returns its row id, or "" if it failed.

    `source` names the picture this one was edited from, so a chain of edits
    can be followed back to what it started as.
    """
    row = {
        "id": uuid.uuid4().hex,
        "blob_name": blob_name,
        "url": url,
        # "image" | "edit" | "video" | "screenshot" | "upload"
        "kind": kind,
        "session_id": session_id,
        "owner": owner,
        # Truncated: this is a label, not the brief. The brief that produced
        # it is in the conversation, which is where it belongs.
        "prompt": (prompt or "")[:500],
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "source": source,
        "created_at": time.time(),
    }
    try:
        await _media.put(row)
        return row["id"]
    except Exception as err:  # noqa: BLE001 — the artefact itself is fine
        logger.warning("could not record %s: %s", blob_name, err)
        return ""


async def for_owner(owner: str, *, session_id: str = "") -> list[dict]:
    """Everything this person has made, newest first; one conversation's
    worth when `session_id` is given."""
    rows = [r for r in await _media.all() if (r.get("owner") or "") == (owner or "")]
    if session_id:
        rows = [r for r in rows if r.get("session_id") == session_id]
    rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return rows


async def for_session(session_id: str) -> list[dict]:
    """Everything made in one conversation, newest first.

    Across owners deliberately: a conversation has one owner, and a row
    written before ownership was recorded has none — filtering by owner here
    would hide exactly the oldest things somebody is looking for.
    """
    rows = [r for r in await _media.all() if r.get("session_id") == session_id]
    rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return rows


async def forget_session(session_id: str) -> int:
    """Drop the rows for a deleted conversation. The bytes are removed by
    whoever owns them; this is the index catching up."""
    gone = 0
    for row in await for_session(session_id):
        try:
            if await _media.remove(str(row["id"])):
                gone += 1
        except Exception:  # noqa: BLE001 — best-effort, like the rest
            logger.debug("could not drop media row %s", row.get("id"))
    return gone
