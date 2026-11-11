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

import ast
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
    divisor_of,
    month_key,
    month_label,
    norm,
    parse_month,
)
from compass.common.config import get_settings
from compass.common.persistence.catalog import Collection

logger = logging.getLogger("compass.businessfunctions")

#: The location sheets in the Consulting consolidation, and the service lines in
#: the overall Finance summary. The manifest repeats them because a form's options
#: live there; a check keeps them equal.
LOCATIONS = ("India", "Argentina", "Hungary", "Mexico", "Philippines", "Poland",
             "Spain", "Sri Lanka", "Consulting MENA")
SERVICE_LINES = ("Assurance", "Client Technology", "Consulting", "EYP", "Tax")
#: The selector value meaning every location together.
ALL = "Pan-GDS"

#: What leadership's file may be made of. The backend workings are not on this
#: list, which is how they stay out of the file.
SHEETS = ("Consolidated", "Location P&Ls", "MTD P&L", "RS wise analysis",
          "RS YTD vs Plan", "RS YTD vs PY", "RS YTD vs Forecast",
          "RS MTD vs Plan", "RS MTD vs PY", "Rate card YTD vs Plan",
          "Exception report", "Reconciliation")

#: Which measure a comparison reads, at YTD and at MTD.
SCENARIOS = {"plan": ("plan", "mtd_plan", "Plan"),
             "prior_year": ("prior_year", "mtd_py", "Prior Year"),
             "forecast": ("forecast", "mtd_forecast", "Forecast")}

#: Inputs that speak for every month rather than for the months they name.
PERIODLESS = ("mapping", "rates", "lines")
#: Inputs filed under a reporting month, which numbers its own versions. A close
#: can take several Plan or Forecast worksheets, and a later month's must not
#: reach back and change this one.
FILED_BY_MONTH = ("plan", "forecast", "finance", "hours")

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
    # The discovery notes' illustrative plus or minus 5%, which they say is not a
    # finished universal rule. Finance's own thresholds replace it.
    "plan_pct": 5.0,
    "py_pct": 5.0,
    "forecast_pct": 5.0,
    # A percentage alone flags 5 against a plan of 1. In the report's own units,
    # which FR divides by a thousand, and for money lines only: an FTE movement
    # of three is not small because three is less than this.
    "floor": 100.0,
    "vp_pct": 20.0,
    "eb_pct": 12.0,
    "eb_by_location": {},
    "dc_revenue_pct": 60.0,
    "dc_per_fte": 300.0,
    "revenue_tolerance_pct": 2.0,
    "recon_abs": 1.0,
    "recon_pct": 0.5,
    "reminder_day": 5,
    # July, because FY27 August YTD and Sep'26 are both FY27 in the supplied
    # files and the firm's global year ends in June. The files alone would also
    # fit April, so Finance confirms this. Wrong, the first month's MTD is a
    # subtraction it should not be.
    "fy_first_month": 7,
    # What a Plan or Forecast month column holds. "monthly": that month's own
    # amount, so YTD adds the year's months and MTD is the month itself, which
    # is what Plan 27 Data shows (Base Comp 2,56,373 in October, 1,98,189 in
    # November). "ytd": the year so far, which an older file might hold.
    "plan_months": "monthly",
    # What a Plan or Forecast's money is in. "whole": whole currency, divided by
    # the FR file's own divisor on the way in, as Plan 27 Data's are (a month's
    # base pay for one segment reads 1,39,443). "report": already in FR's units.
    # Hours, FTE and percentages are never divided.
    "plan_units": "whole",
    # Consulting is the reference model and the other service lines follow it, so
    # only its files are expected until Finance says otherwise.
    "service_lines_in_use": ["Consulting"],
    # What the month-end run waits for besides every expected FR file. Each has
    # to have passed its checks before Start is offered.
    "required_inputs": ["plan", "forecast", "mapping"],
    # What the validations call each line. Matched on the whole name, so that
    # "Other revenue" is never read as Revenue.
    "key_lines": {
        "revenue": ["total net revenue", "revenue", "net revenue", "total revenue"],
        "base": ["base compensation", "base comp", "base pay"],
        "variable_pay": ["variable pay", "bonus"],
        "benefits": ["employee benefits", "benefits"],
        "direct_cost": ["total direct expense", "direct cost", "direct costs",
                        "total direct cost"],
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
    "service_lines_in_use", "plan_months", "plan_units", "required_inputs",
})

#: The calculated lines whose formulas the process document states. The macro's
#: Settings sheet holds more (CS Staff Comp & Benefits, Total Direct Expense, the
#: Total Income GDS and Distributable Income lines, the utilisations): those are
#: not guessed, and arrive with the P&L line master when Finance uploads it.
#: A percentage is a fraction, as Excel holds it (`IFERROR(E22/E9,0)` is 0.25),
#: and is shown as 25.0%; its variance is in points.
DEFAULT_FORMULAS = {
    "Gross Margin": "[Total Net Revenue] - [Total Direct Expense]",
    "Total Indirect Expenses":
        "[Regional Controllable Indirect Expenses] + [Centrally Controlled Indirect Expenses]",
    "Total Income": "[Gross Margin] - [Total Indirect Expenses]",
    "Gross Margin % of TNR": "[Gross Margin] / [Total Net Revenue]",
}

#: The operating measures on the RS comparison sheets, checked against the RS-level
#: breakout separately from the money.
NON_FINANCIAL = ("Over Recovery", "Charged Hours", "FTE", "Full Utilisation",
                 "Effective Utilisation", "Standard Hours", "Effective Available Hours")

#: What the RS and rate-card comparisons report: the sheets' operating measures,
#: and the revenue and cost lines somebody reads them beside.
ANALYSIS_LINES = (*NON_FINANCIAL, "Total Net Revenue", "Total Direct Expense", "Gross Margin")

#: How a line behaves. A `flow` adds up across segments and across months; a
#: `stock` adds across segments only; a `ratio` adds across neither.
_RATIO = re.compile(r"%|\bpercent|\butili[sz]ation\b|\bper fte\b|\brate\b")
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


def _round(value: float | None, ratio: bool = False) -> float | None:
    """Two places for an amount; six for a fraction, which two would make whole %."""
    return None if value is None else round(value, 6 if ratio else 2)


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


def _line_master() -> dict[str, dict[str, Any]]:
    """The governed lines: {"alias": {spoken name: key}, "lines": {key: record}}.

    Descriptions are the row key today, and they change. The master gives a line
    one identity however it is spelled this month. With none uploaded, a line is
    simply its own cleaned description, as it is to the macro.
    """
    def build():
        aliases: dict[str, str] = {}
        lines: dict[str, dict[str, Any]] = {}
        source = active("lines")
        for row in (source["rows"] if source else []):
            key = norm(row["line"])
            lines[key] = row
            for alias in re.split(r"[;|]", str(row.get("alias", ""))):
                if alias.strip():
                    aliases[norm(alias)] = key
        return {"alias": aliases, "lines": lines}
    return _cached(("line_master",), build)


def line_key(name: str) -> str:
    """Which line this description is, whatever it is called this month."""
    said = norm(name)
    return _line_master()["alias"].get(said, said)


def line_id(name: str) -> str:
    return _line_master()["lines"].get(line_key(name), {}).get("line_id", "")


def line_class(name: str) -> str:
    """Whether this line adds up, and over what. Finance's word overrides."""
    key = line_key(name)
    said = rules()["line_rules"].get(key, {})
    if said.get("class"):
        return said["class"]
    master = norm(_line_master()["lines"].get(key, {}).get("class", ""))
    if master in ("flow", "stock", "ratio"):
        return master
    lowered = str(name).lower()
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
    figure. `closing` means the month's value is simply this month's value. A
    ratio with a formula needs neither: it is worked out from its month's parts.
    """
    said = rules()["line_rules"].get(line_key(name), {})
    if said.get("mtd"):
        return said["mtd"]
    return "subtract" if line_class(name) == "flow" else "pending"


def category(name: str) -> str:
    """What an exception is about: revenue, direct cost, indirect cost or other."""
    master = _line_master()["lines"].get(line_key(name), {}).get("section")
    if master:
        return master
    lowered = str(name).lower()
    if any(n in lowered for n in NON_FINANCIAL_WORDS):
        return "Operating measure"
    if "indirect" in lowered:
        return "Indirect cost"
    if any(w in lowered for w in ("direct", "comp", "variable pay", "benefit")):
        return "Direct cost"
    if "revenue" in lowered:
        return "Revenue"
    if any(w in lowered for w in ("margin", "income")):
        return "Result"
    return "Other"


NON_FINANCIAL_WORDS = ("hours", "fte", "utilis", "utiliz", "recovery", "headcount")


_REFERENCE = re.compile(r"\[([^\]]+)\]")


def parse_formula(text: str) -> tuple[ast.Expression, list[str]]:
    """A formula as a tree, and the lines it reads. ValueError if it is not one.

    Written `[Gross Margin] / [Total Net Revenue] * 100`. Read here rather than
    evaluated by Python, because it arrives in a spreadsheet: only numbers, the
    four operators and brackets get through.
    """
    refs: list[str] = []

    def swap(found: re.Match) -> str:
        refs.append(line_key(found[1]))
        return f"v{len(refs) - 1}"

    try:
        tree = ast.parse(_REFERENCE.sub(swap, text.strip()), mode="eval")
    except SyntaxError as err:
        raise ValueError(f"{text!r} is not a formula Compass can read.") from err
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
               ast.USub, ast.UAdd, ast.Constant, ast.Name, ast.Load)
    for node in ast.walk(tree):
        if not isinstance(node, allowed) or (
                isinstance(node, ast.Constant) and not isinstance(node.value, (int, float))):
            raise ValueError(f"{text!r} may only use lines, numbers and + - * / ( ).")
    if not refs:
        raise ValueError(f"{text!r} names no line. Write them as [Total Net Revenue].")
    return tree, refs


def _evaluate(node: ast.AST, refs: list[str], values: dict[str, float | None]) -> float | None:
    """The formula's value, or None if any line it reads has none."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, refs, values)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return values.get(refs[int(node.id[1:])])
    if isinstance(node, ast.UnaryOp):
        inner = _evaluate(node.operand, refs, values)
        return None if inner is None else (-inner if isinstance(node.op, ast.USub) else inner)
    left, right = (_evaluate(node.left, refs, values), _evaluate(node.right, refs, values))
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return left + right
    if isinstance(node.op, ast.Sub):
        return left - right
    if isinstance(node.op, ast.Mult):
        return left * right
    return left / right if right else None


