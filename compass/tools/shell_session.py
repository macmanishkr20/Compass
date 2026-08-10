"""Persistent shell session — port of utils/Shell.ts's cwd-tracking model.

Claude Code does NOT keep a long-lived shell process. It spawns a fresh child
per command but appends `pwd -P` to a temp file, then reads that file back and
updates a tracked working directory. So `cd` persists across separate Bash
calls, while each command stays its own isolated process — which is also why
concurrent read-only commands can't corrupt each other's state.

This module reproduces that exactly:
  * per-session tracked `cwd` (persists across turns; seeded at workspace root)
  * each command runs as `bash -c` with cwd set to the tracked directory
  * the command's real exit code is preserved (`exit $__ec`)
  * the post-command working directory is captured and written back, so a
    `cd` in one call is visible to the next
  * cwd recovery when the tracked directory is deleted out from under us

Matching the original, environment mutations (`export`) do NOT persist across
calls — only the working directory does.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

from compass.config import get_settings
from compass.tools.shell_runtime import cwd_tracking_argv

logger = logging.getLogger("compass.shell")

MAX_OUTPUT_CHARS = 60_000


@dataclass
class ShellState:
    """Per-session shell working directory. Owned by the Session, passed into
    each turn's ToolUseContext so cwd survives across turns. `root` is the
    session's workspace (bash starts there and can never be recovered below
    it); empty means the global workspace."""

    cwd: str = ""
    root: str = ""

    def _base(self) -> str:
        return self.root or str(get_settings().workspace_root)

    def resolved_cwd(self) -> str:
        base = self._base()
        if not self.cwd:
            self.cwd = base
        # Recover if the tracked directory was deleted (e.g. a command removed
        # its own cwd) — same fallback the original performs before spawning.
        if not Path(self.cwd).is_dir():
            logger.warning("shell cwd %r gone, recovering to %s", self.cwd, base)
            self.cwd = base
        return self.cwd


@dataclass
class ShellResult:
    exit_code: int
    output: str
    timed_out: bool = False
    aborted: bool = False


@dataclass
class ShellSession:
    state: ShellState = field(default_factory=ShellState)

    async def run(
        self,
        command: str,
        *,
        timeout: float,
        abort: asyncio.Event,
        on_output: Callable[[str], None] | None = None,
    ) -> ShellResult:
        cwd = self.state.resolved_cwd()
        cwd_file = Path(tempfile.gettempdir()) / f"compass-cwd-{uuid.uuid4().hex}"

        # Wrap the command so it preserves its own exit code and records the
        # final cwd — cross-platform (bash on macOS/Linux & Git Bash, cmd.exe as
        # a Windows fallback). macOS/Linux behaviour is unchanged.
        argv = cwd_tracking_argv(command, cwd_file)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        chunks: list[str] = []
        total = 0
        timed_out = False
        aborted = False
        try:
            async with asyncio.timeout(timeout):
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    if abort.is_set():
                        proc.kill()
                        aborted = True
                        break
                    text = raw.decode(errors="replace")
                    total += len(text)
                    if total <= MAX_OUTPUT_CHARS:
                        chunks.append(text)
                        if on_output:
                            on_output(text)
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            timed_out = True

        # Update the tracked cwd from what the command left us in — but only on
        # a clean finish. A killed command's cwd capture is unreliable.
        if not timed_out and not aborted:
            try:
                new_cwd = cwd_file.read_text().strip()
                if new_cwd and Path(new_cwd).is_dir():
                    self.state.cwd = new_cwd
            except OSError:
                pass
        try:
            cwd_file.unlink(missing_ok=True)
        except OSError:
            pass

        output = "".join(chunks)
        if total > MAX_OUTPUT_CHARS:
            output += f"\n[... output truncated at {MAX_OUTPUT_CHARS} chars ...]"
        return ShellResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            output=output,
            timed_out=timed_out,
            aborted=aborted,
        )
