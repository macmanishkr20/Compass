"""Resolving a path inside a folder, without letting it escape.

Used by the workspace file browser and by a design project's own files.
The names keep their leading underscore: they were private to server.py
and renaming them here would be a change for its own sake.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".angular", "dist", ".next"}


def _safe_join(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, refusing anything that escapes the workspace."""
    target = (root / rel).resolve() if rel else root.resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes the workspace")
    return target
