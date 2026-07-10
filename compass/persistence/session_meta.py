"""Server-side conversation metadata — the enterprise home for everything the
sidebar needs that is *not* transcript content: title, pin, archive, group,
per-conversation mode/effort, and timestamps for sort/group-by.

Kept separate from the transcript (which stays an append-only event log) so
renaming or archiving a conversation never rewrites its history. Two backends
mirror the transcript store: a single local JSON file, or a Cosmos container.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from compass.config import get_settings

logger = logging.getLogger("compass.meta")

VALID_MODES = ("default", "accept_edits", "plan", "bypass")
VALID_EFFORTS = ("minimal", "low", "medium", "high")


@dataclass
class SessionMeta:
    id: str
    title: str = ""
    pinned: bool = False
    archived: bool = False
    group: str = ""  # "" = ungrouped
    mode: str = "default"
    effort: str = "medium"
    model: str = ""  # deployment override; "" = server default
    workspace: str = ""  # workspace id; "" = default workspace
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    message_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionMeta":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


class SessionMetaStore(Protocol):
    async def get(self, session_id: str) -> SessionMeta | None: ...
    async def upsert(self, meta: SessionMeta) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def list_all(self) -> list[SessionMeta]: ...


# --------------------------------------------------------------------------- #
# Local JSON backend — one file, whole map. Fine for the local/dev tier; the
# write lock serializes concurrent mutations.
# --------------------------------------------------------------------------- #
class LocalSessionMetaStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cache: dict[str, SessionMeta] | None = None

    def _path(self) -> Path:
        return get_settings().sessions_dir / "_meta.json"

    def _load_all(self) -> dict[str, SessionMeta]:
        if self._cache is not None:
            return self._cache
        path = self._path()
        data: dict[str, SessionMeta] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text())
                for sid, d in raw.items():
                    data[sid] = SessionMeta.from_dict({**d, "id": sid})
            except (OSError, json.JSONDecodeError) as err:
                logger.error("could not read session meta: %s", err)
        self._cache = data
        return data

    def _flush(self) -> None:
        path = self._path()
        assert self._cache is not None
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({s: m.to_dict() for s, m in self._cache.items()}))
        tmp.replace(path)  # atomic on POSIX

    async def get(self, session_id: str) -> SessionMeta | None:
        async with self._lock:
            return self._load_all().get(session_id)

    async def upsert(self, meta: SessionMeta) -> None:
        async with self._lock:
            self._load_all()[meta.id] = meta
            self._flush()

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            if self._load_all().pop(session_id, None) is not None:
                self._flush()

    async def list_all(self) -> list[SessionMeta]:
        async with self._lock:
            return list(self._load_all().values())


# --------------------------------------------------------------------------- #
# Cosmos backend — one document per session (type="meta"), partitioned by id.
# --------------------------------------------------------------------------- #
class CosmosSessionMetaStore:
    def __init__(self) -> None:
        self._client = None
        self._container = None
        self._init_lock = asyncio.Lock()

    async def _get_container(self):
        if self._container is not None:
            return self._container
        async with self._init_lock:
            if self._container is not None:
                return self._container
            from azure.cosmos import PartitionKey
            from azure.cosmos.aio import CosmosClient

            cfg = get_settings().storage
            self._client = CosmosClient(cfg.cosmos_endpoint, credential=cfg.cosmos_key)
            database = await self._client.create_database_if_not_exists(cfg.cosmos_database)
            self._container = await database.create_container_if_not_exists(
                id=f"{cfg.cosmos_container}_meta", partition_key=PartitionKey(path="/id")
            )
            return self._container

    async def get(self, session_id: str) -> SessionMeta | None:
        from azure.cosmos import exceptions

        container = await self._get_container()
        try:
            item = await container.read_item(session_id, partition_key=session_id)
            return SessionMeta.from_dict(item)
        except exceptions.CosmosResourceNotFoundError:
            return None

    async def upsert(self, meta: SessionMeta) -> None:
        container = await self._get_container()
        await container.upsert_item({**meta.to_dict(), "id": meta.id})

    async def delete(self, session_id: str) -> None:
        from azure.cosmos import exceptions

        container = await self._get_container()
        try:
            await container.delete_item(session_id, partition_key=session_id)
        except exceptions.CosmosResourceNotFoundError:
            pass

    async def list_all(self) -> list[SessionMeta]:
        container = await self._get_container()
        items = container.query_items(query="SELECT * FROM c")
        return [SessionMeta.from_dict(i) async for i in items]


_meta_store: SessionMetaStore | None = None


def get_meta_store() -> SessionMetaStore:
    global _meta_store
    if _meta_store is not None:
        return _meta_store
    if get_settings().storage.backend == "cosmos":
        _meta_store = CosmosSessionMetaStore()
    else:
        _meta_store = LocalSessionMetaStore()
    return _meta_store
