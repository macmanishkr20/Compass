"""P&L: the monthly consolidation, and everything that is checked against it.

What Finance does today in Excel, in the order they do it:

    intake        the FR files for one month, per location and service line
    enrichment    Reporting Segment attributes, Plan and Forecast values, and
                  the segments Plan knows about that FR left out
    consolidation unique lines across locations, by name and never by position
    YTD and MTD   MTD = this month's YTD − last month's YTD
    reconciliation against the separate file Finance maintains
    exceptions    only the movements that breach Finance's own rules
    output        chosen sheets, values only, with the run's trail kept here

IT CALCULATES, COMPARES, FLAGS AND REPORTS. IT NEVER ALTERS A FIGURE. There is
no function in this module that writes to a value somebody uploaded. Correcting
one means uploading a new file, which supersedes the old one and leaves it
where it was, because "what did the February run use" has to stay answerable.

THE RULES ARE FINANCE'S, AND NOT ONE OF THEM IS FINAL. The requirement says
illustrative values are not production defaults until Finance confirms them,
and lists the thresholds, the Direct Cost definition and the FTE calculation as
open. So they are versioned data with a `confirmed` flag rather than constants,
and the screens say plainly when they are running on the shipped guesses.

NOTHING IS GUESSED TO KEEP A NUMBER ON THE SCREEN. A plan nobody set is no plan,
not a plan of zero. An MTD with no prior month is blank, not the whole YTD. FTE
has no agreed monthly calculation, so it shows none until Finance sets one. A
check that could not run is listed as untested rather than counted as a pass.

Synchronous in memory and flushed by the route, the bargain `ledger` explains.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import logging
import re
import time
import uuid
from typing import Any

from compass.businessfunctions import notices, people
from compass.businessfunctions.pnl_files import (
    SPECS,
    Parsed,
    month_key,
    month_label,
    norm,
    parse_month,
)
from compass.common.config import get_settings
from compass.common.persistence.catalog import Collection

logger = logging.getLogger("compass.businessfunctions")

#: Who is expected to send a file each month, and the words on the selectors.
#: Illustrative, like the rules: replace with Finance's own list. The manifest
#: repeats them because a form's options live there; a check keeps them equal.
LOCATIONS = ("Bengaluru", "Hyderabad", "Kochi", "Noida")
SERVICE_LINES = ("Assurance", "Consulting", "Tax")
#: The selector value meaning every location together.
ALL = "Pan-GDS"

#: What leadership's file may be made of. The backend workings are not on this
#: list, which is how they stay out of the file.
SHEETS = ("Pan-GDS summary", "Location summaries", "Service line summary",
          "Reporting Segment analysis", "Exception report", "Reconciliation")

#: Every comparison and validation an exception can come from, in report order.
CHECKS = {
    "plan": "Actual vs Plan",
    "prior_year": "Current Year vs Prior Year",
    "forecast": "Actual vs Forecast",
    "revenue": "Revenue validation",
    "variable_pay": "Variable Pay",
    "benefits": "Employee Benefits",
    "direct_cost": "Direct Cost",
}

#: One stored chunk of rows. A Cosmos item stops at 2MB and a row is about 200
#: bytes as an array, so this leaves room.
CHUNK = 2000

_sources_store = Collection("bf_pnl_source", "business_function_pnl_sources.json",
                            shape="map")
_chunks_store = Collection("bf_pnl_chunk", "business_function_pnl_chunks", shape="dir")
_records_store = Collection("bf_pnl_record", "business_function_pnl_records.json",
                            shape="map")

#: Version 1 of the rules. EVERY NUMBER HERE IS ILLUSTRATIVE, which `confirmed`
#: says out loud until somebody in Finance sets a version of their own.
SHIPPED_RULES: dict[str, Any] = {
    "version": 1,
    "confirmed": False,
    "at": 0.0,
    "by": "shipped with Compass",
    "why": "Illustrative values, until Finance confirms its own.",
    "presets": [5, 10, 15, 20],
    "plan_pct": 10.0,
    "py_pct": 10.0,
    "forecast_pct": 10.0,
    # A percentage alone flags ₹500 against a plan of ₹100. Applied to money
    # lines only: an FTE movement of three is not small because three < 1,00,000.
    "floor": 100_000.0,
    "vp_pct": 20.0,
    "eb_pct": 12.0,
    "eb_by_location": {},
    "dc_revenue_pct": 15.0,
    "dc_per_fte": 200_000.0,
    "revenue_tolerance_pct": 2.0,
    "recon_abs": 1_000.0,
    "recon_pct": 0.5,
    "reminder_day": 5,
    # The firm's year runs April to March, as RewardLens's does. Setting this
    # wrong makes the first month's MTD a subtraction it should not be.
    "fy_first_month": 4,
    # What the validations call each line. Matched on the whole name, so that
    # "Other revenue" is never read as Revenue.
    "key_lines": {
        "revenue": ["revenue", "net revenue", "total revenue"],
        "base": ["base compensation", "base comp", "base pay"],
        "variable_pay": ["variable pay", "bonus"],
        "benefits": ["employee benefits", "benefits"],
        "direct_cost": ["direct cost", "direct costs", "total direct cost"],
        "fte": ["fte", "average fte", "fte count"],
    },
    # Finance's answers about individual lines: {name: {"class", "mtd"}}.
    "line_rules": {},
}

#: Which rule fields a form may change. Anything else is a key nobody reads.
RULE_FIELDS = frozenset({
    "plan_pct", "py_pct", "forecast_pct", "floor", "vp_pct", "eb_pct",
    "eb_by_location", "dc_revenue_pct", "dc_per_fte", "revenue_tolerance_pct",
    "recon_abs", "recon_pct", "line_rules", "fy_first_month", "reminder_day",
})

#: How a line behaves. A `flow` adds up across segments and across months; a
#: `stock` adds across segments only; a `ratio` adds across neither.
_RATIO = re.compile(r"%|\bpercent|\butili[sz]ation\b|\bper fte\b|\brate\b|\brecovery\b")
_STOCK = re.compile(r"\bfte\b|\bhead ?count\b|\bhc\b|\bspan\b")

_sources: list[dict[str, Any]] = []
_records: dict[str, dict[str, Any]] = {}
_unsaved_sources: list[dict[str, Any]] = []
_unsaved_records: list[str] = []
#: Bytes waiting to be written, and any still only held here.
_files: dict[str, bytes] = {}
_unsaved_files: list[str] = []
_generation = 0
_cache: dict[tuple, Any] = {}
_loaded = False
#: True while what is held is the sample rather than anybody's real month.
_sample = False
#: What the store holds, set aside while the sample is on screen.
_stored_records: dict[str, dict[str, Any]] = {}


def _touch() -> None:
    """Something changed, so nothing already worked out may be believed."""
    global _generation
    _generation += 1
    _cache.clear()


def _cached(key: tuple, build):
    if key not in _cache:
        _cache[key] = build()
    return _cache[key]


def is_sample() -> bool:
    return _sample


def amount(value: float | None) -> str:
    """A figure as a sentence says it, in Indian grouping and with no ₹.

    No currency sign, because not every line is money: Charged Hours and FTE
    sit in the same columns.
    """
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    whole, _, part = f"{abs(value):.2f}".partition(".")
    tail, head = whole[-3:], whole[:-3]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    body = ",".join([*groups, tail])
    return f"{sign}{body}" + (f".{part}" if part != "00" else "")


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


# ──────────────────────────────────────────────────────────────────────────
# the rules
# ──────────────────────────────────────────────────────────────────────────

def _rule_versions() -> list[dict[str, Any]]:
    found = [r for r in _records.values() if r.get("type") == "rules"]
    return [SHIPPED_RULES, *sorted(found, key=lambda r: r["version"])]


def rules() -> dict[str, Any]:
    """What everything is judged against right now."""
    return _rule_versions()[-1]


def rule_history() -> list[dict[str, Any]]:
    return _rule_versions()


def set_rules(*, by: str, why: str, **changes: Any) -> dict[str, Any]:
    """Add a version. An old one is never rewritten, as with the gift thresholds."""
    if not why.strip():
        raise ValueError("A change to the rules has to say why.")
    if unknown := set(changes) - RULE_FIELDS:
        raise ValueError(f"No such rule: {', '.join(sorted(unknown))}.")
    was = rules()
    if all(was.get(k) == v for k, v in changes.items()):
        raise ValueError("That is what the rules already say.")
    fresh = dict(was, **changes)
    fresh.update(version=was["version"] + 1, confirmed=True, at=time.time(),
                 by=by, why=why.strip(), type="rules",
                 id=f"rules-{was['version'] + 1}")
    _put_record(fresh)
    log("rules_set", by, f"Rules v{fresh['version']}: {why.strip()}")
    return fresh


def line_class(name: str) -> str:
    """Whether this line adds up, and over what. Finance's word overrides."""
    key = norm(name)
    said = rules()["line_rules"].get(key, {})
    if said.get("class"):
        return said["class"]
    lowered = name.lower()
    if _RATIO.search(lowered):
        return "ratio"
    if _STOCK.search(lowered):
        return "stock"
    return "flow"


