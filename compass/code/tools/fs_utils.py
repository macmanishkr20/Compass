"""Workspace path containment shared by all filesystem tools."""

from __future__ import annotations

from pathlib import Path

from compass.common.config import get_settings


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
        # Same refusal either way — a link out of the workspace is an escape
        # whether or not its target happens to exist, and following one is how
        # a read of a repository becomes a read of /etc. Only the explanation
        # differs, because the two are not the same thing to whoever reads it:
        # a repository full of symlinks to a checkout nobody has cloned is
        # ordinary, and reporting that as "resolves outside the workspace"
        # reads as a security refusal and invites a retry that cannot work.
        if path.is_symlink():
            raise WorkspaceEscapeError(
                f"path {raw_path!r} is a symlink pointing outside the "
                f"workspace"
                + ("; its target does not exist" if not path.exists() else "")
            )
        raise WorkspaceEscapeError(
            f"path {raw_path!r} resolves outside the workspace root {root}"
        )
    return resolved
