"""Local JSONL transcript store — the zero-config default backend.

One append-only file per session under data/sessions/. Sync file appends are
fast enough that `append` writes through immediately; `flush` is a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path

from compass.config import get_settings
from compass.models.messages import Message


class SessionStore:
    def _path(self, session_id: str) -> Path:
        return get_settings().sessions_dir / f"{session_id}.jsonl"

    def append(self, session_id: str, message: Message) -> None:
        with self._path(session_id).open("a") as f:
            f.write(json.dumps(message.to_record(), default=str) + "\n")

    async def flush(self) -> None:
        return None

    async def load(
        self, session_id: str, *, include_sidechains: bool = False
    ) -> list[Message]:
        path = self._path(session_id)
        if not path.is_file():
            return []
        messages = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                message = Message.from_record(json.loads(line))
            except (json.JSONDecodeError, KeyError):
                continue  # tolerate a torn tail write
            if not include_sidechains and message.meta.get("agent_id"):
                continue
            messages.append(message)
        return messages

    async def exists(self, session_id: str) -> bool:
        return self._path(session_id).is_file()

    async def list_sessions(self) -> list[str]:
        return sorted(
            p.stem
            for p in get_settings().sessions_dir.glob("*.jsonl")
        )

    async def overwrite(self, session_id: str, messages: list[Message]) -> None:
        path = self._path(session_id)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w") as f:
            for m in messages:
                f.write(json.dumps(m.to_record(), default=str) + "\n")
        tmp.replace(path)  # atomic swap

    async def delete(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)

    async def close(self) -> None:
        return None
