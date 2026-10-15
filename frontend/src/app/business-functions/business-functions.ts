import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { FormsModule } from '@angular/forms';

import { NoticeService } from '../notice.service';
import { BusinessFunctionsLayout } from './bf-layout.service';
import {
  ActionSpec,
  ActResult,
  ChartSlice,
  ChartSpec,
  AskResult,
  BusinessFunctionsApi,
  FeatureCard,
  FeatureView,
  Figure,
  FormSpec,
  FunctionCard,
  FunctionDetail,
  Plan,
  PlanTarget,
  ReportState,
  TrailStep,
} from './bf-api';

/** One line in the rail: what was said, by whom, and what it was about. */
interface RailTurn {
  who: 'you' | 'assistant';
  text: string;
  /** A plan waiting on an answer. Rendered as a card with two buttons. */
  plan?: Plan;
  /** Set once a plan has been answered, so the card stops offering buttons. */
  settled?: string;
  /** Rows the sentence meant but could not choose between. */
  options?: PlanTarget[];
}

/**
 * Business Functions — the parts of the firm, and the applications inside them.
 *
 * Three columns, and each answers a different question. The switcher on the
 * left says which function you are in and what it holds. The stage in the
 * middle is either an overview — a queue of what needs you, not a dashboard —
 * or one feature's workbench. The rail on the right is an assistant that
 * follows you: at the overview it answers about the function, and once a
 * feature is open it can operate what is on screen.
 *
 * THE RAIL NEVER ACTS ON ITS OWN. A sentence comes back as a plan naming the
 * rows it would touch; those rows light up in the table, and nothing changes
 * until somebody presses the button on the card. Row buttons are different —
 * a click already said which row — so they act directly. Inferred targets get
 * a confirmation step; pointed-at targets already had one.
 *
 * Chromeless, like Design, Pipelines, Estimate and Missions: the section hides
 * Compass's own sidebar and renders its own, because what belongs in a sidebar
 * here is a function switcher rather than a conversation list.
 *
 * Everything is in Compass's own tokens. The module introduces no palette of
 * its own, so a theme change reaches it for free and it does not read as a
 * different product inside the same shell.
 */
@Component({
  selector: 'app-business-functions',
  standalone: true,
  imports: [FormsModule, NgTemplateOutlet],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './business-functions.html',
  styleUrl: './business-functions.css',
  // Escape closes the open row wherever focus has got to inside it. Bound
  // on the host rather than the dialog, because the dialog's own children
  // take focus and a keydown on a textarea would otherwise never reach it.
  host: { '(document:keydown.escape)': 'escaped()' },
})
export class BusinessFunctions {
  private readonly api = inject(BusinessFunctionsApi);
  private readonly notice = inject(NoticeService);
  /** Public: the template reads it, and the shell's top bar writes it. */
  readonly layout = inject(BusinessFunctionsLayout);

  // -- what exists
  readonly functions = signal<FunctionCard[]>([]);
  readonly current = signal<FunctionDetail | null>(null);
  readonly switcherOpen = signal(false);

  // -- where we are
  readonly feature = signal<FeatureView | null>(null);
  readonly featureId = signal('');

  // -- the two selectors. Held here rather than on the view so switching tab
  //    does not lose them, and so the rail can send them with every question.
  readonly entity = signal('');
  readonly period = signal('');

  // -- the date window, for a screen that declares one. Separate from the
  //    scope selectors because it is a different kind of question: the
  //    period says which year's limits apply, the window says which orders
  //    to put in a report. A screen can have both and they are not the same
  //    dates.
  readonly since = signal('');
  readonly until = signal('');

  readonly loading = signal(false);
  readonly error = signal('');

  // -- the rail
  readonly turns = signal<RailTurn[]>([]);
  readonly draft = signal('');
  readonly thinking = signal(false);

  /** Which rows a waiting plan would touch, so the table can mark them. */
  readonly aimed = signal<Set<string>>(new Set());

  // -- a form, when one is open. Three signals rather than one object so a
  //    keystroke repaints the field and not the whole panel.
  readonly openForm = signal<FormSpec | null>(null);
  /** The row an open form is about, for a form opened from one. */
  readonly formAbout = signal('');
  readonly entered = signal<Record<string, string>>({});
  /** What the server said this entry would do. Cleared by every edit, because
   *  a preview of what you typed a moment ago is worse than none. */
  readonly previewed = signal<ActResult | null>(null);
  readonly saving = signal(false);

  readonly rail = computed(() => this.feature()?.rail ?? this.current()?.rail ?? null);
  readonly atOverview = computed(() => this.feature() === null);

  // -- which SERVICE is open, if any. The sidebar lists services and not
  //    their parts: the parts are the cards on the service's own page,
  //    which is where somebody reads what each is for. Listing them in
  //    both places said the same thing twice.
  readonly serviceId = signal('');

  /** The open service, or null at the function's own overview. */
  readonly openedService = computed(() => {
    const id = this.serviceId();
    return (this.current()?.services ?? []).find((s) => s.id === id) ?? null;
  });

  /** The cards to show: a service's sub-services, or nothing at the top. */
  readonly shown = computed(() => this.openedService()?.features ?? []);

  /** Whether what is open is a NAMED service, rather than the unnamed
   *  bucket a function with no services sends its own screens in. Both are
   *  "open" — they fill the same grid — but only one of them has parts, and
   *  calling Form 26 a sub-service of nothing read as a missing level. */
  readonly insideService = computed(() => !!this.openedService()?.id);

