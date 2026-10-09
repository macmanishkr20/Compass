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
import { AuthService } from './auth.service';
import { CompassApiService, TranscriptResponse, describeHttpError } from './compass-api.service';
import { LoadError } from './load-error';
import { ThemeService } from './theme.service';
import { ModuleKey, TurnNotifyService } from './turn-notify.service';
import { TurnStatus } from './turn-status';
import { modelLabel } from './model-label';
import { MissionActivityService } from './missions/mission-activity.service';
import { BlurOnChange } from './blur-on-change.directive';
import { CompassMark } from './compass-mark/compass-mark';
import { LoadingRadar } from './loading-radar/loading-radar';
import { Markdown } from './markdown/markdown';
import { ArtifactPanel } from './artifact-panel/artifact-panel';
import { ArtifactService } from './artifact.service';
import { HomeChat } from './home-chat/home-chat';
import { Design } from './design/design';
import { BusinessFunctions } from './business-functions/business-functions';
import { BusinessFunctionsLayout } from './business-functions/bf-layout.service';
import { Pipelines } from './pipelines/pipelines';
import { Estimate } from './estimate/estimate';
import { Missions } from './missions/missions';
import { Confirm } from './confirm/confirm';
import { ConfirmService } from './confirm.service';
import { Lightbox } from './lightbox/lightbox';
import { NoticeService } from './notice.service';
import { NoticeStack } from './notice/notice';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { LightboxService } from './lightbox.service';
import { BrowserSelectService } from './browser-select.service';
import { getPref, setPref } from './prefs';
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
  CustomizeInfo,
  FileEntry,
  FileHit,
  MemoryEntry,
  NoticeVM,
  PermissionVM,
  PreviewCardVM,
  Recap,
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
  QuestionVM,
} from './models';

const MODES = ['default', 'accept_edits', 'plan', 'bypass'] as const;
/** Named from the viewport table itself, so a size added there cannot
 *  leave the type behind — which is how the first four were added and
 *  then rejected by the compiler one at a time. */
type ViewportName =
  | 'responsive' | 'mobile' | 'tablet'
  | 'laptop' | 'desktop' | 'monitor';
/** The fallback ladder, for before /healthz answers and for a deployment the
 *  server did not describe. It is deliberately the three levels every family
 *  measured so far accepts: the real list comes from the server, because the
 *  deployed families take overlapping but different ladders and any list
 *  hardcoded here offers one of them a level the API answers with a 400.
 *  Higher means it thinks more often and goes further. */
const EFFORTS = ['low', 'medium', 'high'] as const;

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

/** How close to the bottom still counts as following the stream. Larger than
 *  the few pixels one token adds, far smaller than a deliberate scroll. */
const FOLLOW_SLACK = 120;

/** Is there anything below the fold to jump down to? A pane that isn't
 *  scrollable — an empty thread, or one shorter than the viewport — has
 *  nothing to jump to, and during first layout clientHeight is briefly 0,
 *  which would otherwise read as "miles from the bottom" and flash the
 *  chevron on a thread the user never scrolled. */
function scrolledUp(el: HTMLElement): boolean {
  if (el.clientHeight === 0) return false;
  return el.scrollHeight - el.scrollTop - el.clientHeight > FOLLOW_SLACK;
}


/** How many conversations a page of the sidebar list shows. The list opens at
 *  one page and grows by one; "Show less" folds it back to exactly this. */
const CONV_PAGE = 4;

@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule,
    LoadError,
    NgTemplateOutlet,
    TitleCasePipe,
    BlurOnChange,
    CompassMark,
    LoadingRadar,
    Markdown,
    ArtifactPanel,
    HomeChat,
    Design,
    Pipelines,
    Estimate,
    Missions,
    Lightbox,
    Confirm,
    NoticeStack,
    BusinessFunctions,
  ],
  templateUrl: './app.html',
  styleUrl: './app.css',
  host: {
    '(document:keydown)': 'onGlobalKeydown($event)',
    '(document:click)': 'onGlobalClick()',
    '(window:resize)': 'measureAtts()',
  },
})
export class App {
  private readonly api = inject(CompassApiService);
  private readonly confirm = inject(ConfirmService);
  private readonly notice = inject(NoticeService);
  readonly theme = inject(ThemeService);
  readonly auth = inject(AuthService);
  readonly artifacts = inject(ArtifactService);
  private readonly sanitizer = inject(DomSanitizer);

  /** What the topbar calls the thing you are looking at.
   *
   *  A total map over ModuleKey, not a switch with a default. The switch this
   *  replaces was itself written to cure a fall-through, and claimed a branch
   *  per section could not develop one again — then Estimate and Missions were
   *  added, landed in `default`, and wore the Code console's conversation
   *  title. Standing in Missions, which has no conversations at all, the
   *  topbar read "New conversation". A map cannot fall through: a seventh
   *  ModuleKey fails to compile until it is named here.
   *
   *  Every section now says its own name, so the label always agrees with the
   *  lit tab beside it. Code's active conversation is not lost by this — the
   *  sidebar list and ⌘K search both name it, and they mark which one is open,
   *  which the crumb never did. Home reads "Chat" because that is what the
   *  section is called on screen. */
  private static readonly TITLES: Record<ModuleKey, string> = {
    home: 'Compass Chat',
    code: 'Compass Code',
    design: 'Compass Design',
    pipelines: 'Compass Pipelines',
    estimate: 'Compass Estimate',
    missions: 'Compass Missions',
    'business-functions': 'Compass Business Functions',
  };

  readonly sectionTitle = computed(() => App.TITLES[this.section()]);

  // In-app browser ("Compass's own browser", like Claude's preview pane).
  readonly browserOpen = signal(false);
  readonly browserAddr = signal('');
  // Maximize the browser pane in-app (float it over the chat), like claude.ai's
  // ⤢ — not a separate OS window. A shrink button restores the docked size.
  readonly browserExpanded = signal(false);
  // Live JPEG frame from the server-side Chromium (data: URI) + last error.
  readonly rbFrame = signal<string>('');
  readonly rbError = signal<string>('');
  // Select/inspect tool (arrow): Chromium's DevTools element overlay follows
  // the cursor. Viewport tool (device): render at a device size like claude.ai.
  readonly browserSelect = signal(false);
  readonly browserViewport = signal<ViewportName>('responsive');
  readonly cbViewMenuOpen = signal(false);
  // Select-tool highlight: pixel rect (within .cb-view) + label of the element
  // under the cursor, drawn as a DevTools-style overlay.
  readonly inspectInfo = signal<{
    left: number; top: number; w: number; h: number;
    tag: string; sub: string; dim: string; role: string;
    name: string; focusable: boolean;
  } | null>(null);
  readonly inspectView = computed(() =>
    this.browserSelect() ? this.inspectInfo() : null,
  );
  // Picked elements from the Select tool → chips in the Home composer.
  readonly browserSelectSvc = inject(BrowserSelectService);
  /** The sizes the viewport menu offers.
   *
   *  `responsive` means the pane itself, which is the honest default: the
   *  page gets exactly the room it has. The rest are real devices, rendered
   *  at their own size and scaled down to fit — so a site can be checked at a
   *  monitor's width from a pane that is nowhere near that wide, which is the
   *  only way to see a desktop layout in a side panel at all. */
  private static readonly VIEWPORTS = {
    responsive: null,
    mobile: { w: 375, h: 812 },
    tablet: { w: 768, h: 1024 },
    laptop: { w: 1280, h: 800 },
    desktop: { w: 1440, h: 900 },
    monitor: { w: 1920, h: 1080 },
  } as const;
  readonly deviceAspect = computed(() => {
    const d = App.VIEWPORTS[this.browserViewport()];
    return d ? `${d.w} / ${d.h}` : null;
  });
  private rbWs: WebSocket | null = null;
  private rbMoveTs = 0;
  /** Watches the browser pane so the server's viewport keeps matching it.
   *
   *  The frame is painted with `object-fit: fill`, which is exactly right
   *  while the two agree and a distortion as soon as they do not — the image
   *  is stretched to the pane rather than letterboxed inside it. They stopped
   *  agreeing constantly: the only resize was sent two frames after the pane
   *  opened, at which point the dock has often not laid out and the pane
   *  still measures zero, so the send was skipped and never retried. The
   *  server stayed at its 1280x800 default while the pane was 743x824 —
   *  measured — which is a page squeezed to 58% across and stretched to 103%
   *  down. Nothing after that corrected it either: dragging the divider or
   *  resizing the window changed the pane and not the viewport. */
  private rbSizeObserver: ResizeObserver | null = null;
  private rbResizeTimer: ReturnType<typeof setTimeout> | null = null;
  private rbLastSize = '';
  private readonly cbView =
    viewChild<ElementRef<HTMLDivElement>>('cbView');
  private readonly cbImg =
    viewChild<ElementRef<HTMLImageElement>>('cbImg');

  // -- top-level section: Home (Chat), Code (the Agent Console), or Design.
  // Home is a separate, tool-free surface (HomeChat) and Design its own
  // canvas surface; neither shares state with the console. All three are
  // switched via the top-bar control.
  readonly section = signal<ModuleKey>('home');
  readonly turnNotify = inject(TurnNotifyService);
  /** The Missions live feed, so the nav's pulse can open it. */
  readonly missionFeed = inject(MissionActivityService);
  /** Clicking the toast takes you to whatever just finished, which is the only
   *  thing anybody wants to do with it. */
  jumpToFinishedTurn(section: ModuleKey): void {
    this.turnNotify.dismiss();
    ({
      home: () => this.enterHome(),
      code: () => this.enterCode(),
      design: () => this.enterDesign(),
      pipelines: () => this.enterPipelines(),
      estimate: () => this.enterEstimate(),
      missions: () => this.enterMissions(),
      'business-functions': () => this.enterBusinessFunctions(),
    })[section]?.();
  }

  enterHome(): void {
    this.section.set('home');
    // Background tasks & the browser are Code-only surfaces — close them so
    // Home stays a clean, tool-free chat.
    this.bgOpen.set(false);
    this.bgExpanded.set(false);
    this.browserOpen.set(false);
    this.browserExpanded.set(false);
    void this.loadHomeSessions();
  }
  enterCode(): void {
    this.section.set('code');
  }
  /** Design is created on first visit and then kept mounted (see app.html). */
  readonly designSeen = signal(false);
  enterDesign(): void {
    this.section.set('design');
    this.designSeen.set(true);
    this.bgOpen.set(false);
    this.bgExpanded.set(false);
    this.browserOpen.set(false);
    this.browserExpanded.set(false);
  }

  /** The Pipelines section, which only exists when the server mounted it.
   *  Guarded here as well as in the template: the nav entry is the only way
   *  in today, but a section that can be reached when its routes are absent
   *  is a bug waiting for the next entry point. */
  enterPipelines(): void {
    if (!this.health()?.pipelines) return;
    this.section.set('pipelines');
    this.bgOpen.set(false);
    this.bgExpanded.set(false);
    this.browserOpen.set(false);
    this.browserExpanded.set(false);
  }

  /** Sections that bring their own full-width surface and hide the sidebar.
   *
   * A computed because the test was written out five times in the template,
   * and adding a sixth section meant finding all five. It read as one fact
   * and behaved as five copies of it — the shape a bug takes when a section
   * gets the sidebar it was supposed to hide and nobody notices until the
   * layout is 260px narrower than it should be. */
  readonly chromeless = computed(
    () => this.section() === 'design'
      || this.section() === 'pipelines'
      || this.section() === 'estimate'
      || this.section() === 'missions'
      || this.section() === 'business-functions',
  );

  /** Sections that act on the Code console's open workspace. Separate from
   *  `chromeless` even though the two agree today: they are different facts,
   *  and a section could perfectly well have a sidebar and no workspace. The
   *  profile menu hides "Open in VS Code" where it would do nothing, because
   *  an item you have to click to discover is dead is worse than one that is
   *  simply not there. */
  readonly hasWorkspace = computed(() => !this.chromeless());

  /** The Estimate section, on the same terms as Pipelines: it only exists
   *  when the server mounted it, and the guard is here as well as in the
   *  template because the nav entry is the only way in *today*. */
  /** Missions, on the same terms as Estimate: it exists only if the server
   *  mounted it, and the guard is here as well as in the template because the
   *  nav entry is the only way in today. */
  /** The console's offer to hand this brief to the Missions harness, or
   *  null. Cleared when it is taken or waved away — and once waved away it
   *  does not come back for the same brief, because a suggestion that keeps
   *  reappearing is one people learn to ignore. */
  readonly missionOffer = signal<{ goal: string; reason: string } | null>(null);
  readonly startingMission = signal(false);
  readonly missionError = signal('');

  dismissMissionOffer(): void {
    this.missionOffer.set(null);
  }

  /** Take the offer: create the mission and go and look at it. It is created
   *  but not started — the Missions screen has the Start button, so the brief
   *  and the budget can be checked before anything runs. */
  async startAsMission(): Promise<void> {
    const offer = this.missionOffer();
    if (!offer || this.startingMission()) return;
    this.startingMission.set(true);
    try {
      await this.api.createMission(offer.goal, 25);
      this.missionOffer.set(null);
      this.enterMissions();
    } catch (err) {
      this.missionError.set(String(err));
    } finally {
      this.startingMission.set(false);
    }
  }

  enterMissions(): void {
    if (!this.health()?.missions) return;
    this.section.set('missions');
    this.bgOpen.set(false);
    this.bgExpanded.set(false);
    this.browserOpen.set(false);
    this.browserExpanded.set(false);
  }

  enterEstimate(): void {
    if (!this.health()?.estimate) return;
    this.section.set('estimate');
    this.bgOpen.set(false);
    this.bgExpanded.set(false);
    this.browserOpen.set(false);
    this.browserExpanded.set(false);
  }

  /** Business Functions. Mounted only when the server says it exists, and the
   *  component loads its own catalogue the first time it is shown — the shell
   *  knows the section is there and nothing else about it. */
  enterBusinessFunctions(): void {
    if (!this.health()?.business_functions) return;
    this.section.set('business-functions');
    this.businessFunctionsSeen.set(true);
    this.bgOpen.set(false);
    this.bgExpanded.set(false);
    this.browserOpen.set(false);
    this.browserExpanded.set(false);
  }

