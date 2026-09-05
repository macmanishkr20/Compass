import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  input,
  output,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import {
  NodeTypeInfo,
  PipelineConnection,
  PipelineEdge,
  PipelineNode,
} from '../models';

/**
 * The pipeline canvas: nodes you can drag, ports you can wire together.
 *
 * Two decisions shape everything else here.
 *
 * Nodes are HTML and edges are SVG, sharing one transformed coordinate space.
 * The alternative — drawing nodes into the SVG too — means re-implementing
 * text wrapping, focus and buttons that the browser already does, and a node
 * needs all three. So a single `<svg>` sits under an absolutely positioned
 * layer, both carrying the same pan/zoom transform, and the two stay in
 * register because neither ever computes a position the other does not share.
 *
 * Geometry lives in graph coordinates, never screen ones. Every pointer event
 * is converted on the way in. Doing it the other way round works until the
 * first zoom, at which point drags drift by the scale factor — and that bug
 * is invisible at 100%, which is where it gets written.
 */

/** Node box geometry. Fixed, because a wire has to know where a port is
 *  before the node has rendered — on first paint, and while dragging. */
/** One of a node's four edges. */
export type Side = 'top' | 'right' | 'bottom' | 'left';
interface Point { x: number; y: number }

export const NODE_W = 210;
// Taller than it was: a node now carries its kind, its name and the line
// saying what it is bound to. The old 62px fitted two of those three, and
// the third was silently clipped by the node's own overflow.
export const NODE_H = 110;
const PORT_R = 5;

interface Wire {
  key: string;
  d: string;
  when: string;
  midX: number;
  midY: number;
  /** "2 items" once a run has been through here. */
  items: string;
  edge: PipelineEdge;
}

@Component({
  selector: 'app-pipeline-canvas',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './canvas.html',
  styleUrl: './canvas.css',
  host: {
    '(pointermove)': 'onPointerMove($event)',
    '(pointerup)': 'onPointerUp($event)',
    '(pointerleave)': 'onPointerUp($event)',
  },
})
export class PipelineCanvas {
  readonly nodes = input.required<PipelineNode[]>();
  readonly edges = input.required<PipelineEdge[]>();
  readonly types = input.required<Map<string, NodeTypeInfo>>();
  /** node id -> run status, so a running graph shows its progress in place. */
  readonly statuses = input<Record<string, string>>({});
  /** node id -> the summary line a loop body node carries ("2/3 item(s)"). */
  readonly notes = input<Record<string, string>>({});
  /** node id -> how many items it produced, drawn on the wires leaving it.
   *  A graph where one step yields three and the next yields none is a graph
   *  whose failure is on an edge, and this is where it shows. */
  readonly counts = input<Record<string, number>>({});
  /** Nodes inside some loop's body. Marked on the canvas because it changes
   *  what a box means: it ran once per item, not once. */
  readonly inLoop = input<Set<string>>(new Set<string>());
  readonly selectedId = input<string>('');
  /** The connections that exist, so a node can say which one it is bound to
   *  rather than only that it wants one. */
  readonly connections = input<PipelineConnection[]>([]);

  /** The box's size, for the template. It used to write 190 and 62 as
   *  literals beside constants that said the same thing, which is exactly
   *  how the two come to disagree — and they did, the moment the box needed
   *  to be taller. */
  readonly nodeW = NODE_W;
  readonly nodeH = NODE_H;

  readonly select = output<string>();
  readonly moveNode = output<{ id: string; x: number; y: number }>();
  readonly connect = output<{ source: string; port: string; target: string }>();
  readonly removeEdge = output<PipelineEdge>();
  /** Double-click opens the node's own view. Single click selects, because
   *  selecting is what you do while wiring and opening is what you do when
   *  you have stopped. */
  readonly openNode = output<string>();
  /** The + on a node's trailing edge: add the next step and wire it, rather
   *  than adding from the palette and then drawing the wire yourself. */
  readonly addAfter = output<{ source: string; port: string; side?: Side }>();
  /** The two things worth doing on an empty canvas. */
  readonly addFirst = output<void>();
  readonly buildWithAI = output<void>();

  private readonly surface = viewChild<ElementRef<HTMLElement>>('surface');

