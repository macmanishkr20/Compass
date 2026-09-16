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

  /** Export links, not fetches. A download is what an anchor is for, and
   *  pulling the bytes into memory to hand them to a blob URL buys nothing
   *  here except a way to run out of it on a large workbook. */
  readonly pdfHref = computed(() => `/v1/estimates/${this.record().id}/export/pdf`);
  readonly excelHref = computed(() => `/v1/estimates/${this.record().id}/export/excel`);

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
    ];
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
  sizeLabel(size: string, units: number): string {
    return size ? `${size.toUpperCase()} · ${units}d` : `${units}d`;
  }

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

  verdictLabel(decision: string): string {
    return {
      build_with_ai: 'Build with AI',
      do_not_use_ai: 'Not with AI',
      hybrid: 'Hybrid',
    }[decision] ?? decision.replace(/_/g, ' ');
  }

  verdictPill(decision: string): string {
    if (decision === 'build_with_ai') return 'p-ok';
    if (decision === 'do_not_use_ai') return 'p-err';
    return 'p-warn';
  }

  verdictColour(decision: string): string {
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
