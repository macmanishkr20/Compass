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

WHO MAY SEE IT. The firm-wide view names people and their totals, so the
manifest declares it firm-only and gives that breadth to Compliance,
Finance, Audit and Leadership. Everybody else gets the explanation and an
empty screen: that this screen exists is not the secret, what is on it is.
Notably there is no `self` breadth here — an aggregate of one person is not
a dashboard, and an employee's own record is RewardLens's job.
"""

from __future__ import annotations

import time
from typing import Any

from compass.businessfunctions import notices
from compass.businessfunctions.features import rewardlens
from compass.businessfunctions.features.base import (
    Chart,
    Feature,
    Figure,
    Slice,
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


def _firm(scope: Scope) -> Scope:
    """This scope at firm breadth. The dashboard is leadership's only."""
    return Scope(user=scope.user, period=scope.period, sees="firm",
                 roles=scope.roles)


def _team_of(employee_id: str) -> str:
    """Which team a recipient works in, or a bucket that says nobody knows."""
    from compass.businessfunctions import people

    return people.team_of_employee(employee_id) or "team not recorded"


def _received_by_team(scope: Scope) -> dict[str, dict[str, Any]]:
    """What each team's PEOPLE received this year.

    By the team that RECEIVED, not the team that paid. One team buys for
    the whole firm, so the paying team stopped being an axis the day the
    budgets became a single per-person allowance — every bar would be the
    same bar. Where it went is still a real question, and it is the one
    leadership was asking when they asked about teams.
    """
    out: dict[str, dict[str, Any]] = {}
    for item in rewardlens.items(_firm(scope)):
        if item["id"] in rewardlens.superseded():
            continue
        team = _team_of(item["employee_id"])
        row = out.setdefault(team, {"source": team, "given": 0, "items": 0,
                                    "people": set()})
        row["given"] += item["value"]
        row["items"] += 1
        row["people"].add(item["employee_id"])
    return out


def _budget_rows(scope: Scope) -> list[dict[str, Any]]:
    """Each team's allowance against what its people have actually had.

    A team's allowance is its HEADCOUNT times the one annual allowance
    every employee has — not a budget somebody typed per team, which is
    what this was. The firm buys through one team and controls the spend
    one person at a time, so the only honest team figure is the sum of its
    people's allowances.
    """
    from compass.businessfunctions import people

    received = _received_by_team(scope)
    out = []
    for team, employees in people.employees_by_team().items():
        allowed = len(employees) * rewardlens.limit()
        had = received.get(team, {}).get("given", 0)
        out.append({"source": team, "people": len(employees),
                    "budget": allowed, "committed": had,
                    "left": allowed - had,
                    "status": "over the allowance" if had > allowed
                              else "within the allowance"})
    return sorted(out, key=lambda r: -r["committed"])


def _team_spend(scope: Scope) -> list[dict[str, Any]]:
    """What each team's people actually received."""
    out = [{**r, "people": len(r["people"])}
           for r in _received_by_team(scope).values()]
    return sorted(out, key=lambda r: -r["given"])


def _orders(scope: Scope) -> list[dict[str, Any]]:
    """Every order in flight, read through the chain's own rule.

    Asked of the chain rather than counted here: which orders somebody may
    see is the chain's answer, and this screen is leadership's, who see
    all of it. A dashboard with its own idea of what is in flight is a
    dashboard that can disagree with the desk working the queue.
    """
    from compass.businessfunctions.features import giftrequests

    return giftrequests.tracked(scope.user, scope.period)


def _where_orders_are(scope: Scope) -> list[dict[str, Any]]:
    """How many orders are standing at each step, and the longest wait.

    THE QUESTION A DASHBOARD IS OPENED WITH. Where has everything got to —
    not what it cost, not who is over, but whether anything is sitting
    still. The counts come from the chain's stages in chain order rather
    than from whatever happens to be in the data, so a step with nothing
    at it shows as empty instead of vanishing, which is the only way to
    see that a desk has stopped answering.
    """
    from compass.businessfunctions.features import giftrequests

    moving = [r for r in _orders(scope)
              if r["stage"] not in giftrequests.ENDED]
    out = []
    for stage in giftrequests.STAGES:
        if stage == "closed":
            continue
        here = [r for r in moving if r["stage"] == stage]
        out.append({
            "stage": giftrequests.stage_label(stage),
            "orders": len(here),
            "value": sum(r["value"] * r.get("quantity", 1) for r in here),
            "longest_wait": max((r["waiting_days"] for r in here), default=0),
            "with": here[0]["with"] if here else "—",
            "can": [],
        })
    return out


