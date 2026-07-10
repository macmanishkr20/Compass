"""Cosmos DB transcript store (NoSQL API).

Document shape — one document per message, partitioned by session:

    {
      "id":        "<message uuid>",
      "sessionId": "<session id>",       # partition key
      "seq":       42,                    # append order within the session
      "record":    { ...Message.to_record()... }
    }

Writes are enqueued and drained by a single background worker so `append`
never blocks the agent loop; `flush()` awaits durability at turn end.
Serverless Cosmos accounts work out of the box (no throughput specified);
provisioned accounts fall back to 400 RU/s on container creation.
"""

from __future__ import annotations

import asyncio
import logging

from compass.config import get_settings
from compass.models.messages import Message

logger = logging.getLogger("compass.cosmos")


class CosmosTranscriptStore:
    def __init__(self) -> None:
        self._client = None
        self._container = None
        self._queue: asyncio.Queue[tuple[str, Message] | None] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._seq: dict[str, int] = {}
        self._init_lock = asyncio.Lock()

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
            database = await self._client.create_database_if_not_exists(
                cfg.cosmos_database
            )
            pk = PartitionKey(path="/sessionId")
            try:
                self._container = await database.create_container_if_not_exists(
                    id=cfg.cosmos_container, partition_key=pk
                )
            except exceptions.CosmosHttpResponseError:
                # Provisioned-throughput account: must specify RU/s explicitly.
                self._container = await database.create_container_if_not_exists(
                    id=cfg.cosmos_container, partition_key=pk, offer_throughput=400
                )
            return self._container

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                session_id, message = item
                container = await self._get_container()
                await container.upsert_item(
                    {
                        "id": message.uuid,
                        "sessionId": session_id,
                        "seq": message.meta.get("_seq", 0),
                        "record": message.to_record(),
                    }
                )
            except Exception as err:  # noqa: BLE001 — persistence must not kill turns
                logger.error("cosmos write failed (message kept in memory): %s", err)
            finally:
                self._queue.task_done()

    # -- TranscriptStore ----------------------------------------------------

    def append(self, session_id: str, message: Message) -> None:
        seq = self._seq.get(session_id, 0)
        self._seq[session_id] = seq + 1
        message.meta["_seq"] = seq
        self._ensure_worker()
        self._queue.put_nowait((session_id, message))

    async def flush(self) -> None:
        await self._queue.join()

    async def load(
        self, session_id: str, *, include_sidechains: bool = False
    ) -> list[Message]:
        container = await self._get_container()
        query = "SELECT * FROM c WHERE c.sessionId = @sid ORDER BY c.seq ASC"
        items = container.query_items(
            query=query,
            parameters=[{"name": "@sid", "value": session_id}],
            partition_key=session_id,
        )
        messages: list[Message] = []
        max_seq = -1
        async for item in items:
            max_seq = max(max_seq, item.get("seq", 0))
            message = Message.from_record(item["record"])
            if not include_sidechains and message.meta.get("agent_id"):
                continue
            messages.append(message)
        # Resume continues the sequence rather than restarting it.
        self._seq[session_id] = max(self._seq.get(session_id, 0), max_seq + 1)
        return messages

    async def exists(self, session_id: str) -> bool:
        container = await self._get_container()
        query = "SELECT VALUE COUNT(1) FROM c WHERE c.sessionId = @sid"
        items = container.query_items(
            query=query,
            parameters=[{"name": "@sid", "value": session_id}],
            partition_key=session_id,
        )
        async for count in items:
            return count > 0
        return False

    async def list_sessions(self) -> list[str]:
        container = await self._get_container()
        items = container.query_items(
            query="SELECT DISTINCT VALUE c.sessionId FROM c"
        )
        return sorted([sid async for sid in items])

    async def overwrite(self, session_id: str, messages: list[Message]) -> None:
        # Drain pending writes for this session, delete its docs, re-seed.
        await self.flush()
        await self.delete(session_id)
        self._seq[session_id] = 0
        for message in messages:
            self.append(session_id, message)
        await self.flush()

    async def delete(self, session_id: str) -> None:
        container = await self._get_container()
        ids = container.query_items(
            query="SELECT c.id FROM c WHERE c.sessionId = @sid",
            parameters=[{"name": "@sid", "value": session_id}],
            partition_key=session_id,
        )
        async for row in ids:
            await container.delete_item(row["id"], partition_key=session_id)
        self._seq.pop(session_id, None)

    async def close(self) -> None:
        if self._worker and not self._worker.done():
            await self._queue.put(None)
            await self._worker
        if self._client is not None:
            await self._client.close()
