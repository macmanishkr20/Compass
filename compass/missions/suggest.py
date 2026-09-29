"""Noticing that a request is a build rather than a task.

The Code console and a mission are different machines: a console turn is one
conversation with somebody watching, and a mission is many sessions with
nobody there. Asking a person to know which one their request needs — before
they have made the request — is asking the wrong person.

So the console reads the brief and, when it looks like hours of work, *offers*
to start it as a mission. It offers. It never starts one.

That is the whole design decision here, and it is about asymmetric costs. A
wrong guess towards "mission" spends twenty-five unattended dollars on
something somebody wanted to watch. A wrong guess towards "console" puts six
hours of work in a browser tab that dies with the laptop lid. A suggestion
with a button under it is wrong in neither direction: the expensive, hard to
reverse act stays a decision rather than an inference.

The detection is a heuristic and is meant to be. A model call to classify
every prompt would cost money on every turn to answer a question a few
keywords answer most of the time, and the cost of being wrong is one dismissed
suggestion.
"""

from __future__ import annotations

import re

#: Verbs that introduce something being made, rather than something being
#: looked at or changed. "build me a", "write an app", "create a tool".
_MAKE = re.compile(
    r"\b(build|create|make|write|implement|develop|scaffold|generate)\b",
    re.I)

#: What is being made. A "thing that runs", not a file or a function — the
#: difference between a mission and an afternoon.
_DELIVERABLE = re.compile(
    r"\b(app|application|website|web ?site|site|tool|cli|dashboard|game|"
    r"clone|platform|service|api|backend|frontend|system|tracker|editor|"
    r"portal|marketplace|prototype|mvp)\b", re.I)

#: Signals of size. Any one of these on its own means little; with a verb and
#: a deliverable they are what separates "write a script" from a build.
#:
#: The second line is a different kind of size: not "big" but "runs by itself".
#: Something asked to keep going without anybody there is a service rather than
#: a script, and a service is the shape a mission exists for — it needs a plan,
#: it needs to survive the tab closing, and it is never an afternoon.
_SCOPE = re.compile(
    r"\b(full|complete|end[- ]to[- ]end|production|from scratch|whole|entire|"
    r"with tests|and tests|multi[- ]?page|authentication|auth|database|"
    r"crud|deploy|several features|many features"
    r"|continuous(ly)?|in the background|on a schedule|around the clock|"
    r"24/7|unattended|every (hour|day|minute)|keeps? running|long[- ]running)\b",
    re.I)

#: Things that are plainly not a mission however they are phrased: a question,
#: or work on what is already open. A brief that mentions "this file" is about
#: the conversation in front of it.
_NOT_A_MISSION = re.compile(
    r"^\s*(what|why|how|when|where|who|which|can you|could you|does|do|is|are|"
    r"should|explain|show|tell|check|look|find|search|read)\b"
    r"|\b(this file|this function|the bug|this error|this test|that error|"
    r"the failing|what i just|you just|last change|previous change)\b", re.I)

#: Verbs that describe a thing the software does for its user. Counted, not
#: matched: one of these is a feature, several is a build. This replaced a
#: word-count threshold, which missed "an application that tracks workouts,
#: shows charts, exports to CSV and remembers personal bests" for being 23
#: words long — four capabilities is not a short request, and length was
#: never what made it one.
#:
#: The second and third lines were added after measuring this list against
#: briefs that were not web apps. It had been written out of dashboard
#: vocabulary — shows, exports, lets, filters — so "monitors prices, alerts on
#: drops and compares sellers" scored one, and "scrapes job boards, ranks
#: postings and emails a digest" scored nothing. Those are not small requests;
#: they were simply phrased in the vocabulary of software that acts on the
#: world rather than software that draws a screen.
_CAPABILITY = re.compile(
    r"\b(tracks?|shows?|displays?|lets?|allows?|supports?|exports?|imports?|"
    r"remembers?|stores?|saves?|manages?|sends?|generates?|calculates?|"
    r"filters?|sorts?|searches?|uploads?|downloads?|logs in|signs? in|"
    r"notifies|schedules?|prints?|charts?"
    # software that goes and does something
    r"|monitors?|polls?|watches|scrapes?|crawls?|fetches|retries|alerts?|"
    r"ranks?|compares?|matches|syncs?|emails?|queues?|parses?|validates?|"
    r"aggregates?|summari[sz]es?|detects?|reports?|archives?|backs up"
    # and what it does at the end of that
    r"|purchases?|buys?|orders?|books?|bids?|submits?|applies)\b", re.I)

#: Capabilities before a brief counts as sized on description alone.
_MANY_CAPABILITIES = 3

#: Below this, it is a one-liner whatever words it uses.
_MIN_WORDS = 6


def looks_like_a_mission(prompt: str) -> tuple[bool, str]:
    """Whether to offer a mission, and the reason to show if asked.

    Conservative on purpose: three independent signals have to agree, because
    a suggestion that appears on ordinary requests is one people learn to
    ignore, and an ignored suggestion is worse than none.
    """
    text = (prompt or "").strip()
    if len(text.split()) < _MIN_WORDS:
        return False, ""
    if _NOT_A_MISSION.search(text):
        return False, ""

    makes = bool(_MAKE.search(text))
    deliverable = bool(_DELIVERABLE.search(text))
    if not (makes and deliverable):
        return False, ""

    # A verb and a deliverable alone are "write a CLI that prints the date" —
    # an afternoon. Size is what tips it, and size shows up two ways: words
    # that say so outright, or a list of things the thing has to do.
    scope = bool(_SCOPE.search(text))
    capabilities = len(set(m.group(0).lower() for m in _CAPABILITY.finditer(text)))
    if not scope and capabilities < _MANY_CAPABILITIES:
        return False, ""

    why = ("it asks for something to be built rather than changed, "
           + ("at some size" if scope
              else f"doing {capabilities} separate things"))
    return True, why


def goal_from(prompt: str) -> str:
    """The brief a mission would be created with — the person's own words,
    trimmed. Not a rewrite: they will see this in the card and in the mission,
    and a paraphrase invites an argument about what they meant."""
    return " ".join((prompt or "").split())[:600]
