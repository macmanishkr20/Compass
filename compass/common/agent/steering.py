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
