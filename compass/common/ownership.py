"""Who a stored record belongs to — one rule, applied by every store.

Compass already knows who is asking: `require_user` is a dependency on all 120
stateful routes. What it has never done is *record* that on the things it
saves, so every list endpoint returns everything on the box. This module is
the missing half, kept in one file so the rule cannot drift between modules.

Two rules, and both of them fail open rather than hiding a user's own data:

  * An empty owner is legacy, and stays visible to everyone. Records written
    before ownership existed have no owner, and making them invisible would
    look exactly like data loss. `scripts/migrate_ownership.py` claims them.

  * When auth is disabled there is no identity to filter on. `require_user`
    returns "guest" for everybody in that mode, so filtering on it would hide
    every record written while auth was on — the local-dev view of a box that
    has real owners would come back empty. A box with no login is a box with
    one user; it sees everything.

The consequence worth stating plainly: this separates users, it does not
isolate them. Code sessions carry a workspace root and the agent has shell and
filesystem access, so scoping the stores stops one user listing another's
conversations and nothing more. It is not a tenancy boundary.
"""

from __future__ import annotations

from compass.common.config import get_settings


def owner_for(user: str) -> str:
    """The owner to stamp on a record being created.

    Empty when auth is off: an unauthenticated box has no identity worth
    recording, and an unowned record stays visible if auth is later turned on.
    """
    return user if get_settings().auth.enabled else ""


def visible_to(user: str, owner: str) -> bool:
    """Whether `user` may see a record owned by `owner`."""
    if not get_settings().auth.enabled:
        return True
    return not owner or owner == user


def owned(rows: list, user: str, *, key: str = "owner") -> list:
    """Filter a list of dict rows (or objects) down to what `user` may see."""
    if not get_settings().auth.enabled:
        return rows
    out = []
    for row in rows:
        owner = row.get(key, "") if isinstance(row, dict) else getattr(row, key, "")
        if visible_to(user, owner or ""):
            out.append(row)
    return out
