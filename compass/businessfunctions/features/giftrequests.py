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
import time
import uuid
from typing import Any

from compass.businessfunctions import ledger, notices, policy, people
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

#: Who buys. ONE team does, for the whole firm, wherever the recipient
#: sits — so this is a constant and not a choice on a form. It was a
#: choice, and the choice carried a per-team budget that could refuse a
#: gift the firm's own allowance allowed.
PROCUREMENT = "Procurement"

#: The most gifts one order may be for. Not a policy — a guard, so a typo
#: in a quantity field cannot write ten thousand register records.
MOST_PER_ORDER = 500

#: How long a reference from another system may be. A guard, not a format:
#: every firm numbers its own purchase orders differently and Compass has
#: no business having an opinion about theirs.
_REF_MAX = 64

#: How long a reason may be. Long enough for a sentence, short enough that
#: nobody pastes a thread into it.
_TEXT_MAX = 400

#: Who may take responsibility for a gift the rules refuse. Finance, as
#: the business's own flow has it — the point of an exception is that
#: somebody senior to the rule owns the decision, so it cannot be the desk
#: the rule is inconveniencing.
EXCEPTION_OWNER = "finance"

#: Who hands the gifts out, besides whoever raised the order. One persona,
#: named here rather than spelled into three functions.
DISTRIBUTOR = "operations"

#: How a gift reached somebody. Asked at distribution because "we have no
#: idea how it got there" is the honest answer otherwise, and an auditor
#: asking how two hundred hampers were handed out deserves better.
MODES = ("in person", "at an event", "by courier", "sent digitally")


def BIG_now() -> int:
    return policy.big()

#: Two requests waiting on a fresh deployment, written only into an empty
#: queue. The second is the whole argument of the requirement in one row: a
#: perfectly ordinary ₹6,000 watch for John, which nobody would question on
#: its own, and which takes him to ₹26,000 because of what three other
#: teams have already given him.
SEED: list[dict[str, Any]] = [
    {"id": "R-SEED01", "for": "E-1066", "kind": "gift",
     "what": "Diwali hamper", "value": 3000, "occasion": "festival",
     "source": PROCUREMENT, "by": "servicelead",
     "by_employee": "E-1033", "at": 0.0},
    {"id": "R-SEED02", "for": "E-1007", "kind": "gift",
     "what": "Long service watch", "value": 6000, "occasion": "long service",
     "source": PROCUREMENT, "by": "servicelead",
     "by_employee": "E-1033", "at": 0.0},
]


#: THE CHAIN. One stage, one persona, one sign-off — and a request is at
#: exactly one of them at a time.
#:
#: This is what makes the hand-off real rather than implied. Before it, a
#: request had a state and every action was gated on a role, which produced
#: a sequence by accident: anybody holding compliance could approve
#: something that had never been near Finance or Payroll. A stage says who
#: it is WITH right now, so signing off is not "I am allowed to do this" but
#: "it is mine, and when I am done it becomes theirs".
#:
#: `owner` is the persona whose queue it sits in. `verb` is what signing off
#: is called on their screen. `next` is where it goes.
#:
#: `back` says whether that desk may REFUSE. The five checks may; the last
#: two may not, because they are not judgements. By distribution the thing
#: has been approved, bought and paid for — the requester's job there is to
#: record what was actually handed over, not to reopen a decision five
#: other desks already took. The recipient cannot refuse either: an
#: employee declining their own gift is not a control, it is a way to make
#: a record disappear.
#:
#: `ask` is what that desk is asked to write down, and empty where there is
#: nothing to write: the question belongs to the STAGE, not to the action.
#: Hung off the action, the recipient confirming a hamper arrived was asked
#: "What did you check?" and could not proceed without answering it.
#:
#: `label` is a COLUMN HEADING — "With Finance" — and `step` and `holder` are
#: the same stage in a SENTENCE. Reusing the heading as prose produced
#: "Signed off at with finance", which is the kind of sentence that tells
#: somebody reading it that nobody read it.
STAGES: dict[str, dict[str, Any]] = {
    "service_line": {
        "back": True,
        "ask": "",
        "step": "the business need",
        "holder": "the Service Line Approver",
        "owner": "service_line",
        "label": "With the Service Line Approver",
        "verb": "Approve the business need",
        "asks": "business need, appropriateness, budget and the recipient list",
        "next": "finance_policy",
    },
    "finance_policy": {
        "back": True,
        "ask": "What did you check — thresholds, rules, any exception?",
        "step": "policy",
        "holder": "Finance",
        "owner": "finance",
        "label": "With Finance",
        "verb": "Confirm it meets policy",
        "asks": "policy thresholds, compliance rules and any exception",
        "next": "procurement",
    },
    # PAYROLL IS NOT A STEP IN THIS CHAIN. It was — third of seven,
    # confirming the tax treatment of one gift before it was bought — and
    # that is the wrong end of the process. A gift's tax consequence is a
    # property of the recipient's YEAR, which cannot be known at the point
    # of purchase and is not settled until the year is aggregated: the
    # third hamper is what takes somebody over, not the first. So payroll
    # reads the employee-level taxable position once a year, on Tax &
    # budget, and processes it in its own system. See `_payroll_rows`
    # there.
    # PROCUREMENT IS NOT APPROVED HERE, IT IS REFERENCED. Buying runs in
    # the firm's existing system — requisition, order, vendor, invoice,
    # payment — and that is out of this module's scope on purpose. What
    # Compass needs is the thread back to it: the PR, PO or invoice that
    # this gift was bought on, so an auditor reading a register record can
    # get to the purchase behind it.
    #
    # It was two sign-off steps, Procurement and Accounts Payable, each
    # "marking" something Compass had no part in doing. That claimed this
    # was the system of record for work that happens elsewhere, and it
    # recorded no reference at all — the one thing it could usefully have
    # held.
    "procurement": {
        "back": True,
        "ask": "",
        "step": "the purchase reference",
        "holder": "Procurement",
        "owner": "procurement",
        "label": "With Procurement",
        "verb": "Record what it was bought on",
        "asks": "the requisition, order or invoice it was bought on",
        "next": "distribution",
    },
    "distribution": {
        # Whoever handed it over records what was ACTUALLY given, because
        # they are the only ones who can say it differed from what was
        # asked for. OPERATIONS or the raiser: the SPOC who has the boxes
        # distributes a bulk order, the colleague who asked distributes a
        # one-off. This is the moment it reaches the register and starts
        # counting.
        "back": False,
        "ask": "What was actually given, and anything different from what "
               "was asked for?",
        "step": "what was actually given",
        "holder": "Operations or the requester, to hand it over",
        "owner": "operations",
        "label": "With Operations to distribute",
        "verb": "Record what was given",
        "asks": "the actual distribution, and any deviation from the request",
        "next": "acknowledgement",
    },
    "acknowledgement": {
        "back": False,
        "ask": "",
        "step": "receipt",
        "holder": "the recipient",
        "owner": "employee",
        "label": "With the recipient",
        "verb": "Confirm receipt",
        "asks": "the recipient's confirmation that they received it",
        "next": "closed",
    },
    "closed": {
        "back": False,
        "ask": "",
        "step": "",
        "holder": "",
        "owner": "",
        "label": "Closed",
        "verb": "",
        "asks": "",
        "next": "",
    },
}

#: Where one starts, and the two ways it can end early.
FIRST_STAGE = "service_line"
ENDED = ("declined", "cancelled", "closed")

#: What a sign-off is recorded as on the trail. ONE event kind, carrying the
#: stage it happened at, rather than eight kinds — so an auditor reads
#: "signed off · with Payroll" and a fold over the trail still has a closed
#: set of events it understands.
SIGN_OFF = "signed_off"


def stage_of(request: dict[str, Any]) -> str:
    """Which stage this request is at, folded from what happened to it.

    Not stored: the trail says who signed what off and when, and the stage
    is where that leaves it. A stored stage could disagree with the events
    that moved it, which for a compliance workflow is the one thing worth
    being strict about.
    """
    if ledger.happened(request["id"], "cancelled"):
        return "cancelled"
    if ledger.happened(request["id"], "declined"):
        return "declined"
    done = {e.get("stage") for e in ledger.events(request["id"])
            if e["kind"] == SIGN_OFF}
    stage = FIRST_STAGE
    while stage != "closed" and stage in done:
        # One order can reach several people. A sign-off at acknowledgement
        # is ONE of them confirming, so the order is not closed until there
        # is nobody left who has not — otherwise the first person to
        # confirm would close it on everybody else's behalf, which is the
        # firm asserting receipt again by a different route.
        if stage == "acknowledgement" and _unconfirmed(request["id"]):
            break
        stage = STAGES[stage]["next"]
    return stage


def allocated_to(request_id: str) -> list[dict[str, Any]]:
    """The register records this order became — one per recipient.

    An order is for a QUANTITY; who received them is answered at
    distribution, which is the whole point of the tracker. Read from the
    register rather than from a field on the request, because the register
    is the thing the totals are built from and a second list would be a
    second answer.
    """
    return [i for i in ledger.items() if i.get("from_request") == request_id]


def _unconfirmed(request_id: str) -> list[dict[str, Any]]:
    """The allocated records nobody has confirmed receiving yet."""
    return [i for i in allocated_to(request_id) if not i["acknowledged"]]


