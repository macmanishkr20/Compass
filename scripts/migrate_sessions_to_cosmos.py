#!/usr/bin/env python3
"""Copy transcripts, chat threads, sidebar metadata and memory into Cosmos.

    python scripts/migrate_sessions_to_cosmos.py                    # say what would move
    python scripts/migrate_sessions_to_cosmos.py --apply
    python scripts/migrate_sessions_to_cosmos.py --apply --only chat
    python scripts/migrate_sessions_to_cosmos.py --apply --resume   # skip finished sessions

Separate from `migrate_to_cosmos.py`, which moves the small stores, because
these are a different job: 257MB across 4,676 messages in 277 files, where
that one moved 169 documents. It is slow enough to be interrupted, so being
resumable matters more than being short.

Four parts, `--only` selects them by name:

  transcripts   data/sessions/*.jsonl        -> transcripts
  meta          data/sessions/_meta.json     -> transcripts-meta
  chat          data/sessions/chat/*.jsonl   -> chat  (messages + one meta doc)
  memory        data/memory.json             -> memory

The same three properties the other migration holds to:

  * Nothing local is deleted or changed, so switching back is one environment
    variable.
  * Idempotent — every document is written by an id derived from the data
    (a message's uuid, a session's id), so a second run overwrites rather
    than duplicates and an interrupted run is finished by re-running.
  * It counts what arrived, per session, and says so when that is fewer than
    what was read. "No errors" is not the same as "the data is there".

One thing it does that the other does not: a message too large to be a
document has its content moved to blob storage as it goes, through the same
`large_content` the application now writes through. On this install seven
messages need it — one of 39MB, in a 2MB world.

Not moved: `data/sessions/chat_media/`, 273MB of uploads. Those are handed to
the render tool as filesystem paths, so they have to stay on a filesystem;
moving them is a change to that tool, not a migration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PARTS = ("transcripts", "meta", "chat", "memory")


# --------------------------------------------------------------------------- #
# Reading what is on disk. Line by line, never whole-file: one message here is
# 39MB, and a session file is 81MB.
# --------------------------------------------------------------------------- #
def _rows(path: Path):
    """Every record in a transcript, with the sequence it was appended in.

    A torn tail write is skipped rather than fatal, matching what the local
    store does when it reads the same file.
    """
    seq = 0
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    with handle as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield seq, rec
            seq += 1


def _doc_id(session_id: str, seq: int, rec: dict, seen: set[str]) -> str:
    """A stable id for a message document.

    The message's own uuid, which is what the application uses, so a migrated
    message and one written live land on the same document. Falls back to the
    position in the file when a record has no uuid or repeats one — derived
    from the data either way, so re-running is still idempotent, and nothing
    is silently collapsed onto an id that is already taken.
    """
    uuid = str(rec.get("uuid") or "")
    if uuid and uuid not in seen:
        seen.add(uuid)
        return uuid
    return f"{session_id}#{seq}"


# --------------------------------------------------------------------------- #
# The parts
# --------------------------------------------------------------------------- #
async def _messages(
    container, session_id: str, path: Path, *, type_field: str | None, apply: bool
) -> tuple[int, int]:
    """Write one session's messages. Returns (read, written)."""
    from compass.common.persistence import large_content

    read = written = 0
    seen: set[str] = set()
    for seq, rec in _rows(path):
        read += 1
        if not apply:
            continue
        doc = {
            "id": _doc_id(session_id, seq, rec, seen),
            "sessionId": session_id,
            "seq": seq,
            "record": await large_content.spill(rec, session_id),
        }
        if type_field:
            doc["type"] = type_field
        try:
            await container.upsert_item(doc)
            written += 1
        except Exception as err:  # noqa: BLE001 — report, keep going
            print(f"      ! {session_id} seq {seq}: {err}")
    return read, written


async def _count(container, session_id: str, type_field: str | None) -> int:
    where = "c.sessionId=@sid" + (f" AND c.type='{type_field}'" if type_field else "")
    items = container.query_items(
        query=f"SELECT VALUE COUNT(1) FROM c WHERE {where}",
        parameters=[{"name": "@sid", "value": session_id}],
        partition_key=session_id,
    )
    async for n in items:
        return int(n)
    return 0


