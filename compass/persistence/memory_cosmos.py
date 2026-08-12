"""Cosmos DB store for memory entries — the cloud backend for MemoryStore.

One document per entry, partitioned by scope (`home` or a workspace/project id)
so a project's memory is a single-partition read. Mirrors MemoryStore's API so
services/memory.py is backend-agnostic.
"""

from __future__ import annotations

import logging
import time
import uuid

from compass.config import get_settings
from compass.services.memory import CATEGORIES, MemoryEntry

logger = logging.getLogger("compass.memory.cosmos")


class CosmosMemoryStore:
    def __init__(self) -> None:
        self._client = None
        self._container = None

    async def _get_container(self):
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

    async def list(self, scope: str | None = None) -> list[dict]:
        c = await self._get_container()
        if scope:
            items = c.query_items(
                query="SELECT * FROM c WHERE c.scope=@s ORDER BY c.updated_at DESC",
                parameters=[{"name": "@s", "value": scope}],
                partition_key=scope,
            )
        else:
            items = c.query_items(query="SELECT * FROM c ORDER BY c.updated_at DESC")
        return [i async for i in items]

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
        items = c.query_items(
            query="SELECT * FROM c WHERE c.id=@i",
            parameters=[{"name": "@i", "value": entry_id}],
        )
        async for row in items:
            if summary is not None:
                row["summary"] = summary.strip()
            if details is not None:
                row["details"] = details.strip()
            if category is not None and category in CATEGORIES:
                row["category"] = category
            row["updated_at"] = time.time()
            await c.upsert_item(row)
            return row
        return None

    async def delete(self, entry_id: str) -> bool:
        c = await self._get_container()
        items = c.query_items(
            query="SELECT c.id, c.scope FROM c WHERE c.id=@i",
            parameters=[{"name": "@i", "value": entry_id}],
        )
        async for row in items:
            await c.delete_item(row["id"], partition_key=row["scope"])
            return True
        return False

    async def clear(self, scope: str | None = None) -> int:
        c = await self._get_container()
        rows = await self.list(scope)
        for r in rows:
            await c.delete_item(r["id"], partition_key=r["scope"])
        return len(rows)
