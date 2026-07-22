"""Routines — templated automations that run on a schedule, by API, or webhook.

The analog of Claude Code's "Routines" page: a routine bundles a natural-language
task ("Summarize my open PRs every weekday morning"), a trigger (schedule / api /
webhook), a target (local or cloud), and the integrations it touches. Persisted as
a single JSON registry (data/routines.json), mirroring the workspace registry.

Actually firing scheduled routines is out of scope of this store; it owns the
definitions and the CRUD the Routines UI drives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from compass.config import get_settings

logger = logging.getLogger("compass.routines")


@dataclass
class Routine:
    id: str
    name: str
    prompt: str
    schedule: str = ""  # human/cron, e.g. "Weekdays at 18:00 GMT+5:30"
    trigger: str = "schedule"  # "schedule" | "api" | "webhook"
    target: str = "local"  # "local" | "cloud"
    integrations: list[str] = field(default_factory=list)
    enabled: bool = True
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Routine":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


# The starter templates shown under "Or start from a template", matching the
# Claude Code Routines gallery.
TEMPLATES: list[dict] = [
    {
        "id": "briefing",
        "icon": "sun",
        "name": "Briefing",
        "description": "Summary of your calendar, emails, and messages.",
        "schedule": "Runs weekdays at 18:00 GMT+5:30",
        "integrations": ["Google Calendar", "Gmail", "Slack"],
        "prompt": "Give me a briefing of my calendar, emails, and messages for today.",
    },
    {
        "id": "email-triage",
        "icon": "mail",
        "name": "Email triage",
        "description": "Categorize and prioritize your inbox, with draft responses for urgent items.",
        "schedule": "Runs weekdays at 20:30 GMT+5:30",
        "integrations": ["Gmail"],
        "prompt": "Categorize and prioritize my inbox and draft responses for urgent items.",
    },
    {
        "id": "system-health-check",
        "icon": "activity",
        "name": "System health check",
        "description": "Monitor infrastructure and services for errors, outages, and performance issues.",
        "schedule": "Runs daily at 17:30 GMT+5:30",
        "integrations": ["PagerDuty", "Datadog", "Sentry"],
        "prompt": "Check infrastructure and services for errors, outages, and performance issues.",
    },
    {
        "id": "issue-triage",
        "icon": "list",
        "name": "Issue triage",
        "description": "Review and categorize incoming issues, bugs, and feature requests.",
        "schedule": "Runs weekdays at 21:00 GMT+5:30",
        "integrations": ["Linear"],
        "prompt": "Review and categorize incoming issues, bugs, and feature requests.",
    },
]

# Suggestion chips under the prompt box.
SUGGESTIONS: list[str] = [
    "Summarize my open PRs every weekday morning",
    "Triage new issues and flag duplicates each morning",
    "Draft release notes whenever a PR merges",
]


class RoutineStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cache: dict[str, Routine] | None = None

    def _path(self) -> Path:
        return get_settings().workspace_root / get_settings().data_dir / "routines.json"

    def _load(self) -> dict[str, Routine]:
        if self._cache is not None:
            return self._cache
        data: dict[str, Routine] = {}
        path = self._path()
        if path.is_file():
            try:
                raw = json.loads(path.read_text())
                for rid, d in raw.items():
                    data[rid] = Routine.from_dict({**d, "id": rid})
            except (OSError, json.JSONDecodeError) as err:
                logger.error("could not read routines.json: %s", err)
        self._cache = data
        return data

    def _flush(self) -> None:
        assert self._cache is not None
        payload = {rid: r.to_dict() for rid, r in self._cache.items()}
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))

    async def list(self) -> list[Routine]:
        async with self._lock:
            return sorted(self._load().values(), key=lambda r: r.created_at, reverse=True)

    async def create(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str = "",
        trigger: str = "schedule",
        target: str = "local",
        integrations: list[str] | None = None,
    ) -> Routine:
        routine = Routine(
            id=uuid.uuid4().hex[:8],
            name=name.strip() or (prompt.strip()[:48] or "Untitled routine"),
            prompt=prompt.strip(),
            schedule=schedule.strip(),
            trigger=trigger,
            target=target,
            integrations=integrations or [],
        )
        async with self._lock:
            self._load()[routine.id] = routine
            self._flush()
        return routine

    async def update(self, routine_id: str, patch: dict) -> Routine | None:
        async with self._lock:
            routines = self._load()
            r = routines.get(routine_id)
            if not r:
                return None
            for k in ("name", "prompt", "schedule", "trigger", "target", "integrations", "enabled"):
                if k in patch and patch[k] is not None:
                    setattr(r, k, patch[k])
            self._flush()
            return r

    async def delete(self, routine_id: str) -> bool:
        async with self._lock:
            routines = self._load()
            if routine_id in routines:
                routines.pop(routine_id)
                self._flush()
                return True
            return False


store = RoutineStore()
