"""Cosmos DB store for Home/Chat threads — the cloud backend for ChatStore.

Kept in its own Cosmos container (`AZURE_COSMOS_CHAT_CONTAINER`, default
"chat") so Home threads stay isolated from the agent transcripts, exactly like
the local `sessions_dir/chat/` namespace.

Document shapes (partition key = /sessionId):

    message:  { id: <uuid>, sessionId, type: "msg", seq, record: {...} }
    meta:     { id: "__meta__", sessionId, type: "meta",
                title, pinned, created_at, updated_at }

`append` is non-blocking (enqueued, drained by one worker); the worker also
maintains the meta doc (updated_at, first-user-message title). This mirrors the
local ChatStore API so ChatEngine is backend-agnostic.

Home is where the attachments are, so this is where the 2MB item limit bites
hardest: measured on this install, three threads hold messages of 17MB, 23MB
and 29MB. Content that large is stored beside the document — see
`large_content` — and put back on read.
"""

from __future__ import annotations

import asyncio
import logging
import time

from compass.common.config import get_settings
from compass.common.models.messages import Message
from compass.common.persistence import large_content

logger = logging.getLogger("compass.chat.cosmos")

_META_ID = "__meta__"


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _title_from(message: Message) -> str:
    text = " ".join(_content_text(message.content).split())
    return text[:60] + ("…" if len(text) > 60 else "")


class CosmosChatStore:
    def __init__(self) -> None:
        self._client = None
        self._container = None
        self._queue: asyncio.Queue = asyncio.Queue()
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
                    id=cfg.cosmos_chat_container, partition_key=pk
                )
            except exceptions.CosmosHttpResponseError:
                self._container = await database.create_container_if_not_exists(
                    id=cfg.cosmos_chat_container, partition_key=pk, offer_throughput=400
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
                record = await large_content.spill(message.to_record(), session_id)
                await container.upsert_item(
                    {
                        "id": message.uuid,
                        "sessionId": session_id,
                        "type": "msg",
                        "seq": message.meta.get("_seq", 0),
                        "record": record,
                    }
                )
                await self._touch_meta(container, session_id, message)
            except Exception as err:  # noqa: BLE001 — persistence must not kill turns
                logger.error("cosmos chat write failed (kept in memory): %s", err)
            finally:
                self._queue.task_done()

    async def _touch_meta(self, container, session_id: str, message: Message) -> None:
        now = time.time()
        try:
            meta = await container.read_item(_META_ID, partition_key=session_id)
        except Exception:  # noqa: BLE001 — first write for this session
            meta = {
                "id": _META_ID,
                "sessionId": session_id,
                "type": "meta",
                "title": "",
                "pinned": False,
                "created_at": now,
                "updated_at": now,
            }
        meta["updated_at"] = now
        if not meta.get("title") and message.role == "user":
            meta["title"] = _title_from(message)
        await container.upsert_item(meta)

    # -- ChatStore API ------------------------------------------------------
    def append(self, session_id: str, message: Message) -> None:
        seq = self._seq.get(session_id, 0)
        self._seq[session_id] = seq + 1
        message.meta["_seq"] = seq
        self._ensure_worker()
        self._queue.put_nowait((session_id, message))

    async def flush(self) -> None:
        await self._queue.join()

    async def load(self, session_id: str) -> list[Message]:
        container = await self._get_container()
        items = container.query_items(
            query="SELECT * FROM c WHERE c.sessionId=@sid AND c.type='msg' ORDER BY c.seq ASC",
            parameters=[{"name": "@sid", "value": session_id}],
            partition_key=session_id,
        )
        records: list[dict] = []
        max_seq = -1
        async for item in items:
            max_seq = max(max_seq, item.get("seq", 0))
            records.append(item["record"])
        self._seq[session_id] = max(self._seq.get(session_id, 0), max_seq + 1)
        # Collected first, filled together: the query is already in seq order
        # and `fill_all` keeps it, so this is the same list either way — just
        # not one download at a time.
        return [Message.from_record(r) for r in await large_content.fill_all(records)]

    async def exists(self, session_id: str) -> bool:
        container = await self._get_container()
        items = container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.sessionId=@sid AND c.type='msg'",
            parameters=[{"name": "@sid", "value": session_id}],
            partition_key=session_id,
        )
        async for count in items:
            return count > 0
        return False

    async def list_sessions(self) -> list[str]:
        container = await self._get_container()
        items = container.query_items(query="SELECT DISTINCT VALUE c.sessionId FROM c")
        return sorted([sid async for sid in items])

    async def list_cards(self) -> list[dict]:
        container = await self._get_container()
        items = container.query_items(query="SELECT * FROM c WHERE c.type='meta'")
        cards: list[dict] = []
        async for m in items:
            cards.append(
                {
                    "id": m["sessionId"],
                    "title": m.get("title") or "New chat",
                    "pinned": bool(m.get("pinned")),
                    "owner": m.get("owner", "") or "",
                    "updated_at": m.get("updated_at", 0),
                    "created_at": m.get("created_at", 0),
                }
            )
        cards.sort(key=lambda c: c["updated_at"], reverse=True)
        return cards

    async def owner_of(self, session_id: str) -> str:
        container = await self._get_container()
        try:
            meta = await container.read_item(_META_ID, partition_key=session_id)
        except Exception:  # noqa: BLE001
            return ""
        return meta.get("owner", "") or ""

    async def set_meta(
        self, session_id: str, *, title: str | None = None,
        pinned: bool | None = None, owner: str | None = None
    ) -> None:
        container = await self._get_container()
        now = time.time()
        try:
            meta = await container.read_item(_META_ID, partition_key=session_id)
        except Exception:  # noqa: BLE001
            meta = {
                "id": _META_ID, "sessionId": session_id, "type": "meta",
                "title": "", "pinned": False, "created_at": now, "updated_at": now,
            }
        if title is not None:
            meta["title"] = title
        if pinned is not None:
            meta["pinned"] = pinned
        if owner is not None:
            meta["owner"] = owner
        await container.upsert_item(meta)

    async def delete(self, session_id: str) -> None:
        container = await self._get_container()
        ids = container.query_items(
            query="SELECT c.id FROM c WHERE c.sessionId=@sid",
            parameters=[{"name": "@sid", "value": session_id}],
            partition_key=session_id,
        )
        async for row in ids:
            await container.delete_item(row["id"], partition_key=session_id)
        self._seq.pop(session_id, None)
        await large_content.discard(session_id)

    async def rewrite(self, session_id: str, messages: list[Message]) -> None:
        """Truncate + re-seed the message docs (regenerate/edit). The meta doc
        (title/pinned) is preserved — only the messages change.

        Stored content is left where it is: a reference is the message's own
        uuid, so a message that survives the rewrite still finds its content
        and an attachment is never uploaded a second time."""
        await self.flush()
        container = await self._get_container()
        old = container.query_items(
            query="SELECT c.id FROM c WHERE c.sessionId=@sid AND c.type='msg'",
            parameters=[{"name": "@sid", "value": session_id}],
            partition_key=session_id,
        )
        async for row in old:
            await container.delete_item(row["id"], partition_key=session_id)
        self._seq[session_id] = 0
        for m in messages:
            self.append(session_id, m)
        await self.flush()

    async def close(self) -> None:
        if self._worker and not self._worker.done():
            await self._queue.put(None)
            await self._worker
        if self._client is not None:
            await self._client.close()
            # Forgotten as well as closed, so a later call reconnects rather
            # than using a closed client through a stale `_container`.
            self._client = None
            self._container = None
