"""OS-enforced boundaries for the commands Compass runs.

One entry point, `wrap()`, which takes the argv a tool was about to spawn and
returns the argv to spawn instead. Everything platform-specific lives behind
it: Seatbelt on macOS, bubblewrap on Linux and WSL2.

Windows runs unsandboxed. Neither mechanism exists there, and the honest
options were to refuse to run or to say so — this says so, once, loudly, and
carries on. `unavailable_reason()` is what a surface shows, and
`get_settings().sandbox.fail_if_unavailable` turns the warning into a refusal
for deployments that need the boundary to be a guarantee.

A denied command is not a broken one. `explain()` turns "Operation not
permitted" into a sentence naming the boundary, because a model that is told
which wall it hit writes a different second attempt than one told only that
something failed.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import tempfile
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from compass.common.sandbox.policy import SandboxPolicy

logger = logging.getLogger("compass.sandbox")

__all__ = ["SandboxPolicy", "Availability", "availability", "wrap", "explain",
           "policy_for"]


@dataclass(frozen=True)
class Availability:
    ok: bool
    backend: str  #: seatbelt | bubblewrap | none
    reason: str = ""


_warned = False

#: Set inside work that must be sandboxed whatever the global setting says —
#: an unattended mission, where there is nobody for the permission gate to
#: ask. A ContextVar rather than a flag because sessions run concurrently and
#: one mission must not sandbox somebody else's console.
_required: ContextVar[bool] = ContextVar("compass_sandbox_required", default=False)


@contextlib.contextmanager
def required():
    """Inside this block, commands are sandboxed even with the setting off."""
    token = _required.set(True)
    try:
        yield
    finally:
        _required.reset(token)


def is_required() -> bool:
    return _required.get()


def availability() -> Availability:
    """Whether a boundary can be enforced here, and by what."""
    system = platform.system()
    if system == "Darwin":
        from compass.common.sandbox import seatbelt

        ok, reason = seatbelt.available()
        return Availability(ok, "seatbelt" if ok else "none", reason)
    if system == "Linux":
        from compass.common.sandbox import bwrap

        ok, reason = bwrap.available()
        return Availability(ok, "bubblewrap" if ok else "none", reason)
    return Availability(
        False, "none",
        f"{system} has no sandbox Compass can use — commands run with the "
        "server's own privileges. Run Compass under WSL2 for an enforced "
        "boundary.")


def unavailable_reason() -> str:
    """Why commands are running unsandboxed, or "" when they are not."""
    state = availability()
    return "" if state.ok else state.reason


def warn_once() -> None:
    """Say it at startup, not per command: a warning on every command is a
    warning nobody reads."""
    global _warned
    if _warned:
        return
    _warned = True
    state = availability()
    if not state.ok:
        logger.warning("sandbox unavailable: %s", state.reason)


def policy_for(workspace: "Path | str | None",
               session_temp: "Path | None" = None) -> SandboxPolicy:
    """The configured policy for work rooted at `workspace`."""
    from compass.common.config import get_settings

    cfg = get_settings().sandbox
    return SandboxPolicy.for_workspace(
        Path(workspace) if workspace else None,
        network=cfg.network,
        session_temp=session_temp,
        extra_writable=list(cfg.allow_write),
        extra_unreadable=list(cfg.deny_read),
    )


def wrap(argv: list[str], policy: SandboxPolicy) -> tuple[list[str], Path | None]:
    """`argv` rewritten to run inside the boundary, plus the temporary profile
    to delete afterwards.

    Returns `argv` unchanged where no sandbox is available, so a caller can
    always spawn what it is handed.
    """
    state = availability()
    if not state.ok:
        warn_once()
        return argv, None

    profile_path = Path(tempfile.gettempdir()) / f"compass-sbx-{uuid.uuid4().hex}.sb"
    if state.backend == "seatbelt":
        from compass.common.sandbox import seatbelt

        return seatbelt.wrap(argv, policy, profile_path), profile_path
    from compass.common.sandbox import bwrap

    return bwrap.wrap(argv, policy, profile_path), None


#: What the two platforms say when they refuse. Matched loosely, because the
#: message is the tool's, not ours: `bash` reports its own failure to open a
#: file, and the sandbox is only the reason.
_DENIAL_MARKERS = (
    "operation not permitted",
    "permission denied",
    "sandbox-exec",
    "bwrap:",
    "cannot create directory",
)


def explain(output: str, policy: SandboxPolicy, *, exit_code: int) -> str:
    """A line to append to a failed command's output, or "".

    Only when it failed and the failure looks like a boundary. A model told
    "the sandbox refused a write outside the workspace" retries somewhere it
    is allowed to write; one told "Operation not permitted" retries the same
    command with `sudo`.
    """
    if exit_code == 0:
        return ""
    haystack = (output or "").lower()
    if not any(marker in haystack for marker in _DENIAL_MARKERS):
        return ""
    network_note = ("" if policy.network else
                    " The network is off for this command, so anything "
                    "fetching or installing from the internet will fail.")
    return (
        "\n\n[sandbox] This command ran inside a boundary and may have been "
        f"stopped by it. {policy.describe()}.{network_note} Work inside the "
        "writable paths above; do not try to widen the boundary from within "
        "the command."
    )
