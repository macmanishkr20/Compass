"""Tools Compass does not run.

Every other tool in Compass works the same way: the model asks for it, the
loop executes it, the result goes back as another message, and the model is
called again. That round trip is what a tool *is*, everywhere else in this
codebase.

These are not that. Azure runs them inside the same response — the model
searches, reads what it found, and keeps writing, without the turn ever coming
back to us. Compass's part is to say the tool may be used and to read what
happened out of the output afterwards. There is nothing to execute, no
permission gate to pass, and no second call to make.

That difference is why they live here rather than in `common/tools`, which is
entirely about the executing kind. Putting them in the registry would mean
teaching the loop that some of its tools are not its to run.

Two are available on this deployment, confirmed against the resource rather
than the documentation:

  * `web_search` — searches, opens pages, and cites what it used. This is the
    only way Compass can know anything that postdates the model's training,
    and it is on by default for that reason.
  * `code_interpreter` — Python in a container Azure hosts. Off by default,
    because Compass already runs code in the workspace through bash, and the
    workspace is usually where the user wants it run.

`file_search` needs a vector store to point at, and `computer_use_preview` is
refused outright by gpt-5; neither is offered.
"""

from __future__ import annotations

from typing import Any

from compass.common.config import get_settings

#: Output item types Azure uses to report these tools. Anything in here is a
#: record of work already done — never something for the loop to execute.
HOSTED_ITEMS = ("web_search_call", "code_interpreter_call")


#: The one effort level that refuses these outright. Measured, after a live
#: turn came back 400: "The following tools cannot be used with
#: reasoning.effort 'minimal': web_search." It refuses `code_interpreter` the
#: same way, and names both when both are offered; `low` and everything above
#: accept them, and ordinary function tools are fine at every level.
#:
#: The refusal is also coherent, which is why the answer is to drop the tools
#: rather than to quietly raise the effort: `minimal` means do not deliberate,
#: and searching the web is deliberation. Someone who picks it wants the fast
#: answer, not the researched one.
NO_HOSTED_AT = "minimal"


def specs(effort: str | None = None) -> list[dict[str, Any]]:
    """The hosted tools to offer on this request, per settings and effort.

    Returns a fresh list each call: it goes into a request body, and a shared
    mutable default in a request body is the kind of bug that only shows up
    under concurrency.
    """
    if (effort or "").strip().lower() == NO_HOSTED_AT:
        return []
    tools = get_settings().tools
    offered: list[dict[str, Any]] = []
    if tools.web_search:
        offered.append({"type": "web_search"})
    if tools.code_interpreter:
        offered.append({"type": "code_interpreter",
                        "container": {"type": "auto"}})
    return offered


#: Told to the model only when the interpreter is actually enabled. Two tools
#: that both "run code" is an ambiguity the model resolves by guessing, and it
#: guesses in a way that reads as correct: asked to run a snippet on a Mac it
#: chose the sandbox, printed `Linux ... /home/sandbox`, and said nothing about
#: where that came from. The answer was true and the impression it left was
#: false. Naming the difference is what turns the guess into a choice.
WHERE_CODE_RUNS = """\
You have two ways to run code and they run in different places.

`code_interpreter` runs Python in a temporary Linux container on Azure. It \
cannot see the user's files, their repository, their installed packages or \
their environment, and everything in it is discarded afterwards. Use it for \
self-contained work — calculating, checking an algorithm, parsing data you \
already have in the conversation.

`bash` runs on the user's own machine, in their workspace. Use it for \
anything that touches their project: their files, their tests, their \
dependencies, their git history, anything whose answer depends on the state \
of their machine.

When a request could mean either, prefer `bash`, because that is the machine \
the user is sitting at. When you use `code_interpreter` and where it ran \
could change how the result should be read, say that it ran in a sandbox \
rather than on their machine.\
"""


def where_code_runs() -> str:
    """The note explaining the two execution surfaces, or "" when there is
    only one. Empty is the default, and deliberately so: with the interpreter
    off there is no ambiguity to resolve, and a system prompt should not carry
    guidance about a tool that is not there."""
    return WHERE_CODE_RUNS if get_settings().tools.code_interpreter else ""


def describe(item: dict[str, Any]) -> str:
    """One line saying what the hosted tool did, for the activity surface.

    Empty when there is nothing worth showing. The point is that a search is
    visible: an answer that quietly depended on three web pages should not look
    identical to one the model produced from memory.
    """
    kind = item.get("type", "")
    if kind == "web_search_call":
        action = item.get("action") or {}
        if action.get("type") == "open_page":
            return f"Read {action.get('url', '')}".strip()
        queries = action.get("queries") or []
        if queries:
            return "Searched the web for " + ", ".join(
                f"{q!r}" for q in queries[:3]
            )
        return "Searched the web"
    if kind == "code_interpreter_call":
        code = (item.get("code") or "").strip()
        first = next((ln for ln in code.splitlines() if ln.strip()), "")
        return f"Ran Python: {first[:80]}" if first else "Ran Python"
    return ""


def sources(item: dict[str, Any]) -> list[str]:
    """URLs this item actually opened, so an answer can show its working."""
    action = item.get("action") or {}
    if item.get("type") == "web_search_call" and action.get("type") == "open_page":
        url = action.get("url") or ""
        return [url] if url else []
    return []