def formulas() -> dict[str, tuple[str, ast.Expression, list[str]]]:
    """Every calculated line: {key: (display name, tree, lines read)}."""
    def build():
        said = dict(DEFAULT_FORMULAS)
        for row in _line_master()["lines"].values():
            if row.get("formula"):
                said[row["line"]] = str(row["formula"]).lstrip("=")
        out = {}
        for name, text in said.items():
            try:
                out[line_key(name)] = (name, *parse_formula(text))
            except ValueError:
                logger.warning("P&L: skipping the formula for %s", name)
        return out
    return _cached(("formulas",), build)


def apply_formulas(values: dict[str, float | None], *, always: bool = False,
                   only: set[str] | None = None) -> None:
    """Fill the calculated lines in `values`, the macro's Settings rules.

    Only where there is no figure, unless `always`: a stored value is not
    overwritten, because that is what is being reconciled. Repeated, because one
    calculated line can read another: Total Income reads Gross Margin.
    """
    table = formulas()
    for _ in range(len(table) + 1):
        moved = False
        for key, (_name, tree, refs) in table.items():
            if only is not None and key not in only:
                continue
            if not always and values.get(key) is not None:
                continue
            figure = _evaluate(tree, refs, values)
            if figure is not None and values.get(key) != figure:
                values[key] = figure
                moved = True
        if not moved:
            break


def fy_start(month: int) -> int:
    """The first month of the financial year this month falls in."""
    first = rules()["fy_first_month"] - 1
    return month - ((month % 12 - first) % 12)


def fiscal_label(month: int) -> str:
    """Which fiscal year this month is in, as the files say it: "27"."""
    last = fy_start(month) + 11
    return f"{(last // 12) % 100:02d}"


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


def _filed(source: dict[str, Any]) -> int:
    """The month an upload was filed under. 0 for an input that has none."""
    return source.get("filed_for", 0)


def active(kind: str, month: int | None = None) -> dict[str, Any] | None:
    """The file of this kind that counts for this month.

    The month's own newest version. Without one, the nearest earlier month's
    that names it, because a Plan worksheet can name twelve months and need
    filing only once. A month before anything was filed takes the first month
    filed that names it. So a file reaches forward and never changes a month
    that already has an answer.
    """
    found = [s for s in _sources if s["kind"] == kind
             and (month is None or month in s["months"] or kind in PERIODLESS)]
    if not found or month is None:
        return found[-1] if found else None
    settled = [s for s in found if _filed(s) <= month]
    if settled:
        return sorted(settled, key=_filed)[-1]
    first = min(_filed(s) for s in found)
    return [s for s in found if _filed(s) == first][-1]


def next_version(kind: str, month: int | None = None, location: str = "",
                 service_line: str = "") -> int:
    """The number the next upload would take: each month counts its own."""
    filed = (month or 0) if kind == "fr" or kind in FILED_BY_MONTH else 0
    return 1 + sum(1 for s in _sources if s["kind"] == kind and _filed(s) == filed
                   and s["location"] == location and s["service_line"] == service_line)


def version_label(source: dict[str, Any]) -> str:
    """How a version is named: v3 of Sep 2026, or v2 for an input with no month."""
    filed = _filed(source)
    return f"v{source['version']}" + (f" of {month_label(filed)}" if filed else "")


def versions(month: int) -> list[dict[str, Any]]:
    """Every version a month can be asked about: its own, and whatever it is reading.

    Oldest first, each with whether this month reads it.
    """
    out = []
    for source in _sources:
        kind = source["kind"]
        counted = (fr_source(month, source["location"], source["service_line"]) is source
                   if kind == "fr" else active(kind, month) is source)
        names_it = kind in PERIODLESS or (not _filed(source) and month in source["months"])
        if _filed(source) == month or names_it or counted:
            out.append({"source": source, "in_use": counted})
    return out


def is_current(source: dict[str, Any]) -> bool:
    """Whether anything still reads this upload."""
    if source["kind"] == "fr":
        return fr_source(source["months"][0], source["location"],
                         source["service_line"]) is source
    if source["kind"] in PERIODLESS:
        return active(source["kind"]) is source
    if any(active(source["kind"], m) is source
           for m in {*source["months"], *filter(None, [_filed(source)])}):
        return True
    newer = [s for s in _sources if s["kind"] == source["kind"]
             and set(s.get("fiscal_years", [])) & set(source.get("fiscal_years", []))]
    return bool(newer) and newer[-1] is source


def _rows_of(kind: str, month: int) -> list[dict[str, Any]]:
    source = active(kind, month)
    if source is None:
        return []
    return [r for r in source["rows"]
            if "fiscal_year" not in r and r.get("month", month) == month]


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
        if "fiscal_year" in row:
            rows.append(kept)
            continue
        if "period" in row or kind in ("plan", "forecast", "history", "finance", "hours"):
            raw_period = row.get("period")
            at = (raw_period if isinstance(raw_period, int)
                  else parse_month(raw_period) if raw_period else month)
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
    version = next_version(kind, month, location, service_line)
    source = {
        "id": f"{kind}-{uuid.uuid4().hex[:10]}",
        "kind": kind,
        "label": SPECS[kind].label,
        "filename": filename,
        "sha256": sha,
        "months": months,
        "filed_for": (month or 0) if kind == "fr" or kind in FILED_BY_MONTH else 0,
        "location": location,
        "service_line": service_line,
        "version": version,
        "by": by,
        "at": time.time(),
        "count": len(rows),
        "totals": parsed.totals,
        "skipped": parsed.skipped,
        "cover": parsed.cover,
        "errors": parsed.errors,
        "fiscal_years": sorted({r["fiscal_year"][-2:] for r in rows if "fiscal_year" in r}),
        "owner": "",
        "rows": rows,
    }
    _sources.append(source)
    _unsaved_sources.append(source)
    _files[f"{sha}{_suffix(filename)}"] = raw
    _unsaved_files.append(f"{sha}{_suffix(filename)}")
    _touch()
    log("uploaded", by, f"{SPECS[kind].label} {version_label(source)}: {filename} "
                        f"({len(rows)} rows)"
                        + (f", {location} · {service_line}" if location else ""),
        _filed(source) or None)
    return source


def _suffix(filename: str) -> str:
    return ".csv" if filename.lower().endswith(".csv") else ".xlsx"


def same_file(raw: bytes, kind: str = "", month: int | None = None) -> dict[str, Any] | None:
    """A current upload with these exact bytes, or None.

    The same report filed twice under two locations is the mistake this
    finds: nothing else would notice, because every figure in it is valid. The
    same Plan worksheet filed for another month is not one: that month has no
    version of its own until it is.
    """
    sha = hashlib.sha256(raw).hexdigest()
    for source in reversed(_sources):
        if source["sha256"] != sha or not is_current(source):
            continue
        if kind in FILED_BY_MONTH and source["kind"] == kind and _filed(source) != month:
            continue
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
                    key = line_key(row["line"])
                    names.setdefault(key, row["line"])
                    names.setdefault(norm(row["rs"]), row["rs"])
                    figures[(location, line_of_service, norm(row["rs"]), key)] = row["actual"]
        for row in _rows_of("history", month):
            slot = (row["location"], row["service_line"])
            if slot in slots and fr_source(month, *slot) is not None:
                continue
            key = line_key(row["line"])
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


#: Words in a line's name that make it a count, not money: never divided.
_COUNTS = ("hours", "fte", "headcount", "head count", "utilis", "utiliz", "additions",
           "resignation", "transfers")


def is_money(name: str) -> bool:
    """Whether a line is an amount of currency, which a divisor applies to."""
    lowered = str(name).lower()
    return line_class(name) != "ratio" and not any(w in lowered for w in _COUNTS)