  readonly scale = signal(1);
  readonly panX = signal(0);
  readonly panY = signal(0);

  /** The wire being pulled right now, in graph coordinates. */
  readonly linking = signal<{ source: string; port: string; x: number; y: number } | null>(
    null,
  );

  private drag: { id: string; dx: number; dy: number } | null = null;
  private panning: { x: number; y: number; ox: number; oy: number } | null = null;

  readonly transform = computed(
    () => `translate(${this.panX()}px, ${this.panY()}px) scale(${this.scale()})`,
  );

  // -- geometry -------------------------------------------------------------

  private node(id: string): PipelineNode | undefined {
    return this.nodes().find((n) => n.id === id);
  }

  /** A point on one of a node's four edges.
   *
   * `index`/`total` spread several attachments along that edge, so two wires
   * leaving the same side do not stack on one pixel. The right side is still
   * where an If's `true` and `false` live; the other three exist because a
   * graph is not always drawn left to right, and forcing every wire out of
   * the right edge makes a step placed above its source look like it feeds
   * backwards.
   */
  anchor(node: PipelineNode, side: Side, index = 0, total = 1): Point {
    const alongX = node.position.x + (NODE_W / (total + 1)) * (index + 1);
    const alongY = node.position.y + (NODE_H / (total + 1)) * (index + 1);
    switch (side) {
      case 'top': return { x: alongX, y: node.position.y };
      case 'bottom': return { x: alongX, y: node.position.y + NODE_H };
      case 'left': return { x: node.position.x, y: alongY };
      default: return { x: node.position.x + NODE_W, y: alongY };
    }
  }

  /** Which edge of the source a wire should leave from, and which edge of the
   *  target it should arrive at — the pair of sides that face each other.
   *  Chosen by whichever axis separates the two nodes more, so a step placed
   *  below its source is wired top-to-bottom rather than looping round. */
  facing(source: PipelineNode, target: PipelineNode): { from: Side; to: Side } {
    const dx = (target.position.x + NODE_W / 2) - (source.position.x + NODE_W / 2);
    const dy = (target.position.y + NODE_H / 2) - (source.position.y + NODE_H / 2);
    if (Math.abs(dx) >= Math.abs(dy)) {
      return dx >= 0 ? { from: 'right', to: 'left' } : { from: 'left', to: 'right' };
    }
    return dy >= 0 ? { from: 'bottom', to: 'top' } : { from: 'top', to: 'bottom' };
  }

  outPort(node: PipelineNode, index: number, total: number): Point {
    return this.anchor(node, 'right', index, total);
  }

  inPort(node: PipelineNode): Point {
    return this.anchor(node, 'left');
  }

  outputsOf(node: PipelineNode): { name: string; label: string }[] {
    const type = this.types().get(node.type);
    const ports = type?.outputs ?? [];
    return ports.length ? ports.map((p) => ({ name: p.name, label: p.label || p.name })) : [];
  }

  /** The port a side handle wires from: the node's first output, which for
   *  everything but a branch is its only one. A branch still has its own
   *  labelled handles on the right. */
  defaultPort(node: PipelineNode): string {
    return this.outputsOf(node)[0]?.name ?? 'out';
  }

  /** The three edges that get a plain handle. Typed here rather than written
   *  as strings in the template, where they would widen to `string` and the
   *  side would stop being one of four. */
  readonly sideHandles: Side[] = ['top', 'bottom', 'left'];

  hasInput(node: PipelineNode): boolean {
    const type = this.types().get(node.type);
    return (type?.inputs?.length ?? 1) > 0;
  }

