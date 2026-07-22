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
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group, so stop() kills children
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
        code = await proc.wait()
        # A user stop() sets status to "stopped" first; don't overwrite it.
        if task.status == "running":
            task.status = "finished" if code == 0 else "error"
            task.exit_code = code
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

    async def stop(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status != "running" or not task._proc:
            return False
        import contextlib
        import os
        import signal

        task.status = "stopped"
        task.finished_at = time.time()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(task._proc.pid), signal.SIGTERM)
        return True

    async def clear_finished(self) -> int:
        removed = [tid for tid, t in self._tasks.items() if t.status != "running"]
        for tid in removed:
            self._tasks.pop(tid, None)
        return len(removed)


# Module-level singleton — one registry per server process.
registry = BackgroundTaskRegistry()
