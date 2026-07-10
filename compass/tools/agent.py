"""The agent tool — subagents as recursion. Port of AgentTool.

Spawning a subagent is just calling query() again with a fresh message
history, a scoped tool set, and a sidechain agent_id. Trust plumbing
(permission broker, abort event, cost tracker) is inherited so permission
prompts bubble to the parent surface and one abort cancels the whole tree.
Sidechain events pass through the parent's stream tagged with agent_id.
"""

from __future__ import annotations

from typing import AsyncIterator, Literal

from pydantic import BaseModel, Field

from compass.config import get_settings
from compass.models import events
from compass.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield


class AgentInput(BaseModel):
    prompt: str = Field(description="Self-contained task for the subagent")
    subagent_type: Literal["explore", "general"] = Field(
        default="general",
        description="explore = read-only search/report; general = full toolset",
    )


class AgentTool(Tool):
    name = "agent"
    description = (
        "Delegate a task to a subagent with its own context window. Use "
        "'explore' for broad read-only codebase questions, 'general' for "
        "self-contained multi-step subtasks. The subagent's final message "
        "is returned as the result."
    )
    input_model = AgentInput

    def is_read_only(self, inp: AgentInput) -> bool:
        return inp.subagent_type == "explore"

    async def call(self, inp: AgentInput, ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        settings = get_settings()
        if ctx.depth >= settings.loop.max_subagent_depth:
            yield ToolOutput(
                f"maximum subagent depth ({settings.loop.max_subagent_depth}) reached",
                is_error=True,
            )
            return

        # Local imports break the tools -> core -> tools cycle, the same job
        # Claude Code's lazy require() does in AgentTool.
        from compass.core.query_loop import query
        from compass.core.system_prompt import build_system_prompt
        from compass.models.messages import user_message
        from compass.tools.registry import subagent_tools

        child_ctx = ctx.child_for_subagent(subagent_tools(inp.subagent_type))
        child_messages = [user_message(inp.prompt)]
        final_text: str | None = None
        terminal_reason = "end_turn"

        async for event in query(
            child_messages,
            child_ctx,
            system_prompt=build_system_prompt(
                role=inp.subagent_type, workspace_root=ctx.workspace_root
            ),
        ):
            if isinstance(event, events.AssistantMessage) and event.content:
                final_text = event.content
            if isinstance(event, events.TurnComplete):
                terminal_reason = event.reason
            yield event  # sidechain events flow to the surface, agent_id-tagged

        if terminal_reason != "end_turn":
            yield ToolOutput(
                f"subagent ended abnormally ({terminal_reason}); "
                f"last message: {final_text or '(none)'}",
                is_error=True,
            )
            return
        yield ToolOutput(final_text or "(subagent produced no final message)")