def monthly_plans() -> bool:
    return rules().get("plan_months", "monthly") == "monthly"


#: (segment, line key, location, service line): a Plan or Forecast row's key.
ScenarioKey = tuple[str, str, str, str]


def _scenario_months(kind: str, month: int) -> dict[int, dict[ScenarioKey, float]]:
    """Each month of the year so far, as the Plan or Forecast in use gives it.

    From the version that counts for this month, which a Plan naming twelve
    months is; a month it does not name comes from the version filed for that
    month or before, never from one filed later, which this month's own version
    replaced. Repeated rows are added up, as the macro's aggregation does.
    """
    def build():
        using = active(kind, month)
        out: dict[int, dict[ScenarioKey, float]] = {}
        for at in range(fy_start(month), month + 1):
            source = using if using is not None and at in using["months"] else active(kind, at)
            if source is None or at not in source["months"]:
                continue
            if source is not using and _filed(source) > at:
                continue
            found = out.setdefault(at, {})
            for row in source["rows"]:
                if "fiscal_year" in row or row.get("month") != at:
                    continue
                key = (row["rs"], line_key(row["line"]), row.get("location", ""),
                       row.get("service_line", ""))
                found[key] = found.get(key, 0.0) + row[kind]
        return out
    return _cached(("scenario_months", kind, month), build)


def _scenario(kind: str, month: int) -> dict[ScenarioKey, float]:
    """A Plan or Forecast as at this month: its YTD, which FR's Actual is compared with.

    Keyed on (segment, line, location, service line), the last two empty when the
    file does not say: the macro's own key is only the first two. With monthly
    columns an amount adds up the year's months so far, a count such as FTE is
    their average, and a percentage is left to its formula.
    """
    def build():
        if not monthly_plans():
            out: dict[ScenarioKey, float] = {}
            for row in _rows_of(kind, month):
                at = (row["rs"], line_key(row["line"]), row.get("location", ""),
                      row.get("service_line", ""))
                out[at] = out.get(at, 0.0) + row[kind]
            return out
        return _year_so_far(_scenario_months(kind, month).values())
    return _cached(("scenario", kind, month), build)


def _year_so_far(months: Any) -> dict[ScenarioKey, float]:
    """Months added into one figure per key: a sum, an average for a count, no ratio."""
    sums: dict[ScenarioKey, float] = {}
    seen: dict[ScenarioKey, int] = {}
    for figures in months:
        for key, figure in figures.items():
            sums[key] = sums.get(key, 0.0) + figure
            seen[key] = seen.get(key, 0) + 1
    out: dict[ScenarioKey, float] = {}
    for key, total in sums.items():
        behaves = line_class(_line_name(key[1]))
        if behaves == "ratio":
            continue
        out[key] = total / seen[key] if behaves == "stock" else total
    return out


def _line_name(key: str) -> str:
    """A line key's governed name, which is what a line's class is read from."""
    return _line_master()["lines"].get(key, {}).get("line") or key


def _scenario_month(kind: str, month: int) -> dict[ScenarioKey, float]:
    """The month's own Plan or Forecast, which is its MTD when the columns are monthly."""
    if not monthly_plans():
        return {}
    figures = _scenario_months(kind, month).get(month, {})
    return {k: v for k, v in figures.items() if line_class(_line_name(k[1])) != "ratio"}


def _plan_gaps(kind: str, month: int) -> list[int]:
    """Months of this year so far that no version of a Plan or Forecast names."""
    if not monthly_plans() or active(kind, month) is None:
        return []
    held = _scenario_months(kind, month)
    return [m for m in range(fy_start(month), month + 1) if m not in held]


def _full_year(kind: str, month: int) -> dict[tuple[str, str], float]:
    """A Plan's whole fiscal year: its fiscal-year column, or else its twelve months.

    The column from the version in use, or else the newest with this year. With
    no such column, the version in use's own months, when it names all twelve.
    """
    label = fiscal_label(month)
    found = [s for s in _sources if s["kind"] == kind and label in s.get("fiscal_years", [])]
    using = active(kind, month)
    chosen = using if using in found else (found[-1] if found else None)
    out: dict[tuple[str, str], float] = {}
    for row in (chosen["rows"] if chosen else []):
        if str(row.get("fiscal_year", ""))[-2:] == label:
            at = (norm(row["rs"]), line_key(row["line"]))
            out[at] = out.get(at, 0.0) + row["full_year"]
    if out or using is None or not monthly_plans():
        return out
    year = range(fy_start(month), fy_start(month) + 12)
    if not all(m in using["months"] for m in year):
        return out
    by_month: dict[int, dict[ScenarioKey, float]] = {}
    for row in using["rows"]:
        if row.get("month") in year and "fiscal_year" not in row:
            # Location and service line dropped: the column this stands in for
            # has neither.
            key = (norm(row["rs"]), line_key(row["line"]), "", "")
            figures = by_month.setdefault(row["month"], {})
            figures[key] = figures.get(key, 0.0) + row[kind]
    return {k[:2]: v for k, v in _year_so_far(by_month.values()).items()}


