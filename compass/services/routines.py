"""Routines — templated automations that run on a schedule, by API, or webhook.

The analog of Claude Code's Routines: a routine bundles instructions, one or more
triggers (schedule / api / webhook), a target (local or cloud), the connectors it
may use, behavior/notification options, and a history of runs. Definitions persist
as JSON registries (data/routines.json, data/routine_runs.json).

A run executes the routine's instructions through the agent server-side (bypass
permissions, since no user is present) and links to the resulting conversation, so
the transcript can be replayed from the Runs list. A lightweight scheduler fires
each enabled routine when its next scheduled time arrives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from calendar import timegm
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from compass.config import get_settings

logger = logging.getLogger("compass.routines")

# GMT+5:30 (IST) — the reference UI shows schedules in this zone.
TZ_OFFSET_MIN = 330
TZ_LABEL = "GMT+5:30"
_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- models


@dataclass
class Trigger:
    type: str = "daily"  # once | hourly | daily | weekdays | weekly | custom
    time: str = "09:00"  # HH:MM, 24h, in TZ_LABEL
    days: list[int] = field(default_factory=list)  # weekly: 0=Mon .. 6=Sun
    cron: str = ""  # custom cron expression (informational)
    date: str = ""  # once: YYYY-MM-DD

    def summary(self) -> str:
        t = self.time  # 24h, matching the reference ("Runs weekdays at 23:30 …")
        if self.type == "once":
            return f"Runs once{(' on ' + self.date) if self.date else ''} at {t} {TZ_LABEL}"
        if self.type == "hourly":
            return f"Runs hourly at :{self.time.split(':')[1] if ':' in self.time else '00'} {TZ_LABEL}"
        if self.type == "daily":
            return f"Runs daily at {t} {TZ_LABEL}"
        if self.type == "weekdays":
            return f"Runs weekdays at {t} {TZ_LABEL}"
        if self.type == "weekly":
            days = ", ".join(_WEEKDAY_NAMES[d] for d in self.days) or "Mon"
            return f"Runs weekly on {days} at {t} {TZ_LABEL}"
        return f"Runs on schedule ({self.cron or 'custom'}) {TZ_LABEL}"


@dataclass
class Routine:
    id: str
    name: str
    prompt: str  # the instructions
    triggers: list[Trigger] = field(default_factory=list)
    target: str = "local"  # local | cloud
    model: str = ""
    repository: str = ""
    connectors: list[str] = field(default_factory=list)
    behavior: dict = field(default_factory=lambda: {"auto_fix_prs": False})
    notifications: dict = field(
        default_factory=lambda: {"enabled": True, "push": True, "email": False, "slack": False}
    )
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_run_at: float | None = None

    def schedule_summary(self) -> str:
        if not self.triggers:
            return "No trigger"
        return self.triggers[0].summary()

    def next_run_at(self) -> float | None:
        if not self.enabled or not self.triggers:
            return None
        times = [t for t in (_next_fire(tr) for tr in self.triggers) if t is not None]
        return min(times) if times else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schedule"] = self.schedule_summary()
        nxt = self.next_run_at()
        d["next_run_at"] = nxt
        d["next_run_label"] = _fmt_when(nxt) if nxt else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Routine":
        triggers = [Trigger(**t) for t in d.get("triggers", [])]
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and k != "triggers"}
        return cls(**known, triggers=triggers)


@dataclass
class RoutineRun:
    id: str
    routine_id: str
    routine_name: str
    trigger: str  # scheduled | manual | api | webhook
    status: str = "running"  # running | completed | failed
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    session_id: str = ""
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- scheduling


def _fmt_time(hhmm: str) -> str:
    try:
        h, m = (int(x) for x in hhmm.split(":"))
        ap = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {ap}"
    except Exception:
        return hhmm


def _now_local() -> datetime:
    return datetime.utcnow() + timedelta(minutes=TZ_OFFSET_MIN)


def _to_epoch(local_dt: datetime) -> float:
    # local_dt is an IST wall-clock naive datetime; shift to UTC then to epoch
    # via timegm (naive .timestamp() would wrongly assume the system timezone).
    return float(timegm((local_dt - timedelta(minutes=TZ_OFFSET_MIN)).timetuple()))


def _fmt_when(epoch: float) -> str:
    local = datetime.utcfromtimestamp(epoch) + timedelta(minutes=TZ_OFFSET_MIN)
    now = _now_local()
    day = "today" if local.date() == now.date() else (
        "tomorrow" if local.date() == (now + timedelta(days=1)).date()
        else local.strftime("%b %-d")
    )
    return f"{day} at {local.strftime('%H:%M')}"


def _next_fire(tr: Trigger) -> float | None:
    """Next epoch this trigger should fire, per its schedule (in TZ_LABEL)."""
    now = _now_local()
    try:
        hh, mm = (int(x) for x in tr.time.split(":"))
    except Exception:
        hh, mm = 9, 0

    if tr.type == "hourly":
        nxt = now.replace(minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(hours=1)
        return _to_epoch(nxt)

    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if tr.type == "once":
        if tr.date:
            try:
                d = datetime.strptime(tr.date, "%Y-%m-%d")
                candidate = candidate.replace(year=d.year, month=d.month, day=d.day)
            except Exception:
                pass
        return _to_epoch(candidate) if candidate > now else None

    if tr.type in ("daily", "weekdays", "weekly", "custom"):
        for add in range(0, 8):
            c = candidate + timedelta(days=add)
            if c <= now:
                continue
            wd = c.weekday()  # 0=Mon
            if tr.type == "daily" or tr.type == "custom":
                return _to_epoch(c)
            if tr.type == "weekdays" and wd < 5:
                return _to_epoch(c)
            if tr.type == "weekly" and (wd in (tr.days or [0])):
                return _to_epoch(c)
        return None
    return _to_epoch(candidate) if candidate > now else None


def _prev_fire(tr: Trigger) -> float | None:
    """Most recent epoch this trigger was due AT OR BEFORE now — what the
    scheduler compares against (next_fire is always strictly future, so it can
    never equal 'now'; the previous due time is what tells us a slot arrived)."""
    now = _now_local()
    try:
        hh, mm = (int(x) for x in tr.time.split(":"))
    except Exception:
        hh, mm = 9, 0

    if tr.type == "hourly":
        c = now.replace(minute=mm, second=0, microsecond=0)
        if c > now:
            c -= timedelta(hours=1)
        return _to_epoch(c)

    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if tr.type == "once":
        if tr.date:
            try:
                d = datetime.strptime(tr.date, "%Y-%m-%d")
                candidate = candidate.replace(year=d.year, month=d.month, day=d.day)
            except Exception:
                pass
        return _to_epoch(candidate) if candidate <= now else None

    for sub in range(0, 8):
        c = candidate - timedelta(days=sub)
        if c > now:
            continue
        wd = c.weekday()
        if tr.type in ("daily", "custom"):
            return _to_epoch(c)
        if tr.type == "weekdays" and wd < 5:
            return _to_epoch(c)
        if tr.type == "weekly" and (wd in (tr.days or [0])):
            return _to_epoch(c)
    return None


# --------------------------------------------------------------------------- templates

TEMPLATES: list[dict] = [
    {"id": "briefing", "icon": "sun", "name": "Briefing",
     "description": "Summary of your calendar, emails, and messages.",
     "schedule": "Runs weekdays at 18:00 GMT+5:30", "trigger_type": "weekdays", "time": "18:00",
     "integrations": ["Google Calendar", "Gmail", "Slack"],
     "prompt": "Give me a briefing of my calendar, emails, and messages for today."},
    {"id": "email-triage", "icon": "mail", "name": "Email triage",
     "description": "Categorize and prioritize your inbox, with draft responses for urgent items.",
     "schedule": "Runs weekdays at 20:30 GMT+5:30", "trigger_type": "weekdays", "time": "20:30",
     "integrations": ["Gmail"],
     "prompt": "Categorize and prioritize my inbox and draft responses for urgent items."},
    {"id": "system-health-check", "icon": "activity", "name": "System health check",
     "description": "Monitor infrastructure and services for errors, outages, and performance issues.",
     "schedule": "Runs daily at 17:30 GMT+5:30", "trigger_type": "daily", "time": "17:30",
     "integrations": ["PagerDuty", "Datadog", "Sentry"],
     "prompt": "Check infrastructure and services for errors, outages, and performance issues."},
    {"id": "issue-triage", "icon": "list", "name": "Issue triage",
     "description": "Review and categorize incoming issues, bugs, and feature requests.",
     "schedule": "Runs weekdays at 21:00 GMT+5:30", "trigger_type": "weekdays", "time": "21:00",
     "integrations": ["Linear"],
     "prompt": "Review and categorize incoming issues, bugs, and feature requests."},
]

SUGGESTIONS: list[str] = [
    "Summarize my open PRs every weekday morning",
    "Triage new issues and flag duplicates each morning",
    "Draft release notes whenever a PR merges",
]

CONNECTOR_OPTIONS = ["Claude_Code_Remote", "GitHub", "Gmail", "Slack", "Linear", "Google Calendar"]


# --------------------------------------------------------------------------- stores


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
        # strip computed fields before persisting
        for d in payload.values():
            d.pop("schedule", None)
            d.pop("next_run_at", None)
            d.pop("next_run_label", None)
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))

    async def list(self) -> list[Routine]:
        async with self._lock:
            return sorted(self._load().values(), key=lambda r: r.created_at, reverse=True)

    async def get(self, routine_id: str) -> Routine | None:
        async with self._lock:
            return self._load().get(routine_id)

    async def create(self, **kwargs) -> Routine:
        triggers = [Trigger(**t) if isinstance(t, dict) else t for t in kwargs.pop("triggers", [])]
        prompt = (kwargs.pop("prompt", "") or "").strip()
        name = (kwargs.pop("name", "") or "").strip() or (prompt[:48] or "Untitled routine")
        routine = Routine(id=uuid.uuid4().hex[:8], name=name, prompt=prompt, triggers=triggers, **kwargs)
        async with self._lock:
            self._load()[routine.id] = routine
            self._flush()
        return routine

    async def update(self, routine_id: str, patch: dict) -> Routine | None:
        async with self._lock:
            r = self._load().get(routine_id)
            if not r:
                return None
            if "triggers" in patch and patch["triggers"] is not None:
                r.triggers = [Trigger(**t) if isinstance(t, dict) else t for t in patch["triggers"]]
            for k in ("name", "prompt", "target", "model", "repository", "connectors",
                      "behavior", "notifications", "enabled"):
                if k in patch and patch[k] is not None:
                    setattr(r, k, patch[k])
            r.updated_at = time.time()
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

    async def mark_ran(self, routine_id: str) -> None:
        async with self._lock:
            r = self._load().get(routine_id)
            if r:
                r.last_run_at = time.time()
                self._flush()


class RunStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cache: dict[str, RoutineRun] | None = None

    def _path(self) -> Path:
        return get_settings().workspace_root / get_settings().data_dir / "routine_runs.json"

    def _load(self) -> dict[str, RoutineRun]:
        if self._cache is not None:
            return self._cache
        data: dict[str, RoutineRun] = {}
        path = self._path()
        if path.is_file():
            try:
                raw = json.loads(path.read_text())
                for rid, d in raw.items():
                    known = {k: d[k] for k in RoutineRun.__dataclass_fields__ if k in d}
                    data[rid] = RoutineRun(**known)
            except (OSError, json.JSONDecodeError) as err:
                logger.error("could not read routine_runs.json: %s", err)
        self._cache = data
        return data

    def _flush(self) -> None:
        assert self._cache is not None
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({rid: r.to_dict() for rid, r in self._cache.items()}, indent=2))

    async def create(self, routine: Routine, trigger: str) -> RoutineRun:
        run = RoutineRun(
            id=uuid.uuid4().hex[:8], routine_id=routine.id,
            routine_name=routine.name, trigger=trigger,
        )
        async with self._lock:
            self._load()[run.id] = run
            self._flush()
        return run

    async def finish(self, run_id: str, *, status: str, session_id: str, summary: str) -> None:
        async with self._lock:
            run = self._load().get(run_id)
            if run:
                run.status = status
                run.finished_at = time.time()
                run.session_id = session_id
                run.summary = summary
                self._flush()

    async def get(self, run_id: str) -> RoutineRun | None:
        async with self._lock:
            return self._load().get(run_id)

    async def list_for(self, routine_id: str) -> list[RoutineRun]:
        async with self._lock:
            runs = [r for r in self._load().values() if r.routine_id == routine_id]
            return sorted(runs, key=lambda r: r.started_at, reverse=True)

    async def finished_since(self, since: float) -> list[RoutineRun]:
        """Runs that finished after `since` — the browser push feed polls this."""
        async with self._lock:
            out = [
                r for r in self._load().values()
                if r.status != "running" and (r.finished_at or 0) > since
            ]
            return sorted(out, key=lambda r: r.finished_at or 0)


store = RoutineStore()
runs = RunStore()


# --------------------------------------------------------------------------- execution


async def execute_routine(routine: Routine, trigger: str, engine) -> RoutineRun:
    """Create a run record and execute the routine's instructions server-side as a
    real agent turn (bypass permissions — no user present). Links the resulting
    session so the run's conversation can be replayed."""
    from compass.core.query_engine import Session

    run = await runs.create(routine, trigger)
    session = Session(permission_mode="bypass", model=routine.model or None)
    if routine.target == "cloud":
        session.workspace_id = None
    await engine._attach_workspace(session)

    async def _drive() -> None:
        summary, status = "", "completed"
        try:
            async for ev in engine.ask(session, routine.prompt):
                data = getattr(ev, "text", None)
                if getattr(ev, "type", "") in ("assistant_message",) and data:
                    summary = data
            # fall back to last assistant text
            for m in reversed(session.messages):
                if getattr(m, "role", "") == "assistant" and getattr(m, "content", ""):
                    summary = str(m.content)[:280]
                    break
            meta = await engine.ensure_meta(session.id)
            meta.title = f"⚡ {routine.name}"
            meta.routine_id = routine.id  # keeps this run out of Conversations
            await engine.meta.upsert(meta)
        except Exception as err:  # noqa: BLE001 — record the failure on the run
            logger.exception("routine run failed")
            status, summary = "failed", str(err)[:280]
        # Strip control chars so the run JSON always parses cleanly on the client.
        clean = "".join(c for c in (summary or "Completed.") if c >= " " or c in "\n\t")
        await runs.finish(run.id, status=status, session_id=session.id, summary=clean[:500])
        await store.mark_ran(routine.id)
        _notify_finished(routine, trigger, status, clean)

    asyncio.create_task(_drive())
    return run


