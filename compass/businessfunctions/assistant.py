"""The rail: turning a sentence into a proposal a person can refuse.

The assistant beside a feature can explain what is on screen and operate it.
Operating it is the dangerous half, so it happens in two moves that cannot be
collapsed into one:

    interpret()   reads the sentence, resolves which rows it means, and
                  returns a PROPOSAL. Nothing has changed.
    confirm()     performs it, once, for the person who made it.

Between those two a human reads what would happen and the rows light up on the
stage. That gap is the feature. A single call that both decided and acted
would be the same design with the safety removed.

WHAT THE MODEL IS FOR, AND WHAT IT IS NOT. The model does not choose rows and
it does not perform actions. This module resolves "all three" or "Arjun's"
against the rows actually in scope, and the handler performs. What is left for
a model is phrasing: an `Answer` carries the facts already gathered, and a
model turns them into a sentence. Narrowing its job to phrasing is what makes
a wrong answer a clumsy sentence rather than a wrong row.

FIVE THINGS A PROPOSAL GUARANTEES, each of which is a way this goes wrong:

  * it belongs to one person — somebody else holding the id cannot confirm it
  * it expires, because a plan read twenty minutes ago describes a screen
    that may have moved
  * it is single-use, so a double-click is not a double-approval
  * it is re-checked at confirm against current state, because the rows may
    have changed since it was made
  * an outward action covering several rows is never proposed at all —
    notifying four people is four decisions

The store is in memory. A proposal is a few seconds of intent, not a record:
losing them on restart is correct, and persisting them would mean a plan
could outlive the screen it was made against.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from compass.businessfunctions.features.base import Feature, Outcome, Scope
from compass.businessfunctions.features.rewardlens import rupees
from compass.businessfunctions.manifest import Action, FeatureManifest

logger = logging.getLogger("compass.businessfunctions")

#: How long a plan stays confirmable. Long enough to read and think, short
#: enough that it still describes the screen it was made against.
TTL_SECONDS = 10 * 60

#: A ceiling on how many rows one proposal may cover. Past this nobody is
#: reading the list they are agreeing to, and "accept everything" should be a
#: deliberate act with its own wording rather than a sentence that happened to
#: match a lot of rows.
MAX_TARGETS = 25


@dataclass(frozen=True)
class Target:
    """One row a plan would touch, with the words a person recognises it by."""

    id: str
    label: str


@dataclass(frozen=True)
class Proposal:
    """A plan, made and not yet carried out."""

    id: str
    user: str
    function_id: str
    feature_id: str
    action: str
    #: "Accept the lower credit on 3 lines" — what the card says.
    headline: str
    #: The manifest's own sentence about what happens. Written, not generated.
    detail: str
    targets: list[Target]
    outward: bool
    reversible: bool
    scope: Scope
    made_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.made_at > TTL_SECONDS

    @property
    def target_ids(self) -> list[str]:
        return [t.id for t in self.targets]


@dataclass(frozen=True)
class Answer:
    """A question this module could resolve without changing anything.

    `facts` is what was read off the feature — figures, the rows in question.
    A model phrases it. It is carried rather than rendered here so the phrasing
    layer cannot quietly invent a figure that was never in the data.
    """

    text: str
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Clarify:
    """The sentence named an action but not clearly enough which rows.

    Returned instead of guessing. An assistant that picks the most likely row
    is right most of the time, and the times it is wrong are the times
    somebody's leave gets approved by accident.
    """

    text: str
    options: list[Target] = field(default_factory=list)


#: proposal id -> proposal. Module-level and in memory on purpose; see above.
_pending: dict[str, Proposal] = {}


def _sweep() -> None:
    """Drop what has expired. Cheap, and keeps the dict from growing."""
    for pid in [p for p, x in _pending.items() if x.expired]:
        _pending.pop(pid, None)


# ──────────────────────────────────────────────────────────────────────────
# resolving a sentence
# ──────────────────────────────────────────────────────────────────────────

#: Words that mean "everything you just showed me".
_ALL = re.compile(r"\b(all|every|each|both|the lot)\b", re.I)
#: "all three", "both of them" — a count the person stated, which is checked
#: against what was actually found. A mismatch is a clarification, not a
#: silent correction: if they said three and there are four, they are looking
#: at a different screen from the one the request will act on.
_COUNTS = {"one": 1, "two": 2, "both": 2, "three": 3, "four": 4, "five": 5,
           "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _stated_count(text: str) -> int | None:
    if m := re.search(r"\b(\d+)\b", text):
        return int(m.group(1))
    for word, n in _COUNTS.items():
        if re.search(rf"\b{word}\b", text, re.I):
            return n
    return None


def _match_action(text: str, actions: list[Action]) -> Action | None:
    """The action this sentence asks for, or None.

    Matched on the manifest's own label and id rather than on a list of verbs
    kept here, so a function that renames "Accept the lower credit" does not
    have to come and tell this module about it.
    """
    low = text.lower()
    best: tuple[int, Action] | None = None
    for action in actions:
        words = [w for w in re.split(r"\W+", action.label.lower()) if len(w) > 3]
        score = sum(1 for w in words if w in low)
        if action.id.lower() in low:
            score += 2
        if score and (best is None or score > best[0]):
            best = (score, action)
    return best[1] if best else None


def _label_for(feature_id: str, row: dict[str, Any]) -> tuple[str, str]:
    """A row's id and the words a person recognises it by.

    Feature-specific, and deliberately not the handler's job: this is how a
    row reads *in a sentence about it*, which is a different question from how
    it reads in a table.
    """
    if feature_id == "form26":
        diff = row.get("difference", 0)
        return row["line"], (f"line {row['line']} · {row['deductor']} · "
                             f"−₹{diff / 100000:.1f}L")
    if feature_id == "lms":
        return row["id"], f"{row['who']} · {row['dates']} · {row['kind']}"
    if feature_id == "rewardlens":
        # The row is a person, so the id is the person and the label leads
        # with the name — that is the only part of it anybody says out loud.
        return row["employee_id"], (f"{row['recipient']} · {rupees(row['total'])}"
                                    f" of {rupees(row['limit'])}")
    # A feature whose rows the rail cannot name is a feature the rail cannot
    # operate: `_pick` would match nothing and every sentence would come back
    # as a clarification with blank options. Say so rather than degrade —
    # `check_every_feature_can_be_named` catches it before it ships.
    raise KeyError(f"no row label defined for feature {feature_id!r}")


def _candidates(feature: FeatureManifest, handler: Feature,
                scope: Scope) -> list[dict[str, Any]]:
    """The rows an action could sensibly apply to right now.

    The first tab is the one that holds what needs deciding, which is also
    what the person is looking at when they ask.
    """
    tabs = handler.tabs(scope)
    return handler.rows(scope, tabs[0].key) if tabs else []


def _pick(text: str, feature: FeatureManifest, rows: list[dict[str, Any]]
          ) -> tuple[list[Target], str]:
    """Which rows the sentence means, and why — "" when it is unambiguous."""
    targets = [Target(*_label_for(feature.id, r)) for r in rows]

    # An explicit id always wins. "line 0044", "0044".
    named = [t for t in targets if re.search(rf"\b{re.escape(t.id)}\b", text)]
    if named:
        return named, ""

    # A person's name, for features whose rows are people.
    by_name = [t for t in targets
               if (first := t.label.split()[0].rstrip("·").lower())
               and len(first) > 2 and re.search(rf"\b{re.escape(first)}", text, re.I)]
    if by_name:
        return by_name, ""

    if _ALL.search(text):
        stated = _stated_count(text)
        if stated is not None and stated != len(targets):
            return targets, (
                f"You said {stated}, and there {'is' if len(targets) == 1 else 'are'} "
                f"{len(targets)} here. Which did you mean?"
            )
        return targets, ""

    if len(targets) == 1:
        return targets, ""
    return targets, "Which one do you mean?"


def interpret(fn_id: str, feature: FeatureManifest, handler: Feature,
              scope: Scope, text: str, user: str) -> Proposal | Answer | Clarify:
    """Read a sentence against what is on screen. Changes nothing."""
    text = (text or "").strip()
    if not text:
        return Answer("Ask me about what is on screen, or tell me what to do.")

    action = _match_action(text, feature.actions)
    if action is None:
        rows = _candidates(feature, handler, scope)
        return Answer(
            text="",
            facts={
                "figures": [(f.key, f.value, f.caption) for f in handler.figures(scope)],
                "rows": rows,
                "rules": handler.rules(scope),
            },
        )

    rows = _candidates(feature, handler, scope)
    if not rows:
        return Answer(f"There is nothing here to {action.label.lower()}.")

    targets, why = _pick(text, feature, rows)
    if why:
        return Clarify(why, targets)

    if len(targets) > MAX_TARGETS:
        return Clarify(
            f"That is {len(targets)} rows. I will not propose more than "
            f"{MAX_TARGETS} at once — narrow it, or do it from the table.",
            targets[:MAX_TARGETS],
        )

    # An outward act covering several rows is never shown as one plan. The
    # handler would refuse it anyway; refusing here means nobody is offered a
    # button that cannot work.
    if action.outward and len(targets) > 1:
        return Clarify(
            f"{action.label} reaches past Compass, so it goes one at a time. "
            f"Which of these {len(targets)}?",
            targets,
        )

    noun = "row" if len(targets) == 1 else "rows"
    proposal = Proposal(
        id=uuid.uuid4().hex[:12],
        user=user,
        function_id=fn_id,
        feature_id=feature.id,
        action=action.id,
        headline=(action.label if len(targets) == 1
                  else f"{action.label} on {len(targets)} {noun}"),
        detail=action.confirm,
        targets=targets,
        outward=action.outward,
        reversible=action.reversible,
        scope=scope,
    )
    _sweep()
    _pending[proposal.id] = proposal
    return proposal


# ──────────────────────────────────────────────────────────────────────────
# carrying it out
# ──────────────────────────────────────────────────────────────────────────

def pending(proposal_id: str, user: str) -> Proposal | None:
    """A proposal this person may still act on, or None.

    One lookup for both confirm and cancel so the ownership and expiry rules
    cannot be enforced in one and forgotten in the other.
    """
    _sweep()
    proposal = _pending.get(proposal_id)
    if proposal is None or proposal.expired or proposal.user != user:
        return None
    return proposal


def confirm(proposal_id: str, user: str, handler: Feature) -> Outcome:
    """Carry out a proposal, once.

    The proposal is removed before the handler runs, not after: a second click
    arriving while the first is still working finds nothing to confirm, which
    is the behaviour that matters for an action that notifies somebody.
    """
    proposal = pending(proposal_id, user)
    if proposal is None:
        return Outcome(
            ok=False,
            said="That plan is no longer open — it may have been carried out, "
                 "cancelled, or left too long. Ask again and I will re-check "
                 "what is on screen.",
        )
    _pending.pop(proposal_id, None)
    # The handler re-checks against current state. Between the plan and this
    # moment somebody else may have decided the same row, and the handler is
    # the only thing that knows.
    return handler.act(proposal.scope, proposal.action, proposal.target_ids)


def cancel(proposal_id: str, user: str) -> Outcome:
    """Drop a proposal. Says plainly that nothing happened."""
    if pending(proposal_id, user) is None:
        return Outcome(ok=False, said="That plan is no longer open.")
    _pending.pop(proposal_id, None)
    return Outcome(ok=True, said="Cancelled — nothing changed.")


def open_count() -> int:
    """How many plans are waiting. For checks and for a health line."""
    _sweep()
    return len(_pending)


# ──────────────────────────────────────────────────────────────────────────
# what the rail shows
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class View:
    """The rail's own header and suggestions, for one place in the app.

    The rail follows the person: at a function's overview it answers across
    every feature, and once a feature is open it narrows to that screen. The
    scope line is the visible half of that — it says what the assistant can
    read, in the words the person would use, so "it can see everything" is
    never left to be assumed.
    """

    title: str
    subtitle: str
    #: The chip under the composer. What is readable from here, and nothing more.
    scope_label: str
    starters: list[str]
    #: True once a feature is open, which is also when operating is possible.
    can_act: bool


def view(fn, feature: FeatureManifest | None, scope: Scope) -> View:
    """What to render in the rail for this function, feature and scope."""
    if feature is None:
        n = len(fn.features)
        return View(
            title=f"{fn.name} assistant",
            subtitle=f"across {n} feature{'s' if n != 1 else ''}",
            scope_label=fn.name,
            starters=list(fn.starters),
            can_act=False,
        )

    # The subtitle is the scope the person chose, because that is the thing
    # that decides what the answers are about.
    chosen = [v for v in (scope.entity, scope.period) if v]
    return View(
        title=feature.name,
        subtitle=" · ".join(chosen) if chosen else feature.short,
        scope_label=feature.name,
        starters=list(feature.starters),
        can_act=bool(feature.actions),
    )
