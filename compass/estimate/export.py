"""An estimate, as something you can send to somebody.

Two formats, chosen because they are what actually happens to a cost estimate:
it gets pasted into a deck, and it gets argued with in a spreadsheet.

**PDF** is the report printed. Not re-laid-out — *printed*, from HTML written
here in the same shapes the screen uses, through Compass's own headless
Chromium. The original drew its PDF with reportlab, which meant a second visual
language: a second set of fonts, a second table style, a second place for every
future change to be made and forgotten. Printing HTML costs a browser on the
server and buys a document that looks like the product it came from.

**Excel** is the numbers, in five sheets, unformatted enough to be usable.
This one is ported nearly as it was, because a workbook is a data structure
rather than a design, and the original's five-sheet split — summary, cost,
tokens, ROI, recommendations — is a good one. What is added is the two columns
that make an infrastructure line checkable: which region it was priced in, and
whether that price was live or the catalog baseline.

Both take the stored record rather than a live recompute. An estimate is a
dated artefact: exporting it must produce the figures that were agreed, not
today's answer to the same question.
"""

from __future__ import annotations

import html as _html
import io
import logging

from .types import Estimation

logger = logging.getLogger("compass.estimate")


def _money(currency: str, amount: float) -> str:
    # The code rather than the symbol. On screen this is `$84,329`, because
    # the reader is inside an app whose currency they just chose; a printed
    # page has no such context and travels further than the person who made
    # it, so it says which dollar it means.
    return f"{currency} {amount:,.0f}"


def _num(value: float | int | None, suffix: str = "") -> str:
    """A number as a person would write it: 6 rather than 6.0, 6.5 kept.

    The model's floats print their trailing zero, and a table of `75.0` and
    `70.0` reads like machine output rather than like a rate and a
    percentage."""
    if value is None:
        return "—"
    text = f"{value:,.0f}" if float(value) == int(value) else f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def slug(name: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in name).strip("-").lower()
    return out or "estimate"


# --------------------------------------------------------------------------- pdf


def _e(value: object) -> str:
    """Escape for HTML. Every string below goes through this: a project name is
    typed by a person, and a report that renders `<script>` because somebody
    called their project that is a report that cannot be shared."""
    return _html.escape(str(value if value is not None else ""))


_PRINT_CSS = """
@page { size: A4; margin: 16mm 14mm; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 10pt/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  color: #1d1d1f;
}
h1 { font-size: 19pt; font-weight: 600; margin: 0 0 2mm; letter-spacing: -0.02em; }
h2 {
  font-size: 8pt; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase;
  color: #8a6a00; margin: 8mm 0 2.5mm;
}
.sub { color: #6b6b70; font-size: 9pt; margin: 0 0 5mm; }
/* Nothing is allowed to break across a page in the middle of an argument. */
section { break-inside: avoid; }
.verdict {
  border: 1px solid #e3e3e6; border-left: 3px solid #8a6a00;
  border-radius: 3mm; padding: 4mm 5mm; margin-bottom: 5mm;
}
.verdict h3 { margin: 1mm 0 1.5mm; font-size: 14pt; font-weight: 600; }
.verdict p { margin: 0; color: #4a4a4f; }
.kicker { font-size: 7.5pt; font-weight: 700; letter-spacing: 0.14em;
          text-transform: uppercase; color: #8a8a90; }
.kpis { display: flex; gap: 3mm; margin-bottom: 5mm; }
.kpi { flex: 1; border: 1px solid #e3e3e6; border-radius: 3mm; padding: 3mm 3.5mm; }
.kpi .v { font-size: 13pt; font-weight: 600; margin-top: 1.5mm;
          font-variant-numeric: tabular-nums; }
.kpi .n { font-size: 7.5pt; color: #8a8a90; margin-top: 1mm; }
table { width: 100%; border-collapse: collapse; font-size: 8.5pt; }
th {
  text-align: left; font-size: 7pt; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: #8a8a90;
  border-bottom: 0.4pt solid #d8d8dc; padding: 0 2mm 1.5mm;
}
td { padding: 1.6mm 2mm; border-bottom: 0.4pt solid #ececef; vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
ul, ol { margin: 0; padding-left: 5mm; }
li { margin-bottom: 1.5mm; }
/* The work-breakdown rows. A phase is a rule across the table, a module is
   bold, and a sub-feature is indented under it — the hierarchy has to survive
   losing colour, because this page is printed. */
tr.ph td { border-top: 0.8pt solid #1d1d1f; padding-top: 3mm; }
tr.md td { background: #f6f6f8; font-weight: 600; }
td.ind { padding-left: 6mm; color: #55555c; }
.foot { margin-top: 8mm; padding-top: 3mm; border-top: 0.4pt solid #d8d8dc;
        font-size: 7.5pt; color: #8a8a90; }
"""


