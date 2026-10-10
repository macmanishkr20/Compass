"""A business-function screen, as a workbook.

THE EXPORT IS THE SCREEN. It is built by asking the handler for the same
figures and the same rows the surface asks for, through the same scope. Not
a second query with its own filters — that is how a spreadsheet comes to say
four people are over the limit while the screen says three, and the
spreadsheet is the one that gets emailed to a board.

Two things fall out of that, and both matter more than the formatting:

  * it cannot bypass the screen's access rules. A feature that shows nothing
    to somebody without the role returns nothing here either, because this
    calls the same methods. There is no second path to the data, which is
    the usual shape of "the UI hid it and the API did not".
  * it cannot drift. Add a tab to a feature and it is in the workbook; take
    one away and it is gone. Nothing here lists what a feature contains.

AND IT SAYS WHEN IT WAS TRUE. A spreadsheet outlives the screen it came
from: it gets attached to an email, read three weeks later, and quoted in a
meeting a month after that. So the first sheet says which period, the moment
it was generated and who asked for it, and says plainly that the figures
move. A number with no date on it is the one that ends up in a board pack.

Column headings are derived from the row keys, because the labels the screen
uses live in the Angular component. The data is identical either way; only
the wording of a heading can differ. When those labels move to the feature —
the same journey `row_key` and `choices` have already made — this picks them
up and the derivation goes away.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

from compass.businessfunctions.features.base import Feature, Scope
from compass.businessfunctions.manifest import FeatureManifest, FunctionManifest

#: Excel's way of spelling Indian digit grouping: ₹1,50,000 rather than
#: ₹150,000. Three clauses because the grouping changes at a lakh and again
#: at a crore, and a number that reads differently on the screen and in the
#: spreadsheet is a number somebody will query.
RUPEES = r'[>=10000000]"₹"#\,##\,##\,##0;[>=100000]"₹"#\,##\,##0;"₹"#,##0'

#: Row keys that hold rupees. A presentation detail and nothing more — the
#: value written is the integer either way, so a column of them adds up.
MONEY = frozenset({
    "total", "excess", "headroom", "limit", "value", "to_people_over",
    "above_limit", "budget", "committed", "left", "not_requested", "given",
})

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: A sheet name Excel will take: 31 characters, and none of : \ / ? * [ ].
_BAD_IN_SHEET = re.compile(r"[:\\/?*\[\]]")


def slug(name: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in name).strip("-").lower()
    return out or "export"


def _sheet_name(label: str, taken: set[str]) -> str:
    name = _BAD_IN_SHEET.sub(" ", label).strip()[:31] or "Sheet"
    if name in taken:
        # Excel refuses duplicates outright, so make it unique rather than
        # letting the workbook fail to open.
        for n in range(2, 100):
            candidate = f"{name[:28]} {n}"
            if candidate not in taken:
                name = candidate
                break
    taken.add(name)
    return name


def _heading(key: str) -> str:
    """A row key as a column heading. "to_people_over" → "To people over"."""
    return key.replace("_", " ").strip().capitalize() or key


def _cell_value(key: str, value: Any) -> Any:
    """What to put in the cell.

    Numbers stay numbers so a column of them can be summed, and dates become
    dates so they can be sorted and filtered. A spreadsheet whose figures are
    text is a worse spreadsheet than a printout — it looks like it can be
    worked with and cannot.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and _ISO_DATE.match(value):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value) or "—"
    return value


