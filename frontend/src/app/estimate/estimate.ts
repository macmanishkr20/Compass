import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';

import { TurnNotifyService } from '../turn-notify.service';
import { ConfirmService } from '../confirm.service';
import { NoticeService } from '../notice.service';
import { EstimateApi } from './estimate-api';
import { EstimateReport } from './report';
import {
  AIUseCase,
  Complexity,
  CostAssumptions,
  EstimateCatalog,
  EstimateRecord,
  EstimateSummary,
  Estimation,
  FeatureItem,
  Module,
  Priority,
  ProjectInput,
  ProjectScale,
  ProjectType,
  SizeBand,
  SubFeature,
} from './models';
import type { BrdAnalysis, SkillScore } from './models';

/** Which surface is showing. The section has three and no router: Compass
 *  navigates by signal, and adding a route table for one module would put this
 *  section's back button in a different place from every other section's. */
type View = 'portfolio' | 'intake' | 'report';

type Filter = 'all' | 'go' | 'no';

let seq = 0;
const rowId = (prefix: string) => `${prefix}${++seq}`;

/** Build hours per complexity, mirroring `COMPLEXITY_HOURS` in catalog.py.
 *  Shown on the feature row so a person can see what a size *means* while
 *  choosing it. It is a label, not a calculation — every figure that matters
 *  still comes from the server. */
const BUILD_HOURS: Record<string, number> = {
  low: 24, medium: 64, high: 130, very_high: 240,
};

@Component({
  selector: 'app-estimate',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './estimate.html',
  styleUrl: './estimate.css',
  imports: [EstimateReport],
})
export class Estimate {
  private readonly api = inject(EstimateApi);
  private readonly confirm = inject(ConfirmService);
  private readonly notice = inject(NoticeService);
  private readonly turnNotify = inject(TurnNotifyService);

  readonly view = signal<View>('portfolio');
  readonly loading = signal(false);
  readonly error = signal('');

  readonly estimates = signal<EstimateSummary[]>([]);
  readonly catalog = signal<EstimateCatalog | null>(null);
  readonly open = signal<EstimateRecord | null>(null);

  readonly stage = signal('');
  readonly stageLabel = signal('');
  readonly progress = signal(0);
  readonly running = signal(false);

  // -- the index ------------------------------------------------------------

  readonly filters: { key: Filter; label: string }[] = [
    { key: 'all', label: 'All' },
    { key: 'go', label: 'Build with AI' },
    { key: 'no', label: 'Says otherwise' },
  ];
  readonly filter = signal<Filter>('all');

  readonly shown = computed(() => {
    const want = this.filter();
    return this.estimates().filter((e) =>
      want === 'all' ? true
        : want === 'go' ? e.verdict === 'build_with_ai'
          : e.verdict !== '' && e.verdict !== 'build_with_ai');
  });

  /** The four figures across the top. */
  readonly stats = computed(() => {
    const all = this.estimates();
    const scored = all.filter((e) => e.score > 0);
    const go = all.filter((e) => e.verdict === 'build_with_ai').length;
    const money = all.reduce((sum, e) => sum + e.total_expected, 0);
    const mean = scored.length
      ? Math.round(scored.reduce((s, e) => s + e.score, 0) / scored.length) : 0;
    return [
      { key: 'count', label: 'Estimates', value: `${all.length}`, of: '',
        note: 'on this workspace', split: [] as { flex: number; colour: string }[] },
      { key: 'money', label: 'First year', value: this.compactMoney(money), of: '',
        note: 'across all of them', split: [] },
      { key: 'go', label: 'Build with AI', value: `${go}`, of: scored.length ? `${scored.length}` : '',
        note: `${Math.max(0, scored.length - go)} say otherwise`,
        // One tick per scored estimate, in its verdict's colour — the strip
        // the mockup uses to make a ratio legible without a second number.
        split: scored.map((e) => ({
          flex: 1,
          colour: e.verdict === 'build_with_ai' ? 'var(--ok)' : 'var(--err)',
        })) },
      { key: 'mean', label: 'Mean feasibility', value: mean ? `${mean}` : '—', of: '',
        note: 'out of 100',
        split: mean
          ? [{ flex: mean, colour: 'var(--brass)' }, { flex: 100 - mean, colour: 'var(--rule)' }]
          : [] },
    ];
  });

  // -- the brief ------------------------------------------------------------

