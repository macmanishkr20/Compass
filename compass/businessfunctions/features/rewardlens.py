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

WHO SEES WHAT IS NOT DECIDED HERE. `scope.narrow` is applied at the single
place the rows are built, and the breadth it applies was decided by the
manifest and the directory before this module was called. An employee
opening this screen sees one row — their own, against the limit — which is
a useful screen rather than a locked door, and it is the same code path
that shows compliance all six.

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

import datetime as dt
import time
import uuid
from typing import Any

from compass.businessfunctions import notices, people, registry
from compass.common.config import get_settings

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

#: What a person may declare. Wider than what counts: hospitality is recorded
#: in this phase and aggregated in a later one, and a form that refused to
#: accept it would be a form that taught people not to mention it.
_KINDS = frozenset({"gift", "award", "voucher", "hospitality"})

#: Periods already signed off. Nothing in them moves, for anybody.
_CLOSED = {"FY 2024-25"}

def _employee(user: str) -> str:
    """The employee record this person is, or "" when Compass cannot say.

    Delegated to the directory. This, the address and the reviewer role were
    three separate tables in this file; they are one fact about a person and
    they now live in one place, where a second feature can ask the same
    question and get the same answer.
    """
    return people.employee_of(user)


def address_of(user: str) -> str:
    """Where to write to this person, or "" when the directory cannot say."""
    return people.address_of(user)


def _is_reviewer(user: str) -> bool:
    """Whether this person holds the compliance role."""
    return "compliance" in people.roles_of(user)


def _may(scope: Scope, action_id: str) -> bool:
    """Whether this person holds a role the action was declared for.

    Read off the manifest at call time rather than copied into a constant,
    because the two drifting apart is how a screen comes to offer a button
    the server refuses — or worse, accept one it should not have.
    """
    feature = registry.feature_of("scs", "rewardlens")[1]
    if feature is None:
        return False
    declared = next((a.roles for a in feature.actions if a.id == action_id), None)
    # No roles declared means anybody who can see the row, which is what
    # every action meant before roles existed.
    return not declared or bool(scope.roles & set(declared))


def _address_for_employee(employee_id: str) -> str:
    """Where to write to the person a record is about.

    The directory is keyed by login and a record names an employee, so this
    is the one place that walks it the other way. Empty when nobody knows,
    which is a state the screen shows rather than a send to somebody else.
    """
    return next((p.email for p in people.DIRECTORY.values()
                 if p.employee_id == employee_id and p.email), "")


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

#: The most anybody can declare in one go. Not a policy limit — a typo
#: guard. Six zeros where five were meant is the mistake this catches, and a
#: real gift that large needs a conversation, not a form.
_MOST_PER_ITEM = 10_00_000

#: How long a description may be. Long enough to say what it was.
_TEXT_MAX = 120


#: What a review can conclude. Deliberately not "approved" and "rejected":
#: a disclosure is a statement that something happened, and refusing it does
#: not un-happen. What a reviewer decides is whether the record stands as
#: made, whether they need to ask the person something, or whether what they
#: were given was not permitted — and in every one of those the item still
#: counts towards the limit.
REVIEW_STATES = ("awaiting", "accepted", "queried", "answered", "breach",
                 "superseded")

#: What each state says on the row.
_STATE_WORDS = {
    "awaiting": "awaiting review",
    "accepted": "accepted",
    "queried": "queried with the declarer",
    "answered": "answered — needs deciding",
    "breach": "not permitted",
    "superseded": "superseded",
}

#: WHY A BREACH STILL COUNTS AND A CORRECTION DOES NOT.
#:
#: These look similar and are opposites, and the difference is the lever
#: somebody would reach for first.
#:
#:   a BREACH says you should not have been given it. You were given it, so
#:   it counts, and no reviewer can decide otherwise.
#:
#:   a CORRECTION says the RECORD was wrong — the figure was the whole
#:   dinner, or the thing was declared twice. What was counted was never
#:   right, so the superseded record stops counting and the corrected one
#:   takes its place.
#:
#: So the only way an item stops counting is for somebody to conclude the
#: record was mistaken, and the person who received it cannot conclude that
#: alone: they propose it and a reviewer decides. Both records stay.

#: Decisions taken on disclosed items: item id -> who, when, what and why.
#: Beside the items rather than inside them because a decision is a record
#: about the record — the item is what was declared, and this is what somebody
#: did about it afterwards.
_REVIEWED: dict[str, dict[str, Any]] = {}

#: What a declarer said when asked: item id -> {response, value, note}.
#: A proposal, not a change — nothing here moves a figure until a reviewer
#: accepts it.
_ANSWERS: dict[str, dict[str, Any]] = {}

#: Records the firm has concluded were wrong: item id -> the id of the record
#: that replaces it, or "" when there is nothing to replace it with. A
#: superseded item stays on every list and in no total.
_SUPERSEDED: dict[str, str] = {}

#: How a declarer may answer being asked about something.
ANSWERS = {
    "stands": "it is right as declared",
    "corrected": "the value was wrong",
    "withdrawn": "I should not have declared it",
}


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


