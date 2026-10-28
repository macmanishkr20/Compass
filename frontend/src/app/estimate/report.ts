import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';

import { Markdown } from '../markdown/markdown';
import { EstimateApi } from './estimate-api';
import { EstimateRecord, ProjectType } from './models';

/** Which half of the report is showing. Two, not five: the mockup's result
 *  screen is one page with the working folded behind a single control. */
type Tab = 'summary' | 'detail';

/**
 * One estimate, read — the mockup's result screen.
 *
 * *The verdict leads and the working folds away.* Everything a decision needs
 * is above the fold: the call, the first-year figure, the feasibility bars,
 * where the money goes, what platform was chosen and what the whole thing
 * assumes. "Assumptions" opens the tables underneath — the development
 * breakdown, every infrastructure line with its region and price source, the
 * return, the comparison, the written report. Same numbers, not a second
 * computation.
 *
 * *The sensitivity panel is real.* "If the volume moves" re-costs the stored
 * brief at half and double its stated requests-per-day, through the same
 * preview endpoint the form's live rail uses. It would have been easy to
 * multiply the total by a plausible factor in TypeScript and call it
 * sensitivity; that number would be a drawing of an analysis rather than one.
 * Development does not move, infrastructure and tokens do, and the panel says
 * so because the engine says so.
 */
@Component({
  selector: 'app-estimate-report',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './report.html',
  styleUrl: './report.css',
  imports: [Markdown],
})
export class EstimateReport {
  private readonly api = inject(EstimateApi);

  readonly record = input.required<EstimateRecord>();
  readonly back = output<void>();

  readonly tab = signal<Tab>('summary');

  readonly est = computed(() => this.record().result);
  /** Whether a model is part of this build. Records from before the flag
   *  existed were all costed as AI estimates, and still read as one. */
  readonly ai = computed(() => this.est().ai_applicable !== false);
  /** What reading the BRD found, when the estimate came from one. */
  readonly analysis = computed(() => this.record().brief?.brd_analysis ?? null);

  /** "2 dev · 1 test · 1 BA/UX", or the rate card's headcount. */
  teamPhrase(): string {
    const w = this.wbs();
    if (w.roles?.length) return w.roles.map((r) => `${r.headcount} ${this.roleShort(r.role)}`).join(' · ');
    return `${w.team_size} people`;
  }

  roleShort(role: string): string {
    return ({ development: 'dev', testing: 'test', analysis_design: 'BA/UX' } as Record<string, string>)[role] ?? role;
  }

  roleLabel(role: string): string {
    return this.wbs().roles?.find((r) => r.role === role)?.label ?? role;
  }

  hasRole(role: string): boolean {
    return !!this.wbs().roles?.some((r) => r.role === role);
  }

  sharesText(): string {
    return (this.wbs().roles ?? []).map((r) => `${r.label.toLowerCase()} ${r.share_percent}%`).join(', ');
  }

  abs(n: number): number {
    return Math.abs(n);
  }
  readonly currency = computed(() => this.est().cost_breakdown.currency);

  /** Which export is running, '' when none. One at a time: they come from the
   *  same stored record and the PDF holds a browser open on the server. */
  readonly exporting = signal<'' | 'pdf' | 'excel' | 'print'>('');
  /** Why the last export produced no file. */
  readonly exportError = signal('');

  /**
   * Save an export, or say why there is none.
   *
   * These were `<a href download>` links, which is the right shape for a
   * download and the wrong one for a download that can fail: the browser
   * writes the response body to the named file whatever the status is, so a
   * 501 saying `playwright install chromium` landed on disk as the PDF. On
   * Windows, where that install step is routinely missed, the whole symptom
   * was a file that exists and will not open — and nothing said so.
   *
   * So: fetch, refuse an empty body, then hand the bytes over. The blob costs
   * one report in memory, which is a page of tables.
   */
  async exportAs(format: 'pdf' | 'excel'): Promise<void> {
    if (this.exporting()) return;
    this.exporting.set(format);
    this.exportError.set('');
    try {
      const blob = await this.api.download(
        `/v1/estimates/${this.record().id}/export/${format}`,
      );
      // Zero bytes is not a file. The server should never send one, but this
      // is the one place that can keep it off the disk.
      if (!blob.size) {
        throw new Error('the server returned an empty file, so nothing was saved');
      }
      this.save(blob, `${this.stem()}-estimate.${format === 'pdf' ? 'pdf' : 'xlsx'}`);
    } catch (err) {
      this.exportError.set(`Export failed: ${(err as Error).message}`);
    } finally {
      this.exporting.set('');
    }
  }