  readonly sections = [
    { id: 's-describe', label: 'Describe it' },
    { id: 's-team', label: 'Team & skills' },
    { id: 's-project', label: 'The project' },
    { id: 's-features', label: 'Work breakdown' },
    { id: 's-assumptions', label: 'Assumptions' },
    { id: 's-usecases', label: 'AI use cases' },
    { id: 's-volume', label: 'Volume' },
    { id: 's-technical', label: 'Technical' },
    { id: 's-rate', label: 'Rate card' },
  ];
  readonly activeSection = signal('s-describe');

  readonly deployments = ['cloud', 'hybrid', 'edge'];

  readonly prose = signal('');
  readonly drafting = signal(false);
  readonly drafted = signal(false);

  // -- the requirements document --------------------------------------------
  /** The uploaded BRD, held as the data URL the server reads. */
  readonly brdFile = signal<{ name: string; mime: string; data_url: string; size: number } | null>(null);
  readonly readingBrd = signal(false);
  /** What reading the BRD found. Carried with the brief, so the report can
   *  show the questions and risks the numbers were priced under. */
  readonly analysis = signal<BrdAnalysis | null>(null);
  readonly analysisOpen = signal(true);
  private readonly brdMaxBytes = 20 * 1024 * 1024;

  // -- who is available -----------------------------------------------------
  // Asked of the person, never drafted: how many people an organisation has
  // and how well they know a stack are not facts any document contains.
  readonly developers = signal(0);
  readonly testers = signal(0);
  readonly analysts = signal(0);
  readonly skills = signal<SkillScore[]>([]);
  readonly teamEntered = computed(() =>
    this.developers() + this.testers() + this.analysts() > 0 ||
    this.skills().some((s) => s.technology.trim()));
  readonly skillLevels = [
    { score: 1, label: 'Novice' },
    { score: 2, label: 'Beginner' },
    { score: 3, label: 'Competent' },
    { score: 4, label: 'Proficient' },
    { score: 5, label: 'Expert' },
  ];
  /** What a module can be tagged with: every technology that has a score row. */
  readonly techNames = computed(() =>
    this.skills().map((s) => s.technology.trim()).filter(Boolean));

  readonly name = signal('');
  readonly projectType = signal<ProjectType>('new');
  readonly description = signal('');
  readonly domain = signal('');
  readonly targetUsers = signal('');
  readonly scale = signal<ProjectScale>('medium');
  /** The work breakdown being edited: modules, each holding sub-features.
   *  This is what the engine prices when it is non-empty. `features` below is
   *  the older flat list — an estimate made before the breakdown existed opens
   *  with one and no modules, and is priced the way it always was. */
  readonly modules = signal<Module[]>([]);
  readonly assumptionRows = signal<{ id: string; text: string }[]>([]);
  readonly features = signal<FeatureItem[]>([]);
  readonly useCases = signal<AIUseCase[]>([]);

  readonly provider = signal('Azure OpenAI');
  readonly deployment = signal('cloud');
  readonly existingInfra = signal('');
  readonly compliance = signal('');
  readonly budget = signal<number | null>(null);
  readonly currency = signal('USD');

  readonly dailyUsers = signal<number | null>(null);
  readonly requestsPerDay = signal<number | null>(null);
  readonly dataGb = signal<number | null>(null);
  readonly peakPattern = signal('');
  readonly growthPercent = signal<number | null>(null);

  readonly assumptionsOpen = signal(false);
  readonly rateCardSaved = signal(false);
  readonly rateEdited = signal(false);
  readonly savingRates = signal(false);
  readonly devRate = signal(115);
  readonly maintRate = signal(95);
  readonly hoursPerWeek = signal(32);
  readonly teamSize = signal(3);
  readonly includeRampUp = signal(true);
  readonly loadedRate = signal(75);
  readonly automationPercent = signal(35);
  readonly rampMonths = signal(6);

  readonly repoUrl = signal('');
  readonly framework = signal('');
  readonly language = signal('');
  readonly database = signal('');
  readonly hosting = signal('');

  /** The five the mockup shows in the rate grid — the ones that move a figure
   *  most. The rest of the card is reachable by saving a default; putting
   *  eight boxes in a five-column grid would be the mockup with an extra row. */
  readonly rateDials = [
    { key: 'dev', label: 'Blended delivery rate', unit: 'per hour',
      get: () => this.devRate() },
    { key: 'team', label: 'Engineers on it', unit: 'in parallel',
      get: () => this.teamSize() },
    { key: 'maint', label: 'Maintenance rate', unit: 'per hour',
      get: () => this.maintRate() },
    { key: 'loaded', label: 'Loaded rate offset', unit: 'per hour',
      get: () => this.loadedRate() },
    { key: 'automation', label: 'Automated end to end', unit: '% of calls',
      get: () => this.automationPercent() },
  ];

