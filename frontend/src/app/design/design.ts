import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';
import { CompassApiService } from '../compass-api.service';
import { DesignProject, DesignSystem, DesignTemplate, DesignTurn } from '../models';

type Tab = 'projects' | 'systems' | 'templates';
type Layout = 'list' | 'grid';

/**
 * Design — the port of Claude Design. Two screens live here:
 *
 *   landing   hero prompt + template grid, then the project library
 *             (Projects / Design systems / Templates, searchable, list or grid)
 *   workspace chat on the left, the live design on the right
 *
 * A design is a standalone HTML document, so the canvas is a sandboxed iframe
 * fed by srcdoc — the same thing that gets exported.
 */
@Component({
  selector: 'app-design',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule],
  templateUrl: './design.html',
  styleUrl: './design.css',
  // Any click that isn't inside the Export menu dismisses it (the menu itself
  // stops propagation), matching every other menu in the app.
  host: { '(document:click)': 'exportOpen.set(false)' },
})
export class Design {
  private readonly api = inject(CompassApiService);
  private readonly sanitizer = inject(DomSanitizer);

  readonly templates = signal<DesignTemplate[]>([]);
  readonly projects = signal<DesignProject[]>([]);
  readonly systems = signal<DesignSystem[]>([]);
  readonly tab = signal<Tab>('projects');
  readonly layout = signal<Layout>('list');
  readonly query = signal('');
  readonly loading = signal(true);

  // -- landing composer
  readonly prompt = signal('');
  readonly template = signal('blank');
  readonly system = signal(''); // design-system id, '' = none
  readonly creating = signal(false);
  readonly error = signal('');

  // -- workspace (a project is open)
  readonly open = signal<DesignProject | null>(null);
  readonly turns = signal<DesignTurn[]>([]);
  readonly refine = signal('');
  readonly working = signal(false);
  readonly device = signal<'desktop' | 'tablet' | 'mobile'>('desktop');
  readonly codeOpen = signal(false);
  readonly exportOpen = signal(false);

  // -- design-system import
  readonly importOpen = signal(false);
  readonly importName = signal('');
  readonly importText = signal('');
  readonly importCss = signal('');
  readonly importing = signal(false);

  /** Export formats, in the order the menu lists them. */
  readonly formats = [
    { id: 'html', label: 'HTML', hint: 'the document itself' },
    { id: 'pdf', label: 'PDF', hint: 'printed at full height' },
    { id: 'png', label: 'PNG', hint: 'a full-page image' },
    { id: 'zip', label: 'ZIP', hint: 'with a README' },
    { id: 'pptx', label: 'PowerPoint', hint: 'each slide, rendered' },
  ];

  /** srcdoc for the canvas iframe. Bypassing here is safe in the same sense the
   *  artifact panel is: the document is model-authored and sandboxed. */
  readonly canvas = computed<SafeHtml>(() =>
    this.sanitizer.bypassSecurityTrustHtml(this.open()?.html ?? ''),
  );

  /** The width the design is laid out at, regardless of how wide the pane is —
   *  a design built for 1280px must be previewed at 1280px or it reflows into
   *  something the export won't match. The stage scales it to fit instead. */
  readonly canvasWidth = computed(() =>
    this.device() === 'mobile' ? 390 : this.device() === 'tablet' ? 834 : 1280,
  );

  private readonly stage = viewChild<ElementRef<HTMLElement>>('stage');
  private readonly stageBox = signal({ w: 0, h: 0 });

  /** Scale that fits the laid-out width into the stage. Never above 1 — a
   *  design is shown at most life-size, the way a canvas zooms to fit. */
  readonly scale = computed(() => {
    const box = this.stageBox();
    if (!box.w) return 1;
    return Math.min(1, box.w / this.canvasWidth());
  });

  /** The iframe is scaled, so it needs to be taller than the stage by exactly
   *  the scale factor for the visible area to still fill it. */
  readonly frameHeight = computed(() => {
    const box = this.stageBox();
    return box.h ? box.h / this.scale() : 0;
  });

