"""Reading Finance's spreadsheets into rows. Records nothing.

The FR report is the difficult one, and the requirement says why: its layout
moves. Lines appear and disappear, a Reporting Segment comes as a block with
its identifier above it or as a column, and the process today breaks whenever
a row is not where a macro expected it. So NOTHING here reads a position. A
sheet is scanned for the row that names the columns this kind of file needs,
and from then on a value is found by what its column is called.

Three shapes of FR file are understood, because Compass has been told about
the process and has not yet been shown the file:

    long      a column for the segment, one for the line, one per measure
    blocks    "Reporting Segment: RS-101" above each block of lines
    crosstab  lines down the side, one column per segment

A TOTAL IS NOT A SEGMENT. A row or column called "Total" is kept apart: added
to the others it would double every figure in the consolidation, and it is
also the one thing that lets Compass check the RS-level breakup against the
location's own figure.

A REFUSAL NAMES THE PROBLEM. This is somebody's month-end file and they are
going to fix it and try again, so "could not read" helps nobody and "no column
called Reporting Segment on any sheet" does.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from typing import Any

#: How far down a sheet a heading may be. FR reports open with a title block;
#: nothing puts its columns further down than this.
HEADER_SCAN = 60
#: One file. Past this it is not a month's P&L for one location.
MAX_ROWS = 200_000

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: What each column may be called, normalised. Finance's own files use several
#: of these for one idea, and "Actual" and "YTD" are the same figure here.
ALIASES: dict[str, frozenset[str]] = {
    "period": frozenset({"period", "month", "reporting period", "reporting month"}),
    "location": frozenset({"location", "gds location", "office", "city"}),
    "service_line": frozenset({"service line", "serviceline", "sl"}),
    "rs": frozenset({"reporting segment", "reporting segment id", "rs", "rs id",
                     "rs code", "segment"}),
    "line": frozenset({"p&l line", "p&l line item", "p&l item", "pnl line",
                       "pl line", "line", "line item", "account", "particulars",
                       "description"}),
    "section": frozenset({"section", "category", "group"}),
    "actual": frozenset({"actual", "actuals", "ytd actual", "actual ytd", "ytd",
                         "current year", "cy", "cy ytd", "current ytd"}),
    "plan": frozenset({"plan", "ytd plan", "plan ytd", "budget"}),
    "forecast": frozenset({"forecast", "fcst", "ytd forecast", "forecast ytd"}),
    "prior_year": frozenset({"prior year", "py", "py ytd", "ytd py", "last year"}),
    "value": frozenset({"value", "amount", "finance value", "finance"}),
    "parameter": frozenset({"parameter", "metric", "measure", "p&l line", "line"}),
    "type": frozenset({"type", "rs type"}),
    "sub_service_line": frozenset({"sub service line", "sub-service line",
                                   "sub service-line", "ssl"}),
    "rate_card": frozenset({"rate card", "ratecard"}),
    "span": frozenset({"span"}),
    "competency": frozenset({"competency"}),
    "effective_from": frozenset({"effective from", "effective"}),
    "grade": frozenset({"grade", "rank", "level"}),
    "hours": frozenset({"hours", "charged hours", "chargeable hours"}),
    "rate": frozenset({"rate", "hourly rate", "bill rate"}),
}


@dataclass(frozen=True)
class Spec:
    """What one kind of file must and may hold."""

    label: str
    #: Columns without which the file means nothing.
    needs: tuple[str, ...]
    may: tuple[str, ...] = ()
    #: Columns read as numbers. Everything else is an identifier.
    numbers: tuple[str, ...] = ()


#: `fr` needs a line and nothing else here, because its segment and its figure
#: may be carried by the layout; `_read_fr` checks those itself.
SPECS: dict[str, Spec] = {
    "fr": Spec("FR P&L file", ("line",),
               ("rs", "section", "actual", "plan", "forecast", "prior_year",
                "period", "location", "service_line"),
               ("actual", "plan", "forecast", "prior_year")),
    "mapping": Spec("RS mapping", ("rs",),
                    ("type", "sub_service_line", "rate_card", "span",
                     "competency", "effective_from")),
    "plan": Spec("Plan database", ("location", "service_line", "rs", "line", "plan"),
                 ("period", "section"), ("plan",)),
    "forecast": Spec("Forecast", ("location", "service_line", "rs", "line", "forecast"),
                     ("period", "section"), ("forecast",)),
    "history": Spec("Prior-period history",
                    ("period", "location", "service_line", "rs", "line", "actual"),
                    ("section",), ("actual",)),
    "finance": Spec("Finance consolidation",
                    ("location", "service_line", "parameter", "value"),
                    ("period",), ("value",)),
    "hours": Spec("Charged hours", ("location", "service_line", "rs", "hours"),
                  ("period", "grade"), ("hours",)),
    "rates": Spec("Rate card", ("rate_card", "rate"), ("grade",), ("rate",)),
}

#: A segment called one of these is the file's own total.
_TOTALS = frozenset({"total", "grand total", "total rs", "all", "all rs",
                     "location total", "sl total", "service line total"})

_BLOCK = re.compile(r"^\s*(?:reporting\s+segment|rs)(?:\s+id)?\s*[:\-–]\s*(.+?)\s*$",
                    re.IGNORECASE)
_NOT_WORD = re.compile(r"[^a-z0-9&%]+")


class Unreadable(ValueError):
    """Why a file cannot be used, in words its owner can act on."""


@dataclass
class Parsed:
    """What a file turned out to hold."""

    kind: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: The file's own totals, by line: {"revenue": 1200.0}.
    totals: dict[str, float] = field(default_factory=dict)
    sheets: list[str] = field(default_factory=list)
    #: Rows that looked like data and were left out, and why. Said to the
    #: person who uploaded it rather than dropped quietly.
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


def norm(text: Any) -> str:
    """A label reduced to what it says: case, spacing and punctuation gone."""
    return _NOT_WORD.sub(" ", str(text or "").strip().lower()).strip()


def clean(text: Any) -> str:
    """An identifier, made safe to write back into a spreadsheet.

    A segment called `=HYPERLINK(...)` would be a formula in the workbook
    Compass hands leadership, and the leadership file is the one thing that
    must carry no links.
    """
    return re.sub(r"\s+", " ", str(text if text is not None else "")).strip().lstrip("=+@\t")


def number(value: Any) -> float | None:
    """A figure, however a finance sheet happened to write it.

    None means there is no figure, which is not the same as zero: a plan
    nobody set must not read as a plan of nothing, or every actual against it
    is an infinite variance.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "–", "—", "n/a", "N/A", "NA", "#N/A"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace("₹", "").replace("%", "").strip()
    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def month_key(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def month_label(key: int) -> str:
    return f"{MONTHS[key % 12]} {key // 12}"


def parse_month(value: Any) -> int | None:
    """A reporting month as a sortable number, or None.

    Periods are not hard-coded anywhere, so this is what stands between
    "2026-10", "Oct-26", "October 2026" and a real date all being one month.
    """
    if isinstance(value, (dt.datetime, dt.date)):
        return month_key(value.year, value.month)
    text = str(value or "").strip()
    if found := re.fullmatch(r"(\d{4})[-/](\d{1,2})(?:[-/]\d{1,2})?", text):
        year, month = int(found[1]), int(found[2])
    elif found := re.fullmatch(r"(\d{1,2})[-/](\d{4})", text):
        year, month = int(found[2]), int(found[1])
    elif found := re.fullmatch(r"([A-Za-z]{3,9})[\s\-/']*(\d{2}|\d{4})", text):
        try:
            month = MONTHS.index(found[1][:3].capitalize()) + 1
        except ValueError:
            return None
        year = int(found[2])
        year = year + 2000 if year < 100 else year
    else:
        return None
    return month_key(year, month) if 1 <= month <= 12 else None


def _grids(filename: str, raw: bytes) -> list[tuple[str, list[list[Any]]]]:
    """Every sheet as a grid of cells, trailing blanks and all."""
    name = filename.lower()
    if name.endswith(".csv"):
        text = raw.decode("utf-8-sig", "replace")
        return [("csv", [list(r) for r in csv.reader(io.StringIO(text))][:MAX_ROWS])]
    if not name.endswith((".xlsx", ".xlsm")):
        raise Unreadable(f"{filename} is not a spreadsheet Compass can read. "
                         f"Save it as .xlsx or .csv and try again.")
    from openpyxl import load_workbook

    try:
        # data_only: the figures Excel last calculated, never the formulas. A
        # file with no cached values says so below rather than reading as zero.
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as err:  # noqa: BLE001 — openpyxl raises many shapes of "no"
        raise Unreadable(f"{filename} would not open as a workbook. If Excel "
                         f"opens it, save a fresh copy as .xlsx.") from err
    grids = []
    for sheet in book.worksheets:
        if sheet.sheet_state != "visible":
            continue
        grid = [list(r) for r in sheet.iter_rows(values_only=True, max_row=MAX_ROWS)]
        if grid:
            grids.append((sheet.title, grid))
    book.close()
    return grids


def _columns(row: list[Any], wanted: tuple[str, ...]) -> dict[str, int]:
    """Which column holds which idea, if this row is a row of headings."""
    found: dict[str, int] = {}
    for at, cell in enumerate(row):
        said = norm(cell)
        if not said:
            continue
        for idea in wanted:
            if idea not in found and said in ALIASES[idea]:
                found[idea] = at
                break
    return found


def _read_plain(kind: str, spec: Spec, grids: list[tuple[str, list[list[Any]]]]) -> Parsed:
    """A master: one heading row, then records, on whichever sheets have them."""
    out = Parsed(kind)
    wanted = tuple(dict.fromkeys(spec.needs + spec.may))
    closest: dict[str, int] = {}
    for title, grid in grids:
        for at, row in enumerate(grid[:HEADER_SCAN]):
            columns = _columns(row, wanted)
            if len(columns) > len(closest):
                closest = columns
            if all(need in columns for need in spec.needs):
                out.sheets.append(title)
                _records(out, spec, columns, grid[at + 1:])
                break
    if not out.sheets:
        missing = [n for n in spec.needs if n not in closest]
        raise Unreadable(f"No sheet here has the columns a {spec.label} needs. "
                         f"Missing: {', '.join(_said(m) for m in missing)}.")
    return out


def _said(idea: str) -> str:
    return {"rs": "Reporting Segment", "line": "P&L line",
            "service_line": "Service line", "rate_card": "Rate card",
            }.get(idea, idea.replace("_", " ").capitalize())


def _records(out: Parsed, spec: Spec, columns: dict[str, int],
             rows: list[list[Any]]) -> None:
    for row in rows:
        record: dict[str, Any] = {}
        for idea, at in columns.items():
            cell = row[at] if at < len(row) else None
            record[idea] = number(cell) if idea in spec.numbers else cell
        if not any(v not in (None, "") for v in record.values()):
            continue
        # A row missing an identifier or its figure is a heading, a spacer or
        # a footnote. Counted, because a real one lost here is a wrong total.
        if any(record.get(n) in (None, "") for n in spec.needs):
            out.skipped += 1
            continue
        for idea in columns:
            if idea not in spec.numbers and idea != "period" and idea != "effective_from":
                record[idea] = clean(record[idea])
        out.rows.append(record)


def _read_fr(spec: Spec, grids: list[tuple[str, list[list[Any]]]]) -> Parsed:
    """The FR report, in whichever of its three shapes this one came in."""
    out = Parsed("fr")
    wanted = tuple(dict.fromkeys(spec.needs + spec.may))
    for title, grid in grids:
        columns: dict[str, int] = {}
        crosstab: dict[int, str] = {}
        block = ""
        used = False
        for row in grid:
            filled = [c for c in row if c not in (None, "")]
            if not filled:
                continue
            # A marker stands alone on its row, and only means anything where
            # no column already says which segment a row is. Without both
            # conditions the data row "RS-101 | Revenue | 500" reads as one.
            if "rs" not in columns:
                if len(filled) == 1 and (found := _BLOCK.match(str(filled[0]))):
                    block = clean(found[1])
                    continue
                # "Reporting Segment | RS-101" across two cells is the same.
                if (len(filled) == 2 and norm(filled[0]) in ALIASES["rs"]
                        and norm(filled[1]) not in ALIASES["line"]):
                    block = clean(filled[1])
                    continue
            heading = _columns(row, wanted)
            if "line" in heading and len(heading) + _others(row, heading) >= 2:
                columns = heading
                # Neither a segment column nor a figure column: then the
                # other columns ARE the segments.
                crosstab = {}
                if "rs" not in columns and "actual" not in columns:
                    crosstab = {at: clean(c) for at, c in enumerate(row)
                                if c not in (None, "") and at not in columns.values()}
                used = True
                continue
            if not columns:
                continue
            _fr_row(out, columns, crosstab, block, row)
        if used:
            out.sheets.append(title)
    if not out.sheets:
        raise Unreadable("No sheet here has a column for the P&L line, so this "
                         "does not look like an FR report.")
    if not out.rows:
        raise Unreadable(
            "Found the P&L lines but no figures against a Reporting Segment. "
            "Compass needs a Reporting Segment column, an 'RS: <id>' row above "
            "each block, or one column per segment. If the figures are formulas, "
            "open the file in Excel and save it so their values are stored.")
    return out


def _others(row: list[Any], heading: dict[str, int]) -> int:
    """How many other labelled cells a candidate heading row has."""
    return sum(1 for at, c in enumerate(row)
               if c not in (None, "") and at not in heading.values()
               and not isinstance(c, (int, float)))


def _fr_row(out: Parsed, columns: dict[str, int], crosstab: dict[int, str],
            block: str, row: list[Any]) -> None:
    def at(idea: str) -> Any:
        spot = columns.get(idea)
        return row[spot] if spot is not None and spot < len(row) else None

    line = clean(at("line"))
    if not line:
        return
    base = {"line": line, "section": clean(at("section")),
            "period": at("period"), "location": clean(at("location")),
            "service_line": clean(at("service_line"))}

    if crosstab:
        for spot, segment in crosstab.items():
            figure = number(row[spot]) if spot < len(row) else None
            if figure is not None:
                _keep(out, dict(base, rs=segment, actual=figure))
        return

    figures = {k: number(at(k)) for k in ("actual", "plan", "forecast", "prior_year")}
    if all(v is None for v in figures.values()):
        # A section heading: a line with no figure anywhere on the row.
        return
    segment = clean(at("rs")) or block
    if not segment:
        out.skipped += 1
        return
    _keep(out, dict(base, rs=segment, **figures))


def _keep(out: Parsed, row: dict[str, Any]) -> None:
    if norm(row["rs"]) in _TOTALS:
        if row.get("actual") is not None:
            out.totals[row["line"]] = float(row["actual"])
        return
    row.setdefault("plan", None)
    row.setdefault("forecast", None)
    row.setdefault("prior_year", None)
    out.rows.append(row)


def read(kind: str, filename: str, raw: bytes) -> Parsed:
    """One file, as rows. Raises `Unreadable` saying what is wrong with it."""
    spec = SPECS.get(kind)
    if spec is None:
        raise Unreadable(f"Compass does not take a file of kind {kind!r}.")
    grids = _grids(filename, raw)
    if not grids:
        raise Unreadable(f"{filename} has no visible sheets with anything on them.")
    parsed = _read_fr(spec, grids) if kind == "fr" else _read_plain(kind, spec, grids)
    if kind == "fr":
        _refuse_repeats(parsed)
    return parsed


def _refuse_repeats(parsed: Parsed) -> None:
    """One segment listing one line twice, with two different figures.

    Adding them would be a guess and so would taking either. Usually it is a
    line name used under two sections, which a Section column settles: such a
    line is named with its section from here on, and every other line keeps
    the plain name the rules and the Finance file know it by.
    """
    sections: dict[tuple[str, str], set[str]] = {}
    for row in parsed.rows:
        sections.setdefault((norm(row["rs"]), norm(row["line"])), set()).add(
            norm(row.get("section", "")))
    shared = {line for (_rs, line), found in sections.items() if len(found) > 1}

    seen: dict[tuple[str, str], Any] = {}
    kept = []
    for row in parsed.rows:
        if norm(row["line"]) in shared and row.get("section"):
            row["line"] = f"{row['section']} · {row['line']}"
        key = (norm(row["rs"]), norm(row["line"]))
        if key in seen:
            if seen[key] != row.get("actual"):
                raise Unreadable(
                    f"{row['rs']} lists '{row['line']}' twice with different "
                    f"figures, so Compass cannot tell which is right. If they "
                    f"sit under different headings, add a Section column.")
            parsed.skipped += 1
            continue
        seen[key] = row.get("actual")
        kept.append(row)
    parsed.rows = kept
