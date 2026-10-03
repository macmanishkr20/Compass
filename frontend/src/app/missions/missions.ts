import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';

import { CompassApiService } from '../compass-api.service';
import { ConfirmService } from '../confirm.service';
import { NoticeService } from '../notice.service';
import { ActivityKind, MissionActivityService } from './mission-activity.service';
import {
  CompassEvent,
  MissionDetail,
  MissionFeature,
  MissionFeatureState,
  MissionSession,
  MissionSummary,
} from '../models';

/**
 * Missions — long-running builds that plan, work one feature at a time, and
 * review themselves.
 *
 * The screen is built around the one number that means anything here:
 * features passing out of features planned. A mission's plan is written once,
 * by the planner session, and never grows, so unlike a progress bar over a
 * task list it is a real fraction rather than a guess. Everything else on the
 * page exists to explain that number — which session claimed what, what a
 * reviewer said about it, and what has been spent getting there.
 *
 * Two screens, because the questions are different. The index answers "what
 * is happening and what has it cost"; opening one answers "what exactly is
 * built, and what did the reviewer say". The squares on a card are the join
 * between them: one per feature in plan order, so the shape of a build reads
 * at a glance and clicking through is for when it does not.
 *
 * A running mission streams the agent's own events plus the supervisor's
 * notes. Only the notes and the headline are kept: an hours-long build makes
 * more tool calls than anybody will read, and the transcript that matters is
 * `PROGRESS.md`, which the agent writes for the next session rather than for
 * the person watching.
 */
@Component({
  selector: 'app-missions',
  standalone: true,
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './missions.html',
  styleUrl: './missions.css',
})
export class Missions {
  private readonly api = inject(CompassApiService);
  private readonly confirm = inject(ConfirmService);
  private readonly notice = inject(NoticeService);
  /** Public so the template can render the feed the nav's pulse opens. */
  readonly feed = inject(MissionActivityService);

  readonly missions = signal<MissionSummary[]>([]);
  readonly openId = signal<string | null>(null);
  readonly detail = signal<MissionDetail | null>(null);
  readonly loading = signal(true);
  readonly error = signal('');

  // The composer for a new mission.
  readonly goal = signal('');
  readonly budget = signal(25);
  readonly creating = signal(false);

  // Live state while a mission runs.
  readonly running = signal<string | null>(null);
  readonly notes = signal<string[]>([]);
  readonly activity = signal('');

  // Index: searching, filtering, paging.
  readonly query = signal('');
  readonly filter = signal<'all' | 'run' | 'done' | 'cap' | 'stop'>('all');
  readonly page = signal(1);
  readonly PER = 4;

  // Detail: which feature, which tab, which session.
  readonly featureFilter = signal<'all' | MissionFeatureState>('all');
  readonly selected = signal<string | null>(null);
  readonly timelineOpen = signal(true);
  readonly openSession = signal<number | null>(null);

  /** Circumference of the r=17 progress ring, so the template can offset it. */
  readonly RING = 2 * Math.PI * 17;

  readonly examples = [
    { label: 'Expense CLI', brief: 'A command-line tool that tracks expenses in a JSON file, with add, list, and a monthly summary' },
    { label: 'Status page', brief: 'An internal status page that reads our health endpoints and shows uptime per service over 30 days' },
    { label: 'Slides converter', brief: 'A markdown-to-slides converter with three themes and a live preview' },
    { label: 'CSV validator', brief: 'A CSV schema validator with a CLI and a browser drop zone that reports errors by row' },
  ];

  readonly stateFilters = [
    { key: 'all' as const, label: 'All' },
    { key: 'run' as const, label: 'Running' },
    { key: 'done' as const, label: 'Complete' },
    { key: 'cap' as const, label: 'Budget reached' },
    { key: 'stop' as const, label: 'Stopped' },
  ];

