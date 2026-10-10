"""Finance's two questions: what has reached me, and what has it cost.

Compliance refers somebody to Finance and, until now, the referral went
nowhere — the register recorded that it had happened and no screen in
Compass belonged to the people it had happened to. This is that screen, and
the other half of what Finance actually asks:

    referred    who compliance has sent over, and why
    taxable     everybody past the annual limit, and by how much
    budgets     what each team has committed against what it was given

WHAT THIS DOES NOT DO IS CALCULATE TAX. The requirement gives one figure —
"an employee can receive only ₹15,000 per year before additional tax
implications arise" — and that is a THRESHOLD, not a rate. So this reports
the amount above it and stops. Inventing a percentage would put a number on
a screen that looks like a tax liability, is not one, and would be believed:
the rate, the heads of income and the deduction are Finance's own system's
to apply, and this hands them the figure they need to do it.

COMMITTED, NOT SPENT. A budget that only counted what had already been
handed over would let a team approve its way past the line and discover it
at distribution. An approval commits the money; handing it over spends it;
both are shown, because the gap between them is the thing a budget holder
wants to see.

Finance records that it has dealt with a referral, and that is the only
thing anybody does here. It does not decide the breach — compliance already
did — and it cannot change a total, because a total is the sum of what was
received and no department gets to edit that.
"""

from __future__ import annotations

from typing import Any

from compass.businessfunctions import ledger
from compass.businessfunctions.features import giftrequests, rewardlens
from compass.businessfunctions.features.base import (
    Feature,
    Figure,
    Outcome,
    Scope,
    Tab,
    register,
)


def _firm(scope: Scope) -> Scope:
    """The same scope at firm breadth.

    Finance's questions are about the firm. The manifest only gives this
    screen to people who may see it at all, so by the time a row is built
    the narrowing has already happened — reading the register at self
    breadth here would show a Finance mailbox its own empty year.
    """
    return Scope(user=scope.user, period=scope.period, sees="firm")


def _referrals(scope: Scope) -> list[dict[str, Any]]:
    """Everybody compliance has sent over, newest first."""
    people_rows = {r["employee_id"]: r
                   for r in rewardlens.totals(_firm(scope))}
    out: list[dict[str, Any]] = []
    for event in ledger.events():
        if event["kind"] != "referred":
            continue
        row = people_rows.get(event["subject"])
        if row is None:
            continue
        handled = ledger.latest(event["subject"], "excepted")
        out.append({
            "employee_id": event["subject"],
            "recipient": row["recipient"],
            "total": row["total"],
            # What Finance is being asked about: the amount above the
            # threshold. Not a tax figure; see the note at the top.
            "above_limit": row["excess"],
            "referred_by": event["by"],
            "when": _day(event["at"]),
            "status": "recorded by Finance" if handled else "with Finance",
            "note": (handled.get("note", "") if handled else "") or "—",
            "done": bool(handled),
            "can": (["record_tax"] if not handled and "finance" in scope.roles
                    else []),
        })
    out.reverse()
    return out


def _taxable(scope: Scope) -> list[dict[str, Any]]:
    """Everybody past the threshold, referred or not.

    Deliberately wider than the referral queue: a breach nobody referred is
    still a breach, and Finance finding out only about the ones compliance
    remembered to send is the shape of the problem this whole function
    exists to end.
    """
    out = []
    for row in rewardlens.totals(_firm(scope)):
        if not row["over_limit"]:
            continue
        out.append({
            "employee_id": row["employee_id"],
            "recipient": row["recipient"],
            "total": row["total"],
            "limit": row["limit"],
            "above_limit": row["excess"],
            "crossed_on": row["crossed_on"],
            "status": row["status"],
            "referred": row["referred"],
        })
    return sorted(out, key=lambda r: -r["above_limit"])