  /**
   * Print the report through the browser in front of you.
   *
   * The route that cannot fail for want of a browser, because it uses the one
   * the person is already looking at. `Export PDF` needs a Chromium on the
   * *server*, and on a managed laptop the install that would put one there is
   * blocked by the proxy — there is no server-side answer to that. This asks
   * for the same HTML the server would have printed and hands it to the print
   * dialog here, where "Save as PDF" is a destination. Same stylesheet, same
   * `@page` rules, same document.
   */
  async printReport(): Promise<void> {
    if (this.exporting()) return;
    this.exporting.set('print');
    this.exportError.set('');
    try {
      const body = await this.api.download(
        `/v1/estimates/${this.record().id}/export/html`,
      );
      const html = await body.text();
      if (!html.trim()) throw new Error('the server returned an empty report');
      this.printHtml(html);
    } catch (err) {
      this.exportError.set(`Print failed: ${(err as Error).message}`);
    } finally {
      this.exporting.set('');
    }
  }

  /** Put the document in a frame of its own and print that frame.
   *
   *  `allow-same-origin` is what lets this page reach in and call `print()`;
   *  withholding `allow-scripts` beside it means nothing in the document can
   *  run, so the report is printable and inert at once.
   */
  private printHtml(html: string): void {
    const frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-same-origin allow-modals');
    frame.setAttribute('aria-hidden', 'true');
    frame.srcdoc = html;
    frame.style.cssText = 'position:fixed;inset:0 auto auto 0;width:0;height:0;border:0;opacity:0';
    frame.addEventListener('load', () => {
      frame.contentWindow?.focus();
      frame.contentWindow?.print();
      // The dialog is modal, so in most browsers nothing below runs until it
      // is dismissed. The timer is for the ones where it does: the frame has
      // to outlive the dialog, or there is nothing left to print.
      setTimeout(() => frame.remove(), 60_000);
    });
    document.body.append(frame);
  }

  /** Hand a blob to the browser as a file. The anchor has to be in the
   *  document for Firefox to honour the click. */
  private save(blob: Blob, name: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    // Not immediately: the download reads the URL after the click returns.
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }

  /** The filename the server's Content-Disposition used to give, by the same
   *  rule — the header is gone now that the bytes arrive as a blob. */
  private stem(): string {
    const slug = this.record()
      .name.replace(/[^a-zA-Z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .toLowerCase();
    return slug || 'estimate';
  }

  readonly subScores = computed(() => {
    const s = this.est().feasibility.sub_scores;
    return [
      { label: 'AI necessity', value: s.ai_necessity },
      { label: 'Agentic fit', value: s.agentic_suitability },
      { label: 'Traditional fit', value: s.traditional_suitability },
    ];
  });

  /** The weakest dimension, named. A score of 64 says nothing about what to
   *  do; "the traditional-fit score is the one holding this back" does. */
  readonly weakest = computed(() => {
    // With no AI in the build, the low AI scores are the finding, not a
    // weakness to fix: "improve AI necessity" would be advice to add a model
    // to a system that was right not to have one.
    if (!this.ai()) {
      const trad = this.est().feasibility.sub_scores.traditional_suitability;
      return `Traditional fit is ${trad}. The AI scores are low because nothing here `
        + 'needs a model — which is why none is costed.';
    }
    const low = this.subScores().reduce((a, b) => (b.value < a.value ? b : a));
    return `Weakest dimension is ${low.label.toLowerCase()} at ${low.value}. `
      + 'Improving it is worth more than any change to the rate card.';
  });

  /** Where the money goes: four parts and their total, each bar drawn against
   *  the largest part so the shape is a comparison rather than a percentage. */
  readonly waterfall = computed(() => {
    const c = this.est().cost_breakdown;
    const parts = this.parts();
    const max = Math.max(...parts.map((p) => p.amount), 1);
    return [
      ...parts.map((p) => ({ ...p, width: (p.amount / max) * 88, total: false })),
      { label: 'First-year total', amount: c.total.expected, colour: 'var(--ink)',
        width: 100, total: true },
    ];
  });

  /** The four parts of a first-year cost, in fixed slot order. One
   *  definition: the composition stack, its legend and the waterfall all
   *  read it, so a colour means the same part in all three. */
  readonly parts = computed(() => {
    const c = this.est().cost_breakdown;
    return [
      { label: 'Development', amount: c.development.total_cost, colour: 'var(--series-1)' },
      { label: 'Model usage', amount: c.ai_tokens.annual_cost.expected, colour: 'var(--series-2)' },
      { label: 'Infrastructure', amount: c.infrastructure.annual_cost, colour: 'var(--series-3)' },
      { label: 'Maintenance', amount: c.maintenance.annual_cost, colour: 'var(--series-4)' },
    // A "Model usage — $0" row on a build with no model reads as a model that
    // happens to be free. The slot keeps its colour for the ones that have it.
    ].filter((p) => this.ai() || p.label !== 'Model usage');
  });

  /** Part-to-whole as one bar. The waterfall beside it compares magnitudes;
   *  this is the share shape, which is a different reading and the one a
   *  four-row table cannot give. Shares are rounded for the label only — the
   *  bar is laid out on the raw amounts, so a 1% slice is a 1% slice rather
   *  than being rounded up into visibility. */
  readonly composition = computed(() => {
    const parts = this.parts();
    const total = parts.reduce((s, p) => s + p.amount, 0) || 1;
    return parts.map((p) => ({
      ...p, share: Math.round((p.amount / total) * 100), flex: p.amount,
    })).filter((p) => p.amount > 0);
  });

  /** The ROI curve.
   *
   * The reading this is for is not "what is the value at month 18" — it is
   * *where the line crosses zero*, and how steeply it climbs after. So the
   * zero baseline is a drawn rule, the area between line and baseline is
   * washed at 10%, and the crossing is the one labelled point. A value on
   * every point is chaos and goes unread.
   */
  readonly roiChart = computed(() => {
    const width = 660;
    const height = 190;
    const padLeft = 4;
    const padBottom = 20;
    const roi = this.est().roi_projection;
    const points = roi.curve;
    if (points.length < 2) return null;

    const months = points.map((p) => p.month);
    const values = points.map((p) => p.cumulative_net);
    const minMonth = Math.min(...months);
    const maxMonth = Math.max(...months);
    // Zero is always in range: the crossing is the whole point, and a scale
    // that excludes the baseline hides it.
    const lo = Math.min(0, ...values);
    const hi = Math.max(0, ...values);
    const span = hi - lo || 1;
    const plotH = height - padBottom;

    const x = (m: number) =>
      padLeft + ((m - minMonth) / (maxMonth - minMonth || 1)) * (width - padLeft);
    const y = (v: number) => plotH - ((v - lo) / span) * plotH;

    const path = points
      .map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(p.month).toFixed(1)} ${y(p.cumulative_net).toFixed(1)}`)
      .join(' ');
    const zeroY = y(0);
    const area = `${path} L ${x(maxMonth).toFixed(1)} ${zeroY.toFixed(1)} `
      + `L ${x(minMonth).toFixed(1)} ${zeroY.toFixed(1)} Z`;

    const payback = roi.payback_months;
    const crossing = payback !== null && payback >= minMonth && payback <= maxMonth
      ? { x: x(payback), y: zeroY }
      : null;

    return {
      width, height, zeroY, path, area, crossing,
      dots: points.map((p) => ({
        cx: x(p.month), cy: y(p.cumulative_net), month: p.month, value: p.cumulative_net,
      })),
      ticks: [
        { x: x(minMonth), label: `${minMonth}` },
        { x: x((minMonth + maxMonth) / 2), label: `${Math.round((minMonth + maxMonth) / 2)}` },
        { x: x(maxMonth), label: `${maxMonth} mo` },
      ],
      // The caption describes the data, not the scale. `hi` and `lo` are the
      // axis bounds and both are clamped to include zero, so captioning them
      // once printed "$0 by month 36" for a series that ended at −$272,015.
      endLabel: `${this.money(values[values.length - 1])} by month ${maxMonth}`,
      troughLabel: Math.min(...values) < 0
        && Math.min(...values) !== values[values.length - 1]
        ? `${this.money(Math.min(...values))} at the deepest point`
        : '',
      crossLabel: payback === null ? 'never crosses zero' : `pays back at ${payback} mo`,
    };
  });

  /** The work breakdown, ready to draw: phases, their modules, and the
   *  sub-features under each. Empty for an older estimate that was made from
   *  a flat feature list — those have no breakdown, and the panel says so
   *  rather than inventing one. */
  readonly wbs = computed(() => this.est().cost_breakdown.development.work_breakdown);
  /** Whether the engine added any line the brief did not name. */
  readonly hasDerived = computed(() =>
    (this.wbs()?.phases ?? []).some((p) => p.modules.some((m) => m.derived)));

  readonly hasWbs = computed(() => (this.wbs()?.phases?.length ?? 0) > 0);

  /** Which modules are expanded. Collapsed by default: sixteen modules of four
   *  sub-features is sixty-four rows, and the shape of the plan is the modules.
   *  The sub-features are what you open when you disagree with one. */
  readonly openModules = signal<Set<string>>(new Set());

  toggleModule(name: string): void {
    this.openModules.update((set) => {
      const next = new Set(set);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }

  isOpen(name: string): boolean {
    return this.openModules().has(name);
  }

  expandAll(): void {
    this.openModules.set(new Set(
      this.wbs().phases.flatMap((p) => p.modules.map((m) => m.name)),
    ));
  }

  collapseAll(): void {
    this.openModules.set(new Set());
  }

  /** How wide to draw a module's bar: against the largest module in the plan,
   *  so the eye compares modules with each other rather than with the total. */
  readonly widestModule = computed(() => Math.max(
    1, ...this.wbs().phases.flatMap((p) => p.modules.map((m) => m.hours)),
  ));

  /** The pill on a sub-feature. A line whose day count matches no band shows
   *  the days alone — the days are what was priced, and a letter that does not
   *  mean them is noise. */
  sizeLabel(size: string, units: number, basis?: string): string {
    // A delivery row is a share of the build. Its day count would be a size
    // nobody chose, so the share is what goes in the pill.
    if (basis) return basis;
    return size ? `${size.toUpperCase()} · ${units}d` : `${units}d`;
  }

  /** What the engine added, by kind — the note under the plan names each,
   *  because "one line was added" stopped being true when there were two. */
  readonly derivedKinds = computed(() => new Set(
    (this.wbs()?.phases ?? []).flatMap((p) =>
      p.modules.filter((m) => m.derived).map((m) => m.kind || 'ramp_up')),
  ));

  readonly infraCounted = computed(() =>
    this.est().cost_breakdown.infrastructure.services.filter((s) => s.included_in_total));

  readonly devHours = computed(() =>
    this.est().cost_breakdown.development.breakdown.reduce((sum, b) => sum + b.hours, 0)
      .toLocaleString('en-US'));

  readonly tokensPerMonth = computed(() => {
    const t = this.est().cost_breakdown.ai_tokens.model_breakdown;
    const total = t.reduce((s, m) => s + m.monthly_input_tokens + m.monthly_output_tokens, 0);
    const models = t.map((m) => m.model).join(', ') || 'none';
    return `${this.compactNum(total)} · ${models}`;
  });

  readonly benefitBasis = computed(() => {
    const d = this.est().roi_projection.value_drivers[0];
    if (!d || d.minutes_per_call === null) return 'task-type defaults';
    return `${d.minutes_per_call} min at ${this.money(d.loaded_hourly_rate ?? 0)}/hr, `
      + `${d.automation_rate_percent}% automated`;
  });

  readonly region = computed(() =>
    this.infraCounted()[0]?.region || 'not priced by region');

  readonly complianceLine = computed(() => {
    const list = this.record().brief?.technical_preferences?.compliance_requirements ?? [];
    return list.length ? list.join(', ') : 'None declared';
  });

  readonly platformSource = computed(() => ({
    explicit: 'you asked for it',
    agent: 'the architect, from the brief',
    heuristic: 'matching the brief',
  }[this.est().solution_proposal?.source ?? ''] ?? 'the default'));

  // -- sensitivity ----------------------------------------------------------

  readonly sensitivity = signal<
    { label: string; total: number; delta: number; baseline: boolean }[]
  >([]);

  constructor() {
    effect(() => {
      const record = this.record();
      this.sensitivity.set([]);
      void this.loadSensitivity(record);
    });
  }

  private async loadSensitivity(record: EstimateRecord): Promise<void> {
    const brief = record.brief;
    const base = record.result.cost_breakdown.total.expected;
    const requests = brief?.volume_and_scale?.requests_per_day ?? 0;
    // Without a stated volume there is nothing to move, and inventing one to
    // fill the panel would be exactly the fabrication this module refuses
    // everywhere else.
    if (!brief || !requests) return;

    const at = async (factor: number) => {
      const scaled: typeof brief = {
        ...brief,
        volume_and_scale: {
          ...brief.volume_and_scale,
          requests_per_day: Math.max(1, Math.round(requests * factor)),
        },
      };
      const { estimation } = await this.api.preview(scaled);
      return estimation.cost_breakdown.total.expected;
    };

    try {
      const [half, double] = await Promise.all([at(0.5), at(2)]);
      this.sensitivity.set([
        { label: 'Half volume', total: half, delta: half - base, baseline: false },
        { label: 'As briefed', total: base, delta: 0, baseline: true },
        { label: 'Double volume', total: double, delta: double - base, baseline: false },
      ]);
    } catch {
      // A panel that cannot be computed shows nothing rather than a guess.
    }
  }

  // -- presentation ---------------------------------------------------------

  /** Thousands separated, no decimals — hours, not currency. */
  num(value: number): string {
    return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(value);
  }

  money(amount: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency', currency: this.currency(), maximumFractionDigits: 0,
    }).format(amount);
  }

  compactMoney(amount: number): string {
    if (amount >= 1e6) return `$${(amount / 1e6).toFixed(2)}M`;
    if (amount >= 1000) return `$${Math.round(amount / 1000)}k`;
    return `$${Math.round(amount)}`;
  }

  private compactNum(n: number): string {
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
    return `${Math.round(n)}`;
  }

  scoreColour(score: number): string {
    if (!score) return 'var(--rule)';
    return score >= 70 ? 'var(--ok)' : score >= 50 ? 'var(--warn)' : 'var(--err)';
  }

  // A brief with no AI in it was never weighed for AI and found wanting, so
  // it does not get the red "Not with AI" a rejected AI proposal gets. It is a
  // go, for ordinary software.
  verdictLabel(decision: string): string {
    if (!this.ai()) return 'Standard software';
    return {
      build_with_ai: 'Build with AI',
      do_not_use_ai: 'Not with AI',
      hybrid: 'Hybrid',
    }[decision] ?? decision.replace(/_/g, ' ');
  }

  verdictPill(decision: string): string {
    if (!this.ai()) return 'p-ok';
    if (decision === 'build_with_ai') return 'p-ok';
    if (decision === 'do_not_use_ai') return 'p-err';
    return 'p-warn';
  }

  verdictColour(decision: string): string {
    if (!this.ai()) return 'var(--ok)';
    if (decision === 'build_with_ai') return 'var(--ok)';
    if (decision === 'do_not_use_ai') return 'var(--err)';
    return 'var(--warn)';
  }

  typeLabel(type: ProjectType): string {
    return type === 'enhancement' ? 'Enhancement' : 'New build';
  }

  label(value: string): string {
    return (value || '').replace(/_/g, ' ');
  }
}