def _notify_finished(routine: Routine, trigger: str, status: str, summary: str) -> None:
    """Fan out completion notifications per the routine's settings. Push is
    delivered by the browser (via the recent-runs feed); email is sent here."""
    notif = routine.notifications or {}
    if not notif.get("enabled", True):
        return
    if notif.get("email"):
        from compass.services.notify import send_email

        verb = "completed" if status == "completed" else status
        send_email(
            f"⚡ {routine.name} — {verb}",
            f"Your routine “{routine.name}” ({trigger} run) {verb}.\n\n{summary}\n\n— Compass",
        )


_scheduler_task: asyncio.Task | None = None


# A due slot is only fired if we notice it within this window, so a server that
# starts hours after a missed schedule doesn't replay stale runs.
_FIRE_WINDOW_SEC = 150


async def scheduler_loop(engine) -> None:
    """Fire each enabled routine when a scheduled slot arrives. Ticks every 15s
    and fires when the most-recent due time is recent and not yet fired."""
    fired: dict[str, float] = {}  # routine_id -> due epoch already fired
    logger.info("routine scheduler started")
    while True:
        try:
            now = time.time()
            for r in await store.list():
                if not r.enabled or not r.triggers:
                    continue
                for tr in r.triggers:
                    due = _prev_fire(tr)
                    if due is None:
                        continue
                    if now - due <= _FIRE_WINDOW_SEC and fired.get(r.id, 0) < due:
                        fired[r.id] = due
                        logger.info(
                            "scheduler firing routine %r (%s) for slot %s",
                            r.name, r.id, _fmt_when(due),
                        )
                        await execute_routine(r, "scheduled", engine)
                        break
        except Exception:  # noqa: BLE001 — never let the loop die
            logger.exception("scheduler tick failed")
        await asyncio.sleep(15)


def start_scheduler(engine) -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(scheduler_loop(engine))
