import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { DomSanitizer, SafeHtml, SafeResourceUrl } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';
import { CompassApiService } from '../compass-api.service';
import {
  DesignClarify,
  DesignClarifyField,
  DesignComment,
  DesignFile,
  DesignPage,
  DesignProject,
  DesignSection,
  DesignSystem,
  DesignSystemDoc,
  DesignTemplate,
  DesignTurn,
  DesignTweak,
  DesignVersion,
  Workspace,
} from '../models';
import {
  EDITOR_SCRIPT,
  EditorCommand,
  EditorDetails,
  EditorEvent,
  EditorRect,
} from './design-editor';

type Tab = 'projects' | 'systems' | 'templates';
type Layout = 'list' | 'grid';
type Tool = 'view' | 'inspect' | 'comment' | 'edit';
type ImportStep = '' | 'choose' | 'here' | 'code';
type ImportSource = 'paste' | 'url' | 'repo' | 'upload';

/** Zoom stops the % control cycles through, on top of Fit. */
const ZOOMS = [0.5, 0.75, 1, 1.25, 1.5];

/** How many designs the landing lists before deferring to the full history.
 *  The landing is for starting work, not for browsing everything you own. */
const LANDING_ROWS = 4;

/**
 * Design — the port of Claude Design. Two screens live here:
 *
 *   landing   hero prompt + template picker, then the library
 *             (Projects / Design systems / Templates)
 *   workspace the design conversation on the left, the design on the right,
 *             editable in place
 *
 * A design is a standalone HTML document. The canvas is a sandboxed iframe fed
 * by srcdoc; direct manipulation happens through an editor agent injected into
 * the preview copy only (see design-editor.ts), so what is stored and exported
 * never carries the editor.
 */
@Component({
  selector: 'app-design',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule],
  templateUrl: './design.html',
  styleUrl: './design.css',
  host: { '(document:click)': 'closeMenus()' },
})
export class Design {
  private readonly api = inject(CompassApiService);
  private readonly sanitizer = inject(DomSanitizer);

  readonly templates = signal<DesignTemplate[]>([]);
  readonly projects = signal<DesignProject[]>([]);
  readonly systems = signal<DesignSystem[]>([]);
  readonly included = signal<DesignSystem[]>([]);
  readonly models = signal<string[]>([]);
  readonly tab = signal<Tab>('projects');
  readonly layout = signal<Layout>('list');
  readonly query = signal('');
  readonly favouritesOnly = signal(false);
  readonly loading = signal(true);
  readonly error = signal('');

  // -- landing composer
  readonly prompt = signal('');
  readonly template = signal('blank');
  /** The systems a new design will follow. More than one is allowed — a brand
   *  plus a product system, say; the first leads. */
  readonly chosenSystems = signal<string[]>([]);
  readonly system = computed(() => this.chosenSystems()[0] ?? '');
  readonly systemPickerOpen = signal(false);
  readonly systemPickerMulti = signal(false);
  readonly systemQuery = signal('');
  readonly workspaces = signal<Workspace[]>([]);
  readonly codebase = signal('');
  readonly model = signal('');
  readonly templatesOpen = signal(true);
  readonly creating = signal(false);

  // -- workspace
  readonly open = signal<DesignProject | null>(null);
  readonly turns = signal<DesignTurn[]>([]);
  readonly refine = signal('');
  readonly working = signal(false);
  readonly device = signal<'desktop' | 'tablet' | 'mobile'>('desktop');
  readonly chatOpen = signal(true);
  readonly codeOpen = signal(false);
  // A design opens as a design — a page you look at. Editing is a tool you
  // pick, not the state you land in.
  readonly tool = signal<Tool>('view');
  readonly selection = signal('');
  readonly selectionRect = signal<EditorRect | null>(null);
  readonly details = signal<EditorDetails | null>(null);
  readonly typing = signal(false);
  /** Did the canvas agent start? A design whose own script breaks the page can
   *  take the tools down with it, and silence there is indistinguishable from
   *  "nothing selected" — so say so instead. */
  readonly toolsLive = signal(false);
  readonly zoom = signal<'fit' | number>('fit');

  // -- menus and panels
  readonly exportOpen = signal(false);
  readonly presentOpen = signal(false);
  readonly projectMenuOpen = signal(false);
  readonly rowMenuId = signal('');
  readonly historyOpen = signal(false);
  readonly versions = signal<DesignVersion[]>([]);
  readonly shareNote = signal('');

  // -- design-system import
  readonly importStep = signal<ImportStep>('');
  readonly importSource = signal<ImportSource>('paste');
  readonly importName = signal('');
  readonly importText = signal('');
  readonly importCss = signal('');
  readonly importUrl = signal('');
  readonly importWorkspace = signal('');
  readonly importPath = signal('');
  readonly importing = signal(false);

  // -- the form Compass asks when a brief is too thin to design from
  readonly clarify = signal<DesignClarify | null>(null);
  /** The brief the form is standing in for, kept while the project waits. */
  readonly askedFor = signal('');
  readonly clarifyAnswers = signal<Record<string, string | string[]>>({});
  /** Answers from earlier rounds — a follow-up replaces the form, not the brief. */
  private readonly priorAnswers = signal<string[]>([]);
  private readonly subjectSoFar = signal('');
  readonly checking = signal(false);

  // -- the composer's own menus
  readonly attachOpen = signal(false);
  readonly codebaseOpen = signal(false);
  readonly githubRepo = signal('');
  readonly cloning = signal(false);
  readonly contextFiles = signal<Array<{ name: string; text: string }>>([]);
  readonly referenced = signal<string[]>([]);   // other designs used as reference
  readonly attachNote = signal('');

  // -- pages and the project's own files
  readonly pages = signal<DesignPage[]>([]);
  readonly activePage = signal('');
  readonly filesOpen = signal(false);
  readonly filesPath = signal('');
  readonly folders = signal<DesignFile[]>([]);
  readonly files = signal<DesignFile[]>([]);
  readonly previewFile = signal<DesignFile | null>(null);
  readonly previewText = signal('');
  readonly dropping = signal(false);

  // -- tweaks: the knobs a design declares, applied live
  readonly tweaksOpen = signal(false);
  readonly tweaks = signal<DesignTweak[]>([]);

  // -- the set-up form
  readonly setupOpen = signal(false);
  readonly setupName = signal('');
  readonly setupBlurb = signal('');
  readonly setupGithub = signal('');
  readonly setupWorkspace = signal('');
  readonly setupPath = signal('');
  readonly setupNotes = signal('');
  readonly setupCss = signal('');
  readonly setupFiles = signal<Array<{ name: string; text: string }>>([]);
  readonly setupImages = signal<string[]>([]);
  readonly setupFig = signal('');
  readonly setupBusy = signal(false);

  // -- a system opened as a project
  readonly openSystem = signal<DesignSystem | null>(null);
  readonly doc = signal<DesignSystemDoc | null>(null);
  readonly docLoading = signal(false);
  readonly railOpen = signal(true);
  readonly docSearch = signal('');
  readonly docSearchOpen = signal(false);
  readonly systemMenuOpen = signal(false);
  readonly filesMenuOpen = signal(false);
  readonly openFile = signal('');           // '' = no file open
  readonly openFileText = signal('');
  readonly usageFor = signal('');           // the section whose note is being edited
  readonly usageDraft = signal('');
  /** Images attached to the next prompt — the Screenshot context path. */
  readonly shots = signal<string[]>([]);

  /** The files a system ships, listed in its top-bar dropdown. */
  readonly systemFiles = ['styles.css', 'readme.md', 'theme.json'];

  readonly tools = [
    { id: 'view', label: '', hint: 'View — the design as a page' },
    { id: 'inspect', label: 'Inspect', hint: 'Click an element to see how it is built' },
    { id: 'comment', label: 'Comment', hint: 'Leave a pin on the design' },
    { id: 'edit', label: 'Edit', hint: 'Move, resize, retype and delete' },
  ] as const;

  readonly formats = [
    { id: 'html', label: 'HTML', hint: 'the document itself', mime: 'text/html' },
    { id: 'pdf', label: 'PDF', hint: 'printed at full height', mime: 'application/pdf' },
    { id: 'png', label: 'PNG', hint: 'a full-page image', mime: 'image/png' },
    { id: 'zip', label: 'ZIP', hint: 'with a README', mime: 'application/zip' },
    {
      id: 'pptx',
      label: 'PowerPoint',
      hint: 'each slide, rendered',
      mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    },
  ];