  /** Open a service — its sub-services, as cards. */
  async openService(id: string): Promise<void> {
    this.serviceId.set(id);
    this.feature.set(null);
    this.featureId.set('');
    this.aimed.set(new Set());
    this.resetRail();
    const fn = this.current();
    if (!fn) return;
    try {
      this.current.set(await this.api.detail(fn.id, this.scope()));
    } catch {
      // What is already on screen is true enough; a failed refresh is not
      // worth replacing a working page with an error.
    }
  }

  /** What a scope dimension may be set to, for the feature that is open.
   *
   *  Asked of the feature rather than held here. The first version of this
   *  carried one list of quarters for the whole section, which read fine
   *  until a feature arrived whose limit is annual and was offered "Q2 FY25"
   *  to total a year against. The values are a claim about the firm, so they
   *  live with the rest of the manifest's words. */
  choicesFor(id: string, dimension: string): string[] {
    return this.featureById(id)?.choices?.[dimension] ?? [];
  }

  constructor() {
    // The component is created the first time the section is entered and kept
    // after, so loading here runs once and not on every visit. The shell knows
    // the section exists and nothing else about it — it does not fetch on the
    // component's behalf and then have to decide when to refetch.
    void this.load();
  }

  async load(): Promise<void> {
    if (this.functions().length) return;
    this.loading.set(true);
    this.error.set('');
    try {
      const { functions } = await this.api.functions();
      this.functions.set(functions);
      if (functions.length) await this.openFunction(functions[0].id);
    } catch (err) {
      this.error.set(String(err));
    } finally {
      this.loading.set(false);
    }
  }

  async openFunction(id: string): Promise<void> {
    this.switcherOpen.set(false);
    this.loading.set(true);
    this.error.set('');
    try {
      const detail = await this.api.detail(id, this.scope());
      this.current.set(detail);
      this.feature.set(null);
      this.featureId.set('');
      this.serviceId.set('');
      this.resetRail();
    } catch (err) {
      this.error.set(String(err));
    } finally {
      this.loading.set(false);
    }
  }

  /** Back to the overview, keeping the function and the selectors. */
  async toOverview(): Promise<void> {
    const fn = this.current();
    if (!fn) return;
    this.feature.set(null);
    this.featureId.set('');
    this.serviceId.set('');
    this.aimed.set(new Set());
    this.resetRail();
    // Re-read so the queue reflects anything that just changed.
    try {
      this.current.set(await this.api.detail(fn.id, this.scope()));
    } catch {
      // The overview we already have is still true enough to show; a failed
      // refresh is not worth replacing the screen with an error.
    }
  }

  async openFeature(featureId: string, tab = ''): Promise<void> {
    const fn = this.current();
    if (!fn) return;
    // Keep the rail showing which service this belongs to.
    const owner = (fn.services ?? []).find(
      (s) => s.features.some((f) => f.id === featureId));
    if (owner) this.serviceId.set(owner.id);
    const fresh = featureId !== this.featureId();
    this.featureId.set(featureId);
    this.loading.set(true);
    this.error.set('');
    try {
      const view = await this.api.feature(fn.id, featureId, { ...this.scope(), tab });
      this.feature.set(view);
      if (fresh) this.resetRail();
      if (fresh) void this.loadReport();
    } catch (err) {
      this.error.set(String(err));
    } finally {
      this.loading.set(false);
    }
  }

  /** Re-read the workbench without touching the rail. After an action. */
  private async refresh(): Promise<void> {
    const fn = this.current();
    const id = this.featureId();
    if (!fn || !id) return;
    try {
      this.feature.set(
        await this.api.feature(fn.id, id, { ...this.scope(), tab: this.feature()?.tab ?? '' }),
      );
    } catch (err) {
      this.notice.error(`Could not refresh: ${err}`);
    }
  }

  async pickTab(tab: string): Promise<void> {
    await this.openFeature(this.featureId(), tab);
  }

  async setScope(which: 'entity' | 'period', value: string): Promise<void> {
    (which === 'entity' ? this.entity : this.period).set(value);
    // A plan made against one period must not survive into another.
    this.dropPlans('The selection changed, so that plan no longer applies.');
    if (this.featureId()) await this.openFeature(this.featureId(), this.feature()?.tab ?? '');
    else await this.openFunction(this.current()?.id ?? '');
  }

  private scope(): {
    entity?: string; period?: string; since?: string; until?: string;
  } {
    return {
      entity: this.entity(), period: this.period(),
      since: this.since(), until: this.until(),
    };
  }

  /** Move one end of the window. Empty means open-ended at that end. */
  async setWindow(which: 'since' | 'until', value: string): Promise<void> {
    (which === 'since' ? this.since : this.until).set(value);
    this.dropPlans('The window changed, so that plan no longer applies.');
    await this.openFeature(this.featureId(), this.feature()?.tab ?? '');
  }

  async clearWindow(): Promise<void> {
    this.since.set('');
    this.until.set('');
    await this.openFeature(this.featureId(), this.feature()?.tab ?? '');
  }

  // ── charts ──────────────────────────────────────────────────────────────
  //
  // Hand-drawn SVG rather than a charting library: every one of those
  // brings its own palette and its own canvas, and this module's whole
  // argument is that it introduces no colour of its own. A bar is a rect.

  /** The tallest value in a chart, so every bar is drawn against it. */
  private peak(c: ChartSpec): number {
    return Math.max(1, ...c.slices.map((s) => Math.max(s.value, s.against ?? 0)));
  }

  /** How wide a bar is, as a percentage of the widest. */
  barWidth(c: ChartSpec, s: ChartSlice): string {
    return `${Math.round((s.value / this.peak(c)) * 100)}%`;
  }

