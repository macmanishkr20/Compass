#!/usr/bin/env python3
"""Move what is already stored into blob storage, and leave metadata behind.

    python scripts/move_large_content_to_blob.py                 # say what would move
    python scripts/move_large_content_to_blob.py --apply
    python scripts/move_large_content_to_blob.py --apply --only media

The rule is one sentence: a document holds what a document is for, and a
picture, a film, a recording or a page of markup is not one of those. The code
writes new data that way already. This brings across what was written before.

Three parts, and `--only` selects them by name:

  designs    a project's live markup -> compass-design, under `pages/`
             Measured before this: of 3.17MB in the `designs` container,
             `html` was 1.552MB and `pages` 1.485MB — 96% was markup, four
             projects with photographs base64'd into it.

  media      data/sessions/chat_media -> compass-media, under `chat/`
             286MB on one laptop: 162 photos, 20 films, 6 recordings. The
             only copy of any of it, gitignored and unbacked. The directory
             stays where it is and keeps working — ffmpeg needs a path — but
             it stops being the only copy.

  messages   message content carrying an attachment -> compass-media
             A size limit alone left a 362KB photograph in a document because
             it fitted. Five messages held 1.24MB of images that way.

Idempotent, and safe to stop. Everything is keyed by the data's own ids, so a
second run overwrites rather than duplicates and an interrupted run is
finished by running it again. Nothing is deleted: the local files stay, and
switching back stays one environment variable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PARTS = ("designs", "media", "messages")


def _size(obj) -> int:
    return len(json.dumps(obj, default=str).encode())


# --------------------------------------------------------------------------- #
async def do_designs(*, apply: bool) -> tuple[int, int]:
    """Re-save every project, which files its markup away on the way out."""
    from compass.design import markup
    from compass.design.store import _projects, get_design_store

    store = get_design_store()
    rows = await _projects.all()
    todo = [r for r in rows if (r.get("html") or "")
            or any(p.get("html") for p in (r.get("pages") or []))]
    inline = sum(_size(r.get("html")) + _size(r.get("pages")) for r in todo)
    print(f"  {len(rows)} project(s), {len(todo)} still holding markup "
          f"({inline / 1e6:.2f}MB)")
    if not apply:
        return len(todo), 0

    moved = 0
    for row in todo:
        pid = str(row.get("id", ""))
        try:
            # Filled first — a project whose markup is partly moved already
            # must be read whole before being written whole.
            await store._save(await markup.fill(row))
            moved += 1
        except Exception as err:  # noqa: BLE001 — report, keep going
            print(f"      ! {pid}: {err}")
    after = await _projects.all()
    left = [r for r in after if (r.get("html") or "")
            or any(p.get("html") for p in (r.get("pages") or []))]
    held = sum(_size(r) for r in after)
    print(f"  {moved} moved; {len(left)} still inline; "
          f"container now {held / 1e6:.2f}MB")
    return len(todo), moved


# --------------------------------------------------------------------------- #
async def do_media(*, apply: bool) -> tuple[int, int]:
    """Upload every thread's uploads. The directory is left exactly as it is."""
    from compass.common.config import get_settings
    from compass.home import media

    root = get_settings().sessions_dir / "chat_media"
    if not root.is_dir():
        print("  no chat_media directory — nothing to move")
        return 0, 0
    sessions = sorted(p for p in root.iterdir() if p.is_dir())
    files = [f for s in sessions for f in s.iterdir() if f.is_file()]
    total = sum(f.stat().st_size for f in files)
    print(f"  {len(sessions)} thread(s), {len(files)} file(s), {total / 1e6:.1f}MB")
    if not apply:
        return len(files), 0

    moved = 0
    for session in sessions:
        local = [f for f in session.iterdir() if f.is_file()]
        mb = sum(f.stat().st_size for f in local) / 1e6
        n = await media.upload(session.name)
        # Counted from storage rather than from the return value, so the number
        # printed is what is actually there.
        stored = await asyncio.to_thread(media._names_sync, session.name)
        flag = "" if len(stored) >= len(local) else "  <-- FEWER THAN EXPECTED"
        print(f"    {session.name[:13]} {mb:7.1f}MB  {n:3} sent, "
              f"{len(stored):3} stored{flag}")
        moved += n
    return len(files), moved