def mtd_rule(name: str) -> str:
    """How this line's month is worked out.

    `subtract` is the requirement's generic rule. It does not hold for a stock
    or a ratio, and the document says Finance must define those, so their
    default is `pending`, which shows nothing rather than a confident wrong
    figure. `closing` means the month's value is simply this month's value.
    """
    said = rules()["line_rules"].get(norm(name), {})
    if said.get("mtd"):
        return said["mtd"]
    return "subtract" if line_class(name) == "flow" else "pending"


def fy_start(month: int) -> int:
    """The first month of the financial year this month falls in."""
    first = rules()["fy_first_month"] - 1
    return month - ((month % 12 - first) % 12)


def month_of(period: str) -> int | None:
    return parse_month(period)


def months() -> list[int]:
    """Every month anybody has sent an FR file for, oldest first."""
    return sorted({m for s in _sources if s["kind"] == "fr" for m in s["months"]})


def latest_month() -> int | None:
    held = months()
    return held[-1] if held else None


# ──────────────────────────────────────────────────────────────────────────
# the sources, and which one counts
# ──────────────────────────────────────────────────────────────────────────

def sources(kind: str = "") -> list[dict[str, Any]]:
    """Every upload, oldest first, replaced ones included."""
    return [s for s in _sources if not kind or s["kind"] == kind]


def fr_source(month: int, location: str, service_line: str) -> dict[str, Any] | None:
    """The file that counts for one slot: the newest."""
    found = [s for s in _sources if s["kind"] == "fr" and month in s["months"]
             and s["location"] == location and s["service_line"] == service_line]
    return found[-1] if found else None


def active(kind: str, month: int | None = None) -> dict[str, Any] | None:
    """The newest file of this kind that speaks for this month.

    Per month rather than per file, because a Plan file can name twelve
    months and a later one only three: the three are replaced and the rest of
    the first file still stands.
    """
    found = [s for s in _sources if s["kind"] == kind
             and (month is None or month in s["months"] or not s["months"])]
    return found[-1] if found else None


def is_current(source: dict[str, Any]) -> bool:
    """Whether anything still reads this upload."""
    if source["kind"] == "fr":
        return fr_source(source["months"][0], source["location"],
                         source["service_line"]) is source
    if not source["months"]:
        return active(source["kind"]) is source
    return any(active(source["kind"], m) is source for m in source["months"])


def _rows_of(kind: str, month: int) -> list[dict[str, Any]]:
    source = active(kind, month)
    if source is None:
        return []
    return [r for r in source["rows"] if r.get("month", month) == month]


def record_source(kind: str, parsed: Parsed, *, filename: str, raw: bytes, by: str,
                  month: int | None = None, location: str = "",
                  service_line: str = "") -> dict[str, Any]:
    """Keep one file. Replaces nothing: the newest simply counts from now on."""
    _leave_sample()
    sha = hashlib.sha256(raw).hexdigest()
    rows: list[dict[str, Any]] = []
    for row in parsed.rows:
        kept = {k: v for k, v in row.items()
                if k not in ("period", "effective_from") and v not in (None, "")}
        if "period" in row or kind in ("plan", "forecast", "history", "finance", "hours"):
            at = parse_month(row.get("period")) if row.get("period") else month
            if at is None:
                raise ValueError(
                    f"A row in {filename} says its period is "
                    f"{row.get('period')!r}, and no period was chosen to use instead.")
            kept["month"] = at
        if row.get("effective_from"):
            kept["effective_from"] = parse_month(row["effective_from"]) or 0
        rows.append(kept)
    months = sorted({r["month"] for r in rows if "month" in r})
    if kind == "fr":
        months = [month]
    version = 1 + sum(1 for s in _sources if s["kind"] == kind
                      and s["location"] == location
                      and s["service_line"] == service_line
                      and (not months or set(months) & set(s["months"])))
    source = {
        "id": f"{kind}-{uuid.uuid4().hex[:10]}",
        "kind": kind,
        "label": SPECS[kind].label,
        "filename": filename,
        "sha256": sha,
        "months": months,
        "location": location,
        "service_line": service_line,
        "version": version,
        "by": by,
        "at": time.time(),
        "count": len(rows),
        "totals": parsed.totals,
        "skipped": parsed.skipped,
        "owner": "",
        "rows": rows,
    }
    _sources.append(source)
    _unsaved_sources.append(source)
    _files[f"{sha}{_suffix(filename)}"] = raw
    _unsaved_files.append(f"{sha}{_suffix(filename)}")
    _touch()
    log("uploaded", by, f"{SPECS[kind].label} v{version}: {filename} "
                        f"({len(rows)} rows)"
                        + (f", {location} · {service_line}" if location else ""),
        month if kind == "fr" else None)
    return source


def _suffix(filename: str) -> str:
    return ".csv" if filename.lower().endswith(".csv") else ".xlsx"


def same_file(raw: bytes) -> dict[str, Any] | None:
    """A current upload with these exact bytes, or None.

    The same report filed twice under two locations is the mistake this
    finds: nothing else would notice, because every figure in it is valid.
    """
    sha = hashlib.sha256(raw).hexdigest()
    for source in reversed(_sources):
        if source["sha256"] == sha and is_current(source):
            return source
    return None


# ──────────────────────────────────────────────────────────────────────────
# the consolidated month
# ──────────────────────────────────────────────────────────────────────────

