"""Where missions are remembered between restarts.

A single JSON registry, the same shape the routines registry uses, because a
mission is the same kind of thing: a definition plus a history of runs, small
enough that a file is the right answer and long-lived enough that memory is
not.

What is stored here is the *record* — the goal, the workspace, what each
session cost and achieved. What the mission actually built lives in the
workspace, and the features and progress are read from there rather than
duplicated here: two copies of the score is one copy too many, and the one on
disk beside the code is the one that is true.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import fields
from pathlib import Path

from compass.common.config import get_settings
from compass.common.persistence.catalog import Collection
from compass.missions.engine import DEFAULT_BUDGET_USD, Mission, SessionResult

logger = logging.getLogger("compass.missions")


def _to_dict(mission: Mission) -> dict:
    return {
        "id": mission.id,
        "goal": mission.goal,
        "workspace": str(mission.workspace),
        "model": mission.model,
        "budget_usd": mission.budget_usd,
        "spent_usd": mission.spent_usd,
        "owner": mission.owner,
        "triggers": list(mission.triggers),
        "created_at": getattr(mission, "created_at", time.time()),
        "sessions": [vars(s) for s in mission.sessions],
    }


def _from_dict(row: dict) -> Mission:
    mission = Mission(
        id=str(row.get("id") or ""),
        goal=str(row.get("goal") or ""),
        workspace=Path(str(row.get("workspace") or ".")),
        model=row.get("model") or None,
        budget_usd=float(row.get("budget_usd") or DEFAULT_BUDGET_USD),
        spent_usd=float(row.get("spent_usd") or 0.0),
        owner=str(row.get("owner") or ""),
        triggers=list(row.get("triggers") or []),
    )
    mission.created_at = float(row.get("created_at") or time.time())
    mission.sessions = [_session_from(s) for s in (row.get("sessions") or [])]
    return mission


def _session_from(row: dict) -> SessionResult:
    """One session record, tolerant of keys it does not know.

    `SessionResult(**row)` refused anything unexpected, which made a single
    stray key fatal to the *whole* list endpoint rather than to one row — and
    stray keys are the normal case here, not an exotic one: the API's own view
    of a session adds a derived `tokens_per_minute`, a record written by a
    newer Compass can carry fields an older one has never heard of, and either
    took the Missions screen down completely. Unknown keys are dropped;
    missing ones keep their defaults, so a record written before tokens were
    measured still loads.
    """
    known = {f.name for f in fields(SessionResult)}
    return SessionResult(**{k: v for k, v in row.items() if k in known})


#: The mission registry. Small, owner-scoped records in the shared catalog —
#: the workspace on disk is not stored here, only the path to it.
_missions = Collection("mission", "missions.json", shape="map")


class MissionStore:
    """The missions this install knows about.

    Async throughout, which it was not: every method read and wrote a JSON
    file synchronously, which is microseconds on local disk and a network
    round trip against Cosmos. A synchronous network call on the event loop
    blocks every other mission, every streaming turn and every request in the
    process — the same defect the sandbox's git lookup had.
    """

    async def _read(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in await _missions.all():
            mid = str(row.get("id", ""))
            if mid:
                out[mid] = row
        return out

    async def list(self) -> list[Mission]:
        missions = [_from_dict(row) for row in (await self._read()).values()]
        missions.sort(key=lambda m: getattr(m, "created_at", 0.0), reverse=True)
        return missions

    async def get(self, mission_id: str) -> Mission | None:
        row = await _missions.get(mission_id)
        return _from_dict(row) if row else None

    async def save(self, mission: Mission) -> Mission:
        await _missions.put(_to_dict(mission))
        return mission

    async def create(self, *, goal: str, workspace: Path, model: str | None = None,
                     budget_usd: float = DEFAULT_BUDGET_USD,
                     triggers: list[dict] | None = None,
                     owner: str = "") -> Mission:
        mission = Mission(id=str(uuid.uuid4()), goal=goal.strip(),
                          workspace=workspace, model=model, owner=owner,
                          budget_usd=budget_usd, triggers=list(triggers or []))
        mission.created_at = time.time()
        workspace.mkdir(parents=True, exist_ok=True)
        _make_its_own_repo(workspace)
        return await self.save(mission)

    async def delete(self, mission_id: str) -> bool:
        """Forget the record. The workspace is left exactly where it is —
        deleting somebody's code because they closed a job is not a thing this
        gets to decide."""
        return await _missions.remove(mission_id)


def _make_its_own_repo(workspace: Path) -> None:
    """Give the workspace its own git repository, immediately.

    Not a convenience — a containment measure. Git binds to the nearest `.git`
    above the working directory, and the default workspaces live under the
    server's own data directory, which is inside the server's own repository.
    Measured rather than assumed: `git rev-parse --show-toplevel` from a fresh
    mission workspace answered with the *Compass* checkout, which means a
    session told to "commit your progress" would have committed into the
    project it is running inside.

    The planner is asked to `git init` if it is not already in a repository,
    and from in there the honest answer to that question is yes — so the
    instruction cannot save us and the harness has to.
    """
    import subprocess

    if (workspace / ".git").is_dir():
        return
    try:
        subprocess.run(["git", "init", "--quiet"], cwd=workspace,
                       capture_output=True, timeout=30, check=False)
        # A commit needs an identity, and a mission has no person behind it.
        for key, value in (("user.name", "Compass Mission"),
                           ("user.email", "missions@compass.local")):
            subprocess.run(["git", "config", key, value], cwd=workspace,
                           capture_output=True, timeout=10, check=False)
    except Exception as err:  # noqa: BLE001 — a mission without git still runs
        logger.warning("could not initialise a repository in %s: %s",
                       workspace, err)


_store: MissionStore | None = None


def get_mission_store() -> MissionStore:
    global _store
    if _store is None:
        _store = MissionStore()
    return _store
