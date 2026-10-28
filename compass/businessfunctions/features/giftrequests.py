"""Asking for a gift, and what has to be true before it is given.

RewardLens records what the firm GAVE. This is the half that happens first,
and it is where the money and the breach are still preventable:

    requested → policy · tax · budget → approved or declined
              → purchased → list closed → given

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
never their own request, and never a gift to themselves. Procurement buys.
Operations settle who is getting one and then hand them out.

THE RECIPIENT DOES NOTHING, and that is deliberate. This chain used to end
with the recipient confirming receipt, on the argument that it made the
register something they had agreed to. The business does not work that way:
the list Operations close IS the acknowledgement, signed by the desk that
handed the gifts over and can be held to it. Asking a hundred thousand
people to log in and agree to a record the firm already holds adds no fact
the firm did not have — it only adds the people who never answer, and a
register that reads as a permanent backlog of their silence.

So recipients are TOLD, once, at distribution. Nothing is expected back.

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
#: in a quantity field cannot write more records than the firm could ever
#: mean. It was 500, which was the typo guard and a scale assumption
#: wearing one hat: a festival order reaches the whole firm, and the
#: business puts the ceiling at a lakh. Fifty, two hundred and a hundred
#: thousand are all ordinary numbers here, so the guard has to sit above
#: the largest of them rather than in the middle of the range.
#:
#: SEE `_tell_the_recipients` before raising this further. The guard is
#: cheap; what it admits is not, and the limit that actually binds a big
#: distribution is the outbox, not this number.
MOST_PER_ORDER = 100_000

#: The stages at which the recipient list is still open. Signing off
#: `recipient_list` is what closes it, so distribution is already too
#: late — the register is built from the list there and nothing may move
#: underneath it afterwards.
BEFORE_CLOSED = ("service_line", "finance_policy", "procurement",
                 "recipient_list")

#: How many people one sentence may name before it stops being a sentence
#: and starts being a list. Past this the count carries the fact.
_NAMES_IN_A_SENTENCE = 5

#: How long a reference from another system may be. A guard, not a format:
#: every firm numbers its own purchase orders differently and Compass has
#: no business having an opinion about theirs.
_REF_MAX = 64

#: How long a reason may be. Long enough for a sentence, short enough that
#: nobody pastes a thread into it.
_TEXT_MAX = 400

#: Who hears when somebody crosses the threshold. The persona whose job
#: is watching it, not the person who tripped it: being told you are over
#: is the recipient's business only once somebody has decided what it
#: means, and deciding that is not this tool's.
ALERT_TO = "compliance"

#: Who may take responsibility for a gift the rules refuse. Finance, as
#: the business's own flow has it — the point of an exception is that
#: somebody senior to the rule owns the decision, so it cannot be the desk
#: the rule is inconveniencing.
EXCEPTION_OWNER = "finance"

#: Who hands the gifts out, besides whoever raised the order. One persona,
#: named here rather than spelled into three functions.
DISTRIBUTOR = "operations"

#: HOW a gift reached somebody is no longer asked. It was a field on the
#: allocation form, and the allocation form is gone: the business hands a
#: whole order out at once, so "in person or by courier" was one answer
#: standing in for two hundred hand-offs — which is not a better record
#: than no answer, it is a worse one, because it reads as though somebody
#: checked. The register keeps an empty mode on records made this way.


def TAX_FREE_now() -> int:
    """The per-gift tax-free limit in force. Above it a gift is TAXABLE —
    which is a fact recorded about the gift, not a gate in front of it."""
    return policy.big()


#: Above this, a CLIENT COURTESY is a conversation rather than a form.
#: Its own number, deliberately not the tax-free limit: those were the same
#: figure by accident, and when the tax-free limit came down to ₹5,000 the
#: courtesy rule would have silently started refusing two thirds of what it
#: used to allow. One is about what a gift costs the recipient in tax; the
#: other is about who the firm is giving to. They move for different
#: reasons, so they are different constants.
CONVERSATION_ABOVE = 10_000

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
        "next": "recipient_list",
    },
    # THE LIST IS CLOSED BEFORE ANYTHING IS HANDED OUT. This used to be
    # half of distribution: one step where Operations both decided who was
    # getting one and recorded that they had. Those are two decisions and
    # the business separates them, because between ordering and handing
    # out is exactly when the list moves — somebody leaves, somebody is
    # missed, the event changes — and the desk needs a moment where it can
    # still be put right and a moment after which it cannot.
    #
    # Signing this off IS the closing. Past it the list is what the
    # register will be built from, so nothing may add to it or take from
    # it: a list that can still change after it was called final is not a
    # list anybody can be held to.
    "recipient_list": {
        # Not a check and not refusable: Operations are not approving the
        # order, they are settling who it reaches. A wrong list is fixed
        # here rather than sent back three desks.
        "back": False,
        "ask": "",
        "step": "the final recipient list",
        "holder": "Operations / SPOC, to settle the list",
        "owner": "operations",
        "label": "With Operations for the list",
        "verb": "Close the recipient list",
        "asks": "who is actually getting one",
        "next": "distribution",
    },
    # DISTRIBUTION IS THE LAST STEP. It used to hand off to the recipient,
    # who confirmed receipt and closed the order. That stage is gone: the
    # list Operations closed at the step before is the acknowledgement, so
    # there is nobody left to wait for and the click that sends the gifts
    # out is the click that finishes the order.
    "distribution": {
        # DISTRIBUTION STARTED. One act, and the moment the order reaches
        # the register: a record per name on the closed list, and one email
        # to each of them saying they have received a gift. The email tells
        # them; it asks them for nothing.
        #
        # It asks for nothing, because by here there is nothing left to
        # decide — the list was settled at the step before and the value
        # was approved three steps before that. The click is the act, and
        # what it leaves behind is the records rather than a sentence
        # about them.
        "back": False,
        "ask": "",
        "step": "the distribution",
        "holder": "Operations / SPOC, to hand them out",
        "owner": "operations",
        "label": "With Operations to distribute",
        "verb": "Start the distribution",
        "asks": "that the gifts have gone out to the people on the list",
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
    # Every stage in this chain is now signed off by a DESK, so the walk is
    # a plain one. It used to break at acknowledgement while any recipient
    # had yet to confirm, because a sign-off there was one of twenty people
    # answering for themselves rather than the order being finished. With
    # the recipient out of the chain there is no stage where one sign-off
    # speaks for part of an order.
    while stage != "closed" and stage in done:
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
    # The recipient's own step, while the chain had one. Orders closed
    # under it carry `signed_off · acknowledgement` on their trail, and the
    # whole point of this table is that those still read.
    "acknowledgement": {"label": "With the recipient", "step": "receipt"},
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
    if (request["occasion"] == "client courtesy"
            and request["value"] > CONVERSATION_ABOVE):
        return {"ok": False,
                "said": f"client courtesy above "
                        f"{rewardlens.rupees(CONVERSATION_ABOVE)} needs a "
                        f"conversation, not a form"}
    if request["for"] and request["for"] == request["by_employee"]:
        return {"ok": False, "said": "you cannot request a gift for yourself"}
    if request["value"] > TAX_FREE_now():
        # NOT a refusal and not a different approver. Above the tax-free
        # limit a gift is taxable, which is something the tax teams act on
        # offline; the order carries on down the chain exactly as it would
        # have. Said here so it is on the row and on the trail rather than
        # discovered by somebody reconciling at the year end.
        return {"ok": True,
                "said": f"above the {rewardlens.rupees(TAX_FREE_now())} "
                        f"tax-free limit, so this gift is taxable — the "
                        f"tax teams' own to work out, not Compass's"}
    return {"ok": True,
            "said": f"within policy, and inside the "
                    f"{rewardlens.rupees(TAX_FREE_now())} tax-free limit"}


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

    THE DESK THAT OWNS THE STEP, every time. Operations' two steps used to
    read "Operations / SPOC or Rahul Desai", naming whoever raised the
    order beside the desk, because both of them may act there — a bulk
    order is listed and handed out by the SPOC who has the boxes, a
    one-off gift by the colleague who asked for it.

    Naming both answered a question nobody asks of this column. "Who is it
    with" is where the order is standing, and it is standing with
    Operations; that one named person was the only place a row carried
    somebody's name as its status, and it read as though the order had
    been assigned to them personally.

    Whoever raised it CAN still act — see the matching clause in `_may` —
    this is what the row says, not what the server allows.
    """
    return people.LABELS.get(STAGES.get(stage, {}).get("owner", ""), "—")


