"""RewardLens — what each person received, totalled against the annual limit.

The firm already knows what it spent. What it could not say is that one person
received ₹8,000, then ₹5,000, then ₹7,000 from three different teams and is
now at ₹20,000 — past the point where Finance has to look at it. Nobody was
hiding anything; the three purchases simply never met.

So the unit on screen is a PERSON, not a purchase. Every tab lists recipients
and their running total, and the individual items sit underneath. A list of
purchases is what the firm already had.

THE TOTAL IS ADDITION AND NOTHING ELSE. `_totals` sums the items; the limit
comparison is `>`; the excess is a subtraction. No part of the number a tax
decision rests on is generated, and the check beside this file asserts the
arithmetic against the worked example from the requirement.

A PERSON CANNOT CLEAR THEIR OWN BREACH. If the signed-in reviewer is also a
recipient their row is shown — hiding it would be worse — but every action
against it is refused. That is the one rule a system like this exists to
enforce, so it lives in code rather than in a prompt or a convention.

PHASE. Gifts, awards and vouchers are recorded here and count towards the
limit. Hospitality and reimbursements are named in the requirement for a
later phase: they are kept out of the total rather than counted as zero,
because a limit that silently omits a category is worse than one that says
what it covers.

The records are a FIXTURE. `_items_for` is the single place the store
replaces, and the worked example from the requirement is in it on purpose so
the arithmetic can be checked against something somebody wrote down.
"""

from __future__ import annotations

from typing import Any

from compass.businessfunctions.features.base import (
    Feature,
    Figure,
    Outcome,
    Scope,
    Tab,
    register,
)

#: The annual limit per person, in whole rupees. Past it, Finance decides the
#: tax treatment. The figure is the firm's, not this module's — it lives here
#: only until there is somewhere to configure it.
ANNUAL_LIMIT = 15_000

#: What counts towards the limit today. Hospitality and reimbursements are
#: recorded by the business but not yet aggregated here; see PHASE above.
COUNTS_TOWARDS_LIMIT = frozenset({"gift", "award", "voucher"})

#: Periods already signed off. Nothing in them moves, for anybody.
_CLOSED = {"FY 2024-25"}

#: Which employee the signed-in user is. A stand-in for the directory lookup
#: that arrives with the store — it exists now so the conflict-of-interest
#: rule is exercised rather than assumed.
_USER_IS = {"mk": "E-1041"}

_ITEMS: list[dict[str, Any]] = [
    # The worked example from the requirement: three gifts, three teams,
    # ₹20,000 in total, nobody aware of the other two.
    {"id": "G-2201", "employee_id": "E-1007", "recipient": "John Mathew",
     "kind": "gift", "what": "Diwali hamper", "value": 8000, "given": "2026-10-12",
     "source": "Procurement · Chennai", "channel": "procured", "acknowledged": True},
    {"id": "G-2238", "employee_id": "E-1007", "recipient": "John Mathew",
     "kind": "voucher", "what": "Retail voucher", "value": 5000, "given": "2026-11-03",
     "source": "Service line · Advisory", "channel": "procured", "acknowledged": True},
    {"id": "G-2290", "employee_id": "E-1007", "recipient": "John Mathew",
     "kind": "award", "what": "Long service award", "value": 7000, "given": "2026-12-01",
     "source": "People team", "channel": "procured", "acknowledged": False},

    # The threshold example: 5,000 + 4,000 + 7,000 = 16,000.
    {"id": "G-2211", "employee_id": "E-1019", "recipient": "Priya Nair",
     "kind": "gift", "what": "Festival box", "value": 5000, "given": "2026-10-14",
     "source": "Procurement · Bengaluru", "channel": "procured", "acknowledged": True},
    {"id": "G-2255", "employee_id": "E-1019", "recipient": "Priya Nair",
     "kind": "gift", "what": "Client dinner gift", "value": 4000, "given": "2026-11-20",
     "source": "Service line · Tax", "channel": "disclosed", "acknowledged": True},
    {"id": "G-2301", "employee_id": "E-1019", "recipient": "Priya Nair",
     "kind": "award", "what": "Quarter award", "value": 7000, "given": "2026-12-09",
     "source": "People team", "channel": "procured", "acknowledged": True},

    {"id": "G-2240", "employee_id": "E-1033", "recipient": "Rahul Desai",
     "kind": "gift", "what": "Diwali hamper", "value": 6000, "given": "2026-10-12",
     "source": "Procurement · Chennai", "channel": "procured", "acknowledged": True},
    {"id": "G-2272", "employee_id": "E-1033", "recipient": "Rahul Desai",
     "kind": "voucher", "what": "Bookstore voucher", "value": 3000, "given": "2026-11-28",
     "source": "Service line · Assurance", "channel": "procured", "acknowledged": False},

    {"id": "G-2248", "employee_id": "E-1052", "recipient": "Aisha Khan",
     "kind": "award", "what": "Innovation award", "value": 12000, "given": "2026-11-05",
     "source": "People team", "channel": "procured", "acknowledged": False},

    # Declared by the employee rather than bought by Procurement. It counts
    # from the moment it is recorded, which is the point of the disclosure.
    {"id": "G-2311", "employee_id": "E-1066", "recipient": "Vikram Rao",
     "kind": "gift", "what": "Vendor hamper", "value": 2500, "given": "2026-12-15",
     "source": "Declared by recipient", "channel": "disclosed", "acknowledged": True},

    # The signed-in reviewer's own row, so the conflict rule has something to
    # catch rather than being a comment.
    {"id": "G-2264", "employee_id": "E-1041", "recipient": "Manish K.",
     "kind": "gift", "what": "Diwali hamper", "value": 8000, "given": "2026-10-12",
     "source": "Procurement · Bengaluru", "channel": "procured", "acknowledged": True},
    {"id": "G-2299", "employee_id": "E-1041", "recipient": "Manish K.",
     "kind": "award", "what": "Long service award", "value": 9000, "given": "2026-12-02",
     "source": "People team", "channel": "procured", "acknowledged": True},

    # Recorded, and deliberately outside the total. See PHASE.
    {"id": "H-0412", "employee_id": "E-1007", "recipient": "John Mathew",
     "kind": "hospitality", "what": "Client dinner", "value": 3200, "given": "2026-11-11",
     "source": "Service line · Advisory", "channel": "procured", "acknowledged": True},
]

