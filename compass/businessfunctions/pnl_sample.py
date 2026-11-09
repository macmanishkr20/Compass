"""A sample month, so the P&L screens can be read before a real file exists.

Written as spreadsheets and read through the same reader a real upload goes
through, so the sample cannot show anything a real file could not. Held in
memory only and never stored; the first real file ends it (see `pnl.ready`).

THE FILES COPY THE SHAPES SEEN IN PHOTOGRAPHS OF FINANCE'S FILES, NOT THE FILES
THEMSELVES, which Compass has not had: an FR report with a Cover Sheet and seven
columns per Reporting Segment block around Total Selected, and a Plan database
laid out as Plan 27 Data is (Year, Scenario, Scope, both segment names, FR P&L,
then each month's own amount in whole currency), storing detail lines and
leaving the subtotals to be worked out.

Every figure is invented. The trouble is planted, one of each kind the screens
exist to find, and `scripts/check_businessfunctions.py` asserts that each one is
found, which makes this the test data as well as the demonstration.

Also usable as files: `python3 scripts/make_pnl_samples.py <folder>` writes what
this builds, for trying the upload forms by hand.
"""

from __future__ import annotations

import io
from typing import Any

from compass.businessfunctions import pnl, pnl_files
from compass.businessfunctions.pnl_files import month_key, month_label

SEPTEMBER = month_key(2026, 9)
AUGUST = month_key(2026, 8)
LAST_SEPTEMBER = month_key(2025, 9)

#: Consulting is the reference model. Two locations have not filed, so the
#: coverage screen and the reminder have something to show.
FILED = [(loc, "Consulting") for loc in pnl.LOCATIONS
         if loc not in {"Hungary", "Consulting MENA"}]

#: How each location is abbreviated in a segment's name.
_CODES = {"India": "IND", "Argentina": "ARG", "Hungary": "HUN", "Mexico": "MEX",
          "Philippines": "PHL", "Poland": "POL", "Spain": "ESP", "Sri Lanka": "LKA",
          "Consulting MENA": "MEN"}


def segments(location: str) -> list[str]:
    """Each location's segments as FR's headings name them: "IND-101"."""
    return [f"{_CODES[location]}-{n}" for n in (101, 102, 103)]


#: What a segment is called across locations, as the Plan's plain Reporting
#: Segment column says: IND-101 and ARG-101 are both Technology Risk.
PLAIN = {101: "Technology Risk", 102: "Process & Controls", 103: "Cyber Security"}


def plain_name(rs: str) -> str:
    number = rs.rsplit("-", 1)[-1]
    return PLAIN.get(int(number), rs) if number.isdigit() else rs


#: One detail line's YTD September figure for a segment of ordinary size, in
#: thousands. These are what the Plan stores; the subtotals below are calculated.
_DETAIL: list[tuple[str, float]] = [
    ("Total Net Revenue", 5_000.0),
    ("Base Compensation", 1_800.0),
    ("Variable Pay", 270.0),
    ("Employee Benefits", 180.0),
    ("Total Direct Expense", 2_600.0),
    ("Regional Controllable Indirect Expenses", 300.0),
    ("Centrally Controlled Indirect Expenses", 200.0),
    ("Over Recovery", 150.0),
    ("Charged Hours", 2_500.0),
    ("Standard Hours", 3_200.0),
    ("Effective Available Hours", 3_000.0),
    ("FTE", 12.0),
]

#: (segment, line, multiplier on the actual). The planted trouble.
_PLANTED = {
    ("IND-101", "Regional Controllable Indirect Expenses"): 1.40,  # over everything
    ("POL-102", "Total Net Revenue"): 0.70,    # short, and fails revenue validation
    ("PHL-103", "Variable Pay"): 1.90,         # past the Variable Pay share
    ("MEX-101", "Employee Benefits"): 1.60,    # past the benefits benchmark
    ("ESP-101", "Total Direct Expense"): 1.30,  # past Direct Cost to Revenue
}


def _seed(*parts: str) -> float:
    """A small, stable wobble, so the rows do not all read the same."""
    total = sum(ord(c) * (i + 1) for i, c in enumerate("".join(parts)))
    return 1 + ((total % 7) - 3) / 100


