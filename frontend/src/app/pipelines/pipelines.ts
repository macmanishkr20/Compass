import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { TurnNotifyService } from '../turn-notify.service';
import { CompassApiService } from '../compass-api.service';
import {
  ConnectionKind,
  CredentialTypeInfo,
  NodeTypeInfo,
  PipelineConnection,
  PipelineEdge,
  PipelineExport,
  PipelineNode,
  PipelineNodeRun,
  PipelineProblem,
  PipelineRun,
  PipelineSummary,
} from '../models';
import { NODE_H, NODE_W, PipelineCanvas, Side } from './canvas';

/** Builder tools that change the graph, so the canvas is worth re-reading
 *  the moment one finishes. The read-only ones would only cost a round trip. */
const MUTATING = new Set([
  'pipeline_add_node',
  'pipeline_connect',
  'pipeline_set_config',
  'pipeline_remove_node',
]);
import { PipelineInspector } from './inspector';
import { Markdown } from '../markdown/markdown';
import { EstimateApi } from '../estimate/estimate-api';
import { EstimateSummary } from '../estimate/models';

/**
 * The Pipelines section: list, palette, canvas, properties, runs.
 *
 * State lives here and the canvas is told what to draw, rather than the canvas
 * owning the graph. That is what lets a run update node colours without the
 * canvas knowing runs exist, and it keeps one place responsible for the thing
 * that has to be right — the saved graph.
 *
 * Saving is explicit. A canvas that autosaves every drag writes a new version
 * on every pixel, and versions are what runs pin themselves to; a run's
 * history would then describe a pipeline nobody deliberately created.
 */
@Component({
  selector: 'app-pipelines',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './pipelines.html',
  // The capability explainer closes when you look away from it, like every
  // other popover in the product.
  host: { '(document:click)': 'capsOpen.set(false)' },
  styleUrl: './pipelines.css',
  imports: [PipelineCanvas, PipelineInspector, Markdown, JsonPipe],
})
export class Pipelines {
  private readonly api = inject(CompassApiService);
  private readonly turnNotify = inject(TurnNotifyService);
  /** The Estimate module's own service, injected rather than duplicated. The
   *  dependency points one way and only from here: Estimate does not know
   *  Pipelines exists. */
  private readonly estimateApi = inject(EstimateApi);

  readonly loading = signal(false);
  readonly busy = signal('');
  readonly error = signal('');
  readonly pipelines = signal<PipelineSummary[]>([]);
  readonly nodeTypes = signal<NodeTypeInfo[]>([]);
  readonly connections = signal<PipelineConnection[]>([]);
  /** The kinds the built-in connectors expect. Offered in the
   *  Connections panel so the string matches without guessing. */
  readonly connectionKinds = signal<ConnectionKind[]>([]);

  // -- the index, as a chart ---------------------------------------------
  //
  // The mockup's health strip wants healthy/failing counts and a sparkline of
  // recent runs. Compass does not hold that on the list: runs live behind
  // `/v1/pipelines/{id}/runs`, so filling those cards honestly would mean one
  // request per pipeline on every visit to this page. The strip therefore
  // counts what the list actually knows — which is enough to answer the
  // question it is really asking, "what needs me?" — rather than inventing a
  // health it would have to guess at.

  /** Which card or chip is filtering the list. '' is everything. */
  readonly filter = signal<'' | 'ready' | 'setup' | 'never' | 'paused'>('');
  readonly search = signal('');
  /** Cards or rows. Both show the same pipelines. */
  readonly gridView = signal(true);

  /** The connection kinds a pipeline's nodes ask for that nothing satisfies.
   *  Generalised from `neededSetup`, which answered this for the open one
   *  only — the index needs it for all of them, and every input is already
   *  in the browser. */
  private missingFor(pipeline: PipelineSummary): string[] {
    const types = this.typeMap();
    const have = new Set(this.connections().map((c) => c.kind));
    const want = new Set<string>();
    for (const node of pipeline.nodes) {
      const kind = types.get(node.type)?.connection_kind;
      if (kind && !have.has(kind)) want.add(kind);
    }
    return [...want];
  }

  /** One pipeline's standing, in a word. Ordered by what a person would want
   *  to see first: something switched off is paused whatever else is true of
   *  it, and something missing a credential cannot run whether or not it once
   *  did. */
  status(pipeline: PipelineSummary): 'paused' | 'setup' | 'never' | 'ready' {
    if (!pipeline.enabled) return 'paused';
    if (this.missingFor(pipeline).length) return 'setup';
    if (!pipeline.proven_at) return 'never';
    return 'ready';
  }

  statusLabel(pipeline: PipelineSummary): string {
    return {
      paused: 'Paused',
      setup: 'Needs setup',
      never: 'Not yet run',
      ready: 'Ready',
    }[this.status(pipeline)];
  }

  /** What each card counts, and the word under it. */
  readonly health = computed(() => {
    const all = this.pipelines();
    const by = (want: string) => all.filter((p) => this.status(p) === want).length;
    const kinds = new Set(this.connections().map((c) => c.kind));
    return [
      { key: '' as const, dot: 'brass', label: 'All pipelines', value: all.length,
        note: `across ${kinds.size} connection${kinds.size === 1 ? '' : 's'}` },
      { key: 'ready' as const, dot: 'ok', label: 'Ready', value: by('ready'),
        note: 'proved by a manual run' },
      { key: 'setup' as const, dot: 'bad', label: 'Needs setup', value: by('setup'),
        note: 'missing a connection' },
      { key: 'never' as const, dot: 'warn', label: 'Not yet run', value: by('never'),
        note: 'cannot be scheduled' },
      { key: 'paused' as const, dot: 'off', label: 'Paused', value: by('paused'),
        note: 'switched off' },
    ];
  });

  /** The list after the chips and the search box. */
  readonly shown = computed(() => {
    const want = this.filter();
    const term = this.search().trim().toLowerCase();
    return this.pipelines().filter((p) => {
      if (want && this.status(p) !== want) return false;
      if (term && !p.name.toLowerCase().includes(term)) return false;
      return true;
    });
  });

  /** How this pipeline starts, said in words rather than in trigger objects. */
  triggerLine(pipeline: PipelineSummary): string {
    const types = this.typeMap();
    const trigger = pipeline.nodes.find(
      (n) => types.get(n.type)?.id === 'gmail.trigger');
    if (trigger) return 'On new mail matching a search';
    const every = (pipeline.triggers ?? []).find(
      (t) => Number((t as Record<string, unknown>)['every_minutes']) > 0);
    if (every) {
      const minutes = Number((every as Record<string, unknown>)['every_minutes']);
      return `Every ${minutes} minute${minutes === 1 ? '' : 's'}`;
    }
    return 'Run by hand';
  }

  /** "12m ago". Same wording as the Design index, so the two lists read the
   *  same way rather than each inventing a house style for time. */
  age(epochSeconds: number): string {
    const secs = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (secs < 60) return 'just now';
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(epochSeconds * 1000).toLocaleDateString();
  }

  pickFilter(key: '' | 'ready' | 'setup' | 'never' | 'paused'): void {
    this.filter.set(this.filter() === key ? '' : key);
  }

  readonly open = signal<PipelineSummary | null>(null);
  readonly selectedId = signal('');
  readonly dirty = signal(false);
  readonly problems = signal<PipelineProblem[]>([]);