  readonly filtered = computed(() => {
    const q = this.query().trim().toLowerCase();
    const rows = this.projects();
    if (!q) return rows;
    return rows.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.prompt.toLowerCase().includes(q) ||
        p.template.toLowerCase().includes(q),
    );
  });

  readonly filteredTemplates = computed(() => {
    const q = this.query().trim().toLowerCase();
    const rows = this.templates();
    if (!q || this.tab() !== 'templates') return rows;
    return rows.filter(
      (t) => t.name.toLowerCase().includes(q) || t.hint.toLowerCase().includes(q),
    );
  });

  constructor() {
    void this.load();
    // Track the stage so the fit recomputes when the pane or window resizes.
    effect((onCleanup) => {
      const host = this.stage()?.nativeElement;
      if (!host) return;
      const ro = new ResizeObserver(([entry]) => {
        const r = entry.contentRect;
        this.stageBox.set({ w: r.width, h: r.height });
      });
      ro.observe(host);
      onCleanup(() => ro.disconnect());
    });
  }

  private async load(): Promise<void> {
    this.loading.set(true);
    try {
      const [t, p, sys] = await Promise.all([
        this.api.designTemplates(),
        this.api.designProjects(),
        this.api.designSystems(),
      ]);
      this.templates.set(t.templates);
      this.projects.set(p.projects);
      this.systems.set(sys.systems);
    } catch {
      this.error.set('Could not reach the design service.');
    } finally {
      this.loading.set(false);
    }
  }

  templateName(id: string): string {
    return this.templates().find((t) => t.id === id)?.name ?? id;
  }

  systemName(id: string): string {
    return this.systems().find((s) => s.id === id)?.name ?? '';
  }

  /** Relative age, matching the rest of the app ("2h ago"). */
  age(epochSeconds: number): string {
    const secs = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (secs < 60) return 'just now';
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(epochSeconds * 1000).toLocaleDateString();
  }

  pickTemplate(id: string): void {
    this.template.set(id);
    this.tab.set('projects');
    document.getElementById('design-prompt')?.focus();
  }

  /** Enter submits; Shift+Enter is a newline — the composer convention here. */
  onComposerKey(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void this.create();
    }
  }

  /** Create a project from the landing composer, then generate it. */
  async create(): Promise<void> {
    const prompt = this.prompt().trim();
    if (!prompt || this.creating()) return;
    this.creating.set(true);
    this.error.set('');
    try {
      const project = await this.api.createDesign({
        name: this.titleFrom(prompt),
        template: this.template(),
        prompt,
        design_system: this.system(),
      });
      this.projects.update((rows) => [project, ...rows]);
      this.prompt.set('');
      this.openProject(project, prompt);
      await this.run(prompt);
    } catch {
      this.error.set('Could not create the project.');
    } finally {
      this.creating.set(false);
    }
  }

  private titleFrom(prompt: string): string {
    const first = prompt.split('\n')[0].trim();
    const short = first.length > 60 ? first.slice(0, 57).trimEnd() + '…' : first;
    return short.charAt(0).toUpperCase() + short.slice(1);
  }

  openProject(project: DesignProject, firstTurn = ''): void {
    this.open.set(project);
    this.turns.set(firstTurn ? [{ role: 'user', text: firstTurn }] : []);
    this.codeOpen.set(false);
    if (!firstTurn) void this.hydrate(project.id);
  }

  /** The list endpoint omits `html` (designs are large) — fetch the full row. */
  private async hydrate(id: string): Promise<void> {
    try {
      const full = await this.api.designProject(id);
      this.open.set(full);
      this.turns.set(
        full.turns?.length ? full.turns : full.prompt ? [{ role: 'user', text: full.prompt }] : [],
      );
    } catch {
      this.error.set('Could not load that design.');
    }
  }

  close(): void {
    this.open.set(null);
    void this.load();
  }

  /** Send a refinement from the workspace composer. */
  async sendRefine(): Promise<void> {
    const text = this.refine().trim();
    if (!text || this.working()) return;
    this.refine.set('');
    this.turns.update((t) => [...t, { role: 'user', text }]);
    await this.run(text);
  }

  onRefineKey(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void this.sendRefine();
    }
  }

  /** Generate (or refine) — the backend decides which by whether html exists. */
  private async run(prompt: string): Promise<void> {
    const project = this.open();
    if (!project) return;
    this.working.set(true);
    this.error.set('');
    try {
      const updated = await this.api.generateDesign(project.id, prompt);
      this.open.set(updated);
      this.projects.update((rows) =>
        rows.map((r) =>
          r.id === updated.id ? { ...r, ...updated, html: undefined, turns: undefined } : r,
        ),
      );
      // The server owns the transcript, so the panel matches what a reload shows.
      this.turns.set(updated.turns ?? []);
    } catch {
      this.error.set('Generation failed. Try again, or reword the prompt.');
      this.turns.update((t) => [
        ...t,
        { role: 'assistant', text: 'That one failed. Try rewording the request.' },
      ]);
    } finally {
      this.working.set(false);
    }
  }

  async star(project: DesignProject, event: Event): Promise<void> {
    event.stopPropagation();
    const starred = !project.starred;
    this.projects.update((rows) =>
      rows.map((r) => (r.id === project.id ? { ...r, starred } : r)),
    );
    await this.api.patchDesign(project.id, { starred });
  }

  async remove(project: DesignProject, event: Event): Promise<void> {
    event.stopPropagation();
    this.projects.update((rows) => rows.filter((r) => r.id !== project.id));
    await this.api.deleteDesign(project.id);
  }

  /** Export in one of `formats`. The server renders it, so the file matches
   *  what the canvas shows; the browser fetches it directly so it downloads. */
  exportAs(format: string): void {
    const project = this.open();
    this.exportOpen.set(false);
    if (!project?.html) return;
    const a = document.createElement('a');
    a.href = this.api.designExportUrl(project.id, format);
    a.rel = 'noopener';
    a.click();
  }

  /** Attach (or clear) a design system on the open project. It applies from the
   *  next refinement — the current design isn't rewritten behind the user. */
  async setSystem(systemId: string): Promise<void> {
    const project = this.open();
    if (!project) return;
    this.open.set({ ...project, design_system: systemId });
    await this.api.patchDesign(project.id, { design_system: systemId });
  }

  async importSystem(): Promise<void> {
    const text = this.importText().trim();
    const css = this.importCss().trim();
    if ((!text && !css) || this.importing()) return;
    this.importing.set(true);
    this.error.set('');
    try {
      const created = await this.api.createDesignSystem({
        name: this.importName().trim(),
        source: 'pasted',
        text,
        css,
      });
      this.systems.update((rows) => [created, ...rows]);
      this.importOpen.set(false);
      this.importName.set('');
      this.importText.set('');
      this.importCss.set('');
    } catch {
      this.error.set('Could not read that into a design system.');
    } finally {
      this.importing.set(false);
    }
  }

  async removeSystem(system: DesignSystem, event: Event): Promise<void> {
    event.stopPropagation();
    this.systems.update((rows) => rows.filter((r) => r.id !== system.id));
    if (this.system() === system.id) this.system.set('');
    await this.api.deleteDesignSystem(system.id);
  }

  /** Open the design on its own, so it can be printed to PDF from the browser. */
  openStandalone(): void {
    const html = this.open()?.html;
    if (!html) return;
    const win = window.open('', '_blank');
    if (!win) return;
    win.document.write(html);
    win.document.close();
  }

  async copyCode(): Promise<void> {
    const html = this.open()?.html;
    if (html) await navigator.clipboard.writeText(html);
  }
}
