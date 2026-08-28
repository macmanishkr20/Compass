"""Tool registry and scoped tool sets (tools.ts analog)."""

from __future__ import annotations

from compass.code.tools.agent import AgentTool
from compass.common.tools.advisor import ConsultTool
from compass.common.tools.web_fetch import WebFetchTool
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
        ConsultTool(),
        WebFetchTool(),
    ]


def subagent_tools(subagent_type: str) -> list[Tool]:
    if subagent_type == "explore":
        # Read-only sidechain: search and report, mutate nothing.
        return [FileReadTool(), GlobTool(), GrepTool()]
    # General subagents get everything except the agent tool itself; the
    # depth guard is the real recursion limit, this just avoids fan-out.
    #
    # `web_fetch` is here because a subagent sent off to research something
    # could otherwise search the web — server tools ride on the request, not
    # on this list — and then not read any of the pages it found, which is a
    # strange half-capability to hand someone.
    #
    # `consult` is deliberately not. It is the expensive tool, and the parent
    # is the one that decided to delegate: if a question is hard enough to buy
    # an opinion on, that is a judgement for whoever is holding the whole task,
    # not for each helper spawned underneath it.
    return [
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        BashOutputTool(),
        TodoWriteTool(),
        WebFetchTool(),
    ]
