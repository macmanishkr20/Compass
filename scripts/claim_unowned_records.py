#!/usr/bin/env python3
"""Put a user on the pipelines, runs, estimates and missions that have none.

    python scripts/claim_unowned_records.py                      # say what would change
    python scripts/claim_unowned_records.py --apply --to <email>

The third of these, after `attribute_unowned.py` (conversations) and
`claim_unowned_media.py` (stored files). This one covers the remaining
owner-bearing stores: pipelines, their connections and runs, estimates and
missions.

WHY AN EMPTY OWNER IS NOT THE SAME AS A HIDDEN RECORD. `compass.common.
ownership` fails open: a record owned by nobody is shown to everybody. That
is deliberate and right for a box that has only ever had one account, where
the alternative looks exactly like data loss. It is wrong the moment a second
person signs in — they are shown the first person's pipelines, their
connections, their estimates and their missions, because none of those
records say whose they are.

Filtering already happens on the way out for all of these. The records simply
had nothing to filter on.

WHAT THIS CHANGES. The named user becomes the owner of those records. They
then appear for that account and no other. Nothing else is touched: not the
pipelines' definitions, not the runs' results, not the workspaces a mission
built. A record that already has an owner is left exactly as it is.

Reversible: owner is a field, and setting it back to "" restores what was
there. It refuses when more than one account exists unless you say --force,
because on such a box naming one person hides these from everybody else.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _owner(record) -> str:
    value = (record.get("owner") if isinstance(record, dict)
             else getattr(record, "owner", ""))
    return (value or "").strip()


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--to", default="", help="the account to attribute them to")
    ap.add_argument("--force", action="store_true",
                    help="proceed even though more than one account exists")
    args = ap.parse_args()

    from compass.common.config import get_settings
    from compass.common.persistence.shutdown import close_all
    from compass.common.users import _users
    from compass.estimate import store as estore
    from compass.missions.store import get_mission_store
    from compass.pipelines import store as pstore

    cfg = get_settings().storage
    was, cfg.backend = cfg.backend, "cosmos"
    try:
        accounts = [str(u.get("id") or u.get("email") or "") for u in await _users.all()]
        accounts = [a for a in accounts if a]
        print(f"accounts on record: {accounts or '(none)'}")
        target = args.to or (accounts[0] if len(accounts) == 1 else "")
        if not target:
            print("Say who with --to <email>; there is no single obvious account.")
            return 2
        if len(accounts) > 1 and not args.force:
            print(f"{len(accounts)} accounts exist. Naming one would hide these "
                  f"records from the others — re-run with --force if that is "
                  f"what you mean.")
            return 2
        print(f"attributing to: {target}\n")

        missions = get_mission_store()

        # Each store is written back the way that store writes itself.
        # Three of them need saying explicitly:
        #
        #   pipelines    `save` bumps the version by default, and this is not
        #                an edit to the pipeline — v8 must still read v8.
        #   connections  has no `save` at all; `create` mints a fresh id, so
        #                the record goes back through the underlying write
        #                under the id it already has.
        #   runs         `list` is a UI listing and stops at 50. The default
        #                would silently leave every older run unowned.
        from dataclasses import asdict

        async def save_pipeline(record):
            await pstore.pipelines.save(record, bump=False)

        async def save_connection(record):
            await pstore.connections._write(record.id, asdict(record))

        async def all_runs():
            return await pstore.runs.list(limit=1_000_000)

        # (label, read everything, write one back)
        groups = [
            ("pipelines",   pstore.pipelines.list,   save_pipeline),
            ("connections", pstore.connections.list, save_connection),
            ("runs",        all_runs,                pstore.runs.save),
            ("estimates",   estore.estimates.list,   estore.estimates.save),
            ("missions",    missions.list,           missions.save),
        ]

        plan: list[tuple[str, list]] = []
        for label, read, _ in groups:
            rows = await read()
            unowned = [r for r in rows if not _owner(r)]
            plan.append((label, unowned))
            print(f"  {label:<12} {len(rows):>4} total, {len(unowned):>4} without an owner")
        if not any(rows for _, rows in plan):
            print("\nNothing to do — every record already names an owner.")
            await close_all()
            return 0
        if not args.apply:
            print("\nNothing written. Re-run with --apply.")
            await close_all()
            return 0

        done = 0
        for (label, unowned), (_, _, save) in zip(plan, groups):
            for record in unowned:
                if isinstance(record, dict):
                    record["owner"] = target
                else:
                    record.owner = target
                await save(record)
                done += 1

        left = 0
        for label, read, _ in groups:
            still = [r for r in await read() if not _owner(r)]
            left += len(still)
            print(f"  {label:<12} {len(still)} still without an owner")
        print(f"\n{done} attributed; {left} left.")
        await close_all()
        return 0 if left == 0 else 1
    finally:
        cfg.backend = was


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
