"""What must hold about thinking, checked without calling a model.

The translation between Compass's chat-completions shapes and the Responses
API is the part of thinking that can fail quietly: reasoning that is dropped
on the way out still produces a perfectly good-looking answer, just a more
expensive and less coherent one. So the invariants are asserted here rather
than eyeballed.

    python3 scripts/check_reasoning.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COMPASS_AUTH_ENABLED", "0")
sys.path.insert(0, str(ROOT))

from compass.common.config import EFFORT_LEVELS, ThinkingSettings  # noqa: E402
from compass.common.gateway.responses import (  # noqa: E402
    ReasoningTrace,
    ResponsesOutcome,
    build_request,
    consume,
    split_instructions,
    to_input,
    to_tools,
)

FAILURES: list[str] = []


def ok(condition: bool, what: str) -> None:
    print(f"   {'ok  ' if condition else 'FAIL'}  {what}")
    if not condition:
        FAILURES.append(what)


SYSTEM = {"role": "system", "content": "You are Compass."}
ASK = {"role": "user", "content": "What's the weather in Paris?"}
ASSISTANT = {
    "role": "assistant",
    "content": "",
    "tool_calls": [{
        "id": "call_abc", "type": "function",
        "function": {"name": "get_weather", "arguments": '{"location":"Paris"}'},
    }],
}
RESULT = {"role": "tool", "tool_call_id": "call_abc", "content": "20C, sunny"}
REASONING = [{"type": "reasoning", "id": "rs_1", "summary": [],
              "encrypted_content": "SEALED"}]


def check_translation() -> None:
    print("\ntranslation to the Responses shape")
    instructions, rest, lifted = split_instructions([SYSTEM, ASK])
    ok(instructions == "You are Compass.", "the leading system prompt becomes instructions")
    ok(lifted == 1, "the count of lifted messages is reported")

    mid = [ASK, {"role": "system", "content": "-- compacted --"}, ASK]
    _, rest_mid, lifted_mid = split_instructions(mid)
    ok(lifted_mid == 0 and len(rest_mid) == 3,
       "a system message mid-conversation stays where it is")

    items = to_input([ASK, ASSISTANT, RESULT])
    kinds = [i.get("type") or i.get("role") for i in items]
    ok(kinds == ["user", "function_call", "function_call_output"],
       f"a tool call and its result become separate items ({kinds})")
    ok(items[1]["call_id"] == "call_abc" and items[2]["call_id"] == "call_abc",
       "the call id ties the call to its result")

    tools = to_tools([{"type": "function", "function": {
        "name": "get_weather", "description": "d", "parameters": {"type": "object"}}}])
    ok(tools[0].get("name") == "get_weather" and "function" not in tools[0],
       "tool schemas lose the nesting chat completions adds")
    ok("strict" not in tools[0], "and carry no strict flag when none was set")

    strict = to_tools([{"type": "function", "function": {
        "name": "t", "description": "d", "strict": True,
        "parameters": {"type": "object"}}}])
    ok(strict[0].get("strict") is True,
       "a strict schema stays strict through the flattening")


def check_reasoning_round_trip() -> None:
    """The one that was actually broken: positions shift when the system
    prompt is lifted, and reasoning keyed by the caller's index vanished."""
    print("\nreasoning survives the round trip")

    messages = [SYSTEM, ASK, ASSISTANT, RESULT]  # ASSISTANT is index 2 here
    body = build_request(
        deployment="gpt-5", messages=messages, tools=None,
        max_output_tokens=1000, effort="high", display="summarized",
        reasoning_by_index={2: REASONING},
    )
    sealed = [i for i in body["input"] if i.get("type") == "reasoning"]
    ok(bool(sealed), "reasoning keyed by the caller's index reaches the request")
    ok(sealed and sealed[0].get("encrypted_content") == "SEALED",
       "it arrives unmodified")

    order = [i.get("type") or i.get("role") for i in body["input"]]
    ok(order.index("reasoning") < order.index("function_call"),
       f"reasoning precedes the tool call it produced ({order})")

    without = build_request(
        deployment="gpt-5", messages=messages, tools=None,
        max_output_tokens=1000, effort="high", display="summarized")
    ok(not [i for i in without["input"] if i.get("type") == "reasoning"],
       "and is absent when none was captured")