  readonly featureTabs = [
    { key: 'all' as const, label: 'All' },
    { key: 'ok' as const, label: 'Accepted' },
    { key: 'rev' as const, label: 'In review' },
    { key: 'run' as const, label: 'Building' },
    { key: 'blk' as const, label: 'Returned' },
    { key: 'q' as const, label: 'Queued' },
  ];

  constructor() {
    void this.refresh();
  }

  // ── loading ─────────────────────────────────────────────────────────
  async refresh(): Promise<void> {
    this.loading.set(true);
    try {
      this.missions.set(await this.api.missions());
      this.error.set('');
      this.adopt();
    } catch (err) {
      this.error.set(String(err));
    } finally {
      this.loading.set(false);
    }
  }

  /** Pick up a mission the server is already running.
   *
   *  The work belongs to the server, not to this tab — so arriving at the
   *  page while a build is under way should show it running, not idle. This
   *  attaches rather than starting: reopening a page must never be the thing
   *  that begins a build. */
  private adopt(): void {
    if (this.running()) return;
    const live = this.missions().find((m) => m.running);
    if (!live) return;
    this.running.set(live.id);
    this.notes.set([]);
    this.activity.set('rejoining…');
    this.feed.live.set(true);
    this.feed.push('session_start', `rejoined a mission already running`);
    void (async () => {
      try {
        await this.api.attachMission(live.id, (ev) => this.onEvent(ev));
      } catch {
        /* it finished between the list and the attach */
      } finally {
        this.running.set(null);
        this.activity.set('');
        this.feed.live.set(false);
        await this.refreshQuietly();
      }
    })();
  }

  async openMission(m: MissionSummary | string): Promise<void> {
    const id = typeof m === 'string' ? m : m.id;
    this.openId.set(id);
    this.detail.set(null);
    this.selected.set(null);
    this.featureFilter.set('all');
    this.openSession.set(null);
    try {
      this.detail.set(await this.api.mission(id));
    } catch (err) {
      this.error.set(String(err));
    }
  }

  closeMission(): void {
    this.openId.set(null);
    this.detail.set(null);
  }

  async create(): Promise<void> {
    const goal = this.goal().trim();
    if (!goal || this.creating()) return;
    this.creating.set(true);
    try {
      const mission = await this.api.createMission(goal, this.budget());
      this.goal.set('');
      await this.refresh();
      await this.openMission(mission.id);
    } catch (err) {
      this.error.set(String(err));
    } finally {
      this.creating.set(false);
    }
  }

  /** Start the supervisor and follow it.
   *
   *  `force` overrides a stop the supervisor made on the person's behalf —
   *  a stall, repeated failures, the session cap — for one session. */
  async run(id: string, force = false): Promise<void> {
    if (this.running()) return;
    this.running.set(id);
    this.notes.set([]);
    this.activity.set('starting…');
    this.feed.live.set(true);
    this.feed.push('session_start', 'mission started');
    try {
      await this.api.streamMission(id, (ev) => this.onEvent(ev), force);
    } catch (err) {
      this.notes.update((n) => [...n, `stopped: ${err}`]);
    } finally {
      this.running.set(null);
      this.activity.set('');
      this.feed.live.set(false);
      await this.refresh();
      if (this.openId() === id) await this.openMission(id);
    }
  }

  start(m: MissionSummary, force = false): void { void this.run(m.id, force); }

  async abort(m: MissionSummary | string): Promise<void> {
    const id = typeof m === 'string' ? m : m.id;
    try {
      await this.api.abortMission(id);
      this.activity.set('stopping after this session…');
    } catch (err) {
      this.error.set(String(err));
    }
  }

  /** The card is itself a button, so the actions inside it have to stop the
   *  click before it opens the mission underneath them. */
  startFrom(ev: Event, m: MissionSummary, force = false): void {
    ev.stopPropagation();
    void this.run(m.id, force);
  }