def _base(rs: str, line: str, base: float, *, scale: float = 1.0) -> float:
    if line == "FTE":
        return float(round(base * _seed(rs, line), 1))
    return float(round(base * _seed(rs, line) * scale))


def _lines_for(rs: str, *, scale: float, planted: bool) -> dict[str, float]:
    """A segment's whole P&L, subtotals and percentages included, as FR reports it."""
    values = {}
    for line, base in _DETAIL:
        figure = _base(rs, line, base, scale=scale)
        values[line] = float(round(figure * (_PLANTED.get((rs, line), 1.0) if planted else 1.0), 1))
    margin = values["Total Net Revenue"] - values["Total Direct Expense"]
    indirect = (values["Regional Controllable Indirect Expenses"]
                + values["Centrally Controlled Indirect Expenses"])
    # Percentages as Excel holds them: fractions, shown as per cent.
    values.update({
        "Gross Margin": margin,
        "Gross Margin % of TNR": round(margin / values["Total Net Revenue"], 4),
        "Total Indirect Expenses": indirect,
        "Total Income": margin - indirect,
        "Full Utilisation": round(values["Charged Hours"] / values["Standard Hours"], 4),
        "Effective Utilisation":
            round(values["Charged Hours"] / values["Effective Available Hours"], 4),
    })
    return values


#: The order the report lists them in, with a heading row among them as FR has.
_ROWS = ["Revenue", "Total Net Revenue", "Direct", "Base Compensation", "Variable Pay",
         "Employee Benefits", "Total Direct Expense", "Gross Margin", "Gross Margin % of TNR",
         "Indirect", "Regional Controllable Indirect Expenses",
         "Centrally Controlled Indirect Expenses", "Total Indirect Expenses", "Total Income",
         "Operating measures", "Over Recovery", "Charged Hours", "Standard Hours",
         "Effective Available Hours", "FTE", "Full Utilisation", "Effective Utilisation"]
_HEADINGS = {"Revenue", "Direct", "Indirect", "Operating measures"}
_RATIOS = {"Gross Margin % of TNR", "Full Utilisation", "Effective Utilisation"}


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
    """An FR report: a Cover Sheet, then repeated blocks with Total Selected first."""
    # August's YTD is two-thirds of September's, near enough for a sample.
    scale = 1.0 if month == SEPTEMBER else 0.67
    names = segments(location)
    by_segment = {rs: _lines_for(rs, scale=scale, planted=month == SEPTEMBER) for rs in names}
    plans = {rs: _lines_for(rs, scale=scale, planted=False) for rs in names}
    full = {rs: _lines_for(rs, scale=4.0, planted=False) for rs in names}

    measures = ["Actual at Plan Rate", "Plan", "Inc/(Dec) % vs Plan", "PY at Plan Rate",
                "Inc/(Dec) % vs PY", "Full Year Plan", "Full Year PY"]
    blocks = ["Total Selected", *names]
    heads: list[Any] = ["GDS Mgmt P&L YTD"]
    for name in blocks:
        heads += [name] + [None] * (len(measures) - 1)
    second: list[Any] = ["P&L line"]
    for _ in blocks:
        second += measures

    table: list[list[Any]] = [[f"{location} · {service_line}"], heads, second]
    for line in _ROWS:
        if line in _HEADINGS:
            table.append([line])
            continue
        row: list[Any] = [line]
        total: dict[str, float] = {"a": 0.0, "p": 0.0, "py": 0.0, "fp": 0.0, "fy": 0.0}
        cells: list[Any] = []
        for rs in names:
            a, p = by_segment[rs][line], plans[rs][line]
            py, fp = round(p * 0.97, 1), full[rs][line]
            fy = round(fp * 0.97, 1)
            total = {"a": total["a"] + a, "p": total["p"] + p, "py": total["py"] + py,
                     "fp": total["fp"] + fp, "fy": total["fy"] + fy}
            cells += [a, p, _change(a, p), py, _change(a, py), fp, fy]
        if line in _RATIOS:
            row += [None] * len(measures)
        else:
            row += [total["a"], total["p"], _change(total["a"], total["p"]), total["py"],
                    _change(total["a"], total["py"]), total["fp"], total["fy"]]
        table.append(row + cells)
    # A new line appears in one location this month, and consolidation must carry
    # it through and say so.
    if month == SEPTEMBER and location == "Philippines":
        line_row: list[Any] = ["Investment Hours", 140.0, *[None] * 6, 140.0, *[None] * 6]
        table.insert(-1, line_row)

    cover = [["GDS Management Profit and Loss"], [],
             ["Fiscal Year", "FY27"],
             ["Period", pnl_files.MONTHS[month % 12]], ["Period Type", "YTD"],
             ["Currency", "USD constant"], ["Divisor", "Thousands"],
             ["Location", location], ["Service Line", service_line]]
    return _xlsx({"Cover Sheet": cover, "GDS Mgmt P&L YTD": table})