# COMPASS MINTS NOTHING ANY MORE. It used to issue a `GIFT-2627-…`
# reference the moment Finance approved an order, email it to whoever
# raised it, and expect somebody to quote it on the requisition they then
# raised in the purchasing system. The business does not need a second
# number: an order already has its own id, and the purchasing system has
# its own PR, PO and GRN. A reference that exists only to be copied from
# one system into another is a number for the firm to keep in step, not a
# fact about a gift.
#
# What remains is the thread BACK — `purchase_links` below — which is the
# half that was always the point: a register record leads to a gift, to
# an order, to the purchase behind it, using the purchasing system's own
# numbers rather than one of ours.


def purchase_links(request_id: str) -> dict[str, str]:
    """What the firm's own purchasing system raised against this order.

    THE THREAD BACK, and the other half of the reference. Compass issues
    `GIFT-…`, somebody quotes it on the requisition in the existing
    system, and this is where that requisition's own numbers are written
    down beside it — so a register record leads to a gift, which leads to
    an order, which leads to the purchase behind it.

    Reference only, and in one direction: nothing here raises, approves,
    amends or pays anything, and nothing is read back out of Mercury or
    the PR portal. The business asked for the PR/PO to be linked back to
    the tracker, not for the tracker to become part of procurement.
    """
    event = ledger.latest(request_id, "purchase_linked")
    if event is None:
        return {}
    return {k: str(event.get(k, "")) for k in ("pr", "po", "grn")
            if event.get(k)}


def _links_said(request_id: str) -> str:
    """The purchasing system's own numbers, as a row shows them."""
    links = purchase_links(request_id)
    if not links:
        return "not linked yet"
    return " · ".join(f"{k.upper()} {v}" for k, v in links.items())


def _tell(role: str, *, subject: str, body: str, about: str) -> list[str]:
    """Write to everybody whose role this is to know. Says who was told.

    A ROLE IS NOT A PERSON. Several entries in the directory are team
    mailboxes with no address, so writing to the role itself would make one
    notice held for "nobody knows their address" while the people holding
    that role sit beside it with addresses on them.

    When nobody can be reached, ONE notice is still recorded, held with the
    reason. A notice nobody could send is a fact somebody can act on; an
    alert that quietly did not happen is not, and the firm finding out at
    the year end is the whole problem this is here to fix.
    """
    reachable = people.holders(role)
    if not reachable:
        notices.record(to="", name=people.LABELS.get(role, role),
                       subject=subject, body=body, about=about)
        return []
    return [notices.record(to=person.email, name=person.name, subject=subject,
                           body=body, about=about).name
            for person in reachable]


def _told_said(who: list[str], fallback: str) -> str:
    """"told X and Y", or why nobody was."""
    if not who:
        return f"nobody could be told — no address on file for {fallback}"
    if len(who) == 1:
        return f"told {who[0]}"
    return f"told {', '.join(who[:-1])} and {who[-1]}"