  /** What the start button should say, given what the supervisor has decided.
   *  A blocked mission that can be overridden says so outright rather than
   *  offering a plain "Continue" the server will refuse. */
  startLabel(m: MissionSummary): string {
    if (m.blocked_reason && m.can_force) return 'Continue anyway';
    return m.sessions.length ? 'Continue' : 'Start';
  }

  /** Whether to offer a start button at all. A finished or overspent mission
   *  cannot be started by pressing harder, so it gets the reason and no
   *  button. */
  canStart(m: MissionSummary): boolean {
    return !m.running && (!m.blocked_reason || m.can_force);
  }

  stopFrom(ev: Event, m: MissionSummary): void {
    ev.stopPropagation();
    void this.abort(m.id);
  }

  async remove(id: string): Promise<void> {
    const mission = this.missions().find((m) => m.id === id);
    const label = (mission?.goal || 'this mission').slice(0, 120);
    if (!(await this.confirm.ask({
      title: 'Delete this mission?',
      subject: label,
      body: 'Its record and session history go. The workspace it built is '
        + 'left on disk exactly as it is. This cannot be undone.',
    }))) return;

    const before = this.missions();
    const wasOpen = this.openId() === id;
    this.missions.update((list) => list.filter((m) => m.id !== id));
    if (wasOpen) this.closeMission();

    try {
      await this.api.deleteMission(id);
      this.notice.ok('Mission deleted.');
      await this.refresh();
    } catch (err) {
      this.missions.set(before);
      this.notice.error(`Could not delete it — ${String(err)}`, {
        label: 'Try again',
        run: () => void this.remove(id),
      });
    }
  }

  private onEvent(ev: CompassEvent): void {
    switch (ev.type) {
      case 'mission_note': {
        // The supervisor's own commentary: which session, why, and where the
        // score stood afterwards. This is the spine of the live view.
        const message = String(ev['message'] ?? '');
        this.notes.update((n) => [...n, message]);
        this.feed.push(this.kindOfNote(message), message);
        void this.refreshQuietly();
        break;
      }
      case 'tool_call_started': {
        const tool = String(ev['tool_name'] ?? 'working');
        this.activity.set(tool);
        this.feed.push('tool_call', this.toolLine(ev, tool));
        break;
      }
      case 'assistant_message':
        // Deliberately not fed: a builder thinks between every tool call, and
        // a feed that says "thinking" forty times an hour is one people stop
        // reading.
        this.activity.set('thinking…');
        break;
      case 'error':
        this.notes.update((n) => [...n, `error: ${ev['message'] ?? ''}`]);
        this.feed.push('error', String(ev['message'] ?? 'error'));
        break;
    }
  }

  /** What colour a supervisor note deserves, read from what it says.
   *
   *  The server sends one `mission_note` for everything, so the distinctions
   *  are recovered here from its wording rather than invented: a note about a
   *  session opening is not the same event as a verdict, and colouring them
   *  alike would make the feed a wall of one colour. */
  private kindOfNote(message: string): ActivityKind {
    const m = message.toLowerCase();
    if (/\bas (builder|reviewer|planner)\b|session \d+ as\b/.test(m)) return 'session_start';
    if (/\baccepted\b|\bpassing\b|\bsigned off\b/.test(m)) return 'feature_accepted';
    if (/\bbudget\b|\bspent\b|\$\d/.test(m)) return 'budget_tick';
    if (/\bstopped\b|\bfailed\b|\brate limit\b|\bended early\b/.test(m)) return 'error';
    return 'mission_verdict';
  }

  /** A tool call as one line: the command where there is one, the tool's name
   *  where there is not. `bash ./init.sh` says more than `bash`. */
  private toolLine(ev: CompassEvent, tool: string): string {
    const args = ev['arguments'] ?? ev['args'] ?? ev['input'];
    let detail = '';
    if (typeof args === 'string') detail = args;
    else if (args && typeof args === 'object') {
      const a = args as Record<string, unknown>;
      detail = String(a['command'] ?? a['path'] ?? a['file_path'] ?? a['query'] ?? '');
    }
    detail = detail.replace(/\s+/g, ' ').trim();
    return detail ? `${tool} ${detail.slice(0, 90)}` : tool;
  }