def to_html(est: Estimation) -> str:
    """The printable report. Self-contained: no fetches, no fonts to wait for."""
    cur = est.cost_breakdown.currency
    money = lambda n: _e(_money(cur, n))  # noqa: E731 - a local alias, used 40 times
    cost = est.cost_breakdown
    roi = est.roi_projection
    cmp_ = est.comparison
    mode = "Enhancement" if est.project_type == "enhancement" else "New build"

    parts: list[str] = [
        f"<style>{_PRINT_CSS}</style>",
        f"<h1>{_e(est.project_name)}</h1>",
        f'<p class="sub">AI feasibility &amp; cost · {_e(mode)}'
        f'{" · " + _e(est.industry_domain) if est.industry_domain else ""}'
        f' · {_e(est.generated_at[:10])}</p>',
    ]

    if est.verdict:
        conf = (f" · {est.confidence.level} confidence ({est.confidence.score}/100)"
                if est.confidence else "")
        parts.append(
            '<section class="verdict">'
            f'<span class="kicker">Verdict{_e(conf)}</span>'
            f"<h3>{_e(est.verdict.headline)}</h3>"
            f"<p>{_e(est.verdict.one_liner)}</p></section>"
        )

    payback = ("none modelled" if roi.payback_months is None
               else f"{roi.payback_months} months")
    parts.append(
        '<section class="kpis">'
        f'<div class="kpi"><span class="kicker">First year</span>'
        f'<div class="v">{money(cost.total.expected)}</div>'
        f'<div class="n">{money(cost.total.min)} – {money(cost.total.max)}</div></div>'
        f'<div class="kpi"><span class="kicker">Year one benefit</span>'
        f'<div class="v">{money(roi.first_year_benefit)}</div>'
        f'<div class="n">{money(roi.annual_benefit)} a year at full adoption</div></div>'
        f'<div class="kpi"><span class="kicker">Payback</span>'
        f'<div class="v">{_e(payback)}</div>'
        f'<div class="n">feasibility {est.feasibility.score}/100</div></div>'
        f'<div class="kpi"><span class="kicker">Three years</span>'
        f'<div class="v">{money(roi.three_year_value)}</div>'
        f'<div class="n">{est.roi_projection.roi_percent}% return</div></div>'
        "</section>"
    )

    rows = "".join(
        f"<tr><td>{_e(label)}</td><td class='num'>{money(amount)}</td></tr>"
        for label, amount in (
            ("Development", cost.development.total_cost),
            ("Infrastructure (annual)", cost.infrastructure.annual_cost),
            ("AI tokens (annual, expected)", cost.ai_tokens.annual_cost.expected),
            ("Maintenance (annual)", cost.maintenance.annual_cost),
        )
    )
    parts.append(
        "<section><h2>First-year cost</h2><table>"
        "<thead><tr><th>Part</th><th class='num'>Amount</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        f"<tfoot><tr><td><b>Total (expected)</b></td>"
        f"<td class='num'><b>{money(cost.total.expected)}</b></td></tr></tfoot>"
        "</table></section>"
    )

    # The development detail, module by module. Printed between the cost summary
    # and the infrastructure, because it is the line item most people argue
    # with — and the one they need in front of them to argue usefully.
    wbs = cost.development.work_breakdown
    if wbs is not None and wbs.phases:
        body: list[str] = []
        for ph in wbs.phases:
            body.append(
                f"<tr class='ph'><td colspan='2'><b>Phase {ph.phase}</b></td>"
                f"<td class='num'><b>{_e(_num(ph.hours, 'h'))}</b></td>"
                f"<td class='num'><b>{money(ph.cost)}</b></td>"
                f"<td class='num'><b>{_e(_num(ph.weeks, ' wks'))}</b></td></tr>"
            )
            for mod in ph.modules:
                added = " <i>added by the engine</i>" if mod.derived else ""
                body.append(
                    f"<tr class='md'><td colspan='2'>{_e(mod.name)}{added}</td>"
                    f"<td class='num'>{_e(_num(mod.hours, 'h'))}</td>"
                    f"<td class='num'>{money(mod.cost)}</td>"
                    f"<td class='num'>{_e(_num(mod.weeks, ' wks'))}</td></tr>"
                )
                for sub in mod.sub_features:
                    body.append(
                        f"<tr><td class='ind'>{_e(sub.name)}</td>"
                        f"<td>{_e(sub.size.upper() + ' · ' if sub.size else '')}"
                        f"{_e(_num(sub.units, 'd'))}</td>"
                        f"<td class='num'>{_e(_num(sub.hours, 'h'))}</td>"
                        f"<td class='num'>{money(sub.cost)}</td><td></td></tr>"
                    )
        ceiling = ""
        if wbs.at_ceiling:
            names = "; ".join(_e(n) for n in wbs.at_ceiling)
            plural = "" if len(wbs.at_ceiling) == 1 else "s"
            ceiling = (
                f"<p><b>{len(wbs.at_ceiling)} line{plural} at the "
                f"{wbs.unit_hours * 10}-hour ceiling.</b> Not an error — a flag that the "
                f"estimate has reached the edge of what it understands. Break these down "
                f"before anyone commits to the number: {names}.</p>"
            )
        parts.append(
            "<section><h2>Development — module by module</h2>"
            f"<p class='sub'>One unit = {wbs.unit_hours}h · elapsed weeks assume "
            f"{_team_phrase(wbs, html=True)}</p>"
            "<table><thead><tr><th>Line</th><th>Size</th><th class='num'>Hours</th>"
            "<th class='num'>Cost</th><th class='num'>Weeks</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody>"
            f"<tfoot><tr><td colspan='2'><b>Total</b></td>"
            f"<td class='num'><b>{_e(_num(wbs.total_hours, 'h'))}</b></td>"
            f"<td class='num'><b>{money(wbs.total_cost)}</b></td>"
            f"<td class='num'><b>{_e(_num(wbs.total_weeks, ' wks'))}</b></td></tr></tfoot>"
            f"</table>{ceiling}</section>"
        )

    infra = "".join(
        f"<tr><td>{_e(s.service_name)}</td><td>{_e(s.tier)}</td>"
        f"<td>{_e(s.region)}</td><td>{_e(s.price_source)}</td>"
        f"<td class='num'>{money(s.monthly_cost)}</td></tr>"
        for s in cost.infrastructure.services if s.included_in_total
    )
    parts.append(
        f"<section><h2>Infrastructure — {_e(cost.infrastructure.platform_label)}</h2>"
        "<table><thead><tr><th>Service</th><th>Tier</th><th>Region</th>"
        "<th>Price source</th><th class='num'>Monthly</th></tr></thead>"
        f"<tbody>{infra}</tbody>"
        f"<tfoot><tr><td colspan='4'><b>Monthly</b></td>"
        f"<td class='num'><b>{money(cost.infrastructure.monthly_cost)}</b></td>"
        "</tr></tfoot></table></section>"
    )

    drivers = "".join(
        f"<tr><td>{_e(d.use_case)}</td>"
        f"<td class='num'>{_e(_num(d.minutes_per_call))}</td>"
        f"<td class='num'>{_e('—' if d.loaded_hourly_rate is None else _money(cur, d.loaded_hourly_rate))}</td>"
        f"<td class='num'>{_e(_num(d.automation_rate_percent, '%'))}</td>"
        f"<td class='num'>{money(d.annual_value)}</td></tr>"
        for d in roi.value_drivers
    )
    parts.append(
        "<section><h2>Where the value comes from</h2><table>"
        "<thead><tr><th>Use case</th><th class='num'>Minutes/call</th>"
        "<th class='num'>Loaded rate</th><th class='num'>Automated</th>"
        "<th class='num'>Value/year</th></tr></thead>"
        f"<tbody>{drivers}</tbody></table></section>"
    )

    parts.append(
        "<section><h2>AI against standard software</h2>"
        f"<p>{_e(cmp_.summary)}</p><table>"
        "<thead><tr><th>Approach</th><th class='num'>Expected</th>"
        "<th class='num'>Timeline</th><th class='num'>Team</th>"
        "<th class='num'>Monthly run</th></tr></thead><tbody>"
        f"<tr><td>With AI</td><td class='num'>{money(cmp_.ai_approach.total_cost.expected)}</td>"
        f"<td class='num'>{cmp_.ai_approach.timeline_weeks} wks</td>"
        f"<td class='num'>{cmp_.ai_approach.team_size}</td>"
        f"<td class='num'>{money(cmp_.ai_approach.monthly_run_cost)}</td></tr>"
        f"<tr><td>Standard</td><td class='num'>{money(cmp_.standard_approach.total_cost.expected)}</td>"
        f"<td class='num'>{cmp_.standard_approach.timeline_weeks} wks</td>"
        f"<td class='num'>{cmp_.standard_approach.team_size}</td>"
        f"<td class='num'>{money(cmp_.standard_approach.monthly_run_cost)}</td></tr>"
        "</tbody></table></section>"
    )

    if est.recommendations:
        recs = "".join(
            f"<li><b>{_e(r.title)}</b> — {_e(r.description)} "
            f"<i>{_e(r.estimated_impact)}</i></li>"
            for r in est.recommendations
        )
        parts.append(f"<section><h2>What to do next</h2><ul>{recs}</ul></section>")

    if est.assumptions:
        stated = "".join(f"<li>{_e(a)}</li>" for a in est.assumptions)
        parts.append(
            "<section><h2>Scope assumptions</h2>"
            f"<ol>{stated}</ol></section>"
        )

    if roi.assumptions:
        basis = "".join(f"<li>{_e(a)}</li>" for a in roi.assumptions)
        parts.append(f"<section><h2>Basis</h2><ul>{basis}</ul></section>")

    parts.append(
        '<p class="foot">Every figure was computed from the brief by Compass\'s '
        "deterministic engine against a versioned rate card. A model labelled the "
        "use cases and chose the delivery platform; it computed nothing. The same "
        "brief produces the same estimate.</p>"
    )
    return "".join(parts)