  /** Every edge as a cubic curve. Horizontal control points, so a wire leaves
   *  a port going right and arrives going right — which reads as flow even
   *  when the target sits above or behind the source. */
  readonly wires = computed<Wire[]>(() => {
    const out: Wire[] = [];

    // How many wires leave each (node, side) and arrive at each, so several
    // sharing an edge of the box are spread along it instead of stacking on
    // one point. Counted first, then placed — a wire cannot know its own slot
    // without knowing how many neighbours it has.
    const leaving = new Map<string, number>();
    const arriving = new Map<string, number>();
    const sides = new Map<string, { from: Side; to: Side }>();
    for (const edge of this.edges()) {
      const source = this.node(edge.source);
      const target = this.node(edge.target);
      if (!source || !target) continue;
      const face = this.facing(source, target);
      sides.set(this.wireKey(edge), face);
      const fromKey = `${edge.source}:${face.from}`;
      const toKey = `${edge.target}:${face.to}`;
      leaving.set(fromKey, (leaving.get(fromKey) ?? 0) + 1);
      arriving.set(toKey, (arriving.get(toKey) ?? 0) + 1);
    }
    const usedFrom = new Map<string, number>();
    const usedTo = new Map<string, number>();

    for (const edge of this.edges()) {
      const source = this.node(edge.source);
      const target = this.node(edge.target);
      if (!source || !target) continue;
      const face = sides.get(this.wireKey(edge)) ?? { from: 'right' as Side, to: 'left' as Side };
      const fromKey = `${edge.source}:${face.from}`;
      const toKey = `${edge.target}:${face.to}`;
      const fromSlot = usedFrom.get(fromKey) ?? 0;
      const toSlot = usedTo.get(toKey) ?? 0;
      usedFrom.set(fromKey, fromSlot + 1);
      usedTo.set(toKey, toSlot + 1);

      const a = this.anchor(source, face.from, fromSlot, leaving.get(fromKey) ?? 1);
      const b = this.anchor(target, face.to, toSlot, arriving.get(toKey) ?? 1);
      const count = this.counts()[edge.source];
      out.push({
        key: `${edge.source}:${edge.port}->${edge.target}:${edge.when}`,
        d: this.curve(a, b, face.from, face.to),
        when: edge.when,
        midX: (a.x + b.x) / 2,
        midY: (a.y + b.y) / 2,
        items: count === undefined ? '' : `${count} item${count === 1 ? '' : 's'}`,
        edge,
      });
    }
    return out;
  });

  readonly linkWire = computed<string>(() => {
    const link = this.linking();
    if (!link) return '';
    const source = this.node(link.source);
    if (!source) return '';
    const ports = this.outputsOf(source);
    const index = Math.max(0, ports.findIndex((p) => p.name === link.port));
    const a = this.outPort(source, index, ports.length || 1);
    return this.curve(a, { x: link.x, y: link.y });
  });

  private wireKey(edge: PipelineEdge): string {
    return `${edge.source}:${edge.port}->${edge.target}:${edge.when}`;
  }

  /** A cubic whose control points leave along the side's own normal, so a
   *  wire departs perpendicular to the box it came from rather than always
   *  heading right. That is what makes a downward connection read as
   *  downward instead of as a loop. */
  private curve(a: Point, b: Point, from: Side = 'right', to: Side = 'left'): string {
    const reach = Math.max(
      40,
      Math.max(Math.abs(b.x - a.x), Math.abs(b.y - a.y)) * 0.5,
    );
    const push = (p: Point, side: Side, sign: number): Point => {
      switch (side) {
        case 'top': return { x: p.x, y: p.y - reach * sign };
        case 'bottom': return { x: p.x, y: p.y + reach * sign };
        case 'left': return { x: p.x - reach * sign, y: p.y };
        default: return { x: p.x + reach * sign, y: p.y };
      }
    };
    const c1 = push(a, from, 1);
    const c2 = push(b, to, 1);
    return `M ${a.x} ${a.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${b.x} ${b.y}`;
  }

  /** The drawing area, sized to hold every node plus room to drag into. */
  readonly extent = computed(() => {
    let w = 900;
    let h = 520;
    for (const node of this.nodes()) {
      w = Math.max(w, node.position.x + NODE_W + 240);
      h = Math.max(h, node.position.y + NODE_H + 200);
    }
    return { w, h };
  });

  // -- pointer --------------------------------------------------------------

  /** Screen point to graph point. Every handler goes through here; skipping
   *  it is the drift-on-zoom bug described at the top of the file. */
  private toGraph(ev: PointerEvent): { x: number; y: number } {
    const host = this.surface()?.nativeElement;
    if (!host) return { x: 0, y: 0 };
    const box = host.getBoundingClientRect();
    return {
      x: (ev.clientX - box.left - this.panX()) / this.scale(),
      y: (ev.clientY - box.top - this.panY()) / this.scale(),
    };
  }