  /** Update the score without the spinner — this fires between sessions of a
   *  run that is still going, and a flashing "loading" would be noise. */
  private async refreshQuietly(): Promise<void> {
    try {
      this.missions.set(await this.api.missions());
      if (this.openId()) this.detail.set(await this.api.mission(this.openId()!));
    } catch {
      /* the stream is the source of truth while it runs */
    }
  }

  // ── index: headline numbers ─────────────────────────────────────────
  readonly runningCount = computed(() => this.missions().filter((m) => m.running).length);
  readonly totalPassing = computed(() => this.missions().reduce((a, m) => a + m.passing, 0));
  readonly totalSpent = computed(() => this.missions().reduce((a, m) => a + m.spent_usd, 0));
  readonly totalBudget = computed(() => this.missions().reduce((a, m) => a + m.budget_usd, 0));
  readonly totalSessions = computed(() =>
    this.missions().reduce((a, m) => a + m.sessions.length, 0));

  /** Mean of the missions that have a mean — an unscored mission must not
   *  drag the average towards zero by counting as zero. */
  readonly meanScoreLine = computed(() => {
    const scored = this.missions().map((m) => m.mean_score).filter((s): s is number => s != null);
    if (!scored.length) return 'no reviews yet';
    const mean = scored.reduce((a, b) => a + b, 0) / scored.length;
    return `mean score ${mean.toFixed(1)} of 5`;
  });

  /** Cost and wall time per session, from what has actually been run. */
  readonly sessionAverageLine = computed(() => {
    const all = this.missions().flatMap((m) => m.sessions);
    if (!all.length) return 'none run yet';
    const cost = all.reduce((a, s) => a + s.cost_usd, 0) / all.length;
    const secs = all.reduce((a, s) => a + (s.seconds ?? 0), 0) / all.length;
    return secs > 0
      ? `avg ${this.money(cost)} · ${(secs / 60).toFixed(1)} min each`
      : `avg ${this.money(cost)} each`;
  });

  /** What the cap buys, at the rate this install is actually achieving.
   *  Measured rather than assumed: the number that matters is what a feature
   *  has cost here, not a figure from a pricing page. */
  readonly projection = computed(() => {
    const all = this.missions();
    const spent = all.reduce((a, m) => a + m.spent_usd, 0);
    const done = all.reduce((a, m) => a + m.passing, 0);
    const cap = this.budget();
    if (!done || !spent) {
      return `A feature costs roughly $1.40 to build and review, so $${cap} plans for about ${Math.max(1, Math.floor(cap / 1.4))} features.`;
    }
    const per = spent / done;
    const reach = Math.max(1, Math.floor(cap / per));
    return `At this install's average of ${this.money(per)} a feature, $${cap} buys roughly ${reach} feature${reach === 1 ? '' : 's'}.`;
  });

  // ── index: search, filter, paging ───────────────────────────────────
  /** Which of the five index buckets a mission is in. */
  private bucket(m: MissionSummary): 'run' | 'done' | 'cap' | 'stop' {
    if (m.running) return 'run';
    if (m.features && m.passing >= m.features) return 'done';
    if (m.budget_usd && m.spent_usd >= m.budget_usd) return 'cap';
    return 'stop';
  }

  filterCount(key: string): number {
    return key === 'all'
      ? this.missions().length
      : this.missions().filter((m) => this.bucket(m) === key).length;
  }

  readonly filteredRows = computed(() => {
    const q = this.query().trim().toLowerCase();
    const f = this.filter();
    return this.missions().filter((m) => {
      if (f !== 'all' && this.bucket(m) !== f) return false;
      if (!q) return true;
      return (m.goal + ' ' + m.id).toLowerCase().includes(q);
    });
  });

