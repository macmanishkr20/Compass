"""macOS: Seatbelt, through `sandbox-exec`.

Built into every Mac, which is why Claude Code needs nothing installed on this
platform. `sandbox-exec` is formally deprecated and has been for years; it is
also what the sandboxing in current tools uses, because the replacement
(App Sandbox entitlements) applies to signed applications rather than to
arbitrary child processes.

The profile below is deny-by-default with reads opened back up. That ordering
matters: `(deny default)` alone stops `bash` from starting, because it cannot
read the dynamic linker cache.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from compass.common.sandbox.policy import SandboxPolicy

#: Operations a normal command needs and no boundary is served by refusing:
#: forking, exec, reading kernel state, talking to launchd.
_PREAMBLE = """(version 1)
(deny default)
(allow process* sysctl-read mach* ipc* signal)
(allow file-read*)
"""


def available() -> tuple[bool, str]:
    if not shutil.which("sandbox-exec"):
        return False, "sandbox-exec is not on PATH"
    return True, ""


def profile(policy: SandboxPolicy) -> str:
    """The Seatbelt profile for this policy."""
    lines = [_PREAMBLE]

    if policy.unreadable:
        lines.append("(deny file-read* " + _paths(policy.unreadable) + ")\n")

    if policy.writable:
        lines.append("(allow file-write* " + _paths(policy.writable) + ")\n")
    # The character devices every shell writes to. Without these, output
    # redirection fails and the failure looks like the command itself broke.
    lines.append('(allow file-write-data (literal "/dev/null") '
                 '(literal "/dev/stdout") (literal "/dev/stderr") '
                 '(literal "/dev/tty") (literal "/dev/dtracehelper") '
                 '(literal "/dev/urandom") (literal "/dev/random"))\n')
    lines.append("(allow file-ioctl)\n")

    lines.append("(allow network*)\n" if policy.network else "(deny network*)\n")
    return "".join(lines)


def _paths(paths: list[Path]) -> str:
    # `subpath` covers the directory and everything under it. Quoting is
    # deliberate: a path with a space in it is ordinary on a Mac.
    return " ".join(f'(subpath "{p}")' for p in paths)


def wrap(argv: list[str], policy: SandboxPolicy, profile_path: Path) -> list[str]:
    """`argv`, rewritten to run under the profile.

    The profile is passed as a file rather than inline (`-p`), because inline
    profiles are subject to the shell's quoting and these contain quotes.
    """
    profile_path.write_text(profile(policy))
    return ["sandbox-exec", "-f", str(profile_path), *argv]
