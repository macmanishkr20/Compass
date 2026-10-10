"""Monthly reports: a screen, read on a date, by somebody who is not there.

This is the first thing in the section that runs with nobody present, and
unattended work that sends email is where "it quietly mailed the wrong
people for six weeks" lives. So three rules shape the whole module, and the
features it does not have are as deliberate as the ones it does.

IT RUNS AS A PERSON, NOT AS THE SERVER. A subscription records whose eyes
it is read through, and every figure in it comes from the same handler call
that person's own screen makes, with `Scope(user=...)` set to them. There is
no privileged render. If they lose the compliance role tomorrow, next
month's report is empty — not because anything here checks, but because it
is the same code path that would show them an empty screen. A scheduled job
that could read more than the person it reports to is a way of getting data
you are not allowed to see, with a month's delay.

YOU CAN ONLY SUBSCRIBE YOURSELF. There is no recipient list and no "send
this to the board" field. The identity comes from the session, the address
comes from the directory, and a second person who wants it subscribes
themselves. A list would mean one person's click causes mail to somebody
who never asked for it and may not be allowed to see it, which is the
failure this design removes rather than guards.

IT SENDS A SUMMARY, NOT THE SPREADSHEET. The figures and what is waiting go
in the mail; the workbook stays behind the login. Attaching it would put
every employee's gift record in an inbox on a schedule, where it is
forwarded, archived and searched by things that are not Compass. The mail
says what the numbers are and where to get the detail, which is what
somebody reads on a phone anyway.

EVERY RUN IS RECORDED, with the figures as they were. "The November report
said eight thousand" has to be answerable later; a mail server's log does
not answer it, and neither does the screen, which has moved on. The runs
are stored with the subscription, so the answer survives a deploy — which
is the whole point of a standing request outliving the process that took
it.

Monthly is not one of Routines' trigger types and its clock logic is not
reimplemented here — `_now_local` and `_to_epoch` carry the timezone, and
this adds only the day-of-month arithmetic, which is a clamp for the months
that have no 31st.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import time
import uuid
from dataclasses import dataclass, field, replace

from compass.businessfunctions import notices, registry
from compass.common.persistence.catalog import Collection
from compass.businessfunctions.features import rewardlens
from compass.businessfunctions.features.base import Scope
from compass.code.routines import TZ_LABEL, _now_local, _to_epoch

logger = logging.getLogger("compass.businessfunctions")

#: How often the loop looks at the clock.
TICK_SECONDS = 60.0

#: How late a monthly slot may still be honoured. A day, not the five
#: minutes the mission scheduler allows: a report that arrives on the
#: afternoon of the 1st because the server was restarted that morning is
#: fine, and one that never arrives because of it is not.
GRACE_SECONDS = 24 * 3600.0

#: How many runs to remember per subscription. Enough to answer "what did we
#: say in the spring"; small enough to be a list in a process.
KEEP_RUNS = 24


@dataclass(frozen=True)
class Run:
    """One firing: when, what it covered, what it said, and whether it went."""

    at: float
    period: str
    #: The headline figures as they were at that moment, so the report can be
    #: reconstructed after the screen has moved on.
    figures: list[tuple[str, str]]
    notice_id: str = ""
    said: str = ""


@dataclass(frozen=True)
class Subscription:
    """One person's standing request for one screen, once a month."""

    id: str
    #: Whose eyes it is read through, and the only person it is sent to.
    user: str
    function_id: str
    feature_id: str
    #: Which period the report covers. A year, for a screen scoped by one.
    period: str
    #: 1–31, clamped to the last day in months that are shorter.
    day: int = 1
    #: HH:MM in the deployment's timezone. Not called `time`: inside a
    #: dataclass body that name shadows the module, and the failure is an
    #: AttributeError on `time.time` at import.
    time_of_day: str = "08:00"
    runs: list[Run] = field(default_factory=list)
    made_at: float = field(default_factory=time.time)

    @property
    def summary(self) -> str:
        which = {1: "1st", 2: "2nd", 3: "3rd", 21: "21st", 22: "22nd",
                 23: "23rd", 31: "31st"}.get(self.day, f"{self.day}th")
        return (f"Emailed on the {which} of each month at "
                f"{self.time_of_day} {TZ_LABEL}")


#: Where they live. The same small whole-document collection the rest of
#: Compass uses for configuration-shaped records — JSON on a box, Cosmos on
#: Azure — rather than a second store written for one feature.
#:
#: Partitioned by owner, which for a subscription is the person it reports
#: to: the only identity it has and the only one allowed to change it.
_store = Collection("bf_report", "business_function_reports.json", shape="map")


