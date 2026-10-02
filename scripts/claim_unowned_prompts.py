#!/usr/bin/env python3
"""Put a user on the saved prompts that have none.

    python scripts/claim_unowned_prompts.py                      # say what would change
    python scripts/claim_unowned_prompts.py --apply --to <email>

The prompt library is stricter than the rest of Compass. Everywhere else an
empty owner is legacy and stays visible to everyone; here it is matched
exactly, because a saved prompt is written in the first person about the
person's own day and showing one to a stranger is a different kind of wrong.

The price of that strictness is this script. A prompt saved before owners
were recorded belongs to nobody, and nobody -- including whoever wrote it --
will see it again until it is named. Run this once after upgrading.
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
    ap.add_argument("--to", default="", help="the account to attribute them to")
    ap.add_argument("--force", action="store_true",
                    help="proceed even though more than one account exists")
    args = ap.parse_args()

    from compass.common.config import get_settings
    from compass.common.persistence.shutdown import close_all
    from compass.common.users import _users
    from compass.home.prompts import _prompts, get_prompt_library

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
                  f"prompts from the others — re-run with --force if that is "
                  f"what you mean.")
            return 2
        print(f"attributing to: {target}\n")

        rows = await _prompts().all()
        unowned = [r for r in rows if not (r.get("owner") or "").strip()]
        print(f"  {len(rows)} saved prompts; {len(unowned)} without an owner")
        for row in unowned:
            print(f"      {str(row.get('title') or '')[:60]!r}")
        if not unowned:
            await close_all()
            return 0
        if not args.apply:
            print("\nNothing written. Re-run with --apply.")
            await close_all()
            return 0

        for row in unowned:
            row["owner"] = target
            await _prompts().put(row)
        left = sum(1 for r in await _prompts().all()
                   if not (r.get("owner") or "").strip())
        mine = len(await get_prompt_library().list(target))
        print(f"\n{len(unowned)} attributed; {left} still without an owner.")
        print(f"{target} now has {mine} saved prompt(s).")
        await close_all()
        return 0 if left == 0 else 1
    finally:
        cfg.backend = was


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
