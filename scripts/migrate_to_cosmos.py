#!/usr/bin/env python3
"""Copy the small stores from local JSON files into Cosmos.

    python scripts/migrate_to_cosmos.py            # say what would move
    python scripts/migrate_to_cosmos.py --apply    # move it
    python scripts/migrate_to_cosmos.py --apply --only missions,prompts

Reads the files under `data/` and writes them through the same Collection
definitions the application uses, so there is one description of what each
collection is called, which container it belongs in and how it is
partitioned — and no second copy of that to drift.

Three properties worth stating, because a migration that lacks them is one
nobody can run twice:

  * Nothing local is deleted or changed. The files stay exactly as they are,
    so switching back is changing one environment variable.
  * It is idempotent. Documents are written by their own id, so a second run
    overwrites rather than duplicates, and an interrupted run is finished by
    running it again.
  * It reports per collection and compares counts afterwards, because "it
    printed no errors" is not the same as "the data is there".

Not covered: transcripts, chat threads and memory. Those already have Cosmos
backends of their own, and they are the large, append-heavy ones — 531MB of
sessions here — with the 2MB item limit to respect. They need their own
migration, not a line in this one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _collections() -> dict:
    """Every collection this script knows how to move.

    Imported here rather than at module scope so `--help` works without a
    configured backend, and so an import failure names the collection it
    happened in.
    """
    from compass.code.routines import _routines, _runs
    from compass.common.users import _users
    from compass.common.workspaces import _workspaces
    from compass.estimate.store import EstimateStore, RateCardStore
    from compass.home.prompts import _prompts
    from compass.missions.store import _missions
    from compass.design.store import _projects as _designs
    from compass.pipelines.store import ConnectionStore, PipelineStore, RunStore

    return {
        "designs": _designs,
        "prompts": _prompts(),
        "missions": _missions,
        "routines": _routines,
        "routine_runs": _runs,
        "workspaces": _workspaces,
        "users": _users,
        "estimates": EstimateStore()._items,
        "rate_cards": RateCardStore()._items,
        "pipelines": PipelineStore()._items,
        "connections": ConnectionStore()._items,
        "pipeline_runs": RunStore()._items,
    }


async def _prepare(name: str, row: dict) -> dict:
    """Anything a document needs before it can be stored.

    Only designs need it, and they need it badly: a version is a full HTML
    snapshot, and on this install they were 21.8MB of a 24.8MB store — five
    projects of thirty-seven were already past Cosmos's 2MB item limit and
    could not have been written at all. The snapshots go to blob storage and
    the project keeps the reference, which is what the application does on
    every save now; this brings the existing ones into line.
    """
    if name != "designs":
        return row
    from compass.design import version_html

    row = dict(row)
    row["versions"] = await version_html.spill(
        str(row.get("id", "")), list(row.get("versions") or [])
    )
    return row


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, nothing is sent.")
    ap.add_argument("--only", default="",
                    help="comma-separated collection names; default is all")
    args = ap.parse_args()

    from compass.common.config import get_settings
    from compass.common.persistence import catalog

    cfg = get_settings().storage
    if not cfg.cosmos_configured:
        print("Cosmos is not configured — set AZURE_COSMOS_ENDPOINT and "
              "AZURE_COSMOS_KEY first.")
        return 2

    wanted = {n.strip() for n in args.only.split(",") if n.strip()}
    collections = _collections()
    if wanted:
        unknown = wanted - collections.keys()
        if unknown:
            print(f"unknown collection(s): {', '.join(sorted(unknown))}")
            print(f"known: {', '.join(sorted(collections))}")
            return 2
        collections = {k: v for k, v in collections.items() if k in wanted}

    print(f"database {cfg.cosmos_database} at "
          f"{cfg.cosmos_endpoint.split('//')[-1].rstrip('/')}")
    print("dry run — nothing will be written\n" if not args.apply
          else "writing\n")

    # Reading is local and writing is Cosmos, in one process: the local read
    # goes through `local_rows`, which does not consult the backend, while
    # the writes need the backend to say cosmos.
    was, cfg.backend = cfg.backend, "cosmos"
    total_read = total_written = 0
    failures: list[str] = []
    try:
        for name, coll in collections.items():
            rows = coll.local_rows()
            total_read += len(rows)
            if not args.apply:
                print(f"  {name:15} {len(rows):5} document(s) -> {coll.container}")
                continue
            written = 0
            for row in rows:
                try:
                    await coll.put(await _prepare(name, row))
                    written += 1
                except Exception as err:  # noqa: BLE001 — report, keep going
                    failures.append(f"{name}/{row.get(coll.id_field, '?')}: {err}")
            after = len(await coll.all())
            total_written += written
            flag = "" if after >= len(rows) else "  <-- FEWER THAN EXPECTED"
            print(f"  {name:15} {written:5} written, {after:5} now in "
                  f"{coll.container}{flag}")
        await catalog.close()
    finally:
        cfg.backend = was

    print()
    if not args.apply:
        print(f"{total_read} document(s) would move. Re-run with --apply.")
        return 0
    print(f"{total_written} document(s) written.")
    if failures:
        print(f"\n{len(failures)} failed:")
        for f in failures[:20]:
            print(f"  {f}")
        return 1
    print("Local files are untouched; set COMPASS_STORAGE_BACKEND=cosmos to use them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