def _build_view(month: int) -> dict[str, Any]:
    cells: dict[tuple, dict[str, Any]] = {}
    names: dict[str, str] = {}
    slots: set[tuple[str, str]] = set()
    reported: dict[str, set[tuple[str, str]]] = {}
    alerts: list[dict[str, Any]] = []

    def cell(location: str, sl: str, rs: str, line: str) -> dict[str, Any]:
        key = line_key(line)
        names.setdefault(key, _line_master()["lines"].get(key, {}).get("line") or line)
        at = (location, sl, norm(rs), key)
        if at not in cells:
            cells[at] = {"location": location, "service_line": sl, "rs": rs,
                         "line": names[key], "line_key": key, "actual": None,
                         "plan": None, "forecast": None, "prior_year": None,
                         "full_year_plan": None, "full_year_py": None,
                         "mtd": None, "mtd_plan": None, "mtd_py": None,
                         "mtd_forecast": None, "filled": False}
        return cells[at]

    units: dict[str, set[tuple[str, str]]] = {}
    #: What each slot's FR file divides by, which a Plan in whole currency is
    #: divided by to meet it.
    divisors: dict[tuple[str, str], float] = {}
    for location in LOCATIONS:
        for sl in SERVICE_LINES:
            source = fr_source(month, location, sl)
            if source is None:
                continue
            slots.add((location, sl))
            cover = source.get("cover", {})
            units.setdefault(" · ".join(filter(None, (cover.get("currency"),
                                                      cover.get("divisor")))),
                             set()).add((location, sl))
            divisors[(location, sl)] = divisor_of(cover.get("divisor"))
            for row in source["rows"]:
                reported.setdefault(norm(row["rs"]), set()).add((location, sl))
                got = cell(location, sl, row["rs"], row["line"])
                got["actual"] = row.get("actual")
                for measure in ("plan", "forecast", "prior_year", "full_year_plan",
                                "full_year_py"):
                    if row.get(measure) is not None:
                        got[measure] = row[measure]
                if row.get("prior_year") is not None:
                    got["py_from"] = "fr"

    whole = rules().get("plan_units", "whole") == "whole"

    def in_report_units(figure: float, slot: tuple[str, str], key: str) -> float:
        """A Plan figure in the units its slot's FR file reports in."""
        if whole and is_money(_line_name(key)):
            return figure / divisors.get(slot, 1.0)
        return figure

    #: (cell, field) whose month came straight from a monthly Plan or Forecast,
    #: so is not worked out again by subtracting last month's.
    direct: set[tuple[tuple, str]] = set()

    attributes, conflicts = mapping(month)

    def resolve(rs: str, location: str, sl: str) -> tuple[str, str] | None:
        """Where a segment nobody reported belongs: its file's word, then the mapping's."""
        attrs = attributes.get(norm(rs), {})
        where = location or attrs.get("location", "")
        line = sl or attrs.get("service_line", "")
        if where and not line:
            # Only one service line in play for that location settles it.
            held = [s for w, s in slots if w == where]
            line = held[0] if len(held) == 1 else ""
        return (where, line) if where and line else None

    # PLAN AND FORECAST. A segment with values here that FR left out is what
    # Finance inserts by hand today; it goes in with no actual, and is listed. The
    # FR file is not the segment master: the population is the union of Actual,
    # Plan, Forecast, prior year and valid master data.
    missing: dict[tuple[str, str, str], dict[str, Any]] = {}
    needs_location: dict[str, dict[str, Any]] = {}
    spread: set[str] = set()

    def adding(where: tuple[str, str], rs: str, why: str) -> dict[str, Any]:
        return missing.setdefault((*where, norm(rs)), {
            "location": where[0], "service_line": where[1], "rs": rs, "why": why, "lines": 0})

    def placed(got: dict[str, Any], slot: tuple[str, str], measure: str, key: str,
               figure: float, this_month: float | None) -> None:
        got[measure] = in_report_units(figure, slot, key)
        field = SCENARIOS[measure][1]
        if this_month is not None:
            got[field] = in_report_units(this_month, slot, key)
        if this_month is not None or monthly_plans():
            # Monthly columns with no column for this month: no MTD, rather
            # than this YTD less some other file's last month.
            direct.add(((*slot, norm(got["rs"]), key), field))

    for measure in ("plan", "forecast"):
        own_month = _scenario_month(measure, month)
        for (rs, key, location, sl), figure in _scenario(measure, month).items():
            this_month = own_month.get((rs, key, location, sl), 0.0 if own_month else None)
            segment = norm(rs)
            # The macro's key is segment and line, so a file that does not say
            # where goes to wherever that segment reported.
            targets = [t for t in reported.get(segment, set())
                       if (not location or t[0] == location) and (not sl or t[1] == sl)]
            if len(targets) > 1:
                spread.add(rs)
            if targets:
                for slot in targets:
                    placed(cell(*slot, rs, _named(key, measure, month)), slot, measure, key,
                           figure, this_month)
                continue
            where = resolve(rs, location, sl)
            if where is None:
                needs_location.setdefault(segment, {
                    "location": "", "service_line": "", "rs": rs, "lines": 0,
                    "why": "In the Plan or Forecast, absent from FR, and nothing says "
                           "which location it belongs to"})["lines"] += 1
                continue
            if where not in slots:
                continue
            got = cell(*where, rs, _named(key, measure, month))
            placed(got, where, measure, key, figure, this_month)
            got["filled"] = True
            adding(where, rs, "In the Plan or Forecast this month, absent from FR")["lines"] += 1

    # A segment with plan only in later months of this year has no actuals yet
    # and still needs its row, or it surprises everybody on the day it starts.
    source = active("plan", month)
    if source is not None:
        last = fy_start(month) + 11
        for row in source["rows"]:
            at = row.get("month")
            if at is None or not month < at <= last:
                continue
            segment = norm(row["rs"])
            if segment in reported:
                continue
            where = resolve(row["rs"], row.get("location", ""), row.get("service_line", ""))
            if where is None:
                if segment not in needs_location:
                    needs_location[segment] = {
                        "location": "", "service_line": "", "rs": row["rs"], "lines": 0,
                        "why": f"Planned from {month_label(at)}, and nothing says "
                               f"which location it belongs to"}
                continue
            if where not in slots:
                continue
            fresh = (*where, segment, line_key(row["line"])) not in cells
            cell(*where, row["rs"], row["line"])["filled"] = True
            entry = adding(where, row["rs"], f"Planned from {month_label(at)}, nothing to report yet")
            if fresh:
                entry["lines"] += 1

    # Full-year Plan comes from the fiscal-year column, or the twelve months.
    for (segment, key), figure in _full_year("plan", month).items():
        for at, got in cells.items():
            if at[2] == segment and at[3] == key:
                got["full_year_plan"] = in_report_units(figure, at[:2], key)

    # PRIOR YEAR. FR's own "PY at Plan Rate" first: last year restated at this
    # year's plan rate, which what was reported twelve months ago, at last
    # year's rate, is not. That record serves where FR carried no PY, and for a
    # segment only last year had.
    last_year, _py_slots, py_names = actuals_at(month - 12)
    for at, figure in last_year.items():
        location, sl, segment, key = at
        if at in cells:
            if cells[at]["prior_year"] is None:
                cells[at]["prior_year"] = figure
                cells[at]["py_from"] = "history"
        elif (location, sl) in slots and segment not in reported:
            got = cell(location, sl, py_names.get(segment, segment), py_names.get(key, key))
            got["prior_year"] = figure
            got["py_from"] = "history"
            got["filled"] = True
            adding((location, sl), got["rs"], "In last year's figures, nothing this year")["lines"] += 1

    # Master data is the last of the union: a mapped segment with a location,
    # and no figure anywhere this month.
    for segment, attrs in attributes.items():
        where = resolve(attrs["rs"], "", "")
        if where in slots and segment not in reported and (*where, segment) not in missing:
            missing[(*where, segment)] = {
                "location": where[0], "service_line": where[1], "rs": attrs["rs"], "lines": 0,
                "why": "In the mapping, no figures this month"}

    # THE CALCULATED LINES. The Plan does not store its subtotals, so they are
    # worked out per segment for each measure, where nothing was supplied.
    for measure in ("actual", "plan", "forecast", "prior_year", "full_year_plan", "full_year_py"):
        _fill_calculated(cells, cell, measure)

    # MTD. The first month of the year IS its YTD. Otherwise a slot with no
    # prior month has no MTD, because subtracting nothing would report the
    # whole year as one month and nobody would notice.
    first_month = month == fy_start(month)
    before, before_slots, _ = actuals_at(month - 1)
    before = dict(before)
    plan_before = _measure_before("plan", month - 1, cells, in_report_units)
    forecast_before = _measure_before("forecast", month - 1, cells, in_report_units)
    # Last month's PY from where this month's came from, so a month is never one
    # source's YTD less another's.
    py_from_fr = _measure_before("prior_year", month - 1, cells, in_report_units)
    py_from_history = actuals_at(month - 13)[0]
    no_prior = sorted(slots - before_slots) if not first_month else []
    for at, got in cells.items():
        rule = mtd_rule(got["line"])
        slot = (got["location"], got["service_line"])
        if got["actual"] is not None and rule != "pending":
            if rule == "closing" or first_month:
                got["mtd"] = got["actual"]
            elif slot in before_slots:
                got["mtd"] = got["actual"] - before.get(at, 0.0)
        if rule == "subtract":
            py_before = py_from_fr if got.get("py_from") == "fr" else py_from_history
            for measure, field, prior in (("plan", "mtd_plan", plan_before),
                                          ("forecast", "mtd_forecast", forecast_before),
                                          ("prior_year", "mtd_py", py_before)):
                if got[measure] is None or (at, field) in direct:
                    continue
                # No figure for last month is no MTD. Subtracting nothing would
                # present the whole year as a month.
                if first_month:
                    got[field] = got[measure]
                elif at in prior:
                    got[field] = got[measure] - prior[at]
    # A calculated line's month is its parts' month, whatever its class. Where a
    # subtraction above already gave one it is the same figure, and a percentage
    # could never have had one by subtracting.
    for field in ("mtd", "mtd_plan", "mtd_py", "mtd_forecast"):
        _fill_calculated(cells, cell, field, always=True)

    # The plain name a segment goes by across locations: "SOX" for each of
    # FR's "SOX_IN" and "SOX_PH". The mapping's word, else the Plan's, else FR's.
    plain: dict[str, str] = {}
    for kind in ("forecast", "plan"):
        source = active(kind, month)
        for row in (source["rows"] if source else []):
            if row.get("rs_name"):
                plain[norm(row["rs"])] = row["rs_name"]
    for got in cells.values():
        attrs = attributes.get(norm(got["rs"]), {})
        for field in ("type", "sub_service_line", "rate_card", "span", "competency", "mena"):
            got[field] = attrs.get(field, "")
        got["segment"] = attrs.get("rs_name") or plain.get(norm(got["rs"])) or got["rs"]
    every_rs = {norm(c["rs"]): c["rs"] for c in cells.values()}
    every_rs.update({k: v["rs"] for k, v in needs_location.items()})
    unmapped = sorted(r for k, r in every_rs.items()
                      if k not in attributes and k not in conflicts)

    if len(units) > 1:
        for said, where in sorted(units.items()):
            alerts.append({"id": f"units/{said}", "location": ", ".join(sorted(w[0] for w in where)),
                           "service_line": "", "change": "Files in different units",
                           "name": said or "Not stated", "can": []})
    for rs in sorted(spread):
        alerts.append({"id": f"spread/{rs}", "location": "", "service_line": "",
                       "change": "Plan with no location, segment in several",
                       "name": f"{rs}: its Plan is counted in each, as the macro would",
                       "can": []})
    for kind in ("plan", "forecast"):
        if gaps := _plan_gaps(kind, month):
            alerts.append({"id": f"gaps/{kind}", "location": "", "service_line": "",
                           "change": f"{SPECS[kind].label} short of months",
                           "name": f"No {SPECS[kind].label} names "
                                   f"{', '.join(month_label(m) for m in gaps)}, so its YTD "
                                   f"leaves {'it' if len(gaps) == 1 else 'them'} out",
                           "can": []})
    said_units = next(iter(units), "") if len(units) == 1 else ""
    return {
        "month": month,
        "cells": cells,
        "names": names,
        "slots": slots,
        "units": said_units,
        "divisor": divisor_of(said_units.split(" · ")[-1]) if said_units else 1.0,
        "missing": sorted(missing.values(),
                          key=lambda m: (m["location"], m["service_line"], m["rs"]))
                   + sorted(needs_location.values(), key=lambda m: m["rs"]),
        "unmapped": unmapped,
        "conflicts": sorted(conflicts.values()),
        "no_prior": no_prior,
        "alerts": alerts + _structure_alerts(month, cells, slots, before, before_slots, names),
    }


def _named(key: str, measure: str, month: int) -> str:
    """A line's words, from whichever row of the input said them."""
    def build():
        out: dict[str, str] = {}
        for row in _rows_of(measure, month):
            out.setdefault(line_key(row["line"]), row["line"])
        return out
    return _cached(("named", measure, month), build).get(key, key)