def _change(actual: float, against: float) -> float | None:
    """The report's own Inc/(Dec) %, which Compass does not read."""
    return round(actual / against - 1, 4) if against else None


def _long(heading: list[str], rows: list[list[Any]]) -> bytes:
    return _xlsx({"Data": [heading, *rows]})


_MONTHS = ["Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun"]
#: Plan 27 Data's columns, in its order.
_PLAN_HEADING = ["Year", "Scenario", "Scope", "GDS Location", "Service Line", "Rate Card",
                 "Reporting Segment - FR", "Reporting Segment", "MENA", "FR P&L", "P&L",
                 "Snapshot", *_MONTHS]


def _whole(line: str, figure: float) -> float:
    """A figure in the Plan's own units: money in whole currency, counts as they are."""
    return figure if not pnl.is_money(line) else figure * 1000


def _monthly(line: str, ytd_september: float, full_year: float | None = None, *,
             months: range = range(12)) -> list[Any]:
    """Each month's own amount: Jul to Sep add up to September's YTD, and all
    twelve to the full year FR reports.

    A count such as FTE is the same each month, so its average is the figure.
    """
    if line == "FTE":
        return [ytd_september if i in months else None for i in range(12)]
    early = round(_whole(line, ytd_september) / 3, 2)
    rest = (round(_whole(line, full_year - ytd_september) / 9, 2)
            if full_year is not None else early)
    return [(early if i < 3 else rest) if i in months else None for i in range(12)]


def _scenario_file(scenario: str, factor: float) -> bytes:
    """The Plan database, or a Forecast, laid out as Plan 27 Data is."""
    stored = [(line, base) for line, base in _DETAIL
              if line not in ("Base Compensation", "Variable Pay", "Employee Benefits")]

    def row(location: str, rs: str, line: str, months: list[Any], *, scope: str = "Yes",
            sl: str = "Consulting", plain: str = "") -> list[Any]:
        return ["FY27", scenario, scope, location, sl, "Rate Card A", rs,
                plain or plain_name(rs), "Non MENA", line, line, "", *months]

    rows: list[list[Any]] = []
    for location, _sl in FILED:
        for rs in segments(location):
            for line, base in stored:
                rows.append(row(location, rs, line, _monthly(
                    line, _base(rs, line, base) * factor, _base(rs, line, base, scale=4.0))))
    # Planned this month and absent from FR: what is inserted by hand today. The
    # mapping says where this one belongs.
    for line, base in stored[:4]:
        rows.append(row("India", "IND-NEW", line, _monthly(line, base * 0.2), plain="Data"))
    # And one nothing says the location of.
    for line, base in stored[:2]:
        rows.append(row("", "XXX-900", line, _monthly(line, base * 0.1), plain="Unknown"))
    # Planned from January and nothing to report yet: still needs its row.
    for line, base in stored[:4]:
        rows.append(row("Mexico", "MEX-FUT", line,
                        _monthly(line, base * 0.3, months=range(6, 12)), plain="Future"))
    # What the real file also holds, and the macro does not count: headcount
    # movements with no FR line, rows out of scope, and another service line.
    rows.append(row("India", "IND-101", "NA", [1] * 12))
    rows.append(row("India", "IND-101", "Total Net Revenue", [999_000] * 12, scope="No"))
    rows.append(row("India", "IND-ASR", "Total Net Revenue", [500_000] * 12, sl="Assurance"))
    return _long(_PLAN_HEADING, rows)


def plan_file(factor: float = 1.0) -> bytes:
    """The Plan database as Plan 27 Data lays it out: each month's own amount.

    Detail lines only. The macro does not expect it to store every subtotal, and
    neither does this: Gross Margin and the rest are worked out per segment.
    """
    return _scenario_file("Plan", factor)


