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
