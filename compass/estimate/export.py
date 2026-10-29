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
td.ind .cov { font-size: 7.5pt; color: #8a8a90; margin-top: 0.6mm; }
/* The written report: its own title and sub-headings sit inside the section,
   one step below the section heading, so it reads as a document within. */
.wr p { margin: 0 0 2mm; }
.wr .wr-h { font-size: 11pt; font-weight: 600; margin: 0 0 2mm; }
.wr .wr-s { font-size: 8pt; font-weight: 700; letter-spacing: 0.08em;
            text-transform: uppercase; color: #6b6b70; margin: 3mm 0 1.5mm; }
.foot { margin-top: 8mm; padding-top: 3mm; border-top: 0.4pt solid #d8d8dc;
        font-size: 7.5pt; color: #8a8a90; }
"""


def _shown(display: dict | None) -> tuple[bool, bool, bool]:
    """(development, infrastructure, maintenance) — which costs are shown.

    Infrastructure is the services, model usage included; maintenance is the
    people who keep them up, and has its own switch. A total is printed only
    when all three are: a total beside one hidden part gives that part away
    by subtraction.
    """
    d = display or {}
    return (bool(d.get("development_cost", True)), bool(d.get("infrastructure_cost", True)),
            bool(d.get("maintenance_cost", True)))


#: The written report's cost lines, by the flag that hides each. The engine
#: writes every cost line under one of these prefixes, which is what makes
#: dropping them safe; the screen drops exactly the same ones.
_REPORT_COST_LINES: tuple[tuple[str, str], ...] = (
    ("- Development:", "dev"),
    ("- Infrastructure:", "infra"),
    ("- AI tokens:", "infra"),
    ("- Maintenance:", "maint"),
    ("- **Total expected", "total"),
)


def _single(mod):
    """The module's one line, when the module *is* that line — one sub-feature
    named as the module is, the way a sheet priced line by line arrives.
    Printed as a parent with a child of the same name, it says every title
    twice; a single line with a name of its own keeps its row."""
    def key(s: str) -> str:
        return " ".join((s or "").casefold().split())
    subs = mod.sub_features
    return subs[0] if len(subs) == 1 and key(subs[0].name) == key(mod.name) else None


def _size_label(sub) -> str:
    return (sub.basis if sub.basis else
            (sub.size.upper() + " · " if sub.size else "") + _num(sub.units, "d"))


def _md_inline(text: str) -> str:
    """One line of the report, escaped first and then given its bold.

    Escaped before anything else: the report carries the project name, which
    a person typed, and nothing in it may become markup."""
    out = _e(text)
    parts = out.split("**")
    # An odd count of `**` leaves the last one literal rather than unclosed.
    if len(parts) % 2 == 1:
        out = "".join(f"<b>{p}</b>" if i % 2 else p for i, p in enumerate(parts))
    return out


def _report_html(markdown: str, shown: dict[str, bool]) -> str:
    """The written report, as the PDF prints it.

    The engine writes four shapes — a title, sub-headings, bullet lists and
    paragraphs — so this renders those four and no more, rather than taking
    on a markdown library for a document whose grammar is ours. Any cost line
    a flag hides is dropped before rendering, so a hidden figure never reaches
    the page in the one section that states them all again.
    """
    html: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            html.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if any(line.startswith(prefix) and not shown[flag]
               for prefix, flag in _REPORT_COST_LINES):
            continue
        if line.startswith("- "):
            bullets.append(_md_inline(line[2:]))
            continue
        flush()
        if not line.strip():
            continue
        if line.startswith("## "):
            html.append(f"<p class='wr-s'>{_md_inline(line[3:])}</p>")
        elif line.startswith("# "):
            html.append(f"<p class='wr-h'>{_md_inline(line[2:])}</p>")
        else:
            html.append(f"<p>{_md_inline(line)}</p>")
    flush()
    return "".join(html)


def to_html(est: Estimation, display: dict | None = None) -> str:
    """The printable report. Self-contained: no fetches, no fonts to wait for.

    `display` is the record's flags: a cost switched off is left out of every
    table, total and figure it would otherwise appear in, and the hours stay.
    """
    cur = est.cost_breakdown.currency
    money = lambda n: _e(_money(cur, n))  # noqa: E731 - a local alias, used 40 times
    cost = est.cost_breakdown
    roi = est.roi_projection
    mode = "Enhancement" if est.project_type == "enhancement" else "New build"
    show_dev, show_infra, show_maint = _shown(display)
    show_total = show_dev and show_infra and show_maint

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
    wbs = cost.development.work_breakdown
    if est.ai_applicable:
        kpis = (
            f'<div class="kpi"><span class="kicker">Year one benefit</span>'
            f'<div class="v">{money(roi.first_year_benefit)}</div>'
            f'<div class="n">{money(roi.annual_benefit)} a year at full adoption</div></div>'
        )
        # Payback and three-year value are benefit less the whole cost, so
        # they would give a hidden cost away as surely as a total would.
        if show_total:
            kpis += (
                f'<div class="kpi"><span class="kicker">Payback</span>'
                f'<div class="v">{_e(payback)}</div>'
                f'<div class="n">feasibility {est.feasibility.score}/100</div></div>'
                f'<div class="kpi"><span class="kicker">Three years</span>'
                f'<div class="v">{money(roi.three_year_value)}</div>'
                f'<div class="n">{est.roi_projection.roi_percent}% return</div></div>'
            )
    else:
        # A benefit of nothing and a payback of never are not findings about a
        # system with no AI in it; they are the AI model reporting that it was
        # not used. What a reader of a standard build wants on the first page
        # is how much work it is and what it costs to keep running.
        hours = wbs.total_hours if wbs is not None and wbs.phases else sum(
            b.hours for b in cost.development.breakdown)
        weeks = wbs.total_weeks if wbs is not None and wbs.phases else 0
        build_note = f"{money(cost.development.total_cost)} to build" if show_dev else "of work"
        kpis = (
            f'<div class="kpi"><span class="kicker">Effort</span>'
            f'<div class="v">{_e(_num(hours, " h"))}</div>'
            f'<div class="n">{build_note}</div></div>'
            f'<div class="kpi"><span class="kicker">Timeline</span>'
            f'<div class="v">{_e(_num(weeks, " wks"))}</div>'
            f'<div class="n">{_e(_team_phrase(wbs, html=False)) if wbs is not None and wbs.phases else ""}</div></div>'
        )
        # What it costs to keep running, from whichever of its two parts is
        # shown, and saying which — "to run" of the services alone is not the
        # same figure as the services and the people.
        run = [(cost.infrastructure.monthly_cost, "infrastructure")] if show_infra else []
        run += [(cost.maintenance.monthly_cost, "maintenance")] if show_maint else []
        if run:
            kpis += (
                f'<div class="kpi"><span class="kicker">To run</span>'
                f'<div class="v">{money(sum(a for a, _ in run))}</div>'
                f'<div class="n">a month — {" and ".join(w for _, w in run)}</div></div>'
            )
    first_year = (
        f'<div class="kpi"><span class="kicker">First year</span>'
        f'<div class="v">{money(cost.total.expected)}</div>'
        f'<div class="n">{money(cost.total.min)} – {money(cost.total.max)}</div></div>'
        if show_total else ""
    )
    parts.append(f'<section class="kpis">{first_year}{kpis}</section>')

    rows = "".join(
        f"<tr><td>{_e(label)}</td><td class='num'>{money(amount)}</td></tr>"
        for label, amount, shown in (
            ("Development", cost.development.total_cost, show_dev),
            ("Infrastructure (annual)", cost.infrastructure.annual_cost, show_infra),
            ("AI tokens (annual, expected)", cost.ai_tokens.annual_cost.expected, show_infra),
            ("Maintenance (annual)", cost.maintenance.annual_cost, show_maint),
        )
        # A zero row for tokens on a system that calls no model reads as a
        # model that happens to be free.
        if shown and (est.ai_applicable or not label.startswith("AI tokens"))
    )
    if rows:
        total = (f"<tfoot><tr><td><b>Total (expected)</b></td>"
                 f"<td class='num'><b>{money(cost.total.expected)}</b></td></tr></tfoot>"
                 if show_total else "")
        parts.append(
            "<section><h2>First-year cost</h2><table>"
            "<thead><tr><th>Part</th><th class='num'>Amount</th></tr></thead>"
            f"<tbody>{rows}</tbody>{total}</table></section>"
        )

    # The development detail, module by module. Printed between the cost summary
    # and the infrastructure, because it is the line item most people argue
    # with — and the one they need in front of them to argue usefully.
    if wbs is not None and wbs.phases:
        # The money column goes and the hours stay: effort is what an estimate
        # shared without its rate card is for.
        def cost_cell(n, bold=False):
            if not show_dev:
                return ""
            return f"<td class='num'><b>{money(n)}</b></td>" if bold else f"<td class='num'>{money(n)}</td>"

        body: list[str] = []
        for ph in wbs.phases:
            body.append(
                f"<tr class='ph'><td colspan='2'><b>Phase {ph.phase}</b></td>"
                f"<td class='num'><b>{_e(_num(ph.hours, 'h'))}</b></td>"
                f"{cost_cell(ph.cost, bold=True)}"
                f"<td class='num'><b>{_e(_num(ph.weeks, ' wks'))}</b></td></tr>"
            )
            for mod in ph.modules:
                added = " <i>added by the engine</i>" if mod.derived else ""
                line = _single(mod)
                if line is not None:
                    covers = (f"<div class='cov'>covers {_e(' · '.join(line.covers))}</div>"
                              if line.covers else "")
                    body.append(
                        f"<tr class='md'><td>{_e(mod.name)}{added}{covers}</td>"
                        f"<td>{_e(_size_label(line))}</td>"
                        f"<td class='num'>{_e(_num(mod.hours, 'h'))}</td>"
                        f"{cost_cell(mod.cost)}"
                        f"<td class='num'>{_e(_num(mod.weeks, ' wks'))}</td></tr>"
                    )
                    continue
                body.append(
                    f"<tr class='md'><td colspan='2'>{_e(mod.name)}{added}</td>"
                    f"<td class='num'>{_e(_num(mod.hours, 'h'))}</td>"
                    f"{cost_cell(mod.cost)}"
                    f"<td class='num'>{_e(_num(mod.weeks, ' wks'))}</td></tr>"
                )
                for sub in mod.sub_features:
                    size = _e(_size_label(sub))
                    # What the line includes without pricing it, under its
                    # name — the architect's own layout, where the sub-items
                    # sit beneath the line that carries their hours.
                    covers = (f"<div class='cov'>covers {_e(' · '.join(sub.covers))}</div>"
                              if sub.covers else "")
                    body.append(
                        f"<tr><td class='ind'>{_e(sub.name)}{covers}</td>"
                        f"<td>{size}</td>"
                        f"<td class='num'>{_e(_num(sub.hours, 'h'))}</td>"
                        f"{cost_cell(sub.cost)}<td></td></tr>"
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
            f"{'<th class=num>Cost</th>' if show_dev else ''}"
            "<th class='num'>Weeks</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody>"
            f"<tfoot><tr><td colspan='2'><b>Total</b></td>"
            f"<td class='num'><b>{_e(_num(wbs.total_hours, 'h'))}</b></td>"
            f"{cost_cell(wbs.total_cost, bold=True)}"
            f"<td class='num'><b>{_e(_num(wbs.total_weeks, ' wks'))}</b></td></tr></tfoot>"
            f"</table>{ceiling}</section>"
        )

    # The stack is printed whatever the flag says; the flag takes away what it
    # costs. Every service is listed, counted or not — Power BI is part of the
    # build even though its licences are the organisation's line, not this one.
    services = cost.infrastructure.services
    if show_infra:
        infra = "".join(
            f"<tr><td>{_e(s.service_name)}</td><td>{_e(s.tier)}</td>"
            f"<td>{_e(s.region)}</td><td>{_e(s.price_source)}</td>"
            f"<td class='num'>{money(s.monthly_cost) if s.included_in_total else 'not counted'}</td></tr>"
            for s in services
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
    elif services:
        infra = "".join(
            f"<tr><td>{_e(s.service_name)}</td><td>{_e(s.tier)}</td><td>{_e(s.region)}</td></tr>"
            for s in services
        )
        parts.append(
            f"<section><h2>Infrastructure — {_e(cost.infrastructure.platform_label)}</h2>"
            "<p class='sub'>The services the build runs on. Costs are not shown.</p>"
            "<table><thead><tr><th>Service</th><th>Tier</th><th>Region</th></tr></thead>"
            f"<tbody>{infra}</tbody></table></section>"
        )

    if not est.ai_applicable:
        # Said once, in place of the three AI sections, so the absence reads
        # as a finding rather than as pages that failed to print.
        parts.append(
            "<section><h2>No AI in this estimate</h2>"
            "<p>Every use case in the brief is rules, lookups or alerts that "
            "ordinary software does exactly, so nothing calls a model. No tokens, "
            "no AI services and no AI benefit are costed, and there is no AI "
            "approach to compare against. This is the cost of a standard build.</p>"
            "</section>"
        )
    else:
        _ai_sections(parts, est, money, cur, compare=show_total)

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

    if est.ai_applicable and roi.assumptions:
        basis = "".join(f"<li>{_e(a)}</li>" for a in roi.assumptions)
        parts.append(f"<section><h2>Basis</h2><ul>{basis}</ul></section>")

    # Last before the footer, as on the screen, and under the same switch.
    if est.report_markdown and (display or {}).get("written_report", True):
        body = _report_html(est.report_markdown, {
            "dev": show_dev, "infra": show_infra, "maint": show_maint, "total": show_total,
        })
        if body:
            parts.append(f"<section class='wr'><h2>The written report</h2>{body}</section>")

    parts.append(
        '<p class="foot">Every figure was computed from the brief by Compass\'s '
        "deterministic engine against a versioned rate card. A model labelled the "
        "use cases and chose the delivery platform; it computed nothing. The same "
        "brief produces the same estimate.</p>"
    )
    return "".join(parts)


def _ai_sections(parts: list[str], est: Estimation, money, cur: str,
                 compare: bool = True) -> None:
    """Where the value comes from, and AI against standard software — the two
    sections that only mean something when a model is part of the build. The
    comparison is two whole-cost totals, so it goes when either cost is hidden."""
    roi = est.roi_projection
    cmp_ = est.comparison
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

    if not compare:
        return
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


def _team_phrase(wbs, *, html: bool) -> str:
    """What the elapsed weeks assume. The team as entered, by discipline, when
    there is one; the rate card's parallel engineers otherwise."""
    if wbs.roles:
        people = " · ".join(f"{r.headcount} {r.label.lower()}" for r in wbs.roles)
        people = _e(people) if html else people
        return f"the team entered ({people}); a phase runs as long as its slowest discipline"
    count = _e(_num(wbs.team_size)) if html else _num(wbs.team_size)
    return f"{count} engineers in parallel"


async def to_pdf(est: Estimation, display: dict | None = None) -> bytes:
    from compass.common.screenshot import html_to_pdf

    return await html_to_pdf(to_html(est, display))


# --------------------------------------------------------------------------- excel


def to_excel(est: Estimation, display: dict | None = None) -> bytes:
    """Summary, cost, the work breakdown, assumptions, tokens, ROI and
    recommendations. The two middle sheets appear only when the brief carried
    a breakdown and assumptions to put in them; tokens and ROI only when a
    model is part of the build. `display` hides costs here exactly as it does
    in the PDF: a workbook is the easiest place for a hidden number to leak."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    cur = est.cost_breakdown.currency
    show_dev, show_infra, show_maint = _shown(display)
    show_total = show_dev and show_infra and show_maint
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
    ]
    if show_total:
        rows += [
            ("Total expected (first year)", est.cost_breakdown.total.expected),
            ("Total range", f"{est.cost_breakdown.total.min} – {est.cost_breakdown.total.max}"),
        ]
    rows.append(("Currency", cur))
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
    if show_dev:
        for b in c.development.breakdown:
            ws.append(["Development", f"{b.category} ({b.hours}h)", "", "", b.cost])
        ws.append(["Development", "Total development", "", "", c.development.total_cost])
    if show_infra:
        for svc in c.infrastructure.services:
            # The region and the price source are what make a line checkable: a
            # figure you can trace beats a figure you have to trust.
            ws.append([
                "Infrastructure (monthly)" if svc.included_in_total else "Infrastructure (not counted)",
                f"{svc.service_name} — {svc.tier}", svc.region, svc.price_source, svc.monthly_cost,
            ])
        ws.append(["Infrastructure", "Annual infrastructure", "", "", c.infrastructure.annual_cost])
        if est.ai_applicable:
            for label, value in (("optimistic", c.ai_tokens.annual_cost.optimistic),
                                 ("expected", c.ai_tokens.annual_cost.expected),
                                 ("pessimistic", c.ai_tokens.annual_cost.pessimistic)):
                ws.append(["AI tokens", f"Annual ({label})", "", "", value])
    else:
        # The stack stays; only what it costs goes. Price source with it —
        # "live" beside an empty amount says there is a number being kept back
        # rather than that the services are listed.
        for svc in c.infrastructure.services:
            ws.append(["Infrastructure (cost not shown)",
                       f"{svc.service_name} — {svc.tier}", svc.region, "", None])
    if show_maint:
        ws.append(["Maintenance",
                   f"{c.maintenance.monthly_hours}h/mo @ {_money(cur, c.maintenance.hourly_rate)}/h",
                   "", "", c.maintenance.annual_cost])
    if show_total:
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
        # Hidden, the cost column stays in place and stays empty, so a sheet
        # somebody has built formulas against keeps its columns where they were.
        money = (lambda n: n) if show_dev else (lambda n: None)
        ws.append(["Phase", "Module", "Sub-feature", "Size", "Days", "Hours",
                   f"Cost ({cur})" if show_dev else "Cost (not shown)", "Weeks", "Covers"])
        style_header(ws, 1, 9)
        for ph in wbs.phases:
            for mod in ph.modules:
                line = _single(mod)
                if line is not None:
                    # The module is its one line: one row, with the module's
                    # weeks on it, and no total that repeats it underneath.
                    ws.append([ph.phase,
                               f"{mod.name} (added by the engine)" if mod.derived else mod.name,
                               "", line.basis or line.size.upper() or "—",
                               "" if line.basis else line.units,
                               mod.hours, money(mod.cost), mod.weeks, "; ".join(line.covers)])
                    continue
                for sub in mod.sub_features:
                    # A delivery row is a share of the build, not a size, and
                    # its day count would be a number nobody chose.
                    ws.append([ph.phase, mod.name, sub.name,
                               sub.basis or sub.size.upper() or "—",
                               "" if sub.basis else sub.units,
                               sub.hours, money(sub.cost), "", "; ".join(sub.covers)])
                ws.append([ph.phase,
                           f"{mod.name} (added by the engine)" if mod.derived else mod.name,
                           "Module total", "", "", mod.hours, money(mod.cost), mod.weeks])
                for col in (3, 6, 7, 8):
                    ws.cell(row=ws.max_row, column=col).font = bold
            ws.append([ph.phase, f"PHASE {ph.phase} TOTAL", "", "", "",
                       ph.hours, money(ph.cost), ph.weeks])
            for col in range(1, 9):
                ws.cell(row=ws.max_row, column=col).font = bold
        ws.append(["", "GRAND TOTAL", "", "", "",
                   wbs.total_hours, money(wbs.total_cost), wbs.total_weeks])
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
        widths(ws, [8, 34, 44, 18, 8, 9, 15, 9, 60])

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

    # Tokens and ROI only mean something when a model is part of the build;
    # for a standard one they were two sheets of zeros. And the run cost, the
    # net benefit, payback and the cumulative curve are all benefit less cost,
    # so they leave with a hidden cost rather than reveal it.
    if est.ai_applicable:
        # ── Tokens ──
        ws = wb.create_sheet("Tokens")
        ws.append(["Model", "Use cases", "Monthly input tokens", "Monthly output tokens",
                   f"Monthly cost ({cur})" if show_infra else "Monthly cost (not shown)",
                   "In $/1M", "Out $/1M"])
        style_header(ws, 1, 7)
        for m in c.ai_tokens.model_breakdown:
            ws.append([m.model, ", ".join(m.use_cases), m.monthly_input_tokens,
                       m.monthly_output_tokens, m.monthly_cost if show_infra else None,
                       m.input_price_per_1m, m.output_price_per_1m])
        widths(ws, [20, 34, 20, 20, 17, 10, 10])

        # ── ROI ──
        ws = wb.create_sheet("ROI")
        roi = est.roi_projection
        ws.append(["Metric", "Value"])
        style_header(ws, 1, 2)
        metrics: list[tuple[str, object]] = [
            ("Annual benefit (steady state)", roi.annual_benefit),
            ("Year one benefit (ramped)", roi.first_year_benefit),
            ("Adoption ramp (months)", roi.benefit_ramp_months),
        ]
        if show_total:
            metrics += [
                ("Annual run cost", roi.annual_run_cost),
                ("Net annual benefit", roi.net_annual_benefit),
                ("Payback (months)", roi.payback_months if roi.payback_months is not None else "none"),
                ("3-year net value", roi.three_year_value),
                ("3-year ROI %", roi.roi_percent),
            ]
        for label, value in metrics:
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

        if show_total:
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