def _unconfirmed(scope: Scope) -> list[dict[str, Any]]:
    """Gifts on the register that nobody has confirmed receiving.

    The requirement asks for this by name: gifts that have not reached
    confirmed receipt. It is the quietest way for this process to fail —
    the money is spent, the record exists, and whether it ever reached a
    person is a thing nobody has written down.
    """
    out = []
    for item in rewardlens.items(_firm(scope)):
        if item["acknowledged"] or item["id"] in rewardlens.superseded():
            continue
        out.append({
            "id": item["id"],
            "recipient": item["recipient"],
            "employee_id": item["employee_id"],
            "what": item["what"],
            "value": item["value"],
            "given": item["given"],
            "waiting": _days_since_day(item["given"]),
            "can": [],
        })
    return sorted(out, key=lambda r: -r["waiting"])


def _days_since_day(day: str) -> int:
    """Whole days since a YYYY-MM-DD, never negative."""
    import datetime as dt

    try:
        then = dt.date.fromisoformat(day)
    except (TypeError, ValueError):
        return 0
    return max(0, (dt.date.today() - then).days)


def _notable(scope: Scope, above: int) -> list[dict[str, Any]]:
    """Everybody whose year is past the threshold, biggest first.

    `items_counted`, not `items`: the register's row has no `items` key,
    so this raised the moment anybody actually crossed — and because
    nothing in the fixture ever did, nothing caught it. The first person
    in the firm to cross the threshold would have taken the whole
    Dashboard down with them, which is the day it is most needed.
    """
    return sorted(
        [{"employee_id": r["employee_id"], "recipient": r["recipient"],
          "total": r["total"], "items": r["items_counted"],
          "over_limit_by": r["excess"], "status": r["status"]}
         for r in rewardlens.totals(_firm(scope)) if r["total"] > above],
        key=lambda r: -r["total"])


