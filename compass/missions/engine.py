"""Running a build across many context windows.

One session is one full agent run: a fresh message history, a system prompt
naming which persona it is, and a briefing assembled from the files the last
session left behind. When it ends, the next one starts from nothing again.

That last part is the point, and it is what makes this different from the
context pipeline Compass already has. `compaction` keeps one conversation
going by summarising what is behind it; the agent continues, shortened. A
mission does not continue — it stops, and a new agent reads the workspace. The
handoff is the artifacts, not a summary, and the work of the harness is making
sure they are worth reading.

Sessions are run sandboxed, whatever the global setting says. Nobody is
watching an unattended build, so the permission gate has nobody to ask; the
boundary is what makes running without approval something other than
recklessness.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from compass.common import sandbox
from compass.common.agent.query_loop import query
from compass.common.gateway import limits
from compass.common.gateway.cost_tracker import CostTracker
from compass.common.models import events
from compass.common.models.messages import Message, user_message
from compass.common.tools.base import PermissionBroker, ToolUseContext
from compass.common.tools import reaper
from compass.common.tools.shell_session import ShellState
from compass.missions.artifacts import (
    COST_PER_FEATURE_USD, MissionFiles, affordable_features,
)
from compass.missions import personas

logger = logging.getLogger("compass.missions")

#: A session that has not finished in this many turns is looping, not working.
#: Lower than the console's 50 because a mission session has one feature to
#: build and then a handoff to write; a session still going at 40 is one that
#: has lost the thread, and stopping it leaves the next session a clean start
#: rather than a half-finished change.
MAX_TURNS_PER_SESSION = 40

#: What a whole mission may cost before it stops and asks. Unattended work
#: with no ceiling is how a six-hour build becomes a surprise.
DEFAULT_BUDGET_USD = 25.0


@dataclass
class SessionResult:
    session: int
    persona: str
    turns: int
    cost_usd: float
    passing: int
    total: int
    stopped: str = ""  #: why it ended, when it was not the model's choice
    summary: str = ""
    #: Tokens and wall time, without which a slow mission cannot be explained.
    #: Their absence was a real gap: the first mission on this install took an
    #: hour and the record of it could not say whether that was the model
    #: thinking, the tool calls, or — as it turned out — a 50,000 token/minute
    #: quota metering a conversation that had grown to 28,000 tokens a call.
    #: `prompt_tokens` is the one that matters, because it is what the quota
    #: is spent on and it is the number that grows with every turn.
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    #: Model calls made. The divisor for tokens-per-call, and the only honest
    #: one: `turns` counts completed turns, and a session that dies mid-turn
    #: records zero of them while having made thirty calls.
    calls: int = 0

    @property
    def tokens_per_call(self) -> int:
        return round(self.prompt_tokens / self.calls) if self.calls else 0

    @property
    def tokens_per_minute(self) -> float:
        """What this session drew against the deployment's quota.

        Comparing this with the quota says whether a session was slow because
        the work was hard or because it spent the minute waiting for one.
        """
        if self.seconds <= 0:
            return 0.0
        return (self.prompt_tokens + self.output_tokens) / (self.seconds / 60.0)


@dataclass
class Mission:
    """One long-running build, and where it has got to."""

    id: str
    goal: str
    workspace: Path
    model: str | None = None
    budget_usd: float = DEFAULT_BUDGET_USD
    spent_usd: float = 0.0
    #: When it may start itself, in Routines' trigger shape. Empty means it
    #: only ever runs because somebody asked it to.
    triggers: list[dict] = field(default_factory=list)
    sessions: list[SessionResult] = field(default_factory=list)

    @property
    def files(self) -> MissionFiles:
        return MissionFiles(self.workspace)

    def finished(self) -> bool:
        done, total = self.files.tally()
        return total > 0 and done == total

    def over_budget(self) -> bool:
        return self.spent_usd >= self.budget_usd


class MissionEngine:
    """Runs sessions. Knows nothing about HTTP or storage."""

    def __init__(self, tools_for=None) -> None:
        # Injected so a test can run a mission with two harmless tools, and so
        # the reviewer can be given a different set from the builder.
        self._tools_for = tools_for or _default_tools

    async def run_session(self, mission: Mission, *,
                          persona: str = "builder") -> AsyncIterator[events.Event]:
        """One session, start to finish. Yields the agent's events as they
        happen, so a surface can stream them."""
        files = mission.files
        number = len(mission.sessions) + 1

        if persona == "planner":
            system, opening = personas.PLANNER, _planner_briefing(mission)
        elif persona == "reviewer":
            system, opening = personas.REVIEWER, _reviewer_briefing(mission)
        else:
            system = personas.BUILDER
            opening = personas.briefing(mission.goal, files.bearings(), number)

        # A fresh history every time. This is the context reset: nothing from
        # the previous session is carried in, and what it knew has to have
        # been written down to survive.
        messages: list[Message] = [user_message(opening)]
        ctx = self._context(mission, persona)
        tracker = ctx.cost_tracker

        turns = 0
        started = time.monotonic()
        last_text: list[str] = []
        stopped = ""
        # Whatever the install's setting, this session's commands are bounded.
        # The whole consumption of the stream is inside the scope, not just
        # the call that creates it: an async generator reads context variables
        # when it runs, so a `with` block around the call alone would have
        # been reset before the first command ever spawned.
        #
        # Reaping is NOT opened here, and that is deliberate. This function
        # is an async generator, and a `with` inside one only unwinds when the
        # generator is closed — which the consumer may never do promptly. When
        # a session died on a rate limit mid-command, this block's cleanup ran
        # late and in a context that no longer held the tracked groups, so the
        # server that session had started was never stopped. The scope now
        # lives in `MissionRun._drive`, an ordinary coroutine whose `finally`
        # is guaranteed. Sandboxing stays here because its failure mode is the
        # safe direction: a leaked scope sandboxes more, not less.
        try:
            with sandbox.required():
                async for event in query(messages, ctx, system_prompt=system,
                                         model=mission.model,
                                         max_turns=MAX_TURNS_PER_SESSION):
                    if isinstance(event, events.AssistantMessage) and event.content:
                        last_text.append(str(event.content))
                    if isinstance(event, events.TurnComplete):
                        turns = getattr(event, "turns", turns)
                    yield event
        except Exception as err:  # noqa: BLE001
            # A session that dies is still a session. Recorded with the reason
            # and the work it had already committed, because the alternative —
            # an exception escaping into the supervisor — loses the cost, the
            # turn count and any note of what happened, and the next session
            # starts with an unexplained gap in the log. Rate limits and
            # dropped connections are the ordinary case over hours of work,
            # not the exceptional one.
            stopped = f"{type(err).__name__}: {err}"
            logger.warning("mission %s session %d ended early: %s",
                           mission.id, number, stopped)
            yield events.ErrorEvent(message=f"session {number} stopped: {stopped}")

        if persona == "planner":
            _normalise_plan(mission)

        # `total_cost_usd()` is a method, not a property — adding the bound
        # method to a float is a TypeError, and it is the sort that only
        # shows up on the failure path if the failure path is never tested.
        try:
            cost = float(tracker.total_cost_usd())
        except Exception:  # noqa: BLE001 — accounting must not end a session
            cost = 0.0
        mission.spent_usd += cost
        done, total = files.tally()
        if not stopped and mission.over_budget():
            stopped = (f"budget: ${mission.spent_usd:.2f} of "
                       f"${mission.budget_usd:.2f} spent")

        usage = _usage_of(tracker)
        result = SessionResult(session=number, persona=persona, turns=turns,
                               cost_usd=round(cost, 4), passing=done,
                               total=total, stopped=stopped,
                               summary=" ".join(last_text)[-1200:],
                               prompt_tokens=usage["prompt"],
                               cached_tokens=usage["cached"],
                               output_tokens=usage["output"],
                               calls=usage["calls"],
                               seconds=round(time.monotonic() - started, 1))
        mission.sessions.append(result)
        logger.info(
            "mission %s session %d (%s): %d/%d features, $%.2f, "
            "%d turns, %d calls, %s prompt tokens (%s/call, %d%% cached), "
            "%.0fs, %.0f tok/min",
            mission.id, number, persona, done, total, cost, turns,
            result.calls, f"{usage['prompt']:,}",
            f"{result.tokens_per_call:,}",
            round(100 * usage["cached"] / usage["prompt"]) if usage["prompt"] else 0,
            result.seconds, result.tokens_per_minute,
        )
        _warn_if_quota_bound(mission.id, number, result)

    def _context(self, mission: Mission, persona: str) -> ToolUseContext:
        """A session's tools and trust.

        `auto_grant` with a sandbox, rather than `auto_deny` or a human: there
        is nobody to ask, so the question is whether the boundary is real, not
        whether somebody approved each command.
        """
        # `shell_state.root` is what actually decides where bash starts, and
        # it is separate from `workspace_root`. Setting only the latter is how
        # a mission's first session wrote its whole plan into the server's own
        # repository: the shell fell back to the global workspace, the sandbox
        # drew its boundary round *that*, and every write was permitted
        # because it was inside the wrong workspace.
        return ToolUseContext(
            session_id=f"{mission.id}-{len(mission.sessions) + 1}",
            tools=self._tools_for(persona),
            broker=PermissionBroker(policy="auto_grant"),
            cost_tracker=CostTracker(),
            permission_mode="bypass",
            workspace_root=mission.workspace,
            shell_state=ShellState(root=str(mission.workspace)),
        )


def _default_tools(persona: str) -> list:
    """The console's own tools, plus the one transition this persona may make.

    The split is enforced here rather than in the prompt. A builder is handed
    `mission_claim` and never sees `mission_verdict`, so it cannot mark its
    own work as done however convinced it is. A reviewer gets the verdict tool
    and no way to write a file at all — an evaluator that can edit the code it
    is grading fixes the bug instead of reporting it, and the session that
    claimed the feature works turns out to have been right all along.
    """
    from compass.code.tools.registry import get_all_tools
    from compass.missions.tools import ClaimTool, VerdictTool

    tools = get_all_tools()
    if persona == "reviewer":
        writers = {"file_write", "file_edit", "notebook_edit"}
        return [t for t in tools
                if getattr(t, "name", "") not in writers] + [VerdictTool()]
    if persona == "builder":
        return tools + [ClaimTool()]
    return tools  # the planner writes the plan and claims nothing


def _normalise_plan(mission: Mission) -> None:
    """Tidy what the planner left, and record the goal beside it.

    A model asked for a JSON file of features writes a bare array about as
    often as it writes the envelope, and both are reasonable readings of the
    instruction. Rather than insist, the file is rewritten once into the shape
    every later session reads — which is also the moment to write the goal
    into it, so a builder's bearings say what it is building rather than
    "(not recorded)".
    """
    files = mission.files
    features = files.read_features()
    if not features:
        return
    # Nothing may arrive already passing or claimed: a plan is a list of what
    # is not done, and a planner that pre-ticks its own boxes has written a
    # report rather than a plan.
    for feature in features:
        feature.passing = False
        feature.claimed = False
        feature.verified_at = 0.0
    files.write_features(features, goal=mission.goal)


def _usage_of(tracker) -> dict[str, int]:
    """Tokens this session drew, flattened across models.

    Defensive because accounting must never be the thing that ends a session:
    a tracker that cannot be read gives zeros and a slightly poorer log, not
    a lost hour of work.
    """
    out = {"prompt": 0, "cached": 0, "output": 0, "calls": 0}
    try:
        for usage in getattr(tracker, "by_model", {}).values():
            out["prompt"] += getattr(usage, "prompt_tokens", 0) or 0
            out["cached"] += getattr(usage, "cached_prompt_tokens", 0) or 0
            out["output"] += getattr(usage, "completion_tokens", 0) or 0
            out["calls"] += getattr(usage, "requests", 0) or 0
    except Exception:  # noqa: BLE001
        pass
    return out


def _warn_if_quota_bound(mission_id: str, number: int, result: SessionResult) -> None:
    """Say so when a session was limited by the deployment, not the work.

    This is the line that was missing when the first mission took an hour.
    Everything needed to write it was already known — the quota comes back on
    every response and the tokens were in the tracker — but nothing put the
    two together, so a mission metered at under two model calls a minute
    looked exactly like a mission that was simply thinking hard.
    """
    quota = limits.observed_quota()
    if not quota or result.seconds < 60 or result.turns < 2:
        return
    drawn = result.tokens_per_minute
    if drawn < quota * 0.6:
        return
    # Divided by calls, not turns: a session that dies mid-turn has made
    # dozens of calls and completed no turns, and dividing by turns then
    # reports the whole session's prompt as the size of one call.
    per_call = result.tokens_per_call or (result.prompt_tokens / max(1, result.turns))
    logger.warning(
        "mission %s session %d was quota-bound: drew ~%s tokens/min against a "
        "%s/min deployment, at ~%s prompt tokens per call. The work was not "
        "the slow part — roughly %.1f model calls per minute were possible. "
        "Trimming context per call, or raising the deployment's quota, is "
        "what makes this faster.",
        mission_id, number, f"{drawn:,.0f}", f"{quota:,}", f"{per_call:,.0f}",
        quota / per_call if per_call else 0,
    )


def _planner_briefing(mission: Mission) -> str:
    """What the planner is told, including what the mission can afford.

    The budget is named here rather than left implicit because the planner is
    the only session that can act on it: once the feature list is written,
    every later session is reading a plan whose size is already decided.
    """
    low, high = affordable_features(mission.budget_usd)
    return (
        f"The brief is: {mission.goal}\n\n"
        f"The workspace is {mission.workspace}.\n\n"
        f"This mission's whole budget is ${mission.budget_usd:.2f}, and a "
        f"feature costs roughly ${COST_PER_FEATURE_USD:.2f} to build and "
        f"review. Aim for {low}–{high} features — that is what this budget "
        f"can carry to passing, so a longer list would only be work nobody "
        f"pays for. Lay the foundations as instructed, then stop."
    )


def _reviewer_briefing(mission: Mission) -> str:
    """What a reviewer is handed: the claims, their contracts, and the
    evidence offered for them — so the session starts by testing a specific
    promise rather than by working out what changed."""
    files = mission.files
    waiting = files.claimed()
    lines = [f"The build's goal is: {mission.goal}", ""]
    if not waiting:
        lines.append("Nothing is claimed. Start the application, confirm it "
                     "runs, and report what state it is in.")
        return "\n".join(lines)

    lines.append(f"{len(waiting)} feature(s) claimed and waiting on you. "
                 "Verify each one yourself and record a verdict for every one "
                 "with `mission_verdict`.")
    for feature in waiting:
        lines.append(f"\n[{feature.id}] {feature.description}")
        if feature.contract:
            lines.append(f"  the builder's contract: {feature.contract}")
        if feature.notes:
            lines.append(f"  evidence offered: {feature.notes}")
        for step in feature.steps[:6]:
            lines.append(f"  step: {step}")
    recent = files.read_progress(last=1200)
    if recent.strip():
        lines.append("\nWhat the last session said it did:\n" + recent.strip())
    return "\n".join(lines)