def _year_window(period: str) -> tuple[dt.date, dt.date] | None:
    """The first and last day of an Indian financial year, or None.

    "FY 2026-27" runs 1 April 2026 to 31 March 2027. Spelt out because the
    limit is annual: a date outside the year belongs to a different total,
    and quietly accepting it would put the item in the wrong year rather
    than refusing it.
    """
    try:
        start = int(period.split()[1].split("-")[0])
    except (IndexError, ValueError):
        return None
    return dt.date(start, 4, 1), dt.date(start + 1, 3, 31)


def _whole_rupees(raw: str) -> int | None:
    """A value a person typed, as whole rupees, or None if it is not one.

    Accepts "8000", "8,000", "₹8,000" and " 8000 ", because those are all the
    same thing to the person typing and a form that refuses a comma is a form
    people route around.
    """
    cleaned = raw.strip().replace(",", "").replace("₹", "").strip()
    if not cleaned.isdigit():
        return None
    return int(cleaned)


def _review_of(item_id: str) -> dict[str, Any]:
    """The decision on this item, or the absence of one."""
    return _REVIEWED.get(item_id,
                         {"state": "awaiting", "by": "", "note": "", "at": 0.0})


def _allowed(scope: Scope, item: dict[str, Any], state: str) -> list[str]:
    """What THIS person may do to THIS declaration, right now.

    A list rather than a flag, because what is open depends on the state as
    well as the person: a queried item is the declarer's to answer and not
    the reviewer's to accept, and an answered one is the other way round.

    On the row because the surface cannot work any of it out — it does not
    know who holds the reviewer role, whose declaration this is, or what the
    rules are. Drawing a button the server will refuse is worse than drawing
    none: by the time the person is told, they have already decided to do it.
    """
    if scope.period in _CLOSED or item["id"] in _SUPERSEDED:
        return []

    mine = _employee(scope.user) == item["employee_id"]
    # The declarer answers, and only when they have been asked.
    if mine:
        return ["answer_query"] if state == "queried" else []

    # What the manifest said about this action, not what this file thinks.
    # One rule, declared once, applied here — so changing who may review is
    # a line of YAML rather than a hunt through handlers.
    if not _may(scope, "accept_disclosure"):
        return []

    # A notice that could not be delivered is a dead end unless somebody can
    # try again — which is the point of recording it rather than firing it.
    undelivered = [n for n in notices.for_item(item["id"])
                   if n.state in ("held", "failed")]
    # Only when a retry could actually work: something undelivered, an
    # address to send it to, and a mail server to send it with. Without any
    # of those, retrying would refuse every time and the fix is somewhere
    # else — the directory, or the deployment's configuration. The row's own
    # "not emailed — ..." text already says which, and a button that
    # restates it and then fails adds nothing.
    retry = (["resend_notice"] if undelivered
             and _address_for_employee(item["employee_id"])
             and notices.configured() else [])

    if state == "queried":
        return retry
    if state == "awaiting":
        return ["accept_disclosure", "query_disclosure", "record_breach"] + retry
    if state == "answered":
        # Accepting an answer is its own decision: it may apply a correction,
        # which "accept as declared" would be the wrong words for.
        return ["accept_answer", "query_disclosure", "record_breach"] + retry
    return retry


def _disclosed_rows(scope: Scope) -> list[dict[str, Any]]:
    """One row per DECLARED ITEM, not per person.

    Every other tab here lists people, because a limit is a thing a person
    has. A review is a thing an item has, so this tab lists items — the same
    reason Talent's balances tab lists leave types rather than requests.
    """
    out: list[dict[str, Any]] = []
    for item in _items_for(scope):
        if item["channel"] != "disclosed":
            continue
        review = _review_of(item["id"])
        answer = _ANSWERS.get(item["id"], {})
        state = ("superseded" if item["id"] in _SUPERSEDED
                 else "answered" if answer and review["state"] == "queried"
                 else review["state"])
        out.append({
            "id": item["id"],
            "employee_id": item["employee_id"],
            "declared_by": item["recipient"],
            "what": item["what"],
            "kind": item["kind"],
            "value": item["value"],
            "given": item["given"],
            "source": item["source"],
            # Said on every row, in every state, because the one thing people
            # will assume is that a queried item stops counting.
            "counts": ("yes" if item["kind"] in COUNTS_TOWARDS_LIMIT
                       else "later phase"),
            "state": state,
            "status": _STATE_WORDS[state],
            "reviewed_by": review["by"],
            "reason": review["note"] or "—",
            # When the decision was taken, so another screen can say how
            # long something has been sitting. A decision on somebody's
            # record without a time against it is half a record.
            "asked_at": review.get("at", 0.0),
            # The declarer's side of the conversation, kept beside the
            # reviewer's rather than replacing it.
            "answered": ANSWERS.get(answer.get("response", ""), "—"),
            "answer_note": answer.get("note", "") or "—",
            "replaced_by": _SUPERSEDED.get(item["id"]) or
                           ("withdrawn" if item["id"] in _SUPERSEDED else "—"),
            "corrects": item.get("corrects", "") or "—",
            # Whether the person was actually told. On the row because the
            # alternative is a reviewer assuming it, and "I asked them three
            # weeks ago" is the kind of thing somebody says in an audit.
            "notified": (notice.said if (notice := notices.latest_for(item["id"]))
                         else "—"),
            "can": _allowed(scope, item, state),
        })
    # What somebody has to deal with, first: unanswered queries and answers
    # waiting on a decision come before anything already settled.
    order = {"answered": 0, "awaiting": 1, "queried": 2}
    out.sort(key=lambda r: (order.get(r["state"], 3), r["given"]))
    return scope.narrow(out, person_key="employee_id")


