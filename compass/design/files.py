"""A design project's own files.

Claude Design gives a project a folder: assets it was given, scraps it made
along the way, uploads dropped onto it, and the pages themselves. This is that
folder — one directory per project under the data dir, with the three
subfolders created on first use so the browser has somewhere to put things.

Everything here refuses to leave the project's folder. The paths arrive from a
browser, so they are treated as hostile input, not as trusted names.
"""

from __future__ import annotations

import base64
import shutil
import time
from pathlib import Path

from compass.common.config import get_settings

# The folders a project starts with, in the order the browser lists them.
DEFAULT_FOLDERS = ("assets", "scraps", "uploads")

# What a file is called in the listing, by extension.
_KINDS = {
    ".html": "HTML page",
    ".htm": "HTML page",
    ".css": "Stylesheet",
    ".js": "Script",
    ".ts": "Script",
    ".json": "Data",
    ".md": "Document",
    ".txt": "Text",
    ".svg": "Vector image",
    ".png": "Image",
    ".jpg": "Image",
    ".jpeg": "Image",
    ".gif": "Image",
    ".webp": "Image",
    ".pdf": "PDF",
    ".zip": "Archive",
}

# Only these are ever returned as text; anything else is offered as a download.
_TEXTY = {
    ".html", ".htm", ".css", ".js", ".ts", ".json", ".md", ".txt", ".svg", ".csv",
}

_MAX_UPLOAD = 12 * 1024 * 1024   # 12 MB per file


def project_root(project_id: str) -> Path:
    """The project's folder, created with its starting subfolders."""
    settings = get_settings()
    root = settings.workspace_root / settings.data_dir / "design_files" / project_id
    for folder in DEFAULT_FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    return root


