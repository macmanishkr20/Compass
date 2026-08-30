import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { CompassApiService } from '../compass-api.service';
import { NodeTypeInfo, PipelineSummary } from '../models';

/**
 * The Pipelines section.
 *
 * Scaffold, deliberately: the list, the palette read from the server's own
 * catalogue, and the run history. The canvas — dragging nodes, pulling arrows
 * between ports — is the next piece and the largest, and putting a fake one
 * here would make it harder to tell what works from what does not.
 *
 * The palette is worth looking at even at this stage, because it is the whole
 * design in miniature: nothing in this component knows what a node *is*. It
 * renders whatever `/v1/pipelines/node-types` returns, so a node type added by
 * a provider — a connector, an MCP server that just connected — appears here
 * with no frontend change at all.
 */
@Component({
  selector: 'app-pipelines',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './pipelines.html',
  styleUrl: './pipelines.css',
})
export class Pipelines {
  private readonly api = inject(CompassApiService);

  readonly loading = signal(false);
  readonly error = signal('');
  readonly pipelines = signal<PipelineSummary[]>([]);
  readonly nodeTypes = signal<NodeTypeInfo[]>([]);
  readonly selected = signal<PipelineSummary | null>(null);
  readonly newName = signal('');

  /** Node types grouped for the palette, in a stable order. */
  readonly categories = signal<{ name: string; types: NodeTypeInfo[] }[]>([]);

  /** Loads on construction, which happens when the section is first entered
   *  rather than at startup — the template creates this component only for
   *  `section() === 'pipelines'`. That ordering matters: with the module
   *  disabled these endpoints do not exist, and fetching them on every boot
   *  would be a pair of 404s in the console of an app that is working fine. */
  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    try {
      const [list, cat] = await Promise.all([
        this.api.pipelines(),
        this.api.pipelineNodeTypes(),
      ]);
      this.pipelines.set(list.pipelines);
      this.nodeTypes.set(cat.node_types);
      this.categories.set(this.group(cat.node_types));
    } catch (err: unknown) {
      this.error.set(this.message(err));
    } finally {
      this.loading.set(false);
    }
  }

  private group(types: NodeTypeInfo[]): { name: string; types: NodeTypeInfo[] }[] {
    const order = ['flow', 'connector', 'intelligence', 'code', 'module', 'tool', 'io'];
    const byCategory = new Map<string, NodeTypeInfo[]>();
    for (const type of types) {
      const list = byCategory.get(type.category) ?? [];
      list.push(type);
      byCategory.set(type.category, list);
    }
    return [...byCategory.entries()]
      .sort((a, b) => {
        const ai = order.indexOf(a[0]);
        const bi = order.indexOf(b[0]);
        return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
      })
      .map(([name, list]) => ({
        name,
        types: list.sort((a, b) => a.label.localeCompare(b.label)),
      }));
  }

  async create(): Promise<void> {
    const name = this.newName().trim();
    if (!name) return;
    try {
      const made = await this.api.createPipeline(name);
      this.pipelines.update((all) => [made, ...all]);
      this.newName.set('');
      this.selected.set(made);
    } catch (err: unknown) {
      this.error.set(this.message(err));
    }
  }

  select(pipeline: PipelineSummary): void {
    this.selected.set(pipeline);
  }

  countFor(category: string): number {
    return this.nodeTypes().filter((t) => t.category === category).length;
  }

  private message(err: unknown): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    return detail || 'Could not reach the pipelines service.';
  }
}
