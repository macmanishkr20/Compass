"""Message content too big to store as a document.

Cosmos refuses an item over 2MB, and a handful of messages here are far past
it: measured on this install, one user message of 39MB and several over 20,
in transcripts whose other messages are a few kilobytes each. They are photo
and video attachments, inlined as base64 data URIs in the message content,
which is why a single turn can outweigh an entire conversation.

So an oversized message keeps its shape and loses its bulk: the content goes
to blob storage and the record carries a reference. Everything else about the
message — role, uuid, sequence, tool calls, metadata — stays in the document,
because those are what queries and ordering are built on.

Only the outliers move. A threshold rather than a rule for every message: a
transcript is read in full on every turn, and turning a few kilobytes of
conversation into a few hundred blob fetches would cost far more than it
saves. Measured across this install, 7 messages out of many thousands are
over the threshold.

Blob when it is configured, local disk otherwise, on the same terms as the
artifact store — a zero-config install must still work.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from compass.common.config import get_settings

logger = logging.getLogger("compass.persistence")

#: Past this, the content is stored away. Half the 2MB item limit, so the
#: rest of the record — tool calls, metadata, the wrapper Cosmos adds — has
#: room without anyone having to reason about how much.
MAX_INLINE_BYTES = 1024 * 1024

#: The field that replaces `content` when it has been moved. A field of its
#: own rather than a sentinel string inside `content`, so a message that is
#: genuinely empty and one whose content is elsewhere stay different things.
CONTENT_REF = "content_ref"

#: Everything written here sits under one prefix, because `compass-media` is
#: the container for media in general and a conversation's photos will land in
#: it too. Without the prefix, clearing a deleted conversation's message
#: content would mean deleting `<session>/*` — which would take the photos
#: with it.
PREFIX = "messages"

#: How a 20MB message is moved: in 1MB pieces, sixteen at a time. The default
#: is one request for the whole blob, and measured against this account that
#: ran at 0.30 MB/s — 57 seconds for one 17MB message, which is how the first
#: live read of a thread managed to time out on content that was sitting
#: there. The same blob in 1MB ranges, sixteen in flight, takes 7.1 seconds.
#: Measured, not assumed: 4MB/8-way came to 11.2s, so the small pieces win.
CHUNK_BYTES = 1024 * 1024
CONCURRENCY = 16

_service = None


def _blob_enabled() -> bool:
    return bool(get_settings().storage.blob_connection_string)


def _local_dir() -> Path:
    settings = get_settings()
    d = settings.workspace_root / settings.data_dir / "message_content"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _container():
    global _service
    from azure.storage.blob import BlobServiceClient

    cfg = get_settings().storage
    if _service is None:
        _service = BlobServiceClient.from_connection_string(
            cfg.blob_connection_string,
            max_single_get_size=CHUNK_BYTES,
            max_chunk_get_size=CHUNK_BYTES,
            max_single_put_size=CHUNK_BYTES,
            max_block_size=CHUNK_BYTES,
        )
    client = _service.get_container_client("compass-media")
    try:
        client.create_container()
    except Exception:  # noqa: BLE001 — already there is the normal case
        pass
    return client


def _put_sync(ref: str, payload: str) -> None:
    _container().upload_blob(
        ref, payload.encode("utf-8"), overwrite=True, max_concurrency=CONCURRENCY
    )


def _get_sync(ref: str) -> str:
    return (
        _container()
        .download_blob(ref, max_concurrency=CONCURRENCY)
        .readall()
        .decode("utf-8", "replace")
    )


async def spill(record: dict, session_id: str) -> dict:
    """A record safe to store: oversized content moved out, or unchanged.

    Returns the record as given when it is small enough, which is almost
    always. A failure to store the content leaves the record alone rather
    than writing one that points at nothing — a message that is too large to
    save is a problem, and a message that claims its content is somewhere it
    is not is a worse one.
    """
    content = record.get("content")
    if content is None:
        return record
    payload = content if isinstance(content, str) else json.dumps(content, default=str)
    if len(payload.encode("utf-8", "ignore")) <= MAX_INLINE_BYTES:
        return record

    ref = f"{PREFIX}/{session_id}/{record.get('uuid', 'unknown')}.json"
    body = json.dumps(
        {"raw": content if isinstance(content, str) else content,
         "was_string": isinstance(content, str)},
        default=str,
    )
    stored = False
    if _blob_enabled():
        try:
            await asyncio.to_thread(_put_sync, ref, body)
            stored = True
        except Exception as err:  # noqa: BLE001 — try local disk before giving up
            logger.error("large message upload failed (%s); keeping it local", err)
    if not stored:
        try:
            path = _local_dir() / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, body, "utf-8")
            stored = True
        except OSError as err:
            logger.error("could not store large message %s: %s", ref, err)
    if not stored:
        return record

    out = dict(record)
    out["content"] = None
    out[CONTENT_REF] = ref
    return out


async def _read_blob(ref: str) -> tuple[str, bool]:
    """(body, absent). `absent` is only true when the blob really is not there.

    The distinction is the point. A read that failed and a blob that is gone
    used to look the same here, and the caller said "no longer stored" for
    both — so a slow transfer was reported to the reader as lost data while
    the bytes sat in the container untouched. That is what happened on the
    first live read of a 24MB message.
    """
    from azure.core.exceptions import ResourceNotFoundError

    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return await asyncio.to_thread(_get_sync, ref), False
        except ResourceNotFoundError:
            return "", True
        except Exception as err:  # noqa: BLE001 — transport, timeout, throttle
            last = err
            if attempt == 1:
                logger.warning("re-reading large message %s after %s", ref, err)
    logger.error("could not read large message %s: %s", ref, last)
    return "", False


async def fill(record: dict) -> dict:
    """The record with its content back, if it was moved out."""
    ref = record.get(CONTENT_REF)
    if not ref:
        return record
    body = ""
    absent = True
    if _blob_enabled():
        body, absent = await _read_blob(str(ref))
    if not body:
        # Local disk regardless of which backend is configured now: an install
        # that was local yesterday still has yesterday's content on disk.
        try:
            path = _local_dir() / str(ref)
            if path.is_file():
                body = await asyncio.to_thread(path.read_text, "utf-8")
        except OSError as err:
            logger.warning("could not read local copy of %s: %s", ref, err)
    out = dict(record)
    out.pop(CONTENT_REF, None)
    if not body:
        # Said out loud in the transcript rather than returned as an empty
        # message: a turn that silently loses what somebody attached reads
        # as though they never attached it. Which of the two it is matters —
        # one is gone, the other is worth reloading the page for.
        if absent:
            logger.warning("large message content %s is missing", ref)
            out["content"] = "(this message's content is no longer stored)"
        else:
            out["content"] = "(this message's content could not be loaded — retry)"
        return out
    try:
        parsed = json.loads(body)
        out["content"] = parsed.get("raw")
    except (ValueError, AttributeError):
        out["content"] = body
    return out


async def fill_all(records: list[dict]) -> list[dict]:
    """Every record with its content back, fetched concurrently.

    Worth not doing one at a time: a thread here holds three stored messages
    totalling 67MB, and awaiting them in a loop makes the reader wait for the
    sum of three downloads rather than the longest. Order is preserved, which
    is the whole point of the sequence they were read in.

    Records that were never spilled cost nothing — `fill` returns them as they
    are without touching the network.
    """
    if not records:
        return []
    return list(await asyncio.gather(*(fill(r) for r in records)))


async def discard(session_id: str) -> int:
    """Throw away everything stored for a deleted conversation.

    Worth doing rather than leaving behind: the content moved out here is the
    large content, so a deleted conversation that leaves its attachments in
    place leaves nearly all of its bytes in place. Returns how many were
    removed. Never raises — a conversation the user deleted is deleted
    whether or not the cleanup succeeded.
    """
    removed = 0
    prefix = f"{PREFIX}/{session_id}/"
    if _blob_enabled():
        try:
            removed += await asyncio.to_thread(_discard_sync, prefix)
        except Exception as err:  # noqa: BLE001 — cleanup is best-effort
            logger.warning("could not clear stored content for %s: %s", session_id, err)
    try:
        local = _local_dir() / PREFIX / session_id
        if local.is_dir():
            for path in local.glob("*.json"):
                path.unlink(missing_ok=True)
                removed += 1
            local.rmdir()
    except OSError as err:
        logger.warning("could not clear local content for %s: %s", session_id, err)
    return removed


def _discard_sync(prefix: str) -> int:
    client = _container()
    names = [b.name for b in client.list_blobs(name_starts_with=prefix)]
    for name in names:
        client.delete_blob(name)
    return len(names)
