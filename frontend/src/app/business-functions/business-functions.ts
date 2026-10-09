import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';

import { NoticeService } from '../notice.service';
import { BusinessFunctionsLayout } from './bf-layout.service';
import {
  ActionSpec,
  ActResult,
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
} from './bf-api';

/** The actions that decide a declared item rather than a person's year.
 *  Named here so the table can tell the two sets apart; which of them a
 *  given row may take is still the server's answer, not this list's. */
const REVIEW_ACTIONS = new Set([
  'accept_disclosure', 'query_disclosure', 'record_breach',
]);

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
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './business-functions.html',
  styleUrl: './business-functions.css',
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
  readonly entered = signal<Record<string, string>>({});
  /** What the server said this entry would do. Cleared by every edit, because
   *  a preview of what you typed a moment ago is worse than none. */
  readonly previewed = signal<ActResult | null>(null);
  readonly saving = signal(false);

  readonly rail = computed(() => this.feature()?.rail ?? this.current()?.rail ?? null);
  readonly atOverview = computed(() => this.feature() === null);

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
    const fresh = featureId !== this.featureId();
    this.featureId.set(featureId);
    this.loading.set(true);
    this.error.set('');
    try {
      const view = await this.api.feature(fn.id, featureId, { ...this.scope(), tab });
      this.feature.set(view);
      if (fresh) this.resetRail();
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

  private scope(): { entity?: string; period?: string } {
    return { entity: this.entity(), period: this.period() };
  }

  // ── the table ───────────────────────────────────────────────────────────

  readonly expanded = signal<string | null>(null);

  toggleRow(id: string): void {
    this.expanded.update((open) => (open === id ? null : id));
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
    if (action.note_label && !note.trim()) {
      this.askReason(action, row);
      return;
    }
    const id = this.rowId(row);
    try {
      const out = await this.api.act(fn.id, this.featureId(), {
        action: action.id, targets: [id], note, ...this.scope(),
      });
      this.dropReason();
      if (out.ok) this.notice.ok(out.said);
      else this.notice.error(out.said);
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
    if (this.featureId() === 'rewardlens') {
      // A declared item is reviewed; a person's year is referred, chased or
      // excepted. Two kinds of row, so two sets of buttons, and neither is
      // ever offered against the other.
      if (this.feature()?.tab === 'disclosed') {
        // The server decides, because only it knows who holds the reviewer
        // role and whose declaration this is. Asking the row rather than
        // guessing from the state is what stops somebody being offered a
        // button and then told they could not have used it.
        return row['can_review'] === true
          ? all.filter((a) => REVIEW_ACTIONS.has(a.id))
          : [];
      }
      // Only what this row can actually take. A referral that would be
      // refused is a button that should not have been drawn.
      const over = row['over_limit'] === true;
      const referred = row['referred'] === true;
      const unack = Number(row['unacknowledged'] ?? 0) > 0;
      return all.filter((a) =>
        (a.id === 'refer_to_finance' && over && !referred)
        || (a.id === 'record_exception' && over)
        || (a.id === 'chase_acknowledgement' && unack));
    }
    return all;
  }

  // ── forms ───────────────────────────────────────────────────────────────
  //
  // A form makes a row rather than deciding one, so it is not an action and
  // does not go through the rail. The assistant is deliberately not involved:
  // a declaration is the person's own statement about what they were given,
  // and one phrased for them is not their statement.

  startForm(form: FormSpec): void {
    this.openForm.set(form);
    this.previewed.set(null);
    // Date fields start on today, which is right far more often than blank
    // and is the only default worth guessing.
    const today = new Date().toISOString().slice(0, 10);
    const start: Record<string, string> = {};
    for (const field of form.fields) start[field.id] = field.kind === 'date' ? today : '';
    this.entered.set(start);
  }

  closeForm(): void {
    this.openForm.set(null);
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
          values: this.entered(), ...this.scope(),
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
        values: this.entered(), ...this.scope(),
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

    // At the overview there is no feature to ask about yet. Said plainly
    // rather than silently doing nothing, because the composer is there.
    if (!this.featureId()) {
      this.say(`Open a feature and I will follow you in — then I can read what is on screen and act on it.`);
      return;
    }

    this.thinking.set(true);
    try {
      const result = await this.api.ask(fn.id, this.featureId(), {
        // The tab too: "that one" means a row they can see, and which rows
        // those are depends on which view is open.
        text: question, tab: this.feature()?.tab ?? '', ...this.scope(),
      });
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
    if (this.featureId() === 'rewardlens') {
      if (this.feature()?.tab === 'disclosed') {
        return [
          { key: 'declared_by', label: 'Declared by' },
          { key: 'what', label: 'What' },
          { key: 'value', label: 'Value', numeric: true },
          { key: 'given', label: 'Received' },
          { key: 'counts', label: 'Counts' },
          { key: 'status', label: 'Review' },
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
    if (key === 'waiting_days') return `${value}d`;
    return String(value);
  }

  /** The detail rows under an expanded line. Whatever the row carries that the
   *  table did not show — so a new field on the server appears without a
   *  frontend change. */
  details(row: Record<string, unknown>): { k: string; v: string }[] {
    const shown = new Set(this.columns().map((c) => c.key));
    const skip = new Set([...shown, 'late', 'initials', 'role', 'id', 'days',
                      'employee_id', 'over_limit', 'referred', 'exception',
                      'disclosed', 'items', 'state', 'can_review']);
    return Object.entries(row)
      .filter(([k]) => !skip.has(k))
      .map(([k, v]) => ({ k: k.replace(/_/g, ' '), v: this.cell(row, k) }));
  }
}