  /** Created on first entry and kept mounted after, the treatment Design gets:
   *  a function and a half-written question survive a trip to Home and back. */
  readonly businessFunctionsSeen = signal(false);

  /** Business Functions' own two panels, collapsed from the top bar the way
   *  Code's working copy is. The section is chromeless, so these buttons are
   *  the only chrome it gets — and the assistant is the one most worth
   *  folding away, since the stage is what the person came for. */
  readonly bfLayout = inject(BusinessFunctionsLayout);

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

  // -- Voice mode availability (Home): realtime deployment configured?
  readonly voiceAvailable = signal(false);
  async loadVoiceStatus(): Promise<void> {
    try {
      this.voiceAvailable.set((await this.api.voiceStatus()).available);
    } catch {
      this.voiceAvailable.set(false);
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

  /** The sidebar row currently waiting on the network, per module, or "".
   *
   *  The rail already says which conversation is *open*; this says which one
   *  is still arriving, which during the second or so between the click and
   *  the messages is the more useful of the two. Home's comes from the chat
   *  component through `loadingChanged`, because that is where the fetch
   *  lives; Code's is set here, because that is where its fetch lives. */
  readonly homeLoadingId = signal('');
  readonly codeLoadingId = signal('');
  // Show 4 initially; the down-arrow reveals 4 more at a time (like the agent).
  readonly homeLimit = signal(CONV_PAGE);
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
    const all = this.sortedHome();
    // Starred (pinned) chats float to a section at the top, like claude.ai.
    const pinned = all.filter((c) => c.pinned);
    const rest = all.filter((c) => !c.pinned);
    const groups: { label: string; cards: ChatCard[] }[] = [];
    if (pinned.length) groups.push({ label: 'Starred', cards: pinned });
    if (this.groupBy() === 'date') {
      const order = ['Today', 'Yesterday', 'Previous 7 days', 'Older'];
      const map = new Map<string, ChatCard[]>();
      for (const c of rest) {
        const k = this.dateBucket(c.updated_at);
        (map.get(k) ?? map.set(k, []).get(k)!).push(c);
      }
      for (const label of order) if (map.has(label)) groups.push({ label, cards: map.get(label)! });
    } else if (rest.length) {
      groups.push({ label: 'Chats', cards: rest });
    }
    return groups;
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
  readonly homeExpanded = computed(() => this.homeLimit() > CONV_PAGE);
  loadMoreHome(): void {
    this.homeLimit.update((n) => n + CONV_PAGE);
  }
  collapseHome(): void {
    this.homeLimit.set(CONV_PAGE);
  }

  /** The same, for Home's list. */
  readonly homeSessionsError = signal('');

  async loadHomeSessions(): Promise<void> {
    try {
      this.homeSessions.set((await this.api.listChatSessions()).sessions);
      this.homeSessionsError.set('');
    } catch (err) {
      if (!this.homeSessions().length) this.homeSessionsError.set(describeHttpError(err));
    }
  }
  openHomeConversation(id: string): void {
    this.section.set('home');
    this.homeActiveId.set(id);
  }
  /** A conversation the chat tried to open is not there — deleted elsewhere,
   *  or belonging to an account that is no longer the one signed in. Stop
   *  pointing at it and take the row out of the list, rather than leaving a
   *  highlighted row that cannot be opened. */
  onHomeConversationGone(id: string): void {
    if (this.homeActiveId() === id) this.homeActiveId.set(null);
    this.homeSessions.update((cards) => cards.filter((c) => c.id !== id));
  }

  /** The HomeChat created a fresh session on its first message. */
  onHomeSessionCreated(id: string): void {
    this.homeActiveId.set(id);
    void this.loadHomeSessions();
  }
  async deleteHomeConversation(card: ChatCard, ev?: Event): Promise<void> {
    ev?.stopPropagation();
    this.homeMenuOpenId.set(null);
    if (!(await this.confirm.ask({
      title: 'Delete this conversation?',
      subject: card.title || 'Untitled conversation',
      body: 'Its messages and anything attached to it go with it. '
        + 'This cannot be undone.',
    }))) return;

    // Gone from the list before the request leaves: the row is what the
    // person is looking at, and leaving it there while the round trip
    // happens is the lag they reported. Kept here so it can be put back.
    const before = this.homeSessions();
    const wasActive = this.homeActiveId() === card.id;
    this.homeSessions.update((cards) => cards.filter((c) => c.id !== card.id));
    if (wasActive) this.homeActiveId.set(null);

    try {
      await this.api.deleteChatSession(card.id);
      this.notice.ok(`Deleted “${card.title || 'Untitled conversation'}”.`);
      await this.loadHomeSessions();
    } catch (err) {
      // Put it back exactly as it was. A row that vanished and stayed gone
      // while the server still has it is worse than never removing it.
      this.homeSessions.set(before);
      if (wasActive) this.homeActiveId.set(card.id);
      this.notice.error(`Could not delete it — ${describeHttpError(err)}`, {
        label: 'Try again',
        run: () => void this.deleteHomeConversation(card),
      });
    }
  }

  // -- Home chat-row menu: Rename / Star / Delete (like claude.ai + Code) ----
  readonly homeMenuOpenId = signal<string | null>(null);
  readonly homeRenamingId = signal<string | null>(null);

  openHomeMenu(id: string, ev: Event): void {
    ev.stopPropagation();
    if (this.homeMenuOpenId() === id) {
      this.homeMenuOpenId.set(null);
      return;
    }
    const r = (ev.currentTarget as HTMLElement).getBoundingClientRect();
    this.menuX.set(r.right);
    this.menuY.set(r.bottom + 4);
    this.homeMenuOpenId.set(id);
  }
  closeHomeMenu(): void {
    this.homeMenuOpenId.set(null);
  }
  startHomeRename(card: ChatCard): void {
    this.homeMenuOpenId.set(null);
    this.homeRenamingId.set(card.id);
  }
  async commitHomeRename(card: ChatCard, value: string): Promise<void> {
    const title = value.trim();
    this.homeRenamingId.set(null);
    if (!title || title === card.title) return;
    try {
      await this.api.patchChatSession(card.id, { title });
    } catch {
      /* ignore */
    }
    await this.loadHomeSessions();
  }
  async toggleHomePin(card: ChatCard, ev?: Event): Promise<void> {
    ev?.stopPropagation();
    this.homeMenuOpenId.set(null);
    try {
      await this.api.patchChatSession(card.id, { pinned: !card.pinned });
    } catch {
      /* ignore */
    }
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
  readonly bgExpanded = signal(false); // widen the panel (like claude.ai's expand)
  readonly bgTasks = signal<BackgroundTask[]>([]);
  readonly bgFinishedOpen = signal(false);
  toggleBgExpand(): void {
    this.bgExpanded.update((v) => !v);
  }
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

  // -- the composer's three pickers ------------------------------------------
  //
  // Mode, model and effort were native <select>s, so each one dropped an OS
  // menu into the middle of the app: a different typeface, a different metric,
  // and on Windows a grey system list that looked nothing like the surface it
  // came from. They are the three settings a turn runs under and they are read
  // far more often than they are changed, so they are worth the custom menu.
  //
  // One signal rather than three booleans: opening any picker must close the
  // other two, and three flags make that a rule you can forget to apply.
  readonly pickMenu = signal<'mode' | 'model' | 'effort' | 'voice' | null>(null);

  togglePick(kind: 'mode' | 'model' | 'effort' | 'voice'): void {
    const next = this.pickMenu() === kind ? null : kind;
    this.closeAllMenus();
    this.pickMenu.set(next);
  }

  /** The deployment's name as people say it, shown beside each effort
   *  level so the menu says which model the choice applies to — the
   *  ladders differ per family, so the level alone is ambiguous. */
  readonly modelLabel = modelLabel;
  readonly modes = MODES;

  /** What the selected deployment will think at. Read from the server, which
   *  knows which ladder each family takes; EFFORTS is only the fallback for
   *  the moment before boot answers, and for a deployment the server has not
   *  described. */
  readonly efforts = computed<readonly string[]>(() => {
    const byModel = this.health()?.efforts;
    return byModel?.[this.activeModel()] ?? EFFORTS;
  });
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
  /** What the turn is actually doing, from the stream. See TurnStatus: this
   *  was a list of phrases on a timer, which said "Tracing the query plan…"
   *  while a shell command ran. */
  private readonly turnStatus = new TurnStatus();
  readonly thinkingMsg = this.turnStatus.label;
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

  // -- the pull request review dialog ----------------------------------------
  //
  // "Create PR" used to push and open the PR on the first click, with the
  // title and body written from the commit messages and no chance to look at
  // either. Opening a pull request is the one thing in this screen that other
  // people see, so it gets a look first: what it will be called, what it says,
  // and which files are in it.
  readonly prModalOpen = signal(false);
  readonly prTitle = signal('');
  readonly prBody = signal('');

  /** Draft a title and a description from what is actually there — the branch
   *  name and the changed files. Nothing is asserted about the change beyond
   *  the file list, because nothing else is known at this point. */
  async openPrModal(): Promise<void> {
    this.prMenuOpen.set(false);
    if (this.prBusy()) return;
    this.prError.set('');
    this.prModalOpen.set(true);
    if (!this.railFiles().length) await this.loadRail();

    const branch = this.gitStatus()?.branch || '';
    const words = branch
      .replace(/^(feature|feat|fix|chore|bugfix)\//, '')
      .replace(/[-_]+/g, ' ')
      .trim();
    this.prTitle.set(words ? words.charAt(0).toUpperCase() + words.slice(1) : '');

    const files = this.railFiles();
    const lines = files
      .slice(0, 20)
      .map((f) => `- \`${f.path}\` (+${this.railAdded(f)} −${this.railRemoved(f)})`);
    if (files.length > 20) lines.push(`- …and ${files.length - 20} more`);
    this.prBody.set(
      files.length
        ? `Changes in this branch:\n\n${lines.join('\n')}`
        : '',
    );
  }

  /** Total +/− across the working copy, for the dialog's file heading. */
  readonly prAdded = computed(() =>
    this.railFiles().reduce((n, f) => n + this.railAdded(f), 0),
  );
  readonly prRemoved = computed(() =>
    this.railFiles().reduce((n, f) => n + this.railRemoved(f), 0),
  );

  /** Why this branch cannot open a pull request, or '' if it can. Checked
   *  before the request so the dialog can say so while there is still
   *  something the person can do about it, rather than after they have
   *  written a title and pressed the button. */
  readonly prBlocked = computed(() => {
    const b = this.gitStatus()?.branch ?? '';
    if (b === 'main' || b === 'master') {
      return `You're on ${b}. A pull request needs a branch to merge from — ` +
        `create a feature branch, commit to it, then open the PR.`;
    }
    return '';
  });

  /** The last failure from the create attempt. Held until the next attempt
   *  rather than timed out: the dialog stays open on failure, and an error
   *  that erases itself while you are still reading it is worse than none. */
  readonly prError = signal('');

  /** Confirmed from the dialog. The dialog closes on success only — closing it
   *  first meant a rejected push reported itself as a line of grey text in the
   *  status bar that cleared after six seconds, which is indistinguishable
   *  from the button having done nothing at all. */
  async submitPr(draft = false): Promise<void> {
    this.prError.set('');
    const ok = await this.createPr({
      draft, title: this.prTitle(), body: this.prBody(),
    });
    if (ok) this.prModalOpen.set(false);
  }
  readonly prNotice = signal('');
  /** The PR the last successful create opened, so the notice can link
   *  to it rather than only naming it. */
  readonly prUrl = signal('');
  readonly diffOpen = signal(false);
  readonly diffLines = signal<{ t: string; text: string }[]>([]);
  /** Empty means the whole working tree; otherwise the file being read. */
  readonly diffTitle = signal('');
  readonly diffBusy = signal(false);
  readonly prMenuOpen = signal(false);
  readonly repoMenuOpen = signal(false);
  readonly branchMenuOpen = signal(false);

  // -- sidebar / history
  readonly sidebarOpen = signal(true);
  readonly cards = signal<SessionCard[]>([]);
  // Default to date-grouping so both lists open in claude.ai's Today/Yesterday/
  // Previous 7 days sections; the Group control can still switch it.
  readonly groupBy = signal<GroupBy>('date');
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

  // -- the attachment strip -------------------------------------------------
  // Scrolls sideways instead of wrapping, so the composer keeps its height
  // however many files are attached. The arrows exist because a mouse has no
  // obvious way to scroll horizontally, and appear only when something sits
  // past the edge.
  private readonly attStrip = viewChild<ElementRef<HTMLElement>>('attStrip');
  readonly attsOverflow = signal(false);
  readonly attsAtStart = signal(true);
  readonly attsAtEnd = signal(true);

  /** Live geometry, read from the element rather than derived from the count:
   *  how many chips fit depends on their names and the window's width. */
  measureAtts(): void {
    const el = this.attStrip()?.nativeElement;
    if (!el) {
      this.attsOverflow.set(false);
      return;
    }
    const slack = el.scrollWidth - el.clientWidth;
    this.attsOverflow.set(slack > 4);
    this.attsAtStart.set(el.scrollLeft <= 2);
    this.attsAtEnd.set(el.scrollLeft >= slack - 2);
  }

  scrollAtts(direction: -1 | 1): void {
    const el = this.attStrip()?.nativeElement;
    if (!el) return;
    el.scrollBy({ left: direction * Math.max(180, el.clientWidth * 0.8), behavior: 'smooth' });
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
      (this.draft().trim().length > 0 ||
        this.attachments().length > 0 ||
        this.browserSelectSvc.picks().length > 0) &&
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
      // Asking has its own card, and that card *is* the act. A row beside it
      // saying a tool was used says nothing a reader wants and reveals
      // machinery they have no use for — the question and the answer are the
      // whole story.
      if (it.kind === 'tool' && (it as ToolCardVM).name === 'ask_user') continue;
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
  /** A one-line, readable account of a tool call — the thing on the row.
   *
   *  The card used to print the raw arguments JSON into the transcript, and
   *  while a call was still streaming it printed the half-written JSON. A
   *  `file_write` of a page of HTML therefore filled the chat with escaped
   *  markup before anything had even run. Nobody reads that; what they want
   *  to know is which file, which command, which URL.
   *
   *  Reads a partial draft as happily as a finished call, because the point
   *  is to say something useful while the call is still arriving. */
  asQuestion = (i: TimelineItem) => i as QuestionVM;

  /** Tick or untick an option. Single-choice replaces; multi toggles. */
  pickOption(q: QuestionVM, label: string): void {
    if (q.answered) return;
    this.patch(q.id, (it) => {
      const cur = (it as QuestionVM).picked;
      const picked = q.multiSelect
        ? cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label]
        : [label];
      return { ...(it as QuestionVM), picked };
    });
  }

  setOther(q: QuestionVM, text: string): void {
    if (q.answered) return;
    this.patch(q.id, (it) => ({ ...(it as QuestionVM), other: text }));
  }

  /** Whether there is anything to send. Writing an answer counts. */
  canSubmitQuestion(q: QuestionVM): boolean {
    return !q.answered && (q.picked.length > 0 || q.other.trim().length > 0);
  }

  /** Send the answer, or decline it.
   *
   *  Skipping is a real choice and is sent as one: the model is told nobody
   *  answered and to proceed on its own judgement, which is better than a
   *  turn that waits out the timeout in silence. */
  async answerQuestion(q: QuestionVM, skip = false): Promise<void> {
    if (q.answered) return;
    const sid = this.sessionId();
    const chosen = skip ? [] : q.picked;
    const other = skip ? '' : q.other.trim();
    // Settle the card first: the answer travels on a different request from
    // the turn that is waiting for it, and a card that stays live while that
    // happens invites a second click.
    this.patch(q.id, (it) => ({
      ...(it as QuestionVM),
      answered: { chosen, other, skipped: skip },
    }));
    if (!sid) return;
    try {
      await this.api.answerQuestion(sid, q.id, { chosen, other, skipped: skip });
    } catch {
      /* the turn times out on its own and is told nobody answered */
    }
  }

  toolSummary(t: ToolCardVM): string {
    const raw = t.args || t.argsDraft || '';
    // Deliberately a regex and not JSON.parse: mid-stream the string is not
    // valid JSON yet, and the first field is usually the one worth showing.
    const field = (key: string): string => {
      const m = new RegExp('"' + key + '"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"').exec(raw);
      return m ? m[1].replace(/\\(.)/g, '$1') : '';
    };
    const tail = (p: string) => (p.split('/').pop() || p);
    switch (t.name) {
      case 'bash':
      case 'bash_output': {
        const c = field('command') || field('description');
        return c ? c.replace(/\s+/g, ' ').slice(0, 120) : '';
      }
      case 'file_read':
      case 'file_write':
      case 'file_edit':
        return tail(field('path') || field('file_path'));
      case 'glob':
      case 'grep':
        return field('pattern') || field('query');
      case 'web_fetch':
      case 'browser':
      case 'screenshot':
        return field('url') || field('action');
      case 'consult':
        return field('question').slice(0, 120);
      case 'find_tools':
        return field('query');
      default: {
        const first = /"[^"]+"\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(raw);
        return first ? first[1].replace(/\\(.)/g, '$1').slice(0, 120) : '';
      }
    }
  }

  /** Arguments as something a person would read, not a wire format. */
  toolArgsPretty(t: ToolCardVM): string {
    const raw = t.args || t.argsDraft || '';
    if (!raw) return '';
    try {
      return JSON.stringify(JSON.parse(raw), null, 2);
    } catch {
      return raw; // still streaming, or not JSON at all
    }
  }

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

  /** claude.ai-style activity summary: comma-joined natural phrases, the first
   *  capitalised, filenames included for reads — e.g. "Ran a command, read
   *  app.ts", "Ran 2 commands", "Ran a command, used 2 tools". */
  private summarizeActivity(tools: ToolCardVM[]): string {
    if (!tools.length) return 'Working…';
    let commands = 0, searches = 0, plans = 0, edits = 0, wrote = 0, delegated = 0;
    let fetched = 0, browsed = 0, shots = 0, consulted = 0, remembered = 0, others = 0;
    const reads: string[] = [];
    for (const t of tools) {
      const a = this.argObj(t);
      switch (t.name) {
        case 'bash': commands++; break;
        case 'grep': case 'glob': searches++; break;
        case 'todo_write': plans++; break;
        case 'file_edit': edits++; break;
        case 'file_write': wrote++; break;
        case 'file_read': reads.push(this.baseName(String(a['path'] ?? a['file_path'] ?? 'a file'))); break;
        case 'agent': delegated++; break;
        // These used to land on "used a tool", which tells a reader nothing
        // except that machinery exists. Each of them is an act with a plain
        // name, so say the name.
        case 'web_fetch': fetched++; break;
        case 'browser': browsed++; break;
        case 'screenshot': shots++; break;
        case 'consult': consulted++; break;
        case 'memory': remembered++; break;
        default: others++; break;
      }
    }
    const parts: string[] = [];
    if (commands) parts.push(commands === 1 ? 'ran a command' : `ran ${commands} commands`);
    if (reads.length === 1) parts.push(`read ${reads[0]}`);
    else if (reads.length > 1) parts.push(`read ${reads.length} files`);
    if (searches) parts.push('searched the code');
    if (plans) parts.push('updated the plan');
    if (edits) parts.push(edits === 1 ? 'edited a file' : `edited ${edits} files`);
    if (wrote) parts.push(wrote === 1 ? 'wrote a file' : `wrote ${wrote} files`);
    if (delegated) parts.push(delegated === 1 ? 'delegated to a subagent' : `delegated to ${delegated} subagents`);
    if (fetched) parts.push(fetched === 1 ? 'read a page' : `read ${fetched} pages`);
    if (browsed) parts.push('used the browser');
    if (shots) parts.push(shots === 1 ? 'took a screenshot' : `took ${shots} screenshots`);
    if (consulted) parts.push('asked for a second opinion');
    if (remembered) parts.push('checked its memory');
    // Whatever is left is genuinely unknown — an MCP tool from a server this
    // build has never heard of. "Used a tool" is honest there.
    if (others) parts.push(others === 1 ? 'used a tool' : `used ${others} tools`);
    const s = parts.join(', ') || `used ${tools.length} tools`;
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  private argObj(t: ToolCardVM): Record<string, unknown> {
    try {
      return JSON.parse(t.args || '{}') as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  private baseName(p: string): string {
    return (p || '').split(/[\\/]/).pop() || p;
  }

  /** Human label for one tool step — claude.ai shows the command's description
   *  or a derived action ("Read app.ts", "Interacted with the page"), not the
   *  raw tool name + args. */
  stepLabel(t: ToolCardVM): string {
    const a = this.argObj(t);
    const s = (v: unknown) => (typeof v === 'string' ? v : '');
    switch (t.name) {
      case 'bash':
        return s(a['description']) || s(a['command']).split('\n')[0].trim() || 'Ran a command';
      case 'file_read':
        return 'Read ' + this.baseName(s(a['path']) || s(a['file_path']) || 'a file');
      case 'file_write':
        return 'Wrote ' + this.baseName(s(a['path']) || s(a['file_path']) || 'a file');
      case 'file_edit':
        return 'Edited ' + this.baseName(s(a['path']) || s(a['file_path']) || 'a file');
      case 'grep':
        return s(a['pattern']) ? `Searched for “${s(a['pattern'])}”` : 'Searched the code';
      case 'glob':
        return s(a['pattern']) ? `Found files matching “${s(a['pattern'])}”` : 'Listed files';
      case 'todo_write':
        return 'Updated the plan';
      case 'agent':
        return 'Delegated to a subagent';
      case 'web_fetch':
        return s(a['url']) ? `Read ${s(a['url'])}` : 'Read a page';
      case 'screenshot':
        return 'Took a screenshot';
      case 'consult':
        return 'Asked for a second opinion';
      case 'memory':
        return 'Checked its memory';
      case 'browser':
        return this.browserStepLabel(a);
      default:
        return t.name;
    }
  }
  /** What a browser step did, from the action it was asked to perform.
   *
   *  This was unreachable. A second `case 'browser'` sat above it in the
   *  switch and answered every browser call before it was consulted, so each
   *  one was labelled "Opened <url>" whatever it actually did — a click, a
   *  keypress and a scroll all read as opening a page nobody navigated to.
   *  The compiler had been saying so on every build (duplicate-case) and the
   *  warning scrolled past among the routine ones. */
  private browserStepLabel(a: Record<string, unknown>): string {
    const host = (typeof a['url'] === 'string' ? a['url'] : '').replace(/^https?:\/\//, '');
    switch (a['action']) {
      case 'navigate': return host ? `Opened ${host}` : 'Opened a page';
      case 'click': return 'Clicked in the page';
      case 'type': return 'Typed text';
      case 'key': return 'Pressed a key';
      case 'scroll': return 'Scrolled the page';
      case 'screenshot': return 'Captured a screenshot';
      case 'read': return 'Read the page';
      // No action at all — an older transcript, or a call shaped before the
      // action field existed. The url is still the most useful thing known
      // about it, which is what the clause this replaced got right.
      default: return host ? `Opened ${host}` : 'Interacted with the page';
    }
  }
  /** Minimal shell highlighting for an expanded bash step: the command word in
   *  each segment, quoted strings, and flags — the way Claude colours the
   *  command it ran. Escaped first, so the output is safe to bind as HTML. */
  shellHtml(cmd: string): SafeHtml {
    const esc = (s: string) =>
      s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    let html = esc(cmd);
    // quoted strings
    html = html.replace(/(&#39;|')[^']*\1|"[^"]*"/g, (m) => `<i class="sh-str">${m}</i>`);
    // the command word at the start, or after a ; | && || pipeline separator
    html = html.replace(
      /(^|[;|]\s*|&amp;&amp;\s*)([a-zA-Z_][\w./-]*)/g,
      (_m, pre, word) => `${pre}<b class="sh-cmd">${word}</b>`,
    );
    // flags
    html = html.replace(/(\s)(--?[\w-]+)/g, (_m, sp, flag) => `${sp}<u class="sh-flag">${flag}</u>`);
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }

  /** The one argument that identifies a call, for the row: the command, the
   *  path, the pattern. Long values are cut in the middle rather than the end
   *  — the tail of a path is the part that says which file it was. */
  toolArgLine(t: ToolCardVM): string {
    const a = this.argObj(t);
    const s = (v: unknown) => (typeof v === 'string' ? v : '');
    const pick =
      s(a['command']).split('\n')[0] ||
      s(a['path']) ||
      s(a['file_path']) ||
      s(a['pattern']) ||
      s(a['url']) ||
      s(a['query']) ||
      s(a['description']);
    const v = (pick || t.args || '').trim();
    if (v.length <= 64) return v;
    return v.slice(0, 40) + '…' + v.slice(-20);
  }

  /** How long the call took, in the units somebody reads at a glance. Blank
   *  while it is still running — a duration that ticks upward reads as a
   *  finished number and would be wrong the moment it was believed. */
  toolDuration(t: ToolCardVM): string {
    const ms = t.durationMs;
    if (t.status === 'running' || ms == null || ms < 0) return '';
    if (ms < 1000) return Math.max(1, Math.round(ms)) + 'ms';
    if (ms < 60_000) return (ms / 1000).toFixed(1) + 's';
    const m = Math.floor(ms / 60_000);
    return m + 'm ' + Math.round((ms % 60_000) / 1000) + 's';
  }

  /** The raw command / args shown when a step is expanded. */
  stepDetail(t: ToolCardVM): string {
    const a = this.argObj(t);
    if (t.name === 'bash' && typeof a['command'] === 'string') return a['command'];
    return t.args;
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

  // -- conversation pagination: show N, "load more" reveals a page at a time.
  readonly convLimit = signal(CONV_PAGE);
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
  /** Whether anything is currently hidden behind the collapse. */
  readonly convsExpanded = computed(() => this.convLimit() > CONV_PAGE);
  loadMoreConvs(): void {
    this.convLimit.update((n) => n + CONV_PAGE);
  }
  /** Fold the list back to its first page. Expanding was a one-way door: once
   *  a long history was open the only way back was a reload. */
  collapseConvs(): void {
    this.convLimit.set(CONV_PAGE);
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

  /** Below this the sidebar is a drawer over the content rather than a
   *  column beside it — a 264px panel on a 390px screen is the screen. */
  private static readonly NARROW = 840;

  constructor() {
    void this.boot();
    // The sidebar starts closed on a narrow window, and closes itself when a
    // window becomes narrow. It is never reopened automatically: somebody who
    // opened it on a wide screen did not ask for it back, and a panel that
    // reappears on every resize is worse than one that stays where it was put.
    const narrow = () => window.innerWidth <= App.NARROW;
    if (narrow()) this.sidebarOpen.set(false);
    let wasNarrow = narrow();
    window.addEventListener('resize', () => {
      const now = narrow();
      if (now && !wasNarrow) this.sidebarOpen.set(false);
      wasNarrow = now;
    });
    // Keep the notifier in step with what is on screen. It is the only thing
    // that decides whether a finished turn is worth interrupting somebody for.
    effect(() => this.turnNotify.activeSection.set(this.section()));
    // One more file can be the one that makes the row scrollable.
    effect(() => {
      this.attachments();
      queueMicrotask(() => this.measureAtts());
    });
    effect(() => {
      this.timeline();
      this.thinking();
      queueMicrotask(() => {
        const el = this.logEl()?.nativeElement;
        // Instant follow so rAF-paced streaming text doesn't fight a smooth
        // scroll animation (which stutters when content grows every frame) —
        // but only while the user is still parked at the bottom.
        if (!el) return;
        // Decide from LIVE geometry, never a cached flag: this microtask runs
        // before the browser dispatches the `scroll` event, so a flag updated
        // by that handler is still stale here and would yank the user back.
        if (!scrolledUp(el)) {
          el.scrollTop = el.scrollHeight; // stay pinned to the newest content
          this.showJumpToBottom.set(false);
        } else {
          this.showJumpToBottom.set(true); // user is reading further up
        }
      });
    });
    // Tick the elapsed-time meter while a turn is in flight. The status line
    // rides the same timer, but only so its thinking phrase can move: every
    // other phase changes when the stream says so, not when a clock fires.
    effect((onCleanup) => {
      if (!this.streaming()) return;
      let sinceBeat = 0;
      const id = setInterval(() => {
        this.elapsedMs.set(Math.round(performance.now() - this.turnStartMs));
        if ((sinceBeat += 250) >= 2400) {
          sinceBeat = 0;
          this.turnStatus.tick();
        }
      }, 250);
      onCleanup(() => clearInterval(id));
    });
    // Interactive remote browser: connect the frame stream while the pane is
    // open, tear it down when it closes. Resize the server viewport once the
    // pane has laid out (and again whenever it expands/collapses).
    effect(() => {
      if (this.browserOpen()) {
        this.rbConnect();
        this.browserExpanded(); // re-run on expand/collapse to re-fit
        requestAnimationFrame(() =>
          requestAnimationFrame(() => this.rbSendResize()),
        );
      } else {
        this.rbDisconnect();
      }
    });
    // Inline artifact editing: the panel raises "change this highlighted part";
    // turn it into a targeted edit prompt and send it like any other message.
    effect(() => {
      const req = this.artifacts.editRequest();
      if (!req) return;
      this.artifacts.editRequest.set(null);
      const a = this.artifacts.active();
      if (!a) return;
      const lang = a.kind === 'mermaid' ? 'mermaid' : a.kind || '';
      // The full current source is included: the artifact may predate this
      // thread (or have been edited since), so the model must not rely on the
      // transcript to know what it is editing.
      this.draft.set(
        `Here is the current artifact${a.title ? ` "${a.title}"` : ''}:\n\n` +
          '```' + lang + '\n' + a.code + '\n```\n\n' +
          `Change only this highlighted part:\n\n"""\n${req.selection}\n"""\n\n` +
          `${req.instruction}\n\n` +
          'Leave everything else exactly as-is and re-emit the complete ' +
          'updated artifact in a single code block.',
      );
      void this.send();
    });
    // Time & focus: nudge for a break after the configured stretch of work.
    // Quiet hours mute the nudge entirely.
    setInterval(() => {
      const every = this.breakEvery();
      if (!every || this.breakDue() || this.inQuietHours()) return;
      if (Date.now() - this.sessionStartMs >= every * 60_000) this.breakDue.set(true);
    }, 30_000);
    // Poll background tasks so the top-bar badge and panel stay live; tick a
    // clock every second so "18m 26s" style timers advance without a refetch.
    void this.refreshBgTasks();
    setInterval(() => void this.refreshBgTasks(), 2500);
    setInterval(() => this.nowTick.set(Date.now()), 1000);
    // Routines and the Home conversation list are NOT loaded here. They are
    // somebody's, and at this point nobody has signed in — these calls were
    // two guaranteed 401s on a cold start. `enterWorkspace` asks for them
    // once there is a person to ask for.
    // Is Work IQ (Azure AI Search) configured? Drives the Home toggle.
    void this.loadWorkIqStatus();
    // Is realtime voice configured? Drives the Home voice-mode button.
    void this.loadVoiceStatus();
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
    // Home's list and the routines belong to whoever just signed in, and
    // this is the only moment that is known. They were loaded once in the
    // constructor instead — before anybody had signed in, so on a fresh
    // start that call is a 401 and the list stays empty until something
    // else happens to ask again, which is why switching tabs "fixed" it.
    // It went unnoticed while signing out left the previous list on screen.
    void this.loadHomeSessions();
    void this.loadRoutines();
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
      // Again here, not only on the way out. A session that expired never ran
      // `signOut`, so the screen still holds the previous person's lists when
      // the login panel appears over them — and whoever signs in next would
      // inherit them.
      this.forgetTheLastUser();
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
    void this.auth.logout();
    this.forgetTheLastUser();
  }

  /** Drop everything on screen that belonged to whoever was signed in.
   *
   *  Signing out does not reload the page, so without this the next person to
   *  sign in inherits the last one's view: their conversation titles still in
   *  the sidebar, and `homeActiveId` still pointing at a thread that is not
   *  theirs — which the server then refuses, and the screen reports as a
   *  failure to load. Only Code's three signals were being cleared here, so
   *  Home kept all of it.
   *
   *  The server is the boundary and always was; this is the screen catching
   *  up with it. Lists are emptied rather than re-fetched: there is nobody to
   *  fetch them for until somebody signs in, and `enterWorkspace` loads them
   *  again when they do.
   */
  private forgetTheLastUser(): void {
    // Code
    this.sessionId.set(null);
    this.timeline.set([]);
    this.usage.set(null);
    this.cards.set([]);
    this.sessionsError.set('');
    this.routines.set([]);
    // Home
    this.homeActiveId.set(null);
    this.homeSessions.set([]);
    this.homeSessionsError.set('');
    this.homeLoadingId.set('');
    this.homeLimit.set(CONV_PAGE);
    this.homeMenuOpenId.set(null);
    this.homeRenamingId.set(null);
    // Everything else the shell holds on somebody's behalf
    this.workspaces.set([]);
    this.missionOffer.set(null);
    this.missionError.set('');
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
  // Auto-follow streaming output ONLY while the user is parked near the bottom.
  // The moment they scroll up to read, we stop yanking them back down (claude.ai
  // behaviour); scrolling back to the bottom re-arms the follow.
  private stickBottom = true;

  /** Auto-follow state. Detaching is driven by scroll DIRECTION rather than a
   *  distance threshold or a single input event: while tokens stream the bottom
   *  runs away faster than a short scroll can escape, and listening only for
   *  `wheel` misses scrollbar drags, keyboard paging and momentum scrolling.
   *  Any upward movement the app did not cause detaches follow; returning to
   *  the bottom re-attaches it. */
  private lastScrollTop = 0;
  private programmaticScroll = false;
  /** Shown when follow is detached, to jump back to the newest message. */
  readonly showJumpToBottom = signal(false);

  /** Scroll the log to the bottom without it counting as user intent. */
  private scrollLogToBottom(el: HTMLElement, smooth = false): void {
    this.programmaticScroll = true;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    // Clear after the resulting scroll events have been dispatched.
    setTimeout(() => (this.programmaticScroll = false), smooth ? 400 : 60);
  }

  jumpToBottom(): void {
    const el = this.logEl()?.nativeElement;
    if (!el) return;
    // Direct assignment: a smooth animation over a long transcript can still be
    // running when the next token arrives, leaving the view short of the end.
    el.scrollTop = el.scrollHeight;
    this.showJumpToBottom.set(false);
  }

  onLogScroll(): void {
    const log = this.logEl()?.nativeElement;
    if (!log) return;
    this.showJumpToBottom.set(scrolledUp(log));
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
  /** "DEFAULT · GPT-5 · MEDIUM" — the settings the turn ran under, in the
   *  order the composer shows them. Uppercased in CSS, not here, so the value
   *  a screen reader announces is the one the controls use. */
  readonly turnPills = computed(() => {
    const model = this.activeModel() || 'model';
    return [this.activeMode(), model, this.activeEffort()].join(' · ');
  });

  // -- the permission gate ---------------------------------------------------
  //
  // The card answers the three questions somebody actually has before they
  // click: what is it about to do, how much could it cost me, and what does
  // each button commit me to. Everything below is derived from the request
  // itself — nothing is asserted that cannot be read off the command.

  /** What kind of act is being asked for. */
  permKind(p: PermissionVM): 'shell' | 'write' {
    return p.toolName === 'bash' ? 'shell' : 'write';
  }

  permTitle(p: PermissionVM): string {
    if (p.resolved) {
      const noun = this.permKind(p) === 'shell' ? 'Shell command' : 'Writes';
      return `${noun} ${p.resolved === 'deny' ? 'denied' : 'approved'}`;
    }
    if (this.permKind(p) === 'shell') return 'Compass wants to run a shell command';
    const n = this.permPaths(p).length;
    return n > 1 ? `Compass wants to write to ${n} files` : 'Compass wants to write a file';
  }

  /** Files a write request touches, for the title and the detail line. */
  permPaths(p: PermissionVM): string[] {
    try {
      const a = JSON.parse(p.args);
      if (Array.isArray(a?.paths)) return a.paths.filter((x: unknown) => typeof x === 'string');
      if (typeof a?.path === 'string') return [a.path];
    } catch {
      /* fall through */
    }
    return [];
  }

  /** Low / medium / high, from what the request says about itself. Erring
   *  upward: a gate that under-states risk is worse than one that nags. */
  permRisk(p: PermissionVM): string {
    const r = (p.reason || '').toLowerCase();
    const cmd = this.permDetail(p).toLowerCase();
    if (r.includes('destructive') || /\brm\s+-|\bgit\s+push|--force|\bdd\b|mkfs|shutdown/.test(cmd))
      return 'high';
    if (this.permKind(p) === 'write' || /\bsudo\b|>|\btee\b|\bmv\b|\bchmod\b/.test(cmd))
      return 'medium';
    return 'low';
  }

  /** The three facts the card states. Each is read off the command, and says
   *  "possible" rather than "none" wherever the answer cannot be known —
   *  claiming a command writes nothing when it might is the one mistake that
   *  would make this row worth less than no row at all. */
  permFacts(p: PermissionVM): { label: string; value: string }[] {
    if (this.permKind(p) === 'write') {
      return [
        { label: 'Scope', value: 'workspace only' },
        { label: 'Reversible', value: 'yes — discard' },
        { label: 'Commit', value: 'not yet' },
      ];
    }
    const cmd = this.permDetail(p);
    const net = /\b(curl|wget|npm|pnpm|yarn|pip|git|ssh|scp|nc|brew|apt|docker)\b/.test(cmd);
    const writes = />|>>|\btee\b|\brm\b|\bmv\b|\bcp\b|\bmkdir\b|\btouch\b|\bsed\s+-i/.test(cmd);
    return [
      { label: 'Network', value: net ? 'may be used' : 'not required' },
      { label: 'File writes', value: writes ? 'possible' : 'none' },
      { label: 'Working dir', value: './' },
    ];
  }

  /** The prefix "always allow" would remember — the first word of a command,
   *  or the tool for a write. Shown on the button so the promise is legible
   *  before it is made. */
  permAlwaysLabel(p: PermissionVM): string {
    if (this.permKind(p) === 'write') return p.toolName;
    const cmd = this.permDetail(p).trim();
    const parts = cmd.split(/\s+/).slice(0, 2);
    return parts.join(' ') || p.toolName;
  }

  /** What the resolved card says happened. */
  permVerdict(p: PermissionVM): string {
    if (p.resolved === 'deny') {
      return this.permKind(p) === 'shell'
        ? 'Denied by you — nothing was executed'
        : 'Nothing was changed on disk.';
    }
    if (p.decision === 'allow_always') {
      return `Always allowed for \`${this.permAlwaysLabel(p)}\` in this workspace`;
    }
    if (this.permKind(p) === 'write') {
      const n = this.permPaths(p).length;
      return `${n || 'The'} file${n === 1 ? '' : 's'} written to the working copy — not committed.`;
    }
    return `Allowed once by you${p.decidedAt ? ' · ' + p.decidedAt : ''}`;
  }

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
    } catch (err: unknown) {
      // Said, not swallowed — the same silent catch hid every Browse failure
      // on Windows. Safe to surface now that a successful reveal on Windows no
      // longer comes back as an error.
      const detail = (err as { error?: { detail?: string } })?.error?.detail;
      this.push({ kind: 'notice', id: crypto.randomUUID(), tone: 'error',
                  text: detail || `Could not open the folder in ${this.fileManagerName}.` });
    }
  }

  /** The host file manager's own name, for the menu label. */
  get fileManagerName(): string {
    const ua = typeof navigator !== 'undefined' ? navigator.userAgent : '';
    if (/Windows/i.test(ua)) return 'File Explorer';
    return /Mac/i.test(ua) ? 'Finder' : 'the file manager';
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
  // -- working copy rail ----------------------------------------------------
  //
  // The same changes the diff modal shows, kept open beside the conversation
  // instead of behind a button. Watching files appear as the agent edits them
  // is most of what the panel is for, and a modal you have to reopen after
  // every turn cannot do that.

  readonly railOpen = signal(false);
  /** Files in the working copy, parsed out of the diff the server already
   *  sends. No new endpoint: `git diff` names every file in its headers, so
   *  the list and the per-file patch come from one request. */
  readonly railFiles = signal<{ path: string; status: 'M' | 'A' | 'D'; body: string[] }[]>([]);
  readonly railBusy = signal(false);
  readonly railActive = signal('');

  readonly railGroups = computed(() => {
    const label: Record<string, string> = { M: 'Modified', A: 'Added', D: 'Deleted' };
    return (['M', 'A', 'D'] as const)
      .map((k) => ({ key: k, label: label[k], files: this.railFiles().filter((f) => f.status === k) }))
      .filter((g) => g.files.length > 0);
  });
  /** The patch for whichever file is open, as classified lines. */
  readonly railDiffLines = computed(() => {
    const f = this.railFiles().find((x) => x.path === this.railActive());
    if (!f) return [] as { t: string; text: string }[];
    return f.body.map((text) => {
      let t = 'ctx';
      if (text.startsWith('@@')) t = 'hunk';
      else if (text.startsWith('+++') || text.startsWith('---')) t = 'meta';
      else if (text.startsWith('+')) t = 'add';
      else if (text.startsWith('-')) t = 'del';
      return { t, text: text || ' ' };
    });
  });

  toggleRail(): void {
    this.railOpen.update((v) => !v);
    if (this.railOpen()) void this.loadRail();
  }

  /** Open one file's patch in the changes dialog — the same reader the diff
   *  button uses, at full width. A 320px rail can show that a file changed;
   *  it cannot show a diff anybody wants to read. */
  openRailFile(path: string): void {
    const f = this.railFiles().find((x) => x.path === path);
    if (!f) return;
    this.railActive.set(path);
    this.diffTitle.set(path);
    this.diffLines.set(
      f.body.map((text) => {
        let ty = 'ctx';
        if (text.startsWith('@@')) ty = 'hunk';
        else if (text.startsWith('+')) ty = 'add';
        else if (text.startsWith('-')) ty = 'del';
        return { t: ty, text: text || ' ' };
      }),
    );
    this.diffBusy.set(false);
    this.diffOpen.set(true);
  }

  // -- discarding the working copy -------------------------------------------
  readonly discardAsk = signal(false);
  readonly discardBusy = signal(false);

  async confirmDiscard(): Promise<void> {
    this.discardBusy.set(true);
    try {
      await this.api.discardChanges(this.activeWorkspaceId());
      this.discardAsk.set(false);
      this.railActive.set('');
      await this.loadRail();
      void this.loadGitStatus();
    } catch (err) {
      this.push({ kind: 'notice', id: crypto.randomUUID(), tone: 'error',
                  text: 'Could not discard: ' + String(err) });
      this.discardAsk.set(false);
    } finally {
      this.discardBusy.set(false);
    }
  }

  /** Split `git diff` into one entry per file. */
  async loadRail(): Promise<void> {
    this.railBusy.set(true);
    try {
      const { diff } = await this.api.gitDiff(this.activeWorkspaceId());
      const files: { path: string; status: 'M' | 'A' | 'D'; body: string[] }[] = [];
      let cur: { path: string; status: 'M' | 'A' | 'D'; body: string[] } | null = null;
      for (const line of diff.split('\n')) {
        if (line.startsWith('diff --git ')) {
          // "diff --git a/x b/x" — the b-side is the path after any rename.
          const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
          cur = { path: m ? m[2] : line.slice(11), status: 'M', body: [] };
          files.push(cur);
        } else if (cur) {
          if (line.startsWith('new file')) cur.status = 'A';
          else if (line.startsWith('deleted file')) cur.status = 'D';
          // `index`/`similarity` lines say nothing a reader wants; the ---/+++
          // pair is already stated by the filename above the patch.
          else if (!line.startsWith('index ') && !line.startsWith('--- ')
                   && !line.startsWith('+++ ') && !line.startsWith('similarity ')
                   && !line.startsWith('rename ') && !line.startsWith('old mode')
                   && !line.startsWith('new mode')) {
            cur.body.push(line);
          }
        }
      }
      this.railFiles.set(files);
      if (!files.some((f) => f.path === this.railActive())) this.railActive.set('');
    } catch {
      this.railFiles.set([]);
    } finally {
      this.railBusy.set(false);
    }
  }

  railAdded(f: { body: string[] }): number {
    return f.body.filter((l) => l.startsWith('+')).length;
  }
  railRemoved(f: { body: string[] }): number {
    return f.body.filter((l) => l.startsWith('-')).length;
  }

  async openDiff(): Promise<void> {
    this.diffTitle.set('');
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
  async createPr(
    opts: { draft?: boolean; manual?: boolean; title?: string; body?: string } = {},
  ): Promise<boolean> {
    this.prMenuOpen.set(false);
    if (this.prBusy()) return false;
    this.prBusy.set(true);
    this.prNotice.set('');
    this.prUrl.set('');
    try {
      const res = await this.api.createPr(this.activeWorkspaceId(), opts);
      const num = (res.url || '').match(/\/pull\/(\d+)/)?.[1] ?? '';
      const named = num ? `PR #${num}` : 'PR';
      this.prNotice.set(
        res.manual
          ? 'Opening GitHub…'
          : res.existing
            ? `${named} already open`
            : opts.draft
              ? `${named} opened as draft`
              : `${named} opened`,
      );
      this.prUrl.set(res.url || '');
      if (res.url) window.open(res.url, '_blank', 'noopener');
      setTimeout(() => this.prNotice.set(''), 8000);
      return true;
    } catch (err: unknown) {
      const detail =
        (err as { error?: { detail?: string } })?.error?.detail ??
        'Could not create PR';
      this.prError.set(detail);
      this.prNotice.set(detail);
      setTimeout(() => this.prNotice.set(''), 6000);
      return false;
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
    const on = !this.annotateOn();
    this.annotateOn.set(on);
    if (on) this.setSelect(false); // one active tool at a time
    // Size after the canvas actually paints. A microtask fires before the
    // zoneless render, so the viewChild is still undefined then — rAF x2 lands
    // after layout. Drawing also re-checks the size on pointerdown as a backstop.
    if (on)
      requestAnimationFrame(() => requestAnimationFrame(() => this.sizeAnnoCanvas()));
  }

  /** Select / inspect tool (arrow) — Chromium's own element overlay follows the
   *  cursor (like claude.ai). Clicking still interacts with the page. */
  setSelect(on: boolean): void {
    if (this.browserSelect() === on) return;
    this.browserSelect.set(on);
    if (on) this.annotateOn.set(false);
    else this.inspectInfo.set(null);
    this.rbSend({ t: 'select', on });
  }
  toggleSelect(): void {
    this.setSelect(!this.browserSelect());
  }

  /** Viewport tool (device) — render the page at a device size, like claude.ai's
   *  Responsive / Mobile 375×812 / Tablet 768×1024. */
  setViewport(v: ViewportName): void {
    this.browserViewport.set(v);
    this.cbViewMenuOpen.set(false);
    requestAnimationFrame(() => this.rbSendResize());
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

  /** Post the CURRENT frame with the annotation drawing baked in — this is what
   *  "Screenshot → chat" does in annotate mode, so the marks are included
   *  (exactly like claude.ai), rather than a fresh clean re-render. */
  captureAnnotatedShot(): void {
    const img = this.cbImg()?.nativeElement;
    if (!img || !img.naturalWidth) {
      void this.captureBrowserShot(false); // no frame yet — fall back
      return;
    }
    const off = document.createElement('canvas');
    off.width = img.naturalWidth;
    off.height = img.naturalHeight;
    const ctx = off.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(img, 0, 0, off.width, off.height);
    const canvas = this.annoCanvas()?.nativeElement;
    if (canvas && canvas.width && canvas.height)
      ctx.drawImage(canvas, 0, 0, off.width, off.height);
    this.postImage(off.toDataURL('image/png'), this.browserAddr());
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

  // -- in-app browser ------------------------------------------------------
  toggleBrowser(): void {
    this.browserOpen.update((v) => !v);
    if (this.browserOpen() && !this.browserAddr()) {
      this.browserAddr.set('https://learn.microsoft.com/azure/architecture/');
      this.navigateBrowser();
    }
    if (!this.browserOpen()) this.browserExpanded.set(false);
  }

  navigateBrowser(): void {
    let u = this.browserAddr().trim();
    if (!u) return;
    if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
    this.browserAddr.set(u);
    this.rbError.set('');
    // Drive the live server-side Chromium (renders any site, interactive) —
    // rbConnect's onopen navigates to browserAddr; if already open, go now.
    this.rbConnect();
    this.rbSend({ t: 'nav', url: u });
  }

  reloadBrowser(): void {
    this.rbError.set('');
    this.rbSend({ t: 'reload' });
  }

  // -- remote browser: frame stream + input forwarding ---------------------
  /** Send one command, and say whether it actually went out.
   *
   *  The return value matters to the resize memo: a size recorded as sent
   *  when the socket was not open is a size that will never be sent again,
   *  and the server sits on its default viewport for the rest of the
   *  session. */
  private rbSend(msg: Record<string, unknown>): boolean {
    const ws = this.rbWs;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(msg));
    return true;
  }

  rbConnect(): void {
    const existing = this.rbWs;
    if (
      existing &&
      (existing.readyState === WebSocket.OPEN ||
        existing.readyState === WebSocket.CONNECTING)
    )
      return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    // The display's pixel ratio, in the URL because the server needs it
    // before it builds the browser context — a first message would arrive
    // after the context exists, and it cannot be changed then. Without it
    // every frame is rendered at 1x and stretched over a Retina screen,
    // which is what made the pane look soft beside a real browser.
    const dpr = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
    const ws = new WebSocket(
      `${proto}//${location.host}/v1/browser/ws?dpr=${dpr}`);
    this.rbWs = ws;
    ws.onopen = () => {
      // A new connection is a new browser context on its default viewport,
      // so anything the old one was told no longer applies.
      this.rbLastSize = '';
      this.rbWatchPaneSize();
      this.rbSendResize();
      if (this.browserSelect()) this.rbSend({ t: 'select', on: true });
      const u = this.browserAddr();
      if (u) this.rbSend({ t: 'nav', url: u });
    };
    ws.onmessage = (ev) => {
      let m: {
        t: string; data?: string; url?: string; message?: string;
        box?: { x: number; y: number; w: number; h: number };
        label?: string; tag?: string; sub?: string; dim?: string;
        role?: string; name?: string; focusable?: boolean;
        opening?: string; selector?: string; text?: string;
        styles?: Record<string, string>;
      };
      try {
        m = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (m.t === 'frame' && m.data) {
        this.rbFrame.set('data:image/jpeg;base64,' + m.data);
        this.rbError.set('');
      } else if (m.t === 'nav' && m.url) {
        this.browserAddr.set(m.url);
      } else if (m.t === 'error') {
        this.rbError.set(m.message || 'Navigation failed.');
      } else if (m.t === 'inspect' && m.box) {
        // Map the element box (0..1 of the frame) to pixels within .cb-view.
        const img = this.cbImg()?.nativeElement;
        const view = this.cbView()?.nativeElement;
        if (img && view) {
          const ir = img.getBoundingClientRect();
          const vr = view.getBoundingClientRect();
          this.inspectInfo.set({
            left: ir.left - vr.left + m.box.x * ir.width,
            top: ir.top - vr.top + m.box.y * ir.height,
            w: m.box.w * ir.width,
            h: m.box.h * ir.height,
            tag: m.tag ?? m.label ?? '',
            sub: m.sub ?? '',
            dim: m.dim ?? '',
            role: m.role ?? '',
            name: m.name ?? '',
            focusable: !!m.focusable,
          });
        }
      } else if (m.t === 'pick' && m.opening) {
        this.addBrowserPick(m);
      }
    };
    ws.onclose = () => {
      if (this.rbWs === ws) this.rbWs = null;
    };
    ws.onerror = () => {};
  }

  rbDisconnect(): void {
    const ws = this.rbWs;
    this.rbWs = null;
    if (ws) {
      try {
        ws.close();
      } catch {
        /* already closing */
      }
    }
    this.rbFrame.set('');
    this.rbError.set('');
    this.rbSizeObserver?.disconnect();
    this.rbSizeObserver = null;
    if (this.rbResizeTimer) {
      clearTimeout(this.rbResizeTimer);
      this.rbResizeTimer = null;
    }
    this.rbLastSize = '';
  }

  /** Keep the server viewport equal to the pane, for as long as it is open.
   *
   *  An observer rather than a guess about when layout finishes: it fires
   *  when the pane actually has a size, which covers the case the old code
   *  missed entirely — opening — as well as every divider drag and window
   *  resize after it. Debounced so dragging a divider does not resize a real
   *  browser sixty times a second. */
  private rbWatchPaneSize(): void {
    const el = this.cbView()?.nativeElement;
    if (!el || this.rbSizeObserver) return;
    this.rbSizeObserver = new ResizeObserver(() => {
      if (this.rbResizeTimer) clearTimeout(this.rbResizeTimer);
      this.rbResizeTimer = setTimeout(() => {
        this.rbResizeTimer = null;
        this.rbSendResize();
      }, 120);
    });
    this.rbSizeObserver.observe(el);
  }

  private rbSendResize(): void {
    // Server viewport = a fixed device size (mobile/tablet) or, in responsive
    // mode, the live pane size so the frame is 1:1.
    const el0 = this.cbView()?.nativeElement;
    const shown = el0 ? el0.getBoundingClientRect().width : 0;
    const dev = App.VIEWPORTS[this.browserViewport()];
    if (dev) {
      // A 1920-wide page shown in a 700-wide pane is already being thrown
      // away on the way in; asking the server for a full-resolution frame on
      // top of that costs several megabytes to draw pixels nobody will see.
      // Sharpen only when the frame is displayed at its own size or larger.
      const sharp = shown > 0 && shown >= dev.w;
      const key = `dev:${dev.w}x${dev.h}:${sharp}`;
      if (key === this.rbLastSize) return;
      // Remembered only once it is genuinely on the wire — see rbSend.
      if (this.rbSend({ t: 'resize', w: dev.w, h: dev.h, sharp })) {
        this.rbLastSize = key;
      }
      return;
    }
    const el = this.cbView()?.nativeElement;
    if (!el) return;
    const r = el.getBoundingClientRect();
    // A pane that has not laid out yet measures zero. Nothing to send — but
    // the observer above is watching, so the size arrives the moment it is
    // real instead of being dropped the way it used to be.
    if (!r.width || !r.height) return;
    const w = Math.round(r.width);
    const h = Math.round(r.height);
    const key = `${w}x${h}:sharp`;
    if (key === this.rbLastSize) return;
    // Responsive mode is 1:1 by construction, so the detail is always worth
    // having.
    if (this.rbSend({ t: 'resize', w, h, sharp: true })) {
      this.rbLastSize = key;
    }
  }

  /** Normalise a pointer to 0..1 of the DISPLAYED frame image (not the pane),
   *  so clicks stay accurate when a device viewport is letterboxed/centred. */
  private rbNorm(e: PointerEvent | WheelEvent): { x: number; y: number } {
    const el = this.cbImg()?.nativeElement ?? this.cbView()!.nativeElement;
    const r = el.getBoundingClientRect();
    return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
  }

  rbDown(e: PointerEvent): void {
    if (this.annotateOn()) return;
    this.cbView()?.nativeElement.focus();
    // In Select mode a click PICKS the element (adds it to the prompt) rather
    // than interacting with the page — same as claude.ai's Select tool.
    if (this.browserSelect() && e.button === 0) {
      const p = this.rbNorm(e);
      this.rbSend({ t: 'pick', x: p.x, y: p.y });
      return;
    }
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    const p = this.rbNorm(e);
    this.rbSend({ t: 'down', x: p.x, y: p.y, button: e.button });
  }
  /** Turn a picked element into a Home-composer chip: a compact label plus a
   *  full context block (tag, selector, size, text, CSS) for the model. */
  private addBrowserPick(m: {
    opening?: string; selector?: string; dim?: string; text?: string;
    styles?: Record<string, string>;
  }): void {
    const styles = m.styles ?? {};
    const css = Object.entries(styles)
      .map(([k, v]) => `  ${k}: ${v};`)
      .join('\n');
    const detail =
      `Selected element (from ${this.browserAddr()}):\n` +
      `${m.opening ?? ''}\n` +
      `Selector: ${m.selector ?? ''}\n` +
      `Size: ${m.dim ?? ''}\n` +
      (m.text ? `Text: ${m.text}\n` : '') +
      (css ? `CSS:\n${css}` : '');
    this.browserSelectSvc.add({
      id: crypto.randomUUID(),
      label: m.opening ?? m.selector ?? 'element',
      detail,
    });
  }
  rbMove(e: PointerEvent): void {
    if (this.annotateOn()) return;
    const now = performance.now();
    if (now - this.rbMoveTs < 16) return; // ~60fps cap on the wire
    this.rbMoveTs = now;
    const p = this.rbNorm(e);
    this.rbSend({ t: 'move', x: p.x, y: p.y });
    // Select tool: ask Chromium to highlight the element under the cursor.
    if (this.browserSelect()) this.rbSend({ t: 'inspect', x: p.x, y: p.y });
  }
  rbUp(e: PointerEvent): void {
    if (this.annotateOn()) return;
    if (this.browserSelect() && e.button === 0) return; // handled on down (pick)
    const p = this.rbNorm(e);
    this.rbSend({ t: 'up', x: p.x, y: p.y, button: e.button });
  }
  rbWheel(e: WheelEvent): void {
    if (this.annotateOn()) return;
    e.preventDefault();
    this.rbSend({ t: 'wheel', dx: e.deltaX, dy: e.deltaY });
  }
  rbLeave(): void {
    this.inspectInfo.set(null);
  }
  private static readonly RB_KEYS = new Set([
    'Enter', 'Backspace', 'Delete', 'Tab', 'Escape', 'ArrowLeft', 'ArrowRight',
    'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown',
  ]);
  rbKey(e: KeyboardEvent): void {
    if (this.annotateOn()) return;
    const k = e.key;
    if (k.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      this.rbSend({ t: 'type', text: k });
      e.preventDefault();
    } else if (App.RB_KEYS.has(k)) {
      this.rbSend({ t: 'key', key: k });
      e.preventDefault();
    }
  }

  openBrowserExternal(): void {
    const u = this.browserAddr();
    if (u) window.open(u, '_blank', 'noopener');
  }

  /** Maximize the browser pane in-app (float over the chat), like claude.ai's
   *  expand — a shrink press restores the docked size. Opening it in a real OS
   *  window stays available via the "…" menu (Export / open externally). */
  expandBrowser(): void {
    this.browserExpanded.update((v) => !v);
  }

  // -- background tasks ----------------------------------------------------
  async refreshBgTasks(): Promise<void> {
    try {
      const res = await this.api.backgroundTasks();
      this.bgTasks.set(res.tasks);
    } catch {
      /* backend may be briefly unavailable */
    }
    // Keep an open task's log view live.
    if (this.bgLogsOpenId()) void this.loadBgLogs(this.bgLogsOpenId()!);
  }

  // -- background task log/output viewer (like Claude's task output) --------
  readonly bgLogsOpenId = signal<string | null>(null);
  readonly bgLogs = signal<string[]>([]);
  toggleBgLogs(t: BackgroundTask): void {
    if (this.bgLogsOpenId() === t.id) {
      this.bgLogsOpenId.set(null);
      this.bgLogs.set([]);
    } else {
      this.bgLogsOpenId.set(t.id);
      this.bgLogs.set([]);
      void this.loadBgLogs(t.id);
    }
  }
  private async loadBgLogs(id: string): Promise<void> {
    try {
      this.bgLogs.set((await this.api.backgroundTaskLogs(id)).lines);
    } catch {
      /* task may have been cleared */
    }
  }
  toggleBgPanel(): void {
    this.bgOpen.update((v) => !v);
    if (this.bgOpen()) void this.refreshBgTasks();
    else this.bgExpanded.set(false);
  }

  /** Open the panel on what has already finished.
   *
   *  Reached from the line in the chat that says how many tasks completed.
   *  Someone clicking that wants to read the commands and their output, so
   *  the Finished section is unfolded on the way in rather than left for a
   *  second click. */
  openFinishedTasks(): void {
    this.bgOpen.set(true);
    this.bgFinishedOpen.set(true);
    void this.refreshBgTasks();
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
  // -- Customize: tools, connectors and automations in one place ------------
  readonly customizeOpen = signal(false);
  readonly customize = signal<CustomizeInfo | null>(null);
  /** Customize, opened at the part that was asked for — the Design composer's
   *  menu points at Skills and at connectors, which both live in here. */
  async openCustomizeAt(part: 'tools' | 'connectors'): Promise<void> {
    await this.openCustomize();
    setTimeout(() => {
      const target = part === 'tools'
        ? document.querySelector('.cz-tools')
        : document.querySelector('.mem-body .cz-row');
      target?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 80);
  }

  // What Compass calls you. Written on blur or Enter rather than on every
  // keystroke, so a name is one request and not one per letter.
  readonly nameSaved = signal(false);

  async saveDisplayName(value: string): Promise<void> {
    const next = (value ?? '').trim();
    if (next === (this.auth.user()?.displayName ?? '').trim()) return;
    await this.auth.setDisplayName(next);
    this.nameSaved.set(true);
    setTimeout(() => this.nameSaved.set(false), 1600);
  }

  async openCustomize(): Promise<void> {
    this.userMenuOpen.set(false);
    this.customizeOpen.set(true);
    try {
      this.customize.set(await this.api.customize());
    } catch {
      /* non-fatal */
    }
  }

  // -- Reflect (Settings → Reflect): how you've been working, + Time & focus --
  readonly reflectOpen = signal(false);
  readonly recap = signal<Recap | null>(null);
  /** Break reminder cadence in minutes; 0 = off. Quiet hours mute reminders. */
  readonly breakEvery = signal(Number(getPref('compass-break-every') ?? 0));
  readonly quietFrom = signal(getPref('compass-quiet-from') ?? '');
  readonly quietTo = signal(getPref('compass-quiet-to') ?? '');
  readonly breakDue = signal(false);
  private sessionStartMs = Date.now();

  async openReflect(): Promise<void> {
    this.userMenuOpen.set(false);
    this.reflectOpen.set(true);
    try {
      this.recap.set(await this.api.recap());
    } catch {
      /* non-fatal */
    }
  }
  setBreakEvery(v: string): void {
    const n = Number(v) || 0;
    this.breakEvery.set(n);
    setPref('compass-break-every', String(n));
    this.sessionStartMs = Date.now();
    this.breakDue.set(false);
  }
  setQuiet(which: 'from' | 'to', v: string): void {
    if (which === 'from') {
      this.quietFrom.set(v);
      setPref('compass-quiet-from', v);
    } else {
      this.quietTo.set(v);
      setPref('compass-quiet-to', v);
    }
  }
  /** True inside the configured quiet hours (handles overnight ranges). */
  inQuietHours(): boolean {
    const f = this.quietFrom();
    const t = this.quietTo();
    if (!f || !t) return false;
    const now = new Date();
    const cur = now.getHours() * 60 + now.getMinutes();
    const [fh, fm] = f.split(':').map(Number);
    const [th, tm] = t.split(':').map(Number);
    const start = fh * 60 + (fm || 0);
    const end = th * 60 + (tm || 0);
    return start <= end ? cur >= start && cur < end : cur >= start || cur < end;
  }
  dismissBreak(): void {
    this.breakDue.set(false);
    this.sessionStartMs = Date.now();
  }

  // -- Memory (Settings → Memory): what Compass remembers, by category -------
  readonly memoryOpen = signal(false);
  readonly memoryEntries = signal<MemoryEntry[]>([]);
  readonly memoryCategories = signal<string[]>([]);
  readonly memoryEditId = signal<string | null>(null);
  readonly memoryDraft = signal('');
  /** Entries grouped by category, in the canonical category order. */
  readonly memoryGroups = computed<{ category: string; items: MemoryEntry[] }[]>(() => {
    const out: { category: string; items: MemoryEntry[] }[] = [];
    for (const c of this.memoryCategories()) {
      const items = this.memoryEntries().filter((e) => e.category === c);
      if (items.length) out.push({ category: c, items });
    }
    return out;
  });

  async openMemory(): Promise<void> {
    this.userMenuOpen.set(false);
    this.memoryOpen.set(true);
    await this.loadMemory();
  }
  async loadMemory(): Promise<void> {
    try {
      const r = await this.api.listMemory();
      this.memoryEntries.set(r.entries);
      this.memoryCategories.set(r.categories);
    } catch {
      /* non-fatal */
    }
  }
  startMemoryEdit(e: MemoryEntry): void {
    this.memoryEditId.set(e.id);
    this.memoryDraft.set(e.summary);
  }
  async commitMemoryEdit(e: MemoryEntry): Promise<void> {
    const summary = this.memoryDraft().trim();
    this.memoryEditId.set(null);
    if (!summary || summary === e.summary) return;
    try {
      await this.api.patchMemory(e.id, { summary });
    } catch {
      /* ignore */
    }
    await this.loadMemory();
  }
  async deleteMemory(e: MemoryEntry): Promise<void> {
    try {
      await this.api.deleteMemory(e.id);
    } catch {
      /* ignore */
    }
    await this.loadMemory();
  }

  // -- Files browser (⇧⌘F) --------------------------------------------------
  readonly filesOpen = signal(false);
  readonly filesQuery = signal('');
  readonly filesRoot = signal<FileEntry[]>([]);
  /** Expanded folder path -> its children. */
  readonly filesChildren = signal<Record<string, FileEntry[]>>({});
  readonly filesExpanded = signal<Set<string>>(new Set());
  readonly filesHits = signal<FileHit[]>([]);
  readonly fileOpenPath = signal('');
  readonly fileContent = signal('');
  readonly topMenuOpen = signal(false);

  async openFiles(): Promise<void> {
    this.topMenuOpen.set(false);
    this.filesOpen.set(true);
    if (!this.filesRoot().length) {
      try {
        const r = await this.api.listFiles(this.activeWorkspaceId(), '');
        this.filesRoot.set(r.entries);
      } catch {
        /* non-fatal */
      }
    }
  }
  isFolderOpen(path: string): boolean {
    return this.filesExpanded().has(path);
  }
  async toggleFolder(e: FileEntry): Promise<void> {
    const open = new Set(this.filesExpanded());
    if (open.has(e.path)) {
      open.delete(e.path);
      this.filesExpanded.set(open);
      return;
    }
    open.add(e.path);
    this.filesExpanded.set(open);
    if (!this.filesChildren()[e.path]) {
      try {
        const r = await this.api.listFiles(this.activeWorkspaceId(), e.path);
        this.filesChildren.update((m) => ({ ...m, [e.path]: r.entries }));
      } catch {
        /* non-fatal */
      }
    }
  }
  childrenOf(path: string): FileEntry[] {
    return this.filesChildren()[path] ?? [];
  }
  async openFile(e: FileEntry): Promise<void> {
    try {
      const r = await this.api.readFile(this.activeWorkspaceId(), e.path);
      this.fileOpenPath.set(e.path);
      this.fileContent.set(r.content);
    } catch {
      this.fileOpenPath.set(e.path);
      this.fileContent.set('(cannot preview this file)');
    }
  }
  /** "?text" searches file contents; anything else filters by name. */
  async runFileSearch(): Promise<void> {
    const q = this.filesQuery().trim();
    if (!q) {
      this.filesHits.set([]);
      return;
    }
    const content = q.startsWith('?');
    const term = content ? q.slice(1).trim() : q;
    if (!term) return;
    try {
      const r = await this.api.searchFiles(this.activeWorkspaceId(), term, content);
      this.filesHits.set(r.hits);
    } catch {
      this.filesHits.set([]);
    }
  }

  /** Which preview card's "⋮" menu is open. */
  readonly previewMenuId = signal<string | null>(null);
  openExternal(url: string): void {
    if (url) window.open(url, '_blank', 'noopener');
  }
  async copyText(text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* clipboard unavailable */
    }
  }

  /** Open a browser-preview card's page live in the Compass browser pane. */
  openPreview(pageUrl: string): void {
    if (!pageUrl) return;
    this.browserAddr.set(pageUrl);
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
    if (!(await this.confirm.ask({
      title: 'Delete this routine?',
      subject: r.name || 'Untitled routine',
      body: 'It stops running on its schedule and its run history goes. '
        + 'This cannot be undone.',
    }))) return;

    const before = this.routines();
    this.routines.update((list) => list.filter((x) => x.id !== r.id));
    this.routineView.set('list');

    try {
      await this.api.deleteRoutine(r.id);
      this.notice.ok(`Deleted “${r.name || 'Untitled routine'}”.`);
      await this.loadRoutines();
    } catch (err) {
      this.routines.set(before);
      this.notice.error(`Could not delete it — ${describeHttpError(err)}`);
    }
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

  /** Why the conversation list is empty, when it is empty because it failed.
   *
   *  An empty sidebar and a sidebar whose request did not come back look the
   *  same, and only one of them is worth pressing a button about. Cleared on
   *  success, so a list that arrives late simply replaces it. */
  readonly sessionsError = signal('');

  async refreshSessions(): Promise<void> {
    try {
      this.cards.set((await this.api.listSessions()).sessions);
      this.sessionsError.set('');
    } catch (err) {
      // Only when there is nothing to show. A background refresh that fails
      // while a list is already on screen leaves the list alone — replacing
      // conversations the user can still open with an error would be worse
      // than the stale list it is complaining about.
      if (!this.cards().length) this.sessionsError.set(describeHttpError(err));
    }
  }

  async newSession(): Promise<void> {
    this.view.set('chat');
    this.activeMode.set('default');
    this.activeEffort.set('medium');
    // A fetch in flight stops owning the window the moment a new conversation
    // is started; its own guard will drop the result when it arrives.
    this.openingSessionId = '';
    this.loadingSession.set('');
    this.codeLoadingId.set('');
    this.loadingSessionSlow.set(false);
    this.loadSessionError.set(null);
    this.earlierSeq.set(null);
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

  /** Which conversation the user last asked for. Compared when a load
   *  finishes, so a slower earlier request cannot overwrite a newer one. */
  private openingSessionId = '';

  async resumeSession(id: string): Promise<void> {
    if (!id) return this.newSession();
    this.openingSessionId = id;
    this.view.set('chat');
    this.activeDotColor.set(this.randomDotColor()); // light up this conv's dot
    const card = this.cards().find((c) => c.id === id);
    this.activeMode.set(card?.mode ?? 'default');
    this.activeEffort.set(card?.effort ?? 'medium');
    if (card?.model) this.activeModel.set(card.model);
    if (card?.workspace) this.activeWorkspaceId.set(card.workspace);
    // Cleared before the skeleton goes up, so the conversation being left is
    // never shown underneath a spinner that belongs to another one.
    this.timeline.set([]);
    this.earlierSeq.set(null);
    this.loadSessionError.set(null);
    this.loadingSession.set(id);
    this.codeLoadingId.set(id);
    this.loadingSessionSlow.set(false);
    const slow = setTimeout(() => {
      if (this.openingSessionId === id) this.loadingSessionSlow.set(true);
    }, App.SLOW_AFTER_MS);
    try {
      // Both at once. Resuming a session and reading its transcript are
      // independent — the transcript is read from storage, not from the
      // resumed session — and each is a round trip to another continent. One
      // after the other, opening a conversation waited for the sum of them.
      const [res, t] = await Promise.all([
        this.api.createSession({
          resume: true,
          sessionId: id,
          permissionMode: card?.mode,
          effort: card?.effort,
          model: card?.model || this.activeModel() || undefined,
          workspaceId: card?.workspace || this.activeWorkspaceId(),
        }),
        this.api.transcript(id, { limit: App.TRANSCRIPT_PAGE }),
      ]);
      // A click on another conversation while this one was loading wins: its
      // request started later and would otherwise be overwritten by this one
      // arriving second. Without this, clicking down a list quickly leaves the
      // window showing whichever reply happened to be slowest.
      if (this.openingSessionId !== id) return;
      this.sessionId.set(res.session_id);
      this.currentBubble = null;
      // Where the conversation continues above this page, if it does. A long
      // one is opened at its end — see TRANSCRIPT_PAGE — and this is what the
      // "Load earlier messages" control asks for.
      this.earlierSeq.set(t.before_seq ?? null);
      this.timeline.set(this.buildTimeline(t.messages));
    } catch (err) {
      // Said out loud rather than shown as an empty conversation. The two
      // looked identical before, and only one of them is worth retrying.
      if (this.openingSessionId !== id) return;
      this.timeline.set([]);
      this.loadSessionError.set({ id, detail: describeHttpError(err) });
    } finally {
      clearTimeout(slow);
      if (this.openingSessionId === id) {
        this.loadingSession.set('');
        this.codeLoadingId.set('');
        this.loadingSessionSlow.set(false);
      }
    }
  }

  /** How long a fetch may take before the note says why it is still going. */
  private static readonly SLOW_AFTER_MS = 900;

  /** Which conversation is being fetched, or "". Drives the bar, the
   *  skeleton and the locked composer; `codeLoadingId` drives the sidebar. */
  readonly loadingSession = signal('');
  readonly loadingSessionSlow = signal(false);
  readonly loadSessionError = signal<{ id: string; detail: string } | null>(null);

  /** Try the failed conversation again. */
  retrySession(): void {
    const failed = this.loadSessionError();
    if (!failed) return;
    this.loadSessionError.set(null);
    void this.resumeSession(failed.id);
  }

  /** How much of a conversation is fetched when it is opened.
   *
   *  A conversation is read from its end, like every other chat application,
   *  because the end is what somebody opening it wants to see. The one long
   *  conversation here is 2,005 messages and 2.5MB — twelve seconds before
   *  anything appeared — and almost all of it is tool calls from turns that
   *  finished weeks ago.
   *
   *  150 rather than a round 50: a turn that uses tools is a dozen messages,
   *  so this is still several turns of scrollback, and it leaves 76 of the 77
   *  conversations here arriving whole in one page, behaving exactly as they
   *  did before paging existed. */
  private static readonly TRANSCRIPT_PAGE = 150;

  /** The sequence number the page above the current one ends at, or null when
   *  the whole conversation is on screen. */
  readonly earlierSeq = signal<number | null>(null);
  readonly loadingEarlier = signal(false);

  /** Fetch the page before the one on screen and put it above. */
  async loadEarlier(): Promise<void> {
    const sid = this.sessionId();
    const cursor = this.earlierSeq();
    if (!sid || cursor === null || this.loadingEarlier()) return;
    this.loadingEarlier.set(true);
    try {
      const t = await this.api.transcript(sid, {
        limit: App.TRANSCRIPT_PAGE,
        beforeSeq: cursor,
      });
      // The conversation may have been left while this was in flight.
      if (this.sessionId() !== sid) return;
      const older = this.buildTimeline(t.messages);
      this.timeline.update((items) => [...older, ...items]);
      this.earlierSeq.set(t.before_seq ?? null);
    } catch {
      // Left as it was, with the control still offering to try again.
    } finally {
      this.loadingEarlier.set(false);
    }
  }

  /** Stored messages turned into what the timeline draws.
   *
   *  Pulled out of `resumeSession` so that a page fetched later is built the
   *  same way as the first one — two builders would drift, and the one used
   *  less often would be the one that drifted.
   */
  private buildTimeline(messages: TranscriptResponse['messages']): TimelineItem[] {
    const t = { messages };
    const items: TimelineItem[] = [];
    // What each call returned, so a restored card can show its result. The
    // results arrive as their own `tool` messages, keyed by call id.
    const results = new Map<string, { output: string; failed: boolean }>();
    for (const m of t.messages) {
      if (m.role === 'tool' && m.tool_call_id) {
        results.set(m.tool_call_id, {
          output: this.msgText(m.content),
          failed: !!m.is_error,
        });
      }
    }
    for (const m of t.messages) {
      const meta = m.meta ?? {};
      const at = m.timestamp ? m.timestamp * 1000 : undefined;
      // content may be a multimodal parts array (image attachments) — flatten
      // to text so bubbles always carry a string (a raw array crashes render).
      const text = this.msgText(m.content);
      if (m.role === 'user' && !meta['synthetic'] && !meta['compact_boundary']) {
        items.push(this.bubble('user', text, false, m.uuid, at));
      } else if (m.role === 'assistant') {
        // Reasoning is part of the record too: a reopened session shows what
        // the model thought, collapsed, as it was left. The sealed reasoning
        // stays on the server — this is the summary it wrote for people.
        //
        // The turn that calls a tool often has no text of its own, and that
        // is exactly the turn whose reasoning is worth seeing: it is why the
        // tool was chosen. So a message earns a bubble for its thinking as
        // well as for its words, and only a turn with neither is skipped.
        const usage = (meta['usage'] ?? {}) as Record<string, unknown>;
        const thinking = (meta['thinking_summary'] as string) || undefined;
        if (text || thinking) {
          items.push({
            ...this.bubble('assistant', text, false, undefined, at),
            thinking,
            thinkingTokens: (usage['reasoning_tokens'] as number) || undefined,
          });
        }
        // The work itself. A reopened session used to show what the model
        // thought and what it said, and nothing of what it did — no commands,
        // no files written, no results. The transcript carried all of it; the
        // restore simply never looked. Emitted in place so the grouping that
        // folds them into "Ran 2 commands" applies here exactly as it does
        // live.
        for (const call of m.tool_calls ?? []) {
          const name = call.function?.name || 'tool';
          const done = results.get(call.id);
          items.push({
            kind: 'tool',
            id: call.id,
            name,
            args: call.function?.arguments || '',
            output: done?.output ?? '',
            // A call with no stored result never finished — the turn was
            // stopped, or the permission timed out. Saying "ok" would be a
            // lie, and "running" would spin for ever on a dead session.
            status: done ? (done.failed ? 'error' : 'ok') : 'error',
            agentId: (meta['agent_id'] as string) || null,
            isMcp: name.startsWith('mcp__'),
          });
        }
      }
    }
    return items;
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
    this.previewMenuId.set(null);
    this.topMenuOpen.set(false);
    this.homeMenuOpenId.set(null);
    this.userMenuOpen.set(false);
    this.repoMenuOpen.set(false);
    this.branchMenuOpen.set(false);
    this.prMenuOpen.set(false);
    this.routineMenuOpen.set(false);
    this.cbMenuOpen.set(false);
    this.cbViewMenuOpen.set(false);
    this.plusMenuOpen.set(false);
    this.plusConnectorsOpen.set(false);
    this.pickMenu.set(null);
  }
  onGlobalClick(): void {
    this.closeAllMenus();
  }
  /** Open the host's native folder chooser (Finder / File Explorer) and fill the path. */
  /** True while the host's folder window is open. The request does not
   *  return until a folder is chosen or the window is cancelled, so without
   *  this the button looked dead for as long as the window stayed open —
   *  which, when the window opened behind the browser, was indefinitely. */
  readonly pickingFolder = signal(false);
  readonly pickFolderError = signal('');
  /** Only for the placeholder: a Windows user pasting a path should be shown
   *  a Windows path. The picker itself runs on the backend host. */
  readonly isWindowsClient =
    typeof navigator !== 'undefined' && /Windows/i.test(navigator.userAgent);

  async pickWorkspaceFolder(): Promise<void> {
    if (this.pickingFolder()) return;
    this.pickingFolder.set(true);
    this.pickFolderError.set('');
    try {
      const { path } = await this.api.pickFolder();
      if (path) this.newFolderPath.set(path);
    } catch (err: unknown) {
      // Said, not swallowed. Every Windows failure used to land in an empty
      // catch here, which is why the button appeared to do nothing at all.
      const detail = (err as { error?: { detail?: string } })?.error?.detail;
      this.pickFolderError.set(
        detail || 'Could not open the folder picker. Paste the folder path instead.',
      );
    } finally {
      this.pickingFolder.set(false);
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

  /** Branch the conversation at this message — everything up to and including
   *  it is copied into a new thread, so you can explore a different direction
   *  without losing the original (Claude's per-message branch action). */
  async forkFromMessage(b: ChatBubble): Promise<void> {
    const sid = this.sessionId();
    if (!sid || this.streaming()) return;
    const { session_id } = await this.api.forkSession(sid, b.msgUuid || undefined);
    await this.refreshSessions();
    await this.resumeSession(session_id);
  }

  async deleteConversation(card: SessionCard): Promise<void> {
    this.closeMenu();
    if (!(await this.confirm.ask({
      title: 'Delete this session?',
      subject: card.title || 'Untitled session',
      body: 'Its transcript goes, along with the screenshots the agent took '
        + 'and anything it drew. The workspace on disk is untouched. '
        + 'This cannot be undone.',
    }))) return;

    const before = this.cards();
    const wasOpen = this.sessionId() === card.id;
    this.cards.update((list) => list.filter((c) => c.id !== card.id));

    try {
      await this.api.deleteSession(card.id);
      this.notice.ok(`Deleted “${card.title || 'Untitled session'}”.`);
      await this.refreshSessions();
      if (wasOpen) await this.newSession();
    } catch (err) {
      this.cards.set(before);
      this.notice.error(`Could not delete it — ${describeHttpError(err)}`, {
        label: 'Try again',
        run: () => void this.deleteConversation(card),
      });
    }
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

  // -- The composer's + menu -------------------------------------------------
  //
  // Three entries, because Compass has three things to put there. Claude Code
  // also offers slash commands and plugins; Compass has neither — nothing
  // reads "/" in the composer and there is no plugin loader — and a menu that
  // lists what does not exist is worse than a shorter one that is true.

  readonly plusMenuOpen = signal(false);
  readonly plusConnectorsOpen = signal(false);
  readonly plusConnectors = signal<
    { name: string; detail: string; connected: boolean }[]
  >([]);

  /** ⌘ on a Mac, Ctrl everywhere else — the menu has to show the key that
   *  actually works on the machine reading it. */
  readonly shortcutKey =
    typeof navigator !== 'undefined' && /Mac|iP(hone|ad)/.test(navigator.platform)
      ? '⌘'
      : 'Ctrl+';

  togglePlusMenu(): void {
    const opening = !this.plusMenuOpen();
    this.closeAllMenus();
    this.plusMenuOpen.set(opening);
    if (opening) void this.loadPlusConnectors();
  }

  /** Fetched when the menu opens, not at startup: it is one request, and it
   *  is wasted on every session where nobody touches the +. Failure is quiet
   *  on purpose — the menu still opens, and the submenu says nothing is
   *  listed rather than showing an error where a list should be. */
  private async loadPlusConnectors(): Promise<void> {
    if (this.plusConnectors().length) return;
    try {
      const info = await this.api.customize();
      this.plusConnectors.set([...info.connectors, ...info.mcp_servers]);
    } catch {
      /* leave it empty; the submenu says so */
    }
  }

  plusAddFiles(): void {
    this.plusMenuOpen.set(false);
    this.openAttachPicker();
  }

  /** Straight to the host's folder chooser, the way "Add folder" behaves in
   *  the app this is modelled on — no modal in between.
   *
   *  The two failure modes are different and must not be conflated. A
   *  cancelled chooser comes back 200 with an empty path, and cancelling
   *  should do nothing at all; a host with no chooser at all comes back 422,
   *  and there the workspace panel is the real fallback, since it accepts a
   *  typed path. */
  async plusAddFolder(): Promise<void> {
    this.plusMenuOpen.set(false);
    let path: string;
    try {
      path = (await this.api.pickFolder()).path || '';
    } catch {
      this.workspacePanelOpen.set(true);
      return;
    }
    if (!path) return;
    this.newFolderPath.set(path);
    await this.addFolderPath();
  }

  plusOpenConnectors(): void {
    this.plusMenuOpen.set(false);
    this.plusConnectorsOpen.set(false);
    void this.openCustomizeAt('connectors');
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
    if (!(await this.confirm.ask({
      title: 'Remove this workspace?',
      subject: ws.name || ws.id,
      body: 'Compass forgets the folder. Nothing inside it is deleted — the '
        + 'files stay exactly where they are on disk.',
      confirmLabel: 'Remove',
    }))) return;

    const before = this.workspaces();
    const wasActive = this.activeWorkspaceId() === ws.id;
    this.workspaces.update((list) => list.filter((w) => w.id !== ws.id));
    if (wasActive) this.activeWorkspaceId.set('default');

    try {
      await this.api.deleteWorkspace(ws.id);
      this.notice.ok(`Removed “${ws.name || ws.id}”.`);
      await this.refreshWorkspaces();
    } catch (err) {
      this.workspaces.set(before);
      if (wasActive) this.activeWorkspaceId.set(ws.id);
      this.notice.error(`Could not remove it — ${describeHttpError(err)}`, {
        label: 'Try again',
        run: () => void this.removeWorkspace(ws, ev),
      });
    }
  }

  // -- sending / editing / regenerating -----------------------------------

  async send(): Promise<void> {
    if (!this.canSend()) return;
    this.stickBottom = true; // a fresh prompt re-arms auto-follow
    const content = this.draft().trim();
    const sid = this.sessionId();
    if (!sid) return;
    const atts = this.attachments();
    const picks = this.browserSelectSvc.picks();
    this.draft.set('');
    this.attachments.set([]);
    this.browserSelectSvc.clear();
    // Element references picked from the browser Select tool become context for
    // the model; the visible bubble shows the typed text (or the chip labels).
    const context = picks.map((p) => p.detail).join('\n\n');
    const apiContent = context ? `${context}\n\n${content}`.trim() : content;
    const shownText = content || picks.map((p) => p.label).join('  ');
    const b = this.bubble('user', shownText);
    if (atts.length) b.atts = atts;
    this.push(b);
    const payload = toWire(atts);
    await this.runStream(sid, (cb) =>
      this.api.streamMessage(sid, apiContent, cb, payload),
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
    this.turnNotify.arm();
    this.elapsedMs.set(0);
    this.turnStatus.start();
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
      // Tell the person only if they were not watching Code when it landed.
      this.turnNotify.finished('code', this.lastAssistantText || 'Turn finished.',
                               !this.turnAborted);
      this.autoOpenArtifact();
      await this.backfillUuids(sid);
      await this.refreshSessions();
      void this.loadGitStatus();
      if (this.railOpen()) void this.loadRail();
      void this.loadNextSuggestion(sid);
    }
  }

  // -- next-step suggestion pre-filled in the composer (claude.ai style) -----
  readonly nextSuggestion = signal('');
  /** Ask the model for the single most likely follow-up, shown as ghost text
   *  in the composer; Tab (or clicking it) accepts. */
  private async loadNextSuggestion(sid: string): Promise<void> {
    this.nextSuggestion.set('');
    if (this.turnAborted) return;
    try {
      const r = await this.api.suggestNext(sid);
      // Only offer it while the composer is still empty and idle.
      if (!this.draft().trim() && !this.streaming()) {
        this.nextSuggestion.set(r.suggestion || '');
      }
    } catch {
      /* a suggestion is optional */
    }
  }
  acceptSuggestion(): void {
    const s = this.nextSuggestion();
    if (!s) return;
    this.draft.set(s);
    this.nextSuggestion.set('');
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

  async resolve(
    perm: PermissionVM,
    behavior: 'allow' | 'deny' | 'allow_always',
  ): Promise<void> {
    const sid = this.sessionId();
    if (!sid) return;
    await this.api.resolvePermission(sid, perm.id, behavior);
    // 'allow_always' displays as allowed (and future calls stop prompting).
    const shown = behavior === 'deny' ? 'deny' : 'allow';
    const at = new Date().toLocaleTimeString('en-GB', { hour12: false });
    this.patch(perm.id, (p) => ({
      ...(p as PermissionVM), resolved: shown, decision: behavior, decidedAt: at,
    }));
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
    if ((ev.metaKey || ev.ctrlKey) && ev.shiftKey && (ev.key === 'f' || ev.key === 'F')) {
      ev.preventDefault();
      void this.openFiles();
      return;
    }
    if ((ev.metaKey || ev.ctrlKey) && (ev.key === 'k' || ev.key === 'K')) {
      ev.preventDefault();
      this.searchOpen() ? this.closeSearch() : this.openSearch();
      return;
    }
    // ⌘U attaches, because the + menu says it does. Only on Code: the shortcut
    // is advertised by that composer, and Home has its own picker.
    if (
      (ev.metaKey || ev.ctrlKey) &&
      (ev.key === 'u' || ev.key === 'U') &&
      this.section() === 'code'
    ) {
      ev.preventDefault();
      this.plusMenuOpen.set(false);
      this.openAttachPicker();
      return;
    }
    if (ev.key === 'Escape' && this.pickMenu()) {
      ev.preventDefault();
      this.pickMenu.set(null);
      return;
    }
    if (ev.key === 'Escape' && this.plusMenuOpen()) {
      ev.preventDefault();
      this.plusMenuOpen.set(false);
      this.plusConnectorsOpen.set(false);
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
    } else if (ev.key === '3' && !typing) {
      ev.preventDefault();
      void this.resolve(p, 'allow_always');
    }
  }

  onKeydown(ev: KeyboardEvent): void {
    // Tab accepts the pre-filled next-step suggestion (claude.ai behaviour).
    if (ev.key === 'Tab' && !this.draft().trim() && this.nextSuggestion()) {
      ev.preventDefault();
      this.acceptSuggestion();
      return;
    }
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
        ev.type === 'server_tool_used' ||
        ev.type === 'tool_arguments' ||
        ev.type === 'permission_request' ||
        ev.type === 'assistant_message')
    ) {
      this.thinking.set(false);
    }
    // Every event, before anything branches on it: the status line is a fold
    // over the whole stream, so a case that returns early must not skip it.
    this.turnStatus.note(ev.type, ev as { tool_name?: unknown });
    switch (ev.type) {
      case 'thinking_delta': {
        if (agentId) return;
        // Reasoning opens the bubble, so the thinking is visible while it
        // happens rather than only after the answer starts.
        if (!this.currentBubble) {
          this.currentBubble = this.bubble('assistant', '', true);
          this.push(this.currentBubble);
        }
        {
          const id = this.currentBubble.id;
          const chunk = (ev['text'] as string) ?? '';
          const gap = (ev['starts_part'] as boolean) ? '\n\n' : '';
          this.patch(id, (b) => {
            const bubble = b as ChatBubble;
            const so_far = bubble.thinking ?? '';
            return {
              ...bubble,
              thinking: so_far + (so_far ? gap : '') + chunk,
              thinkingLive: true,
            };
          });
        }
        break;
      }
      case 'thinking_complete': {
        if (agentId || !this.currentBubble) return;
        {
          const id = this.currentBubble.id;
          const tokens = (ev['tokens'] as number) ?? 0;
          this.patch(id, (b) => ({
            ...(b as ChatBubble),
            thinkingLive: false,
            thinkingTokens: tokens,
          }));
        }
        break;
      }
      case 'text_delta': {
        if (agentId) return;
        if (!this.currentBubble) {
          this.currentBubble = this.bubble('assistant', '', true);
          this.push(this.currentBubble);
        }
        if (!this.textSmoother) {
          // Reveal tokens smoothly (rAF-paced) rather than per-network-chunk.
          // The bubble may already exist because thinking opened it; either
          // way the answer streams into that same one.
          const id = this.currentBubble.id;
          this.textSmoother = new SmoothText((t) =>
            this.patch(id, (b) => ({ ...(b as ChatBubble), text: t })),
          );
        }
        this.textSmoother.push((ev['text'] as string) ?? '');
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
      case 'server_tool_used': {
        // Azure ran this one; there is no start, no progress and no result to
        // wait for, so the card is posted already finished. It is still a
        // card, because the alternative is an answer that silently rests on
        // three web pages and looks exactly like one that does not.
        this.push({
          kind: 'tool',
          id: crypto.randomUUID(),
          name: (ev['tool'] as string) ?? 'server_tool',
          args: '',
          output: (ev['detail'] as string) ?? '',
          status: 'ok',
          agentId,
          isMcp: false,
        });
        break;
      }
      case 'question_asked': {
        this.thinking.set(false);
        this.push({
          kind: 'question',
          id: (ev['request_id'] as string) ?? crypto.randomUUID(),
          question: (ev['question'] as string) ?? '',
          header: (ev['header'] as string) ?? '',
          options: (ev['options'] as Array<{ label: string; description: string }>) ?? [],
          multiSelect: !!ev['multi_select'],
          agentId,
          picked: [],
          other: '',
        });
        break;
      }
      case 'question_answered': {
        // Settles a card this client did not answer — a second window, or a
        // reload mid-question. Harmless when it was this one: same values.
        const id = (ev['request_id'] as string) ?? '';
        this.patch(id, (it) => ({
          ...(it as QuestionVM),
          answered: {
            chosen: (ev['chosen'] as string[]) ?? [],
            other: (ev['other'] as string) ?? '',
            skipped: !!ev['skipped'],
          },
        }));
        break;
      }
      case 'tool_arguments': {
        // The call has not arrived yet, so this both creates the card and
        // fills it. Whatever the model has written so far goes in `argsDraft`,
        // which `tool_call_started` then supersedes with the real arguments.
        const id = (ev['tool_call_id'] as string) ?? '';
        if (!id) break;
        const chunk = (ev['delta'] as string) ?? '';
        const existing = this.timeline().some(
          (it: TimelineItem) => it.kind === 'tool' && it.id === id,
        );
        if (existing) {
          this.patch(id, (c) => ({
            ...(c as ToolCardVM),
            argsDraft: ((c as ToolCardVM).argsDraft ?? '') + chunk,
          }));
        } else {
          const name = (ev['tool_name'] as string) ?? 'tool';
          this.push({
            kind: 'tool',
            id,
            name,
            args: '',
            argsDraft: chunk,
            output: '',
            status: 'running',
            agentId,
            isMcp: name.startsWith('mcp__'),
          });
        }
        break;
      }
      case 'tool_call_started': {
        const name = (ev['tool_name'] as string) ?? 'tool';
        const callId = (ev['tool_call_id'] as string) ?? crypto.randomUUID();
        const args = JSON.stringify(ev['arguments'] ?? {});
        // Streaming the arguments already created this card. Filling it in is
        // not the same as pushing another one: a second card for the same call
        // would leave the half-written one on screen forever.
        const already = this.timeline().some(
          (it: TimelineItem) => it.kind === 'tool' && it.id === callId,
        );
        if (already) {
          this.patch(callId, (c) => ({
            ...(c as ToolCardVM), name, args, argsDraft: undefined,
          }));
        } else {
          this.push({
            kind: 'tool',
            id: callId,
            name,
            args,
            output: '',
            status: 'running',
            agentId,
            isMcp: name.startsWith('mcp__'),
          });
        }
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
        // A screenshot/browser tool result posts the captured image into chat.
        const content = (ev['content'] as string) ?? '';
        const shot = /screenshot:\/\/([a-f0-9]+)/.exec(content);
        if (shot && !(ev['is_error'] as boolean)) {
          const imageUrl = '/v1/screenshot-cache/' + shot[1];
          if ((ev['tool_name'] as string) === 'browser') {
            // Render a browser-preview card (app screenshot + Open button),
            // like claude.ai — not a bare inline image.
            const pageUrl = (/Now at (https?:\/\/[^\s]+)/.exec(content)?.[1] ?? '')
              .replace(/[.\s]+$/, '');
            const title = (/Title:\s*(.+?)\.\s*Now at/.exec(content)?.[1] ?? '').trim();
            this.push({
              kind: 'preview',
              id: crypto.randomUUID(),
              imageUrl,
              pageUrl,
              title: title || 'Preview',
            });
          } else {
            this.postImage(imageUrl, 'Screenshot');
          }
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
      case 'mission_offer':
        // The console noticing this is a build rather than a task. An offer
        // with a button, never an action: starting one spends money for
        // hours with nobody watching, and that stays the person's decision.
        this.missionOffer.set({
          goal: String(ev['goal'] ?? ''),
          reason: String(ev['reason'] ?? ''),
        });
        break;
      case 'compaction': {
        // A compaction that degraded — no summary written, images dropped —
        // still lets the turn continue, so it has to say so here. Buried in
        // the transcript it reads as normal housekeeping while every later
        // request quietly goes out oversized.
        const detail = (ev['detail'] as string) ?? '';
        const failed = ev['ok'] === false;
        this.push({
          kind: 'notice',
          id: crypto.randomUUID(),
          tone: failed ? 'warn' : 'compaction',
          text: `context compacted (${ev['stage']}): ${ev['tokens_before']} → ${ev['tokens_after']} tokens`
            + (detail ? ` — ${detail}` : ''),
        });
        break;
      }
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
      case 'refused': {
        // Not an error: the request was fine and the model declined. It gets
        // its own notice so the reader is told the turn was stopped, rather
        // than being shown nothing and left to wonder.
        if (this.currentBubble) {
          const id = this.currentBubble.id;
          this.textSmoother?.finish();
          this.textSmoother = null;
          this.patch(id, (b) => ({ ...(b as ChatBubble), streaming: false }));
          this.currentBubble = null;
        }
        this.push({
          kind: 'notice',
          id: crypto.randomUUID(),
          tone: 'warn',
          text: (ev['message'] as string) ?? 'The response was stopped.',
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

  /** Show or hide a finished turn's reasoning. It collapses on its own once
   *  the answer starts, so what stays on screen is the answer. */
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
    setPref('compass-tts-voice', voice);
  }

  private loadVoicePref(): string {
    return getPref('compass-tts-voice') ?? '';
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
  /** Relative age ("just now", "7 hours ago", "3 days ago") — Claude labels
   *  messages by how long ago they were sent, not the wall-clock time. */
  formatTime(ms: number | undefined): string {
    if (!ms) return '';
    const secs = Math.max(0, Math.round((this.nowTick() - ms) / 1000));
    if (secs < 45) return 'just now';
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins} minute${mins === 1 ? '' : 's'} ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
    const days = Math.round(hours / 24);
    if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`;
    return new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  formatDuration(ms: number): string {
    return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)}s`;
  }

  asBubble = (i: TimelineItem): ChatBubble => i as ChatBubble;
  asTool = (i: TimelineItem): ToolCardVM => i as ToolCardVM;
  asPerm = (i: TimelineItem): PermissionVM => i as PermissionVM;
  asNotice = (i: TimelineItem): NoticeVM => i as NoticeVM;
  asPreview = (i: TimelineItem): PreviewCardVM => i as PreviewCardVM;

  trackItem = (_: number, i: TimelineItem): string => i.id;
  trackCard = (_: number, c: SessionCard): string => c.id;
}
