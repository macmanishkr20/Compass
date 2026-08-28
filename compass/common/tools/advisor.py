"""A second opinion, bought one question at a time.

Thinking harder is not free, and it is not free in a way that matters: `high`
effort can spend several times the reasoning tokens `medium` does on the same
turn. So Compass runs at `medium`, which is right for the great majority
of turns — reading a file, running a command, making an edit that follows from
the last one. Raising the default to cover the few turns that genuinely need
more would mean paying that premium on all the ones that do not.

The advisor is the other way round. The loop keeps running cheaply, and when
it hits something it cannot see its way through, it asks one expensive
question and gets one considered answer back. The cost lands on the turns that
earn it.

On Anthropic's stack this pattern is a smaller model calling a larger one.
Here the dial is effort rather than model — gpt-5 is already the best thing on
the resource — so the advisor is the same deployment thinking as hard as the
resource allows. That is a real difference in behaviour rather than a cosmetic
one: at `high` the model almost always reasons, and reasons for longer, before
it commits to an answer.

The level comes from settings rather than being written in here, because what
the deployment accepts is a measured fact that changes. It was `xhigh` until
the resource was asked and said otherwise.

What comes back is advice, not action. The advisor cannot read files, run
commands or edit anything; it sees only what the caller chose to tell it. That
is deliberate — it keeps the expensive call to one round trip, and it keeps
the agent doing the work responsible for checking the advice against the
repository before acting on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator

from pydantic import BaseModel, Field

from compass.common.config import get_settings
from compass.common.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield

if TYPE_CHECKING:  # pragma: no cover
    pass

#: What the advisor is told it is. It is answering a colleague mid-task, not
#: writing documentation: the caller is blocked right now and needs a
#: recommendation it can act on, with the reasoning that makes it checkable.
ADVISOR_PROMPT = """\
You are advising another engineer who is in the middle of a task and has hit \
something they cannot resolve. They have paid for your time deliberately \
because the question is hard, so answer it properly.

You cannot see their repository. Work from what they have told you, and where \
a detail would change your answer, say which detail and what each way would \
imply — do not stall waiting for it.

Give a recommendation, not a survey. Say what you would do, why, and what \
would tell them it was wrong. Be concrete: name the approach, the trade-off \
it accepts, and the first thing to check. If their framing of the problem is \
itself the mistake, say so plainly and give the better framing.\
"""


class ConsultInput(BaseModel):
    question: str = Field(
        description="What you are stuck on, asked directly. A specific "
                    "question gets a specific answer; 'what should I do' gets "
                    "a survey. Say what you have already ruled out."
    )
    context: str = Field(
        default="",
        description="Everything the advisor needs and cannot see: the relevant "
                    "code, the error, what you have already tried and how it "
                    "failed, the constraints you are working under. It has no "
                    "access to the repository, so anything you leave out is "
                    "something it will have to guess.",
    )


class ConsultTool(Tool):
    """Ask the same model, thinking as hard as the deployment allows."""

    name = "consult"
    description = (
        "Get a second opinion on something you are stuck on, from the same "
        "model reasoning at maximum effort. Use it when you have tried an "
        "approach and it did not work, when two designs both look defensible "
        "and the choice matters, or when a bug's cause is not where the "
        "symptom is. It costs noticeably more than a normal turn and takes "
        "longer, so it is worth it for a genuine impasse and wasteful for "
        "anything you could work out by reading another file. The advisor "
        "cannot see the repository or run anything — put what it needs in "
        "`context`, and check its advice against the actual code before you "
        "act on it."
    )
    input_model = ConsultInput

    def is_read_only(self, inp: ConsultInput) -> bool:
        return True  # it produces words, and touches nothing

    def is_concurrency_safe(self, inp: ConsultInput) -> bool:
        return True

    def check_tool_permissions(
        self, inp: ConsultInput, ctx: ToolUseContext
    ) -> None:
        return None  # no gate: asking a question changes nothing

    async def call(
        self, inp: ConsultInput, ctx: ToolUseContext
    ) -> AsyncIterator[ToolYield]:
        from compass.common.gateway.azure_client import get_model_client

        settings = get_settings()
        effort = settings.thinking.advisor_effort
        asked = inp.question.strip()
        if inp.context.strip():
            asked += f"\n\nWhat I can tell you:\n{inp.context.strip()}"

        try:
            answer, trace = await get_model_client().complete_reasoning(
                prompt=ADVISOR_PROMPT,
                text=asked,
                max_tokens=settings.context.max_output_tokens,
                deployment=settings.azure.deployment,
                effort=effort,
            )
        except Exception as err:  # noqa: BLE001
            # A failed consultation is a dead end for this tool, not for the
            # turn: the caller should carry on with its own judgement rather
            # than treat the task as blocked.
            yield ToolOutput(
                f"The advisor could not be reached ({type(err).__name__}: "
                f"{err}). Continue with your own judgement.",
                is_error=True,
            )
            return

        if not (answer or "").strip():
            yield ToolOutput(
                "The advisor returned nothing. Continue with your own "
                "judgement.", is_error=True)
            return

        # The cost is named because it is the whole reason this is a separate
        # tool rather than the default: an agent that cannot see what a call
        # costs cannot be expected to spend it well.
        yield ToolOutput(
            f"Advice (gpt-5 at {effort} effort, "
            f"{trace.tokens:,} reasoning tokens):\n\n{answer.strip()}"
        )