def actuals_at(month: int) -> tuple[dict[tuple, float], set[tuple[str, str]], dict[str, str]]:
    """What was actually reported for a month: from the tool, else history.

    The one-time load serves until the tool has run that month itself, slot
    by slot, so a month half-run here and half-loaded still answers.
    """
    def build():
        figures: dict[tuple, float] = {}
        slots: set[tuple[str, str]] = set()
        names: dict[str, str] = {}
        for location in LOCATIONS:
            for line_of_service in SERVICE_LINES:
                source = fr_source(month, location, line_of_service)
                if source is None:
                    continue
                slots.add((location, line_of_service))
                for row in source["rows"]:
                    if row.get("actual") is None:
                        continue
                    key = norm(row["line"])
                    names.setdefault(key, row["line"])
                    names.setdefault(norm(row["rs"]), row["rs"])
                    figures[(location, line_of_service, norm(row["rs"]), key)] = row["actual"]
        for row in _rows_of("history", month):
            slot = (row["location"], row["service_line"])
            if slot in slots and fr_source(month, *slot) is not None:
                continue
            key = norm(row["line"])
            names.setdefault(key, row["line"])
            names.setdefault(norm(row["rs"]), row["rs"])
            figures[(*slot, norm(row["rs"]), key)] = row["actual"]
        slots |= {(r["location"], r["service_line"]) for r in _rows_of("history", month)}
        return figures, slots, names
    return _cached(("actuals", month), build)


