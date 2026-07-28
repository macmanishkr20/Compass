import {
  Component,
  computed,
  effect,
  inject,
  signal,
  viewChild,
  ElementRef,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgTemplateOutlet, TitleCasePipe } from '@angular/common';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { COLLAB_APPS, CollabApp } from './collab-apps.config';
import { AuthService } from './auth.service';
import { CompassApiService } from './compass-api.service';
import { ThemeService } from './theme.service';
import { TiltDirective } from './tilt.directive';
import { BlurOnChange } from './blur-on-change.directive';
import { CompassMark } from './compass-mark/compass-mark';
import { LoadingRadar } from './loading-radar/loading-radar';
import { Markdown } from './markdown/markdown';
import { ArtifactPanel } from './artifact-panel/artifact-panel';
import { ArtifactService } from './artifact.service';
import { HomeChat } from './home-chat/home-chat';
import { Lightbox } from './lightbox/lightbox';
import { LightboxService } from './lightbox.service';
import { ATTACH_ACCEPT, UiAttachment, formatSize, readFiles, toWire } from './attachments';
import { SmoothText } from './smooth-text';
import {
  BackgroundTask,
  ChatBubble,
  ChatCard,
  CompassEvent,
  GitStatus,
  GithubRepo,
  GroupBy,
  HealthInfo,
  NoticeVM,
  PermissionVM,
  Routine,
  RoutineRun,
  RoutineTemplate,
  SessionCard,
  SessionGroup,
  SortBy,
  TimelineItem,
  ToolCardVM,
  UsageVM,
  Workspace,
} from './models';

const MODES = ['default', 'accept_edits', 'plan', 'bypass'] as const;
const EFFORTS = ['minimal', 'low', 'medium', 'high'] as const;

/** A rendered timeline block: either a standalone item (user/assistant bubble,
 * permission card, meaningful notice) or a collapsed "activity" group folding
 * the background tool work — the analog of Claude's "Ran a command, used a
 * tool ⌄" caret. */
type RenderBlock =
  | { kind: 'single'; item: TimelineItem }
  | {
      kind: 'activity';
      id: string;
      items: TimelineItem[];
      summary: string;
      running: boolean;
      count: number;
    };

@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule,
    NgTemplateOutlet,
    TitleCasePipe,
    TiltDirective,
    BlurOnChange,
    CompassMark,
    LoadingRadar,
    Markdown,
    ArtifactPanel,
    HomeChat,
    Lightbox,
  ],
  templateUrl: './app.html',
  styleUrl: './app.css',
  host: {
    '(document:keydown)': 'onGlobalKeydown($event)',
    '(document:click)': 'onGlobalClick()',
  },
})
export class App {
  private readonly api = inject(CompassApiService);
  readonly theme = inject(ThemeService);
  readonly auth = inject(AuthService);
  readonly artifacts = inject(ArtifactService);
  private readonly sanitizer = inject(DomSanitizer);

  // Compass Collab — sibling apps launched from the sidebar.
  readonly collabApps = COLLAB_APPS;

  // In-app browser ("Compass's own browser", like Claude's preview pane).
  readonly browserOpen = signal(false);
  readonly browserAddr = signal('');
  readonly browserSrc = signal<SafeResourceUrl | ''>('');
  readonly browserBlocked = signal(false);

  // -- top-level section: Home (Chat) vs Code (the Agent Console). Home is a
  // separate, tool-free surface (HomeChat) and shares no state with the
  // console; the two are switched via the top-bar Home/Code control.
  readonly section = signal<'home' | 'code'>('home');
  enterHome(): void {
    this.section.set('home');
    void this.loadHomeSessions();
  }
  enterCode(): void {
    this.section.set('code');
  }

  // -- Work IQ (Home-only): toggle grounding the chat in Azure AI Search.
  readonly workIqOn = signal(false); // default off → plain chat
  readonly workIqConfigured = signal(false);
  readonly workIqToast = signal('');
  async loadWorkIqStatus(): Promise<void> {
    try {
      this.workIqConfigured.set((await this.api.workIqStatus()).configured);
    } catch {
      this.workIqConfigured.set(false);
    }
  }
  toggleWorkIq(): void {
    if (!this.workIqConfigured()) {
      this.workIqToast.set('Work IQ isn’t configured — set AZURE_AISEARCH_* in .env');
      setTimeout(() => this.workIqToast.set(''), 4500);
      return;
    }
    this.workIqOn.update((v) => !v);
  }

  // -- Home conversation history (separate from agent Conversations) -------
  readonly homeSessions = signal<ChatCard[]>([]);
  readonly homeActiveId = signal<string | null>(null);
  // Show 4 initially; the down-arrow reveals 4 more at a time (like the agent).
  readonly homeLimit = signal(4);
  // Home chats honour the same Group/Sort controls as the agent list.
  private readonly sortedHome = computed(() => {
    const list = [...this.homeSessions()];
    const by = this.sortBy();
    if (by === 'title') list.sort((a, b) => a.title.localeCompare(b.title));
    else if (by === 'created') list.sort((a, b) => b.created_at - a.created_at);
    else list.sort((a, b) => b.updated_at - a.updated_at);
    return list;
  });
  readonly homeGroups = computed<{ label: string; cards: ChatCard[] }[]>(() => {
    const list = this.sortedHome();
    if (this.groupBy() === 'date') {
      const order = ['Today', 'Yesterday', 'Previous 7 days', 'Older'];
      const map = new Map<string, ChatCard[]>();
      for (const c of list) {
        const k = this.dateBucket(c.updated_at);
        (map.get(k) ?? map.set(k, []).get(k)!).push(c);
      }
      return order.filter((k) => map.has(k)).map((label) => ({ label, cards: map.get(label)! }));
    }
    return [{ label: 'Chats', cards: list }];
  });
  readonly limitedHomeGroups = computed<{ label: string; cards: ChatCard[] }[]>(() => {
    let budget = this.homeLimit();
    const out: { label: string; cards: ChatCard[] }[] = [];
    for (const g of this.homeGroups()) {
      if (budget <= 0) break;
      const cards = g.cards.slice(0, budget);
      budget -= cards.length;
      out.push({ label: g.label, cards });
    }
    return out;
  });
  readonly moreHome = computed(() => Math.max(0, this.homeSessions().length - this.homeLimit()));
  loadMoreHome(): void {
    this.homeLimit.update((n) => n + 4);
  }

  async loadHomeSessions(): Promise<void> {
    try {
      this.homeSessions.set((await this.api.listChatSessions()).sessions);
    } catch {
      /* non-fatal */
    }
  }
  openHomeConversation(id: string): void {
    this.section.set('home');
    this.homeActiveId.set(id);
  }
  /** The HomeChat created a fresh session on its first message. */
  onHomeSessionCreated(id: string): void {
    this.homeActiveId.set(id);
    void this.loadHomeSessions();
  }
  async deleteHomeConversation(card: ChatCard, ev: Event): Promise<void> {
    ev.stopPropagation();
    try {
      await this.api.deleteChatSession(card.id);
    } catch {
      /* ignore */
    }
    if (this.homeActiveId() === card.id) this.homeActiveId.set(null);
    await this.loadHomeSessions();
  }

  /** New-conversation button: section-aware — a fresh Home chat when on Home,
   *  a new agent session when on Code. */
  newConversation(): void {
    if (this.section() === 'home') {
      this.homeActiveId.set(null); // HomeChat resets to an empty thread
      void this.loadHomeSessions();
    } else {
      void this.newSession();
    }
  }

  // -- main view switch (within the Code section): console vs. Routines page.
  readonly view = signal<'chat' | 'routines'>('chat');

  // -- Background tasks panel (long-running processes the agent spawned).
  readonly bgOpen = signal(false);
  readonly bgTasks = signal<BackgroundTask[]>([]);
  readonly bgFinishedOpen = signal(false);
  readonly nowTick = signal(Date.now()); // drives live elapsed timers
  readonly bgRunning = computed(() => this.bgTasks().filter((t) => t.status === 'running'));
  readonly bgFinished = computed(() => this.bgTasks().filter((t) => t.status !== 'running'));

  // -- Routines page (list / builder / detail sub-views).
  readonly routines = signal<Routine[]>([]);
  readonly routineTemplates = signal<RoutineTemplate[]>([]);
  readonly routineSuggestions = signal<string[]>([]);
  readonly routineConnectorOptions = signal<string[]>([]);
  readonly newRoutinePrompt = signal('');
  readonly newRoutineTarget = signal<'local' | 'cloud'>('local');
  readonly routineMenuOpen = signal(false);
  readonly routineBusy = signal(false);
  readonly routineView = signal<'list' | 'builder' | 'detail'>('list');
  readonly activeRoutine = signal<Routine | null>(null);
  readonly routineRuns = signal<RoutineRun[]>([]);
  readonly routineToast = signal('');
  // Builder form state.
  readonly fName = signal('');
  readonly fInstructions = signal('');
  readonly fTarget = signal<'local' | 'cloud'>('local');
  readonly fRepo = signal('');
  readonly fTriggerType = signal<'once' | 'hourly' | 'daily' | 'weekdays' | 'weekly' | 'custom'>('weekdays');
  readonly fTriggerTime = signal('09:00');
  readonly fTriggerDays = signal<number[]>([0]);
  readonly fConnectors = signal<string[]>([]);
  readonly fAutoFix = signal(false);
  readonly fNotifyEnabled = signal(true);
  readonly fNotifyPush = signal(true);
  readonly fNotifyEmail = signal(false);
  readonly fNotifySlack = signal(false);
  readonly fTab = signal<'connectors' | 'behavior' | 'notifications'>('connectors');
  readonly fEditId = signal<string | null>(null);
  readonly timePickerOpen = signal(false);
  readonly triggerOpen = signal(false);
  // Notifications: watch finished runs and fire a native browser notification
  // for any routine with push enabled.
  private notifySince = Math.floor(Date.now() / 1000);
  readonly emailConfigured = signal(true); // false => show a hint in the builder

  readonly modes = MODES;
  readonly efforts = EFFORTS;
  readonly modeLabels: Record<string, string> = {
    default: 'Default',
    accept_edits: 'Accept edits',
    plan: 'Plan',
    bypass: 'Bypass',
  };

  // -- login form + avatar menu
  readonly loginUsername = signal('');
  readonly loginPassword = signal('');
  readonly userMenuOpen = signal(false);

  // -- global state
  readonly health = signal<HealthInfo | null>(null);
  readonly sessionId = signal<string | null>(null);
  readonly timeline = signal<TimelineItem[]>([]);
  readonly usage = signal<UsageVM | null>(null);
  readonly streaming = signal(false);
  readonly draft = signal('');
  readonly connError = signal<string | null>(null);