  setRampUp(on: boolean): void {
    this.includeRampUp.set(on);
    this.rateEdited.set(true);
    this.rateCardSaved.set(false);
  }

  setRate(key: string, value: number): void {
    ({
      dev: () => this.devRate.set(value),
      team: () => this.teamSize.set(value),
      maint: () => this.maintRate.set(value),
      loaded: () => this.loadedRate.set(value),
      automation: () => this.automationPercent.set(value),
    } as Record<string, () => void>)[key]?.();
    this.rateEdited.set(true);
    this.rateCardSaved.set(false);
  }

  rateStatePill(): string {
    return this.rateCardSaved() ? 'p-ok' : this.rateEdited() ? 'p-warn' : 'p-idle';
  }
  rateStateLabel(): string {
    return this.rateCardSaved() ? 'Saved' : this.rateEdited() ? 'Edited' : 'Workspace default';
  }

  readonly canEstimate = computed(
    () => this.name().trim().length > 0 && !this.running(),
  );

  /** What the checklist and the progress bar both read. One definition, so the
   *  bar can never say five of six while the list shows four ticks. */
  readonly checklist = computed(() => [
    { done: !!this.name().trim(), text: 'Name the project' },
    { done: this.hasWork(),
      text: this.subCount()
        ? `Break the work down — ${this.modules().length} modules, ${this.subCount()} sub-features`
        : 'Break the work down into modules and sub-features' },
    { done: this.useCases().length > 0,
      text: `Add at least one AI use case${this.useCases().length ? ` — ${this.useCases().length} added` : ''}` },
    { done: (this.requestsPerDay() ?? 0) > 0, text: 'State requests per day' },
    { done: !!this.existingInfra().trim(), text: 'Name the existing infrastructure' },
    { done: !!this.compliance().trim(), text: 'List compliance regimes (optional, sharpens risk)' },
  ]);
  readonly done = computed(() => this.checklist().filter((c) => c.done).length);

  sectionDone(id: string): boolean {
    return ({
      's-describe': !!this.prose().trim() || !!this.analysis(),
      's-team': this.teamEntered(),
      's-project': !!this.name().trim(),
      's-features': this.hasWork(),
      's-assumptions': this.assumptionRows().some((a) => a.text.trim()),
      's-usecases': this.useCases().length > 0,
      's-volume': (this.requestsPerDay() ?? 0) > 0,
      's-technical': !!this.existingInfra().trim(),
      's-rate': true,
    } as Record<string, boolean>)[id] ?? false;
  }