  /** The pending export: which format, and what to call the file. */
  readonly exportAsk = signal<{
    format: string;
    label: string;
    mime: string;
    name: string;
    url: string;
  } | null>(null);

  /** Whether this browser can offer a folder as well as a name. */
  readonly canChooseFolder = signal('showSaveFilePicker' in window);

  private readonly host: ElementRef<HTMLElement> = inject(ElementRef);
  private readonly injector = inject(Injector);
  private readonly frame = viewChild<ElementRef<HTMLIFrameElement>>('frame');
  private readonly stage = viewChild<ElementRef<HTMLElement>>('stage');
  private readonly stageBox = signal({ w: 0, h: 0 });
  private saveTimer: ReturnType<typeof setTimeout> | null = null;

  // The last document handed to the iframe, and the SafeHtml wrapping it.
  // Identical html must yield the identical object: a new one rebinds srcdoc,
  // which reloads the frame — losing the selection and any in-flight edit.
  private lastHtml = ' ';
  private lastCanvas: SafeHtml = '';

  /** The preview document: the design plus the editor agent. The agent is
   *  appended here and nowhere else, so it can never reach the store. */
  readonly canvas = computed<SafeHtml>(() => {
    const html = this.open()?.html ?? '';
    if (html === this.lastHtml) return this.lastCanvas;
    this.lastHtml = html;
    const script = `<script data-dz="1">${EDITOR_SCRIPT}<\/script>`;
    const withAgent = !html
      ? ''
      : html.includes('</body>')
        ? html.replace('</body>', `${script}</body>`)
        : html + script;
    this.lastCanvas = this.sanitizer.bypassSecurityTrustHtml(withAgent);
    return this.lastCanvas;
  });

  /** The width the design is laid out at, whatever the pane is — a design built
   *  for 1280px must be previewed at 1280px or it reflows into something the
   *  export won't match. */
  readonly canvasWidth = computed(() =>
    this.device() === 'mobile' ? 390 : this.device() === 'tablet' ? 834 : 1280,
  );

  readonly fitScale = computed(() => {
    const box = this.stageBox();
    return box.w ? Math.min(1, box.w / this.canvasWidth()) : 1;
  });

  readonly scale = computed(() => {
    const z = this.zoom();
    return z === 'fit' ? this.fitScale() : z;
  });

  readonly zoomLabel = computed(() => Math.round(this.scale() * 100) + '%');

  readonly frameHeight = computed(() => {
    const box = this.stageBox();
    return box.h ? box.h / this.scale() : 0;
  });

  /** Measure the canvas stage. Cheap, idempotent, and safe to call often. */
  measureStage(): void {
    const stage = this.host.nativeElement.querySelector<HTMLElement>('.dz-stage');
    if (!stage) return;
    const r = stage.getBoundingClientRect();
    const box = this.stageBox();
    // The padding is the stage's own; the canvas gets what is left.
    const w = Math.max(0, r.width - 36);
    const h = Math.max(0, r.height - 36);
    if (Math.abs(box.w - w) > 0.5 || Math.abs(box.h - h) > 0.5) {
      this.stageBox.set({ w, h });
    }
  }

  /** Where to float the edit toolbar: over the selection, in the panel's
   *  coordinates. The frame's box is scaled, so the rect scales with it. */
  readonly toolbarAt = computed(() => {
    const rect = this.selectionRect();
    const frame = this.frame()?.nativeElement;
    const stage = this.host.nativeElement.querySelector<HTMLElement>('.dz-stage');
    if (!rect || !frame || !stage) return null;
    const f = frame.getBoundingClientRect();
    const s = stage.getBoundingClientRect();
    const scale = this.scale() || 1;
    const left = f.left - s.left + rect.x * scale;
    const top = f.top - s.top + rect.y * scale;
    return {
      left: Math.max(6, Math.min(left, s.width - 220)),
      // Above the selection when there's room, below it when there isn't.
      top: top > 46 ? top - 42 : top + rect.h * scale + 8,
      svg: rect.svg,
    };
  });

  /** The laptop shown while a design is being made. It opens on arrival and
   *  closes when clicked — the lid is a CSS 3D rotation, not a video. */
  readonly lidOpen = signal(true);

  toggleLid(): void {
    this.lidOpen.set(!this.lidOpen());
  }

  readonly comments = computed(() => this.open()?.comments ?? []);