def resolve(project_id: str, rel: str) -> Path:
    """Resolve a path inside the project, refusing anything that escapes it."""
    root = project_root(project_id).resolve()
    target = (root / (rel or "")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes the project")
    return target


def kind_of(path: Path) -> str:
    if path.is_dir():
        return "Folder"
    return _KINDS.get(path.suffix.lower(), "File")


def listing(project_id: str, rel: str = "") -> dict:
    """One level of the project's folder, folders first."""
    target = resolve(project_id, rel)
    if not target.is_dir():
        raise FileNotFoundError(rel)

    root = project_root(project_id)
    folders: list[dict] = []
    files: list[dict] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entry = {
            "name": child.name,
            "path": str(child.relative_to(root)),
            "kind": kind_of(child),
            "size": 0 if child.is_dir() else stat.st_size,
            "at": stat.st_mtime,
            "text": child.suffix.lower() in _TEXTY,
        }
        (folders if child.is_dir() else files).append(entry)
    return {"path": rel, "folders": folders, "files": files}


def read_text(project_id: str, rel: str, limit: int = 200_000) -> str:
    target = resolve(project_id, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target.read_text(errors="replace")[:limit]


def read_bytes(project_id: str, rel: str) -> tuple[bytes, str]:
    target = resolve(project_id, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    # Stated, not guessed: `mimetypes` reads the Windows registry, so on a
    # Windows host the type a design file is served as depends on what is
    # installed there rather than on the file. See common.media_types.
    from compass.common.media_types import media_type_for

    return target.read_bytes(), media_type_for(target)


def write(project_id: str, rel: str, *, text: str = "", data_url: str = "") -> dict:
    """Store a file. Text arrives as text; anything else as a data: URL."""
    target = resolve(project_id, rel)
    if target.is_dir():
        raise IsADirectoryError(rel)
    target.parent.mkdir(parents=True, exist_ok=True)

    if data_url:
        head, _, payload = data_url.partition(",")
        if "base64" not in head:
            raise ValueError("only base64 data URLs are accepted")
        blob = base64.b64decode(payload)
        if len(blob) > _MAX_UPLOAD:
            raise ValueError("that file is larger than 12 MB")
        target.write_bytes(blob)
    else:
        if len(text) > _MAX_UPLOAD:
            raise ValueError("that file is larger than 12 MB")
        target.write_text(text)

    stat = target.stat()
    root = project_root(project_id)
    return {
        "name": target.name,
        "path": str(target.relative_to(root)),
        "kind": kind_of(target),
        "size": stat.st_size,
        "at": stat.st_mtime,
        "text": target.suffix.lower() in _TEXTY,
    }


def make_folder(project_id: str, rel: str) -> dict:
    target = resolve(project_id, rel)
    target.mkdir(parents=True, exist_ok=True)
    root = project_root(project_id)
    return {
        "name": target.name,
        "path": str(target.relative_to(root)),
        "kind": "Folder",
        "size": 0,
        "at": time.time(),
        "text": False,
    }


def remove(project_id: str, rel: str) -> bool:
    """Delete a file or an empty-or-not folder. The project's own three
    starting folders stay: emptying one is fine, losing it is confusing."""
    if not rel:
        return False
    target = resolve(project_id, rel)
    if not target.exists():
        return False
    if target.is_dir():
        if target.name in DEFAULT_FOLDERS and target.parent == project_root(project_id):
            for child in target.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            return True
        shutil.rmtree(target, ignore_errors=True)
        return True
    target.unlink(missing_ok=True)
    return True


def delete_project(project_id: str) -> None:
    """Drop the whole folder when its project goes."""
    settings = get_settings()
    root = settings.workspace_root / settings.data_dir / "design_files" / project_id
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
# Written through to blob storage.
#
# The folder above is a working copy, not the record. Everything else a
# design is made of — its markup, its history, its row — moved to the cloud;
# its own files did not, so a project opened on a second machine showed an
# empty Files pane and a design restored from Cosmos came back without the
# assets it was built from.
#
# The same arrangement `home.media` uses for a thread's uploads, and for the
# same reasons: the disk stays because reading and listing are synchronous
# and want real paths, and blob holds the copy that survives the machine.
# ══════════════════════════════════════════════════════════════════════════

import asyncio
import logging

from compass.common.persistence import blob

logger = logging.getLogger("compass.design.files")

#: Where a project's files live inside `compass-design`, beside the markup.
BLOB_PREFIX = "files"


def _blob_enabled() -> bool:
    return blob.enabled()


def _container():
    return blob.container("compass-design")


def _blob_name(project_id: str, rel: str) -> str:
    return f"{BLOB_PREFIX}/{project_id}/{rel}"


def _put_sync(name: str, path: Path) -> None:
    with path.open("rb") as handle:
        _container().upload_blob(
            name, handle, overwrite=True, max_concurrency=blob.CONCURRENCY
        )


def _get_sync(name: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("wb") as handle:
        _container().download_blob(name, max_concurrency=blob.CONCURRENCY).readinto(handle)
    tmp.replace(path)


def _names_sync(project_id: str) -> dict[str, int]:
    prefix = _blob_name(project_id, "")
    return {
        b.name[len(prefix):]: b.size
        for b in _container().list_blobs(name_starts_with=prefix)
        if b.name != prefix
    }


def _drop_sync(prefix: str) -> int:
    gone = 0
    for b in list(_container().list_blobs(name_starts_with=prefix)):
        try:
            _container().delete_blob(b.name)
            gone += 1
        except Exception:  # noqa: BLE001 — already gone counts as gone
            logger.debug("could not delete %s", b.name)
    return gone


async def store(project_id: str, rel: str) -> bool:
    """Write one file through to blob. Never raises: it is already on disk
    and already in the answer the browser is about to get."""
    if not _blob_enabled() or not rel:
        return False
    try:
        path = resolve(project_id, rel)
        if not path.is_file():
            return False
        await asyncio.to_thread(_put_sync, _blob_name(project_id, rel), path)
        return True
    except Exception as err:  # noqa: BLE001
        logger.warning("could not store %s/%s: %s", project_id, rel, err)
        return False


async def hydrate(project_id: str) -> int:
    """Make sure this project's files are on disk. Returns how many came down.

    Does nothing in the normal case, where they are already there — one
    listing of the prefix and no transfers. It earns its place on a second
    machine, a rebuilt container, or a disk that was cleared.

    Compared by size as well as presence, so a file that was truncated or
    half-written is fetched again rather than trusted.
    """
    if not _blob_enabled():
        return 0
    try:
        remote = await asyncio.to_thread(_names_sync, project_id)
    except Exception as err:  # noqa: BLE001 — offline is not a failed request
        logger.warning("could not list stored files for %s: %s", project_id, err)
        return 0
    if not remote:
        return 0
    root = project_root(project_id)

    async def one(rel: str, size: int) -> bool:
        path = root / rel
        try:
            if path.is_file() and path.stat().st_size == size:
                return False
            await asyncio.to_thread(_get_sync, _blob_name(project_id, rel), path)
            return True
        except Exception as err:  # noqa: BLE001
            logger.warning("could not fetch %s: %s", rel, err)
            return False

    done = await asyncio.gather(*(one(r, s) for r, s in remote.items()))
    return sum(done)


async def forget(project_id: str, rel: str = "") -> int:
    """Remove one file, or the whole project's files, from blob storage.

    Called beside the local delete rather than instead of it: a file that
    goes from the disk and stays in blob comes back on the next hydrate,
    which looks exactly like a delete that did not work.
    """
    if not _blob_enabled():
        return 0
    prefix = _blob_name(project_id, rel) if rel else _blob_name(project_id, "")
    try:
        return await asyncio.to_thread(_drop_sync, prefix)
    except Exception as err:  # noqa: BLE001
        logger.warning("could not drop stored files for %s: %s", project_id, err)
        return 0