def _alert_on_crossing(scope: Scope, request: dict[str, Any],
                       given: list[dict[str, Any]]) -> str:
    """Tell compliance when somebody has just crossed the threshold.

    THE ALERT IS THE POINT. The firm's complaint is that it finds out at
    the year end, and a dashboard that would have shown it is only as good
    as somebody opening the dashboard. The moment a gift is recorded is the
    moment the crossing becomes true, so that is the moment it is said.

    A FLAG, NOT AN ASSESSMENT. What crossing ₹50,000 means for somebody's
    tax is the tax teams' own work, in their own systems, and the body of
    this says so. This module records that the line was crossed and tells
    the people whose job is to look; it does not compute a liability, and
    it does not refuse the gift.

    Once per person per year, not once per gift: whoever reads this needs
    to know that John is over, and needs to know it on the day. The trail
    is what remembers that they were already told, which is why the alert
    is an event on the register and not a flag on a row.
    """
    said: list[str] = []
    firm = Scope(user=scope.user, period=scope.period, sees="firm")
    totals = {r["employee_id"]: r for r in rewardlens.totals(firm)}
    for employee in sorted({i["employee_id"] for i in given}):
        row = totals.get(employee)
        if row is None or not row["over_limit"]:
            continue
        if ledger.happened(employee, "threshold_crossed"):
            continue
        told = _tell(
            ALERT_TO,
            subject=f"{row['recipient']} is past the "
                    f"{rewardlens.rupees(row['limit'])} threshold",
            body=(f"{row['recipient']} ({employee}) has received "
                  f"{rewardlens.rupees(row['total'])} in gifts this year, "
                  f"which is {rewardlens.rupees(row['excess'])} past the "
                  f"{rewardlens.rupees(row['limit'])} threshold.\n\n"
                  f"The line was crossed by {request['what']}, signed off "
                  f"at distribution on order {request['id']}.\n\n"
                  f"This is a flag, not an assessment. What it means for "
                  f"their tax is the tax teams' own work, in their own "
                  f"systems. Compass does not compute it, and deducts "
                  f"nothing from anybody."),
            about=employee)
        ledger.add_event(subject=employee, kind="threshold_crossed",
                         by=scope.user, total=row["total"],
                         excess=row["excess"], request=request["id"],
                         told=len(told))
        said.append(f"{row['recipient']} is now past the "
                    f"{rewardlens.rupees(row['limit'])} threshold — "
                    f"{_told_said(told, people.LABELS[ALERT_TO])}.")
    return " ".join(said)


def _allocation_said(request: dict[str, Any]) -> str:
    """How far an order has got towards knowing whose gifts these are.

    Counted against the LIST when there is one, because that is what the
    desk handing things out is working through, and the approved quantity
    carries a buffer. Says both numbers when more have been given than
    were approved: that is drawn from the spare stock rather than
    something going wrong, and it is still a fact somebody should see
    rather than one the screen rounds away.
    """
    wanted = request.get("quantity", 1)
    done = len(allocated_to(request["id"]))
    listed = len(planned_list(request["id"]))
    if wanted == 1 and request["for"] and not listed:
        return "named when asked for"
    if not done:
        # BEFORE DISTRIBUTION there is a list and no records. Saying
        # "none of 3 named" read as work outstanding when the naming was
        # done — the list is settled and nothing has gone out yet, which
        # is a different thing and the row should say which.
        closed = stage_of(request) not in BEFORE_CLOSED
        return (f"{listed} on the list, "
                f"{'closed' if closed else 'still open'}" if listed
                else "nobody on the list yet")
    if done > wanted:
        return (f"{done} given · {done - wanted} past the {wanted} "
                f"approved")
    return f"{done} given of {wanted} approved"


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
        # SETTLED THE MOMENT IT IS WRITTEN. This was False and waited for
        # the recipient to say otherwise. Nobody is asked now: the record
        # is made from a list Operations closed and signed off, which is
        # what the business means by the gift being acknowledged, so a
        # record that arrives already settled is the truth rather than a
        # shortcut. Left False it would have made every procured record on
        # the register permanently unconfirmed — a backlog of silence from
        # people who were never asked to speak.
        "acknowledged": True,
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
        # WHEN THE GIFTS WENT OUT. This was the day the recipient confirmed,
        # read off the acknowledgement sign-off. With that stage gone the
        # order's own closing act is distribution, which is also the day
        # the register records were written — so it is the honest date for
        # a report to be cut on and the one a reader means by "when did
        # they get it". `acknowledgement` is still read for orders closed
        # under the old chain, whose trail has no distribution sign-off.
        out_on = next((e for e in trail if e["kind"] == SIGN_OFF
                       and e.get("stage") in ("distribution",
                                              "acknowledgement")), None)
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
            "distributed_on": _day(out_on["at"]) if out_on else "—",
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
            # The thread back to the system that bought it: the PR, PO or
            # GRN the purchasing system raised. There were two fields
            # here, `bought_on` carrying a reference Compass minted
            # itself — a number whose only job was to be copied into the
            # other system. With that gone the two said the same thing, so
            # there is one.
            "purchase": _links_said(request["id"]),
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
            # WHAT THE BUTTON SAYS, when the stage's own words are better
            # than the action's. One `sign_off` carries six different
            # steps, and "Sign off" is the right word for a check and the
            # wrong one for the act that puts two hundred gifts on the
            # register — the business calls that starting the
            # distribution, and the button should say what it does. On
            # the row because only the row knows which step it is at.
            "labels": {"sign_off": STAGES[stage]["verb"]}
                      if stage in ("recipient_list", "distribution") else {},
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


