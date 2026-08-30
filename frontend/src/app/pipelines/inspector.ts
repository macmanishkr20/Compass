import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { NodeTypeInfo, PipelineNode } from '../models';

/**
 * The properties pane: one shared frame, plus a form generated from the
 * node type's own JSON Schema.
 *
 * The generated half is the point. Nothing here knows what any node does — it
 * reads `config_schema` and renders a control per property, so a node type
 * contributed by a provider (a connector, an MCP server that just connected)
 * arrives with a working settings form and no frontend change. Hand-writing a
 * form per node type would cap the catalogue at whatever the frontend had
 * been taught, which is exactly what the registry exists to avoid.
 *
 * The shared frame follows Fabric's General tab, because those turn out to be
 * the fields every node needs whatever it does: a name, a timeout, a retry
 * policy, a way to keep a payload out of the log, and a way to switch a node
 * off without deleting it.
 */

export interface Field {
  key: string;
  title: string;
  description: string;
  /** The control to draw. Derived from the schema, not from the node type. */
  control: 'text' | 'textarea' | 'number' | 'checkbox' | 'select' | 'json';
  options: string[];
  required: boolean;
  placeholder: string;
}

@Component({
  selector: 'app-pipeline-inspector',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './inspector.html',
  styleUrl: './inspector.css',
})
export class PipelineInspector {
  readonly node = input.required<PipelineNode>();
  readonly type = input<NodeTypeInfo | undefined>(undefined);
  /** Connections the user has, for a node type that needs one. */
  readonly connections = input<{ id: string; name: string; kind: string }[]>([]);

  readonly patch = output<Partial<PipelineNode>>();
  readonly remove = output<string>();

  readonly tab = input<'general' | 'settings'>('settings');

  /** The schema turned into a list of controls. */
  readonly fields = computed<Field[]>(() => {
    const schema = this.type()?.config_schema as
      | { properties?: Record<string, Record<string, unknown>>; required?: string[] }
      | undefined;
    const properties = schema?.properties ?? {};
    const required = new Set(schema?.required ?? []);
    return Object.entries(properties).map(([key, raw]) =>
      this.toField(key, raw ?? {}, required.has(key)),
    );
  });

  private toField(key: string, raw: Record<string, unknown>, required: boolean): Field {
    // A schema type can be a union ("string" or "array"); the first entry is
    // the one to draw a control for, and the JSON escape hatch covers the rest.
    const declared = raw['type'];
    const kind = Array.isArray(declared) ? String(declared[0]) : String(declared ?? 'string');
    const options = (raw['enum'] as string[] | undefined)?.map(String) ?? [];
    const description = String(raw['description'] ?? '');

    let control: Field['control'] = 'text';
    if (options.length) control = 'select';
    else if (kind === 'boolean') control = 'checkbox';
    else if (kind === 'number' || kind === 'integer') control = 'number';
    else if (kind === 'array' || kind === 'object') control = 'json';
    else if (description.length > 90) control = 'textarea';

    return {
      key,
      title: String(raw['title'] ?? key.replace(/_/g, ' ')),
      description,
      control,
      options,
      required,
      // Expressions are the reason most fields are typed at all, so the hint
      // shows one rather than describing the syntax in the abstract.
      placeholder: control === 'text' || control === 'textarea'
        ? "value, or @nodes('id').data.field"
        : '',
    };
  }

  valueOf(key: string): unknown {
    return this.node().config?.[key];
  }

  displayOf(key: string): string {
    const value = this.valueOf(key);
    if (value === undefined || value === null) return '';
    return typeof value === 'string' ? value : JSON.stringify(value);
  }

  /** Emits only the key that changed, never a rebuilt config object.
   *
   *  Rebuilding from `this.node()` looks equivalent and is not: an input
   *  signal updates on change detection, not synchronously, so two edits in
   *  one cycle both read the same stale config and the second silently
   *  discards the first. Measured — setting a name and a value together kept
   *  only the value. The parent merges, so each edit is independent of what
   *  this component last saw. */
  setConfig(key: string, value: unknown): void {
    this.patch.emit({ config: { [key]: value } });
  }

  /** JSON fields accept either an expression or literal JSON. A half-typed
   *  value is kept as text rather than thrown away — losing what someone is
   *  mid-way through typing is worse than holding an invalid value briefly. */
  setJson(key: string, raw: string): void {
    const text = raw.trim();
    if (!text) return this.setConfig(key, undefined);
    if (text.startsWith('@')) return this.setConfig(key, text);
    try {
      this.setConfig(key, JSON.parse(text));
    } catch {
      this.setConfig(key, text);
    }
  }

  setNumber(key: string, raw: string): void {
    const text = raw.trim();
    if (text.startsWith('@')) return this.setConfig(key, text);
    const n = Number(text);
    this.setConfig(key, Number.isFinite(n) ? n : undefined);
  }

  asInput(ev: Event): HTMLInputElement {
    return ev.target as HTMLInputElement;
  }
}
