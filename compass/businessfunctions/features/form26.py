"""Form 26 — reconciling tax credits against the ledger.

A deductor files what it withheld; the company books what it expected. Where
the two disagree the credit is short, and somebody has to decide whether to
accept the lower figure or go back to the deductor. That decision is the whole
feature, and it is a person's to make.

NOT the annual TDS return. "Form 26" here means the credit statement
reconciliation — a line per credit, carrying the deductor, its TAN, the
challan it was deposited under and the invoice it relates to.

ARITHMETIC IS NOT THE MODEL'S. Every figure below is computed from the rows.
The assistant can propose accepting three lines and can explain why one was
flagged; it cannot author the difference between two numbers. That is the same
rule Estimate states in its catalog and it holds here for the same reason —
a number somebody will act on should come from a subtraction, not a sampler.

The rows are a FIXTURE. They mirror the mockup so the contract can be
exercised end to end before the store exists; `_rows_for` is the one place
that changes when it does.
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

#: Amounts in whole rupees. Not floats: a reconciliation that disagrees with
#: itself in the third decimal is worse than no reconciliation.
_FIXTURE: list[dict[str, Any]] = [
    {"line": "0041", "deductor": "Sharma & Co", "tan": "BLRS04412F",
     "credit": 420000, "ledger": 420000, "status": "matched",
     "challan": "CIN 0024119", "invoice": "INV-8841", "deposited": "2026-07-14"},
    {"line": "0044", "deductor": "Sharma & Co", "tan": "BLRS04412F",
     "credit": 380000, "ledger": 520000, "status": "short",
     "challan": "CIN 0024127", "invoice": "INV-8902", "deposited": "2026-07-22"},
    {"line": "0051", "deductor": "Sharma & Co", "tan": "BLRS04412F",
     "credit": 290000, "ledger": 410000, "status": "short",
     "challan": "CIN 0024188", "invoice": "INV-8955", "deposited": "2026-08-03"},
    {"line": "0058", "deductor": "Sharma & Co", "tan": "BLRS04412F",
     "credit": 510000, "ledger": 730000, "status": "short",
     "challan": "CIN 0024203", "invoice": "INV-9011", "deposited": "2026-08-19"},
    {"line": "0062", "deductor": "Vertex Supply", "tan": "MUMV09233K",
     "credit": 185000, "ledger": 185000, "status": "matched",
     "challan": "CIN 0031044", "invoice": "INV-9088", "deposited": "2026-08-27"},
    {"line": "0067", "deductor": "Orion Freight", "tan": "DELO11872B",
     "credit": 240000, "ledger": 240000, "status": "matched",
     "challan": "CIN 0033901", "invoice": "INV-9120", "deposited": "2026-09-02"},
]

#: Periods already signed. Nothing in them can move, by anybody.
_CLOSED = {"Q4 FY24"}

#: Who prepared the open period. Until there is a store this is a constant;
#: it exists so the second-reviewer rule is exercised rather than assumed.
_PREPARED_BY = "mk"

_DECIDED = ("accepted", "queried")


def _rows_for(scope: Scope) -> list[dict[str, Any]]:
    """Every credit line in scope. The store replaces this and nothing else."""
    return _FIXTURE


def _short(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["status"] == "short"]


def _rupees(amount: int) -> str:
    """Indian lakh notation, the way the statement itself is read."""
    return f"₹{amount / 100000:.1f}L"


class Form26(Feature):
    key = "form26"
    #: A row is one credit line of the statement.
    row_key = "line"

    def figures(self, scope: Scope) -> list[Figure]:
        rows = _rows_for(scope)
        short = _short(rows)
        at_risk = sum(r["ledger"] - r["credit"] for r in short)
        decided = [r for r in rows if r["status"] in _DECIDED]
        return [
            Figure("lines", str(len(rows)), "in the statement"),
            Figure("matched", str(len(rows) - len(short) - len(decided)),
                   "against the ledger", "good"),
            Figure("mismatches", str(len(short)),
                   "need a decision" if short else "all resolved",
                   "warn" if short else "good"),
            Figure("at_risk", _rupees(at_risk), "short credit value",
                   "bad" if at_risk else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        rows = _rows_for(scope)
        return [
            Tab("mismatches", "Mismatches", len(_short(rows))),
            Tab("all", "All lines", len(rows)),
            Tab("resolved", "Resolved",
                len([r for r in rows if r["status"] in _DECIDED])),
        ]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        rows = _rows_for(scope)
        if tab == "mismatches":
            rows = _short(rows)
        elif tab == "resolved":
            rows = [r for r in rows if r["status"] in _DECIDED]
        # The difference is derived on the way out rather than stored, so it
        # cannot drift from the two numbers it comes from.
        return [dict(r, difference=r["ledger"] - r["credit"]) for r in rows]

    def rules(self, scope: Scope) -> list[str]:
        """Which rules are true now. A closed period says one thing only."""
        if scope.period in _CLOSED:
            return ["period_closed"]
        if scope.user == _PREPARED_BY:
            return ["prepared_by_you"]
        return []

    def act(self, scope: Scope, action: str, targets: list[str]) -> Outcome:
        if scope.period in _CLOSED:
            return Outcome(
                ok=False,
                said=f"{scope.period} was signed off — nothing in it can change.",
            )

        rows = {r["line"]: r for r in _rows_for(scope)}
        chosen = [rows[t] for t in targets if t in rows]
        if not chosen:
            return Outcome(ok=False, said="No line by that number is in view.")

        if action in ("accept", "query"):
            wrong = [r["line"] for r in chosen if r["status"] != "short"]
            if wrong:
                return Outcome(
                    ok=False,
                    said=f"Line {wrong[0]} is not a mismatch, so there is "
                         f"nothing to decide on it.",
                )
            new = "accepted" if action == "accept" else "queried"
            for row in chosen:
                row["status"] = new
            left = len(_short(_rows_for(scope)))
            return Outcome(
                ok=True,
                said=f"{len(chosen)} line{'s' if len(chosen) > 1 else ''} marked "
                     f"{new}. {left} mismatch{'es' if left != 1 else ''} left.",
                touched=[r["line"] for r in chosen],
            )

        if action == "undo":
            wrong = [r["line"] for r in chosen if r["status"] not in _DECIDED]
            if wrong:
                return Outcome(ok=False,
                               said=f"Line {wrong[0]} has not been decided yet.")
            for row in chosen:
                row["status"] = "short"
            return Outcome(
                ok=True,
                said=f"{len(chosen)} line{'s' if len(chosen) > 1 else ''} back "
                     f"in the queue.",
                touched=[r["line"] for r in chosen],
            )

        return Outcome(ok=False, said=f"Form 26 has no action called {action!r}.")


register(Form26())
