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
import { NgTemplateOutlet } from '@angular/common';
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
import {
  ChatBubble,
  CompassEvent,
  GitStatus,
  GithubRepo,
  GroupBy,
  HealthInfo,
  NoticeVM,
  PermissionVM,
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
    TiltDirective,
    BlurOnChange,
    CompassMark,
    LoadingRadar,
    Markdown,
    ArtifactPanel,
  ],
  templateUrl: './app.html',
  styleUrl: './app.css',
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

  // -- sidebar / history
  readonly sidebarOpen = signal(true);
  readonly cards = signal<SessionCard[]>([]);
  readonly groupBy = signal<GroupBy>('none');
  readonly sortBy = signal<SortBy>('recent');
  readonly showArchived = signal(false);
  readonly historyMenuOpen = signal(false);

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
  readonly canSend = computed(
    () => this.draft().trim().length > 0 && !this.streaming(),
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
      const isBackground =
        it.kind === 'tool' ||
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
  readonly pinnedGroup = computed<SessionGroup | null>(() => {
    const pins = this.cards().filter((c) => c.pinned && !c.archived);
    return pins.length
      ? { label: 'Pinned', cards: this.sortCards(pins) }
      : null;
  });

  /** The remaining conversations, grouped and sorted per the controls. */
  readonly groups = computed<SessionGroup[]>(() => {
    const rest = this.cards().filter(
      (c) => !c.pinned && (this.showArchived() ? true : !c.archived),
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
        el?.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
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
  async createPr(): Promise<void> {
    if (this.prBusy()) return;
    this.prBusy.set(true);
    this.prNotice.set('');
    try {
      const res = await this.api.createPr(this.activeWorkspaceId());
      this.prNotice.set(res.existing ? 'PR already open' : 'PR created');
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

  // -- sessions ------------------------------------------------------------

  async refreshSessions(): Promise<void> {
    try {
      this.cards.set((await this.api.listSessions()).sessions);
    } catch {
      /* non-fatal */
    }
  }

  async newSession(): Promise<void> {
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
      if (m.role === 'user' && !meta['synthetic'] && !meta['compact_boundary']) {
        items.push(this.bubble('user', m.content ?? '', false, m.uuid, at));
      } else if (m.role === 'assistant' && m.content) {
        items.push(this.bubble('assistant', m.content, false, undefined, at));
      }
    }
    this.timeline.set(items);
  }

  // -- conversation actions (menu) ----------------------------------------

  toggleSidebar(): void {
    this.sidebarOpen.update((v) => !v);
  }
  openMenu(id: string, ev: Event): void {
    ev.stopPropagation();
    this.menuOpenId.update((cur) => (cur === id ? null : id));
  }
  closeMenu(): void {
    this.menuOpenId.set(null);
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
    this.draft.set('');
    this.push(this.bubble('user', content));
    await this.runStream(sid, (cb) => this.api.streamMessage(sid, content, cb));
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
    const items = this.timeline();
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (it.kind === 'bubble' && it.role === 'assistant' && it.text) {
        const art = ArtifactService.extract(it.text);
        if (art) this.artifacts.open(art);
        return; // only consider the most recent assistant message
      }
    }
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
        }
        const text = (ev['text'] as string) ?? '';
        this.patch(this.currentBubble.id, (b) => ({
          ...(b as ChatBubble),
          text: (b as ChatBubble).text + text,
        }));
        break;
      }
      case 'assistant_message':
        if (!agentId && this.currentBubble) {
          this.patch(this.currentBubble.id, (b) => ({
            ...(b as ChatBubble),
            streaming: false,
          }));
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
      case 'tool_result':
        this.patch(ev['tool_call_id'] as string, (c) => ({
          ...(c as ToolCardVM),
          status: (ev['is_error'] as boolean) ? 'error' : 'ok',
          durationMs: ev['duration_ms'] as number,
          output: (ev['is_error'] as boolean)
            ? ((ev['content'] as string) ?? (c as ToolCardVM).output)
            : (c as ToolCardVM).output,
        }));
        break;
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
