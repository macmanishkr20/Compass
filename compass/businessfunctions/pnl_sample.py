"""A sample month, so the P&L screens can be read before a real file exists.

Written as spreadsheets and read through the same reader a real upload goes
through, so the sample cannot show anything a real file could not. Held in
memory only and never stored; the first real file ends it (see `pnl.ready`).

Every figure is invented. The trouble is planted, one of each kind the screens
exist to find, and `scripts/check_businessfunctions.py` asserts that each one
is found, which makes this the test data as well as the demonstration.

Also usable as files: `python3 scripts/make_pnl_samples.py <folder>` writes
what this builds, for trying the upload forms by hand.
"""

from __future__ import annotations

import io
from typing import Any

from compass.businessfunctions import pnl, pnl_files
from compass.businessfunctions.pnl_files import month_key, month_label

OCTOBER = month_key(2026, 10)
SEPTEMBER = month_key(2026, 9)
LAST_OCTOBER = month_key(2025, 10)

#: Which slots have sent a file. Two have not, so the coverage screen and the
#: reminder have something to show.
FILED = [(loc, sl) for loc in pnl.LOCATIONS for sl in pnl.SERVICE_LINES
         if (loc, sl) not in {("Noida", "Tax"), ("Kochi", "Assurance")}]

#: Three segments per slot, named so a reader can see whose they are.
def _segments(location: str, service_line: str) -> list[str]:
    return [f"{location[:3].upper()}-{service_line[:3].upper()}-{n}" for n in (101, 102, 103)]


#: One line's YTD October figure for a segment of ordinary size.
_LINES: list[tuple[str, float]] = [
    ("Revenue", 5_000_000.0),
    ("Base Compensation", 1_800_000.0),
    ("Variable Pay", 270_000.0),
    ("Employee Benefits", 180_000.0),
    ("Direct Cost", 600_000.0),
    ("Indirect Cost", 450_000.0),
    ("Charged Hours", 2_500.0),
    ("FTE", 12.0),
    ("Utilisation %", 78.0),
]

#: (segment, line, multiplier on the actual). The planted trouble.
_PLANTED = {
    ("BEN-CON-101", "Indirect Cost"): 1.40,   # far over plan, last year, forecast
    ("HYD-TAX-102", "Revenue"): 0.70,         # far under, and fails revenue validation
    ("HYD-CON-103", "Variable Pay"): 1.90,    # past the Variable Pay share
    ("NOI-CON-101", "Employee Benefits"): 1.60,  # past the benefits benchmark
    ("KOC-TAX-101", "Direct Cost"): 1.70,     # past Direct Cost to Revenue
}


def _seed(*parts: str) -> float:
    """A small, stable wobble, so the rows do not all read the same."""
    total = sum(ord(c) * (i + 1) for i, c in enumerate("".join(parts)))
    return 1 + ((total % 7) - 3) / 100


def _figure(rs: str, line: str, base: float, *, scale: float = 1.0) -> float:
    if line in ("FTE", "Utilisation %"):
        return float(round(base * _seed(rs, line), 1))
    return float(round(base * _seed(rs, line) * scale))


def _actual(rs: str, line: str, base: float, *, scale: float = 1.0) -> float:
    return float(round(_figure(rs, line, base, scale=scale) * _PLANTED.get((rs, line), 1.0), 1))


