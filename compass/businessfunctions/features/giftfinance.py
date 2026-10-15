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

from compass.businessfunctions import ledger, people, policy
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
            # Nothing is decided from this tab. `act` resolves its targets
            # against the REFERRALS, so a button here would refuse on every
            # row — and the surface draws what the row says and nothing
            # else, so saying nothing is how a tab reports without
            # pretending to decide.
            "can": [],
        })
    return sorted(out, key=lambda r: -r["above_limit"])


def _thresholds(scope: Scope) -> list[dict[str, Any]]:
    """Every version of the rules, newest first.

    A threshold is not a setting with a current value; it is a run of
    versions, each with the moment it took effect. A record is judged by
    the one in force when it was made, so the old rows are not history
    nobody needs — they are the rule something is still being measured by.
    """
    out = []
    every = policy.versions()
    for i, v in enumerate(reversed(every)):
        earlier = every[len(every) - i - 2] if len(every) - i - 2 >= 0 else None
        out.append({
            "version": f"v{v['version']}",
            "annual_limit": v["annual_limit"],
            "big": v["big"],
            "counts": ", ".join(sorted(v["counts"])),
            "in_force_from": (_day(v["effective_from"])
                              if v["effective_from"] else "the beginning"),
            "set_by": v["by"],
            "why": v["why"],
            "changed": "; ".join(policy.describe(earlier, v)) if earlier
                       else "the rules as shipped",
            "current": v is every[-1],
            # A version in force is a fact, not a queue. Setting a new one
            # is the admin's form, not a button on an old row — and nothing
            # ever rewrites a version that has been in force.
            "can": [],
        })
    return out


def _day(when: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(when).strftime("%Y-%m-%d") if when else "—"


class GiftFinance(Feature):
    key = "gift_finance"
    row_key = "employee_id"
    private = frozenset({"done", "can", "referred", "current"})

    THRESHOLD_FORM = "set_thresholds"
    THRESHOLD_FIELDS = frozenset({"annual_limit", "big", "why"})

    def accepts(self, form_id: str) -> set[str]:
        return (set(self.THRESHOLD_FIELDS)
                if form_id == self.THRESHOLD_FORM else set())

    def _proposed(self, scope: Scope, values: dict[str, str]
                  ) -> tuple[dict[str, Any] | None, Outcome | None]:
        """The version these values describe, or why they describe none."""
        if "admin" not in scope.roles:
            return None, Outcome(
                ok=False,
                said="Thresholds are the Administrator's. They are the rule "
                     "every other team is judged by, so they are not in the "
                     "hands of the teams whose work they judge.")
        why = values.get("why", "").strip()
        if not why:
            return None, Outcome(
                ok=False,
                said="Say why. Moving a threshold silently reclassifies "
                     "every gift that comes after it, which is the one "
                     "change nobody should be able to make quietly.")
        changes: dict[str, Any] = {}
        for field, label in (("annual_limit", "the annual limit"),
                             ("big", "the single-gift threshold")):
            raw = values.get(field, "").strip()
            if not raw:
                continue
            amount = rewardlens.whole_rupees(raw)
            if amount is None or amount <= 0:
                return None, Outcome(
                    ok=False,
                    said=f"Give {label} in whole rupees — 20000, or 20,000.")
            changes[field] = amount
        if not changes:
            return None, Outcome(ok=False, said="Nothing to change.")
        return {"changes": changes, "why": why}, None

    def preview(self, scope: Scope, form_id: str, values: dict[str, str],
                about: str = "") -> Outcome:
        asked, refusal = self._proposed(scope, values)
        if refusal is not None:
            return refusal
        was = policy.current()
        would = dict(was, **asked["changes"])
        said = policy.describe(was, would)
        if not said:
            return Outcome(ok=False, said="That is what the thresholds "
                                          "already say.")
        return Outcome(
            ok=True,
            said=f"Would become v{was['version'] + 1}: {'; '.join(said)}. "
                 f"It applies from the moment you set it. Everything "
                 f"already recorded stays judged by v{was['version']} — a "
                 f"gift inside the limit on the day it was given does not "
                 f"become a breach because the limit moved.")

    def submit(self, scope: Scope, form_id: str, values: dict[str, str],
               about: str = "") -> Outcome:
        asked, refusal = self._proposed(scope, values)
        if refusal is not None:
            return refusal
        was = policy.current()
        try:
            fresh = policy.add(by=scope.user, why=asked["why"],
                               **asked["changes"])
        except ValueError as exc:
            return Outcome(ok=False, said=str(exc))
        said = "; ".join(policy.describe(was, fresh))
        # On the same trail as every other decision, under the version it
        # created, so "when did the limit change and who moved it" has an
        # answer that is not a deploy log.
        ledger.add_event(subject=f"policy/v{fresh['version']}",
                         kind="threshold_set", by=scope.user,
                         note=asked["why"], why=said,
                         state=f"v{was['version']} \u2192 v{fresh['version']}")
        return Outcome(
            ok=True,
            said=f"v{fresh['version']} is in force from now: {said}. "
                 f"Records made before this moment keep v{was['version']}.",
            touched=[f"v{fresh['version']}"])

    def figures(self, scope: Scope) -> list[Figure]:
        if scope.sees != "firm":
            return []
        referrals = _referrals(scope)
        waiting = [r for r in referrals if not r["done"]]
        taxable = _taxable(scope)
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
            # The firm's own ceiling, which is now one number per person
            # rather than six team budgets. Said here because it is the
            # figure every other one on this screen is measured against.
            Figure("allowance", rewardlens.rupees(rewardlens.limit()),
                   "allowed per employee, per year"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        if scope.sees != "firm":
            return []
        return [
            Tab("referred", "With Finance",
                len([r for r in _referrals(scope) if not r["done"]])),
            Tab("taxable", "Past the limit", len(_taxable(scope))),
            # Visible to everybody who can read this screen: the rule you
            # are judged by is not a secret. Only `admin` can add to it.
            Tab("thresholds", "Thresholds", len(_thresholds(scope)),
                key_field="version"),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if scope.sees != "firm":
            return []
        if tab == "taxable":
            return _taxable(scope)
        if tab == "thresholds":
            return _thresholds(scope)
        return _referrals(scope)

    def rules(self, scope: Scope) -> list[str]:
        if scope.sees != "firm":
            return ["finance_is_a_role"]
        said = []
        taxable = _taxable(scope)
        if [r for r in taxable if not r["referred"]]:
            said.append("not_everything_was_referred")
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