def _awaiting(scope: Scope) -> list[dict[str, Any]]:
    """What is on a reviewer's desk: never looked at, or answered and waiting."""
    return [r for r in _disclosed_rows(scope)
            if r["state"] in ("awaiting", "answered")]


def _unanswered(scope: Scope) -> list[dict[str, Any]]:
    """What is on a DECLARER's desk: asked about, and not yet answered."""
    return [r for r in _disclosed_rows(scope) if r["state"] == "queried"]


def _totals(scope: Scope) -> list[dict[str, Any]]:
    """One row per person: their items, their total, and where it stands.

    Built fresh from the items every time rather than kept alongside them —
    a stored total is a total that can disagree with the things it is the sum
    of, which is the whole failure this feature exists to fix.
    """
    people: dict[str, dict[str, Any]] = {}
    for item in _items_for(scope):
        # A superseded record is one the firm has concluded was wrong. It
        # stays on the declarations tab, where the trail is; it is not in
        # anybody's total, because the thing it recorded did not happen the
        # way it said.
        if item["id"] in _SUPERSEDED:
            continue
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
        # The date the running total first went past the limit — a fact, and
        # not a number of days: several of these are dated later in the
        # financial year than today, and "over for -54 days" is the kind of
        # arithmetic that makes a reader stop believing the rest.
        crossed, running = "", 0
        for item in sorted(counted, key=lambda i: i["given"]):
            running += item["value"]
            if running > ANNUAL_LIMIT:
                crossed = item["given"]
                break
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
            "crossed_on": crossed or "—",
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
    out.sort(key=lambda r: (-r["total"], r["recipient"]))
    # And narrowed to what this person may see, HERE rather than on the way
    # out: figures, tabs and the table are all built from this one call, so
    # narrowing it once is what stops an employee being shown "6 people
    # tracked" above a list containing only themselves.
    return scope.narrow(out, person_key="employee_id")


# ── what another screen may read ──────────────────────────────────────────
#
# A dashboard over this data must not do the arithmetic again. If it did,
# the dashboard and the workbench could disagree about who is over the
# limit, and for a compliance system two numbers for one fact is the worst
# outcome available — worse than one wrong number, because nobody can tell
# which to believe.
#
# So these are the same rows the workbench shows, exported under names that
# say they are a read. Nothing here is a second calculation.

def totals(scope: Scope) -> list[dict[str, Any]]:
    """One row per person, exactly as the workbench shows them."""
    return _totals(scope)


def declarations(scope: Scope) -> list[dict[str, Any]]:
    """One row per self-disclosed item, exactly as the workbench shows them."""
    return _disclosed_rows(scope)


def items(scope: Scope) -> list[dict[str, Any]]:
    """Every recorded item, superseded ones included — this is the register."""
    return list(_items_for(scope))


def superseded() -> dict[str, str]:
    """Records the firm concluded were wrong, and what replaced each."""
    return dict(_SUPERSEDED)


def address_of(user: str) -> str:
    """Where to write to this person, or "" when the directory cannot say."""
    return people.address_of(user)


def is_reviewer(user: str) -> bool:
    """Whether this person holds the compliance role."""
    return _is_reviewer(user)


def limit() -> int:
    return ANNUAL_LIMIT


