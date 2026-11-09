"""Reading Finance's spreadsheets into rows. Records nothing.

The FR report is the difficult one, and the requirement says why: its layout
moves. Lines appear and disappear, a Reporting Segment comes as a block with
its identifier above it or as a column, and the process today breaks whenever
a row is not where a macro expected it. So NOTHING here reads a position. A
sheet is scanned for the row that names the columns this kind of file needs,
and from then on a value is found by what its column is called.

The FR report itself comes as REPEATED COLUMN BLOCKS: P&L lines down the side,
and across the top a merged Reporting Segment heading over the same measures
each time (Actual at Plan Rate, Plan, variance, prior year, Full Year Plan, Full
Year PY), with a "Total Selected" block among them. Its Cover Sheet says which
fiscal year, period, currency and divisor it is. The macro finds its way by
searching the top rows for Total Selected, and so does this.

Compass has been told about that layout and not shown a file, so three simpler
shapes are understood too, for what people export by hand:

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
#: How far down the macro looks for Total Selected.
ANCHOR_SCAN = 10
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
    # The name FR's own column headings use, with its location code on the end:
    # "FAAS Core_AR" where the plain column says "FAAS Core". The macro matches
    # on this one, so a file that has both is keyed on it.
    "rs_fr": frozenset({"reporting segment fr", "rs fr", "fr reporting segment",
                        "fr rs", "reporting segment fr name"}),
    "line": frozenset({"p&l line", "p&l line item", "p&l item", "pnl line",
                       "pl line", "line", "line item", "account", "particulars",
                       "description", "fr p&l", "fr p&l description",
                       "p&l description"}),
    "section": frozenset({"section", "category", "group"}),
    "actual": frozenset({"actual", "actuals", "ytd actual", "actual ytd", "ytd",
                         "current year", "cy", "cy ytd", "current ytd",
                         "actual at plan rate", "actual at plan rates"}),
    "plan": frozenset({"plan", "ytd plan", "plan ytd", "budget"}),
    "forecast": frozenset({"forecast", "fcst", "ytd forecast", "forecast ytd",
                           "projection", "ytd projection"}),
    # "PY at Plan Rate" is the real report's heading: last year restated at this
    # year's plan rate, which is why it, and not last year's own Actual, is PY.
    "prior_year": frozenset({"prior year", "py", "py ytd", "ytd py", "last year",
                             "previous year", "py at plan rate", "py at plan rates",
                             "prior year at plan rate", "ytd py at plan rate"}),
    "full_year_plan": frozenset({"full year plan", "fy plan"}),
    "full_year_py": frozenset({"full year py", "fy py", "full year prior year"}),
    "scenario": frozenset({"scenario", "basis", "measure type"}),
    # The Plan database's own columns: which fiscal year a row's months are in,
    # and whether the row counts at all.
    "year": frozenset({"year", "fiscal year", "fy"}),
    "scope": frozenset({"scope", "in scope"}),
    "mena": frozenset({"mena", "mena non mena", "mena group"}),
    "line_id": frozenset({"line id", "line code", "line key"}),
    "alias": frozenset({"alias", "aliases", "also called", "old description"}),
    "class": frozenset({"class", "behaviour", "behavior"}),
    "formula": frozenset({"formula", "calculation"}),
    "value": frozenset({"value", "amount", "finance value", "finance"}),
    "parameter": frozenset({"parameter", "metric", "measure", "p&l line", "line"}),
    "type": frozenset({"type", "rs type"}),
    "sub_service_line": frozenset({"sub service line", "sub-service line",
                                   "sub service-line", "ssl", "sub service line ssl"}),
    "rate_card": frozenset({"rate card", "ratecard"}),
    "span": frozenset({"span"}),
    "competency": frozenset({"competency", "competencies"}),
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


#: The measures an FR report can carry against one segment and line.
MEASURES = ("actual", "plan", "forecast", "prior_year", "full_year_plan", "full_year_py")

#: `fr` needs a line and nothing else here, because its segment and its figure
#: may be carried by the layout; `_read_fr` checks those itself.
SPECS: dict[str, Spec] = {
    "fr": Spec("FR P&L file", ("line",),
               ("rs", "section", "actual", "plan", "forecast", "prior_year",
                "full_year_plan", "full_year_py", "period", "location", "service_line"),
               MEASURES),
    "mapping": Spec("RS mapping", ("rs",),
                    ("rs_fr", "type", "sub_service_line", "rate_card", "span", "competency",
                     "mena", "location", "service_line", "effective_from")),
    # The macro needs only these two and the period's columns: the match is
    # Reporting Segment + FR P&L. Location is taken when the file has it, and
    # the reader adds the figure itself, because it is read two ways.
    "plan": Spec("Plan database", ("rs", "line"),
                 ("rs_fr", "location", "service_line", "period", "plan", "year", "scope",
                  "scenario"), ("plan",)),
    "forecast": Spec("Forecast", ("rs", "line"),
                     ("rs_fr", "location", "service_line", "period", "forecast", "year", "scope",
                      "scenario"), ("forecast",)),
    "lines": Spec("P&L line master", ("line",),
                  ("line_id", "alias", "class", "section", "formula")),
    "history": Spec("Prior-period history",
                    ("period", "location", "service_line", "rs", "line", "actual"),
                    ("section",), ("actual",)),
    "finance": Spec("Finance consolidation",
                    ("location", "service_line", "parameter", "value"),
                    ("period", "scenario"), ("value",)),
    "hours": Spec("Charged hours", ("location", "service_line", "rs", "hours"),
                  ("period", "grade"), ("hours",)),
    "rates": Spec("Rate card", ("rate_card", "rate"), ("grade",), ("rate",)),
}

#: A segment called one of these is the file's own total.
_TOTALS = frozenset({"total", "grand total", "total rs", "all", "all rs",
                     "location total", "sl total", "service line total",
                     "total selected"})

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
    #: The file's own totals, by line and measure: {"Revenue": {"actual": 1200.0}}.
    totals: dict[str, dict[str, float]] = field(default_factory=dict)
    #: What its Cover Sheet says: fiscal_year, period, period_type, currency, divisor.
    cover: dict[str, str] = field(default_factory=dict)
    #: Cells holding an Excel error such as #REF!. Never read as a figure, and
    #: never dropped quietly: the workbook behind this has several.
    errors: int = 0
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


def divisor_of(text: Any) -> float:
    """What the report's figures are divided by. "Thousands" is 1000."""
    said = norm(text)
    for word, value in (("thousand", 1e3), ("lakh", 1e5), ("million", 1e6), ("crore", 1e7)):
        if word in said:
            return value
    figure = number(text)
    return figure if figure and figure > 0 else 1.0