def _measure_before(measure: str, month: int, cells: dict[tuple, dict[str, Any]],
                    in_report_units: Any) -> dict[tuple, float]:
    """One measure as it stood last month, keyed the way this month's cells are.

    From last month's FR file, then its Plan or Forecast input, in the order this
    month's own figure was arrived at. A monthly Plan gives its month directly,
    so is not needed here.
    """
    out: dict[tuple, float] = {}
    for location, sl in {at[:2] for at in cells}:
        source = fr_source(month, location, sl)
        for row in (source["rows"] if source else []):
            if row.get(measure) is not None:
                out[(location, sl, norm(row["rs"]), line_key(row["line"]))] = row[measure]
    if measure in ("plan", "forecast") and not monthly_plans():
        by_segment: dict[tuple[str, str], float] = {}
        for (rs, key, _location, _sl), figure in _scenario(measure, month).items():
            at = (norm(rs), key)
            by_segment[at] = by_segment.get(at, 0.0) + figure
        out.update({at: in_report_units(by_segment[at[2:]], at[:2], at[3])
                    for at in cells if at[2:] in by_segment})
    return out


def _fill_calculated(cells: dict[tuple, dict[str, Any]], cell, measure: str,
                     *, always: bool = False) -> None:
    """Work out the calculated lines for one measure, segment by segment."""
    table = formulas()
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for at, got in cells.items():
        group = groups.setdefault(at[:3], {"rs": got["rs"], "values": {}, "added": True})
        group["values"][at[3]] = got[measure]
        group["added"] = group["added"] and got["filled"]
    for (location, sl, _segment), group in groups.items():
        values = dict(group["values"])
        apply_formulas(values, always=always)
        for key, figure in values.items():
            if figure is None or key not in table or figure == group["values"].get(key):
                continue
            got = cell(location, sl, group["rs"], table[key][0])
            got[measure] = figure
            # Worked out, not reported, so its absence from last month's file
            # is not a change of shape.
            if measure == "actual":
                got.setdefault("calculated", True)
            # A subtotal of a segment Compass added is part of what it added.
            got["filled"] = got["filled"] or group["added"]


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
        if got["actual"] is not None and not got.get("calculated"):
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


def _variance(actual: float | None, against: float | None, ratio: bool = False
              ) -> tuple[float | None, float | None]:
    """The difference, and as a percentage of what it is against.

    A percentage line's variance is in points, as the macro's `(D20-E20)*100`
    gives it: 25.0% against 27.5% is -2.5 pts, not -9.1%.
    """
    if actual is None or against is None:
        return None, None
    difference = actual - against
    if ratio:
        return difference, difference * 100
    return difference, (difference / abs(against) * 100 if against else None)


def in_location(got: dict[str, Any], location: str) -> bool:
    return not location or location == ALL or got["location"] == location


def summary(month: int, *, location: str = "", service_line: str = "",
            rs: str = "", reported_only: bool = False) -> list[dict[str, Any]]:
    """The P&L at some level: one row per line, in the order the files give them.

    Ratios do not add up across segments, so above one segment they are blank.
    A blank is honest; the sum of twelve utilisation percentages is not.
    """
    picked = [c for c in view(month)["cells"].values()
              if in_location(c, location)
              and (not service_line or c["service_line"] == service_line)
              and (not rs or norm(c["rs"]) == norm(rs))
              and not (reported_only and c["filled"])]
    by_line: dict[str, list[dict[str, Any]]] = {}
    for got in picked:
        by_line.setdefault(got["line_key"], []).append(got)
    # A percentage above one segment is worked out again from its parts at this
    # level, where a formula says how. Its parts were added up; it was not.
    ratios = {k for k, g in by_line.items() if len(g) > 1 and line_class(g[0]["line"]) == "ratio"}
    levelled: dict[str, dict[str, float | None]] = {}
    if ratios:
        for measure in ("actual", "plan", "forecast", "prior_year", "mtd", "full_year_plan",
                        "full_year_py", "mtd_plan", "mtd_py", "mtd_forecast"):
            values: dict[str, float | None] = {}
            for key, group in by_line.items():
                found = [g[measure] for g in group if g[measure] is not None]
                values[key] = sum(found) if found and key not in ratios else None
            apply_formulas(values, only=ratios)
            levelled[measure] = values
    out = []
    for key, group in by_line.items():
        name = group[0]["line"]
        ratio = line_class(name) == "ratio"

        def total(measure: str) -> float | None:
            if key in ratios:
                return _round(levelled[measure].get(key), ratio)
            found = [g[measure] for g in group if g[measure] is not None]
            return _round(sum(found), ratio) if found else None

        actual, plan, forecast = total("actual"), total("plan"), total("forecast")
        prior = total("prior_year")
        to_plan, to_plan_pct = _variance(actual, plan, ratio)
        to_forecast, to_forecast_pct = _variance(actual, forecast, ratio)
        to_prior, to_prior_pct = _variance(actual, prior, ratio)
        out.append({
            "id": key, "line": name, "class": line_class(name),
            "ytd": actual, "plan": plan,
            "vs_plan": _round(to_plan, ratio), "vs_plan_pct": _round(to_plan_pct),
            "forecast": forecast,
            "vs_forecast": _round(to_forecast, ratio),
            "vs_forecast_pct": _round(to_forecast_pct),
            "prior_year": prior,
            "vs_prior_year": _round(to_prior, ratio),
            "vs_prior_year_pct": _round(to_prior_pct),
            "full_year_plan": total("full_year_plan"),
            "full_year_py": total("full_year_py"),
            "mtd": total("mtd"),
            "mtd_plan": total("mtd_plan"),
            "mtd_forecast": total("mtd_forecast"),
            "mtd_py": total("mtd_py"),
            "can": [],
        })
    return out


def consolidated(month: int) -> tuple[list[str], list[dict[str, Any]]]:
    """Lines down, locations across, then the Pan-GDS total: the console sheet."""
    locations = [w for w in LOCATIONS if any(s[0] == w for s in view(month)["slots"])]
    rows = {r["id"]: dict(r, total=r["ytd"]) for r in summary(month)}
    for location in locations:
        for row in summary(month, location=location):
            rows[row["id"]][_at(location)] = row["ytd"]
    return locations, list(rows.values())


def _at(location: str) -> str:
    return "at_" + norm(location).replace(" ", "_")


def comparison(month: int, scenario: str, *, basis: str = "ytd", by: str = "rs"
               ) -> tuple[list[str], list[dict[str, Any]]]:
    """The RS or rate-card sheets: each segment's measures, by location, against one view.

    `by` is "rs" or "rate_card". Its location columns and total are this period's;
    the comparison and its variance are against Plan, prior year or Forecast.
    """
    return _cached(("comparison", month, scenario, basis, by),
                   lambda: _compare(month, scenario, basis, by))


def _compare(month: int, scenario: str, basis: str, by: str
             ) -> tuple[list[str], list[dict[str, Any]]]:
    field = "actual" if basis == "ytd" else "mtd"
    other = SCENARIOS[scenario][0 if basis == "ytd" else 1]
    wanted = [line_key(n) for n in ANALYSIS_LINES]
    table = {k: f for k, f in formulas().items() if k in wanted}
    ratios = {k for k in wanted if k in table and line_class(table[k][0]) == "ratio"}
    now = view(month)
    locations = [w for w in LOCATIONS if any(s[0] == w for s in now["slots"])]

    groups: dict[str, dict[str, Any]] = {}
    for got in now["cells"].values():
        # One row per segment by its plain name, each location a column: FR's
        # "SOX_IN" and "SOX_PH" are both SOX.
        name = got["segment"] if by == "rs" else (got["rate_card"] or "Not mapped")
        group = groups.setdefault(norm(name), {
            "name": name, "first": got, "here": {}, "there": {}, "names": {}, "cells": {},
            "mixed": set(), "against": {}})
        key = got["line_key"]
        group["names"].setdefault(key, got["line"])
        group["cells"][key] = got
        ratio = line_class(got["line"]) == "ratio"
        # Percentages of several segments do not add, so a rate card's come from
        # its formula or not at all.
        if by != "rs" and ratio:
            continue
        if got[field] is not None:
            here = group["here"].setdefault(got["location"], {})
            if ratio and key in here:
                group["mixed"].add((got["location"], key))
            here[key] = here.get(key, 0.0) + got[field]
        if got[other] is not None:
            group["there"][key] = group["there"].get(key, 0.0) + got[other]
            group["against"][key] = group["against"].get(key, 0) + 1

    rows = []
    for segment, group in sorted(groups.items()):
        # Two segments' percentages in one place were added above: worked out
        # again from their parts, or left blank.
        for location, key in group["mixed"]:
            group["here"][location].pop(key, None)
        for figures in group["here"].values():
            apply_formulas(figures, always=True, only=ratios)
        totals: dict[str, float | None] = {}
        for figures in group["here"].values():
            for key, figure in figures.items():
                if line_class(group["names"][key]) != "ratio" or len(group["here"]) == 1:
                    totals[key] = (totals.get(key) or 0.0) + figure
        apply_formulas(totals, always=True, only=ratios)
        there = {k: v for k, v in group["there"].items()
                 if line_class(group["names"][k]) != "ratio" or group["against"][k] == 1}
        apply_formulas(there, always=True, only=ratios)
        for key in wanted:
            if key not in group["names"] and key not in totals:
                continue
            total, against = totals.get(key), there.get(key)
            if total is None and against is None:
                continue
            ratio = line_class(group["names"].get(key, key)) == "ratio"
            variance, pct = _variance(total, against, ratio)
            row = {"id": f"{segment}/{key}", "metric": group["names"].get(key, key),
                   "class": "ratio" if ratio else "", "total": _round(total, ratio),
                   "against": _round(against, ratio), "variance": _round(variance, ratio),
                   "variance_pct": _round(pct), "can": []}
            if by == "rs":
                first = group["first"]
                row.update(rs=group["name"], type=first["type"],
                           sub_service_line=first["sub_service_line"],
                           rate_card=first["rate_card"], span=first["span"],
                           competency=first["competency"])
            else:
                row["rate_card"] = group["name"]
            for location in locations:
                row[_at(location)] = _round(group["here"].get(location, {}).get(key), ratio)
            rows.append(row)
    return locations, rows


