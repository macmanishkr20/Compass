"""Tool registry and scoped tool sets (tools.ts analog)."""

from __future__ import annotations

from compass.code.tools.agent import AgentTool
from compass.common.tools.base import Tool
from compass.code.tools.bash import BashOutputTool, BashTool
from compass.code.tools.browser import BrowserTool
from compass.code.tools.filesystem import FileEditTool, FileReadTool, FileWriteTool
from compass.common.tools.memory import MemoryTool
from compass.code.tools.screenshot import ScreenshotTool
from compass.code.tools.search import GlobTool, GrepTool
from compass.code.tools.todo import TodoWriteTool


def get_all_tools() -> list[Tool]:
    return [
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        BashOutputTool(),
        TodoWriteTool(),
        ScreenshotTool(),
        BrowserTool(),
        MemoryTool(),
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
        BashOutputTool(),
        TodoWriteTool(),
    ]