#: Stages the chain no longer has, and the words they used while it did.
#: THE TRAIL OUTLIVES THE CHAIN. Events are append-only and carry the
#: stage they were taken at, so every stage ever shipped is still being
#: read back years later — and removing one from `STAGES` made every
#: screen that reads a stored stage raise `KeyError` on the records that
#: had already happened. The audit trail went down first, which is exactly
#: the screen that must not, because its whole job is that the past stays
#: legible.
#:
#: Kept here for the same reason `ledger.READS_AS` keeps retired event
#: kinds: a record written under a rule that has since changed still has
#: to read in the words it was written in.
RETIRED_STAGES: dict[str, dict[str, str]] = {
    "payroll_tax": {"label": "With Payroll", "step": "the tax treatment"},
    "accounts_payable": {"label": "With Accounts Payable",
                         "step": "payment"},
}


def stage_label(stage: str) -> str:
    """The column heading: where it is. Never raises on an old stage."""
    if stage == "declined":
        return "Declined"
    if stage == "cancelled":
        return "Withdrawn"
    if stage in STAGES:
        return STAGES[stage]["label"]
    if stage in RETIRED_STAGES:
        return RETIRED_STAGES[stage]["label"]
    # Something no version of this chain ever had. Tidied rather than
    # raised: a trail that cannot be read is worse than one with an odd
    # word in it, and this is the screen people go to when they need the
    # record most.
    return stage.replace("_", " ").capitalize() if stage else "—"


def step_name(stage: str) -> str:
    """The same stage in a sentence: what was being decided."""
    known = STAGES.get(stage) or RETIRED_STAGES.get(stage) or {}
    return known.get("step", "") or "this step"


def _state(request: dict[str, Any]) -> str:
    """The stage, under the name the rest of this module already used."""
    return stage_of(request)


def committed(source: str, period: str = "") -> int:
    """What this team has committed IN THAT YEAR. Public, so the budget
    screen reads the same figure the sign-off check uses rather than summing
    requests again."""
    return _spent(source, period)


def _spent(source: str, period: str = "") -> int:
    """What this team has committed in a year: approved requests and given.

    Committed rather than spent, because a budget that only counted what had
    already been handed over would let a team approve its way past the line
    and find out at distribution.

    PER YEAR, because a budget is. Summed across all time, a team's budget
    never reset: every approval since the register began counted against
    this April's allowance, and a team would be told it was out of money
    for things it paid for two years ago.
    """
    total = 0
    for request in ledger.requests():
        if request["source"] != source:
            continue
        if not rewardlens.within(period, request.get("at")):
            continue
        stage = stage_of(request)
        # Committed from the first sign-off: a team that has approved its
        # way past the line should find out at the next request, not at
        # distribution.
        if stage not in ("service_line", "declined", "cancelled"):
            total += request["value"]
    return total


# ──────────────────────────────────────────────────────────────────────────
# the three checks
# ──────────────────────────────────────────────────────────────────────────

def _may_read_totals(scope: Scope, employee_id: str) -> bool:
    """Whether this reader may be told another person's running total.

    The check itself always runs at firm breadth — it has to, or it would
    clear a gift merely because the person asking cannot see the
    recipient's year. What this decides is whether the ANSWER may carry the
    figures, which is a different question and was not being asked at all.

    Anybody may now raise a request, including an employee thanking a
    colleague. The sentence that helps an approver decide — "takes them to
    ₹2,17,000 of ₹15,000" — hands that colleague's whole year to somebody
    whose own register view is one row. The warning is theirs to see; the
    arithmetic behind it is not.
    """
    return scope.sees in ("firm", "team") or scope.mine(employee_id)


def _tax_check(scope: Scope, employee_id: str, value: int,
               kind: str) -> dict[str, Any]:
    if not employee_id:
        # A bulk order with nobody named yet. Said plainly rather than
        # answered: the limit is a property of a person's year, and there
        # is no person to ask about until the gifts are allocated.
        return {"ok": True,
                "said": "checked against each recipient's year when they "
                        "are named at distribution"}
    """What approving this would do to the recipient's year.

    The point of the whole system, asked one gift earlier than before. The
    arithmetic is RewardLens's — this does not add anything up itself, so
    the figure the approver sees is the figure the register will show.
    """
    if kind not in rewardlens.counts_towards_limit():
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
    figures = _may_read_totals(scope, employee_id)
    if after > limit:
        return {
            "ok": False,
            "said": (f"takes them to {rewardlens.rupees(after)} of "
                     f"{rewardlens.rupees(limit)} — "
                     f"{rewardlens.rupees(after - limit)} over") if figures else
                    "this would take them over the annual limit",
        }
    return {"ok": True,
            "said": (f"{rewardlens.rupees(after)} of {rewardlens.rupees(limit)}, "
                     f"{rewardlens.rupees(limit - after)} still inside") if figures
                    else "within the annual limit"}


def _policy_check(request: dict[str, Any]) -> dict[str, Any]:
    """Whether this is permitted at all, before any arithmetic.

    Three rules, written out rather than configured, because a policy
    engine with one policy in it is a worse way of saying the same thing.
    They are the ones the requirement's own examples imply.
    """
    if request["kind"] == "hospitality":
        return {"ok": False,
                "said": "hospitality is not requested through this yet"}
    if request["occasion"] == "client courtesy" and request["value"] > BIG_now():
        return {"ok": False,
                "said": f"client courtesy above {rewardlens.rupees(BIG_now())} "
                        f"needs a conversation, not a form"}
    if request["for"] and request["for"] == request["by_employee"]:
        return {"ok": False, "said": "you cannot request a gift for yourself"}
    if request["value"] > BIG_now():
        return {"ok": True,
                "said": f"over {rewardlens.rupees(BIG_now())}, so compliance "
                        f"approves rather than a line manager"}
    return {"ok": True, "said": "within policy"}


def intended_list(request_id: str) -> list[str]:
    """Who the order was ORIGINALLY asked for, as named when it was raised.

    Kept because a list that was revised and a list that was right first
    time are not the same fact, and the difference is the one an auditor
    asks about: two hundred hampers were approved for these people and
    went to those people. The original is on the trail from the moment the
    request is raised and nothing ever edits it.
    """
    event = ledger.latest(request_id, "requested")
    if event is None:
        return []
    return [n for n in str(event.get("intended", "")).split("|") if n]


def list_revisions(request_id: str) -> list[dict[str, Any]]:
    """Every change made to the recipient list, in order, with who and why.

    CONTROLLED ADD AND DELETE, which is what makes this a list somebody
    can maintain rather than a list somebody can overwrite. A name taken
    off is not gone — it is a line on the trail saying who took it off and
    what they said about it.
    """
    return [e for e in ledger.events(request_id)
            if e["kind"] in ("listed", "unlisted")]


def planned_list(request_id: str) -> list[str]:
    """The list as it stands now: the original, plus and minus the changes.

    A fold, like the stage. Stored, it could disagree with the changes
    that produced it, and for the one record that says who was supposed to
    receive something that is the wrong thing to be relaxed about.
    """
    names = list(intended_list(request_id))
    for event in list_revisions(request_id):
        who = str(event.get("who", ""))
        if event["kind"] == "listed" and who not in names:
            names.append(who)
        elif event["kind"] == "unlisted" and who in names:
            names.remove(who)
    return names


def exception_on(request_id: str) -> dict[str, Any] | None:
    """The exception granted on this order, if Finance has granted one.

    THE WAY PAST A BLOCK, and the only one. A request that policy refuses
    is not a request nobody can ever make — it is one somebody senior has
    to take responsibility for, in writing, before it goes any further.
    Without this the only answers were "send it back" and "withdraw it",
    so the real-world case the business has every month — a gift that
    matters and a rule that did not foresee it — had no route through
    Compass at all and would have been done beside it.

    A property of the request, not a stage: the order does not go
    sideways to Finance and come back, it carries a decision that unblocks
    it and stays on the trail for ever.
    """
    return ledger.latest(request_id, "excepted")


def checks(scope: Scope, request: dict[str, Any]) -> dict[str, Any]:
    """Both of them, in the words this reader may have them in.

    There were THREE. The third asked whether the team paying still had
    budget, and it stopped being a question the day the firm settled on one
    allowance per employee and one team that buys: the money is the
    recipient's allowance, which is what the tax check already reads. A
    second limit on top of it could refuse a gift the real rule allowed,
    and nobody could say which of the two had done it.

    The VERDICTS are the same for everybody — blocked is blocked whoever is
    looking. What changes is whether the sentence carries the figures
    behind it, because a colleague's year is not an ordinary employee's to
    read just because they asked for a hamper.
    """
    policy = _policy_check(request)
    tax = _tax_check(scope, request["for"], request["value"], request["kind"])
    granted = exception_on(request["id"])
    if granted is not None and not policy["ok"]:
        policy = {"ok": True,
                  "said": f"{policy['said']} — exception granted by "
                          f"{granted.get('by', 'Finance')}"}
    return {"policy": policy, "tax": tax,
            "exception": granted is not None,
            "blocked": not policy["ok"],
            # A tax consequence is not a reason to refuse — it is a reason
            # for somebody senior to decide knowingly, which is the whole
            # argument of the requirement.
            "warns": not tax["ok"]}


# ──────────────────────────────────────────────────────────────────────────
# rows
# ──────────────────────────────────────────────────────────────────────────

