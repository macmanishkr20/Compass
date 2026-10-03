#!/usr/bin/env python3
"""Move each person's prompt arrangement off the disk and into the catalog.

    python scripts/migrate_prompt_order.py            # say what would move
    python scripts/migrate_prompt_order.py --apply

The library's prompts have always been records; the order they are shown in
was a JSON file beside them, which made it the last thing in Home that did
not travel. Somebody who arranged their prompts on one machine found them
unarranged on the next, and the file was the only copy there was.

FINDING WHOSE IS WHOSE. The files are named by a hash of the owner, so the
owner cannot be read back out of the name — it has to be guessed and
checked. Every account on record is hashed the same way and matched against
what is on disk; `prompt_order.json`, with no suffix, is the arrangement
from before accounts existed and belongs to nobody.

A file whose hash matches no account is left alone and reported. That is an
arrangement belonging to an account that has since been removed, and there
is nobody to give it to.

Idempotent: an owner who already has a row in the catalog is skipped, so
this can be run again after more arranging without overwriting it. The
files are left on disk either way — removing them is a separate decision,
and they are a few hundred bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _tag(owner: str) -> str:
    """The same 16 hex characters the store used to name the file with."""
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from compass.common.config import get_settings
    from compass.common.persistence.shutdown import close_all
    from compass.common.users import _users
    from compass.home.prompts import _NOBODY, _clean_keys, _order_rows

    settings = get_settings()
    cfg = settings.storage
    was, cfg.backend = cfg.backend, "cosmos"
    try:
        folder = settings.workspace_root / settings.data_dir
        accounts = [str(u.get("id") or u.get("email") or "") for u in await _users.all()]
        accounts = [a for a in accounts if a]
        print(f"accounts on record: {accounts or '(none)'}")

        # owner -> the file that holds their arrangement
        wanted: dict[str, Path] = {}
        plain = folder / "prompt_order.json"
        if plain.is_file():
            wanted[_NOBODY] = plain
        for account in accounts:
            path = folder / f"prompt_order.{_tag(account)}.json"
            if path.is_file():
                wanted[account] = path

        found = {p.name for p in folder.glob("prompt_order.*.json")}
        claimed = {p.name for p in wanted.values()}
        orphans = sorted(found - claimed)

        existing = {str(r.get("id")) for r in await _order_rows().all()}
        print(f"\n{len(wanted)} arrangement(s) on disk with an owner:")
        plan: list[tuple[str, Path, list[str]]] = []
        for owner, path in wanted.items():
            try:
                keys = _clean_keys(json.loads(path.read_text() or "[]"))
            except (OSError, json.JSONDecodeError):
                print(f"   {owner:<28} {path.name}  UNREADABLE, skipping")
                continue
            if owner in existing:
                print(f"   {owner:<28} already in the catalog, skipping")
                continue
            plan.append((owner, path, keys))
            print(f"   {owner:<28} {len(keys)} key(s) from {path.name}")
        if orphans:
            print(f"\n{len(orphans)} file(s) match no account and are left alone:")
            for name in orphans:
                print(f"   {name}")

        if not plan:
            print("\nNothing to move.")
            await close_all()
            return 0
        if not args.apply:
            print("\nNothing written. Re-run with --apply.")
            await close_all()
            return 0

        for owner, _path, keys in plan:
            await _order_rows().put({"id": owner, "owner": owner, "keys": keys})
        after = {str(r.get("id")) for r in await _order_rows().all()}
        moved = sum(1 for owner, _p, _k in plan if owner in after)
        print(f"\n{moved} of {len(plan)} moved into the catalog.")
        print("The files are left on disk; delete them when you are satisfied.")
        await close_all()
        return 0 if moved == len(plan) else 1
    finally:
        cfg.backend = was


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
