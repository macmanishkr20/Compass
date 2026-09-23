"""The table of people who have logged in.

Compass authenticates against a configured credential map and, until now,
kept no record of who actually used it. That map is the list of who *may*
log in; this is the list of who *has* — which is the one you need to answer
"whose are these records", to see that a new username has appeared, and to
tell a typo apart from a new colleague.

A row is created the first time a username authenticates successfully and
updated on every login after that. Nothing here grants access: the credential
map is still the authority, and a row appearing has no effect on what its
owner can see. It is a record, not a permission.

Identity aliases live here too, and they exist because a username is not a
person. `admin` is the demo credential Compass ships with, and the person
using it has a real address; if those stay two identities then the records
owned by one are invisible to the other, and enabling auth would look like
the data had vanished. `canonical()` maps a login name to the identity that
owns records, so several credentials can be one person.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from compass.common.config import get_settings

logger = logging.getLogger("compass.users")


@dataclass
class UserRecord:
    """One person, as seen by the login endpoint."""

    id: str  # the canonical identity — what stores stamp as `owner`
    #: What this person would like to be called. Empty until they say, and
    #: never guessed from the login: an organisation issues addresses like
    #: `macmanishkr20@…`, and greeting somebody by the handle their IT
    #: department gave them is not friendlier than not greeting them by name.
    display_name: str = ""
    logins: list[str] = field(default_factory=list)  # names they signed in as
    first_seen: float = field(default_factory=time.time)
    last_login: float = field(default_factory=time.time)
    login_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UserRecord":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def canonical(username: str) -> str:
    """The identity that owns records, for someone signing in as `username`.

    Unaliased names pass through unchanged, so a new username is a new
    identity that owns nothing — which is the intended behaviour, not an
    oversight: sharing by default is what ownership exists to end.
    """
    name = (username or "").strip()
    return get_settings().auth.identity_aliases.get(name, name)


class UserStore:
    """A single JSON file, mirroring the local session-meta store."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _path(self) -> Path:
        settings = get_settings()
        folder = settings.workspace_root / settings.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "users.json"

    def _read(self) -> dict[str, UserRecord]:
        path = self._path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            logger.error("could not read the user table: %s", err)
            return {}
        return {k: UserRecord.from_dict(v) for k, v in raw.items()}

    def _write(self, rows: dict[str, UserRecord]) -> None:
        path = self._path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: v.to_dict() for k, v in rows.items()}))
        tmp.replace(path)  # atomic on POSIX

    async def record_login(self, username: str) -> UserRecord:
        """Note that `username` signed in, creating the row if it is new."""
        identity = canonical(username)
        async with self._lock:
            rows = self._read()
            row = rows.get(identity)
            if row is None:
                row = UserRecord(id=identity)
                logger.info("new user in the table: %s", identity)
            if username and username not in row.logins:
                # Kept so an alias is visible as what it is — one identity
                # reached by more than one credential.
                row.logins.append(username)
            row.last_login = time.time()
            row.login_count += 1
            rows[identity] = row
            self._write(rows)
            return row

    async def set_display_name(self, identity: str, name: str) -> UserRecord:
        """Record what this person would like to be called.

        Creates the row if it is not there: with auth disabled everyone is
        `guest`, and a guest who has told Compass their name should still be
        greeted by it.
        """
        identity = canonical(identity)
        # A name, not an essay, and not markup: this is rendered in a heading.
        clean = " ".join(str(name).split())[:60]
        async with self._lock:
            rows = self._read()
            row = rows.get(identity) or UserRecord(id=identity)
            row.display_name = clean
            rows[identity] = row
            self._write(rows)
            return row

    async def list(self) -> list[UserRecord]:
        async with self._lock:
            rows = list(self._read().values())
        rows.sort(key=lambda r: r.last_login, reverse=True)
        return rows

    async def get(self, identity: str) -> UserRecord | None:
        async with self._lock:
            return self._read().get(identity)

    async def ensure(self, identity: str) -> UserRecord:
        """Put an identity in the table without counting it as a login.

        Used by the ownership migration, so the person the existing records
        were assigned to appears in the table before they have signed in —
        otherwise the table would say nobody owns 241 records.
        """
        async with self._lock:
            rows = self._read()
            row = rows.get(identity)
            if row is None:
                row = UserRecord(id=identity, login_count=0)
                rows[identity] = row
                self._write(rows)
            return row


_store: UserStore | None = None


def get_user_store() -> UserStore:
    global _store
    if _store is None:
        _store = UserStore()
    return _store
