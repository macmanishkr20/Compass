"""The register and the trail: what was given, and what was done about it.

Two things the requirement names separately, because they are different:

    the REGISTER   what the firm gave and to whom. One record per item,
                   written once and never altered.
    the TRAIL      what people did about those records — reviewed, queried,
                   answered, superseded, told. Appended, never edited.

Keeping them apart is not tidiness. The invariant this feature has defended
since the disclosure form is that a record does not change after the fact: a
correction supersedes rather than edits, and a breach does not un-receive a
gift. A store that let a decision write back onto the item would quietly
undo that, and the only sign would be an audit that no longer reconciles.

So state is a FOLD. Whether an item is accepted, queried, answered or
superseded is not stored anywhere — it is read off the events about it, in
order. There is one way for a fact to be true, which is that something
happened to make it true, and the thing that happened is still there.

MEMORY IS AUTHORITATIVE WITHIN THE PROCESS; THE STORE IS AUTHORITATIVE
ACROSS RESTARTS. `Feature.figures`, `rows` and `act` are synchronous and the
store is not, so a handler cannot await. Rather than make the whole feature
contract async — which would push `asyncio` into every handler, every check
and the workbook builder — writes land in memory immediately and are
flushed to the store by the route that caused them, the same way notices
are delivered. Compass runs one process, so there is no second writer to
diverge from, which is the same bargain `Collection` itself strikes with
its Cosmos cache.

WHAT THAT COSTS, said plainly: a crash between the write and the flush, a
window of under a millisecond in the same request, loses the last decision.
The fix is an async feature contract, and it is worth doing when the
contract is next opened rather than as a change nobody asked for in the
middle of this one.

PLANS ARE NOT PERSISTED, deliberately. A proposal in the assistant is a few
seconds of intent, and one that outlived the screen it was made against
would be a plan confirmed against rows that have moved. Losing those on
restart is correct and stays that way.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from compass.common.persistence.catalog import Collection

logger = logging.getLogger("compass.businessfunctions")

#: What the firm gave. Written once; nothing here updates one.
_items_store = Collection("bf_gift_item", "business_function_gift_items.json",
                          shape="map")

#: What was done about it. Append-only.
_events_store = Collection("bf_gift_event", "business_function_gift_events.json",
                           shape="map")

#: What somebody has ASKED to give, before it exists. A request is not a
#: record of something that happened — it is a proposal that may be
#: declined, and conflating the two would put things on the register that
#: nobody ever received. It becomes an item when it is handed over.
_requests_store = Collection("bf_gift_request",
                             "business_function_gift_requests.json", shape="map")

#: The kinds of thing that happen. A closed set, because an event nobody
#: can fold is an event that silently does nothing.
#:
#: Some are about an ITEM — a declaration reviewed, answered, superseded —
#: and some are about a PERSON's year: referred to Finance, an exception
#: accepted, an acknowledgement chased. Both are decisions somebody took and
#: has to answer for, so both are on the same trail, under whatever they
#: were about. Keeping the second kind in two module-level sets, as this did
#: at first, meant the thing Audit most wants to read — who referred whom,
#: and when — did not survive a restart.
EVENTS = ("reviewed", "answered", "superseded", "notified",
          "referred", "excepted", "chased",
          # the request lifecycle
          "requested", "approved", "declined", "purchased", "given",
          "acknowledged", "cancelled")

#: In-process state. Authoritative for reads; see the note above.
_items: list[dict[str, Any]] = []
_events: list[dict[str, Any]] = []
_requests: list[dict[str, Any]] = []

#: Written but not yet flushed. Ids, so a double flush is not a double write.
_unsaved_items: list[str] = []
_unsaved_events: list[str] = []
_unsaved_requests: list[str] = []

_loaded = False


# ──────────────────────────────────────────────────────────────────────────
# reading — synchronous, because handlers are
# ──────────────────────────────────────────────────────────────────────────

def items() -> list[dict[str, Any]]:
    """Every recorded item, superseded ones included. This is the register."""
    return _items


def requests() -> list[dict[str, Any]]:
    """Everything that has been asked for, in whatever state."""
    return _requests


def events(subject: str = "") -> list[dict[str, Any]]:
    """The trail, oldest first — for one subject, or all of it."""
    if not subject:
        return list(_events)
    return [e for e in _events if e["subject"] == subject]


def latest(subject: str, kind: str) -> dict[str, Any] | None:
    """The most recent event of a kind about one subject, or None."""
    found = [e for e in _events if e["subject"] == subject and e["kind"] == kind]
    return found[-1] if found else None


def happened(subject: str, kind: str) -> bool:
    """Whether this ever happened to this subject."""
    return latest(subject, kind) is not None


def since_last(subject: str, kind: str, after: str) -> dict[str, Any] | None:
    """The latest `kind` event that happened after the latest `after` one.

    What makes an answer current: a reply given before the question was
    asked again is a reply to a question nobody is asking. Folded rather
    than cleared, so the superseded answer is still on the trail.
    """
    mine = [e for e in _events if e["subject"] == subject]
    cut = max((i for i, e in enumerate(mine) if e["kind"] == after), default=-1)
    later = [e for e in mine[cut + 1:] if e["kind"] == kind]
    return later[-1] if later else None


# ──────────────────────────────────────────────────────────────────────────
# writing — synchronous into memory, flushed by the route
# ──────────────────────────────────────────────────────────────────────────

def add_item(item: dict[str, Any]) -> dict[str, Any]:
    """Record something the firm gave. Never called twice for one id."""
    _items.append(item)
    _unsaved_items.append(item["id"])
    return item


def add_request(request: dict[str, Any]) -> dict[str, Any]:
    """Ask for something. Changes nothing on the register until it is given."""
    _requests.append(request)
    _unsaved_requests.append(request["id"])
    return request


def touch_request(request_id: str) -> None:
    """Mark a request as changed, so the next flush writes it.

    Requests are the one thing here that is edited rather than appended:
    a request moves through states, and its state IS the record. What
    happened to it is still on the trail, which is where the history lives.
    """
    if request_id not in _unsaved_requests:
        _unsaved_requests.append(request_id)


def touch_item(item_id: str) -> None:
    """Mark a record as changed, so the next flush writes it.

    The one field on an item that moves: whether the recipient has
    confirmed receiving it. It is theirs to set and nobody else's, and the
    acknowledgement itself is on the trail — this only keeps the register
    and the trail saying the same thing.
    """
    if item_id not in _unsaved_items:
        _unsaved_items.append(item_id)


def add_event(*, subject: str, kind: str, by: str,
              **payload: Any) -> dict[str, Any]:
    """Record something somebody did — to an item, or to a person's year."""
    if kind not in EVENTS:
        raise ValueError(f"no such event: {kind!r}")
    event = {"id": uuid.uuid4().hex[:12], "subject": subject, "kind": kind,
             "by": by, "at": time.time(), **payload}
    _events.append(event)
    _unsaved_events.append(event["id"])
    return event