#: Who has been sent to Finance, and who has an accepted exception against
#: them. Separate from the items because they are decisions about a person's
#: year, not facts about one purchase.
_REFERRED: set[str] = set()
_EXCEPTED: set[str] = set()


def _items_for(scope: Scope) -> list[dict[str, Any]]:
    """Every recorded item in the period. The store replaces this and nothing else."""
    return _ITEMS


def rupees(amount: int) -> str:
    """Indian digit grouping — ₹20,000, ₹1,50,000.

    Not the lakh notation Form 26 uses: a credit statement deals in lakhs and
    a gift does not, and "₹0.2L" is a worse way to say eight thousand rupees.
    """
    s = str(abs(int(amount)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{'-' if amount < 0 else ''}₹{s}"


def _totals(scope: Scope) -> list[dict[str, Any]]:
    """One row per person: their items, their total, and where it stands.

    Built fresh from the items every time rather than kept alongside them —
    a stored total is a total that can disagree with the things it is the sum
    of, which is the whole failure this feature exists to fix.
    """
    people: dict[str, dict[str, Any]] = {}
    for item in _items_for(scope):
        row = people.setdefault(item["employee_id"], {
            "employee_id": item["employee_id"],
            "recipient": item["recipient"],
            "items": [],
        })
        row["items"].append(item)

    out: list[dict[str, Any]] = []
    for row in people.values():
        counted = [i for i in row["items"] if i["kind"] in COUNTS_TOWARDS_LIMIT]
        total = sum(i["value"] for i in counted)
        unack = [i for i in row["items"] if not i["acknowledged"]]
        disclosed = [i for i in row["items"] if i["channel"] == "disclosed"]
        over = total > ANNUAL_LIMIT
        out.append({
            "employee_id": row["employee_id"],
            "recipient": row["recipient"],
            "items_counted": len(counted),
            "total": total,
            "limit": ANNUAL_LIMIT,
            # Positive when over, so a reader does not have to work out the
            # sign of a "headroom" that has gone negative.
            "excess": max(0, total - ANNUAL_LIMIT),
            "headroom": max(0, ANNUAL_LIMIT - total),
            "over_limit": over,
            "unacknowledged": len(unack),
            "disclosed": len(disclosed),
            "referred": row["employee_id"] in _REFERRED,
            "exception": row["employee_id"] in _EXCEPTED,
            "status": ("referred" if row["employee_id"] in _REFERRED
                       else "exception" if row["employee_id"] in _EXCEPTED
                       else "over limit" if over
                       else "within limit"),
            # Pre-formatted so the detail panel reads as a list of things
            # rather than as a dump of a nested structure.
            "breakdown": " · ".join(
                f"{i['what']} {rupees(i['value'])} ({i['kind']})" for i in row["items"]
            ),
            "not_counted": " · ".join(
                f"{i['what']} {rupees(i['value'])} ({i['kind']}, later phase)"
                for i in row["items"] if i["kind"] not in COUNTS_TOWARDS_LIMIT
            ) or "—",
        })
    # Worst first: the people a reviewer has to deal with are at the top.
    return sorted(out, key=lambda r: (-r["total"], r["recipient"]))


class RewardLens(Feature):
    key = "rewardlens"
    #: A row is a person, not a purchase — the whole point of the screen.
    row_key = "employee_id"

    def figures(self, scope: Scope) -> list[Figure]:
        rows = _totals(scope)
        over = [r for r in rows if r["over_limit"]]
        unack = sum(r["unacknowledged"] for r in rows)
        counted = sum(r["total"] for r in rows)
        return [
            Figure("recipients", str(len(rows)), "people tracked"),
            Figure("recorded", rupees(counted), "counting towards limits"),
            Figure("over", str(len(over)),
                   "over the annual limit" if over else "all within limit",
                   "bad" if over else "good"),
            Figure("unacknowledged", str(unack),
                   "not yet acknowledged" if unack else "all acknowledged",
                   "warn" if unack else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        rows = _totals(scope)
        return [
            Tab("over", "Over the limit",
                len([r for r in rows if r["over_limit"]])),
            Tab("all", "Everyone", len(rows)),
            Tab("unacknowledged", "Awaiting acknowledgement",
                len([r for r in rows if r["unacknowledged"]])),
            Tab("disclosed", "Self-disclosed",
                len([r for r in rows if r["disclosed"]])),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        rows = _totals(scope)
        if tab == "over":
            return [r for r in rows if r["over_limit"]]
        if tab == "unacknowledged":
            return [r for r in rows if r["unacknowledged"]]
        if tab == "disclosed":
            return [r for r in rows if r["disclosed"]]
        return rows

    def rules(self, scope: Scope) -> list[str]:
        if scope.period in _CLOSED:
            return ["period_closed"]
        rows = _totals(scope)
        said: list[str] = []
        if any(r["over_limit"] for r in rows):
            said.append("over_limit")
        mine = _USER_IS.get(scope.user)
        if mine and any(r["employee_id"] == mine for r in rows):
            said.append("you_are_a_recipient")
        if any(r["disclosed"] for r in rows):
            said.append("disclosures_waiting")
        return said

    def act(self, scope: Scope, action: str, targets: list[str]) -> Outcome:
        if scope.period in _CLOSED:
            return Outcome(
                ok=False,
                said=f"{scope.period} has been signed off — nothing in it can change.",
            )

        rows = {r["employee_id"]: r for r in _totals(scope)}
        chosen = [rows[t] for t in targets if t in rows]
        if not chosen:
            return Outcome(ok=False, said="Nobody by that id is in this view.")

        # The rule the whole feature exists for. Checked before anything else,
        # because a reviewer acting on their own year is not a smaller problem
        # when the action happens to be a reasonable one.
        mine = _USER_IS.get(scope.user)
        if mine and any(r["employee_id"] == mine for r in chosen):
            return Outcome(
                ok=False,
                said="That is your own record. Someone else has to decide it.",
            )

        if action == "refer_to_finance":
            if len(chosen) > 1:
                return Outcome(
                    ok=False,
                    said="A referral names one person and goes to Finance as "
                         "their case, so they go one at a time.",
                )
            row = chosen[0]
            if not row["over_limit"]:
                return Outcome(
                    ok=False,
                    said=f"{row['recipient']} is at {rupees(row['total'])}, "
                         f"{rupees(row['headroom'])} inside the limit. There is "
                         f"nothing for Finance to decide.",
                )
            if row["referred"]:
                return Outcome(
                    ok=False,
                    said=f"{row['recipient']} has already gone to Finance.",
                )
            _REFERRED.add(row["employee_id"])
            return Outcome(
                ok=True,
                said=f"{row['recipient']} referred to Finance — "
                     f"{rupees(row['total'])} against a {rupees(ANNUAL_LIMIT)} "
                     f"limit, {rupees(row['excess'])} over.",
                touched=[row["employee_id"]],
            )

        if action == "chase_acknowledgement":
            if len(chosen) > 1:
                return Outcome(
                    ok=False,
                    said="Each chase is an email to one person. Name the one "
                         "you mean.",
                )
            row = chosen[0]
            if not row["unacknowledged"]:
                return Outcome(
                    ok=False,
                    said=f"{row['recipient']} has acknowledged everything.",
                )
            return Outcome(
                ok=True,
                said=f"Asked {row['recipient']} to acknowledge "
                     f"{row['unacknowledged']} item"
                     f"{'s' if row['unacknowledged'] > 1 else ''}.",
                touched=[row["employee_id"]],
            )

        if action == "record_exception":
            row = chosen[0] if len(chosen) == 1 else None
            if row is None:
                return Outcome(
                    ok=False,
                    said="An exception is recorded against one person, with a "
                         "reason. Name the one you mean.",
                )
            if not row["over_limit"]:
                return Outcome(
                    ok=False,
                    said=f"{row['recipient']} is within the limit, so there is "
                         f"no breach to accept.",
                )
            _EXCEPTED.add(row["employee_id"])
            return Outcome(
                ok=True,
                said=f"Exception recorded for {row['recipient']}. The breach "
                     f"stays on the record and Finance still sees the total.",
                touched=[row["employee_id"]],
            )

        return Outcome(ok=False, said=f"RewardLens has no action called {action!r}.")


register(RewardLens())
