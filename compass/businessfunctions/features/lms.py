"""Leave Management System — balances, applications and approvals.

Two audiences in one feature, which is why it has two tabs rather than two
features: what is waiting on you as a manager, and what you hold yourself.

THE SCOPE IS THE POINT. A manager sees the people who report to them and
nobody else. That is not a filter the person chooses — it comes from the
request, so there is no view of it to widen and nothing for the assistant to
be talked into. Leave is about named individuals, and the blast radius of
getting this wrong is somebody's medical absence being visible to the wrong
manager.

Approving is OUTWARD. It notifies the applicant and books the days, and
Compass cannot take that back. The manifest marks it so, which is what makes
the assistant confirm each one rather than offering to clear the queue.

The records are a FIXTURE mirroring the mockup; `_queue_for` and `_balances`
are the two places that change when the store exists.
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

_FIXTURE: list[dict[str, Any]] = [
    {"id": "a1", "who": "Arjun P.", "initials": "AP",
     "role": "Associate · Assurance", "dates": "30 Oct", "kind": "Casual",
     "days": 1, "waiting_days": 4, "status": "open"},
    {"id": "a2", "who": "Divya R.", "initials": "DR",
     "role": "Senior · Tax", "dates": "31 Oct", "kind": "Casual",
     "days": 1, "waiting_days": 2, "status": "open"},
    {"id": "a3", "who": "Meera S.", "initials": "MS",
     "role": "Manager · Advisory", "dates": "6–8 Nov", "kind": "Earned",
     "days": 3, "waiting_days": 1, "status": "open"},
]

#: My own balances: taken, entitlement, and whether the rest carries over.
_BALANCES: list[dict[str, Any]] = [
    {"kind": "Earned", "left": 11, "of": 18, "note": "Carries up to 10 days"},
    {"kind": "Casual", "left": 4, "of": 6,
     "note": "Lapses 31 Dec — no carry-forward", "lapses": True},
    {"kind": "Sick", "left": 8, "of": 8, "note": "Certificate over 2 days"},
]

#: What the policy expects of an approver, in hours. Past it, a request is
#: late rather than merely waiting, and the queue says so.
_EXPECTED_HOURS = 72


def _queue_for(scope: Scope) -> list[dict[str, Any]]:
    """Requests from this person's own reports. Never anybody else's."""
    return _FIXTURE


def _open(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["status"] == "open"]


class Lms(Feature):
    key = "lms"
    #: A row is one leave request.
    row_key = "id"

    def figures(self, scope: Scope) -> list[Figure]:
        waiting = _open(_queue_for(scope))
        oldest = max((r["waiting_days"] for r in waiting), default=0)
        lapsing = sum(b["left"] for b in _BALANCES if b.get("lapses"))
        mine = sum(b["left"] for b in _BALANCES)
        return [
            Figure("balance", f"{mine}d", "three types"),
            Figure("awaiting", str(len(waiting)),
                   f"oldest is {oldest} days" if waiting else "queue is clear",
                   "warn" if waiting else "good"),
            Figure("team_out", "4", "during close week"),
            Figure("lapsing", f"{lapsing}d", "casual, 31 Dec",
                   "warn" if lapsing else "plain"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        return [
            Tab("approvals", "Team approvals", len(_open(_queue_for(scope)))),
            # Balances are leave types, not requests: a row here is "Casual",
            # not "LR-114".
            Tab("mine", "My leave", len(_BALANCES), key_field="kind"),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if tab == "mine":
            return [dict(b) for b in _BALANCES]
        # `late` is derived rather than stored so it cannot go stale while a
        # request sits in the queue.
        return [dict(r, late=r["waiting_days"] * 24 > _EXPECTED_HOURS)
                for r in _queue_for(scope)]

    def rules(self, scope: Scope) -> list[str]:
        return ["team_only"]

    def act(self, scope: Scope, action: str, targets: list[str]) -> Outcome:
        rows = {r["id"]: r for r in _queue_for(scope)}
        chosen = [rows[t] for t in targets if t in rows]
        if not chosen:
            return Outcome(ok=False, said="No request by that id is in your queue.")

        if action in ("approve", "decline"):
            settled = [r["who"] for r in chosen if r["status"] != "open"]
            if settled:
                return Outcome(
                    ok=False,
                    said=f"{settled[0]}'s request has already been decided.",
                )
            # Outward and irreversible, so one at a time. A proposal covering
            # the whole queue is exactly the thing a person should have to
            # restate request by request.
            if len(chosen) > 1:
                return Outcome(
                    ok=False,
                    said="Approving notifies the applicant and books the days, "
                         "so they go one at a time. Name the request you mean.",
                )
            row = chosen[0]
            row["status"] = "approved" if action == "approve" else "declined"
            left = len(_open(_queue_for(scope)))
            if action == "decline":
                return Outcome(
                    ok=True,
                    said=f"Declined {row['who']}. A reason is required before "
                         f"it sends. {left} left in the queue.",
                    touched=[row["id"]],
                )
            return Outcome(
                ok=True,
                said=f"Approved {row['who']} — {row['dates']}, "
                     f"{row['kind'].lower()}. Notified. {left} left in the queue.",
                touched=[row["id"]],
            )

        return Outcome(ok=False, said=f"LMS has no action called {action!r}.")


register(Lms())