def mapping(month: int) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Segment attributes in force for a month, and any segment with two answers.

    A conflict is not resolved here. Finance owns the rule for one-to-many,
    and applying either row would be choosing for them.
    """
    def build():
        source = active("mapping")
        applied: dict[str, dict[str, Any]] = {}
        conflicts: dict[str, str] = {}
        if source is None:
            return applied, conflicts
        rows_by_rs: dict[str, list[dict[str, Any]]] = {}
        for row in source["rows"]:
            if row.get("effective_from", 0) <= month:
                rows_by_rs.setdefault(norm(row["rs"]), []).append(row)
        for rs, rows in rows_by_rs.items():
            newest = max(r.get("effective_from", 0) for r in rows)
            live = [r for r in rows if r.get("effective_from", 0) == newest]
            kinds = {tuple(sorted((k, str(v)) for k, v in r.items()
                                  if k != "rs")) for r in live}
            if len(kinds) > 1:
                conflicts[rs] = live[0]["rs"]
            else:
                applied[rs] = live[0]
        return applied, conflicts
    return _cached(("mapping", month), build)


def view(month: int) -> dict[str, Any]:
    """One month, consolidated. Built once per change to anything it reads."""
    return _cached(("view", month), lambda: _build_view(month))


def _build_view(month: int) -> dict[str, Any]:
    cells: dict[tuple, dict[str, Any]] = {}
    names: dict[str, str] = {}
    slots: set[tuple[str, str]] = set()
    reported: dict[tuple[str, str], set[str]] = {}

    def cell(location: str, sl: str, rs: str, line: str) -> dict[str, Any]:
        key = norm(line)
        names.setdefault(key, line)
        at = (location, sl, norm(rs), key)
        if at not in cells:
            cells[at] = {"location": location, "service_line": sl, "rs": rs,
                         "line": names[key], "line_key": key, "actual": None,
                         "plan": None, "forecast": None, "prior_year": None,
                         "mtd": None, "filled": False}
        return cells[at]

    for location in LOCATIONS:
        for sl in SERVICE_LINES:
            source = fr_source(month, location, sl)
            if source is None:
                continue
            slots.add((location, sl))
            reported[(location, sl)] = set()
            for row in source["rows"]:
                reported[(location, sl)].add(norm(row["rs"]))
                got = cell(location, sl, row["rs"], row["line"])
                got["actual"] = row.get("actual")
                for measure in ("plan", "forecast", "prior_year"):
                    if row.get(measure) is not None:
                        got[measure] = row[measure]

    # PLAN AND FORECAST. Macros add these to the FR output today. A segment
    # that has values here and is absent from FR is what Finance now inserts by
    # hand; it is inserted with no actual, and listed so somebody can see it
    # happened. Only for a slot whose file has arrived: for one that has not,
    # the missing file is the problem to report rather than missing segments.
    missing: dict[tuple[str, str, str], dict[str, Any]] = {}
    for measure, kind in (("plan", "plan"), ("forecast", "forecast")):
        for row in _rows_of(kind, month):
            slot = (row["location"], row["service_line"])
            if slot not in slots:
                continue
            got = cell(*slot, row["rs"], row["line"])
            got[measure] = row[measure]
            if norm(row["rs"]) not in reported[slot]:
                got["filled"] = True
                entry = missing.setdefault((*slot, norm(row["rs"])), {
                    "location": slot[0], "service_line": slot[1], "rs": row["rs"],
                    "why": "In the Plan or Forecast this month, absent from FR",
                    "lines": 0})
                entry["lines"] += 1

    # A segment with plan only in later months of this year has no actuals yet
    # and still needs its row, or it surprises everybody on the day it starts.
    source = active("plan", None)
    if source is not None:
        last = fy_start(month) + 11
        for row in source["rows"]:
            at = row.get("month", month)
            slot = (row["location"], row["service_line"])
            if not month < at <= last or slot not in slots:
                continue
            if norm(row["rs"]) in reported[slot]:
                continue
            already = (*slot, norm(row["rs"]))
            got = cell(*slot, row["rs"], row["line"])
            got["filled"] = True
            if already not in missing:
                missing[already] = {
                    "location": slot[0], "service_line": slot[1], "rs": row["rs"],
                    "why": f"Planned from {month_label(at)}, nothing to report yet",
                    "lines": 0}
                missing[already]["_from"] = at
            if missing[already].get("_from") == at:
                missing[already]["lines"] += 1

    # PRIOR YEAR, from what was reported twelve months ago, ahead of any PY
    # column the FR file carried: the tool's own record is the one it can vouch
    # for, and the file's column is the fallback for before the tool existed.
    last_year, _py_slots, _ = actuals_at(month - 12)
    for at, got in cells.items():
        if at in last_year:
            got["prior_year"] = last_year[at]

    # MTD. The first month of the year IS its YTD. Otherwise a slot with no
    # prior month has no MTD, because subtracting nothing would report the
    # whole year as one month and nobody would notice.
    first_month = month == fy_start(month)
    before, before_slots, _ = actuals_at(month - 1)
    no_prior = sorted(slots - before_slots) if not first_month else []
    for at, got in cells.items():
        rule = mtd_rule(got["line"])
        if got["actual"] is None or rule == "pending":
            got["mtd"] = None
        elif rule == "closing" or first_month:
            got["mtd"] = got["actual"]
        elif (got["location"], got["service_line"]) in before_slots:
            got["mtd"] = got["actual"] - before.get(at, 0.0)

    attributes, conflicts = mapping(month)
    for got in cells.values():
        attrs = attributes.get(norm(got["rs"]), {})
        for field in ("type", "sub_service_line", "rate_card", "span", "competency"):
            got[field] = attrs.get(field, "")
    every_rs = {norm(c["rs"]): c["rs"] for c in cells.values()}
    unmapped = sorted(r for k, r in every_rs.items()
                      if k not in attributes and k not in conflicts)
    return {
        "month": month,
        "cells": cells,
        "names": names,
        "slots": slots,
        "missing": sorted(missing.values(),
                          key=lambda m: (m["location"], m["service_line"], m["rs"])),
        "unmapped": unmapped,
        "conflicts": sorted(conflicts.values()),
        "no_prior": no_prior,
        "alerts": _structure_alerts(month, cells, slots, before, before_slots, names),
    }


def _structure_alerts(month: int, cells: dict[tuple, dict[str, Any]],
                      slots: set[tuple[str, str]], before: dict[tuple, float],
                      before_slots: set[tuple[str, str]],
                      names: dict[str, str]) -> list[dict[str, Any]]:
    """What is shaped differently from last month, per slot.

    Consolidation carries a new line through by itself, which is exactly why
    somebody needs telling: Finance validates how a new line is treated.
    """
    if not before_slots:
        return []
    out: list[dict[str, Any]] = []
    now_lines: dict[tuple[str, str], set[str]] = {}
    now_rs: dict[tuple[str, str], dict[str, str]] = {}
    for got in cells.values():
        slot = (got["location"], got["service_line"])
        if got["actual"] is not None:
            now_lines.setdefault(slot, set()).add(got["line_key"])
            now_rs.setdefault(slot, {})[norm(got["rs"])] = got["rs"]
    then_lines: dict[tuple[str, str], set[str]] = {}
    then_rs: dict[tuple[str, str], set[str]] = {}
    _, _, then_names = actuals_at(month - 1)
    for location, sl, rs, line in before:
        then_lines.setdefault((location, sl), set()).add(line)
        then_rs.setdefault((location, sl), set()).add(rs)

    def say(slot: tuple[str, str], what: str, name: str) -> None:
        out.append({"id": f"{slot[0]}/{slot[1]}/{what}/{name}", "location": slot[0],
                    "service_line": slot[1], "change": what, "name": name,
                    "can": []})

    for slot in sorted(slots & before_slots):
        for line in sorted(now_lines.get(slot, set()) - then_lines.get(slot, set())):
            say(slot, "New line", names.get(line, line))
        for line in sorted(then_lines.get(slot, set()) - now_lines.get(slot, set())):
            say(slot, "Line gone since last month", then_names.get(line, line))
        for rs in sorted(set(now_rs.get(slot, {})) - then_rs.get(slot, set())):
            say(slot, "New Reporting Segment", now_rs[slot][rs])
        for rs in sorted(then_rs.get(slot, set()) - set(now_rs.get(slot, {}))):
            say(slot, "Reporting Segment gone since last month", then_names.get(rs, rs))
    return out


def _variance(actual: float | None, against: float | None) -> tuple[float | None, float | None]:
    """The difference, and as a percentage of what it is against."""
    if actual is None or against is None:
        return None, None
    difference = actual - against
    return difference, (difference / abs(against) * 100 if against else None)


def in_location(got: dict[str, Any], location: str) -> bool:
    return not location or location == ALL or got["location"] == location


def summary(month: int, *, location: str = "", service_line: str = "",
            rs: str = "") -> list[dict[str, Any]]:
    """The P&L at some level: one row per line, in the order the files give them.

    Ratios do not add up across segments, so above one segment they are blank.
    A blank is honest; the sum of twelve utilisation percentages is not.
    """
    picked = [c for c in view(month)["cells"].values()
              if in_location(c, location)
              and (not service_line or c["service_line"] == service_line)
              and (not rs or norm(c["rs"]) == norm(rs))]
    by_line: dict[str, list[dict[str, Any]]] = {}
    for got in picked:
        by_line.setdefault(got["line_key"], []).append(got)
    out = []
    for key, group in by_line.items():
        name = group[0]["line"]
        single = len(group) == 1
        adds = single or line_class(name) != "ratio"

        def total(measure: str) -> float | None:
            found = [g[measure] for g in group if g[measure] is not None]
            return _round(sum(found)) if found and adds else None

        actual, plan, forecast = total("actual"), total("plan"), total("forecast")
        prior = total("prior_year")
        to_plan, to_plan_pct = _variance(actual, plan)
        to_forecast, to_forecast_pct = _variance(actual, forecast)
        to_prior, to_prior_pct = _variance(actual, prior)
        out.append({
            "id": key, "line": name, "class": line_class(name),
            "ytd": actual, "plan": plan,
            "vs_plan": _round(to_plan), "vs_plan_pct": _round(to_plan_pct),
            "forecast": forecast,
            "vs_forecast": _round(to_forecast),
            "vs_forecast_pct": _round(to_forecast_pct),
            "prior_year": prior,
            "vs_prior_year": _round(to_prior),
            "vs_prior_year_pct": _round(to_prior_pct),
            "mtd": total("mtd"),
            "can": [],
        })
    return out


def _role_keys() -> dict[str, set[str]]:
    return {role: {norm(n) for n in names}
            for role, names in rules()["key_lines"].items()}


def segments(month: int, location: str = "") -> list[dict[str, Any]]:
    """One row per Reporting Segment, with what somebody reads it for."""
    roles = _role_keys()
    found: dict[tuple, dict[str, Any]] = {}
    for got in view(month)["cells"].values():
        if not in_location(got, location):
            continue
        at = (got["location"], got["service_line"], norm(got["rs"]))
        row = found.setdefault(at, {
            "id": "/".join(at), "location": got["location"],
            "service_line": got["service_line"], "rs": got["rs"],
            "type": got["type"], "sub_service_line": got["sub_service_line"],
            "rate_card": got["rate_card"], "span": got["span"],
            "competency": got["competency"], "revenue": None, "direct_cost": None,
            "fte": None, "filled": False, "can": []})
        row["filled"] = row["filled"] or got["filled"]
        for role in ("revenue", "direct_cost", "fte"):
            if got["line_key"] in roles[role] and got["actual"] is not None:
                row[role] = _round(got["actual"])
    return sorted(found.values(),
                  key=lambda r: (r["location"], r["service_line"], r["rs"]))


def slot_rollup(month: int, by: str) -> list[dict[str, Any]]:
    """The headline lines per location, or per service line."""
    roles = _role_keys()
    found: dict[str, dict[str, Any]] = {}
    for got in view(month)["cells"].values():
        name = got[by]
        row = found.setdefault(name, {
            "id": name, by: name, "revenue": None, "revenue_plan": None,
            "direct_cost": None, "fte": None, "segments": set(), "can": []})
        row["segments"].add(norm(got["rs"]))
        for role, field, measure in (("revenue", "revenue", "actual"),
                                     ("revenue", "revenue_plan", "plan"),
                                     ("direct_cost", "direct_cost", "actual"),
                                     ("fte", "fte", "actual")):
            if got["line_key"] in roles[role] and got[measure] is not None:
                row[field] = _round((row[field] or 0) + got[measure])
    out = []
    for name in sorted(found):
        row = found[name]
        row["segments"] = len(row["segments"])
        _, row["vs_plan_pct"] = _variance(row["revenue"], row["revenue_plan"])
        row["vs_plan_pct"] = _round(row["vs_plan_pct"])
        out.append(row)
    return out


def detail(month: int, location: str = "") -> list[dict[str, Any]]:
    """Every segment and line. The level the exceptions are found at."""
    out = []
    for got in view(month)["cells"].values():
        if not in_location(got, location):
            continue
        out.append({
            "id": f"{got['location']}/{got['service_line']}/"
                  f"{norm(got['rs'])}/{got['line_key']}",
            "location": got["location"], "service_line": got["service_line"],
            "rs": got["rs"], "line": got["line"], "ytd": _round(got["actual"]),
            "plan": _round(got["plan"]), "forecast": _round(got["forecast"]),
            "prior_year": _round(got["prior_year"]), "mtd": _round(got["mtd"]),
            "inserted": "yes" if got["filled"] else "", "can": [],
        })
    return out


# ──────────────────────────────────────────────────────────────────────────
# coverage: who has sent what
# ──────────────────────────────────────────────────────────────────────────

def skipped_slot(month: int, location: str, sl: str) -> dict[str, Any] | None:
    record = _records.get(f"skip-{month}-{location}-{sl}".replace(" ", "_"))
    return record if record and record.get("active") else None


def set_skip(month: int, location: str, sl: str, *, by: str, note: str,
             on: bool) -> None:
    """Whether a slot is expected this month. A person says, with a reason."""
    _put_record({"id": f"skip-{month}-{location}-{sl}".replace(" ", "_"),
                 "type": "skip", "active": on, "by": by, "note": note, "at": time.time()})
    log("slot_not_required" if on else "slot_required", by,
        f"{location} · {sl} {'not required' if on else 'required again'}"
        + (f": {note}" if note else ""), month)


def coverage(month: int) -> list[dict[str, Any]]:
    """Every location and service line, and where its file has got to."""
    out = []
    for location in LOCATIONS:
        for sl in SERVICE_LINES:
            source = fr_source(month, location, sl)
            skip = skipped_slot(month, location, sl)
            about = reminder_about(month, location, sl)
            notice = notices.latest_for(about)
            if source:
                status = "Uploaded"
            elif skip:
                status = "Not required"
            else:
                status = "Missing"
            out.append({
                "id": f"{location}|{sl}", "location": location, "service_line": sl,
                "status": status,
                "file": source["filename"] if source else "",
                "version": source["version"] if source else None,
                "rows": source["count"] if source else None,
                "by": source["by"] if source else (skip or {}).get("by", ""),
                "when": _day(source["at"]) if source else "",
                "note": (skip or {}).get("note", ""),
                "reminder": notice.said if notice else "",
            })
    return out


def missing_slots(month: int) -> list[tuple[str, str]]:
    return [(r["location"], r["service_line"]) for r in coverage(month)
            if r["status"] == "Missing"]


def reminder_about(month: int, location: str, sl: str) -> str:
    return f"pnl/{month}/{location}/{sl}"


def remind(month: int, location: str, sl: str, *, by: str) -> list[notices.Notice]:
    """Tell Finance a file has not arrived. Recorded first, sent afterwards.

    To whoever holds the role and can be written to. Nobody reachable is one
    held notice rather than silence, so the screen can say nobody was told.
    """
    who = [(p.email, p.name) for role in ("finance", "admin")
           for p in people.holders(role)] or [("", "Finance Team")]
    out = []
    for address, name in dict(who).items():
        out.append(notices.record(
            to=address, name=name,
            subject=f"FR file not uploaded: {location} · {sl}, {month_label(month)}",
            body=f"The FR P&L file for {location} · {sl} has not been uploaded "
                 f"for {month_label(month)}, so this month cannot be consolidated "
                 f"for that location.\n\nUpload it under Finance → P&L → Source files.",
            about=reminder_about(month, location, sl)))
    log("reminded", by, f"Reminder about {location} · {sl}", month)
    return out


def send_due_reminders(now: float | None = None) -> int:
    """The automatic reminder, from the background loop. Once a day at most.

    For last calendar month, from the rule's day onward. Never while the
    sample is on screen: a sample's missing files are not anybody's job. And
    not before anybody has filed one: a deployment that has not started using
    this owes nobody a file, and its twelve expected slots are only a list.
    """
    if _sample or not sources("fr"):
        return 0
    today = dt.date.fromtimestamp(time.time() if now is None else now)
    if today.day < rules()["reminder_day"]:
        return 0
    month = month_key(today.year, today.month) - 1
    stamp = f"reminded-{today.isoformat()}"
    if stamp in _records:
        return 0
    missing = missing_slots(month)
    if not missing:
        return 0
    for location, sl in missing:
        remind(month, location, sl, by="Compass")
    _put_record({"id": stamp, "type": "reminder_day", "at": time.time()})
    return len(missing)


# ──────────────────────────────────────────────────────────────────────────
# what the user has chosen, and the exception report
# ──────────────────────────────────────────────────────────────────────────

def selection(month: int) -> dict[str, Any]:
    """The thresholds this month is being run at."""
    base = rules()
    chosen = _records.get(f"selection-{month}", {})
    return {
        "plan_pct": chosen.get("plan_pct", base["plan_pct"]),
        "py_pct": chosen.get("py_pct", base["py_pct"]),
        "forecast_pct": chosen.get("forecast_pct", base["forecast_pct"]),
        "floor": chosen.get("floor", base["floor"]),
        "by": chosen.get("by", ""),
        "at": chosen.get("at", 0.0),
    }


def select_thresholds(month: int, *, by: str, **chosen: float) -> None:
    now = selection(month)
    fresh = {k: now[k] for k in ("plan_pct", "py_pct", "forecast_pct", "floor")}
    fresh.update(chosen)
    _put_record({"id": f"selection-{month}", "type": "selection", "by": by,
                 "at": time.time(), **fresh})
    log("thresholds_selected", by,
        f"Plan {fresh['plan_pct']:g}%, Prior Year {fresh['py_pct']:g}%, Forecast "
        f"{fresh['forecast_pct']:g}%, ignoring under {amount(fresh['floor'])}", month)


def _exception_id(check: str, month: int, *parts: str) -> str:
    text = "|".join([check, str(month), *parts])
    return f"EX-{hashlib.sha1(text.encode()).hexdigest()[:10]}"


def _segment_lines(month: int) -> dict[tuple[str, str, str], dict[str, dict[str, Any]]]:
    """Each segment's cells by the role the validations call them."""
    roles = _role_keys()
    found: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for got in view(month)["cells"].values():
        at = (got["location"], got["service_line"], got["rs"])
        found.setdefault(at, {})
        for role, names in roles.items():
            if got["line_key"] in names and got["actual"] is not None:
                found[at][role] = got
    return found


