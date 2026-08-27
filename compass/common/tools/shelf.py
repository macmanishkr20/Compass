"""Which tools the model is shown, when there are too many to show at once.

Every tool definition is sent on every request, so the whole catalogue is paid
for on every turn whether or not any of it is used. Compass's own twelve cost
about 2,850 tokens; that is affordable. The problem is that the list is not
just those twelve — every tool from every connected MCP server is appended to
it, and a handful of servers is easily a hundred tools. Two things go wrong at
once: the definitions crowd out the conversation, and choosing between that
many descriptions is itself something the model gets worse at.

The answer is to stop sending the whole catalogue. The tools that are used on
almost every turn stay visible; the rest are held back, and the model is given
one tool that searches for them by name and description. A tool it finds stays
visible for the rest of the session, so it is searched for once and then used
as normal.

Nothing is ever hidden from execution. `find_tool` resolves against the full
catalogue, so a call to a tool that was never searched for still runs — being
off the shelf only means its description was not spent on this request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from compass.common.tools.base import Tool, ToolUseContext, ToolYield


@dataclass
class Shelf:
    """What has been found so far, and the tool that finds it.

    Lives on the session, because a tool discovered on one turn should still
    be there on the next: searching for the same thing every turn would cost
    more than sending the definition did.
    """

    #: Names found by searching, in the order they were found.
    found: list[str] = field(default_factory=list)

    def remember(self, names: list[str]) -> None:
        for name in names:
            if name not in self.found:
                self.found.append(name)


def attach(tools: list["Tool"]) -> tuple[list["Tool"], Shelf]:
    """Give a catalogue a shelf, and the search tool that reads it.

    The search tool joins the catalogue so the loop can resolve a call to it
    like any other. Whether it is *described* on a given request is decided
    later, by `visible`, because there is no point offering a way to search a
    list that is being shown in full.
    """
    shelf = Shelf()
    finder = FindToolsTool(catalogue=tools, shelf=shelf)
    catalogue = [*tools, finder]
    finder.catalogue = catalogue  # so a search can see the whole list
    return catalogue, shelf


def core_names(tools: list["Tool"]) -> set[str]:
    """The tools that are never held back.

    Compass's own tools are used on almost every turn and are few enough to
    always afford. What arrives from an MCP server is the part that grows
    without limit, so that is the part that gets searched for.
    """
    from compass.code.mcp.tool_wrapper import MCPTool

    return {t.name for t in tools if not isinstance(t, MCPTool)}


def visible(
    tools: list["Tool"], shelf: Shelf | None, *, threshold: int
) -> list["Tool"]:
    """The tools to describe on this request.

    Below the threshold nothing is held back and the search tool is not
    offered: searching costs a round trip, and a round trip is worse than a
    few hundred tokens of description when the whole list already fits. This
    is the case Compass is in today, and it returns exactly the same list it
    always did.
    """
    shown = [t for t in tools if t.name != FindToolsTool.name]
    if shelf is None or threshold <= 0 or len(shown) <= threshold:
        return shown
    core = core_names(shown)
    keep = [t for t in shown if t.name in core or t.name in shelf.found]
    if len(keep) == len(shown):  # nothing left to search for
        return shown
    finder = next((t for t in tools if t.name == FindToolsTool.name), None)
    return [*keep, finder] if finder else shown


#: Words that match everything and therefore distinguish nothing.
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
         "my", "me", "please", "can", "you", "get", "use", "using", "tool"}


def _score(tool: "Tool", terms: list[str]) -> tuple[int, int]:
    """How well a tool answers a search: (distinct terms matched, weight).

    Distinct terms come first because that is what separates the right tool
    from its neighbours. A query like "open a pull request on github" matches
    every github_* tool on the service name alone; ranking by total weight
    lets that one shared word decide the order, and the answer comes back
    alphabetical. Counting how many *different* words each tool matched puts
    the one that also matched "pull" and "request" above the rest.

    A keyword score is still only a keyword score: it cannot know that "prs"
    is what "pull request" is called here. Tool descriptions carry the words
    people actually use, which is the other half of making this work.
    """
    name = tool.name.lower()
    description = (tool.description or "").lower()
    try:
        params = " ".join(tool.input_model.model_json_schema()
                          .get("properties", {})).lower()
    except Exception:  # noqa: BLE001 — a tool with an odd schema is still searchable
        params = ""
    distinct = weight = 0
    for term in terms:
        hit = 0
        if term in name:
            hit += 10
        if term in description:
            hit += 3
        if term in params:
            hit += 2
        if hit:
            distinct += 1
            weight += hit
    return distinct, weight


def search(tools: list["Tool"], query: str, *, limit: int = 5) -> list["Tool"]:
    """The tools that best answer `query`, best first."""
    terms = [t for t in re.split(r"[^a-z0-9_]+", query.lower())
             if len(t) > 1 and t not in _STOP]
    if not terms:
        return []
    scored = []
    for tool in tools:
        distinct, weight = _score(tool, terms)
        if distinct:
            scored.append((distinct, weight, tool))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2].name))
    return [t for _, _, t in scored[:limit]]


class FindToolsInput(BaseModel):
    query: str = Field(
        description="What you need to do, in a few words — 'create a github "
                    "issue', 'send a slack message', 'read a jira ticket'."
    )
    limit: int = Field(
        default=5, ge=1, le=20,
        description="How many tools to return. Ask for more only when the "
                    "first search was too narrow.",
    )


class FindToolsTool:
    """Search the tools that are connected but not described in this request.

    Deliberately not a subclass of Tool: it is assembled per request around a
    catalogue and a shelf, and Tool's registry is a list of stateless
    singletons. It implements the same surface the loop calls.
    """

    name = "find_tools"
    description = (
        "Search for a tool that is connected but not listed here. Compass is "
        "connected to more tools than fit in one request, so the ones used less "
        "often are found by searching rather than listed up front. Use this when "
        "the task needs something none of the listed tools do — reaching GitHub, "
        "Slack, Jira or any other connected service. Search by what you want to "
        "do, not by a tool name you are guessing at. Anything found is listed "
        "normally from then on, so search once and then call it like any other "
        "tool."
    )
    input_model = FindToolsInput

    def __init__(self, catalogue: list["Tool"], shelf: Shelf) -> None:
        self.catalogue = catalogue
        self.shelf = shelf

    # -- the surface the loop expects ------------------------------------
    def is_read_only(self, inp: BaseModel) -> bool:
        return True  # searching a list changes nothing

    def check_tool_permissions(self, inp: BaseModel, ctx: "ToolUseContext"):
        return None

    def validate_input(self, arguments: dict) -> BaseModel:
        return self.input_model.model_validate(arguments)

    def to_openai_schema(self) -> dict:
        from compass.common.tools.base import Tool

        return Tool.to_openai_schema(self)  # type: ignore[arg-type]

    async def call(
        self, inp: FindToolsInput, ctx: "ToolUseContext"
    ) -> AsyncIterator["ToolYield"]:
        from compass.common.tools.base import ToolOutput

        core = core_names(self.catalogue)
        searchable = [
            t for t in self.catalogue
            if t.name not in core and t.name not in self.shelf.found
        ]
        hits = search(searchable, inp.query, limit=inp.limit)
        if not hits:
            yield ToolOutput(
                f"No connected tool matches {inp.query!r}. "
                f"{len(searchable)} tools were searched. Try different words, "
                "or use the tools already listed.",
                is_error=False,
            )
            return
        self.shelf.remember([t.name for t in hits])
        lines = [f"{len(hits)} tool(s) found and now available to call:"]
        for tool in hits:
            lines.append(f"\n- {tool.name}: {(tool.description or '').strip()}")
        yield ToolOutput("\n".join(lines))