def check_request_shape() -> None:
    print("\nthe request asks for what thinking needs")
    body = build_request(
        deployment="gpt-5", messages=[SYSTEM, ASK], tools=None,
        max_output_tokens=1000, effort="high", display="summarized")
    ok(body.get("store") is False, "store: false — Compass keeps its own transcripts")
    ok("reasoning.encrypted_content" in body.get("include", []),
       "the encrypted reasoning is asked for")
    ok(body["reasoning"] == {"effort": "high", "summary": "auto"},
       "effort and a summary are requested")

    omitted = build_request(
        deployment="gpt-5", messages=[SYSTEM, ASK], tools=None,
        max_output_tokens=1000, effort="low", display="omitted")
    ok("summary" not in omitted["reasoning"],
       "display: omitted asks for no summary")
    ok(omitted["reasoning"]["effort"] == "low", "and still sets effort")


def check_effort_ladder() -> None:
    """The ladder, as the deployment reports it.

    This check previously asserted a ladder with `xhigh` at the top, which the
    resource rejects outright — so it was not protecting the ladder, it was
    protecting a mistake, and it passed every time it ran. The levels below
    are what gpt-5-2025-08-07 names in its own 400: 'minimal', 'low',
    'medium', 'high'. Anything added here should come from asking the API, not
    from a document, and `scripts/` has a probe that asks it.
    """
    print("\nthe effort ladder matches what the API accepts")
    t = ThinkingSettings()
    ok(EFFORT_LEVELS == ("minimal", "low", "medium", "high"),
       f"the ladder is {EFFORT_LEVELS}")
    ok("xhigh" not in EFFORT_LEVELS,
       "'xhigh' is not offered — the deployment refuses it")
    ok(t.normalize_effort("xhigh") == "high",
       "'xhigh' on an old session becomes high, not nothing")
    ok(t.normalize_effort("max") == "high", "'max' becomes the highest Azure has")
    ok(t.normalize_effort("minimal") == "minimal", "'minimal' is a real level now")
    ok(t.normalize_effort("nonsense") is None, "an unknown level is dropped, not sent")
    ok(t.normalize_effort(None) is None, "no effort stays no effort")
    ok(t.reasons("gpt-5") and not t.reasons("gpt-4o-mini"),
       "only reasoning deployments take the reasoning path")
    ok(ThinkingSettings().advisor_effort in EFFORT_LEVELS,
       "the advisor asks for a level that exists")