def forecast_file(factor: float = 1.02) -> bytes:
    return _scenario_file("Forecast", factor)


def history_file() -> bytes:
    """Last September, as the one-time load. Everything is a little smaller."""
    rows = []
    for location, service_line in FILED:
        for rs in segments(location):
            for line, base in _DETAIL:
                rows.append([month_label(LAST_SEPTEMBER), location, service_line, rs, line,
                             round(_base(rs, line, base) * 0.97, 1)])
    return _long(["Period", "Location", "Service line", "Reporting Segment",
                  "P&L line", "Actual"], rows)


def mapping_file() -> bytes:
    rows = []
    for location, service_line in FILED:
        for n, rs in enumerate(segments(location)):
            # One segment nobody has mapped, so the gap report is not empty.
            if rs == "PHL-103":
                continue
            rows.append([rs, plain_name(rs), ("Core", "Growth", "Support")[n],
                         "Technology Consulting", f"Rate Card {'AB'[n % 2]}", f"1:{4 + n}",
                         "Digital", "Non-MENA", location, service_line])
    rows.append(["IND-NEW", "Data", "Growth", "Technology Consulting", "Rate Card A", "1:6",
                 "Data", "Non-MENA", "India", "Consulting"])
    rows.append(["MEX-FUT", "Future", "Growth", "Technology Consulting", "Rate Card A", "1:6",
                 "Data", "Non-MENA", "Mexico", "Consulting"])
    # Two rows, two answers: Finance's one-to-many rule to make.
    rows.append(["MEX-102", plain_name("MEX-102"), "Growth", "Risk", "Rate Card B", "1:9",
                 "Data", "Non-MENA", "Mexico", "Consulting"])
    return _long(["Reporting Segment - FR", "Reporting Segment", "Type", "SSL", "Rate Card",
                  "Span", "Competencies", "MENA", "Location", "Service line"], rows)


def hours_file() -> bytes:
    rows = []
    for location, service_line in FILED:
        for rs in segments(location):
            # Charged hours × the card's rate is near the revenue reported, in
            # whole currency, for every segment but the one planted short.
            rows.append([month_label(SEPTEMBER), location, service_line, rs,
                         _base(rs, "Total Net Revenue", 5_000.0) * 1000 / 2_000])
    return _long(["Period", "Location", "Service line", "Reporting Segment",
                  "Charged hours"], rows)


def rates_file() -> bytes:
    return _long(["Rate Card", "Rate"], [["Rate Card A", 2_000], ["Rate Card B", 2_000]])


def finance_file() -> bytes:
    """Finance's own consolidation. Agrees with FR except for one parameter."""
    rows = []
    for location, service_line in FILED:
        for line in ("Total Net Revenue", "Total Direct Expense"):
            total = sum(_lines_for(rs, scale=1.0, planted=True)[line]
                        for rs in segments(location))
            if (location, line) == ("Argentina", "Total Net Revenue"):
                total += 250
            rows.append([month_label(SEPTEMBER), location, service_line, line, total])
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


def drafts() -> dict[str, tuple[str, bytes]]:
    """The worksheets September's close started from, before `files` replaced them."""
    return {
        "plan": ("plan-database first draft.xlsx", plan_file(factor=0.97)),
        "forecast": ("forecast first draft.xlsx", forecast_file(factor=1.05)),
    }


def load() -> None:
    """Put the sample month in the engine, through the real reader."""
    first = pnl.rules()["fy_first_month"]
    for month in (AUGUST, SEPTEMBER):
        for location, service_line in FILED:
            name = f"FR {location} {service_line} {month_label(month)}.xlsx"
            raw = fr_file(month, location, service_line)
            pnl.record_source("fr", pnl_files.read("fr", name, raw, first_month=first), filename=name,
                              raw=raw, by="sample", month=month, location=location,
                              service_line=service_line)
    for kind, (name, raw) in [*drafts().items(), *files().items()]:
        pnl.record_source(kind, pnl_files.read(kind, name, raw, first_month=first, month=SEPTEMBER),
                          filename=name, raw=raw, by="sample", month=SEPTEMBER)
