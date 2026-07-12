import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { ArtifactService } from '../artifact.service';
import { ThemeService } from '../theme.service';

/**
 * Live artifact preview beside the chat — the Compass take on Claude's
 * Artifacts panel.
 *   - html / svg: rendered in a sandboxed iframe (no allow-same-origin).
 *   - mermaid:    rendered via Mermaid's auto-layout engine to an SVG, so a
 *                 diagram's nodes and edges can never overlap — the mechanism
 *                 claude.ai uses for clean diagrams.
 * Preview / Code tabs, copy, open-in-new-tab, refresh, close.
 */
@Component({
  selector: 'app-artifact-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (svc.active(); as a) {
      <div class="ap-head">
        <span class="ap-ico">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 5a1 1 0 011-1h14a1 1 0 011 1v14a1 1 0 01-1 1H5a1 1 0 01-1-1z"/><path d="M4 9h16" stroke-linecap="round"/></svg>
        </span>
        <div class="ap-id">
          <span class="ap-title">{{ a.title }}</span>
          <span class="ap-meta">{{ label(a.kind) }} · updated just now</span>
        </div>
        <div class="ap-actions">
          <button class="ap-btn" title="Refresh" (click)="refresh()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12a9 9 0 11-3-6.7M21 4v5h-5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button class="ap-btn" [title]="copied() ? 'Copied' : 'Copy code'" (click)="copy(a.code)">
            @if (copied()) {
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M20 6L9 17l-5-5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            } @else {
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10" stroke-linecap="round" stroke-linejoin="round"/></svg>
            }
          </button>
          <button class="ap-btn" title="Open in new tab" (click)="openNewTab()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button class="ap-btn" title="Close" (click)="svc.close()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke-linecap="round"/></svg>
          </button>
        </div>
      </div>

      <div class="ap-tabbar">
        <div class="ap-tabs">
          <button [class.on]="tab() === 'preview'" (click)="tab.set('preview')">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/></svg>
            Preview
          </button>
          <button [class.on]="tab() === 'code'" (click)="tab.set('code')">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M8 6l-6 6 6 6M16 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
            Code
          </button>
        </div>
      </div>

      <div class="ap-body">
        @if (a.kind === 'mermaid') {
          <div class="ap-diagram" [hidden]="tab() !== 'preview'">
            @if (mermaidError()) { <pre class="ap-derr">{{ mermaidError() }}</pre> }
            <div #mermaidHost class="ap-mermaid"></div>
          </div>
        } @else {
          <iframe #frame class="ap-frame" [hidden]="tab() !== 'preview'"
            sandbox="allow-scripts allow-modals allow-popups allow-forms"
            title="Artifact preview"></iframe>
        }
        @if (tab() === 'code') {
          <pre class="ap-code"><code>{{ a.code }}</code></pre>
        }
      </div>
    }
  `,
  styleUrl: './artifact-panel.css',
})
export class ArtifactPanel {
  readonly svc = inject(ArtifactService);
  private readonly theme = inject(ThemeService);
  readonly tab = signal<'preview' | 'code'>('preview');
  readonly copied = signal(false);
  readonly mermaidError = signal('');
  private mermaidSvg = '';
  private readonly frame = viewChild<ElementRef<HTMLIFrameElement>>('frame');
  private readonly mermaidHost =
    viewChild<ElementRef<HTMLDivElement>>('mermaidHost');

  constructor() {
    // Render the preview when the artifact, tab, or theme changes.
    effect(() => {
      const a = this.svc.active();
      this.tab();
      this.theme.theme();
      if (!a || this.tab() !== 'preview') return;
      if (a.kind === 'mermaid') {
        void this.renderMermaid(a.code);
      } else {
        queueMicrotask(() => {
          const iframe = this.frame()?.nativeElement;
          if (iframe) iframe.srcdoc = ArtifactService.toDocument(a);
        });
      }
    });
    // Each new artifact opens on the Preview tab.
    effect(() => {
      this.svc.active();
      queueMicrotask(() => this.tab.set('preview'));
    });
  }

  label(kind: 'html' | 'svg' | 'mermaid'): string {
    return kind === 'mermaid' ? 'DIAGRAM' : kind === 'svg' ? 'SVG' : 'HTML';
  }

  private async renderMermaid(code: string): Promise<void> {
    try {
      const mermaid = (await import('mermaid')).default;
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: this.theme.theme() === 'dark' ? 'dark' : 'default',
        fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
      });
      const id = 'mmd-' + Math.random().toString(36).slice(2);
      const { svg } = await mermaid.render(id, ArtifactService.sanitizeMermaid(code));
      this.mermaidSvg = svg;
      this.mermaidError.set('');
    } catch (err) {
      this.mermaidSvg = '';
      this.mermaidError.set(
        (err as Error)?.message ?? 'Could not render this diagram.',
      );
    }
    const host = this.mermaidHost()?.nativeElement;
    if (host) host.innerHTML = this.mermaidSvg;
  }

  refresh(): void {
    const a = this.svc.active();
    if (!a) return;
    if (a.kind === 'mermaid') {
      void this.renderMermaid(a.code);
    } else {
      const iframe = this.frame()?.nativeElement;
      if (iframe) iframe.srcdoc = ArtifactService.toDocument(a);
    }
  }

  async copy(code: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(code);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = code;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    this.copied.set(true);
    setTimeout(() => this.copied.set(false), 1400);
  }

  openNewTab(): void {
    const a = this.svc.active();
    if (!a) return;
    const html =
      a.kind === 'mermaid'
        ? `<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%}body{display:grid;place-items:center;background:#fff;padding:24px}
svg{max-width:100%;height:auto}</style></head><body>${this.mermaidSvg}</body></html>`
        : ArtifactService.toDocument(a);
    const blob = new Blob([html], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank', 'noopener');
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
}