#: What a RECIPIENT is shown of an order they are on. Breadth decided
#: which rows they see and said nothing about which fields, so being one
#: name on a list handed somebody the whole order sheet: the other three
#: recipients, the quantity, what each one cost, the purchase reference,
#: who raised it and who signed it.
#:
#: None of that is theirs. What they are given, why and where it has got
#: to — the rest is the firm's own business with itself, and "who else got
#: one" is the other recipients'. Their own annual position is theirs too,
#: and it is on the Tracker, which is the screen for it.
#:
#: A WHITELIST rather than a list of things to strip, because the failure
#: mode of the other shape is a field added later that nobody remembers to
#: hide. A new field is invisible to a recipient until somebody says
#: otherwise, which is the safe direction for it to be wrong in.
MINE_ONLY = frozenset({
    # what it is and why they have it
    "id", "what", "kind", "occasion", "purpose", "value",
    # where it has got to, and the day it was handed out
    "status", "distributed_on",
    # machinery the surface needs to draw their one button. `allocated_ids`
    # is deliberately NOT here: it is every recipient's employee number,
    # the surface never reads it, and `private` only keeps a field out of
    # the workbook — the values still cross the wire.
    "stage", "steps_done", "can", "labels", "ask", "blocked", "warns",
})


def _theirs_only(scope: Scope, row: dict[str, Any]) -> dict[str, Any]:
    """Cut a row to what the reader is entitled to, when all they are is
    a recipient of it.

    TWO KINDS OF "THEIRS" at self breadth, and only one of them is small.
    Somebody who RAISED an order may read it in full: they chose the
    quantity, the value, the occasion and the names, and hiding their own
    request back from them would be absurd — what they still may not read
    is the recipient's year, which `_tax_check` masks on its own and has
    always masked. Somebody who is on it because a gift is coming to them
    is in a different position entirely: the order is the firm's business
    with itself, and the only part of it that is theirs is the gift.

    `value` survives the cut because the gift's own worth is not a secret
    from the person holding it — they can read it off the card. What goes
    is everything about the ORDER: how many, who else is on it, what it
    was bought on, and whose desks it crossed.
    """
    if scope.sees != "self":
        return row
    if row.get("requested_by") == scope.user or (
            scope.employee and row.get("by_employee") == scope.employee):
        return row

    cut = {k: v for k, v in row.items() if k in MINE_ONLY}
    # THEIR NAME, not the order's. `recipient` is whoever the order named
    # when it was raised, which on a bulk order is somebody else entirely
    # — so the row told Manish K. that the gift was Vikram Rao's. Rewritten
    # rather than dropped, because "is this mine" is the first thing they
    # will ask of it.
    cut["recipient"] = _name_of(scope.employee) or "you"
    cut["employee_id"] = scope.employee
    # AND WHOSE DESK IT IS ON, without naming the other people standing on
    # it. There is no longer a stage where that desk is the recipient's
    # own, so this is the chain's answer or nothing — and a recipient is
    # never asked to do anything to it.
    cut["with"] = (row.get("with") if row.get("stage") in ENDED
                   else "the firm, nothing for you to do")
    return cut


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

    feature = _registry.feature_of(rewardlens.FUNCTION, "requests")[1]
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
            # NO MOMENT MEANS NO WAIT. Aged from `at`, and a request that
            # carries none — the seeds a fresh deployment starts with are
            # `at: 0.0` — aged from the epoch and reported 20,744 days.
            # A queue claiming something has been waiting fifty-six years
            # is a queue nobody reads the rest of, and the figure it feeds
            # ("something has been waiting too long") fired on every empty
            # deployment. Guarded the way `oversight._days_since` already
            # guards it.
            "waiting_days": (max(0, int((time.time() - at) // 86_400))
                             if at and stage not in ENDED else 0),
            "distributed_on": row["distributed_on"],
            # The chain's own answer about what this desk may do, carried
            # through rather than worked out again. The Tracker is where
            # the signing off happens now, so the buttons it draws have to
            # be the ones `act` will accept — and there is exactly one
            # function that knows which those are.
            "can": row["can"],
            "labels": row["labels"],
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
            "purchase": row["purchase"],
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
    """The closed orders inside the chosen window, by DISTRIBUTION date.

    The date a consolidated report should be cut on is the day the gifts
    actually went out — not when the order was asked for, approved or
    bought. Those can be months apart, and a report cut on the wrong one
    omits exactly the orders that crossed the boundary.

    It used to cut on the day the recipient confirmed, which was the same
    argument pointed at a date the firm did not control: an order sat
    outside every window until somebody who did not have to answer
    answered. Distribution is the firm's own act and the day the register
    records carry, so the report and the register now agree.
    """
    if not scope.since and not scope.until:
        return rows
    out = []
    for row in rows:
        when = row["distributed_on"]
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

    The three stages that are a CONTROL are routed by PERSONA: the stage
    has to be owned by one the person holds, it must not be a request they
    raised, and it must not be a gift to them.

    The last two are not controls and are routed by IDENTITY instead, with
    the persona gate relaxed — being one named person is already narrower
    than holding a role:

      * DISTRIBUTION is Operations / SPOC, who have the boxes, OR whoever
        raised the order, who handed it over. Both happen, and the second
        is why the persona alone will not do: anybody may raise a request
        — a Business Requester whose job it is, or an employee who wants
        to thank somebody — so gating this on a role stranded every
        request an ordinary employee raised, with every desk signed off
        and nobody on earth able to record the distribution.
      * ACKNOWLEDGEMENT is whoever was ALLOCATED one, not whoever the
        request named. Confirming you received something is not deciding
        your own case.
    """
    if stage in ("recipient_list", "distribution"):
        # OPERATIONS SETTLE THE LIST AND HAND THINGS OUT, and so may
        # whoever raised it. Both, because both happen: a bulk order is
        # listed and distributed by the SPOC who has the boxes, a one-off
        # gift by the colleague who asked for it. Routed by identity OR by
        # that one persona rather than by the persona alone — gating it on
        # a role stranded every request an ordinary employee raised, which
        # is the bug the comment below was written about. Both of
        # Operations' steps route the same way: a desk that may hand an
        # order out may certainly say who to.
        return request["by"] == scope.user or DISTRIBUTOR in scope.roles
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

    # The exception branch. A flagged order is not a dead end: whoever is
    # holding it may ask Finance to take responsibility for it, and
    # Finance may grant that or refuse it. Offered only while something IS
    # biting and nobody has decided yet, because an exception to a rule
    # that is not biting is not an exception.
    #
    # TWO ROUTES PAST THE CUMULATIVE THRESHOLD, which is what the business
    # asked for: a sign-off made knowingly, or an exception somebody puts
    # their name to. A crossing used to offer only the first — the signer
    # could wave it through on their own authority but could not escalate
    # it, so the desk that wanted cover for an ordinary ₹6,000 watch that
    # happened to take somebody past ₹50,000 had nowhere to go but out of
    # Compass. `warns` is that crossing; `blocked` is a rule that refuses
    # outright. Both are things Finance can be asked to own.
    if ((verdict["blocked"] or verdict["warns"])
            and exception_on(request["id"]) is None
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

    # THE FORMS A ROW MAY OPEN, named here beside the actions because the
    # surface draws what a row says it allows and nothing else. These were
    # missing, and missing meant unreachable: `formsFor` filters the
    # on-row forms by this list, so a form nobody names here has no
    # button anywhere in the UI. Operations were told "name who received
    # the other 2 before signing this off" by a chain that then offered
    # them no way to name anybody — the handler accepted the submit all
    # along, and every check drives `submit` directly, so 1,558 of them
    # passed over a screen with no door in it.
    can.extend(_forms_open(scope, request, stage))
    return can


def list_reopened(request_id: str) -> bool:
    """Whether Finance has reopened a closed list by granting an exception.

    The closed list is the firm's own rule, so the way past it is the way
    past any rule here: somebody senior takes responsibility for the
    change, in their own name, with a reason, on the trail. Only an
    exception granted AT or AFTER the list step counts — one granted three
    desks earlier was about the order's value, and reusing it here would
    let a decision about money quietly unlock a different control.
    """
    granted = exception_on(request_id)
    return bool(granted
                and granted.get("stage") in ("recipient_list", "distribution"))


def _forms_open(scope: Scope, request: dict[str, Any],
                stage: str) -> list[str]:
    """Which on-row forms this person may open on this order, right now.

    Each condition MIRRORS the guard the matching `_read_*` applies, so a
    button appears exactly when the submit behind it would be accepted. A
    button that refuses when pressed is worse than no button: it teaches
    people that the screen lies.
    """
    open_now: list[str] = []
    mine = request["by"] == scope.user
    hands_over = mine or DISTRIBUTOR in scope.roles

    # Keeping the recipient list straight. Whoever raised it, their
    # approver, or Operations — and only up to the step where Operations
    # CLOSE it. Past that the list is what the register was built from,
    # and a list that can still change after it was called final is not a
    # list anybody can be held to. An exception taken at the list step or
    # after is the one thing that reopens it, because then somebody senior
    # has put their name to the change.
    if ((stage in BEFORE_CLOSED or list_reopened(request["id"]))
            and stage not in ENDED
            and (hands_over or "service_line" in scope.roles)):
        open_now.append("revise_list")

    # The purchase, linked back. Procurement's, and theirs as soon as the
    # order reaches them: it was gated on a reference Compass had minted,
    # which no longer exists, and gating it on that now would mean nobody
    # could ever record what an order was bought on.
    if stage == "procurement" and STAGES[stage]["owner"] in scope.roles:
        open_now.append("link_purchase")

    return open_now


class GiftRequests(Feature):
    key = "gift_requests"
    row_key = "id"
    private = frozenset({"by_employee", "stage", "blocked", "warns", "can",
                         "steps_done", "ask", "allocated_ids",
                         "list_changes", "labels"})

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
    # ALLOCATION IS NOT A FORM ANY MORE. Who receives one is the closed
    # recipient list, settled and signed off at its own step, and asking
    # again at distribution was asking the same question twice and
    # inviting two answers to it. The per-person date, mode and actual
    # cost went with it: the business keeps one date for the hand-out and
    # the approved value, and a figure nobody fills in honestly is worse
    # than a figure nobody is asked for.
    LIST_FORM = "revise_list"
    LIST_FIELDS = frozenset({"add", "remove", "why"})
    PURCHASE_FORM = "link_purchase"
    PURCHASE_FIELDS = frozenset({"pr", "po", "grn"})

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
        """The rows this reader may see, cut to the part that is theirs.

        THE CUT IS HERE, at the edge, and not inside `_rows`. Everything
        that reads the chain reads it through `_rows` — the Tracker's own
        progress rows, the Dashboard's count of where orders are, the
        assistant's brief — and those are the chain asking itself a
        question rather than a person being shown an answer. Cutting
        inside it took fields off rows that were never going anywhere
        near a screen, and the first symptom was the Tracker failing on a
        key it had always had.
        """
        rows = _rows(scope, _is_yours)
        if tab == "mine":
            rows = _my_step(rows)
        elif tab == "moving":
            rows = [r for r in rows if r["stage"] not in ENDED]
        elif tab == "closed":
            rows = _closed(scope, rows)
        return [_theirs_only(scope, r) for r in rows]

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
        if stage == "recipient_list":
            # A LIST WITH NOBODY ON IT IS NOT A FINAL LIST. Closing one
            # would send the order to distribution with nothing to write
            # the register against, and the refusal would land one step
            # later on somebody who could no longer fix it — the list
            # shuts when this is signed off.
            final = planned_list(row["id"])
            if not final and not request["for"]:
                return Outcome(
                    ok=False,
                    said=f"Nobody is on this list yet. Put the "
                         f"{request['quantity']} recipients on it with "
                         f"“Change the recipient list” before "
                         f"closing it — closing it is what stops it "
                         f"changing.")
            held = len(final) or 1
            said_extra = (f" The list is closed at {held} "
                          f"{'people' if held > 1 else 'person'}, and "
                          f"nothing changes it now.")
            over = held - request.get("quantity", 1)
            if over > 0:
                said_extra += (f" {over} of them come out of the buffer, "
                               f"past the {request.get('quantity', 1)} "
                               f"approved.")

        if stage == "distribution":
            # DISTRIBUTION STARTED, and the one moment a proposal becomes
            # a record. Nothing before this is on anybody's total and
            # nothing after it can be taken off.
            #
            # BUILT FROM THE CLOSED LIST, not from a second naming. Who is
            # getting one was settled and signed off at the step before,
            # so asking again here would be asking the same question twice
            # and inviting two answers. One record per name, at the value
            # that was approved, dated today.
            #
            # ONE RECIPIENT IS A SPECIAL CASE OF MANY: an order raised for
            # a named person has no list, and that person is the list.
            given = allocated_to(row["id"])
            if not given:
                final = planned_list(row["id"]) or (
                    [request["for"]] if request["for"] else [])
                if not final:
                    return Outcome(
                        ok=False,
                        said="Nobody is on this order's list, so there is "
                             "nothing to hand out. Put the recipients on it "
                             "first — the register cannot hold a gift "
                             "without a person to hold it against.")
                today = dt.date.today().isoformat()
                given = [_record(request, employee, _name_of(employee),
                                 today, "")
                         for employee in final]

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
            over = len(given) - request.get("quantity", 1)
            if over > 0:
                said_extra += (f" {len(given)} against "
                               f"{request.get('quantity', 1)} approved — "
                               f"{over} out of the buffer, and the row says "
                               f"so.")
            # EACH OF THEM IS TOLD, because the next step is theirs and
            # nobody confirms a gift they were never told about. Written
            # down before it is sent, like every other notice here, so an
            # address nobody has is a held notice with a reason rather
            # than a recipient who silently never hears.
            said_extra += " " + self._tell_the_recipients(scope, request, given)
            # The records exist by now, so the totals are true and a
            # crossing is a fact rather than a projection. Said AFTER the
            # register is written for exactly that reason.
            if crossed := _alert_on_crossing(scope, request, given):
                said_extra += " " + crossed

        # APPROVAL MINTS NOTHING. Finance's sign-off used to issue a
        # `GIFT-…` reference and email it to whoever raised the order, for
        # them to quote on the requisition. A number that exists only to
        # be copied between two systems is the firm's bookkeeping, not a
        # fact about a gift, and the thread back is kept the other way
        # round: Procurement writes down the PR/PO the purchasing system
        # raised. The order id was always enough to quote.

        ledger.add_event(subject=row["id"], kind=SIGN_OFF, by=scope.user,
                         stage=stage, note=note.strip(), value=row["value"])

        nxt = STAGES[stage]["next"]
        # CLOSED WHEN THE LAST DESK SIGNS. This used to be guarded: the old
        # last stage was the recipient's, one order could reach twenty of
        # them, and a sign-off there was one person answering for
        # themselves \u2014 so writing `closed` on the first confirmation put an
        # order on the trail as finished while three people had yet to
        # receive theirs. Every stage is a desk's now, and a desk signs for
        # the whole order, so the guard has nothing left to defend.
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

    def _tell_the_recipients(self, scope: Scope, request: dict[str, Any],
                             given: list[dict[str, Any]]) -> str:
        """Tell everybody who has just been given one. Says who heard.

        ONE EMAIL, NOTHING ASKED. It used to ask them to come and confirm,
        because the chain's last stage was theirs. That stage is gone: the
        closed list is the acknowledgement, so this tells them they have
        received a gift and stops. Nothing in RewardLens waits on a reply,
        and the email no longer implies one is due.

        What it says is deliberately small: what they were given and that
        it counts against their year. It does not tell them what it means
        for their tax, because that is not this module's to say.

        THE OUTBOX IS THE CEILING, not this loop. `notices.KEEP` is how
        many notices the process holds, and `notices.record` evicts the
        oldest past it — so a list longer than that would queue a hundred
        thousand notices, keep the last few hundred, send those, and say
        nothing about the rest. A distribution that silently fails to tell
        most of the people it was for is the worst failure this feature
        has, so the shortfall is counted, recorded on the trail and said
        out loud. See `MOST_PER_ORDER`: an order may be for a lakh, and
        the register holds a lakh of records without complaint; it is the
        TELLING that needs a real queue before it reaches that size.
        """
        # Taken once. One scan of the directory per recipient is the
        # quadratic the big lists would have found first.
        directory = people.by_employee()
        # Room for this distribution's own notices, so the count is what
        # the outbox can still carry rather than what it holds empty.
        room = max(0, notices.KEEP - notices.waiting())
        told = heard = addressless = unsent = 0
        held_names: list[str] = []
        for item in given[:room]:
            person = directory.get(item["employee_id"])
            notice = notices.record(
                to=person.email if person else "",
                name=item["recipient"],
                subject=f"{request['what']} — you have received a gift",
                body=(f"{request['what']}, recorded at "
                      f"{rewardlens.rupees(item['value'])}, has been handed "
                      f"out to you.\n\nIt is on the register against your "
                      f"name and counts towards your annual limit. There is "
                      f"nothing for you to do — this is to tell you, not to "
                      f"ask you.\n\nIf anything here is wrong, tell the team "
                      f"who gave it to you."),
                about=item["id"])
            told += 1
            if notice.state != "held":
                heard += 1
            elif "no email address" in notice.detail:
                # WHY it was held is the whole point of holding it. "No
                # address for Priya" is somebody's to go and fix; "no mail
                # server" is the deployment's and says nothing about Priya
                # at all. Reporting both as the first sent people looking
                # for a missing address that was never missing.
                addressless += 1
                # Named only while naming them is readable. Past that the
                # count is the fact and the register is where the names
                # are — a sentence listing four thousand people is not a
                # sentence anybody reads.
                if len(held_names) < _NAMES_IN_A_SENTENCE:
                    held_names.append(item["recipient"])
            else:
                unsent += 1
        missed = len(given) - told
        ledger.add_event(subject=request["id"], kind="recipients_told",
                         by=scope.user, stage="distribution",
                         told=heard, held=addressless + unsent,
                         not_queued=missed)
        parts = []
        if heard:
            parts.append(f"{heard} told by email")
        if unsent:
            parts.append(f"{unsent} not emailed — no mail server configured")
        if addressless:
            named = ", ".join(held_names)
            more = addressless - len(held_names)
            parts.append(f"no address on file for {named}"
                         + (f" and {more} others" if more > 0 else ""))
        if missed:
            # SAID FIRST, because it is the one that matters: these people
            # have a gift on the register and no idea.
            parts.insert(0, f"{missed} NOT notified — the outbox holds "
                            f"{notices.KEEP} and this list is longer")
        if not parts:
            return "Nobody was emailed."
        return "; ".join(parts) + "."

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
            if not (row["blocked"] or row["warns"]):
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
            told = self._tell_the_asker(scope, row, request, granted=False,
                                        note=note.strip())
            return Outcome(
                ok=True,
                said=f"Exception refused, with your reason on the record. "
                     f"The order stays blocked and can be sent back. {told}",
                touched=[row["id"]])

        # Granted. The order is not changed — the rule still says what it
        # said, and what is recorded is that somebody took responsibility
        # for going ahead anyway.
        ledger.add_event(subject=row["id"], kind="excepted", by=scope.user,
                         note=note.strip(), stage=stage,
                         value=request["value"])
        told = self._tell_the_asker(scope, row, request, granted=True,
                                    note=note.strip())
        # What granting actually CHANGED depends on which rule was biting.
        # A blocked order could not be signed off and now can; a crossing
        # could always be signed off, and what it gains is somebody other
        # than the signer carrying it. Saying "it can now be signed off"
        # about the second would be telling the reader they had been
        # stopped by something that never stopped them.
        now = (" — and it can now be signed off" if row["blocked"]
               else " — and the crossing is yours rather than the "
                    "signer's")
        return Outcome(
            ok=True,
            said=f"Exception granted, in your name. {row['policy']}{now}. "
                 f"The rule stays on the record alongside your decision; "
                 f"neither replaces the other. {told}",
            touched=[row["id"]])

    def _tell_the_asker(self, scope: Scope, row: dict[str, Any],
                        request: dict[str, Any], *, granted: bool,
                        note: str) -> str:
        """Tell whoever asked for the exception what Finance decided.

        AN ANSWER NOBODY HEARS IS NOT AN ANSWER. The order stays where it
        is either way — granted, it can be signed off; refused, it stays
        blocked — so nothing about the row moves to catch the raiser's eye,
        and without this they find out by going back and looking. That is
        the same failure as finding out at the year end, one decision
        smaller.

        To the person who ASKED, not to the raiser: on a request somebody
        escalated on behalf of a service line, they are the one waiting.
        The reason goes in the body, because a decision without its reason
        is the rule quietly not applying and the asker is exactly who needs
        to be able to see it.
        """
        asked = ledger.latest(row["id"], "exception_asked")
        who = str(asked.get("by", "")) if asked else request["by"]
        person = people.find(who)
        verdict = "granted" if granted else "refused"
        notice = notices.record(
            to=person.email if person else "",
            name=person.name if person else who,
            subject=f"Exception {verdict} on {row['what']}",
            body=(f"{people.LABELS[EXCEPTION_OWNER]} has {verdict} the "
                  f"exception asked for on order {row['id']} "
                  f"({request['quantity']} \u00d7 {request['what']}).\n\n"
                  f"Their reason: {note}\n\n"
                  f"The rule that was biting: {row['policy']}\n\n"
                  + ("The order carries on down the chain, and the "
                     "decision is Finance's rather than the signing desk's. "
                     "The rule stays on the record alongside it; neither "
                     "replaces the other."
                     if granted else
                     "Nothing has moved. A rule that refuses leaves the "
                     "order blocked; a crossing still needs a sign-off made "
                     "knowingly, by whoever holds it.")),
            about=row["id"])
        if notice.state == "held":
            return f"{notice.name} was not told: {notice.detail}."
        return f"{notice.name}, who asked, has been told."

    # ── asking ──────────────────────────────────────────────────────────

    def accepts(self, form_id: str) -> set[str]:
        if form_id == self.REQUEST_FORM:
            return set(self.FIELDS)
        if form_id == self.LIST_FORM:
            return set(self.LIST_FIELDS)
        if form_id == self.PURCHASE_FORM:
            return set(self.PURCHASE_FIELDS)
        return set()

    def _read_purchase(self, scope: Scope, values: dict[str, str],
                       about: str) -> tuple[dict[str, Any] | None,
                                            Outcome | None]:
        """The purchasing system's own numbers, or why they cannot be linked."""
        request = next((r for r in ledger.requests() if r["id"] == about), None)
        if request is None:
            return None, Outcome(ok=False, said="No order by that id.")
        if STAGES["procurement"]["owner"] not in scope.roles:
            return None, Outcome(
                ok=False,
                said="Procurement raised it, so Procurement is who writes "
                     "its number down.")
        # AND NOT BEFORE IT IS THEIRS. The row offers this button only at
        # the procurement step, and a submit that took it at any other
        # would be the screen and the server disagreeing about the same
        # rule. The stage was never checked here: a reference Compass
        # minted at approval did the job by accident, so dropping the
        # reference left the form open from the moment an order was
        # raised.
        stage = stage_of(request)
        if stage != "procurement":
            return None, Outcome(
                ok=False,
                said=f"{request['id']} is {stage_label(stage).lower()}. The "
                     f"purchase is written down at Procurement's own step, "
                     f"once the order has reached it.")

        links = {k: values.get(k, "").strip()[:_REF_MAX]
                 for k in ("pr", "po", "grn")}
        links = {k: v for k, v in links.items() if v}
        if not links:
            return None, Outcome(
                ok=False,
                said="Give at least one of the requisition, order or GRN "
                     "number. A gift nobody can trace to what paid for it "
                     "is the gap this closes.")
        return {"request": request, "links": links}, None

    def _read_list(self, scope: Scope, values: dict[str, str],
                   about: str) -> tuple[dict[str, Any] | None,
                                        Outcome | None]:
        """The change to the recipient list, or why it cannot be made.

        Controlled: who may change it and when. A list somebody can
        rewrite silently is not a control, and a list nobody can correct
        is a control people work around — so the window is open right up
        to the step where Operations close it, and shut from then on.
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
        # CLOSED IS CLOSED. Operations signed this list off as final, and
        # the register at distribution is built from it — so a change
        # after that point is a change to what the firm has already
        # recorded giving away. Not a dead end: Finance may take the
        # decision in their own name, and then it opens again.
        if stage not in BEFORE_CLOSED and not list_reopened(request["id"]):
            return None, Outcome(
                ok=False,
                said=f"The list was closed when Operations signed it off, "
                     f"and {stage_label(stage).lower()} is past that. Ask "
                     f"Finance for an exception if it has to change — "
                     f"somebody has to put their name to moving a list the "
                     f"register was built from.")
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
        # ALREADY ON THE REGISTER is the thing that cannot change, and it
        # is a fact about a PERSON rather than about the order. A list
        # frozen the moment the first gift was handed out was the wrong
        # rule: at one of three allocated, Operations could neither add
        # the person they had been told to add nor take off somebody who
        # had left, for two gifts nobody had received. The register is
        # write-once for the one who received theirs; the other two are
        # still a plan, and a plan is what this form is for.
        held = {i["employee_id"] for i in allocated_to(request["id"])}
        for name in [n.strip() for n in values.get("remove", "").split(",")
                     if n.strip()]:
            known = _employee_named(name)
            if known in held:
                return None, Outcome(
                    ok=False,
                    said=f"{_name_of(known)} has already received one from "
                         f"this order, and the register is write-once. A "
                         f"record that was wrong is superseded on the "
                         f"Tracker, not unlisted here.")
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
        # NOT CAPPED BY THE QUANTITY. The list is who MIGHT receive one;
        # the quantity is how many were bought, and the Service Line
        # Approver orders with a buffer precisely so a late addition does
        # not have to go round the chain again. Capped at the quantity,
        # adding the fourth name to a three-gift order was refused, which
        # made the buffer unusable: the desk holding the spare stock
        # could not write down who it was for. Adding somebody is a
        # recorded change with a reason against it, not a new approval —
        # nothing about it asks Procurement to order anything more.
        if not values.get("why", "").strip():
            return None, Outcome(
                ok=False,
                said="Say why it changed. A list that was revised and a list "
                     "that was right first time are different facts, and the "
                     "difference is the one somebody will ask about.")
        return {"request": request, "add": adding, "remove": removing,
                "why": values["why"].strip(), "after": after}, None

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
        # STILL CAPPED HERE, and deliberately not where the list is
        # revised. At the moment of ASKING, more names than gifts is a
        # mismatch worth catching: say how many you mean to give. The
        # buffer the Service Line Approver orders is what covers a LATER
        # addition — somebody who left, somebody who was missed — and
        # that is why "Change the recipient list" has no such cap.
        if len(intended) > quantity:
            return None, Outcome(
                ok=False,
                said=f"{len(intended)} names for {quantity}. Ask for as many "
                     f"as you mean to give — somebody added later draws "
                     f"on the buffer and does not come back here.")

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
        if form_id == self.PURCHASE_FORM:
            read, refused = self._read_purchase(scope, values, about)
            if refused is not None:
                return refused
            assert read is not None
            return Outcome(
                ok=True,
                said="Links " + " · ".join(f"{k.upper()} {v}" for k, v
                                           in read["links"].items())
                     + f" to {read['request']['id']}. Reference only — "
                       f"nothing is raised, amended or paid from here.")
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
        if form_id == self.PURCHASE_FORM:
            read, refused = self._read_purchase(scope, values, about)
            if refused is not None:
                return refused
            assert read is not None
            ledger.add_event(subject=read["request"]["id"],
                             kind="purchase_linked", by=scope.user,
                             stage=stage_of(read["request"]),
                             **read["links"])
            return Outcome(
                ok=True,
                said=f"Linked: {_links_said(read['request']['id'])}. The "
                     f"order and the purchase behind it now lead to each "
                     f"other.",
                touched=[read["request"]["id"]])
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
