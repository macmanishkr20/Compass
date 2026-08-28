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


def specs() -> list[dict[str, Any]]:
    """The hosted tools to offer on this request, per settings.

    Returns a fresh list each call: it goes into a request body, and a shared
    mutable default in a request body is the kind of bug that only shows up
    under concurrency.
    """
    tools = get_settings().tools
    offered: list[dict[str, Any]] = []
    if tools.web_search:
        offered.append({"type": "web_search"})
    if tools.code_interpreter:
        offered.append({"type": "code_interpreter",
                        "container": {"type": "auto"}})
    return offered


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
