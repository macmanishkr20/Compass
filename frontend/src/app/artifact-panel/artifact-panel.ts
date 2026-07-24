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
import { layout, parseSpec, toDrawio, toSvg } from '../azure-diagram';
import { Artifact, ArtifactKind } from '../models';
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
          @if (a.kind === 'drawio' || a.kind === 'azure') {
            <button class="ap-btn" title="Download .drawio" (click)="download(a)">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
          } @else {
            <button class="ap-btn" title="Refresh" (click)="refresh()">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12a9 9 0 11-3-6.7M21 4v5h-5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
          }
          <button class="ap-btn" [title]="copied() ? 'Copied' : 'Copy code'" (click)="copy(a.code)">
            @if (copied()) {
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M20 6L9 17l-5-5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            } @else {
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10" stroke-linecap="round" stroke-linejoin="round"/></svg>
            }
          </button>
          @if (a.kind === 'mermaid') {
            <button class="ap-btn" title="Open in draw.io" (click)="openInDrawio()">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M10 6.5h4a1 1 0 011 1V14" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
          }
          <button class="ap-btn" [title]="a.kind === 'drawio' || a.kind === 'azure' ? 'Open in diagrams.net' : 'Open in new tab'" (click)="openNewTab()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button class="ap-btn" title="Open full size in a new window" (click)="popOut()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 9V5a1 1 0 011-1h4M20 15v4a1 1 0 01-1 1h-4M15 4h4a1 1 0 011 1v4M9 20H5a1 1 0 01-1-1v-4" stroke-linecap="round" stroke-linejoin="round"/></svg>
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
        @if (a.kind === 'azure') {
          <div class="ap-diagram ap-azure" [hidden]="tab() !== 'preview'">
            @if (azureError()) { <pre class="ap-derr">{{ azureError() }}</pre> }
            <div #azureHost class="ap-azuresvg"></div>
            <div class="ap-azure-bar">
              <span>Editable Azure diagram · real icons travel inside the file</span>
              <span class="ap-azure-acts">
                <button (click)="openNewTab()">Open in diagrams.net</button>
                <button (click)="download(a)">Download .drawio</button>
              </span>
            </div>
          </div>
        } @else if (a.kind === 'drawio') {
          <div class="ap-drawio" [hidden]="tab() !== 'preview'">
            <div class="ap-dio-card">
              <span class="ap-dio-ico">
                <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><path d="M10 6.5h4a1 1 0 011 1V14" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </span>
              <h3>{{ a.title }}</h3>
              <p>An editable Azure architecture diagram with the official Azure
                icon set. Open it in draw.io to view, edit, and export to
                PNG, SVG, or Visio.</p>
              <div class="ap-dio-btns">
                <button class="ap-dio-primary" (click)="openNewTab()">Open in diagrams.net</button>
                <button class="ap-dio-secondary" (click)="download(a)">Download .drawio</button>
              </div>
              <p class="ap-dio-hint">Tip: in draw.io, <strong>Arrange → Layout</strong>
                re-flows the diagram if any connectors sit tight.</p>
            </div>
          </div>
        } @else if (a.kind === 'mermaid') {
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
  readonly azureError = signal('');
  private mermaidSvg = '';
  private azureDrawio = ''; // compiled draw.io for the active azure artifact
  private azureSvg = ''; // compiled inline SVG for the active azure artifact
  private azureDims = { w: 0, h: 0 };
  private readonly frame = viewChild<ElementRef<HTMLIFrameElement>>('frame');
  private readonly mermaidHost =
    viewChild<ElementRef<HTMLDivElement>>('mermaidHost');
  private readonly azureHost =
    viewChild<ElementRef<HTMLDivElement>>('azureHost');

  constructor() {
    // Render the preview when the artifact, tab, or theme changes.
    effect(() => {
      const a = this.svc.active();
      this.tab();
      this.theme.theme();
      if (!a || this.tab() !== 'preview') return;
      if (a.kind === 'drawio') return; // rendered as a call-to-action card
      if (a.kind === 'azure') {
        void this.renderAzure(a.code);
      } else if (a.kind === 'mermaid') {
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

  label(kind: ArtifactKind): string {
    return kind === 'drawio' || kind === 'azure'
      ? 'AZURE DIAGRAM'
      : kind === 'mermaid'
        ? 'DIAGRAM'
        : kind === 'svg'
          ? 'SVG'
          : 'HTML';
  }

  private async renderMermaid(code: string): Promise<void> {
    try {
      const mermaid = (await import('mermaid')).default;
      const dark = this.theme.theme() === 'dark';
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'loose', // allow classDef/style colours from the model
        theme: 'base',
        themeVariables: dark
          ? {
              darkMode: true,
              background: '#0F1219',
              primaryColor: '#312E81',
              primaryBorderColor: '#818CF8',
              primaryTextColor: '#E0E7FF',
              secondaryColor: '#4A1D3F',
              secondaryBorderColor: '#F472B6',
              tertiaryColor: '#14532D',
              tertiaryBorderColor: '#4ADE80',
              lineColor: '#94A3B8',
              textColor: '#E2E8F0',
              edgeLabelBackground: '#1B202B',
              clusterBkg: 'rgba(129,140,248,0.08)',
              clusterBorder: '#3B4252',
              titleColor: '#E0E7FF',
              nodeBorder: '#818CF8',
            }
          : {
              background: '#FFFFFF',
              primaryColor: '#E0E7FF',
              primaryBorderColor: '#6366F1',
              primaryTextColor: '#1E1B4B',
              secondaryColor: '#FCE7F3',
              secondaryBorderColor: '#EC4899',
              tertiaryColor: '#DCFCE7',
              tertiaryBorderColor: '#22C55E',
              lineColor: '#475569',
              textColor: '#1E293B',
              edgeLabelBackground: '#FFFFFF',
              clusterBkg: '#F8FAFC',
              clusterBorder: '#CBD5E1',
              titleColor: '#1E1B4B',
              nodeBorder: '#6366F1',
            },
        flowchart: { curve: 'basis', htmlLabels: true, padding: 12 },
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

  /** Compile the Azure spec into a laid-out inline SVG (and cache the matching
   * draw.io export). Layout runs through ELK (async), so nodes/edges/groups are
   * properly ranked and never overlap. */
  private async renderAzure(code: string): Promise<void> {
    const spec = parseSpec(code);
    if (!spec) {
      this.azureError.set(
        'Could not read the Azure diagram spec (expected JSON with a "nodes" array).',
      );
      this.azureDrawio = '';
      this.setAzureHost('');
      return;
    }
    try {
      const laid = await layout(spec);
      // A newer artifact may have superseded this one while ELK ran.
      if (this.svc.active()?.code !== code) return;
      this.azureDrawio = toDrawio(laid);
      this.azureDims = { w: laid.width, h: laid.height };
      this.azureSvg = toSvg(laid, this.theme.theme() === 'dark');
      this.azureError.set('');
      this.setAzureHost(this.azureSvg);
    } catch (err) {
      this.azureError.set(
        (err as Error)?.message ?? 'Could not render this diagram.',
      );
      this.azureDrawio = '';
      this.setAzureHost('');
    }
  }

  private setAzureHost(svg: string): void {
    queueMicrotask(() => {
      const host = this.azureHost()?.nativeElement;
      if (!host) return;
      host.innerHTML = svg;
      // Start at the entry point (top-left), not wherever it last scrolled.
      const scroller = host.closest('.ap-azure') as HTMLElement | null;
      if (scroller) {
        scroller.scrollLeft = 0;
        scroller.scrollTop = 0;
      }
    });
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

  async openNewTab(): Promise<void> {
    const a = this.svc.active();
    if (!a) return;
    // draw.io / azure: open the diagram in the diagrams.net editor; fall back
    // to a download if the browser can't build the compressed URL.
    if (a.kind === 'drawio' || a.kind === 'azure') {
      const xml = a.kind === 'azure' ? this.azureDrawio : a.code;
      if (!xml) {
        this.download(a);
        return;
      }
      try {
        const url = await ArtifactService.drawioViewerUrl(xml);
        window.open(url, '_blank', 'noopener');
      } catch {
        this.download(a);
      }
      return;
    }
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

  /** Wrap the rendered mermaid SVG into a draw.io model (as a scalable image),
   * so the flowchart can be opened/annotated/exported in diagrams.net. */
  private mermaidDrawioModel(): string {
    const svg = this.mermaidSvg || '<svg xmlns="http://www.w3.org/2000/svg"/>';
    const vb = /viewBox="([\d.\- ]+)"/
      .exec(svg)?.[1]
      ?.trim()
      .split(/\s+/)
      .map(Number);
    const w = vb && vb.length === 4 ? Math.round(vb[2]) : 1200;
    const h = vb && vb.length === 4 ? Math.round(vb[3]) : 800;
    const dataUri = 'data:image/svg+xml,' + encodeURIComponent(svg);
    return `<mxGraphModel dx="1200" dy="800" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1169" pageHeight="826" math="0" shadow="0">
  <root>
    <mxCell id="0"/>
    <mxCell id="1" parent="0"/>
    <mxCell id="2" value="" style="shape=image;imageAspect=1;aspect=fixed;image=${dataUri};" vertex="1" parent="1">
      <mxGeometry x="40" y="40" width="${w}" height="${h}" as="geometry"/>
    </mxCell>
  </root>
</mxGraphModel>`;
  }

  /** Open the current diagram in draw.io. Azure/drawio go through the existing
   * diagrams.net path; mermaid is embedded as an image and opened there too. */
  async openInDrawio(): Promise<void> {
    const a = this.svc.active();
    if (!a) return;
    if (a.kind === 'azure' || a.kind === 'drawio') return this.openNewTab();
    const model = this.mermaidDrawioModel();
    try {
      const url = await ArtifactService.drawioViewerUrl(model);
      window.open(url, '_blank', 'noopener');
    } catch {
      // Fallback: download a .drawio file the user can open manually.
      const xml = ArtifactService.drawioFile(model);
      const blob = new Blob([xml], { type: 'application/xml' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = (a.title || 'diagram') + '.drawio';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    }
  }

  /** Open the current artifact full-size in a new popup window: a zoomable,
   * scrollable page for diagrams; the live document for html/svg. */
  popOut(): void {
    const a = this.svc.active();
    if (!a) return;
    let html: string;
    const title = (a.title || 'Artifact').replace(/[<&]/g, '');
    if (a.kind === 'azure' || a.kind === 'mermaid') {
      const svg = a.kind === 'azure' ? this.azureSvg : this.mermaidSvg;
      const w = a.kind === 'azure' ? this.azureDims.w : 0;
      const h = a.kind === 'azure' ? this.azureDims.h : 0;
      const sizeStyle = w && h ? `width:${w}px;height:${h}px` : 'max-width:100%';
      html = `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title>
<style>
  html,body{margin:0;height:100%;background:#f4f6f9;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .bar{position:fixed;top:0;left:0;right:0;display:flex;gap:8px;align-items:center;
    padding:8px 14px;background:#fff;border-bottom:1px solid #e2e8f0;box-shadow:0 1px 4px rgba(0,0,0,.06);z-index:10}
  .bar b{font-size:13px;color:#1f2733;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar button{font:inherit;font-size:13px;font-weight:600;padding:5px 12px;border-radius:8px;border:1px solid #d7dee7;background:#fff;color:#26313d;cursor:pointer}
  .bar button:hover{background:#eef2f7}
  .stage{position:absolute;inset:46px 0 0 0;overflow:auto;padding:28px;display:flex;justify-content:center;align-items:flex-start;cursor:grab}
  .stage.grabbing{cursor:grabbing}
  .wrap{transform-origin:top center;transition:transform .08s ease-out}
  .wrap svg{${sizeStyle};height:auto;display:block;background:#fff;border-radius:10px;box-shadow:0 4px 24px rgba(15,26,43,.12)}
</style></head><body>
<div class="bar"><b>${title}</b>
  <button onclick="z(-1)">−</button><span id="zl" style="font-size:12px;color:#5a6b7b;min-width:42px;text-align:center">100%</span><button onclick="z(1)">+</button>
  <button onclick="z(0)">Reset</button><button onclick="fit()">Fit</button><button onclick="window.print()">Print</button>
</div>
<div class="stage" id="stage"><div class="wrap" id="wrap">${svg}</div></div>
<script>
  var s=1;var wrap=document.getElementById('wrap');var zl=document.getElementById('zl');var stage=document.getElementById('stage');
  function apply(){wrap.style.transform='scale('+s+')';zl.textContent=Math.round(s*100)+'%';}
  function z(d){s=d===0?1:Math.min(6,Math.max(0.15,s+d*0.15));apply();}
  function fit(){var svg=wrap.querySelector('svg');if(!svg)return;var r=svg.getBoundingClientRect();var aw=stage.clientWidth-56,ah=stage.clientHeight-56;s=Math.min(aw/(r.width/s),ah/(r.height/s),3);apply();}
  addEventListener('wheel',function(e){if(e.ctrlKey||e.metaKey){e.preventDefault();z(e.deltaY<0?1:-1);}},{passive:false});
  var pan=false,px,py,pl,pt;
  stage.addEventListener('pointerdown',function(e){pan=true;px=e.clientX;py=e.clientY;pl=stage.scrollLeft;pt=stage.scrollTop;stage.classList.add('grabbing');});
  addEventListener('pointerup',function(){pan=false;stage.classList.remove('grabbing');});
  stage.addEventListener('pointermove',function(e){if(!pan)return;stage.scrollLeft=pl-(e.clientX-px);stage.scrollTop=pt-(e.clientY-py);});
</script>
</body></html>`;
    } else {
      html = ArtifactService.toDocument(a);
    }
    const win = window.open('', '_blank', 'popup,width=1400,height=900');
    if (!win) return;
    win.document.open();
    win.document.write(html);
    win.document.close();
  }

  /** Save the diagram as a `.drawio` file the user can open in draw.io / Visio. */
  download(a: Artifact): void {
    const xml =
      a.kind === 'azure'
        ? this.azureDrawio || ArtifactService.drawioFile(a.code)
        : ArtifactService.drawioFile(a.code);
    const name =
      (a.title || 'azure-architecture').replace(/[^\w.-]+/g, '-').toLowerCase() +
      '.drawio';
    const blob = new Blob([xml], { type: 'application/xml' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
}