def calendar_month(period: Any, fiscal_year: Any, first_month: int) -> int | None:
    """"August" in "FY27" as a real month, given when the year starts.

    A month name on its own is what the Cover Sheet and the Plan's columns say,
    and which calendar year it falls in depends on where the fiscal year begins.
    """
    found = re.search(r"([A-Za-z]{3})", str(period or ""))
    year = re.search(r"(\d{4}|\d{2})\s*$", str(fiscal_year or "").strip())
    if not found or not year:
        return None
    try:
        month = MONTHS.index(found[1].capitalize()) + 1
    except ValueError:
        return None
    end = int(year[1]) + (2000 if len(year[1]) == 2 else 0)
    # A year starting in January is named for that year; any other is named for
    # the year it ends in, so its earlier months fall in the year before.
    calendar = end if first_month == 1 or month < first_month else end - 1
    return month_key(calendar, month)


def is_error(cell: Any) -> bool:
    return isinstance(cell, str) and cell.strip().startswith("#") and cell.strip().endswith(("!", "?", "A"))


def _read_cover(grids: list[tuple[str, list[list[Any]]]]) -> dict[str, str]:
    """The report's own parameters, from a label and the cell beside it."""
    labels = (("fiscal_year", re.compile(r"^(fiscal year|year|fy)\b")),
              ("period_type", re.compile(r"^(period type|view|ytd mtd|ytd)\b")),
              ("period", re.compile(r"^(period|month|reporting period)\b")),
              ("currency", re.compile(r"^(currency|ccy)\b")),
              ("divisor", re.compile(r"^(divisor|scale|units?)\b")))
    out: dict[str, str] = {}
    for title, grid in grids:
        if "cover" not in norm(title):
            continue
        for row in grid:
            filled = [c for c in row if c not in (None, "")]
            if len(filled) < 2:
                continue
            said = norm(filled[0]).rstrip(" :")
            for key, pattern in labels:
                if key not in out and pattern.match(said):
                    out[key] = clean(filled[1])
                    break
    return out


