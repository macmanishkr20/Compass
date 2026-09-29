"""Processes a session started, and did not stop.

The sandbox bounds where a command may write. It says nothing about how long
what that command started stays alive, and for unattended work that turned out
to be the more expensive gap — measured twice on this install, not imagined:

  * a mission testing "the app picks the next free port when 8000 is busy"
    ran `python -m http.server 8000` to make 8000 busy, and left it running.
    It bound IPv6 `*:8000` while Compass held IPv4 `127.0.0.1:8000`, and since
    macOS resolves `localhost` to `::1` first, every request to Compass went
    to the mission's squatter instead. Compass was healthy and unreachable.
  * the same mission then left its own FastAPI app running on `0.0.0.0:8000`
    with `--reload` — on every network interface — for nineteen hours.

Neither is misbehaviour. A mission that builds a web app starts web servers;
that is the work. The defect is that nothing was responsible for stopping
them, because the thing that would normally stop them is a person closing a
terminal, and a mission has no person and no terminal.

Two mechanisms, deliberately different in strength:

  * **Reaping.** Inside a `reaping()` scope every command gets its own process
    group, and when the scope ends the whole group goes. Session-scoped rather
    than command-scoped on purpose: a mission has to be able to start a server
    in one command and curl it in the next, which is exactly what `init.sh`
    is for.
  * **Port protection.** Reaping still leaves a window — the length of the
    session — in which a command can bind the port Compass itself is serving
    on and make it unreachable. So after each command, anything we started
    that is holding one of our own ports is stopped immediately and the model
    is told why, in the same voice the sandbox uses to explain a denied write.

Only ever processes this module started: every kill is checked against the
group ids it recorded, and its own group is refused outright. A reaper that
could kill its parent is a worse bug than the one it fixes.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import signal
import subprocess
from contextvars import ContextVar

logger = logging.getLogger("compass.tools")

#: The process groups started inside the current scope. None outside one,
#: which is what "not reaping" means — the Code console has a person in it
#: who may well want the dev server they just started to keep running.
_groups: ContextVar[set[int] | None] = ContextVar("compass_reap_groups", default=None)

#: How long a process group gets to stop politely before it is killed.
GRACE_SECONDS = 3.0


def active() -> bool:
    """Whether commands should be started in their own process group."""
    return _groups.get() is not None


@contextlib.contextmanager
def reaping():
    """Inside this block, what a command starts does not outlive the block."""
    token = _groups.set(set())
    try:
        yield
    finally:
        groups = _groups.get() or set()
        _groups.reset(token)
        for pgid in sorted(groups):
            _stop_group(pgid, why="the session that started it has ended")


def track(pid: int | None) -> None:
    """Record a process group leader started inside the scope.

    The pid is the group id: commands are spawned with `start_new_session`,
    which makes the child the leader of a new group, so everything it goes on
    to start is reachable from this one number.
    """
    groups = _groups.get()
    if groups is None or not pid:
        return
    groups.add(pid)


# -- ports Compass is serving on -----------------------------------------
_own_ports: set[int] | None = None


def protected_ports() -> set[int]:
    """The ports this process is listening on.

    Read from the running process rather than from configuration, because
    configuration does not know: Compass is started by a command line that
    names the port, and the only reliable statement about which port it is
    serving is the socket it actually holds.
    """
    global _own_ports
    if _own_ports is not None:
        return _own_ports
    _own_ports = _listening_ports(os.getpid())
    if _own_ports:
        logger.info("protecting Compass's own ports from unattended commands: %s",
                    ", ".join(str(p) for p in sorted(_own_ports)))
    return _own_ports


def sweep() -> str:
    """Stop anything we started that has taken one of Compass's own ports.

    Returns what to tell the model, or "" when there is nothing to say. The
    message matters as much as the kill: a mission whose server keeps dying
    without explanation will keep starting it, and the useful thing to say is
    not "denied" but "that port is taken, use another one".
    """
    groups = _groups.get()
    if not groups or not _supported():
        return ""
    ours = protected_ports()
    if not ours:
        return ""
    stopped: list[tuple[int, int]] = []
    for port in sorted(ours):
        for pid in _listeners_on(port):
            pgid = _group_of(pid)
            # Only ever something this scope started. A listener we did not
            # start is Compass itself, or the user's own work.
            if pgid is None or pgid not in groups:
                continue
            _stop_group(pgid, why=f"it bound port {port}, which Compass serves on")
            groups.discard(pgid)
            stopped.append((pid, port))
    if not stopped:
        return ""
    ports = sorted({port for _, port in stopped})
    listed = ", ".join(str(p) for p in ports)
    return (
        f"\n[compass] A process this command started was listening on "
        f"{listed}, which is the port Compass itself serves on, so it was "
        f"stopped. Binding it makes Compass unreachable for everyone — "
        f"including the session running this build. Start the app on a "
        f"different port (ask the operating system for a free one rather "
        f"than picking a number), and record which port you chose."
    )


# -- the parts that touch the OS -----------------------------------------
def _supported() -> bool:
    """Process groups and `lsof` — POSIX. Windows runs unreaped, which is the
    same honest position the sandbox takes there."""
    return platform.system() in ("Darwin", "Linux")


def _listening_ports(pid: int) -> set[int]:
    out = _lsof(["-a", "-p", str(pid), "-i", "-sTCP:LISTEN", "-P", "-n"])
    ports: set[int] = set()
    for line in out.splitlines()[1:]:
        _, _, addr = line.rpartition(":")
        addr = addr.split()[0] if addr else ""
        if addr.isdigit():
            ports.add(int(addr))
    return ports


def _listeners_on(port: int) -> list[int]:
    out = _lsof(["-ti", f":{port}", "-sTCP:LISTEN"])
    return [int(x) for x in out.split() if x.strip().isdigit()]


def _lsof(args: list[str]) -> str:
    if not _supported():
        return ""
    try:
        done = subprocess.run(["lsof", *args], capture_output=True, text=True,
                              timeout=10)
        return done.stdout or ""
    except (OSError, subprocess.SubprocessError):
        # No lsof, or it refused. Port protection degrades to nothing, which
        # is survivable; reaping at scope exit still happens.
        return ""


def _group_of(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def _stop_group(pgid: int, *, why: str) -> None:
    """SIGTERM the group, then SIGKILL what is left.

    Refuses its own group. Compass runs the agent loop, so killing the group
    Compass is in would stop the server to tidy up after a command — which is
    precisely the failure this module exists to prevent, arrived at from the
    other direction.
    """
    if not _supported() or pgid <= 1:
        return
    try:
        if pgid in (os.getpgrp(), os.getpgid(0)):
            logger.error("refusing to reap process group %d — it is Compass's own",
                         pgid)
            return
    except OSError:
        return
    for sig, wait in ((signal.SIGTERM, GRACE_SECONDS), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return          # already gone, which is the point
        except (PermissionError, OSError) as err:
            logger.warning("could not signal process group %d: %s", pgid, err)
            return
        if wait:
            logger.info("stopping process group %d — %s", pgid, why)
            _wait_for_exit(pgid, wait)
            if not _group_alive(pgid):
                return


def _wait_for_exit(pgid: int, seconds: float) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.1)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False
