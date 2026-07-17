"""Workspace registry — the set of directories the agent can operate in.

A *workspace* is a root folder: the built-in Compass repo ("default"), a
local folder the user points at, or a GitHub repo the app cloned. Each session
targets one workspace; its file tools and shell are scoped to that root, so
"edit code and commit" works against whichever project is selected.

Stored as a single JSON registry (data/workspaces.json), mirroring the session
metadata store. Paths are absolute and validated on read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from compass.config import get_settings

logger = logging.getLogger("compass.workspaces")

DEFAULT_ID = "default"


@dataclass
class Workspace:
    id: str
    name: str
    path: str
    kind: str = "local"  # "local" | "github"
    remote_url: str = ""  # github html/clone url (token stripped)
    branch: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        p = Path(self.path)
        d["exists"] = p.is_dir()
        d["is_git"] = (p / ".git").exists()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Workspace":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


class WorkspaceRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cache: dict[str, Workspace] | None = None

    def _path(self) -> Path:
        return get_settings().workspace_root / get_settings().data_dir / "workspaces.json"

    def _default(self) -> Workspace:
        root = get_settings().workspace_root
        return Workspace(
            id=DEFAULT_ID, name="Compass (this repo)", path=str(root), kind="local"
        )

    def _load(self) -> dict[str, Workspace]:
        if self._cache is not None:
            return self._cache
        data: dict[str, Workspace] = {DEFAULT_ID: self._default()}
        path = self._path()
        if path.is_file():
            try:
                raw = json.loads(path.read_text())
                for wid, d in raw.items():
                    data[wid] = Workspace.from_dict({**d, "id": wid})
            except (OSError, json.JSONDecodeError) as err:
                logger.error("could not read workspaces.json: %s", err)
        self._cache = data
        return data

    def _flush(self) -> None:
        assert self._cache is not None
        # Never persist the built-in default; it's derived from settings.
        payload = {
            wid: w.to_dict()
            for wid, w in self._cache.items()
            if wid != DEFAULT_ID
        }
        for w in payload.values():
            w.pop("exists", None)
            w.pop("is_git", None)
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))

    async def list(self) -> list[Workspace]:
        async with self._lock:
            return list(self._load().values())

    async def get(self, workspace_id: str) -> Workspace | None:
        async with self._lock:
            return self._load().get(workspace_id)

    async def resolve_root(self, workspace_id: str | None) -> Path:
        """Absolute path for a session's workspace, falling back to default."""
        if workspace_id:
            ws = await self.get(workspace_id)
            if ws and Path(ws.path).is_dir():
                return Path(ws.path).resolve()
        return get_settings().workspace_root

    async def add_local(self, path: str, name: str | None = None) -> Workspace:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {resolved}")
        ws = Workspace(
            id=str(uuid.uuid4())[:8],
            name=name or resolved.name,
            path=str(resolved),
            kind="local",
            branch=_current_branch(resolved),
            remote_url=_origin_url(resolved),
        )
        async with self._lock:
            self._load()[ws.id] = ws
            self._flush()
        return ws

    async def create_folder(self, name: str) -> Workspace:
        safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip() or "project"
        dest = get_settings().workspaces_dir / safe
        dest.mkdir(parents=True, exist_ok=True)
        return await self.add_local(str(dest), name=safe)

    async def register_clone(
        self, name: str, path: Path, remote_url: str, branch: str
    ) -> Workspace:
        ws = Workspace(
            id=str(uuid.uuid4())[:8],
            name=name,
            path=str(path),
            kind="github",
            remote_url=remote_url,
            branch=branch,
        )
        async with self._lock:
            self._load()[ws.id] = ws
            self._flush()
        return ws

    async def delete(self, workspace_id: str) -> None:
        if workspace_id == DEFAULT_ID:
            raise ValueError("cannot remove the default workspace")
        async with self._lock:
            if self._load().pop(workspace_id, None) is not None:
                self._flush()


def _run_git(args: list[str], cwd: Path) -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=8
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _current_branch(path: Path) -> str:
    return _run_git(["branch", "--show-current"], path)


def _origin_url(path: Path) -> str:
    url = _run_git(["remote", "get-url", "origin"], path)
    return _strip_token(url)


def _strip_token(url: str) -> str:
    # https://x-access-token:TOKEN@github.com/o/r.git -> https://github.com/o/r.git
    import re

    return re.sub(r"https://[^@/]+@", "https://", url)


def git_summary(root: Path) -> dict:
    """Working-tree summary for the composer status bar: branch, diff stats
    (added/removed lines vs HEAD), commits ahead of upstream, and dirtiness."""
    branch = _current_branch(root)
    remote = _origin_url(root)
    added = removed = files = 0
    numstat = _run_git(["diff", "HEAD", "--numstat"], root)
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files += 1
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                removed += int(parts[1])
    untracked = [
        f
        for f in _run_git(
            ["ls-files", "--others", "--exclude-standard"], root
        ).splitlines()
        if f
    ]
    ahead = _run_git(["rev-list", "--count", "@{u}..HEAD"], root)
    return {
        "branch": branch,
        "remote": remote,
        "is_git": bool(branch or remote),
        "added": added,
        "removed": removed,
        "files_changed": files + len(untracked),
        "untracked": len(untracked),
        "ahead": int(ahead) if ahead.isdigit() else 0,
        "dirty": bool(numstat or untracked),
    }


