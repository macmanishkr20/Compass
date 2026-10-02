"""The Missions REST surface — a self-contained router, mounted by server.py.

    POST   /v1/missions                  start a mission from a one-line brief
    GET    /v1/missions                  what has been run and where it got to
    GET    /v1/missions/{id}             one mission, with its features
    POST   /v1/missions/{id}/run         start it (detached) and watch, SSE
    GET    /v1/missions/{id}/stream      watch one already running, SSE
    POST   /v1/missions/{id}/abort       stop after the session in flight
    DELETE /v1/missions/{id}             forget the record, keep the code

Starting and watching are separate concerns here, which they were not at
first: the mission used to run *inside* the response that started it, so
closing the browser tab killed the session mid-flight. The server owns the
task now (see `runner`), and these endpoints are windows onto it.

The stream is the same shape the Code console uses — the agent's own events,
plus `mission_note` frames from the supervisor saying which session is
starting and why. A client that only knows the console's events sees an
ordinary agent transcript and ignores the notes.

Long by nature: a session is minutes and a mission is hours, so the stream is
wrapped in the same heartbeat the other surfaces use. Without it, silence
during a long build is indistinguishable from a dead connection.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from compass.common.auth import require_user
from compass.common.ownership import owned, visible_to
from compass.common.config import get_settings
from compass.common.gateway import limits
from compass.common.models.events import ErrorEvent
from compass.common.sse import with_heartbeat
from compass.missions.engine import DEFAULT_BUDGET_USD, Mission, MissionEngine
from compass.missions.store import get_mission_store
from compass.missions.runner import get_runners
from compass.missions.supervisor import blocked

logger = logging.getLogger("compass.missions")

router = APIRouter(prefix="/v1/missions", tags=["missions"])

#: Missions running in this process live in the runner registry, which owns
#: their tasks. Nothing here holds a mission open: the request that starts one
#: can end without stopping it.


class CreateMissionRequest(BaseModel):
    goal: str = Field(description="One to four sentences: what to build.")
    workspace: str = Field(
        default="",
        description="Where to build it. Defaults to a new folder named after "
                    "the mission under the server's workspaces directory.")
    model: str | None = None
    budget_usd: float = Field(
        default=DEFAULT_BUDGET_USD,
        description="What the whole mission may spend before it stops.")
    triggers: list[dict] = Field(
        default_factory=list,
        description="When it may start itself, in the same shape Routines "
                    "uses: {type: daily|weekdays|weekly|once|hourly, time: "
                    "'02:00', days: [0-6], date: 'YYYY-MM-DD'}. Empty means it "
                    "runs only when asked.")


async def _mission_or_404(mission_id: str, user: str = "") -> Mission:
    """The mission, if this person may see it.

    Every route that touches one mission comes through here, which is why the
    check lives here and not in each of them: reading it, running it, watching
    it, aborting it and deleting it are all the same question about the same
    record, and five copies of an answer is five chances to miss one.

    Not theirs reads as not there. A 403 would confirm the mission exists to
    somebody who has no business knowing even that, and the id is the only
    thing they would need to ask again.
    """
    mission = await get_mission_store().get(mission_id)
    if mission is None or not visible_to(user, mission.owner):
        raise HTTPException(status_code=404, detail="unknown mission")
    return mission


def _schedule_summary(mission: Mission) -> str:
    """One line describing when it starts itself, or "" when it does not."""
    from compass.missions.schedule import parse_triggers

    triggers = parse_triggers(mission.triggers)
    return " · ".join(t.summary() for t in triggers) if triggers else ""


#: What a feature is, for the card chart and the detail list's tabs. Five
#: states out of the three the model stores, because two of the three carry a
#: second meaning that matters to somebody reading the screen: a feature with
#: a reviewer's findings on it was *returned*, not merely unstarted, and the
#: one a running mission is on now is *building* rather than queued.
def _feature_state(feature, *, building: bool) -> str:
    if feature.passing:
        return "ok"
    if feature.claimed:
        return "rev"
    if building:
        return "run"
    return "blk" if feature.notes else "q"


def _states_and_score(mission: Mission, running: bool) -> tuple[list[str], float | None]:
    """Every feature's state, and the mean review mark across the scored ones.

    Read here rather than in the client because the client would otherwise
    have to fetch every mission's plan to draw one row of squares.
    """
    features = mission.files.read_features()
    # The one being built is the first that is neither passing nor claimed —
    # the same choice `next_to_build` makes — and only while something is
    # actually running to build it.
    building_id = None
    if running:
        nxt = next((f for f in features if not f.passing and not f.claimed), None)
        building_id = nxt.id if nxt else None
    states = [_feature_state(f, building=(f.id == building_id)) for f in features]

    marks = [sum(f.scores.values()) / len(f.scores)
             for f in features if f.scores]
    mean = round(sum(marks) / len(marks), 2) if marks else None
    return states, mean


def _view(mission: Mission, *, features: bool = False) -> dict:
    done, total = mission.files.tally()
    running = get_runners().running(mission.id)
    states, mean_score = _states_and_score(mission, running)
    blocked_reason, can_force = ("", False) if running else blocked(mission)
    out = {
        "id": mission.id,
        "goal": mission.goal,
        "workspace": str(mission.workspace),
        "model": mission.model,
        "budget_usd": mission.budget_usd,
        "spent_usd": round(mission.spent_usd, 4),
        "passing": done,
        "features": total,
        #: One short code per feature, in plan order: the card draws a square
        #: for each, so the shape of the work is visible without opening it.
        "feature_states": states,
        "mean_score": mean_score,
        "running": running,
        #: Why it will not start, and whether a person may override that.
        #: Read from the supervisor rather than guessed at by the client, so
        #: a button cannot offer something the server will refuse.
        "blocked_reason": blocked_reason,
        "can_force": can_force,
        "planned": mission.files.initialised(),
        "triggers": list(mission.triggers),
        "schedule": _schedule_summary(mission),
        # `tokens_per_minute` is derived, so it is not in `vars()` — and it is
        # the number that says whether a slow session was slow because of the
        # work or because of the deployment's quota.
        "sessions": [{**vars(s), "tokens_per_minute": round(s.tokens_per_minute),
                      "tokens_per_call": s.tokens_per_call}
                     for s in mission.sessions],
        "quota_tokens_per_minute": limits.observed_quota(),
    }
    if features:
        # The same five states the card chart uses, attached to each feature
        # so the list and the squares above it cannot disagree.
        plan = mission.files.read_features()
        out["feature_list"] = [{**f.to_dict(), "state": state}
                               for f, state in zip(plan, states)]
        out["progress"] = mission.files.read_progress()
    return out


@router.post("")
async def create_mission(body: CreateMissionRequest,
                         user: str = Depends(require_user)) -> dict:
    if not body.goal.strip():
        raise HTTPException(status_code=400, detail="a mission needs a goal")
    settings = get_settings()
    workspace = (Path(body.workspace) if body.workspace else
                 settings.workspaces_dir / f"mission-{len(await get_mission_store().list()) + 1}")
    mission = await get_mission_store().create(
        goal=body.goal, workspace=workspace, model=body.model,
        budget_usd=max(1.0, float(body.budget_usd)),
        triggers=body.triggers, owner=user)
    logger.info("mission %s created in %s", mission.id, workspace)
    return _view(mission)


@router.get("")
async def list_missions(user: str = Depends(require_user)) -> dict:
    return {"missions": [_view(m)
                         for m in owned(await get_mission_store().list(), user)]}


@router.get("/{mission_id}")
async def get_mission(mission_id: str, user: str = Depends(require_user)) -> dict:
    return _view(await _mission_or_404(mission_id, user), features=True)


@router.post("/{mission_id}/run")
async def run_mission(mission_id: str, force: bool = False,
                      user: str = Depends(require_user)) -> StreamingResponse:
    """Start the mission if it is not already going, and watch it.

    Starting and watching are the same call on purpose: a client does not have
    to know whether it is the first window onto this mission or the second.

    `force` is a person overriding a stop the supervisor made on their behalf
    — a stall, repeated failures, the session cap. It buys one more session,
    never a finished or overspent mission, and never more than one.
    """
    mission = await _mission_or_404(mission_id, user)
    runners = get_runners()
    store = get_mission_store()
    # A mission already running is a second window onto it, not a new start,
    # so the cap does not apply — that call has always been idempotent.
    if not runners.running(mission_id):
        cap = get_settings().missions.max_parallel
        if runners.live() >= cap and not force:
            # Refused rather than queued: this was a person pressing a button,
            # and a button that silently does nothing for ten minutes is worse
            # than one that says why. Forceable, like the supervisor's own
            # stop reasons, because the cap protects a shared quota and the
            # person may know something about it that this does not.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{runners.live()} missions are already running "
                    f"(limit {cap}). Wait for one to finish, raise "
                    f"COMPASS_MISSIONS_MAX_PARALLEL, or run it with force."
                ),
            )
    run = runners.start(mission, MissionEngine(), store.save, force=force)
    return _watch(run, replay=True)


@router.get("/{mission_id}/stream")
async def stream_mission(mission_id: str,
                         user: str = Depends(require_user)) -> StreamingResponse:
    """Watch a mission already running, without starting anything.

    What a reopened browser calls. A mission that is not running answers with
    the events it has in hand and closes, rather than hanging on a stream that
    will never produce anything.
    """
    await _mission_or_404(mission_id, user)
    run = get_runners().get(mission_id)
    if run is None:
        raise HTTPException(status_code=404, detail="this mission is not running")
    return _watch(run, replay=True)


def _watch(run, *, replay: bool) -> StreamingResponse:
    async def stream():
        try:
            async for event in run.watch(replay=replay):
                yield event.to_sse()
        except asyncio.CancelledError:
            # A closed tab. The mission is a task the server owns, so this
            # ends the watching and nothing else.
            logger.info("mission %s: a watcher disconnected", run.mission.id)
            raise
        except Exception as err:  # noqa: BLE001 — the stream ends with an event
            logger.exception("mission %s stream failed", run.mission.id)
            yield ErrorEvent(message=str(err)).to_sse()

    return StreamingResponse(
        with_heartbeat(stream()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{mission_id}/abort")
async def abort_mission(mission_id: str, user: str = Depends(require_user)) -> dict:
    await _mission_or_404(mission_id, user)
    if not get_runners().abort(mission_id):
        return {"aborted": False, "detail": "not running"}
    # After the session in flight, not during it: see Supervisor.abort.
    return {"aborted": True, "detail": "will stop after the current session"}


@router.delete("/{mission_id}")
async def delete_mission(mission_id: str, user: str = Depends(require_user)) -> dict:
    if get_runners().running(mission_id):
        raise HTTPException(status_code=409,
                            detail="stop the mission before deleting it")
    mission = await _mission_or_404(mission_id, user)
    from compass.common import audit, media_index
    from compass.common.ownership import owner_for

    # Anything its sessions drew or captured. The workspace on disk is left
    # exactly where it is — see MissionStore.delete; deleting somebody's code
    # because they closed a job is not a thing this gets to decide — so the
    # note says so rather than implying everything went.
    tally = {}
    for index in range(1, len(mission.sessions) + 1):
        for key, value in (await media_index.purge_session(
                f"{mission.id}-{index}")).items():
            tally[key] = tally.get(key, 0) + value
    deleted = await get_mission_store().delete(mission_id)
    await audit.note_deletion(
        module="missions", kind="mission", record_id=mission_id,
        owner=owner_for(user), title=mission.goal[:200],
        removed={"mission": deleted, "workspace_kept": str(mission.workspace),
                 "sessions": len(mission.sessions), **tally},
    )
    return {"deleted": deleted}