def _read_repeated(out: Parsed, grid: list[list[Any]]) -> bool:
    """The real report: Total Selected at the top, blocks of measures across.

    Merged headings arrive with the value in their first cell and nothing in the
    rest, so a segment's name is carried right until the next one. A column is
    then known by its segment and its measure, wherever it sits, and a column
    whose heading means nothing here (a variance) is simply not read: Compass
    works those out itself.
    """
    anchor = None
    for at, row in enumerate(grid[:ANCHOR_SCAN]):
        if any(norm(c) == "total selected" for c in row):
            anchor = at
            break
    if anchor is None:
        return False
    measures = tuple(MEASURES)
    labels: dict[int, str] = {}
    start = anchor
    for at in range(anchor, min(anchor + 4, len(grid))):
        found = {c: m for c, cell in enumerate(grid[at])
                 for m in measures if norm(cell) in ALIASES[m]}
        if len(found) >= 2:
            labels, start = found, at
            break
    if not labels:
        return False

    # Carried right from the anchor row, so a heading merged across a block
    # reaches every column under it.
    heads: dict[int, str] = {}
    current = ""
    widest = max(len(grid[anchor]), max(labels) + 1)
    for column in range(widest):
        cell = grid[anchor][column] if column < len(grid[anchor]) else None
        if cell not in (None, ""):
            current = clean(cell)
        heads[column] = current
    first = min(c for c in labels if heads.get(c))

    heading = ""
    for row in grid[start + 1:]:
        line = next((clean(c) for c in row[:first] if c not in (None, "")), "")
        if not line:
            continue
        by_segment: dict[str, dict[str, float]] = {}
        for column, measure in labels.items():
            segment = heads.get(column, "")
            if not segment or column >= len(row):
                continue
            cell = row[column]
            if is_error(cell):
                out.errors += 1
                continue
            figure = number(cell)
            if figure is not None:
                by_segment.setdefault(segment, {})[measure] = figure
        # A line with no figure anywhere is a heading. Remembered, because the
        # same description under two headings is two different lines.
        if not by_segment:
            heading = line
            continue
        for segment, figures in by_segment.items():
            _keep(out, {"rs": segment, "line": line, "section": heading,
                        "period": None, "location": "", "service_line": "", **figures})
    return True


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
            if all(_has(columns, need) for need in spec.needs):
                out.sheets.append(title)
                _records(out, spec, columns, grid[at + 1:])
                break
    if not out.sheets:
        missing = [n for n in spec.needs if not _has(closest, n)]
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
        for idea in columns:
            if idea not in spec.numbers and idea != "period" and idea != "effective_from":
                record[idea] = clean(record[idea])
        _key_on_fr_name(record)
        # A row missing an identifier or its figure is a heading, a spacer or
        # a footnote. Counted, because a real one lost here is a wrong total.
        if any(record.get(n) in (None, "") for n in spec.needs):
            out.skipped += 1
            continue
        out.rows.append(record)


def _has(columns: dict[str, int], need: str) -> bool:
    """Whether a heading row gives this idea. FR's segment name is a segment name."""
    return need in columns or (need == "rs" and "rs_fr" in columns)


def _key_on_fr_name(record: dict[str, Any]) -> None:
    """Key a row on the name FR's headings use, keeping the plain one beside it.

    The Plan database carries both, "FAAS Core_AR" and "FAAS Core". Matched on
    the plain one, nothing in it would meet FR, whose blocks are headed with the
    first; the plain one is what the RS sheets group by, across locations.
    """
    fr_name = record.pop("rs_fr", None)
    if fr_name in (None, ""):
        return
    plain = record.get("rs")
    record["rs"] = fr_name
    if plain not in (None, ""):
        record["rs_name"] = plain


def _read_fr(spec: Spec, grids: list[tuple[str, list[list[Any]]]]) -> Parsed:
    """The FR report, in whichever of its three shapes this one came in."""
    out = Parsed("fr", cover=_read_cover(grids))
    wanted = tuple(dict.fromkeys(spec.needs + spec.may))
    for title, grid in grids:
        if "cover" in norm(title):
            continue
        if _read_repeated(out, grid):
            out.sheets.append(title)
            continue
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

    out.errors += sum(1 for k in MEASURES if is_error(at(k)))
    figures = {k: number(at(k)) for k in MEASURES}
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
        figures = {m: float(row[m]) for m in MEASURES if row.get(m) is not None}
        if figures:
            out.totals.setdefault(row["line"], {}).update(figures)
        return
    for measure in MEASURES:
        row.setdefault(measure, None)
    out.rows.append(row)


