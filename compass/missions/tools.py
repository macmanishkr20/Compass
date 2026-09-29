"""Two tools that move a feature between states, and nothing else.

Until now a session changed `features.json` by editing it, under a prompt
telling it which field it was allowed to touch. That is a rule enforced by
asking nicely, and the one thing in a mission that must not be decided by
asking nicely is the scoreboard.

So the file becomes read-only to sessions and the transitions become tools:

  mission_claim    a builder: "built, and here is how I checked it"
  mission_verdict  a reviewer: "I tried it myself; here is the mark"

That split is the point of this phase. The builder cannot reach
`mission_verdict` — it is not in its tool list — so it cannot mark its own
work as done however convinced it is. The reviewer cannot reach
`mission_claim`, and has no file-writing tools at all, so it cannot quietly
fix what it was meant to report.

Both refuse an unknown feature id rather than inventing one, and both return
the mission's new score, so the session can see what it has actually changed.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from compass.common.tools.base import Tool, ToolOutput, ToolUseContext, ToolYield
from compass.missions.artifacts import MissionFiles

logger = logging.getLogger("compass.missions")

#: What a reviewer marks, and the mark each must reach. Adapted from the
#: criteria Anthropic used for the same job: two about whether the thing
#: works, two about whether it was built well. Thresholds rather than an
#: average, because an average lets a beautiful broken feature through.
CRITERIA: dict[str, tuple[str, int]] = {
    "functionality": ("Does the feature do what the contract says, when used "
                      "the way a person would use it?", 4),
    "completeness": ("Are the obvious edges handled — empty input, a second "
                     "click, a reload — or only the happy path?", 3),
    "craft": ("Is the implementation clean, consistent with the codebase, and "
              "free of debris left behind while building it?", 3),
    "durability": ("Will this still work after a restart, and does it leave "
                   "the app in a state the next session can build on?", 3),
}


def _files(ctx: ToolUseContext) -> MissionFiles:
    return MissionFiles(ctx.workspace_root or ".")


class ClaimInput(BaseModel):
    feature_id: str = Field(
        description="The id from features.json, exactly as written there.")
    contract: str = Field(
        description="What you agreed 'done' means for this feature — the "
                    "behaviour you set out to deliver and how it would be "
                    "checked. Write it before you write the code; it is what "
                    "the reviewer will grade against.")
    evidence: str = Field(
        description="What you actually did to check it: the commands you ran, "
                    "what you clicked, what you saw. Not 'implemented and "
                    "tested' — the specifics, so a reviewer can repeat them.")


class ClaimTool(Tool):
    name = "mission_claim"
    description = (
        "Say that you have built a feature and checked it yourself. This is a "
        "claim, not a verdict: the feature is held for review, and a separate "
        "reviewer session decides whether it passes. Call it once, at the end "
        "of the session, for the one feature you took — and only if you "
        "genuinely exercised the behaviour end to end. A claim that fails "
        "review costs another session and tells the next one where you went "
        "wrong, which is a perfectly good outcome; a claim you did not check "
        "wastes the review and teaches the mission nothing."
    )
    input_model = ClaimInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return False

    async def call(self, inp: ClaimInput,
                   ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        files = _files(ctx)
        feature = next((f for f in files.read_features()
                        if f.id == inp.feature_id), None)
        if feature is None:
            yield ToolOutput(_unknown(files, inp.feature_id), is_error=True)
            return
        if feature.passing:
            yield ToolOutput(
                f"{inp.feature_id} already passed review — nothing to claim. "
                "Take the next feature that is not passing.", is_error=True)
            return

        files.claim(inp.feature_id, contract=inp.contract, evidence=inp.evidence)
        done, total = files.tally()
        yield ToolOutput(
            f"Claimed {inp.feature_id}. It is now waiting for a reviewer; it "
            f"does not count as passing until one agrees. The mission stands "
            f"at {done} of {total}. Finish the session: commit, then write "
            "your handoff note.")


class VerdictInput(BaseModel):
    feature_id: str = Field(description="The feature you reviewed.")
    passed: bool = Field(
        description="True only if you exercised the behaviour yourself and it "
                    "worked. Default to false.")
    findings: str = Field(
        description="What you did, what you expected and what happened. On a "
                    "failure this is the whole value of the review: be "
                    "specific enough that it can be fixed without being "
                    "rediscovered.")
    scores: dict[str, int] = Field(
        default_factory=dict,
        description="A mark out of 5 for each of: "
                    + ", ".join(CRITERIA) + ".")


class VerdictTool(Tool):
    name = "mission_verdict"
    description = (
        "Record your decision on a feature you have just reviewed. This is "
        "the only way a feature becomes passing, and you are the only role "
        "that can call it.\n\n"
        "Marks are out of 5 and every one has a floor: "
        + "; ".join(f"{name} ≥ {floor}" for name, (_, floor) in CRITERIA.items())
        + ". A mark below its floor fails the feature whatever the others say "
        "— an average would let a beautiful broken feature through."
    )
    input_model = VerdictInput

    def is_read_only(self, inp: BaseModel) -> bool:
        return False

    async def call(self, inp: VerdictInput,
                   ctx: ToolUseContext) -> AsyncIterator[ToolYield]:
        files = _files(ctx)
        feature = next((f for f in files.read_features()
                        if f.id == inp.feature_id), None)
        if feature is None:
            yield ToolOutput(_unknown(files, inp.feature_id), is_error=True)
            return

        scores = {k: int(v) for k, v in (inp.scores or {}).items() if k in CRITERIA}
        below = [f"{name} {scores[name]} (needs {CRITERIA[name][1]})"
                 for name in scores if scores[name] < CRITERIA[name][1]]
        passed = bool(inp.passed) and not below
        # The floors are arithmetic, not judgement: a reviewer that marks
        # functionality 2 and still says PASS has contradicted itself, and the
        # marks are the part that was thought about.
        overridden = bool(inp.passed) and below

        files.verdict(inp.feature_id, passed=passed, findings=inp.findings,
                      scores=scores)
        done, total = files.tally()
        if passed:
            body = (f"{inp.feature_id} passes. The mission stands at {done} of "
                    f"{total}.")
        elif overridden:
            body = (f"{inp.feature_id} is marked failed despite your PASS: "
                    + "; ".join(below) + ". The floors decide. Your findings "
                    "are recorded and the next builder session will read them.")
        else:
            body = (f"{inp.feature_id} fails and goes back to the queue. Your "
                    "findings are recorded for the next builder session. The "
                    f"mission stands at {done} of {total}.")
        yield ToolOutput(body)


def _unknown(files: MissionFiles, feature_id: str) -> str:
    known = [f.id for f in files.read_features()]
    return (f"There is no feature called {feature_id!r} in this mission. The "
            f"ids are: {', '.join(known[:20]) or '(none — the plan is empty)'}. "
            "Use one exactly as written; do not add a feature.")