def _plain(name: str) -> str:
    """A name reduced to what somebody typing it would get right.

    Case, punctuation and runs of spaces all go. The directory holds
    "Manish K." and a person types "Manish K", which matched nothing and
    refused the whole form — a full stop is not a different person.
    """
    return " ".join("".join(c for c in name if c.isalnum() or c.isspace()).split()).lower()


def _employee_named(name: str) -> str:
    """The employee id for a name as somebody typed it, or "".

    The directory first, then the register — somebody may have declared a
    gift for a person the directory does not carry, and they are still a
    person a later gift can be recorded against.
    """
    wanted = _plain(name)
    if not wanted:
        return ""
    for person in people.DIRECTORY.values():
        if person.employee_id and _plain(person.name) == wanted:
            return person.employee_id
    for item in ledger.items():
        if _plain(item["recipient"]) == wanted:
            return item["employee_id"]
    return ""


def employee_named(name: str) -> str:
    """The employee id for a name as somebody typed it, or "".

    Public because an upload has to know, before it writes anything,
    whether the names in somebody's spreadsheet are people Compass can
    record a gift against.
    """
    return _employee_named(name)


def _known_names() -> list[str]:
    """Everybody a gift can be recorded against, as they are spelled."""
    seen: dict[str, str] = {}
    for person in people.DIRECTORY.values():
        if person.employee_id:
            seen.setdefault(_plain(person.name), person.name)
    for item in ledger.items():
        seen.setdefault(_plain(item["recipient"]), item["recipient"])
    return sorted(seen.values())


def _name_of(employee_id: str) -> str:
    for person in people.DIRECTORY.values():
        if person.employee_id == employee_id:
            return person.name
    for item in ledger.items():
        if item["employee_id"] == employee_id:
            return item["recipient"]
    return employee_id


def _with(request: dict[str, Any], stage: str) -> str:
    """Who it is with, as somebody wanting to chase it would say.

    A PERSONA for the six steps that are a control — any of Finance will
    do. A NAMED PERSON for the two that are routed by identity, because
    "with Business Requester" is not who to chase when the person holding
    it is an employee who raised one gift and holds no persona at all.
    """
    if stage == "distribution":
        who = people.find(request["by"])
        raiser = who.name if who else request["by"]
        return f"{people.LABELS[DISTRIBUTOR]} or {raiser}"
    if stage == "acknowledgement":
        waiting = sorted({i["recipient"] for i in _unconfirmed(request["id"])})
        if waiting:
            return waiting[0] if len(waiting) == 1 else (
                f"{waiting[0]} and {len(waiting) - 1} other"
                f"{'s' if len(waiting) > 2 else ''}")
        return _name_of(request["for"])
    return people.LABELS.get(STAGES.get(stage, {}).get("owner", ""), "—")


def reference_for(request: dict[str, Any]) -> str:
    """The reference COMPASS mints for an approved order.

    THE TRACKER ISSUES IT; the purchasing system quotes it. That is the
    whole integration, and it is deliberately not one: when an order is
    approved, Compass generates this and emails it, somebody carries it to
    the PR/PO tool by hand, and the requisition they raise there carries
    the reference. Nothing is read back.

    It was the other way round for a day — Procurement typed their PO
    number into Compass — which needs the purchasing team to do data entry
    in a second system for no benefit to them, and leaves the order stuck
    until they do.

    Derived from the order id and the year rather than counted, so it
    needs no sequence to keep and cannot be minted twice for one order.
    """
    raised = dt.date.fromtimestamp(request.get("at", 0) or 0)
    # April to March, like every other year in this module.
    start = raised.year if raised.month >= 4 else raised.year - 1
    return f"GIFT-{start % 100:02d}{(start + 1) % 100:02d}-{request['id'][2:]}"


def reference_issued(request_id: str) -> dict[str, Any] | None:
    """When the reference was issued and what was said, or None."""
    return ledger.latest(request_id, "referenced")


def _ref_said(request_id: str) -> str:
    """The reference as a row shows it."""
    event = reference_issued(request_id)
    if event is None:
        return "not issued yet"
    return str(event.get("reference", "")) or "issued"


def _allocation_said(request: dict[str, Any]) -> str:
    """How far an order has got towards knowing whose gifts these are."""
    wanted = request.get("quantity", 1)
    done = len(allocated_to(request["id"]))
    if wanted == 1 and request["for"]:
        return "named when asked for"
    if not done:
        return f"none of {wanted} named"
    return f"{done} of {wanted} named"


def _record(request: dict[str, Any], employee_id: str, name: str,
            given: str, mode: str, actual: int | None = None) -> dict[str, Any]:
    """Put one gift on the register, against one person.

    The ONE place an order becomes a record. Called once for a request
    raised in somebody's name and once per recipient for a bulk order, so
    the two cannot end up writing different shapes of row.
    """
    item = {
        "id": f"G-{uuid.uuid4().hex[:6].upper()}",
        "employee_id": employee_id,
        "recipient": name,
        "kind": request["kind"],
        "what": request["what"],
        # What it ACTUALLY cost. The register is the record of what was
        # received, so the estimate stays on the request and the real
        # figure lands here — a limit measured against an estimate is a
        # limit measured against a guess.
        "value": request["value"] if actual is None else actual,
        "estimated": request["value"],
        "given": given,
        "source": request["source"],
        "channel": "procured",
        "acknowledged": False,
        "from_request": request["id"],
        # How it reached them. Empty for an order raised in one person's
        # name and handed over without an allocation step.
        "mode": mode,
    }
    ledger.add_item(item)
    return item


def _rows(scope: Scope, seen_by=None) -> list[dict[str, Any]]:
    """Every request this reader may see, under the rule they are reading by.

    TWO RULES, TWO SCREENS. Gift requests is where somebody asks for
    something and watches their own; the Tracker is where the desks sign
    things off. They are different questions about the same records, so the
    rule for "is this mine to see" is passed in rather than fixed here —
    and a desk's queue cannot leak onto the requester's screen because the
    requester's screen never asks for it.
    """
    rule = seen_by or _concerns
    out: list[dict[str, Any]] = []
    for request in ledger.requests():
        stage = stage_of(request)
        # Filed under the year it was raised — but LIVE WORK IS NEVER
        # HIDDEN BY A SELECTOR. A request raised in March and still with
        # Payroll in April is somebody's job today, and a year filter that
        # made it vanish would strand it where nobody is looking.
        if stage in ENDED and not rewardlens.within(scope.period,
                                                    request.get("at")):
            continue
        verdict = checks(scope, request)
        trail = ledger.events(request["id"])
        signed = [e for e in trail if e["kind"] == SIGN_OFF]
        last = signed[-1] if signed else None
        ended = next((e for e in trail
                      if e["kind"] in ("declined", "cancelled")), None)
        ack = next((e for e in trail if e["kind"] == SIGN_OFF
                    and e.get("stage") == "acknowledgement"), None)
        out.append({
            "id": request["id"],
            "employee_id": request["for"],
            "by_employee": request.get("by_employee", ""),
            "recipient": _name_of(request["for"]),
            "what": request["what"],
            "kind": request["kind"],
            "value": request["value"],
            "occasion": request["occasion"],
            "purpose": request.get("purpose", "—"),
            "source": request["source"],
            "requested_by": request["by"],
            "stage": stage,
            "status": stage_label(stage),
            # Who it is WITH — the question everybody asks of a workflow,
            # and the one a status alone never answers.
            "with": _with(request, stage),
            "policy": verdict["policy"]["said"],
            "tax": verdict["tax"]["said"],
            "blocked": verdict["blocked"],
            "warns": verdict["warns"],
            "steps_done": len(signed),
            "last_signed_by": last["by"] if last else "",
            "last_signed_at": _day(last["at"]) if last else "—",
            "why": ((ended.get("note") if ended else None)
                    or (last.get("note") if last else None) or "—"),
            "acknowledged_on": _day(ack["at"]) if ack else "—",
            # How many were asked for, and how many have a person against
            # them. "3 of 200 allocated" is the sentence a bulk order is
            # read for, and no status word carries it.
            "quantity": request.get("quantity", 1),
            "allocated": len(allocated_to(request["id"])),
            # In words, for the column: "2 of 3 named" is what a bulk
            # order is read for, and neither number says it alone.
            "allocation": _allocation_said(request),
            # The list as it stands, the list as approved, and whether the
            # two differ — three facts, because "it was revised" is the
            # one somebody asks about and neither list says it alone.
            "for_list": ", ".join(_name_of(e)
                                  for e in planned_list(request["id"])) or "—",
            "listed_as": ", ".join(_name_of(e)
                                   for e in intended_list(request["id"]))
                         or "nobody named up front",
            "list_changes": len(list_revisions(request["id"])),
            # The thread back to the system that bought it.
            "bought_on": _ref_said(request["id"]),
            # Who actually got one. On the row because "is this mine" can
            # no longer be answered from the request alone: a bulk order
            # names nobody when it is raised.
            "allocated_ids": [i["employee_id"]
                              for i in allocated_to(request["id"])],
            # What to ask this desk before it signs off, per action. The
            # screen asks it; the server requires an answer to the same
            # question, so the two cannot disagree about what is needed.
            "ask": {"sign_off": STAGES.get(stage, {}).get("ask", "")},
            "can": _allowed(scope, request, stage, verdict),
        })
    # Whatever is with somebody comes before whatever is finished, and the
    # oldest first inside that — a queue, not a list.
    out.sort(key=lambda r: (r["stage"] in ENDED, r["steps_done"], -r["value"]))
    # Breadth, applied here rather than through `Scope.narrow`, because
    # one of these rows can be about a LIST of people. Narrow compares a
    # named field to the ids somebody may see, and an order for two
    # hundred hampers has no single field that says whose it is — the
    # answer is the allocation. Keyed on the request alone, a recipient
    # could not see, or confirm, the gift they had just been handed.
    out = [r for r in out if _within_breadth(scope, r)]
    return [r for r in out if rule(scope, r)]


