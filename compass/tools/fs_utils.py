"""Workspace path containment shared by all filesystem tools."""

from __future__ import annotations

from pathlib import Path

from compass.config import get_settings


class WorkspaceEscapeError(Exception):
    pass


def resolve_in_workspace(raw_path: str, root: Path | None = None) -> Path:
    """Resolve a model-supplied path and refuse anything that escapes the
    workspace root — the trust boundary every file tool shares. `root` is the
    session's selected workspace; falls back to the global workspace."""
    root = (root or get_settings().workspace_root).resolve()
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise WorkspaceEscapeError(
            f"path {raw_path!r} resolves outside the workspace root {root}"
        )
    return resolved
