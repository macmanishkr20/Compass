"""The estimate record, and where it is kept.

One dataclass and one store, following the pattern `pipelines/store.py` set: a
JSON file per record under the data directory, an atomic write, and a Cosmos
variant slotting in behind the same methods when there is one.

The record is a thin envelope — id, owner, times — around two payloads: the
`brief` that was submitted and the `result` that came back. They are stored as
plain dicts rather than as the Pydantic models, for a reason worth stating: an
estimate is a *dated artefact*. It was computed against a rate card, a price
list and a set of catalog assumptions as they stood that morning. Re-validating
a year-old record through today's models would quietly re-interpret it, and a
field this version has dropped would take a stored figure with it. Read back as
data, an old estimate stays exactly the answer that was given — which is the
only version of it worth keeping, since the reason to keep one at all is that
somebody made a decision on it.

Keeping the brief beside the result is what makes re-running possible: ask the
same question against a newer rate card and compare. That is not built yet, but
storing only the answer would have made it unbuildable without asking everyone
to fill the form in again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from compass.common.config import get_settings

logger = logging.getLogger("compass.estimate")


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"est_{uuid.uuid4().hex[:12]}"


@dataclass
class Estimate:
    """One estimate, as stored."""

    id: str
    #: What was asked. A `ProjectInput`, dumped.
    brief: dict[str, Any] = field(default_factory=dict)
    #: What came back. An `Estimation`, dumped. Empty while one is running.
    result: dict[str, Any] = field(default_factory=dict)
    #: Denormalised for the list view, so showing a portfolio of fifty does not
    #: mean loading fifty full reports to read four fields off each.
    name: str = ""
    project_type: str = "new"
    industry_domain: str = ""
    score: int = 0
    total_expected: float = 0.0
    currency: str = "USD"
    verdict: str = ""
    #: "high" | "medium" | "low" — the index shows it as a column, and loading
    #: fifty full reports to read one word off each is not a list view.
    confidence: str = ""
    status: str = "complete"  # complete | failed
    #: Who this estimate belongs to. Empty is legacy and stays visible; see
    #: `compass.common.ownership`.
    owner: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        """The row a list view needs — everything but the two heavy payloads."""
        d = self.to_dict()
        d.pop("brief", None)
        d.pop("result", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Estimate":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


class _JsonStore:
    """One JSON file per record, under the data directory.

    The same zero-config default the rest of Compass uses when Cosmos is not
    configured.
    """

    def __init__(self, folder: str) -> None:
        self._folder = folder

    @property
    def _dir(self) -> Path:
        settings = get_settings()
        path = settings.workspace_root / settings.data_dir / self._folder
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _path(self, record_id: str) -> Path:
        # Guard the id: it reaches the filesystem, and a record id that walks
        # out of the directory is the kind of thing that is only funny once.
        safe = "".join(c for c in record_id if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("invalid id")
        return self._dir / f"{safe}.json"

    def _write(self, record_id: str, payload: dict[str, Any]) -> None:
        path = self._path(record_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic, so a crash mid-write cannot truncate

    def _read(self, record_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(record_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _all(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                logger.warning("skipping unreadable record %s", path)
        return out

    def _delete(self, record_id: str) -> bool:
        try:
            self._path(record_id).unlink()
            return True
        except (OSError, ValueError):
            return False


class EstimateStore(_JsonStore):
    def __init__(self) -> None:
        super().__init__("estimates")

    async def create(self, **kwargs: Any) -> Estimate:
        record = Estimate(id=_new_id(), **kwargs)
        self._write(record.id, record.to_dict())
        return record

    async def get(self, estimate_id: str) -> Estimate | None:
        raw = self._read(estimate_id)
        return Estimate.from_dict(raw) if raw else None

    async def list(self) -> list[Estimate]:
        return [Estimate.from_dict(r) for r in self._all()]

    async def save(self, record: Estimate) -> Estimate:
        record.updated_at = _now()
        self._write(record.id, record.to_dict())
        return record

    async def delete(self, estimate_id: str) -> bool:
        return self._delete(estimate_id)


#: The platform baselines, in one place. `engine.py` reads the same numbers
#: from `catalog.py`; these are what the API hands a client that has never
#: saved a card, so the form opens showing what would actually be used.
BASELINE_RATES: dict[str, float] = {
    "dev_hourly_rate": 115,
    "maint_hourly_rate": 95,
    "effective_hours_per_week": 32,
    "delivery_overhead": 1.0,
    "loaded_hourly_rate": 75,
    "automation_rate_percent": 35,
    "benefit_ramp_months": 6,
}


class RateCardStore(_JsonStore):
    """One saved rate card per person.

    Per person rather than per workspace, and that is a decision rather than a
    default. A rate card is genuinely an organisation's fact — what it pays its
    own people — so a shared one is the right idea. But Compass's ownership
    model separates users without isolating them and has no notion of an admin,
    so a shared card would be a document any user could silently rewrite for
    everyone else, and the failure mode is every future estimate quietly
    costed at somebody else's rates. Per person is the version that cannot go
    wrong; a shared card wants a role to own it first.

    The id is a digest of the owner rather than the owner itself: an owner is
    an email address, the id reaches the filesystem, and stripping the
    punctuation out of `a.b@x.com` and `ab@x.com` gives the same name.
    """

    def __init__(self) -> None:
        super().__init__("estimate_rates")

    @staticmethod
    def _key(owner: str) -> str:
        return hashlib.sha256((owner or "_shared").encode("utf-8")).hexdigest()[:20]

    async def get(self, owner: str) -> dict[str, float]:
        saved = self._read(self._key(owner)) or {}
        # Merged over the baseline, so a card written before a dial existed
        # still opens with every field populated instead of a blank input.
        return {**BASELINE_RATES, **{k: v for k, v in saved.items() if k in BASELINE_RATES}}

    async def save(self, owner: str, card: dict[str, float]) -> dict[str, float]:
        merged = {**BASELINE_RATES, **{k: float(v) for k, v in card.items() if k in BASELINE_RATES}}
        self._write(self._key(owner), merged)
        return merged


estimates = EstimateStore()
rate_cards = RateCardStore()