def _xlsx(sheets: dict[str, list[list[Any]]]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        ws = book.create_sheet(title)
        for row in rows:
            ws.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def fr_file(month: int, location: str, service_line: str) -> bytes:
    """An FR report, in the block shape: a title, then one block per segment."""
    # September's YTD is nine-tenths of October's, near enough for a sample.
    scale = 1.0 if month == OCTOBER else 0.9
    rows: list[list[Any]] = [
        [f"FR P&L report: {location} · {service_line}"],
        [f"Reporting period: {month_label(month)}"],
        [],
    ]
    totals: dict[str, float] = {}
    for rs in _segments(location, service_line):
        rows.append([f"Reporting Segment: {rs}"])
        rows.append(["P&L line", "YTD Actual"])
        for line, base in _LINES:
            value = (_actual(rs, line, base, scale=scale) if month == OCTOBER
                     else _figure(rs, line, base, scale=scale))
            rows.append([line, value])
            if line != "Utilisation %":
                totals[line] = totals.get(line, 0.0) + value
        rows.append([])
    # A new line appears in one location this month, and consolidation must
    # carry it through and say so.
    if month == OCTOBER and (location, service_line) == ("Hyderabad", "Consulting"):
        rs = _segments(location, service_line)[0]
        rows.insert(rows.index([f"Reporting Segment: {rs}"]) + 2, ["Investment Hours", 140.0])
        totals["Investment Hours"] = 140.0
    rows.append(["Reporting Segment: Total"])
    rows.append(["P&L line", "YTD Actual"])
    for line, value in totals.items():
        rows.append([line, value])
    return _xlsx({"P&L": rows})


def _long(heading: list[str], rows: list[list[Any]]) -> bytes:
    return _xlsx({"Data": [heading, *rows]})


def plan_file() -> bytes:
    rows = []
    for location, service_line in FILED:
        for rs in _segments(location, service_line):
            for line, base in _LINES:
                if line != "Utilisation %":
                    rows.append([month_label(OCTOBER), location, service_line, rs, line,
                                 _figure(rs, line, base)])
    # Planned for this month and absent from FR: what is inserted by hand today.
    for line, base in _LINES[:5]:
        rows.append([month_label(OCTOBER), "Bengaluru", "Consulting", "BEN-CON-NEW", line,
                     base * 0.2])
    # Planned from January and nothing to report yet: still needs its row.
    for line, base in _LINES[:5]:
        rows.append(["Jan 2027", "Noida", "Consulting", "NOI-CON-FUT", line, base * 0.1])
    return _long(["Period", "Location", "Service line", "Reporting Segment",
                  "P&L line", "Plan"], rows)


def forecast_file() -> bytes:
    rows = []
    for location, service_line in FILED:
        for rs in _segments(location, service_line):
            for line, base in _LINES:
                if line != "Utilisation %":
                    rows.append([month_label(OCTOBER), location, service_line, rs, line,
                                 round(_figure(rs, line, base) * 1.02)])
    return _long(["Period", "Location", "Service line", "Reporting Segment",
                  "P&L line", "Forecast"], rows)


def history_file() -> bytes:
    """Last October, as the one-time load. Everything is a little smaller."""
    rows = []
    for location, service_line in FILED:
        for rs in _segments(location, service_line):
            for line, base in _LINES:
                if line != "Utilisation %":
                    rows.append([month_label(LAST_OCTOBER), location, service_line, rs,
                                 line, round(_figure(rs, line, base) * 0.96)])
    return _long(["Period", "Location", "Service line", "Reporting Segment",
                  "P&L line", "Actual"], rows)


def mapping_file() -> bytes:
    rows = []
    for location, service_line in FILED:
        for n, rs in enumerate(_segments(location, service_line)):
            # One segment nobody has mapped, so the gap report is not empty.
            if rs == "KOC-CON-103":
                continue
            rows.append([rs, ("Core", "Growth", "Support")[n], f"{service_line} Delivery",
                         f"{service_line} Card", f"1:{4 + n}", "Digital"])
    # Two rows, two answers: Finance's one-to-many rule to make.
    rows.append(["NOI-CON-102", "Growth", "Tech", "Tax Card", "1:9", "Data"])
    return _long(["Reporting Segment", "Type", "Sub service line", "Rate card",
                  "Span", "Competency"], rows)


def hours_file() -> bytes:
    rows = []
    for location, service_line in FILED:
        for rs in _segments(location, service_line):
            # Charged hours × the card's rate is near the revenue reported,
            # for every segment but the one whose revenue was planted short.
            rows.append([month_label(OCTOBER), location, service_line, rs,
                         _figure(rs, "Revenue", 5_000_000.0) / 2_000])
    return _long(["Period", "Location", "Service line", "Reporting Segment",
                  "Charged hours"], rows)


def rates_file() -> bytes:
    return _long(["Rate card", "Rate"],
                 [[f"{sl} Card", 2_000] for sl in pnl.SERVICE_LINES])


def finance_file() -> bytes:
    """Finance's own consolidation. Agrees with FR except for one parameter."""
    rows = []
    for location, service_line in FILED:
        for line, base in (("Revenue", 5_000_000.0), ("Direct Cost", 600_000.0)):
            total = sum(_actual(rs, line, base)
                        for rs in _segments(location, service_line))
            if (location, service_line, line) == ("Bengaluru", "Tax", "Revenue"):
                total += 250_000
            rows.append([month_label(OCTOBER), location, service_line, line, total])
    return _long(["Period", "Location", "Service line", "Parameter", "Value"], rows)


def files() -> dict[str, tuple[str, bytes]]:
    """Every master by kind, as a filename and its bytes."""
    return {
        "plan": ("plan-database.xlsx", plan_file()),
        "forecast": ("forecast.xlsx", forecast_file()),
        "history": ("prior-period-history.xlsx", history_file()),
        "mapping": ("rs-mapping.xlsx", mapping_file()),
        "hours": ("charged-hours.xlsx", hours_file()),
        "rates": ("rate-card.xlsx", rates_file()),
        "finance": ("finance-consolidation.xlsx", finance_file()),
    }


def load() -> None:
    """Put the sample month in the engine, through the real reader."""
    for month in (SEPTEMBER, OCTOBER):
        for location, service_line in FILED:
            name = f"FR {location} {service_line} {month_label(month)}.xlsx"
            raw = fr_file(month, location, service_line)
            pnl.record_source("fr", pnl_files.read("fr", name, raw), filename=name, raw=raw,
                              by="sample", month=month, location=location,
                              service_line=service_line)
    for kind, (name, raw) in files().items():
        pnl.record_source(kind, pnl_files.read(kind, name, raw), filename=name, raw=raw,
                          by="sample")