  goToSection(id: string): void {
    this.activeSection.set(id);
    document.getElementById(id)?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  featureHours(complexity: string): number {
    return BUILD_HOURS[complexity] ?? 64;
  }

  // -- the live rail --------------------------------------------------------
  //
  // The rail is the same engine, called for real, debounced. The alternatives
  // were a TypeScript mirror of the arithmetic — the 1,400-line duplicate this
  // port deleted — or a cheaper second formula just for the sidebar, which is
  // worse: a rail that disagrees with the report it is previewing teaches
  // people to distrust both.

  readonly preview = signal<Estimation | null>(null);
  private previewTimer: ReturnType<typeof setTimeout> | null = null;
  private previewSeq = 0;

  readonly score = computed(() => this.preview()?.feasibility.score ?? 0);

  readonly subScores = computed(() => {
    const f = this.preview()?.feasibility.sub_scores;
    return [
      { label: 'AI necessity', value: f?.ai_necessity ?? 0 },
      { label: 'Agentic fit', value: f?.agentic_suitability ?? 0 },
      { label: 'Traditional fit', value: f?.traditional_suitability ?? 0 },
    ];
  });

  readonly moneyRows = computed(() => {
    const c = this.preview()?.cost_breakdown;
    return [
      { label: 'Development', value: this.money(c?.development.total_cost ?? 0) },
      { label: 'Model usage', value: this.money(c?.ai_tokens.annual_cost.expected ?? 0) },
      { label: 'Infrastructure', value: this.money(c?.infrastructure.annual_cost ?? 0) },
      { label: 'Maintenance', value: this.money(c?.maintenance.annual_cost ?? 0) },
    ];
  });
  readonly totalMoney = computed(() =>
    this.money(this.preview()?.cost_breakdown.total.expected ?? 0));

  readonly moneySplit = computed(() => {
    const c = this.preview()?.cost_breakdown;
    if (!c) return [];
    // Fixed slot order, never cycled — the same four the report's composition
    // stack and waterfall use, so a colour means one part everywhere.
    return [
      { flex: c.development.total_cost, colour: 'var(--series-1)' },
      { flex: c.ai_tokens.annual_cost.expected, colour: 'var(--series-2)' },
      { flex: c.infrastructure.annual_cost, colour: 'var(--series-3)' },
      { flex: c.maintenance.annual_cost, colour: 'var(--series-4)' },
    ].filter((p) => p.flex > 0);
  });

  readonly budgetNote = computed(() => {
    const ceiling = this.budget();
    const total = this.preview()?.cost_breakdown.total.expected;
    if (!ceiling || !total) return '';
    return total <= ceiling
      ? `Lands ${this.money(ceiling - total)} under the ${this.money(ceiling)} ceiling.`
      : `Exceeds the ${this.money(ceiling)} ceiling by ${this.money(total - ceiling)}.`;
  });

  constructor() {
    void this.refresh();

    // Re-price whenever the brief changes. Reading every field is the point:
    // the effect must depend on all of them, and listing them is how it does.
    effect(() => {
      const brief = this.brief();
      if (this.view() !== 'intake') return;
      const ready = brief.project_name.trim()
        && (brief.features.length || brief.ai_use_cases.length);
      if (!ready) {
        this.preview.set(null);
        return;
      }
      this.schedulePreview(brief);
    });
  }

  private schedulePreview(brief: ProjectInput): void {
    if (this.previewTimer) clearTimeout(this.previewTimer);
    // 400ms: long enough that typing a name is one request rather than nine,
    // short enough that the rail feels attached to the form.
    this.previewTimer = setTimeout(async () => {
      const mine = ++this.previewSeq;
      try {
        const { estimation } = await this.api.preview(brief);
        // Only the newest request may write. Without this a slow early
        // response can land after a fast later one and the rail shows a
        // costing for a brief that no longer exists.
        if (mine === this.previewSeq) this.preview.set(estimation);
      } catch {
        // A preview that fails is not an error worth a banner — the estimate
        // itself will say so properly if it fails too.
      }
    }, 400);
  }

  // -- loading --------------------------------------------------------------

  async refresh(): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    try {
      const [list, catalog, rates] = await Promise.all([
        this.api.list(), this.api.catalog(), this.api.rateCard(),
      ]);
      this.estimates.set(list.estimates);
      this.catalog.set(catalog);
      this.applyRateCard(rates.rate_card);
    } catch (err) {
      this.error.set(this.message(err));
    } finally {
      this.loading.set(false);
    }
  }

  // -- navigation -----------------------------------------------------------

  startNew(type: ProjectType): void {
    this.reset();
    this.projectType.set(type);
    this.view.set('intake');
  }

  backToPortfolio(): void {
    this.view.set('portfolio');
    this.open.set(null);
  }

