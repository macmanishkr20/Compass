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
from compass.businessfunctions.features.base import Feature, Scope
from compass.businessfunctions.manifest import FeatureManifest

logger = logging.getLogger("compass.businessfunctions")

#: How many rows of one screen to put in front of the model, and how much
#: of the whole brief to build before trimming. A register of ten thousand
#: items cannot go into a prompt; what matters is that the cap is said OUT
#: LOUD in the brief when it bites, so the model answers "the first 200 of
#: 10,000" rather than treating a truncated list as the whole firm.
MOST_ROWS = 300
MOST_CHARS = 90_000

PROMPT = """You answer questions about one business function inside Compass, \
a workspace used by a firm's own staff. You are given a BRIEF: the figures \
and rows from the screens the person asking is allowed to see, as they stand \
right now.

Answer from the brief and from nothing else.

- Use the figures as given. Do not compute totals, averages or percentages \
that are not in the brief, and never estimate one.
- The brief covers EVERY period these screens hold, not just the one the \
person has selected. Each screen is given per period. Say which period a \
figure belongs to whenever more than one could be meant, and when asked \
about "this year" use the period shown as on_screen_now.
- A row list marked `only_the_first` is CUT: there are `rows_in_all` of \
them. Answer from the counts and figures in that case and say the list is \
partial. Never present a cut list as everything there is.
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


def _periods(feature: FeatureManifest) -> list[str]:
    """Every period this screen can be read at, not only the one on screen.

    "How many gifts has John had" is not a question about the year somebody
    happens to have selected, and an assistant that silently answered for
    one year would be wrong in the most convincing possible way.
    """
    if "period" not in feature.scope:
        return [""]
    return list(feature.choices.get("period", [])) or [""]


def _one_period(feature: FeatureManifest, handler: Feature, scope: Scope
                ) -> dict[str, Any] | None:
    """One screen at one period, for this reader."""
    if scope.missing(feature.scope):
        return None
    try:
        tabs = handler.tabs(scope)
        if not tabs:
            return None
        figures = [{"says": f.caption, "value": f.value}
                   for f in handler.figures(scope)]
        # Every view's COUNT, because the counts are themselves answers —
        # but the rows only once, off the widest view. The narrow tabs are
        # subsets of it, and sending the same row four times spends the
        # budget that a second year of data should have had.
        counted = [(t, handler.rows(scope, t.key)) for t in tabs]
        widest = max(counted, key=lambda pair: len(pair[1]))
        rows = widest[1]
        return {
            "period": scope.period or "not scoped",
            "figures": figures,
            "views": [{"view": t.label, "count": len(r)} for t, r in counted],
            "rows_are_from": widest[0].label,
            "rows_in_all": len(rows),
            "rows": [{k: v for k, v in r.items() if k not in handler.private}
                     for r in rows[:MOST_ROWS]],
            **({"only_the_first": MOST_ROWS} if len(rows) > MOST_ROWS else {}),
        }
    except Exception:  # noqa: BLE001 — one unhappy screen is not the answer
        logger.warning("business functions: %s could not be read for the "
                       "assistant", feature.id, exc_info=True)
        return None


def _one_feature(fn_id: str, feature: FeatureManifest, user: str,
                 entity: str, period: str) -> dict[str, Any] | None:
    """What one screen says ACROSS EVERY PERIOD, for this reader.

    The scope is built per period through `people.scope_for`, so the
    narrowing is still the handler's and this module never re-derives who
    may see what. That matters more than it looks: a second path to the
    rows would be a second place for a privacy rule to be wrong.
    """
    handler = _features.get(feature.handler)
    if handler is None:
        return None
    if people.breadth(user, feature.visibility) == "none":
        return None

    years = [period] if period else []
    for year in _periods(feature):
        if year not in years:
            years.append(year)

    read = []
    for year in years:
        scope = people.scope_for(user, feature, entity=entity, period=year)
        if scope.sees == "none":
            return None
        said = _one_period(feature, handler, scope)
        if said is not None:
            read.append(said)
    if not read:
        return {"screen": feature.name,
                "waiting_for": list(people.scope_for(
                    user, feature, entity=entity).missing(feature.scope))}
    return {"screen": feature.name, "about": feature.short, "periods": read}


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
    said = {"function": fn.name, "what_it_is": fn.blurb,
            "covers": "every period these screens hold, not only the one on "
                      "screen",
            "on_screen_now": period or "no period chosen",
            "entity": entity or "not chosen",
            "screens": screens}
    return _within_budget(said)


def _within_budget(said: dict[str, Any]) -> dict[str, Any]:
    """Trim the brief to something a prompt can hold, and SAY that it was.

    Rows go before figures and counts, because a count survives trimming
    and answers most questions; and the trimming is recorded in the brief
    so the model can say "the first 40 of 900" instead of reading a cut
    list as the whole firm.
    """
    if len(json.dumps(_flatten(said), ensure_ascii=False)) <= MOST_CHARS:
        return said
    for keep in (120, 60, 25, 10, 0):
        for screen in said.get("screens", []):
            for era in screen.get("periods", []):
                if era.get("rows_in_all", 0) > keep:
                    era["rows"] = era["rows"][:keep]
                    era["only_the_first"] = keep
        said["trimmed"] = ("Too much to send whole. The counts and figures "
                           "are complete; the row lists are cut, and each "
                           "says how many it is showing of how many.")
        if len(json.dumps(_flatten(said), ensure_ascii=False)) <= MOST_CHARS:
            break
    return said


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
