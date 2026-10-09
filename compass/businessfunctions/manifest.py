"""What a business function and its features are, as data.

A business function is a part of the firm — Finance, Talent — not a part of
the market. It is a folder with a `manifest.yaml` in it, and it holds
features: Form 26 under Finance, the Leave Management System under Talent.

THE THING THAT IS NOT LIKE A PROMPT. A feature is an application. Form 26 has
an entity and a period, a summary, three tabs, a table of credit lines that
expand, and operations that change their status. None of that is expressible
as text, so a feature does not carry a prompt — it names a `handler`, which is
registered code, and the manifest carries everything *around* it: where it
appears, what it is called, how it is scoped, which operations exist and what
the assistant is allowed to offer.

That split is the whole design:

  the manifest owns the WORDS and the SHAPE   id, name, blurb, scope, the
                                              operations, the sentence each
                                              rule says
  the handler owns the DATA and the WHEN      what the figures are, which
                                              rows exist, whether a rule
                                              applies right now

A rule only speaks when it applies, so the manifest holds the sentence and the
handler decides whether it is said. Writing the sentence in the manifest means
somebody who is not a developer can change what the firm tells its own people.

A WORD ON TRUST, as compass/common/skills.py puts it: this text reaches a
system prompt. Markup in a name is refused, lengths are capped, and a
malformed manifest is skipped with a warning rather than loaded in part.

Describing only. Finding manifests and resolving handlers is registry.py.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_ID_OK = re.compile(r"^[a-z0-9_]+$")
ID_MAX = 48
NAME_MAX = 64
TEXT_MAX = 1024

#: Anything that could be read as markup, refused in text that reaches a prompt.
_MARKUP = re.compile(r"<[^>]*>")

#: How a feature is narrowed before it shows anything. Finance's Form 26 is
#: meaningless without an entity and a period; Talent's LMS is scoped to the
#: people who report to you. These are the dimensions a handler may be given,
#: and a manifest naming one that does not exist is a manifest that would ask
#: for a filter nobody can apply.
#: A ceiling on how many values one scope selector may offer. Past this it is
#: a search box, not a dropdown, and this is a manifest rather than a store.
MAX_CHOICES = 40

SCOPES = frozenset({
    "entity",   # a legal entity — Contoso India, Northwind Services
    "period",   # a reporting period — Q2 FY25
    "team",     # the people who report to the signed-in person
    "self",     # the signed-in person's own records
})

#: Tools no business-function agent may hold, whatever a manifest says. These
#: are the names from compass/code/tools/registry.py. The assistant here reads
#: what is on one screen and operates it; a shell, a filesystem and the open
#: web are all ways out of that, so the denial lives above every manifest
#: rather than being something each one remembers to switch off.
WITHHELD_TOOLS = frozenset({
    "bash", "bash_output",
    "file_read", "file_write", "file_edit", "glob", "grep",
    "browser", "screenshot",
    "web_fetch",
    "agent",
})

MAX_FEATURES = 16
MAX_STARTERS = 6


def _unusable(text: str, *, limit: int, what: str) -> str:
    """Why this prompt-bound text is unusable, or "" when it is fine."""
    if not text.strip():
        return f"no {what}"
    if len(text) > limit:
        return f"{what} is longer than {limit} characters"
    if _MARKUP.search(text):
        return f"{what} contains markup"
    return ""


class Action(BaseModel):
    """One operation a feature offers, and that the assistant may propose.

    The assistant never performs one of these directly. It builds a plan, name
    the rows it would touch, and waits — so `confirm` is the sentence a person
    reads before deciding, and it is written here rather than generated,
    because a generated description of an irreversible act is exactly the
    thing that should not be generated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    #: What the button says. "Accept the lower credit", not "accept".
    label: str
    #: What happens, in a sentence, shown in the plan before anybody commits.
    confirm: str
    #: Whether the person can put it back. Drives whether Undo is offered, and
    #: how loudly the plan warns.
    reversible: bool = True
    #: True when it reaches past Compass — notifies somebody, books days,
    #: sends a query to a deductor. These are confirmed one at a time and are
    #: never included in a bulk proposal the person did not name.
    outward: bool = False

    def problems(self) -> list[str]:
        found: list[str] = []
        if not _ID_OK.match(self.id) or len(self.id) > ID_MAX:
            found.append(f"action id {self.id!r} is not a usable identifier")
        for text, limit, what in ((self.label, NAME_MAX, "label"),
                                  (self.confirm, TEXT_MAX, "confirm text")):
            if why := _unusable(text, limit=limit, what=what):
                found.append(f"action {self.id!r}: {why}")
        return found