  startDrag(ev: PointerEvent, node: PipelineNode): void {
    ev.stopPropagation();
    const point = this.toGraph(ev);
    this.drag = { id: node.id, dx: point.x - node.position.x, dy: point.y - node.position.y };
    this.select.emit(node.id);
  }

  startLink(ev: PointerEvent, node: PipelineNode, port: string): void {
    ev.stopPropagation();
    const point = this.toGraph(ev);
    this.linking.set({ source: node.id, port, x: point.x, y: point.y });
  }

  startPan(ev: PointerEvent): void {
    if (ev.button !== 0) return;
    this.panning = { x: ev.clientX, y: ev.clientY, ox: this.panX(), oy: this.panY() };
    this.select.emit('');
  }

  onPointerMove(ev: PointerEvent): void {
    if (this.drag) {
      const point = this.toGraph(ev);
      this.moveNode.emit({
        id: this.drag.id,
        x: Math.max(0, Math.round(point.x - this.drag.dx)),
        y: Math.max(0, Math.round(point.y - this.drag.dy)),
      });
      return;
    }
    if (this.linking()) {
      const point = this.toGraph(ev);
      this.linking.update((l) => (l ? { ...l, x: point.x, y: point.y } : l));
      return;
    }
    if (this.panning) {
      this.panX.set(this.panning.ox + (ev.clientX - this.panning.x));
      this.panY.set(this.panning.oy + (ev.clientY - this.panning.y));
    }
  }

  onPointerUp(ev: PointerEvent): void {
    const link = this.linking();
    if (link) {
      // The drop target is whatever node is under the pointer. Reading it
      // from the DOM rather than tracking hover state keeps the two from
      // disagreeing when a pointer leaves and re-enters during one drag.
      const el = document.elementFromPoint(ev.clientX, ev.clientY);
      const box = el?.closest<HTMLElement>('[data-node-id]');
      const target = box?.dataset['nodeId'];
      if (target && target !== link.source) {
        this.connect.emit({ source: link.source, port: link.port, target });
      }
      this.linking.set(null);
    }
    this.drag = null;
    this.panning = null;
  }

  onWheel(ev: WheelEvent): void {
    if (!ev.ctrlKey && !ev.metaKey) return; // plain scroll still scrolls
    ev.preventDefault();
    const next = Math.min(1.8, Math.max(0.4, this.scale() * (ev.deltaY < 0 ? 1.1 : 0.9)));
    this.scale.set(Number(next.toFixed(3)));
  }

  zoomBy(factor: number): void {
    this.scale.set(Number(Math.min(1.8, Math.max(0.4, this.scale() * factor)).toFixed(3)));
  }

  /** Pans just enough to show the node that was selected, when it is off the
   *  pane.
   *
   * A step added from the left or top handle lands at a smaller coordinate
   * than anything already there — often a negative one — and the default view
   * starts at the origin, so the node you just asked for is created outside
   * the window. Fitting the whole graph would answer that, but it also rescales
   * a graph nobody asked to rescale.
   *
   * Only the selection is tracked. Pan and zoom are read untracked on purpose:
   * reading them would make this re-run whenever the canvas is dragged, and
   * then dragging a selected node off the edge would snap it back, which is
   * the one thing a pan must never do.
   */
  private readonly reveal = effect(() => {
    const id = this.selectedId();
    if (!id) return;
    untracked(() => this.bringIntoView(id));
  });

  private bringIntoView(id: string): void {
    const node = untracked(() => this.node(id));
    const surface = this.surface()?.nativeElement;
    if (!node || !surface) return;

    const scale = this.scale();
    const margin = 32;
    const left = this.panX() + node.position.x * scale;
    const top = this.panY() + node.position.y * scale;
    const right = left + NODE_W * scale;
    const bottom = top + NODE_H * scale;
    const width = surface.clientWidth;
    const height = surface.clientHeight;
    if (!width || !height) return;

    let dx = 0;
    let dy = 0;
    if (left < margin) dx = margin - left;
    else if (right > width - margin) dx = width - margin - right;
    if (top < margin) dy = margin - top;
    else if (bottom > height - margin) dy = height - margin - bottom;

    if (dx) this.panX.set(Math.round(this.panX() + dx));
    if (dy) this.panY.set(Math.round(this.panY() + dy));
  }