class RewardLens(Feature):
    key = "rewardlens"
    #: A row is a person, not a purchase — the whole point of the screen.
    row_key = "employee_id"
    #: `status` already says all of these in words, `state` is its key form,
    #: `can` is this feature's own rules and `asked_at` is a float.
    private = frozenset({"over_limit", "referred", "exception", "state",
                         "can", "asked_at"})

    def figures(self, scope: Scope) -> list[Figure]:
        rows = _totals(scope)
        over = [r for r in rows if r["over_limit"]]

        if scope.sees == "self":
            # Written for the person whose year it is. "People tracked: 1"
            # and "declarations to review" are a reviewer's numbers; to
            # somebody reading their own record they are at best odd and at
            # worst suggest the screen is about somebody else.
            me = rows[0] if rows else None
            headroom = me["headroom"] if me else ANNUAL_LIMIT
            return [
                Figure("recorded", rupees(me["total"] if me else 0),
                       "counting towards your limit"),
                Figure("limit", rupees(ANNUAL_LIMIT), "the annual limit"),
                Figure("headroom", rupees(headroom) if headroom
                       else rupees(me["excess"]) if me else rupees(0),
                       "left before the limit" if headroom
                       else "over the limit",
                       "good" if headroom else "bad"),
                Figure("unacknowledged",
                       str(me["unacknowledged"] if me else 0),
                       "for you to acknowledge" if me and me["unacknowledged"]
                       else "nothing to acknowledge",
                       "warn" if me and me["unacknowledged"] else "good"),
                Figure("to_review", str(len(_awaiting(scope))),
                       "of yours waiting on compliance" if _awaiting(scope)
                       else "nothing waiting on compliance"),
            ]

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
            Figure("to_review", str(len(waiting := _awaiting(scope))),
                   "declarations to review" if waiting else "every declaration reviewed",
                   "warn" if waiting else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        rows = _totals(scope)
        over = [r for r in rows if r["over_limit"]]
        if scope.sees == "self":
            # A reviewer opens this on the queue, which is what needs them.
            # Somebody reading their own year has no queue — landing them on
            # "over the limit", empty, makes their own record look like it
            # is not there. Their year comes first; the rest still follow.
            return [
                Tab("all", "Your year", len(rows)),
                Tab("over", "Over the limit", len(over)),
                Tab("unacknowledged", "To acknowledge",
                    len([r for r in rows if r["unacknowledged"]])),
                Tab("disclosed", "What you declared", len(_awaiting(scope)),
                    key_field="id"),
            ]
        return [
            Tab("over", "Over the limit", len(over)),
            Tab("all", "Everyone", len(rows)),
            Tab("unacknowledged", "Awaiting acknowledgement",
                len([r for r in rows if r["unacknowledged"]])),
            # Items, not people — so it carries its own key. The count is
            # what is still waiting, because a tab badge showing everything
            # ever declared is a badge nobody looks at twice.
            Tab("disclosed", "Self-disclosed", len(_awaiting(scope)),
                key_field="id"),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        rows = _totals(scope)
        if tab == "over":
            return [r for r in rows if r["over_limit"]]
        if tab == "unacknowledged":
            return [r for r in rows if r["unacknowledged"]]
        if tab == "disclosed":
            return _disclosed_rows(scope)
        return rows

    def rules(self, scope: Scope) -> list[str]:
        if scope.sees == "none":
            # Said rather than shown a blank screen. Somebody the directory
            # cannot place is not a thief at the door; usually they are new.
            return ["not_yours_to_see"]
        if scope.period in _CLOSED:
            return ["period_closed"]
        rows = _totals(scope)
        said: list[str] = []
        if scope.sees == "self":
            # Each of the rules below is written for somebody looking at
            # other people. To a person reading their own year they are
            # either confusing or faintly accusatory, so the screen says
            # what it actually is instead.
            said.append("your_own_record")
            if any(r["over_limit"] for r in rows):
                said.append("you_are_over")
            return said

        if any(r["over_limit"] for r in rows):
            said.append("over_limit")
        mine = _employee(scope.user)
        # Only to somebody who could otherwise act on it: being in the list
        # is not news to the person whose list it is.
        if mine and _is_reviewer(scope.user) and any(
                r["employee_id"] == mine for r in rows):
            said.append("you_are_a_recipient")
        if _awaiting(scope):
            said.append("disclosures_waiting")
            if not _is_reviewer(scope.user):
                said.append("review_is_a_role")
        # Only to the people who could do something about it, and only while
        # it is true. A rule that is always on screen stops being read.
        if _is_reviewer(scope.user) and any(
                n.state in ("held", "failed")
                for r in _disclosed_rows(scope)
                for n in notices.for_item(r["id"])):
            said.append("notices_not_sent")
        return said

    #: The actions that decide a declared ITEM rather than a person's year.
    #: Separate because their targets are item ids, and mixing the two would
    #: mean one list of targets that is sometimes people and sometimes not.
    REVIEW_ACTIONS = ("accept_disclosure", "query_disclosure", "record_breach",
                      "accept_answer", "resend_notice")

    def act(self, scope: Scope, action: str, targets: list[str],
            note: str = "") -> Outcome:
        if scope.period in _CLOSED:
            return Outcome(
                ok=False,
                said=f"{scope.period} has been signed off — nothing in it can change.",
            )

        # Every action, not only the review ones. Declaring roles on all
        # eight and enforcing them on five left Audit — a persona that can
        # already see the whole register — able to refer somebody to
        # Finance. One gate, at the way in.
        if not _may(scope, action):
            return Outcome(
                ok=False,
                said="That decision belongs to another team. You can see "
                     "this and not decide it.",
            )

        if action in self.REVIEW_ACTIONS:
            return self._review(scope, action, targets, note)

        rows = {r["employee_id"]: r for r in _totals(scope)}
        chosen = [rows[t] for t in targets if t in rows]
        if not chosen:
            return Outcome(ok=False, said="Nobody by that id is in this view.")

        # The rule the whole feature exists for. Checked before anything else,
        # because a reviewer acting on their own year is not a smaller problem
        # when the action happens to be a reasonable one.
        mine = _employee(scope.user)
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

    # ── reviewing what was declared ─────────────────────────────────────────
    #
    # What a reviewer decides is whether the record stands, whether they need
    # to ask the declarer something, or whether what was given was not
    # permitted. What they cannot decide is whether it counts: the total is
    # the sum of what was received, and a figure that moved because somebody
    # disagreed with an item would be a figure worth arguing with. So none of
    # these touch the item, its value or the total — they record a decision
    # beside it, with a name against it.

    def _review(self, scope: Scope, action: str, targets: list[str],
                note: str) -> Outcome:
        rows = {r["id"]: r for r in _disclosed_rows(scope)}
        chosen = [rows[t] for t in targets if t in rows]
        if not chosen:
            return Outcome(ok=False, said="No declaration by that id is in this view.")
        if len(chosen) > 1:
            return Outcome(
                ok=False,
                said="Each declaration is decided on its own. Name the one "
                     "you mean.",
            )
        row = chosen[0]

        # The conflict rule, at the level a review happens. The person-level
        # one above would not catch this: reviewing your own declaration is
        # not an action on your row, it is an action on your item.
        if _employee(scope.user) == row["employee_id"]:
            return Outcome(
                ok=False,
                said="That is your own declaration. Declaring it was the right "
                     "thing; deciding it is somebody else's.",
            )

        # What is open depends on the state, and the row already says so.
        # Asking the same function the row was built from means a refusal
        # here and a missing button there can never disagree.
        if action not in row["can"]:
            # The row knows why, so the refusal says what the row says. For
            # a re-send that is the notice's own state — "no address on
            # file", "no mail server configured" — which is the sentence
            # somebody needs, rather than the state of the review.
            if action == "resend_notice":
                return Outcome(
                    ok=False,
                    said=f"Nothing can be sent again for {row['what']}: "
                         f"{row['notified']}.",
                )
            return Outcome(
                ok=False,
                said=f"{row['what']} is {_STATE_WORDS[row['state']]}"
                     f"{' by ' + row['reviewed_by'] if row['reviewed_by'] else ''}"
                     f" — that is not something you can do to it now.",
            )

        wants_reason = action in ("query_disclosure", "record_breach")
        if wants_reason and not note.strip():
            return Outcome(
                ok=False,
                said="Say why. A decision on somebody's record with no reason "
                     "against it is one nobody can review later.",
            )
        if len(note) > _TEXT_MAX:
            return Outcome(
                ok=False,
                said=f"Keep the reason under {_TEXT_MAX} characters.",
            )

        if action == "resend_notice":
            # No refusals of its own: `_allowed` already established that
            # something is waiting, that there is an address, and that there
            # is a mail server. Repeating those here would be three branches
            # nothing can reach — and unreachable code with reassuring
            # comments on it is worse than none, because it reads like a
            # guarantee somebody is relying on.
            notices.retry(row["id"])
            return Outcome(
                ok=True,
                said=f"Queued again for {row['declared_by']}.",
                touched=[row["id"]],
            )

        if action == "accept_answer":
            return self._settle(scope, row, note)

        state = {"accept_disclosure": "accepted",
                 "query_disclosure": "queried",
                 "record_breach": "breach"}[action]
        # Asking again clears the previous answer: the question has changed,
        # so the reply to the old one is no longer a reply to anything.
        if state == "queried":
            _ANSWERS.pop(row["id"], None)
        _REVIEWED[row["id"]] = {"state": state, "by": scope.user,
                                "note": note.strip(), "at": time.time()}

        if state == "queried":
            # Recorded after the query, so a mail server being down cannot
            # undo the question. The outcome says what became of the notice
            # rather than letting the reviewer assume it went.
            notice = self._ask_by_email(row, note.strip(), scope.user)
            return Outcome(
                ok=True,
                said=f"Asked {row['declared_by']} about {row['what']} — "
                     f"{notice.said}. {self._still_counts(row)} Nothing about "
                     f"the record changes while you wait for an answer.",
                touched=[row["id"]],
            )

        still = self._still_counts(row)
        said = {
            "accepted": f"{row['what']} accepted as declared. {still}",
            "breach": f"{row['what']} recorded as not permitted, with your "
                      f"reason against it. {still}",
        }[state]
        return Outcome(ok=True, said=said, touched=[row["id"]])

    @staticmethod
    def _still_counts(row: dict[str, Any]) -> str:
        """The sentence every review decision ends with, in one place."""
        if row["counts"] == "yes":
            return f"It still counts towards {row['declared_by']}'s total."
        return (f"It is recorded against {row['declared_by']} and is not "
                f"counted yet.")

    # ── self-disclosure ─────────────────────────────────────────────────────
    #
    # The one path where a person writes into this feature rather than
    # reading it. It exists because the gap the whole screen is about is not
    # only that three teams never compared notes — it is that a vendor hamper
    # handed over at a client site never entered any system at all.
    #
    # It records against the person who is signed in, and against nobody
    # else. There is no "declare on behalf of": a declaration is a statement
    # about what you were given, and one made by somebody else is a different
    # kind of record with different rules.

    FORM = "disclose"
    FIELDS = frozenset({"what", "kind", "value", "given", "source"})
    ANSWER_FORM = "answer_query"
    ANSWER_FIELDS = frozenset({"response", "value", "note"})

    def accepts(self, form_id: str) -> set[str]:
        if form_id == self.FORM:
            return set(self.FIELDS)
        if form_id == self.ANSWER_FORM:
            return set(self.ANSWER_FIELDS)
        return set()

    def _read(self, scope: Scope, form_id: str,
              values: dict[str, str]) -> tuple[dict[str, Any] | None, Outcome | None]:
        """The item these values describe, or why they do not describe one.

        One place, used by both preview and submit, so the preview cannot
        accept something the submit would refuse — the two disagreeing is how
        a person ends up seeing "you would be at ₹16,000" and then an error.
        """
        if form_id != self.FORM:
            return None, Outcome(ok=False,
                                 said=f"RewardLens has no form called {form_id!r}.")
        if scope.period in _CLOSED:
            return None, Outcome(
                ok=False,
                said=f"{scope.period} has been signed off. Anything received "
                     f"since belongs to the current year.",
            )

        mine = _employee(scope.user)
        if not mine:
            return None, Outcome(
                ok=False,
                said="Compass does not know which employee record is yours, so "
                     "it cannot record this against anybody. SCS can link your "
                     "account.",
            )

        what = values.get("what", "").strip()
        source = values.get("source", "").strip()
        kind = values.get("kind", "").strip().lower()
        if not what:
            return None, Outcome(ok=False, said="Say what it was.")
        if not source:
            return None, Outcome(
                ok=False,
                said="Say who gave it to you. Who it came from is the half "
                     "that matters once the total is over the limit.",
            )
        for text, field in ((what, "description"), (source, "giver")):
            if len(text) > _TEXT_MAX:
                return None, Outcome(
                    ok=False,
                    said=f"That {field} is longer than {_TEXT_MAX} characters.",
                )
        if kind not in _KINDS:
            return None, Outcome(
                ok=False,
                said=f"{kind or 'That'} is not something this records. "
                     f"Pick one of: {', '.join(sorted(_KINDS))}.",
            )

        value = _whole_rupees(values.get("value", ""))
        if value is None:
            return None, Outcome(
                ok=False,
                said="Give the value in whole rupees — 8000, or 8,000.",
            )
        if value <= 0:
            return None, Outcome(
                ok=False,
                said="A value of nothing is not a declaration. If you do not "
                     "know what it was worth, give your best estimate.",
            )
        if value > _MOST_PER_ITEM:
            return None, Outcome(
                ok=False,
                said=f"{rupees(value)} is past what this form takes. If that "
                     f"is right, SCS records it with you rather than you "
                     f"typing it.",
            )

        window = _year_window(scope.period)
        try:
            given = dt.date.fromisoformat(values.get("given", "").strip())
        except ValueError:
            return None, Outcome(ok=False, said="Give the date as YYYY-MM-DD.")
        if window and not (window[0] <= given <= window[1]):
            return None, Outcome(
                ok=False,
                said=f"{given.isoformat()} is outside {scope.period}, which "
                     f"runs {window[0].isoformat()} to {window[1].isoformat()}. "
                     f"It counts towards that year's total, not this one.",
            )

        # Already declared. Not a hard guarantee — two identical gifts on one
        # day are possible — so it refuses and says what it found rather than
        # discarding the second silently.
        twin = next((i for i in _items_for(scope)
                     if i["employee_id"] == mine and i["value"] == value
                     and i["given"] == given.isoformat()
                     and i["what"].strip().lower() == what.lower()), None)
        if twin is not None:
            return None, Outcome(
                ok=False,
                said=f"{what} for {rupees(value)} on {given.isoformat()} is "
                     f"already recorded against you. If this is a second one, "
                     f"say so in the description.",
            )

        row = next((r for r in _totals(scope) if r["employee_id"] == mine), None)
        return {
            "employee_id": mine,
            "recipient": row["recipient"] if row else scope.user,
            "kind": kind,
            "what": what,
            "value": value,
            "given": given.isoformat(),
            "source": source,
            "channel": "disclosed",
            # Declaring it is acknowledging it. Asking somebody to confirm
            # receipt of the thing they just told you about would be theatre.
            "acknowledged": True,
        }, None

    def _effect(self, scope: Scope, item: dict[str, Any]) -> str:
        """What this item does to the declarer's year, as a sentence.

        The number nobody could see before. It is computed from the items,
        not from a stored total, and the counted/not-counted split is said
        out loud rather than left for somebody to discover.
        """
        row = next((r for r in _totals(scope)
                    if r["employee_id"] == item["employee_id"]), None)
        before = row["total"] if row else 0
        if item["kind"] not in COUNTS_TOWARDS_LIMIT:
            return (f"Recorded, and not counted towards your limit — "
                    f"{item['kind']} is not aggregated yet. You stay at "
                    f"{rupees(before)} of {rupees(ANNUAL_LIMIT)}.")
        after = before + item["value"]
        if after > ANNUAL_LIMIT:
            crossing = (" This is the one that takes you over."
                        if before <= ANNUAL_LIMIT else "")
            return (f"That puts you at {rupees(after)} of "
                    f"{rupees(ANNUAL_LIMIT)} — {rupees(after - ANNUAL_LIMIT)} "
                    f"over.{crossing} Compliance sees it and Finance decides "
                    f"the tax treatment. Declaring it is not the problem; not "
                    f"declaring it would have been.")
        return (f"That puts you at {rupees(after)} of {rupees(ANNUAL_LIMIT)}, "
                f"{rupees(ANNUAL_LIMIT - after)} still inside the limit.")

    def _ask_by_email(self, row: dict[str, Any], question: str,
                      asked_by: str) -> notices.Notice:
        """Write down that the declarer is to be told what was asked.

        The words are built here and not by a model. This is a message about
        somebody's own record that goes out under the firm's name: what it
        says has to be the question the reviewer typed and the figures that
        are recorded, and nothing else.

        It tells them about THEIR item only. A notice that helpfully mentioned
        where they sit against the limit relative to anybody else would be a
        notice that leaks one employee's record into another's inbox.
        """
        return notices.record(
            to=_address_for_employee(row["employee_id"]),
            name=row["declared_by"],
            about=row["id"],
            subject=f"A question about what you declared: {row['what']}",
            body=(
                f"{row['declared_by']},\n\n"
                f"You declared this, and somebody in compliance has a "
                f"question about it.\n\n"
                f"  What:      {row['what']} ({row['kind']})\n"
                f"  Value:     {rupees(row['value'])}\n"
                f"  Received:  {row['given']}\n"
                f"  From:      {row['source']}\n\n"
                f"They asked:\n\n  {question}\n\n"
                f"You can answer in Compass, under SCS, RewardLens, the "
                f"Self-disclosed tab: say it is right as declared, give the "
                f"value it should have been, or say you should not have "
                f"declared it. It keeps counting towards your annual limit "
                f"until a reviewer settles it either way.\n\n"
                f"Asked by {asked_by}.\n"
            ),
        )

    def _settle(self, scope: Scope, row: dict[str, Any], note: str) -> Outcome:
        """Accept what the declarer said, and apply it if it changes anything.

        The one place a recorded figure stops counting, and it takes two
        people to get here: the person who received it proposed that the
        record was wrong, and somebody else agreed. Neither the original nor
        the correction is ever deleted — superseding is a thing that happens
        TO a record, which is why both are still on the tab afterwards.
        """
        answer = _ANSWERS.get(row["id"])
        if not answer:
            return Outcome(ok=False, said=f"{row['what']} has not been answered.")

        _REVIEWED[row["id"]] = {"state": "accepted", "by": scope.user,
                                "note": note.strip(), "at": time.time()}

        if answer["response"] == "stands":
            return Outcome(
                ok=True,
                said=f"{row['what']} accepted at {rupees(row['value'])}, as "
                     f"declared. {row['declared_by']}'s total is unchanged.",
                touched=[row["id"]],
            )

        original = next(i for i in _items_for(scope) if i["id"] == row["id"])
        before = next(r["total"] for r in _totals(scope)
                      if r["employee_id"] == row["employee_id"])

        if answer["response"] == "withdrawn":
            _SUPERSEDED[row["id"]] = ""
            after = next((r["total"] for r in _totals(scope)
                          if r["employee_id"] == row["employee_id"]), 0)
            return Outcome(
                ok=True,
                said=f"{row['what']} withdrawn and superseded. It stays on "
                     f"the record and out of the total: "
                     f"{row['declared_by']} goes from {rupees(before)} to "
                     f"{rupees(after)}.",
                touched=[row["id"]],
            )

        replacement = dict(original)
        replacement["id"] = f"C-{uuid.uuid4().hex[:6].upper()}"
        replacement["value"] = answer["value"]
        replacement["corrects"] = row["id"]
        _ITEMS.append(replacement)
        _SUPERSEDED[row["id"]] = replacement["id"]
        # The correction arrives already reviewed: it is what this decision
        # concluded, so leaving it "awaiting" would put it straight back on
        # the queue the decision just cleared.
        _REVIEWED[replacement["id"]] = {
            "state": "accepted", "by": scope.user, "at": time.time(),
            "note": note.strip() or f"Corrected from {rupees(row['value'])} "
                                    f"on {row['id']}.",
        }
        after = next(r["total"] for r in _totals(scope)
                     if r["employee_id"] == row["employee_id"])
        return Outcome(
            ok=True,
            said=f"{row['what']} corrected to {rupees(answer['value'])} from "
                 f"{rupees(row['value'])}. The original stays on the record, "
                 f"superseded. {row['declared_by']} goes from "
                 f"{rupees(before)} to {rupees(after)}.",
            touched=[row["id"], replacement["id"]],
        )

    # ── answering a query ───────────────────────────────────────────────────
    #
    # The other half of the conversation a review starts. A reviewer asks;
    # this is where the person who declared it replies, and it is a form
    # rather than an action because one of the three answers carries a
    # number with it.
    #
    # Nothing here changes a figure. The declarer PROPOSES — it stands, the
    # value was wrong, or it should not have been declared — and a reviewer
    # decides. A person who could correct their own record down to nothing
    # is a person for whom the limit is advisory.

    def _answer(self, scope: Scope, values: dict[str, str], about: str,
                commit: bool) -> Outcome:
        if scope.period in _CLOSED:
            return Outcome(
                ok=False,
                said=f"{scope.period} has been signed off — nothing in it "
                     f"can change.",
            )

        row = next((r for r in _disclosed_rows(scope) if r["id"] == about), None)
        if row is None:
            return Outcome(ok=False, said="No declaration by that id is in this view.")
        if _employee(scope.user) != row["employee_id"]:
            return Outcome(
                ok=False,
                said=f"This was asked of {row['declared_by']}, so it is "
                     f"theirs to answer.",
            )
        if row["state"] != "queried":
            return Outcome(
                ok=False,
                said=f"Nobody has asked you about {row['what']}. There is "
                     f"nothing to answer.",
            )

        response = values.get("response", "").strip().lower()
        if response not in ANSWERS:
            return Outcome(
                ok=False,
                said=f"Say which it is: {', '.join(ANSWERS)}.",
            )
        note = values.get("note", "").strip()
        if not note:
            return Outcome(
                ok=False,
                said="Say something to the person who asked. An answer with "
                     "no words in it does not settle anything.",
            )
        if len(note) > _TEXT_MAX:
            return Outcome(ok=False, said=f"Keep it under {_TEXT_MAX} characters.")

        corrected: int | None = None
        if response == "corrected":
            corrected = _whole_rupees(values.get("value", ""))
            if corrected is None:
                return Outcome(
                    ok=False,
                    said="Give the corrected value in whole rupees.",
                )
            if corrected <= 0:
                return Outcome(
                    ok=False,
                    said="A corrected value of nothing is a withdrawal. Say "
                         "that instead, so the record reads as what happened.",
                )
            if corrected > _MOST_PER_ITEM:
                return Outcome(ok=False, said=f"{rupees(corrected)} is past "
                                              f"what this form takes.")
            if corrected == row["value"]:
                return Outcome(
                    ok=False,
                    said=f"That is what is already recorded. If it is right, "
                         f"say so instead of correcting it to itself.",
                )
        elif values.get("value", "").strip():
            return Outcome(
                ok=False,
                said="A value only belongs with a correction. Clear it, or "
                     "say the value was wrong.",
            )

        would = {
            "stands": f"Tells the reviewer it is right as declared. "
                      f"{rupees(row['value'])} stays on your total either way "
                      f"while they decide.",
            "corrected": f"Proposes {rupees(corrected or 0)} in place of "
                         f"{rupees(row['value'])}. Nothing moves until a "
                         f"reviewer accepts it — and if they do, the original "
                         f"stays on the record, superseded.",
            "withdrawn": f"Proposes that this should not have been declared. "
                         f"{rupees(row['value'])} keeps counting until a "
                         f"reviewer accepts that, and the record stays either "
                         f"way.",
        }[response]
        if not commit:
            return Outcome(ok=True, said=would)

        _ANSWERS[row["id"]] = {"response": response, "value": corrected,
                               "note": note, "by": scope.user,
                               "at": time.time()}
        return Outcome(
            ok=True,
            said=f"Answered: {ANSWERS[response]}. It goes back to the "
                 f"reviewer, and your total is unchanged until they decide.",
            touched=[row["id"]],
        )

    def preview(self, scope: Scope, form_id: str, values: dict[str, str],
                about: str = "") -> Outcome:
        if form_id == self.ANSWER_FORM:
            return self._answer(scope, values, about, commit=False)
        item, refused = self._read(scope, form_id, values)
        if refused is not None:
            return refused
        assert item is not None
        return Outcome(ok=True, said=self._effect(scope, item))

    def submit(self, scope: Scope, form_id: str, values: dict[str, str],
               about: str = "") -> Outcome:
        if form_id == self.ANSWER_FORM:
            return self._answer(scope, values, about, commit=True)
        # Validated again from scratch rather than trusting what the preview
        # saw: the preview was a different request, and the year may have been
        # signed off between the two.
        item, refused = self._read(scope, form_id, values)
        if refused is not None:
            return refused
        assert item is not None

        effect = self._effect(scope, item)
        item["id"] = f"D-{uuid.uuid4().hex[:6].upper()}"
        _ITEMS.append(item)
        return Outcome(
            ok=True,
            said=f"Declared: {item['what']}, {rupees(item['value'])}, from "
                 f"{item['source']}. {effect}",
            touched=[item["employee_id"]],
        )


register(RewardLens())
