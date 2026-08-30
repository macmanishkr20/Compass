import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { CompassApiService } from '../compass-api.service';
import {
  NodeTypeInfo,
  PipelineEdge,
  PipelineNode,
  PipelineProblem,
  PipelineRun,
  PipelineSummary,
} from '../models';
import { NODE_H, NODE_W, PipelineCanvas } from './canvas';
import { PipelineInspector } from './inspector';

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
  styleUrl: './pipelines.css',
  imports: [PipelineCanvas, PipelineInspector],
})
export class Pipelines {
  private readonly api = inject(CompassApiService);

  readonly loading = signal(false);
  readonly busy = signal('');
  readonly error = signal('');
  readonly pipelines = signal<PipelineSummary[]>([]);
  readonly nodeTypes = signal<NodeTypeInfo[]>([]);
  readonly connections = signal<{ id: string; name: string; kind: string }[]>([]);

  readonly open = signal<PipelineSummary | null>(null);
  readonly selectedId = signal('');
  readonly dirty = signal(false);
  readonly problems = signal<PipelineProblem[]>([]);
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

  readonly palette = computed(() => {
    const term = this.paletteFilter().trim().toLowerCase();
    const order = ['flow', 'connector', 'intelligence', 'code', 'module', 'tool', 'io'];
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
      this.connections.set(conns.connections);
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
  addNode(type: NodeTypeInfo): void {
    const pipeline = this.open();
    if (!pipeline) return;
    const right = pipeline.nodes.reduce((max, n) => Math.max(max, n.position.x), -1);
    const column = right < 0 ? 40 : right + NODE_W + 70;
    const stacked = pipeline.nodes.filter((n) => n.position.x === column).length;
    const node: PipelineNode = {
      id: `n_${Math.random().toString(36).slice(2, 9)}`,
      type: type.id,
      name: type.label,
      description: '',
      config: {},
      connection_id: '',
      position: { x: column, y: 40 + stacked * (NODE_H + 26) },
      timeout_s: 43200,
      retries: 0,
      retry_interval_s: 30,
      secure_input: false,
      secure_output: false,
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

  async runNow(): Promise<void> {
    const pipeline = this.open();
    if (!pipeline) return;
    if (this.dirty()) await this.save();
    this.busy.set('Running…');
    this.error.set('');
    try {
      this.run.set(await this.api.runPipeline(pipeline.id));
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.busy.set('');
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

  readonly runNodes = computed(() => {
    const current = this.run();
    if (!current) return [];
    const names = new Map(this.open()?.nodes.map((n) => [n.id, n.name || n.type]) ?? []);
    return Object.values(current.nodes)
      .filter((n) => n.status !== 'pending')
      .map((n) => ({ ...n, label: names.get(n.node_id) ?? n.node_id }));
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

  private message(err: unknown): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    return detail || 'Could not reach the pipelines service.';
  }
}