class Rule(BaseModel):
    """Something true about the current state that the person must be told.

    "You prepared this, so a second reviewer has to sign it off." "FY24 was
    signed on 12 April — nothing here can be changed." "You see your team, not
    the firm."

    The sentence lives here. Whether it applies right now is the handler's
    decision, because it depends on who is asking and what state the data is
    in. A rule that is always on screen is wallpaper and stops being read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    #: The short bold lead. "Read-only."
    headline: str
    #: The explanation after it. Why, not just what.
    detail: str
    #: "notice" explains; "limit" says something cannot be done.
    tone: Literal["notice", "limit"] = "notice"

    def problems(self) -> list[str]:
        found: list[str] = []
        if not _ID_OK.match(self.id) or len(self.id) > ID_MAX:
            found.append(f"rule id {self.id!r} is not a usable identifier")
        for text, limit, what in ((self.headline, NAME_MAX, "headline"),
                                  (self.detail, TEXT_MAX, "detail")):
            if why := _unusable(text, limit=limit, what=what):
                found.append(f"rule {self.id!r}: {why}")
        return found


class FeatureManifest(BaseModel):
    """One application inside a business function."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    #: The line under the name in the sidebar. "Reconciliation · tax credits".
    short: str
    #: The paragraph on the launcher card.
    blurb: str
    #: Registered code that supplies the data and decides which rules apply.
    #: A key into the handler registry, not an import path: a manifest naming
    #: a dotted path would be a manifest that can import arbitrary modules.
    handler: str
    #: Which dimensions narrow it before it shows anything.
    scope: list[str] = Field(default_factory=list)
    #: What each scope dimension may be set to — {"period": ["FY 2026-27", …]}.
    #: Here rather than in the frontend because the values are a claim about
    #: the firm, not about the screen: Form 26 is reconciled per quarter and a
    #: gift limit is annual, and a UI holding one list for both would offer
    #: RewardLens a quarter it cannot total. Empty means the deployment has
    #: not said, and the selector then has nothing to offer — which is
    #: visible, where a wrong default would not be.
    choices: dict[str, list[str]] = Field(default_factory=dict)
    #: The sentence shown while a scope is still unchosen. Says why the screen
    #: is empty in this feature's own terms.
    scope_why: str = ""
    actions: list[Action] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    #: What the assistant offers when this feature is open. Suggestions, not
    #: capabilities — anything offered here must map to an action or be a
    #: question the handler can answer.
    starters: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    def problems(self, fn: "FunctionManifest") -> list[str]:
        """Why this feature cannot load, or [] when it is fine."""
        found: list[str] = []
        if not _ID_OK.match(self.id) or len(self.id) > ID_MAX:
            found.append(f"id {self.id!r} is not a usable identifier")
        for text, limit, what in ((self.name, NAME_MAX, "name"),
                                  (self.short, NAME_MAX, "short"),
                                  (self.blurb, TEXT_MAX, "blurb")):
            if why := _unusable(text, limit=limit, what=what):
                found.append(why)
        if not self.handler.strip():
            found.append("names no handler")

        if unknown := sorted(set(self.scope) - SCOPES):
            found.append(
                f"is scoped by {', '.join(unknown)}, which is not a scope "
                f"Compass can apply"
            )

        # Choices for a dimension this feature is not scoped by would never be
        # shown, and are far more likely to be a typo in the dimension's name
        # than a deliberate spare list.
        if stray := sorted(set(self.choices) - set(self.scope)):
            found.append(
                f"offers choices for {', '.join(stray)}, which it is not "
                f"scoped by"
            )
        for dim, values in self.choices.items():
            if len(values) > MAX_CHOICES:
                found.append(f"offers more than {MAX_CHOICES} {dim} choices")
            if len(set(values)) != len(values):
                found.append(f"repeats a {dim} choice")
            for value in values:
                if why := _unusable(value, limit=NAME_MAX, what=f"{dim} choice"):
                    found.append(why)
        if self.scope_why:
            if why := _unusable(self.scope_why, limit=TEXT_MAX, what="scope_why"):
                found.append(why)

        # The two-layer rule: a feature narrows its function's grant, never
        # widens it. Stated as two failures because they are different
        # mistakes — one misunderstands what a business function is, the other
        # is usually a missing line in the function's own manifest.
        if withheld := sorted(set(self.tools) & WITHHELD_TOOLS):
            found.append(
                f"asks for {', '.join(withheld)}, which no business function "
                f"may hold"
            )
        if beyond := sorted(set(self.tools) - set(fn.tools) - WITHHELD_TOOLS):
            found.append(f"asks for {', '.join(beyond)}, which {fn.id} does not allow")

        if len(self.starters) > MAX_STARTERS:
            found.append(f"offers more than {MAX_STARTERS} starters")
        for text in self.starters:
            if why := _unusable(text, limit=TEXT_MAX, what="starter"):
                found.append(why)

        seen: set[str] = set()
        for action in self.actions:
            if action.id in seen:
                found.append(f"two actions share the id {action.id!r}")
            seen.add(action.id)
            found += action.problems()

        seen = set()
        for rule in self.rules:
            if rule.id in seen:
                found.append(f"two rules share the id {rule.id!r}")
            seen.add(rule.id)
            found += rule.problems()
        return found


