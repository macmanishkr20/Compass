"""Running a mission without anybody watching it.

Until now a mission ran *inside* the HTTP response that started it: the
supervisor was driven by the SSE generator, so the work only advanced while a
browser was reading. Closing the tab cancelled the generator and killed the
session mid-flight — measured, not supposed: a mission started and abandoned
after one frame recorded zero sessions and spent nothing, with the planner
stopped halfway through writing the plan.

For a feature whose whole claim is "works for hours", that is the bug. So the
mission runs as a task the server owns, and the stream becomes a window onto
it. Closing the window does not stop the work, two windows can watch the same
mission, and a window opened late is shown what it missed.

What is deliberately *not* here: restarting a mission when the server boots. A
process that spends money on its own after a crash, with nobody having asked
it to, is not a feature. A mission interrupted by a restart is left where it
stopped, and its artifacts are what makes picking it up cheap.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from compass.common.tools import reaper
from compass.missions.engine import Mission, MissionEngine
from compass.missions.supervisor import MissionNote, Supervisor

logger = logging.getLogger("compass.missions")

#: Events kept for a window that opens late. Enough to show how the mission
#: got here — the supervisor's notes are a handful per session — without
#: holding an hour of tool calls in memory.
REPLAY = 200

#: A slow reader must not hold up the mission. Past this many unread events a
#: subscriber is dropped: the work matters, watching it does not.
SUBSCRIBER_BACKLOG = 500


class _Subscriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_BACKLOG)
        self.dropped = False


class MissionRun:
    """One mission running in the background, with whoever is watching."""

    def __init__(self, mission: Mission, engine: MissionEngine, on_change,
                 *, force: bool = False) -> None:
        self.mission = mission
        #: A person overrode a stop reason for this run's first session.
        self.force = force
        self.supervisor = Supervisor(engine)
        self._on_change = on_change
        self._subscribers: set[_Subscriber] = set()
        self._replay: list[Any] = []
        self._task: asyncio.Task | None = None
        self.finished = asyncio.Event()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drive())

    def abort(self) -> None:
        self.supervisor.abort()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _drive(self) -> None:
        mission = self.mission
        # The reaping scope belongs here rather than around a single session:
        # this is a coroutine, so its `finally` is guaranteed to run, whereas
        # a `with` inside an async generator unwinds only when the generator
        # is closed. Nothing a mission's commands start outlives the mission.
        try:
            with reaper.reaping():
                async for event in self.supervisor.run(mission, force=self.force):
                    self._publish(event)
                    # Saved as it goes: a mission is hours long, and a restart
                    # in hour three should not lose hours one and two.
                    self._on_change(mission)
        except asyncio.CancelledError:
            logger.info("mission %s cancelled", mission.id)
            raise
        except Exception as err:  # noqa: BLE001 — a crash must still be visible
            logger.exception("mission %s failed", mission.id)
            self._publish(MissionNote(message=f"mission failed: {err}"))
        finally:
            self._on_change(mission)
            self.finished.set()
            for subscriber in list(self._subscribers):
                # None is the end-of-stream marker every watcher is waiting for.
                _offer(subscriber, None)

    def _publish(self, event: Any) -> None:
        self._replay.append(event)
        if len(self._replay) > REPLAY:
            del self._replay[: len(self._replay) - REPLAY]
        for subscriber in list(self._subscribers):
            _offer(subscriber, event)

    # -- watching ---------------------------------------------------------
    async def watch(self, *, replay: bool = True) -> AsyncIterator[Any]:
        """Everything from here on, and optionally what came before.

        Cancelling this — which is what a closed tab does — removes the
        subscriber and nothing else. The mission carries on.
        """
        subscriber = _Subscriber()
        self._subscribers.add(subscriber)
        try:
            if replay:
                for event in list(self._replay):
                    yield event
            if not self.running:
                return
            while True:
                event = await subscriber.queue.get()
                if event is None:
                    return
                yield event
        finally:
            self._subscribers.discard(subscriber)


def _offer(subscriber: _Subscriber, event: Any) -> None:
    try:
        subscriber.queue.put_nowait(event)
    except asyncio.QueueFull:
        # Drop the watcher rather than the work.
        subscriber.dropped = True


class MissionRunners:
    """Every mission running in this process."""

    def __init__(self) -> None:
        self._runs: dict[str, MissionRun] = {}

    def get(self, mission_id: str) -> MissionRun | None:
        return self._runs.get(mission_id)

    def running(self, mission_id: str) -> bool:
        run = self._runs.get(mission_id)
        return bool(run and run.running)

    def start(self, mission: Mission, engine: MissionEngine, on_change,
              *, force: bool = False) -> MissionRun:
        """Start it if it is not already going, and return the run either way.

        Returning the existing run rather than refusing means a second window
        onto a running mission is the same call as the first — the client does
        not have to know which of the two it is.
        """
        run = self._runs.get(mission.id)
        if run and run.running:
            return run
        run = MissionRun(mission, engine, on_change, force=force)
        self._runs[mission.id] = run
        run.start()
        return run

    def abort(self, mission_id: str) -> bool:
        run = self._runs.get(mission_id)
        if not run or not run.running:
            return False
        run.abort()
        return True

    def forget(self, mission_id: str) -> None:
        run = self._runs.get(mission_id)
        if run and not run.running:
            self._runs.pop(mission_id, None)


_runners: MissionRunners | None = None


def get_runners() -> MissionRunners:
    global _runners
    if _runners is None:
        _runners = MissionRunners()
    return _runners