async def do_transcripts(cfg, *, apply: bool, resume: bool) -> tuple[int, int]:
    from compass.common.persistence.cosmos_store import CosmosTranscriptStore

    store = CosmosTranscriptStore()
    try:
        container = await store._get_container()
        files = sorted((ROOT / "data" / "sessions").glob("*.jsonl"))
        return await _run_files(container, files, None, apply=apply, resume=resume)
    finally:
        await store.close()


async def do_chat(cfg, *, apply: bool, resume: bool) -> tuple[int, int]:
    """Chat messages, plus the meta document each thread needs.

    The meta doc is not optional and not cosmetic. On disk, Home builds the
    sidebar from the files themselves — the title from the first user message,
    the dates from the file's own timestamps. In Cosmos there are no files to
    ask, so the sidebar reads a meta document instead. Migrating the messages
    and not these would leave every thread in the list called "New chat" and
    dated 1970.
    """
    from compass.home.engine import ChatStore, _first_user_title
    from compass.home.store import CosmosChatStore, _META_ID

    store = CosmosChatStore()
    try:
        container = await store._get_container()
        chat_dir = ROOT / "data" / "sessions" / "chat"
        files = sorted(chat_dir.glob("*.jsonl"))
        read, written = await _run_files(
            container, files, "msg", apply=apply, resume=resume
        )
        overrides = ChatStore()._read_meta()
        for path in files:
            sid = path.stem
            try:
                st = path.stat()
            except OSError:
                continue
            row = overrides.get(sid, {})
            doc = {
                "id": _META_ID,
                "sessionId": sid,
                "type": "meta",
                # A manual rename wins, exactly as `list_cards` decides it
                # locally; otherwise the first user message, worked out once
                # here because there is no file to derive it from later.
                "title": row.get("title") or _first_user_title(path) or "",
                "pinned": bool(row.get("pinned")),
                "owner": row.get("owner", "") or "",
                "created_at": getattr(st, "st_birthtime", st.st_ctime),
                "updated_at": st.st_mtime,
            }
            read += 1
            if not apply:
                continue
            try:
                await container.upsert_item(doc)
                written += 1
            except Exception as err:  # noqa: BLE001
                print(f"      ! meta {sid}: {err}")
        return read, written
    finally:
        await store.close()


async def _run_files(container, files, type_field, *, apply: bool, resume: bool):
    read = written = 0
    for i, path in enumerate(files, 1):
        sid = path.stem
        try:
            mb = path.stat().st_size / 1e6
        except OSError as err:
            # A file that has gone between the listing and now. Skipped and
            # named, not fatal: this run takes long enough that a session can
            # be deleted while it is going, and one missing file must not
            # abandon the files after it — or the parts after that.
            print(f"  [{i}/{len(files)}] {sid[:13]}  skipped: {err.strerror}")
            continue
        if resume and apply:
            # Counted only here. Reading an 81MB file to find out how many
            # lines it has is worth it to skip uploading it again, and not
            # worth it otherwise.
            n = sum(1 for _ in _rows(path))
            if await _count(container, sid, type_field) >= n:
                print(f"  [{i}/{len(files)}] {sid[:13]} {mb:7.1f}MB "
                      f"{n:5} msgs  already there")
                read += n
                written += n
                continue
        r, w = await _messages(
            container, sid, path, type_field=type_field, apply=apply
        )
        read += r
        written += w
        if not apply:
            print(f"  [{i}/{len(files)}] {sid[:13]} {mb:7.1f}MB {r:5} msgs")
            continue
        after = await _count(container, sid, type_field)
        flag = "" if after >= r else "  <-- FEWER THAN EXPECTED"
        print(f"  [{i}/{len(files)}] {sid[:13]} {mb:7.1f}MB {w:5} written, "
              f"{after:5} there{flag}")
    return read, written