def _role_keys() -> dict[str, set[str]]:
    return {role: {line_key(n) for n in names}
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
            "competency": got["competency"], "mena": got["mena"],
            "revenue": None, "direct_cost": None,
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
            "rs": got["rs"], "line": got["line"], "class": line_class(got["line"]),
            **{field: _round(got[measure], line_class(got["line"]) == "ratio")
               for field, measure in (("ytd", "actual"), ("plan", "plan"),
                                      ("forecast", "forecast"), ("prior_year", "prior_year"),
                                      ("full_year_plan", "full_year_plan"),
                                      ("full_year_py", "full_year_py"), ("mtd", "mtd"))},
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
    in_use = rules()["service_lines_in_use"]
    for location in LOCATIONS:
        for sl in SERVICE_LINES:
            source = fr_source(month, location, sl)
            # Only what is expected, and anything that has arrived regardless.
            if sl not in in_use and source is None:
                continue
            skip = skipped_slot(month, location, sl)
            about = reminder_about(month, location, sl)
            notice = notices.latest_for(about)
            if source:
                status = "Uploaded"
            elif skip:
                status = "Not required"
            else:
                status = "Missing"
            # The latest upload decides: a correction that failed its checks
            # leaves the slot waiting, whatever was there before.
            failed = _rejection("fr", month, location, sl)
            if status != "Not required" and failed and (
                    source is None or failed["at"] > source["at"]):
                status = "Failed checks"
            out.append({
                "id": f"{location}|{sl}", "location": location, "service_line": sl,
                "status": status,
                "file": source["filename"] if source else "",
                "version": version_label(source) if source else "",
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
            "check": check, "comparison": CHECKS[check], "category": category(line),
            # Only the standard three compare a line with itself; the others'
            # figures are shares, already in per cent.
            "class": line_class(line) if check in SCENARIOS else "",
            "location": got_location,
            "service_line": sl, "rs": rs, "line": line,
            "value": _round(value, line_class(line) == "ratio"),
            "against": _round(against, line_class(line) == "ratio"),
            "variance": _round(variance, line_class(line) == "ratio"),
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
            if (line_class(got["line"]) == "flow" and category(got["line"]) != "Operating measure"
                    and abs(difference) < picked["floor"]):
                continue
            # A percentage line moves in points, as the pack reports it.
            ratio = line_class(got["line"]) == "ratio"
            _, pct = _variance(actual, against, ratio)
            if pct is not None and abs(pct) <= limit:
                continue
            word = "over" if difference > 0 else "under"
            unit = " pts" if ratio else "%"
            raised(check, got["location"], got["service_line"], got["rs"], got["line"],
                   actual, against, variance=difference, pct=pct,
                   threshold=f"{limit:g}{unit}",
                   why=(f"{abs(pct):.1f}{unit} {word}, past the {limit:g}{unit} selected"
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
            divisor = view(month)["divisor"]
            for grade, count in worked.items():
                rate = rates.get((norm(rate_card), norm(grade)),
                                 rates.get((norm(rate_card), "")))
                if rate is None:
                    gap = f"Rate card {rate_card} has no rate for {grade or 'this segment'}"
                    break
                # Hours × rate is in whole currency; FR reports in its divisor.
                expected += count * rate / divisor
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

def _within(difference: float, against: float, *, money: bool = True) -> bool:
    """Whether a difference is small enough to call a match.

    The amount is in the report's units, so it means nothing for a count: an FTE
    short by one is not within a tolerance of one thousand dollars.
    """
    base = rules()
    share = abs(against) * base["recon_pct"] / 100
    return abs(difference) <= (max(base["recon_abs"], share) if money else share)


def reconciliation(month: int) -> dict[str, list[dict[str, Any]]]:
    return _cached(("recon", month), lambda: _reconcile(month))


#: How a Finance file names a measure, and which figure of ours that is.
_MEASURE_NAMES = {"": "ytd", "actual": "ytd", "ytd": "ytd", "plan": "plan",
                  "forecast": "forecast", "projection": "forecast", "py": "prior_year",
                  "prior year": "prior_year", "full year plan": "full_year_plan",
                  "fy plan": "full_year_plan", "full year py": "full_year_py",
                  "fy py": "full_year_py"}
_TOTAL_FIELDS = {"actual": ("ytd", "Actual"), "plan": ("plan", "Plan"),
                 "forecast": ("forecast", "Forecast"),
                 "prior_year": ("prior_year", "Prior Year"),
                 "full_year_plan": ("full_year_plan", "Full Year Plan"),
                 "full_year_py": ("full_year_py", "Full Year PY")}


def _reconcile(month: int) -> dict[str, list[dict[str, Any]]]:
    """The process's four layers, plus whether the calculated lines hold.

      segments      within a location's file, segments against Total Selected
      location      that file's Total Selected against its Consolidated column,
                    for every measure
      non_financial the operating measures against the RS-level breakout
      finance       selected measures against Finance's own workbook
      calculated    a subtotal FR reported against what its formula gives
    """
    now = view(month)
    by_slot = {slot: {r["id"]: r for r in summary(month, location=slot[0], service_line=slot[1])}
               for slot in now["slots"]}
    # A file's own totals cannot include a segment Compass added to it, so they
    # are compared with what the file reported. Finance's file is compared with
    # the whole result.
    as_reported = {slot: {r["id"]: r for r in summary(month, location=slot[0], service_line=slot[1],
                                                      reported_only=True)}
                   for slot in now["slots"]}
    non_financial = {line_key(n) for n in NON_FINANCIAL}

    segments, operating, location_rows = [], [], []
    for location, sl in sorted(now["slots"]):
        source = fr_source(month, location, sl)
        if source is None:
            continue
        ours = as_reported[(location, sl)]
        tallies: dict[str, list[float]] = {}
        for line, measures in source.get("totals", {}).items():
            key = line_key(line)
            row = ours.get(key)
            if row is None:
                continue
            for measure, figure in measures.items():
                field, _label = _TOTAL_FIELDS[measure]
                mine = row.get(field)
                if mine is None:
                    continue
                difference = mine - figure
                tallies.setdefault(measure, []).append(difference)
                if line_class(line) == "ratio" and not _within(difference, figure, money=False):
                    # A fraction is never more than a unit out, so the money
                    # tolerance below would pass any of them.
                    tallies.setdefault("_off_" + measure, []).append(difference)
                if measure != "actual":
                    continue
                listed = {
                    "id": f"{location}|{sl}|{key}", "location": location, "service_line": sl,
                    "parameter": line, "class": line_class(line),
                    "segments_total": _round(mine, line_class(line) == "ratio"),
                    "file_total": _round(figure, line_class(line) == "ratio"),
                    "difference": _round(difference, line_class(line) == "ratio"),
                    "status": "Matched" if _within(difference, figure,
                                                   money=key not in non_financial
                                                   and line_class(line) != "ratio")
                    else "Mismatched",
                    "can": []}
                (operating if key in non_financial else segments).append(listed)
        for measure, differences in tallies.items():
            if measure.startswith("_off_"):
                continue
            off = [d for d in differences if abs(d) > rules()["recon_abs"]]
            off += tallies.get("_off_" + measure, [])
            location_rows.append({
                "id": f"{location}|{sl}|{measure}", "location": location, "service_line": sl,
                "parameter": _TOTAL_FIELDS[measure][1], "lines": len(differences),
                "differing": len(off),
                "largest": _round(max(differences, key=abs)),
                "status": "Matched" if not off else "Mismatched", "can": []})

    against_finance = []
    for row in _rows_of("finance", month):
        slot = (row["location"], row["service_line"])
        key = line_key(row["parameter"])
        word = norm(row.get("scenario", ""))
        field = _MEASURE_NAMES.get(word, "ytd")
        ours = by_slot.get(slot, {}).get(key, {}).get(field)
        theirs = row["value"]
        if slot not in now["slots"]:
            status, difference = "No FR file", None
        elif ours is None:
            status, difference = "Not in the P&L", None
        else:
            difference = ours - theirs
            status = "Matched" if _within(difference, theirs) else "Mismatched"
        against_finance.append({
            "id": f"{slot[0]}|{slot[1]}|{key}|{field}", "location": slot[0],
            "service_line": slot[1], "parameter": row["parameter"],
            "measure": row.get("scenario", "") or "Actual",
            "compass": _round(ours), "finance": _round(theirs),
            "difference": _round(difference),
            "difference_pct": _round(difference / abs(theirs) * 100
                                     if difference is not None and theirs else None),
            "status": status, "can": [],
        })

    # A calculated line as reported, against the same line worked out from what
    # the file reported for its parts. Finds a formula that has drifted from the
    # layout, which is the macro's own weakness.
    calculated = []
    table = formulas()
    for slot in sorted(now["slots"]):
        rows = by_slot[slot]
        source = fr_source(month, *slot)
        stated = {line_key(r["line"]) for r in source["rows"]} if source else set()
        for key, (name, tree, refs) in table.items():
            if key not in stated or key not in rows:
                continue
            values = {k: r["ytd"] for k, r in rows.items()}
            figure = _evaluate(tree, refs, values)
            reported = rows[key]["ytd"]
            if figure is None or reported is None:
                continue
            difference = reported - figure
            calculated.append({
                "id": f"{slot[0]}|{slot[1]}|{key}", "location": slot[0], "service_line": slot[1],
                "parameter": name, "class": line_class(name),
                "reported": _round(reported, line_class(name) == "ratio"),
                "calculated": _round(figure, line_class(name) == "ratio"),
                "difference": _round(difference, line_class(name) == "ratio"),
                "status": "Matched" if _within(difference, figure,
                                               money=line_class(name) != "ratio")
                else "Mismatched",
                "can": []})
    return {"finance": against_finance, "segments": segments, "location": location_rows,
            "non_financial": operating, "calculated": calculated}


def recon_problems(month: int) -> int:
    return sum(1 for layer in reconciliation(month).values() for r in layer
               if r["status"] != "Matched")


def blockers(month: int) -> list[str]:
    """What stands between this month and a release, in words."""
    out = []
    if missing := missing_slots(month):
        out.append(f"{len(missing)} FR file{'' if len(missing) == 1 else 's'} missing")
    if failed := [r for r in coverage(month) if r["status"] == "Failed checks"]:
        out.append(f"{len(failed)} FR file{'' if len(failed) == 1 else 's'} failed "
                   f"{'its' if len(failed) == 1 else 'their'} checks")
    if active("finance", month) is None:
        out.append("no Finance consolidation to reconcile against")
    if problems := recon_problems(month):
        out.append(f"{problems} reconciliation difference{'' if problems == 1 else 's'}")
    if not step_done(month, "reconciliation"):
        out.append("the month-end run has not reached Reconciliation")
    return out


# ──────────────────────────────────────────────────────────────────────────
# the month-end run
# ──────────────────────────────────────────────────────────────────────────
#
# The preparer runs the month, not a schedule. Every file is checked as it
# arrives and one that fails is never used. Start is offered once every file
# the month needs has passed, and each step then works out its report and
# waits for Next. A file uploaded after Start puts the run out of date, because
# its reports were made from the files before it.

#: The steps, in the order the preparer goes through them: (id, name, what it makes).
STEPS = (
    ("locations", "Location-wise P&L",
     "Each location's P&L against Plan, Forecast and PY, YTD and MTD"),
    ("consolidated", "Consolidated P&L",
     "The locations consolidated into the Pan-GDS P&L"),
    ("service_lines", "Service line-wise P&L",
     "The month by service line"),
    ("analysis", "RS and rate card analysis",
     "Each segment and rate card against Plan, PY and Forecast"),
    ("reconciliation", "Reconciliation",
     "Segments, locations and operating measures against their totals and Finance's file"),
)
#: Made when somebody asks for it, once the locations have run.
EXCEPTIONS_STEP = ("exceptions", "Exception report",
                   "What breaches the thresholds, and the checks that could not run")
STEP_NAMES = {key: name for key, name, _ in (*STEPS, EXCEPTIONS_STEP)}
#: Inputs a month may also use. Not waited for, but one that failed its checks
#: still stops the run: a failed file is never quietly left out.
OPTIONAL_INPUTS = ("finance", "hours", "rates", "lines")


def record_rejection(kind: str, *, filename: str, raw: bytes, by: str, why: str,
                     month: int | None = None, location: str = "",
                     service_line: str = "") -> dict[str, Any]:
    """Keep the fact that a file failed its checks. Its figures are not kept.

    So the slot shows red with the reason until a corrected file arrives,
    rather than quietly carrying on with whatever was there before.
    """
    record = {
        "id": f"rejected-{uuid.uuid4().hex[:12]}", "type": "rejected", "kind": kind,
        "filed_for": (month or 0) if kind == "fr" or kind in FILED_BY_MONTH else 0,
        "location": location, "service_line": service_line, "filename": filename,
        "sha256": hashlib.sha256(raw).hexdigest(), "by": by, "at": time.time(), "why": why,
    }
    _put_record(record)
    log("rejected", by, f"{SPECS[kind].label} {filename} failed its checks: {why}",
        record["filed_for"] or month)
    return record


def _rejection(kind: str, month: int, location: str = "",
               service_line: str = "") -> dict[str, Any] | None:
    filed = month if kind == "fr" or kind in FILED_BY_MONTH else 0
    found = [r for r in _records.values() if r.get("type") == "rejected"
             and r["kind"] == kind and r["filed_for"] == filed
             and r["location"] == location and r["service_line"] == service_line]
    return max(found, key=lambda r: r["at"]) if found else None


def _in_use(kind: str, month: int, location: str = "",
            service_line: str = "") -> dict[str, Any] | None:
    if kind == "fr":
        return fr_source(month, location, service_line)
    return active(kind) if kind in PERIODLESS else active(kind, month)


def file_check(kind: str, month: int, location: str = "",
               service_line: str = "") -> dict[str, Any]:
    """Where one input stands for the month: passed, failed, or not there.

    The latest upload decides. A failure after a good file is a correction
    that did not work, so the slot waits for one that does.
    """
    source = _in_use(kind, month, location, service_line)
    failed = _rejection(kind, month, location, service_line)
    if failed and (source is None or failed["at"] > source["at"]):
        return {"status": "Failed checks", "why": failed["why"], "file": failed["filename"],
                "version": "", "by": failed["by"], "when": _day(failed["at"]), "passed": False}
    if source:
        return {"status": "Passed checks", "why": "", "file": source["filename"],
                "version": version_label(source), "by": source["by"],
                "when": _day(source["at"]), "passed": True}
    return {"status": "Not uploaded", "why": "", "file": "", "version": "", "by": "",
            "when": "", "passed": False}


def readiness(month: int) -> list[dict[str, Any]]:
    """Every file the month runs on, and whether it has passed its checks."""
    out = []
    for slot in coverage(month):
        where = (slot["location"], slot["service_line"])
        if slot["status"] == "Not required":
            check = {"status": "Not required", "why": slot["note"], "file": "", "version": "",
                     "by": slot["by"], "when": "", "passed": True}
        else:
            check = file_check("fr", month, *where)
        out.append({"id": f"fr|{where[0]}|{where[1]}", "input": "FR file",
                    "for_what": f"{where[0]} · {where[1]}", "required": "Required",
                    "kind": "fr", **check})
    wanted = rules().get("required_inputs", [])
    for kind in (*wanted, *(k for k in OPTIONAL_INPUTS if k not in wanted)):
        check = file_check(kind, month)
        needed = kind in wanted
        if not needed and check["status"] == "Not uploaded":
            check = dict(check, status="Optional, not uploaded", passed=True)
        out.append({"id": kind, "input": SPECS[kind].label, "for_what": month_label(month)
                    if kind in FILED_BY_MONTH else "Every month",
                    "required": "Required" if needed else "Optional", "kind": kind, **check})
    return out


def start_blockers(month: int) -> list[str]:
    """Why Start is not offered yet, in words. Empty when it is."""
    rows = readiness(month)
    out = []
    if failed := [r for r in rows if r["status"] == "Failed checks"]:
        out.append(f"{len(failed)} file{'' if len(failed) == 1 else 's'} failed "
                   f"{'its' if len(failed) == 1 else 'their'} checks: "
                   + ", ".join(r["for_what"] if r["kind"] == "fr" else r["input"]
                               for r in failed[:4]) + ("…" if len(failed) > 4 else ""))
    if absent := [r for r in rows if r["status"] == "Not uploaded"]:
        out.append(f"{len(absent)} not uploaded: "
                   + ", ".join(r["for_what"] if r["kind"] == "fr" else r["input"]
                               for r in absent[:4]) + ("…" if len(absent) > 4 else ""))
    return out


def _inputs_of_run(month: int) -> list[str]:
    """What a run is made from: the version of every input it reads."""
    ids = [r["id"] for r in (fr_source(month, s["location"], s["service_line"])
                             for s in coverage(month)) if r]
    for kind in (*rules().get("required_inputs", []), *OPTIONAL_INPUTS, "history"):
        if source := _in_use(kind, month):
            ids.append(source["id"])
    return sorted(set(ids))


def current_run(month: int) -> dict[str, Any] | None:
    runs = [r for r in _records.values() if r.get("type") == "run" and r["month"] == month]
    return max(runs, key=lambda r: r["at"]) if runs else None


def run_is_stale(month: int) -> bool:
    """Whether a file arrived after Start, so the reports are of files no longer used."""
    run = current_run(month)
    return run is not None and run["inputs"] != _inputs_of_run(month)


def step_done(month: int, step: str) -> bool:
    """Whether this month's run has made this report, from the files in use now."""
    run = current_run(month)
    return run is not None and step in run["steps"] and not run_is_stale(month)


def next_step(month: int) -> str | None:
    """The step Next would run, once a run has started. None when they have all run."""
    run = current_run(month)
    if run is None or run_is_stale(month):
        return None
    return next((key for key, _name, _what in STEPS if key not in run["steps"]), None)


def start_run(month: int, *, by: str) -> dict[str, Any]:
    """Start the month: check the files once more, then make the first report."""
    if blocked := start_blockers(month):
        raise ValueError("Not every file has passed its checks yet. " + "; ".join(blocked) + ".")
    run = {"id": f"run-{month}-{time.time_ns()}", "type": "run", "month": month,
           "by": by, "at": time.time(), "inputs": _inputs_of_run(month), "steps": {}}
    _put_record(run)
    log("run_started", by, f"Month-end run started for {month_label(month)}", month)
    return run_step(month, STEPS[0][0], by=by)


def run_step(month: int, step: str, *, by: str) -> dict[str, Any]:
    """Make one step's report. Only the next one, and only from the files Start used."""
    run = current_run(month)
    if run is None or run_is_stale(month):
        raise ValueError("Start the month-end run first: "
                         + ("a file has changed since it started." if run else
                            "nothing has been run for this month."))
    if step == EXCEPTIONS_STEP[0]:
        if STEPS[0][0] not in run["steps"]:
            raise ValueError(f"The exception report needs the {STEPS[0][1]} first.")
    elif step != next_step(month):
        expected = next_step(month)
        raise ValueError(f"{STEP_NAMES.get(step, step)} is not next. "
                         + (f"Next is {STEP_NAMES[expected]}." if expected else
                            "Every step has run."))
    said = _make(month, step)
    fresh = dict(run, steps=dict(run["steps"], **{step: {"by": by, "at": time.time(),
                                                         "said": said}}))
    _put_record(fresh)
    log("run_step", by, f"{STEP_NAMES[step]}, {month_label(month)}: {said}", month)
    return fresh


def _make(month: int, step: str) -> str:
    """Work out one step's report now, and say what it came to."""
    now = view(month)
    if step == "locations":
        places = sorted({w for w, _sl in now["slots"]})
        lines = max((len(summary(month, location=w)) for w in places), default=0)
        return f"{len(places)} location P&Ls, up to {lines} lines each"
    if step == "consolidated":
        places, rows = consolidated(month)
        return f"Pan-GDS from {len(places)} locations, {len(rows)} lines"
    if step == "service_lines":
        rows = slot_rollup(month, "service_line")
        return f"{len(rows)} service line{'' if len(rows) == 1 else 's'}"
    if step == "analysis":
        for scenario in SCENARIOS:
            comparison(month, scenario)
            comparison(month, scenario, by="rate_card")
        segments_seen = {r["rs"] for r in comparison(month, "plan")[1]}
        cards = {r["rate_card"] for r in comparison(month, "plan", by="rate_card")[1]}
        return f"{len(segments_seen)} segments, {len(cards)} rate cards"
    if step == "reconciliation":
        problems = recon_problems(month)
        return ("Everything matched" if not problems else
                f"{problems} difference{'' if problems == 1 else 's'} to look at")
    if step == "exceptions":
        found = exceptions(month)
        return (f"{len(found['found'])} exceptions, {len(found['untested'])} checks "
                f"could not run")
    raise ValueError(f"No step called {step!r}.")


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
    for kind in PERIODLESS:
        if source := active(kind):
            used.append(source)
    return [{"id": s["id"], "kind": s["label"], "file": s["filename"],
             "version": version_label(s), "by": s["by"], "sha256": s["sha256"]}
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
        "units": view(month)["units"],
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


#: A percentage line's differences: fractions apart, so points once multiplied.
_POINT_GAPS = frozenset({"vs_plan", "vs_forecast", "vs_prior_year", "variance", "difference"})


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
            ratio = row.get("class") == "ratio"
            for c, (key, _label) in enumerate(columns, start=1):
                value = row.get(key)
                # A percentage line keeps Excel's fraction, formatted as one, and
                # its variance is in points as the pack has it.
                if ratio and isinstance(value, float) and key in _POINT_GAPS:
                    value *= 100
                cell = ws.cell(row=r, column=c, value=value)
                if not isinstance(value, float):
                    continue
                if ratio and (key.endswith("_pct") or key in _POINT_GAPS):
                    cell.number_format = '+0.0" pts";-0.0" pts"'
                elif ratio:
                    cell.number_format = "0.0%"
                else:
                    cell.number_format = "#,##0.00"
        ws.freeze_panes = "A2"

    lines = [("line", "P&L line"), ("ytd", "YTD Actual"), ("plan", "YTD Plan"),
             ("vs_plan", "vs Plan"), ("vs_plan_pct", "vs Plan %"),
             ("forecast", "YTD Forecast"), ("vs_forecast_pct", "vs Forecast %"),
             ("prior_year", "Prior Year"), ("vs_prior_year_pct", "vs Prior Year %"),
             ("full_year_plan", "Full Year Plan"), ("full_year_py", "Full Year PY")]
    if "Consolidated" in sheets:
        where, rows = consolidated(month)
        chosen = [w for w in where if not locations or w in locations]
        sheet("Consolidated",
              [("line", "P&L line"), *[(_at(w), w) for w in chosen], ("total", "Pan-GDS"),
               ("plan", "YTD Plan"), ("vs_plan_pct", "vs Plan %"),
               ("prior_year", "Prior Year"), ("vs_prior_year_pct", "vs Prior Year %")], rows)
    if "Location P&Ls" in sheets:
        for location in locations or list(LOCATIONS):
            if rows := summary(month, location=location):
                sheet(location, lines, rows)
    if "MTD P&L" in sheets:
        sheet("MTD P&L",
              [("line", "P&L line"), ("mtd", "MTD Actual"), ("mtd_plan", "MTD Plan"),
               ("mtd_forecast", "MTD Forecast"), ("mtd_py", "MTD Prior Year")], summary(month))
    if "RS wise analysis" in sheets:
        sheet("RS wise analysis",
              [("type", "Type"), ("sub_service_line", "SSL"), ("rate_card", "Rate Card"),
               ("rs", "Reporting Segment"), ("span", "Span"), ("competency", "Competencies"),
               ("location", "Location"), ("service_line", "Service line"),
               ("revenue", "Revenue"), ("direct_cost", "Direct cost"), ("fte", "FTE")],
              [r for r in segments(month)
               if not locations or r["location"] in locations])
    for title, scenario, basis, by in (
            ("RS YTD vs Plan", "plan", "ytd", "rs"), ("RS YTD vs PY", "prior_year", "ytd", "rs"),
            ("RS YTD vs Forecast", "forecast", "ytd", "rs"), ("RS MTD vs Plan", "plan", "mtd", "rs"),
            ("RS MTD vs PY", "prior_year", "mtd", "rs"),
            ("Rate card YTD vs Plan", "plan", "ytd", "rate_card")):
        if title not in sheets:
            continue
        where, rows = comparison(month, scenario, basis=basis, by=by)
        head = ([("type", "Type"), ("sub_service_line", "SSL"), ("rate_card", "Rate Card"),
                 ("rs", "Reporting Segment"), ("span", "Span"), ("competency", "Competencies")]
                if by == "rs" else [("rate_card", "Rate Card")])
        sheet(title, [*head, ("metric", "Measure"),
                      *[(_at(w), w) for w in where if not locations or w in locations],
                      ("total", "Total"), ("against", SCENARIOS[scenario][2]),
                      ("variance", "Variance"), ("variance_pct", "Variance %")], rows)
    if "Exception report" in sheets:
        sheet("Exceptions",
              [("comparison", "Comparison"), ("category", "Category"), ("location", "Location"),
               ("service_line", "Service line"), ("rs", "RS"), ("line", "P&L line"),
               ("value", "Value"), ("against", "Against"), ("variance", "Variance"),
               ("variance_pct", "Variance %"), ("threshold", "Threshold"), ("why", "Why")],
              [r for r in exceptions(month)["found"]
               if not locations or r["location"] in locations])
    if "Reconciliation" in sheets:
        sheet("Reconciliation",
              [("location", "Location"), ("service_line", "Service line"),
               ("parameter", "Parameter"), ("measure", "Measure"), ("compass", "P&L"),
               ("finance", "Finance"), ("difference", "Difference"), ("status", "Status")],
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