def _budgets(scope: Scope) -> list[dict[str, Any]]:
    """What each team has committed and actually given."""
    given: dict[str, int] = {}
    for item in rewardlens.items(_firm(scope)):
        if item["id"] in rewardlens.superseded():
            continue
        if item["kind"] not in rewardlens.COUNTS_TOWARDS_LIMIT:
            continue
        given[item["source"]] = given.get(item["source"], 0) + item["value"]

    # What was handed over WITHOUT a request behind it: the register
    # predates the request flow, and anything declared by a recipient never
    # had one. Left out, a team showed ₹13,000 given against ₹0 committed
    # and a full budget remaining — understating its own spend by every
    # gift it gave before this screen existed.
    unrequested: dict[str, int] = {}
    with_request = {i["source"]: 0 for i in rewardlens.items(_firm(scope))}
    for item in rewardlens.items(_firm(scope)):
        if item["id"] in rewardlens.superseded():
            continue
        if item["kind"] not in rewardlens.COUNTS_TOWARDS_LIMIT:
            continue
        if not item.get("from_request"):
            unrequested[item["source"]] = (
                unrequested.get(item["source"], 0) + item["value"])

    out = []
    for source, budget in giftrequests.BUDGETS.items():
        approved = giftrequests.committed(source)
        outside = unrequested.get(source, 0)
        committed = approved + outside
        out.append({
            "source": source,
            "budget": budget,
            "committed": committed,
            "given": given.get(source, 0),
            # Said separately, because "we never asked for it" is a
            # different conversation from "we approved too much".
            "not_requested": outside,
            "left": budget - committed,
            "status": "over budget" if committed > budget
                      else "fully committed" if committed == budget
                      else "within budget",
        })
    # Tightest first: a team with nothing left is the one somebody has to
    # talk to before the next request arrives.
    return sorted(out, key=lambda r: r["left"])


def _day(when: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(when).strftime("%Y-%m-%d") if when else "—"


class GiftFinance(Feature):
    key = "gift_finance"
    row_key = "employee_id"
    private = frozenset({"done", "can", "referred"})

    def figures(self, scope: Scope) -> list[Figure]:
        if scope.sees != "firm":
            return []
        referrals = _referrals(scope)
        waiting = [r for r in referrals if not r["done"]]
        taxable = _taxable(scope)
        budgets = _budgets(scope)
        over = [b for b in budgets if b["left"] < 0]
        return [
            Figure("waiting", str(len(waiting)),
                   "referred and not dealt with" if waiting
                   else "nothing waiting", "warn" if waiting else "good"),
            # The figure Finance is actually asked for, and the one no
            # per-person screen adds up.
            Figure("above_limit",
                   rewardlens.rupees(sum(r["above_limit"] for r in taxable)),
                   "above the annual limit in total",
                   "bad" if taxable else "good"),
            Figure("people", str(len(taxable)), "people past the limit"),
            Figure("unreferred",
                   str(len([r for r in taxable if not r["referred"]])),
                   "past it and never referred", "warn"
                   if [r for r in taxable if not r["referred"]] else "good"),
            Figure("budgets", str(len(over)),
                   "teams over budget" if over else "every team within budget",
                   "bad" if over else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        if scope.sees != "firm":
            return []
        return [
            Tab("referred", "With Finance",
                len([r for r in _referrals(scope) if not r["done"]])),
            Tab("taxable", "Past the limit", len(_taxable(scope))),
            Tab("budgets", "Budgets", len(_budgets(scope)), key_field="source"),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if scope.sees != "firm":
            return []
        if tab == "taxable":
            return _taxable(scope)
        if tab == "budgets":
            return _budgets(scope)
        return _referrals(scope)

    def rules(self, scope: Scope) -> list[str]:
        if scope.sees != "firm":
            return ["finance_is_a_role"]
        said = []
        taxable = _taxable(scope)
        if [r for r in taxable if not r["referred"]]:
            said.append("not_everything_was_referred")
        if [b for b in _budgets(scope) if b["left"] < 0]:
            said.append("a_team_is_over_budget")
        if not taxable:
            said.append("nothing_above_the_limit")
        return said

    def act(self, scope: Scope, action: str, targets: list[str],
            note: str = "") -> Outcome:
        if action != "record_tax":
            return Outcome(ok=False, said="This screen reports and records "
                                          "what Finance has dealt with.")
        rows = {r["employee_id"]: r for r in _referrals(scope)}
        chosen = [rows[t] for t in targets if t in rows]
        if len(chosen) != 1:
            return Outcome(ok=False, said="One referral at a time.")
        row = chosen[0]
        if action not in row["can"]:
            return Outcome(
                ok=False,
                said=f"{row['recipient']}'s referral is {row['status']} — "
                     f"that is not something you can do to it now.")
        if not note.strip():
            return Outcome(ok=False, said="Say what was done. A referral "
                                          "closed with no record of the "
                                          "treatment is a referral nobody "
                                          "can audit.")
        # Recorded against the person, on the same trail as everything else,
        # so the thing Audit reads is one list and not several.
        ledger.add_event(subject=row["employee_id"], kind="excepted",
                         by=scope.user, note=note.strip(),
                         total=row["total"])
        return Outcome(
            ok=True,
            said=f"Recorded for {row['recipient']} — "
                 f"{rewardlens.rupees(row['above_limit'])} above the limit, "
                 f"with your note. The total itself does not change; it is "
                 f"the sum of what they received.",
            touched=[row["employee_id"]],
        )


register(GiftFinance())
