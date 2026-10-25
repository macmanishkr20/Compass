"""Letting go of everything that holds a connection.

Five things in Compass hold a Cosmos client or a connection pool: the
transcript store, the chat store, the sidebar-metadata store, the memory
store, and the catalog the small collections share. On the local backend they
hold nothing and this costs nothing.

One place rather than five call sites, because the failure mode is a store
that nobody remembered: three of these were missed in the app's own shutdown
for as long as they existed, and every script that touched Cosmos ended with
"Unclosed client session" printed after its results — a warning nobody can
act on, sitting exactly where a real one would go unnoticed.

So a new store is added to the tuple below and is then closed by the server,
by the migrations, and by the guards, without any of them being changed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("estimator.persistence")


# Where each store's getter lives. A pair rather than a direct import so the
# list can name a store this build doesn't have: the Estimator carries the
# estimate module alone, so the chat store and the memory store simply are not
# here, and importing all four in one statement meant one absent module took
# the whole list down — shutdown then closed *nothing*, including the stores
# that did exist, and ended every run with a traceback.
_STORE_GETTERS = (
    ("estimator.common.persistence.factory", "get_transcript_store"),
    ("estimator.home.engine", "get_chat_store"),
    ("estimator.common.persistence.session_meta", "get_meta_store"),
    ("estimator.common.memory", "get_memory_store"),
)


def _stores() -> list:
    """Every store this build has that might be holding a connection.

    Imported here rather than at module scope: this is called at shutdown, and
    importing the world to close it would be a way of starting it instead. The
    getters build a store if none exists, which on these is cheap — a client is
    only opened on first use — so asking is never worse than guessing.
    """
    from importlib import import_module

    found = []
    for module, getter in _STORE_GETTERS:
        try:
            found.append(getattr(import_module(module), getter)())
        except (ImportError, AttributeError):
            continue  # not part of this build
    return found


async def close_all() -> None:
    """Close every store, then the shared catalog client.

    Each one independently: a store that fails to close must not keep the
    others open, which is the whole reason this is a loop and not a sequence
    of awaits.

    Call it from the loop that opened them. A client belongs to its loop and
    cannot be closed from another — see `catalog.close`.
    """
    for store in _stores():
        close = getattr(store, "close", None)
        if close is None:
            continue
        try:
            await close()
        except Exception:  # noqa: BLE001 — one bad close must not skip the rest
            logger.warning("could not close %s", type(store).__name__, exc_info=True)
    # The catalog keeps its own client, per event loop, so it closes itself.
    from estimator.common.persistence import catalog

    try:
        await catalog.close()
    except Exception:  # noqa: BLE001
        logger.warning("could not close the catalog client", exc_info=True)