  /** The same for the number drawn behind it — a budget behind a spend. */
  againstWidth(c: ChartSpec, s: ChartSlice): string {
    return `${Math.round(((s.against ?? 0) / this.peak(c)) * 100)}%`;
  }

  /** True when the bar has outgrown what it is drawn against. */
  overspent(s: ChartSlice): boolean {
    return s.against !== null && s.value > s.against;
  }

  /** Wedges as SVG arc paths on a 100-unit circle, largest first. */
  wedges(c: ChartSpec): { path: string; slice: ChartSlice; share: string }[] {
    const total = c.slices.reduce((n, s) => n + s.value, 0);
    if (!total) return [];
    let from = -Math.PI / 2;
    return c.slices.map((slice) => {
      const sweep = (slice.value / total) * Math.PI * 2;
      const to = from + sweep;
      const big = sweep > Math.PI ? 1 : 0;
      const [x1, y1] = [50 + 42 * Math.cos(from), 50 + 42 * Math.sin(from)];
      const [x2, y2] = [50 + 42 * Math.cos(to), 50 + 42 * Math.sin(to)];
      // A single wedge is the whole circle, which an arc cannot draw.
      const path = sweep >= Math.PI * 2 - 0.001
        ? 'M50 8A42 42 0 1 1 49.9 8Z'
        : `M50 50L${x1.toFixed(2)} ${y1.toFixed(2)}` +
          `A42 42 0 ${big} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}Z`;
      from = to;
      return { path, slice, share: `${Math.round((slice.value / total) * 100)}%` };
    });
  }

  /** The chart whose figures are open over the dashboard, or null. */
  readonly chartOpen = signal<ChartSpec | null>(null);

  /** Open the rows a chart was drawn from, over the dashboard.
   *
   *  The tab is switched first so the grid gets its own columns and its
   *  own rows — then shown in a sheet rather than only below the fold,
   *  because the point of clicking a chart is to check it without losing
   *  sight of the picture you were reading. */
  async openChart(c: ChartSpec): Promise<void> {
    if (!c.tab || !c.slices.length) return;
    await this.pickTab(c.tab);
    this.chartOpen.set(c);
  }

  closeChart(): void {
    this.chartOpen.set(null);
  }

  // ── the table ───────────────────────────────────────────────────────────

  // -- the row somebody has opened, by id rather than by value. Holding the
  //    id means the panel re-reads the row after every refresh, so a stage
  //    that moved while the panel was open shows its new state instead of
  //    the copy that was captured when it opened.
  readonly viewing = signal<string | null>(null);

  /** The row being looked at, as it stands now, or null. */
  readonly viewed = computed(() => {
    const id = this.viewing();
    if (!id) return null;
    return (this.feature()?.rows ?? []).find((r) => this.rowId(r) === id) ?? null;
  });

  openRow(row: Record<string, unknown>): void {
    this.dropReason();
    this.viewing.set(this.rowId(row));
  }

  closeRow(): void {
    this.dropReason();
    this.viewing.set(null);
  }

  /** Escape closes whatever is on top: a row first, then a chart. */
  escaped(): void {
    if (this.viewing()) this.closeRow();
    else this.closeChart();
  }

  /** The id a row is known by, which differs per feature. */
  /** What identifies this row — the value an action is aimed at.
   *
   *  The feature says which field it is. This used to try `line`, then `id`,
   *  then `kind`, and a feature whose rows carried none of them got `''` for
   *  every row: one expander opened all of them and an action was sent at
   *  nothing. Guessing failed silently, so it does not guess. */
  rowId(row: Record<string, unknown>): string {
    const key = this.feature()?.row_key;
    return key ? String(row[key] ?? '') : '';
  }

  isAimed(row: Record<string, unknown>): boolean {
    return this.aimed().has(this.rowId(row));
  }

  // -- an action that asks for a reason. Which row it is about and what has
  //    been typed so far; both cleared the moment it is done or dropped.
  readonly asking = signal<{ action: ActionSpec; row: string } | null>(null);
  readonly reason = signal('');

  /** Begin an action that needs a reason, rather than performing it. */
  askReason(action: ActionSpec, row: Record<string, unknown>): void {
    this.asking.set({ action, row: this.rowId(row) });
    this.reason.set('');
  }

  dropReason(): void {
    this.asking.set(null);
    this.reason.set('');
  }

  /** What to ask before this action on THIS row, or '' to just do it.
   *
   *  The action's own label is the fallback. A row may override it, because
   *  on a screen with stages the question belongs to the stage and not to
   *  the button: hung off the action alone, somebody confirming a hamper
   *  had arrived was asked "What did you check?" and could not get past it.
   */
  asks(action: ActionSpec, row: Record<string, unknown>): string {
    const per = row['ask'] as Record<string, string> | undefined;
    if (per && action.id in per) return per[action.id];
    return action.note_label ?? '';
  }

  isAsking(action: ActionSpec, row: Record<string, unknown>): boolean {
    const open = this.asking();
    return !!open && open.action.id === action.id && open.row === this.rowId(row);
  }

