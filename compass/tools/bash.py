"""bash — the port of BashTool, with per-input concurrency classification.

Claude Code's deepest permission machinery lives on its Bash tool; this port
keeps the essential pieces: a read-only command classifier (drives both
parallel scheduling and the plan-mode/read-only permission default), streaming
stdout as progress, timeouts, and a persistent working directory — a `cd` in
one call is visible to the next (see shell_session.ShellSession).
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from pydantic import BaseModel, Field

from compass.tools.base import Progress, Tool, ToolOutput, ToolUseContext, ToolYield
from compass.tools.bash_security import analyze_command
from compass.tools.shell_session import ShellResult, ShellSession


class BashInput(BaseModel):
    command: str = Field(
        description=(
            "Shell command to run. The working directory persists between "
            "calls, so a `cd` carries to your next command."
        )
    )
    description: str = Field(
        default="",
        description=(
            "One short sentence, in plain language, explaining what this command "
            "does and why you are running it — shown to the user as your reasoning "
            "before they approve it. E.g. 'Check which process is using port 65092 "
            "so I can restart the Pulse server.'"
        ),
    )
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    run_in_background: bool = Field(
        default=False,
        description=(
            "Run the command as a background task instead of waiting for it. Use "
            "this for long-lived processes like dev servers (`npm run dev`, "
            "`uvicorn ...`) so the turn isn't blocked. The task appears in the "
            "Background tasks panel where the user can watch and stop it; returns "
            "immediately with a task id. A localhost port in the command becomes "
            "the task's preview URL."
        ),
    )


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command in the workspace root. Output is streamed. "
        "Prefer file_read/grep/glob for inspection — they are cheaper and safer."
    )
    input_model = BashInput

    def is_read_only(self, inp: BashInput) -> bool:
        return analyze_command(inp.command).read_only

    def check_tool_permissions(self, inp: BashInput, ctx):
        # Catastrophic commands are hard-denied here as defense in depth;
        # the rule engine repeats the check for the ask/allow verdicts.
        analysis = analyze_command(inp.command)
        if analysis.catastrophic:
            from compass.policy.permissions import Behavior, PermissionDecision

            return PermissionDecision(
                Behavior.DENY, f"blocked destructive command: {analysis.catastrophic}"
            )
        return None

    async def call(self, inp: BashInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        from compass.services.background_tasks import looks_like_server, registry

        # Long-lived dev servers must be detached — running one in the foreground
        # blocks the turn until the timeout (up to 10 min). Honour an explicit
        # request, and also auto-detect a forgotten one so the turn never hangs.
        auto_bg = not inp.run_in_background and looks_like_server(inp.command)
        if inp.run_in_background or auto_bg:
            name = inp.description or inp.command.split("&&")[-1].strip()[:60]
            task = await registry.start(
                name=name,
                command=inp.command,
                cwd=ctx.shell_state.resolved_cwd(),
                workspace_id=getattr(ctx, "workspace_id", None),
            )
            where = f" — preview at {task.url}" if task.url else ""
            auto_note = (
                " (a long-lived server was auto-detected and started in the "
                "background so this turn isn't blocked; give it a few seconds to "
                "come up, then use the screenshot tool on its preview URL)"
                if auto_bg
                else ""
            )
            yield ToolOutput(
                f"Started background task [{task.id}] “{task.name}”{where}.{auto_note} "
                f"It runs in the Background tasks panel (the user can stop it there). "
                f"Call bash_output with task_id \"{task.id}\" to read its output."
            )
            return

        session = ShellSession(state=ctx.shell_state)
        # Bridge ShellSession.run's push-callback to this pull-generator via a
        # queue, so stdout streams as Progress the instant it arrives.
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def execute() -> ShellResult:
            return await session.run(
                inp.command,
                timeout=inp.timeout_seconds,
                abort=ctx.abort_event,
                on_output=queue.put_nowait,
            )

        task = asyncio.create_task(execute())
        while not task.done() or not queue.empty():
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.05)
                yield Progress(data=chunk)
            except asyncio.TimeoutError:
                continue
        result = await task  # surface any exception, get the result

        if result.aborted:
            yield ToolOutput("command aborted by user", is_error=True)
            return
        if result.timed_out:
            yield ToolOutput(
                f"command timed out after {inp.timeout_seconds}s", is_error=True
            )
            return
        output = result.output
        if result.exit_code != 0:
            yield ToolOutput(
                f"exit code {result.exit_code}\n{output}".strip(), is_error=True
            )
        else:
            yield ToolOutput(output.strip() or "(no output)")


class BashOutputInput(BaseModel):
    task_id: str = Field(
        description=(
            "The background task id returned when the task was started with "
            "run_in_background (shown as [id] in that tool's result)."
        )
    )
    filter: str = Field(
        default="",
        description="Optional regular expression; only output lines matching it are returned.",
    )


class BashOutputTool(Tool):
    """Read new stdout from a background task — the port of Claude Code's
    BashOutput. Returns only the output printed since the last check, plus the
    task's current status, so the agent can monitor a dev server, build, or
    watcher it started with run_in_background."""

    name = "bash_output"
    description = (
        "Read new output from a background task started with run_in_background. "
        "Returns the stdout/stderr printed since your last check for that task, "
        "plus its status (running/finished/error) and exit code. Poll it to "
        "confirm a server came up, watch a build, or read a long-running command."
    )
    input_model = BashOutputInput

    def is_read_only(self, inp: BashOutputInput) -> bool:
        return True

    async def call(self, inp: BashOutputInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        from compass.services.background_tasks import registry

        task = registry.get(inp.task_id)
        if task is None:
            yield ToolOutput(f"No background task with id {inp.task_id}.", is_error=True)
            return
        lines = registry.read_new(inp.task_id)
        if inp.filter:
            import re

            try:
                pat = re.compile(inp.filter)
                lines = [ln for ln in lines if pat.search(ln)]
            except re.error:
                pass
        status = f"status={task.status}"
        if task.exit_code is not None:
            status += f" exit={task.exit_code}"
        body = "\n".join(lines) if lines else "(no new output)"
        yield ToolOutput(f"[{task.name}] {status}\n{body}")
