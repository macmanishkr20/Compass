"""Lifecycle hook bus — port of utils/hooks.ts.

Two kinds of hooks, same contract as Claude Code:
  * Python callables registered in-process (the SDK path)
  * shell commands from `.compass/hooks.json`, which receive the event
    payload as JSON on stdin. Exit code 2 blocks; stdout JSON may return
    {"decision": "block", "reason": ...} or {"updated_input": {...}}.

Each hook carries a matcher and a timeout (port of the settings-hooks schema):
  * matcher — for tool events, tested against the tool name. Forms:
      "*"                 → every tool
      "bash"              → exact
      "file_write|file_edit" → pipe-separated exact list
      "^file_.*$"         → regex (anything not a bare word/pipe list)
    Non-tool events (session_start, stop, …) ignore the matcher.
  * timeout — per-hook seconds; a slow hook can't wedge the turn.

Config (.compass/hooks.json) accepts three shapes per event, all supported:
  1. ["cmd1", "cmd2"]                                   (matcher "*", default timeout)
  2. [{"matcher": "bash", "command": "x", "timeout": 30}]
  3. [{"matcher": "bash", "hooks": [{"type": "command",
        "command": "x", "timeout": 30}]}]               (Claude Code settings shape)
Event keys accept snake_case ("pre_tool_use") or CamelCase ("PreToolUse").

Events: session_start, user_prompt_submit, pre_tool_use, post_tool_use,
post_tool_use_failure, stop, subagent_stop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from compass.config import get_settings

logger = logging.getLogger("compass.hooks")

DEFAULT_HOOK_TIMEOUT_SECONDS = 60

_BARE_OR_PIPE = re.compile(r"^[a-zA-Z0-9_|-]+$")


class HookEvent(str, Enum):
    SESSION_START = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_USE_FAILURE = "post_tool_use_failure"
    STOP = "stop"
    SUBAGENT_STOP = "subagent_stop"


# Accept CamelCase event keys (Claude Code settings convention) in addition to
# the native snake_case, so existing hook configs port over unchanged.
_CAMEL_ALIASES = {
    "SessionStart": HookEvent.SESSION_START,
    "UserPromptSubmit": HookEvent.USER_PROMPT_SUBMIT,
    "PreToolUse": HookEvent.PRE_TOOL_USE,
    "PostToolUse": HookEvent.POST_TOOL_USE,
    "PostToolUseFailure": HookEvent.POST_TOOL_USE_FAILURE,
    "Stop": HookEvent.STOP,
    "SubagentStop": HookEvent.SUBAGENT_STOP,
}


def matches_pattern(match_query: str, matcher: str) -> bool:
    """Port of matchesPattern — bare word / pipe-list are exact matches,
    anything else is a regex; an invalid regex matches nothing."""
    if not matcher or matcher == "*":
        return True
    if _BARE_OR_PIPE.match(matcher):
        if "|" in matcher:
            return match_query in [p.strip() for p in matcher.split("|")]
        return match_query == matcher
    try:
        return re.search(matcher, match_query) is not None
    except re.error:
        logger.warning("invalid regex in hook matcher: %r", matcher)
        return False


@dataclass
class HookOutcome:
    blocked: bool = False
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    # Stop hooks: block=True means "do not stop; keep working on `reason`".


HookFn = Callable[[dict[str, Any]], Awaitable[HookOutcome | None]]


@dataclass
class HookSpec:
    fn: HookFn
    matcher: str = "*"
    timeout: float = DEFAULT_HOOK_TIMEOUT_SECONDS


@dataclass
class HookRegistry:
    _hooks: dict[HookEvent, list[HookSpec]] = field(default_factory=dict)
    _shell_loaded: bool = False

    def register(
        self,
        event: HookEvent,
        fn: HookFn,
        *,
        matcher: str = "*",
        timeout: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
    ) -> None:
        self._hooks.setdefault(event, []).append(
            HookSpec(fn=fn, matcher=matcher, timeout=timeout)
        )

    def _load_shell_hooks(self) -> None:
        if self._shell_loaded:
            return
        self._shell_loaded = True
        path = get_settings().workspace_root / ".compass" / "hooks.json"
        if not path.is_file():
            return
        try:
            config = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            logger.warning("could not load hooks.json: %s", err)
            return
        for event_name, entries in config.items():
            event = self._resolve_event(event_name)
            if event is None:
                logger.warning("unknown hook event %r in hooks.json", event_name)
                continue
            for entry in entries or []:
                for matcher, command, timeout in self._parse_entry(entry):
                    self.register(
                        event,
                        _shell_hook(command, timeout),
                        matcher=matcher,
                        timeout=timeout,
                    )

    @staticmethod
    def _resolve_event(name: str) -> HookEvent | None:
        try:
            return HookEvent(name)
        except ValueError:
            return _CAMEL_ALIASES.get(name)

    @staticmethod
    def _parse_entry(entry: Any) -> list[tuple[str, str, float]]:
        """Normalize one config entry into (matcher, command, timeout) tuples."""
        if isinstance(entry, str):
            return [("*", entry, DEFAULT_HOOK_TIMEOUT_SECONDS)]
        if not isinstance(entry, dict):
            logger.warning("ignoring malformed hook entry: %r", entry)
            return []
        matcher = entry.get("matcher", "*")
        # Claude Code settings shape: {matcher, hooks: [{type, command, timeout}]}
        if "hooks" in entry and isinstance(entry["hooks"], list):
            out: list[tuple[str, str, float]] = []
            for h in entry["hooks"]:
                if isinstance(h, dict) and h.get("command"):
                    out.append(
                        (
                            matcher,
                            h["command"],
                            float(h.get("timeout", DEFAULT_HOOK_TIMEOUT_SECONDS)),
                        )
                    )
            return out
        # Flat shape: {matcher, command, timeout}
        if entry.get("command"):
            return [
                (
                    matcher,
                    entry["command"],
                    float(entry.get("timeout", DEFAULT_HOOK_TIMEOUT_SECONDS)),
                )
            ]
        logger.warning("hook entry has no command: %r", entry)
        return []

    async def run(self, event: HookEvent, payload: dict[str, Any]) -> HookOutcome:
        """Run all matching hooks for an event. First block wins; input
        updates chain. `tool_name` in the payload drives matcher filtering."""
        self._load_shell_hooks()
        tool_name = str(payload.get("tool_name", ""))
        merged = HookOutcome()
        for spec in self._hooks.get(event, []):
            if tool_name and not matches_pattern(tool_name, spec.matcher):
                continue
            try:
                outcome = await spec.fn(payload)
            except Exception as err:  # noqa: BLE001 — a broken hook must not kill the turn
                logger.error("hook for %s raised: %s", event.value, err)
                continue
            if outcome is None:
                continue
            if outcome.updated_input is not None:
                merged.updated_input = outcome.updated_input
                payload = {**payload, "arguments": outcome.updated_input}
            if outcome.blocked:
                return HookOutcome(
                    blocked=True,
                    reason=outcome.reason,
                    updated_input=merged.updated_input,
                )
        return merged


def _shell_hook(command: str, timeout: float) -> HookFn:
    async def run_shell(payload: dict[str, Any]) -> HookOutcome | None:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=get_settings().workspace_root,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload).encode()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("hook %r timed out after %.0fs", command, timeout)
            return None
        if proc.returncode == 2:
            return HookOutcome(
                blocked=True,
                reason=stderr.decode(errors="replace").strip() or "blocked by hook",
            )
        if stdout:
            try:
                data = json.loads(stdout)
                return HookOutcome(
                    blocked=data.get("decision") == "block",
                    reason=data.get("reason", ""),
                    updated_input=data.get("updated_input"),
                )
            except json.JSONDecodeError:
                pass
        return None

    return run_shell


_registry: HookRegistry | None = None


def get_hook_registry() -> HookRegistry:
    global _registry
    if _registry is None:
        _registry = HookRegistry()
    return _registry