def check_server_tools() -> None:
    """Tools Azure runs itself: offered where they belong, never executed."""
    print("\ntools the server runs are offered without being executed")
    from compass.common.gateway import hosted
    from compass.common.gateway.responses import build_request, parse_response

    msgs = [{"role": "user", "content": "hello"}]
    agentic = build_request(deployment="gpt-5", messages=msgs, tools=None,
                            max_output_tokens=99, effort="medium",
                            display="auto", server_tools=True)
    names = {t.get("type") for t in agentic.get("tools") or []}
    ok("web_search" in names, "the agentic path offers web search")

    oneshot = build_request(deployment="gpt-5", messages=msgs, tools=None,
                            max_output_tokens=99, effort="medium",
                            display="auto", stream=False,
                            schema={"type": "object"})
    ok(not oneshot.get("tools"),
       "a one-shot call gets none unless it asks — utility calls stay bare")

    # Design asks. A hosted search and a strict json_schema were measured
    # coexisting on this API before this was relied on: the call searched and
    # still returned JSON that matched the schema.
    researching = build_request(deployment="gpt-5", messages=msgs, tools=None,
                               max_output_tokens=99, effort="high",
                               display="auto", stream=False,
                               schema={"type": "object"}, server_tools=True)
    ok(any(t.get("type") == "web_search" for t in researching.get("tools") or []),
       "but Design can research while writing, schema and all")

    # The invariant that matters: a record of work already done must never be
    # mistaken for a request to do work.
    outcome = parse_response({"output": [
        {"type": "web_search_call", "status": "completed",
         "action": {"type": "open_page", "url": "https://example.com"}},
        {"type": "code_interpreter_call", "code": "print(1)"},
        {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
    ]})
    ok(outcome.tool_calls == [], "hosted items never become tool calls")
    ok(len(outcome.hosted) == 2, "but they are kept, so the turn can show them")
    ok(hosted.sources(outcome.hosted[0]) == ["https://example.com"],
       "a page that was opened is reported as a source")
    ok("example.com" in hosted.describe(outcome.hosted[0]),
       "and described in a line a reader can use")


def check_execution_surfaces() -> None:
    """Two ways to run code is an ambiguity; saying so is what resolves it.

    Measured, not assumed. With the interpreter on and no guidance, "run this
    code" on a Mac was answered `Linux ... /home/sandbox` — true, and leaving
    a false impression. With the guidance the same request goes to bash on the
    user's machine, and self-contained arithmetic still goes to the sandbox.
    """
    print("\nthe two places code can run are told apart")
    import os

    from compass.common.config import get_settings
    from compass.common.gateway import hosted

    ok(not hosted.where_code_runs(),
       "with the interpreter off there is no note, so prompts are untouched")

    was = get_settings().tools.code_interpreter
    try:
        get_settings().tools.code_interpreter = True
        note = hosted.where_code_runs()
        ok(bool(note), "with it on, the note appears")
        ok("bash" in note and "code_interpreter" in note,
           "and names both surfaces rather than only the new one")
        ok("prefer `bash`" in note,
           "the ambiguous case resolves to the user's own machine")
        ok(any(t.get("type") == "code_interpreter"
               for t in hosted.specs()), "and the tool is actually offered")
    finally:
        get_settings().tools.code_interpreter = was
    ok(not hosted.where_code_runs(), "and the default is restored")


def check_reach() -> None:
    """Which surface can do what. Written down because "the tool exists" and
    "this surface can use it" are different claims, and the gap between them
    is invisible from the registry alone."""
    print("\neach surface reaches what it is meant to")
    from compass.code.tools.registry import get_all_tools, subagent_tools

    main = {t.name for t in get_all_tools()}
    ok({"consult", "web_fetch"} <= main, "Code/Agent has both new tools")

    # Home is tool-free by design; the exceptions are read-only and touch
    # nothing on the machine, which is the bar the docstring there sets.
    from compass.home.engine import ChatSession
    import inspect
    home = inspect.getsource(ChatSession.make_context)
    ok("WebFetchTool()" in home, "Home can read a link it is given")
    ok("MemoryTool()" in home, "and still remembers what it learns")
    for absent in ("BashTool", "FileWriteTool", "FileEditTool", "BrowserTool"):
        ok(absent not in home, f"Home still has no {absent}")

    general = {t.name for t in subagent_tools("general")}
    ok("web_fetch" in general,
       "a subagent can read the pages a search turns up")
    ok("consult" not in general,
       "but cannot buy an expensive opinion — that is the parent's call")
    ok("agent" not in general, "and still cannot spawn further subagents")

    explore = {t.name for t in subagent_tools("explore")}
    ok(explore == {"file_read", "glob", "grep"},
       "the read-only sidechain stays local and unchanged")


def check_fetching() -> None:
    """web_fetch refuses the addresses that turn a fetch into an escalation."""
    print("\nfetching refuses what it should")
    from compass.common.tools.web_fetch import _to_text, _unsafe

    for url in ("https://example.com", "http://localhost:4310/x"):
        ok(not _unsafe(url), f"allowed: {url}")
    for url in ("http://169.254.169.254/latest/meta-data/",
                "http://metadata.google.internal/x",
                "file:///etc/passwd", "javascript:alert(1)"):
        ok(bool(_unsafe(url)), f"refused: {url}")

    text = _to_text(b"<h1>Title</h1><p>Body.</p><script>x=1</script>", "text/html")
    ok("x=1" not in text, "script contents are dropped")
    ok(text.splitlines() == ["Title", "Body."],
       "block elements stay on their own lines rather than running together")


async def check_stream_folding() -> None:
    print("\nthe event stream folds into the right places")

    async def events():
        for e in [
            {"type": "response.reasoning_summary_part.added"},
            {"type": "response.reasoning_summary_text.delta", "delta": "First I"},
            {"type": "response.reasoning_summary_text.delta", "delta": " check."},
            {"type": "response.reasoning_summary_part.added"},
            {"type": "response.reasoning_summary_text.delta", "delta": "Then I answer."},
            {"type": "response.output_item.done", "item": {
                "type": "reasoning", "id": "rs_1", "encrypted_content": "SEALED"}},
            {"type": "response.output_text.delta", "delta": "It is "},
            {"type": "response.output_text.delta", "delta": "sunny."},
            {"type": "response.output_item.done", "item": {
                "type": "function_call", "call_id": "call_x",
                "name": "get_weather", "arguments": '{"location":"Paris"}'}},
            {"type": "response.completed", "response": {"usage": {
                "input_tokens": 10, "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 25},
                "input_tokens_details": {"cached_tokens": 4}}}},
        ]:
            yield e

    outcome = ResponsesOutcome()
    thinking, answer = [], []
    async for item in consume(events(), outcome):
        (thinking if not isinstance(item, str) else answer).append(item)

    ok("".join(t.text for t in thinking) == "First I check.Then I answer.",
       "thinking text arrives on its own channel")
    ok("".join(answer) == "It is sunny.", "answer text arrives on its own channel")
    ok(sum(1 for t in thinking if t.starts_part) == 2,
       "each reasoning part is marked, so they read as paragraphs")
    ok(outcome.reasoning.summary == "First I check.\n\nThen I answer.",
       f"the assembled summary keeps the paragraph break "
       f"({outcome.reasoning.summary!r})")
    ok(outcome.reasoning.items and
       outcome.reasoning.items[0]["encrypted_content"] == "SEALED",
       "the sealed reasoning is captured for the next turn")
    ok(outcome.reasoning.tokens == 25, "reasoning tokens are read from usage")
    ok(outcome.cached_prompt_tokens == 4, "cached prompt tokens are still read")
    ok([c["name"] for c in outcome.tool_calls] == ["get_weather"],
       "the tool call is captured")
    ok(outcome.finish_reason == "tool_calls", "a pending tool call ends the turn as such")

    async def truncated():
        yield {"type": "response.incomplete", "response": {
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"output_tokens": 8000,
                      "output_tokens_details": {"reasoning_tokens": 7900}}}}

    cut = ResponsesOutcome()
    async for _ in consume(truncated(), cut):
        pass
    ok(cut.finish_reason == "length",
       "running out of room is reported as the loop's own 'length'")
    ok(cut.reasoning.tokens == 7900,
       "and the reasoning tokens that consumed it are visible")


