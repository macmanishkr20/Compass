#!/usr/bin/env python3
"""Put each design project's own files into blob storage.

    python scripts/migrate_design_files.py            # say what would go
    python scripts/migrate_design_files.py --apply

A design's markup, its version history and its record all moved to the
cloud. The folder of files beside it — the assets it was given, the scraps
it made, the images dropped onto it — did not, so a project opened on a
second machine showed an empty Files pane, and one restored from Cosmos came
back without the things it was built from.

They are written through from now on. This carries across what is already on
the disk of this machine.

A folder whose project no longer exists is reported and left alone: those
are the leftovers of designs deleted before the files were cleaned up with
them, and uploading them would put back exactly what somebody removed.

Idempotent: a file already in blob at the same size is skipped, so this can
be run again. The disk copies stay — they are the working copy the Files
pane reads, and nothing here is removed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from compass.common.config import get_settings
    from compass.common.persistence.shutdown import close_all
    from compass.design import files as design_files
    from compass.design.store import get_design_store

    settings = get_settings()
    cfg = settings.storage
    if not cfg.blob_configured:
        print("Blob storage is not configured; nothing to migrate to.")
        return 2
    was, cfg.backend = cfg.backend, "cosmos"
    try:
        root = settings.workspace_root / settings.data_dir / "design_files"
        if not root.is_dir():
            print("No design_files folder on this machine.")
            await close_all()
            return 0

        known = {str(p.get("id")) for p in await get_design_store().list()}
        folders = sorted(d for d in root.iterdir() if d.is_dir())
        print(f"{len(folders)} project folder(s) on disk; "
              f"{len(known)} project(s) on record\n")

        plan: list[tuple[str, str, int]] = []
        orphans: list[str] = []
        for folder in folders:
            pid = folder.name
            if pid not in known:
                orphans.append(pid)
                continue
            try:
                remote = await asyncio.to_thread(design_files._names_sync, pid)
            except Exception as err:  # noqa: BLE001
                print(f"  {pid}: could not list blob ({err}); skipping")
                continue
            for path in sorted(folder.rglob("*")):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(folder))
                size = path.stat().st_size
                if remote.get(rel) == size:
                    continue
                plan.append((pid, rel, size))

        by_project: dict[str, int] = {}
        total = 0
        for pid, _rel, size in plan:
            by_project[pid] = by_project.get(pid, 0) + 1
            total += size
        for pid, count in sorted(by_project.items()):
            print(f"  {pid}  {count} file(s)")
        print(f"\n{len(plan)} file(s) to upload, {total / 1_048_576:.1f} MB")
        if orphans:
            print(f"\n{len(orphans)} folder(s) belong to no project and are left "
                  f"alone:\n   " + ", ".join(orphans))
        if not plan:
            await close_all()
            return 0
        if not args.apply:
            print("\nNothing uploaded. Re-run with --apply.")
            await close_all()
            return 0

        done = 0
        for pid, rel, _size in plan:
            if await design_files.store(pid, rel):
                done += 1
        print(f"\n{done} of {len(plan)} uploaded.")
        await close_all()
        return 0 if done == len(plan) else 1
    finally:
        cfg.backend = was


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