# --------------------------------------------------------------------------- #
async def do_messages(*, apply: bool) -> tuple[int, int]:
    """Re-store message content that now counts as an attachment."""
    from compass.common.config import get_settings
    from compass.common.persistence import large_content as lc

    cfg = get_settings().storage
    from azure.cosmos.aio import CosmosClient

    client = CosmosClient(cfg.cosmos_endpoint, credential=cfg.cosmos_key)
    db = client.get_database_client(cfg.cosmos_database)
    read = moved = 0
    try:
        for name in ("chat", "transcripts"):
            container = db.get_container_client(name)
            rows = [d async for d in container.query_items("SELECT * FROM c")]
            todo = []
            for row in rows:
                record = row.get("record")
                if not record or record.get(lc.CONTENT_REF):
                    continue
                content = record.get("content")
                if content is None:
                    continue
                payload = (content if isinstance(content, str)
                           else json.dumps(content, default=str))
                if lc._should_store(payload):
                    todo.append((row, len(payload.encode())))
            read += len(todo)
            print(f"  {name:12} {len(rows):5} document(s), {len(todo)} to move "
                  f"({sum(n for _, n in todo) / 1e6:.3f}MB)")
            if not apply:
                continue
            for row, _ in todo:
                try:
                    row["record"] = await lc.spill(row["record"], row["sessionId"])
                    await container.upsert_item(row)
                    moved += 1
                except Exception as err:  # noqa: BLE001
                    print(f"      ! {row.get('id')}: {err}")
            if todo:
                print(f"  {name:12} {moved} moved")
    finally:
        await client.close()
    return read, moved


PART_FUNCS = {"designs": do_designs, "media": do_media, "messages": do_messages}


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true",
                    help="actually move. Without it, nothing is written.")
    ap.add_argument("--only", default="",
                    help=f"comma-separated: {', '.join(PARTS)}. Default is all.")
    args = ap.parse_args()

    from compass.common.config import get_settings
    from compass.common.persistence.shutdown import close_all

    cfg = get_settings().storage
    if not cfg.blob_configured:
        print("Blob storage is not configured — set AZURE_STORAGE_CONNECTION_STRING.")
        return 2
    if not cfg.cosmos_configured:
        print("Cosmos is not configured — set AZURE_COSMOS_ENDPOINT and AZURE_COSMOS_KEY.")
        return 2

    wanted = [n.strip() for n in args.only.split(",") if n.strip()] or list(PARTS)
    unknown = set(wanted) - set(PARTS)
    if unknown:
        print(f"unknown part(s): {', '.join(sorted(unknown))}")
        print(f"known: {', '.join(PARTS)}")
        return 2

    print("dry run — nothing will be written\n" if not args.apply else "moving\n")
    was, cfg.backend = cfg.backend, "cosmos"
    totals: dict[str, tuple[int, int]] = {}
    try:
        for name in wanted:
            print(f"{name}:")
            try:
                totals[name] = await PART_FUNCS[name](apply=args.apply)
            except Exception as err:  # noqa: BLE001 — one part, not the run
                print(f"  ! {name} did not finish: {type(err).__name__}: {err}")
                totals[name] = (1, 0)
            print()
        await close_all()
    finally:
        cfg.backend = was

    read = sum(r for r, _ in totals.values())
    moved = sum(m for _, m in totals.values())
    if not args.apply:
        print(f"{read} item(s) would move. Re-run with --apply.")
        return 0
    print(f"{moved} of {read} item(s) moved.")
    if moved < read:
        print("Some did not move — re-run with --apply; it is idempotent.")
        return 1
    print("Local files are untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
