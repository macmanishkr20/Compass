"""Linux and WSL2: bubblewrap.

The same boundary by a different route. Where Seatbelt filters syscalls
against a profile, bubblewrap builds a new mount namespace: the filesystem is
bound in read-only, the writable paths are bound back over it read-write, and
the denied paths are covered with an empty tmpfs so there is nothing to read.

`--die-with-parent` matters more than it looks. Without it a backgrounded
child outlives the command that spawned it and keeps its sandbox open.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from compass.common.sandbox.policy import SandboxPolicy


def available() -> tuple[bool, str]:
    if not shutil.which("bwrap"):
        return False, ("bubblewrap is not installed — `apt install bubblewrap` "
                       "(Debian/Ubuntu) or `dnf install bubblewrap` (Fedora)")
    return True, ""


def wrap(argv: list[str], policy: SandboxPolicy, _profile_path: Path) -> list[str]:
    """`argv`, rewritten to run inside a bubblewrap namespace."""
    args = [
        "bwrap",
        "--die-with-parent",
        "--ro-bind", "/", "/",
        # A private /proc and /dev, so the command cannot inspect or signal
        # processes outside its own namespace.
        "--proc", "/proc",
        "--dev", "/dev",
    ]
    for path in policy.writable:
        if path.exists():
            args += ["--bind", str(path), str(path)]
    for path in policy.unreadable:
        if path.exists():
            # Covered rather than removed: the path still exists, and reads of
            # it find an empty directory instead of a key.
            args += ["--tmpfs", str(path)]
    if not policy.network:
        args.append("--unshare-net")
    return [*args, "--", *argv]
