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

from compass.config import get_settings

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
    import mimetypes

    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return target.read_bytes(), media


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