def _within_breadth(scope: Scope, row: dict[str, Any]) -> bool:
    """How much of this screen is theirs, for a row about several people."""
    if scope.sees == "firm":
        return True
    if scope.sees == "none":
        return False
    allowed = {scope.employee} if scope.employee else set()
    if scope.sees == "team":
        allowed |= set(scope.team)
    if not allowed:
        return False
    if str(row.get("employee_id", "")) in allowed:
        return True
    if str(row.get("by_employee", "")) in allowed:
        return True
    return bool(allowed & set(row.get("allocated_ids", ())))


#: Who reads this screen as a POSITION rather than as a queue. Everybody
#: else is a desk in the chain and sees their own work; these two personas
#: exist to see all of it, which is what the dashboard and the trail are.
SEES_EVERYTHING = frozenset({"compliance", "leadership"})


def tracked(user: str, period: str = "") -> list[dict[str, Any]]:
    """Every order this person is involved in, and how far each has got.

    The TRACKER's source. Built here rather than in the tracker because
    the rule about which orders are somebody's business lives here, and a
    second screen deriving it again would be a second place for it to be
    wrong. The tracker asks; this answers under the chain's own rules.

    What it adds over the chain's own rows is PROGRESS: which of the seven
    steps are done, what each one was, who took it, and how long it has
    been sitting where it is. The chain is a queue — what needs me now.
    This is the journey — where has everything got to.
    """
    from compass.businessfunctions import people as _people
    from compass.businessfunctions import registry as _registry

    feature = _registry.feature_of("scs", "requests")[1]
    if feature is None:
        return []
    scope = _people.scope_for(user, feature, period=period)

    out = []
    for row in _rows(scope):
        request = next((r for r in ledger.requests() if r["id"] == row["id"]),
                       None)
        if request is None:
            continue
        trail = ledger.events(row["id"])
        done = [e for e in trail if e["kind"] == SIGN_OFF]
        stage = row["stage"]
        steps = [s for s in STAGES if s != "closed"]
        at = next((e["at"] for e in reversed(trail)), request.get("at", 0))
        out.append({
            "id": row["id"],
            "what": row["what"],
            "recipient": row["recipient"],
            "employee_id": row["employee_id"],
            "value": row["value"],
            "source": row["source"],
            "raised_by": row["requested_by"],
            "raised_on": _day(request.get("at", 0)),
            "stage": stage,
            "status": row["status"],
            # "whom to assign": who the next move belongs to.
            "with": row["with"],
            "progress": (f"{len(done)} of {len(steps)}"
                         if stage not in ENDED else row["status"].lower()),
            "done": len(done),
            "of": len(steps),
            "last_step": (stage_label(done[-1].get("stage", "")).lower()
                          if done and done[-1].get("stage") in STAGES else "—"),
            "last_by": done[-1]["by"] if done else "—",
            "waiting_days": max(0, int((time.time() - at) // 86_400))
                            if stage not in ENDED else 0,
            "acknowledged_on": row["acknowledged_on"],
            # The chain's own answer about what this desk may do, carried
            # through rather than worked out again. The Tracker is where
            # the signing off happens now, so the buttons it draws have to
            # be the ones `act` will accept — and there is exactly one
            # function that knows which those are.
            "can": row["can"],
            "ask": row["ask"],
            "blocked": row["blocked"],
            "policy": row["policy"],
            "tax": row["tax"],
            # What was said at the last decision. A desk that sent an order
            # back wrote a reason, and the screen where orders are now read
            # is the one where that reason has to be legible.
            "why": row["why"],
            "kind": row["kind"],
            "occasion": row["occasion"],
            "quantity": row["quantity"],
            "allocation": row["allocation"],
            "bought_on": row["bought_on"],
            "purpose": row["purpose"],
            "for_list": row["for_list"],
            "listed_as": row["listed_as"],
            "allocated_ids": row["allocated_ids"],
        })
    # Whatever is moving comes first, least far along first: a tracker is
    # read to find what has not arrived, not to admire what has.
    out.sort(key=lambda r: (r["stage"] in ENDED, r["done"], -r["waiting_days"]))
    return out


def _is_yours(scope: Scope, row: dict[str, Any]) -> bool:
    """Whether this request is the reader's OWN — the Gift requests rule.

    Two ways, and no others. You raised it, or it was raised for you: a
    service line lead asking on behalf of somebody puts it on that
    person's screen as well as their own, because a request about you that
    you cannot see is a thing being done to you.

    Nothing about roles, stages or desks. A Finance reviewer opening Gift
    requests sees what they themselves asked for and what was asked for
    them, exactly like everybody else — the approving they do is the
    Tracker's screen, and mixing the two put the firm's whole queue on the
    page where an employee asks for a hamper.

    No exemption for Compliance or Leadership either. Seeing everything is
    what the Tracker, the Dashboard and the trail are for; this screen is
    personal, and a personal screen with an exception in it is not one.
    """
    return (row["requested_by"] == scope.user
            or scope.mine(row["employee_id"])
            # ...or they were handed one out of it. A bulk order names
            # nobody when it is raised, and the people it reached have as
            # much right to see it as somebody named up front.
            or (bool(scope.employee)
                and scope.employee in row.get("allocated_ids", [])))


def _concerns(scope: Scope, row: dict[str, Any]) -> bool:
    """Whether this request is any of this person's business.

    Breadth says how much of the firm somebody may see; this says which of
    it is theirs. Every desk in the chain had firm breadth, so Finance
    opened the screen onto every request in the company including the ones
    sitting untouched at the service line — a queue nobody could read.

    A desk sees a request when:

      * it is AT a stage they own — their queue;
      * they RAISED it — you do not lose sight of your own request because
        it moved to somebody else's desk;
      * it is FOR them — the recipient's own gift;
      * they have ALREADY ACTED on it — so what a desk approved last month
        stays on its Closed tab, in its consolidated report and in the
        order report, and a sign-off is not a thing that vanishes.

    Leadership and Compliance see all of it: reading the whole position is
    the job, not an accident of breadth.
    """
    if SEES_EVERYTHING & scope.roles:
        return True
    owner = STAGES.get(row["stage"], {}).get("owner")
    if owner and owner in scope.roles:
        return True
    if row["requested_by"] == scope.user:
        return True
    if scope.mine(row["employee_id"]):
        return True
    if scope.employee and scope.employee in row.get("allocated_ids", []):
        return True
    # An order waiting on an exception is the exception owner's business
    # wherever it is standing. Without this, Finance was asked to take
    # responsibility for an order it could not see — the request sat at
    # the service line, which is not Finance's stage, so the question
    # arrived with no way to answer it.
    if (EXCEPTION_OWNER in scope.roles
            and ledger.happened(row["id"], "exception_asked")
            and exception_on(row["id"]) is None
            and not ledger.happened(row["id"], "exception_refused")):
        return True
    return any(e.get("by") == scope.user for e in ledger.events(row["id"]))


def _my_step(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows whose CURRENT STEP is this person's to take.

    Not "rows with a button on them". Anybody who raised a request can
    withdraw it while it is still being checked, and counting that as a
    queue told an employee "something is waiting on you" about a request
    sitting with the Service Line Approver — which is the one sentence
    this screen most needs to be true.
    """
    return [r for r in rows if "sign_off" in r["can"]]


def _closed(scope: Scope, rows: list[dict[str, Any]]
            ) -> list[dict[str, Any]]:
    """The closed orders a report would contain: ended, and in the window.

    One function, so the tab's count, the figure and the rows it leads to
    cannot disagree. They did: the tab said "Closed 1" above "Nothing in
    this view", which teaches somebody that the counts are decorative.
    """
    return _in_range(scope, [r for r in rows if r["stage"] == "closed"])


def _in_range(scope: Scope, rows: list[dict[str, Any]]
              ) -> list[dict[str, Any]]:
    """The closed orders inside the chosen window, by ACKNOWLEDGEMENT date.

    The date a consolidated report should be cut on is the day the person
    confirmed receiving it — not when it was asked for, approved or bought.
    Those can be months apart, and a report cut on the wrong one omits
    exactly the orders that crossed the boundary.
    """
    if not scope.since and not scope.until:
        return rows
    out = []
    for row in rows:
        when = row["acknowledged_on"]
        if when == "—":
            continue
        if scope.since and when < scope.since:
            continue
        if scope.until and when > scope.until:
            continue
        out.append(row)
    return out


def _day(when: float) -> str:
    return (dt.datetime.fromtimestamp(when).strftime("%Y-%m-%d")
            if when else "—")


def mine_to_sign(scope: Scope, request: dict[str, Any], stage: str) -> bool:
    """Whether this request is sitting in THIS person's queue.

    The six stages that are a CONTROL are routed by PERSONA: the stage has
    to be owned by one the person holds, it must not be a request they
    raised, and it must not be a gift to them.

    The two that are not controls are routed by IDENTITY instead, and the
    persona gate does not apply to them at all — being one named person is
    already narrower than holding a role:

      * DISTRIBUTION is whoever RAISED it. They handed it over, so they are
        the only one who can say what was actually given and where it
        differed. Anybody may raise a request — a Business Requester whose
        job it is, or an employee who wants to thank somebody — so gating
        this on the `requester` persona stranded every request an ordinary
        employee raised: five desks signed it off and then nobody on earth
        could record the distribution.
      * ACKNOWLEDGEMENT is whoever it is FOR. Confirming you received
        something is not deciding your own case.
    """
    if stage == "distribution":
        # OPERATIONS HANDS THINGS OUT, and so may whoever raised it. Both,
        # because both happen: a bulk order is distributed by the SPOC who
        # has the boxes, and a one-off gift by the colleague who asked for
        # it. Routed by identity OR by that one persona rather than by the
        # persona alone — gating it on a role stranded every request an
        # ordinary employee raised, which is the bug the comment below was
        # written about.
        return request["by"] == scope.user or DISTRIBUTOR in scope.roles
    if stage == "acknowledgement":
        # Whoever was ALLOCATED one, not whoever the request named. A bulk
        # order names nobody when it is raised; the people who received it
        # are known only once it has been handed out, and each of them
        # confirms their own.
        return bool(scope.employee) and scope.employee in {
            i["employee_id"] for i in _unconfirmed(request["id"])}

    owner = STAGES.get(stage, {}).get("owner")
    if not owner or owner not in scope.roles:
        return False
    if request["by"] == scope.user:
        return False
    return not scope.mine(request["for"])


def _allowed(scope: Scope, request: dict[str, Any], stage: str,
             verdict: dict[str, Any]) -> list[str]:
    """What this person may do to this request, right now.

    Derived from the STAGE rather than from a list of role checks, so
    adding a step to the chain adds it to everybody's queue without
    touching this function.
    """
    if scope.period in rewardlens.closed_periods():
        return []
    if stage in ENDED:
        return []

    can: list[str] = []

    # The exception branch. A blocked order is not a dead end: whoever is
    # holding it may ask Finance to take responsibility for it, and
    # Finance may grant that or refuse it. Offered only while it IS
    # blocked and nobody has decided yet, because an exception to a rule
    # that is not biting is not an exception.
    if (verdict["blocked"] and exception_on(request["id"]) is None
            and not ledger.happened(request["id"], "exception_refused")):
        if mine_to_sign(scope, request, stage) or request["by"] == scope.user:
            can.append("request_exception")
        if EXCEPTION_OWNER in scope.roles and request["by"] != scope.user:
            if ledger.happened(request["id"], "exception_asked"):
                can.extend(["grant_exception", "refuse_exception"])

    if mine_to_sign(scope, request, stage):
        # Policy stops it at the gate; a tax consequence does not — it is
        # shown, and whoever signs off does so knowing.
        if not verdict["blocked"]:
            can.append("sign_off")
        # Only the desks whose step is a CHECK. The last two record what
        # happened; they do not decide it. See `back` on STAGES.
        if STAGES[stage]["back"]:
            can.append("decline")
    # The person who raised it can withdraw it until it has been bought.
    if request["by"] == scope.user and stage in (
            "service_line", "finance_policy"):
        can.append("cancel_request")
    return can


class GiftRequests(Feature):
    key = "gift_requests"
    row_key = "id"
    private = frozenset({"by_employee", "stage", "blocked", "warns", "can",
                         "steps_done", "ask", "allocated_ids",
                         "list_changes"})

    def step_label(self, step: str) -> str:
        """A stage on the trail, in the words the screen uses for it."""
        # Retired stages too: a workbook of an order from last year still
        # has to name the desks that signed it off.
        if step in STAGES or step in RETIRED_STAGES:
            return stage_label(step)
        return super().step_label(step)

    REQUEST_FORM = "request_gift"
    FIELDS = frozenset({"recipient", "recipient_list", "kind", "what",
                        "value", "occasion", "quantity", "purpose"})
    ALLOCATE_FORM = "allocate"
    ALLOCATE_FIELDS = frozenset({"recipients", "given", "mode", "actual"})
    LIST_FORM = "revise_list"
    LIST_FIELDS = frozenset({"add", "remove", "why"})

    def figures(self, scope: Scope) -> list[Figure]:
        rows = _rows(scope, _is_yours)
        mine = _my_step(rows)
        moving = [r for r in rows if r["stage"] not in ENDED]
        blocked = [r for r in moving if r["blocked"]]
        crossing = [r for r in moving if r["warns"]]
        return [
            # First, because it is the only figure that is about the reader.
            Figure("mine", str(len(mine)),
                   "waiting on you" if mine else "nothing waiting on you",
                   "warn" if mine else "good"),
            Figure("moving", str(len(moving)),
                   "in flight" if moving else "nothing in flight"),
            Figure("blocked", str(len(blocked)),
                   "cannot proceed as asked" if blocked else "none blocked",
                   "bad" if blocked else "good"),
            Figure("crossing", str(len(crossing)),
                   "would take somebody over the limit" if crossing
                   else "none would cross the limit",
                   "bad" if crossing else "good"),
            # Inside the chosen window, because that is what "reportable"
            # means on a screen with one: a count of every order ever
            # closed, sitting above a table showing the three in March, is
            # a figure somebody will quote.
            Figure("closed", str(len(_closed(scope, rows))),
                   "closed and reportable"
                   + (" in this window" if scope.since or scope.until else "")),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        rows = _rows(scope, _is_yours)
        return [
            # Only two of the seven steps are ever the reader's own here —
            # handing over what they raised, and confirming what they were
            # given. The other five are desks, and desks work in the
            # Tracker.
            Tab("mine", "Waiting on you", len(_my_step(rows))),
            Tab("moving", "In flight",
                len([r for r in rows if r["stage"] not in ENDED])),
            Tab("closed", "Closed", len(_closed(scope, rows))),
            Tab("all", "Everything", len(rows)),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        rows = _rows(scope, _is_yours)
        if tab == "mine":
            return _my_step(rows)
        if tab == "moving":
            return [r for r in rows if r["stage"] not in ENDED]
        if tab == "closed":
            return _closed(scope, rows)
        return rows

    def rules(self, scope: Scope) -> list[str]:
        if scope.sees == "none":
            return ["not_yours_to_see"]
        if scope.period in rewardlens.closed_periods():
            return ["period_closed"]
        rows = _rows(scope, _is_yours)
        moving = [r for r in rows if r["stage"] not in ENDED]
        said: list[str] = []
        # The reader's own queue first: a screen that leads with the firm's
        # problems when three things are waiting on you has its priorities
        # the wrong way round.
        if _my_step(rows):
            said.append("waiting_on_you")
        if any(r["warns"] for r in moving):
            said.append("would_cross_the_limit")
        if any(r["blocked"] for r in moving):
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
        request = next(r for r in ledger.requests() if r["id"] == row["id"])
        stage = row["stage"]

        # The exception branch answers its own refusals, because "that is
        # not something you can do to it now" is the wrong sentence for
        # every one of them: the useful answer is that nothing is blocking
        # it, or that Finance has already decided, or that the decision is
        # Finance's — and a reader who is told only the stage goes looking
        # in the wrong place.
        if action in ("request_exception", "grant_exception",
                      "refuse_exception"):
            return self._exception(scope, action, row, request, stage, note)

        if action not in row["can"]:
            # Say whose it is, not just that it is not theirs — "with
            # Procurement" tells somebody where to chase; "you cannot do
            # that" tells them nothing.
            return Outcome(
                ok=False,
                said=f"{row['what']} is {row['status'].lower()} — that is "
                     f"not something you can do to it now.",
            )

        if action == "cancel_request":
            ledger.add_event(subject=row["id"], kind="cancelled",
                             by=scope.user, note=note.strip(), stage=stage)
            return Outcome(ok=True, said="Withdrawn. Nothing was bought.",
                           touched=[row["id"]])

        if action == "decline":
            if not note.strip():
                return Outcome(ok=False, said="Say why. A request declined "
                                              "with no reason is one nobody "
                                              "can learn from.")
            ledger.add_event(subject=row["id"], kind="declined", by=scope.user,
                             note=note.strip(), stage=stage)
            return Outcome(
                ok=True,
                said=f"Sent back at {step_name(stage)}, with your reason on "
                     f"the record. Nothing was bought and nothing reaches "
                     f"anybody's total.",
                touched=[row["id"]])

        if action != "sign_off":
            return Outcome(ok=False, said=f"No action called {action!r}.")

        # ── signing off: the hand-off ────────────────────────────────────
        if STAGES[stage]["ask"] and not note.strip():
            # The stages whose whole job is a judgement or a record of what
            # happened. One of those signed off with nothing written against
            # it is a name on a form.
            return Outcome(
                ok=False,
                said=f"{STAGES[stage]['ask']} {STAGES[stage]['label']} is "
                     f"about {STAGES[stage]['asks']}, and a sign-off with "
                     f"no record of it is one nobody can audit.")

        said_extra = ""
        if stage == "distribution":
            # Where a proposal becomes a record. Nothing before this is on
            # anybody's total, and nothing after it can be taken off.
            #
            # ONE RECIPIENT IS A SPECIAL CASE OF MANY. An order raised for
            # a named person is allocated to them here, exactly as it
            # always was; anything else has to have been allocated first,
            # because the quantity is the only thing the order knows and a
            # number is not a list of people.
            given = allocated_to(row["id"])
            if not given:
                if not request["for"]:
                    return Outcome(
                        ok=False,
                        said=f"Name who received them first — "
                             f"{request['quantity']} to allocate, and the "
                             f"register cannot hold a gift without a person "
                             f"to hold it against.")
                given = [_record(request, request["for"], row["recipient"],
                                 dt.date.today().isoformat(), "")]

            short = request["quantity"] - len(given)
            if short > 0:
                return Outcome(
                    ok=False,
                    said=f"{len(given)} of {request['quantity']} allocated. "
                         f"Name who received the other {short} before "
                         f"signing this off.")
            names = ", ".join(sorted({i["recipient"] for i in given}))
            said_extra = (f" {len(given)} record"
                          f"{'s are' if len(given) > 1 else ' is'} on the "
                          f"register, against {names}.")
            if len(given) == 1:
                # One recipient, so the sentence that matters is still what
                # this did to THEIR year. With twenty it would be twenty
                # sentences, and the register is where you read those.
                after = next((r["total"] for r in rewardlens.totals(
                    Scope(user=scope.user, period=scope.period, sees="firm"))
                    if r["employee_id"] == given[0]["employee_id"]),
                    request["value"])
                said_extra = (f" It is on the register against "
                              f"{given[0]['recipient']}, whose year is now "
                              f"{rewardlens.rupees(after)}.")

        if stage == "finance_policy":
            # APPROVED, so the reference is issued and emailed. This is the
            # hand-off to purchasing and the whole of it: somebody takes
            # the reference from that email to the PR/PO tool, and the
            # requisition they raise there carries it. Nothing is read
            # back, which is why there is no integration to build.
            said_extra = " " + self._issue_reference(scope, request)

        if stage == "acknowledgement":
            # THEIRS, not the order's. One order can reach twenty people,
            # and each of them confirms what they themselves were given.
            mine = [i for i in _unconfirmed(row["id"])
                    if i["employee_id"] == scope.employee]
            for item in mine:
                item["acknowledged"] = True
                ledger.touch_item(item["id"])
                ledger.add_event(subject=item["id"], kind="acknowledged",
                                 by=scope.user)
            still = len(_unconfirmed(row["id"]))
            said_extra = (" The record now says you confirmed receiving it, "
                          "rather than the firm saying you did.")
            if still:
                said_extra += (f" {still} other"
                               f"{'s have' if still > 1 else ' has'} not "
                               f"confirmed yet, so the order stays open.")

        ledger.add_event(subject=row["id"], kind=SIGN_OFF, by=scope.user,
                         stage=stage, note=note.strip(), value=row["value"])

        nxt = STAGES[stage]["next"]
        if nxt == "closed":
            ledger.add_event(subject=row["id"], kind="closed", by=scope.user,
                             stage=stage)
            return Outcome(
                ok=True,
                said=f"{row['what']} is closed \u2014 the last step in the "
                     f"chain.{said_extra} The order report, with every step "
                     f"and who took it, is on the row.",
                touched=[row["id"]])

        crossing = (" It takes them over the annual limit, which you have "
                    "signed off knowing." if row["warns"] else "")
        return Outcome(
            ok=True,
            said=f"Signed off: {step_name(stage)}.{crossing}{said_extra} "
                 f"It is now with {STAGES[nxt]['holder']}.",
            touched=[row["id"]])

    def _issue_reference(self, scope: Scope, request: dict[str, Any]) -> str:
        """Mint the order's reference and email it. Says what happened."""
        if reference_issued(request["id"]) is not None:
            return ""
        ref = reference_for(request)
        raiser = people.find(request["by"])
        notice = notices.record(
            to=raiser.email if raiser else "",
            name=raiser.name if raiser else request["by"],
            subject=f"{ref} — approved, ready to order",
            body=(f"{request['quantity']} \u00d7 {request['what']} at "
                  f"{rewardlens.rupees(request['value'])} each has been "
                  f"approved.\n\nQuote {ref} on the requisition when you "
                  f"raise it in the purchasing system, so what is bought "
                  f"can be matched back to this order.\n\nNothing is "
                  f"ordered by this email. Compass does not raise, order "
                  f"or pay for anything."),
            about=request["id"])
        ledger.add_event(subject=request["id"], kind="referenced",
                         by=scope.user, stage="finance_policy",
                         reference=ref, notice=notice.id)
        if notice.state == "held":
            return (f"Reference {ref} issued — not emailed: "
                    f"{notice.detail}. Quote it on the requisition.")
        return (f"Reference {ref} issued and emailed to "
                f"{notice.name}. Quote it on the requisition.")

    def _exception(self, scope: Scope, action: str, row: dict[str, Any],
                   request: dict[str, Any], stage: str, note: str) -> Outcome:
        """Ask for, grant or refuse an exception to a rule that is blocking.

        Every branch here writes a reason, including the asking. An
        exception with nothing written against it is the rule quietly not
        applying, which is worse than the rule not existing — somebody
        reading the trail a year later has to be able to see who decided
        and what they thought.
        """
        if action not in row["can"]:
            if exception_on(row["id"]) is not None:
                return Outcome(ok=False, said="An exception has already been "
                                              "granted on this one.")
            if ledger.happened(row["id"], "exception_refused"):
                return Outcome(ok=False, said="Finance has already refused an "
                                              "exception on this one.")
            if not row["blocked"]:
                return Outcome(
                    ok=False,
                    said="Nothing is blocking this, so there is no exception "
                         "to make. A rule that is not biting does not need "
                         "one.")
            return Outcome(ok=False, said="That decision is Finance's.")

        if not note.strip():
            return Outcome(
                ok=False,
                said="Say why. An exception with no reason against it is the "
                     "rule quietly not applying.")

        if action == "request_exception":
            ledger.add_event(subject=row["id"], kind="exception_asked",
                             by=scope.user, note=note.strip(), stage=stage)
            return Outcome(
                ok=True,
                said=f"Asked Finance for an exception: {row['policy']} "
                     f"It stays where it is until they decide.",
                touched=[row["id"]])

        if action == "refuse_exception":
            ledger.add_event(subject=row["id"], kind="exception_refused",
                             by=scope.user, note=note.strip(), stage=stage)
            return Outcome(
                ok=True,
                said="Exception refused, with your reason on the record. The "
                     "order stays blocked and can be sent back.",
                touched=[row["id"]])

        # Granted. The order is not changed — the rule still says what it
        # said, and what is recorded is that somebody took responsibility
        # for going ahead anyway.
        ledger.add_event(subject=row["id"], kind="excepted", by=scope.user,
                         note=note.strip(), stage=stage,
                         value=request["value"])
        return Outcome(
            ok=True,
            said=f"Exception granted, in your name. {row['policy']} — and it "
                 f"can now be signed off. The rule stays on the record "
                 f"alongside your decision; neither replaces the other.",
            touched=[row["id"]])

    # ── asking ──────────────────────────────────────────────────────────

    def accepts(self, form_id: str) -> set[str]:
        if form_id == self.REQUEST_FORM:
            return set(self.FIELDS)
        if form_id == self.ALLOCATE_FORM:
            return set(self.ALLOCATE_FIELDS)
        if form_id == self.LIST_FORM:
            return set(self.LIST_FIELDS)
        return set()

    def _read_list(self, scope: Scope, values: dict[str, str],
                   about: str) -> tuple[dict[str, Any] | None,
                                        Outcome | None]:
        """The change to the recipient list, or why it cannot be made.

        Controlled: who may change it, when it may be changed, and never
        past the quantity that was approved. A list somebody can rewrite
        silently is not a control, and a list nobody can correct is a
        control people work around.
        """
        request = next((r for r in ledger.requests() if r["id"] == about), None)
        if request is None:
            return None, Outcome(ok=False, said="No order by that id.")
        stage = stage_of(request)
        if stage in ENDED:
            return None, Outcome(
                ok=False,
                said=f"{request['id']} is {stage_label(stage).lower()}. The "
                     f"list of an order that has ended does not change.")
        if allocated_to(request["id"]):
            return None, Outcome(
                ok=False,
                said="Some of these are already on the register, and the "
                     "register is write-once. A record that was wrong is "
                     "superseded there, not unlisted here.")
        if (request["by"] != scope.user and DISTRIBUTOR not in scope.roles
                and "service_line" not in scope.roles):
            return None, Outcome(
                ok=False,
                said="Whoever raised it, their approver, or Operations keep "
                     "the list straight.")

        now = planned_list(request["id"])
        adding, removing = [], []
        for name in [n.strip() for n in values.get("add", "").split(",")
                     if n.strip()]:
            known = _employee_named(name)
            if not known:
                return None, Outcome(
                    ok=False,
                    said=f"Compass does not know anybody called {name!r}. "
                         f"It knows: {', '.join(_known_names())}.")
            if known in now:
                return None, Outcome(
                    ok=False,
                    said=f"{_name_of(known)} is already on the list.")
            if known not in adding:
                adding.append(known)
        for name in [n.strip() for n in values.get("remove", "").split(",")
                     if n.strip()]:
            known = _employee_named(name)
            if known not in now:
                return None, Outcome(
                    ok=False,
                    said=f"{name} is not on the list, so there is nothing to "
                         f"take off.")
            if known not in removing:
                removing.append(known)
        if not adding and not removing:
            return None, Outcome(ok=False, said="Name somebody to add or to "
                                                "take off.")
        after = len(now) + len(adding) - len(removing)
        if after > request.get("quantity", 1):
            return None, Outcome(
                ok=False,
                said=f"That would be {after} people for "
                     f"{request.get('quantity', 1)} gifts. The quantity is "
                     f"what was approved.")
        if not values.get("why", "").strip():
            return None, Outcome(
                ok=False,
                said="Say why it changed. A list that was revised and a list "
                     "that was right first time are different facts, and the "
                     "difference is the one somebody will ask about.")
        return {"request": request, "add": adding, "remove": removing,
                "why": values["why"].strip(), "after": after}, None

    def _read_list(self, scope: Scope, values: dict[str, str],
                   about: str) -> tuple[dict[str, Any] | None,
                                        Outcome | None]:
        """The change to the recipient list, or why it cannot be made.

        Controlled: who may change it, when it may be changed, and never
        past the quantity that was approved. A list somebody can rewrite
        silently is not a control, and a list nobody can correct is a
        control people work around.
        """
        request = next((r for r in ledger.requests() if r["id"] == about), None)
        if request is None:
            return None, Outcome(ok=False, said="No order by that id.")
        stage = stage_of(request)
        if stage in ENDED:
            return None, Outcome(
                ok=False,
                said=f"{request['id']} is {stage_label(stage).lower()}. The "
                     f"list of an order that has ended does not change.")
        if allocated_to(request["id"]):
            return None, Outcome(
                ok=False,
                said="Some of these are already on the register, and the "
                     "register is write-once. A record that was wrong is "
                     "superseded there, not unlisted here.")
        if (request["by"] != scope.user and DISTRIBUTOR not in scope.roles
                and "service_line" not in scope.roles):
            return None, Outcome(
                ok=False,
                said="Whoever raised it, their approver, or Operations keep "
                     "the list straight.")

        now = planned_list(request["id"])
        adding, removing = [], []
        for name in [n.strip() for n in values.get("add", "").split(",")
                     if n.strip()]:
            known = _employee_named(name)
            if not known:
                return None, Outcome(
                    ok=False,
                    said=f"Compass does not know anybody called {name!r}. "
                         f"It knows: {', '.join(_known_names())}.")
            if known in now:
                return None, Outcome(
                    ok=False,
                    said=f"{_name_of(known)} is already on the list.")
            if known not in adding:
                adding.append(known)
        for name in [n.strip() for n in values.get("remove", "").split(",")
                     if n.strip()]:
            known = _employee_named(name)
            if known not in now:
                return None, Outcome(
                    ok=False,
                    said=f"{name} is not on the list, so there is nothing to "
                         f"take off.")
            if known not in removing:
                removing.append(known)
        if not adding and not removing:
            return None, Outcome(ok=False, said="Name somebody to add or to "
                                                "take off.")
        after = len(now) + len(adding) - len(removing)
        if after > request.get("quantity", 1):
            return None, Outcome(
                ok=False,
                said=f"That would be {after} people for "
                     f"{request.get('quantity', 1)} gifts. The quantity is "
                     f"what was approved.")
        if not values.get("why", "").strip():
            return None, Outcome(
                ok=False,
                said="Say why it changed. A list that was revised and a list "
                     "that was right first time are different facts, and the "
                     "difference is the one somebody will ask about.")
        return {"request": request, "add": adding, "remove": removing,
                "why": values["why"].strip(), "after": after}, None

    def _read_allocation(self, scope: Scope, values: dict[str, str],
                         about: str) -> tuple[dict[str, Any] | None,
                                              Outcome | None]:
        """The allocation these values describe, or why they do not.

        This is "Provide recipient list / allocation details": the step
        where a quantity becomes people. Everything it refuses, it refuses
        BEFORE anything is written, because a half-written allocation on a
        write-once register is not something anybody can tidy up.
        """
        request = next((r for r in ledger.requests() if r["id"] == about), None)
        if request is None:
            return None, Outcome(ok=False, said="No order by that id.")
        if stage_of(request) != "distribution":
            return None, Outcome(
                ok=False,
                said=f"{request['id']} is {stage_label(stage_of(request)).lower()}. "
                     f"Gifts are allocated when it reaches distribution.")
        if request["by"] != scope.user and DISTRIBUTOR not in scope.roles:
            return None, Outcome(
                ok=False,
                said="Whoever raised it hands it over, or Operations — they "
                     "are the ones who can say who actually received it.")

        already = allocated_to(request["id"])
        room = request.get("quantity", 1) - len(already)
        if room <= 0:
            return None, Outcome(
                ok=False,
                said=f"All {request.get('quantity', 1)} are allocated.")

        names = [n.strip() for n in values.get("recipients", "").split(",")
                 if n.strip()]
        if not names:
            return None, Outcome(ok=False, said="Name who received them, "
                                                "separated by commas.")
        if len(names) > room:
            return None, Outcome(
                ok=False,
                said=f"{len(names)} names for {room} remaining. Allocate no "
                     f"more than were asked for.")

        held = {i["employee_id"] for i in already}
        # What was approved. Empty means nobody was named up front, and
        # then distribution is where the names come from.
        planned = planned_list(request["id"])
        people_named: list[tuple[str, str]] = []
        for name in names:
            employee = _employee_named(name)
            if not employee:
                return None, Outcome(
                    ok=False,
                    said=f"Compass does not know anybody called {name!r}. "
                         f"It knows: {', '.join(_known_names())}.")
            if planned and employee not in planned:
                return None, Outcome(
                    ok=False,
                    said=f"{name} is not on this order's recipient list. "
                         f"Put them on it first — the list is what was "
                         f"approved, and allocating round it is how a gift "
                         f"ends up on somebody nobody signed off for.")
            if employee in held or employee in {e for e, _ in people_named}:
                return None, Outcome(
                    ok=False,
                    said=f"{name} is already down for one from this order. "
                         f"One record per person per order, so a second "
                         f"gift is a second order.")
            people_named.append((employee, _name_of(employee)))

        given = values.get("given", "").strip() or dt.date.today().isoformat()
        try:
            dt.date.fromisoformat(given)
        except ValueError:
            return None, Outcome(ok=False, said="Give the date they were "
                                                "handed over as YYYY-MM-DD.")
        mode = values.get("mode", "").strip().lower()
        if mode not in MODES:
            return None, Outcome(ok=False, said=f"How did it reach them? One "
                                                f"of: {', '.join(MODES)}.")

        # WHAT IT ACTUALLY COST, which is not what was estimated. The
        # request carries an estimate made before anything was bought; the
        # register has to carry what was really given, because that is the
        # figure somebody's annual limit is measured against and the one a
        # tax position rests on. Empty means it came in at the estimate.
        actual = request["value"]
        if values.get("actual", "").strip():
            actual = rewardlens.whole_rupees(values["actual"])
            if actual is None or actual <= 0:
                return None, Outcome(ok=False, said="Give what one of them "
                                                    "actually cost in whole "
                                                    "rupees, or leave it "
                                                    "empty for the estimate.")
            if actual > rewardlens.most_per_item():
                return None, Outcome(ok=False, said="That is past what this "
                                                    "form takes.")
        return {"request": request, "people": people_named, "given": given,
                "mode": mode, "room": room, "actual": actual}, None

    def _read(self, scope: Scope, values: dict[str, str]
              ) -> tuple[dict[str, Any] | None, Outcome | None]:
        """The request these values describe, or why they do not describe one."""
        if scope.period in rewardlens.closed_periods():
            return None, Outcome(ok=False, said=f"{scope.period} has been "
                                                f"signed off.")
        # REQUIRED, because Procurement orders this many from the vendor
        # in their own system. A quantity nobody gave is a number somebody
        # has to go back and ask for, which is the phone call this screen
        # exists to stop.
        quantity = rewardlens.whole_rupees(values.get("quantity", ""))
        if quantity is None or quantity < 1 or quantity > MOST_PER_ORDER:
            return None, Outcome(ok=False, said=f"How many? A whole number "
                                                f"from 1 to {MOST_PER_ORDER} "
                                                f"— Procurement orders that "
                                                f"many.")

        # The recipient is OPTIONAL, which is the whole change: the firm
        # buys two hundred hampers before it knows whose they are, and a
        # form that insisted on a name was a form nobody could use for the
        # purchase this exists to control. Named here when it is known;
        # named at distribution when it is not.
        # A LIST, not a name. The business captures who it is for up
        # front when it knows — "known recipients (if any)" — and a form
        # that took one name could not carry a festival order for four
        # people somebody already had in mind.
        #
        # TWO WAYS IN, one meaning. A few names are typed with commas; two
        # hundred are pasted a line at a time out of a spreadsheet, which
        # is the Service Line Approver's box. Both land here, because a
        # recipient named one way and a recipient named the other are the
        # same fact and a handler that kept them apart would be the place
        # they drifted.
        typed = values.get("recipient", "")
        # The paste box is the Service Line Approver's. Checked here and
        # not only in the route that drew the form, because a field
        # somebody was not offered is a field they must not be able to
        # send anyway — the screen is where a box is hidden, the handler
        # is where it is refused.
        pasted = ""
        if "service_line" in scope.roles:
            pasted = values.get("recipient_list", "").replace("\n", ",")
        elif values.get("recipient_list", "").strip():
            return None, Outcome(
                ok=False,
                said="Pasting a list of recipients is the Service Line "
                     "Approver's. Name them with commas, or ask them to "
                     "raise it.")
        wanted, seen = [], set()
        for name in (typed + "," + pasted).split(","):
            name = name.strip()
            if name and _plain(name) not in seen:
                seen.add(_plain(name))
                wanted.append(name)
        recipient = wanted[0] if wanted else ""
        employee = _employee_named(recipient) if recipient else ""
        if recipient and not employee:
            # Say who it DOES know. A refusal that only says no turns a
            # typed name into a guessing game, and the thing it most often
            # catches is punctuation: "Manish K" against "Manish K.".
            known = _known_names()
            near = [n for n in known if _plain(n).startswith(_plain(recipient)[:3])
                    and _plain(recipient)] if recipient else []
            return None, Outcome(
                ok=False,
                said=(f"Compass does not know anybody called {recipient!r}. "
                      + (f"Did you mean {' or '.join(repr(n) for n in near[:3])}? "
                         if near else "")
                      + f"A gift has to be recorded against somebody, and "
                        f"Compass knows: {', '.join(known)}.")
                if recipient else
                "Say who it is for, or leave it empty and name them at "
                "distribution.")

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
        # WHY the firm is giving it, in the asker's own words. The occasion
        # is a category and the policy reads it; this is the sentence an
        # approver actually decides on, and a business reason nobody wrote
        # down is a decision nobody can review.
        purpose = values.get("purpose", "").strip()[:_TEXT_MAX]
        if not purpose:
            return None, Outcome(
                ok=False,
                said="Say what it is for. The occasion is the category; this "
                     "is the business reason, and an approver has nothing to "
                     "decide on without it.")
        # Nobody is asked who pays. One team buys for the whole firm, so
        # the answer was the same every time and the question was a field
        # people got wrong.
        intended: list[str] = []
        for name in wanted:
            known = _employee_named(name)
            if not known:
                return None, Outcome(
                    ok=False,
                    said=f"Compass does not know anybody called {name!r}. "
                         f"It knows: {', '.join(_known_names())}.")
            if known not in intended:
                intended.append(known)
        if len(intended) > quantity:
            return None, Outcome(
                ok=False,
                said=f"{len(intended)} names for {quantity}. Ask for as many "
                     f"as you mean to give.")

        value = rewardlens.whole_rupees(values.get("value", ""))
        if value is None or value <= 0:
            return None, Outcome(ok=False, said="Give the value of ONE of "
                                                "them in whole rupees — "
                                                "8000, or 8,000.")
        if value > rewardlens.most_per_item():
            return None, Outcome(ok=False, said="That is past what this form "
                                                "takes.")
        return {
            "id": f"R-{uuid.uuid4().hex[:6].upper()}",
            "for": employee, "kind": kind, "what": what, "value": value,
            # Per gift, not for the lot. A limit is a thing one person has,
            # so the number every check reads has to be what one person
            # receives; the order's cost is this times the quantity.
            "quantity": quantity,
            # Everybody named up front, resolved to employee ids. The
            # FIRST of them is also `for`, because one named recipient is
            # the ordinary case and every check that reads a single person
            # reads that one.
            "intended": intended,
            "occasion": occasion, "purpose": purpose,
            "source": PROCUREMENT,
            "by": scope.user, "by_employee": scope.employee,
            "at": dt.datetime.now().timestamp(),
        }, None

    def preview(self, scope: Scope, form_id: str, values: dict[str, str],
                about: str = "") -> Outcome:
        if form_id == self.LIST_FORM:
            read, refused = self._read_list(scope, values, about)
            if refused is not None:
                return refused
            assert read is not None
            parts = []
            if read["add"]:
                parts.append("on: " + ", ".join(_name_of(e)
                                                for e in read["add"]))
            if read["remove"]:
                parts.append("off: " + ", ".join(_name_of(e)
                                                 for e in read["remove"]))
            return Outcome(
                ok=True,
                said=" · ".join(parts)
                     + f" — {read['after']} of "
                       f"{read['request'].get('quantity', 1)} named after "
                       f"this. The original list stays on the trail.")
        if form_id == self.ALLOCATE_FORM:
            read, refused = self._read_allocation(scope, values, about)
            if refused is not None:
                return refused
            assert read is not None
            crossing = []
            for employee, name in read["people"]:
                verdict = _tax_check(scope, employee, read["request"]["value"],
                                     read["request"]["kind"])
                if not verdict["ok"]:
                    crossing.append(f"{name} ({verdict['said']})")
            left = read["room"] - len(read["people"])
            differs = read["actual"] != read["request"]["value"]
            said = (f"{len(read['people'])} to be recorded at "
                    f"{rewardlens.rupees(read['actual'])} each, "
                    f"{read['mode']}, on {read['given']}."
                    + (f" The estimate was "
                       f"{rewardlens.rupees(read['request']['value'])}."
                       if differs else "")
                    + (f" {left} still to allocate after this." if left else
                       " That allocates the order in full."))
            if crossing:
                said += (" Over the annual limit: " + "; ".join(crossing)
                         + ". It still records — the limit is reported, not "
                           "enforced at the door.")
            return Outcome(ok=True, said=said)
        if form_id != self.REQUEST_FORM:
            return Outcome(ok=False, said=f"No form called {form_id!r}.")
        request, refused = self._read(scope, values)
        if refused is not None:
            return refused
        assert request is not None
        verdict = checks(scope, request)
        parts = [f"Policy: {verdict['policy']['said']}.",
                 f"Their year: {verdict['tax']['said']}."]
        if verdict["blocked"]:
            parts.append("As asked, this cannot be approved.")
        elif verdict["warns"]:
            parts.append("It can be approved, and whoever does will see that "
                         "it crosses the limit.")
        return Outcome(ok=True, said=" ".join(parts))

    def submit(self, scope: Scope, form_id: str, values: dict[str, str],
               about: str = "") -> Outcome:
        if form_id == self.LIST_FORM:
            read, refused = self._read_list(scope, values, about)
            if refused is not None:
                return refused
            assert read is not None
            for employee in read["add"]:
                ledger.add_event(subject=read["request"]["id"], kind="listed",
                                 by=scope.user, who=employee,
                                 note=read["why"])
            for employee in read["remove"]:
                ledger.add_event(subject=read["request"]["id"],
                                 kind="unlisted", by=scope.user,
                                 who=employee, note=read["why"])
            return Outcome(
                ok=True,
                said=f"The list is now "
                     f"{', '.join(_name_of(e) for e in planned_list(read['request']['id'])) or 'empty'}"
                     f" — {read['after']} of "
                     f"{read['request'].get('quantity', 1)}. Every change is "
                     f"on the trail with your reason, and the list as first "
                     f"approved is still there beside it.",
                touched=[read["request"]["id"]])
        if form_id == self.ALLOCATE_FORM:
            read, refused = self._read_allocation(scope, values, about)
            if refused is not None:
                return refused
            assert read is not None
            request = read["request"]
            for employee, name in read["people"]:
                _record(request, employee, name, read["given"],
                        read["mode"], read["actual"])
            ledger.add_event(subject=request["id"], kind="allocated",
                             by=scope.user, stage="distribution",
                             items=len(read["people"]),
                             note=", ".join(n for _e, n in read["people"]))
            left = request.get("quantity", 1) - len(allocated_to(request["id"]))
            return Outcome(
                ok=True,
                said=f"Recorded against "
                     f"{', '.join(n for _e, n in read['people'])} at "
                     f"{rewardlens.rupees(read['actual'])} each, "
                     f"{read['mode']}, on {read['given']}."
                     + (f" {left} still to allocate." if left else
                        " Everything in this order now has somebody against "
                        "it, so it can be signed off."),
                touched=[request["id"]])
        if form_id != self.REQUEST_FORM:
            return Outcome(ok=False, said=f"No form called {form_id!r}.")
        request, refused = self._read(scope, values)
        if refused is not None:
            return refused
        assert request is not None
        ledger.add_request(request)
        ledger.add_event(subject=request["id"], kind="requested",
                         by=scope.user, value=request["value"],
                         recipient=request["for"],
                         # The original list, on the trail from the start.
                         # Nothing ever edits this event, so "who was it
                         # approved for" has one answer for ever.
                         intended="|".join(request["intended"]))
        verdict = checks(scope, request)
        note = ("" if not verdict["warns"]
                else " It would take them over the annual limit, and whoever "
                     "approves it will be told so.")
        return Outcome(
            ok=True,
            said=f"Requested: {request['quantity']} \u00d7 {request['what']} "
                 f"{'for ' + ', '.join(_name_of(e) for e in request['intended']) if request['intended'] else 'to be allocated'}"
                 f", {rewardlens.rupees(request['value'])} each."
                 + (f" {request['quantity'] - len(request['intended'])} still "
                    f"to name." if len(request['intended']) < request['quantity']
                    else "") + note,
            touched=[request["id"]],
        )


register(GiftRequests())
