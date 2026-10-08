"""What a service line and its skills are, as data rather than code.

A service line — Tax, Talent, Finance, Risk — is a folder with a
`manifest.yaml` in it. The manifest says who owns the line, how its data must
be handled, and which skills it offers; each skill is a nested entry naming a
prompt file and an output contract. Nothing here is a Python subclass, and
adding a service line adds no code at all.

That is the whole point. The people who know Tax are tax people, and a
manifest, a prompt and a field list are things they can read and argue with.
A `TaxServiceLine(ServiceLine)` is not.

TWO LAYERS, AND THE INNER ONE CANNOT WIDEN THE OUTER. The service line answers
four governance questions once; every skill inside it inherits those answers
and may not weaken them. A skill may ask for *fewer* tools than the line
allows and never for more, and `WITHHELD_TOOLS` is refused to both — a skill
that lists `bash` does not get a warning and a narrowed toolset, it fails to
load, because a manifest that says something false about its own powers is
worse than no manifest.

A WORD ON TRUST, borrowed from compass/common/skills.py and for the same
reason: a manifest's text ends up in a system prompt. Names and descriptions
carrying markup are rejected rather than passed through, lengths are capped,
and a malformed manifest is skipped with a warning instead of taking the
server down with it.

This module only *describes*. Finding manifests on disk and caching them is
compass/servicelines/registry.py.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Identifier rules, matching the skills loader's so there is one convention
#: for "a name Compass will accept" rather than two.
_ID_OK = re.compile(r"^[a-z0-9_]+$")
ID_MAX = 48
NAME_MAX = 64
DESCRIPTION_MAX = 1024

#: Anything that could be read as markup. These strings reach a system prompt,
#: and a description full of tags is an injection attempt wearing a
#: description's clothes.
_MARKUP = re.compile(r"<[^>]*>")

#: Tools no service-line agent may ever hold, whatever any manifest says.
#: These are the names from compass/code/tools/registry.py. A service line
#: works on client documents inside one engagement; a shell, a filesystem and
#: the open web are all ways out of that boundary, so the denial lives here —
#: above every manifest — rather than being something each one remembers to
#: switch off.
WITHHELD_TOOLS = frozenset({
    "bash", "bash_output",
    "file_read", "file_write", "file_edit", "glob", "grep",
    "browser", "screenshot",
    "web_fetch",
    "agent",  # no sidechains: a subagent would inherit a context it cannot re-check
})

#: How many skills one service line may offer. Past this the lead agent is
#: choosing from a wall rather than a menu — the same recall problem that
#: makes the tool shelf necessary.
MAX_SKILLS = 24


def _unusable(text: str, *, limit: int, what: str) -> str:
    """Why this piece of prompt-bound text is unusable, or "" when it is fine."""
    if not text.strip():
        return f"no {what}"
    if len(text) > limit:
        return f"{what} is longer than {limit} characters"
    if _MARKUP.search(text):
        return f"{what} contains markup"
    return ""


class Governance(BaseModel):
    """The four questions every service line answers before it may be used.

    They are deliberately not optional and deliberately not defaulted. A
    service line created this morning with no answers is *visible* and
    *unusable*: `configured` is False, and the registry will not let an
    engagement open against it. Silence is not consent about where client data
    lives or how long it is kept — see `ServiceLineManifest.unconfigured`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: What kind of data this line handles, in the firm's own words
    #: ("client-confidential", "personal data"). Carried onto every engagement
    #: and shown on screen; not an enum, because the vocabulary is the firm's.
    classification: str = ""
    #: Where it may be stored, e.g. "india-south". Matched against the
    #: deployment's own region at engagement time, not here.
    residency: str = ""
    #: Statutory or policy retention. Tax keeps working papers for eight
    #: years; Talent keeps personal data for two. Compass's own audit rows
    #: expire in three months, and this overrides that upwards, never down.
    retention_months: int = 0
    #: Who must sign a run off before anything is emitted. A role, not a
    #: person, so it survives somebody leaving.
    reviewer: str = ""

    @property
    def configured(self) -> bool:
        return bool(
            self.classification.strip()
            and self.residency.strip()
            and self.reviewer.strip()
            and self.retention_months > 0
        )

    def problems(self) -> list[str]:
        """Each unanswered question, for a screen that has to say what is missing."""
        missing = []
        if not self.classification.strip():
            missing.append("classification")
        if not self.residency.strip():
            missing.append("residency")
        if self.retention_months <= 0:
            missing.append("retention_months")
        if not self.reviewer.strip():
            missing.append("reviewer")
        return missing


class Evidence(BaseModel):
    """What a skill must be able to show for the values it produces.

    Both default to the strict setting and a manifest may only ever confirm
    them. They are expressed as fields rather than being implicit so that a
    reader of the manifest can see the rule, and so a future service line that
    genuinely does not need provenance has somewhere to say so — under review,
    rather than by quietly omitting a check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Every value carries the document and page it came from. A field with no
    #: source is reported as not found; it is never inferred.
    require_source: bool = True
    #: Who computes derived fields. "engine" means code does the arithmetic
    #: and the model cannot overwrite the result.
    computed_fields: Literal["engine", "model"] = "engine"


class Review(BaseModel):
    """Whether a person stands between the agent's output and the world."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Nothing is emitted without a named human approving it.
    signoff: bool = True
    #: Which role signs. Empty inherits the service line's `reviewer`, which
    #: is the normal case; a skill overrides only to ask for someone *more*
    #: senior, which `SkillManifest.problems` does not police because seniority
    #: is not a thing this module can rank.
    reviewer: str = ""


