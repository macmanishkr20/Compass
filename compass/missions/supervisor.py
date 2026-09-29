"""Deciding whether a mission should run another session.

The agent decides what to build. This decides whether to keep going, and it
exists because the two failure modes of a long-running build are both ones an
agent cannot see from inside a session: it has no memory of the three sessions
that also achieved nothing, and no idea what it has spent.

Four reasons to stop, in the order they are checked:

  finished   every feature passes — the only happy ending
  budget     the money is gone
  sessions   the session cap is reached
  stalled    several sessions in a row moved nothing

`stalled` is the one worth having. An agent that has lost the thread does not
announce it; it keeps working, plausibly, on a feature it will not finish,
and the only evidence is the score not moving. Anthropic's own harness found
the same failure from the other side — a later agent looks around, sees
progress, and declares the job done — and both are invisible from inside one
context window.

Nothing here talks to a model. It reads the score, counts, and says stop or
carry on, which makes every rule in it testable without spending anything.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator

from compass.common.models import events
from compass.missions.engine import Mission, MissionEngine

logger = logging.getLogger("compass.missions")

#: Sessions with no feature flipped before the mission is stopped and handed
#: back. Three rather than one, because a hard feature legitimately takes more
#: than one session, and rather than five because six sessions of nothing is
#: an expensive way to learn the agent is stuck.
STALL_LIMIT = 3

#: A ceiling on sessions regardless of anything else, so a mission whose
#: features can never pass cannot run for a week.
MAX_SESSIONS = 40

#: Sessions that failed outright, in a row, before the mission gives up. A
#: rate limit or a dropped connection is the ordinary weather of a run that
#: lasts hours, so the answer is to wait and try again rather than to stop —
#: but not forever, because a deployment that is out of quota until tomorrow
#: should not be discovered tomorrow.
FAILURE_LIMIT = 5

#: How long to wait after a failed session, doubling each time, capped. The
#: first retry is quick because most rate limits clear in seconds; the cap
#: keeps a long outage from becoming an hour of silence.
BACKOFF_SECONDS = 20.0
BACKOFF_CAP = 300.0

#: How many features may sit claimed before a reviewer is sent in. One means
#: every claim is checked while the work that produced it is still the last
#: thing in the git log — which is when a failure is cheapest to act on.
#: Raising it batches reviews and saves sessions, at the cost of a builder
#: piling work on top of a claim that turns out to be wrong.
REVIEW_AFTER = 1


@dataclass
class Verdict:
    """Whether to run another session, and what it should be."""

    go: bool
    persona: str = "builder"
    reason: str = ""


def blocked(mission: Mission) -> tuple[str, bool]:
    """Why this mission will not start, and whether a person may override it.

    Answered here rather than left to the client to infer, so the screen and
    the supervisor cannot disagree about what a button will do. Returns
    ("", False) when it would start normally.
    """
    verdict = next_step(mission)
    if verdict.go:
        return "", False
    # Overridable exactly when forcing changes the answer — asked rather than
    # re-derived from the wording of the reason, which would be a second copy
    # of the rules to keep in step.
    return verdict.reason, next_step(mission, force=True).go


def next_step(mission: Mission, *, stall_limit: int = STALL_LIMIT,
              max_sessions: int = MAX_SESSIONS,
              review_after: int = REVIEW_AFTER,
              force: bool = False) -> Verdict:
    """What this mission should do next.

    `force` is a person saying "run one more anyway". It skips the three stops
    that are a *judgement* about whether more of the same is worth the money —
    the session cap, repeated failures, and a stall — and skips neither of the
    two that are facts: a finished mission has nothing to do, and an overspent
    one would break the budget the person set.

    The distinction matters because those three exist to protect an unattended
    run. Somebody sitting in front of the screen pressing a button is exactly
    the case they were not written for, and refusing them silently is how a
    working button comes to look broken.
    """
    files = mission.files
    done, total = files.tally()

    if not files.initialised():
        return Verdict(True, "planner", "no plan yet")
    if total and done >= total:
        return Verdict(False, reason=f"finished: all {total} features pass")
    if mission.over_budget():
        return Verdict(False, reason=(f"budget spent: ${mission.spent_usd:.2f} "
                                      f"of ${mission.budget_usd:.2f}"))
    if not force and len(mission.sessions) >= max_sessions:
        return Verdict(False, reason=f"session cap reached ({max_sessions})")

    failures = consecutive_failures(mission)
    if not force and failures >= FAILURE_LIMIT:
        last = mission.sessions[-1].stopped
        return Verdict(False, reason=(
            f"{failures} sessions in a row failed before doing any work — "
            f"last: {last[:120]}"))

    builders = [s for s in mission.sessions if s.persona == "builder"]
    if not force and _stalled(mission, stall_limit):
        return Verdict(False, reason=(
            f"stalled: {stall_limit} sessions in a row moved nothing — the "
            f"score has been {done}/{total} since session "
            f"{builders[-stall_limit].session}"))

    # A claim is a fact on disk, not a guess from the session count: a builder
    # that finished something set `claimed` through `mission_claim`, and until
    # a reviewer rules on it the feature is neither done nor available to
    # build again. Reviewing as soon as anything is waiting also keeps the
    # feedback close to the work that caused it.
    waiting = mission.files.claimed()
    if len(waiting) >= review_after:
        names = ", ".join(f.id for f in waiting[:3])
        return Verdict(True, "reviewer",
                       f"{len(waiting)} claimed and unreviewed: {names}")

    if mission.files.next_to_build() is None:
        # Everything left is claimed — nothing to build until a reviewer rules.
        if waiting:
            return Verdict(True, "reviewer",
                           f"nothing left to build; {len(waiting)} awaiting review")
        return Verdict(False, reason="nothing left to build")
    return Verdict(True, "builder", f"{total - done} features left")


def _stalled(mission: Mission, limit: int) -> bool:
    """True when the last `limit` builder sessions all ended on the same score.

    Sessions that failed outright are not counted. A session killed by a rate
    limit before it ran a single tool has not stalled — it has not happened,
    and treating the two the same stops a mission for being throttled, which
    is the opposite of what should happen to it.
    """
    builders = [s for s in mission.sessions
                if s.persona == "builder" and not s.stopped]
    if len(builders) < limit:
        return False
    recent = builders[-limit:]
    return len({s.passing for s in recent}) == 1


def consecutive_failures(mission: Mission) -> int:
    """How many sessions in a row ended in a failure rather than a finish."""
    count = 0
    for session in reversed(mission.sessions):
        if not session.stopped:
            break
        count += 1
    return count


def backoff_for(failures: int) -> float:
    """How long to wait before trying again, after `failures` in a row."""
    if failures <= 0:
        return 0.0
    return min(BACKOFF_CAP, BACKOFF_SECONDS * (2 ** (failures - 1)))


class Supervisor:
    """Runs sessions until one of the four reasons says stop."""

    def __init__(self, engine: MissionEngine | None = None) -> None:
        self.engine = engine or MissionEngine()
        self._stop = False

    def abort(self) -> None:
        """Stop after the session in flight. Not mid-session: a session killed
        halfway leaves the workspace in the state the next one has to
        untangle, which is the mess the artifacts exist to prevent."""
        self._stop = True

    async def run(self, mission: Mission, *, force: bool = False) -> AsyncIterator[object]:
        """Session after session, streaming everything as it happens.

        `force` applies to the first decision only, and then the ordinary
        rules come back. One more session is a person overriding a judgement;
        an indefinite override is that person accidentally turning the guard
        off, and the guard is what stops a stalled mission spending the rest
        of its budget going nowhere.
        """
        first = True
        while True:
            if self._stop:
                yield _note(f"mission {mission.id} stopped on request",
                            len(mission.sessions))
                return
            verdict = next_step(mission, force=force and first)
            first = False
            if not verdict.go:
                yield _note(f"mission {mission.id} ended — {verdict.reason}")
                return

            number = len(mission.sessions) + 1
            yield _note(f"session {number} ({verdict.persona}): {verdict.reason}",
                        number)
            logger.info("mission %s session %d as %s — %s", mission.id, number,
                        verdict.persona, verdict.reason)
            async for event in self.engine.run_session(mission,
                                                       persona=verdict.persona):
                yield event

            # A failure is not a reason to stop, but it is a reason to wait:
            # retrying a rate limit immediately earns another rate limit.
            pause = backoff_for(consecutive_failures(mission))
            if pause:
                yield _note(f"waiting {pause:.0f}s before trying again — "
                            f"{mission.sessions[-1].stopped[:90]}", number)
                await asyncio.sleep(pause)

            done, total = mission.files.tally()
            last = mission.sessions[-1] if mission.sessions else None
            spent = f"${mission.spent_usd:.2f}"
            yield _note(f"session {number} done — {done}/{total} features, "
                        f"{spent} spent so far"
                        + (f" — {last.stopped}" if last and last.stopped else ""),
                        number)


@dataclass
class MissionNote:
    """Supervisor commentary — which session is starting, why, and how the
    mission stands afterwards.

    Its own event rather than an `ErrorEvent` carrying good news, and local to
    this module rather than added to the shared events, following the same
    reasoning as Home's Work IQ event: nothing about the agent console's
    stream should change because a new surface exists. A client that does not
    know the type ignores it.
    """

    message: str
    session: int = 0

    def to_sse(self) -> str:
        import json

        payload = json.dumps({"type": "mission_note", "message": self.message,
                              "session": self.session})
        return f"event: mission_note\ndata: {payload}\n\n"


def _note(text: str, session: int = 0) -> MissionNote:
    return MissionNote(message=text, session=session)
