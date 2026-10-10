"""The trail, as somebody who has to prove it reads it.

Every other screen in SCS shows a position: who is over, what is waiting,
what the firm is exposed to. Audit's question is different and harder — can
every gift and every approval be shown to have happened, in order, with a
name against it. A position cannot answer that. The events can, and they
have been accumulating since the ledger landed.

SO THIS IS THE RAW TRAIL, not a summary of it. One row per thing that
happened, newest first, in the words of what it was. A screen that
aggregated would be a fourth opinion about the same facts; the point of
this one is that it is the facts, and that it is boring.

IT CHANGES NOTHING, and declares no actions at all — which also tells the
assistant it cannot operate here. An audit function that could alter what
it audits is not one, and the temptation is real: Audit can see everything,
so it is the persona most likely to be quietly over-granted by somebody
being helpful.

WHY IT IS NOT A LOG FILE. A server log is a side effect: it rotates, it is
not a record anybody promised to keep, and it does not survive the thing
that wrote it. These events are stored, ordered and addressed to a subject,
so "show me everything that happened to John's gifts" is a question with an
answer rather than a grep.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from compass.businessfunctions import ledger
from compass.businessfunctions.features import rewardlens
from compass.businessfunctions.features.base import (
    Feature,
    Figure,
    Outcome,
    Scope,
    Tab,
    register,
)

#: Which events are somebody deciding something, as against something
#: happening. The distinction Audit cares about: a decision has a person
#: answerable for it.
DECISIONS = frozenset({"reviewed", "approved", "declined", "referred",
                       "excepted", "superseded", "cancelled"})

#: What each kind of event reads as, in the active voice. Written out rather
#: than generated from the key, because "superseded" and "a record was
#: replaced" are not the same sentence to somebody reading a trail.
_SAYS = {
    "requested": "asked for",
    "approved": "approved",
    "declined": "declined",
    "purchased": "recorded as bought",
    "given": "handed over",
    "acknowledged": "confirmed receiving",
    "cancelled": "withdrew",
    "reviewed": "decided",
    "answered": "answered a question about",
    "superseded": "replaced the record of",
    "notified": "was told about",
    "referred": "referred to Finance",
    "excepted": "recorded a decision on",
    "chased": "chased an acknowledgement from",
}


def _subject_of(subject: str) -> str:
    """What the subject of an event is called, in words."""
    for item in ledger.items():
        if item["id"] == subject:
            return f"{item['what']} ({item['recipient']})"
    for request in ledger.requests():
        if request["id"] == subject:
            return f"{request['what']} (requested)"
    for row in rewardlens.totals(Scope(user="", sees="firm")):
        if row["employee_id"] == subject:
            return row["recipient"]
    return subject


def _detail(event: dict[str, Any]) -> str:
    """The part of an event worth reading on a row."""
    bits = []
    if event.get("state"):
        bits.append(str(event["state"]))
    if event.get("value"):
        bits.append(rewardlens.rupees(int(event["value"])))
    if event.get("excess"):
        bits.append(f"{rewardlens.rupees(int(event['excess']))} over")
    if event.get("replacement"):
        bits.append(f"replaced by {event['replacement']}")
    if event.get("response"):
        bits.append(str(event["response"]))
    if event.get("note"):
        bits.append(f"“{event['note']}”")
    if event.get("why"):
        bits.append(str(event["why"]))
    return " · ".join(bits) or "—"


def _rows(scope: Scope) -> list[dict[str, Any]]:
    out = []
    for event in ledger.events():
        out.append({
            "id": event["id"],
            "when": dt.datetime.fromtimestamp(event.get("at", 0)).strftime(
                "%Y-%m-%d %H:%M") if event.get("at") else "—",
            "who": event.get("by") or "—",
            "did": _SAYS.get(event["kind"], event["kind"]),
            "what": _subject_of(event["subject"]),
            "detail": _detail(event),
            "kind": event["kind"],
            "subject": event["subject"],
        })
    out.reverse()
    return out


class GiftAudit(Feature):
    key = "gift_audit"
    row_key = "id"
    private = frozenset({"kind", "subject"})

    def figures(self, scope: Scope) -> list[Figure]:
        if scope.sees != "firm":
            return []
        rows = _rows(scope)
        decisions = [r for r in rows if r["kind"] in DECISIONS]
        items = rewardlens.items(Scope(user=scope.user, period=scope.period,
                                       sees="firm"))
        unacknowledged = [i for i in items if not i["acknowledged"]
                          and i["id"] not in rewardlens.superseded()]
        return [
            Figure("events", str(len(rows)), "things recorded"),
            Figure("decisions", str(len(decisions)),
                   "with somebody answerable"),
            Figure("records", str(len(items)), "gifts on the register"),
            # The one figure here that is a finding rather than a count: a
            # record nobody has confirmed receiving is a record the firm
            # asserts and the recipient has not.
            Figure("unconfirmed", str(len(unacknowledged)),
                   "not confirmed by the recipient" if unacknowledged
                   else "all confirmed",
                   "warn" if unacknowledged else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        if scope.sees != "firm":
            return []
        rows = _rows(scope)
        return [
            Tab("decisions", "Decisions",
                len([r for r in rows if r["kind"] in DECISIONS])),
            Tab("everything", "Everything", len(rows)),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if scope.sees != "firm":
            return []
        rows = _rows(scope)
        if tab == "decisions":
            return [r for r in rows if r["kind"] in DECISIONS]
        return rows

    def rules(self, scope: Scope) -> list[str]:
        if scope.sees != "firm":
            return ["audit_is_a_role"]
        if not _rows(scope):
            return ["nothing_has_happened"]
        return []

    def act(self, scope: Scope, action: str, targets: list[str],
            note: str = "") -> Outcome:
        return Outcome(
            ok=False,
            said="Audit reads. Nothing on this screen can be changed from "
                 "it, including by Audit — a function that could alter what "
                 "it audits is not one.",
        )


register(GiftAudit())
