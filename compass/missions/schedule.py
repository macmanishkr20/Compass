"""Starting a mission when nobody is there to press the button.

The point of the whole harness is work that takes hours, and hours are cheaper
overnight than during the day: a build that runs from two in the morning is
one you read over coffee rather than one you sit through.

The schedule itself is Routines' — `Trigger`, `_prev_fire` and their timezone
handling are already written, already tested against real clock edges, and a
second implementation of "was this due in the last minute" is a second set of
bugs around daylight saving. This module is the loop and the judgement about
what may start.

Two rules about starting, both of which exist because this spends money while
you are asleep:

  * a mission that is finished, over budget or stalled is never started, even
    when its schedule says so — the supervisor's reasons to stop outrank the
    clock;
  * a slot that has already fired does not fire again, so a restart at 03:00
    does not re-run the two o'clock mission.
"""

from __future__ import annotations

import asyncio
import logging
import time

from compass.code.routines import Trigger, _prev_fire
from compass.missions.engine import MissionEngine
from compass.missions.runner import get_runners
from compass.missions.store import get_mission_store
from compass.missions.supervisor import next_step

logger = logging.getLogger("compass.missions")

#: How often the loop looks at the clock. The same cadence Routines uses; a
#: slot is claimed if it fell within the last few minutes, so a tick this slow
#: cannot miss one.
TICK_SECONDS = 15.0

#: How far back a due slot is still honoured. Long enough to survive a tick
#: being late or a brief restart, short enough that a server started hours
#: later does not fire the night's work at lunchtime.
GRACE_SECONDS = 300.0


def parse_triggers(rows: list[dict] | None) -> list[Trigger]:
    """Triggers from what the API was given, ignoring what it cannot read."""
    out: list[Trigger] = []
    for row in rows or []:
        try:
            out.append(Trigger(**{k: v for k, v in row.items()
                                  if k in Trigger.__dataclass_fields__}))
        except Exception:  # noqa: BLE001 — a bad trigger is not a bad mission
            logger.warning("mission schedule: ignoring %r", row)
    return out


def due_now(triggers: list[Trigger], *, now: float | None = None,
            grace: float = GRACE_SECONDS) -> float | None:
    """The slot that just came due, or None.

    Returns the slot's own time rather than True, because that is what the
    caller records to stop the same slot firing twice.
    """
    now = time.time() if now is None else now
    slots = [s for s in (_prev_fire(t) for t in triggers) if s is not None]
    recent = [s for s in slots if 0 <= now - s <= grace]
    return max(recent) if recent else None


def may_start(mission) -> tuple[bool, str]:
    """Whether the clock is allowed to start this one.

    The supervisor already knows every reason a mission should not run another
    session — finished, out of budget, stalled, capped. Asking it here means
    the schedule cannot restart a mission that stopped for a good reason, and
    that there is one place where those reasons live.
    """
    verdict = next_step(mission)
    if not verdict.go:
        return False, verdict.reason
    if get_runners().running(mission.id):
        return False, "already running"
    return True, verdict.reason


async def scheduler_loop() -> None:
    """Start scheduled missions as their slots arrive."""
    fired: dict[str, float] = {}
    logger.info("mission scheduler started")
    store = get_mission_store()
    while True:
        try:
            for mission in store.list():
                triggers = parse_triggers(getattr(mission, "triggers", None))
                if not triggers:
                    continue
                slot = due_now(triggers)
                if slot is None or fired.get(mission.id) == slot:
                    continue
                allowed, reason = may_start(mission)
                fired[mission.id] = slot
                if not allowed:
                    logger.info("mission %s was due but not started: %s",
                                mission.id, reason)
                    continue
                logger.info("mission %s starting on schedule — %s",
                            mission.id, reason)
                get_runners().start(mission, MissionEngine(), store.save)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad tick must not end the loop
            logger.exception("mission scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
