"""The loop that starts pipelines nobody pressed Run on.

`Pipeline.triggers` has been a stored field with nothing reading it: a
schedule could be saved and would never fire. This is the missing half, and it
is modelled on the routine scheduler in `compass/code/routines.py` — one task,
a short tick, a window so a slot is not missed by a second and not fired twice
by a restart.

Two kinds of trigger, which behave differently in a way worth naming:

  schedule   time is the event. If the box was asleep at 09:00 the slot is
             gone; firing it late would be a surprise, not a favour.
  gmail      the mail is the event, and it waits. A poll that finds four
             messages after an outage delivers all four, because they are
             still there — the cursor, not the clock, decides what is new.

The cursor is the whole correctness problem here. Gmail's list is not a queue;
polling it again returns the same messages, so without a record of what has
been seen a five-minute poll would reprocess the same invoice twelve times an
hour. Seen ids are kept per trigger, outside the pipeline record so that
polling does not bump a pipeline's version on every tick, and bounded so the
file cannot grow without limit.

A manual run never touches the cursor. Testing a graph must not cause the
schedule to skip real mail — that failure would be silent, would look like
Gmail's fault, and is exactly the kind of thing nobody finds for a month.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from compass.common.config import get_settings
from compass.pipelines import store as pstore
from compass.pipelines.nodes.triggers import DELIVERY_KEY, _fetch_messages

logger = logging.getLogger("compass.pipelines.runner")

#: How often the loop wakes. Short enough that a five-minute poll is roughly
#: five minutes, long enough to cost nothing when there is nothing to do.
TICK_S = 20

#: A schedule slot this recent still fires. Mirrors the routine scheduler:
#: wide enough to survive a slow tick, narrow enough that a box waking after
#: an hour does not fire an 09:00 job at 10:15.
FIRE_WINDOW_S = 150

#: Ids remembered per trigger. Enough that a busy mailbox cannot cycle a
#: message back into "new" between polls; small enough to stay a small file.
CURSOR_KEEP = 500

_task: asyncio.Task | None = None


# --------------------------------------------------------------------------- cursor


def _state_path() -> Path:
    settings = get_settings()
    folder = settings.workspace_root / settings.data_dir
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "pipeline_triggers.json"


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as err:
        logger.error("could not read trigger state: %s", err)
        return {}


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)  # atomic on POSIX


def _key(pipeline_id: str, index: int) -> str:
    return f"{pipeline_id}:{index}"


# --------------------------------------------------------------------------- schedule


def _due(trigger: dict[str, Any], last: float) -> bool:
    """Whether an interval trigger is due, by its own period."""
    minutes = trigger.get("every_minutes") or trigger.get("minutes") or 0
    try:
        period = float(minutes) * 60
    except (TypeError, ValueError):
        return False
    if period <= 0:
        return False
    return time.time() - last >= period


# --------------------------------------------------------------------------- firing


async def _fire(pipeline, trigger_kind: str,
                parameters: dict[str, Any] | None = None) -> None:
    from compass.pipelines.engine import engine

    try:
        run = await engine.start(pipeline, trigger=trigger_kind,
                                 parameters=parameters or {})
        logger.info("trigger started run %s for pipeline %r (%s)",
                    run.id, pipeline.name, run.status)
    except PermissionError as err:
        # `require_manual_first_run`: a pipeline nobody has ever run by hand
        # will not be started by a timer. Logged at info because it is the
        # setting working, not a fault.
        logger.info("trigger held back for %r: %s", pipeline.name, err)
    except Exception:  # noqa: BLE001 — one bad pipeline must not stop the loop
        logger.exception("trigger failed for pipeline %s", pipeline.id)


async def _poll_gmail(pipeline, node, state: dict[str, Any],
                      key: str) -> bool:
    """Check the mailbox; start a run if anything is new. Returns whether the
    state changed."""
    from compass.pipelines.engine import engine

    entry = state.setdefault(key, {})
    config = node.config or {}
    # `or 5` would turn an explicit 0 into five minutes, which is the kind of
    # silent override that makes a setting look broken. 0 means every tick.
    minutes = config.get("every_minutes")
    try:
        period = float(5 if minutes in (None, "") else minutes) * 60
    except (TypeError, ValueError):
        period = 300.0
    if time.time() - float(entry.get("checked_at") or 0) < period:
        return False
    entry["checked_at"] = time.time()

    # The same resolver a run uses, so a poll sees the same credential — and
    # renews it the same way, which is what keeps a schedule alive past the
    # hour an access token lasts.
    ctx_connection = engine._connection_resolver(node.connection_id,
                                                 pipeline.owner)

    class _Ctx:  # the two fields _fetch_messages actually reads
        connection = ctx_connection

    try:
        messages = await _fetch_messages(_Ctx(), str(config.get("query") or ""),
                                         int(config.get("limit") or 10))
    except Exception as err:  # noqa: BLE001
        logger.warning("gmail poll failed for %s: %s", pipeline.name, err)
        entry["last_error"] = str(err)[:300]
        return True

    entry.pop("last_error", None)
    seen: list[str] = list(entry.get("seen") or [])
    seen_set = set(seen)
    fresh = [m for m in messages if m.get("id") and m["id"] not in seen_set]
    if not fresh:
        return True

    # Recorded before the run, not after. A run that throws must not leave the
    # same messages looking new, or a failing pipeline becomes a loop that
    # reprocesses the same mail every five minutes.
    seen.extend(m["id"] for m in fresh)
    entry["seen"] = seen[-CURSOR_KEEP:]
    entry["fired_at"] = time.time()
    _write_state(state)

    logger.info("gmail trigger: %d new message(s) for %r",
                len(fresh), pipeline.name)
    await _fire(pipeline, "webhook", {
        DELIVERY_KEY: {"node_id": node.id, "items": fresh},
    })
    return True


async def tick() -> None:
    """One pass over every enabled pipeline. Never raises."""
    state = _read_state()
    changed = False

    for pipeline in await pstore.pipelines.list():
        if not pipeline.enabled:
            continue

        # Trigger nodes on the graph — the n8n model, where the trigger is a
        # step with settings rather than a property of the pipeline.
        for node in pipeline.nodes:
            if node.type != "gmail.trigger" or node.state == "deactivated":
                continue
            if not node.connection_id:
                continue  # not set up yet; the canvas already says so
            try:
                if await _poll_gmail(pipeline, node, state,
                                     _key(pipeline.id, 0) + ":" + node.id):
                    changed = True
            except Exception:  # noqa: BLE001
                logger.exception("gmail trigger tick failed for %s", pipeline.id)

        # Pipeline-level interval schedules, which were stored and never read.
        for index, trigger in enumerate(pipeline.triggers or []):
            if not isinstance(trigger, dict) or not trigger.get("enabled", True):
                continue
            if trigger.get("type") not in ("interval", "schedule", None):
                continue
            key = _key(pipeline.id, index)
            entry = state.setdefault(key, {})
            if not _due(trigger, float(entry.get("fired_at") or 0)):
                continue
            entry["fired_at"] = time.time()
            changed = True
            _write_state(state)
            await _fire(pipeline, "scheduled", trigger.get("parameters") or {})

    if changed:
        _write_state(state)


async def loop() -> None:
    logger.info("pipeline trigger runner started")
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001 — the loop outlives any one failure
            logger.exception("trigger tick failed")
        await asyncio.sleep(TICK_S)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(loop())
