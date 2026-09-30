"""The small owner-scoped collections: saved prompts, missions, routines,
workspaces, estimates, pipeline definitions.

Every one of these was a JSON file under `data/`, which is why a Compass
moved to the cloud took its conversations with it and left everything else
behind. They are all the same shape of thing — a handful of documents with an
`id`, belonging to somebody, read as "everything of mine" — so they share one
store rather than growing six of them.

Two backends behind one contract, chosen the same way the transcript store
chooses: `COMPASS_STORAGE_BACKEND`.

  local   the same JSON files, in the same place, in the same shape. Not a
          new layout with a migration in front of it: an install that never
          goes near the cloud should not be asked to convert anything, and
          the file it has keeps working byte for byte.
  cosmos  one item per document in a single `catalog` container, partitioned
          by owner. Six collections of a few kilobytes do not deserve six
          containers and six throughput allocations; they are read together
          and they belong together.

`shape` exists because the files disagree. prompts.json is a list,
missions.json is a map keyed by id, and estimates are a directory with one
file per record. All three are collections of documents and none is worth
rewriting, so the shape is declared and the difference stops at this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from compass.common.config import get_settings

logger = logging.getLogger("compass.persistence")

#: The one Cosmos container all of these share. Partitioned by owner, which is
#: how they are always read.
CONTAINER = "catalog"

#: Containers, by name. Most collections share `catalog`; the two that are
#: big or hot enough to deserve their own — designs, users — say so.
_containers: dict[str, Any] = {}
_cosmos_client: Any = None
_cosmos_lock = asyncio.Lock()


async def close() -> None:
    """Release the shared Cosmos client.

    Called from the app's shutdown, beside the transcript store's own close.
    Without it the aiohttp session behind the SDK is never released and
    Python says so on exit — "Unclosed client session", once per connection,
    which is both a leak and a warning nobody can act on.
    """
    global _cosmos_client
    client, _cosmos_client = _cosmos_client, None
    _containers.clear()
    if client is not None:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 — shutting down anyway
            logger.debug("catalog cosmos client did not close cleanly")


def _use_cosmos() -> bool:
    cfg = get_settings().storage
    return cfg.backend == "cosmos" and cfg.cosmos_configured


async def _container(name: str, pk_path: str) -> Any:
    """One container, created on first use and then reused.

    `create_container_if_not_exists` is deliberate rather than an assumption
    that somebody ran a setup script: a fresh account should work. It does
    mean the name has to be exactly right, though — a typo makes a second
    container instead of an error, which is how `transcripts_meta` nearly
    ended up beside `transcripts-meta`.
    """
    hit = _containers.get(name)
    if hit is not None:
        return hit
    async with _cosmos_lock:
        hit = _containers.get(name)
        if hit is not None:
            return hit
        from azure.cosmos import PartitionKey
        from azure.cosmos.aio import CosmosClient

        global _cosmos_client
        cfg = get_settings().storage
        if _cosmos_client is None:
            _cosmos_client = CosmosClient(
                cfg.cosmos_endpoint, credential=cfg.cosmos_key
            )
        db = await _cosmos_client.create_database_if_not_exists(cfg.cosmos_database)
        pk = PartitionKey(path=pk_path)
        try:
            container = await db.create_container_if_not_exists(id=name, partition_key=pk)
        except TypeError:  # older SDKs want throughput named for provisioned
            container = await db.create_container_if_not_exists(
                id=name, partition_key=pk, offer_throughput=400
            )
        _containers[name] = container
        return container


class Collection:
    """One named collection of documents, each carrying an id.

    The API is deliberately small and whole-document: these are configuration
    records edited one at a time, not a log. Nothing here streams, and nothing
    here is hot.
    """

    def __init__(
        self,
        kind: str,
        filename: str,
        *,
        shape: str = "list",
        id_field: str = "id",
        owner_field: str = "owner",
        container: str = CONTAINER,
        partition_field: str | None = None,
        partition_source: str | None = None,
    ) -> None:
        self.kind = kind
        self.filename = filename
        self.shape = shape          # "list" | "map"
        self.id_field = id_field
        self.owner_field = owner_field
        #: Which container, and which field of the document is its partition
        #: key. Defaults to the shared `catalog`, partitioned by owner; a
        #: collection with its own container names both.
        self.container = container
        self.partition_field = partition_field or owner_field
        #: Which field of the document the partition value comes from, when
        #: it is not named the same as the container's path. A routine run
        #: partitions by `/parentId` and calls the field `routine_id`; the
        #: record keeps its own name and the item carries both.
        self.partition_source = partition_source or self.partition_field
        self._lock = asyncio.Lock()

    # ── local ───────────────────────────────────────────────────────────
    def _path(self) -> Path:
        settings = get_settings()
        folder = settings.workspace_root / settings.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        return folder / self.filename

    def _dir(self) -> Path:
        """For `shape="dir"`: the folder holding one file per record."""
        settings = get_settings()
        d = settings.workspace_root / settings.data_dir / self.filename
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _safe(record_id: str) -> str:
        # The id reaches the filesystem, and one that walks out of the
        # directory is the kind of thing that is only funny once.
        safe = "".join(c for c in str(record_id) if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("invalid id")
        return safe

    def _read_local(self) -> list[dict]:
        if self.shape == "dir":
            out: list[dict] = []
            for f in sorted(self._dir().glob("*.json")):
                try:
                    row = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    logger.warning("skipping unreadable record %s", f)
                    continue
                if isinstance(row, dict):
                    out.append(row)
            return out
        path = self._path()
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text() or ("[]" if self.shape == "list" else "{}"))
        except (OSError, json.JSONDecodeError) as err:
            # The same bargain the rest of these stores strike: one unreadable
            # file is an empty collection and a warning, not a dead section.
            logger.warning("%s unreadable (%s); treating as empty", self.filename, err)
            return []
        if self.shape == "map":
            rows = list(raw.values()) if isinstance(raw, dict) else []
        else:
            rows = raw if isinstance(raw, list) else []
        return [r for r in rows if isinstance(r, dict)]

    def _write_one_local(self, doc: dict) -> None:
        """One record, one file. Written atomically so a crash mid-write
        cannot truncate it."""
        path = self._dir() / f"{self._safe(doc.get(self.id_field, ''))}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    def _write_local(self, rows: list[dict]) -> None:
        if self.shape == "dir":
            keep = {self._safe(r.get(self.id_field, "")) for r in rows}
            for f in self._dir().glob("*.json"):
                if f.stem not in keep:
                    f.unlink(missing_ok=True)
            for r in rows:
                self._write_one_local(r)
            return
        path = self._path()
        if self.shape == "map":
            payload: Any = {str(r.get(self.id_field, "")): r for r in rows}
        else:
            payload = rows
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(path)  # atomic on POSIX

    # ── cosmos ──────────────────────────────────────────────────────────
    def _item_id(self, doc_id: str) -> str:
        # Namespaced, because one container holds every collection and two of
        # them could easily pick the same id.
        return f"{self.kind}:{doc_id}"

    def _partition(self, doc: dict) -> str:
        """The partition key value for one document.

        A container partitioned by `/id` is the awkward case, and it is the
        common one: the partition key *is* the item id, which is namespaced.
        Writing the raw document id there instead silently replaced the
        namespaced id with the bare one — the document still stored and still
        listed, but `get` looked for `user:alice` and the item was called
        `alice`, so it came back as missing.
        """
        if self.partition_field == "id":
            return self._item_id(str(doc.get(self.id_field, "")))
        return str(doc.get(self.partition_source, "") or "")

    def _wrap(self, doc: dict) -> dict:
        item = {
            "id": self._item_id(str(doc.get(self.id_field, ""))),
            "kind": self.kind,
            "doc": doc,
        }
        # The partition key must be a top-level field named for the
        # container's own path. When that path is `/id` the field is already
        # there and must not be written over.
        if self.partition_field != "id":
            item[self.partition_field] = self._partition(doc)
        return item

    # ── migration ───────────────────────────────────────────────────────
    def local_rows(self) -> list[dict]:
        """Everything in the local files, whatever backend is configured.

        For the migration, which has to read one side and write the other in
        the same process. Deliberately not routed through `all()`: that asks
        the configured backend, and during a migration the answer would be
        the destination rather than the source.
        """
        return self._read_local()

    def describe(self) -> str:
        return f"{self.kind} ({self.filename} -> {self.container})"

    # ── the contract ────────────────────────────────────────────────────
    async def all(self) -> list[dict]:
        """Every document, in no particular order — callers sort."""
        if not _use_cosmos():
            async with self._lock:
                return self._read_local()
        container = await _container(self.container, f'/{self.partition_field}')
        out: list[dict] = []
        items = container.query_items(
            query="SELECT c.doc FROM c WHERE c.kind=@kind",
            parameters=[{"name": "@kind", "value": self.kind}],
        )
        async for row in items:
            doc = row.get("doc")
            if isinstance(doc, dict):
                out.append(doc)
        return out

    async def get(self, doc_id: str) -> dict | None:
        if not _use_cosmos():
            for row in await self.all():
                if str(row.get(self.id_field)) == str(doc_id):
                    return row
            return None
        container = await _container(self.container, f'/{self.partition_field}')
        # The owner is part of the key and the caller does not have it, so
        # this is a query rather than a point read. These collections are
        # small; a cross-partition read of a few kilobytes is not the thing
        # worth contorting the API to avoid.
        items = container.query_items(
            query="SELECT c.doc FROM c WHERE c.id=@id",
            parameters=[{"name": "@id", "value": self._item_id(str(doc_id))}],
        )
        async for row in items:
            doc = row.get("doc")
            if isinstance(doc, dict):
                return doc
        return None

    async def put(self, doc: dict) -> dict:
        """Insert or replace one document, by its id."""
        doc_id = str(doc.get(self.id_field, ""))
        if not doc_id:
            raise ValueError(f"{self.kind}: a document needs an {self.id_field}")
        if not _use_cosmos():
            async with self._lock:
                if self.shape == "dir":
                    self._write_one_local(doc)
                else:
                    rows = self._read_local()
                    rows = [r for r in rows if str(r.get(self.id_field)) != doc_id]
                    rows.append(doc)
                    self._write_local(rows)
            return doc
        container = await _container(self.container, f'/{self.partition_field}')
        await container.upsert_item(self._wrap(doc))
        return doc

    async def remove(self, doc_id: str) -> bool:
        if not _use_cosmos():
            async with self._lock:
                if self.shape == "dir":
                    f = self._dir() / f"{self._safe(doc_id)}.json"
                    if not f.is_file():
                        return False
                    f.unlink(missing_ok=True)
                    return True
                rows = self._read_local()
                kept = [r for r in rows if str(r.get(self.id_field)) != str(doc_id)]
                if len(kept) == len(rows):
                    return False
                self._write_local(kept)
            return True
        existing = await self.get(doc_id)
        if existing is None:
            return False
        container = await _container(self.container, f'/{self.partition_field}')
        await container.delete_item(
            self._item_id(str(doc_id)), partition_key=self._partition(existing)
        )
        return True

    async def replace_all(self, docs: list[dict]) -> None:
        """Set the whole collection. Used where an order or a bulk edit is the
        operation, rather than a change to one record."""
        if not _use_cosmos():
            async with self._lock:
                self._write_local(list(docs))
            return
        keep = {str(d.get(self.id_field, "")) for d in docs}
        for old in await self.all():
            old_id = str(old.get(self.id_field, ""))
            if old_id and old_id not in keep:
                await self.remove(old_id)
        for doc in docs:
            await self.put(doc)
