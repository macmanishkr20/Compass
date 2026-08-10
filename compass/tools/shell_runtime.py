"""Cross-platform shell selection for the bash tool and background tasks.

macOS/Linux use **bash** (the agent's commands are bash-flavoured — `ls`, `grep`,
`&&`, `$?`). On **Windows** we prefer Git Bash / WSL `bash.exe` when it's on PATH
so those same commands work unchanged; otherwise we fall back to `cmd.exe`.
`COMPASS_SHELL` overrides the choice explicitly (e.g. a full path to bash.exe).

The macOS/Linux path is byte-for-byte the previous behaviour, so nothing changes
there — the Windows branches are purely additive.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
from pathlib import Path

_CMD_NAMES = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def resolve_shell() -> tuple[str, str]:
    """Return (kind, executable). `kind` is 'posix' (bash/sh) or 'cmd'."""
    override = os.environ.get("COMPASS_SHELL", "").strip()
    if override:
        kind = "cmd" if Path(override).name.lower() in _CMD_NAMES else "posix"
        return kind, override
    if platform.system() == "Windows":
        bash = shutil.which("bash")  # Git Bash or WSL — keeps Unix commands working
        if bash:
            return "posix", bash
        return "cmd", shutil.which("cmd") or "cmd.exe"
    return "posix", shutil.which("bash") or "/bin/bash"


def is_posix_shell() -> bool:
    return resolve_shell()[0] == "posix"


def shell_argv(command: str, *, login: bool = False) -> list[str]:
    """Argv to run `command` in the platform shell (no cwd tracking)."""
    kind, exe = resolve_shell()
    if kind == "posix":
        return [exe, "-lc" if login else "-c", command]
    return [exe, "/c", command]


def cwd_tracking_argv(command: str, cwd_file: Path) -> list[str]:
    """Argv that runs `command`, preserves its exit code, and writes the final
    working directory to `cwd_file` so a `cd` persists to the next call."""
    kind, exe = resolve_shell()
    if kind == "posix":
        # `>|` overrides noclobber; `exit $__ec` yields the command's status.
        wrapped = (
            f"{command}\n"
            f"__ec=$?\n"
            f"pwd -P >| {shlex.quote(str(cwd_file))} 2>/dev/null || true\n"
            f"exit $__ec\n"
        )
        return [exe, "-c", wrapped]
    # cmd.exe: delayed expansion (/v:on) so !errorlevel! is read AFTER the
    # command runs, then record the directory and re-raise the exit code.
    wrapped = (
        f"{command} & set __ec=!errorlevel! & "
        f'cd > "{cwd_file}" & exit /b !__ec!'
    )
    return [exe, "/v:on", "/c", wrapped]
