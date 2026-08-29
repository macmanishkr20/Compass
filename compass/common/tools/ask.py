"""Asking the person, when guessing would be worse than pausing.

Most of the time an agent should decide and get on with it. A question costs a
round trip and the reader's attention, and one asked about something the code
already answers is just noise.

But some choices are not the agent's to make. Two designs are both defensible
and the difference is a preference nobody has stated; a instruction reads two
ways and the readings lead to different work; a destructive step is about to
happen and nobody has said which of several things to destroy. Guessing there
does not save anyone time — it produces work that has to be thrown away, and
the throwing away is the expensive part.

So this is deliberately narrow. It offers two to four options, each with a
label and a sentence saying what picking it means, and the person may write
their own answer instead. It is not a chat box, and it is not a way to make
the reader do the thinking: an option list is only useful when the agent has
already worked out what the choices are.

The mechanism is the permission gate's, because the shape is the same: the
loop stops on a future, a surface renders the question, an endpoint resolves
it. The difference is what is being asked — may I, versus which — and the two
are kept apart so a surface can render them differently, as claude.ai does.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from compass.common.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield

#: Fewer than two is not a choice; more than four is a form. The same bounds
#: the model-facing tool uses, for the same reason: a list long enough to need
#: scrolling is one the asker had not finished thinking about.
MIN_OPTIONS = 2
MAX_OPTIONS = 4


class AskOption(BaseModel):
    label: str = Field(
        description="The choice itself, in one to five words — what the button "
                    "says. Not a sentence."
    )
    description: str = Field(
        description="What picking this actually means: the work it implies, or "
                    "the trade-off it accepts. A person should be able to "
                    "choose from the descriptions without asking you to "
                    "explain them."
    )


class AskInput(BaseModel):
    question: str = Field(
        description="The question, complete and specific, ending in a question "
                    "mark. Say what you have already established, so the "
                    "answer is the only thing left to supply."
    )
    header: str = Field(
        default="",
        description="A two-or-three word label for what is being decided — "
                    "'Auth method', 'Scope', 'Library'. Shown as a chip.",
    )
    options: list[AskOption] = Field(
        description=f"Between {MIN_OPTIONS} and {MAX_OPTIONS} distinct "
                    "choices. Put the one you would recommend first. Do not "
                    "add an 'Other' option — one is always offered."
    )
    multi_select: bool = Field(
        default=False,
        description="True when more than one option can sensibly be chosen at "
                    "once. Phrase the question accordingly.",
    )


class AskUserTool(Tool):
    """Put a decision to the person, with the options already worked out."""

    name = "ask_user"
    description = (
        "Ask the person a question when the answer is genuinely theirs to give "
        "and guessing would produce work that has to be redone. Use it when two "
        "approaches are both defensible and the choice turns on a preference "
        "nobody has stated, when an instruction reads two ways and the readings "
        "lead to different work, or before something destructive where several "
        "targets would fit. Offer two to four options, best first, each with a "
        "sentence saying what choosing it means; the person can also write their "
        "own answer. Do not use it for anything the code, the files or the "
        "conversation already answer — read those instead — and do not use it to "
        "hand back a decision you were asked to make. Asking costs a round trip "
        "and the reader's attention, so ask once, about the thing that actually "
        "blocks you."
    )
    input_model = AskInput

    def is_read_only(self, inp: AskInput) -> bool:
        return True  # a question changes nothing

    def is_concurrency_safe(self, inp: AskInput) -> bool:
        # Two questions at once would race for the same screen, and the person
        # would answer them in an order nobody chose.
        return False

    def check_tool_permissions(self, inp: AskInput, ctx: ToolUseContext) -> None:
        return None  # asking needs no permission; it *is* the asking

    def wants_answer(self, inp: AskInput) -> dict[str, Any] | None:
        """The payload the surface renders. Returning it stops the executor.

        Validation lives here rather than on the schema so a badly formed
        question comes back to the model as an ordinary tool error it can fix,
        instead of a 422 that ends the turn.
        """
        options = [
            {"label": o.label.strip(), "description": o.description.strip()}
            for o in inp.options
            if o.label.strip()
        ]
        return {
            "question": inp.question.strip(),
            "header": inp.header.strip(),
            "options": options,
            "multi_select": bool(inp.multi_select),
        }

    async def call(
        self, inp: AskInput, ctx: ToolUseContext
    ) -> AsyncIterator[ToolYield]:
        # Unreachable in the normal path: `wants_answer` returns a payload, so
        # the executor answers the question and never calls this. It exists so
        # the tool is still a complete Tool if that ever changes.
        yield ToolOutput(
            "The question was not put to anyone, so it has no answer. "
            "Continue with your own judgement.",
            is_error=True,
        )


def format_answer(payload: dict[str, Any], reply: dict[str, Any] | None) -> str:
    """What the model is told once the question has been resolved.

    A skipped or unanswered question says so plainly and tells the model to
    carry on. The failure to avoid is a turn that stalls waiting for something
    that is never coming, or one that treats silence as agreement.
    """
    if not reply:
        return (
            "Nobody answered — the question was skipped or nothing was "
            "watching. Do not ask it again. Choose the option you would have "
            "recommended, say which you chose and why, and continue."
        )
    chosen = [str(c) for c in (reply.get("chosen") or []) if str(c).strip()]
    other = str(reply.get("other") or "").strip()
    parts: list[str] = []
    if chosen:
        parts.append("They chose: " + ", ".join(chosen) + ".")
    if other:
        parts.append(f"They wrote: {other}")
    if not parts:
        return (
            "The question came back with no choice made. Choose the option you "
            "would have recommended, say so, and continue."
        )
    return " ".join(parts) + " Take that as decided and carry on."
