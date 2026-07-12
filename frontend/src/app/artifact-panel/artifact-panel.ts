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

/**
 * Live artifact preview beside the chat — the Compass take on Claude's
 * Artifacts panel. Renders the document in a sandboxed iframe (Preview),
 * shows the source (Code), and offers copy + open-in-new-tab. srcdoc is set
 * imperatively so nothing runs on our origin (sandbox has no allow-same-origin).
 */
@Component({
  selector: 'app-artifact-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (svc.active(); as a) {
      <div class="ap-head">
        <div class="ap-id">
          <span class="ap-ico">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 5a1 1 0 011-1h14a1 1 0 011 1v14a1 1 0 01-1 1H5a1 1 0 01-1-1z"/><path d="M4 9h16" stroke-linecap="round"/></svg>
          </span>
          <span class="ap-title">{{ a.title }}</span>
          <span class="ap-kind">{{ a.kind }}</span>
        </div>
        <div class="ap-tabs">
          <button [class.on]="tab() === 'preview'" (click)="tab.set('preview')">Preview</button>
          <button [class.on]="tab() === 'code'" (click)="tab.set('code')">Code</button>
        </div>
        <div class="ap-actions">
          <button class="ap-btn" [title]="copied() ? 'Copied' : 'Copy code'" (click)="copy(a.code)">
            @if (copied()) {
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M20 6L9 17l-5-5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            } @else {
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10" stroke-linecap="round" stroke-linejoin="round"/></svg>
            }
          </button>
          <button class="ap-btn" title="Open in new tab" (click)="openNewTab()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button class="ap-btn" title="Close" (click)="svc.close()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke-linecap="round"/></svg>
          </button>
        </div>
      </div>

      <div class="ap-body">
        <iframe #frame class="ap-frame" [hidden]="tab() !== 'preview'"
          sandbox="allow-scripts allow-modals allow-popups allow-forms"
          title="Artifact preview"></iframe>
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
  readonly tab = signal<'preview' | 'code'>('preview');
  readonly copied = signal(false);
  private readonly frame =
    viewChild<ElementRef<HTMLIFrameElement>>('frame');

  constructor() {
    // Re-render the iframe whenever the artifact changes or we return to the
    // preview tab. New artifact opens on the Preview tab.
    effect(() => {
      const a = this.svc.active();
      this.tab();
      const iframe = this.frame()?.nativeElement;
      if (iframe && a && this.tab() === 'preview') {
        iframe.srcdoc = ArtifactService.toDocument(a);
      }
    });
    effect(() => {
      // Reset to Preview each time a different artifact opens.
      this.svc.active();
      queueMicrotask(() => this.tab.set('preview'));
    });
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
    const blob = new Blob([ArtifactService.toDocument(a)], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank', 'noopener');
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
}