def git_diff(root: Path) -> str:
    """Full unified diff of the working tree vs HEAD (staged + unstaged)."""
    return _run_git(["diff", "HEAD"], root)


def _compare_url(root: Path, branch: str) -> str:
    """github.com/owner/repo/compare/<branch>?expand=1 for manual PR creation."""
    remote = _origin_url(root)
    base = remote[:-4] if remote.endswith(".git") else remote
    return f"{base}/compare/{branch}?expand=1" if base else ""


def create_pull_request(root: Path, *, draft: bool = False, manual: bool = False) -> dict:
    """Push the current branch, then either open a PR with the GitHub CLI
    (optionally as a draft) or, for `manual`, return the GitHub compare URL so
    the user fills it in themselves. Raises RuntimeError on failure."""
    import shutil
    import subprocess

    branch = _current_branch(root)
    if not branch:
        raise RuntimeError("Not on a git branch")
    if branch in ("main", "master"):
        raise RuntimeError(
            f"You're on '{branch}'. Create a feature branch before opening a PR."
        )

    push = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if push.returncode != 0:
        raise RuntimeError("git push failed: " + (push.stderr or push.stdout).strip())

    if manual:
        url = _compare_url(root, branch)
        if not url:
            raise RuntimeError("No GitHub remote to open a compare page for.")
        return {"url": url, "branch": branch, "existing": False, "manual": True}

    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("GitHub CLI ('gh') not found on the server host")
    cmd = [gh, "pr", "create", "--fill", "--head", branch]
    if draft:
        cmd.append("--draft")
    pr = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    out = (pr.stdout or "").strip()
    if pr.returncode != 0:
        # `gh` prints the existing PR URL to stderr when one already exists.
        err = (pr.stderr or "").strip()
        url = next(
            (w for w in (out + " " + err).split() if w.startswith("http")), ""
        )
        if url:
            return {"url": url, "branch": branch, "existing": True}
        raise RuntimeError(err or "gh pr create failed")
    url = out.splitlines()[-1] if out else ""
    return {"url": url, "branch": branch, "existing": False}


def _run(cmd: list[str]) -> None:
    import subprocess

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise RuntimeError(str(err))
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip())


def reveal_in_file_manager(path: Path) -> str:
    """Reveal the folder in the host's file manager and bring it to the front.
    On macOS uses AppleScript (`activate`), which surfaces the window reliably
    even from a background server process — plain `open` often does not."""
    import platform

    p = str(path)
    system = platform.system()
    if system == "Darwin":
        _run(
            [
                "osascript",
                "-e",
                f'tell application "Finder" to reveal (POSIX file "{p}" as alias)',
                "-e",
                'tell application "Finder" to activate',
            ]
        )
        return f"Finder: {p}"
    if system == "Windows":  # pragma: no cover
        _run(["explorer", p])
    else:
        _run(["xdg-open", p])
    return p


def open_in_terminal(path: Path) -> str:
    """Open a terminal at the folder and bring it to the front."""
    import platform

    p = str(path)
    system = platform.system()
    if system == "Darwin":
        _run(
            [
                "osascript",
                "-e",
                f'tell application "Terminal" to do script "cd " & quoted form of "{p}"',
                "-e",
                'tell application "Terminal" to activate',
            ]
        )
        return f"Terminal: {p}"
    if system == "Windows":  # pragma: no cover
        _run(["cmd", "/c", "start", "cmd", "/K", f"cd /d {p}"])
    else:
        _run(["x-terminal-emulator", "--working-directory", p])
    return p


def open_in_vscode(path: Path) -> str:
    """Launch VS Code on `path` from the host running this backend — the same
    thing the Claude Code CLI does. Only meaningful when the backend and the
    user's VS Code are on the same machine (the normal local setup). Returns the
    command used; raises RuntimeError if VS Code can't be found/launched."""
    import platform
    import shutil
    import subprocess

    target = str(path)
    system = platform.system()

    # Preferred: the `code` CLI on PATH (works on all platforms when installed).
    code = shutil.which("code")
    candidates: list[list[str]] = []
    if code:
        candidates.append([code, target])
    if system == "Darwin":
        # macOS: fall back to the app bundle even if `code` isn't on PATH.
        candidates.append(["open", "-b", "com.microsoft.VSCode", target])
        candidates.append(["open", "-a", "Visual Studio Code", target])
    elif system == "Windows":  # pragma: no cover - platform-specific
        candidates.append(["cmd", "/c", "code", target])

    last_err = "VS Code CLI ('code') not found on PATH"
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                return " ".join(cmd)
            last_err = (proc.stderr or proc.stdout or "").strip() or last_err
        except (OSError, subprocess.TimeoutExpired) as err:
            last_err = str(err)
    raise RuntimeError(last_err)


_registry: WorkspaceRegistry | None = None


def get_workspace_registry() -> WorkspaceRegistry:
    global _registry
    if _registry is None:
        _registry = WorkspaceRegistry()
    return _registry
