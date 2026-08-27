"""When the model declines, and how Compass is told.

A declined turn is not an error. It arrives as a perfectly successful
response whose reason for stopping happens to be a refusal, which means code
that watches only for exceptions never sees it: the turn ends, the answer is
short or empty, and the person reading it is told nothing. That is the failure
this module exists to prevent — the turn should say it was declined, and why.

Azure reports the same thing in three places, and Compass has to recognise all
of them:

  * a streamed turn stops with `finish_reason: "content_filter"`, sometimes
    after some of the answer has already been shown;
  * a structured-output turn returns a `refusal` string on the message instead
    of content;
  * a prompt that trips the filter never starts at all, and comes back as a
    400 whose error code is `content_filter`.

The Responses API spells the first of these `incomplete_details.reason:
"content_filter"`. All four are one thing to the rest of Compass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: What `finish_reason` says when the filter stopped the answer. Kept as the
#: gateway's own name so nothing downstream has to know whose spelling it is.
REFUSED = "refusal"

#: Azure's names for the same thing, across the APIs and the error body. The
#: last two are prose rather than codes: a refused prompt comes back as a 400
#: whose message reads "filtered due to the prompt triggering Azure OpenAI's
#: content management policy", which contains none of the machine names.
_FILTER_WORDS = (
    "content_filter",
    "contentfilter",
    "responsibleaipolicy",
    "content management policy",
    "content filter",
)

#: The categories Azure reports on, in the order worth mentioning.
_CATEGORIES = ("hate", "sexual", "violence", "self_harm", "jailbreak",
               "protected_material_code", "protected_material_text", "profanity")


@dataclass
class Refusal:
    """Why a turn was declined, in terms a person can be shown.

    `partial` matters: the filter can stop a turn after some of the answer has
    already been streamed, and the reader has seen it. Pretending it was never
    there is its own kind of lie, so the surface is told and decides.
    """

    #: What tripped it, when Azure says. Empty when it does not.
    category: str = ""
    #: A sentence for the reader.
    explanation: str = ""
    #: Text had already been shown when the turn was stopped.
    partial: bool = False

    def message(self) -> str:
        """The line to show. Never empty: a refusal with no detail is still a
        refusal, and saying nothing is what this exists to stop."""
        if self.explanation:
            said = self.explanation
        elif self.category:
            said = (
                "The response was stopped by the content filter "
                f"({self.category.replace('_', ' ')})."
            )
        else:
            said = "The response was stopped by the content filter."
        if self.partial:
            said += " What is shown above is the part that was written first."
        return said


def looks_like_filter(text: str) -> bool:
    """Whether an error body is the content filter rather than a real fault."""
    low = (text or "").lower()
    return any(word in low for word in _FILTER_WORDS)


def _category_from(results: Any) -> str:
    """The category Azure flagged, out of whichever shape it used.

    `content_filter_results` is a mapping of category to a small record with
    `filtered` and `severity`. Only the filtered ones are worth naming.
    """
    if not isinstance(results, dict):
        return ""
    flagged = []
    for name in _CATEGORIES:
        entry = results.get(name)
        if isinstance(entry, dict) and entry.get("filtered"):
            flagged.append(name)
    if not flagged:
        # Some shapes report the offending category without a `filtered` flag.
        flagged = [n for n in _CATEGORIES if results.get(n) is True]
    return ", ".join(flagged)


def from_choice(choice: Any, *, partial: bool) -> Refusal | None:
    """Read a refusal off a chat-completions choice, if that is what it is."""
    finish = getattr(choice, "finish_reason", None)
    message = getattr(choice, "message", None)
    said = getattr(message, "refusal", None) if message is not None else None
    if finish != "content_filter" and not said:
        return None
    results = getattr(choice, "content_filter_results", None)
    if results is None and hasattr(choice, "model_dump"):
        try:
            results = choice.model_dump().get("content_filter_results")
        except Exception:  # noqa: BLE001 — a missing field is not a failure
            results = None
    return Refusal(
        category=_category_from(results),
        explanation=str(said or ""),
        partial=partial,
    )


def from_incomplete(details: Any, *, partial: bool) -> Refusal | None:
    """Read a refusal off the Responses API's `incomplete_details`."""
    reason = (details or {}).get("reason", "") if isinstance(details, dict) else ""
    if not looks_like_filter(reason):
        return None
    return Refusal(category="", explanation="", partial=partial)


def from_error(message: str) -> Refusal | None:
    """Read a refusal off a 400 whose body names the content filter.

    This is the one that fires before any answer exists: the prompt itself was
    declined, so nothing has been shown and there is nothing partial about it.
    """
    if not looks_like_filter(message):
        return None
    category = ""
    low = (message or "").lower()
    for name in _CATEGORIES:
        if name in low:
            category = name
            break
    return Refusal(
        category=category,
        explanation="The request was stopped by the content filter before "
                    "the model answered.",
        partial=False,
    )
