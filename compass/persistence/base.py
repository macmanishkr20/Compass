"""TranscriptStore protocol — one contract, pluggable backends.

`append` is synchronous and non-blocking (backends enqueue); `flush` drains
pending writes and is awaited at turn end so a completed turn is always
durable. Ordering within a session is preserved by a per-message sequence
number assigned at append time.
"""

from __future__ import annotations

from typing import Protocol

from compass.models.messages import Message


class TranscriptStore(Protocol):
    def append(self, session_id: str, message: Message) -> None: ...

    async def flush(self) -> None: ...

    async def load(
        self, session_id: str, *, include_sidechains: bool = False
    ) -> list[Message]: ...

    async def exists(self, session_id: str) -> bool: ...

    async def list_sessions(self) -> list[str]: ...

    # -- checkpoint operations (edit / regenerate / fork / delete) --------

    async def overwrite(self, session_id: str, messages: list[Message]) -> None:
        """Replace a transcript wholesale. Used to truncate at a checkpoint
        (edit/regenerate) or seed a fork. Append-only history is a storage
        detail, not a product guarantee — the UI edits the log."""
        ...

    async def delete(self, session_id: str) -> None: ...