  // -- "thinking" loader (shown until the first token / tool / permission)
  readonly thinking = signal(false);
  readonly thinkingMsg = signal('');
  // Live turn meters (Claude-style): elapsed wall-clock and output tokens so
  // far, shown next to the radar while the whole turn streams. Tokens are
  // derived from the streaming assistant text (~4 chars/token) so the count
  // tracks visible output live; after the turn it reflects the real usage.
  readonly elapsedMs = signal(0);
  readonly liveTokens = computed(() => {
    const items = this.timeline();
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (
        it.kind === 'bubble' &&
        (it as ChatBubble).role === 'assistant' &&
        (it as ChatBubble).streaming
      ) {
        return Math.round(((it as ChatBubble).text?.length ?? 0) / 4);
      }
    }
    return Math.max(
      0,
      (this.usage()?.completionTokens ?? 0) - this.turnStartCompletion,
    );
  });
  private readonly thinkingLines = [
    'Consulting the schema…',
    'Tracing the query plan…',
    'Weighing the approaches…',
    'Composing a response…',
    'Checking the edge cases…',
    'Lining up the syntax…',
    'Thinking it through…',
  ];
  private thinkingIdx = 0;

  // -- per-session controls
  readonly activeMode = signal('default');
  readonly activeEffort = signal('medium');
  readonly activeModel = signal('');
  readonly models = signal<string[]>([]);

  // -- workspaces
  readonly workspaces = signal<Workspace[]>([]);
  readonly activeWorkspaceId = signal('default');
  readonly workspacePanelOpen = signal(false);
  readonly githubRepos = signal<GithubRepo[]>([]);
  readonly githubLoading = signal(false);
  readonly githubEnabled = signal(false);
  readonly workspaceBusy = signal<string | null>(null); // status text
  readonly newFolderName = signal('');

  readonly activeWorkspace = computed(() =>
    this.workspaces().find((w) => w.id === this.activeWorkspaceId()),
  );

  // -- git / PR status shown in the composer bar
  readonly gitStatus = signal<GitStatus | null>(null);
  readonly prBusy = signal(false);
  readonly prNotice = signal('');
  readonly diffOpen = signal(false);
  readonly diffLines = signal<{ t: string; text: string }[]>([]);
  readonly diffBusy = signal(false);
  readonly prMenuOpen = signal(false);
  readonly repoMenuOpen = signal(false);
  readonly branchMenuOpen = signal(false);

  // -- sidebar / history
  readonly sidebarOpen = signal(true);
  readonly cards = signal<SessionCard[]>([]);
  readonly groupBy = signal<GroupBy>('none');
  readonly sortBy = signal<SortBy>('recent');
  readonly showArchived = signal(false);
  readonly historyMenuOpen = signal(false);

  // -- conversation search (command palette)
  readonly searchOpen = signal(false);
  readonly searchQuery = signal('');
  readonly searchIndex = signal(0);
  private readonly searchInput = viewChild<ElementRef<HTMLInputElement>>('searchInput');
  // Search is section-aware: Home searches its chats, Code the agent list.
  readonly searchResults = computed<{ id: string; title: string; updated_at: number }[]>(() => {
    const q = this.searchQuery().trim().toLowerCase();
    const list: { id: string; title: string; updated_at: number }[] =
      this.section() === 'home'
        ? this.homeSessions()
        : this.cards().filter((c) => !this.isRoutineRun(c));
    const matched = q ? list.filter((c) => (c.title || '').toLowerCase().includes(q)) : list;
    return [...matched].sort((a, b) => b.updated_at - a.updated_at).slice(0, 50);
  });

  // -- transient row editors
  readonly menuOpenId = signal<string | null>(null);
  readonly renamingId = signal<string | null>(null);
  readonly groupingId = signal<string | null>(null);

  readonly suggestions = [
    'Summarize the files in this workspace',
    'Find every TODO and list them by file',
    'Run the test suite and report failures',
  ];

  readonly mcpCount = computed(() => {
    const h = this.health();
    return h ? Object.keys(h.mcp_servers).length : 0;
  });
  // -- Agent Console composer attachments (images / files / zip) ----------
  readonly attachments = signal<UiAttachment[]>([]);
  readonly agentDragOver = signal(false);
  readonly attachError = signal('');
  readonly attachAccept = ATTACH_ACCEPT;
  private readonly agentFileInput = viewChild<ElementRef<HTMLInputElement>>('agentFileInput');
  agentFormatSize = formatSize;

  openAttachPicker(): void {
    this.agentFileInput()?.nativeElement.click();
  }
  onAttachPicked(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    if (input.files) void this.addAttachFiles(input.files);
    input.value = '';
  }
  onAttachPaste(ev: ClipboardEvent): void {
    const files = ev.clipboardData?.files;
    if (files && files.length) {
      ev.preventDefault();
      void this.addAttachFiles(files);
    }
  }
  onAttachDragOver(ev: DragEvent): void {
    if (ev.dataTransfer?.types?.includes('Files')) {
      ev.preventDefault();
      this.agentDragOver.set(true);
    }
  }
  onAttachDragLeave(): void {
    this.agentDragOver.set(false);
  }
  onAttachDrop(ev: DragEvent): void {
    ev.preventDefault();
    this.agentDragOver.set(false);
    if (ev.dataTransfer?.files?.length) void this.addAttachFiles(ev.dataTransfer.files);
  }
  removeAttachment(id: string): void {
    this.attachments.update((list) => list.filter((a) => a.id !== id));
  }
  private async addAttachFiles(files: FileList): Promise<void> {
    const { added, errors } = await readFiles(files);
    if (added.length) this.attachments.update((list) => [...list, ...added]);
    if (errors.length) {
      this.attachError.set(errors[0]);
      setTimeout(() => this.attachError.set(''), 4000);
    }
  }

  readonly canSend = computed(
    () =>
      (this.draft().trim().length > 0 || this.attachments().length > 0) &&
      !this.streaming(),
  );
  readonly activeCard = computed(() =>
    this.cards().find((c) => c.id === this.sessionId()),
  );
  readonly knownGroups = computed(() => {
    const set = new Set<string>();
    for (const c of this.cards()) if (c.group) set.add(c.group);
    return [...set].sort();
  });

  readonly lastAssistantId = computed(() => {
    const items = this.timeline();
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (it.kind === 'bubble' && it.role === 'assistant') return it.id;
    }
    return null;
  });

  // -- activity grouping (collapsible background work)
  readonly expandedActivities = signal<Set<string>>(new Set());

  readonly renderBlocks = computed<RenderBlock[]>(() => {
    const blocks: RenderBlock[] = [];
    let group: TimelineItem[] = [];
    const flush = () => {
      if (group.length) {
        const tools = group.filter((g) => g.kind === 'tool') as ToolCardVM[];
        blocks.push({
          kind: 'activity',
          id: group[0].id,
          items: group,
          summary: this.summarizeActivity(tools),
          running: tools.some((t) => t.status === 'running'),
          count: tools.length,
        });
      }
      group = [];
    };
    for (const it of this.timeline()) {
      // File writes/edits surface as their own "Edited file +N −M" row (like
      // Claude), not folded into the collapsed background-activity group.
      const isFileEdit =
        it.kind === 'tool' &&
        ((it as ToolCardVM).name === 'file_edit' ||
          (it as ToolCardVM).name === 'file_write');
      const isBackground =
        (it.kind === 'tool' && !isFileEdit) ||
        (it.kind === 'notice' && (it as NoticeVM).tone === 'compaction');
      if (isBackground) {
        group.push(it);
      } else {
        flush();
        blocks.push({ kind: 'single', item: it });
      }
    }
    flush();
    return blocks;
  });

  /** Parse a file_edit/file_write tool into a Claude-style edit row + diff. */
  fileEditInfo(
    t: ToolCardVM,
  ): { verb: string; file: string; adds: number; dels: number; diff: { type: 'add' | 'del'; text: string }[] } | null {
    if (t.name !== 'file_edit' && t.name !== 'file_write') return null;
    try {
      const a = JSON.parse(t.args);
      const path: string = a.file_path || a.path || '';
      const file = path.split('/').pop() || path || 'file';
      if (t.name === 'file_write') {
        const content: string = typeof a.content === 'string' ? a.content : '';
        const lines = content === '' ? [] : content.split('\n');
        return {
          verb: 'Created',
          file,
          adds: lines.length,
          dels: 0,
          diff: lines.map((text) => ({ type: 'add' as const, text })),
        };
      }
      const oldL: string[] =
        typeof a.old_string === 'string' && a.old_string !== '' ? a.old_string.split('\n') : [];
      const newL: string[] =
        typeof a.new_string === 'string' && a.new_string !== '' ? a.new_string.split('\n') : [];
      // Trim identical leading/trailing lines so +N −M reflects the real change.
      let pre = 0;
      while (pre < oldL.length && pre < newL.length && oldL[pre] === newL[pre]) pre++;
      let suf = 0;
      while (
        suf < oldL.length - pre &&
        suf < newL.length - pre &&
        oldL[oldL.length - 1 - suf] === newL[newL.length - 1 - suf]
      )
        suf++;
      const oldMid = oldL.slice(pre, oldL.length - suf);
      const newMid = newL.slice(pre, newL.length - suf);
      return {
        verb: 'Edited',
        file,
        adds: newMid.length,
        dels: oldMid.length,
        diff: [
          ...oldMid.map((text) => ({ type: 'del' as const, text })),
          ...newMid.map((text) => ({ type: 'add' as const, text })),
        ],
      };
    } catch {
      return null;
    }
  }

  toggleActivity(id: string): void {
    this.expandedActivities.update((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  isActivityExpanded = (id: string): boolean => this.expandedActivities().has(id);

  private summarizeActivity(tools: ToolCardVM[]): string {
    if (!tools.length) return 'Working…';
    const counts: Record<string, number> = {};
    for (const t of tools) {
      const cat = this.toolCategory(t.name);
      counts[cat] = (counts[cat] ?? 0) + 1;
    }
    const n = (c: number, one: string, many: string) =>
      `${c} ${c === 1 ? one : many}`;
    const phrases: Record<string, (c: number) => string> = {
      read: (c) => `Read ${n(c, 'file', 'files')}`,
      searched: () => 'Searched the code',
      ran: (c) => `Ran ${n(c, 'command', 'commands')}`,
      edited: (c) => `Edited ${n(c, 'file', 'files')}`,
      wrote: (c) => `Wrote ${n(c, 'file', 'files')}`,
      planned: () => 'Updated the plan',
      delegated: (c) => `Delegated to ${n(c, 'subagent', 'subagents')}`,
      used: (c) => `Used ${n(c, 'tool', 'tools')}`,
    };
    const order = ['ran', 'edited', 'wrote', 'read', 'searched', 'delegated', 'planned', 'used'];
    const parts = order
      .filter((k) => counts[k])
      .map((k) => phrases[k](counts[k]));
    return parts.slice(0, 3).join(' · ') || 'Used a tool';
  }

  private toolCategory(name: string): string {
    if (name === 'file_read') return 'read';
    if (name === 'glob' || name === 'grep') return 'searched';
    if (name === 'bash') return 'ran';
    if (name === 'file_edit') return 'edited';
    if (name === 'file_write') return 'wrote';
    if (name === 'todo_write') return 'planned';
    if (name === 'agent') return 'delegated';
    return 'used';
  }

  trackBlock = (_: number, b: RenderBlock): string =>
    b.kind === 'activity' ? 'a:' + b.id : 's:' + b.item.id;

  /** Pinned conversations, always shown first as their own group. */
  /** A routine-run session — tagged by routine_id, or (for sessions created
   *  before tagging existed) recognizable by the ⚡ title prefix. */
  private isRoutineRun = (c: SessionCard): boolean =>
    !!c.routine_id || c.title.startsWith('⚡');

  readonly pinnedGroup = computed<SessionGroup | null>(() => {
    const pins = this.cards().filter((c) => c.pinned && !c.archived && !this.isRoutineRun(c));
    return pins.length
      ? { label: 'Pinned', cards: this.sortCards(pins) }
      : null;
  });

  /** The remaining conversations, grouped and sorted per the controls.
   *  Routine-run sessions are excluded — they live under the Routines section. */
  readonly groups = computed<SessionGroup[]>(() => {
    const rest = this.cards().filter(
      (c) => !c.pinned && !this.isRoutineRun(c) && (this.showArchived() ? true : !c.archived),
    );
    const by = this.groupBy();
    let buckets: SessionGroup[];
    if (by === 'group') {
      const map = new Map<string, SessionCard[]>();
      for (const c of rest) {
        const key = c.group || 'Ungrouped';
        (map.get(key) ?? map.set(key, []).get(key)!).push(c);
      }
      buckets = [...map.entries()]
        .sort((a, b) =>
          a[0] === 'Ungrouped' ? 1 : b[0] === 'Ungrouped' ? -1 : a[0].localeCompare(b[0]),
        )
        .map(([label, cards]) => ({ label, cards: this.sortCards(cards) }));
    } else if (by === 'date') {
      const order = ['Today', 'Yesterday', 'Previous 7 days', 'Older'];
      const map = new Map<string, SessionCard[]>();
      for (const c of rest) {
        const key = this.dateBucket(c.updated_at);
        (map.get(key) ?? map.set(key, []).get(key)!).push(c);
      }
      buckets = order
        .filter((k) => map.has(k))
        .map((label) => ({ label, cards: this.sortCards(map.get(label)!) }));
    } else {
      buckets = [{ label: 'Conversations', cards: this.sortCards(rest) }];
    }
    return buckets;
  });

  // -- conversation pagination: show N, "load more" reveals 4 at a time.
  readonly convLimit = signal(4);
  readonly totalConvs = computed(() =>
    this.groups().reduce((n, g) => n + g.cards.length, 0),
  );
  /** Groups trimmed so at most convLimit() conversations show in total. */
  readonly limitedGroups = computed<SessionGroup[]>(() => {
    let budget = this.convLimit();
    const out: SessionGroup[] = [];
    for (const g of this.groups()) {
      if (budget <= 0) break;
      const cards = g.cards.slice(0, budget);
      budget -= cards.length;
      out.push({ label: g.label, cards });
    }
    return out;
  });
  readonly moreConvs = computed(() => Math.max(0, this.totalConvs() - this.convLimit()));
  loadMoreConvs(): void {
    this.convLimit.update((n) => n + 4);
  }

  // -- active conversation's dot lights up in a random colour on open.
  readonly activeDotColor = signal('');
  private randomDotColor(): string {
    return `hsl(${Math.floor(Math.random() * 360)}, 72%, 58%)`;
  }

  private sortCards(cards: SessionCard[]): SessionCard[] {
    const by = this.sortBy();
    const copy = [...cards];
    if (by === 'title') copy.sort((a, b) => a.title.localeCompare(b.title));
    else if (by === 'created') copy.sort((a, b) => b.created_at - a.created_at);
    else copy.sort((a, b) => b.updated_at - a.updated_at);
    return copy;
  }

  private dateBucket(ts: number): string {
    const now = Date.now() / 1000;
    const day = 86400;
    const startOfToday = now - (now % day);
    if (ts >= startOfToday) return 'Today';
    if (ts >= startOfToday - day) return 'Yesterday';
    if (ts >= startOfToday - 7 * day) return 'Previous 7 days';
    return 'Older';
  }

  private readonly logEl = viewChild<ElementRef<HTMLElement>>('log');
  private currentBubble: ChatBubble | null = null;
  private textSmoother: SmoothText | null = null; // smooth token reveal
  private lastAssistantText = ''; // authoritative full text of the last reply
  private turnStartMs = 0;
  private turnStartCompletion = 0;

  // copy / read-aloud transient state (keyed by bubble id)
  readonly copiedId = signal<string | null>(null);
  readonly speakingId = signal<string | null>(null);
  readonly speakLoadingId = signal<string | null>(null);
  private currentAudio: HTMLAudioElement | null = null;

  // read-aloud voice (client preference, persisted)
  readonly voices = signal<string[]>([]);
  readonly activeVoice = signal(this.loadVoicePref());
  private turnAborted = false;

  constructor() {
    void this.boot();
    effect(() => {
      this.timeline();
      this.thinking();
      queueMicrotask(() => {
        const el = this.logEl()?.nativeElement;
        // Instant follow so rAF-paced streaming text doesn't fight a smooth
        // scroll animation (which stutters when content grows every frame).
        el?.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
      });
    });
    // Rotate the status message for the whole turn (Claude keeps its verbs
    // cycling until the final answer lands, not just until the first token).
    effect((onCleanup) => {
      if (!this.streaming()) return;
      this.thinkingIdx = 0;
      this.thinkingMsg.set(this.thinkingLines[0]);
      const id = setInterval(() => {
        this.thinkingIdx = (this.thinkingIdx + 1) % this.thinkingLines.length;
        this.thinkingMsg.set(this.thinkingLines[this.thinkingIdx]);
      }, 2400);
      onCleanup(() => clearInterval(id));
    });
    // Tick the elapsed-time meter while a turn is in flight.
    effect((onCleanup) => {
      if (!this.streaming()) return;
      const id = setInterval(
        () => this.elapsedMs.set(Math.round(performance.now() - this.turnStartMs)),
        250,
      );
      onCleanup(() => clearInterval(id));
    });
    // Poll background tasks so the top-bar badge and panel stay live; tick a
    // clock every second so "18m 26s" style timers advance without a refetch.
    void this.refreshBgTasks();
    setInterval(() => void this.refreshBgTasks(), 2500);
    setInterval(() => this.nowTick.set(Date.now()), 1000);
    // Populate the sidebar Routines section up front.
    void this.loadRoutines();
    // Populate the Home conversation list (shown when the Home tab is active).
    void this.loadHomeSessions();
    // Is Work IQ (Azure AI Search) configured? Drives the Home toggle.
    void this.loadWorkIqStatus();
    // Watch for finished routine runs and fire a native push notification for
    // any routine with push enabled — works whenever the app is open (including
    // a background tab), which is how a laptop "push" is delivered.
    setInterval(() => void this.pollRoutineNotifications(), 12000);
    // While viewing a routine, keep its Runs list live so scheduled runs that
    // fire in the background appear without reopening the page.
    setInterval(() => {
      if (
        this.view() === 'routines' &&
        this.routineView() === 'detail' &&
        !this.routineBusy()
      ) {
        const r = this.activeRoutine();
        if (r) void this.loadRuns(r.id);
      }
    }, 8000);
  }

  // -- boot / auth ---------------------------------------------------------

  private async boot(): Promise<void> {
    try {
      const h = await this.api.health();
      this.health.set(h);
      this.models.set(h.models ?? []);
      this.activeModel.set(h.deployment ?? '');
      this.githubEnabled.set(h.github ?? false);
      this.voices.set(h.tts_voices ?? []);
      // Adopt the server default voice only if the user hasn't chosen one.
      if (!this.loadVoicePref() && h.tts_voice) this.activeVoice.set(h.tts_voice);
      await this.auth.restore(h.auth ?? true);
      if (this.auth.user()) await this.enterWorkspace();
    } catch (err) {
      this.connError.set(
        'Backend unreachable. Start it with: uvicorn compass.api.server:app --port 8000',
      );
      console.error(err);
    } finally {
      this.auth.checking.set(false);
    }
  }

  private async enterWorkspace(): Promise<void> {
    await this.refreshWorkspaces();
    await this.refreshSessions();
    await this.newSession();
  }

  async refreshWorkspaces(): Promise<void> {
    try {
      this.workspaces.set((await this.api.listWorkspaces()).workspaces);
      void this.loadGitStatus();
    } catch {
      /* non-fatal */
    }
  }

  async signIn(): Promise<void> {
    const ok = await this.auth.login(
      this.loginUsername().trim(),
      this.loginPassword(),
    );
    if (ok) {
      this.loginPassword.set('');
      await this.enterWorkspace();
    }
  }

  onLoginKeydown(ev: KeyboardEvent): void {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      void this.signIn();
    }
  }

  signOut(): void {
    this.userMenuOpen.set(false);
    this.auth.logout();
    this.sessionId.set(null);
    this.timeline.set([]);
    this.usage.set(null);
    this.cards.set([]);
  }

  /** Short repo name for the composer status bar — the git remote's basename
   * (e.g. "Compass") when available, else the workspace name. */
  repoLabel(): string {
    const remote = this.gitStatus()?.remote;
    if (remote) {
      const m = /([^/]+?)(?:\.git)?\/?$/.exec(remote);
      if (m?.[1]) return m[1];
    }
    return this.activeWorkspace()?.name || 'Workspace';
  }

  // -- prompt navigator (right-rail, jump to a user prompt) ---------------
  readonly navActive = signal<string | null>(null);
  readonly promptNav = computed(() =>
    this.timeline()
      .filter(
        (it): it is ChatBubble =>
          it.kind === 'bubble' && (it as ChatBubble).role === 'user',
      )
      .map((b) => ({
        id: b.id,
        text: String(b.text ?? '').replace(/\s+/g, ' ').trim().slice(0, 80),
      })),
  );

  /** Capitalize the first visible character (for conversation titles). */
  capFirst(s: string | null | undefined): string {
    const t = (s ?? '').trimStart();
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : '';
  }
  scrollToPrompt(id: string): void {
    document.getElementById('msg-' + id)?.scrollIntoView({ block: 'start' });
    this.navActive.set(id);
  }
  onLogScroll(): void {
    const log = this.logEl()?.nativeElement;
    if (!log) return;
    const marker = log.getBoundingClientRect().top + 120;
    let current: string | null = null;
    for (const p of this.promptNav()) {
      const el = document.getElementById('msg-' + p.id);
      if (el && el.getBoundingClientRect().top <= marker) current = p.id;
    }
    this.navActive.set(current);
  }

  // -- permission card (Claude-style approval dialog) ---------------------
  private permBasename(p: PermissionVM): string {
    try {
      const a = JSON.parse(p.args);
      const path = a?.file_path || a?.path || '';
      return typeof path === 'string' && path ? path.split('/').pop() || path : '';
    } catch {
      return '';
    }
  }
  /** Bold question at the top of the card: "Allow Compass to run …?" */
  permQuestion(p: PermissionVM): string {
    if (p.toolName === 'bash') {
      const why = this.permReason(p).replace(/[.。]\s*$/, '');
      return why
        ? `Allow Compass to run ${why[0].toLowerCase()}${why.slice(1)}?`
        : 'Allow Compass to run this command?';
    }
    const file = this.permBasename(p);
    if (p.toolName === 'file_write')
      return file ? `Allow Compass to create ${file}?` : 'Allow Compass to create a file?';
    if (p.toolName === 'file_edit')
      return file ? `Allow Compass to edit ${file}?` : 'Allow Compass to edit a file?';
    if (p.toolName === 'screenshot') return 'Allow Compass to take a screenshot?';
    return `Allow Compass to use ${p.toolName}?`;
  }
  /** One line explaining WHY approval is needed (the risk), Claude-style. */
  permWhy(p: PermissionVM): string {
    const r = (p.reason || '').toLowerCase();
    if (r.includes('destructive')) return 'This command can delete or overwrite data.';
    if (r.includes('substitution'))
      return 'Uses command substitution, which can’t be auto-approved.';
    if (r.includes('parse')) return 'This command couldn’t be parsed safely.';
    if (r.includes('rule')) return 'A workspace rule requires your confirmation.';
    if (p.toolName === 'bash') return 'This runs a command on your machine.';
    if (p.toolName === 'file_write') return 'This creates a new file in your workspace.';
    if (p.toolName === 'file_edit') return 'This edits a file in your workspace.';
    return p.reason || 'This action needs your approval.';
  }
  permTitle(tool: string): string {
    const t: Record<string, string> = {
      bash: 'Compass wants to run a command',
      file_write: 'Compass wants to create a file',
      file_edit: 'Compass wants to edit a file',
      screenshot: 'Compass wants to take a screenshot',
    };
    return t[tool] ?? `Compass wants to use “${tool}”`;
  }
  permBlurb(p: PermissionVM): string {
    if (p.toolName === 'bash')
      return 'This runs a terminal command on your machine — review it, then Allow or Deny.';
    if (p.toolName === 'file_write' || p.toolName === 'file_edit')
      return 'This changes a file in your workspace — review it, then Allow or Deny.';
    return p.reason || 'This action needs your approval — review it, then Allow or Deny.';
  }
  /** Show the actual command/args in a readable form (not raw JSON). */
  permDetail(p: PermissionVM): string {
    try {
      const a = JSON.parse(p.args);
      if (typeof a?.command === 'string') return a.command;
      if (typeof a?.path === 'string') return a.path;
      return Object.entries(a)
        .filter(([k]) => k !== 'description')
        .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
        .join('\n');
    } catch {
      return p.args;
    }
  }
  /** The model's own one-line rationale for this action ("thinking"), if any. */
  permReason(p: PermissionVM): string {
    try {
      const a = JSON.parse(p.args);
      if (typeof a?.description === 'string' && a.description.trim())
        return a.description.trim();
    } catch {
      /* ignore */
    }
    return '';
  }

  // -- repo / branch context menus (like Claude) --------------------------
  private async copy(text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* ignore */
    }
  }
  copyRepoPath(): void {
    this.repoMenuOpen.set(false);
    const p = this.activeWorkspace()?.path;
    if (p) void this.copy(p);
  }
  copyBranchName(): void {
    this.branchMenuOpen.set(false);
    const b = this.gitStatus()?.branch;
    if (b) void this.copy(b);
  }
  openRepoInGithub(): void {
    this.repoMenuOpen.set(false);
    const url = this.gitStatus()?.remote || this.activeWorkspace()?.remote_url;
    if (url) window.open(url, '_blank', 'noopener');
  }
  async revealWorkspace(): Promise<void> {
    this.repoMenuOpen.set(false);
    try {
      await this.api.revealWorkspace(this.activeWorkspaceId());
    } catch {
      /* host-only */
    }
  }
  async openWorkspaceTerminal(): Promise<void> {
    this.repoMenuOpen.set(false);
    this.branchMenuOpen.set(false);
    try {
      await this.api.openWorkspaceTerminal(this.activeWorkspaceId());
    } catch {
      /* host-only */
    }
  }

  /** Refresh the working-tree diff/branch shown in the composer status bar. */
  async loadGitStatus(): Promise<void> {
    try {
      this.gitStatus.set(await this.api.gitStatus(this.activeWorkspaceId()));
    } catch {
      this.gitStatus.set(null);
    }
  }

  /** Show the working-tree diff (like Claude's inline diff view). */
  async openDiff(): Promise<void> {
    this.diffOpen.set(true);
    this.diffBusy.set(true);
    try {
      const { diff } = await this.api.gitDiff(this.activeWorkspaceId());
      this.diffLines.set(
        diff.split('\n').map((text) => {
          let t = 'ctx';
          if (text.startsWith('diff --git') || text.startsWith('index ')) t = 'file';
          else if (text.startsWith('+++') || text.startsWith('---')) t = 'meta';
          else if (text.startsWith('@@')) t = 'hunk';
          else if (text.startsWith('+')) t = 'add';
          else if (text.startsWith('-')) t = 'del';
          return { t, text: text || ' ' };
        }),
      );
    } catch {
      this.diffLines.set([{ t: 'ctx', text: 'Could not load the diff.' }]);
    } finally {
      this.diffBusy.set(false);
    }
  }

  /** Push the branch and open a GitHub PR (backend runs gh). */
  async createPr(opts: { draft?: boolean; manual?: boolean } = {}): Promise<void> {
    this.prMenuOpen.set(false);
    if (this.prBusy()) return;
    this.prBusy.set(true);
    this.prNotice.set('');
    try {
      const res = await this.api.createPr(this.activeWorkspaceId(), opts);
      this.prNotice.set(
        res.manual
          ? 'Opening GitHub…'
          : res.existing
            ? 'PR already open'
            : opts.draft
              ? 'Draft PR created'
              : 'PR created',
      );
      if (res.url) window.open(res.url, '_blank', 'noopener');
      setTimeout(() => this.prNotice.set(''), 4000);
    } catch (err: unknown) {
      const detail =
        (err as { error?: { detail?: string } })?.error?.detail ??
        'Could not create PR';
      this.prNotice.set(detail);
      setTimeout(() => this.prNotice.set(''), 6000);
    } finally {
      this.prBusy.set(false);
    }
  }

  /** Open the active workspace in VS Code. Preferred: the backend launches
   * `code <path>` on its host (same as the Claude Code CLI). Falls back to the
   * vscode:// URI if the backend host has no VS Code CLI. */
  async openInVsCode(): Promise<void> {
    this.userMenuOpen.set(false);
    const id = this.activeWorkspaceId();
    try {
      await this.api.openWorkspaceInVsCode(id);
    } catch {
      const path = this.activeWorkspace()?.path || this.health()?.workspace;
      if (!path) return;
      const p = path.startsWith('/') ? path : '/' + path;
      window.location.href = 'vscode://file' + p;
    }
  }

  /** Open Compass in its own standalone browser window (app-style). */
  openAppWindow(): void {
    this.userMenuOpen.set(false);
    window.open(
      location.origin + location.pathname,
      'compass-app',
      'popup,noopener,width=1440,height=940',
    );
  }

  /** Post an image (data: or /v1/ URL) into the chat as an assistant bubble. */
  postImage(src: string, alt = 'Screenshot'): void {
    this.push(this.bubble('assistant', `![${alt}](${src})`, false));
  }

  readonly shotBusy = signal(false);
  readonly cbMenuOpen = signal(false);
  // Chat image lightbox — open any inline (md-img) or attached (bubble-att-img)
  // image in the shared full-screen viewer (zoom in/out, pan, close).
  readonly lightbox = inject(LightboxService);
  onLogClick(e: MouseEvent): void {
    const t = e.target as HTMLElement;
    if (
      t?.tagName === 'IMG' &&
      (t.classList.contains('md-img') || t.classList.contains('bubble-att-img'))
    ) {
      const img = t as HTMLImageElement;
      this.lightbox.open(img.src, img.alt || 'Image');
    }
  }
  readonly annotateOn = signal(false);
  private readonly annoCanvas =
    viewChild<ElementRef<HTMLCanvasElement>>('annoCanvas');
  private annoDrawing = false;
  private annoLast: { x: number; y: number } | null = null;

  toggleAnnotate(): void {
    this.annotateOn.update((v) => !v);
    // Size after the canvas actually paints. A microtask fires before the
    // zoneless render, so the viewChild is still undefined then — rAF x2 lands
    // after layout. Drawing also re-checks the size on pointerdown as a backstop.
    if (this.annotateOn())
      requestAnimationFrame(() => requestAnimationFrame(() => this.sizeAnnoCanvas()));
  }
  /** Match the canvas backing store to its displayed size (× DPR). A <canvas>
   *  keeps its default 300×150 buffer until this runs, which is why strokes
   *  landed off-canvas before. Only resizes when needed so it never wipes an
   *  in-progress drawing. */
  private sizeAnnoCanvas(): void {
    const c = this.annoCanvas()?.nativeElement;
    if (!c) return;
    const r = c.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(r.width * dpr);
    const h = Math.round(r.height * dpr);
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
      const ctx = c.getContext('2d');
      if (ctx) {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr); // draw in CSS pixels; map to device pixels
      }
    }
  }
  annoStart(e: PointerEvent): void {
    this.sizeAnnoCanvas(); // guarantee correct size before the first stroke
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    this.annoDrawing = true;
    this.annoLast = { x: e.offsetX, y: e.offsetY };
  }
  annoMove(e: PointerEvent): void {
    if (!this.annoDrawing) return;
    const ctx = this.annoCanvas()?.nativeElement.getContext('2d');
    if (!ctx || !this.annoLast) return;
    ctx.strokeStyle = '#ff4d4f';
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(this.annoLast.x, this.annoLast.y);
    ctx.lineTo(e.offsetX, e.offsetY);
    ctx.stroke();
    this.annoLast = { x: e.offsetX, y: e.offsetY };
  }
  annoEnd(): void {
    this.annoDrawing = false;
    this.annoLast = null;
  }
  clearAnno(): void {
    const c = this.annoCanvas()?.nativeElement;
    c?.getContext('2d')?.clearRect(0, 0, c.width, c.height);
  }

  /** Screenshot the current in-app browser URL (headless) and post to chat. */
  async captureBrowserShot(download = false): Promise<void> {
    const url = this.browserAddr();
    if (!url || this.shotBusy()) return;
    this.shotBusy.set(true);
    try {
      const { image } = await this.api.screenshot(url);
      if (download) {
        const a = document.createElement('a');
        a.href = image;
        a.download = 'screenshot.png';
        a.click();
      } else {
        this.postImage(image, url);
      }
    } catch {
      this.push({
        kind: 'notice',
        id: crypto.randomUUID(),
        tone: 'error',
        text: 'Screenshot failed (is the page reachable?).',
      });
    } finally {
      this.shotBusy.set(false);
    }
  }

  /** Launch a Compass Collab app in the in-app browser dock. */
  launchCollab(app: CollabApp): void {
    this.browserAddr.set(app.url);
    this.navigateBrowser();
    this.browserOpen.set(true);
  }

  // -- in-app browser ------------------------------------------------------
  toggleBrowser(): void {
    this.browserOpen.update((v) => !v);
    if (this.browserOpen() && !this.browserAddr()) {
      this.browserAddr.set('https://learn.microsoft.com/azure/architecture/');
      this.navigateBrowser();
    }
  }

  navigateBrowser(): void {
    let u = this.browserAddr().trim();
    if (!u) return;
    if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
    this.browserAddr.set(u);
    this.browserBlocked.set(false);
    this.browserSrc.set(this.sanitizer.bypassSecurityTrustResourceUrl(u));
  }

  reloadBrowser(): void {
    const cur = this.browserAddr();
    this.browserSrc.set('');
    setTimeout(() => {
      if (cur) this.browserSrc.set(this.sanitizer.bypassSecurityTrustResourceUrl(cur));
    }, 0);
  }

  openBrowserExternal(): void {
    const u = this.browserAddr();
    if (u) window.open(u, '_blank', 'noopener');
  }

  /** Expand the docked browser into a full standalone window. */
  expandBrowser(): void {
    const u = this.browserAddr();
    if (u) window.open(u, 'compass-browser', 'popup,width=1440,height=940');
  }

  // -- background tasks ----------------------------------------------------
  async refreshBgTasks(): Promise<void> {
    try {
      const res = await this.api.backgroundTasks();
      this.bgTasks.set(res.tasks);
    } catch {
      /* backend may be briefly unavailable */
    }
  }
  toggleBgPanel(): void {
    this.bgOpen.update((v) => !v);
    if (this.bgOpen()) void this.refreshBgTasks();
  }
  async stopBgTask(t: BackgroundTask): Promise<void> {
    try {
      await this.api.stopBackgroundTask(t.id);
    } finally {
      void this.refreshBgTasks();
    }
  }
  async clearBgFinished(): Promise<void> {
    try {
      await this.api.clearBackgroundTasks();
    } finally {
      void this.refreshBgTasks();
    }
  }
  openBgUrl(t: BackgroundTask): void {
    if (!t.url) return;
    this.browserAddr.set(t.url);
    this.navigateBrowser();
    this.browserOpen.set(true);
  }
  /** Live "18m 26s" / "5s" elapsed label for a task (running counts up). */
  bgElapsed(t: BackgroundTask): string {
    const end = t.status === 'running' ? this.nowTick() : (t.finished_at ?? 0) * 1000;
    const ms = Math.max(0, end - t.started_at * 1000);
    const s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h) return `${h}h ${m}m ${sec}s`;
    if (m) return `${m}m ${sec}s`;
    return `${sec}s`;
  }

  // -- routines: list / builder / detail -----------------------------------
  readonly triggerTypes = ['once', 'hourly', 'daily', 'weekdays', 'weekly', 'custom'] as const;
  readonly weekdayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  async openRoutines(): Promise<void> {
    this.view.set('routines');
    this.routineView.set('list');
    this.browserOpen.set(false);
    this.artifacts.close();
    this.requestNotifyPermission();
    await this.loadRoutines();
  }
  backToChat(): void {
    this.view.set('chat');
  }
  async loadRoutines(): Promise<void> {
    try {
      const res = await this.api.routines();
      this.routines.set(res.routines);
      this.routineTemplates.set(res.templates);
      this.routineSuggestions.set(res.suggestions);
      this.routineConnectorOptions.set(res.connectors ?? []);
    } catch {
      /* ignore */
    }
  }

  // -- builder --------------------------------------------------------------
  private resetForm(): void {
    this.fName.set('');
    this.fInstructions.set('');
    this.fTarget.set(this.newRoutineTarget());
    this.fRepo.set('');
    this.fTriggerType.set('weekdays');
    this.fTriggerTime.set('09:00');
    this.fTriggerDays.set([0]);
    this.fConnectors.set([]);
    this.fAutoFix.set(false);
    this.fNotifyEnabled.set(true);
    this.fNotifyPush.set(true);
    this.fNotifyEmail.set(false);
    this.fNotifySlack.set(false);
    this.fTab.set('connectors');
    this.fEditId.set(null);
    this.triggerOpen.set(true);
    this.timePickerOpen.set(false);
  }
  newRoutine(): void {
    this.resetForm();
    this.routineView.set('builder');
  }
  useSuggestion(text: string): void {
    this.resetForm();
    this.fInstructions.set(text);
    this.fName.set(text.slice(0, 48));
    this.routineView.set('builder');
  }
  draftRoutine(): void {
    const prompt = this.newRoutinePrompt().trim();
    if (!prompt) return;
    this.resetForm();
    this.fInstructions.set(prompt);
    this.fName.set(prompt.slice(0, 48));
    this.newRoutinePrompt.set('');
    this.routineView.set('builder');
  }
  useTemplate(t: RoutineTemplate): void {
    this.resetForm();
    this.fName.set(t.name);
    this.fInstructions.set(t.prompt);
    this.fTriggerType.set(t.trigger_type);
    this.fTriggerTime.set(t.time);
    this.fConnectors.set([...t.integrations]);
    this.routineView.set('builder');
  }
  pickNewRoutineTarget(target: 'local' | 'cloud'): void {
    this.newRoutineTarget.set(target);
    this.fTarget.set(target);
    this.routineMenuOpen.set(false);
    this.newRoutine();
  }
  setTriggerType(t: 'once' | 'hourly' | 'daily' | 'weekdays' | 'weekly' | 'custom'): void {
    this.fTriggerType.set(t);
  }
  toggleTriggerDay(d: number): void {
    this.fTriggerDays.update((days) =>
      days.includes(d) ? days.filter((x) => x !== d) : [...days, d].sort(),
    );
  }
  toggleConnector(c: string): void {
    this.fConnectors.update((cs) => (cs.includes(c) ? cs.filter((x) => x !== c) : [...cs, c]));
  }
  /** 12h label for the current trigger time (picker display). */
  timeLabel12(): string {
    const [h, m] = this.fTriggerTime().split(':').map(Number);
    const ap = h < 12 ? 'AM' : 'PM';
    return `${((h % 12) || 12).toString()}:${(m || 0).toString().padStart(2, '0')} ${ap}`;
  }
  setTimeParts(h12: number, m: number, ap: 'AM' | 'PM'): void {
    let h = h12 % 12;
    if (ap === 'PM') h += 12;
    this.fTriggerTime.set(`${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`);
  }
  get hourOptions(): number[] { return Array.from({ length: 12 }, (_, i) => i + 1); }
  get minuteOptions(): number[] { return Array.from({ length: 60 }, (_, i) => i); }
  triggerSummary(): string {
    const t = this.fTriggerTime();
    const type = this.fTriggerType();
    if (type === 'once') return `Runs once at ${t} GMT+5:30`;
    if (type === 'hourly') return `Runs hourly at :${t.split(':')[1]} GMT+5:30`;
    if (type === 'daily') return `Runs daily at ${t} GMT+5:30`;
    if (type === 'weekdays') return `Runs weekdays at ${t} GMT+5:30`;
    if (type === 'weekly') {
      const d = this.fTriggerDays().map((x) => this.weekdayLabels[x]).join(', ') || 'Mon';
      return `Runs weekly on ${d} at ${t} GMT+5:30`;
    }
    return `Runs on schedule at ${t} GMT+5:30`;
  }
  cancelBuilder(): void {
    if (this.fEditId()) {
      void this.openRoutineDetail(this.fEditId()!);
    } else {
      this.routineView.set('list');
    }
  }
  async saveRoutine(): Promise<void> {
    const name = this.fName().trim();
    const prompt = this.fInstructions().trim();
    if (!name || !prompt || this.routineBusy()) return;
    this.routineBusy.set(true);
    const body: Partial<Routine> = {
      name,
      prompt,
      triggers: [
        {
          type: this.fTriggerType(),
          time: this.fTriggerTime(),
          days: this.fTriggerDays(),
          cron: '',
          date: '',
        },
      ],
      target: this.fTarget(),
      connectors: this.fConnectors(),
      behavior: { auto_fix_prs: this.fAutoFix() },
      notifications: {
        enabled: this.fNotifyEnabled(),
        push: this.fNotifyPush(),
        email: this.fNotifyEmail(),
        slack: this.fNotifySlack(),
      },
    };
    try {
      const editId = this.fEditId();
      const saved = editId
        ? await this.api.updateRoutine(editId, body)
        : await this.api.createRoutine(body);
      await this.loadRoutines();
      await this.openRoutineDetail(saved.id);
    } finally {
      this.routineBusy.set(false);
    }
  }

  /** Ask the browser for notification permission (needs a user gesture in most
   *  browsers — called when opening Routines or toggling push). */
  requestNotifyPermission(): void {
    try {
      if ('Notification' in window && Notification.permission === 'default') {
        void Notification.requestPermission();
      }
    } catch {
      /* unsupported */
    }
  }
  /** Poll finished runs; fire a native notification for push-enabled routines. */
  private async pollRoutineNotifications(): Promise<void> {
    let res;
    try {
      res = await this.api.recentRoutineRuns(this.notifySince);
    } catch {
      return;
    }
    this.emailConfigured.set(res.email_configured);
    for (const run of res.runs) {
      const fin = run.finished_at ?? 0;
      if (fin > this.notifySince) this.notifySince = fin;
      if (!run.notify_enabled || !run.notify_push) continue;
      this.showRunNotification(run);
    }
  }
  private showRunNotification(run: {
    routine_name: string;
    status: string;
    summary: string;
    trigger: string;
  }): void {
    const title = `⚡ ${run.routine_name} — ${run.status}`;
    const body = `${run.trigger} run · ${(run.summary || '').slice(0, 140)}`;
    // Always surface it in-app (works regardless of OS/browser permission)…
    this.showRoutineToast(title);
    // …and also fire a native OS notification when the user has allowed it.
    try {
      if ('Notification' in window && Notification.permission === 'granted') {
        new Notification(title, { body, tag: 'compass-routine' });
      }
    } catch {
      /* unsupported */
    }
  }

  /** Open a routine's detail from the sidebar Routines section. */
  async openRoutineFromSidebar(id: string): Promise<void> {
    this.view.set('routines');
    this.browserOpen.set(false);
    this.artifacts.close();
    await this.openRoutineDetail(id);
  }

  // -- detail ---------------------------------------------------------------
  async openRoutineDetail(id: string): Promise<void> {
    try {
      const r = await this.api.getRoutine(id);
      this.activeRoutine.set(r);
      this.routineView.set('detail');
      await this.loadRuns(id);
    } catch {
      /* ignore */
    }
  }
  async loadRuns(id: string): Promise<void> {
    try {
      this.routineRuns.set((await this.api.routineRuns(id)).runs);
    } catch {
      /* ignore */
    }
  }
  editRoutine(): void {
    const r = this.activeRoutine();
    if (!r) return;
    const tr = r.triggers[0] ?? { type: 'weekdays', time: '09:00', days: [0], cron: '', date: '' };
    this.fName.set(r.name);
    this.fInstructions.set(r.prompt);
    this.fTarget.set(r.target);
    this.fRepo.set(r.repository);
    this.fTriggerType.set(tr.type);
    this.fTriggerTime.set(tr.time);
    this.fTriggerDays.set(tr.days?.length ? tr.days : [0]);
    this.fConnectors.set([...r.connectors]);
    this.fAutoFix.set(r.behavior?.auto_fix_prs ?? false);
    this.fNotifyEnabled.set(r.notifications?.enabled ?? true);
    this.fNotifyPush.set(r.notifications?.push ?? true);
    this.fNotifyEmail.set(r.notifications?.email ?? false);
    this.fNotifySlack.set(r.notifications?.slack ?? false);
    this.fTab.set('connectors');
    this.fEditId.set(r.id);
    this.triggerOpen.set(true);
    this.routineView.set('builder');
  }
  async deleteRoutineDetail(): Promise<void> {
    const r = this.activeRoutine();
    if (!r) return;
    await this.api.deleteRoutine(r.id);
    await this.loadRoutines();
    this.routineView.set('list');
  }
  async toggleRoutineActive(): Promise<void> {
    const r = this.activeRoutine();
    if (!r) return;
    const updated = await this.api.updateRoutine(r.id, { enabled: !r.enabled });
    this.activeRoutine.set(updated);
  }
  async runRoutineNow(): Promise<void> {
    const r = this.activeRoutine();
    if (!r) return;
    this.routineBusy.set(true);
    try {
      await this.api.runRoutineNow(r.id);
      this.showRoutineToast('Workflow run started');
      await this.loadRuns(r.id);
      // Poll so the run flips running -> completed live (server-side gpt-5 runs
      // can take a few minutes).
      for (let i = 0; i < 90; i++) {
        await new Promise((res) => setTimeout(res, 4000));
        if (this.routineView() !== 'detail' || this.activeRoutine()?.id !== r.id) break;
        await this.loadRuns(r.id);
        if (!this.routineRuns().some((x) => x.status === 'running')) break;
      }
    } finally {
      this.routineBusy.set(false);
    }
  }
  private showRoutineToast(msg: string): void {
    this.routineToast.set(msg);
    setTimeout(() => this.routineToast.set(''), 4000);
  }
  /** Open a run's conversation transcript in the chat view. */
  async openRun(run: RoutineRun): Promise<void> {
    if (!run.session_id) return;
    await this.resumeSession(run.session_id);
  }
  runTriggerLabel(t: string): string {
    return t.toUpperCase();
  }
  relTime(epoch: number | null): string {
    if (!epoch) return '';
    const d = new Date(epoch * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const hh = d.getHours().toString().padStart(2, '0');
    const mm = d.getMinutes().toString().padStart(2, '0');
    return sameDay ? `today at ${hh}:${mm}` : `${d.toLocaleDateString()} ${hh}:${mm}`;
  }

  // -- sessions ------------------------------------------------------------

  async refreshSessions(): Promise<void> {
    try {
      this.cards.set((await this.api.listSessions()).sessions);
    } catch {
      /* non-fatal */
    }
  }

  async newSession(): Promise<void> {
    this.view.set('chat');
    this.activeMode.set('default');
    this.activeEffort.set('medium');
    // Keep the currently-selected model and workspace for the new conversation.
    const res = await this.api.createSession({
      permissionMode: 'default',
      effort: 'medium',
      model: this.activeModel() || undefined,
      workspaceId: this.activeWorkspaceId(),
    });
    this.sessionId.set(res.session_id);
    this.timeline.set([]);
    this.usage.set(null);
    this.currentBubble = null;
  }

  async resumeSession(id: string): Promise<void> {
    if (!id) return this.newSession();
    this.view.set('chat');
    this.activeDotColor.set(this.randomDotColor()); // light up this conv's dot
    const card = this.cards().find((c) => c.id === id);
    this.activeMode.set(card?.mode ?? 'default');
    this.activeEffort.set(card?.effort ?? 'medium');
    if (card?.model) this.activeModel.set(card.model);
    if (card?.workspace) this.activeWorkspaceId.set(card.workspace);
    const res = await this.api.createSession({
      resume: true,
      sessionId: id,
      permissionMode: card?.mode,
      effort: card?.effort,
      model: card?.model || this.activeModel() || undefined,
      workspaceId: card?.workspace || this.activeWorkspaceId(),
    });
    this.sessionId.set(res.session_id);
    this.currentBubble = null;
    const t = await this.api.transcript(id);
    const items: TimelineItem[] = [];
    for (const m of t.messages) {
      const meta = m.meta ?? {};
      const at = m.timestamp ? m.timestamp * 1000 : undefined;
      // content may be a multimodal parts array (image attachments) — flatten
      // to text so bubbles always carry a string (a raw array crashes render).
      const text = this.msgText(m.content);
      if (m.role === 'user' && !meta['synthetic'] && !meta['compact_boundary']) {
        items.push(this.bubble('user', text, false, m.uuid, at));
      } else if (m.role === 'assistant' && text) {
        items.push(this.bubble('assistant', text, false, undefined, at));
      }
    }
    this.timeline.set(items);
  }

  /** Plain text from a stored message's content — a string, or the text parts
   *  of a multimodal (image-attachment) content array. */
  private msgText(content: unknown): string {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content
        .filter(
          (p): p is { type: string; text: string } =>
            !!p && typeof p === 'object' && (p as { type?: string }).type === 'text',
        )
        .map((p) => p.text)
        .join(' ');
    }
    return '';
  }

  // -- conversation actions (menu) ----------------------------------------

  toggleSidebar(): void {
    this.sidebarOpen.update((v) => !v);
  }
  // -- conversation search -------------------------------------------------
  openSearch(): void {
    this.searchQuery.set('');
    this.searchIndex.set(0);
    this.searchOpen.set(true);
    requestAnimationFrame(() => this.searchInput()?.nativeElement.focus());
  }
  closeSearch(): void {
    this.searchOpen.set(false);
  }
  onSearchInput(v: string): void {
    this.searchQuery.set(v);
    this.searchIndex.set(0);
  }
  onSearchKeydown(ev: KeyboardEvent): void {
    const n = this.searchResults().length;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      this.closeSearch();
    } else if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      this.searchIndex.update((i) => (n ? (i + 1) % n : 0));
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      this.searchIndex.update((i) => (n ? (i - 1 + n) % n : 0));
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      const c = this.searchResults()[this.searchIndex()];
      if (c) void this.openSearchResult(c);
    }
  }
  async openSearchResult(card: { id: string }): Promise<void> {
    this.closeSearch();
    if (this.section() === 'home') this.openHomeConversation(card.id);
    else await this.resumeSession(card.id);
  }
  /** "Past week" / "Past month" / "Past year" / "Older" bucket for a card. */
  recencyLabel(updatedAt: number): string {
    const days = (Date.now() / 1000 - updatedAt) / 86400;
    if (days < 7) return 'Past week';
    if (days < 31) return 'Past month';
    if (days < 366) return 'Past year';
    return 'Older';
  }

  readonly menuX = signal(0);
  readonly menuY = signal(0);
  openMenu(id: string, ev: Event): void {
    ev.stopPropagation();
    if (this.menuOpenId() === id) {
      this.menuOpenId.set(null);
      return;
    }
    // Anchor with fixed coords from the button so the menu escapes the
    // conversation scroller's clipping and floats on top.
    const r = (ev.currentTarget as HTMLElement).getBoundingClientRect();
    this.menuX.set(r.right);
    this.menuY.set(r.bottom + 3);
    this.closeAllMenus();
    this.menuOpenId.set(id);
  }
  closeMenu(): void {
    this.menuOpenId.set(null);
  }
  /** Close every dropdown — bound to a document click so any outside click
   *  dismisses open menus. Toggles stopPropagation so they aren't re-closed. */
  closeAllMenus(): void {
    this.menuOpenId.set(null);
    this.userMenuOpen.set(false);
    this.repoMenuOpen.set(false);
    this.branchMenuOpen.set(false);
    this.prMenuOpen.set(false);
    this.routineMenuOpen.set(false);
    this.cbMenuOpen.set(false);
  }
  onGlobalClick(): void {
    this.closeAllMenus();
  }
  /** Open the host's native folder chooser (macOS Finder) and fill the path. */
  async pickWorkspaceFolder(): Promise<void> {
    try {
      const { path } = await this.api.pickFolder();
      if (path) this.newFolderPath.set(path);
    } catch {
      /* host-only / cancelled */
    }
  }

  async togglePin(card: SessionCard, ev?: Event): Promise<void> {
    ev?.stopPropagation();
    this.closeMenu();
    await this.api.updateSession(card.id, { pinned: !card.pinned });
    await this.refreshSessions();
  }

  async toggleArchive(card: SessionCard): Promise<void> {
    this.closeMenu();
    await this.api.updateSession(card.id, { archived: !card.archived });
    await this.refreshSessions();
  }

  startRename(card: SessionCard): void {
    this.closeMenu();
    this.renamingId.set(card.id);
  }
  async commitRename(value: string): Promise<void> {
    const id = this.renamingId();
    const title = value.trim();
    this.renamingId.set(null);
    if (id && title) {
      await this.api.updateSession(id, { title });
      await this.refreshSessions();
    }
  }

  startMoveGroup(card: SessionCard): void {
    this.closeMenu();
    this.groupingId.set(card.id);
  }
  async commitMoveGroup(value: string): Promise<void> {
    const id = this.groupingId();
    const group = value.trim();
    this.groupingId.set(null);
    if (id) {
      await this.api.updateSession(id, { group });
      if (group && this.groupBy() === 'none') this.groupBy.set('group');
      await this.refreshSessions();
    }
  }

  async forkConversation(card: SessionCard): Promise<void> {
    this.closeMenu();
    const { session_id } = await this.api.forkSession(card.id);
    await this.refreshSessions();
    await this.resumeSession(session_id);
  }

  async deleteConversation(card: SessionCard): Promise<void> {
    this.closeMenu();
    await this.api.deleteSession(card.id);
    await this.refreshSessions();
    if (this.sessionId() === card.id) await this.newSession();
  }

  // -- mode / effort -------------------------------------------------------

  async setMode(mode: string): Promise<void> {
    this.activeMode.set(mode);
    const sid = this.sessionId();
    if (sid) {
      await this.api.updateSession(sid, { mode });
      await this.refreshSessions();
    }
  }
  async setEffort(effort: string): Promise<void> {
    this.activeEffort.set(effort);
    const sid = this.sessionId();
    if (sid) {
      await this.api.updateSession(sid, { effort });
      await this.refreshSessions();
    }
  }

  async setModel(model: string): Promise<void> {
    this.activeModel.set(model);
    const sid = this.sessionId();
    if (sid) {
      await this.api.updateSession(sid, { model });
      await this.refreshSessions();
    }
  }

  // -- workspaces ----------------------------------------------------------

  toggleWorkspacePanel(): void {
    this.workspacePanelOpen.update((v) => !v);
  }

  async selectWorkspace(ws: Workspace): Promise<void> {
    this.activeWorkspaceId.set(ws.id);
    const sid = this.sessionId();
    // If the current conversation has no messages yet, just retarget it;
    // otherwise open a fresh conversation in the new workspace.
    if (sid && this.timeline().length === 0) {
      await this.api.updateSession(sid, { workspace: ws.id });
    } else {
      await this.newSession();
    }
    this.workspacePanelOpen.set(false);
    void this.loadGitStatus();
  }

  async addFolder(): Promise<void> {
    const name = this.newFolderName().trim();
    if (!name) return;
    this.workspaceBusy.set('Creating folder…');
    try {
      const ws = await this.api.addFolderWorkspace({ name });
      this.newFolderName.set('');
      await this.refreshWorkspaces();
      await this.selectWorkspace(ws);
    } catch (err) {
      this.workspaceBusy.set(`Failed: ${err}`);
      return;
    } finally {
      this.workspaceBusy.set(null);
    }
  }

  readonly newFolderPath = signal('');
  /** Add an existing local folder by its absolute path, then switch to it. */
  async addFolderPath(): Promise<void> {
    const path = this.newFolderPath().trim();
    if (!path) return;
    this.workspaceBusy.set('Adding folder…');
    try {
      const ws = await this.api.addFolderWorkspace({ path });
      this.newFolderPath.set('');
      await this.refreshWorkspaces();
      await this.selectWorkspace(ws);
    } catch (err: unknown) {
      const detail =
        (err as { error?: { detail?: string } })?.error?.detail ?? String(err);
      this.workspaceBusy.set(`Failed: ${detail}`);
      setTimeout(() => this.workspaceBusy.set(null), 4000);
      return;
    } finally {
      if (!this.workspaceBusy()?.startsWith('Failed')) this.workspaceBusy.set(null);
    }
  }

  async loadGithubRepos(): Promise<void> {
    if (!this.githubEnabled()) return;
    this.githubLoading.set(true);
    try {
      this.githubRepos.set((await this.api.githubRepos()).repos);
    } catch (err) {
      this.workspaceBusy.set(`GitHub: ${err}`);
    } finally {
      this.githubLoading.set(false);
    }
  }

  async cloneRepo(repo: GithubRepo): Promise<void> {
    this.workspaceBusy.set(`Cloning ${repo.full_name}…`);
    try {
      const ws = await this.api.githubClone(repo.full_name, repo.default_branch);
      await this.refreshWorkspaces();
      await this.selectWorkspace(ws);
    } catch (err) {
      this.workspaceBusy.set(`Clone failed: ${err}`);
      return;
    } finally {
      this.workspaceBusy.set(null);
    }
  }

  async removeWorkspace(ws: Workspace, ev: Event): Promise<void> {
    ev.stopPropagation();
    if (ws.id === 'default') return;
    await this.api.deleteWorkspace(ws.id);
    if (this.activeWorkspaceId() === ws.id) this.activeWorkspaceId.set('default');
    await this.refreshWorkspaces();
  }

  // -- sending / editing / regenerating -----------------------------------

  async send(): Promise<void> {
    if (!this.canSend()) return;
    const content = this.draft().trim();
    const sid = this.sessionId();
    if (!sid) return;
    const atts = this.attachments();
    this.draft.set('');
    this.attachments.set([]);
    const b = this.bubble('user', content);
    if (atts.length) b.atts = atts;
    this.push(b);
    const payload = toWire(atts);
    await this.runStream(sid, (cb) =>
      this.api.streamMessage(sid, content, cb, payload),
    );
  }

  async regenerate(): Promise<void> {
    const sid = this.sessionId();
    if (!sid || this.streaming()) return;
    // Drop everything after the last user bubble, then re-run.
    this.timeline.update((items) => {
      let lastUser = -1;
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i];
        if (it.kind === 'bubble' && it.role === 'user') {
          lastUser = i;
          break;
        }
      }
      return lastUser >= 0 ? items.slice(0, lastUser + 1) : items;
    });
    await this.runStream(sid, (cb) => this.api.streamRegenerate(sid, cb));
  }

  startEdit(bubble: ChatBubble): void {
    this.patch(bubble.id, (b) => ({
      ...(b as ChatBubble),
      editing: true,
    }));
    this.draft.set(''); // avoid confusion with composer
  }
  cancelEdit(bubble: ChatBubble): void {
    this.patch(bubble.id, (b) => ({ ...(b as ChatBubble), editing: false }));
  }
  async commitEdit(bubble: ChatBubble, newText: string): Promise<void> {
    const sid = this.sessionId();
    const text = newText.trim();
    if (!sid || !text || !bubble.msgUuid) {
      this.cancelEdit(bubble);
      return;
    }
    // Truncate the timeline at the edited bubble, replace with the new prompt.
    this.timeline.update((items) => {
      const idx = items.findIndex((it) => it.id === bubble.id);
      const head = idx >= 0 ? items.slice(0, idx) : items;
      return [...head, this.bubble('user', text)];
    });
    await this.runStream(sid, (cb) =>
      this.api.streamEdit(sid, bubble.msgUuid!, text, cb),
    );
  }

  /** Shared streaming driver used by send/edit/regenerate. */
  private async runStream(
    sid: string,
    start: (cb: (ev: CompassEvent) => void) => Promise<void>,
  ): Promise<void> {
    this.streaming.set(true);
    this.thinking.set(true);
    this.turnAborted = false;
    this.textSmoother?.cancel();
    this.textSmoother = null;
    this.lastAssistantText = '';
    this.currentBubble = null;
    this.turnStartMs = performance.now();
    this.turnStartCompletion = this.usage()?.completionTokens ?? 0;
    this.elapsedMs.set(0);
    try {
      await start((ev) => this.onEvent(ev));
    } catch (err) {
      this.push({
        kind: 'notice',
        id: crypto.randomUUID(),
        tone: 'error',
        text: String(err),
      });
    } finally {
      this.streaming.set(false);
      this.thinking.set(false);
      // Freeze every loading animation: a turn that was stopped mid-stream
      // never emits the assistant_message that would clear a bubble's
      // streaming flag, so clear them all here.
      this.clearStreamingFlags();
      this.autoOpenArtifact();
      await this.backfillUuids(sid);
      await this.refreshSessions();
      void this.loadGitStatus();
    }
  }

  /** When a completed response contains an artifact, open it in the panel —
   * the way Claude reveals an artifact as soon as it's produced. */
  private autoOpenArtifact(): void {
    // Use the authoritative full reply text (the smoother may still be animating
    // the bubble's last characters, which would truncate a closing code fence).
    const text = this.lastAssistantText;
    if (!text) return;
    const art = ArtifactService.extract(text);
    if (art) this.artifacts.open(art);
  }

  /** Turn off any lingering streaming/caret/star animation. */
  private clearStreamingFlags(): void {
    this.currentBubble = null;
    this.timeline.update((items) =>
      items.map((it) =>
        it.kind === 'bubble' && (it as ChatBubble).streaming
          ? { ...(it as ChatBubble), streaming: false }
          : it,
      ),
    );
  }

  /** After a turn, assign server message uuids to user bubbles in order so
   *  they can be edited. */
  private async backfillUuids(sid: string): Promise<void> {
    try {
      const t = await this.api.transcript(sid);
      const uuids = t.messages
        .filter(
          (m) =>
            m.role === 'user' &&
            !(m.meta ?? {})['synthetic'] &&
            !(m.meta ?? {})['compact_boundary'],
        )
        .map((m) => m.uuid);
      let i = 0;
      this.timeline.update((items) =>
        items.map((it) => {
          if (it.kind === 'bubble' && it.role === 'user') {
            const u = uuids[i++];
            return u ? { ...it, msgUuid: u } : it;
          }
          return it;
        }),
      );
    } catch {
      /* best effort */
    }
  }

  abort(): void {
    const sid = this.sessionId();
    if (sid) void this.api.abort(sid);
    // Reflect the stop immediately — don't wait for the stream to unwind:
    // clear loading state, drop the Stop button, and ignore any in-flight
    // events so no stray tokens land after the user stopped.
    this.turnAborted = true;
    this.textSmoother?.cancel();
    this.textSmoother = null;
    this.streaming.set(false);
    this.thinking.set(false);
    this.clearStreamingFlags();
  }

  async resolve(perm: PermissionVM, behavior: 'allow' | 'deny'): Promise<void> {
    const sid = this.sessionId();
    if (!sid) return;
    await this.api.resolvePermission(sid, perm.id, behavior);
    this.patch(perm.id, (p) => ({ ...(p as PermissionVM), resolved: behavior }));
  }

  /** The newest unresolved permission — target of keyboard shortcuts. */
  readonly pendingPerm = computed<PermissionVM | null>(() => {
    const items = this.timeline();
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (it.kind === 'permission' && !(it as PermissionVM).resolved)
        return it as PermissionVM;
    }
    return null;
  });

  /** 1 = Deny, 2 / ⌘↵ = Allow once — mirrors Claude's approval shortcuts. */
  onGlobalKeydown(ev: KeyboardEvent): void {
    // ⌘K / Ctrl+K toggles conversation search from anywhere.
    if ((ev.metaKey || ev.ctrlKey) && (ev.key === 'k' || ev.key === 'K')) {
      ev.preventDefault();
      this.searchOpen() ? this.closeSearch() : this.openSearch();
      return;
    }
    const p = this.pendingPerm();
    if (!p) return;
    const el = ev.target as HTMLElement | null;
    const typing =
      el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
    if ((ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) || (ev.key === '2' && !typing)) {
      ev.preventDefault();
      void this.resolve(p, 'allow');
    } else if (ev.key === '1' && !typing) {
      ev.preventDefault();
      void this.resolve(p, 'deny');
    }
  }

  onKeydown(ev: KeyboardEvent): void {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      void this.send();
    }
  }

  // -- SSE event reducer ---------------------------------------------------

  private onEvent(ev: CompassEvent): void {
    // Once the user has stopped the turn, drop any in-flight events so no
    // stray tokens or animations resume.
    if (this.turnAborted) return;
    const agentId = (ev['agent_id'] as string | null) ?? null;
    // First sign of real output dismisses the thinking loader.
    if (
      this.thinking() &&
      (ev.type === 'text_delta' ||
        ev.type === 'tool_call_started' ||
        ev.type === 'permission_request' ||
        ev.type === 'assistant_message')
    ) {
      this.thinking.set(false);
    }
    switch (ev.type) {
      case 'text_delta': {
        if (agentId) return;
        if (!this.currentBubble) {
          this.currentBubble = this.bubble('assistant', '', true);
          this.push(this.currentBubble);
          // Reveal tokens smoothly (rAF-paced) rather than per-network-chunk.
          const id = this.currentBubble.id;
          this.textSmoother = new SmoothText((t) =>
            this.patch(id, (b) => ({ ...(b as ChatBubble), text: t })),
          );
        }
        this.textSmoother?.push((ev['text'] as string) ?? '');
        break;
      }
      case 'assistant_message':
        if (!agentId && this.currentBubble) {
          const id = this.currentBubble.id;
          // Keep the authoritative full text for artifact extraction while the
          // smoother animates the last few characters into the bubble.
          this.lastAssistantText =
            (ev['content'] as string) ?? this.textSmoother?.fullText ?? '';
          this.textSmoother?.finish();
          this.textSmoother = null;
          this.patch(id, (b) => ({ ...(b as ChatBubble), streaming: false }));
          this.currentBubble = null;
        }
        break;
      case 'tool_call_started': {
        const name = (ev['tool_name'] as string) ?? 'tool';
        this.push({
          kind: 'tool',
          id: (ev['tool_call_id'] as string) ?? crypto.randomUUID(),
          name,
          args: JSON.stringify(ev['arguments'] ?? {}),
          output: '',
          status: 'running',
          agentId,
          isMcp: name.startsWith('mcp__'),
        });
        break;
      }
      case 'tool_progress':
        this.patch(ev['tool_call_id'] as string, (c) => ({
          ...(c as ToolCardVM),
          output: (c as ToolCardVM).output + ((ev['data'] as string) ?? ''),
        }));
        break;
      case 'tool_result': {
        this.patch(ev['tool_call_id'] as string, (c) => ({
          ...(c as ToolCardVM),
          status: (ev['is_error'] as boolean) ? 'error' : 'ok',
          durationMs: ev['duration_ms'] as number,
          output: (ev['is_error'] as boolean)
            ? ((ev['content'] as string) ?? (c as ToolCardVM).output)
            : (c as ToolCardVM).output,
        }));
        // A screenshot tool result posts the captured image into the chat.
        const shot = /screenshot:\/\/([a-f0-9]+)/.exec(
          (ev['content'] as string) ?? '',
        );
        if (shot && !(ev['is_error'] as boolean)) {
          this.postImage('/v1/screenshot-cache/' + shot[1], 'Screenshot');
        }
        break;
      }
      case 'permission_request':
        this.push({
          kind: 'permission',
          id: (ev['request_id'] as string) ?? crypto.randomUUID(),
          toolCallId: (ev['tool_call_id'] as string) ?? '',
          toolName: (ev['tool_name'] as string) ?? 'tool',
          args: JSON.stringify(ev['arguments'] ?? {}),
          reason: (ev['reason'] as string) ?? '',
          agentId,
        });
        break;
      case 'compaction':
        this.push({
          kind: 'notice',
          id: crypto.randomUUID(),
          tone: 'compaction',
          text: `context compacted (${ev['stage']}): ${ev['tokens_before']} → ${ev['tokens_after']} tokens`,
        });
        break;
      case 'usage_report':
        this.usage.set({
          promptTokens: (ev['prompt_tokens'] as number) ?? 0,
          cachedPromptTokens: (ev['cached_prompt_tokens'] as number) ?? 0,
          completionTokens: (ev['completion_tokens'] as number) ?? 0,
          costUsd: (ev['cost_usd'] as number) ?? 0,
        });
        break;
      case 'turn_complete': {
        // Attach per-response duration + output-token count to the last
        // assistant bubble (Claude shows these under the message).
        const ms = Math.round(performance.now() - this.turnStartMs);
        const tokens = Math.max(
          0,
          (this.usage()?.completionTokens ?? 0) - this.turnStartCompletion,
        );
        const lastId = this.lastAssistantId();
        if (lastId) {
          this.patch(lastId, (b) => ({
            ...(b as ChatBubble),
            stats: { ms, tokens },
          }));
        }
        this.push({
          kind: 'notice',
          id: crypto.randomUUID(),
          tone: 'complete',
          text: `${ev['reason']} · ${ev['turns']} turns`,
        });
        break;
      }
      case 'error':
        this.push({
          kind: 'notice',
          id: crypto.randomUUID(),
          tone: 'error',
          text: (ev['message'] as string) ?? 'unknown error',
        });
        break;
    }
  }

  // -- timeline helpers ----------------------------------------------------

  private bubble(
    role: 'user' | 'assistant',
    text: string,
    streaming = false,
    msgUuid?: string,
    at?: number,
  ): ChatBubble {
    return {
      kind: 'bubble',
      id: crypto.randomUUID(),
      role,
      text,
      streaming,
      msgUuid,
      at: at ?? (role === 'user' ? Date.now() : undefined),
    };
  }

  private push(item: TimelineItem): void {
    this.timeline.update((t) => [...t, item]);
  }

  private patch(id: string, fn: (item: TimelineItem) => TimelineItem): void {
    this.timeline.update((t) => t.map((it) => (it.id === id ? fn(it) : it)));
  }

  // -- message actions: copy, read-aloud, formatting ----------------------

  async copyBubble(b: ChatBubble): Promise<void> {
    let ok = false;
    try {
      await navigator.clipboard.writeText(b.text);
      ok = true;
    } catch {
      ok = this.execCopy(b.text);
    }
    if (ok) {
      this.copiedId.set(b.id);
      setTimeout(() => {
        if (this.copiedId() === b.id) this.copiedId.set(null);
      }, 1400);
    }
  }

  private execCopy(text: string): boolean {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }

  /** Read the response aloud. Prefers the expressive Azure TTS voice; if that
   * isn't deployed (503) or fails, falls back to the browser voice. Toggling
   * the same bubble stops playback. */
  async toggleSpeak(b: ChatBubble): Promise<void> {
    if (this.speakingId() === b.id || this.speakLoadingId() === b.id) {
      this.stopSpeaking();
      return;
    }
    this.stopSpeaking();

    if (this.health()?.tts) {
      this.speakLoadingId.set(b.id);
      try {
        const blob = await this.api.synthesizeSpeech(
          this.plainText(b.text),
          this.activeVoice() || undefined,
        );
        // A newer request may have superseded this one while we awaited.
        if (this.speakLoadingId() !== b.id) {
          return;
        }
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        this.currentAudio = audio;
        const done = () => {
          URL.revokeObjectURL(url);
          if (this.currentAudio === audio) this.currentAudio = null;
          if (this.speakingId() === b.id) this.speakingId.set(null);
        };
        audio.onended = done;
        audio.onerror = done;
        this.speakLoadingId.set(null);
        this.speakingId.set(b.id);
        await audio.play();
        return;
      } catch {
        // TTS not deployed / failed — quietly fall back to the browser voice.
        this.speakLoadingId.set(null);
      }
    }
    this.browserSpeak(b);
  }

  setVoice(voice: string): void {
    this.activeVoice.set(voice);
    try {
      localStorage.setItem('compass-tts-voice', voice);
    } catch {
      /* private mode — in-memory only */
    }
  }

  private loadVoicePref(): string {
    try {
      return localStorage.getItem('compass-tts-voice') ?? '';
    } catch {
      return '';
    }
  }

  private browserSpeak(b: ChatBubble): void {
    const synth = window.speechSynthesis;
    if (!synth) return;
    synth.cancel();
    const u = new SpeechSynthesisUtterance(this.plainText(b.text));
    u.rate = 1.02;
    u.onend = () => {
      if (this.speakingId() === b.id) this.speakingId.set(null);
    };
    u.onerror = () => {
      if (this.speakingId() === b.id) this.speakingId.set(null);
    };
    this.speakingId.set(b.id);
    synth.speak(u);
  }

  private stopSpeaking(): void {
    if (this.currentAudio) {
      this.currentAudio.pause();
      this.currentAudio = null;
    }
    window.speechSynthesis?.cancel();
    this.speakingId.set(null);
    this.speakLoadingId.set(null);
  }

  readonly speechSupported =
    typeof window !== 'undefined' && 'speechSynthesis' in window;

  /** Strip Markdown noise so speech and clipboard-free reads are clean. */
  private plainText(md: string): string {
    return md
      .replace(/```[\s\S]*?```/g, ' (code block) ')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/[*_#>]/g, '')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/\n{2,}/g, '. ')
      .trim();
  }

  /** "2:14 PM" for a hover timestamp. */
  formatTime(ms: number | undefined): string {
    if (!ms) return '';
    return new Date(ms).toLocaleTimeString([], {
      hour: 'numeric',
      minute: '2-digit',
    });
  }

  formatDuration(ms: number): string {
    return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)}s`;
  }

  asBubble = (i: TimelineItem): ChatBubble => i as ChatBubble;
  asTool = (i: TimelineItem): ToolCardVM => i as ToolCardVM;
  asPerm = (i: TimelineItem): PermissionVM => i as PermissionVM;
  asNotice = (i: TimelineItem): NoticeVM => i as NoticeVM;

  trackItem = (_: number, i: TimelineItem): string => i.id;
  trackCard = (_: number, c: SessionCard): string => c.id;
}
