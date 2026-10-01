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

A message whose content is larger than a document can be — an attached photo
or clip, inlined as a data URI — has that content stored beside the document
rather than in it. See `large_content`; the record is otherwise unchanged, and
reading puts the content back.
"""

from __future__ import annotations

import asyncio
import logging

from compass.common.config import get_settings
from compass.common.models.messages import Message
from compass.common.persistence import large_content

logger = logging.getLogger("compass.cosmos")


class CosmosTranscriptStore:
    def __init__(self) -> None:
        self._client = None
        self._container = None
        self._queue: asyncio.Queue[tuple[str, Message] | None] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._seq: dict[str, int] = {}
        self._init_lock = asyncio.Lock()
        #: Which sessions exist, once it has been worked out. See list_sessions.
        self._sessions: set[str] | None = None
        self._sessions_lock = asyncio.Lock()

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
                record = await large_content.spill(message.to_record(), session_id)
                await container.upsert_item(
                    {
                        "id": message.uuid,
                        "sessionId": session_id,
                        "seq": message.meta.get("_seq", 0),
                        "record": record,
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
        if self._sessions is not None:
            # A session with a message is a session that exists. Recorded here
            # rather than when the write lands, so a conversation appears in
            # the list as soon as it has been spoken to.
            self._sessions.add(session_id)
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
        records: list[dict] = []
        max_seq = -1
        async for item in items:
            max_seq = max(max_seq, item.get("seq", 0))
            record = item["record"]
            # Sidechains are dropped before the content is fetched: a sub-agent
            # turn that this caller does not want should not cost a download.
            if not include_sidechains and (record.get("meta") or {}).get("agent_id"):
                continue
            records.append(record)
        # Resume continues the sequence rather than restarting it.
        self._seq[session_id] = max(self._seq.get(session_id, 0), max_seq + 1)
        # Filled together rather than one at a time; `fill_all` keeps the order
        # the query returned them in.
        return [Message.from_record(r) for r in await large_content.fill_all(records)]

    async def load_page(
        self,
        session_id: str,
        *,
        limit: int,
        before_seq: int | None = None,
        include_sidechains: bool = False,
    ) -> tuple[list[Message], int | None]:
        """The last `limit` messages, newest page first. See TranscriptStore.

        Read backwards and stopped early, which is the point: the one large
        conversation here is 2,005 messages and 5MB, and reading it to show
        the last fifty took several seconds. Descending order means the
        database returns the end of the conversation first, and the iterator
        is abandoned as soon as there is a screenful — so what is transferred
        is a page, not a transcript.

        Sidechains are filtered as the rows arrive rather than in the query,
        so that `limit` counts messages the caller will actually be shown. It
        is also the same test `load` uses, which matters more than saving a
        few rows: two different definitions of what counts as a sidechain
        would mean the paged view and the full one disagreed.
        """
        container = await self._get_container()
        where = "c.sessionId = @sid"
        params: list[dict] = [{"name": "@sid", "value": session_id}]
        if before_seq is not None:
            where += " AND c.seq < @before"
            params.append({"name": "@before", "value": int(before_seq)})
        items = container.query_items(
            query=f"SELECT * FROM c WHERE {where} ORDER BY c.seq DESC",
            parameters=params,
            partition_key=session_id,
        )
        records: list[dict] = []
        seqs: list[int] = []
        more = False
        async for item in items:
            record = item["record"]
            if not include_sidechains and (record.get("meta") or {}).get("agent_id"):
                continue
            if len(records) >= limit:
                # One row past the page, which is how we know there is one.
                # The iterator is dropped here rather than drained.
                more = True
                break
            records.append(record)
            seqs.append(int(item.get("seq", 0)))
        records.reverse()
        seqs.reverse()
        if seqs and before_seq is None:
            # The newest page carries the highest sequence number, so reading
            # it is enough to keep appends continuing the conversation rather
            # than restarting at zero. `max` because an older page must never
            # drag the counter backwards.
            self._seq[session_id] = max(self._seq.get(session_id, 0), seqs[-1] + 1)
        filled = await large_content.fill_all(records)
        return (
            [Message.from_record(r) for r in filled],
            seqs[0] if (more and seqs) else None,
        )

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
        """Which sessions have a transcript.

        Cached, because the query behind it is the expensive one here: finding
        74 distinct ids means looking at all 2,638 message documents, across
        partitions, and it grows with the number of messages rather than the
        number of conversations. Measured at 0.88s, paid on every load of the
        sidebar.

        Kept true by the two things that change the answer, both of which go
        through this object: appending the first message of a session adds its
        id, and deleting a session removes it.
        """
        if self._sessions is not None:
            return sorted(self._sessions)
        async with self._sessions_lock:
            if self._sessions is not None:
                return sorted(self._sessions)
            container = await self._get_container()
            items = container.query_items(
                query="SELECT DISTINCT VALUE c.sessionId FROM c"
            )
            self._sessions = {sid async for sid in items}
            return sorted(self._sessions)

    async def overwrite(self, session_id: str, messages: list[Message]) -> None:
        # Drain pending writes for this session, delete its docs, re-seed.
        # Only the documents: stored content keeps its reference, which is the
        # message's own uuid, so a message that survives the rewrite finds its
        # content where it left it and nothing is uploaded twice.
        await self.flush()
        await self._delete_docs(session_id)
        self._seq[session_id] = 0
        for message in messages:
            self.append(session_id, message)
        await self.flush()

    async def _delete_docs(self, session_id: str) -> None:
        container = await self._get_container()
        ids = container.query_items(
            query="SELECT c.id FROM c WHERE c.sessionId = @sid",
            parameters=[{"name": "@sid", "value": session_id}],
            partition_key=session_id,
        )
        async for row in ids:
            await container.delete_item(row["id"], partition_key=session_id)
        self._seq.pop(session_id, None)

    async def delete(self, session_id: str) -> None:
        await self._delete_docs(session_id)
        if self._sessions is not None:
            self._sessions.discard(session_id)
        await large_content.discard(session_id)

    async def close(self) -> None:
        if self._worker and not self._worker.done():
            await self._queue.put(None)
            await self._worker
        if self._client is not None:
            await self._client.close()
            # Forgotten as well as closed. Left in place, `_container` still
            # referred to a dead client, so anything that came in after
            # shutdown failed on the closed connection instead of opening a
            # new one — and a second close tried to close it again.
            self._client = None
            self._container = None