async def do_meta(cfg, *, apply: bool, resume: bool) -> tuple[int, int]:
    from compass.common.persistence.session_meta import (
        CosmosSessionMetaStore,
        LocalSessionMetaStore,
        SessionMeta,
    )

    rows = await LocalSessionMetaStore().list_all()
    if not apply:
        print(f"  {len(rows)} session(s) -> {cfg.cosmos_meta_container}")
        return len(rows), 0
    store = CosmosSessionMetaStore()
    written = 0
    try:
        for meta in rows:
            try:
                await store.upsert(meta)
                written += 1
            except Exception as err:  # noqa: BLE001
                print(f"      ! {meta.id}: {err}")
        after = len(await store.list_all())
        flag = "" if after >= len(rows) else "  <-- FEWER THAN EXPECTED"
        print(f"  {written} written, {after} in {cfg.cosmos_meta_container}{flag}")
    finally:
        await store.close()
    return len(rows), written


async def do_memory(cfg, *, apply: bool, resume: bool) -> tuple[int, int]:
    from compass.common.memory import MemoryStore
    from compass.common.persistence.memory_cosmos import CosmosMemoryStore

    rows = await MemoryStore().list()
    if not apply:
        print(f"  {len(rows)} entr(ies) -> memory")
        return len(rows), 0
    store = CosmosMemoryStore()
    written = 0
    try:
        container = await store._get_container()
        for row in rows:
            try:
                # Written whole rather than through `add`, which would mint a
                # new id and a new timestamp — and so would duplicate every
                # entry on a second run.
                await container.upsert_item(dict(row))
                written += 1
            except Exception as err:  # noqa: BLE001
                print(f"      ! {row.get('id')}: {err}")
        after = len(await store.list())
        flag = "" if after >= len(rows) else "  <-- FEWER THAN EXPECTED"
        print(f"  {written} written, {after} in memory{flag}")
    finally:
        await store.close()
    return len(rows), written


PART_FUNCS = {
    "transcripts": do_transcripts,
    "meta": do_meta,
    "chat": do_chat,
    "memory": do_memory,
}


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, nothing is sent.")
    ap.add_argument("--only", default="",
                    help=f"comma-separated: {', '.join(PARTS)}. Default is all.")
    ap.add_argument("--resume", action="store_true",
                    help="skip a session that already has all of its messages")
    args = ap.parse_args()

    from compass.common.config import get_settings

    cfg = get_settings().storage
    if not cfg.cosmos_configured:
        print("Cosmos is not configured — set AZURE_COSMOS_ENDPOINT and "
              "AZURE_COSMOS_KEY first.")
        return 2
    if not cfg.blob_configured:
        print("Note: blob storage is not configured, so a message too large to "
              "be a document will be kept under data/message_content/ instead. "
              "That works, but it is not the cloud.\n")

    wanted = [n.strip() for n in args.only.split(",") if n.strip()] or list(PARTS)
    unknown = set(wanted) - set(PARTS)
    if unknown:
        print(f"unknown part(s): {', '.join(sorted(unknown))}")
        print(f"known: {', '.join(PARTS)}")
        return 2

    print(f"database {cfg.cosmos_database} at "
          f"{cfg.cosmos_endpoint.split('//')[-1].rstrip('/')}")
    print("dry run — nothing will be written\n" if not args.apply else "writing\n")

    # Reading is local and writing is Cosmos, in one process. The local reads
    # here go through the Local* classes directly, so the switch only affects
    # what the Cosmos stores need it to say.
    was, cfg.backend = cfg.backend, "cosmos"
    totals: dict[str, tuple[int, int]] = {}
    try:
        for name in wanted:
            print(f"{name}:")
            try:
                totals[name] = await PART_FUNCS[name](
                    cfg, apply=args.apply, resume=args.resume
                )
            except Exception as err:  # noqa: BLE001 — one part, not the run
                # Each part is independent, so one of them failing should cost
                # you that part and not the three after it. Recorded as a
                # shortfall so the exit status still says something went wrong.
                print(f"  ! {name} did not finish: {type(err).__name__}: {err}")
                totals[name] = (1, 0)
            print()
    finally:
        cfg.backend = was

    read = sum(r for r, _ in totals.values())
    written = sum(w for _, w in totals.values())
    if not args.apply:
        print(f"{read} document(s) would move. Re-run with --apply.")
        return 0
    print(f"{written} of {read} document(s) written.")
    if written < read:
        print("Some did not arrive — re-run with --apply --resume.")
        return 1
    print("Local files are untouched; set COMPASS_STORAGE_BACKEND=cosmos to use them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
