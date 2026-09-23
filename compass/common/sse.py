"""Keep-alive for server-sent event streams.

A turn can be legitimately silent for minutes — a long shell command, a model
thinking — and from the browser that is indistinguishable from a dead
connection: it waits on a socket nobody will ever write to again. That is how a
turn whose model request was dropped mid-flight leaves a spinner running for
half an hour with no way to tell that nothing is happening.

A ping every few seconds makes silence mean something. The client can give up
on a stream that stops pinging, and a reverse proxy stops cutting connections
it believes are abandoned (nginx closes an idle upstream after 60s by default,
which a four-minute request would otherwise hit).

`: ping` is an SSE comment — every client ignores it, so nothing downstream
needs to know it exists.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator

#: Short enough that a 60s proxy timeout never fires and a client can treat a
#: longer gap as death, long enough to cost nothing.
HEARTBEAT_SECONDS = 15.0

_DONE = object()


async def with_heartbeat(frames: AsyncIterator[str], *,
                         every: float = HEARTBEAT_SECONDS) -> AsyncIterator[str]:
    """`frames`, plus a comment line whenever the stream goes quiet."""
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for frame in frames:
                await queue.put(frame)
        except BaseException as err:  # noqa: BLE001 - re-raised on the far side
            await queue.put(err)
        else:
            await queue.put(_DONE)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=every)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        # The client went away (or we raised): stop the turn behind us, which
        # is what closing the response already meant.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
