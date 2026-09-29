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
import os
import shutil
import logging
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

from compass.common.config import get_settings
from compass.common.tools.shell_runtime import cwd_tracking_argv

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


def _sandboxing() -> bool:
    """Whether to put a boundary round the next command.

    Reads the setting each time rather than caching it, so turning the sandbox
    on does not need a restart — and refuses outright when it is configured as
    a security gate and the platform cannot provide one.
    """
    from compass.common.config import get_settings

    from compass.common import sandbox

    cfg = get_settings().sandbox
    # A mission overrides the setting: unattended work is exactly the case the
    # boundary exists for, so "somebody turned it off globally" is not a
    # reason to run a six-hour build without one.
    if not cfg.enabled and not sandbox.is_required():
        return False

    state = sandbox.availability()
    if not state.ok:
        if cfg.fail_if_unavailable:
            raise RuntimeError(
                "COMPASS_SANDBOX_STRICT is set and no sandbox is available: "
                + state.reason)
        sandbox.warn_once()
        return False
    return True


#: How often to look at the ports while a command is still running. Slow
#: enough that `lsof` costs nothing over a long build, quick enough that a
#: server which grabs Compass's port is stopped before anyone notices.
PORT_WATCH_SECONDS = 4.0


async def _watch_ports(notes: list[str]) -> None:
    """Stop anything taking Compass's own port, while the command still runs.

    Only does anything inside a reaping scope, and `sweep` itself only ever
    touches process groups that scope started. Runs `lsof` off the event loop
    so a slow call cannot stall the output the command is streaming.
    """
    from compass.common.tools import reaper

    if not reaper.active():
        return
    try:
        while True:
            await asyncio.sleep(PORT_WATCH_SECONDS)
            said = await asyncio.to_thread(reaper.sweep)
            if said:
                notes.append(said)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a watchdog must not fail a command
        logger.exception("port watch failed")


def _kill(proc) -> None:
    """End a command, and what it started along with it.

    `proc.kill()` signals the one process Compass spawned. A shell script that
    launched a server and then hung is the common case here, and killing only
    the shell leaves the server running — which is how an unattended build
    ends up with a web server still bound to a port hours later. When the
    command has its own process group, the group is what gets signalled.
    """
    import os
    import signal

    from compass.common.tools import reaper

    if reaper.active() and proc.pid:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            pass        # fall through: better to kill the one than neither
    try:
        proc.kill()
    except ProcessLookupError:
        pass


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
        # A directory of this command's own, for the cwd note below and for
        # anything the command itself puts in TMPDIR. Its own, because the
        # sandbox makes it writable and the shared temp tree holds every
        # other application's caches.
        run_temp = Path(tempfile.mkdtemp(prefix="compass-cmd-"))
        cwd_file = run_temp / "cwd"

        # Wrap the command so it preserves its own exit code and records the
        # final cwd — cross-platform (bash on macOS/Linux & Git Bash, cmd.exe as
        # a Windows fallback). macOS/Linux behaviour is unchanged.
        argv = cwd_tracking_argv(command, cwd_file)

        # The one place Compass starts a process, and therefore the one place
        # a boundary has to be applied: everything the agent runs — builds,
        # tests, background tasks — arrives here. Unsandboxed installs get
        # their argv back unchanged.
        policy = None
        profile_path = None
        env = {**os.environ, "TMPDIR": str(run_temp)}
        if _sandboxing():
            from compass.common import sandbox

            # Anchored to the session's root rather than to `cwd`: a policy
            # rebuilt from the current directory widens itself the moment a
            # command does `cd ..`, which is the one thing a boundary must
            # not do.
            policy = sandbox.policy_for(self.state.root or cwd, run_temp)
            argv, profile_path = sandbox.wrap(argv, policy)

        # Its own process group when somebody is going to have to clean up
        # after it. That makes the child a group leader, so whatever it starts
        # — a dev server, a watcher, a server's own reload child — is
        # reachable from one id and can be stopped together. Outside a reaping
        # scope this is off and the behaviour is exactly what it always was.
        from compass.common.tools import reaper

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=reaper.active(),
        )
        reaper.track(proc.pid)

        chunks: list[str] = []
        total = 0
        timed_out = False
        aborted = False
        # Watch the ports for as long as the command runs, not only after it.
        # Measured, not supposed: a mission ran `uvicorn main:app --reload` in
        # the foreground, which blocks for the whole command timeout, so the
        # check that happens when a command *returns* never got a turn and the
        # mission's server sat on Compass's port for the rest of the session.
        # A server started this way is the normal way to start one.
        port_notes: list[str] = []
        watch = asyncio.create_task(_watch_ports(port_notes))
        try:
            async with asyncio.timeout(timeout):
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    if abort.is_set():
                        _kill(proc)
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
            _kill(proc)
            await proc.wait()
            timed_out = True
        except asyncio.CancelledError:
            # The turn was torn down under us — the model call failed, the
            # client went away, the session was aborted. Measured, not
            # supposed: a mission session died on a rate limit while a command
            # was still running, nothing killed the command, and its server
            # was still holding Compass's port nineteen hours later. A
            # cancelled command has to take its process group with it; the
            # alternative is a process nobody owns and nobody will stop.
            _kill(proc)
            raise
        finally:
            watch.cancel()

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
            shutil.rmtree(run_temp, ignore_errors=True)
        except OSError:
            pass

        try:
            if profile_path:
                profile_path.unlink(missing_ok=True)
        except OSError:
            pass

        output = "".join(chunks)
        if total > MAX_OUTPUT_CHARS:
            output += f"\n[... output truncated at {MAX_OUTPUT_CHARS} chars ...]"
        exit_code = proc.returncode if proc.returncode is not None else -1
        if policy is not None:
            # A denial reported as "Operation not permitted" and nothing else
            # gets retried with sudo. Named, it gets retried somewhere the
            # command is allowed to write.
            from compass.common import sandbox

            output += sandbox.explain(output, policy, exit_code=exit_code)
        # A command that left a server on Compass's own port is stopped now
        # rather than at the end of the session: for the length of a session,
        # that port being taken means Compass is unreachable to everybody.
        # `port_notes` carries anything the watcher caught while the command
        # was still running, which is the case a check here cannot see.
        output += "".join(port_notes) + reaper.sweep()
        return ShellResult(
            exit_code=exit_code,
            output=output,
            timed_out=timed_out,
            aborted=aborted,
        )
