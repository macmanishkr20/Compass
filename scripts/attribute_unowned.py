#!/usr/bin/env python3
"""Put a user on conversations that have none, so their work is theirs.

    python scripts/attribute_unowned.py                      # say what would change
    python scripts/attribute_unowned.py --apply --to <email>

A conversation records who it belongs to. Ones created before that field
existed have it empty, and so do the Code conversations rebuilt from their
transcripts — a transcript says what was said, not whose account said it.

Empty does not mean hidden: `compass.common.ownership` fails open, so an
unowned conversation is visible to everyone. That is the right default for a
single-person install and the wrong one for anything else, and it has a
second cost even alone: anything asked *by user* — what have I made, show me
my pictures — cannot see them, because they are not anybody's.

WHAT THIS CHANGES. After it runs, those conversations and the media filed
under them belong to the named user. On an install with one account that is
simply the truth written down. On an install with several it would hide them
from everybody else, which is why the user is named explicitly rather than
guessed, and why it refuses when more than one account exists unless you say
`--force`.

Reversible: owners are a field, and setting them back to "" restores exactly
what was there. Nothing else is touched.
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
    from compass.common.persistence.session_meta import get_meta_store
    from compass.common.persistence.shutdown import close_all
    from compass.common.users import _users
    from compass.home.engine import get_chat_store

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
                  f"conversations from the others — re-run with --force if that "
                  f"is what you mean.")
            return 2
        print(f"attributing to: {target}\n")

        meta = get_meta_store()
        chat = get_chat_store()
        code_rows = [m for m in await meta.list_all() if not (m.owner or "").strip()]
        chat_cards = [c for c in await chat.list_cards() if not (c.get("owner") or "").strip()]
        print(f"  Code conversations without an owner: {len(code_rows)}")
        print(f"  Home threads without an owner      : {len(chat_cards)}")
        if not args.apply:
            print("\nNothing written. Re-run with --apply.")
            return 0

        done = 0
        for m in code_rows:
            m.owner = target
            await meta.upsert(m)
            done += 1
        for c in chat_cards:
            await chat.set_meta(c["id"], owner=target)
            done += 1

        left_code = sum(1 for m in await meta.list_all() if not (m.owner or "").strip())
        left_chat = sum(1 for c in await chat.list_cards() if not (c.get("owner") or "").strip())
        print(f"\n{done} attributed; {left_code + left_chat} still without an owner.")
        await close_all()
        return 0 if (left_code + left_chat) == 0 else 1
    finally:
        cfg.backend = was


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