  resetView(): void {
    this.scale.set(1);
    this.panX.set(0);
    this.panY.set(0);
  }

  /** Scale the graph to the pane, with a margin, and centre it.
   *
   * Different from reset: reset returns to 100% wherever the graph happens to
   * be, which on a wide pipeline shows the first two steps and nothing else.
   * Fit answers "show me all of it", which is the question the button beside
   * the zoom is actually being asked.
   */
  fitView(): void {
    const nodes = this.nodes();
    const surface = this.surface()?.nativeElement;
    if (!nodes.length || !surface) return this.resetView();

    const left = Math.min(...nodes.map((n) => n.position.x));
    const top = Math.min(...nodes.map((n) => n.position.y));
    const right = Math.max(...nodes.map((n) => n.position.x + NODE_W));
    const bottom = Math.max(...nodes.map((n) => n.position.y + NODE_H));

    const pad = 48;
    const scale = Math.min(
      1.8,
      Math.max(0.4, Math.min(
        (surface.clientWidth - pad * 2) / Math.max(1, right - left),
        (surface.clientHeight - pad * 2) / Math.max(1, bottom - top),
      )),
    );
    this.scale.set(Number(scale.toFixed(3)));
    this.panX.set(Math.round(pad - left * scale));
    this.panY.set(Math.round(pad - top * scale));
  }

  // -- presentation ---------------------------------------------------------

  labelFor(node: PipelineNode): string {
    return node.name || this.types().get(node.type)?.label || node.type;
  }

  kindFor(node: PipelineNode): string {
    return this.types().get(node.type)?.category ?? '';
  }

  /** What the badge says. A Gmail trigger is categorised `connector` so the
   *  engine will mock it, which is right — but on the canvas it is the step
   *  the graph begins at, and calling it CONNECTOR beside the connector it
   *  feeds is the opposite of a label's job. */
  kindLabel(node: PipelineNode): string {
    return this.isTrigger(node) ? 'trigger' : (this.kindFor(node) || 'node');
  }

  statusFor(node: PipelineNode): string {
    return this.statuses()[node.id] ?? '';
  }

  /** The per-item line for a loop body node, preferred over the bare status:
   *  "2/3 item(s), 1 failed" says more than "failed". */
  noteFor(node: PipelineNode): string {
    return this.notes()[node.id] ?? '';
  }

  /** The line under the name: what this step actually is, and what it is
   *  bound to. The name is the author's words — "Save to SQL Server" — and
   *  says nothing about which connector that is or which account it uses, so
   *  a box on a canvas cannot otherwise be told apart from a box beside it
   *  that does something else entirely. */
  subFor(node: PipelineNode): string {
    const type = this.types().get(node.type);
    const bits: string[] = [];
    if (type) {
      // The type id past its provider — "gmail.list_messages" is read as the
      // operation on the connector already named by the kind badge.
      bits.push(node.type.includes('.') ? node.type.split('.')[1] : node.type);
    } else {
      bits.push(node.type);
    }
    const connection = this.connections().find((c) => c.id === node.connection_id);
    if (connection) bits.push(connection.name);
    else if (type?.connection_kind) bits.push('no connection');
    return bits.join(' · ');
  }

  /** A step the graph begins at. Coloured apart from the connectors it is
   *  otherwise one of, because where a graph starts is the first thing
   *  anyone looks for — the question that got asked about a graph the
   *  builder drew was exactly this one. */
  isTrigger(node: PipelineNode): boolean {
    return node.type.endsWith('.trigger') || node.type === 'flow.start';
  }

  loopedFor(node: PipelineNode): boolean {
    return this.inLoop().has(node.id);
  }

  /** A missing type is worth showing rather than hiding: it means a provider
   *  went away — an MCP server disconnected — and the node cannot run. */
  isUnknown(node: PipelineNode): boolean {
    return !this.types().has(node.type);
  }
}