  /** Problems with the node named as it is on the canvas.
   *
   *  The server reports the node id, which is right for an API and useless to
   *  a reader: "n_1_786 needs a gmail connection" means finding which box
   *  that is before you can act on it. */
  readonly namedProblems = computed(() => {
    const names = new Map(
      this.open()?.nodes.map((n) => [n.id, n.name || n.type]) ?? [],
    );
    return this.problems().map((p) => ({
      ...p,
      label: names.get(p.node) || p.node || 'This pipeline',
    }));
  });
  readonly run = signal<PipelineRun | null>(null);
  readonly newName = signal('');
  readonly paletteFilter = signal('');

  readonly typeMap = computed(
    () => new Map(this.nodeTypes().map((t) => [t.id, t])),
  );

  readonly selectedNode = computed<PipelineNode | null>(
    () => this.open()?.nodes.find((n) => n.id === this.selectedId()) ?? null,
  );

  readonly selectedType = computed<NodeTypeInfo | undefined>(() => {
    const node = this.selectedNode();
    return node ? this.typeMap().get(node.type) : undefined;
  });

  /** node id -> status, so the canvas can colour a graph mid-run. */
  readonly statuses = computed<Record<string, string>>(() => {
    const current = this.run();
    if (!current) return {};
    const out: Record<string, string> = {};
    for (const [id, nodeRun] of Object.entries(current.nodes)) {
      out[id] = nodeRun.status;
    }
    return out;
  });

  /** The per-item line a loop body node carries after a run. */
  readonly notes = computed<Record<string, string>>(() => {
    const current = this.run();
    const body = this.loopBody();
    if (!current) return {};
    const out: Record<string, string> = {};
    for (const [id, nodeRun] of Object.entries(current.nodes)) {
      if (body.has(id) && nodeRun.text) out[id] = nodeRun.text;
    }
    return out;
  });

  /**
   * Every node inside some loop's body, by the same rule the engine uses:
   * reachable from a loop's `each` port, minus anything reachable from its
   * `out` port.
   *
   * Computed here rather than fetched because the canvas has to show it while
   * someone is still drawing — before any save, and with no run to ask about.
   * The duplication is real and deliberate; the alternative is a round trip
   * on every edge drawn, which would make the marker lag the wire that caused
   * it. The engine remains the authority: this only decides what is drawn.
   */
  readonly loopBody = computed<Set<string>>(() => {
    const pipeline = this.open();
    if (!pipeline) return new Set<string>();
    const types = this.typeMap();
    const out = new Set<string>();

    const reach = (loopId: string, ports: Set<string>): Set<string> => {
      const seen = new Set<string>();
      const stack = pipeline.edges
        .filter((e) => e.source === loopId && ports.has(e.port))
        .map((e) => e.target);
      while (stack.length) {
        const id = stack.pop()!;
        if (id === loopId || seen.has(id)) continue;
        seen.add(id);
        for (const e of pipeline.edges) if (e.source === id) stack.push(e.target);
      }
      return seen;
    };

    for (const node of pipeline.nodes) {
      const type = types.get(node.type);
      if (!type?.outputs?.some((p) => p.name === 'each')) continue;
      const after = reach(node.id, new Set(['out']));
      for (const id of reach(node.id, new Set(['each']))) {
        if (!after.has(id)) out.add(id);
      }
    }
    return out;
  });

  /** Iterations of the loop currently selected, for the run panel. */
  readonly selectedIterations = computed(() => {
    const current = this.run();
    const id = this.selectedId();
    if (!current || !id) return [];
    const iterations = current.iterations?.[id];
    if (!iterations) return [];
    return iterations.map((states, index) => ({
      index,
      nodes: Object.values(states),
    }));
  });

  // -- the run log ----------------------------------------------------------
  //
  // Docked under the canvas rather than in the side panel, because it answers
  // a different question. The side panel is about the node you are editing;
  // the log is about the run that just happened, and you read it while
  // looking at the graph rather than instead of it.

  readonly logsOpen = signal(true);

  /** Which pane of the drawer is showing. All three are real: Logs is this
   *  run's steps, Run history is the runs endpoint, and Data is the selected
   *  step's payload — the same payload the log detail shows, given a place
   *  of its own because that is what people scroll the drawer looking for. */
  readonly drawerTab = signal<'logs' | 'runs' | 'data'>('logs');

  /** The capability explainer in the bar. */
  readonly capsOpen = signal(false);

  /** Whether the builder pane is showing.
   *
   * Remembered, because collapsing it is a statement about how you work
   * rather than about this pipeline — someone who does not use the builder
   * should not have to shut it on every visit. */
  readonly builderOpen = signal(this.rememberedBuilder());

  setBuilderOpen(open: boolean): void {
    this.builderOpen.set(open);
    try {
      localStorage.setItem('compass.pipelines.builder', open ? '1' : '0');
    } catch {
      // A browser refusing storage is not a reason to refuse the toggle.
    }
  }

  private rememberedBuilder(): boolean {
    try {
      return localStorage.getItem('compass.pipelines.builder') !== '0';
    } catch {
      return true;
    }
  }

  /** Openings for the builder's empty state. Three, and each names a
   *  different shape of instruction — a whole pipeline, a condition, a
   *  failure policy — so they read as a range rather than a list. */
  readonly builderExamples = [
    'Read new invoices and record each one in the ledger table',
    'Only trigger on mail from outside the company',
    'Retry the SQL write three times, then alert me',
  ];

  /** What each capability permits, and which steps asked for it. Read off the
   *  graph rather than written down, so it cannot fall out of date with the
   *  node types — and so the answer names the step, which is the thing you
   *  need in order to decide whether to grant it. */
  readonly capabilityNotes = computed(() => {
    const what: Record<string, string> = {
      shell: 'Run commands on the runner',
      write: 'Write files in the workspace',
      network: 'Reach external services',
      agent: 'Call a model to decide steps',
    };
    const pipeline = this.open();
    const types = this.typeMap();
    return Object.entries(what).map(([name, text]) => ({
      name,
      what: text,
      wantedBy: (pipeline?.nodes ?? [])
        .filter((n) => types.get(n.type)?.requires === name)
        .map((n) => n.name || types.get(n.type)?.label || n.type),
    }));
  });
  readonly runHistory = signal<PipelineRun[]>([]);
  readonly runHistoryLoading = signal(false);

  /** The line on the right of the drawer's header: what there is to see. */
  readonly drawerMeta = computed(() => {
    const current = this.run();
    if (current) {
      const ms = this.runMs();
      return `${current.status}${ms === null ? '' : ` · ${ms}ms`}`
        + `${current.mode === 'mock' ? ' · dry run' : ''}`;
    }
    const history = this.runHistory();
    if (history.length) return `${history.length} run(s) recorded`;
    return 'no runs yet';
  });

  /** Granted, and asked for by a step that is on the canvas. Turning it off
   *  would stop that step, so the chip is marked rather than merely filled —
   *  it is a fact about the graph, not a second switch. */
  capabilityLocked(name: string): boolean {
    if (!this.hasCapability(name)) return false;
    return this.capabilityNotes().some(
      (c) => c.name === name && c.wantedBy.length > 0);
  }

  async pickDrawer(tab: 'logs' | 'runs' | 'data'): Promise<void> {
    this.drawerTab.set(tab);
    this.logsOpen.set(true);
    // Fetched when the tab is opened rather than with the pipeline: a run
    // list nobody looks at is a request nobody asked for.
    if (tab === 'runs' && !this.runHistory().length) await this.loadRunHistory();
  }