  readonly pageCount = computed(() =>
    Math.max(1, Math.ceil(this.filteredRows().length / this.PER)));

  readonly pageRows = computed(() => {
    const p = Math.min(this.page(), this.pageCount());
    return this.filteredRows().slice((p - 1) * this.PER, p * this.PER);
  });

  readonly narrowed = computed(() =>
    !!this.query().trim() || this.filter() !== 'all');

  readonly showingFrom = computed(() =>
    this.filteredRows().length ? (Math.min(this.page(), this.pageCount()) - 1) * this.PER + 1 : 0);

  readonly showingTo = computed(() =>
    Math.min(Math.min(this.page(), this.pageCount()) * this.PER, this.filteredRows().length));

  /** First, last, and the pages either side of the current one; null is a gap. */
  readonly pageWindow = computed<(number | null)[]>(() => {
    const pages = this.pageCount();
    const here = Math.min(this.page(), pages);
    const out: (number | null)[] = [];
    for (let i = 1; i <= pages; i++) {
      if (i === 1 || i === pages || Math.abs(i - here) <= 1) out.push(i);
      else if (out[out.length - 1] !== null) out.push(null);
    }
    return out;
  });

  goPage(p: number): void {
    this.page.set(Math.max(1, Math.min(p, this.pageCount())));
  }

  countLine(): string {
    const shown = this.filteredRows().length;
    const all = this.missions().length;
    return shown === all ? `${all} total` : `${shown} of ${all}`;
  }