  /** A row button: act now, because the click said which row.
   *
   *  Unless the action wants a reason, in which case the click opens a box
   *  and nothing happens until the person has written one. */
  async rowAct(action: ActionSpec, row: Record<string, unknown>,
               note = ''): Promise<void> {
    const fn = this.current();
    if (!fn) return;
    if (this.asks(action, row) && !note.trim()) {
      this.askReason(action, row);
      return;
    }
    const id = this.rowId(row);
    try {
      const out = await this.api.act(fn.id, this.featureId(), {
        action: action.id, targets: [id], note, ...this.scope(),
      });
      this.dropReason();
      // A decision that went through is finished with: the panel closes and
      // the table behind it is what answers "did that work". One that was
      // refused leaves the panel open, because the person is not done.
      if (out.ok) {
        this.viewing.set(null);
        this.notice.ok(out.said);
      } else {
        this.notice.error(out.said);
      }
      await this.refresh();
      this.say(out.said);
    } catch (err) {
      this.notice.error(`That did not go through: ${err}`);
    }
  }

  /** Which actions a row can take, given what the feature offers and its state. */
  actionsFor(row: Record<string, unknown>): ActionSpec[] {
    const all = this.feature()?.actions ?? [];
    const status = String(row['status'] ?? '');
    if (this.featureId() === 'form26') {
      return status === 'short'
        ? all.filter((a) => a.id === 'accept' || a.id === 'query')
        : all.filter((a) => a.id === 'undo');
    }
    if (this.featureId() === 'lms') {
      return status === 'open' ? all.filter((a) => a.id !== 'undo') : [];
    }
    // A row that says what may be done to it is believed, whatever feature
    // it came from. The server knows who holds which role, whose record it
    // is and what state it is in; none of that is knowable here.
    //
    // This used to sit inside the RewardLens branch, so a feature added
    // afterwards fell through to "offer everything the manifest declares" —
    // and Gift requests showed an approver the Bought and Handed over
    // buttons, which are Procurement's and which the server refuses. One
    // rule, above the per-feature guessing.
    if (Array.isArray(row['can'])) {
      const can = row['can'] as string[];
      return all.filter((a) => can.includes(a.id));
    }
    // A row that says nothing offers nothing. It used to offer EVERYTHING
    // the manifest declared, which held only while every feature either
    // sent `can` or was named above — and the Tracker stopped being named
    // the moment its id changed from `rewardlens` to `register`. The row
    // went quiet, this fell through, and a within-limit record was given
    // nine buttons: refer it, except it, accept a declaration that was not
    // there, send an email nobody had tried. Eight refusals and one real
    // action, and no way to tell them apart until you clicked.
    //
    // So the default is nothing. A feature that wants a button says so on
    // the row, where the server is — the only place that knows who holds
    // which role, whose record it is and what state it is in.
    return [];
  }

  // ── a standing request ──────────────────────────────────────────────────

  readonly report = signal<ReportState | null>(null);
  readonly reportBusy = signal(false);
  readonly reportDay = signal(1);

  private async loadReport(): Promise<void> {
    const fn = this.current();
    if (!fn || !this.featureId()) return this.report.set(null);
    try {
      const state = await this.api.report(fn.id, this.featureId());
      this.report.set(state);
      if (state.day) this.reportDay.set(state.day);
    } catch {
      // A screen that works is worth more than knowing about a subscription.
      this.report.set(null);
    }
  }

  async toggleReport(): Promise<void> {
    const fn = this.current();
    if (!fn || this.reportBusy()) return;
    this.reportBusy.set(true);
    try {
      const on = this.report()?.subscribed;
      const state = on
        ? await this.api.stopReport(fn.id, this.featureId())
        : await this.api.setReport(fn.id, this.featureId(), {
            period: this.period(), day: this.reportDay(), time_of_day: '08:00',
          });
      this.report.set(state);
      if (!on && state.warning) this.notice.error(state.warning);
      else {
        this.notice.ok(on ? 'Monthly report stopped.'
                          : `Monthly report on. ${state.summary ?? ''}`);
      }
    } catch (err) {
      this.notice.error(`Could not change that: ${err}`);
    } finally {
      this.reportBusy.set(false);
    }
  }

  /** Send it now, so somebody can see what will arrive rather than imagine it. */
  async sendReportNow(): Promise<void> {
    const fn = this.current();
    if (!fn || this.reportBusy()) return;
    this.reportBusy.set(true);
    try {
      const state = await this.api.sendReportNow(fn.id, this.featureId());
      this.report.set(state);
      this.notice.ok(state.runs?.[0]?.said ?? 'Sent.');
    } catch (err) {
      this.notice.error(`Could not send that: ${err}`);
    } finally {
      this.reportBusy.set(false);
    }
  }

  // ── taking it away ──────────────────────────────────────────────────────

  readonly exporting = signal(false);

  /** Download this screen as a workbook.
   *
   *  The server builds it from the same rows this screen is showing, so
   *  there is nothing to assemble here — which is the point: a spreadsheet
   *  put together in the browser could disagree with the screen it came
   *  from, and the spreadsheet is the thing that gets emailed on. */
  /** Which single order is being downloaded, so one row's button can say so
   *  without disabling the other rows'. */
  readonly exportingRow = signal('');