def exceptions(month: int) -> dict[str, list[dict[str, Any]]]:
    """Everything that breaches, and everything that could not be tested."""
    return _cached(("exceptions", month), lambda: exceptions_at(month, selection(month)))


def exceptions_at(month: int, picked: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The report at these thresholds. Also what a form's preview counts with."""
    found: list[dict[str, Any]] = []
    untested: list[dict[str, Any]] = []
    base = rules()

    def raised(check: str, got_location: str, sl: str, rs: str, line: str,
               value: float | None, against: float | None, *,
               variance: float | None, pct: float | None, threshold: str,
               why: str) -> None:
        found.append({
            "id": _exception_id(check, month, got_location, sl, norm(rs), norm(line)),
            "check": check, "comparison": CHECKS[check], "location": got_location,
            "service_line": sl, "rs": rs, "line": line, "value": _round(value),
            "against": _round(against), "variance": _round(variance),
            "variance_pct": _round(pct), "threshold": threshold, "why": why,
        })

    def untried(check: str, location: str, sl: str, rs: str, why: str) -> None:
        untested.append({
            "id": _exception_id("untested-" + check, month, location, sl, norm(rs)),
            "check": check, "comparison": CHECKS[check], "location": location,
            "service_line": sl, "rs": rs, "why": why, "can": [],
        })

    # The standard three, at segment and line. No comparison figure means no
    # comparison: "Where forecast information is available".
    for got in view(month)["cells"].values():
        actual = got["actual"] if got["actual"] is not None else 0.0
        for check, against, limit in (("plan", got["plan"], picked["plan_pct"]),
                                      ("prior_year", got["prior_year"], picked["py_pct"]),
                                      ("forecast", got["forecast"], picked["forecast_pct"])):
            if against is None:
                continue
            difference = actual - against
            if not difference:
                continue
            if line_class(got["line"]) == "flow" and abs(difference) < picked["floor"]:
                continue
            pct = difference / abs(against) * 100 if against else None
            if pct is not None and abs(pct) <= limit:
                continue
            word = "over" if difference > 0 else "under"
            raised(check, got["location"], got["service_line"], got["rs"], got["line"],
                   actual, against, variance=difference, pct=pct, threshold=f"{limit:g}%",
                   why=(f"{abs(pct):.1f}% {word}, past the {limit:g}% selected"
                        if pct is not None else
                        f"{amount(abs(difference))} {word} a comparison figure of zero"))

    # The specialized validations, per segment. Each says why when it cannot
    # run, because a segment with no rate card has not passed anything.
    hours = _hours(month)
    rates = _rates()
    attributes, _ = mapping(month)
    limit_by_location = base["eb_by_location"]
    for (location, sl, rs), lines in _segment_lines(month).items():
        revenue = lines.get("revenue", {}).get("actual")
        base_pay = lines.get("base", {}).get("actual")
        fte = lines.get("fte", {}).get("actual")
        direct = lines.get("direct_cost", {}).get("actual")

        worked = hours.get((location, sl, norm(rs)))
        rate_card = attributes.get(norm(rs), {}).get("rate_card", "")
        if revenue is None:
            untried("revenue", location, sl, rs, "No Revenue line for this segment")
        elif not worked:
            untried("revenue", location, sl, rs, "No charged hours for this segment")
        elif not rate_card:
            untried("revenue", location, sl, rs, "No rate card mapped to this segment")
        else:
            expected = 0.0
            gap = ""
            for grade, count in worked.items():
                rate = rates.get((norm(rate_card), norm(grade)),
                                 rates.get((norm(rate_card), "")))
                if rate is None:
                    gap = f"Rate card {rate_card} has no rate for {grade or 'this segment'}"
                    break
                expected += count * rate
            if gap:
                untried("revenue", location, sl, rs, gap)
            else:
                difference, pct = _variance(revenue, expected)
                limit = base["revenue_tolerance_pct"]
                if difference and (pct is None or abs(pct) > limit):
                    raised("revenue", location, sl, rs, lines["revenue"]["line"], revenue,
                           expected, variance=difference, pct=pct, threshold=f"{limit:g}%",
                           why=f"Reported revenue against hours × rate on {rate_card}")

        for check, role, limit, label in (
                ("variable_pay", "variable_pay", base["vp_pct"], "Variable Pay"),
                ("benefits", "benefits",
                 limit_by_location.get(location, base["eb_pct"]), "Employee Benefits")):
            cost = lines.get(role, {}).get("actual")
            if cost is None:
                continue
            if not base_pay:
                untried(check, location, sl, rs, "No Base Compensation to compare against")
                continue
            share = cost / base_pay * 100
            if share > limit:
                raised(check, location, sl, rs, lines[role]["line"], share, limit,
                       variance=share - limit, pct=None, threshold=f"{limit:g}%",
                       why=f"{label} is {share:.1f}% of Base Compensation")

        if direct is None:
            continue
        if revenue:
            share = direct / revenue * 100
            limit = base["dc_revenue_pct"]
            if share > limit:
                raised("direct_cost", location, sl, rs, "Direct Cost to Revenue", share, limit,
                       variance=share - limit, pct=None, threshold=f"{limit:g}%",
                       why=f"Direct Cost is {share:.1f}% of Revenue (definition pending)")
        else:
            untried("direct_cost", location, sl, rs, "No Revenue to compare Direct Cost against")
        if fte:
            per = direct / fte
            limit = base["dc_per_fte"]
            if per > limit:
                raised("direct_cost", location, sl, rs, "Direct Cost per FTE", per, limit,
                       variance=per - limit, pct=None, threshold=amount(limit),
                       why=f"Direct Cost per FTE is {amount(per)} (definition pending)")
        else:
            untried("direct_cost", location, sl, rs, "No FTE to divide Direct Cost by")

    order = list(CHECKS)
    found.sort(key=lambda r: (order.index(r["check"]), r["location"],
                              r["service_line"], r["rs"], r["line"]))
    for row in found:
        row["signature"] = f"{row['value']}|{row['against']}"
    return {"found": found, "untested": untested}


def _hours(month: int) -> dict[tuple[str, str, str], dict[str, float]]:
    out: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in _rows_of("hours", month):
        at = (row["location"], row["service_line"], norm(row["rs"]))
        grades = out.setdefault(at, {})
        grade = row.get("grade", "")
        grades[grade] = grades.get(grade, 0.0) + row["hours"]
    return out


def _rates() -> dict[tuple[str, str], float]:
    source = active("rates")
    if source is None:
        return {}
    return {(norm(r["rate_card"]), norm(r.get("grade", ""))): r["rate"]
            for r in source["rows"]}


def review_of(row: dict[str, Any]) -> dict[str, Any] | None:
    """Finance's note on an exception, while the figures are what it was about.

    New figures reopen it: a note about last week's upload does not explain
    this one. The note records that somebody looked. It changes no figure.
    """
    record = _records.get(f"review-{row['id']}")
    if record and record.get("active") and record.get("signature") == row["signature"]:
        return record
    return None


def set_review(row: dict[str, Any], month: int, *, by: str, note: str, on: bool) -> None:
    _put_record({"id": f"review-{row['id']}", "type": "review", "active": on,
                 "signature": row["signature"], "by": by, "note": note,
                 "at": time.time()})
    log("exception_reviewed" if on else "exception_reopened", by,
        f"{row['comparison']}: {row['rs']} · {row['line']}"
        + (f": {note}" if note else ""), month)


# ──────────────────────────────────────────────────────────────────────────
# reconciliation
# ──────────────────────────────────────────────────────────────────────────

def _within(difference: float, against: float) -> bool:
    base = rules()
    return abs(difference) <= max(base["recon_abs"], abs(against) * base["recon_pct"] / 100)


def reconciliation(month: int) -> dict[str, list[dict[str, Any]]]:
    return _cached(("recon", month), lambda: _reconcile(month))


def _reconcile(month: int) -> dict[str, list[dict[str, Any]]]:
    """Against Finance's own file, and against the FR file's own totals."""
    now = view(month)
    totals: dict[tuple[str, str, str], float] = {}
    has_line: set[tuple[str, str, str]] = set()
    for got in now["cells"].values():
        at = (got["location"], got["service_line"], got["line_key"])
        if got["actual"] is not None:
            has_line.add(at)
            totals[at] = totals.get(at, 0.0) + got["actual"]

    against_finance = []
    for row in _rows_of("finance", month):
        slot = (row["location"], row["service_line"])
        key = norm(row["parameter"])
        ours = totals.get((*slot, key))
        theirs = row["value"]
        if slot not in now["slots"]:
            status, difference = "No FR file", None
        elif (*slot, key) not in has_line:
            status, difference = "Not in the P&L", None
        else:
            difference = ours - theirs
            status = "Matched" if _within(difference, theirs) else "Mismatched"
        against_finance.append({
            "id": f"{slot[0]}|{slot[1]}|{key}", "location": slot[0],
            "service_line": slot[1], "parameter": row["parameter"],
            "compass": _round(ours), "finance": _round(theirs),
            "difference": _round(difference),
            "difference_pct": _round(difference / abs(theirs) * 100
                                     if difference is not None and theirs else None),
            "status": status, "can": [],
        })

    # "Location-level data should match the RS-level breakup."
    internal = []
    for location, sl in sorted(now["slots"]):
        source = fr_source(month, location, sl)
        if source is None:
            continue
        for line, total in source.get("totals", {}).items():
            key = norm(line)
            if line_class(line) == "ratio":
                continue
            ours = totals.get((location, sl, key), 0.0)
            difference = ours - total
            internal.append({
                "id": f"{location}|{sl}|{key}", "location": location,
                "service_line": sl, "parameter": line, "segments_total": _round(ours),
                "file_total": _round(total), "difference": _round(difference),
                "status": "Matched" if _within(difference, total) else "Mismatched",
                "can": [],
            })
    return {"finance": against_finance, "internal": internal}


def recon_problems(month: int) -> int:
    both = reconciliation(month)
    return sum(1 for r in both["finance"] + both["internal"] if r["status"] != "Matched")


def blockers(month: int) -> list[str]:
    """What stands between this month and a release, in words."""
    out = []
    if missing := missing_slots(month):
        out.append(f"{len(missing)} FR file{'' if len(missing) == 1 else 's'} missing")
    if active("finance", month) is None:
        out.append("no Finance consolidation to reconcile against")
    if problems := recon_problems(month):
        out.append(f"{problems} reconciliation difference{'' if problems == 1 else 's'}")
    return out


# ──────────────────────────────────────────────────────────────────────────
# the trail
# ──────────────────────────────────────────────────────────────────────────

def log(kind: str, by: str, said: str, month: int | None = None) -> None:
    _put_record({"id": f"log-{time.time_ns()}-{uuid.uuid4().hex[:6]}", "type": "log",
                 "kind": kind, "by": by, "said": said, "month": month,
                 "at": time.time()})


def trail(month: int | None = None) -> list[dict[str, Any]]:
    found = [r for r in _records.values() if r.get("type") == "log"
             and (month is None or r.get("month") in (None, month))]
    return sorted(found, key=lambda r: r["at"], reverse=True)


def _day(when: float) -> str:
    return dt.datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M") if when else ""


# ──────────────────────────────────────────────────────────────────────────
# leadership's file
# ──────────────────────────────────────────────────────────────────────────

def inputs_of(month: int) -> list[dict[str, Any]]:
    """Which uploads this month is built from. The run's traceability."""
    used = []
    for location in LOCATIONS:
        for sl in SERVICE_LINES:
            if source := fr_source(month, location, sl):
                used.append(source)
    for kind in ("plan", "forecast", "history", "finance", "hours"):
        if source := active(kind, month):
            used.append(source)
    for kind in ("mapping", "rates"):
        if source := active(kind):
            used.append(source)
    return [{"id": s["id"], "kind": s["label"], "file": s["filename"],
             "version": s["version"], "by": s["by"], "sha256": s["sha256"]}
            for s in used]


def packs(month: int) -> list[dict[str, Any]]:
    found = [r for r in _records.values() if r.get("type") == "pack"
             and r["month"] == month]
    return sorted(found, key=lambda r: r["at"], reverse=True)


def pack(pack_id: str) -> dict[str, Any] | None:
    found = _records.get(pack_id)
    return found if found and found.get("type") == "pack" else None


def build_pack(month: int, *, by: str, sheets: list[str], locations: list[str]) -> dict[str, Any]:
    """Make the file, once. What it says never changes afterwards.

    Stored as built, because the question leadership's file has to answer
    later is what it said on the day. A correction is a new pack.
    """
    number_of = 1 + len(packs(month))
    pack_id = f"PACK-{month_label(month).replace(' ', '-')}-{number_of}"
    data = _workbook(month, pack_id, by, sheets, locations)
    key = f"{pack_id}.xlsx"
    _files[key] = data
    _unsaved_files.append(key)
    record = {
        "id": pack_id, "type": "pack", "month": month, "sheets": sheets,
        "locations": locations, "by": by, "at": time.time(), "state": "draft",
        "file": key, "inputs": inputs_of(month), "rules_version": rules()["version"],
        "selection": {k: v for k, v in selection(month).items() if k not in ("by", "at")},
        "exceptions": len(exceptions(month)["found"]),
        "blockers": blockers(month),
    }
    _put_record(record)
    log("pack_built", by, f"{pack_id}: {', '.join(sheets)}", month)
    return record


def release_pack(record: dict[str, Any], *, by: str, note: str, past: list[str]) -> None:
    """Finance's decision, in their name. `past` is what they went ahead despite."""
    fresh = dict(record, state="released", released_by=by, released_at=time.time(),
                 release_note=note, released_past=past)
    _put_record(fresh)
    log("pack_released", by, f"{record['id']} released"
        + (f" despite {'; '.join(past)}" if past else "")
        + (f": {note}" if note else ""), record["month"])


def pack_is_stale(record: dict[str, Any]) -> bool:
    """Whether the month's files have moved since this was built."""
    return {i["id"] for i in record["inputs"]} != {i["id"] for i in inputs_of(record["month"])}


def _workbook(month: int, pack_id: str, by: str, sheets: list[str],
              locations: list[str]) -> bytes:
    """Values only. No formulas, no links, no workings, no sheet beyond the choice."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    book = Workbook()
    cover = book.active
    cover.title = "About this file"
    cover["A1"] = f"P&L · {month_label(month)}"
    cover["A1"].font = Font(bold=True, size=13)
    for at, (label, value) in enumerate((
            ("Pack", pack_id), ("Prepared by", by),
            ("Prepared on", dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()),
            ("Basis", "Values as uploaded. Compass flags and reports; it alters no figure."),
            ("State", "Draft"),
    ), start=3):
        cover.cell(row=at, column=1, value=label).font = Font(bold=True)
        cover.cell(row=at, column=2, value=value)
    cover.column_dimensions["A"].width = 16
    cover.column_dimensions["B"].width = 70

    fill = PatternFill("solid", fgColor="1D1D1F")

    def sheet(title: str, columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
        ws = book.create_sheet(title[:31])
        for c, (_key, label) in enumerate(columns, start=1):
            head = ws.cell(row=1, column=c, value=label)
            head.fill = fill
            head.font = Font(color="FFFFFF", bold=True)
            ws.column_dimensions[head.column_letter].width = 16 if c > 1 else 34
        for r, row in enumerate(rows, start=2):
            for c, (key, _label) in enumerate(columns, start=1):
                value = row.get(key)
                cell = ws.cell(row=r, column=c, value=value)
                if isinstance(value, float):
                    cell.number_format = "#,##0.00"
        ws.freeze_panes = "A2"

    lines = [("line", "P&L line"), ("ytd", "YTD Actual"), ("plan", "YTD Plan"),
             ("vs_plan", "vs Plan"), ("vs_plan_pct", "vs Plan %"),
             ("forecast", "YTD Forecast"), ("vs_forecast_pct", "vs Forecast %"),
             ("prior_year", "Prior Year"), ("vs_prior_year_pct", "vs Prior Year %"),
             ("mtd", "MTD Actual")]
    if "Pan-GDS summary" in sheets:
        sheet("Pan-GDS", lines, summary(month))
    if "Location summaries" in sheets:
        for location in locations or list(LOCATIONS):
            sheet(location, lines, summary(month, location=location))
    if "Service line summary" in sheets:
        for sl in SERVICE_LINES:
            sheet(sl, lines, summary(month, service_line=sl))
    if "Reporting Segment analysis" in sheets:
        sheet("Reporting Segments",
              [("location", "Location"), ("service_line", "Service line"), ("rs", "RS"),
               ("type", "Type"), ("sub_service_line", "Sub-service line"),
               ("rate_card", "Rate card"), ("span", "Span"), ("competency", "Competency"),
               ("revenue", "Revenue"), ("direct_cost", "Direct cost"), ("fte", "FTE")],
              [r for r in segments(month)
               if not locations or r["location"] in locations])
    if "Exception report" in sheets:
        sheet("Exceptions",
              [("comparison", "Comparison"), ("location", "Location"),
               ("service_line", "Service line"), ("rs", "RS"), ("line", "P&L line"),
               ("value", "Value"), ("against", "Against"), ("variance", "Variance"),
               ("variance_pct", "Variance %"), ("threshold", "Threshold"), ("why", "Why")],
              [r for r in exceptions(month)["found"]
               if not locations or r["location"] in locations])
    if "Reconciliation" in sheets:
        sheet("Reconciliation",
              [("location", "Location"), ("service_line", "Service line"),
               ("parameter", "Parameter"), ("compass", "P&L"), ("finance", "Finance"),
               ("difference", "Difference"), ("status", "Status")],
              reconciliation(month)["finance"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def pack_file(record: dict[str, Any]) -> bytes:
    """The file as built, with today's state on its cover.

    Stamped on the way out so a draft cannot be mistaken for the released one,
    which is the only thing about the file allowed to differ from the day it
    was made. Nothing here computes a value: every other cell is as stored.
    """
    from openpyxl import load_workbook

    raw = _read_file(record["file"])
    book = load_workbook(io.BytesIO(raw))
    cover = book["About this file"]
    state = ("Draft, not released" if record["state"] != "released"
             else f"Released by {record['released_by']} on {_day(record['released_at'])}")
    for row in cover.iter_rows(min_col=1, max_col=2):
        if row[0].value == "State":
            row[1].value = state
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ──────────────────────────────────────────────────────────────────────────
# the store
# ──────────────────────────────────────────────────────────────────────────

def _put_record(record: dict[str, Any]) -> None:
    record.setdefault("owner", "")
    _records[record["id"]] = record
    _unsaved_records.append(record["id"])
    _touch()


def _files_dir():
    settings = get_settings()
    folder = settings.workspace_root / settings.data_dir / "pnl_files"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _in_blob() -> bool:
    """Whether kept files go to blob storage rather than the data folder.

    Only when the rest of the store is in the cloud too. A connection string in
    `.env` is not enough: the checks force the local backend and a throwaway
    folder, and a file written past both would be test data in a live account.
    """
    from compass.common.persistence import blob

    return blob.enabled() and get_settings().storage.backend != "local"


def _write_file(key: str, data: bytes) -> None:
    from compass.common.persistence import blob

    if _in_blob():
        blob.container("pnl-files").upload_blob(key, data, overwrite=True)
    else:
        (_files_dir() / key).write_bytes(data)


def _read_file(key: str) -> bytes:
    if key in _files:
        return _files[key]
    from compass.common.persistence import blob

    if _in_blob():
        return blob.container("pnl-files").download_blob(key).readall()
    return (_files_dir() / key).read_bytes()


async def ready(sample: bool = False) -> None:
    """Load once per process. Fall back to the sample only when nothing is real."""
    global _loaded, _sample
    if _loaded:
        return
    metas = await _sources_store.all()
    chunks: dict[str, list[dict[str, Any]]] = {}
    for chunk in await _chunks_store.all():
        chunks.setdefault(chunk["source"], []).append(chunk)
    loaded = []
    for meta in sorted(metas, key=lambda m: m["at"]):
        parts = sorted(chunks.get(meta["id"], []), key=lambda c: c["n"])
        columns = meta.get("columns", [])
        meta["rows"] = [
            {k: v for k, v in zip(columns, row) if v is not None}
            for part in parts for row in part["rows"]]
        loaded.append(meta)
    _sources[:] = loaded
    _records.clear()
    _records.update({r["id"]: r for r in await _records_store.all()})
    _loaded = True
    _touch()
    if not _sources and sample:
        from compass.businessfunctions import pnl_sample

        # Asked for by no file having arrived, not by nothing at all being
        # stored. A rule version or a log line is not a month of figures, and
        # none of it belongs on the sample's screens; it comes back with the
        # first real file.
        _stored_records.update(_records)
        _records.clear()
        pnl_sample.load()
        _sample = True
        # Everything the sample just did is held here and never written. It is
        # a demonstration, and a firm's store is not where one of those goes.
        _unsaved_sources.clear()
        _unsaved_records.clear()
        _unsaved_files.clear()
    logger.info("P&L: %d source file(s), sample=%s", len(_sources), _sample)


def _leave_sample() -> None:
    """The first real file ends the demonstration, whole.

    A sample Bengaluru left beside a real Hyderabad would add up to a
    Pan-GDS figure nobody could tell was false.
    """
    global _sample
    if not _sample:
        return
    _sample = False
    _sources.clear()
    _records.clear()
    _records.update(_stored_records)
    _stored_records.clear()
    _files.clear()
    _unsaved_sources.clear()
    _unsaved_records.clear()
    _unsaved_files.clear()
    _touch()
    logger.info("P&L: first real file received; the sample is gone")


async def flush() -> None:
    """Write what changed since the last flush. Nothing, while it is a sample."""
    if _sample:
        _unsaved_sources.clear()
        _unsaved_records.clear()
        _unsaved_files.clear()
        return
    while _unsaved_files:
        key = _unsaved_files.pop(0)
        if key in _files:
            await asyncio.to_thread(_write_file, key, _files[key])
            # Held only until it is safely stored; a month's files are not
            # worth keeping in memory as well.
            del _files[key]
    while _unsaved_sources:
        source = _unsaved_sources.pop(0)
        columns = sorted({k for r in source["rows"] for k in r})
        meta = {k: v for k, v in source.items() if k != "rows"}
        meta["columns"] = columns
        for n in range(0, max(len(source["rows"]), 1), CHUNK):
            part = source["rows"][n:n + CHUNK]
            await _chunks_store.put({
                "id": f"{source['id']}-{n // CHUNK}", "source": source["id"],
                "n": n // CHUNK, "owner": "",
                "rows": [[r.get(c) for c in columns] for r in part]})
        # Last, so a source that reads as stored has all its rows.
        await _sources_store.put(meta)
    while _unsaved_records:
        record_id = _unsaved_records.pop(0)
        if record_id in _records:
            await _records_store.put(_records[record_id])


def pending() -> int:
    return len(_unsaved_sources) + len(_unsaved_records) + len(_unsaved_files)


def forget() -> None:
    """Drop what is held, so the next `ready` reloads. For checks only."""
    global _loaded, _sample
    _sources.clear()
    _records.clear()
    _stored_records.clear()
    _files.clear()
    _unsaved_sources.clear()
    _unsaved_records.clear()
    _unsaved_files.clear()
    _loaded = False
    _sample = False
    _touch()
