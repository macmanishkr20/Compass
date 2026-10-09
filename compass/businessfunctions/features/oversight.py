"""Gift oversight — what compliance leadership needs to know, and no more.

A dashboard is where a feature goes to become decorative, so this one is
built around the questions somebody accountable actually asks, and nothing
is on it that does not answer one:

    How exposed are we?        the value sitting over the limit, in rupees
    Is anybody dealing with it? how much of that nobody has decided
    Is the process working?     what has been asked and never answered, and
                                who was never reached at all

The last one is the reason this screen exists separately from RewardLens.
The workbench answers "who is over the limit", which is a compliance
question. Leadership's question is different and worse: it is "is the thing
we built actually being used". A query sent three weeks ago that nobody
answered is not a compliance breach, it is a process that has stopped, and
nothing in the workbench would ever show it.

IT DOES NOT DO THE ARITHMETIC AGAIN. Every figure here is built from the
rows RewardLens hands out, through the readers at the bottom of that module.
If this screen summed the items itself, two screens could disagree about who
is over the limit, and two numbers for one fact is worse than one wrong
number — nobody can tell which to believe, and both become unusable.

IT CANNOT DECIDE ANYTHING. No actions, deliberately: leadership looks at the
shape of the thing, and the individual decisions belong to the people whose
names go against them. A feature with no actions also tells the assistant it
cannot act, so the rail here answers and never proposes.

WHO MAY SEE IT. The firm-wide view names people and their totals, so it is
for the compliance role. Everybody else gets the explanation and an empty
screen: that this screen exists is not the secret, what is on it is.
"""

from __future__ import annotations

import time
from typing import Any

from compass.businessfunctions import notices
from compass.businessfunctions.features import rewardlens
from compass.businessfunctions.features.base import (
    Feature,
    Figure,
    Outcome,
    Scope,
    Tab,
    register,
)

#: Past this, something that was asked has stopped being a question and
#: started being a process failure. Leadership's number, not compliance's.
STALE_DAYS = 7


