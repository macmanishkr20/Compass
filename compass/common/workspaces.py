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

from compass.common.config import get_settings

logger = logging.getLogger("compass.workspaces")

DEFAULT_ID = "default"


class UnknownWorkspace(ValueError):
    """A workspace id that was given and does not resolve to a folder.

    Raised rather than quietly falling back. `resolve_root` used to answer the
    server's own `workspace_root` for *any* id it could not find, which meant a
    stale id — a workspace somebody had deleted, a second tab holding the old
    selection, a typo — silently redirected file reads, the working diff, and
    an agent's bash and write tools onto whatever repo Compass itself is
    running from. Falling back is not needed for the honest case either: the
    default workspace has its own id, is always registered, and resolves the
    ordinary way."""


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
        """Absolute path for a session's workspace.

        No id means the default workspace, which is what an empty selection
        has always meant. An id that *was* given and does not resolve raises
        `UnknownWorkspace`: answering with a different directory than the one
        that was asked for is worse than answering with an error, because
        nothing downstream can tell the difference — the files come back, the
        diff comes back, and bash runs somewhere nobody chose.
        """
        if not workspace_id:
            return get_settings().workspace_root
        ws = await self.get(workspace_id)
        if ws is None:
            raise UnknownWorkspace(f"unknown workspace: {workspace_id}")
        root = Path(ws.path)
        if not root.is_dir():
            # Registered, but the folder has since been moved or deleted.
            raise UnknownWorkspace(f"workspace folder is missing: {ws.path}")
        return root.resolve()

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


def git_discard(root: Path) -> dict:
    """Throw away every uncommitted change in the working tree.

    `checkout -- .` restores tracked files; `clean -fd` removes files and
    directories git has never seen. Both are needed — either alone leaves half
    the mess behind, and a person who asked for a clean tree and got a
    half-clean one is worse off than before, because they now trust it.

    Committed history is untouched: this reverts *to* HEAD, never past it.
    Nothing here is recoverable through git, which is why the surface asks
    first and reports what it removed.
    """
    import subprocess

    summary = git_summary(root)
    files = summary.get("files_changed", 0) + summary.get("untracked", 0)
    try:
        for args in (["checkout", "--", "."], ["clean", "-fd"]):
            done = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, timeout=20
            )
            if done.returncode != 0:
                return {"ok": False, "detail": (done.stderr or "git failed").strip()[:300]}
    except (OSError, subprocess.TimeoutExpired) as err:
        return {"ok": False, "detail": str(err)[:300]}
    return {"ok": True, "discarded": files}


def _compare_url(root: Path, branch: str) -> str:
    """github.com/owner/repo/compare/<branch>?expand=1 for manual PR creation."""
    remote = _origin_url(root)
    base = remote[:-4] if remote.endswith(".git") else remote
    return f"{base}/compare/{branch}?expand=1" if base else ""


def create_pull_request(
    root: Path,
    *,
    draft: bool = False,
    manual: bool = False,
    title: str = "",
    body: str = "",
) -> dict:
    """Push the current branch, then either open a PR with the GitHub CLI
    (optionally as a draft) or, for `manual`, return the GitHub compare URL so
    the user fills it in themselves. `title`/`body` are what the user wrote in
    the review dialog; when both are blank `gh --fill` writes them from the
    commits, which is what every caller used to get. Raises RuntimeError on
    failure."""
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
    cmd = [gh, "pr", "create", "--head", branch]
    if title.strip():
        # --title/--body and --fill are mutually exclusive in gh; a title with
        # no body still needs a body flag or gh opens an editor and hangs.
        cmd += ["--title", title.strip(), "--body", body]
    else:
        cmd.append("--fill")
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
        # Not _run(["explorer", p]): explorer.exe exits 1 even when it opens the
        # window, so its exit code cannot tell success from failure, and _run
        # reported every reveal on Windows as an error. os.startfile goes
        # through ShellExecute instead, which raises on a real failure (folder
        # gone, access denied) and returns quietly when the window opens.
        import os

        try:
            os.startfile(p)  # type: ignore[attr-defined]
        except OSError as err:
            raise RuntimeError(f"Could not open File Explorer: {err}")
        return f"Explorer: {p}"
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


