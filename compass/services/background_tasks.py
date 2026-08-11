"""Background tasks — long-running processes the agent spawns and forgets.

The analog of Claude Code's "Background tasks" panel: when the agent runs a dev
server (or any long-lived command) with `run_in_background`, we spawn it
detached, keep a handle, and surface it as a task the user can watch and stop.
A task is Running until its process exits, then Finished (with an exit code) or
Stopped (if the user killed it). State is in-memory — processes die with the
server anyway, so there is nothing to persist.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Status = Literal["running", "finished", "stopped", "error"]

# `--port 4310`, `-p 8000`, `PORT=65092`, `:3000`, `localhost:5173`
_PORT_RE = re.compile(
    r"(?:--port[=\s]+|-p[=\s]+|PORT[=\s]+|localhost:|127\.0\.0\.1:|:)(\d{2,5})\b"
)
# Bare port as the last arg of a well-known dev server, e.g.
# `python -m http.server 8899`, `manage.py runserver 8000`, `serve -l 3000`.
_BARE_PORT_RE = re.compile(
    r"\b(?:http\.server|runserver|serve|serve\.py|php\s+-S\s+\S*?)[=:\s]+(\d{2,5})\b"
)

MAX_LOG_LINES = 400

# Long-lived dev/servers that must never run in the foreground — doing so blocks
# the whole turn until the command timeout (up to 10 min). Matched at the START
# of each chained segment (after stripping `cd`/env prefixes) so a build/test or
# a git message that merely mentions a server word is NOT misclassified.
_SERVER_START_RE = re.compile(
    r"""(?xi)^(
        ng\s+serve | ng\s+s\b |
        (npm|pnpm|yarn|bun)\s+(run\s+)?(dev|start|serve|preview|watch)\b |
        next\s+(dev|start)\b | nuxt\s+(dev|start)\b | remix\s+dev\b |
        (npx\s+)?vite(?!\s+build)\b | (npx\s+)?astro\s+dev\b |
        webpack(-dev-server|\s+serve)\b | nodemon\b |
        (npx\s+)?(http-server|serve|live-server)\b |
        python[0-9.]*\s+-m\s+(http\.server|uvicorn|gunicorn|hypercorn|daphne|flask|streamlit|gradio)\b |
        (python[0-9.]*\s+)?manage\.py\s+runserver\b |
        streamlit\s+run\b |
        uvicorn\b | gunicorn\b | hypercorn\b | daphne\b |
        flask\s+run\b | fastapi\s+dev\b |
        rails\s+(s|server)\b | php\s+-S\b | dotnet\s+(run|watch)\b |
        jekyll\s+serve\b | hugo\s+server\b
    )"""
)


def looks_like_server(command: str) -> bool:
    """True when a chained command starts a long-lived dev server, so the bash
    tool can auto-detach it instead of blocking the turn."""
    for seg in re.split(r"&&|\|\||;|\n", command):
        s = seg.strip()
        s = re.sub(r"^cd\s+\S+\s+", "", s)  # drop a leading `cd path`
        s = re.sub(r"^([A-Za-z_][A-Za-z0-9_]*=\S+\s+)+", "", s)  # drop env prefixes
        if _SERVER_START_RE.match(s):
            return True
    return False


@dataclass
class BackgroundTask:
    id: str
    name: str
    command: str
    status: Status = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    url: str | None = None
    workspace_id: str | None = None
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _log: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES), repr=False)
    _produced: int = 0  # total lines ever appended (survives the deque cap)
    _read_cursor: int = 0  # index up to which bash_output has already returned

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": int(((self.finished_at or time.time()) - self.started_at) * 1000),
            "exit_code": self.exit_code,
            "url": self.url,
            "workspace_id": self.workspace_id,
        }


class BackgroundTaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _guess_url(command: str) -> str | None:
        m = _PORT_RE.search(command) or _BARE_PORT_RE.search(command)
        return f"http://localhost:{m.group(1)}" if m else None

    async def start(
        self,
        *,
        name: str,
        command: str,
        cwd: Path | str,
        workspace_id: str | None = None,
    ) -> BackgroundTask:
        task = BackgroundTask(
            id=uuid.uuid4().hex[:8],
            name=name.strip() or "background task",
            command=command,
            url=self._guess_url(command),
            workspace_id=workspace_id,
        )
        # Own process group so stop() can kill the whole tree (a dev server and
        # its children). POSIX uses a new session; Windows uses a new process
        # group. macOS/Linux behaviour is unchanged.
        from compass.tools.shell_runtime import shell_argv

        group_kwargs: dict = {}
        if sys.platform == "win32":  # pragma: no cover - platform-specific
            group_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            group_kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *shell_argv(command, login=True),
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **group_kwargs,
        )
        task._proc = proc
        async with self._lock:
            self._tasks[task.id] = task
        asyncio.create_task(self._monitor(task))
        return task

    async def _monitor(self, task: BackgroundTask) -> None:
        proc = task._proc
        assert proc is not None
        if proc.stdout is not None:
            async for raw in proc.stdout:
                task._log.append(raw.decode(errors="replace").rstrip("\n"))
                task._produced += 1
        code = await proc.wait()
        # A user stop() sets status to "stopped" first; don't overwrite it.
        if task.status == "running":
            task.status = "finished" if code == 0 else "error"
            task.exit_code = code
            task.finished_at = time.time()

    def register_browser(
        self, session_id: str, url: str, *, workspace_id: str | None = None
    ) -> BackgroundTask:
        """Register/refresh the agent's live browser session as a visible entry
        (no subprocess) so the Background tasks panel shows it with an
        Open/Preview URL — like claude.ai's browser session. Keyed per session."""
        tid = f"browser-{session_id}"
        task = self._tasks.get(tid)
        if task is None:
            task = BackgroundTask(
                id=tid,
                name="Agent browser",
                command=url,
                url=url,
                workspace_id=workspace_id,
            )
            self._tasks[tid] = task
        else:
            task.url = url
            task.command = url
            task.status = "running"
            task.finished_at = None
        return task

    def end_browser(self, session_id: str) -> None:
        task = self._tasks.get(f"browser-{session_id}")
        if task and task.status == "running":
            task.status = "finished"
            task.finished_at = time.time()

    def list(self) -> list[BackgroundTask]:
        # Running first, then most-recently-finished.
        return sorted(
            self._tasks.values(),
            key=lambda t: (t.status != "running", -(t.finished_at or t.started_at)),
        )

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def logs(self, task_id: str) -> list[str]:
        t = self._tasks.get(task_id)
        return list(t._log) if t else []

    def read_new(self, task_id: str) -> list[str]:
        """Lines produced since the last read_new() for this task (the analog of
        Claude Code's BashOutput). Handles the deque's line-cap: if old lines
        were dropped, it returns from the oldest still-buffered line."""
        t = self._tasks.get(task_id)
        if t is None:
            return []
        buffered = list(t._log)
        buffer_start = t._produced - len(buffered)  # abs index of buffered[0]
        start = max(t._read_cursor, buffer_start)
        new = buffered[start - buffer_start:] if start < t._produced else []
        t._read_cursor = t._produced
        return new

    async def stop(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status != "running":
            return False
        import contextlib
        import os
        import signal

        task.status = "stopped"
        task.finished_at = time.time()

        # Browser sessions have no subprocess — close the Playwright session.
        if task._proc is None:
            if task.id.startswith("browser-"):
                with contextlib.suppress(Exception):
                    from compass.services.agent_browser import close_agent_browser

                    await close_agent_browser(task.id[len("browser-"):])
            return True

        pid = task._proc.pid
        if sys.platform == "win32":  # pragma: no cover - platform-specific
            # taskkill /T ends the whole process tree (the dev server + children).
            with contextlib.suppress(Exception):
                await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True

    async def clear_finished(self) -> int:
        removed = [tid for tid, t in self._tasks.items() if t.status != "running"]
        for tid in removed:
            self._tasks.pop(tid, None)
        return len(removed)


# Module-level singleton — one registry per server process.
registry = BackgroundTaskRegistry()
