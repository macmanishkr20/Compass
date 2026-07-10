"""Bash command analysis — port of bashSecurity/commandSemantics/
destructiveCommandWarning from the original BashTool.

A command is analyzed as a whole, then split into segments on shell
operators (;, &&, ||, |, &) with quote awareness. Verdicts compose
conservatively:

  * catastrophic patterns        -> hard DENY, no human override offered
  * command substitution         -> never auto-allowed (injection channel:
                                    `git status $(rm -rf ~)` must not ride
                                    an "allow git *" rule)
  * destructive patterns         -> always ASK, with a warning reason
  * read-only iff EVERY segment is read-only and nothing redirects
  * allow rules must cover EVERY segment; a deny rule on ANY segment wins
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# ---------------------------------------------------------------- classifiers

READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "printf", "date",
    "whoami", "id", "uname", "grep", "rg", "find", "which", "type", "file",
    "stat", "du", "df", "env", "printenv", "ps", "uptime", "hostname",
    "basename", "dirname", "realpath", "readlink", "tr", "cut", "sort",
    "uniq", "diff", "cmp", "md5", "shasum", "sha256sum", "jq", "column",
    "true", "test", "[",
}
READ_ONLY_GIT = {
    "status", "log", "diff", "show", "branch", "remote", "shortlog",
    "describe", "rev-parse", "ls-files", "ls-remote", "blame", "reflog",
    "cherry", "tag",
}

# Hard-deny: no permission prompt can make these OK.
CATASTROPHIC_PATTERNS = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME)(\s|$)"), "recursive delete of / or home"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"), "fork bomb"),
    (re.compile(r"\bmkfs(\.| )"), "filesystem format"),
    (re.compile(r"\bdd\b.*\bof=/dev/(sd|disk|nvme)"), "raw disk write"),
    (re.compile(r">\s*/dev/(sd|disk|nvme)"), "raw disk write"),
    (re.compile(r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/(\s|$)"), "world-writable root"),
]

# Always ask, with an explicit warning, regardless of allow rules.
DESTRUCTIVE_PATTERNS = [
    (re.compile(r"\brm\s+-[a-zA-Z]*r"), "recursive delete"),
    (re.compile(r"\bgit\s+push\s+.*(--force\b|-f\b)"), "force push"),
    (re.compile(r"\bgit\s+reset\s+--hard"), "hard reset discards work"),
    (re.compile(r"\bgit\s+clean\b"), "deletes untracked files"),
    (re.compile(r"\bdrop\s+(table|database)\b", re.IGNORECASE), "SQL drop"),
    (re.compile(r"\b(shutdown|reboot|halt)\b"), "system power control"),
    (re.compile(r"\bkill(all)?\s+-9"), "SIGKILL"),
    (re.compile(r"(curl|wget)\b[^|;&]*\|\s*(ba|z|da)?sh\b"), "pipes web content into a shell"),
]

SUBSTITUTION_PATTERN = re.compile(r"\$\(|`")
REDIRECT_PATTERN = re.compile(r"(?<![\d&])[><]|>>")


@dataclass
class CommandAnalysis:
    command: str
    segments: list[list[str]] = field(default_factory=list)  # argv per segment
    read_only: bool = False
    has_substitution: bool = False
    has_redirect: bool = False
    catastrophic: str | None = None   # reason, if hard-denied
    destructive: str | None = None    # reason, if ask-always
    parse_ok: bool = True


def split_segments(command: str) -> list[list[str]] | None:
    """Split on ;, &&, ||, |, & with quote awareness via shlex
    punctuation_chars. Returns None when unparseable (conservative)."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= {";", "&", "|"}:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_env_prefix(argv: list[str]) -> list[str]:
    """FOO=bar cmd ... -> cmd ...; also unwraps `env` and `command`."""
    i = 0
    while i < len(argv) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[i]):
        i += 1
    argv = argv[i:]
    while argv and argv[0] in ("env", "command", "nohup", "time"):
        argv = argv[1:]
    return argv


def _segment_read_only(argv: list[str]) -> bool:
    argv = _strip_env_prefix(argv)
    if not argv:
        return False
    head = argv[0].rsplit("/", 1)[-1]
    if head not in READ_ONLY_COMMANDS and head != "git":
        return False
    if head == "git":
        sub = next((a for a in argv[1:] if not a.startswith("-")), "")
        return sub in READ_ONLY_GIT
    return True


def analyze_command(command: str) -> CommandAnalysis:
    analysis = CommandAnalysis(command=command)

    for pattern, reason in CATASTROPHIC_PATTERNS:
        if pattern.search(command):
            analysis.catastrophic = reason
            return analysis
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            analysis.destructive = reason
            break

    analysis.has_substitution = bool(SUBSTITUTION_PATTERN.search(command))
    analysis.has_redirect = bool(REDIRECT_PATTERN.search(command))

    segments = split_segments(command)
    if segments is None or not segments:
        analysis.parse_ok = False
        return analysis
    analysis.segments = segments

    analysis.read_only = (
        not analysis.has_substitution
        and not analysis.has_redirect
        and analysis.destructive is None
        and all(_segment_read_only(argv) for argv in segments)
    )
    return analysis


def segment_commands(analysis: CommandAnalysis) -> list[str]:
    """Each segment rendered back to a string, for per-segment rule matching."""
    return [" ".join(argv) for argv in analysis.segments]
