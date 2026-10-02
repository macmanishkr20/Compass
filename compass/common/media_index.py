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

    One row per blob, enforced here. Drawn pictures and screenshots each mint
    a fresh name, so for those it never bites; a thread's uploads do not —
    the path is the session and the filename, and `home.media.upload` writes
    them through again whenever the thread is saved. Called twice for the same
    bytes, this keeps the original row and its id and updates it in place,
    which is also how a file uploaded before anyone was recorded gets its
    owner the next time round.
    """
    existing = None
    try:
        for candidate in await _media.all():
            if candidate.get("blob_name") == blob_name:
                existing = candidate
                break
    except Exception:  # noqa: BLE001 — unreadable index: write and move on
        logger.debug("could not check the index for %s", blob_name)

    row = {
        "id": existing.get("id") if existing else uuid.uuid4().hex,
        "blob_name": blob_name,
        "url": url,
        # "image" | "edit" | "video" | "screenshot" | "upload"
        "kind": kind,
        # A later write that does not know who it is for must not erase a
        # row that does. Identity is only ever filled in, never blanked.
        "session_id": session_id or (existing.get("session_id", "") if existing else ""),
        "owner": owner or (existing.get("owner", "") if existing else ""),
        # Truncated: this is a label, not the brief. The brief that produced
        # it is in the conversation, which is where it belongs.
        "prompt": (prompt or "")[:500],
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "source": source,
        # When it first appeared, not when it was last written through.
        "created_at": (existing.get("created_at") if existing else None) or time.time(),
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


async def owner_of(blob_name: str) -> str | None:
    """Who the bytes at `blob_name` belong to, or None if nothing is on record.

    None and "" are different answers and the caller must treat them so. ""
    is a row that exists and names nobody — legacy, from before owners were
    written down. None is no row at all, which is the same thing a typo gets.

    A scan rather than a lookup, because the index is partitioned by owner and
    the owner is the question. It is cheap anyway: `all()` is held in memory
    per event loop and refreshed only when something is written, so this costs
    one dictionary comparison per row after the first call.
    """
    for row in await _media.all():
        if row.get("blob_name") == blob_name:
            return str(row.get("owner") or "")
    return None


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
