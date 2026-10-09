"""The outbox: what a business function told somebody, and whether it arrived.

A NOTICE IS A RECORD, NOT A SIDE EFFECT. "We asked them on the 9th" is a
thing a compliance screen has to be able to say, and a screen cannot say it
if the only trace is a line in some mail server's log. So every notice is
written down first — who it was for, what it said, which row it was about —
and delivery is an attempt made against that record afterwards.

That ordering is the whole design. It means:

  * a notice nobody could deliver is visible rather than lost. With no mail
    server configured, the notice is HELD and says so, on the row. A reviewer
    who believes somebody was emailed and nobody was is the failure this
    feature exists to prevent, and it is exactly what "fire and forget would
    have been simpler" buys you.
  * the audit trail survives the mail server. Whether it sent, when, and what
    went wrong are properties of the record.
  * it can be retried, because there is something to retry.

NEVER TO WHOEVER RUNS THE SERVER. `compass.code.notify.send_email` falls back
to `COMPASS_NOTIFY_EMAIL` when given no recipient — right for "your routine
finished", wrong here, where it would post one employee's gift record to the
operator. So a notice with no address is held, never sent, and this module
passes `to=` on every call.

The store is in memory, newest kept. That is the same decision the assistant's
plans make and for a weaker reason: these belong in the store when it lands,
and losing them on restart loses an audit trail, which is a real cost. It is
recorded here rather than pretended away.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Literal

from compass.code import notify

logger = logging.getLogger("compass.businessfunctions")

#: How many notices to keep. Enough that a screen can show the history of
#: anything still open; small enough to be a dict in a process.
KEEP = 500

#: Caps on what a notice may carry. A subject is a line and a body is a short
#: message; anything longer is a document and does not belong in an email a
#: feature generates.
SUBJECT_MAX = 160
BODY_MAX = 4000

State = Literal["pending", "sent", "held", "failed"]

#: What each state means where somebody can read it.
WORDS: dict[str, str] = {
    "pending": "not sent yet",
    "sent": "emailed",
    "held": "not emailed",
    "failed": "email failed",
}


@dataclass(frozen=True)
class Notice:
    """One message a feature meant to send, and what became of it."""

    id: str
    #: The address. Empty means nobody knew one, which is a held notice and
    #: never a send to somebody else.
    to: str
    #: Who it was for, in words, for the screen. Not the address.
    name: str
    subject: str
    body: str
    #: The row this is about, so a screen can ask "was this person told?".
    about: str
    state: State = "pending"
    #: Why it is held or failed, in a sentence somebody can act on.
    detail: str = ""
    made_at: float = field(default_factory=time.time)
    sent_at: float = 0.0

    @property
    def said(self) -> str:
        """How this reads on a row."""
        if self.state == "sent":
            return f"emailed {self.name}"
        if self.state == "pending":
            return f"queued for {self.name}"
        return f"{WORDS[self.state]} — {self.detail}"


#: notice id -> notice. Insertion-ordered, which is also oldest-first.
_outbox: dict[str, Notice] = {}


def configured() -> bool:
    """Whether a notice could be delivered at all right now."""
    return notify.email_configured()


def _trim() -> None:
    while len(_outbox) > KEEP:
        _outbox.pop(next(iter(_outbox)))


def record(*, to: str, name: str, subject: str, body: str, about: str) -> Notice:
    """Write down that somebody is to be told. Sends nothing.

    A notice with no address, or with nothing to say, is held at the moment it
    is made rather than attempted and failed: there was never a send to make,
    and the reason a person needs is "nobody knows their address", not
    "delivery failed".
    """
    subject = subject.strip()[:SUBJECT_MAX]
    body = body.strip()[:BODY_MAX]

    notice = Notice(id=uuid.uuid4().hex[:12], to=to.strip(), name=name or "them",
                    subject=subject, body=body, about=about)
    if not notice.to:
        notice = replace(notice, state="held",
                         detail=f"no email address on file for {notice.name}")
    elif not subject or not body:
        notice = replace(notice, state="held", detail="nothing to say")
    elif not configured():
        notice = replace(
            notice, state="held",
            detail="no mail server configured (COMPASS_SMTP_HOST, "
                   "COMPASS_SMTP_USER, COMPASS_SMTP_PASSWORD)",
        )

    _outbox[notice.id] = notice
    _trim()
    logger.info("notice %s for %s about %s: %s",
                notice.id, notice.name, notice.about, notice.state)
    return notice


def for_item(about: str) -> list[Notice]:
    """Every notice about one row, newest last."""
    return [n for n in _outbox.values() if n.about == about]


def latest_for(about: str) -> Notice | None:
    """The most recent notice about one row, which is what a row shows."""
    found = for_item(about)
    return found[-1] if found else None


def retry(about: str) -> int:
    """Put held or failed notices about one row back in the queue.

    Held is the ordinary case: somebody configured a mail server after the
    notice was made. The notice itself is not rewritten — what was said does
    not change because the sending of it is being tried again.
    """
    count = 0
    for key, notice in list(_outbox.items()):
        if notice.about == about and notice.state in ("held", "failed"):
            _outbox[key] = replace(notice, state="pending", detail="")
            count += 1
    return count


def flush() -> tuple[int, int]:
    """Attempt every pending notice. Returns (sent, held or failed).

    BLOCKS: this talks to a mail server. Callers on an event loop run it in a
    thread — `asyncio.to_thread(notices.flush)` — because a 20-second SMTP
    timeout inside a request is a 20-second request.

    Called after the thing that caused the notice has already been recorded,
    so a mail server that is down slows a reply down and does not undo a
    decision somebody made.
    """
    sent = stuck = 0
    for key, notice in list(_outbox.items()):
        if notice.state != "pending":
            continue
        if not configured():
            _outbox[key] = replace(
                notice, state="held",
                detail="no mail server configured (COMPASS_SMTP_HOST, "
                       "COMPASS_SMTP_USER, COMPASS_SMTP_PASSWORD)",
            )
            stuck += 1
            continue
        # `to=` always, explicitly. Without it this falls back to the
        # operator's own address — see the note at the top of this file.
        if notify.send_email(notice.subject, notice.body, to=notice.to):
            _outbox[key] = replace(notice, state="sent", detail="",
                                   sent_at=time.time())
            sent += 1
        else:
            _outbox[key] = replace(
                notice, state="failed",
                detail="the mail server would not take it; the server log has "
                       "the reason",
            )
            stuck += 1
    return sent, stuck


def waiting() -> int:
    """How many notices have not been delivered. For checks and a health line."""
    return len([n for n in _outbox.values() if n.state != "sent"])


def clear() -> None:
    """Empty the outbox. For checks; nothing in the app calls this."""
    _outbox.clear()
