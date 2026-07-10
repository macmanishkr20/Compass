"""GitHub integration — list repos and clone them into managed workspaces.

Uses a personal access token (GITHUB_TOKEN, `repo` scope). Clones embed the
token in the `origin` remote so the agent's `git push` (run through the bash
tool) works without extra credential plumbing — appropriate for a local dev
tool. The token is stripped from any URL surfaced back to the UI.

Push/commit themselves are done by the model via the bash tool inside the
cloned workspace; this module only handles discovery and cloning.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from compass.config import get_settings
from compass.services.telemetry import log_event
from compass.services.workspaces import get_workspace_registry

logger = logging.getLogger("compass.github")


class GitHubDisabledError(RuntimeError):
    pass


@dataclass
class RepoInfo:
    full_name: str
    default_branch: str
    private: bool
    description: str
    updated_at: str
    html_url: str


def _require_token() -> str:
    token = get_settings().github.token
    if not token:
        raise GitHubDisabledError(
            "GitHub is not configured. Set GITHUB_TOKEN (repo scope) in .env."
        )
    return token


async def list_repos(limit: int = 100) -> list[RepoInfo]:
    import httpx

    token = _require_token()
    api = get_settings().github.api_url
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repos: list[RepoInfo] = []
    async with httpx.AsyncClient(timeout=20) as client:
        # sorted by most recently pushed; one page is plenty for a picker
        r = await client.get(
            f"{api}/user/repos",
            headers=headers,
            params={"per_page": min(limit, 100), "sort": "pushed", "affiliation": "owner,collaborator,organization_member"},
        )
        r.raise_for_status()
        for item in r.json():
            repos.append(
                RepoInfo(
                    full_name=item["full_name"],
                    default_branch=item.get("default_branch", "main"),
                    private=item.get("private", False),
                    description=item.get("description") or "",
                    updated_at=item.get("pushed_at") or item.get("updated_at") or "",
                    html_url=item.get("html_url", ""),
                )
            )
    log_event("github_list_repos", count=len(repos))
    return repos


def _authed_url(full_name: str) -> str:
    token = _require_token()
    return f"https://x-access-token:{token}@github.com/{full_name}.git"


async def clone_repo(full_name: str, branch: str | None = None):
    """Clone `owner/repo` into the managed workspaces dir and register it.
    Returns the created Workspace. Idempotent-ish: a name clash gets a suffix."""
    _require_token()
    base = get_settings().workspaces_dir
    base.mkdir(parents=True, exist_ok=True)
    short = full_name.split("/")[-1]
    dest = base / short
    n = 2
    while dest.exists():
        dest = base / f"{short}-{n}"
        n += 1

    args = ["git", "clone", "--depth", "1"]
    if branch:
        args += ["--branch", branch]
    args += [_authed_url(full_name), str(dest)]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    ok = proc.returncode == 0
    log_event("github_clone", repo=full_name, ok=ok)
    if not ok:
        # Never leak the token that's inside the clone URL.
        msg = out.decode(errors="replace").replace(_require_token(), "***")
        raise RuntimeError(f"git clone failed: {msg[-400:]}")

    # Identify the agent as a committer so commits succeed out of the box.
    for cfg in (
        ["config", "user.name", "Compass Agent"],
        ["config", "user.email", "compass-agent@users.noreply.github.com"],
    ):
        p = await asyncio.create_subprocess_exec(
            "git", "-C", str(dest), *cfg,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await p.wait()

    actual_branch = branch or _detect_branch(dest)
    return await get_workspace_registry().register_clone(
        name=short,
        path=dest,
        remote_url=f"https://github.com/{full_name}.git",
        branch=actual_branch,
    )


def _detect_branch(path: Path) -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "main"
    except (OSError, subprocess.TimeoutExpired):
        return "main"
