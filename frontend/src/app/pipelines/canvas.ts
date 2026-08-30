import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { PipelineEdge, PipelineNode, NodeTypeInfo } from '../models';

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
export const NODE_W = 190;
export const NODE_H = 62;
const PORT_R = 5;

interface Wire {
  key: string;
  d: string;
  when: string;
  midX: number;
  midY: number;
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
  readonly selectedId = input<string>('');

  readonly select = output<string>();
  readonly moveNode = output<{ id: string; x: number; y: number }>();
  readonly connect = output<{ source: string; port: string; target: string }>();
  readonly removeEdge = output<PipelineEdge>();

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

  /** Where an output port sits, in graph coordinates. Ports are spread down
   *  the right edge so a two-output node (an If) has room for both. */
  outPort(node: PipelineNode, index: number, total: number): { x: number; y: number } {
    const step = NODE_H / (total + 1);
    return { x: node.position.x + NODE_W, y: node.position.y + step * (index + 1) };
  }

  inPort(node: PipelineNode): { x: number; y: number } {
    return { x: node.position.x, y: node.position.y + NODE_H / 2 };
  }

  outputsOf(node: PipelineNode): { name: string; label: string }[] {
    const type = this.types().get(node.type);
    const ports = type?.outputs ?? [];
    return ports.length ? ports.map((p) => ({ name: p.name, label: p.label || p.name })) : [];
  }

  hasInput(node: PipelineNode): boolean {
    const type = this.types().get(node.type);
    return (type?.inputs?.length ?? 1) > 0;
  }

  /** Every edge as a cubic curve. Horizontal control points, so a wire leaves
   *  a port going right and arrives going right — which reads as flow even
   *  when the target sits above or behind the source. */
  readonly wires = computed<Wire[]>(() => {
    const out: Wire[] = [];
    for (const edge of this.edges()) {
      const source = this.node(edge.source);
      const target = this.node(edge.target);
      if (!source || !target) continue;
      const ports = this.outputsOf(source);
      const index = Math.max(0, ports.findIndex((p) => p.name === (edge.port || 'out')));
      const a = this.outPort(source, index, ports.length || 1);
      const b = this.inPort(target);
      out.push({
        key: `${edge.source}:${edge.port}->${edge.target}:${edge.when}`,
        d: this.curve(a, b),
        when: edge.when,
        midX: (a.x + b.x) / 2,
        midY: (a.y + b.y) / 2,
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

  private curve(a: { x: number; y: number }, b: { x: number; y: number }): string {
    const reach = Math.max(40, Math.abs(b.x - a.x) * 0.5);
    return `M ${a.x} ${a.y} C ${a.x + reach} ${a.y}, ${b.x - reach} ${b.y}, ${b.x} ${b.y}`;
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

  resetView(): void {
    this.scale.set(1);
    this.panX.set(0);
    this.panY.set(0);
  }

  // -- presentation ---------------------------------------------------------

  labelFor(node: PipelineNode): string {
    return node.name || this.types().get(node.type)?.label || node.type;
  }

  kindFor(node: PipelineNode): string {
    return this.types().get(node.type)?.category ?? '';
  }

  statusFor(node: PipelineNode): string {
    return this.statuses()[node.id] ?? '';
  }

  /** A missing type is worth showing rather than hiding: it means a provider
   *  went away — an MCP server disconnected — and the node cannot run. */
  isUnknown(node: PipelineNode): boolean {
    return !this.types().has(node.type);
  }
}