def _days_since(when: float) -> int:
    """Whole days since a recorded moment, or 0 when there is no moment.

    Only ever applied to timestamps Compass wrote itself. The dates ON the
    items are when a gift was received, and several of those sit later in
    the financial year than today — ageing from them would print negative
    days, which is the kind of number that makes a reader stop trusting the
    screen.
    """
    if not when:
        return 0
    return max(0, int((time.time() - when) // 86_400))


def _undecided(scope: Scope) -> list[dict[str, Any]]:
    """Over the limit, and nobody has done anything about it.

    Not "over the limit" — that is the workbench's list. This is the subset
    that has been neither referred to Finance nor accepted as an exception,
    which is the only one leadership can act on, by asking somebody why.
    """
    return [r for r in rewardlens.totals(scope)
            if r["over_limit"] and not r["referred"] and not r["exception"]]


def _unanswered(scope: Scope) -> list[dict[str, Any]]:
    """Asked, and no reply."""
    return [r for r in rewardlens.declarations(scope) if r["state"] == "queried"]


def _unreached(scope: Scope) -> list[tuple[dict[str, Any], Any]]:
    """Questions that never got to the person they were about.

    The quietest failure in the whole feature: the record says the question
    was asked, the person never heard it, and without this nobody finds out
    until somebody wonders why there has been no answer for a month.
    """
    out = []
    for row in rewardlens.declarations(scope):
        for notice in notices.for_item(row["id"]):
            if notice.state in ("held", "failed"):
                out.append((row, notice))
    return out


def _stuck(scope: Scope) -> list[dict[str, Any]]:
    """Everything the process has dropped, as one list.

    Three different failures — never reached, never answered, never
    acknowledged — on one screen because to leadership they are the same
    thing: somebody was supposed to do something and it did not happen.
    """
    out: list[dict[str, Any]] = []
    unreached = {row["id"] for row, _ in _unreached(scope)}

    for row, notice in _unreached(scope):
        out.append({
            "id": f"{row['id']}:told",
            "who": row["declared_by"],
            "what": row["what"],
            "problem": "never told — " + notice.detail,
            "waiting": _days_since(notice.made_at),
        })

    for row in _unanswered(scope):
        if row["id"] in unreached:
            # Already listed as never reached, and "no answer" is not a
            # second failure when nobody was asked.
            continue
        out.append({
            "id": f"{row['id']}:answer",
            "who": row["declared_by"],
            "what": row["what"],
            "problem": "asked, no answer yet",
            "waiting": _days_since(row.get("asked_at", 0.0)),
        })

    for person in rewardlens.totals(scope):
        if person["unacknowledged"]:
            out.append({
                "id": f"{person['employee_id']}:ack",
                "who": person["recipient"],
                "what": f"{person['unacknowledged']} item"
                        f"{'s' if person['unacknowledged'] > 1 else ''}",
                "problem": "not acknowledged by the recipient",
                "waiting": 0,
            })

    return sorted(out, key=lambda r: (-r["waiting"], r["who"]))


def _sources(scope: Scope) -> list[dict[str, Any]]:
    """Where it all comes from, worst first.

    Concentration is the one thing on this screen that suggests a change
    rather than a chase: if most of the over-limit value comes through one
    team, the fix is a conversation with that team and not a stream of
    referrals.
    """
    over = {r["employee_id"] for r in rewardlens.totals(scope) if r["over_limit"]}
    gone = rewardlens.superseded()

    rows: dict[str, dict[str, Any]] = {}
    for item in rewardlens.items(scope):
        if item["id"] in gone or item["kind"] not in rewardlens.COUNTS_TOWARDS_LIMIT:
            continue
        row = rows.setdefault(item["source"], {
            "source": item["source"], "items": 0, "people": set(),
            "value": 0, "to_people_over": 0,
        })
        row["items"] += 1
        row["people"].add(item["employee_id"])
        row["value"] += item["value"]
        if item["employee_id"] in over:
            row["to_people_over"] += item["value"]

    out = [{"source": r["source"], "items": r["items"],
            "people": len(r["people"]), "value": r["value"],
            "to_people_over": r["to_people_over"]}
           for r in rows.values()]
    return sorted(out, key=lambda r: (-r["to_people_over"], -r["value"]))


class Oversight(Feature):
    key = "gift_oversight"
    #: The attention tab lists people; the other two say their own.
    row_key = "employee_id"
    #: It shows RewardLens's own rows, so it inherits what RewardLens calls
    #: machinery, and adds the composite key its process list is built on.
    private = rewardlens.RewardLens.private | frozenset({"id"})

    def _shut_out(self, scope: Scope) -> bool:
        """Whether this person may not see the firm-wide view."""
        return not rewardlens.is_reviewer(scope.user)

    def figures(self, scope: Scope) -> list[Figure]:
        if self._shut_out(scope):
            return []
        people = rewardlens.totals(scope)
        over = [r for r in people if r["over_limit"]]
        exposure = sum(r["excess"] for r in over)
        undecided = _undecided(scope)
        unanswered = _unanswered(scope)
        unreached = _unreached(scope)
        return [
            # The rupee figure first, because it is the one a board asks for
            # and the one the per-person screens never add up.
            Figure("exposure", rewardlens.rupees(exposure),
                   "over the limit in total" if exposure else "nothing over",
                   "bad" if exposure else "good"),
            Figure("people_over", str(len(over)),
                   f"of {len(people)} tracked"),
            Figure("undecided", str(len(undecided)),
                   "nobody has decided" if undecided else "all decided",
                   "warn" if undecided else "good"),
            Figure("unanswered", str(len(unanswered)),
                   "asked, no answer" if unanswered else "no questions open",
                   "warn" if unanswered else "good"),
            Figure("unreached", str(len(unreached)),
                   "never reached the person" if unreached else "everyone reached",
                   "bad" if unreached else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        if self._shut_out(scope):
            return []
        return [
            Tab("attention", "Nobody has decided", len(_undecided(scope))),
            Tab("stuck", "Process gaps", len(_stuck(scope)), key_field="id"),
            Tab("sources", "Where it comes from", len(_sources(scope)),
                key_field="source"),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if self._shut_out(scope):
            return []
        if tab == "stuck":
            return _stuck(scope)
        if tab == "sources":
            return _sources(scope)
        return _undecided(scope)

    def rules(self, scope: Scope) -> list[str]:
        if self._shut_out(scope):
            return ["oversight_is_a_role"]
        said = []
        if _unreached(scope):
            said.append("somebody_never_heard")
        stale = [r for r in _stuck(scope) if r["waiting"] >= STALE_DAYS]
        if stale:
            said.append("the_process_has_stalled")
        if not _undecided(scope) and not _stuck(scope):
            said.append("nothing_outstanding")
        return said

    def act(self, scope: Scope, action: str, targets: list[str],
            note: str = "") -> Outcome:
        # No actions are declared, so nothing should reach this. Saying why
        # rather than "unknown action" means a client that finds a way here
        # gets the reason instead of a shrug.
        return Outcome(
            ok=False,
            said="This screen reports. The decisions belong to the people "
                 "whose names go against them, on RewardLens.",
        )


register(Oversight())
