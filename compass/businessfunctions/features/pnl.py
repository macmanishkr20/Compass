"""P&L's five sub-services: one month, from the files arriving to the file leaving.

    Source files        what has arrived, what it maps to, what is missing
    Consolidated P&L    the month at every level, YTD and MTD
    Reconciliation      against Finance's own file, and the release control
    Exceptions          only what breaches, for Finance to investigate
    Leadership output   chosen sheets, values only, and the run's trail

The arithmetic is `pnl.py`'s. These say who may see it, what each tab holds
and which button a row takes. Every screen narrows through `scope.sees`, and
every action re-checks what its row's `can` said, because a button that was
drawn is not a permission.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from compass.businessfunctions import pnl, pnl_files
from compass.businessfunctions.features.base import (
    Feature,
    Figure,
    Outcome,
    Scope,
    Tab,
    register,
)
from compass.businessfunctions.pnl_files import month_label

#: Who does this work. Finance's own, plus whoever administers the system.
DESK = frozenset({"finance", "admin"})

#: What an FR or master file may weigh. An FR workbook carries formats and
#: hidden sheets as well as the figures, so far past the names-list limit.
UPLOAD_MAX_BYTES = 25 * 1024 * 1024

#: The kinds of input the second upload form offers, by the words on it.
MASTER_KINDS = {
    "Plan database": "plan",
    "Forecast": "forecast",
    "RS mapping": "mapping",
    "Prior-period history": "history",
    "Finance consolidation": "finance",
    "Charged hours": "hours",
    "Rate card": "rates",
}

#: What MTD may be set to, by the words on the form.
MTD_RULES = {"Subtract last month (the generic rule)": "subtract",
             "This month's value": "closing",
             "Leave blank until Finance defines it": "pending"}
LINE_CLASSES = {"Adds across segments and months": "flow",
                "Adds across segments only (FTE, headcount)": "stock",
                "Does not add (a percentage or rate)": "ratio"}

#: Staged uploads. A file is read when it is picked and recorded when somebody
#: presses the button, and between the two it lives here: a form value is a
#: short string, and a month's workbook is not.
_staged: dict[str, dict[str, Any]] = {}
_STAGE_SECONDS = 3600
_STAGE_MOST = 8


def stage(user: str, filename: str, raw: bytes) -> str:
    """Hold a file for the person who picked it. Records nothing."""
    now = time.time()
    for token, held in list(_staged.items()):
        if now - held["at"] > _STAGE_SECONDS:
            del _staged[token]
    while len(_staged) >= _STAGE_MOST:
        del _staged[min(_staged, key=lambda t: _staged[t]["at"])]
    token = uuid.uuid4().hex
    _staged[token] = {"user": user, "filename": filename, "raw": raw, "at": now}
    return token


def _held(scope: Scope, token: str) -> dict[str, Any] | None:
    """A staged file, only for the person who staged it."""
    held = _staged.get(token.strip())
    return held if held and held["user"] == scope.user else None


def _read_held(held: dict[str, Any], kind: str) -> pnl_files.Parsed:
    """What a held file holds, read once per kind.

    A form asks twice, to preview and to record, and these handlers run on the
    server's loop. A month's workbook takes long enough to read that the second
    reading would be everybody's wait.
    """
    read = held.setdefault("parsed", {})
    if kind not in read:
        read[kind] = pnl_files.read(kind, held["filename"], held["raw"])
    return read[kind]


def _month(scope: Scope) -> int | None:
    return pnl.month_of(scope.period)


def _is_desk(scope: Scope) -> bool:
    return bool(scope.roles & DESK)


def _col(key: str, label: str, fmt: str = "") -> dict[str, Any]:
    """A column. `fmt` makes it a figure, drawn right-aligned and grouped."""
    out: dict[str, Any] = {"key": key, "label": label}
    if fmt:
        out.update(numeric=True, format=fmt)
    return out


_WHERE = [_col("location", "Location"), _col("service_line", "Service line")]


class PnlScreen(Feature):
    """What the five have in common: a month, and who may read it."""

    row_key = "id"
    private = frozenset({"id", "can", "filled", "check", "signature", "class",
                         "reviewed", "download", "stale"})
    #: Whether this screen only makes sense once a file has arrived.
    needs_data = True

    def held_periods(self) -> list[str]:
        return [month_label(m) for m in pnl.months()]

    def _open(self, scope: Scope) -> int | None:
        """The month, if this person may read this screen and it means something."""
        if scope.sees != "firm":
            return None
        return _month(scope)

    def rules(self, scope: Scope) -> list[str]:
        if scope.sees != "firm":
            return ["pnl_is_finance"]
        month = _month(scope)
        said = []
        if pnl.is_sample():
            said.append("sample_data")
        if month is None:
            return [*said, "choose_a_month"]
        if self.needs_data and not pnl.view(month)["slots"]:
            said.append("no_files_yet")
        return said


class PnlIntake(PnlScreen):
    """Source files: intake, master data, and what the joins could not find."""

    key = "pnl_intake"
    needs_data = False

    FR_FIELDS = frozenset({"location", "service_line", "file"})
    MASTER_FIELDS = frozenset({"kind", "file"})
    LINE_FIELDS = frozenset({"line_class", "mtd", "why"})

    def accepts(self, form_id: str) -> set[str]:
        return {"upload_fr": set(self.FR_FIELDS),
                "upload_master": set(self.MASTER_FIELDS),
                "line_rule": set(self.LINE_FIELDS)}.get(form_id, set())

    def upload_limit(self) -> int:
        return UPLOAD_MAX_BYTES

    def stage_file(self, scope: Scope, form_id: str, field: str, filename: str,
                   raw: bytes) -> dict[str, Any]:
        """What the route calls when a file is picked. Says what was found."""
        if not _is_desk(scope):
            return {"ok": False, "said": "Uploading sources is Finance's."}
        name = filename.rsplit("/", 1)[-1]
        said = f"{name} is ready. Press “What would this do?” to see what is in it."
        if form_id == "upload_fr":
            try:
                parsed = pnl_files.read("fr", name, raw)
            except pnl_files.Unreadable as err:
                return {"ok": False, "said": str(err)}
            said = f"{name}: {_found(parsed)}."
            token = stage(scope.user, name, raw)
            _staged[token]["parsed"] = {"fr": parsed}
            return {"ok": True, "token": token, "said": said}
        if not name.lower().endswith((".xlsx", ".xlsm", ".csv")):
            return {"ok": False, "said": f"{name} is not a spreadsheet Compass "
                                         f"can read. Save it as .xlsx or .csv."}
        return {"ok": True, "token": stage(scope.user, name, raw), "said": said}

    # ── the two uploads ─────────────────────────────────────────────────────

    def _fr_plan(self, scope: Scope, values: dict[str, str]
                 ) -> tuple[dict[str, Any] | None, Outcome | None]:
        month = _month(scope)
        if month is None:
            return None, _no("Choose the reporting month first. It is what this "
                             "file will be filed under.")
        location = values.get("location", "").strip()
        sl = values.get("service_line", "").strip()
        if location not in pnl.LOCATIONS or sl not in pnl.SERVICE_LINES:
            return None, _no("Choose the location and the service line this file is for.")
        held = _held(scope, values.get("file", ""))
        if held is None:
            return None, _no("Pick the file again. Compass only holds one for an hour.")
        try:
            parsed = _read_held(held, "fr")
        except pnl_files.Unreadable as err:
            return None, _no(str(err))

        # Does the file agree with what it is being filed as? The classic
        # month-end slip is the right report under the wrong location, and
        # nothing downstream would catch it because its figures are all valid.
        for row in parsed.rows[:200]:
            if row.get("location") and pnl_files.norm(row["location"]) != pnl_files.norm(location):
                return None, _no(f"This file says it is for {row['location']}, "
                                 f"and you are filing it under {location}.")
            if row.get("service_line") and pnl_files.norm(row["service_line"]) != pnl_files.norm(sl):
                return None, _no(f"This file says it is for {row['service_line']}, "
                                 f"and you are filing it under {sl}.")
            at = pnl_files.parse_month(row.get("period")) if row.get("period") else None
            if at is not None and at != month:
                return None, _no(f"This file says it is for {month_label(at)}, "
                                 f"and you are filing it under {month_label(month)}.")
        return {"month": month, "location": location, "service_line": sl,
                "held": held, "parsed": parsed}, _refuse_same(held)

    def _master_plan(self, scope: Scope, values: dict[str, str]
                     ) -> tuple[dict[str, Any] | None, Outcome | None]:
        kind = MASTER_KINDS.get(values.get("kind", "").strip())
        if kind is None:
            return None, _no("Choose which input this file is.")
        held = _held(scope, values.get("file", ""))
        if held is None:
            return None, _no("Pick the file again. Compass only holds one for an hour.")
        try:
            parsed = _read_held(held, kind)
        except pnl_files.Unreadable as err:
            return None, _no(str(err))
        month = _month(scope)
        needs_period = kind in ("plan", "forecast", "finance", "hours")
        if needs_period and month is None and not all(r.get("period") for r in parsed.rows):
            return None, _no("Some rows name no period, so choose the month they "
                             "are for first.")
        bad = next((r["period"] for r in parsed.rows
                    if r.get("period") and pnl_files.parse_month(r["period"]) is None), None)
        if bad is not None:
            return None, _no(f"A row names its period as {bad!r}. Compass reads "
                             f"Oct 2026, 2026-10 or a date.")
        return {"kind": kind, "month": month, "held": held, "parsed": parsed}, _refuse_same(held)

    def preview(self, scope: Scope, form_id: str, values: dict[str, str],
                about: str = "") -> Outcome:
        if not _is_desk(scope):
            return _no("This is Finance's to change.")
        if form_id == "line_rule":
            return self._line_rule(scope, values, about, commit=False)
        planned, refusal = (self._fr_plan if form_id == "upload_fr"
                            else self._master_plan)(scope, values)
        if refusal is not None:
            return refusal
        sample = (" The sample data goes away with your first real file."
                  if pnl.is_sample() else "")
        parsed = planned["parsed"]
        if form_id == "upload_fr":
            now = pnl.fr_source(planned["month"], planned["location"], planned["service_line"])
            effect = (f"Replaces v{now['version']} ({now['filename']}), which is kept."
                      if now and not pnl.is_sample() else "First file for this slot.")
            return Outcome(True, f"{planned['location']} · {planned['service_line']}, "
                                 f"{month_label(planned['month'])}: {_found(parsed)}. "
                                 f"{effect}{sample}")
        months = sorted({pnl_files.parse_month(r["period"]) for r in parsed.rows
                         if r.get("period")} - {None})
        covers = (f" covering {month_label(months[0])}"
                  + (f" to {month_label(months[-1])}" if len(months) > 1 else "")
                  if months else
                  (f" for {month_label(planned['month'])}" if planned["month"] else ""))
        return Outcome(True, f"{pnl_files.SPECS[planned['kind']].label}: "
                             f"{len(parsed.rows)} rows{covers}."
                             + (f" {parsed.skipped} incomplete rows left out."
                                if parsed.skipped else "") + sample)

    def submit(self, scope: Scope, form_id: str, values: dict[str, str],
               about: str = "") -> Outcome:
        if not _is_desk(scope):
            return _no("This is Finance's to change.")
        if form_id == "line_rule":
            return self._line_rule(scope, values, about, commit=True)
        planned, refusal = (self._fr_plan if form_id == "upload_fr"
                            else self._master_plan)(scope, values)
        if refusal is not None:
            return refusal
        held = planned["held"]
        try:
            if form_id == "upload_fr":
                source = pnl.record_source(
                    "fr", planned["parsed"], filename=held["filename"], raw=held["raw"],
                    by=scope.user, month=planned["month"],
                    location=planned["location"], service_line=planned["service_line"])
            else:
                source = pnl.record_source(
                    planned["kind"], planned["parsed"], filename=held["filename"],
                    raw=held["raw"], by=scope.user, month=planned["month"])
        except ValueError as err:
            return _no(str(err))
        _staged.pop(values.get("file", "").strip(), None)
        return Outcome(True, f"{source['label']} v{source['version']} recorded: "
                             f"{source['filename']}, {source['count']} rows.",
                       touched=[source["id"]])

    def _line_rule(self, scope: Scope, values: dict[str, str], line: str,
                   *, commit: bool) -> Outcome:
        kind = LINE_CLASSES.get(values.get("line_class", "").strip())
        mtd = MTD_RULES.get(values.get("mtd", "").strip())
        why = values.get("why", "").strip()
        if kind is None or mtd is None:
            return _no("Say how this line adds up and how its month is worked out.")
        if not why:
            return _no("Say why. This changes how the line is calculated from now on.")
        key = pnl_files.norm(line)
        if not commit:
            return Outcome(True, f"{line} would be calculated this way from now on, "
                                 f"in every month. Figures already uploaded do not change.")
        table = dict(pnl.rules()["line_rules"])
        table[key] = {"class": kind, "mtd": mtd}
        try:
            fresh = pnl.set_rules(by=scope.user, why=why, line_rules=table)
        except ValueError as err:
            return _no(str(err))
        return Outcome(True, f"Rules v{fresh['version']}: {line} is set.", touched=[line])

    # ── the row buttons ─────────────────────────────────────────────────────

    def _slot_can(self, scope: Scope, row: dict[str, Any]) -> list[str]:
        if not _is_desk(scope):
            return []
        return {"Missing": ["remind", "not_required"],
                "Not required": ["required_again"]}.get(row["status"], [])

    def act(self, scope: Scope, action: str, targets: list[str], note: str = "") -> Outcome:
        month = self._open(scope)
        if month is None:
            return _no("Choose a month first.")
        rows = {r["id"]: dict(r, can=self._slot_can(scope, r)) for r in pnl.coverage(month)}
        chosen = [rows[t] for t in targets if t in rows]
        if not chosen:
            return _no("No such location and service line in view.")
        for row in chosen:
            if action not in row["can"]:
                return _no(f"{row['location']} · {row['service_line']} is "
                           f"{row['status'].lower()}, so that does not apply.")
        for row in chosen:
            where = (month, row["location"], row["service_line"])
            if action == "remind":
                pnl.remind(*where, by=scope.user)
            elif action == "not_required":
                if not note.strip():
                    return _no("Say why this one is not expected this month.")
                pnl.set_skip(*where, by=scope.user, note=note.strip(), on=True)
            elif action == "required_again":
                pnl.set_skip(*where, by=scope.user, note="", on=False)
            else:
                return _no(f"Source files has no action called {action!r}.")
        said = {"remind": "Reminder recorded", "not_required": "Marked not required",
                "required_again": "Expected again"}[action]
        return Outcome(True, f"{said} for {len(chosen)} slot"
                             f"{'' if len(chosen) == 1 else 's'}.",
                       touched=[r["id"] for r in chosen])

    # ── what is on the screen ───────────────────────────────────────────────

    def figures(self, scope: Scope) -> list[Figure]:
        if scope.sees != "firm":
            return []
        month = _month(scope) or pnl.latest_month()
        if month is None:
            return []
        rows = pnl.coverage(month)
        missing = sum(r["status"] == "Missing" for r in rows)
        now = pnl.view(month)
        gaps = len(now["unmapped"]) + len(now["conflicts"])
        return [
            Figure("arrived", f"{sum(r['status'] == 'Uploaded' for r in rows)} of {len(rows)}",
                   f"FR files for {month_label(month)}"),
            Figure("missing", str(missing),
                   f"FR files missing for {month_label(month)}" if missing else "FR files missing",
                   "warn" if missing else "good"),
            Figure("gaps", str(gaps), "segments unmapped or mapped twice",
                   "bad" if gaps else "good"),
            Figure("inserted", str(len(now["missing"])),
                   "segments added from the Plan"),
            Figure("alerts", str(len(now["alerts"])), "structure changes since last month"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        month = self._open(scope)
        if month is None:
            return []
        now = pnl.view(month)
        return [
            Tab("coverage", "FR files", len(pnl.coverage(month))),
            Tab("inputs", "Plan, mappings and inputs", len(MASTER_KINDS)),
            Tab("gaps", "Mapping gaps", len(now["unmapped"]) + len(now["conflicts"])),
            Tab("missing_rs", "Segments added", len(now["missing"])),
            Tab("structure", "Structure changes", len(now["alerts"])),
            Tab("lines", "Line rules", len({c["line_key"] for c in now["cells"].values()})),
            Tab("uploads", "Every upload", len(pnl.sources())),
        ]

    def columns(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        return {
            "coverage": [*_WHERE, _col("status", "Status"), _col("file", "File"),
                         _col("rows", "Rows", "whole"), _col("when", "When")],
            "inputs": [_col("input", "Input"), _col("status", "Status"), _col("file", "File"),
                       _col("rows", "Rows", "whole"), _col("when", "When")],
            "gaps": [_col("rs", "Reporting Segment"), _col("problem", "Problem")],
            "missing_rs": [*_WHERE, _col("rs", "Reporting Segment"),
                           _col("lines", "Lines", "whole"), _col("reason", "Why")],
            "structure": [*_WHERE, _col("change", "Change"), _col("name", "What")],
            "lines": [_col("line", "P&L line"), _col("behaves", "Adds up"),
                      _col("mtd", "MTD"), _col("set_by", "Set by")],
            "uploads": [_col("input", "Input"), _col("file", "File"),
                        _col("for_what", "For"), _col("status", "Status"),
                        _col("when", "When")],
        }.get(tab, [])

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        month = self._open(scope)
        if month is None:
            return []
        now = pnl.view(month)
        if tab == "inputs":
            return self._inputs(month)
        if tab == "gaps":
            return ([{"id": f"unmapped/{rs}", "rs": rs, "problem": "Not in the mapping",
                      "can": []} for rs in now["unmapped"]]
                    + [{"id": f"conflict/{rs}", "rs": rs,
                        "problem": "Mapped twice, with different attributes. Neither is "
                                   "applied until Finance decides.",
                        "can": []} for rs in now["conflicts"]])
        if tab == "missing_rs":
            return [{"id": f"{m['location']}/{m['service_line']}/{m['rs']}",
                     "location": m["location"], "service_line": m["service_line"],
                     "rs": m["rs"], "lines": m["lines"], "reason": m["why"], "can": []}
                    for m in now["missing"]]
        if tab == "structure":
            return now["alerts"]
        if tab == "lines":
            return self._lines(scope, now)
        if tab == "uploads":
            return self._uploads()
        return [dict(r, can=self._slot_can(scope, r)) for r in pnl.coverage(month)]

    def _inputs(self, month: int) -> list[dict[str, Any]]:
        out = []
        for label, kind in MASTER_KINDS.items():
            source = (pnl.active(kind) if kind in ("mapping", "rates")
                      else pnl.active(kind, month))
            out.append({
                "id": kind, "input": label,
                "status": "Loaded" if source else "Not loaded",
                "file": source["filename"] if source else "",
                "version": source["version"] if source else None,
                "rows": source["count"] if source else None,
                "by": source["by"] if source else "",
                "when": pnl._day(source["at"]) if source else "",  # noqa: SLF001
                "can": [],
            })
        return out

    def _lines(self, scope: Scope, now: dict[str, Any]) -> list[dict[str, Any]]:
        said = pnl.rules()["line_rules"]
        behaves = {v: k for k, v in LINE_CLASSES.items()}
        mtd = {"subtract": "Subtract last month", "closing": "This month's value",
               "pending": "Blank, Finance to define"}
        seen: dict[str, str] = {}
        for got in now["cells"].values():
            seen.setdefault(got["line_key"], got["line"])
        return [{"id": name, "line": name, "behaves": behaves[pnl.line_class(name)],
                 "mtd": mtd[pnl.mtd_rule(name)],
                 "set_by": "Finance" if key in said else "Compass's default",
                 "can": ["line_rule"] if _is_desk(scope) else []}
                for key, name in seen.items()]

    def _uploads(self) -> list[dict[str, Any]]:
        out = []
        for source in reversed(pnl.sources()):
            months = source["months"]
            span = (f"{month_label(months[0])}" + (f" to {month_label(months[-1])}"
                                                   if len(months) > 1 else "")
                    if months else "All months")
            where = (f"{source['location']} · {source['service_line']}, "
                     if source["location"] else "")
            out.append({"id": source["id"], "input": source["label"],
                        "file": source["filename"], "for_what": where + span,
                        "version": source["version"], "rows": source["count"],
                        "status": "Current" if pnl.is_current(source) else "Replaced",
                        "by": source["by"],
                        "when": pnl._day(source["at"]),  # noqa: SLF001
                        "fingerprint": source["sha256"][:12], "can": []})
        return out


class PnlStatement(PnlScreen):
    """The consolidated month. Reads; nothing here decides anything."""

    key = "pnl_statement"

    LINES = [_col("line", "P&L line"), _col("ytd", "YTD Actual", "number"),
             _col("plan", "YTD Plan", "number"), _col("vs_plan_pct", "vs Plan", "pct"),
             _col("forecast", "YTD Forecast", "number"),
             _col("vs_forecast_pct", "vs Forecast", "pct"),
             _col("prior_year", "Prior Year", "number"),
             _col("vs_prior_year_pct", "vs PY", "pct"), _col("mtd", "MTD Actual", "number")]
    ROLLUP = [_col("revenue", "Revenue", "number"), _col("revenue_plan", "Plan", "number"),
              _col("vs_plan_pct", "vs Plan", "pct"), _col("direct_cost", "Direct cost", "number"),
              _col("fte", "FTE", "number"), _col("segments", "Segments", "whole")]

    def figures(self, scope: Scope) -> list[Figure]:
        month = self._open(scope)
        if month is None:
            return []
        now = pnl.view(month)
        cells = [c for c in now["cells"].values() if pnl.in_location(c, scope.entity)]
        return [
            Figure("slots", str(len(now["slots"])), "location and service line files in"),
            Figure("segments", str(len({pnl_files.norm(c["rs"]) for c in cells})),
                   "Reporting Segments"),
            Figure("lines", str(len({c["line_key"] for c in cells})), "unique P&L lines"),
            Figure("no_prior", str(len(now["no_prior"])),
                   "slots with no prior month, so no MTD",
                   "bad" if now["no_prior"] else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        month = self._open(scope)
        if month is None:
            return []
        lines = pnl.summary(month, location=scope.entity)
        return [
            Tab("statement", "P&L", len(lines)),
            Tab("locations", "By location", len(pnl.slot_rollup(month, "location"))),
            Tab("service_lines", "By service line", len(pnl.slot_rollup(month, "service_line"))),
            Tab("segments", "By Reporting Segment", len(pnl.segments(month, scope.entity))),
            Tab("detail", "Every segment and line", len(pnl.detail(month, scope.entity))),
        ]

    def columns(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        return {
            "statement": self.LINES,
            "locations": [_col("location", "Location"), *self.ROLLUP],
            "service_lines": [_col("service_line", "Service line"), *self.ROLLUP],
            "segments": [*_WHERE, _col("rs", "Reporting Segment"), _col("type", "Type"),
                         _col("sub_service_line", "Sub-service line"),
                         _col("rate_card", "Rate card"), _col("revenue", "Revenue", "number"),
                         _col("direct_cost", "Direct cost", "number"),
                         _col("fte", "FTE", "number")],
            "detail": [*_WHERE, _col("rs", "Reporting Segment"), _col("line", "P&L line"),
                       _col("ytd", "YTD Actual", "number"), _col("plan", "YTD Plan", "number"),
                       _col("forecast", "YTD Forecast", "number"),
                       _col("prior_year", "Prior Year", "number"),
                       _col("mtd", "MTD Actual", "number"), _col("inserted", "Added")],
        }.get(tab, [])

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        month = self._open(scope)
        if month is None:
            return []
        if tab == "locations":
            return pnl.slot_rollup(month, "location")
        if tab == "service_lines":
            return pnl.slot_rollup(month, "service_line")
        if tab == "segments":
            return pnl.segments(month, scope.entity)
        if tab == "detail":
            return pnl.detail(month, scope.entity)
        return pnl.summary(month, location=scope.entity)

    def rules(self, scope: Scope) -> list[str]:
        said = super().rules(scope)
        month = _month(scope)
        if month is not None and scope.sees == "firm":
            if pnl.view(month)["no_prior"]:
                said.append("no_prior_month")
            if any(pnl.mtd_rule(c["line"]) == "pending"
                   for c in pnl.view(month)["cells"].values()):
                said.append("mtd_definitions_pending")
        return said


class PnlRecon(PnlScreen):
    """Against Finance's own file, parameter by parameter."""

    key = "pnl_recon"

    TOLERANCE_FIELDS = frozenset({"recon_abs", "recon_pct", "why"})

    def accepts(self, form_id: str) -> set[str]:
        return set(self.TOLERANCE_FIELDS) if form_id == "set_tolerance" else set()

    def _parsed(self, scope: Scope, values: dict[str, str]
                ) -> tuple[dict[str, float] | None, Outcome | None]:
        if not _is_desk(scope):
            return None, _no("Tolerances are Finance's.")
        changes, why = _numbers(values, ("recon_abs", "recon_pct"))
        if why:
            return None, _no(why)
        if not changes:
            return None, _no("Nothing to change.")
        if not values.get("why", "").strip():
            return None, _no("Say why. A wider tolerance turns a mismatch into a match.")
        return changes, None

    def preview(self, scope: Scope, form_id: str, values: dict[str, str],
                about: str = "") -> Outcome:
        changes, refusal = self._parsed(scope, values)
        if refusal is not None:
            return refusal
        base = pnl.rules()
        return Outcome(True, "Would tolerate a difference up to "
                             f"{pnl.amount(changes.get('recon_abs', base['recon_abs']))} or "
                             f"{changes.get('recon_pct', base['recon_pct']):g}% of Finance's "
                             f"figure, whichever is larger, in every month from now.")

    def submit(self, scope: Scope, form_id: str, values: dict[str, str],
               about: str = "") -> Outcome:
        changes, refusal = self._parsed(scope, values)
        if refusal is not None:
            return refusal
        try:
            fresh = pnl.set_rules(by=scope.user, why=values["why"], **changes)
        except ValueError as err:
            return _no(str(err))
        return Outcome(True, f"Rules v{fresh['version']} are in force.",
                       touched=[f"v{fresh['version']}"])

    def figures(self, scope: Scope) -> list[Figure]:
        if scope.sees != "firm":
            return []
        month = _month(scope) or pnl.latest_month()
        if month is None:
            return []
        both = pnl.reconciliation(month)
        rows = both["finance"]
        off = sum(r["status"] != "Matched" for r in rows)
        inside = sum(r["status"] != "Matched" for r in both["internal"])
        return [
            Figure("compared", str(len(rows)), "parameters compared"),
            Figure("matched", str(len(rows) - off), "matched", "good" if rows else "plain"),
            Figure("mismatched", str(off),
                   f"reconciliation differences in {month_label(month)}",
                   "warn" if off else "good"),
            Figure("internal", str(inside), "segments not adding to the file's total",
                   "bad" if inside else "good"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        month = self._open(scope)
        if month is None:
            return []
        both = pnl.reconciliation(month)
        return [
            Tab("mismatched", "Differences",
                sum(r["status"] != "Matched" for r in both["finance"])),
            Tab("all", "Against Finance", len(both["finance"])),
            Tab("internal", "Segments against file totals", len(both["internal"])),
        ]

    def columns(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if tab == "internal":
            return [*_WHERE, _col("parameter", "Line"),
                    _col("segments_total", "Sum of segments", "number"),
                    _col("file_total", "File's total", "number"),
                    _col("difference", "Difference", "number"), _col("status", "Status")]
        return [*_WHERE, _col("parameter", "Parameter"), _col("compass", "P&L", "number"),
                _col("finance", "Finance", "number"), _col("difference", "Difference", "number"),
                _col("difference_pct", "Difference %", "pct"), _col("status", "Status")]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        month = self._open(scope)
        if month is None:
            return []
        both = pnl.reconciliation(month)
        if tab == "internal":
            return both["internal"]
        if tab == "all":
            return both["finance"]
        return [r for r in both["finance"] if r["status"] != "Matched"]

    def rules(self, scope: Scope) -> list[str]:
        said = super().rules(scope)
        month = _month(scope)
        if month is not None and scope.sees == "firm" and pnl.active("finance", month) is None:
            said.append("no_finance_file")
        if scope.sees == "firm" and _illustrative():
            said.append("rules_illustrative")
        return said


class PnlExceptions(PnlScreen):
    """Exception-only output: what breached, for Finance to look into.

    Reports and nothing else. Marking one reviewed records that somebody
    looked; it never touches a figure, and new figures reopen it.
    """

    key = "pnl_exceptions"

    SELECT_FIELDS = frozenset({"comparison", "preset", "custom", "floor"})
    BENCHMARK_FIELDS = frozenset({"vp_pct", "eb_pct", "eb_by_location", "dc_revenue_pct",
                                  "dc_per_fte", "revenue_tolerance_pct", "why"})
    COMPARISONS = {"All three": ("plan_pct", "py_pct", "forecast_pct"),
                   "Actual vs Plan": ("plan_pct",),
                   "Current Year vs Prior Year": ("py_pct",),
                   "Actual vs Forecast": ("forecast_pct",)}

    def accepts(self, form_id: str) -> set[str]:
        return {"select_thresholds": set(self.SELECT_FIELDS),
                "set_benchmarks": set(self.BENCHMARK_FIELDS)}.get(form_id, set())

    def _rows(self, scope: Scope, month: int) -> list[dict[str, Any]]:
        out = []
        for row in pnl.exceptions(month)["found"]:
            if not pnl.in_location(row, scope.entity):
                continue
            done = pnl.review_of(row)
            desk = _is_desk(scope)
            out.append(dict(
                row, status="Reviewed" if done else "To review",
                reviewed=bool(done),
                reviewed_by=done["by"] if done else "",
                note=done["note"] if done else "",
                can=(["reopen"] if done else ["mark_reviewed"]) if desk else []))
        return out

    def figures(self, scope: Scope) -> list[Figure]:
        if scope.sees != "firm":
            return []
        month = _month(scope) or pnl.latest_month()
        if month is None:
            return []
        rows = self._rows(scope, month)
        waiting = sum(not r["reviewed"] for r in rows)
        picked = pnl.selection(month)
        return [
            Figure("to_review", str(waiting),
                   f"exceptions to review in {month_label(month)}",
                   "warn" if waiting else "good"),
            Figure("found", str(len(rows)), "exceptions found"),
            Figure("segments", str(len({pnl_files.norm(r["rs"]) for r in rows})),
                   "segments with an exception"),
            Figure("threshold", _selected(picked),
                   "selected thresholds: Plan, Prior Year, Forecast"),
            Figure("untested",
                   str(sum(pnl.in_location(r, scope.entity)
                           for r in pnl.exceptions(month)["untested"])),
                   "checks that could not run", "plain"),
        ]

    def tabs(self, scope: Scope) -> list[Tab]:
        month = self._open(scope)
        if month is None:
            return []
        rows = self._rows(scope, month)
        tabs = [Tab("all", "All exceptions", len(rows))]
        tabs += [Tab(check, label, sum(r["check"] == check for r in rows))
                 for check, label in pnl.CHECKS.items()]
        tabs.append(Tab("untested", "Could not run",
                        sum(pnl.in_location(r, scope.entity)
                            for r in pnl.exceptions(month)["untested"])))
        tabs.append(Tab("rules", "Rules in force", len(pnl.rule_history())))
        return tabs

    def columns(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if tab == "untested":
            return [_col("comparison", "Check"), *_WHERE, _col("rs", "Reporting Segment"),
                    _col("why", "Why it could not run")]
        if tab == "rules":
            return [_col("version", "Version"), _col("plan_pct", "Plan", "pct"),
                    _col("py_pct", "Prior Year", "pct"), _col("forecast_pct", "Forecast", "pct"),
                    _col("vp_pct", "VP / Base", "pct"), _col("eb_pct", "Benefits / Base", "pct"),
                    _col("dc_revenue_pct", "DC / Revenue", "pct"),
                    _col("dc_per_fte", "DC per FTE", "number"),
                    _col("revenue_tolerance_pct", "Revenue tolerance", "pct"),
                    _col("by", "Set by"), _col("why", "Why")]
        return [_col("comparison", "Comparison"), *_WHERE, _col("rs", "Reporting Segment"),
                _col("line", "P&L line"), _col("value", "Value", "number"),
                _col("against", "Against", "number"), _col("variance", "Variance", "number"),
                _col("variance_pct", "Variance %", "pct"), _col("threshold", "Threshold"),
                _col("status", "Status")]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        month = self._open(scope)
        if month is None:
            return []
        if tab == "untested":
            return [r for r in pnl.exceptions(month)["untested"]
                    if pnl.in_location(r, scope.entity)]
        if tab == "rules":
            return [{"id": f"v{r['version']}", "version": f"v{r['version']}",
                     **{k: r[k] for k in ("plan_pct", "py_pct", "forecast_pct", "vp_pct",
                                          "eb_pct", "dc_revenue_pct", "dc_per_fte",
                                          "revenue_tolerance_pct", "by", "why")},
                     "can": []}
                    for r in reversed(pnl.rule_history())]
        rows = self._rows(scope, month)
        return rows if tab == "all" else [r for r in rows if r["check"] == tab]

    def rules(self, scope: Scope) -> list[str]:
        said = super().rules(scope)
        if scope.sees == "firm" and _illustrative():
            said.append("rules_illustrative")
        return said

    def act(self, scope: Scope, action: str, targets: list[str], note: str = "") -> Outcome:
        month = self._open(scope)
        if month is None:
            return _no("Choose a month first.")
        rows = {r["id"]: r for r in self._rows(scope, month)}
        chosen = [rows[t] for t in targets if t in rows]
        if not chosen:
            return _no("That exception is not in view.")
        for row in chosen:
            if action not in row["can"]:
                return _no(f"{row['rs']} · {row['line']} is {row['status'].lower()}, "
                           f"so that does not apply.")
        if action == "mark_reviewed" and not note.strip():
            return _no("Say what you found. A review with no note is a tick nobody can read.")
        for row in chosen:
            pnl.set_review(row, month, by=scope.user, note=note.strip(),
                           on=action == "mark_reviewed")
        word = "reviewed" if action == "mark_reviewed" else "back to review"
        return Outcome(True, f"{len(chosen)} exception{'' if len(chosen) == 1 else 's'} "
                             f"marked {word}. No figure changed.",
                       touched=[r["id"] for r in chosen])

    def _chosen(self, scope: Scope, values: dict[str, str]
                ) -> tuple[dict[str, float] | None, Outcome | None]:
        if not _is_desk(scope):
            return None, _no("Choosing the thresholds is Finance's.")
        fields = self.COMPARISONS.get(values.get("comparison", "").strip())
        if fields is None:
            return None, _no("Choose which comparison this applies to.")
        preset = values.get("preset", "").strip()
        raw = values.get("custom", "").strip() if preset == "Custom" else preset.rstrip("%")
        if preset == "Custom" and not raw:
            return None, _no("Enter your custom percentage, or choose a predefined one.")
        pct = pnl_files.number(raw)
        if pct is None or not 0 < pct <= 1000:
            return None, _no("Give the threshold as a percentage above zero, such as 7.5.")
        chosen = {field: pct for field in fields}
        if values.get("floor", "").strip():
            floor = pnl_files.number(values["floor"])
            if floor is None or floor < 0:
                return None, _no("Give the smallest movement to report as a number.")
            chosen["floor"] = floor
        return chosen, None

    def _benchmarks(self, scope: Scope, values: dict[str, str]
                    ) -> tuple[dict[str, Any] | None, Outcome | None]:
        if not _is_desk(scope):
            return None, _no("Benchmarks are Finance's.")
        changes: dict[str, Any]
        changes, why = _numbers(values, ("vp_pct", "eb_pct", "dc_revenue_pct", "dc_per_fte",
                                         "revenue_tolerance_pct"))
        if why:
            return None, _no(why)
        text = values.get("eb_by_location", "").strip()
        if text:
            table = {}
            for part in re.split(r"[,;\n]", text):
                name, _, figure = part.partition("=")
                pct = pnl_files.number(figure)
                if name.strip() not in pnl.LOCATIONS or pct is None:
                    return None, _no("Write location benchmarks as Bengaluru=12, Kochi=11, "
                                     "using the locations Compass knows.")
                table[name.strip()] = pct
            changes["eb_by_location"] = table
        if not changes:
            return None, _no("Nothing to change.")
        if not values.get("why", "").strip():
            return None, _no("Say why. These decide what is reported as an exception.")
        return changes, None

    def preview(self, scope: Scope, form_id: str, values: dict[str, str],
                about: str = "") -> Outcome:
        if form_id == "set_benchmarks":
            changes, refusal = self._benchmarks(scope, values)
            if refusal is not None:
                return refusal
            return Outcome(True, f"Would set {', '.join(sorted(changes))} from now, for every "
                                 f"month. Figures do not change; only what is reported does.")
        month = _month(scope)
        if month is None:
            return _no("Choose a month first.")
        chosen, refusal = self._chosen(scope, values)
        if refusal is not None:
            return refusal
        current = len(self._rows(scope, month))
        would = _count_at(month, chosen, scope)
        return Outcome(True, f"{month_label(month)} would report {would} exceptions, "
                             f"against {current} now.")

    def submit(self, scope: Scope, form_id: str, values: dict[str, str],
               about: str = "") -> Outcome:
        if form_id == "set_benchmarks":
            changes, refusal = self._benchmarks(scope, values)
            if refusal is not None:
                return refusal
            try:
                fresh = pnl.set_rules(by=scope.user, why=values["why"], **changes)
            except ValueError as err:
                return _no(str(err))
            return Outcome(True, f"Rules v{fresh['version']} are in force.",
                           touched=[f"v{fresh['version']}"])
        month = _month(scope)
        if month is None:
            return _no("Choose a month first.")
        chosen, refusal = self._chosen(scope, values)
        if refusal is not None:
            return refusal
        pnl.select_thresholds(month, by=scope.user, **chosen)
        return Outcome(True, f"{month_label(month)} now reports "
                             f"{len(self._rows(scope, month))} exceptions.",
                       touched=[str(month)])


class PnlOutput(PnlScreen):
    """Leadership's file, and the run's trail.

    Leadership reads what has been released and nothing else on this screen.
    The drafts and the trail are the desk's working.
    """

    key = "pnl_output"
    needs_data = False

    PACK_FIELDS = frozenset({"sheets", "locations"})

    def accepts(self, form_id: str) -> set[str]:
        return set(self.PACK_FIELDS) if form_id == "build_pack" else set()

    def opening_tab(self, scope: Scope) -> str:
        return "packs"

    def _pack_row(self, scope: Scope, record: dict[str, Any]) -> dict[str, Any]:
        released = record["state"] == "released"
        stale = pnl.pack_is_stale(record)
        can = []
        if _is_desk(scope) and not released:
            can = ["release_despite"] if pnl.blockers(record["month"]) else ["release"]
        return {
            "id": record["id"], "pack": record["id"],
            "status": "Released" if released else "Draft",
            "sheets": ", ".join(record["sheets"]),
            "locations": ", ".join(record["locations"]) or "All",
            "built_by": record["by"], "built": pnl._day(record["at"]),  # noqa: SLF001
            "released_by": record.get("released_by", ""),
            "released": pnl._day(record.get("released_at", 0)),  # noqa: SLF001
            "note": record.get("release_note", ""),
            "released_despite": "; ".join(record.get("released_past", [])),
            "exceptions": record["exceptions"],
            "inputs": f"{len(record['inputs'])} files",
            "rules": f"v{record['rules_version']}",
            "newer_files": "Newer files have arrived since this was built" if stale else "",
            "stale": stale,
            "download": "Download the file",
            "can": can,
        }

    def _visible(self, scope: Scope, month: int) -> list[dict[str, Any]]:
        return [self._pack_row(scope, r) for r in pnl.packs(month)
                if _is_desk(scope) or r["state"] == "released"]

    def row_workbook(self, scope: Scope, row_id: str) -> bytes:
        """One pack's file, for whoever may see that pack. KeyError otherwise."""
        record = pnl.pack(row_id)
        if record is None or scope.sees != "firm":
            raise KeyError(row_id)
        if not (_is_desk(scope) or record["state"] == "released"):
            raise KeyError(row_id)
        return pnl.pack_file(record)

    def figures(self, scope: Scope) -> list[Figure]:
        if scope.sees != "firm":
            return []
        month = _month(scope) or pnl.latest_month()
        if month is None:
            return []
        rows = self._visible(scope, month)
        drafts = sum(r["status"] == "Draft" for r in rows)
        out = [Figure("released", str(sum(r["status"] == "Released" for r in rows)),
                      "released")]
        if _is_desk(scope):
            blocked = pnl.blockers(month)
            out += [Figure("drafts", str(drafts), f"drafts to decide for {month_label(month)}",
                           "warn" if drafts else "good"),
                    Figure("blockers", str(len(blocked)), "open before release",
                           "bad" if blocked else "good")]
        return out

    def tabs(self, scope: Scope) -> list[Tab]:
        month = self._open(scope)
        if month is None:
            return []
        shown = self._visible(scope, month)
        # Nothing released is all leadership can be told. An empty list under
        # a heading would read as a screen that failed to load.
        if not shown and not _is_desk(scope):
            return []
        tabs = [Tab("packs", "Packs", len(shown))]
        if _is_desk(scope):
            tabs += [Tab("inputs", "This month's inputs", len(pnl.inputs_of(month))),
                     Tab("trail", "What happened", len(pnl.trail(month)))]
        return tabs

    def columns(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        if tab == "inputs":
            return [_col("input", "Input"), _col("file", "File"),
                    _col("version", "Version", "whole"), _col("by", "By"),
                    _col("fingerprint", "Fingerprint")]
        if tab == "trail":
            return [_col("when", "When"), _col("who", "Who"), _col("what", "What")]
        return [_col("pack", "Pack"), _col("sheets", "Sheets"), _col("built_by", "Built by"),
                _col("built", "Built"), _col("released_by", "Released by"),
                _col("status", "Status")]

    def rows(self, scope: Scope, tab: str) -> list[dict[str, Any]]:
        month = self._open(scope)
        if month is None:
            return []
        if tab in ("inputs", "trail") and not _is_desk(scope):
            return []
        if tab == "inputs":
            return [{"id": i["id"], "input": i["kind"], "file": i["file"],
                     "version": i["version"], "by": i["by"],
                     "fingerprint": i["sha256"][:12], "can": []}
                    for i in pnl.inputs_of(month)]
        if tab == "trail":
            return [{"id": r["id"], "when": pnl._day(r["at"]),  # noqa: SLF001
                     "who": r["by"], "what": r["said"], "can": []}
                    for r in pnl.trail(month)]
        return self._visible(scope, month)

    def rules(self, scope: Scope) -> list[str]:
        said = super().rules(scope)
        month = _month(scope)
        if month is not None and _is_desk(scope) and pnl.blockers(month):
            said.append("not_ready_to_release")
        if (month is not None and scope.sees == "firm" and not _is_desk(scope)
                and not self._visible(scope, month)):
            said.append("nothing_released")
        return said

    def _asked(self, scope: Scope, values: dict[str, str]
               ) -> tuple[dict[str, Any] | None, Outcome | None]:
        if not _is_desk(scope):
            return None, _no("Building leadership's file is Finance's.")
        month = _month(scope)
        if month is None:
            return None, _no("Choose a month first.")
        if not pnl.view(month)["slots"]:
            return None, _no(f"Nothing has been uploaded for {month_label(month)} yet.")
        sheets = [s.strip() for s in values.get("sheets", "").split("|") if s.strip()]
        where = [s.strip() for s in values.get("locations", "").split("|") if s.strip()]
        if unknown := [s for s in sheets if s not in pnl.SHEETS]:
            return None, _no(f"Compass has no sheet called {unknown[0]!r}.")
        if unknown := [s for s in where if s not in pnl.LOCATIONS]:
            return None, _no(f"Compass has no location called {unknown[0]!r}.")
        if not sheets:
            return None, _no("Choose at least one sheet.")
        return {"month": month, "sheets": sheets, "locations": where}, None

    def preview(self, scope: Scope, form_id: str, values: dict[str, str],
                about: str = "") -> Outcome:
        asked, refusal = self._asked(scope, values)
        if refusal is not None:
            return refusal
        blocked = pnl.blockers(asked["month"])
        return Outcome(True, f"Builds a draft of {len(asked['sheets'])} chosen "
                             f"sheet{'' if len(asked['sheets']) == 1 else 's'} for "
                             f"{month_label(asked['month'])}, values only."
                             + (f" Still open before release: {'; '.join(blocked)}."
                                if blocked else ""))

    def submit(self, scope: Scope, form_id: str, values: dict[str, str],
               about: str = "") -> Outcome:
        asked, refusal = self._asked(scope, values)
        if refusal is not None:
            return refusal
        record = pnl.build_pack(asked["month"], by=scope.user, sheets=asked["sheets"],
                                locations=asked["locations"])
        return Outcome(True, f"{record['id']} built as a draft. Open it to download "
                             f"it or release it.", touched=[record["id"]])

    def act(self, scope: Scope, action: str, targets: list[str], note: str = "") -> Outcome:
        month = self._open(scope)
        if month is None:
            return _no("Choose a month first.")
        rows = {r["id"]: r for r in self._visible(scope, month)}
        chosen = [rows[t] for t in targets if t in rows]
        if len(chosen) != 1:
            return _no("One pack at a time.")
        row = chosen[0]
        if action not in row["can"]:
            blocked = pnl.blockers(month)
            if action == "release" and blocked:
                return _no(f"This month is not ready: {'; '.join(blocked)}. Fix those, "
                           f"or release it despite them and say why.")
            return _no(f"{row['pack']} is {row['status'].lower()}, so that does not apply.")
        if not note.strip():
            return _no("Say what you checked. The release is your decision, on the record.")
        blocked = pnl.blockers(month) if action == "release_despite" else []
        pnl.release_pack(pnl.pack(row["id"]), by=scope.user, note=note.strip(), past=blocked)
        return Outcome(True, f"{row['pack']} released"
                             + (f" despite {'; '.join(blocked)}" if blocked else "")
                             + ". Compass does not send it; distribution is yours.",
                       touched=[row["id"]])


def _no(said: str) -> Outcome:
    return Outcome(ok=False, said=said)


def _illustrative() -> bool:
    """Whether to say the rules are unconfirmed. The sample says it already."""
    return not pnl.rules()["confirmed"] and not pnl.is_sample()


def _selected(picked: dict[str, Any]) -> str:
    """The thresholds in use, as one figure when they are one figure."""
    three = {picked["plan_pct"], picked["py_pct"], picked["forecast_pct"]}
    if len(three) == 1:
        return f"{three.pop():g}%"
    return f"{picked['plan_pct']:g} / {picked['py_pct']:g} / {picked['forecast_pct']:g}%"


def _found(parsed: pnl_files.Parsed) -> str:
    segments = {pnl_files.norm(r["rs"]) for r in parsed.rows}
    lines = {pnl_files.norm(r["line"]) for r in parsed.rows}
    said = (f"{len(segments)} Reporting Segments, {len(lines)} P&L lines, "
            f"{len(parsed.rows)} figures")
    if parsed.totals:
        said += ", and its own totals to check against"
    if parsed.skipped:
        said += f" ({parsed.skipped} incomplete rows left out)"
    return said


def _refuse_same(held: dict[str, Any]) -> Outcome | None:
    """The same bytes, already counting for something. Duplicates stop here."""
    if pnl.is_sample():
        return None
    same = pnl.same_file(held["raw"])
    if same is None:
        return None
    where = (f"{same['location']} · {same['service_line']}, "
             f"{month_label(same['months'][0])}"
             if same["location"] else same["label"])
    return _no(f"This exact file is already current as {where} ({same['filename']}).")


def _numbers(values: dict[str, str], fields: tuple[str, ...]
             ) -> tuple[dict[str, float], str]:
    """The numeric fields somebody filled in, or what is wrong with one."""
    out: dict[str, float] = {}
    for field in fields:
        raw = values.get(field, "").strip()
        if not raw:
            continue
        figure = pnl_files.number(raw)
        if figure is None or figure < 0:
            return {}, f"{field.replace('_', ' ')} has to be a number, zero or more."
        out[field] = figure
    return out, ""


def _count_at(month: int, chosen: dict[str, float], scope: Scope) -> int:
    """How many the report would hold at these thresholds. Changes nothing."""
    trial = dict(pnl.selection(month), **chosen)
    return sum(pnl.in_location(r, scope.entity)
               for r in pnl.exceptions_at(month, trial)["found"])


register(PnlIntake())
register(PnlStatement())
register(PnlRecon())
register(PnlExceptions())
register(PnlOutput())