  async download(row = ''): Promise<void> {
    const fn = this.current();
    if (!fn || this.exporting()) return;
    this.exporting.set(true);
    this.exportingRow.set(row);
    try {
      const { blob, filename } = await this.api.workbook(
        fn.id, this.featureId(), { ...this.scope(), row },
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      // Freed on the next turn of the loop; revoking immediately races the
      // click in some browsers and the download arrives empty.
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      this.notice.ok(`Saved ${filename}`);
    } catch (err) {
      this.notice.error(`Could not export that: ${err}`);
    } finally {
      this.exporting.set(false);
      this.exportingRow.set('');
    }
  }

  // ── forms ───────────────────────────────────────────────────────────────
  //
  // A form makes a row rather than deciding one, so it is not an action and
  // does not go through the rail. The assistant is deliberately not involved:
  // a declaration is the person's own statement about what they were given,
  // and one phrased for them is not their statement.

  startForm(form: FormSpec, row: Record<string, unknown> | null = null): void {
    this.openForm.set(form);
    this.formAbout.set(row ? this.rowId(row) : '');
    this.dropReason();
    this.previewed.set(null);
    // Date fields start on today, which is right far more often than blank
    // and is the only default worth guessing.
    const today = new Date().toISOString().slice(0, 10);
    const start: Record<string, string> = {};
    for (const field of form.fields) start[field.id] = field.kind === 'date' ? today : '';
    this.entered.set(start);
  }

  /** The form opened from this row, if it is the one that is open. */
  formOn(row: Record<string, unknown>): FormSpec | null {
    const form = this.openForm();
    return form?.on_row && this.formAbout() === this.rowId(row) ? form : null;
  }

  /** The row forms this row may open — one per action it is allowed. */
  formsFor(row: Record<string, unknown>): FormSpec[] {
    const can = (row['can'] as string[] | undefined) ?? [];
    return (this.feature()?.forms ?? []).filter(
      (f) => f.on_row && can.includes(f.id),
    );
  }

  /** The forms that stand alone, shown in the header. */
  headerForms(): FormSpec[] {
    return (this.feature()?.forms ?? []).filter((f) => !f.on_row);
  }

  closeForm(): void {
    this.openForm.set(null);
    this.formAbout.set('');
    this.entered.set({});
    this.previewed.set(null);
  }

  setField(id: string, value: string): void {
    this.entered.update((v) => ({ ...v, [id]: value }));
    // What they were told no longer describes what is in the boxes.
    this.previewed.set(null);
  }

  /** Every required field has something in it. Not validation — the server
   *  decides what the values mean; this only stops an obviously empty send. */
  readonly formReady = computed(() => {
    const form = this.openForm();
    if (!form) return false;
    const values = this.entered();
    return form.fields.every((f) => !f.required || (values[f.id] ?? '').trim());
  });

  async checkForm(): Promise<void> {
    const fn = this.current();
    const form = this.openForm();
    if (!fn || !form) return;
    this.saving.set(true);
    try {
      this.previewed.set(
        await this.api.previewForm(fn.id, this.featureId(), form.id, {
          values: this.entered(), about: this.formAbout(), ...this.scope(),
        }),
      );
    } catch (err) {
      this.notice.error(`Could not check that: ${err}`);
    } finally {
      this.saving.set(false);
    }
  }

  async submitForm(): Promise<void> {
    const fn = this.current();
    const form = this.openForm();
    if (!fn || !form) return;
    this.saving.set(true);
    try {
      const out = await this.api.submitForm(fn.id, this.featureId(), form.id, {
        values: this.entered(), about: this.formAbout(), ...this.scope(),
      });
      if (out.ok) {
        this.notice.ok(out.said);
        // Said in the rail too: what a declaration did to a total is the
        // thing worth still having on screen after the panel closes.
        this.say(out.said);
        this.closeForm();
        await this.refresh();
      } else {
        // A refusal is an answer, not an error — it stays in the panel beside
        // the boxes it is about, so the person can fix what it names.
        this.previewed.set(out);
      }
    } catch (err) {
      this.notice.error(`That did not go through: ${err}`);
    } finally {
      this.saving.set(false);
    }
  }

  // ── the rail ────────────────────────────────────────────────────────────

  private resetRail(): void {
    this.turns.set([]);
    this.draft.set('');
    this.aimed.set(new Set());
  }

  private say(text: string): void {
    this.turns.update((t) => [...t, { who: 'assistant', text }]);
  }

  /** Mark every waiting plan as no longer answerable, and say why. */
  private dropPlans(why: string): void {
    let had = false;
    this.turns.update((turns) =>
      turns.map((t) => {
        if (t.plan && !t.settled) { had = true; return { ...t, settled: why }; }
        return t;
      }),
    );
    if (had) this.aimed.set(new Set());
  }

  async send(text?: string): Promise<void> {
    const question = (text ?? this.draft()).trim();
    const fn = this.current();
    if (!question || !fn || this.thinking()) return;

    this.draft.set('');
    this.turns.update((t) => [...t, { who: 'you', text: question }]);

    this.thinking.set(true);
    try {
      // At the overview the question is about the whole function, so it is
      // answered from every screen the asker can see. Inside one, it is
      // answered from that screen and can still become a plan. Telling
      // somebody to go somewhere else was a true description of what this
      // could do and a useless answer to "how many people received a gift".
      const result = this.featureId()
        ? await this.api.ask(fn.id, this.featureId(), {
            // The tab too: "that one" means a row they can see, and which
            // rows those are depends on which view is open.
            text: question, tab: this.feature()?.tab ?? '', ...this.scope(),
          })
        : await this.api.askFunction(fn.id, { text: question, ...this.scope() });
      this.render(result);
    } catch (err) {
      this.say(`I could not read that: ${err}`);
    } finally {
      this.thinking.set(false);
    }
  }

  private render(result: AskResult): void {
    if (result.kind === 'plan') {
      this.turns.update((t) => [
        ...t, { who: 'assistant', text: '', plan: result.plan },
      ]);
      this.aimed.set(new Set(result.plan.targets.map((x) => x.id)));
      return;
    }
    if (result.kind === 'clarify') {
      this.turns.update((t) => [
        ...t, { who: 'assistant', text: result.text, options: result.options },
      ]);
      return;
    }
    // An answer carries the facts a model would phrase. Until that call is
    // wired, the figures are shown as they came — which is honest: nothing
    // here is invented, and a sentence will replace it rather than the data.
    const facts = result.facts as { figures?: [string, string, string][] };
    const line = (facts?.figures ?? [])
      .map(([, value, caption]) => `${value} ${caption}`.trim())
      .join(' · ');
    this.say(result.text || line || 'Nothing on this screen answers that.');
  }

  async confirmPlan(turn: RailTurn): Promise<void> {
    const fn = this.current();
    if (!fn || !turn.plan || turn.settled) return;
    const plan = turn.plan;
    try {
      const out = await this.api.confirmPlan(plan.id, fn.id, this.featureId());
      this.settle(plan.id, out.said);
      this.aimed.set(new Set());
      if (out.ok) { this.notice.ok(out.said); await this.refresh(); }
      else this.notice.error(out.said);
    } catch (err) {
      this.settle(plan.id, `That did not go through: ${err}`);
    }
  }

  async cancelPlan(turn: RailTurn): Promise<void> {
    if (!turn.plan || turn.settled) return;
    const plan = turn.plan;
    try {
      const out = await this.api.cancelPlan(plan.id);
      this.settle(plan.id, out.said);
    } catch {
      this.settle(plan.id, 'Cancelled — nothing changed.');
    }
    this.aimed.set(new Set());
  }

  private settle(planId: string, said: string): void {
    this.turns.update((turns) =>
      turns.map((t) => (t.plan?.id === planId ? { ...t, settled: said } : t)),
    );
  }

  /** A clarification's options are clickable: picking one re-asks precisely. */
  async pick(option: PlanTarget): Promise<void> {
    await this.send(option.label.split('·')[0].trim());
  }

  // ── small helpers the template reads ────────────────────────────────────

  /** A figure's tone as a class suffix. The server said what it means. */
  toneOf(f: Figure): string {
    return f.tone === 'plain' ? '' : f.tone;
  }

  featureById(id: string): FeatureCard | undefined {
    return this.current()?.features.find((f) => f.id === id);
  }

  /** Column headings for the current feature's table.
   *
   *  Per-feature because a reconciliation and an approval queue are not the
   *  same table with different data — they are different tables. When a third
   *  feature arrives this becomes something the server sends. */
  columns(): { key: string; label: string; numeric?: boolean }[] {
    if (this.featureId() === 'form26') {
      return [
        { key: 'line', label: 'Line' },
        { key: 'deductor', label: 'Deductor' },
        { key: 'credit', label: 'Form 26', numeric: true },
        { key: 'ledger', label: 'Ledger', numeric: true },
        { key: 'difference', label: 'Difference', numeric: true },
        { key: 'status', label: 'Status' },
      ];
    }
    if (this.featureId() === 'lms') {
      return [
        { key: 'who', label: 'Person' },
        { key: 'dates', label: 'Dates' },
        { key: 'kind', label: 'Type' },
        { key: 'waiting_days', label: 'Waiting' },
        { key: 'status', label: 'Status' },
      ];
    }
    // RewardLens lists people, not purchases. A list of purchases is what the
    // firm already had; the total against the limit is the thing it did not.
    // Except on the declarations tab: a review is a thing an ITEM has, so
    // that one lists items.
    // The Tracker, in its three shapes: an order on its way through the
    // chain, a declaration being reviewed, and a person's year against the
    // limit. Keyed on `register`, which is what the sub-service is called —
    // under the old id this branch matched nothing, the columns fell
    // through to one derived from the row's own keys, and the panel headed
    // itself "E-1052" and listed "ITEMS COUNTED" and "UNACKNOWLEDGED".
    if (this.featureId() === 'register') {
      const tab = this.feature()?.tab;
      // The desk's queue, the journey and where orders land are three
      // readings of the same row, so they are one shape — what differs is
      // which column earns its place at the end: what is waiting on you
      // says how long, and a finished order says how it finished.
      if (tab === 'to_sign' || tab === 'orders' || tab === 'completed') {
        return [
          { key: 'id', label: 'Order' },
          { key: 'what', label: 'What' },
          { key: 'recipient', label: 'For' },
          { key: 'quantity', label: 'Qty', numeric: true },
          { key: 'value', label: 'Each', numeric: true },
          { key: 'progress', label: 'Progress' },
          ...(tab === 'completed'
            ? [{ key: 'status', label: 'Status' }]
            : [{ key: 'with', label: 'With' },
               { key: 'waiting_days', label: 'Waiting', numeric: true }]),
        ];
      }
      if (this.feature()?.tab === 'disclosed') {
        return [
          { key: 'declared_by', label: 'Declared by' },
          { key: 'what', label: 'What' },
          { key: 'value', label: 'Value', numeric: true },
          { key: 'counts', label: 'Counts' },
          { key: 'status', label: 'Review' },
          // Whether they were actually told. In the table rather than the
          // detail, because a reviewer scanning the queue is exactly who
          // needs to notice that somebody was never reached.
          { key: 'notified', label: 'Told' },
        ];
      }
      return [
        { key: 'recipient', label: 'Person' },
        { key: 'items_counted', label: 'Items', numeric: true },
        { key: 'total', label: 'Total', numeric: true },
        { key: 'excess', label: 'Over by', numeric: true },
        { key: 'unacknowledged', label: 'Unack.', numeric: true },
        { key: 'status', label: 'Status' },
      ];
    }
    // Audit, in two shapes: the ORDER end to end, and one event out of a
    // trail. The order is the row somebody arrives with a number for.
    if (this.featureId() === 'audit') {
      if (this.feature()?.tab === 'orders') {
        return [
          { key: 'id', label: 'Order' },
          { key: 'what', label: 'What' },
          { key: 'recipient', label: 'For' },
          { key: 'value', label: 'Value' },
          { key: 'raised_on', label: 'Raised' },
          { key: 'received_on', label: 'Received' },
          { key: 'steps', label: 'Steps', numeric: true },
          { key: 'status', label: 'Status' },
        ];
      }
      return [
        { key: 'when', label: 'When' },
        { key: 'who', label: 'Who' },
        { key: 'did', label: 'Did' },
        { key: 'what', label: 'What' },
        { key: 'detail', label: 'Detail' },
      ];
    }
    // Finance: three questions, three shapes.
    if (this.featureId() === 'finance') {
      const tab = this.feature()?.tab;
      // What leaves the function. The taxable amount is the column payroll
      // acts on, so it is the one that reads as a figure.
      if (tab === 'payroll') {
        return [
          { key: 'employee_id', label: 'Employee' },
          { key: 'recipient', label: 'Person' },
          { key: 'received', label: 'Received', numeric: true },
          { key: 'allowance', label: 'Allowance', numeric: true },
          { key: 'taxable', label: 'Taxable', numeric: true },
          { key: 'status', label: 'Finance' },
        ];
      }
      if (tab === 'budgets') {
        return [
          { key: 'source', label: 'Team' },
          { key: 'budget', label: 'Budget', numeric: true },
          { key: 'committed', label: 'Committed', numeric: true },
          { key: 'not_requested', label: 'Never requested', numeric: true },
          { key: 'left', label: 'Left', numeric: true },
          { key: 'status', label: 'Status' },
        ];
      }
      if (tab === 'taxable') {
        return [
          { key: 'recipient', label: 'Person' },
          { key: 'total', label: 'Total', numeric: true },
          { key: 'limit', label: 'Limit', numeric: true },
          { key: 'above_limit', label: 'Above the limit', numeric: true },
          { key: 'crossed_on', label: 'Over since' },
          { key: 'status', label: 'Status' },
        ];
      }
      return [
        { key: 'recipient', label: 'Person' },
        { key: 'total', label: 'Total', numeric: true },
        { key: 'above_limit', label: 'Above the limit', numeric: true },
        { key: 'referred_by', label: 'Referred by' },
        { key: 'when', label: 'When' },
        { key: 'status', label: 'Status' },
      ];
    }
    // Gift requests: what is being asked for, with the three answers the
    // approver needs on the row rather than a click away.
    if (this.featureId() === 'requests') {
      return [
        { key: 'recipient', label: 'For' },
        { key: 'what', label: 'What' },
        { key: 'quantity', label: 'Qty', numeric: true },
        { key: 'value', label: 'Each', numeric: true },
        { key: 'allocation', label: 'Allocated' },
        { key: 'bought_on', label: 'Bought on' },
        { key: 'policy', label: 'Policy' },
        { key: 'tax', label: 'Their year' },
        { key: 'status', label: 'Status' },
      ];
    }
    // The dashboard. Four tabs, four shapes: the budget against the spend,
    // where the value comes from, who is past the notable figure, and what
    // each team has pushed over the limit.
    if (this.featureId() === 'oversight') {
      const tab = this.feature()?.tab;
      // The four views the charts are drawn from. Without these the grid
      // behind a chart fell through to the default columns and showed a
      // column of dashes — a table claiming the chart had no numbers.
      if (tab === 'budgets') {
        return [
          { key: 'source', label: 'Team' },
          { key: 'budget', label: 'Budget', numeric: true },
          { key: 'committed', label: 'Committed', numeric: true },
          { key: 'left', label: 'Left', numeric: true },
          { key: 'status', label: 'Status' },
        ];
      }
      if (tab === 'teams') {
        return [
          { key: 'source', label: 'Team' },
          { key: 'given', label: 'Given', numeric: true },
          { key: 'items', label: 'Gifts', numeric: true },
          { key: 'people', label: 'People', numeric: true },
        ];
      }
      if (tab === 'notable') {
        return [
          { key: 'employee_id', label: 'Employee' },
          { key: 'recipient', label: 'Person' },
          { key: 'total', label: 'This year', numeric: true },
          { key: 'items', label: 'Gifts', numeric: true },
          { key: 'over_limit_by', label: 'Over the limit by', numeric: true },
        ];
      }
      if (tab === 'tax') {
        return [
          { key: 'source', label: 'Team' },
          { key: 'above_limit', label: 'Above the limit', numeric: true },
          { key: 'people', label: 'People', numeric: true },
        ];
      }
      if (tab === 'stuck') {
        return [
          { key: 'who', label: 'Who' },
          { key: 'what', label: 'What' },
          { key: 'problem', label: 'What stopped' },
          { key: 'waiting', label: 'Waiting' },
        ];
      }
      if (tab === 'sources') {
        return [
          { key: 'source', label: 'Where from' },
          { key: 'people', label: 'People', numeric: true },
          { key: 'items', label: 'Items', numeric: true },
          { key: 'value', label: 'Value', numeric: true },
          { key: 'to_people_over', label: 'To people over', numeric: true },
        ];
      }
      return [
        { key: 'recipient', label: 'Person' },
        { key: 'total', label: 'Total', numeric: true },
        { key: 'excess', label: 'Over by', numeric: true },
        { key: 'crossed_on', label: 'Over since' },
        { key: 'status', label: 'Status' },
      ];
    }
    return [];
  }

  /** Indian digit grouping — ₹20,000, ₹1,50,000. Mirrors the server's own
   *  formatter, which is what the assistant's sentences are written with. */
  static rupees(amount: number): string {
    const digits = String(Math.abs(Math.round(amount)));
    if (digits.length <= 3) return `₹${digits}`;
    const tail = digits.slice(-3);
    let head = digits.slice(0, -3);
    const parts: string[] = [];
    while (head.length > 2) {
      parts.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) parts.unshift(head);
    return `₹${[...parts, tail].join(',')}`;
  }

  cell(row: Record<string, unknown>, key: string): string {
    const value = row[key];
    if (value === undefined || value === null || value === '') return '—';
    // Every rule below is keyed on the column's NAME, which is only safe
    // while a name means one thing everywhere. It stopped being: `budget`
    // is a number of rupees on Finance's screen and a sentence — "₹74,000
    // would be left of ₹80,000" — on the request chain, and every row of
    // the chain showed ₹NaN. A string has already been formatted by the
    // handler that wrote it, so there is nothing here left to do to it.
    if (typeof value === 'string') return value;
    if (key === 'credit' || key === 'ledger' || key === 'difference') {
      return `₹${(Number(value) / 100000).toFixed(1)}L`;
    }
    // Gifts are counted in thousands, so they read in thousands. Lakh
    // notation is the right shorthand for a credit statement and the wrong
    // one for eight thousand rupees.
    if (key === 'total' || key === 'excess' || key === 'headroom' || key === 'limit') {
      return Number(value) === 0 ? '—' : BusinessFunctions.rupees(Number(value));
    }
    // A declared item's own amount. Never '—' at zero: the server refuses a
    // declaration of nothing, so a zero here would be a bug worth seeing.
    if (key === 'value') return BusinessFunctions.rupees(Number(value));
    if (key === 'above_limit' || key === 'budget' || key === 'committed'
        || key === 'left' || key === 'not_requested' || key === 'given'
        || key === 'over_limit_by') {
      return Number(value) === 0 ? '—' : BusinessFunctions.rupees(Number(value));
    }
    if (key === 'to_people_over') {
      return Number(value) === 0 ? '—' : BusinessFunctions.rupees(Number(value));
    }
    // Days, and "today" rather than "0d" — a thing that arrived this
    // morning has not been waiting, and saying 0 invites reading it as
    // nothing at all.
    if (key === 'waiting') return Number(value) ? `${value}d` : 'today';
    if (key === 'waiting_days') return `${value}d`;
    return String(value);
  }

  /** The detail rows under an expanded line. Whatever the row carries that the
   *  table did not show — so a new field on the server appears without a
   *  frontend change. */
  /** Everything worth reading about one row, for the panel.
   *
   *  The table's own columns first — the panel covers the table, so a
   *  reader who opened a row should not have to close it to see the value
   *  they were looking at — then whatever else the row carries. */
  fullDetails(row: Record<string, unknown>): { k: string; v: string }[] {
    // Not what the heading already says, not the status (it is the chip),
    // and nothing empty — a panel listing six fields as "—" buries the two
    // that have an answer.
    const said = new Set([...this.titleKeys(row), 'status']);
    const shown = this.columns()
      .filter((c) => !said.has(c.key) && this.cell(row, c.key) !== '\u2014')
      .map((c) => ({ k: c.label, v: this.cell(row, c.key) }));
    return [...shown, ...this.details(row)]
      .filter((d) => d.v && d.v !== '\u2014');
  }

  /** The steps a row carries, when it carries a history rather than a fact.
   *
   *  Only the audit's orders do, today. Typed off the row rather than
   *  declared per feature, because what the sheet needs to know is whether
   *  THIS row has a trail — and a feature that gains one later gets the
   *  rendering without a line here. */
  trailOf(row: Record<string, unknown>): TrailStep[] | null {
    const steps = row['trail'];
    if (!Array.isArray(steps) || !steps.length) return null;
    return steps as TrailStep[];
  }

  /** The heading of the panel: the row under the first column that has one. */
  rowTitle(row: Record<string, unknown>): string {
    const named = this.titleKeys(row).map((k) => this.cell(row, k));
    return named.join(' \u00b7 ') || this.rowId(row);
  }

  /** The columns the panel's heading already says, so the body can skip
   *  them. Two at most: one is often just a name, and a heading that reads
   *  "Vikram Rao" does not say which of his four orders you opened. */
  private titleKeys(row: Record<string, unknown>): string[] {
    return this.columns()
      .filter((c) => !c.numeric && c.key !== 'status'
        && this.cell(row, c.key) !== '\u2014')
      .slice(0, 2)
      .map((c) => c.key);
  }

  details(row: Record<string, unknown>): { k: string; v: string }[] {
    const shown = new Set(this.columns().map((c) => c.key));
    // What the handler says is for this screen's machinery rather than for
    // reading, plus the identifiers every feature carries. The handler's
    // list is the one the workbook obeys, so the two cannot disagree — kept
    // by hand here, this list had already drifted and a reader was shown
    // "stage: payroll_tax".
    const skip = new Set([...shown, ...(this.feature()?.private ?? []),
                      'late', 'initials', 'role', 'id', 'days',
                      'employee_id', 'over_limit', 'referred', 'exception',
                      'disclosed', 'items', 'state', 'can', 'yours']);
    return Object.entries(row)
      .filter(([k]) => !skip.has(k))
      .map(([k, v]) => ({ k: k.replace(/_/g, ' '), v: this.cell(row, k) }));
  }
}
