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

from compass.businessfunctions import ledger, policy, people
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
#: The teams that pay and what each has, and the single-gift threshold —
#: both now versioned in `policy`. A budget changed today does not make
#: last quarter's approvals retrospectively unaffordable, which is the same
#: argument as the annual limit.
def BUDGETS_now() -> dict[str, int]:
    return policy.budgets()


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
     "source": "Procurement · Bengaluru", "by": "servicelead",
     "by_employee": "E-1033", "at": 0.0},
    {"id": "R-SEED02", "for": "E-1007", "kind": "gift",
     "what": "Long service watch", "value": 6000, "occasion": "long service",
     "source": "People team", "by": "servicelead",
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
        "next": "payroll_tax",
    },
    "payroll_tax": {
        "back": True,
        "ask": "What did you check — the tax logic and the taxable value?",
        "step": "the tax treatment",
        "holder": "Payroll",
        "owner": "payroll",
        "label": "With Payroll",
        "verb": "Confirm the tax treatment",
        "asks": "the tax logic and the employee-level taxable value",
        "next": "procurement",
    },
    "procurement": {
        "back": True,
        "ask": "",
        "step": "the purchase",
        "holder": "Procurement",
        "owner": "procurement",
        "label": "With Procurement",
        "verb": "Mark it bought",
        "asks": "purchase through an approved channel",
        "next": "accounts_payable",
    },
    "accounts_payable": {
        "back": True,
        "ask": "",
        "step": "payment",
        "holder": "Accounts Payable",
        "owner": "accounts_payable",
        "label": "With Accounts Payable",
        "verb": "Mark it paid",
        "asks": "financial processing against the approved purchase",
        "next": "distribution",
    },
    "distribution": {
        # The business requester records what was ACTUALLY given, because
        # they are the ones who handed it over and the only ones who can say
        # it differed from what was asked for. This is the moment it reaches
        # the register and starts counting.
        "back": False,
        "ask": "What was actually given, and anything different from what "
               "was asked for?",
        "step": "what was actually given",
        "holder": "the requester, to hand it over",
        "owner": "requester",
        "label": "With the requester to distribute",
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
        stage = STAGES[stage]["next"]
    return stage


def stage_label(stage: str) -> str:
    """The column heading: where it is."""
    if stage == "declined":
        return "Declined"
    if stage == "cancelled":
        return "Withdrawn"
    return STAGES[stage]["label"]


def step_name(stage: str) -> str:
    """The same stage in a sentence: what was being decided."""
    return STAGES.get(stage, {}).get("step", "") or "this step"


def _state(request: dict[str, Any]) -> str:
    """The stage, under the name the rest of this module already used."""
    return stage_of(request)


def committed(source: str) -> int:
    """What this team has committed. Public, so the budget screen reads the
    same figure the sign-off check uses rather than summing requests again."""
    return _spent(source)


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
    if request["for"] == request["by_employee"] and request["by_employee"]:
        return {"ok": False, "said": "you cannot request a gift for yourself"}
    if request["value"] > BIG_now():
        return {"ok": True,
                "said": f"over {rewardlens.rupees(BIG_now())}, so compliance "
                        f"approves rather than a line manager"}
    return {"ok": True, "said": "within policy"}


def _budget_check(request: dict[str, Any],
                  figures: bool = True) -> dict[str, Any]:
    """Whether the team asking still has the money.

    `figures` carries whether the reader may be told HOW MUCH. A team's
    remaining budget is the team's business; somebody raising one request
    needs to know it will not clear, not what the department has left.
    """
    budget = BUDGETS_now().get(request["source"])
    if budget is None:
        return {"ok": True, "said": "no budget recorded for this team"}
    spent = _spent(request["source"])
    if stage_of(request) not in ("service_line", "declined", "cancelled"):
        # Already counted in `spent`; do not charge it twice when the row is
        # redrawn after the first sign-off.
        spent -= request["value"]
    committed = spent
    left = budget - committed
    if request["value"] > left:
        return {"ok": False,
                "said": (f"{rewardlens.rupees(left)} left of "
                         f"{rewardlens.rupees(budget)} — short by "
                         f"{rewardlens.rupees(request['value'] - left)}")
                        if figures else
                        "the team paying does not have this left"}
    return {"ok": True,
            "said": (f"{rewardlens.rupees(left - request['value'])} would be "
                     f"left of {rewardlens.rupees(budget)}") if figures
                    else "the team paying has the budget"}


def checks(scope: Scope, request: dict[str, Any]) -> dict[str, Any]:
    """All three, in the words this reader may have them in.

    The VERDICTS are the same for everybody — blocked is blocked whoever is
    looking. What changes is whether the sentence carries the figures
    behind it, because a colleague's year and a team's budget are not an
    ordinary employee's to read just because they asked for a hamper.
    """
    policy = _policy_check(request)
    tax = _tax_check(scope, request["for"], request["value"], request["kind"])
    budget = _budget_check(request, _may_read_totals(scope, request["for"]))
    return {"policy": policy, "tax": tax, "budget": budget,
            "blocked": not (policy["ok"] and budget["ok"]),
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
        return who.name if who else request["by"]
    if stage == "acknowledgement":
        return _name_of(request["for"])
    return people.LABELS.get(STAGES.get(stage, {}).get("owner", ""), "—")


def _rows(scope: Scope) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for request in ledger.requests():
        stage = stage_of(request)
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
            "source": request["source"],
            "requested_by": request["by"],
            "stage": stage,
            "status": stage_label(stage),
            # Who it is WITH — the question everybody asks of a workflow,
            # and the one a status alone never answers.
            "with": _with(request, stage),
            "policy": verdict["policy"]["said"],
            "tax": verdict["tax"]["said"],
            "budget": verdict["budget"]["said"],
            "blocked": verdict["blocked"],
            "warns": verdict["warns"],
            "steps_done": len(signed),
            "last_signed_by": last["by"] if last else "",
            "last_signed_at": _day(last["at"]) if last else "—",
            "why": ((ended.get("note") if ended else None)
                    or (last.get("note") if last else None) or "—"),
            "acknowledged_on": _day(ack["at"]) if ack else "—",
            # What to ask this desk before it signs off, per action. The
            # screen asks it; the server requires an answer to the same
            # question, so the two cannot disagree about what is needed.
            "ask": {"sign_off": STAGES.get(stage, {}).get("ask", "")},
            "can": _allowed(scope, request, stage, verdict),
        })
    # Whatever is with somebody comes before whatever is finished, and the
    # oldest first inside that — a queue, not a list.
    out.sort(key=lambda r: (r["stage"] in ENDED, r["steps_done"], -r["value"]))
    out = scope.narrow(out, person_key=("employee_id", "by_employee"))
    return [r for r in out if _concerns(scope, r)]


