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
    print("\nthe effort ladder matches what the API accepts")
    t = ThinkingSettings()
    ok(EFFORT_LEVELS == ("low", "medium", "high", "xhigh"),
       f"the ladder is {EFFORT_LEVELS}")
    ok(t.normalize_effort("minimal") == "low", "'minimal' from an old session becomes low")
    ok(t.normalize_effort("max") == "xhigh", "'max' becomes the highest Azure has")
    ok(t.normalize_effort("nonsense") is None, "an unknown level is dropped, not sent")
    ok(t.normalize_effort(None) is None, "no effort stays no effort")
    ok(t.reasons("gpt-5") and not t.reasons("gpt-4o-mini"),
       "only reasoning deployments take the reasoning path")


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


def main() -> int:
    import asyncio

    check_translation()
    check_reasoning_round_trip()
    check_request_shape()
    check_effort_ladder()
    asyncio.run(check_stream_folding())
    asyncio.run(check_refusals())

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