# ──────────────────────────────────────────────────────────────────────────
# the store
# ──────────────────────────────────────────────────────────────────────────

async def ready(seed: list[dict[str, Any]] | None = None,
                requests_seed: list[dict[str, Any]] | None = None) -> None:
    """Load the register and the trail, once per process.

    `seed` is written only when the register is EMPTY — a deployment with
    nothing in it gets the sample the screens were built against, and one
    with records keeps them. Seeding on every start would overwrite a year
    of declarations with a fixture, which is the kind of mistake that is
    only discovered later.
    """
    global _loaded
    if _loaded:
        return

    stored = await _items_store.all()
    if not stored and seed:
        logger.info("business functions: seeding an empty gift register with "
                    "%d sample items", len(seed))
        for item in seed:
            await _items_store.put(dict(item, owner=item.get("employee_id", "")))
        stored = await _items_store.all()

    _items[:] = sorted(stored, key=lambda i: (i.get("given", ""), i.get("id", "")))
    _events[:] = sorted(await _events_store.all(), key=lambda e: e.get("at", 0))
    waiting = await _requests_store.all()
    if not waiting and requests_seed:
        for request in requests_seed:
            await _requests_store.put(dict(request, owner=request.get("by", "")))
        waiting = await _requests_store.all()
    _requests[:] = sorted(waiting, key=lambda r: r.get("at", 0))
    _loaded = True
    logger.info("business functions: %d gift records, %d events, %d requests",
                len(_items), len(_events), len(_requests))


async def flush() -> None:
    """Persist what the last request recorded.

    Called after the action, not before: a store that is slow or unhappy
    delays a reply rather than undoing a decision somebody made, and the
    decision is already true in this process either way.
    """
    while _unsaved_items:
        item_id = _unsaved_items.pop(0)
        item = next((i for i in _items if i["id"] == item_id), None)
        if item is not None:
            await _items_store.put(dict(item, owner=item.get("employee_id", "")))
    while _unsaved_requests:
        request_id = _unsaved_requests.pop(0)
        request = next((r for r in _requests if r["id"] == request_id), None)
        if request is not None:
            await _requests_store.put(dict(request, owner=request.get("by", "")))
    while _unsaved_events:
        event_id = _unsaved_events.pop(0)
        event = next((e for e in _events if e["id"] == event_id), None)
        if event is not None:
            await _events_store.put(dict(event, owner=event.get("by", "")))


def pending() -> int:
    """How much has not reached the store. For checks and a health line."""
    return (len(_unsaved_items) + len(_unsaved_events)
            + len(_unsaved_requests))


def forget() -> None:
    """Drop what is held, so the next `ready` reloads. For checks only."""
    global _loaded
    _items.clear()
    _events.clear()
    _requests.clear()
    _unsaved_items.clear()
    _unsaved_events.clear()
    _unsaved_requests.clear()
    _loaded = False
