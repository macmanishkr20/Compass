"""Cosmos DB store for memory entries — the cloud backend for MemoryStore.

One document per entry, partitioned by scope (`home` or a workspace/project id)
so a project's memory is a single-partition read. Mirrors MemoryStore's API so
services/memory.py is backend-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
import time

from compass.common.config import get_settings
from compass.common.memory import CATEGORIES, MemoryEntry

logger = logging.getLogger("compass.memory.cosmos")


class CosmosMemoryStore:
    def __init__(self) -> None:
        self._client = None
        self._container = None
        self._init_lock = asyncio.Lock()
        #: Every entry, once read. Memory is small — six entries here — and
        #: it is read at the start of every turn to build the system prompt,
        #: so an uncached read put 0.9s in front of each one. Written through
        #: by add/update/delete/clear below, which are the only things that
        #: change it.
        self._cache: list[dict] | None = None
        self._cache_lock = asyncio.Lock()

    async def _get_container(self):
        # Locked like the other stores: two turns starting at once would
        # otherwise each build a client, and whichever lost the race would
        # have its client dropped on the floor still holding connections.
        if self._container is not None:
            return self._container
        async with self._init_lock:
            if self._container is not None:
                return self._container
            from azure.cosmos import PartitionKey, exceptions
            from azure.cosmos.aio import CosmosClient

            cfg = get_settings().storage
            self._client = CosmosClient(cfg.cosmos_endpoint, credential=cfg.cosmos_key)
            db = await self._client.create_database_if_not_exists(cfg.cosmos_database)
            pk = PartitionKey(path="/scope")
            try:
                self._container = await db.create_container_if_not_exists(
                    id="memory", partition_key=pk
                )
            except exceptions.CosmosHttpResponseError:
                self._container = await db.create_container_if_not_exists(
                    id="memory", partition_key=pk, offer_throughput=400
                )
            return self._container

    async def _loaded(self) -> list[dict]:
        """Every entry, read once. Locked so a burst of turns starting
        together makes one query between them rather than one each."""
        if self._cache is not None:
            return self._cache
        async with self._cache_lock:
            if self._cache is not None:
                return self._cache
            c = await self._get_container()
            items = c.query_items(query="SELECT * FROM c ORDER BY c.updated_at DESC")
            self._cache = [i async for i in items]
            return self._cache

    async def refresh(self) -> None:
        """Forget what is cached, for anything that wrote behind this store."""
        async with self._cache_lock:
            self._cache = None

    async def list(self, scope: str | None = None) -> list[dict]:
        rows = await self._loaded()
        if scope:
            rows = [r for r in rows if r.get("scope") == scope]
        # Copies: the caller is handed entries that end up in a prompt, and
        # one that edited them in place would be editing what everyone sees.
        rows = [dict(r) for r in rows]
        rows.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
        return rows

    async def add(
        self, *, scope: str, category: str, summary: str, details: str = ""
    ) -> dict:
        c = await self._get_container()
        entry = MemoryEntry(
            scope=scope,
            category=category if category in CATEGORIES else "Context",
            summary=summary.strip(),
            details=details.strip(),
        ).to_dict()
        await c.upsert_item(entry)
        # After the write, never before: an entry that failed to store must
        # not be remembered as though it had been.
        (await self._loaded()).insert(0, dict(entry))
        return entry

    async def update(
        self,
        entry_id: str,
        *,
        summary: str | None = None,
        details: str | None = None,
        category: str | None = None,
    ) -> dict | None:
        c = await self._get_container()
        rows = await self._loaded()
        for row in rows:
            if row.get("id") != entry_id:
                continue
            updated = dict(row)
            if summary is not None:
                updated["summary"] = summary.strip()
            if details is not None:
                updated["details"] = details.strip()
            if category is not None and category in CATEGORIES:
                updated["category"] = category
            updated["updated_at"] = time.time()
            await c.upsert_item(updated)
            row.clear()
            row.update(updated)
            return dict(updated)
        return None

    async def delete(self, entry_id: str) -> bool:
        c = await self._get_container()
        rows = await self._loaded()
        for row in list(rows):
            if row.get("id") != entry_id:
                continue
            await c.delete_item(row["id"], partition_key=row["scope"])
            rows.remove(row)
            return True
        return False

    async def clear(self, scope: str | None = None) -> int:
        c = await self._get_container()
        rows = await self.list(scope)
        for r in rows:
            await c.delete_item(r["id"], partition_key=r["scope"])
        held = await self._loaded()
        gone = {r["id"] for r in rows}
        held[:] = [r for r in held if r.get("id") not in gone]
        return len(rows)

    async def close(self) -> None:
        self._cache = None
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._container = None