  async loadRunHistory(): Promise<void> {
    const pipeline = this.open();
    if (!pipeline) return;
    this.runHistoryLoading.set(true);
    try {
      const res = await this.api.pipelineRuns(pipeline.id);
      this.runHistory.set(res.runs ?? []);
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.runHistoryLoading.set(false);
    }
  }

  /** Whether a step left anything worth printing. An empty object is not a
   *  payload, and showing one as if it were makes the pane look broken. */
  hasPayload(nodeRun: PipelineNodeRun): boolean {
    const out = nodeRun.output;
    return !!out && Object.keys(out).length > 0;
  }

  /** How long a finished run took, for the history rows. */
  runLength(r: PipelineRun): string {
    if (!r.finished_at || !r.started_at) return '—';
    const ms = Math.round((r.finished_at - r.started_at) * 1000);
    return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  }
  readonly logNodeId = signal('');

  /** Node runs in the order they happened, which is what a log is.
   *
   *  Sorted by when each started rather than by graph position: a fan-out and
   *  a branch both make graph order a lie about what actually ran when. Nodes
   *  that never started are dropped — "pending" after a run has finished means
   *  unreachable, and the canvas already says so. */
  readonly logRows = computed(() => {
    const current = this.run();
    if (!current) return [];
    const names = new Map(
      this.open()?.nodes.map((n) => [n.id, n.name || n.type]) ?? [],
    );
    const body = this.loopBody();
    return Object.values(current.nodes)
      .filter((n) => n.status !== 'pending')
      .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0))
      .map((n) => ({
        run: n,
        label: names.get(n.node_id) ?? n.node_id,
        looped: body.has(n.node_id),
        ms:
          n.started_at && n.finished_at
            ? Math.max(0, Math.round((n.finished_at - n.started_at) * 1000))
            : null,
      }));
  });

  /** Defaults to the first failure, then the first row: opening the log after
   *  something went wrong should land on what went wrong. */
  readonly logSelected = computed(() => {
    const rows = this.logRows();
    if (!rows.length) return null;
    const chosen = rows.find((r) => r.run.node_id === this.logNodeId());
    return chosen ?? rows.find((r) => r.run.status === 'failed') ?? rows[0];
  });

  /**
   * A loop body node's per-item states, for the log panes.
   *
   * Its entry in `run.nodes` is only a rollup — status and a "2/3 item(s)"
   * line — because the canvas draws one box per node however many times it
   * ran. The real inputs and outputs are one per item, and without this the
   * pane would say "nothing recorded" about a node that ran three times with
   * three different inputs, which is worse than saying nothing at all.
   */
  readonly logIterations = computed(() => {
    const current = this.run();
    const selected = this.logSelected();
    if (!current || !selected || !selected.looped) return [];
    const nodeId = selected.run.node_id;
    for (const states of Object.values(current.iterations ?? {})) {
      if (!states.length || !(nodeId in states[0])) continue;
      return states.map((s, index) => ({ index, run: s[nodeId] }));
    }
    return [];
  });

  readonly runMs = computed(() => {
    const current = this.run();
    if (!current?.finished_at) return null;
    return Math.max(0, Math.round((current.finished_at - current.started_at) * 1000));
  });

  /** Pretty-printed, because a run's payload is read, not parsed. */
  asJson(value: unknown): string {
    if (value === undefined || value === null) return '';
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  isEmpty(value: Record<string, unknown> | undefined): boolean {
    return !value || Object.keys(value).length === 0;
  }

  clearRun(): void {
    this.run.set(null);
    this.logNodeId.set('');
  }

  readonly palette = computed(() => {
    const term = this.paletteFilter().trim().toLowerCase();
    const order = ['flow', 'connector', 'mcp', 'intelligence', 'code', 'module', 'tool', 'io'];
    const groups = new Map<string, NodeTypeInfo[]>();
    for (const type of this.nodeTypes()) {
      if (term && !`${type.label} ${type.id} ${type.description}`.toLowerCase().includes(term)) {
        continue;
      }
      const list = groups.get(type.category) ?? [];
      list.push(type);
      groups.set(type.category, list);
    }
    return [...groups.entries()]
      .sort((a, b) => {
        const ai = order.indexOf(a[0]);
        const bi = order.indexOf(b[0]);
        return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
      })
      .map(([name, types]) => ({
        name,
        types: types.sort((a, b) => a.label.localeCompare(b.label)),
      }));
  });

  constructor() {
    void this.refresh();
    void this.loadEstimateAvailability();
  }

  // -- loading --------------------------------------------------------------

  async refresh(): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    try {
      const [list, cat, conns] = await Promise.all([
        this.api.pipelines(),
        this.api.pipelineNodeTypes(),
        this.api.pipelineConnections(),
      ]);
      this.pipelines.set(list.pipelines);
      this.nodeTypes.set(cat.node_types);
      this.connectionKinds.set(cat.connection_kinds ?? []);
      this.connections.set(conns.connections);
      // The credential shapes, so the Connections form is the one the
      // server declares rather than a guess kept in the browser.
      this.api.connectionTypes()
        .then((r) => this.credentialTypes.set(r.types))
        .catch(() => this.credentialTypes.set([]));
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.loading.set(false);
    }
  }

  async openPipeline(summary: PipelineSummary): Promise<void> {
    try {
      const full = await this.api.getPipeline(summary.id);
      this.open.set(full);
      this.selectedId.set('');
      this.run.set(null);
      this.problems.set([]);
      this.dirty.set(false);
      await this.loadChat(full.id);
    } catch (err: unknown) {
      this.error.set(this.message(err));
    }
  }

  close(): void {
    this.open.set(null);
    this.run.set(null);
    void this.refresh();
  }

  async create(): Promise<void> {
    const name = this.newName().trim();
    if (!name) return;
    try {
      const made = await this.api.createPipeline(name);
      this.newName.set('');
      this.pipelines.update((all) => [made, ...all]);
      await this.openPipeline(made);
    } catch (err: unknown) {
      this.error.set(this.message(err));
    }
  }

  // -- editing --------------------------------------------------------------

  /** Adds a node just right of the rightmost one, so a new step lands where
   *  the eye already is rather than on top of an existing node. */
  addNode(type: NodeTypeInfo,
          after?: { source: string; port: string; side?: Side }): void {
    const pipeline = this.open();
    if (!pipeline) return;
    // A step added after a particular node belongs beside that node, not at
    // the far right of the whole graph — otherwise following a branch throws
    // the new node past everything else and the wire crosses the canvas.
    const source = after && pipeline.nodes.find((n) => n.id === after.source);
    const right = pipeline.nodes.reduce((max, n) => Math.max(max, n.position.x), -1);

    // Added from a side handle, the new step lands on that side — which is
    // the whole point of the handle being there. Anywhere it would land on
    // top of something, it steps along until it is clear: two steps hung off
    // the same edge should sit beside each other, not on each other.
    let spot: { x: number; y: number };
    if (source && after?.side) {
      // Off a side handle the step lands on that side, because that is the
      // only thing the handle promised. Where the slot is taken it slides
      // along the edge until it is clear — several steps may hang off one
      // side, and they should sit beside each other rather than on top.
      const gap = { x: NODE_W + 70, y: NODE_H + 60 };
      spot = { x: source.position.x, y: source.position.y };
      if (after.side === 'left') spot.x -= gap.x;
      if (after.side === 'right') spot.x += gap.x;
      if (after.side === 'top') spot.y -= gap.y;
      if (after.side === 'bottom') spot.y += gap.y;
      // Slide across the edge, not along it: nodes hung below spread sideways.
      const step = after.side === 'left' || after.side === 'right'
        ? { x: 0, y: NODE_H + 26 }
        : { x: NODE_W + 26, y: 0 };
      const taken = (at: { x: number; y: number }) => pipeline.nodes.some(
        (n) => Math.abs(n.position.x - at.x) < NODE_W
          && Math.abs(n.position.y - at.y) < NODE_H);
      for (let guard = 0; guard < 40 && taken(spot); guard++) {
        spot = { x: spot.x + step.x, y: spot.y + step.y };
      }
    } else {
      const column = source
        ? source.position.x + NODE_W + 70
        : right < 0
          ? 40
          : right + NODE_W + 70;
      const stacked = pipeline.nodes.filter((n) => n.position.x === column).length;
      spot = {
        x: column,
        y: source && !stacked
          ? source.position.y
          : 40 + stacked * (NODE_H + 26),
      };
    }

    const node: PipelineNode = {
      id: `n_${Math.random().toString(36).slice(2, 9)}`,
      type: type.id,
      name: type.label,
      description: '',
      config: {},
      connection_id: '',
      position: spot,
      timeout_s: 43200,
      retries: 0,
      retry_interval_s: 30,
      secure_input: false,
      secure_output: false,
      mock: null,
      state: 'active',
      mark_as: 'success',
    };
    this.mutate((p) => ({ ...p, nodes: [...p.nodes, node] }));
    this.selectedId.set(node.id);
  }

  moveNode(move: { id: string; x: number; y: number }): void {
    this.mutate((p) => ({
      ...p,
      nodes: p.nodes.map((n) =>
        n.id === move.id ? { ...n, position: { x: move.x, y: move.y } } : n,
      ),
    }));
  }

  /** Applies an inspector edit. `config` is merged rather than replaced,
   *  because the inspector sends only the key that changed — see the note
   *  there on why rebuilding the whole object loses edits. */
  patchNode(patch: Partial<PipelineNode>): void {
    const id = this.selectedId();
    this.mutate((p) => ({
      ...p,
      nodes: p.nodes.map((n) =>
        n.id === id
          ? { ...n, ...patch, config: { ...n.config, ...(patch.config ?? {}) } }
          : n,
      ),
    }));
  }

  deleteNode(id: string): void {
    this.mutate((p) => ({
      ...p,
      nodes: p.nodes.filter((n) => n.id !== id),
      // Edges to a node that no longer exists would validate as broken, so
      // they go with it rather than being left for the validator to report.
      edges: p.edges.filter((e) => e.source !== id && e.target !== id),
    }));
    this.selectedId.set('');
  }

  connect(link: { source: string; port: string; target: string }): void {
    this.mutate((p) => {
      const exists = p.edges.some(
        (e) => e.source === link.source && e.target === link.target && e.port === link.port,
      );
      if (exists) return p;
      const edge: PipelineEdge = {
        source: link.source,
        target: link.target,
        when: 'success',
        port: link.port,
      };
      return { ...p, edges: [...p.edges, edge] };
    });
  }

  removeEdge(edge: PipelineEdge): void {
    this.mutate((p) => ({
      ...p,
      edges: p.edges.filter(
        (e) =>
          !(e.source === edge.source && e.target === edge.target &&
            e.port === edge.port && e.when === edge.when),
      ),
    }));
  }

  cycleEdge(edge: PipelineEdge): void {
    const order: PipelineEdge['when'][] = ['success', 'failure', 'completion', 'skip'];
    const next = order[(order.indexOf(edge.when) + 1) % order.length];
    this.mutate((p) => ({
      ...p,
      edges: p.edges.map((e) =>
        e.source === edge.source && e.target === edge.target && e.port === edge.port
          ? { ...e, when: next }
          : e,
      ),
    }));
  }

  toggleCapability(name: string): void {
    this.mutate((p) => ({
      ...p,
      capabilities: p.capabilities.includes(name)
        ? p.capabilities.filter((c) => c !== name)
        : [...p.capabilities, name],
    }));
  }

  hasCapability(name: string): boolean {
    return !!this.open()?.capabilities.includes(name);
  }

  private mutate(fn: (p: PipelineSummary) => PipelineSummary): void {
    const current = this.open();
    if (!current) return;
    this.open.set(fn(current));
    this.dirty.set(true);
  }

  // -- persisting and running -----------------------------------------------

  // -- the estimate this pipeline was costed against ------------------------
  //
  // A one-way link: Pipelines knows an estimate id, Estimate knows nothing
  // about pipelines. The server stores the string without resolving it,
  // because the two modules switch independently and a pipeline that would
  // not load on a box with costing switched off is a worse bug than a link
  // that resolves to nothing. Everything below is gated on the health flag,
  // so with Estimate off this is not merely inert — it is not rendered.

  /** Whether the Estimate module exists on this server. Read from the health
   *  endpoint, the same source the nav uses, rather than assumed — with the
   *  flag off there are no costing routes and this whole affordance must not
   *  be drawn. */
  readonly estimateEnabled = signal(false);
  readonly estimateOptions = signal<EstimateSummary[]>([]);
  readonly estimatePicking = signal(false);

  readonly linkedEstimate = computed(() => {
    const id = this.open()?.estimate_id;
    if (!id) return null;
    return this.estimateOptions().find((e) => e.id === id) ?? null;
  });

  private async loadEstimateAvailability(): Promise<void> {
    try {
      this.estimateEnabled.set(!!(await this.api.health()).estimate);
    } catch {
      this.estimateEnabled.set(false);
    }
  }

  async openEstimatePicker(): Promise<void> {
    this.estimatePicking.set(true);
    try {
      const { estimates } = await this.estimateApi.list();
      this.estimateOptions.set(estimates);
    } catch {
      // A costing module that cannot answer is not a pipeline error. The
      // picker simply has nothing in it and says so.
      this.estimateOptions.set([]);
    }
  }

  /** Formatted here rather than through a pipe: the section imports no pipes
   *  today, and adding CommonModule for one number is a poor trade. */
  estimateMoney(e: EstimateSummary): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency', currency: e.currency || 'USD', maximumFractionDigits: 0,
    }).format(e.total_expected);
  }

  linkEstimate(id: string): void {
    this.mutate((p) => ({ ...p, estimate_id: id }));
    this.estimatePicking.set(false);
    void this.save();
  }

  async save(): Promise<void> {
    const pipeline = this.open();
    if (!pipeline) return;
    this.busy.set('Saving…');
    try {
      const saved = await this.api.savePipeline(pipeline.id, {
        name: pipeline.name,
        nodes: pipeline.nodes,
        edges: pipeline.edges,
        capabilities: pipeline.capabilities,
        estimate_id: pipeline.estimate_id ?? '',
      });
      this.open.set(saved);
      this.dirty.set(false);
      await this.validate();
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.busy.set('');
    }
  }

  async validate(): Promise<void> {
    const pipeline = this.open();
    if (!pipeline) return;
    try {
      const result = await this.api.validatePipeline(pipeline.id);
      this.problems.set(result.problems);
    } catch (err: unknown) {
      this.error.set(this.message(err));
    }
  }

  /** A dry run calls nothing: every step outside the flow nodes returns
   *  pinned or stubbed data. It is how a pipeline is shown working before
   *  anyone connects an account, and it needs no capabilities granted. */
  /** Jump to the node a problem is about. */
  showProblemNode(nodeId: string): void {
    if (this.open()?.nodes.some((n) => n.id === nodeId)) {
      this.selectedId.set(nodeId);
    }
  }

  async runNow(mode: 'live' | 'mock' = 'live'): Promise<void> {
    const pipeline = this.open();
    if (!pipeline) return;
    if (this.dirty()) await this.save();
    this.busy.set(mode === 'mock' ? 'Dry running…' : 'Running…');
    this.error.set('');
    this.turnNotify.arm();
    try {
      this.run.set(await this.api.runPipeline(pipeline.id, {}, mode));
      this.logsOpen.set(true);
      this.logNodeId.set('');
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.busy.set('');
      const status = this.run()?.status ?? '';
      this.turnNotify.finished(
        'pipelines',
        `${pipeline.name} — ${status || 'run finished'}`,
        status !== 'failed' && !this.error(),
      );
    }
  }

  /** Answers a waiting approval and continues the run. */
  async answer(choice: string): Promise<void> {
    const current = this.run();
    if (!current) return;
    this.busy.set('Continuing…');
    try {
      this.run.set(await this.api.resumePipelineRun(current.id, { choice }));
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.busy.set('');
    }
  }

  /** The question a waiting run is parked on, for the banner. */
  readonly waitingQuestion = computed<string>(() => {
    const current = this.run();
    if (!current || current.status !== 'waiting') return '';
    for (const nodeRun of Object.values(current.nodes)) {
      if (nodeRun.status === 'waiting') {
        return String(nodeRun.output?.['question'] ?? nodeRun.text ?? 'Waiting');
      }
    }
    return 'Waiting';
  });

  async deletePipeline(id: string, ev: Event): Promise<void> {
    ev.stopPropagation();
    try {
      await this.api.deletePipeline(id);
      this.pipelines.update((all) => all.filter((p) => p.id !== id));
      if (this.open()?.id === id) this.open.set(null);
    } catch (err: unknown) {
      this.error.set(this.message(err));
    }
  }

  /** node id -> how many items it produced, for the labels on the wires. */
  readonly itemCounts = computed<Record<string, number>>(() => {
    const current = this.run();
    if (!current) return {};
    const out: Record<string, number> = {};
    for (const [id, nodeRun] of Object.entries(current.nodes)) {
      const data = nodeRun.output ?? {};
      const items = data['items'];
      if (Array.isArray(items)) out[id] = items.length;
      else if (typeof data['count'] === 'number') out[id] = data['count'] as number;
    }
    return out;
  });

  /**
   * Lay the graph out left to right in the order it actually runs.
   *
   * Positions are assigned as nodes are added, which assumes the order they
   * were created is the order they run. The builder breaks that assumption
   * routinely: it added a Gmail search first and wired it third, so the wire
   * ran right-to-left and entered the node from off-screen — a graph that was
   * correctly formed and looked broken, which is worse than one that looks
   * broken and is.
   *
   * Rank is longest-path from a root, not shortest: a node must sit to the
   * right of *every* step that feeds it, or one of its wires still points
   * backwards. Anything unreachable (a cycle, an orphan) keeps its rank at
   * the end rather than being dropped, since a node you cannot see is worse
   * than one in an odd place.
   */
  tidy(): void {
    const pipeline = this.open();
    if (!pipeline || !pipeline.nodes.length) return;

    const incoming = new Map<string, string[]>();
    for (const n of pipeline.nodes) incoming.set(n.id, []);
    for (const e of pipeline.edges) {
      if (incoming.has(e.target)) incoming.get(e.target)!.push(e.source);
    }

    const rank = new Map<string, number>();
    const resolve = (id: string, seen: Set<string>): number => {
      if (rank.has(id)) return rank.get(id)!;
      // A cycle has no longest path; stop rather than recurse forever.
      if (seen.has(id)) return 0;
      seen.add(id);
      const preds = incoming.get(id) ?? [];
      const r = preds.length
        ? Math.max(...preds.map((p) => resolve(p, seen) + 1))
        : 0;
      rank.set(id, r);
      return r;
    };
    for (const n of pipeline.nodes) resolve(n.id, new Set());

    const byRank = new Map<number, string[]>();
    for (const n of pipeline.nodes) {
      const r = rank.get(n.id) ?? 0;
      byRank.set(r, [...(byRank.get(r) ?? []), n.id]);
    }

    const at = new Map<string, { x: number; y: number }>();
    for (const [r, ids] of byRank) {
      ids.forEach((id, i) => {
        at.set(id, { x: 40 + r * (NODE_W + 70), y: 40 + i * (NODE_H + 34) });
      });
    }

    this.mutate((pl) => ({
      ...pl,
      nodes: pl.nodes.map((n) => ({ ...n, position: at.get(n.id) ?? n.position })),
    }));
    void this.save();
  }

  // -- the step picker ------------------------------------------------------
  //
  // Opened from a node's + or from the empty canvas. Adding from here wires
  // the new step as it lands, which is the difference that matters: picking
  // from the palette and then drawing the wire is two acts for one intention,
  // and the wire is the half people forget.

  readonly pickerFor =
    signal<{ source: string; port: string; side?: Side } | null>(null);
  readonly pickerOpen = signal(false);
  readonly pickerFilter = signal('');

  /** "What happens next?" when it follows something; the opening question
   *  when the canvas is empty. Compass has no trigger/step distinction in the
   *  catalogue, so the empty-canvas wording asks about the first step rather
   *  than promising a trigger picker that does not exist. */
  readonly pickerTitle = computed(() =>
    this.pickerFor() ? 'What happens next?' : 'How should this start?',
  );

  readonly pickerGroups = computed(() => {
    const term = this.pickerFilter().trim().toLowerCase();
    const groups = this.palette();
    if (!term) return groups;
    return groups
      .map((g) => ({
        name: g.name,
        types: g.types.filter((t) =>
          `${t.label} ${t.id} ${t.description}`.toLowerCase().includes(term),
        ),
      }))
      .filter((g) => g.types.length);
  });

  openPicker(after: { source: string; port: string; side?: Side } | null): void {
    this.pickerFor.set(after);
    this.pickerFilter.set('');
    this.pickerOpen.set(true);
  }

  closePicker(): void {
    this.pickerOpen.set(false);
  }

  /** Adds the chosen type and, when it followed something, wires it. */
  pickStep(type: NodeTypeInfo): void {
    const after = this.pickerFor();
    this.addNode(type, after ?? undefined);
    if (after) {
      const added = this.open()?.nodes.at(-1);
      if (added) {
        this.connect({ source: after.source, port: after.port, target: added.id });
      }
    }
    this.pickerOpen.set(false);
  }

  // -- credential setup -----------------------------------------------------
  //
  // After a graph exists, the question is what it still needs before it can
  // run for real. Computed on the client from the node types, the connection
  // kinds and the connections that exist — all three are already here, and a
  // round trip would only make the banner lag the node someone just added.

  readonly setupOpen = signal(false);
  readonly setupStep = signal(0);

  readonly neededSetup = computed(() => {
    const pipeline = this.open();
    if (!pipeline) return [];
    const types = this.typeMap();
    const kinds = new Map(this.connectionKinds().map((k) => [k.kind, k]));
    const have = new Set(this.connections().map((c) => c.kind));

    const byKind = new Map<string, string[]>();
    for (const node of pipeline.nodes) {
      const kind = types.get(node.type)?.connection_kind;
      if (!kind) continue;
      byKind.set(kind, [...(byKind.get(kind) ?? []), node.name || node.id]);
    }
    return [...byKind.entries()].map(([kind, nodes]) => ({
      kind,
      label: kinds.get(kind)?.label ?? kind,
      note: kinds.get(kind)?.note ?? '',
      auth: kinds.get(kind)?.auth ?? 'none',
      nodes,
      have: have.has(kind),
    }));
  });

  /** Only the ones still missing — what the banner counts and the stepper
   *  walks. A kind that already has a connection is not a step. */
  readonly setupTodo = computed(() => this.neededSetup().filter((s) => !s.have));

  /** Capabilities the graph needs that nobody has granted. Surfaced beside
   *  the connections because they are the other thing that stops a pipeline
   *  running, and the builder is deliberately not allowed to grant them. */
  readonly missingCapabilities = computed(() => {
    const pipeline = this.open();
    if (!pipeline) return [];
    const types = this.typeMap();
    const needed = new Set<string>();
    for (const node of pipeline.nodes) {
      const requires = types.get(node.type)?.requires;
      if (requires && !pipeline.capabilities.includes(requires)) {
        needed.add(requires);
      }
    }
    return [...needed].sort();
  });

  openSetup(): void {
    this.setupStep.set(0);
    this.setupOpen.set(true);
    this.primeDraftForStep();
  }

  closeSetup(): void {
    this.setupOpen.set(false);
  }

  readonly currentStep = computed(() => this.setupTodo()[this.setupStep()] ?? null);

  /** Fills the connection form with the kind this step is about, so the
   *  stepper is not a form you have to re-answer a question in front of. */
  private primeDraftForStep(): void {
    const step = this.setupTodo()[this.setupStep()];
    if (!step) return;
    this.draft.set({
      kind: step.kind,
      name: `${step.label} connection`,
      auth: step.auth,
      endpoint: '',
      secret: '',
    });
  }

  async connectStep(): Promise<void> {
    const made = await this.addConnection();
    // Attach it to the nodes that were waiting for one. Creating a connection
    // and leaving every node still saying "choose a connection" defeats the
    // point of a stepper — its whole job is to leave the pipeline runnable.
    // Only nodes with none are filled: a node someone deliberately pointed at
    // a different connection keeps it.
    if (made) this.attachConnection(made);
    // The list refreshed, so this kind is no longer a step; the index stays
    // put and now points at whatever was next.
    if (this.setupStep() >= this.setupTodo().length) {
      this.setupOpen.set(false);
      return;
    }
    this.primeDraftForStep();
  }

  private attachConnection(made: PipelineConnection): void {
    const types = this.typeMap();
    this.mutate((p) => ({
      ...p,
      nodes: p.nodes.map((n) =>
        !n.connection_id && types.get(n.type)?.connection_kind === made.kind
          ? { ...n, connection_id: made.id }
          : n,
      ),
    }));
    void this.save();
  }

  skipStep(): void {
    const next = this.setupStep() + 1;
    if (next >= this.setupTodo().length) {
      this.setupOpen.set(false);
      return;
    }
    this.setupStep.set(next);
    this.primeDraftForStep();
  }

  // -- the builder ----------------------------------------------------------
  //
  // A conversation that edits the graph you are looking at. Every edit is
  // written to the store as the model makes it, so the canvas is refreshed
  // when the turn ends rather than reconstructed from what was said — a plan
  // you have to apply is a different, worse product.

  readonly chatDraft = signal('');
  readonly building = signal(false);
  readonly chat = signal<
    { role: 'you' | 'builder'; text: string; steps: string[] }[]
  >([]);

  /** The Builder is always on screen now, so "Build with AI" puts the cursor
   *  in it rather than revealing it. */
  focusBuilder(): void {
    const box = document.querySelector<HTMLTextAreaElement>('.pl-chat-ask textarea');
    box?.focus();
  }

  async sendToBuilder(): Promise<void> {
    const pipeline = this.open();
    const text = this.chatDraft().trim();
    if (!pipeline || !text || this.building()) return;
    if (this.dirty()) await this.save();

    this.chatDraft.set('');
    this.chat.update((c) => [...c, { role: 'you', text, steps: [] }]);
    // The reply is appended now and filled in as it streams, so the steps
    // appear while the work happens rather than all at once at the end.
    this.chat.update((c) => [...c, { role: 'builder', text: '', steps: [] }]);
    this.building.set(true);
    this.error.set('');

    const patch = (fn: (last: { text: string; steps: string[] }) => void) => {
      this.chat.update((c) => {
        const copy = [...c];
        const last = { ...copy[copy.length - 1] };
        fn(last);
        copy[copy.length - 1] = last;
        return copy;
      });
    };

    try {
      await this.api.streamBuild(pipeline.id, text, (ev) => {
        switch (ev.type) {
          case 'tool_call_started':
            patch((l) => {
              l.steps = [...l.steps, this.describeStep(ev)];
            });
            break;
          case 'text_delta':
            patch((l) => {
              l.text += String(ev['text'] ?? '');
            });
            break;
          case 'assistant_message':
            patch((l) => {
              l.text = String(ev['text'] ?? l.text);
            });
            break;
          case 'tool_result':
            // Refresh as edits land, not only when the turn ends. The whole
            // claim of this panel is that the canvas updates while you watch;
            // without this it showed "Adding Start" over an empty canvas and
            // caught up at the end, which is a plan being applied rather than
            // a graph being built.
            if (MUTATING.has(String(ev['tool_name'] ?? ''))) {
              void this.reloadGraph();
            }
            break;
          case 'error':
            this.error.set(String(ev['message'] ?? 'The builder stopped.'));
            break;
        }
      });
    } catch (err: unknown) {
      // An `error` event during the stream already said what went wrong —
      // a rate limit, a refusal — and it is more specific than anything
      // recoverable from the thrown value. Overwriting it turned "exceeded
      // rate limit" into "could not reach the pipelines service", which sent
      // the reader looking for a network fault that was not there.
      if (!this.error()) this.error.set(this.message(err));
    } finally {
      this.building.set(false);
      // The graph changed underneath us while the model worked.
      await this.reloadGraph();
      // And lay it out: the builder creates nodes in the order it thinks of
      // them and wires them in the order they run, which are not the same
      // order and leave wires pointing backwards.
      this.tidy();
    }
  }

  /** A tool call as a line of narration. The tool's own name is Compass's
   *  business — what the reader wants is what happened to their graph. */
  private describeStep(ev: Record<string, unknown>): string {
    const name = String(ev['tool_name'] ?? '');
    const args = (ev['arguments'] ?? {}) as Record<string, unknown>;
    switch (name) {
      case 'pipeline_node_types':
        return args['search'] ? `Looking for ${args['search']} steps` : 'Reading the catalogue';
      case 'pipeline_describe_node_type':
        return `Checking what ${args['type_id']} takes`;
      case 'pipeline_read':
        return 'Reading the current graph';
      case 'pipeline_add_node':
        return `Adding ${args['name'] || args['type_id']}`;
      case 'pipeline_connect':
        return `Wiring ${args['source']} to ${args['target']}`;
      case 'pipeline_set_config':
        return `Configuring ${args['node_id']}`;
      case 'pipeline_remove_node':
        return `Removing ${args['node_id']}`;
      case 'pipeline_validate':
        return 'Checking the graph';
      case 'pipeline_dry_run':
        return 'Dry running it';
      case 'pipeline_needed_connections':
        return 'Working out what still needs connecting';
      case 'ask_user':
        return 'Asking you something';
      default:
        return name || 'Working';
    }
  }

  /** Re-read the graph the builder just changed, keeping the view put. */
  private async reloadGraph(): Promise<void> {
    const current = this.open();
    if (!current) return;
    try {
      const fresh = await this.api.getPipeline(current.id);
      this.open.set(fresh);
      this.dirty.set(false);
    } catch {
      /* leave what is on screen; the next action will surface the problem */
    }
  }

  /**
   * Hand a failed step back to the builder.
   *
   * The message carries what the builder would otherwise have to go and read:
   * which node, its type, the settings it actually ran with, and the error in
   * full. Sending only "fix it" makes the model spend two tool calls
   * rediscovering what the screen already knows.
   *
   * The settings are the *resolved* ones from the run, which is the point —
   * a failure is usually an expression that resolved to something
   * unexpected, and the literal it became is what identifies it.
   */
  async fixWithAI(nodeId: string): Promise<void> {
    const pipeline = this.open();
    const node = pipeline?.nodes.find((n) => n.id === nodeId);
    const failed = this.stepRun()?.nodes?.[nodeId] ?? this.run()?.nodes?.[nodeId];
    if (!pipeline || !node || !failed) return;

    this.detailId.set('');
    this.chatDraft.set(
      [
        `The step "${node.name || node.id}" (${node.id}, type ${node.type}) failed.`,
        '',
        `Error: ${failed.error || 'no message'}`,
        '',
        `It ran with these settings, after expressions resolved:`,
        this.asJson(failed.input) || '{}',
        '',
        'Work out why and fix it. If the cause is a missing connection or a '
          + 'capability the pipeline does not hold, say so instead of working '
          + 'around it.',
      ].join('\n'),
    );
    await this.sendToBuilder();
  }

  /** Whether a step failed, for the button that offers to fix it. */
  failedNodes = computed(() => {
    const current = this.run();
    if (!current) return [];
    return Object.values(current.nodes).filter((n) => n.status === 'failed');
  });

  /** Reads back the builder conversation for this pipeline.
   *
   *  The transcript was always persisted — it is the same store the Code loop
   *  writes to — but nothing fetched it, so opening a pipeline showed an
   *  empty panel and the record of how the graph was built was invisible.
   *  The canvas shows what was decided; only this says why. */
  private async loadChat(pipelineId: string): Promise<void> {
    this.chat.set([]);
    try {
      const history = await this.api.buildHistory(pipelineId);
      this.chat.set(
        history.messages.map((m) => ({
          role: m.role === 'user' ? ('you' as const) : ('builder' as const),
          text: m.text,
          // Steps are live narration of one turn, not part of the record —
          // replaying them would suggest work happening now.
          steps: [],
        })),
      );
    } catch {
      /* an empty panel is a fair fallback; the next turn still works */
    }
  }

  async clearChat(): Promise<void> {
    const pipeline = this.open();
    this.chat.set([]);
    if (pipeline) await this.api.resetBuild(pipeline.id).catch(() => undefined);
  }

  // -- node details ---------------------------------------------------------
  //
  // The three-pane view: what came in, what the node is set to, what came out.
  // The middle pane is the inspector that already exists — this adds the two
  // outer panes, which only became possible once a single node could be run
  // on its own.

  readonly detailId = signal('');
  /** Result of the last "Execute step", kept apart from the pipeline run so
   *  trying one node does not overwrite the record of the last full run. */
  readonly stepRun = signal<PipelineRun | null>(null);
  readonly seedText = signal('');
  readonly mockText = signal('');
  readonly seedError = signal('');

  readonly detailNode = computed<PipelineNode | null>(
    () => this.open()?.nodes.find((n) => n.id === this.detailId()) ?? null,
  );

  readonly detailType = computed<NodeTypeInfo | undefined>(() => {
    const node = this.detailNode();
    return node ? this.typeMap().get(node.type) : undefined;
  });

  /** The node's last recorded run: the step run if one was just done, else
   *  its part of the last full run. */
  readonly detailRun = computed(() => {
    const id = this.detailId();
    if (!id) return null;
    return this.stepRun()?.nodes?.[id] ?? this.run()?.nodes?.[id] ?? null;
  });

  openDetail(id: string): void {
    this.detailId.set(id);
    this.selectedId.set(id);
    this.stepRun.set(null);
    this.seedError.set('');
    this.seedText.set('');
    const node = this.open()?.nodes.find((n) => n.id === id);
    this.mockText.set(node?.mock ? JSON.stringify(node.mock, null, 2) : '');
  }

  closeDetail(): void {
    this.detailId.set('');
    this.stepRun.set(null);
  }

  /** Runs this node alone. `live` calls the world; `mock` returns its pinned
   *  or stubbed data, which is how a step is tried before it can work. */
  async runStep(mode: 'live' | 'mock'): Promise<void> {
    const pipeline = this.open();
    const node = this.detailNode();
    if (!pipeline || !node) return;

    let seed: Record<string, unknown> = {};
    const text = this.seedText().trim();
    if (text) {
      try {
        seed = JSON.parse(text);
      } catch {
        this.seedError.set('That is not valid JSON.');
        return;
      }
    }
    this.seedError.set('');
    if (this.dirty()) await this.save();
    this.busy.set('Running step…');
    try {
      this.stepRun.set(
        await this.api.runPipelineNode(pipeline.id, node.id, seed, mode),
      );
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.busy.set('');
    }
  }

  /** Pins output so a dry run returns it instead of a stub. Taking it from
   *  the last real run is the common case — you ran it once, and now you want
   *  that payload every time without calling out again. */
  pinLastOutput(): void {
    const last = this.detailRun();
    if (!last || this.isEmpty(last.output)) return;
    this.mockText.set(JSON.stringify(last.output, null, 2));
    this.saveMock();
  }

  saveMock(): void {
    const id = this.detailId();
    const text = this.mockText().trim();
    if (!text) {
      this.patchNodeById(id, { mock: null });
      return;
    }
    try {
      this.patchNodeById(id, { mock: JSON.parse(text) });
      this.seedError.set('');
    } catch {
      this.seedError.set('Pinned data must be valid JSON.');
    }
  }

  private patchNodeById(id: string, patch: Partial<PipelineNode>): void {
    this.mutate((p) => ({
      ...p,
      nodes: p.nodes.map((n) => (n.id === id ? { ...n, ...patch } : n)),
    }));
  }

  // -- export ---------------------------------------------------------------
  //
  // A pipeline drawn here is a design, and scheduling is only one thing you
  // might do with it. Export is the other: take the graph out as a diagram,
  // an architecture note, and a package another application can import.

  readonly exported = signal<PipelineExport | null>(null);
  readonly exportTab = signal<'architecture' | 'diagram' | 'files'>('architecture');
  readonly exportFile = signal('');
  readonly copied = signal('');

  readonly currentFile = computed(() => {
    const bundle = this.exported();
    if (!bundle) return null;
    return bundle.files.find((f) => f.path === this.exportFile()) ?? bundle.files[0] ?? null;
  });

  async showExport(): Promise<void> {
    const pipeline = this.open();
    if (!pipeline) return;
    if (this.dirty()) await this.save();
    this.busy.set('Exporting…');
    try {
      const bundle = await this.api.exportPipeline(pipeline.id);
      this.exported.set(bundle);
      this.exportTab.set('architecture');
      this.exportFile.set(bundle.files[0]?.path ?? '');
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.busy.set('');
    }
  }

  closeExport(): void {
    this.exported.set(null);
  }

  /** Copy rather than download: the export is read before it is used, and a
   *  browser download of a many-file package would be a zip nobody opens. */
  async copy(text: string, label: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      this.copied.set(label);
      setTimeout(() => this.copied.set(''), 1600);
    } catch {
      this.error.set('The browser would not allow copying.');
    }
  }

  copyAll(): void {
    const bundle = this.exported();
    if (!bundle) return;
    // One paste that reconstructs the tree: each file under a path header, so
    // it can be pasted into a chat, a ticket, or an agent that writes files.
    const text = bundle.files
      .map((f) => `----- ${f.path} -----\n${f.content}`)
      .join('\n');
    void this.copy(text, 'all');
  }

  // -- connections ----------------------------------------------------------
  //
  // A connection is a separate object holding an endpoint and how to
  // authenticate to it; the credential goes to the server once and is stored
  // behind a reference, never returned. That is what lets a pipeline be
  // exported and shared without leaking, and makes revoking access one
  // delete rather than a search across every graph.

  readonly connectionsOpen = signal(false);
  readonly draft = signal({ kind: 'rest', name: '', auth: 'none', endpoint: '', secret: '' });

  /** What each kind of connection needs. Loaded once; the form is rendered
   *  from it rather than hand-written per service. */
  readonly credentialTypes = signal<CredentialTypeInfo[]>([]);
  /** The connection whose credential is open for editing. */
  readonly editing = signal<string>('');
  /** Field values being typed. Never pre-filled from the server — a stored
   *  secret is never sent back, and an empty box means "leave it alone". */
  readonly credDraft = signal<Record<string, string>>({});
  /** The last test result per connection, so the answer stays on screen. */
  readonly testResult = signal<Record<string, { ok: boolean; message: string }>>({});

  credentialType(kind: string): CredentialTypeInfo | undefined {
    return this.credentialTypes().find((c) => c.kind === kind);
  }

  setCred(name: string, value: string): void {
    this.credDraft.update((d) => ({ ...d, [name]: value }));
  }

  openCredential(id: string): void {
    this.editing.set(this.editing() === id ? '' : id);
    this.credDraft.set({});
  }

  async saveCredential(conn: PipelineConnection): Promise<void> {
    this.busy.set('Saving…');
    try {
      await this.api.patchConnection(conn.id, { values: this.credDraft() });
      this.credDraft.set({});
      await this.reloadConnections();
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.busy.set('');
    }
  }

  async testConnection(conn: PipelineConnection): Promise<void> {
    this.busy.set('Testing…');
    try {
      const res = await this.api.testConnection(conn.id);
      this.testResult.update((r) => ({ ...r, [conn.id]: res }));
    } catch (err: unknown) {
      this.testResult.update((r) => ({
        ...r, [conn.id]: { ok: false, message: this.message(err) },
      }));
    } finally {
      this.busy.set('');
    }
  }

  /**
   * Send the person to the provider, in a separate window.
   *
   * A window rather than a redirect because the canvas may have unsaved
   * work: navigating away to Google and back would be a round trip through
   * a page load for something that takes ten seconds. The callback page
   * closes itself, and the panel re-reads the connection when focus returns.
   */
  async signIn(conn: PipelineConnection): Promise<void> {
    try {
      const { url } = await this.api.signInConnection(conn.id);
      const opened = window.open(url, 'compass-oauth', 'width=520,height=680');
      if (!opened) {
        this.error.set('The sign-in window was blocked. Allow pop-ups for '
          + 'Compass and try again.');
        return;
      }
      const poll = window.setInterval(async () => {
        if (!opened.closed) return;
        window.clearInterval(poll);
        await this.reloadConnections();
      }, 700);
    } catch (err: unknown) {
      this.error.set(this.message(err));
    }
  }

  async signOut(conn: PipelineConnection): Promise<void> {
    try {
      await this.api.signOutConnection(conn.id);
      this.testResult.update((r) => ({ ...r, [conn.id]: undefined as never }));
      await this.reloadConnections();
    } catch (err: unknown) {
      this.error.set(this.message(err));
    }
  }

  /**
   * Pin this node's sample output, so a graph can be run without an account.
   *
   * The sample comes from the node type, which declares what a realistic
   * result of its own looks like — a Gmail step yields a message with a
   * sender and a subject rather than {"id": "mock-1"}. Written onto the node
   * as pinned data, which is the same field a person fills in by hand, so
   * there is one mechanism rather than two.
   */
  useSampleData(node: PipelineNode): void {
    const type = this.nodeTypes().find((t) => t.id === node.type);
    const sample = type?.sample;
    if (!sample) {
      this.error.set('This step has no sample data to stand in for a real '
        + 'call. Paste pinned data on it, or connect an account.');
      return;
    }
    this.patchNode({ mock: sample });
  }

  private async reloadConnections(): Promise<void> {
    const list = await this.api.pipelineConnections();
    this.connections.set(list.connections);
  }

  setDraft(key: 'kind' | 'name' | 'auth' | 'endpoint' | 'secret', value: string): void {
    this.draft.update((d) => ({ ...d, [key]: value }));
  }

  async addConnection(): Promise<PipelineConnection | null> {
    const d = this.draft();
    if (!d.name.trim() || !d.kind.trim()) return null;
    this.busy.set('Saving…');
    let made: PipelineConnection | null = null;
    try {
      made = await this.api.createConnection({
        kind: d.kind.trim(),
        name: d.name.trim(),
        auth: d.auth,
        config: d.endpoint ? { endpoint: d.endpoint.trim() } : {},
        secret: d.secret,
      });
      // Cleared rather than kept: the secret is write-only and holding it in
      // a signal after the round trip serves nothing.
      this.draft.set({ kind: 'rest', name: '', auth: 'none', endpoint: '', secret: '' });
      const list = await this.api.pipelineConnections();
      this.connections.set(list.connections);
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.busy.set('');
    }
    return made;
  }

  async deleteConnection(id: string): Promise<void> {
    try {
      await this.api.deleteConnection(id);
      this.connections.update((all) => all.filter((c) => c.id !== id));
    } catch (err: unknown) {
      this.error.set(this.message(err));
    }
  }

  /**
   * The most specific thing that can be said about a failure.
   *
   * Three shapes reach here: an HttpClient error with a parsed body, a plain
   * Error thrown by the streaming fetch carrying "<status> <body>", and
   * everything else. The old version understood only the first and answered
   * "Could not reach the pipelines service" to all of them — which described
   * a 404 from a service that had answered, and sent the reader hunting a
   * network fault that was not there.
   */
  private message(err: unknown): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    if (detail) return detail;

    const raw = (err as { message?: string })?.message ?? '';
    const status = Number(raw.match(/^(\d{3})\s/)?.[1] ?? 0);
    let body = raw.replace(/^\d{3}\s/, '').trim();
    try {
      const parsed = JSON.parse(body);
      body = String(parsed?.detail ?? body);
    } catch {
      /* not JSON; the text is what there is */
    }

    // A 404 on an endpoint the client knows about means the server does not
    // have it — almost always a backend running older code than the page.
    // Saying so turns a dead end into one instruction.
    if (status === 404) {
      return 'The server does not have this endpoint. It is probably running '
        + 'an older build than this page — restart the backend and try again.';
    }
    if (status === 409) return body || 'Something else is already running.';
    if (status) return body ? `${status}: ${body}` : `Request failed (${status}).`;
    return body || 'Could not reach the pipelines service.';
  }
}
