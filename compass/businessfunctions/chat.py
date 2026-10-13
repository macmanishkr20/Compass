"""Answering questions about a business function, from the reader's own data.

A SEPARATE AGENT from the rail in `assistant.py`, and deliberately so. That
one turns a sentence into a PROPOSAL somebody confirms, and its whole safety
argument is that a model never chooses rows and never acts. Nothing here
changes that: this agent cannot act at all. It has no confirm path, no
proposal store and no handler call that writes. It reads and it answers.

WHAT IT SEES IS WHAT THE READER SEES. The brief is built by asking each
feature for its figures and rows THROUGH THE READER'S OWN SCOPE, so an
employee's question is answered from one row of the register and
Leadership's from the firm. The model is never handed anything the person
asking could not have read off the screen themselves — which is the only
reason it is safe to let it answer in open prose.

IT ANSWERS FROM THE BRIEF OR IT SAYS IT CANNOT. A model asked about gift
policy will happily invent a plausible threshold, and a plausible wrong
threshold is worse here than no answer: somebody would act on it. So the
prompt gives it the figures and forbids arithmetic it was not given, and a
question the brief cannot answer gets "that is not on these screens"
rather than a guess.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from compass.businessfunctions import people, registry
from compass.businessfunctions import features as _features
from compass.businessfunctions.manifest import FeatureManifest

logger = logging.getLogger("compass.businessfunctions")

#: How much of each feature to put in front of the model. A brief that
#: carried every row of a large register would cost more and answer worse:
#: the figures are what most questions are actually about, and the rows are
#: there for "which ones" and "who".
MOST_ROWS = 40

PROMPT = """You answer questions about one business function inside Compass, \
a workspace used by a firm's own staff. You are given a BRIEF: the figures \
and rows from the screens the person asking is allowed to see, as they stand \
right now.

Answer from the brief and from nothing else.

- Use the figures as given. Do not compute totals, averages or percentages \
that are not in the brief, and never estimate one.
- If the brief does not contain the answer, say so plainly, and say whether \
nobody has recorded it yet or it is simply not something you can see. Do not \
guess, and do not describe how the feature works instead of answering.
- The brief is what THIS person may see, and the screens in it are the only \
ones they can open. NEVER name a screen that is not in the brief, not even to \
suggest where to look: sending somebody to a screen they have no access to is \
a dead end, and naming it tells them what other people can see. Never say or \
imply that there is more you are not showing them.
- You cannot change anything. If they ask you to approve, sign off, send or \
record something, say that the buttons on the row do that and that you only \
read.
- Be short. Two or three sentences, and a figure where there is one. No \
preamble, no bullet lists unless you are naming several things, no headings.
- Indian rupee amounts as they appear in the brief."""


def _one_feature(fn_id: str, feature: FeatureManifest, user: str,
                 entity: str, period: str) -> dict[str, Any] | None:
    """What one screen says, for this reader, or None when it says nothing."""
    handler = _features.get(feature.handler)
    if handler is None:
        return None
    scope = people.scope_for(user, feature, entity=entity, period=period)
    if scope.sees == "none":
        return None
    if scope.missing(feature.scope):
        # A screen still waiting for a selector has no figures to give, and
        # saying so is better than an empty section the model reads as "none".
        return {"screen": feature.name, "waiting_for": scope.missing(feature.scope)}

    try:
        tabs = handler.tabs(scope)
        figures = [{"says": f.caption, "value": f.value} for f in handler.figures(scope)]
        views = []
        for tab in tabs:
            rows = handler.rows(scope, tab.key)
            views.append({
                "view": tab.label,
                "count": len(rows),
                "rows": [{k: v for k, v in r.items() if k not in handler.private}
                         for r in rows[:MOST_ROWS]],
            })
    except Exception:  # noqa: BLE001 — one unhappy screen is not the answer
        logger.warning("business functions: %s could not be read for the "
                       "assistant", feature.id, exc_info=True)
        return None

    return {"screen": feature.name, "about": feature.short,
            "figures": figures, "views": views}


def brief(fn_id: str, user: str, *, entity: str = "", period: str = "",
          only: str = "") -> dict[str, Any]:
    """Everything this person can see in this function, as the model gets it.

    `only` narrows it to one feature — what the rail does when somebody is
    standing on a screen and asking about what is in front of them.
    """
    fn = registry.get(fn_id)
    if fn is None:
        return {}
    screens = []
    for feature in fn.features:
        if only and feature.id != only:
            continue
        said = _one_feature(fn_id, feature, user, entity, period)
        if said is not None:
            screens.append(said)
    return {"function": fn.name, "what_it_is": fn.blurb,
            "period": period or "not chosen", "entity": entity or "not chosen",
            "screens": screens}


def _flatten(value: Any) -> Any:
    """JSON the model can read, with no surprises from a stray object."""
    if isinstance(value, dict):
        return {str(k): _flatten(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_flatten(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def answer(fn_id: str, user: str, question: str, *, entity: str = "",
                 period: str = "", only: str = "") -> str:
    """A sentence answering `question`, from what this reader may see."""
    asked = question.strip()
    if not asked:
        return "Ask me something about what is on these screens."

    said = brief(fn_id, user, entity=entity, period=period, only=only)
    if not said.get("screens"):
        return ("There is nothing here you can see yet — either nothing has "
                "been recorded, or these screens belong to other teams.")

    from compass.common.gateway.azure_client import get_model_client

    payload = json.dumps(_flatten(said), ensure_ascii=False, indent=1)
    try:
        out = await get_model_client().complete_utility(
            PROMPT,
            f"BRIEF:\n{payload}\n\nQUESTION FROM THE PERSON READING THESE "
            f"SCREENS:\n{asked}",
            max_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("business functions: the assistant could not reach a "
                       "model", exc_info=True)
        return ("I could not reach the model just now, so I cannot answer "
                "that one. What is on the screen is still correct.")

    out = (out or "").strip()
    return out or ("I could not put that into words from what these screens "
                   "say. The figures above are the answer if one is there.")
