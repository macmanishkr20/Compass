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

from compass.common.config import get_settings

logger = logging.getLogger("compass.meta")

# `VALID_MODES` and `VALID_EFFORTS` used to sit here. Nothing read either one,
# and the efforts list had gone stale — it still said minimal/low/medium/high
# after none, xhigh and max were added, so anyone who wired it up would have
# started rejecting three real settings. What is true about efforts lives in
# `compass.common.config` (EFFORT_LEVELS, and EFFORT_LADDERS for what a given
# deployment actually accepts); what is true about modes is whatever
# `compass.common.policy.permissions` does with them.


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
    routine_id: str = ""  # set on routine-run sessions; keeps them out of Conversations
    # Set on Pipelines builder sessions. They borrow the Code loop, so
    # without a marker their turns are persisted as Code conversations and
    # appear in that list — a conversation about a graph showing up beside
    # conversations about a repository.
    pipeline_id: str = ""
    #: Who this conversation belongs to. Empty is legacy and stays visible to
    #: everyone — see `compass.common.ownership` for why that fails open.
    owner: str = ""
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
    """Sidebar metadata in Cosmos, held in memory once it has been read.

    Cached because of what this is and how it is used. It is one small row per
    conversation — 76 of them here, a few hundred bytes each — and it is read
    on the way into *every* request that names a conversation, to decide
    whether the caller may see it. Against a Cosmos account on another
    continent that is 241ms of pure latency per request, and 455ms when the
    row is missing, because the SDK retries the 404.

    Safe to cache because this process is the only writer: every change goes
    through `upsert` or `delete` here, and both update the cache as they go.
    The one thing that writes behind its back is a migration script, which is
    an offline operation on a stopped server — and `refresh()` exists for
    anything that wants to be sure.

    The whole table is loaded on the first miss rather than row by row. It is
    one query either way, the result is kilobytes, and the sidebar is going to
    ask for all of it a moment later regardless.
    """

    def __init__(self) -> None:
        self._client = None
        self._container = None
        self._init_lock = asyncio.Lock()
        self._cache: dict[str, SessionMeta] | None = None
        self._cache_lock = asyncio.Lock()

    async def _get_container(self):
        if self._container is not None:
            return self._container
        async with self._init_lock:
            if self._container is not None:
                return self._container
            from azure.cosmos import PartitionKey, exceptions
            from azure.cosmos.aio import CosmosClient

            cfg = get_settings().storage
            self._client = CosmosClient(cfg.cosmos_endpoint, credential=cfg.cosmos_key)
            database = await self._client.create_database_if_not_exists(cfg.cosmos_database)
            pk = PartitionKey(path="/id")
            try:
                self._container = await database.create_container_if_not_exists(
                    id=cfg.cosmos_meta_container, partition_key=pk
                )
            except exceptions.CosmosHttpResponseError:
                # Provisioned-throughput account: RU/s must be explicit. Every
                # other container here already handles this; this one did not,
                # so a provisioned account came up with no sidebar metadata.
                self._container = await database.create_container_if_not_exists(
                    id=cfg.cosmos_meta_container, partition_key=pk, offer_throughput=400
                )
            return self._container

    async def _loaded(self) -> dict[str, SessionMeta]:
        """Every row, read once. Locked so a burst of first requests makes one
        query between them rather than one each."""
        if self._cache is not None:
            return self._cache
        async with self._cache_lock:
            if self._cache is not None:
                return self._cache
            container = await self._get_container()
            items = container.query_items(query="SELECT * FROM c")
            rows = [SessionMeta.from_dict(i) async for i in items]
            self._cache = {m.id: m for m in rows}
            logger.info("session metadata: %d row(s) cached", len(rows))
            return self._cache

    async def refresh(self) -> None:
        """Forget what is cached, for anything that wrote behind this store."""
        async with self._cache_lock:
            self._cache = None

    async def get(self, session_id: str) -> SessionMeta | None:
        return (await self._loaded()).get(session_id)

    async def upsert(self, meta: SessionMeta) -> None:
        container = await self._get_container()
        await container.upsert_item({**meta.to_dict(), "id": meta.id})
        # After the write, not before: a row that failed to store must not be
        # served from memory as though it had.
        cache = await self._loaded()
        cache[meta.id] = meta

    async def delete(self, session_id: str) -> None:
        from azure.cosmos import exceptions

        container = await self._get_container()
        try:
            await container.delete_item(session_id, partition_key=session_id)
        except exceptions.CosmosResourceNotFoundError:
            pass
        (await self._loaded()).pop(session_id, None)

    async def list_all(self) -> list[SessionMeta]:
        return list((await self._loaded()).values())

    async def close(self) -> None:
        self._cache = None
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._container = None


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