def _team_tax(scope: Scope) -> list[dict[str, Any]]:
    """How much of each team's year sits above the annual allowance.

    It used to APPORTION a person's excess across the teams that gave to
    them, in proportion to what each gave, because the excess is a property
    of the person's year and no single gift caused it — an apportionment
    that had to be said out loud so it did not read as a measurement.

    There is nothing left to apportion. A person works in one team, and
    their excess is that team's, whole.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rewardlens.totals(_firm(scope)):
        if not row["over_limit"]:
            continue
        team = _team_of(row["employee_id"])
        held = out.setdefault(team, {"source": team, "above_limit": 0,
                                     "people": 0})
        held["above_limit"] += row["excess"]
        held["people"] += 1
    return sorted(out.values(), key=lambda r: -r["above_limit"])


class Oversight(Feature):
    key = "gift_oversight"
    #: Three of the four tabs are keyed by team and one by person, so each
    #: declares its own `key_field` and this is only the fallback.
    row_key = "employee_id"
    #: It shows RewardLens's own rows, so it inherits what RewardLens calls
    #: machinery, and adds the composite key its process list is built on.
    private = rewardlens.RewardLens.private | frozenset({"id"})

    def _shut_out(self, scope: Scope) -> bool:
        """Whether this person may not see the firm-wide view.

        Asked of the scope rather than of a role list kept here: the
        manifest declares who sees this screen, the route resolves it, and
        this is the one line that acts on the answer. A handler holding its
        own copy of the rule is a handler that can disagree with the
        manifest somebody edited.
        """
        return scope.sees != "firm"

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

    # Above this, a year is worth leadership looking at by name. NOT the
    # annual limit, which is the compliance line and lives in `policy`:
    # this is the "who is unusually expensive" watch level, and the two
    # being different numbers is deliberate.
    #: Read from the rulebook rather than kept here. It WAS a second
    #: number — a dashboard "watch level" of ₹50,000 sitting beside a
    #: compliance limit of ₹15,000 — and two thresholds for one question
    #: is one threshold too many: a reader seeing "3 over the limit" on
    #: one screen and "0 past ₹50,000" on another cannot tell which is the
    #: firm's rule. There is one, it is versioned, and it is ₹50,000.
    @property
    def NOTABLE(self) -> int:
        return rewardlens.limit()

    def charts(self, scope: Scope) -> list[Chart]:
        """The four pictures leadership reads the position from.

        Each one is drawn from a tab on this screen, so a click goes to
        the numbers it was drawn from. A chart that cannot be got behind
        is a chart nobody can check.
        """
        if self._shut_out(scope):
            return []
        money = rewardlens.rupees
        budgets = _budget_rows(scope)
        teams = _team_spend(scope)
        over = _notable(scope, self.NOTABLE)
        taxed = _team_tax(scope)
        where = _where_orders_are(scope)
        unconfirmed = _unconfirmed(scope)

        return [
            Chart(
                key="budget", kind="bars", tab="budgets",
                title="Annual allowance against what was received",
                caption=f"{money(rewardlens.limit())} a head across "
                        f"{sum(b['people'] for b in budgets)} people in "
                        f"{len(budgets)} teams — "
                        f"{money(sum(b['budget'] for b in budgets))} allowed, "
                        f"{money(sum(b['committed'] for b in budgets))} of it "
                        f"received.",
                slices=[Slice(label=b["source"], value=b["committed"],
                              against=b["budget"],
                              said=f"{money(b['committed'])} of "
                                   f"{money(b['budget'])}")
                        for b in budgets],
                empty="The directory knows nobody's team."),
            Chart(
                key="teams", kind="split", tab="teams",
                title="Where the spend goes, by team",
                caption="Every gift that reached somebody, by the team they "
                        "work in. One team buys for the whole firm, so who "
                        "paid is the same answer every time; who received "
                        "is not.",
                slices=[Slice(label=t["source"], value=t["given"],
                              said=money(t["given"]))
                        for t in teams if t["given"]],
                empty="Nothing has been handed over yet."),
            Chart(
                key="notable", kind="bars", tab="notable",
                title=f"People past {money(self.NOTABLE)} this year",
                caption=f"A year worth looking at by name. The compliance "
                        f"limit is {money(rewardlens.limit())}; this is the "
                        f"higher watch level.",
                slices=[Slice(label=r["employee_id"], value=r["total"],
                              against=self.NOTABLE,
                              said=f"{r['recipient']} — {money(r['total'])}")
                        for r in over],
                empty=f"Nobody is past {money(self.NOTABLE)} this year."),
            Chart(
                key="tax", kind="split", tab="tax",
                title="Amount above the limit, by team",
                caption="How much of each team's year sits above the "
                        "allowance. The amount, never a rate — the "
                        "treatment is Finance's own system's to decide.",
                slices=[Slice(label=t["source"], value=t["above_limit"],
                              said=money(t["above_limit"]))
                        for t in taxed if t["above_limit"]],
                empty="Nobody is over the limit, so there is nothing above it."),
            # THE TWO THE REQUIREMENT ASKS FOR BY NAME: where everything
            # has got to, and what has not reached a person. Both were
            # lists on this screen once and read as a second screen
            # stacked under the first; as pictures they answer at a glance,
            # which is what a dashboard is for.
            Chart(
                key="where", kind="bars", tab="where",
                title="Where the orders are now",
                caption=f"{sum(w['orders'] for w in where)} in flight. A step "
                        f"with nothing at it is shown empty rather than left "
                        f"out, because an empty desk is the thing worth "
                        f"noticing.",
                slices=[Slice(label=w["stage"].replace("With the ", "")
                              .replace("With ", ""),
                              value=w["orders"],
                              said=f"{w['orders']} order"
                                   f"{'' if w['orders'] == 1 else 's'}"
                                   + (f", longest {w['longest_wait']}d"
                                      if w["longest_wait"] else ""))
                        for w in where],
                empty="Nothing is in flight."),
            Chart(
                key="unconfirmed", kind="bars", tab="unconfirmed",
                title="Given, but nobody has confirmed receiving it",
                caption=f"{money(sum(u['value'] for u in unconfirmed))} across "
                        f"{len(unconfirmed)} record"
                        f"{'' if len(unconfirmed) == 1 else 's'}, longest "
                        f"waiting {max((u['waiting'] for u in unconfirmed), default=0)} "
                        f"days. The money is spent and the record exists; "
                        f"whether it reached a person is not written down.",
                slices=[Slice(label=u["recipient"], value=u["waiting"],
                              said=f"{u['what']} \u00b7 {money(u['value'])} "
                                   f"\u00b7 {u['waiting']}d")
                        for u in unconfirmed[:12]],
                empty="Everything on the register has been confirmed."),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        if self._shut_out(scope):
            return []
        # FOUR TABS, FOUR CHARTS, in the same order, so clicking a picture
        # lands on its own numbers and nothing else.
        #
        # There were seven. Three of them — who nobody had decided, where
        # the process had stalled, where the value came from — predate the
        # charts, and on a screen whose whole argument is "read the
        # position at a glance" they read as a second screen stacked under
        # the first. Their findings are still said, as rules at the top;
        # the orders behind them are the Tracker's, which is where every
        # desk already goes to ask what has got where.
        return [
            Tab("budgets", "Budget vs spent", len(_budget_rows(scope)),
                key_field="source"),
            Tab("teams", "Team spend", len(_team_spend(scope)),
                key_field="source"),
            Tab("notable", f"Past {rewardlens.rupees(self.NOTABLE)}",
                len(_notable(scope, self.NOTABLE)), key_field="employee_id"),
            Tab("tax", "Above the limit, by team", len(_team_tax(scope)),
                key_field="source"),
            Tab("where", "Where the orders are",
                len(_where_orders_are(scope)), key_field="stage"),
            Tab("unconfirmed", "Not yet confirmed",
                len(_unconfirmed(scope)), key_field="id"),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if self._shut_out(scope):
            return []
        if tab == "budgets":
            return _budget_rows(scope)
        if tab == "teams":
            return _team_spend(scope)
        if tab == "notable":
            return _notable(scope, self.NOTABLE)
        if tab == "tax":
            return _team_tax(scope)
        if tab == "where":
            return _where_orders_are(scope)
        if tab == "unconfirmed":
            return _unconfirmed(scope)
        return _budget_rows(scope)

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
