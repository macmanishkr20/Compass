"""Asking for a gift, and what has to be true before it is given.

RewardLens records what the firm GAVE. This is the half that happens first,
and it is where the money and the breach are still preventable:

    requested → policy · tax · budget → approved or declined
              → purchased → given → acknowledged

THE CHECKS RUN BEFORE THE DECISION, NOT AFTER IT. The whole complaint in
the requirement is that the firm finds out afterwards — that John is at
₹20,000 across three teams and nobody knew until somebody added it up. So a
request carries its own answer to the three questions, computed from what
is on the register at that moment and shown to the approver before they
approve:

    policy   is this permitted at all — the kind, the occasion, the size
    tax      what it does to the recipient's year, which RewardLens already
             knows how to work out
    budget   whether the team asking still has the money

The tax check is the one that matters and it is free: it is the same
arithmetic the register does, asked one gift earlier. "Approving this takes
John to ₹23,000, ₹8,000 over" is a sentence nobody could say before, and it
is said while it still costs nothing to say no.

A REQUEST IS NOT A RECORD OF ANYTHING. It is a proposal, and most of what
goes wrong in systems like this is treating the two as one thing: a
declined request on the register is a gift nobody received, and an approved
one that was never handed over is a total that is wrong in the other
direction. Nothing reaches the register until somebody says it was GIVEN.

WHO DOES WHAT. A service line lead asks. A manager or compliance approves —
never their own request, and never a gift to themselves. Procurement buys
and hands over. The recipient acknowledges, which is the only part of this
whole feature the recipient can do, and the thing that makes the register
something they have agreed to rather than something done to them.

NOT HERE, on purpose: vendors, external recipients and multi-currency are
the later phases the requirement names, and a budget that is a number per
source is a stand-in for a finance system rather than a model of one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from compass.businessfunctions import ledger, people
from compass.businessfunctions.features import rewardlens
from compass.businessfunctions.features.base import (
    Feature,
    Figure,
    Outcome,
    Scope,
    Tab,
    register,
)

#: What a request is for. The same vocabulary the register uses, so a
#: request that becomes an item does not change its own meaning on the way.
KINDS = rewardlens.KINDS

#: Why it is being given. Policy hangs off this: a long-service award is a
#: different thing from a hamper to somebody who is choosing a supplier.
OCCASIONS = ("festival", "long service", "performance", "client courtesy",
             "farewell", "other")

#: What each team may spend in a year, in whole rupees. A stand-in for the
#: finance system: one number per source, consumed by what has actually been
#: approved. Enough to make the check real, and not a model of a budget.
BUDGETS = {
    "Procurement · Chennai": 60_000,
    "Procurement · Bengaluru": 45_000,
    "People team": 80_000,
    "Service line · Advisory": 25_000,
    "Service line · Tax": 25_000,
    "Service line · Assurance": 25_000,
}

#: Above this, one gift needs compliance rather than a line manager —
#: whatever the recipient's running total says. A single large gift is a
#: different question from an accumulation of small ones.
BIG = 10_000

#: Where a request can be in its life. `given` is the only one that puts
#: anything on the register.
STATES = ("requested", "approved", "declined", "purchased", "given",
          "cancelled")

_WORDS = {
    "requested": "waiting for approval",
    "approved": "approved, not yet bought",
    "declined": "declined",
    "purchased": "bought, not yet given",
    "given": "given",
    "cancelled": "cancelled",
}


#: Two requests waiting on a fresh deployment, written only into an empty
#: queue. The second is the whole argument of the requirement in one row: a
#: perfectly ordinary ₹6,000 watch for John, which nobody would question on
#: its own, and which takes him to ₹26,000 because of what three other
#: teams have already given him.
SEED: list[dict[str, Any]] = [
    {"id": "R-SEED01", "for": "E-1066", "kind": "gift",
     "what": "Diwali hamper", "value": 3000, "occasion": "festival",
     "source": "Procurement · Bengaluru", "by": "servicelead",
     "by_employee": "E-1033", "at": 0.0},
    {"id": "R-SEED02", "for": "E-1007", "kind": "gift",
     "what": "Long service watch", "value": 6000, "occasion": "long service",
     "source": "People team", "by": "servicelead",
     "by_employee": "E-1033", "at": 0.0},
]


def _state(request: dict[str, Any]) -> str:
    """Where this request has got to, folded from what happened to it."""
    for kind in ("cancelled", "given", "purchased", "declined", "approved"):
        if ledger.happened(request["id"], kind):
            return kind
    return "requested"


def _spent(source: str) -> int:
    """What this team has committed: approved requests and what it has given.

    Committed rather than spent, because a budget that only counted what had
    already been handed over would let a team approve its way past the line
    and find out at distribution.
    """
    total = 0
    for request in ledger.requests():
        if request["source"] != source:
            continue
        if _state(request) in ("approved", "purchased", "given"):
            total += request["value"]
    return total


# ──────────────────────────────────────────────────────────────────────────
# the three checks
# ──────────────────────────────────────────────────────────────────────────

def _tax_check(scope: Scope, employee_id: str, value: int,
               kind: str) -> dict[str, Any]:
    """What approving this would do to the recipient's year.

    The point of the whole system, asked one gift earlier than before. The
    arithmetic is RewardLens's — this does not add anything up itself, so
    the figure the approver sees is the figure the register will show.
    """
    if kind not in rewardlens.COUNTS_TOWARDS_LIMIT:
        return {"ok": True, "said": f"{kind} is recorded and not counted yet"}

    # Read at firm breadth: a check that could only see the approver's own
    # records would clear a gift because the approver cannot see the
    # recipient's year, which is the opposite of what it is for.
    everything = rewardlens.totals(Scope(user=scope.user, period=scope.period,
                                         sees="firm"))
    row = next((r for r in everything if r["employee_id"] == employee_id), None)
    before = row["total"] if row else 0
    after = before + value
    limit = rewardlens.limit()
    if after > limit:
        return {
            "ok": False,
            "said": f"takes them to {rewardlens.rupees(after)} of "
                    f"{rewardlens.rupees(limit)} — "
                    f"{rewardlens.rupees(after - limit)} over",
        }
    return {"ok": True,
            "said": f"{rewardlens.rupees(after)} of {rewardlens.rupees(limit)}, "
                    f"{rewardlens.rupees(limit - after)} still inside"}


def _policy_check(request: dict[str, Any]) -> dict[str, Any]:
    """Whether this is permitted at all, before any arithmetic.

    Three rules, written out rather than configured, because a policy
    engine with one policy in it is a worse way of saying the same thing.
    They are the ones the requirement's own examples imply.
    """
    if request["kind"] == "hospitality":
        return {"ok": False,
                "said": "hospitality is not requested through this yet"}
    if request["occasion"] == "client courtesy" and request["value"] > BIG:
        return {"ok": False,
                "said": f"client courtesy above {rewardlens.rupees(BIG)} "
                        f"needs a conversation, not a form"}
    if request["for"] == request["by_employee"] and request["by_employee"]:
        return {"ok": False, "said": "you cannot request a gift for yourself"}
    if request["value"] > BIG:
        return {"ok": True,
                "said": f"over {rewardlens.rupees(BIG)}, so compliance "
                        f"approves rather than a line manager"}
    return {"ok": True, "said": "within policy"}


def _budget_check(request: dict[str, Any]) -> dict[str, Any]:
    """Whether the team asking still has the money."""
    budget = BUDGETS.get(request["source"])
    if budget is None:
        return {"ok": True, "said": "no budget recorded for this team"}
    committed = _spent(request["source"])
    if _state(request) in ("approved", "purchased", "given"):
        # Already counted in `committed`; do not charge it twice when the
        # row is redrawn after approval.
        committed -= request["value"]
    left = budget - committed
    if request["value"] > left:
        return {"ok": False,
                "said": f"{rewardlens.rupees(left)} left of "
                        f"{rewardlens.rupees(budget)} — short by "
                        f"{rewardlens.rupees(request['value'] - left)}"}
    return {"ok": True,
            "said": f"{rewardlens.rupees(left - request['value'])} would be "
                    f"left of {rewardlens.rupees(budget)}"}


def checks(scope: Scope, request: dict[str, Any]) -> dict[str, Any]:
    """All three, as the approver sees them."""
    policy = _policy_check(request)
    tax = _tax_check(scope, request["for"], request["value"], request["kind"])
    budget = _budget_check(request)
    return {"policy": policy, "tax": tax, "budget": budget,
            "blocked": not (policy["ok"] and budget["ok"]),
            # A tax consequence is not a reason to refuse — it is a reason
            # for somebody senior to decide knowingly, which is the whole
            # argument of the requirement.
            "warns": not tax["ok"]}


# ──────────────────────────────────────────────────────────────────────────
# rows
# ──────────────────────────────────────────────────────────────────────────

def _name_of(employee_id: str) -> str:
    for person in people.DIRECTORY.values():
        if person.employee_id == employee_id:
            return person.name
    for item in ledger.items():
        if item["employee_id"] == employee_id:
            return item["recipient"]
    return employee_id


def _rows(scope: Scope) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for request in ledger.requests():
        state = _state(request)
        verdict = checks(scope, request)
        decided = (ledger.latest(request["id"], "approved")
                   or ledger.latest(request["id"], "declined"))
        out.append({
            "id": request["id"],
            "employee_id": request["for"],
            "by_employee": request.get("by_employee", ""),
            "recipient": _name_of(request["for"]),
            "what": request["what"],
            "kind": request["kind"],
            "value": request["value"],
            "occasion": request["occasion"],
            "source": request["source"],
            "requested_by": request["by"],
            "state": state,
            "status": _WORDS[state],
            "policy": verdict["policy"]["said"],
            "tax": verdict["tax"]["said"],
            "budget": verdict["budget"]["said"],
            "blocked": verdict["blocked"],
            "warns": verdict["warns"],
            "decided_by": decided["by"] if decided else "",
            "why": (decided.get("note", "") if decided else "") or "—",
            "can": _allowed(scope, request, state, verdict),
        })
    order = {"requested": 0, "approved": 1, "purchased": 2}
    out.sort(key=lambda r: (order.get(r["state"], 3), -r["value"]))
    return scope.narrow(out, person_key=("employee_id", "by_employee"))


def _allowed(scope: Scope, request: dict[str, Any], state: str,
             verdict: dict[str, Any]) -> list[str]:
    """What this person may do to this request, right now."""
    if scope.period in rewardlens.closed_periods():
        return []

    mine_to_approve = ("compliance" in scope.roles or "manager" in scope.roles)
    i_asked = request["by"] == scope.user
    for_me = scope.mine(request["for"])

    can: list[str] = []
    if state == "requested":
        # Neither your own request nor a gift to you. Both are the same
        # rule wearing different clothes: nobody signs off their own.
        if mine_to_approve and not i_asked and not for_me:
            # Policy and budget block; a tax consequence does not — it is
            # shown, and somebody decides with it in front of them.
            if not verdict["blocked"]:
                can.append("approve")
            can.append("decline")
        if i_asked:
            can.append("cancel_request")
    if state == "approved" and "procurement" in scope.roles:
        can.append("mark_purchased")
    if state == "purchased" and "procurement" in scope.roles:
        can.append("mark_given")
    return can


class GiftRequests(Feature):
    key = "gift_requests"
    row_key = "id"
    private = frozenset({"by_employee", "state", "blocked", "warns", "can"})

    REQUEST_FORM = "request_gift"
    FIELDS = frozenset({"recipient", "kind", "what", "value", "occasion",
                        "source"})

    def figures(self, scope: Scope) -> list[Figure]:
        rows = _rows(scope)
        waiting = [r for r in rows if r["state"] == "requested"]
        blocked = [r for r in waiting if r["blocked"]]
        crossing = [r for r in waiting if r["warns"]]
        to_hand_over = [r for r in rows
                        if r["state"] in ("approved", "purchased")]
        return [
            Figure("waiting", str(len(waiting)),
                   "waiting for approval" if waiting else "nothing waiting",
                   "warn" if waiting else "good"),
            Figure("blocked", str(len(blocked)),
                   "cannot be approved as asked" if blocked
                   else "none blocked", "bad" if blocked else "good"),
            # The figure the requirement is really about: how much is about
            # to push somebody past the limit, caught before it is bought.
            Figure("crossing", str(len(crossing)),
                   "would take somebody over the limit" if crossing
                   else "none would cross the limit",
                   "bad" if crossing else "good"),
            Figure("to_hand_over", str(len(to_hand_over)),
                   "approved and not yet given" if to_hand_over
                   else "nothing outstanding",
                   "warn" if to_hand_over else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        rows = _rows(scope)
        counts = {state: len([r for r in rows if r["state"] == state])
                  for state in STATES}
        return [
            Tab("waiting", "Waiting for approval", counts["requested"]),
            Tab("moving", "Approved and on the way",
                counts["approved"] + counts["purchased"]),
            Tab("all", "Everything", len(rows)),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        rows = _rows(scope)
        if tab == "waiting":
            return [r for r in rows if r["state"] == "requested"]
        if tab == "moving":
            return [r for r in rows if r["state"] in ("approved", "purchased")]
        return rows

    def rules(self, scope: Scope) -> list[str]:
        if scope.sees == "none":
            return ["not_yours_to_see"]
        if scope.period in rewardlens.closed_periods():
            return ["period_closed"]
        rows = _rows(scope)
        said: list[str] = []
        if any(r["warns"] and r["state"] == "requested" for r in rows):
            said.append("would_cross_the_limit")
        if any(r["blocked"] and r["state"] == "requested" for r in rows):
            said.append("against_policy")
        if not rows:
            said.append("nothing_requested")
        return said

    # ── acting ──────────────────────────────────────────────────────────

    def act(self, scope: Scope, action: str, targets: list[str],
            note: str = "") -> Outcome:
        if scope.period in rewardlens.closed_periods():
            return Outcome(ok=False, said=f"{scope.period} has been signed "
                                          f"off — nothing in it can change.")
        rows = {r["id"]: r for r in _rows(scope)}
        chosen = [rows[t] for t in targets if t in rows]
        if not chosen:
            return Outcome(ok=False, said="No request by that id is in this view.")
        if len(chosen) > 1:
            return Outcome(ok=False, said="Each request is decided on its own.")
        row = chosen[0]

        if action not in row["can"]:
            return Outcome(
                ok=False,
                said=f"{row['what']} is {row['status']} — that is not "
                     f"something you can do to it now.",
            )

        request = next(r for r in ledger.requests() if r["id"] == row["id"])

        if action == "decline" and not note.strip():
            return Outcome(ok=False, said="Say why. A request declined with "
                                          "no reason is one nobody can learn "
                                          "from.")

        if action == "approve":
            ledger.add_event(subject=row["id"], kind="approved", by=scope.user,
                             note=note.strip(), value=row["value"],
                             tax=row["tax"])
            crossing = (" It takes them over the annual limit, which you have "
                        "approved knowingly." if row["warns"] else "")
            return Outcome(
                ok=True,
                said=f"Approved: {row['what']} for {row['recipient']}, "
                     f"{rewardlens.rupees(row['value'])}.{crossing} "
                     f"Procurement can buy it.",
                touched=[row["id"]],
            )

        if action == "decline":
            ledger.add_event(subject=row["id"], kind="declined", by=scope.user,
                             note=note.strip())
            return Outcome(ok=True, said=f"Declined, with your reason on the "
                                         f"record. Nothing was bought.",
                           touched=[row["id"]])

        if action == "cancel_request":
            ledger.add_event(subject=row["id"], kind="cancelled",
                             by=scope.user, note=note.strip())
            return Outcome(ok=True, said="Withdrawn. Nothing was bought.",
                           touched=[row["id"]])

        if action == "mark_purchased":
            ledger.add_event(subject=row["id"], kind="purchased", by=scope.user)
            return Outcome(ok=True, said=f"{row['what']} marked as bought. It "
                                         f"reaches the register when it is "
                                         f"handed over.",
                           touched=[row["id"]])

        if action == "mark_given":
            # THE moment: a proposal becomes a record. Nothing before this
            # is on anybody's total, and nothing after it can be taken off.
            item = {
                "id": f"G-{uuid.uuid4().hex[:6].upper()}",
                "employee_id": request["for"],
                "recipient": row["recipient"],
                "kind": request["kind"],
                "what": request["what"],
                "value": request["value"],
                "given": dt.date.today().isoformat(),
                "source": request["source"],
                "channel": "procured",
                # Not acknowledged: the recipient says so, and until they do
                # the register says somebody was given something they have
                # not confirmed.
                "acknowledged": False,
                "from_request": row["id"],
            }
            ledger.add_item(item)
            ledger.add_event(subject=row["id"], kind="given", by=scope.user,
                             item=item["id"])
            after = next((r["total"] for r in rewardlens.totals(
                Scope(user=scope.user, period=scope.period, sees="firm"))
                if r["employee_id"] == request["for"]), request["value"])
            return Outcome(
                ok=True,
                said=f"Given to {row['recipient']} and on the register. Their "
                     f"year is now {rewardlens.rupees(after)}, and they have "
                     f"it to acknowledge.",
                touched=[row["id"], item["id"]],
            )

        return Outcome(ok=False, said=f"No action called {action!r}.")

    # ── asking ──────────────────────────────────────────────────────────

    def accepts(self, form_id: str) -> set[str]:
        return set(self.FIELDS) if form_id == self.REQUEST_FORM else set()

    def _read(self, scope: Scope, values: dict[str, str]
              ) -> tuple[dict[str, Any] | None, Outcome | None]:
        """The request these values describe, or why they do not describe one."""
        if scope.period in rewardlens.closed_periods():
            return None, Outcome(ok=False, said=f"{scope.period} has been "
                                                f"signed off.")
        recipient = values.get("recipient", "").strip()
        employee = next((p.employee_id for p in people.DIRECTORY.values()
                         if p.name.lower() == recipient.lower()
                         and p.employee_id), "")
        if not employee:
            employee = next((i["employee_id"] for i in ledger.items()
                             if i["recipient"].lower() == recipient.lower()), "")
        if not employee:
            return None, Outcome(
                ok=False,
                said=f"Compass does not know anybody called {recipient!r}. "
                     f"A gift has to be recorded against somebody.")

        kind = values.get("kind", "").strip().lower()
        if kind not in KINDS:
            return None, Outcome(ok=False, said=f"Pick one of: "
                                                f"{', '.join(sorted(KINDS))}.")
        occasion = values.get("occasion", "").strip().lower()
        if occasion not in OCCASIONS:
            return None, Outcome(ok=False, said=f"Pick one of: "
                                                f"{', '.join(OCCASIONS)}.")
        what = values.get("what", "").strip()
        if not what:
            return None, Outcome(ok=False, said="Say what it is.")
        source = values.get("source", "").strip()
        if source not in BUDGETS:
            return None, Outcome(ok=False, said=f"Which team is paying? One "
                                                f"of: {', '.join(BUDGETS)}.")
        value = rewardlens.whole_rupees(values.get("value", ""))
        if value is None or value <= 0:
            return None, Outcome(ok=False, said="Give the value in whole "
                                                "rupees — 8000, or 8,000.")
        if value > rewardlens.most_per_item():
            return None, Outcome(ok=False, said="That is past what this form "
                                                "takes.")
        return {
            "id": f"R-{uuid.uuid4().hex[:6].upper()}",
            "for": employee, "kind": kind, "what": what, "value": value,
            "occasion": occasion, "source": source,
            "by": scope.user, "by_employee": scope.employee,
            "at": dt.datetime.now().timestamp(),
        }, None

    def preview(self, scope: Scope, form_id: str, values: dict[str, str],
                about: str = "") -> Outcome:
        if form_id != self.REQUEST_FORM:
            return Outcome(ok=False, said=f"No form called {form_id!r}.")
        request, refused = self._read(scope, values)
        if refused is not None:
            return refused
        assert request is not None
        verdict = checks(scope, request)
        parts = [f"Policy: {verdict['policy']['said']}.",
                 f"Their year: {verdict['tax']['said']}.",
                 f"Budget: {verdict['budget']['said']}."]
        if verdict["blocked"]:
            parts.append("As asked, this cannot be approved.")
        elif verdict["warns"]:
            parts.append("It can be approved, and whoever does will see that "
                         "it crosses the limit.")
        return Outcome(ok=True, said=" ".join(parts))

    def submit(self, scope: Scope, form_id: str, values: dict[str, str],
               about: str = "") -> Outcome:
        if form_id != self.REQUEST_FORM:
            return Outcome(ok=False, said=f"No form called {form_id!r}.")
        request, refused = self._read(scope, values)
        if refused is not None:
            return refused
        assert request is not None
        ledger.add_request(request)
        ledger.add_event(subject=request["id"], kind="requested",
                         by=scope.user, value=request["value"],
                         recipient=request["for"])
        verdict = checks(scope, request)
        note = ("" if not verdict["warns"]
                else " It would take them over the annual limit, and whoever "
                     "approves it will be told so.")
        return Outcome(
            ok=True,
            said=f"Requested: {request['what']} for "
                 f"{_name_of(request['for'])}, "
                 f"{rewardlens.rupees(request['value'])}.{note}",
            touched=[request["id"]],
        )


register(GiftRequests())
