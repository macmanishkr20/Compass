#!/usr/bin/env python3
"""Claim every record written before ownership existed.

Compass stamps an owner on anything created from now on, and treats an empty
owner as legacy that stays visible to everyone (see
`compass.common.ownership`). That is the safe default and a bad resting
state: it means the whole existing corpus is shared with whoever logs in
next. This script ends that state by assigning what is already on disk.

    python scripts/migrate_ownership.py --owner alice --dry-run
    python scripts/migrate_ownership.py --owner alice

Idempotent, and deliberately timid about it: a record that already has an
owner is never reassigned, so running twice is a no-op and running with the
wrong name cannot take someone's work away from them. Use --force to
overwrite an existing owner, which is the one way to undo a mistake.

Home is the awkward one. Its index only holds a row once a thread has been
renamed or pinned, so most threads have no row at all — the rows are created
here rather than updated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as err:
        print(f"  ! could not read {path.name}: {err}")
        return None


def _save(path: Path, payload, dry: bool) -> None:
    if dry:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)  # atomic on POSIX


def _claim(record: dict, owner: str, force: bool) -> bool:
    """Stamp one record. Returns whether it changed."""
    if record.get("owner") and not force:
        return False
    if record.get("owner") == owner:
        return False
    record["owner"] = owner
    return True


def code_sessions(owner: str, dry: bool, force: bool) -> tuple[int, int]:
    """The Code conversations, in the shared session-meta index."""
    path = DATA / "sessions" / "_meta.json"
    if not path.is_file():
        return 0, 0
    meta = _load(path)
    if meta is None:
        return 0, 0
    changed = sum(_claim(row, owner, force) for row in meta.values())

    # A transcript with no meta row is still a conversation, and the list
    # endpoint invents a default row for it — so it needs claiming too, or it
    # stays visible to everyone.
    for jsonl in (DATA / "sessions").glob("*.jsonl"):
        if jsonl.stem not in meta:
            meta[jsonl.stem] = {"id": jsonl.stem, "owner": owner}
            changed += 1
    if changed:
        _save(path, meta, dry)
    return changed, len(meta)


def home_threads(owner: str, dry: bool, force: bool) -> tuple[int, int]:
    """The Home threads. Rows are created, not updated — see the module
    docstring."""
    folder = DATA / "sessions" / "chat"
    if not folder.is_dir():
        return 0, 0
    path = folder / "_meta.json"
    meta = _load(path) if path.is_file() else {}
    if meta is None:
        return 0, 0
    changed = 0
    for jsonl in folder.glob("*.jsonl"):
        row = meta.setdefault(jsonl.stem, {})
        changed += _claim(row, owner, force)
    if changed:
        _save(path, meta, dry)
    return changed, len(meta)


def _row_list(path: Path, owner: str, dry: bool,
              force: bool) -> tuple[int, int]:
    """A store that keeps its records as a list in one file — design projects
    and the design systems they reference."""
    if not path.is_file():
        return 0, 0
    rows = _load(path)
    if not isinstance(rows, list):
        return 0, 0
    changed = sum(_claim(r, owner, force) for r in rows if isinstance(r, dict))
    if changed:
        _save(path, rows, dry)
    return changed, len(rows)


def _keyed_map(path: Path, owner: str, dry: bool,
               force: bool) -> tuple[int, int]:
    """A store that keeps every record in one file, keyed by id — routines and
    their runs."""
    if not path.is_file():
        return 0, 0
    rows = _load(path)
    if not isinstance(rows, dict):
        return 0, 0
    changed = sum(_claim(r, owner, force) for r in rows.values()
                  if isinstance(r, dict))
    if changed:
        _save(path, rows, dry)
    return changed, len(rows)


def _one_per_file(folder: Path, owner: str, dry: bool,
                  force: bool) -> tuple[int, int]:
    if not folder.is_dir():
        return 0, 0
    changed = total = 0
    for path in sorted(folder.glob("*.json")):
        record = _load(path)
        if not isinstance(record, dict):
            continue
        total += 1
        if _claim(record, owner, force):
            _save(path, record, dry)
            changed += 1
    return changed, total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True,
                    help="the username to assign every unowned record to")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and write nothing")
    ap.add_argument("--force", action="store_true",
                    help="also reassign records that already have an owner")
    args = ap.parse_args()

    from compass.common.users import canonical, get_user_store

    owner = args.owner.strip()
    if not owner:
        print("--owner cannot be empty")
        return 2
    # Resolve through the alias map, so `--owner admin` stamps the identity
    # `admin` actually is rather than the string that was typed.
    identity = canonical(owner)
    if identity != owner:
        print(f"{owner!r} is an alias for {identity!r}; using the identity.\n")
    owner = identity

    if not DATA.is_dir():
        print(f"no data directory at {DATA}")
        return 1

    print(f"{'Would assign' if args.dry_run else 'Assigning'} to {owner!r}"
          f"{' (forced)' if args.force else ''}\n")

    steps = [
        ("code conversations", code_sessions(owner, args.dry_run, args.force)),
        ("home threads", home_threads(owner, args.dry_run, args.force)),
        ("design projects", _row_list(DATA / "design.json", owner,
                                      args.dry_run, args.force)),
        # Claimed alongside the projects that reference them by id. The
        # shipped examples live in code, not here, and stay unowned.
        ("design systems", _row_list(DATA / "design_systems.json", owner,
                                     args.dry_run, args.force)),
        ("pipelines", _one_per_file(DATA / "pipelines", owner,
                                    args.dry_run, args.force)),
        ("pipeline runs", _one_per_file(DATA / "pipeline_runs", owner,
                                        args.dry_run, args.force)),
        ("connections", _one_per_file(DATA / "connections", owner,
                                      args.dry_run, args.force)),
        ("routines", _keyed_map(DATA / "routines.json", owner,
                                args.dry_run, args.force)),
        ("routine runs", _keyed_map(DATA / "routine_runs.json", owner,
                                    args.dry_run, args.force)),
    ]

    total = 0
    for label, (changed, seen) in steps:
        total += changed
        note = "already owned" if seen and not changed else f"{changed} claimed"
        print(f"  {label:<20} {seen:>4} record(s), {note}")

    print(f"\n{total} record(s) {'would change' if args.dry_run else 'claimed'}.")
    if args.dry_run:
        print("Nothing was written. Re-run without --dry-run to apply.")
    elif total:
        # Put the owner in the user table too. Without this the table would
        # say nobody has ever logged in while 258 records name them, which
        # is exactly the kind of half-truth the table exists to prevent.
        asyncio.run(get_user_store().ensure(owner))
        print(f"{owner!r} recorded in the user table.")
        print("Restart the backend so the stores reload from disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
