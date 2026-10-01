#!/usr/bin/env python3
"""Rebuild the sidebar metadata for conversations that have lost it.

    python scripts/rebuild_session_meta.py            # say what would change
    python scripts/rebuild_session_meta.py --apply

A Code conversation's title is not in its transcript; it is in a metadata row
written when the first turn runs. A conversation whose row is missing has a
transcript full of messages and a blank line in the sidebar — which is both
useless to read and, less obviously, slow: a point read that misses costs
455ms against 241ms for one that hits, because the SDK retries the 404.

What can be rebuilt is rebuilt, from the transcript itself:

    title          the first user message, exactly as `_title_from` cuts it
    message_count  how many user messages there are
    created_at     the first message's timestamp
    updated_at     the last message's timestamp

What cannot is left alone, because nothing records it anywhere else: whether
a conversation was pinned, archived, which group it was in, which workspace
and model it used. Those stay at their defaults.

Only fills gaps. A row that already has a title is never touched, so this is
safe to run against a healthy install and safe to run twice.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _text(content) -> str:
    """The words in a message, whatever shape its content is."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, nothing is changed.")
    args = ap.parse_args()

    from compass.code.engine import _title_from
    from compass.common.config import get_settings
    from compass.common.persistence.factory import get_transcript_store
    from compass.common.persistence.session_meta import SessionMeta, get_meta_store
    from compass.common.persistence.shutdown import close_all

    cfg = get_settings().storage
    store, meta = get_transcript_store(), get_meta_store()

    existing = {m.id: m for m in await meta.list_all()}
    sessions = await store.list_sessions()
    print(f"{len(sessions)} transcript(s), {len(existing)} metadata row(s)\n")

    rebuilt = skipped = 0
    try:
        for sid in sessions:
            current = existing.get(sid)
            if current is not None and (current.title or "").strip():
                skipped += 1
                continue
            # Sidechains included: a conversation whose only turns were taken
            # by a sub-agent still has a first user message, and it is the one
            # the person typed.
            messages = await store.load(sid, include_sidechains=True)
            users = [m for m in messages if m.role == "user"]
            if not messages:
                skipped += 1
                continue
            first = _text(users[0].content).strip() if users else ""
            title = _title_from(first) if first else ""
            stamps = [m.timestamp for m in messages if m.timestamp]
            row = current or SessionMeta(id=sid)
            row.title = title or row.title
            row.message_count = len(users)
            if stamps:
                row.created_at = min(stamps)
                row.updated_at = max(stamps)
            print(f"  {sid[:13]}  {len(users):3} user msg(s)  {title[:52]!r}")
            if args.apply:
                await meta.upsert(row)
            rebuilt += 1
        await close_all()
    finally:
        pass

    print(f"\n{rebuilt} rebuilt, {skipped} already had a title.")
    if not args.apply:
        print("Nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