def to_excel(fn: FunctionManifest, feature: FeatureManifest, handler: Feature,
             scope: Scope, *, asked_by: str) -> bytes:
    """One workbook: what this screen says, and when it said it."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill("solid", fgColor="1D1D1F")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    title_font = Font(color="8A6A00", bold=True, size=13)
    bold = Font(bold=True)
    quiet = Font(color="6E6E73", size=9)

    wb = Workbook()
    taken: set[str] = set()

    # ── Summary: what this is, and when it was true ──
    ws = wb.active
    ws.title = _sheet_name("Summary", taken)
    ws["A1"] = f"{fn.name} · {feature.name}"
    ws["A1"].font = title_font
    ws["A2"] = feature.short

    chosen = " · ".join(v for v in (scope.entity, scope.period) if v)
    taken_at = dt.datetime.now().astimezone()
    facts = [
        ("Period", chosen or "not scoped"),
        ("Generated", taken_at.strftime("%Y-%m-%d %H:%M %Z").strip()),
        ("Generated for", asked_by),
        # Said on the sheet, not in a footer somebody scrolls past. This file
        # will be read long after the screen has moved on, and nothing else
        # in it admits that.
        ("Note", "A snapshot. These figures change as declarations are "
                 "recorded and decisions are taken; check the screen before "
                 "quoting them."),
    ]
    row = 4
    for label, value in facts:
        ws.cell(row=row, column=1, value=label).font = bold
        ws.cell(row=row, column=2, value=value).alignment = Alignment(wrap_text=True)
        row += 1

    figures = handler.figures(scope)
    if figures:
        row += 1
        ws.cell(row=row, column=1, value="Figures").font = bold
        row += 1
        for figure in figures:
            ws.cell(row=row, column=1, value=figure.caption.capitalize())
            ws.cell(row=row, column=2, value=_cell_value("", figure.value))
            if figure.tone in ("bad", "warn"):
                ws.cell(row=row, column=3, value=f"({figure.tone})").font = quiet
            row += 1

    applies = set(handler.rules(scope))
    said = [r for r in feature.rules if r.id in applies]
    if said:
        row += 1
        ws.cell(row=row, column=1, value="In force").font = bold
        row += 1
        for rule in said:
            ws.cell(row=row, column=1, value=rule.headline).font = bold
            ws.cell(row=row, column=2, value=rule.detail).alignment = \
                Alignment(wrap_text=True)
            row += 1

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 76
    ws.column_dimensions["C"].width = 10

    # ── One sheet per tab, in the order the screen shows them ──
    for tab in handler.tabs(scope):
        rows = handler.rows(scope, tab.key)
        sheet = wb.create_sheet(_sheet_name(tab.label, taken))
        if not rows:
            sheet["A1"] = "Nothing here."
            sheet["A1"].font = quiet
            sheet.column_dimensions["A"].width = 30
            continue

        # Union rather than the first row's keys: a handler may leave a key
        # off a row, and a column that silently vanished because row one
        # happened not to have it is a hard thing to notice in a spreadsheet.
        keys: list[str] = []
        for item in rows:
            for key in item:
                if key not in keys and key not in handler.private:
                    keys.append(key)
        if not keys:
            sheet["A1"] = "Nothing here."
            sheet["A1"].font = quiet
            continue

        for column, key in enumerate(keys, start=1):
            cell = sheet.cell(row=1, column=column, value=_heading(key))
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="left", vertical="center")

        for r, item in enumerate(rows, start=2):
            for c, key in enumerate(keys, start=1):
                cell = sheet.cell(row=r, column=c,
                                  value=_cell_value(key, item.get(key, "")))
                if key in MONEY and isinstance(item.get(key), (int, float)):
                    cell.number_format = RUPEES
                elif isinstance(cell.value, dt.date):
                    cell.number_format = "yyyy-mm-dd"

        for column, key in enumerate(keys, start=1):
            longest = max([len(_heading(key))]
                          + [len(str(item.get(key, ""))) for item in rows])
            sheet.column_dimensions[get_column_letter(column)].width = \
                min(60, max(12, longest + 2))

        # The two things that make a sheet workable rather than readable.
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = (
            f"A1:{get_column_letter(len(keys))}{len(rows) + 1}")

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def filename(fn: FunctionManifest, feature: FeatureManifest,
             scope: Scope) -> str:
    """What it lands on somebody's disk as.

    The period is in the name because two of these in a downloads folder
    with the same name are two files nobody can tell apart.
    """
    parts = [slug(fn.name), slug(feature.name)]
    if scope.period:
        parts.append(slug(scope.period))
    parts.append(dt.date.today().isoformat())
    return "-".join(parts) + ".xlsx"