def _to_doc(sub: Subscription) -> dict:
    return {
        "id": sub.id,
        # The partition key, and the same value as `user`. Stored under the
        # name the collection expects rather than teaching the collection
        # about this feature.
        "owner": sub.user,
        "user": sub.user,
        "function_id": sub.function_id,
        "feature_id": sub.feature_id,
        "period": sub.period,
        "day": sub.day,
        "time_of_day": sub.time_of_day,
        "made_at": sub.made_at,
        "runs": [
            {"at": r.at, "period": r.period, "notice_id": r.notice_id,
             "said": r.said, "figures": [list(f) for f in r.figures]}
            for r in sub.runs
        ],
    }


def _from_doc(doc: dict) -> Subscription | None:
    """A stored record, or None when it is not one.

    A row this cannot read is skipped with a warning rather than taken down
    the whole feature: the same bargain the collection itself strikes with an
    unreadable file.
    """
    try:
        return Subscription(
            id=str(doc["id"]),
            user=str(doc["user"]),
            function_id=str(doc["function_id"]),
            feature_id=str(doc["feature_id"]),
            period=str(doc.get("period", "")),
            day=int(doc.get("day", 1)),
            time_of_day=str(doc.get("time_of_day", "08:00")),
            made_at=float(doc.get("made_at", 0.0)),
            runs=[
                Run(at=float(r["at"]), period=str(r.get("period", "")),
                    figures=[(str(a), str(b)) for a, b in r.get("figures", [])],
                    notice_id=str(r.get("notice_id", "")),
                    said=str(r.get("said", "")))
                for r in doc.get("runs", []) if isinstance(r, dict) and "at" in r
            ],
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("skipping unreadable report subscription %r",
                       doc.get("id"))
        return None


def _slot(sub: Subscription, *, now: dt.datetime | None = None) -> float | None:
    """The most recent moment this subscription was due, at or before now.

    The same shape as Routines' `_prev_fire` and for the same reason: a slot
    that has already passed is what tells the loop one arrived, where a next
    firing is always in the future and can never equal now.
    """
    here = _now_local() if now is None else now
    try:
        hh, mm = (int(x) for x in sub.time_of_day.split(":"))
    except ValueError:
        hh, mm = 8, 0

    def on(year: int, month: int) -> dt.datetime:
        # The 31st of a 30-day month is the 30th, not a skipped month. A
        # subscription that silently never fires is worse than one that
        # fires a day early.
        day = min(sub.day, calendar.monthrange(year, month)[1])
        return dt.datetime(year, month, day, hh, mm)

    candidate = on(here.year, here.month)
    if candidate > here:
        year, month = (here.year - 1, 12) if here.month == 1 else (here.year,
                                                                   here.month - 1)
        candidate = on(year, month)
    return _to_epoch(candidate)


def due(sub: Subscription, *, now: float | None = None) -> float | None:
    """The slot that just came due and has not been run, or None."""
    slot = _slot(sub)
    if slot is None:
        return None
    moment = time.time() if now is None else now
    if not (0 <= moment - slot <= GRACE_SECONDS):
        return None
    if any(abs(run.at - slot) < 1.0 for run in sub.runs):
        return None
    return slot


# ──────────────────────────────────────────────────────────────────────────
# the report itself
# ──────────────────────────────────────────────────────────────────────────

def render(sub: Subscription) -> tuple[str, str, list[tuple[str, str]]]:
    """Subject, body and the figures, as this person's own screen shows them.

    Built through `Scope(user=sub.user)` and the ordinary handler, so this
    cannot see more than they can. A report that read the data another way
    would be a second path to it, and the whole point of the role check on
    the screen is that there is not one.
    """
    fn, feature, handler = registry.feature_of(sub.function_id, sub.feature_id)
    if fn is None or feature is None or handler is None:
        return "", "", []

    scope = Scope(user=sub.user, period=sub.period)
    figures = [(f.caption.capitalize(), f.value) for f in handler.figures(scope)]
    applies = set(handler.rules(scope))
    said = [r for r in feature.rules if r.id in applies]

    subject = f"{feature.name} — {sub.period}"
    lines = [
        f"{fn.name} · {feature.name}",
        f"{sub.period}, as at {dt.datetime.now().astimezone():%Y-%m-%d %H:%M}.",
        "",
    ]
    if not figures:
        # The honest empty report: it fired, and there was nothing to tell
        # this person. Usually it means they no longer hold the role.
        lines += [
            "There is nothing to report to you for this period.",
            "",
            "If you expected figures here, your access may have changed —",
            "this is read with exactly the permissions you have in Compass.",
        ]
        return subject, "\n".join(lines), []

    width = max(len(caption) for caption, _ in figures)
    lines += [f"  {caption.ljust(width)}   {value}" for caption, value in figures]
    if said:
        lines += ["", "In force:"]
        lines += [f"  · {rule.headline}" for rule in said]
    lines += [
        "",
        "The detail is in Compass, under "
        f"{fn.name} › {feature.name}, where it can also be exported.",
        "",
        "You are getting this because you asked for it monthly. Turn it off "
        "on the same screen.",
    ]
    return subject, "\n".join(lines), figures


def run(sub: Subscription) -> Subscription:
    """Generate and queue one report, with the run recorded on what it
    returns. Sends nothing — `notices.flush` does — and writes nothing: the
    caller saves, because rendering is sync and the store is not."""
    subject, body, figures = render(sub)
    if not subject:
        logger.warning("report %s: %s/%s no longer exists",
                       sub.id, sub.function_id, sub.feature_id)
        return sub

    notice = notices.record(
        to=rewardlens.address_of(sub.user),
        name=sub.user,
        subject=subject,
        body=body,
        about=f"report:{sub.id}",
    )
    record = Run(at=_slot(sub) or time.time(), period=sub.period,
                 figures=figures, notice_id=notice.id, said=notice.said)
    logger.info("report %s ran for %s: %s", sub.id, sub.user, notice.said)
    return replace(sub, runs=(sub.runs + [record])[-KEEP_RUNS:])


# ──────────────────────────────────────────────────────────────────────────
# the standing request
# ──────────────────────────────────────────────────────────────────────────

async def for_user(user: str, function_id: str,
                   feature_id: str) -> Subscription | None:
    """This person's subscription to this screen, if they have one.

    Matched on the user as well as the screen, so this is also the filter
    that stops one person reading another's: nothing above this looks a
    subscription up by id alone.
    """
    return next((s for s in await all_subscriptions()
                 if s.user == user and s.function_id == function_id
                 and s.feature_id == feature_id), None)


async def save(sub: Subscription) -> Subscription:
    """Write one down. Used after a run as well as after a change."""
    await _store.put(_to_doc(sub))
    return sub


async def subscribe(*, user: str, function_id: str, feature_id: str,
                    period: str, day: int = 1,
                    time_of_day: str = "08:00") -> Subscription:
    """Start or replace this person's monthly report for one screen.

    `user` is the caller, taken from the session by the route and never from
    a body. Replacing rather than adding means clicking twice leaves one
    subscription, not two reports on the same morning — and it keeps the
    runs, because the history belongs to the standing request rather than to
    the particular settings it had at the time.
    """
    existing = await for_user(user, function_id, feature_id)
    return await save(Subscription(
        id=existing.id if existing else uuid.uuid4().hex[:12],
        user=user, function_id=function_id, feature_id=feature_id,
        period=period, day=max(1, min(31, day)),
        time_of_day=time_of_day,
        runs=existing.runs if existing else [],
    ))


async def unsubscribe(user: str, function_id: str, feature_id: str) -> bool:
    """Stop your own. Looked up by user first, so there is no shape of call
    here that removes somebody else's."""
    sub = await for_user(user, function_id, feature_id)
    if sub is None:
        return False
    return await _store.remove(sub.id)


async def all_subscriptions() -> list[Subscription]:
    """Every standing request, for the loop. Unreadable rows are skipped."""
    rows = [_from_doc(doc) for doc in await _store.all()]
    return [sub for sub in rows if sub is not None]


async def clear() -> None:
    """Forget every subscription. For checks; nothing in the app calls it."""
    await _store.replace_all([])


# ──────────────────────────────────────────────────────────────────────────
# the loop
# ──────────────────────────────────────────────────────────────────────────

async def reports_loop() -> None:
    """Send what is due, once a minute.

    Rendering and SMTP both block, so both go to a thread. A report that
    fails is logged and the slot is still recorded as run: retrying a
    monthly report every minute for a day would be a day of mail, and the
    notice it left behind already says it did not go.
    """
    logger.info("business-function reports started")
    while True:
        try:
            for sub in await all_subscriptions():
                if due(sub) is None:
                    continue
                # Rendering reads a whole screen, so it goes to a thread;
                # storing is the collection's own async. The run is written
                # down before the mail is attempted, so a send that fails
                # cannot make the same slot fire again on the next tick — a
                # monthly report retried every minute is a day of mail.
                await save(await asyncio.to_thread(run, sub))
            if any(n.state == "pending" for n in notices._outbox.values()):
                await asyncio.to_thread(notices.flush)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad month is not the end of the loop
            logger.warning("business-function reports: tick failed", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