  /** The search term marked inside a field, with the rest escaped. */
  highlight(text: string): string {
    const raw = text ?? '';
    const q = this.query().trim();
    if (!q) return this.escape(raw);
    const i = raw.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0) return this.escape(raw);
    return this.escape(raw.slice(0, i))
      + '<span class="ms-hl">' + this.escape(raw.slice(i, i + q.length)) + '</span>'
      + this.escape(raw.slice(i + q.length));
  }

  private escape(s: string): string {
    return s.replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));
  }

  // ── cards ───────────────────────────────────────────────────────────
  money(n: number): string {
    return `$${(n ?? 0).toFixed(2)}`;
  }

  shortId(m: MissionSummary): string {
    return m.id.slice(0, 8);
  }

  /** The stripe down a card's left edge — the state, without reading a word. */
  accentFor(m: MissionSummary): string {
    return { run: 'var(--sea)', done: 'var(--ok)', cap: 'var(--err)', stop: 'var(--idle)' }[this.bucket(m)];
  }

  statePill(m: MissionSummary): string {
    return { run: 'p-run', done: 'p-ok', cap: 'p-err', stop: 'p-idle' }[this.bucket(m)];
  }

  stateLabel(m: MissionSummary): string {
    if (m.running) return 'running';
    if (!m.planned) return 'not planned yet';
    if (m.features && m.passing >= m.features) return 'complete';
    if (m.budget_usd && m.spent_usd >= m.budget_usd) return 'budget reached';
    return m.sessions.length ? 'paused' : 'ready';
  }

  /** One square per feature. Capped so a 200-feature plan does not turn the
   *  card into a wall — the count beneath it is the precise number. */
  chartCells(m: MissionSummary): MissionFeatureState[] {
    return (m.feature_states ?? []).slice(0, 120);
  }

  budgetPct(m: MissionSummary): number {
    return m.budget_usd ? Math.min(100, (m.spent_usd / m.budget_usd) * 100) : 0;
  }

  budgetColour(m: MissionSummary): string {
    const pct = this.budgetPct(m);
    return pct > 90 ? 'var(--err)' : pct > 70 ? 'var(--warn)' : 'var(--brass)';
  }

  perFeature(m: MissionSummary): number {
    return m.passing ? m.spent_usd / m.passing : 0;
  }

  /** What the money says about whether this plan finishes. */
  budgetLine(m: MissionSummary): string {
    const per = this.perFeature(m);
    if (!per) return '<b>—</b> a feature · nothing accepted yet';
    const reach = Math.floor(m.budget_usd / per);
    const head = `<b>${this.money(per)}</b> a feature · `;
    if (m.running || reach < m.features) {
      return reach >= m.features
        ? head + `this budget covers all ${m.features}`
        : head + `this budget reaches about <b>feature ${reach}</b> of ${m.features}`;
    }
    if (m.passing >= m.features) {
      return head + `finished <b>${this.money(m.budget_usd - m.spent_usd)}</b> under cap`;
    }
    return head + `${m.features - m.passing} unbuilt`;
  }

  /** The last thing that happened, from the session record — Compass has no
   *  separate event log, and the last session is what there is to say. */
  ticker(m: MissionSummary): { event: string; text: string } | null {
    const last = m.sessions[m.sessions.length - 1];
    if (!last) return null;
    if (last.stopped) {
      return { event: 'session_ended', text: `session ${last.session} (${last.persona}): ${last.stopped}` };
    }
    const summary = (last.summary || '').replace(/\s+/g, ' ').trim();
    return {
      event: 'session_done',
      text: `session ${last.session} (${last.persona}): ${summary.slice(0, 120) || `${last.passing} of ${last.total} passing`}`,
    };
  }

  // ── detail ──────────────────────────────────────────────────────────
  progressPct(d: MissionDetail): number {
    return d.features ? (d.passing / d.features) * 100 : 0;
  }

  ringOffset(pct: number): number {
    return this.RING - (this.RING * Math.max(0, Math.min(100, pct))) / 100;
  }

  countOf(d: MissionDetail, key: string): number {
    const list = d.feature_list ?? [];
    return key === 'all' ? list.length : list.filter((f) => f.state === key).length;
  }

  shownFeatures(d: MissionDetail): MissionFeature[] {
    const f = this.featureFilter();
    const list = d.feature_list ?? [];
    return f === 'all' ? list : list.filter((x) => x.state === f);
  }

  selectedFeature(d: MissionDetail): MissionFeature | null {
    const id = this.selected();
    return id ? (d.feature_list ?? []).find((f) => f.id === id) ?? null : null;
  }

  sessionsPerFeature(d: MissionDetail): string {
    return (d.sessions.length / Math.max(1, d.passing)).toFixed(1);
  }

  /** Where the money runs out, when that lands short of the plan. */
  reachLine(d: MissionDetail): string {
    const per = this.perFeature(d);
    const reach = per ? Math.floor(d.budget_usd / per) : 0;
    if (per && reach < d.features) return `budget reaches ~feature ${reach}`;
    return d.mean_score != null ? 'across accepted features' : 'nothing reviewed yet';
  }

  glyph(state: MissionFeatureState): string {
    return { ok: '✓', rev: '◍', run: '▸', blk: '!', q: '' }[state] ?? '';
  }

  scoreList(f: MissionFeature): number[] {
    return Object.values(f.scores ?? {}) as number[];
  }

  scoreEntries(f: MissionFeature): { name: string; value: number }[] {
    return Object.entries(f.scores ?? {}).map(([name, value]) => ({ name, value: Number(value) }));
  }

  meanOf(f: MissionFeature): string {
    const vals = this.scoreList(f);
    if (!vals.length) return '';
    return (vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(1);
  }

  scoreColour(v: number): string {
    return v >= 4 ? 'var(--ok)' : v >= 3 ? 'var(--warn)' : 'var(--err)';
  }

  verdictPill(f: MissionFeature): string {
    return { ok: 'p-ok', rev: 'p-warn', run: 'p-run', blk: 'p-err', q: 'p-idle' }[f.state];
  }

  verdictLabel(f: MissionFeature): string {
    return { ok: 'Accepted', rev: 'In review', run: 'Building', blk: 'Returned', q: 'Queued' }[f.state];
  }

  verdictProse(f: MissionFeature): string {
    switch (f.state) {
      case 'ok': return 'This feature met its contract and was signed off by a reviewer session.';
      case 'rev': return 'A builder has claimed this and written down how it checked it. A reviewer session has not ruled yet.';
      case 'run': return 'A builder session is working on this now. The review appears once it claims it.';
      case 'blk': return 'The contract was not met. The next builder session picks this up with the review below as its context.';
      default: return 'Queued. A session will claim this once the features above it are settled.';
    }
  }

  // ── timeline ────────────────────────────────────────────────────────
  timelineMeta(d: MissionDetail): string {
    if (!d.sessions.length) return 'no sessions yet';
    const personas = new Set(d.sessions.map((s) => s.persona));
    return `${d.sessions.length} sessions · ${[...personas].join(' and ')}`;
  }

  selectedSession(d: MissionDetail): MissionSession | null {
    const n = this.openSession();
    return n == null ? null : d.sessions.find((s) => s.session === n) ?? null;
  }

  sessionDot(s: MissionSession): string {
    if (s.stopped) return 'fail';
    return { planner: 'plan', builder: 'build', reviewer: 'rev' }[s.persona] ?? 'build';
  }

  shortPersona(p: string): string {
    return { planner: 'plan', builder: 'build', reviewer: 'review' }[p] ?? p;
  }

  personaPill(s: MissionSession): string {
    return { planner: 'p-warn', builder: 'p-run', reviewer: 'p-vio' }[s.persona] ?? 'p-idle';
  }

  sessionProse(s: MissionSession): string {
    const summary = (s.summary || '').replace(/\s+/g, ' ').trim();
    const head = 'Started with an empty context, read the plan and the last session\'s notes, then ';
    if (summary) return head + summary.slice(0, 400);
    return head + `ran ${s.turns} turns and left the score at ${s.passing} of ${s.total}.`;
  }

  // ── shared formatting ───────────────────────────────────────────────
  /** A session's wall time, in the coarsest unit that still says something. */
  mins(seconds: number): string {
    if (seconds < 90) return `${Math.round(seconds)}s`;
    return `${(seconds / 60).toFixed(1)}m`;
  }

  tokens(n: number): string {
    return n >= 1000 ? `${Math.round(n / 1000)}k tok` : `${n} tok`;
  }

  /** Whether the deployment's quota, rather than the work, set the pace.
   *  Below 60% of quota a session was thinking; at or above it, it spent the
   *  minute waiting for one. */
  quotaBound(s: MissionSession, quota: number | null | undefined): boolean {
    if (!quota || !s.tokens_per_minute || (s.seconds ?? 0) < 60) return false;
    return s.tokens_per_minute >= quota * 0.6;
  }

  tokenNote(s: MissionSession, quota: number | null | undefined): string {
    // Per *call*, not per turn: a session that dies mid-turn has made dozens
    // of calls and completed no turns.
    const each = s.tokens_per_call || (s.turns ? Math.round(s.prompt_tokens / s.turns) : 0);
    const base = `${s.prompt_tokens.toLocaleString()} prompt tokens over `
      + `${s.calls || s.turns} calls (~${each.toLocaleString()} per call)`
      + (s.cached_tokens
          ? `, ${Math.round((100 * s.cached_tokens) / s.prompt_tokens)}% cached`
          : '');
    if (!this.quotaBound(s, quota)) return base;
    const calls = each ? (quota! / each).toFixed(1) : '?';
    return `${base}.\nQuota-bound: drew ~${Math.round(s.tokens_per_minute)}`
      + ` tokens/min against a ${quota!.toLocaleString()}/min deployment,`
      + ` so only ~${calls} model calls per minute were possible.`;
  }

  folder(path: string): string {
    return path.split('/').filter(Boolean).pop() ?? path;
  }
}