#: Who reads this screen as a POSITION rather than as a queue. Everybody
#: else is a desk in the chain and sees their own work; these two personas
#: exist to see all of it, which is what the dashboard and the trail are.
SEES_EVERYTHING = frozenset({"compliance", "leadership"})


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
        return request["by"] == scope.user
    if stage == "acknowledgement":
        return scope.mine(request["for"])

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
    if mine_to_sign(scope, request, stage):
        # Policy and budget stop it at the gate; a tax consequence does not
        # — it is shown, and whoever signs off does so knowing.
        if not verdict["blocked"]:
            can.append("sign_off")
        # Only the desks whose step is a CHECK. The last two record what
        # happened; they do not decide it. See `back` on STAGES.
        if STAGES[stage]["back"]:
            can.append("decline")
    # The person who raised it can withdraw it until it has been bought.
    if request["by"] == scope.user and stage in (
            "service_line", "finance_policy", "payroll_tax"):
        can.append("cancel_request")
    return can


class GiftRequests(Feature):
    key = "gift_requests"
    row_key = "id"
    private = frozenset({"by_employee", "stage", "blocked", "warns", "can",
                         "steps_done", "ask"})

    def step_label(self, step: str) -> str:
        """A stage on the trail, in the words the screen uses for it."""
        return stage_label(step) if step in STAGES else super().step_label(step)

    REQUEST_FORM = "request_gift"
    FIELDS = frozenset({"recipient", "kind", "what", "value", "occasion",
                        "source"})

    def figures(self, scope: Scope) -> list[Figure]:
        rows = _rows(scope)
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
        rows = _rows(scope)
        return [
            # A queue, not a filter: these are the ones this persona has to
            # do something about, which is the whole point of routing.
            Tab("mine", "Waiting on you", len(_my_step(rows))),
            Tab("moving", "In flight",
                len([r for r in rows if r["stage"] not in ENDED])),
            Tab("closed", "Closed", len(_closed(scope, rows))),
            Tab("all", "Everything", len(rows)),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        rows = _rows(scope)
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
        rows = _rows(scope)
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

        if action not in row["can"]:
            # Say whose it is, not just that it is not theirs — "with
            # Payroll" tells somebody where to chase; "you cannot do that"
            # tells them nothing.
            return Outcome(
                ok=False,
                said=f"{row['what']} is {row['status'].lower()} — that is "
                     f"not something you can do to it now.",
            )

        request = next(r for r in ledger.requests() if r["id"] == row["id"])
        stage = row["stage"]

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
                "acknowledged": False,
                "from_request": row["id"],
            }
            ledger.add_item(item)
            after = next((r["total"] for r in rewardlens.totals(
                Scope(user=scope.user, period=scope.period, sees="firm"))
                if r["employee_id"] == request["for"]), request["value"])
            said_extra = (f" It is on the register against "
                          f"{row['recipient']}, whose year is now "
                          f"{rewardlens.rupees(after)}.")

        if stage == "acknowledgement":
            for item in rewardlens.items(scope):
                if item.get("from_request") == row["id"]:
                    item["acknowledged"] = True
                    ledger.touch_item(item["id"])
                    ledger.add_event(subject=item["id"], kind="acknowledged",
                                     by=scope.user)
            said_extra = (" The record now says you confirmed receiving it, "
                          "rather than the firm saying you did.")

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
        employee = _employee_named(recipient)
        if not employee:
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
                "Say who it is for. A gift has to be recorded against somebody.")

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
        if source not in BUDGETS_now():
            return None, Outcome(ok=False, said=f"Which team is paying? One "
                                                f"of: {', '.join(BUDGETS_now())}.")
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
