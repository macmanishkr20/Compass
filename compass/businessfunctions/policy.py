"""The rules every gift is judged against, and the versions they had.

The annual limit, what counts towards it and the single-gift threshold are
the ONE source of truth: every policy check and every tax sentence reads
them. They were Python constants, which meant the firm's own compliance
rule could only be changed by editing code and deploying — and that nobody
could say what the rule had been last March.

ONE ALLOWANCE, PER PERSON, FIRM-WIDE. There were per-team budgets here
too, and they modelled a firm that does not exist: the business buys
everything through one Procurement team, and the control it actually
applies is a single annual allowance that is the same for every employee
whatever team they are in. A team budget on top of that was a second,
quieter limit that could refuse a gift the real rule allowed. The annual
limit IS the budget now, and `annual_limit` is the only number either
question reads.

THRESHOLDS ARE VERSIONED, NOT EDITED. Setting a new limit does not change
the old one; it adds a version with a time it takes effect from. A record
made before that moment is still judged by the rule that was in force when
it was made, because a gift that was within the limit on the day it was
given does not become a breach because somebody lowered the limit
afterwards — and a breach does not stop being one because somebody raised
it. The register is write-once for the same reason.

So this is append-only, like the trail. `current()` is what a new record is
checked against; `at(when)` is what an old one was checked against; nothing
ever rewrites a version that has been in force.

WHO MAY SET ONE. The `admin` persona, and nobody else. It is the highest
leverage change anybody can make in this module — moving one number
silently reclassifies every future gift in the firm — so it is deliberately
not in the hands of the teams whose own work it judges. Every version
carries who set it and why, and lands on the same audit trail as every
other decision.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from compass.common.persistence.catalog import Collection

logger = logging.getLogger("compass.businessfunctions")

_store = Collection("bf_policy", "business_function_policy.json", shape="map")

#: What the module shipped with. Version 1, in force from the beginning of
#: time, so a deployment that has never set a threshold still has one and
#: `at()` can answer for any record however old.
SHIPPED: dict[str, Any] = {
    "version": 1,
    "annual_limit": 15_000,
    "big": 10_000,
    "counts": ["gift", "award", "voucher"],
    "effective_from": 0.0,
    "by": "shipped with Compass",
    "why": "The rules the module was built against.",
}

#: Which fields a new version may set. Anything else is a key nobody reads,
#: and a threshold nobody reads is worse than no threshold at all.
FIELDS = ("annual_limit", "big", "counts")

_versions: list[dict[str, Any]] = []
_unsaved: list[str] = []
_loaded = False


def versions() -> list[dict[str, Any]]:
    """Every version, oldest first. Includes the shipped one."""
    return _versions or [SHIPPED]


def current() -> dict[str, Any]:
    """What a record made right now is judged against."""
    return at(time.time())


def at(when: float) -> dict[str, Any]:
    """What a record made at `when` was judged against.

    The newest version whose `effective_from` is not in that record's
    future. A record carries its version number, so this is the fallback
    for anything recorded before versions existed.
    """
    in_force = [v for v in versions() if v["effective_from"] <= when]
    return in_force[-1] if in_force else SHIPPED


def version(number: int) -> dict[str, Any]:
    """One version by number, for a record that says which it was judged by."""
    return next((v for v in versions() if v["version"] == number), SHIPPED)


def limit(when: float | None = None) -> int:
    return (at(when) if when is not None else current())["annual_limit"]


def big(when: float | None = None) -> int:
    return (at(when) if when is not None else current())["big"]


def counts(when: float | None = None) -> frozenset[str]:
    return frozenset((at(when) if when is not None else current())["counts"])


def describe(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """What changed between two versions, in words somebody can read."""
    said = []
    if a["annual_limit"] != b["annual_limit"]:
        said.append(f"annual limit ₹{a['annual_limit']:,} → ₹{b['annual_limit']:,}")
    if a["big"] != b["big"]:
        said.append(f"single-gift threshold ₹{a['big']:,} → ₹{b['big']:,}")
    if set(a["counts"]) != set(b["counts"]):
        said.append(f"counts towards the limit: {', '.join(sorted(b['counts']))}")
    return said


def add(*, by: str, why: str, **changes: Any) -> dict[str, Any]:
    """Put a new version in force from now. Never rewrites an old one."""
    if not why.strip():
        raise ValueError("a threshold change has to say why")
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValueError(f"no such threshold: {', '.join(sorted(unknown))}")

    was = current()
    fresh = dict(was, **changes)
    fresh.update({
        "version": max(v["version"] for v in versions()) + 1,
        "effective_from": time.time(),
        "by": by,
        "why": why.strip(),
    })
    if not describe(was, fresh):
        raise ValueError("that is what the thresholds already say")

    # `versions()` FALLS BACK to the shipped rule without storing it, so
    # appending to an empty list would leave v2 as the only version and
    # silently lose the rule v1 records are still judged by. The fallback
    # is materialised before anything is added to it.
    if not _versions:
        _versions.append(dict(SHIPPED))
        _unsaved.append(str(SHIPPED["version"]))
    _versions.append(fresh)
    _unsaved.append(str(fresh["version"]))
    return fresh


# ──────────────────────────────────────────────────────────────────────────
# the store
# ──────────────────────────────────────────────────────────────────────────

async def ready() -> None:
    """Load the versions, once per process. Writes the shipped one if none."""
    global _loaded
    if _loaded:
        return
    stored = await _store.all()
    if not stored:
        await _store.put(dict(SHIPPED, id="1", owner=""))
        stored = await _store.all()
    _versions[:] = sorted(stored, key=lambda v: v["version"])
    _loaded = True
    logger.info("business functions: %d threshold version(s), annual limit "
                "currently ₹%s", len(_versions), f"{current()['annual_limit']:,}")


async def flush() -> None:
    while _unsaved:
        number = _unsaved.pop(0)
        found = next((v for v in _versions if str(v["version"]) == number), None)
        if found is not None:
            await _store.put(dict(found, id=number, owner=found.get("by", "")))


def pending() -> int:
    return len(_unsaved)


def forget() -> None:
    """Drop what is held, so the next `ready` reloads. For checks only."""
    global _loaded
    _versions.clear()
    _unsaved.clear()
    _loaded = False
