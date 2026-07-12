import { Injectable, computed, signal } from '@angular/core';
import { Artifact } from './models';

/** A fenced block whose first line is a Mermaid diagram directive. */
const MERMAID_HEAD =
  /^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram(-v2)?|erDiagram|journey|gantt|pie|mindmap|timeline|gitGraph|quadrantChart|C4Context)\b/i;

/**
 * Holds the currently-open artifact and the preview panel's visibility.
 * Markdown detects an artifact-worthy code block and calls open(); the shell
 * renders the panel bound to active().
 */
@Injectable({ providedIn: 'root' })
export class ArtifactService {
  readonly active = signal<Artifact | null>(null);
  readonly isOpen = computed(() => this.active() !== null);

  open(a: Artifact): void {
    this.active.set(a);
  }

  close(): void {
    this.active.set(null);
  }

  /** Decide whether a fenced code block is an artifact (full document), and
   * of which kind. Small inline HTML snippets stay as ordinary code blocks. */
  static classify(lang: string, code: string): 'html' | 'svg' | 'mermaid' | null {
    const l = (lang || '').toLowerCase();
    const trimmed = code.trim();
    if (l === 'mermaid' || l === 'mmd' || MERMAID_HEAD.test(trimmed)) return 'mermaid';
    if (l === 'svg' || trimmed.toLowerCase().startsWith('<svg')) return 'svg';
    if (l === 'html' || l === 'htm' || l === 'xml') {
      const c = trimmed.toLowerCase();
      if (
        c.includes('<!doctype') ||
        c.includes('<html') ||
        c.includes('<body') ||
        (c.includes('<') && trimmed.length > 400)
      ) {
        return 'html';
      }
    }
    return null;
  }

  /** Stable id from the content (djb2) so the same artifact keeps one identity
   * across re-parses and matches between the chat card and the auto-open. */
  static idFor(code: string): string {
    let h = 5381;
    for (let i = 0; i < code.length; i++) h = ((h << 5) + h + code.charCodeAt(i)) | 0;
    return 'art' + (h >>> 0).toString(36);
  }

  /** Extract the last artifact-worthy fenced block from a message, or null. */
  static extract(text: string): Artifact | null {
    const fence = /```([\w+-]*)\n?([\s\S]*?)```/g;
    let m: RegExpExecArray | null;
    let found: Artifact | null = null;
    while ((m = fence.exec(text))) {
      const code = m[2].replace(/\n$/, '');
      const kind = ArtifactService.classify(m[1] || '', code);
      if (kind) {
        found = {
          id: ArtifactService.idFor(code),
          kind,
          title: ArtifactService.titleFor(kind, code),
          code,
        };
      }
    }
    return found;
  }

  /** A human title from the document's <title>/<h1>, else a generic label. */
  static titleFor(kind: 'html' | 'svg' | 'mermaid', code: string): string {
    if (kind === 'mermaid') {
      // A `%% title: X` comment or the diagram type as a friendly label.
      const t = /%%\s*title:\s*(.+)/i.exec(code)?.[1]?.trim();
      if (t) return t;
      const type = /^(\w[\w-]*)/.exec(code.trim())?.[1] ?? 'Diagram';
      const nice: Record<string, string> = {
        graph: 'Flowchart', flowchart: 'Flowchart',
        sequenceDiagram: 'Sequence Diagram', classDiagram: 'Class Diagram',
        stateDiagram: 'State Diagram', 'stateDiagram-v2': 'State Diagram',
        erDiagram: 'ER Diagram', gantt: 'Gantt Chart', pie: 'Pie Chart',
        mindmap: 'Mind Map', timeline: 'Timeline', gitGraph: 'Git Graph',
      };
      return nice[type] ?? 'Diagram';
    }
    const title = /<title[^>]*>([^<]+)<\/title>/i.exec(code)?.[1]?.trim();
    if (title) return title;
    const h1 = /<h1[^>]*>([^<]+)<\/h1>/i.exec(code)?.[1]?.trim();
    if (h1) return h1;
    return kind === 'svg' ? 'SVG Image' : 'HTML Artifact';
  }

  /**
   * Repair the most common Mermaid mistakes models make so a diagram renders
   * instead of throwing a parse error. Chiefly: node/edge label text with
   * unquoted parentheses/brackets/punctuation (`API[API Layer (REST + SSE)]`),
   * and literal `\n`/`\t` escapes inside labels. We wrap every label in double
   * quotes — Mermaid's own escape hatch for arbitrary label text — and turn
   * `\n` into `<br/>`. Comment/directive lines are left untouched.
   */
  static sanitizeMermaid(code: string): string {
    let s = code.replace(/\r\n?/g, '\n');
    // Literal escape sequences the model sometimes emits inside labels.
    s = s.replace(/\\n/g, '<br/>').replace(/\\t/g, ' ');

    const quote = (inner: string): string => {
      const t = inner.trim();
      if (/^".*"$/.test(t)) return t; // already quoted
      return `"${t.replace(/"/g, '&quot;')}"`;
    };

    // Node shapes, most-specific delimiters first so `[[`, `[(`, `([`, `((`,
    // `{{` are consumed before the single-delimiter forms. Each guards against
    // already-quoted inners and against re-matching a wrapped shape.
    const shapes: Array<[RegExp, (id: string, inner: string) => string]> = [
      [/(\b\w+)\[\[([^\]]+?)\]\]/g, (id, i) => `${id}[[${quote(i)}]]`], // subroutine
      [/(\b\w+)\[\(([^)]+?)\)\]/g, (id, i) => `${id}[(${quote(i)})]`], // cylinder
      [/(\b\w+)\(\[([^\]]+?)\]\)/g, (id, i) => `${id}([${quote(i)}])`], // stadium
      [/(\b\w+)\(\(([^)]+?)\)\)/g, (id, i) => `${id}((${quote(i)}))`], // circle
      [/(\b\w+)\{\{([^}]+?)\}\}/g, (id, i) => `${id}{{${quote(i)}}}`], // hexagon
      [/(\b\w+)\{([^}]+?)\}/g, (id, i) => `${id}{${quote(i)}}`], // rhombus
      [/(\b\w+)\((?!\[)([^)]+?)\)/g, (id, i) => `${id}(${quote(i)})`], // round
      // rectangle + subgraph title — the `(?![[(])` opener guard already skips
      // the already-processed `[[`/`[(` shapes, so match to the first `]`.
      [/(\b\w+)\[(?![[(])([^\]]+?)\]/g, (id, i) => `${id}[${quote(i)}]`],
    ];

    return s
      .split('\n')
      .map((line) => {
        if (/^\s*%%/.test(line)) return line; // comment / directive
        // Edge labels: `-->|label|`, `---|label|`, etc.
        line = line.replace(/\|([^|]+)\|/g, (_m, inner) => `|${quote(inner)}|`);
        for (const [re, fn] of shapes) {
          line = line.replace(re, (_m, id, inner) => fn(id, inner));
        }
        return line;
      })
      .join('\n');
  }

  /** Wrap raw content into a full, standalone HTML document for preview /
   * new-tab. HTML passes through; SVG is centered on a white ground. */
  static toDocument(a: Artifact): string {
    if (a.kind === 'html') return a.code;
    return `<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%}body{display:grid;place-items:center;background:#fff}
svg{max-width:100%;max-height:100vh}</style></head><body>${a.code}</body></html>`;
  }
}