# The Windows folder dialog. Everything in here answers one report — "Browse
# does nothing on Windows" — whose usual cause is that the dialog did open, but
# behind the browser: a FolderBrowserDialog shown from a background process with
# no owner window does not come to the front, and the request simply waited.
# A transparent, top-most owner form, shown and activated first, is what makes
# the dialog appear above everything else.
_WINDOWS_PICKER = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = 'None'
$owner.Opacity = 0
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Show()
$owner.Activate()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select a folder for Compass'
$dialog.ShowNewFolderButton = $true
try {
  if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.SelectedPath)
  }
} finally {
  $dialog.Dispose()
  $owner.Close()
  $owner.Dispose()
}
"""


def _windows_powershell() -> str:
    """Absolute path to a PowerShell that can load WinForms. The in-box one
    lives at a fixed place under SystemRoot, which is looked at first rather
    than trusting PATH: a server started from some shells or IDEs does not have
    System32 on it, and a bare "powershell" then failed with nothing shown."""
    import os
    import shutil

    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    inbox = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(inbox):
        return inbox
    for name in ("powershell", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "Could not find PowerShell, which Compass uses to show the Windows "
        "folder picker. Paste the folder path into the box instead."
    )


def _powershell_error(stderr: str) -> str:
    """The first line of what went wrong, readable. PowerShell writes errors to
    a redirected stderr as CLIXML, so the raw stream is XML, not a message."""
    import html
    import re

    text = (stderr or "").strip()
    if text.startswith("#< CLIXML"):
        text = "".join(re.findall(r'<S S="Error">(.*?)</S>', text, re.S))
        text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), text)
        text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else ""


def choose_folder() -> str:
    """Show the host's native folder chooser and return the selected absolute
    path (empty string if cancelled). Works when the backend shares the user's
    desktop session (the normal local-app setup) — macOS Finder, the Windows
    folder dialog, or zenity on Linux.

    Raises RuntimeError only when no picker is available on the platform."""
    import platform
    import shutil
    import subprocess

    system = platform.system()
    if system == "Darwin":
        out = subprocess.run(
            [
                "osascript",
                "-e", 'tell application "System Events" to activate',
                "-e", 'POSIX path of (choose folder with prompt "Select a folder for Compass")',
            ],
            capture_output=True, text=True, timeout=300,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    if system == "Windows":  # pragma: no cover - platform-specific
        import base64

        exe = _windows_powershell()
        # -EncodedCommand rather than -Command: the script travels as base64
        # UTF-16, so no quote or semicolon in it can be re-split by the Windows
        # command line on the way to PowerShell.
        encoded = base64.b64encode(_WINDOWS_PICKER.encode("utf-16-le")).decode("ascii")
        try:
            out = subprocess.run(
                [exe, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", encoded],
                capture_output=True,
                # The script writes UTF-8; decoding with the ANSI code page
                # (what text=True did) mangled any path outside it — "Résumé",
                # or a user folder in a non-Latin script.
                encoding="utf-8", errors="replace",
                timeout=300,
                # No console window flashing up behind the dialog. This only
                # suppresses the console; the WinForms dialog still shows.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "The folder window was open for five minutes without a choice, "
                "so Compass closed it. Click Browse to try again."
            )
        except OSError as err:
            raise RuntimeError(f"Could not start the Windows folder picker: {err}")
        if out.returncode != 0:
            reason = _powershell_error(out.stderr) or f"exit code {out.returncode}"
            raise RuntimeError(f"The Windows folder picker failed: {reason}")
        return out.stdout.strip()
    # Linux: zenity if present.
    if shutil.which("zenity"):  # pragma: no cover - platform-specific
        out = subprocess.run(
            ["zenity", "--file-selection", "--directory",
             "--title=Select a folder for Compass"],
            capture_output=True, text=True, timeout=300,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    raise RuntimeError("no native folder picker available on this host")


_registry: WorkspaceRegistry | None = None


def get_workspace_registry() -> WorkspaceRegistry:
    global _registry
    if _registry is None:
        _registry = WorkspaceRegistry()
    return _registry
