import { Injectable, computed, signal } from '@angular/core';
import { Artifact, ArtifactKind } from './models';

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

  /** An "edit this part" request raised from the artifact panel: the user
   *  highlighted `selection` and typed `instruction`. The shell watches this
   *  and sends it to the model as a targeted edit. */
  readonly editRequest = signal<{ selection: string; instruction: string } | null>(
    null,
  );
  requestEdit(selection: string, instruction: string): void {
    this.editRequest.set({ selection, instruction });
  }

  open(a: Artifact): void {
    this.active.set(a);
  }

  close(): void {
    this.active.set(null);
  }

  /** Decide whether a fenced code block is an artifact (full document), and
   * of which kind. Small inline HTML snippets stay as ordinary code blocks. */
  static classify(lang: string, code: string): ArtifactKind | null {
    const l = (lang || '').toLowerCase();
    const trimmed = code.trim();
    if (
      l === 'azure' ||
      l === 'azure-arch' ||
      (l === 'json' && /"nodes"\s*:/.test(trimmed) && /"service"\s*:/.test(trimmed))
    ) {
      return 'azure';
    }
    if (
      l === 'drawio' ||
      l === 'mxgraph' ||
      trimmed.startsWith('<mxfile') ||
      /^<mxGraphModel[\s>]/.test(trimmed)
    ) {
      return 'drawio';
    }
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
  static titleFor(kind: ArtifactKind, code: string): string {
    if (kind === 'azure') {
      try {
        const t = JSON.parse(code.trim())?.title;
        if (typeof t === 'string' && t.trim()) return t.trim();
      } catch {
        /* ignore */
      }
      return 'Azure Architecture Diagram';
    }
    if (kind === 'drawio') {
      const name = /<diagram[^>]*\sname="([^"]+)"/i.exec(code)?.[1]?.trim();
      return name || 'Azure Architecture Diagram';
    }
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
      let t = inner.trim();
      // Normalize every quote form the model emits (HTML `&quot;`, Mermaid
      // `#quot;`, or a raw `"`) to a real quote, so we can re-escape cleanly.
      // Leaving a stray `&quot;`/unbalanced `"` in the label is what trips the
      // Mermaid lexer ("Unrecognized text").
      t = t
        .replace(/&quot;|&#0*34;|#quot;/gi, '"')
        .replace(/&#0*39;|&apos;|#39;/gi, "'")
        .replace(/&lt;|#lt;/gi, '<')
        .replace(/&gt;|#gt;/gi, '>')
        .replace(/&amp;/gi, '&');
      // Strip one layer of wrapping quotes (handles already-quoted inners and a
      // stray trailing quote a model adds after an escaped one)…
      if (t.length >= 2 && t.startsWith('"') && t.endsWith('"')) t = t.slice(1, -1);
      // …then emit exactly one clean quoted label, escaping any inner quotes
      // with Mermaid's own entity so the parser never sees a bare `"`.
      return `"${t.replace(/"/g, '#quot;')}"`;
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
      // rhombus — the `(?!\{)` opener guard skips the already-processed hexagon.
      [/(\b\w+)\{(?!\{)([^}]+?)\}/g, (id, i) => `${id}{${quote(i)}}`], // rhombus
      // round — `(?![[(])` skips the already-processed stadium `([` / circle `((`.
      [/(\b\w+)\((?![[(])([^)]+?)\)/g, (id, i) => `${id}(${quote(i)})`], // round
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

  /** Normalize a draw.io artifact into a complete `<mxfile>` document that
   * diagrams.net / the VS Code draw.io extension / Visio can open directly.
   * A bare `<mxGraphModel>` is wrapped; an existing `<mxfile>` passes through. */
  static drawioFile(code: string): string {
    const t = code.trim();
    if (t.startsWith('<mxfile')) return t;
    const model = /<mxGraphModel[\s\S]*<\/mxGraphModel>/i.exec(t)?.[0] ?? t;
    return `<mxfile host="app.diagrams.net" type="device">\n  <diagram name="Azure Architecture" id="compass">\n    ${model}\n  </diagram>\n</mxfile>`;
  }

  /** Build a diagrams.net URL that opens the diagram in the editor, using the
   * same deflate+base64 scheme draw.io uses for its `#R` fragment. Best-effort:
   * throws if the browser lacks CompressionStream, so callers fall back to the
   * guaranteed download path. */
  static async drawioViewerUrl(code: string): Promise<string> {
    const t = code.trim();
    const model = /<mxGraphModel[\s\S]*<\/mxGraphModel>/i.exec(t)?.[0] ?? t;
    // draw.io compresses encodeURIComponent(xml) with raw DEFLATE, then base64.
    const input = new TextEncoder().encode(encodeURIComponent(model));
    const cs = new (globalThis as any).CompressionStream('deflate-raw');
    const stream = new Blob([input]).stream().pipeThrough(cs);
    const buf = new Uint8Array(await new Response(stream).arrayBuffer());
    let bin = '';
    for (const b of buf) bin += String.fromCharCode(b);
    const b64 = btoa(bin);
    return 'https://app.diagrams.net/#R' + encodeURIComponent(b64);
  }

  /** Wrap raw content into a full, standalone HTML document for preview /
   * new-tab. HTML passes through; SVG is centered on a white ground. */
  /** Reports the user's text selection up to the parent so the panel can offer
   *  "edit this part" — the frame is sandboxed (opaque origin), so postMessage
   *  is the only channel. Injected into every previewed artifact. */
  static readonly SELECT_SCRIPT = `<script>
(function(){
  function send(){
    try{
      var s=String(getSelection());
      parent.postMessage({__compassSel:true,text:s.trim()},'*');
    }catch(e){}
  }
  document.addEventListener('mouseup',send);
  document.addEventListener('keyup',send);
})();
</script>`;

  static toDocument(a: Artifact): string {
    if (a.kind === 'html') {
      // Append the selection reporter just before </body> (or at the end).
      const s = ArtifactService.SELECT_SCRIPT;
      return a.code.includes('</body>')
        ? a.code.replace('</body>', `${s}</body>`)
        : a.code + s;
    }
    return `<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%}body{display:grid;place-items:center;background:#fff}
svg{max-width:100%;max-height:100vh}</style></head><body>${a.code}${ArtifactService.SELECT_SCRIPT}</body></html>`;
  }
}