async def check_refusals() -> None:
    """A declined turn has to be recognisable, on all three shapes.

    This cannot be checked by asking the model to refuse — that means writing
    a prompt designed to trip a content filter, which is not a test worth
    having. What can be checked is that every shape Azure reports a refusal
    in is recognised, and that nothing else is mistaken for one.
    """
    print("\na declined turn is recognised, however it arrives")

    from compass.common.gateway.refusals import (
        REFUSED, from_choice, from_error, from_incomplete,
    )
    from compass.common.gateway.responses import ResponsesOutcome, consume

    class Choice:
        def __init__(self, finish, refusal=None, results=None):
            self.finish_reason = finish
            self.message = type("M", (), {"refusal": refusal})()
            self.content_filter_results = results

    filtered = from_choice(
        Choice("content_filter", results={"violence": {"filtered": True, "severity": "high"}}),
        partial=True)
    ok(filtered is not None, "finish_reason 'content_filter' is a refusal")
    ok(filtered and filtered.category == "violence", "the flagged category is read")
    ok(filtered and filtered.partial, "text already shown is remembered as partial")
    ok(filtered and "violence" in filtered.message(),
       f"and it says so: {filtered.message()!r}")

    said = from_choice(Choice("stop", refusal="I can't help with that."), partial=False)
    ok(said is not None and said.explanation == "I can't help with that.",
       "a refusal string on the message is a refusal too")

    ok(from_choice(Choice("stop"), partial=False) is None,
       "an ordinary turn is not mistaken for one")
    ok(from_choice(Choice("length"), partial=False) is None,
       "and neither is one that ran out of room")

    ok(from_incomplete({"reason": "content_filter"}, partial=False) is not None,
       "the Responses API's spelling is recognised")
    ok(from_incomplete({"reason": "max_output_tokens"}, partial=False) is None,
       "running out of room there is still not a refusal")

    ok(from_error("The response was filtered due to the prompt triggering "
                  "Azure OpenAI's content management policy") is not None,
       "a 400 naming the filter is a refusal")
    ok(from_error("The response was filtered due to the prompt triggering "
                  "Azure OpenAI's content management policy. Please modify "
                  "your prompt and retry.") is not None,
       "and so is the prose Azure actually sends, which names no code")
    ok(from_error("Invalid value: 'max'. Supported values are: 'low'") is None,
       "an ordinary 400 is not")
    ok(from_error("context_length_exceeded: too many tokens") is None,
       "and neither is a full context window")

    async def stream():
        yield {"type": "response.output_text.delta", "delta": "Here is th"}
        yield {"type": "response.incomplete", "response": {
            "incomplete_details": {"reason": "content_filter"},
            "usage": {"output_tokens": 12}}}

    outcome = ResponsesOutcome()
    async for _ in consume(stream(), outcome):
        pass
    ok(outcome.finish_reason == REFUSED, "a stopped stream finishes as a refusal")
    ok(outcome.refusal is not None and outcome.refusal.partial,
       "and knows some of the answer was already shown")

    from compass.common.models import events
    ok(events.Refused(message="x").type == "refused",
       "the surface is told with its own event, not an error")


