"""What a sandboxed command is allowed to touch.

Compass's safety model has always been "ask a human": a command is analysed
(`bash_security`), a verdict is produced, and anything doubtful stops for
approval. That works while somebody is watching. It is the wrong shape for an
agent left to run for an hour, where the only two outcomes are a turn that
stalls on the first prompt or `bypass` mode — an agent with the server's full
privileges and nothing between it and the disk.

A sandbox is what replaces the human. The boundary is enforced by the
operating system, for the command and every child it spawns, so approval stops
being the thing that keeps the machine safe and goes back to being a
convenience.

The shape of the default policy is taken from what actually works rather than
from what sounds strict:

  * writes are confined — the workspace, and the temp directory the toolchain
    needs;
  * reads are open, because denying them broadly breaks the shell itself: a
    profile without `/usr` and the dyld cache cannot even start `bash`;
  * a short deny-list closes the reads that actually matter — private keys,
    cloud credentials, the `.env` next to the code;
  * network is allowed by default and switched off for unattended work, where
    a command that can reach the internet is a command that can exfiltrate.

Nothing here executes anything. `policy` is a description; `seatbelt` and
`bwrap` turn it into the arguments their platform understands.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

#: Read-denied by default. These are the files whose contents are worth more
#: than the task: keys, tokens, cloud sessions. Listed by name rather than
#: guessed at, so the list can be read and argued with.
DEFAULT_SECRET_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.azure",
    "~/.kube",
    "~/.docker/config.json",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/Library/Keychains",
)


def _expand(path: str | Path) -> Path:
    """An absolute, symlink-resolved path.

    Resolution is not a nicety on macOS: `/tmp` is a symlink to `/private/tmp`,
    and a Seatbelt profile that names the symlink denies the very directory it
    was written to allow. The first sandbox written here failed exactly that
    way — the allowed write was refused along with the forbidden one.
    """
    return Path(os.path.expanduser(str(path))).resolve()


@dataclass
class SandboxPolicy:
    """The boundary for one command."""

    #: Directories the command may write to. Everything else is read-only.
    writable: list[Path] = field(default_factory=list)
    #: Paths whose contents may not be read at all.
    unreadable: list[Path] = field(default_factory=list)
    #: Whether the command may reach the network at all. Phase 0 is all or
    #: nothing; a per-domain allowlist needs a proxy to enforce it and is
    #: deliberately not pretended at here.
    network: bool = True

    @classmethod
    def for_workspace(cls, workspace: Path | None, *, network: bool = True,
                      session_temp: Path | None = None,
                      extra_writable: list[str] | None = None,
                      extra_unreadable: list[str] | None = None) -> "SandboxPolicy":
        """The everyday policy: write here, read anywhere but the secrets.

        `session_temp` is a directory made for this command alone. The
        toolchain writes temporary files constantly and a sandbox that forbids
        it breaks everything while protecting nothing — but the obvious fix,
        allowing the system temp directory, hands over the whole shared tree
        where every other application on the machine keeps its caches. One
        directory per command costs nothing and gives away nothing.
        """
        writable: list[Path] = []
        if workspace:
            writable.append(_expand(workspace))
        if session_temp:
            writable.append(_expand(session_temp))
        for extra in (extra_writable or []):
            writable.append(_expand(extra))

        unreadable = [_expand(p) for p in (*DEFAULT_SECRET_PATHS,
                                           *(extra_unreadable or []))]
        return cls(writable=_unique(writable), unreadable=_unique(unreadable),
                   network=network)

    def describe(self) -> str:
        """One line for a log or a tool result."""
        where = ", ".join(str(p) for p in self.writable) or "nothing"
        return (f"writes: {where}; reads: everything except "
                f"{len(self.unreadable)} secret paths; "
                f"network: {'on' if self.network else 'off'}")


def _unique(paths: list[Path]) -> list[Path]:
    """Keep order, drop duplicates and anything already covered by a parent."""
    out: list[Path] = []
    for path in paths:
        if any(path == kept or _is_within(path, kept) for kept in out):
            continue
        out = [kept for kept in out if not _is_within(kept, path)]
        out.append(path)
    return out


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