  async openEstimate(id: string): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    try {
      this.open.set(await this.api.get(id));
      this.view.set('report');
    } catch (err) {
      this.error.set(this.message(err));
    } finally {
      this.loading.set(false);
    }
  }

  async remove(id: string, event: Event): Promise<void> {
    event.stopPropagation();
    const row = this.estimates().find((r) => r.id === id);
    const label = row?.name || 'this estimate';
    if (!(await this.confirm.ask({
      title: 'Delete this estimate?',
      subject: label,
      body: 'The brief and the costing go with it. This cannot be undone.',
    }))) return;

    const before = this.estimates();
    this.estimates.update((rows) => rows.filter((r) => r.id !== id));

    try {
      await this.api.remove(id);
      this.notice.ok(`Deleted “${label}”.`);
    } catch (err) {
      this.estimates.set(before);
      this.notice.error(`Could not delete it — ${this.message(err)}`, {
        label: 'Try again',
        run: () => void this.remove(id, event),
      });
    }
  }

  // -- editing --------------------------------------------------------------

  addFeature(): void {
    this.features.update((rows) => [...rows, {
      id: rowId('f'), name: '', description: '',
      complexity: 'medium' as Complexity, ai_candidate: false,
    }]);
  }

  patchFeature(index: number, patch: Partial<FeatureItem>): void {
    this.features.update((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  dropFeature(index: number): void {
    this.features.update((rows) => rows.filter((_, i) => i !== index));
  }

  addModule(): void {
    this.modules.update((rows) => [...rows, {
      id: rowId('m'), name: '', phase: 1, sub_features: [
        { id: rowId('s'), name: '', description: '', size: 's' as SizeBand, ai_candidate: false },
      ],
    }]);
  }

  patchModule(index: number, patch: Partial<Module>): void {
    this.modules.update((rows) => rows.map((m, i) => (i === index ? { ...m, ...patch } : m)));
  }

  dropModule(index: number): void {
    this.modules.update((rows) => rows.filter((_, i) => i !== index));
  }

  addSub(mi: number): void {
    this.modules.update((rows) => rows.map((m, i) => (i === mi ? {
      ...m,
      sub_features: [...m.sub_features, {
        id: rowId('s'), name: '', description: '', size: 's' as SizeBand, ai_candidate: false,
      }],
    } : m)));
  }

  patchSub(mi: number, si: number, patch: Partial<SubFeature>): void {
    this.modules.update((rows) => rows.map((m, i) => (i === mi ? {
      ...m,
      sub_features: m.sub_features.map((s, j) => (j === si ? { ...s, ...patch } : s)),
    } : m)));
  }

  dropSub(mi: number, si: number): void {
    this.modules.update((rows) => rows.map((m, i) => (i === mi
      ? { ...m, sub_features: m.sub_features.filter((_, j) => j !== si) }
      : m)));
  }

  addAssumption(): void {
    this.assumptionRows.update((rows) => [...rows, { id: rowId('a'), text: '' }]);
  }

  patchAssumption(index: number, text: string): void {
    this.assumptionRows.update((rows) => rows.map((a, i) => (i === index ? { ...a, text } : a)));
  }

  dropAssumption(index: number): void {
    this.assumptionRows.update((rows) => rows.filter((_, i) => i !== index));
  }

  /** Hours behind a band, read from the catalog rather than assumed, so the
   *  form and the engine cannot drift apart. */
  bandHours(size: string): number {
    return this.catalog()?.size_bands.find((b) => b.key === size)?.hours ?? 0;
  }

  moduleHours(m: Module): number {
    return m.sub_features.reduce(
      (sum, s) => sum + (s.units ? s.units * (this.catalog()?.unit_hours ?? 9) : this.bandHours(s.size)),
      0,
    );
  }

  /** Every sub-feature in the plan, priced. Drives the running total in the
   *  breakdown header — the number the person filling the form is watching. */
  readonly wbsHours = computed(() =>
    this.modules().reduce((sum, m) => sum + this.moduleHours(m), 0));

  /** Whether the brief names any work at all — a breakdown, or the flat
   *  feature list an older estimate opens with. Either one prices. */
  readonly hasWork = computed(() => this.subCount() > 0 || this.features().length > 0);

  readonly subCount = computed(() =>
    this.modules().reduce((n, m) => n + m.sub_features.filter((s) => s.name.trim()).length, 0));

  addUseCase(): void {
    this.useCases.update((rows) => [...rows, {
      id: rowId('u'), name: '', task_type: null, description: '',
      priority: 'must_have' as Priority, linked_feature_ids: [], minutes_per_call: null,
    }]);
  }

  patchUseCase(index: number, patch: Partial<AIUseCase>): void {
    this.useCases.update((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  dropUseCase(index: number): void {
    this.useCases.update((rows) => rows.filter((_, i) => i !== index));
  }

  /** The brief as the server expects it. A computed, so the live rail can
   *  depend on it and re-price when any field moves. */
  readonly brief = computed<ProjectInput>(() => {
    const enhancement = this.projectType() === 'enhancement';
    return {
      project_name: this.name().trim() || 'Untitled project',
      project_type: this.projectType(),
      description: this.description().trim(),
      industry_domain: this.domain().trim(),
      target_users: this.targetUsers().trim(),
      scale: this.scale(),
      modules: this.modules()
        .filter((m) => m.name.trim())
        .map((m) => ({ ...m, sub_features: m.sub_features.filter((s) => s.name.trim()) }))
        .filter((m) => m.sub_features.length),
      assumptions: this.assumptionRows().map((a) => a.text.trim()).filter(Boolean),
      features: this.features().filter((f) => f.name.trim()),
      ai_use_cases: this.useCases().filter((u) => u.name.trim()),
      technical_preferences: {
        preferred_llm_provider: this.provider(),
        deployment_model: this.deployment(),
        existing_infra: this.existingInfra().trim(),
        compliance_requirements: this.compliance().split(',').map((s) => s.trim()).filter(Boolean),
        budget_ceiling: this.budget(),
        budget_currency: this.currency().trim() || 'USD',
      },
      volume_and_scale: {
        expected_daily_users: this.dailyUsers(),
        requests_per_day: this.requestsPerDay(),
        data_volume_gb: this.dataGb(),
        peak_load_pattern: this.peakPattern().trim(),
        growth_rate_percent: this.growthPercent(),
      },
      cost_assumptions: this.currentRateCard(),
      team: this.teamEntered() ? {
        developers: this.developers(),
        testers: this.testers(),
        analysts_designers: this.analysts(),
        skills: this.skills()
          .filter((s) => s.technology.trim())
          .map((s) => ({ technology: s.technology.trim(), score: s.score })),
      } : null,
      brd_analysis: this.analysis(),
      ...(enhancement ? {
        repo_url: this.repoUrl().trim() || null,
        current_architecture: {
          framework: this.framework().trim(),
          language: this.language().trim(),
          database: this.database().trim(),
          api_pattern: '',
          hosting_platform: this.hosting().trim(),
          ci_cd: '',
        },
      } : {}),
    };
  });

  private applyRateCard(card: CostAssumptions): void {
    this.devRate.set(card.dev_hourly_rate);
    this.maintRate.set(card.maint_hourly_rate);
    this.hoursPerWeek.set(card.effective_hours_per_week);
    this.teamSize.set(card.team_size);
    this.includeRampUp.set(card.include_ramp_up ?? true);
    this.loadedRate.set(card.loaded_hourly_rate);
    this.automationPercent.set(card.automation_rate_percent);
    this.rampMonths.set(card.benefit_ramp_months);
    this.rateEdited.set(false);
  }

  private currentRateCard(): CostAssumptions {
    return {
      dev_hourly_rate: this.devRate(),
      maint_hourly_rate: this.maintRate(),
      effective_hours_per_week: this.hoursPerWeek(),
      team_size: this.teamSize(),
      include_ramp_up: this.includeRampUp(),
      loaded_hourly_rate: this.loadedRate(),
      automation_rate_percent: this.automationPercent(),
      benefit_ramp_months: this.rampMonths(),
    };
  }

  async saveRates(): Promise<void> {
    this.savingRates.set(true);
    try {
      await this.api.saveRateCard(this.currentRateCard());
      this.rateCardSaved.set(true);
      this.rateEdited.set(false);
    } catch (err) {
      this.error.set(this.message(err));
    } finally {
      this.savingRates.set(false);
    }
  }

  onBrdChosen(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    this.error.set('');
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    if (!['pdf', 'docx', 'md', 'markdown', 'txt'].includes(ext)) {
      this.error.set('Upload the BRD as a PDF, Word (.docx), Markdown or text file.');
      return;
    }
    if (file.size > this.brdMaxBytes) {
      this.error.set(`That file is ${this.brdSize(file.size)} — the limit is 20 MB.`);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => this.brdFile.set({
      name: file.name, mime: file.type, data_url: String(reader.result), size: file.size,
    });
    reader.onerror = () => this.error.set('Could not read that file from disk.');
    reader.readAsDataURL(file);
  }

  clearBrd(): void {
    this.brdFile.set(null);
  }

  async readBrd(): Promise<void> {
    const file = this.brdFile();
    if (!file || this.readingBrd()) return;
    this.readingBrd.set(true);
    this.error.set('');
    this.turnNotify.arm();
    try {
      const { brief } = await this.api.readBrd(
        { name: file.name, mime: file.mime, data_url: file.data_url }, this.projectType());
      this.applyBrief(brief);
      this.drafted.set(true);
      this.analysisOpen.set(true);
      // Straight to the questions only a person can answer.
      this.goToSection('s-team');
    } catch (err) {
      this.error.set(this.message(err));
    } finally {
      this.readingBrd.set(false);
      this.turnNotify.finished(
        'estimate',
        this.error() ? 'Could not read the BRD.' : `${this.name() || 'BRD'} — read. Add the team and skills.`,
        !this.error(),
      );
    }
  }

  brdSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  analysisLists(a: BrdAnalysis): { label: string; items: string[] }[] {
    return [
      { label: 'Functional requirements', items: a.functional_requirements ?? [] },
      { label: 'Scenarios', items: a.scenarios ?? [] },
      { label: 'Edge cases', items: a.edge_cases ?? [] },
      { label: 'Non-functional requirements', items: a.non_functional_requirements ?? [] },
      { label: 'User roles', items: a.user_roles ?? [] },
      { label: 'Integrations', items: a.integrations ?? [] },
      { label: 'Out of scope', items: a.out_of_scope ?? [] },
    ];
  }

  setHeadcount(which: 'developers' | 'testers' | 'analysts', value: number): void {
    const n = Number.isFinite(value) ? Math.floor(value) : 0;
    this[which].set(Math.max(0, Math.min(500, n)));
  }

  addSkill(): void {
    this.skills.update((rows) => [...rows, { technology: '', score: 3 }]);
  }

  patchSkill(index: number, patch: Partial<SkillScore>): void {
    const before = this.skills()[index]?.technology ?? '';
    this.skills.update((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
    // A renamed technology keeps the modules tagged with it pointing at it.
    const after = patch.technology;
    if (after !== undefined && before && before !== after) {
      this.modules.update((ms) => ms.map((m) => ({
        ...m,
        technologies: (m.technologies ?? []).map((x) => (x === before.trim() ? after.trim() : x)),
      })));
    }
  }

  dropSkill(index: number): void {
    const gone = this.skills()[index]?.technology.trim();
    this.skills.update((rows) => rows.filter((_, i) => i !== index));
    if (gone) {
      this.modules.update((ms) => ms.map((m) => ({
        ...m, technologies: (m.technologies ?? []).filter((x) => x !== gone),
      })));
    }
  }

  toggleModuleTech(index: number, tech: string): void {
    const m = this.modules()[index];
    if (!m) return;
    const tags = m.technologies ?? [];
    this.patchModule(index, {
      technologies: tags.includes(tech) ? tags.filter((x) => x !== tech) : [...tags, tech],
    });
  }

  skillLabel(score: number): string {
    return this.skillLevels.find((l) => l.score === score)?.label ?? '';
  }

  async draftFromProse(): Promise<void> {
    const text = this.prose().trim();
    if (!text || this.drafting()) return;
    this.drafting.set(true);
    this.error.set('');
    try {
      const { brief } = await this.api.draft(text, this.projectType());
      this.applyBrief(brief);
      this.drafted.set(true);
    } catch (err) {
      this.error.set(this.message(err));
    } finally {
      this.drafting.set(false);
    }
  }

  /** Load a brief into the form. Every field lands somewhere visible and
   *  editable — there is no hidden half that goes to the engine unseen. */
  private applyBrief(b: ProjectInput): void {
    this.name.set(b.project_name ?? '');
    this.description.set(b.description ?? '');
    this.domain.set(b.industry_domain ?? '');
    this.targetUsers.set(b.target_users ?? '');
    this.scale.set(b.scale ?? 'medium');
    this.modules.set((b.modules ?? []).map((m, i) => ({
      ...m,
      id: m.id || `m${i + 1}`,
      sub_features: (m.sub_features ?? []).map((s, j) => ({ ...s, id: s.id || `m${i + 1}s${j + 1}` })),
    })));
    this.assumptionRows.set((b.assumptions ?? []).map((text, i) => ({ id: `a${i + 1}`, text })));
    this.features.set((b.features ?? []).map((f, i) => ({ ...f, id: f.id || `f${i + 1}` })));
    this.useCases.set((b.ai_use_cases ?? []).map((u, i) => ({ ...u, id: u.id || `u${i + 1}` })));
    const v = b.volume_and_scale;
    if (v) {
      this.dailyUsers.set(v.expected_daily_users ?? null);
      this.requestsPerDay.set(v.requests_per_day ?? null);
      this.dataGb.set(v.data_volume_gb ?? null);
      this.peakPattern.set(v.peak_load_pattern ?? '');
      this.growthPercent.set(v.growth_rate_percent ?? null);
    }
    // The rate card is never in a draft — it is what the organisation pays its
    // own people — so it is left exactly as the person last set it.
    this.analysis.set(b.brd_analysis ?? null);
    if (b.team) {
      // A stored brief: the team as it was entered.
      this.developers.set(b.team.developers ?? 0);
      this.testers.set(b.team.testers ?? 0);
      this.analysts.set(b.team.analysts_designers ?? 0);
      this.skills.set((b.team.skills ?? []).map((s) => ({ technology: s.technology, score: s.score })));
    } else if (b.brd_analysis?.technologies?.length) {
      // A fresh read: one row per technology the document uses, keeping any
      // score already entered against the same name. Headcount is left alone —
      // the document does not know it, and the person may already have said.
      const had = new Map(this.skills().map((s) => [s.technology.trim().toLowerCase(), s.score]));
      this.skills.set(b.brd_analysis.technologies.map((x) => ({
        technology: x.name,
        score: had.get(x.name.trim().toLowerCase()) ?? 3,
      })));
    }
  }

  async estimate(): Promise<void> {
    if (!this.canEstimate()) return;
    this.running.set(true);
    this.drafted.set(false);
    this.error.set('');
    this.progress.set(0);
    this.stageLabel.set('Sending the brief…');
    this.turnNotify.arm();
    try {
      const record = await this.api.stream(this.brief(), (frame) => {
        this.stage.set(frame.stage);
        if (frame.label) this.stageLabel.set(frame.label);
        this.progress.set(frame.progress);
      });
      this.open.set(record);
      this.estimates.update((rows) => [
        { ...record, brief: undefined, result: undefined } as unknown as EstimateSummary,
        ...rows,
      ]);
      this.view.set('report');
    } catch (err) {
      this.error.set(this.message(err));
    } finally {
      this.running.set(false);
      const rec = this.open();
      this.turnNotify.finished(
        'estimate',
        rec ? `${rec.name || 'Estimate'} — costed` : 'Estimate finished.',
        !this.error(),
      );
    }
  }

  private reset(): void {
    this.prose.set('');
    this.drafted.set(false);
    this.brdFile.set(null);
    this.analysis.set(null);
    this.analysisOpen.set(true);
    this.developers.set(0);
    this.testers.set(0);
    this.analysts.set(0);
    this.skills.set([]);
    this.preview.set(null);
    this.name.set('');
    this.description.set('');
    this.domain.set('');
    this.targetUsers.set('');
    this.scale.set('medium');
    this.modules.set([]);
    this.assumptionRows.set([]);
    this.features.set([]);
    this.useCases.set([]);
    this.existingInfra.set('');
    this.compliance.set('');
    this.budget.set(null);
    this.dailyUsers.set(null);
    this.requestsPerDay.set(null);
    this.dataGb.set(null);
    this.peakPattern.set('');
    this.growthPercent.set(null);
    this.repoUrl.set('');
    this.framework.set('');
    this.language.set('');
    this.database.set('');
    this.hosting.set('');
    this.activeSection.set('s-describe');
    this.error.set('');
  }

  // -- presentation ---------------------------------------------------------

  money(amount: number, currency = ''): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency', currency: currency || this.currency() || 'USD',
      maximumFractionDigits: 0,
    }).format(amount);
  }

  /** $1.2M rather than $1,240,000 — a strip of four numbers is read at a
   *  glance, and a glance does not count digits. */
  compactMoney(amount: number): string {
    if (amount >= 1e6) return `$${(amount / 1e6).toFixed(2)}M`;
    if (amount >= 1000) return `$${Math.round(amount / 1000)}k`;
    return `$${Math.round(amount)}`;
  }

  /** The same relative wording Design and Pipelines use, so time reads the
   *  same everywhere rather than each section inventing a house style. */
  age(epochSeconds: number): string {
    const seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return days < 30 ? `${days}d ago` : `${Math.floor(days / 30)}mo ago`;
  }

  /** Green above 70, amber above 50, red below. The same thresholds the
   *  engine's own rating uses, so the bar and the word agree. */
  scoreColour(score: number): string {
    if (!score) return 'var(--rule)';
    return score >= 70 ? 'var(--ok)' : score >= 50 ? 'var(--warn)' : 'var(--err)';
  }

  verdictLabel(decision: string): string {
    return {
      build_with_ai: 'Build with AI',
      do_not_use_ai: 'Not with AI',
      hybrid: 'Hybrid',
    }[decision] ?? 'Draft';
  }

  verdictPill(decision: string): string {
    if (decision === 'build_with_ai') return 'p-ok';
    if (decision === 'do_not_use_ai') return 'p-err';
    if (decision === 'hybrid') return 'p-warn';
    return 'p-idle';
  }

  confidenceLabel(row: EstimateSummary): string {
    return row.confidence
      ? row.confidence.charAt(0).toUpperCase() + row.confidence.slice(1)
      : '—';
  }

  typeLabel(type: ProjectType): string {
    return type === 'enhancement' ? 'Enhancement' : 'New build';
  }

  taskLabel(task: string): string {
    return task.replace(/_/g, ' ');
  }

  /** `very_high` -> `very high`. The wire uses snake_case enums and a person
   *  should not have to read one. */
  label(value: string): string {
    return (value || '').replace(/_/g, ' ');
  }

  num(value: string): number | null {
    const n = Number(value);
    return value.trim() === '' || Number.isNaN(n) ? null : n;
  }

  private message(err: unknown): string {
    const text = err instanceof Error ? err.message : String(err);
    return text.includes('404')
      ? 'The Estimate module is not mounted on this server.'
      : text;
  }
}
