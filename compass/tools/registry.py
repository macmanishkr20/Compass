"""Tool registry and scoped tool sets (tools.ts analog)."""

from __future__ import annotations

from compass.tools.agent import AgentTool
from compass.tools.base import Tool
from compass.tools.bash import BashTool
from compass.tools.filesystem import FileEditTool, FileReadTool, FileWriteTool
from compass.tools.screenshot import ScreenshotTool
from compass.tools.search import GlobTool, GrepTool
from compass.tools.todo import TodoWriteTool


def get_all_tools() -> list[Tool]:
    return [
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        TodoWriteTool(),
        ScreenshotTool(),
        AgentTool(),
    ]


def subagent_tools(subagent_type: str) -> list[Tool]:
    if subagent_type == "explore":
        # Read-only sidechain: search and report, mutate nothing.
        return [FileReadTool(), GlobTool(), GrepTool()]
    # General subagents get everything except the agent tool itself; the
    # depth guard is the real recursion limit, this just avoids fan-out.
    return [
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        TodoWriteTool(),
    ]