class FunctionManifest(BaseModel):
    """One part of the firm, and the features it offers its own people."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    #: The line under the name in the switcher. "Controllership & Reporting".
    subtitle: str
    #: The paragraph at the top of the overview.
    blurb: str
    #: The role that administers this function. One function, one owner.
    owner: str = ""
    #: What the assistant offers at overview level, before a feature is open.
    starters: list[str] = Field(default_factory=list)
    #: Every tool any feature here may ask for. The ceiling, not the grant.
    tools: list[str] = Field(default_factory=list)
    features: list[FeatureManifest] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        """True for a function nobody has added a feature to yet.

        It is listed and it opens; there is simply nothing in it. A function
        exists from the moment somebody adds the folder, and the overview says
        so rather than looking broken.
        """
        return not self.features

    def feature(self, feature_id: str) -> FeatureManifest | None:
        return next((f for f in self.features if f.id == feature_id), None)

    def problems(self) -> list[str]:
        found: list[str] = []
        if not _ID_OK.match(self.id) or len(self.id) > ID_MAX:
            found.append(f"id {self.id!r} is not a usable identifier")
        for text, limit, what in ((self.name, NAME_MAX, "name"),
                                  (self.subtitle, NAME_MAX, "subtitle"),
                                  (self.blurb, TEXT_MAX, "blurb")):
            if why := _unusable(text, limit=limit, what=what):
                found.append(why)

        if withheld := sorted(set(self.tools) & WITHHELD_TOOLS):
            found.append(
                f"allows {', '.join(withheld)}, which no business function may hold"
            )
        if len(self.features) > MAX_FEATURES:
            found.append(f"has more than {MAX_FEATURES} features")
        if len(self.starters) > MAX_STARTERS:
            found.append(f"offers more than {MAX_STARTERS} starters")
        for text in self.starters:
            if why := _unusable(text, limit=TEXT_MAX, what="starter"):
                found.append(why)

        seen: set[str] = set()
        for feature in self.features:
            if feature.id in seen:
                found.append(f"two features share the id {feature.id!r}")
            seen.add(feature.id)
            found += [f"feature {feature.id!r}: {why}"
                      for why in feature.problems(self)]
        return found
