"""Asking for more thinking, or less, without changing the request.

Effort is the calibrated control and should be the first thing reached for.
But effort is part of the cached prompt prefix, so changing it mid-conversation
throws the cache away and re-sends everything — which makes it the wrong tool
for "think harder about this one message". Words appended to the newest user
turn steer the same decision and leave every earlier cache breakpoint intact.

The phrasings below are the ones the model-facing guidance gives, kept
verbatim. Steering is sensitive to exact wording, so these are not paraphrased
to taste; if one stops working, measure a replacement rather than reword it.
"""

from __future__ import annotations

#: Appended to a single user turn to encourage thinking on that turn only.
THINK_MORE = "Please think hard before responding."

#: Appended to a single user turn to suppress it.
THINK_LESS = "Answer directly without deliberating."

#: Added to a system prompt to lower the threshold for every turn that follows.
ENCOURAGE_THINKING = (
    "This task involves multistep reasoning. Think carefully before responding."
)

#: Added to a system prompt to raise it.
DISCOURAGE_THINKING = (
    "Extended thinking adds latency and should only be used when it "
    "will meaningfully improve answer quality, typically for problems "
    "that require multistep reasoning. When in doubt, respond directly."
)

#: What a caller may ask for. Anything else is ignored rather than guessed at.
HINTS = ("more", "less")


def answer_in(language: str | None) -> str:
    """The line that fixes the language of every reply.

    A model infers the language from the conversation, which is usually right
    and occasionally not — a question asked in English about a French document
    can come back in either. Where an application lets someone choose, the
    guidance is to say so in the system prompt rather than leave it to
    inference, because the system prompt is the one place the instruction
    survives every turn.

    Empty for no choice, which leaves the inference alone. That is the right
    default: naming a language nobody asked for is worse than guessing well.
    """
    if not language:
        return ""
    name = language.strip()
    if not name:
        return ""
    return (
        f"Always respond in {name}, regardless of the language the user "
        f"writes in. Use idiomatic {name} as a native speaker would, in its "
        "own script."
    )


#: Below this share of the budget, saying anything is noise. A model told it
#: has 94% of its context left learns nothing it can act on.
BUDGET_QUIET_BELOW = 0.60


def budget_note(used: int, budget: int) -> str:
    """What to tell the model about the room it has left, or "" while there is
    plenty.

    Claude's models get this injected by the API — a token budget in the
    system prompt and a running `<system_warning>` after each tool call — so
    that a long task can be paced against the space that remains instead of
    running until it stops. Nothing injects it here, so Compass says it.

    It goes in as an operator-level message placed after the conversation so
    far, rather than as an edit to the prompt at the front. Measured on this
    resource: a mid-conversation `system` message is obeyed (an instruction
    added after two turns changed the next answer), and because it is appended
    rather than prepended, the cached prefix in front of it is untouched.

    Phrased as a fact and not an order. The guidance is explicit that stating
    what changed works better than telling the model what to do about it, and
    a hard instruction here would also be wrong: whether to wrap up or keep
    going is the task's business, not the budget's.
    """
    if budget <= 0 or used <= 0:
        return ""
    if used < int(budget * BUDGET_QUIET_BELOW):
        return ""
    left = max(budget - used, 0)
    note = (
        f"Context usage: {used:,} of {budget:,} tokens; {left:,} remaining "
        "before this conversation is summarised and earlier detail is lost."
    )
    if left <= int(budget * 0.15):
        note += (
            " Very little room is left. Prefer finishing what is in progress "
            "and recording anything that must survive over starting new work."
        )
    return note


def steer(content: str, hint: str | None) -> str:
    """Append the phrase for `hint` to one user message.

    Returns `content` untouched for no hint or an unknown one: steering is an
    addition to what the user said, never a replacement for it, and a typo
    should change nothing rather than something.
    """
    if not hint:
        return content
    phrase = {"more": THINK_MORE, "less": THINK_LESS}.get(hint.strip().lower())
    if not phrase:
        return content
    return f"{content}\n\n{phrase}" if content.strip() else phrase


def threshold_guidance(posture: str | None) -> str:
    """The system-prompt line that shifts thinking for a whole conversation.

    Empty for no posture, which leaves the model's own judgement alone — the
    right default, since it is the thing actually looking at the request.
    """
    if not posture:
        return ""
    return {
        "more": ENCOURAGE_THINKING,
        "less": DISCOURAGE_THINKING,
    }.get(posture.strip().lower(), "")
