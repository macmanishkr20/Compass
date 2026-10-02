#!/usr/bin/env python3
"""Write index rows for the pictures, films and uploads already in storage.

    python scripts/index_existing_media.py            # say what would be indexed
    python scripts/index_existing_media.py --apply

The index (compass/common/media_index.py) is written as things are made. It
started empty, and everything made before it existed has bytes in blob and no
row describing them — which is enough to show a picture again, and not enough
to list what somebody has made or to find it from another machine.

Three prefixes in `compass-media`, and what can be known about each:

  generated/   drawn or edited pictures. The session and owner are not in the
               blob name, so they are recovered by looking for the URL in the
               stored conversations — which is where it was written when the
               model answered with it.
  chat/        a thread's uploads and the films cut from them. The session IS
               the path, and the owner comes from the thread's own record.
  screenshots/ what the agent saw. No session in the name and no reference in
               the text — the id is served to the browser rather than written
               into the message — so these are indexed without one.

Idempotent: a blob that already has a row is left alone, so this can be run
again after more work without duplicating anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="actually write the rows")
    args = ap.parse_args()

    from compass.common import media_index
    from compass.common.config import get_settings
    from compass.common.persistence import blob
    from compass.common.persistence.shutdown import close_all

    cfg = get_settings().storage
    if not cfg.blob_configured or not cfg.cosmos_configured:
        print("Needs both blob and Cosmos configured.")
        return 2
    was, cfg.backend = cfg.backend, "cosmos"
    try:
        known = {r.get("blob_name") for r in await media_index._media.all()}
        print(f"{len(known)} already indexed\n")

        container = blob.container("compass-media")
        blobs = [
            (b.name, b.size)
            for b in container.list_blobs()
            if b.name.split("/", 1)[0] in ("generated", "chat", "screenshots")
        ]

        # Where each generated picture was answered, so it can be attributed.
        from azure.cosmos.aio import CosmosClient

        client = CosmosClient(cfg.cosmos_endpoint, credential=cfg.cosmos_key)
        db = client.get_database_client(cfg.cosmos_database)
        where: dict[str, str] = {}
        owners: dict[str, str] = {}
        try:
            for cname in ("chat", "transcripts"):
                async for row in db.get_container_client(cname).query_items(
                    "SELECT * FROM c"
                ):
                    sid = row.get("sessionId", "")
                    if row.get("type") == "meta" and row.get("owner"):
                        owners[sid] = row["owner"]
                    for hit in re.findall(
                        r"/v1/media/generated/([0-9a-f]{32}\.png)",
                        json.dumps(row, default=str),
                    ):
                        where.setdefault(f"generated/{hit}", sid)
        finally:
            await client.close()

        plan: list[dict] = []
        for name, size in blobs:
            if name in known:
                continue
            prefix = name.split("/", 1)[0]
            session = ""
            kind = "image"
            if prefix == "chat":
                parts = name.split("/")
                session = parts[1] if len(parts) > 2 else ""
                kind = "video" if name.lower().endswith((".mp4", ".webm", ".mov")) else "upload"
            elif prefix == "generated":
                session = where.get(name, "")
            else:
                kind = "screenshot"
            plan.append({
                "blob_name": name,
                "url": f"/v1/media/{name}",
                "kind": kind,
                "session_id": session,
                "owner": owners.get(session, ""),
                "size_bytes": size,
            })

        by_kind: dict[str, int] = {}
        attributed = 0
        for row in plan:
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
            if row["session_id"]:
                attributed += 1
        print(f"{len(plan)} to index: {by_kind}")
        print(f"  with a conversation: {attributed}   without: {len(plan) - attributed}")
        if not args.apply:
            print("\nNothing written. Re-run with --apply.")
            return 0

        written = 0
        for row in plan:
            if await media_index.record(**row):
                written += 1
        after = len({r.get("blob_name") for r in await media_index._media.all()})
        print(f"\n{written} written; {after} blobs indexed in total.")
        await close_all()
        return 0 if written == len(plan) else 1
    finally:
        cfg.backend = was


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
