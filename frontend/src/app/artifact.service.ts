import { Injectable, computed, signal } from '@angular/core';
import { Artifact } from './models';

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
  static classify(lang: string, code: string): 'html' | 'svg' | null {
    const l = (lang || '').toLowerCase();
    const trimmed = code.trim();
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
  static titleFor(kind: 'html' | 'svg', code: string): string {
    const title = /<title[^>]*>([^<]+)<\/title>/i.exec(code)?.[1]?.trim();
    if (title) return title;
    const h1 = /<h1[^>]*>([^<]+)<\/h1>/i.exec(code)?.[1]?.trim();
    if (h1) return h1;
    return kind === 'svg' ? 'SVG Image' : 'HTML Artifact';
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