class SkillManifest(BaseModel):
    """One capability a service line offers.

    A skill is a prompt plus an output contract plus a list of what it may
    read. It is not a Python class and it holds no logic: the engine that runs
    it is the same for every skill, and what differs is this file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    description: str
    #: Path to the briefing, relative to the service line's own folder.
    prompt: str
    #: Semantic version, pinned onto an engagement when it is opened so a
    #: later approval cannot change a run already under way.
    version: str = "0.1.0"
    #: "draft" cannot run; "approved" can; "retired" stays loadable so old
    #: engagements can still name the version they pinned.
    status: Literal["draft", "approved", "retired"] = "draft"
    #: What it produces, e.g. {"kind": "form", "form": "forms/form-26.yaml"}.
    #: Deliberately loose here: the form engine validates its own schema, and
    #: duplicating that check in two places is how the two drift apart.
    output: dict = Field(default_factory=dict)
    #: Document kinds it may read, as the intake classifier labels them.
    sources: list[str] = Field(default_factory=list)
    #: Tools it may hold, which must be a subset of the service line's.
    tools: list[str] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)
    review: Review = Field(default_factory=Review)

    @property
    def runnable(self) -> bool:
        return self.status == "approved"

    def problems(self, line: "ServiceLineManifest") -> list[str]:
        """Why this skill cannot be loaded, or [] when it is fine.

        Takes the service line because most of what can be wrong with a skill
        is only wrong *relative to its line* — a tool it was never granted, a
        lock it tried to undo.
        """
        found: list[str] = []
        if not _ID_OK.match(self.id) or len(self.id) > ID_MAX:
            found.append(
                f"id {self.id!r} may use only lowercase letters, numbers and "
                f"underscores, up to {ID_MAX} characters"
            )
        for text, limit, what in (
            (self.name, NAME_MAX, "name"),
            (self.description, DESCRIPTION_MAX, "description"),
        ):
            if why := _unusable(text, limit=limit, what=what):
                found.append(why)
        if not self.prompt.strip():
            found.append("no prompt file named")

        # The two-layer rule. Stated as two separate failures because they are
        # different mistakes: asking for a forbidden tool is a misunderstanding
        # of what a service line is, asking for one the line did not grant is
        # usually a missing line in the line's own manifest.
        if withheld := sorted(set(self.tools) & WITHHELD_TOOLS):
            found.append(
                f"asks for {', '.join(withheld)}, which no service line may hold"
            )
        if beyond := sorted(set(self.tools) - set(line.tools) - WITHHELD_TOOLS):
            found.append(
                f"asks for {', '.join(beyond)}, which {line.id} does not allow"
            )

        # A skill may confirm the locks and never loosen them.
        if not self.evidence.require_source:
            found.append("cannot switch off require_source")
        if self.evidence.computed_fields != "engine":
            found.append("cannot hand computed fields to the model")
        if not self.review.signoff:
            found.append("cannot switch off human sign-off")
        return found


class ServiceLineManifest(BaseModel):
    """One service line: who owns it, how its data is handled, what it offers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    description: str
    #: Path to the lead agent's briefing, relative to this folder. The lead
    #: answers questions about the practice and runs no skill itself.
    lead_prompt: str
    #: The role that administers this line. One service line, one owner —
    #: nobody administers two by accident.
    owner: str = ""
    governance: Governance = Field(default_factory=Governance)
    #: Every tool any skill here may ask for. The ceiling, not the grant.
    tools: list[str] = Field(default_factory=list)
    skills: list[SkillManifest] = Field(default_factory=list)

    @property
    def unconfigured(self) -> bool:
        """True while the four governance questions are unanswered.

        A line in this state is listed, is openable by its owner in admin, and
        refuses to let an engagement open. That is the Sustainability case: a
        service line exists from the moment somebody adds the folder, and
        existing is not the same as being ready for client data.
        """
        return not self.governance.configured

    @property
    def runnable_skills(self) -> list[SkillManifest]:
        return [s for s in self.skills if s.runnable]

    def skill(self, skill_id: str) -> SkillManifest | None:
        return next((s for s in self.skills if s.id == skill_id), None)

    def problems(self) -> list[str]:
        """Everything wrong with this manifest, or [] when it loads cleanly.

        Governance being unanswered is NOT a problem here — it is a state the
        line is allowed to be in, and `unconfigured` reports it. This method is
        about manifests that are malformed, which is a different thing from
        manifests that are merely unfinished.
        """
        found: list[str] = []
        if not _ID_OK.match(self.id) or len(self.id) > ID_MAX:
            found.append(
                f"id {self.id!r} may use only lowercase letters, numbers and "
                f"underscores, up to {ID_MAX} characters"
            )
        for text, limit, what in (
            (self.name, NAME_MAX, "name"),
            (self.description, DESCRIPTION_MAX, "description"),
        ):
            if why := _unusable(text, limit=limit, what=what):
                found.append(why)
        if not self.lead_prompt.strip():
            found.append("no lead_prompt file named")

        if withheld := sorted(set(self.tools) & WITHHELD_TOOLS):
            found.append(
                f"allows {', '.join(withheld)}, which no service line may hold"
            )
        if len(self.skills) > MAX_SKILLS:
            found.append(f"has more than {MAX_SKILLS} skills")

        seen: set[str] = set()
        for skill in self.skills:
            if skill.id in seen:
                found.append(f"two skills share the id {skill.id!r}")
            seen.add(skill.id)
            found += [f"skill {skill.id!r}: {why}" for why in skill.problems(self)]
        return found
