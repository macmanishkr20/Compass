"""Where a design's live markup lives.

The companion to `version_html`, which moved the *history* out of the project
document. This moves what is on the canvas now. Measured after that first
change, the markup was still almost all of what Cosmos held: of 3.17MB across
37 projects, `html` was 1.552MB and `pages` 1.485MB — 96% of the container was
documents, four of them with photographs base64'd into the markup.

A document should hold what a document is for. So the project keeps its name,
its pages' names and ids, its conversation, its prompt and the size of each
page; the markup itself goes to blob storage, beside the versions.

Two refs rather than one, because a project's `html` and its active page's
markup are allowed to differ: `PATCH /v1/design/projects/{id}` writes `html`
without touching `pages`, so deriving either from the other would quietly lose
whatever that endpoint had just saved.

Filling is lazy and per project. `DesignStore._read` reads every project, and
fetching 37 projects' markup to answer a question about one would be slower
than leaving it in the document ever was.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from compass.common.config import get_settings
from compass.common.persistence import blob

logger = logging.getLogger("compass.design")

#: Marks markup kept out of the document, on both a project and a page. A
#: field of its own rather than a sentinel inside `html`, so a page that is
#: genuinely blank and a page whose markup is elsewhere stay different things.
SPILLED = "html_ref"

#: How long the markup is, kept in the document because the list and the page
#: switcher want it and neither wants the markup. `list` draws a placeholder
#: tile for a project with nothing rendered; `pages` shows a size per page.
LENGTH = "chars"

#: Under its own prefix inside `compass-design`, where the versions already
#: live at `<project>/<version>.html`. A project id is twelve hex characters,
#: so it can never be the string "pages" and the two cannot collide.
PREFIX = "pages"

#: The ref for a project's `html`, as distinct from any one page's.
ACTIVE = "active"

#: Below this, markup stays in the document. A blank page's stub is 430 bytes,
#: and turning that into a request would cost more than it saves. Anything a
#: person has actually designed is far above it.
MIN_SPILL_BYTES = 4096

def _blob_enabled() -> bool:
    return blob.enabled()


def _local_dir() -> Path:
    settings = get_settings()
    d = settings.workspace_root / settings.data_dir / "design_pages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ref(project_id: str, page_id: str) -> str:
    return f"{PREFIX}/{project_id}/{page_id}.html"


def _container():
    return blob.container("compass-design")


def _put_sync(ref: str, html: str) -> None:
    _container().upload_blob(
        ref, html.encode("utf-8"), overwrite=True, max_concurrency=blob.CONCURRENCY
    )


def _get_sync(ref: str) -> str:
    return (
        _container()
        .download_blob(ref, max_concurrency=blob.CONCURRENCY)
        .readall()
        .decode("utf-8", "replace")
    )


async def put(project_id: str, page_id: str, html: str) -> str | None:
    """Store one page's markup. Returns its reference, or None on failure.

    None rather than an exception, and the caller then keeps the markup in the
    document: a design that could not be filed away is still a design, and
    must not be lost because blob storage was unreachable.
    """
    ref = _ref(project_id, page_id)
    if _blob_enabled():
        try:
            await asyncio.to_thread(_put_sync, ref, html)
            return ref
        except Exception as err:  # noqa: BLE001 — fall through to local disk
            logger.error("design markup upload failed (%s); keeping it local", err)
    try:
        path = _local_dir() / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, html, "utf-8")
        return ref
    except OSError as err:
        logger.error("could not store design markup %s: %s", ref, err)
        return None


async def get(ref: str) -> str:
    """One page's markup, or "" if it cannot be found.

    Tries blob and local disk regardless of which is configured now, for the
    same reason the versions do: an install that was local yesterday still has
    yesterday's pages on disk.
    """
    if _blob_enabled():
        try:
            return await asyncio.to_thread(_get_sync, ref)
        except Exception:  # noqa: BLE001 — may predate the move to blob
            pass
    try:
        path = _local_dir() / ref
        if path.is_file():
            return await asyncio.to_thread(path.read_text, "utf-8")
    except OSError as err:
        logger.warning("could not read design markup %s: %s", ref, err)
    logger.warning("design markup %s is missing", ref)
    return ""


async def _store_one(project_id: str, page_id: str, html: str) -> dict:
    """The fields that replace `html` for one page, or keep it."""
    if len(html.encode("utf-8", "ignore")) < MIN_SPILL_BYTES:
        return {"html": html, LENGTH: len(html)}
    ref = await put(project_id, page_id, html)
    if ref is None:
        return {"html": html, LENGTH: len(html)}
    return {SPILLED: ref, LENGTH: len(html)}


async def spill(row: dict) -> dict:
    """A project ready to store: its markup moved out, its shape unchanged.

    Every page at once rather than one after another — a project with eight
    pages would otherwise wait for eight uploads in series.
    """
    project_id = str(row.get("id", ""))
    if not project_id:
        return row
    out = dict(row)
    pages = [dict(p) for p in (row.get("pages") or [])]

    jobs = [_store_one(project_id, ACTIVE, str(row.get("html") or ""))]
    jobs += [
        _store_one(project_id, str(p.get("id") or f"p{i}"), str(p.get("html") or ""))
        for i, p in enumerate(pages)
    ]
    results = await asyncio.gather(*jobs)

    out.pop("html", None)
    out.pop(SPILLED, None)
    out.update(results[0])
    for page, stored in zip(pages, results[1:]):
        page.pop("html", None)
        page.pop(SPILLED, None)
        page.update(stored)
    out["pages"] = pages
    return out


async def fill(row: dict) -> dict:
    """A project with its markup back, wherever it is.

    Inline wins over a reference, because a project written before this
    existed still has its markup in the document, and because a page too small
    to file away keeps it there on purpose.
    """
    out = dict(row)
    pages = [dict(p) for p in (row.get("pages") or [])]

    async def one(holder: dict) -> str:
        if holder.get("html"):
            return str(holder["html"])
        ref = holder.get(SPILLED)
        return await get(str(ref)) if ref else ""

    htmls = await asyncio.gather(one(row), *(one(p) for p in pages))
    out["html"] = htmls[0]
    out.pop(SPILLED, None)
    for page, html in zip(pages, htmls[1:]):
        page["html"] = html
        page.pop(SPILLED, None)
    out["pages"] = pages
    return out


def length(holder: dict) -> int:
    """How long a project's or page's markup is, without fetching it."""
    if holder.get(LENGTH) is not None:
        return int(holder[LENGTH])
    return len(holder.get("html") or "")


async def drop(project_id: str, page_id: str) -> None:
    """Throw away one deleted page's markup. Best-effort, like `discard`."""
    ref = _ref(project_id, page_id)
    if _blob_enabled():
        try:
            await asyncio.to_thread(_container().delete_blob, ref)
        except Exception:  # noqa: BLE001 — never stored, or already gone
            pass
    try:
        path = _local_dir() / ref
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def discard(project_id: str) -> int:
    """Throw away a deleted project's markup. Never raises — a project the
    user deleted is deleted whether or not the cleanup succeeded."""
    removed = 0
    prefix = f"{PREFIX}/{project_id}/"
    if _blob_enabled():
        try:
            removed += await asyncio.to_thread(_discard_sync, prefix)
        except Exception as err:  # noqa: BLE001 — cleanup is best-effort
            logger.warning("could not clear markup for %s: %s", project_id, err)
    try:
        local = _local_dir() / PREFIX / project_id
        if local.is_dir():
            for path in local.glob("*.html"):
                path.unlink(missing_ok=True)
                removed += 1
            local.rmdir()
    except OSError as err:
        logger.warning("could not clear local markup for %s: %s", project_id, err)
    return removed


def _discard_sync(prefix: str) -> int:
    client = _container()
    names = [b.name for b in client.list_blobs(name_starts_with=prefix)]
    for name in names:
        client.delete_blob(name)
    return len(names)