_FISCAL = re.compile(r"^(?:fy|fiscal year|full year)?\s*(\d{4}|\d{2})(?:\s*full year)?$")
_MONTH_WORDS = frozenset({
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december"})
_MONTH_NAME = re.compile(r"^(?:ytd\s*)?([a-z]{3,9})(?:\s*(\d{2}|\d{4}))?$")


def _scenario_columns(row: list[Any], first_month: int
                      ) -> tuple[dict[int, tuple[str, int]], dict[int, str]]:
    """Which columns of a Plan or Forecast hold a month, and which a whole year."""
    found: list[tuple[int, str, str]] = []
    years: dict[int, str] = {}
    for at, cell in enumerate(row):
        said = norm(cell)
        if not said:
            continue
        if isinstance(cell, (dt.date, dt.datetime)):
            months = cell.strftime("%b %Y").lower()
            found.append((at, months[:3], months[-4:]))
            continue
        if (fiscal := _FISCAL.match(said)) and str(cell).strip().upper().startswith(("FY", "FULL")):
            years[at] = fiscal[1]
            continue
        # A whole word only: "Marketing" is not March.
        if (named := _MONTH_NAME.match(said)) and named[1] in _MONTH_WORDS:
            found.append((at, named[1][:3], named[2] or ""))
    default_year = next(iter(years.values()), "") if len(set(years.values())) == 1 else ""
    months: dict[int, tuple[str, int]] = {}
    for at, name, year in found:
        months[at] = (name, 0)
        if year:
            key = parse_month(f"{name} {year}")
        else:
            key = calendar_month(name, default_year, first_month) if default_year else None
        if key is not None:
            months[at] = (name, key)
    return months, years


def _read_scenario(kind: str, spec: Spec, grids: list[tuple[str, list[list[Any]]]],
                   *, first_month: int, month: int | None) -> Parsed:
    """The Plan database, or a Forecast, the way the macro reads one.

    It finds its columns from a heading row: Reporting Segment, FR P&L, the
    month and the fiscal year. So a file here has one column per month and one
    per year; a single value column with a Period beside it reads the same.
    Rows repeating a segment and line are added up, as the macro's conditional
    aggregation does, and the text is cleaned on the way in.

    The Plan database's month columns are bare ("Jul" to "Jun"), and each row's
    Year says which fiscal year they are. A row whose Scope is No does not
    count, a row whose FR P&L is NA is no FR line (Additions, Resignation,
    Transfers), and a file holding several scenarios gives only this one.
    """
    out = Parsed(kind)
    wanted = ("rs_fr", "rs", "line", "location", "service_line", "period", kind, "year",
              "scope", "scenario")
    closest: dict[str, int] = {}
    tally = {"na": 0, "out_of_scope": 0}
    for title, grid in grids:
        if "cover" in norm(title):
            continue
        for at, row in enumerate(grid[:HEADER_SCAN]):
            columns = _columns(row, wanted)
            if len(columns) > len(closest):
                closest = columns
            if not _has(columns, "rs") or "line" not in columns:
                continue
            months, years = _scenario_columns(row, first_month)
            if kind not in columns and not months and not years:
                continue
            out.sheets.append(title)
            for record in grid[at + 1:]:
                _scenario_rows(out, kind, columns, months, years, record, month,
                               first_month, tally)
            break
    if not out.sheets:
        missing = [n for n in ("rs", "line") if not _has(closest, n)]
        raise Unreadable(
            f"No sheet here has the columns a {spec.label} needs"
            + (f". Missing: {', '.join(_said(m) for m in missing)}." if missing else
               ": a column for each month, or a value column with a Period."))
    _one_scenario(out, kind)
    if tally["na"]:
        out.notes.append(f"{_rows(tally['na'])} whose FR P&L is NA left out: no FR report "
                         f"line is called that.")
    if tally["out_of_scope"]:
        out.notes.append(f"{_rows(tally['out_of_scope'])} marked out of scope left out.")
    return out


def _rows(count: int) -> str:
    return f"{count} row" + ("" if count == 1 else "s")


#: What a Scenario column calls each kind of file.
_SCENARIO_WORDS = {
    "plan": frozenset({"plan", "budget", "annual plan", "ap", "plan ytd"}),
    "forecast": frozenset({"forecast", "fcst", "projection", "outlook", "reforecast",
                           "re forecast", "rf", "latest estimate", "le"}),
}
#: A Scope saying one of these takes the row out.
_OUT_OF_SCOPE = frozenset({"no", "n", "false", "0", "out", "out of scope", "excluded"})
#: An FR P&L naming no FR line.
_NO_LINE = frozenset({"na", "n a", "not applicable", "none"})


def _one_scenario(out: Parsed, kind: str) -> None:
    """Keep this kind's rows when the file holds several scenarios.

    One scenario throughout is taken as filed, whatever it is called: the file
    the macro reads as its Plan input is the one it writes Forecast columns
    from, and which it is here is Finance's to say, not a word in a column.
    """
    said = {norm(r["scenario"]) for r in out.rows if r.get("scenario")}
    if len(said) > 1:
        kept = [r for r in out.rows if norm(r.get("scenario", "")) in _SCENARIO_WORDS[kind]]
        if not kept:
            found = sorted({r["scenario"] for r in out.rows if r.get("scenario")})
            raise Unreadable(f"This file holds the scenarios {', '.join(found)}, and none of "
                             f"them is a {SPECS[kind].label}. Upload it as the one it is.")
        out.notes.append(f"{_rows(len(out.rows) - len(kept))} of other scenarios left out.")
        out.rows = kept
    for row in out.rows:
        row.pop("scenario", None)


def _scenario_rows(out: Parsed, kind: str, columns: dict[str, int],
                   months: dict[int, tuple[str, int]], years: dict[int, str],
                   record: list[Any], chosen: int | None, first_month: int,
                   tally: dict[str, int]) -> None:
    def at(idea: str) -> Any:
        spot = columns.get(idea)
        return record[spot] if spot is not None and spot < len(record) else None

    names = {"rs": clean(at("rs")), "rs_fr": clean(at("rs_fr"))}
    _key_on_fr_name(names)
    segment, line = names["rs"], clean(at("line"))
    if not segment or not line:
        if any(c not in (None, "") for c in record):
            out.skipped += 1
        return
    if norm(line) in _NO_LINE:
        tally["na"] += 1
        return
    if norm(at("scope")) in _OUT_OF_SCOPE:
        tally["out_of_scope"] += 1
        return
    base = {"rs": segment, "line": line, "location": clean(at("location")),
            "service_line": clean(at("service_line"))}
    if names.get("rs_name"):
        base["rs_name"] = names["rs_name"]
    if "scenario" in columns:
        base["scenario"] = clean(at("scenario"))
    row_year = clean(at("year"))
    if kind in columns:
        cell = at(kind)
        out.errors += is_error(cell)
        figure = number(cell)
        if figure is not None:
            out.rows.append(dict(base, period=at("period"), **{kind: figure}))
    for spot, (name, key) in months.items():
        cell = record[spot] if spot < len(record) else None
        out.errors += is_error(cell)
        figure = number(cell)
        if figure is None:
            continue
        # A bare month is dated by the row's own Year. With no year anywhere it
        # is the month somebody chose, if that is the month it names, and
        # nothing otherwise.
        if not key and row_year:
            key = calendar_month(name, row_year, first_month) or 0
        if not key and chosen is not None and MONTHS[chosen % 12].lower() == name:
            key = chosen
        if key:
            out.rows.append(dict(base, period=key, **{kind: figure}))
    for spot, year in years.items():
        cell = record[spot] if spot < len(record) else None
        out.errors += is_error(cell)
        figure = number(cell)
        if figure is not None:
            out.rows.append(dict(base, fiscal_year=year, full_year=figure))


def read(kind: str, filename: str, raw: bytes, *, first_month: int = 7,
         month: int | None = None) -> Parsed:
    """One file, as rows. Raises `Unreadable` saying what is wrong with it."""
    spec = SPECS.get(kind)
    if spec is None:
        raise Unreadable(f"Compass does not take a file of kind {kind!r}.")
    grids = _grids(filename, raw)
    if not grids:
        raise Unreadable(f"{filename} has no visible sheets with anything on them.")
    if kind == "fr":
        parsed = _read_fr(spec, grids)
    elif kind in ("plan", "forecast"):
        parsed = _read_scenario(kind, spec, grids, first_month=first_month, month=month)
    else:
        parsed = _read_plain(kind, spec, grids)
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
