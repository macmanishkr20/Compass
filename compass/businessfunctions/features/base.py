"""The contract a feature's code satisfies, and the register it lives in.

A manifest names a handler by key — `form26`, `lms` — and never by import
path. That is deliberate: a dotted path in a YAML file is a YAML file that can
import any module on the box, and a manifest is data, read from a folder,
edited by people who are not reviewing it as code.

So handlers register themselves, the key is looked up in a dict, and a
manifest naming a key nobody registered does not load.

WHAT A HANDLER IS FOR. The manifest holds the words and the shape; the handler
holds the data and the judgement:

    figures()   the line of numbers across the top of the workbench
    tabs()      the views, and what each one currently counts
    rows()      the table under the selected tab
    rules()     which of the manifest's rules apply *right now*
    act()       perform one action against named rows, and say what happened
    preview()   say what a filled-in form would do, without doing it
    submit()    record it

`rules()` is the interesting one. The manifest writes "Read-only. FY24 was
signed on 12 April"; only the handler knows the period is closed and who
prepared it. A rule that is always on screen stops being read, so the handler
returns the ids of the ones that are true at this moment and no others.

`act()` is the only method that changes anything, and nothing calls it until a
person has seen the plan and confirmed it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True)
class Scope:
    """What the person has narrowed the feature to.

    The dimensions a feature declares in its manifest arrive here. `entity`
    and `period` are chosen from the selectors; `user` is who is asking, and
    it is never chosen — it comes from the request, so a feature scoped to
    "team" cannot be talked into showing somebody else's.
    """

    user: str
    entity: str = ""
    period: str = ""

    def missing(self, wanted: list[str]) -> list[str]:
        """Which declared dimensions have not been chosen yet."""
        have = {"entity": self.entity, "period": self.period,
                "team": self.user, "self": self.user}
        return [d for d in wanted if not have.get(d, "")]


@dataclass(frozen=True)
class Figure:
    """One number in the strip across the top. A value and what it means."""

    key: str
    value: str
    caption: str = ""
    #: "plain", "good", "warn", "bad" — the reading, not the colour. The
    #: surface decides how to show it; the handler decides what it means.
    tone: str = "plain"


@dataclass(frozen=True)
class Tab:
    """One view of the same data, and how many rows are in it."""

    key: str
    label: str
    count: int | None = None
    #: Which field identifies a row on THIS tab, when it is not the feature's
    #: usual one. Leave's queue lists requests keyed by id and its balances
    #: list leave types keyed by kind — the same feature, two kinds of thing,
    #: so the key cannot only be a property of the feature.
    key_field: str = ""


@dataclass(frozen=True)
class Outcome:
    """What `act` did, in terms a person and the assistant can both use."""

    ok: bool
    #: One sentence, past tense. "Line 0044 marked accepted. 2 left."
    said: str
    #: The rows that changed, so a surface can repaint just those.
    touched: list[str] = field(default_factory=list)


class Feature(ABC):
    """One application inside a business function."""

    #: The key a manifest names. Unique across every business function,
    #: because the register is flat — two functions wanting the same feature
    #: share the handler rather than each registering their own.
    key: ClassVar[str]

    #: Which field in a row identifies it — the value `act()` will be handed
    #: back as a target. Declared by the handler because the handler is what
    #: knows: a reconciliation is keyed by credit line, an approval queue by
    #: request, and RewardLens by the person. The UI asked by trying a list of
    #: field names until one matched, which worked until a feature arrived
    #: whose rows had none of them, and then every row quietly shared the
    #: empty id.
    row_key: ClassVar[str] = "id"

    @abstractmethod
    def figures(self, scope: Scope) -> list[Figure]:
        ...

    @abstractmethod
    def tabs(self, scope: Scope) -> list[Tab]:
        ...

    @abstractmethod
    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        ...

    def rules(self, scope: Scope) -> list[str]:
        """Ids of the manifest rules that are true right now. None by default."""
        return []

    def act(self, scope: Scope, action: str, targets: list[str],
            note: str = "") -> Outcome:
        """Perform one action. Refusing is a normal outcome, not an error.

        `note` is the reason the person gave, for actions whose manifest asks
        for one. It is theirs — nothing here writes it, suggests it or fills
        it in, because its only value is that a person wrote it.
        """
        return Outcome(ok=False, said=f"{self.key} cannot {action} yet.")

    # ── creating a row, rather than deciding one ────────────────────────────
    #
    # `act` operates on rows that already exist, so everything it needs in
    # order to refuse is in front of it. A form arrives with values that came
    # from a person and from nothing else, which is a different problem: there
    # is no row to check, the handler validates every value itself, and the
    # person sees what their entry would do before it is recorded.

    def accepts(self, form_id: str) -> set[str]:
        """Field ids this handler understands for that form.

        Declared so the catalog can refuse a manifest that disagrees with the
        code. A field somebody fills in that the handler ignores is worse than
        a missing field: the person believes they declared it.
        """
        return set()

    def preview(self, scope: Scope, form_id: str,
                values: dict[str, str]) -> Outcome:
        """Say what recording this would do. Changes nothing."""
        return Outcome(ok=False, said=f"{self.key} has no form called {form_id!r}.")

    def submit(self, scope: Scope, form_id: str,
               values: dict[str, str]) -> Outcome:
        """Record it. Validates again rather than trusting the preview."""
        return Outcome(ok=False, said=f"{self.key} has no form called {form_id!r}.")


#: handler key -> instance. Flat and module-level: handlers are stateless
#: readers of a store, so one instance serves every request, and there is
#: nothing here that belongs to an event loop.
_handlers: dict[str, Feature] = {}


def register(handler: Feature) -> Feature:
    """Add a handler to the register. Called at import time by each feature."""
    if handler.key in _handlers:
        raise ValueError(f"two features registered as {handler.key!r}")
    _handlers[handler.key] = handler
    return handler


def get(key: str) -> Feature | None:
    return _handlers.get(key)


def keys() -> list[str]:
    return sorted(_handlers)
