"""What this deployment will actually accept in one request.

A model's context window and a deployment's quota are different numbers, and
only one of them is written in the config. gpt-5's window is large. The
resource Compass is pointed at allows 50,000 tokens per minute, and a single
request has to fit inside that minute's budget — measured: a 30,000-token
request is accepted, a 70,000-token one comes back 429 with the budget
reported as zero, on a freshly renewed window.

That matters because Compass compacts at 80% of `context_window_tokens`, which
is 102,400. On this deployment a conversation reaching 102,400 tokens was
never going to be sent: it would 429, retry, 429 again, and surface a rate
limit — while the compaction that would have rescued it sat behind a threshold
it could not reach. The threshold was decoration.

So the budget is learned rather than configured. Azure reports the quota on
every single response, in headers Compass was already receiving and throwing
away. Whatever the resource says is what gets used.

Nothing here is a hard limit on the model. It is a statement about the
deployment, and a deployment can be upgraded — which is exactly why reading it
from the response beats writing it down.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping

logger = logging.getLogger("compass.gateway")

#: Azure's name for the per-minute token quota.
_LIMIT_HEADER = "x-ratelimit-limit-tokens"

#: How much of the quota one request may plan to occupy. Not 1.0, for two
#: reasons: the accounting is not exact (a 30,014-token input was billed about
#: 37,500 against the minute), and a quota shared with any other in-flight
#: request is not wholly ours to spend.
_SHARE = 0.70

_lock = threading.Lock()
_observed: int | None = None


def remember(headers: Mapping[str, Any]) -> None:
    """Record the quota from a response's headers, if it named one.

    Called on every response. Cheap, and the value only ever comes from the
    resource itself, so a changed quota is picked up on the next request
    rather than at the next deployment.
    """
    global _observed
    raw = headers.get(_LIMIT_HEADER) or headers.get(_LIMIT_HEADER.title())
    if raw is None:
        return
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return
    if value <= 0:
        return
    with _lock:
        if _observed == value:
            return
        first, _observed = _observed is None, value
    if first:
        logger.info(
            "deployment token quota observed: %s per minute, so one request "
            "is planned to stay under ~%s", f"{value:,}", f"{int(value * _SHARE):,}"
        )


def observed_quota() -> int | None:
    """The per-minute token quota this deployment reported, if it has."""
    with _lock:
        return _observed


def request_ceiling() -> int | None:
    """The largest request worth attempting, or None when nothing is known.

    None is the honest answer before the first response has come back, and
    callers treat it as "no opinion" rather than as zero.
    """
    quota = observed_quota()
    return int(quota * _SHARE) if quota else None


def effective_window(configured: int) -> int:
    """The budget to plan a conversation against.

    The smaller of what the model can hold and what the deployment will accept
    in one go. A conversation that outgrows the second is just as stuck as one
    that outgrows the first, and it is stuck sooner.
    """
    ceiling = request_ceiling()
    return min(configured, ceiling) if ceiling else configured


def reset() -> None:
    """Forget what was observed. For tests."""
    global _observed
    with _lock:
        _observed = None
