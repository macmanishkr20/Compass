"""The files a mission leaves behind for the agent that comes after it.

A long-running build happens in sessions, and each session starts with no
memory of the last one. Compaction does not solve this — it shortens a history
so the *same* agent can keep going. It says nothing to an agent that starts
fresh tomorrow.

What bridges the gap is not cleverness but paperwork, which is what human
engineers use for the same problem: a list of what the thing must do, a log of
what has been done, a script that starts the app, and a git history. Three
files and a repository:

  features.json  every feature, each `passing` false until proved otherwise
  PROGRESS.md    what each session did, newest last
  init.sh        how to start this thing, so nobody has to work it out again

`features.json` is JSON rather than Markdown deliberately. Anthropic's teams
found models will quietly rewrite a Markdown checklist and are markedly more
reluctant to restructure JSON — and the one thing that must not happen here is
an agent deleting the tests it cannot pass.

Nothing in this module talks to a model. It reads and writes files, validates
what it finds, and refuses changes that would let a session declare victory by
editing the scoreboard.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("compass.missions")

FEATURES = "features.json"
PROGRESS = "PROGRESS.md"
INIT = "init.sh"

#: A mission with fewer than this has not been thought about; one with more is
#: a roadmap rather than a build. Both ends are advisory — the planner is told,
#: not enforced against.
SENSIBLE_FEATURES = (12, 200)

#: What one feature costs to carry from "todo" to "passing": at least one
#: builder session and one reviewer session, plus the rejected claims and the
#: sessions lost to a rate limit that nobody budgeted for.
#:
#: Measured from the first real mission on this install rather than guessed:
#: 13 sessions, $4.42, 2 features passing. Excluding the $1.56 spent on five
#: sessions that died mid-flight, $2.86 bought two features — $1.43 each.
COST_PER_FEATURE_USD = 1.40


def affordable_features(budget_usd: float) -> tuple[int, int]:
    """How many features a budget can actually finish.

    The planner used to be told "aim for 12–200" with no idea what it could
    afford, so it planned for the software rather than for the money: a $25
    mission was given 50 features, which is about $70 of work, and it stopped
    at 2 of them with the budget gone. A plan that cannot be finished is not
    an ambitious plan, it is an unfinished one, and every session after the
    money runs out reads a list mostly made of things that will never be
    built.

    The range is deliberately narrow at the top. Planning under what the
    budget allows leaves room for the reviews and the retries, and a mission
    that finishes can always be given more.
    """
    low, high = SENSIBLE_FEATURES
    affordable = int(max(1.0, budget_usd) / COST_PER_FEATURE_USD)
    ceiling = max(low, min(high, affordable))
    floor = max(4, int(ceiling * 0.6))
    return floor, ceiling


@dataclass
class Feature:
    """One end-to-end thing the finished software must do.

    `steps` is what a person would do to check it, written for whoever
    verifies rather than whoever implements — the point is to describe the
    behaviour, not the code.

    Three states, and the middle one is the whole of Phase 2. A builder may
    say "I have built this and here is how I checked it" — that is `claimed`.
    Only a reviewer, which is a different session with no ability to edit the
    code, may turn a claim into `passing`. An agent asked to grade its own
    work praises it; separating the two is the only thing that reliably
    stops a feature being marked done while the button does nothing.
    """

    id: str
    description: str
    steps: list[str] = field(default_factory=list)
    passing: bool = False
    #: Built and self-checked, waiting for a reviewer. Never both this and
    #: `passing`: a verdict replaces a claim.
    claimed: bool = False
    #: What the builder agreed "done" would mean, written before the code and
    #: what the reviewer grades against.
    contract: str = ""
    #: The builder's evidence when claiming; replaced by the reviewer's
    #: findings when a claim is rejected, which is what the next session
    #: reads instead of rediscovering the bug.
    notes: str = ""
    #: The reviewer's marks per criterion, for the record.
    scores: dict = field(default_factory=dict)
    verified_at: float = 0.0

    @property
    def state(self) -> str:
        return "passing" if self.passing else ("claimed" if self.claimed else "todo")

    def to_dict(self) -> dict:
        return {"id": self.id, "description": self.description,
                "steps": self.steps, "passing": self.passing,
                "claimed": self.claimed, "contract": self.contract,
                "notes": self.notes, "scores": self.scores,
                "verified_at": self.verified_at}

    @classmethod
    def from_dict(cls, row: dict) -> "Feature":
        return cls(
            id=str(row.get("id") or "").strip() or "unnamed",
            description=str(row.get("description") or "").strip(),
            steps=[str(s) for s in (row.get("steps") or [])],
            passing=bool(row.get("passing")),
            claimed=bool(row.get("claimed")),
            contract=str(row.get("contract") or ""),
            notes=str(row.get("notes") or ""),
            scores=dict(row.get("scores") or {}),
            verified_at=float(row.get("verified_at") or 0.0),
        )


class MissionFiles:
    """The artifacts inside one workspace."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ── features ─────────────────────────────────────────────────────────
    @property
    def features_path(self) -> Path:
        return self.root / FEATURES

    def read_features(self) -> list[Feature]:
        path = self.features_path
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            logger.error("mission: %s is unreadable: %s", FEATURES, err)
            return []
        rows = raw.get("features") if isinstance(raw, dict) else raw
        return [Feature.from_dict(r) for r in (rows or []) if isinstance(r, dict)]

    def write_features(self, features: list[Feature], *, goal: str = "") -> None:
        payload = {"goal": goal or self.goal(), "features":
                   [f.to_dict() for f in features]}
        self.features_path.write_text(json.dumps(payload, indent=2) + "\n")

    def goal(self) -> str:
        try:
            raw = json.loads(self.features_path.read_text())
            return str(raw.get("goal") or "") if isinstance(raw, dict) else ""
        except (OSError, json.JSONDecodeError):
            return ""

    def remaining(self) -> list[Feature]:
        """Not yet proved — which includes claims a reviewer has not seen."""
        return [f for f in self.read_features() if not f.passing]

    def claimed(self) -> list[Feature]:
        return [f for f in self.read_features() if f.claimed and not f.passing]

    def next_to_build(self) -> Feature | None:
        """The feature a builder should take: the first that is neither
        passing nor already waiting on a reviewer."""
        return next((f for f in self.read_features()
                     if not f.passing and not f.claimed), None)

    def tally(self) -> tuple[int, int]:
        features = self.read_features()
        return sum(1 for f in features if f.passing), len(features)

    def _update(self, feature_id: str, change) -> bool:
        features = self.read_features()
        for feature in features:
            if feature.id == feature_id:
                change(feature)
                self.write_features(features)
                return True
        return False

    def claim(self, feature_id: str, *, contract: str, evidence: str) -> bool:
        """A builder says it is done and how it checked. Not a verdict."""
        def change(feature: Feature) -> None:
            feature.claimed = True
            feature.passing = False
            feature.contract = contract[:2000] or feature.contract
            feature.notes = evidence[:1200]
        return self._update(feature_id, change)

    def verdict(self, feature_id: str, *, passed: bool, findings: str,
                scores: dict | None = None) -> bool:
        """A reviewer's decision. The only way a feature becomes `passing`."""
        def change(feature: Feature) -> None:
            feature.passing = passed
            feature.claimed = False
            feature.notes = findings[:1200]
            feature.scores = dict(scores or {})
            feature.verified_at = time.time()
        return self._update(feature_id, change)

    def mark(self, feature_id: str, *, passing: bool, notes: str = "") -> bool:
        """Set a verdict directly. Kept for the store's own use and tests;
        sessions go through `claim` and `verdict`, which is what keeps the
        two roles apart."""
        return self.verdict(feature_id, passed=passing, findings=notes)

    # ── progress ─────────────────────────────────────────────────────────
    @property
    def progress_path(self) -> Path:
        return self.root / PROGRESS

    def read_progress(self, *, last: int = 4000) -> str:
        try:
            text = self.progress_path.read_text()
        except OSError:
            return ""
        # The tail, because the next session needs what happened recently and
        # a mission that runs for days would otherwise spend its context on
        # its own history.
        return text[-last:] if len(text) > last else text

    def append_progress(self, session: int, body: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        entry = f"\n## Session {session} — {stamp}\n\n{body.strip()}\n"
        with self.progress_path.open("a") as handle:
            handle.write(entry)

    # ── init script ──────────────────────────────────────────────────────
    @property
    def init_path(self) -> Path:
        return self.root / INIT

    def has_init(self) -> bool:
        return self.init_path.is_file()

    def initialised(self) -> bool:
        """Whether a planner has already laid the foundations here."""
        return self.features_path.is_file() and bool(self.read_features())

    def bearings(self) -> str:
        """What a session is told before it starts work.

        Deliberately assembled here rather than left to the agent to gather
        with tool calls: it is the same three files every time, and paying a
        round trip each to read them is paying for nothing.
        """
        done, total = self.tally()
        lines = [f"Mission goal: {self.goal() or '(not recorded)'}",
                 f"Features passing: {done} of {total}."]
        waiting = self.claimed()
        if waiting:
            lines.append("\nBuilt and waiting on a reviewer (do not rebuild):")
            for feature in waiting[:5]:
                lines.append(f"  [{feature.id}] {feature.description}")
        remaining = [f for f in self.remaining() if not f.claimed]
        if remaining:
            lines.append("\nStill to do (the first is the one to take):")
            for feature in remaining[:8]:
                lines.append(f"  [{feature.id}] {feature.description}")
                if feature.notes:
                    # A rejected claim carries the reviewer's findings. This
                    # is the single most useful line a new session can read.
                    lines.append(f"      last review: {feature.notes[:200]}")
            if len(remaining) > 8:
                lines.append(f"  … and {len(remaining) - 8} more")
        else:
            lines.append("\nEvery feature is marked passing.")
        progress = self.read_progress(last=2000)
        if progress.strip():
            lines.append("\nRecent progress notes:\n" + progress.strip())
        if self.has_init():
            lines.append(f"\n`{INIT}` exists — run it to start the app before "
                         "changing anything, and confirm the app still works.")
        return "\n".join(lines)