def _team_phrase(wbs, *, html: bool) -> str:
    """What the elapsed weeks assume. The team as entered, by discipline, when
    there is one; the rate card's parallel engineers otherwise."""
    if wbs.roles:
        people = " · ".join(f"{r.headcount} {r.label.lower()}" for r in wbs.roles)
        people = _e(people) if html else people
        return f"the team entered ({people}); a phase runs as long as its slowest discipline"
    count = _e(_num(wbs.team_size)) if html else _num(wbs.team_size)
    return f"{count} engineers in parallel"


async def to_pdf(est: Estimation) -> bytes:
    from compass.common.screenshot import html_to_pdf

    return await html_to_pdf(to_html(est))


# --------------------------------------------------------------------------- excel


def to_excel(est: Estimation) -> bytes:
    """Summary, cost, the work breakdown, assumptions, tokens, ROI and
    recommendations. The two middle sheets appear only when the brief carried
    a breakdown and assumptions to put in them."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    cur = est.cost_breakdown.currency
    wb = Workbook()

    header_fill = PatternFill("solid", fgColor="1D1D1F")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    title_font = Font(color="8A6A00", bold=True, size=13)
    bold = Font(bold=True)

    def style_header(ws, row: int, ncols: int) -> None:
        for col in range(1, ncols + 1):
            cell = ws.cell(row=row, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="left", vertical="center")

    def widths(ws, cols: list[int]) -> None:
        for i, w in enumerate(cols, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # ── Summary ──
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = est.project_name
    ws["A1"].font = title_font
    mode = "Enhancement" if est.project_type == "enhancement" else "New build"
    ws["A2"] = f"AI feasibility & cost · {mode} · {est.generated_at[:10]}"

    f = est.feasibility
    rows: list[tuple[str, object]] = [("Recommendation", f.archetype_label)]
    if est.verdict is not None:
        rows += [("Verdict", est.verdict.headline),
                 ("Verdict rationale", est.verdict.one_liner)]
    if est.confidence is not None:
        rows.append(("Confidence",
                     f"{est.confidence.level.capitalize()} ({est.confidence.score}/100)"))
    rows += [
        ("Feasibility score", f"{f.score}/100 ({f.rating})"),
        ("AI necessity", f.sub_scores.ai_necessity),
        ("Agentic suitability", f.sub_scores.agentic_suitability),
        ("Traditional suitability", f.sub_scores.traditional_suitability),
        ("Delivery platform", est.cost_breakdown.infrastructure.platform_label),
        ("Total expected (first year)", est.cost_breakdown.total.expected),
        ("Total range", f"{est.cost_breakdown.total.min} – {est.cost_breakdown.total.max}"),
        ("Currency", cur),
    ]
    r = 4
    for label, value in rows:
        ws.cell(row=r, column=1, value=label).font = bold
        ws.cell(row=r, column=2, value=value)
        r += 1

    if est.confidence is not None:
        r += 1
        ws.cell(row=r, column=1, value="Confidence factors").font = bold
        ws.cell(row=r, column=2, value=est.confidence.rationale)
        r += 1
        for factor in est.confidence.factors:
            ws.cell(row=r, column=1, value=f"  {factor.label} ({factor.impact})")
            ws.cell(row=r, column=2, value=factor.detail)
            r += 1
    widths(ws, [30, 62])

    # ── Cost breakdown ──
    ws = wb.create_sheet("Cost Breakdown")
    ws.append(["Category", "Detail", "Region", "Price source", f"Amount ({cur})"])
    style_header(ws, 1, 5)
    c = est.cost_breakdown
    for b in c.development.breakdown:
        ws.append(["Development", f"{b.category} ({b.hours}h)", "", "", b.cost])
    ws.append(["Development", "Total development", "", "", c.development.total_cost])
    for svc in c.infrastructure.services:
        # The region and the price source are what make a line checkable: a
        # figure you can trace beats a figure you have to trust.
        ws.append([
            "Infrastructure (monthly)" if svc.included_in_total else "Infrastructure (not counted)",
            f"{svc.service_name} — {svc.tier}", svc.region, svc.price_source, svc.monthly_cost,
        ])
    ws.append(["Infrastructure", "Annual infrastructure", "", "", c.infrastructure.annual_cost])
    for label, value in (("optimistic", c.ai_tokens.annual_cost.optimistic),
                         ("expected", c.ai_tokens.annual_cost.expected),
                         ("pessimistic", c.ai_tokens.annual_cost.pessimistic)):
        ws.append(["AI tokens", f"Annual ({label})", "", "", value])
    ws.append(["Maintenance",
               f"{c.maintenance.monthly_hours}h/mo @ {_money(cur, c.maintenance.hourly_rate)}/h",
               "", "", c.maintenance.annual_cost])
    ws.append(["TOTAL", "First-year expected", "", "", c.total.expected])
    ws.cell(row=ws.max_row, column=1).font = bold
    ws.cell(row=ws.max_row, column=5).font = bold
    widths(ws, [26, 44, 12, 13, 18])

    # ── Work breakdown ──
    #
    # The sheet an architect actually hands over: every line that was sized,
    # what it was sized at, and what it rolls up to. Only present when the
    # brief was broken down — an estimate made from a flat feature list has no
    # breakdown, and an empty sheet would imply one exists.
    wbs = c.development.work_breakdown
    if wbs is not None and wbs.phases:
        ws = wb.create_sheet("Work Breakdown")
        ws.append(["Phase", "Module", "Sub-feature", "Size", "Days", "Hours",
                   f"Cost ({cur})", "Weeks"])
        style_header(ws, 1, 8)
        for ph in wbs.phases:
            for mod in ph.modules:
                for sub in mod.sub_features:
                    ws.append([ph.phase, mod.name, sub.name, sub.size.upper() or "—",
                               sub.units, sub.hours, sub.cost, ""])
                ws.append([ph.phase,
                           f"{mod.name} (added by the engine)" if mod.derived else mod.name,
                           "Module total", "", "", mod.hours, mod.cost, mod.weeks])
                for col in (3, 6, 7, 8):
                    ws.cell(row=ws.max_row, column=col).font = bold
            ws.append([ph.phase, f"PHASE {ph.phase} TOTAL", "", "", "",
                       ph.hours, ph.cost, ph.weeks])
            for col in range(1, 9):
                ws.cell(row=ws.max_row, column=col).font = bold
        ws.append(["", "GRAND TOTAL", "", "", "",
                   wbs.total_hours, wbs.total_cost, wbs.total_weeks])
        for col in range(1, 9):
            ws.cell(row=ws.max_row, column=col).font = bold
        ws.append([])
        ws.append(["", f"One unit = {wbs.unit_hours}h · weeks assume "
                       f"{_team_phrase(wbs, html=False)}"])
        if wbs.at_ceiling:
            ws.append([])
            ws.append(["", f"At the {wbs.unit_hours * 10}-hour ceiling — "
                           "break these down before committing:"])
            ws.cell(row=ws.max_row, column=2).font = bold
            for name in wbs.at_ceiling:
                ws.append(["", name])
        widths(ws, [8, 34, 44, 8, 8, 9, 15, 9])

    # ── Assumptions ──
    if est.assumptions:
        ws = wb.create_sheet("Assumptions")
        ws.append(["#", "Assumption"])
        style_header(ws, 1, 2)
        for i, line in enumerate(est.assumptions, start=1):
            ws.append([i, line])
        widths(ws, [6, 110])
        for row in ws.iter_rows(min_row=2, min_col=2, max_col=2):
            row[0].alignment = Alignment(wrap_text=True, vertical="top")

    # ── Tokens ──
    ws = wb.create_sheet("Tokens")
    ws.append(["Model", "Use cases", "Monthly input tokens", "Monthly output tokens",
               f"Monthly cost ({cur})", "In $/1M", "Out $/1M"])
    style_header(ws, 1, 7)
    for m in c.ai_tokens.model_breakdown:
        ws.append([m.model, ", ".join(m.use_cases), m.monthly_input_tokens,
                   m.monthly_output_tokens, m.monthly_cost,
                   m.input_price_per_1m, m.output_price_per_1m])
    widths(ws, [20, 34, 20, 20, 17, 10, 10])

    # ── ROI ──
    ws = wb.create_sheet("ROI")
    roi = est.roi_projection
    ws.append(["Metric", "Value"])
    style_header(ws, 1, 2)
    for label, value in (
        ("Annual benefit (steady state)", roi.annual_benefit),
        ("Year one benefit (ramped)", roi.first_year_benefit),
        ("Adoption ramp (months)", roi.benefit_ramp_months),
        ("Annual run cost", roi.annual_run_cost),
        ("Net annual benefit", roi.net_annual_benefit),
        ("Payback (months)", roi.payback_months if roi.payback_months is not None else "none"),
        ("3-year net value", roi.three_year_value),
        ("3-year ROI %", roi.roi_percent),
    ):
        ws.append([label, value])

    ws.append([])
    ws.append(["Use case", "Minutes/call", "Loaded $/hr", "Automation %",
               "Value/call", "Annual calls", "Annual value"])
    style_header(ws, ws.max_row, 7)
    for d in roi.value_drivers:
        ws.append([
            d.use_case,
            # Numbers stay numbers in a spreadsheet — this is the one place
            # a reader will sort and total them.
            d.minutes_per_call, d.loaded_hourly_rate, d.automation_rate_percent,
            d.value_per_call, d.annual_calls, d.annual_value,
        ])

    ws.append([])
    ws.append(["Basis"])
    style_header(ws, ws.max_row, 1)
    for line in roi.assumptions:
        ws.append([line])

    ws.append([])
    ws.append(["Month", "Cumulative net"])
    style_header(ws, ws.max_row, 2)
    for point in roi.curve:
        ws.append([point.month, point.cumulative_net])
    widths(ws, [30, 14, 13, 15, 13, 15, 17])

    # ── Recommendations ──
    ws = wb.create_sheet("Recommendations")
    ws.append(["Priority", "Category", "Title", "Description", "Estimated impact"])
    style_header(ws, 1, 5)
    for rec in est.recommendations:
        ws.append([rec.priority, rec.category, rec.title, rec.description, rec.estimated_impact])
    for col in range(1, 6):
        for row in range(2, ws.max_row + 1):
            ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
    widths(ws, [11, 15, 32, 52, 40])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