def check_browsing() -> None:
    """A browser may be pointed at the web, and at nothing else.

    The URL can come from a page the agent just read, so it is untrusted. The
    schemes below are not "unexpected sites" — they are a file reader, a
    script injector and a way to render attacker-authored markup as a page.
    """
    print("\nthe browser goes to the web and nowhere else")
    from compass.common.urls import refuse_reason

    for allowed in ("https://example.com", "http://localhost:4310",
                    "http://127.0.0.1:8000/healthz", "example.com"):
        ok(refuse_reason(allowed) == "", f"allowed: {allowed}")

    for blocked in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x",
                    "vbscript:x", "view-source:https://e.com", "chrome://settings",
                    "blob:https://e.com/a"):
        ok(bool(refuse_reason(blocked)), f"refused: {blocked.split(':')[0]}:")

    ok(refuse_reason("") != "", "an empty URL is refused too")


def check_shelf() -> None:
    """A catalogue too large to describe is searched instead — and, crucially,
    a catalogue small enough is not."""
    print("\ntools are held back only when there are too many")
    from pydantic import BaseModel

    from compass.code.mcp.tool_wrapper import MCPTool
    from compass.code.tools.registry import get_all_tools
    from compass.common.tools.shelf import attach, search, visible

    class Fake(MCPTool):
        def __init__(self, name: str, description: str) -> None:
            self.name, self.description = name, description
            self._schema = {"type": "object", "properties": {}}
            self.input_model = type("I", (BaseModel,), {"__annotations__": {}})

    own = get_all_tools()
    small, shelf_small = attach(own)
    shown = visible(small, shelf_small, threshold=24)
    ok([t.name for t in shown] == [t.name for t in own],
       f"below the threshold the list is exactly what it always was ({len(shown)})")
    ok(all(t.name != "find_tools" for t in shown),
       "and no search tool is offered when nothing is hidden")

    many = [*own, *(Fake(f"svc{i}_op{j}", f"Operation {j} on service {i}.")
                    for i in range(10) for j in range(8))]
    big, shelf = attach(many)
    held = visible(big, shelf, threshold=24)
    ok(len(held) < len(many), f"above it, {len(many)} becomes {len(held)}")
    ok(any(t.name == "find_tools" for t in held), "and a way to search is offered")
    kept = {x.name for x in held}
    ok(all(x.name in kept for x in own),
       "Compass's own tools are never held back")

    shelf.remember(["svc3_op2"])
    after = visible(big, shelf, threshold=24)
    ok(any(t.name == "svc3_op2" for t in after),
       "a tool found by searching stays listed afterwards")

    ranked = search(
        [Fake("github_merge_pull_request", "Merge an open pull request."),
         Fake("github_list_repos", "List repositories for the account."),
         Fake("slack_post_message", "Post a message to a Slack channel.")],
        "merge a pull request", limit=2)
    ok(ranked and ranked[0].name == "github_merge_pull_request",
       "the best match ranks first, not the alphabetically first")
    ok(search([Fake("a_b", "c")], "") == [], "an empty query finds nothing")

    off = visible(big, shelf, threshold=0)
    ok(len(off) == len(many), "a threshold of 0 turns the whole thing off")


def main() -> int:
    import asyncio

    check_translation()
    check_reasoning_round_trip()
    check_request_shape()
    check_effort_ladder()
    asyncio.run(check_stream_folding())
    asyncio.run(check_refusals())
    check_browsing()
    check_shelf()
    check_server_tools()
    check_execution_surfaces()
    check_reach()
    check_fetching()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        for f in FAILURES:
            print(f"   {f}")
        return 1
    print("every thinking invariant holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
