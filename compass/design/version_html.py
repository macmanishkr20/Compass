"""Where a design version's HTML lives.

A version is a snapshot of a rendered design, and the snapshots are the bulk
of everything here: measured on this install, 21.8MB of a 24.8MB store, where
the projects themselves are 1.4MB. One project had 25 versions of 125KB each
and came to 3.3MB on its own — which matters because Cosmos refuses an item
over 2MB, so five of thirty-seven projects could not have been stored at all.

So the snapshot goes to blob storage and the project keeps the note: id, when,
and what it was called. That is what the versions list shows anyway — the API
strips `html` from it explicitly — and the only thing that ever wants the
markup back is a restore, which wants exactly one.

Local disk when blob is not configured, on the same terms as the artifact
store: a zero-config install should still work, and it is the same directory
the designs already use.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from compass.common.config import get_settings

logger = logging.getLogger("compass.design")

#: Marks a version whose markup is stored away rather than inline. Kept as a
#: field of its own rather than a sentinel inside `html`, so a version that
#: genuinely has no markup and one whose markup is elsewhere are different
#: things — the first is empty, the second is a fetch.
SPILLED = "html_ref"

_service = None


def _blob_enabled() -> bool:
    return bool(get_settings().storage.blob_connection_string)


def _local_dir() -> Path:
    settings = get_settings()
    d = settings.workspace_root / settings.data_dir / "design_versions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ref(project_id: str, version_id: str) -> str:
    return f"{project_id}/{version_id}.html"


def _container():
    global _service
    from azure.storage.blob import BlobServiceClient

    cfg = get_settings().storage
    if _service is None:
        _service = BlobServiceClient.from_connection_string(
            cfg.blob_connection_string
        )
    client = _service.get_container_client("compass-design")
    try:
        client.create_container()
    except Exception:  # noqa: BLE001 — already there is the normal case
        pass
    return client


def _put_sync(ref: str, html: str) -> None:
    _container().upload_blob(ref, html.encode("utf-8"), overwrite=True)


def _get_sync(ref: str) -> str:
    return _container().download_blob(ref).readall().decode("utf-8", "replace")


async def put(project_id: str, version_id: str, html: str) -> str | None:
    """Store one version's markup. Returns its reference, or None on failure.

    None rather than an exception: a snapshot that could not be filed is a
    lost undo step, not a lost design, and it must not take the save of the
    design itself down with it.
    """
    ref = _ref(project_id, version_id)
    if _blob_enabled():
        try:
            await asyncio.to_thread(_put_sync, ref, html)
            return ref
        except Exception as err:  # noqa: BLE001 — fall through to local disk
            logger.error("design version upload failed (%s); keeping it local", err)
    try:
        path = _local_dir() / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, html, "utf-8")
        return ref
    except OSError as err:
        logger.error("could not store design version %s: %s", ref, err)
        return None


async def get(ref: str) -> str:
    """One version's markup, or "" if it cannot be found.

    Tries blob and local disk regardless of which is configured now: an
    install that was local yesterday and is on blob today still has yesterday
    's snapshots on disk, and a version that cannot be read should come back
    empty rather than break the page that asked for it.
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
        logger.warning("could not read design version %s: %s", ref, err)
    logger.warning("design version %s is missing", ref)
    return ""


async def spill(project_id: str, versions: list[dict]) -> list[dict]:
    """Replace inline markup with references, ready to store.

    A version that already carries a reference is left alone, so saving a
    project does not re-upload its whole history every time.
    """
    out: list[dict] = []
    for v in versions:
        v = dict(v)
        html = v.get("html")
        if v.get(SPILLED) or not html:
            v.pop("html", None)
            out.append(v)
            continue
        ref = await put(project_id, str(v.get("id", "")), html)
        if ref:
            v[SPILLED] = ref
            v.pop("html", None)
        out.append(v)
    return out


async def fill(version: dict) -> str:
    """One version's markup, wherever it is. Inline wins, because a project
    written before this existed still has it there."""
    if version.get("html"):
        return str(version["html"])
    ref = version.get(SPILLED)
    return await get(str(ref)) if ref else ""
