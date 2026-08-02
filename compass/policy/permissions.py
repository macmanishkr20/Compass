"""Permission rules and modes — the port of the checkPermissions layer.

Order of authority (same as Claude Code): deny rules beat everything, then
explicit allow/ask rules, then the permission mode's default stance. A tool's
own `check_permissions` can pre-empt with a hard deny (e.g. path escapes the
workspace) before rules are even consulted.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum
from typing import Any

from compass.config import PermissionRule, get_settings


class Behavior(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    behavior: Behavior
    reason: str = ""


def _rule_matches(rule: PermissionRule, tool_name: str, arguments: dict[str, Any]) -> bool:
    if rule.tool != "*" and rule.tool != tool_name:
        return False
    if rule.pattern == "*":
        return True
    # Match the pattern against the tool's primary argument (command for bash,
    # path for file tools) — the same shape as Bash(git *) rules.
    primary = str(
        arguments.get("command")
        or arguments.get("path")
        or arguments.get("file_path")
        or ""
    )
    return fnmatch.fnmatch(primary, rule.pattern)


def check_permissions(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    is_read_only: bool,
    permission_mode: str | None = None,
    extra_rules: list[PermissionRule] | None = None,
) -> PermissionDecision:
    settings = get_settings()
    mode = permission_mode or settings.permission_mode
    # Session "Always allow" rules take precedence over the global config rules
    # (they're consulted first, but deny still beats allow within the merged set).
    rules = [*(extra_rules or []), *settings.permission_rules]

    if tool_name == "bash":
        return _check_bash_permissions(arguments, mode, rules)

    for rule in rules:
        if rule.action == "deny" and _rule_matches(rule, tool_name, arguments):
            return PermissionDecision(
                Behavior.DENY, f"denied by rule {rule.tool}({rule.pattern})"
            )
    for rule in rules:
        if rule.action == "allow" and _rule_matches(rule, tool_name, arguments):
            return PermissionDecision(
                Behavior.ALLOW, f"allowed by rule {rule.tool}({rule.pattern})"
            )
    for rule in rules:
        if rule.action == "ask" and _rule_matches(rule, tool_name, arguments):
            return PermissionDecision(Behavior.ASK, "rule requires confirmation")

    # Mode defaults
    if mode == "bypass":
        return PermissionDecision(Behavior.ALLOW, "bypass mode")
    if mode == "plan":
        if is_read_only:
            return PermissionDecision(Behavior.ALLOW, "plan mode: read-only allowed")
        return PermissionDecision(Behavior.DENY, "plan mode: writes are blocked")
    if mode == "accept_edits" and tool_name in ("file_write", "file_edit"):
        return PermissionDecision(Behavior.ALLOW, "accept_edits mode")
    if is_read_only:
        return PermissionDecision(Behavior.ALLOW, "read-only tool")
    return PermissionDecision(Behavior.ASK, "state-changing tool requires confirmation")


def _check_bash_permissions(
    arguments: dict[str, Any], mode: str, rules: list[PermissionRule]
) -> PermissionDecision:
    """Bash-aware verdict — port of bashPermissions.

    Composition order: catastrophic deny > deny rules (any segment) >
    destructive ask > substitution ask > read-only allow > allow rules
    (must cover every segment) > mode default. An `allow bash git *` rule
    therefore cannot smuggle `git status && rm -rf .` (second segment
    unmatched) or `git status $(rm .)` (substitution never auto-allows).
    """
    from compass.tools.bash_security import analyze_command, segment_commands

    command = str(arguments.get("command", ""))
    analysis = analyze_command(command)

    if analysis.catastrophic:
        return PermissionDecision(
            Behavior.DENY, f"blocked destructive command: {analysis.catastrophic}"
        )

    bash_rules = [r for r in rules if r.tool in ("bash", "*")]
    segments = segment_commands(analysis) or [command]

    def matches(rule: PermissionRule, segment: str) -> bool:
        return rule.pattern == "*" or fnmatch.fnmatch(segment, rule.pattern)

    for rule in bash_rules:
        if rule.action == "deny" and any(matches(rule, s) for s in [command, *segments]):
            return PermissionDecision(
                Behavior.DENY, f"denied by rule bash({rule.pattern})"
            )

    if mode == "bypass":
        return PermissionDecision(Behavior.ALLOW, "bypass mode")

    # Plan mode blocks every non-read-only command outright — a destructive
    # or unparseable command must not downgrade the verdict to "ask".
    if mode == "plan" and not analysis.read_only:
        return PermissionDecision(Behavior.DENY, "plan mode: writes are blocked")

    if analysis.destructive:
        return PermissionDecision(
            Behavior.ASK, f"destructive command: {analysis.destructive}"
        )

    # Read-only wins first: the analyzer already proved any substitution/redirect
    # here is harmless (read-only inner commands, output only discarded), so
    # auto-allow it instead of stopping to ask.
    if analysis.read_only:
        return PermissionDecision(Behavior.ALLOW, "read-only command")

    if analysis.has_substitution:
        return PermissionDecision(
            Behavior.ASK, "command substitution detected — cannot be auto-allowed"
        )
    if not analysis.parse_ok:
        return PermissionDecision(
            Behavior.ASK if mode != "plan" else Behavior.DENY,
            "command could not be parsed safely",
        )

    allow_rules = [r for r in bash_rules if r.action == "allow"]
    if allow_rules and all(
        any(matches(rule, segment) for rule in allow_rules) for segment in segments
    ):
        return PermissionDecision(Behavior.ALLOW, "all segments allowed by rules")

    if mode == "plan":
        return PermissionDecision(Behavior.DENY, "plan mode: writes are blocked")
    return PermissionDecision(Behavior.ASK, "state-changing command requires confirmation")
