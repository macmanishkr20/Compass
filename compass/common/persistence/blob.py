"""Getting at a blob container, once.

Four places store large things in blob storage — message content, design
versions, design markup, chat uploads — and each had grown its own copy of the
same twenty lines: build a client from the connection string, tune it, ask for
the container, create it if it is not there.

Two things were wrong with having four copies.

The first is measurable. Every one of them called `create_container()` on
every single operation, which is a network round trip that fails with "already
exists" and is then swallowed. Opening one design made four blob calls and
paid for eight. Measured on this account, filling a design's 222KB of markup
took 2.08 seconds; the container is created once now, and the same fill is a
fraction of that.

The second is that the tuning has to match. A single request for a whole blob
ran at 0.30 MB/s here and timed out on a 17MB message; in 1MB ranges, sixteen
in flight, the same blob took 7.1 seconds. That number was worked out once and
then had to be remembered in three other files.

So: one function, one client per process, one creation attempt per container.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from compass.common.config import get_settings

logger = logging.getLogger("compass.persistence")

#: How a large blob moves: in 1MB pieces, several at a time. The default is one
#: request for the whole thing, which on this account ran an order of magnitude
#: slower and timed out on the big ones.
CHUNK_BYTES = 1024 * 1024

#: How many of those pieces are in flight. Measured: 1MB/16-way beat 4MB/8-way
#: (7.1s against 11.2s for the same 17MB blob).
CONCURRENCY = 16

_service: Any = None
_created: set[str] = set()
#: These are the synchronous SDK clients, reached from several worker threads
#: through `asyncio.to_thread`, so building them is guarded.
_lock = threading.Lock()


def enabled() -> bool:
    """Whether blob storage is configured at all. When it is not, every caller
    falls back to local disk — a zero-config install still works."""
    return bool(get_settings().storage.blob_connection_string)


def container(name: str):
    """A tuned client for `name`, created on the first call and then reused.

    The creation attempt happens once per container per process. It is still an
    attempt rather than a requirement, because a fresh storage account should
    work without anybody having run a setup script first.
    """
    global _service
    from azure.storage.blob import BlobServiceClient

    with _lock:
        if _service is None:
            _service = BlobServiceClient.from_connection_string(
                get_settings().storage.blob_connection_string,
                max_single_get_size=CHUNK_BYTES,
                max_chunk_get_size=CHUNK_BYTES,
                max_single_put_size=CHUNK_BYTES,
                max_block_size=CHUNK_BYTES,
            )
        client = _service.get_container_client(name)
        if name not in _created:
            try:
                client.create_container()
            except Exception:  # noqa: BLE001 — already there is the normal case
                pass
            # Recorded either way. A container that could not be created will
            # fail the actual read or write with something worth reading,
            # rather than being retried silently on every call forever.
            _created.add(name)
        return client


def reset() -> None:
    """Forget the client and what has been created.

    For a process that changes the connection string underneath itself — the
    tests, and the migrations that flip the backend — so the next call builds
    a client for wherever it is now pointed.
    """
    global _service
    with _lock:
        _service = None
        _created.clear()
