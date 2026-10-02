"""Tool registry and scoped tool sets (tools.ts analog)."""

from __future__ import annotations

from compass.code.tools.agent import AgentTool
from compass.common.gateway import images
from compass.common.tools.advisor import ConsultTool
from compass.common.tools.ask import AskUserTool
from compass.common.tools.web_fetch import WebFetchTool
from compass.common.tools.base import Tool
from compass.code.tools.bash import BashOutputTool, BashTool
from compass.code.tools.browser import BrowserTool
from compass.code.tools.filesystem import FileEditTool, FileReadTool, FileWriteTool
from compass.common.tools.memory import MemoryTool
from compass.code.tools.screenshot import ScreenshotTool
from compass.code.tools.search import GlobTool, GrepTool
from compass.code.tools.todo import TodoWriteTool
from compass.common.tools.draw import DrawTool, EditImageTool


def get_all_tools() -> list[Tool]:
    tools: list[Tool] = [
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
        AskUserTool(),
    ]
    # Only when there is an image deployment to run it on. A model told it can
    # draw, that then cannot, spends a turn finding out — and this list is
    # what Code, Missions and the Pipelines builder all take their tools
    # from, so one condition here covers the three of them.
    if images.available():
        tools += [DrawTool(), EditImageTool()]
    return tools


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
    general = [
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
    # A subagent sent off to build a page needs the same picture the parent
    # would have drawn; one sent off to read does not, and `explore` above
    # does not get it.
    if images.available():
        general += [DrawTool(), EditImageTool()]
    return general