  readonly filtered = computed(() => {
    const q = this.query().trim().toLowerCase();
    let rows = this.projects();
    if (this.favouritesOnly()) rows = rows.filter((p) => p.starred);
    if (!q) return rows;
    return rows.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.prompt.toLowerCase().includes(q) ||
        p.template.toLowerCase().includes(q),
    );
  });

  /** What the landing shows: the most recent few. */
  readonly recent = computed(() => this.filtered().slice(0, LANDING_ROWS));
  readonly hiddenCount = computed(() => Math.max(0, this.filtered().length - LANDING_ROWS));
  readonly historyOpenAll = signal(false);

  readonly filteredTemplates = computed(() => {
    const q = this.query().trim().toLowerCase();
    const rows = this.templates();
    if (!q || this.tab() !== 'templates') return rows;
    return rows.filter(
      (t) => t.name.toLowerCase().includes(q) || t.hint.toLowerCase().includes(q),
    );
  });

  /** Every system that can be attached — the user's own first. */
  readonly allSystems = computed(() => [...this.systems(), ...this.included()]);

  constructor() {
    void this.load();

    // Refit when anything changes the stage's box. The observer watches the
    // component's own element, which is always there — watching the stage
    // itself meant nothing was measured on the first render, when the query
    // hadn't resolved yet, and the canvas came out zero-height.
    effect((onCleanup) => {
      const ro = new ResizeObserver(() => this.measureStage());
      ro.observe(this.host.nativeElement);
      const onResize = () => this.measureStage();
      window.addEventListener('resize', onResize);
      onCleanup(() => {
        ro.disconnect();
        window.removeEventListener('resize', onResize);
      });
    });

    // ...and after any change that swaps the canvas in or out. The measurement
    // waits for the render rather than a frame: the stage is created by the
    // same change that triggers this, so measuring before it exists is how the
    // canvas ended up zero-height.
    effect(() => {
      this.open();
      this.chatOpen();
      this.codeOpen();
      this.device();
      this.tool();
      afterNextRender(() => this.measureStage(), { injector: this.injector });
    });

    // Listen to the editor agent. The source window is the check — a sandboxed
    // frame has an opaque origin, so the origin can't be matched.
    effect((onCleanup) => {
      const onMessage = (event: MessageEvent<EditorEvent>) => {
        const el = this.frame()?.nativeElement;
        if (!el || event.source !== el.contentWindow) return;
        this.onEditorEvent(event.data);
      };
      window.addEventListener('message', onMessage);
      onCleanup(() => window.removeEventListener('message', onMessage));
    });

    // Keep the agent's mode and pins in step with the panel.
    effect(() => {
      const mode = this.tool();
      this.open();
      queueMicrotask(() => this.toAgent({ dz: 'mode', mode }));
    });
    effect(() => {
      const pins = this.comments();
      queueMicrotask(() =>
        this.toAgent({
          dz: 'pins',
          pins: pins.map((c) => ({ id: c.id, x: c.x, y: c.y, text: c.text })),
        }),
      );
    });
  }

  // ===================== loading =====================

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
      this.included.set(sys.included);
    } catch {
      this.error.set('Could not reach the design service.');
    } finally {
      this.loading.set(false);
    }
    void this.loadWorkspaces();
    try {
      const health = await this.api.health();
      this.models.set(health.models ?? []);
      this.model.set(health.deployment ?? '');
    } catch {
      /* the picker just stays empty */
    }
  }

  closeAllHistory(): void {
    this.historyOpenAll.set(false);
  }

  // ===================== the composer's menus =====================

  /** Text files ride along as reference; images go in as vision context. */
  async onAttach(event: Event, kind: 'file' | 'folder'): Promise<void> {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    let added = 0;
    for (const file of files.slice(0, 24)) {
      if (file.type.startsWith('image/')) {
        const dataUrl = await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.readAsDataURL(file);
        });
        this.shots.update((rows) => [...rows, dataUrl].slice(0, 4));
        added++;
        continue;
      }
      if (/\.(css|scss|less|js|ts|jsx|tsx|json|md|txt|html?|svg|csv)$/i.test(file.name)) {
        const text = await file.text();
        this.contextFiles.update((rows) =>
          [...rows, { name: file.name, text }].slice(0, 24),
        );
        added++;
      }
    }
    this.attachOpen.set(false);
    this.attachNote.set(
      added
        ? `Attached ${added} ${kind === 'folder' ? 'files from that folder' : 'file(s)'}.`
        : 'Nothing there Compass can read as context.',
    );
    setTimeout(() => this.attachNote.set(''), 4000);
    input.value = '';
  }

  referenceProject(id: string): void {
    this.referenced.update((ids) =>
      ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id],
    );
  }

  dropContext(index: number): void {
    this.contextFiles.update((rows) => rows.filter((_, i) => i !== index));
  }

  /** Clone a GitHub repo and design within it. */
  async connectGithub(): Promise<void> {
    const repo = this.githubRepo().trim();
    if (!repo || this.cloning()) return;
    this.cloning.set(true);
    this.error.set('');
    try {
      const full = repo
        .replace(/^https?:\/\/github\.com\//, '')
        .replace(/\.git$/, '')
        .replace(/\/$/, '');
      const ws = await this.api.githubClone(full);
      this.workspaces.update((rows) => [ws, ...rows.filter((w) => w.id !== ws.id)]);
      this.codebase.set(ws.id);
      this.githubRepo.set('');
      this.codebaseOpen.set(false);
    } catch (err) {
      this.error.set(`Could not connect that repository: ${(err as Error).message}`);
    } finally {
      this.cloning.set(false);
    }
  }

  systemNameOfWorkspace(id: string): string {
    return this.workspaces().find((w) => w.id === id)?.name ?? 'No codebase selected';
  }

  openCodebase(): void {
    this.codebaseOpen.set(true);
    this.attachOpen.set(false);
    void this.loadWorkspaces();
  }

  closeMenus(): void {
    this.exportOpen.set(false);
    this.presentOpen.set(false);
    this.projectMenuOpen.set(false);
    this.rowMenuId.set('');
    this.systemMenuOpen.set(false);
    this.filesMenuOpen.set(false);
    this.systemPickerOpen.set(false);
    this.tweaksOpen.set(false);
    this.attachOpen.set(false);
    this.codebaseOpen.set(false);
  }

  templateName(id: string): string {
    return this.templates().find((t) => t.id === id)?.name ?? id;
  }

  systemName(id: string): string {
    return this.allSystems().find((s) => s.id === id)?.name ?? '';
  }

  /** What the picker's field reads: None, the system, or "N systems". */
  readonly systemLabel = computed(() => {
    const ids = this.chosenSystems();
    if (!ids.length) return 'None';
    if (ids.length === 1) return this.systemName(ids[0]) || 'None';
    return `${ids.length} systems`;
  });

  readonly pickerSystems = computed(() => {
    const q = this.systemQuery().trim().toLowerCase();
    const rows = this.allSystems();
    if (!q) return rows;
    return rows.filter(
      (s) => s.name.toLowerCase().includes(q) || (s.fonts ?? '').toLowerCase().includes(q),
    );
  });

  toggleSystem(id: string): void {
    if (this.systemPickerMulti()) {
      this.chosenSystems.update((ids) =>
        ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id],
      );
      return;
    }
    this.chosenSystems.set(this.chosenSystems()[0] === id ? [] : [id]);
    this.systemPickerOpen.set(false);
  }

  clearSystems(): void {
    this.chosenSystems.set([]);
    this.systemPickerOpen.set(false);
  }

  /** What the laptop shows while it works: the design as it stands, the way
   *  claude.ai keeps the current version on the screen. Nothing yet, nothing
   *  to show — the skeleton stands in. */
  lastShot(): string {
    const project = this.open();
    return project && project.html ? this.thumb(project) : '';
  }

  thumb(p: DesignProject): string {
    return this.api.designThumbUrl(p.id, p.updated_at);
  }

  /** Projects whose thumbnail did not load — a design that has not rendered
   *  yet has none, and a broken image is worse than an honest placeholder. */
  private readonly noThumb = signal<ReadonlySet<string>>(new Set());

  hasThumb(p: DesignProject): boolean {
    if (this.noThumb().has(p.id) || p.empty || p.awaiting) return false;
    // A full record carries the design itself; a table row carries the flags.
    return p.html === undefined || !!p.html.trim();
  }

  thumbFailed(p: DesignProject): void {
    this.noThumb.update((ids) => new Set(ids).add(p.id));
  }

  // ===================== creating =====================

  /** Picking a template opens the sentence for you — claude.ai seeds the
   *  composer with the template's own words and leaves the caret at the end. */
  pickTemplate(id: string): void {
    this.template.set(id);
    const stem = this.templates().find((t) => t.id === id)?.stem ?? '';
    const current = this.prompt();
    const previous = this.templates().find((t) => this.prompt().startsWith(t.stem ?? '\u0000'));
    if (!current.trim() || previous) this.prompt.set(stem);
    queueMicrotask(() => {
      const box = document.getElementById('design-prompt') as HTMLTextAreaElement | null;
      box?.focus();
      box?.setSelectionRange(box.value.length, box.value.length);
    });
  }

  onComposerKey(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void this.create();
    }
  }

  /** Before designing, check the brief is one. A thin brief opens the project
   *  and asks on the canvas rather than guessing — the guess is the thing that
   *  wastes the minute. */
  async create(): Promise<void> {
    const prompt = this.prompt().trim();
    if (!prompt || this.creating() || this.checking()) return;
    // Whether to ask is decided here, not after a round trip: a template's own
    // opening words with nothing added is the case worth asking about, and
    // waiting on the landing page for that answer is the thing claude.ai
    // does not do. Open the project, then think inside it.
    if (this.thinBrief(prompt)) {
      await this.askInProject(prompt);
      return;
    }
    await this.startDesign(prompt);
  }

  /** Is this brief still just the template's opening words? Mirrors the rule
   *  the clarify endpoint applies, so the two never disagree about who asks. */
  private thinBrief(prompt: string, template = this.template()): boolean {
    const stem = (this.templates().find((t) => t.id === template)?.stem ?? '').trim();
    const rest = stem && prompt.startsWith(stem) ? prompt.slice(stem.length) : prompt;
    return rest.trim().length < 25;
  }

  /** The project's name while the question is still being written. */
  private requestName(): string {
    const name = this.templateName(this.template());
    return name && this.template() !== 'blank' ? `${name} request` : 'New request';
  }

  /** Open the project first and ask from inside it — the wait belongs on the
   *  canvas, next to the brief, not on the landing page. */
  private async askInProject(prompt: string): Promise<void> {
    this.checking.set(true);
    this.error.set('');
    let project: DesignProject;
    try {
      project = await this.api.createDesign({
        name: this.requestName(),
        template: this.template(),
        prompt,
        design_systems: this.chosenSystems(),
      });
    } catch {
      this.checking.set(false);
      this.error.set('Could not start that design.');
      return;
    }

    this.projects.update((rows) => [project, ...rows]);
    this.askedFor.set(prompt);
    this.prompt.set('');
    this.priorAnswers.set([]);
    this.subjectSoFar.set('');
    this.lidOpen.set(true);
    // Passing the prompt keeps openProject from hydrating turns off the
    // server, which would land after this and wipe the question.
    this.openProject(project, prompt);
    this.turns.set([
      { role: 'user', text: prompt, template: this.templateName(project.template) },
    ]);

    let form: DesignClarify | null = null;
    try {
      form = await this.api.clarifyDesign(prompt, project.template);
    } catch {
      /* if the check itself fails, get on with the design */
    }
    this.checking.set(false);

    if (form && !form.ready && form.fields?.length) {
      await this.showForm(project, form);
      return;
    }
    await this.renameOpen(project, this.titleFrom(prompt));
    await this.run(this.withContext(prompt));
  }

  /** Put the form on the canvas, and keep it on the project so that closing
   *  the tab and coming back shows the question rather than an empty stage. */
  private async showForm(project: DesignProject, form: DesignClarify): Promise<void> {
    this.seedAnswers(form);
    const turns: DesignTurn[] = [
      {
        role: 'user',
        text: this.askedFor(),
        template: this.templateName(project.template),
      },
      { role: 'assistant', steps: ['Ask user'], text: form.note || 'Waiting on the form.' },
    ];
    this.turns.set(turns);
    this.clarify.set(form);
    await this.saveWaiting(project, form, turns, form.waiting || project.name);
  }

  /** Store the waiting state: the name, the transcript so far, and the form. */
  private async saveWaiting(
    project: DesignProject,
    form: DesignClarify | Record<string, never>,
    turns: DesignTurn[],
    name: string,
  ): Promise<void> {
    try {
      const updated = await this.api.patchDesign(project.id, { name, turns, clarify: form });
      this.open.set(updated);
      this.projects.update((rows) =>
        rows.map((r) => (r.id === updated.id ? { ...r, ...updated, html: undefined } : r)),
      );
    } catch {
      /* the form is on screen either way */
    }
  }

  /** Rename the project everywhere it shows: header, table row, and store. */
  private async renameOpen(project: DesignProject, name: string): Promise<void> {
    await this.api.patchDesign(project.id, { name }).catch(() => undefined);
    this.open.set({ ...project, name });
    this.projects.update((rows) =>
      rows.map((r) => (r.id === project.id ? { ...r, name } : r)),
    );
  }

  private seedAnswers(form: DesignClarify): void {
    const seeded: Record<string, string | string[]> = {};
    for (const f of form.fields ?? []) {
      seeded[f.id] = f.type === 'checkbox' ? [] : f.value ?? '';
    }
    this.clarifyAnswers.set(seeded);
  }

  /** Every answer so far, as lines, and the subject on its own. Earlier rounds
   *  come first: the form on screen is only the latest one. */
  private answerLines(): { subject: string; lines: string[] } {
    const form = this.clarify();
    const answers = this.clarifyAnswers();
    const lines: string[] = [...this.priorAnswers()];
    // The subject is the first field's answer and nothing else. Segmented and
    // radio fields carry defaults, so "first answer that happens to be filled"
    // would name a project after its page count.
    const first = form?.fields?.[0];
    let subject = this.subjectSoFar();
    for (const field of form?.fields ?? []) {
      const value = answers[field.id];
      const text = Array.isArray(value) ? value.join(', ') : (value ?? '').toString();
      if (!text.trim()) continue;
      if (!subject && field.id === first?.id) subject = text.trim();
      lines.push(`${field.label}: ${text.trim()}`);
    }
    return { subject, lines };
  }

  /** The asked-for prompt, ready for the answer to be appended to it — the
   *  template's stem keeps its trailing space, which trimming eats. */
  private opener(): string {
    const stem = this.askedFor();
    return stem && !/\s$/.test(stem) ? `${stem} ` : stem;
  }

  /** Send answer — fold the form into the brief and design from it. */
  async submitClarify(): Promise<void> {
    const project = this.open();
    const { subject, lines } = this.answerLines();
    if (!project || !subject) return;
    const brief = `${this.opener()}${subject}\n\n${lines.join('\n')}`;
    this.clarify.set(null);
    await this.renameOpen(project, this.titleFrom(subject));
    await this.api.patchDesign(project.id, { clarify: {} }).catch(() => undefined);
    this.turns.update((t) => [...t, { role: 'user', text: lines.join('\n') }]);
    await this.run(this.withContext(brief));
  }

  /** Decide for me — no more questions; the design makes the calls. */
  async decideForMe(): Promise<void> {
    const project = this.open();
    if (!project) return;
    const { subject, lines } = this.answerLines();
    this.clarify.set(null);
    const brief =
      `${this.opener()}${subject}\n\n` +
      (lines.length ? lines.join('\n') + '\n\n' : '') +
      'Choose anything not specified yourself, and say what you chose.';
    // No subject given: the template's own name beats a bare opening line.
    await this.renameOpen(
      project,
      subject
        ? this.titleFrom(subject)
        : this.templateName(project.template) || this.titleFrom(this.askedFor()),
    );
    await this.api.patchDesign(project.id, { clarify: {} }).catch(() => undefined);
    this.turns.update((t) => [
      ...t,
      {
        role: 'user',
        text: lines.length
          ? `${lines.join('\n')}\n\nDecide the rest for me.`
          : 'Decide for me.',
      },
    ]);
    await this.run(this.withContext(brief));
  }

  /** Ask me follow-up questions — another round, informed by this one. */
  async askFollowup(): Promise<void> {
    if (this.checking()) return;
    this.checking.set(true);
    try {
      const { subject, lines } = this.answerLines();
      const next = await this.api.clarifyDesign(this.askedFor(), this.template(), {
        answers: lines.join('\n'),
        followup: true,
      });
      if (next.ready || !next.fields?.length) {
        this.turns.update((t) => [
          ...t,
          { role: 'assistant', text: 'Nothing else worth asking — send when you are ready.' },
        ]);
        return;
      }
      // The next form replaces this one, so bank what it answered first.
      this.subjectSoFar.set(subject);
      this.priorAnswers.set(lines);
      this.seedAnswers(next);
      this.clarify.set(next);
      this.turns.update((t) => [
        ...t,
        { role: 'assistant', steps: ['Ask user'], text: next.note || 'A few more questions.' },
      ]);
      const project = this.open();
      if (project) {
        await this.saveWaiting(project, next, this.turns(), next.waiting || project.name);
      }
    } catch {
      this.error.set('Could not think of follow-up questions.');
    } finally {
      this.checking.set(false);
    }
  }

  setAnswer(field: DesignClarifyField, value: string): void {
    this.clarifyAnswers.update((a) => ({ ...a, [field.id]: value }));
  }

  toggleAnswer(field: DesignClarifyField, option: string): void {
    this.clarifyAnswers.update((a) => {
      const current = Array.isArray(a[field.id]) ? (a[field.id] as string[]) : [];
      return {
        ...a,
        [field.id]: current.includes(option)
          ? current.filter((o) => o !== option)
          : [...current, option],
      };
    });
  }

  answerHas(field: DesignClarifyField, option: string): boolean {
    const value = this.clarifyAnswers()[field.id];
    return Array.isArray(value) ? value.includes(option) : value === option;
  }

  answerText(field: DesignClarifyField): string {
    const value = this.clarifyAnswers()[field.id];
    return Array.isArray(value) ? value.join(', ') : (value ?? '').toString();
  }

  readonly clarifyReady = computed(() => {
    const form = this.clarify();
    if (!form?.fields?.length) return false;
    // A later round no longer carries the subject field — by then it is banked.
    if (this.subjectSoFar().trim()) return true;
    return this.answerText(form.fields[0]).trim().length > 0;
  });

  private async startDesign(prompt: string): Promise<void> {
    if (this.creating()) return;
    this.creating.set(true);
    this.error.set('');
    try {
      const project = await this.api.createDesign({
        name: this.titleFrom(prompt),
        template: this.template(),
        prompt,
        design_systems: this.chosenSystems(),
      });
      this.projects.update((rows) => [project, ...rows]);
      this.prompt.set('');
      this.openProject(project, prompt);
      await this.run(this.withContext(prompt));
    } catch {
      this.error.set('Could not create the project.');
    } finally {
      this.creating.set(false);
    }
  }

  /** Whatever was attached rides along with the brief. */
  private withContext(prompt: string): string {
    const parts = [prompt];
    for (const file of this.contextFiles()) {
      parts.push(`Reference — ${file.name}:\n${file.text.slice(0, 12_000)}`);
    }
    for (const id of this.referenced()) {
      const other = this.projects().find((p) => p.id === id);
      if (other) parts.push(`Match the look of an earlier design: “${other.name}”.`);
    }
    if (this.codebase()) {
      const ws = this.workspaces().find((w) => w.id === this.codebase());
      if (ws) parts.push(`Design to fit the codebase “${ws.name}”.`);
    }
    return parts.join('\n\n');
  }

  private titleFrom(prompt: string): string {
    const first = prompt.split('\n')[0].trim();
    const short = first.length > 60 ? first.slice(0, 57).trimEnd() + '…' : first;
    return short.charAt(0).toUpperCase() + short.slice(1);
  }

  // ===================== the open project =====================

  openProject(project: DesignProject, firstTurn = ''): void {
    this.open.set(project);
    this.clarify.set(null);
    this.turns.set(firstTurn ? [{ role: 'user', text: firstTurn }] : []);
    this.codeOpen.set(false);
    this.historyOpen.set(false);
    this.chatOpen.set(true);
    this.tool.set('view');
    this.zoom.set('fit');
    this.watchAgent();
    this.filesOpen.set(false);
    void this.loadPages(project.id);
    if (!firstTurn) void this.hydrate(project.id);
    void this.api.openDesign(project.id).catch(() => undefined);
  }

  // ===================== pages =====================

  private async loadPages(id: string): Promise<void> {
    try {
      const res = await this.api.designPages(id);
      this.pages.set(res.pages);
      this.activePage.set(res.active);
    } catch {
      this.pages.set([]);
    }
  }

  async newPage(): Promise<void> {
    const project = this.open();
    this.projectMenuOpen.set(false);
    if (!project) return;
    const updated = await this.api.addDesignPage(project.id);
    this.open.set(updated);
    await this.loadPages(project.id);
  }

  async openPage(page: DesignPage): Promise<void> {
    const project = this.open();
    this.projectMenuOpen.set(false);
    if (!project || page.id === this.activePage()) return;
    const updated = await this.api.openDesignPage(project.id, page.id);
    this.open.set(updated);
    this.activePage.set(page.id);
  }

  async deletePage(page: DesignPage, event: Event): Promise<void> {
    event.stopPropagation();
    const project = this.open();
    if (!project || this.pages().length < 2) return;
    if (!window.confirm(`Delete ${page.name}?`)) return;
    const updated = await this.api.deleteDesignPage(project.id, page.id);
    this.open.set(updated);
    await this.loadPages(project.id);
  }

  // ===================== the project's files =====================

  async openFiles(path = ''): Promise<void> {
    const project = this.open();
    this.projectMenuOpen.set(false);
    if (!project) return;
    this.filesOpen.set(true);
    await this.listFiles(path);
  }

  private async listFiles(path: string): Promise<void> {
    const project = this.open();
    if (!project) return;
    try {
      const res = await this.api.designFiles(project.id, path);
      this.filesPath.set(res.path);
      this.folders.set(res.folders);
      this.files.set(res.files);
    } catch {
      this.error.set('Could not read the project folder.');
    }
  }

  /** Up one level, or nowhere if already at the top. */
  async filesUp(): Promise<void> {
    const parts = this.filesPath().split('/').filter(Boolean);
    parts.pop();
    await this.listFiles(parts.join('/'));
  }

  async openFolder(folder: DesignFile): Promise<void> {
    await this.listFiles(folder.path);
  }

  async preview(file: DesignFile): Promise<void> {
    this.previewFile.set(file);
    this.previewText.set('');
    const project = this.open();
    if (!project || !file.text) return;
    try {
      this.previewText.set(await this.api.designFileText(project.id, file.path));
    } catch {
      this.previewText.set('Could not read that file.');
    }
  }

  fileUrl(file: DesignFile): string {
    const project = this.open();
    return project ? this.api.designFileUrl(project.id, file.path) : '';
  }

  isImage(file: DesignFile | null): boolean {
    return !!file && /\.(png|jpe?g|gif|webp|svg)$/i.test(file.name);
  }

  async deleteFile(file: DesignFile, event: Event): Promise<void> {
    event.stopPropagation();
    const project = this.open();
    if (!project) return;
    if (!window.confirm(`Delete ${file.name}?`)) return;
    await this.api.deleteDesignFile(project.id, file.path);
    if (this.previewFile()?.path === file.path) this.previewFile.set(null);
    await this.listFiles(this.filesPath());
  }

  /** Files dropped onto the browser land in the folder being shown. */
  async onDropFiles(event: DragEvent): Promise<void> {
    event.preventDefault();
    this.dropping.set(false);
    const project = this.open();
    if (!project) return;
    const dropped = Array.from(event.dataTransfer?.files ?? []);
    for (const file of dropped.slice(0, 20)) {
      const path = [this.filesPath(), file.name].filter(Boolean).join('/');
      try {
        if (/\.(css|js|ts|json|md|txt|html?|svg|csv)$/i.test(file.name)) {
          await this.api.writeDesignFile(project.id, { path, text: await file.text() });
          continue;
        }
        const dataUrl = await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.readAsDataURL(file);
        });
        await this.api.writeDesignFile(project.id, { path, data_url: dataUrl });
      } catch (err) {
        this.error.set(`Could not store ${file.name}: ${(err as Error).message}`);
      }
    }
    await this.listFiles(this.filesPath());
  }

  async onPickFiles(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const dt = { files: input.files } as DataTransfer;
    await this.onDropFiles({ preventDefault() {}, dataTransfer: dt } as unknown as DragEvent);
    input.value = '';
  }

  /** Keep the design being shown in the project's scraps. */
  async saveScrap(): Promise<void> {
    const project = this.open();
    if (!project?.html) return;
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    await this.api.writeDesignFile(project.id, {
      path: `scraps/sketch-${stamp}.html`,
      text: project.html,
    });
    await this.listFiles(this.filesPath());
  }

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

  private async hydrate(id: string): Promise<void> {
    try {
      const full = await this.api.designProject(id);
      this.open.set(full);
      this.turns.set(
        full.turns?.length ? full.turns : full.prompt ? [{ role: 'user', text: full.prompt }] : [],
      );
      // A project that was waiting on an answer is still waiting on it.
      if (full.clarify?.fields?.length) {
        this.askedFor.set(full.prompt || '');
        this.priorAnswers.set([]);
        this.subjectSoFar.set('');
        this.seedAnswers(full.clarify);
        this.clarify.set(full.clarify);
      } else if (
        // Nothing designed, nothing said past the opening line, and a brief
        // too thin to design from: the question was never asked, or predates
        // the store keeping it. Ask it now rather than showing a blank stage.
        !full.html?.trim() &&
        (full.turns?.length ?? 0) < 2 &&
        this.thinBrief(full.prompt || '', full.template)
      ) {
        await this.askAgain(full);
      }
    } catch {
      this.error.set('Could not load that design.');
    }
  }

  /** Put the question back on a project that has none — the same form the
   *  brief would have got when it was written. */
  private async askAgain(project: DesignProject): Promise<void> {
    this.askedFor.set(project.prompt || '');
    this.priorAnswers.set([]);
    this.subjectSoFar.set('');
    this.checking.set(true);
    this.lidOpen.set(true);
    let form: DesignClarify | null = null;
    try {
      form = await this.api.clarifyDesign(project.prompt || '', project.template);
    } catch {
      /* an unanswerable project is still openable */
    }
    this.checking.set(false);
    if (this.open()?.id !== project.id) return;   // they moved on while we asked
    if (form && !form.ready && form.fields?.length) await this.showForm(project, form);
  }

  /** Leave this design and start another — the composer, cleared and focused. */
  newDesign(): void {
    this.open.set(null);
    this.prompt.set('');
    this.template.set('blank');
    void this.load();
    setTimeout(() => document.getElementById('design-prompt')?.focus(), 60);
  }

  close(): void {
    this.clarify.set(null);
    this.open.set(null);
    this.selection.set('');
    void this.load();
  }

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

  private async run(prompt: string): Promise<void> {
    const project = this.open();
    if (!project) return;
    this.working.set(true);
    this.lidOpen.set(true);
    this.error.set('');
    try {
      const updated = await this.api.generateDesign(
        project.id, prompt, this.model(), this.shots(),
      );
      this.shots.set([]);
      this.open.set(updated);
      this.projects.update((rows) =>
        rows.map((r) => (r.id === updated.id ? { ...r, ...updated, html: undefined } : r)),
      );
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

  /** Thumbs on a reply, kept on the turn so it survives a reload. */
  async vote(index: number, vote: 'up' | 'down'): Promise<void> {
    const turns = this.turns().map((t, i) =>
      i === index ? { ...t, vote: t.vote === vote ? undefined : vote } : t,
    );
    this.turns.set(turns);
    const project = this.open();
    if (project) await this.api.patchDesign(project.id, { turns }).catch(() => undefined);
  }

  // ===================== canvas =====================

  private toAgent(command: EditorCommand): void {
    this.frame()?.nativeElement.contentWindow?.postMessage(command, '*');
  }

  private onEditorEvent(event: EditorEvent): void {
    if (event.dz === 'ready') {
      this.toolsLive.set(true);
      this.toAgent({ dz: 'mode', mode: this.tool() });
      this.toAgent({
        dz: 'pins',
        pins: this.comments().map((c) => ({ id: c.id, x: c.x, y: c.y, text: c.text })),
      });
    } else if (event.dz === 'selected') {
      this.selection.set(event.label ?? '');
      this.selectionRect.set(event.rect ?? null);
      this.details.set(event.details ?? null);
    } else if (event.dz === 'html' && event.html) {
      this.queueSave(event.html);
    } else if (event.dz === 'comment') {
      void this.addComment(event.x ?? 0, event.y ?? 0);
    } else if (event.dz === 'typing') {
      this.typing.set(true);
    } else if (event.dz === 'typed') {
      this.typing.set(false);
    } else if (event.dz === 'tweaks') {
      this.tweaks.set(event.tweaks ?? []);
    }
  }

  /** Canvas edits arrive per gesture; coalesce them so history gets one entry
   *  per pause rather than one per pixel. The open project's `html` is left
   *  alone — replacing it would reload the iframe mid-edit. */
  /** Give the agent a moment to boot, then admit it if it never did. */
  private watchAgent(): void {
    this.toolsLive.set(false);
    if (this.agentTimer) clearTimeout(this.agentTimer);
    this.agentTimer = setTimeout(() => {
      if (!this.toolsLive() && this.open()?.html) {
        this.error.set(
          'The canvas tools did not start for this design — inspect and edit are ' +
            'unavailable. Viewing, Present and Export still work.',
        );
      }
    }, 4000);
  }

  private agentTimer: ReturnType<typeof setTimeout> | null = null;

  private queueSave(html: string): void {
    const project = this.open();
    if (!project) return;
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(async () => {
      try {
        const saved = await this.api.saveDesignHtml(project.id, html);
        const current = this.open();
        if (current?.id === saved.id) {
          this.open.set({ ...current, updated_at: saved.updated_at });
        }
      } catch {
        this.error.set('Could not save that edit.');
      }
    }, 700);
  }

  /** Forward pointer input that landed on the iframe *element* instead of
   *  inside the frame. Browsers deliver clicks straight into a sandboxed frame,
   *  in which case the frame consumes them and nothing arrives here — so this
   *  only fires in embeddings that route differently, and never double-handles.
   *  Coordinates are converted into the frame's own space, undoing the scale. */
  onCanvasDblClick(event: MouseEvent): void {
    if (this.tool() !== 'edit') return;
    const el = this.frame()?.nativeElement;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const scale = this.scale() || 1;
    this.toAgent({
      dz: 'pointer',
      kind: 'dblclick',
      x: (event.clientX - rect.left) / scale,
      y: (event.clientY - rect.top) / scale,
    });
  }

  onCanvasPointer(event: PointerEvent, kind: 'down' | 'move' | 'up'): void {
    if (this.tool() === 'view') return;   // a page behaves like a page
    if (kind === 'move' && !this.dragging) return;
    const el = this.frame()?.nativeElement;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const scale = this.scale() || 1;
    const x = (event.clientX - rect.left) / scale;
    const y = (event.clientY - rect.top) / scale;
    if (kind === 'down') {
      this.dragging = true;
      this.toAgent({ dz: 'pointer', kind: 'click', x, y });
    }
    this.toAgent({ dz: 'pointer', kind, x, y });
    if (kind === 'up') this.dragging = false;
  }

  private dragging = false;

  /** Turn a knob. The canvas answers immediately; the document is saved once
   *  the value settles, so dragging through a palette doesn't write ten times. */
  setTweak(tweak: DesignTweak, value: string): void {
    this.tweaks.update((rows) =>
      rows.map((t) => (t.name === tweak.name ? { ...t, value } : t)),
    );
    this.toAgent({ dz: 'tweak', tweakVar: tweak.var, tweakValue: value });
  }

  align(how: 'left' | 'center' | 'right'): void {
    this.toAgent({ dz: 'align', align: how });
  }

  deleteSelected(): void {
    this.toAgent({ dz: 'delete' });
  }

  cycleZoom(): void {
    const z = this.zoom();
    if (z === 'fit') {
      this.zoom.set(ZOOMS[0]);
      return;
    }
    const next = ZOOMS[ZOOMS.indexOf(z) + 1];
    this.zoom.set(next ?? 'fit');
  }

  private async addComment(x: number, y: number): Promise<void> {
    const project = this.open();
    if (!project) return;
    const text = window.prompt('Comment');
    if (!text) return;
    const updated = await this.api.addDesignComment(project.id, { x, y, text });
    this.open.set({ ...project, comments: updated.comments });
  }

  async removeComment(comment: DesignComment): Promise<void> {
    const project = this.open();
    if (!project) return;
    const updated = await this.api.deleteDesignComment(project.id, comment.id);
    this.open.set({ ...project, comments: updated.comments });
  }

  /** Re-render the canvas from what is stored. */
  async reload(): Promise<void> {
    const project = this.open();
    if (project) await this.hydrate(project.id);
  }

  // ===================== present, share, export =====================

  present(where: 'window' | 'fullscreen' | 'new'): void {
    this.presentOpen.set(false);
    if (!this.open()?.html) return;
    if (where === 'new') {
      this.openStandalone();
      return;
    }
    if (where === 'fullscreen') {
      void this.stage()?.nativeElement.requestFullscreen?.();
      return;
    }
    this.chatOpen.set(false); // present in this window: the design, full width
    this.zoom.set('fit');
  }

  openStandalone(): void {
    const html = this.open()?.html;
    if (!html) return;
    const win = window.open('', '_blank');
    if (!win) return;
    win.document.write(html);
    win.document.close();
  }

  /** Share is a link back into this Compass — anyone who can reach the server
   *  and sign in opens the same design. The link grants no access by itself. */
  shareLink(project: DesignProject): string {
    return `${location.origin}/?design=${project.id}`;
  }

  async share(project: DesignProject): Promise<void> {
    this.closeMenus();
    await navigator.clipboard.writeText(this.shareLink(project));
    this.shareNote.set('Link copied. It opens this design for anyone who can sign in here.');
    setTimeout(() => this.shareNote.set(''), 4000);
  }

  readonly exporting = signal('');

  /** Ask before exporting: the name is the user's to choose, and on browsers
   *  that allow it, so is the folder. */
  exportAs(format: string): void {
    const project = this.open();
    this.exportOpen.set(false);
    if (!project?.html || this.exporting()) return;
    const spec = this.formats.find((f) => f.id === format);
    if (!spec) return;
    this.exportAsk.set({
      format,
      label: spec.label,
      mime: spec.mime,
      name: `${this.fileStem(project.name)}.${format}`,
      url: this.api.designExportUrl(project.id, format),
    });
  }

  /** Export a design system's project the same way. */
  exportSystemAsk(): void {
    const system = this.openSystem();
    this.systemMenuOpen.set(false);
    if (!system) return;
    this.exportAsk.set({
      format: 'zip',
      label: 'Design system',
      mime: 'application/zip',
      name: `${this.fileStem(system.name)}.zip`,
      url: this.api.designSystemExportUrl(system.id),
    });
  }

  /** Run the pending export. The save dialog is opened before anything is
   *  awaited — a picker asked for later has lost the click that justified it. */
  async confirmExport(): Promise<void> {
    const ask = this.exportAsk();
    if (!ask || this.exporting()) return;
    const name = ask.name.trim() || `design.${ask.format}`;
    const picker = (window as unknown as Record<string, unknown>)['showSaveFilePicker'] as
      | ((options: unknown) => Promise<FileSystemFileHandle>)
      | undefined;

    let handle: FileSystemFileHandle | null = null;
    if (picker) {
      try {
        handle = await picker.call(window, {
          suggestedName: name,
          types: [{ description: ask.label, accept: { [ask.mime]: ['.' + ask.format] } }],
        });
      } catch {
        return; // the user closed the dialog
      }
    }

    this.exportAsk.set(null);
    this.exporting.set(ask.format);
    this.error.set('');
    try {
      const blob = await this.api.downloadBlob(ask.url);
      if (handle) {
        const writable = await handle.createWritable();
        await writable.write(blob);
        await writable.close();
      } else {
        this.saveBlob(blob, name);
      }
      this.shareNote.set(`Saved ${name}.`);
      setTimeout(() => this.shareNote.set(''), 4000);
    } catch (err) {
      this.error.set(`Export failed: ${(err as Error).message}`);
    } finally {
      this.exporting.set('');
    }
  }

  private fileStem(name: string): string {
    return name.replace(/[^\w\- ]+/g, '').trim() || 'design';
  }

  /** Hand a blob to the browser as a file. The anchor has to be in the
   *  document for the click to count as a user-initiated download. */
  private saveBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }

  async copyCode(): Promise<void> {
    const html = this.open()?.html;
    if (html) await navigator.clipboard.writeText(html);
  }

  // ===================== history =====================

  async toggleHistory(): Promise<void> {
    const project = this.open();
    this.projectMenuOpen.set(false);
    if (!project) return;
    if (this.historyOpen()) {
      this.historyOpen.set(false);
      return;
    }
    const res = await this.api.designVersions(project.id);
    this.versions.set(res.versions);
    this.historyOpen.set(true);
  }

  async restore(version: DesignVersion): Promise<void> {
    const project = this.open();
    if (!project) return;
    const restored = await this.api.restoreDesignVersion(project.id, version.id);
    this.open.set(restored);
    this.historyOpen.set(false);
  }

  // ===================== project & row actions =====================

  async rename(project: DesignProject): Promise<void> {
    this.closeMenus();
    const name = window.prompt('Rename design', project.name)?.trim();
    if (!name || name === project.name) return;
    await this.api.patchDesign(project.id, { name });
    this.projects.update((rows) => rows.map((r) => (r.id === project.id ? { ...r, name } : r)));
    const current = this.open();
    if (current?.id === project.id) this.open.set({ ...current, name });
  }

  async duplicate(project: DesignProject): Promise<void> {
    this.closeMenus();
    const copy = await this.api.duplicateDesign(project.id);
    this.projects.update((rows) => [copy, ...rows]);
  }

  async star(project: DesignProject, event?: Event): Promise<void> {
    event?.stopPropagation();
    this.closeMenus();
    const starred = !project.starred;
    this.projects.update((rows) =>
      rows.map((r) => (r.id === project.id ? { ...r, starred } : r)),
    );
    await this.api.patchDesign(project.id, { starred });
  }

  async remove(project: DesignProject, event?: Event): Promise<void> {
    event?.stopPropagation();
    this.closeMenus();
    if (!window.confirm(`Delete “${project.name}”? This cannot be undone.`)) return;
    this.projects.update((rows) => rows.filter((r) => r.id !== project.id));
    if (this.open()?.id === project.id) this.open.set(null);
    await this.api.deleteDesign(project.id);
  }

  openRowMenu(project: DesignProject, event: Event): void {
    event.stopPropagation();
    this.rowMenuId.set(this.rowMenuId() === project.id ? '' : project.id);
  }

  openInNewWindow(project: DesignProject): void {
    this.closeMenus();
    window.open(this.shareLink(project), '_blank', 'noopener');
  }

  async setSystem(systemId: string): Promise<void> {
    const project = this.open();
    if (!project) return;
    this.open.set({ ...project, design_system: systemId });
    await this.api.patchDesign(project.id, { design_system: systemId });
  }

  // ===================== a system as a project =====================

  /** Open a design system as its own project: pages, guide, and parameters. */
  async openSystemDoc(system: DesignSystem): Promise<void> {
    this.openSystem.set(system);
    this.doc.set(null);
    this.openFile.set('');
    this.docSearch.set('');
    this.docLoading.set(true);
    try {
      this.doc.set(await this.api.designSystemDoc(system.id));
    } catch {
      this.error.set('Could not open that design system.');
    } finally {
      this.docLoading.set(false);
    }
  }

  closeSystemDoc(): void {
    this.openSystem.set(null);
    this.doc.set(null);
  }

  /** Sections in document order, filtered by the top-bar search. */
  readonly docSections = computed<DesignSection[]>(() => {
    const sections = this.doc()?.sections ?? [];
    const q = this.docSearch().trim().toLowerCase();
    if (!q) return sections;
    return sections.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.blurb.toLowerCase().includes(q) ||
        s.file.toLowerCase().includes(q),
    );
  });

  /** The tree, grouped the way the document is ordered. */
  readonly docGroups = computed(() => {
    const groups: Array<{ name: string; sections: DesignSection[] }> = [];
    for (const section of this.docSections()) {
      const last = groups[groups.length - 1];
      if (last && last.name === section.group) last.sections.push(section);
      else groups.push({ name: section.group, sections: [section] });
    }
    return groups;
  });

  /** Does this section start a new group? Drives the label above it. */
  groupStart(index: number): string {
    const sections = this.docSections();
    const group = sections[index]?.group ?? '';
    if (!group) return '';
    return index === 0 || sections[index - 1].group !== group ? group : '';
  }

  // Cached per section: a fresh SafeResourceUrl on every change detection
  // would re-set src and reload the frame in a loop.
  private readonly pageUrls = new Map<string, SafeResourceUrl>();

  pageUrl(sectionId: string): SafeResourceUrl | null {
    const system = this.openSystem();
    if (!system) return null;
    const key = `${system.id}/${sectionId}`;
    let url = this.pageUrls.get(key);
    if (!url) {
      url = this.sanitizer.bypassSecurityTrustResourceUrl(
        this.api.designSystemPageUrl(system.id, sectionId),
      );
      this.pageUrls.set(key, url);
    }
    return url;
  }

  readonly activeSection = signal('');

  /** Jump the document pane to a section.
   *
   *  Measured from the rects rather than offsetTop — the sections' offset
   *  parent is the page, not the pane — and positioned outright rather than
   *  animated: the previews load lazily, so anything below the viewport is
   *  still changing height while a smooth scroll is in flight, and the
   *  animation lands somewhere other than where it aimed. */
  scrollToSection(sectionId: string): void {
    const pane = document.querySelector<HTMLElement>('.ds-pages');
    const target = document.getElementById('ds-' + sectionId);
    if (!pane || !target) return;
    const delta = target.getBoundingClientRect().top - pane.getBoundingClientRect().top;
    pane.scrollTop += delta - 12;
    this.activeSection.set(sectionId);
  }

  /** Track which section the reader is in, so the tree marks it. Measured from
   *  the pane's own top edge, for the same reason the jump is. */
  onDocScroll(event: Event): void {
    const pane = event.target as HTMLElement;
    const top = pane.getBoundingClientRect().top;
    let current = '';
    for (const section of this.docSections()) {
      const el = document.getElementById('ds-' + section.id);
      if (el && el.getBoundingClientRect().top - top <= 80) current = section.id;
    }
    this.activeSection.set(current);
  }

  async showFile(path: string): Promise<void> {
    const system = this.openSystem();
    this.filesMenuOpen.set(false);
    if (!system) return;
    if (this.openFile() === path) {
      this.openFile.set('');
      return;
    }
    this.openFileText.set('');
    this.openFile.set(path);
    try {
      this.openFileText.set(await this.api.designSystemFile(system.id, path));
    } catch {
      this.openFileText.set('Could not read that file.');
    }
  }

  /** Copy a system into the user's own — the only way to annotate or retune an
   *  included one, which stays read-only. */
  async duplicateSystem(): Promise<void> {
    const system = this.openSystem();
    this.systemMenuOpen.set(false);
    if (!system) return;
    const copy = await this.api.duplicateDesignSystem(system.id);
    this.systems.update((rows) => [copy, ...rows]);
    await this.openSystemDoc(copy);
  }

  exportSystem(): void {
    this.exportSystemAsk();
  }

  async shareSystem(): Promise<void> {
    const system = this.openSystem();
    if (!system) return;
    await navigator.clipboard.writeText(`${location.origin}/?system=${system.id}`);
    this.shareNote.set('Link copied. It opens this system for anyone who can sign in here.');
    setTimeout(() => this.shareNote.set(''), 4000);
  }

  /** Open a section's page in the canvas as a design of its own — which is
   *  where the editing tools live, so "Edit" lands somewhere real. */
  async editSection(section: DesignSection): Promise<void> {
    const system = this.openSystem();
    if (!system) return;
    this.error.set('');
    try {
      const html = await this.api.designSystemPage(system.id, section.id);
      const project = await this.api.createDesign({
        name: `${system.name} — ${section.name}`,
        template: 'blank',
        prompt: `The ${section.name} page from the ${system.name} design system.`,
        design_system: system.id,
      });
      const saved = await this.api.saveDesignHtml(project.id, html, 'From the design system');
      this.projects.update((rows) => [{ ...saved, html: undefined }, ...rows]);
      this.closeSystemDoc();
      this.openProject(saved);
    } catch {
      this.error.set('Could not open that page for editing.');
    }
  }

  startUsage(section: DesignSection): void {
    this.usageFor.set(section.id);
    this.usageDraft.set(this.doc()?.usage?.[section.id] ?? '');
  }

  async saveUsage(): Promise<void> {
    const system = this.openSystem();
    const doc = this.doc();
    const section = this.usageFor();
    if (!system || !doc || !section) return;
    if (system.builtin) {
      this.error.set('The included systems are read-only — duplicate one to annotate it.');
      this.usageFor.set('');
      return;
    }
    try {
      const res = await this.api.saveSystemUsage(system.id, section, this.usageDraft());
      this.doc.set({ ...doc, usage: res.usage });
    } catch {
      this.error.set('Could not save that note.');
    } finally {
      this.usageFor.set('');
    }
  }

  /** Start a design from this system, with whatever context is attached. */
  useSystem(): void {
    const system = this.openSystem();
    if (!system) return;
    this.chosenSystems.set([system.id]);
    this.closeSystemDoc();
    setTimeout(() => document.getElementById('design-prompt')?.focus(), 0);
  }

  /** Screenshot context: the images ride along with the prompt. */
  async onShots(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []).slice(0, 4);
    const urls: string[] = [];
    for (const file of files) {
      urls.push(
        await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.readAsDataURL(file);
        }),
      );
    }
    this.shots.update((rows) => [...rows, ...urls].slice(0, 4));
    input.value = '';
  }

  dropShot(index: number): void {
    this.shots.update((rows) => rows.filter((_, i) => i !== index));
  }

  /** Point the importer at a repository — the Codebase context path. */
  useCodebase(): void {
    this.closeSystemDoc();
    this.startImport();
    this.importStep.set('here');
    this.importSource.set('repo');
  }

  // ===================== design systems =====================

  startImport(): void {
    this.importStep.set('choose');
    this.importSource.set('paste');
    this.importName.set('');
    this.importText.set('');
    this.importCss.set('');
    this.importUrl.set('');
    this.importWorkspace.set('');
    this.importPath.set('');
  }

  /** Upload reads the files here and sends their text — the server never needs
   *  the binaries, and a design system only ever lives in the text anyway. */
  async onImportFiles(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    if (!files.length) return;
    const parts: string[] = [];
    for (const file of files) parts.push(`/* ${file.name} */\n${await file.text()}`);
    this.importText.update((t) => [t, ...parts].filter(Boolean).join('\n\n'));
    if (!this.importName()) this.importName.set(files[0].name.replace(/\.[^.]+$/, ''));
    input.value = '';
  }

  readonly canImport = computed(() => {
    switch (this.importSource()) {
      case 'url':
        return !!this.importUrl().trim();
      case 'repo':
        return !!this.importWorkspace().trim();
      default:
        return !!this.importText().trim() || !!this.importCss().trim();
    }
  });

  async importSystem(): Promise<void> {
    if (!this.canImport() || this.importing()) return;
    this.importing.set(true);
    this.error.set('');
    try {
      const created = await this.api.createDesignSystem({
        name: this.importName().trim(),
        source: this.importSource(),
        text: this.importText().trim(),
        css: this.importCss().trim(),
        url: this.importSource() === 'url' ? this.importUrl().trim() : '',
        workspace_id: this.importSource() === 'repo' ? this.importWorkspace().trim() : '',
        path: this.importSource() === 'repo' ? this.importPath().trim() : '',
      });
      this.systems.update((rows) => [created, ...rows]);
      this.importStep.set('');
    } catch {
      this.error.set('Could not read that into a design system.');
    } finally {
      this.importing.set(false);
    }
  }

  // ===================== set up a design system =====================

  openSetup(): void {
    this.importStep.set('');
    this.systemPickerOpen.set(false);
    this.setupOpen.set(true);
    void this.loadWorkspaces();
  }

  private async loadWorkspaces(): Promise<void> {
    if (this.workspaces().length) return;
    try {
      this.workspaces.set((await this.api.listWorkspaces()).workspaces);
    } catch {
      /* the field just stays a free-text box */
    }
  }

  /** Read text files in the browser and hand over their contents — the server
   *  never needs the folder, only what is written in it. */
  async onSetupFiles(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    const readable = files.filter((f) => /\.(css|scss|less|json|md|txt|html?|tsx?|jsx?|svg)$/i.test(f.name));
    const picked: Array<{ name: string; text: string }> = [];
    for (const file of readable.slice(0, 40)) {
      picked.push({ name: file.name, text: (await file.text()).slice(0, 20_000) });
    }
    this.setupFiles.update((rows) => [...rows, ...picked].slice(0, 40));
    if (!picked.length && files.length) {
      this.error.set('None of those files carry text Compass can read.');
    }
    input.value = '';
  }

  async onSetupImages(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    const urls: string[] = [];
    for (const file of files.slice(0, 6)) {
      if (!file.type.startsWith('image/')) continue;
      urls.push(
        await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.readAsDataURL(file);
        }),
      );
    }
    this.setupImages.update((rows) => [...rows, ...urls].slice(0, 6));
    input.value = '';
  }

  /** A .fig is Figma's own binary. Compass can't read one, and saying so
   *  beats pretending: the same tokens come out of a repo or an export. */
  onSetupFig(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.setupFig.set(input.files?.[0]?.name ?? '');
    input.value = '';
  }

  readonly canSetup = computed(
    () =>
      !!this.setupBlurb().trim() ||
      !!this.setupGithub().trim() ||
      !!this.setupWorkspace().trim() ||
      this.setupFiles().length > 0 ||
      this.setupImages().length > 0 ||
      !!this.setupNotes().trim(),
  );

  async runSetup(): Promise<void> {
    if (!this.canSetup() || this.setupBusy()) return;
    this.setupBusy.set(true);
    this.error.set('');
    try {
      const created = await this.api.setUpDesignSystem({
        name: this.setupName().trim(),
        blurb: this.setupBlurb().trim(),
        github: this.setupGithub().trim(),
        workspace_id: this.setupWorkspace().trim(),
        path: this.setupPath().trim(),
        files: this.setupFiles(),
        images: this.setupImages(),
        notes: this.setupNotes().trim(),
        css: this.setupCss().trim(),
      });
      this.systems.update((rows) => [created, ...rows]);
      this.setupOpen.set(false);
      this.setupName.set('');
      this.setupBlurb.set('');
      this.setupGithub.set('');
      this.setupFiles.set([]);
      this.setupImages.set([]);
      this.setupNotes.set('');
      this.setupCss.set('');
      this.setupFig.set('');
      await this.openSystemDoc(created);
    } catch (err) {
      this.error.set(`Could not build that system: ${(err as Error).message}`);
    } finally {
      this.setupBusy.set(false);
    }
  }

  async removeSystem(system: DesignSystem, event: Event): Promise<void> {
    event.stopPropagation();
    this.systems.update((rows) => rows.filter((r) => r.id !== system.id));
    this.chosenSystems.update((ids) => ids.filter((id) => id !== system.id));
    await this.api.deleteDesignSystem(system.id);
  }

  /** Swatches for a card. An imported system may carry none, in which case the
   *  hexes found in its notes stand in. */
  ramp(system: DesignSystem): string[] {
    if (system.swatches?.length) return system.swatches;
    return (system.notes.match(/#[0-9a-fA-F]{6}/g) ?? []).slice(0, 9);
  }
}
