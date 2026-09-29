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


class MissionStore:
    def _path(self) -> Path:
        settings = get_settings()
        folder = settings.workspace_root / settings.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "missions.json"

    def _read(self) -> dict[str, dict]:
        path = self._path()
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            logger.error("missions registry unreadable: %s", err)
            return {}

    def _write(self, rows: dict[str, dict]) -> None:
        path = self._path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=2, default=str))
        tmp.replace(path)  # atomic on POSIX

    def list(self) -> list[Mission]:
        missions = [_from_dict(row) for row in self._read().values()]
        missions.sort(key=lambda m: getattr(m, "created_at", 0.0), reverse=True)
        return missions

    def get(self, mission_id: str) -> Mission | None:
        row = self._read().get(mission_id)
        return _from_dict(row) if row else None

    def save(self, mission: Mission) -> Mission:
        rows = self._read()
        rows[mission.id] = _to_dict(mission)
        self._write(rows)
        return mission

    def create(self, *, goal: str, workspace: Path, model: str | None = None,
               budget_usd: float = DEFAULT_BUDGET_USD,
               triggers: list[dict] | None = None) -> Mission:
        mission = Mission(id=str(uuid.uuid4()), goal=goal.strip(),
                          workspace=workspace, model=model,
                          budget_usd=budget_usd, triggers=list(triggers or []))
        mission.created_at = time.time()
        workspace.mkdir(parents=True, exist_ok=True)
        _make_its_own_repo(workspace)
        return self.save(mission)

    def delete(self, mission_id: str) -> bool:
        """Forget the record. The workspace is left exactly where it is —
        deleting somebody's code because they closed a job is not a thing this
        gets to decide."""
        rows = self._read()
        if rows.pop(mission_id, None) is None:
            return False
        self._write(rows)
        return True


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
