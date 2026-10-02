#!/usr/bin/env python3
"""Put a user on the stored pictures and screenshots that have none.

    python scripts/claim_unowned_media.py                      # say what would change
    python scripts/claim_unowned_media.py --apply --to <email>

The sibling of `attribute_unowned.py`, which does this for conversations.
This one does it for the media index: the rows that say what a stored blob
is. Rows written before owners were recorded have the field empty, and so do
the ones `index_existing_media.py` wrote for blobs whose owner could not be
recovered — a screenshot's id is served to the browser rather than written
into the message, so nothing in the stored conversation points back at it.

WHY IT MATTERS, both ways round:

  * Nobody can find them. `media_index.for_owner` matches the owner exactly,
    so a row owned by "" comes back for no account at all — not the person
    who made it either. "Everything I have made" silently skips them.

  * The route that serves screenshots checks the recorded owner, and an empty
    one is treated as legacy and served to any signed-in caller. That is the
    documented rule in `compass.common.ownership` and the right default for a
    box with one account. On a box with several it is a hole, and the fix is
    not to change the rule but to stop leaving the field empty.

WHAT THIS CHANGES. The named user becomes the owner of those rows. Nothing
else: not the bytes, not the conversations, not rows that already have an
owner. Screenshot rows also have their `url` corrected to the path that
actually serves them, `/v1/screenshot-cache/<id>` — the indexing script wrote
`/v1/media/<blob name>`, which is not a route.

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


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--to", default="", help="the account to attribute them to")
    ap.add_argument("--force", action="store_true",
                    help="proceed even though more than one account exists")
    args = ap.parse_args()

    from compass.common import media_index
    from compass.common.config import get_settings
    from compass.common.persistence.shutdown import close_all
    from compass.common.users import _users

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
            print(f"{len(accounts)} accounts exist. Naming one would hide this "
                  f"media from the others — re-run with --force if that is "
                  f"what you mean.")
            return 2
        print(f"attributing to: {target}\n")

        rows = await media_index._media.all()
        unowned = [r for r in rows if not (r.get("owner") or "").strip()]
        by_kind: dict[str, int] = {}
        for row in unowned:
            kind = str(row.get("kind") or "?")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        print(f"  {len(rows)} rows indexed; {len(unowned)} without an owner")
        for kind in sorted(by_kind):
            print(f"      {kind:<12} {by_kind[kind]}")
        if not args.apply:
            print("\nNothing written. Re-run with --apply.")
            await close_all()
            return 0

        done = 0
        for row in unowned:
            row["owner"] = target
            name = str(row.get("blob_name") or "")
            if row.get("kind") == "screenshot" and name.startswith("screenshots/"):
                sid = name[len("screenshots/"):].removesuffix(".png")
                row["url"] = f"/v1/screenshot-cache/{sid}"
            await media_index._media.put(row)
            done += 1

        after = await media_index._media.all()
        left = sum(1 for r in after if not (r.get("owner") or "").strip())
        mine = len(await media_index.for_owner(target))
        print(f"\n{done} attributed; {left} still without an owner.")
        print(f"{len(after)} rows in total, {mine} now owned by {target}.")
        await close_all()
        return 0 if left == 0 and len(after) == len(rows) else 1
    finally:
        cfg.backend = was


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
