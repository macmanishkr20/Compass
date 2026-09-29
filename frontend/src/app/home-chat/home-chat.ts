import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { TurnNotifyService } from '../turn-notify.service';
import { CompassApiService } from '../compass-api.service';
import { AuthService } from '../auth.service';
import { BlurOnChange } from '../blur-on-change.directive';
import { CompassMark } from '../compass-mark/compass-mark';
import { Markdown } from '../markdown/markdown';
import { SavedPrompt } from '../models';
import { CompassEvent } from '../models';
import { ATTACH_ACCEPT, UiAttachment, formatSize, readFiles, toWire } from '../attachments';
import { SmoothText } from '../smooth-text';
import { LightboxService } from '../lightbox.service';

interface WorkIqSource {
  n: number;
  title: string;
  url: string;
}

/** What each tool is called in the transcript. The tool's own name is a
 *  function name; this is what a person would say it was doing. */
const TOOL_LABELS: Record<string, string> = {
  make_video: 'Making a video',
  memory: 'Remembering',
  web_fetch: 'Reading a page',
};

/** One tool running inside a turn, as the reader sees it. */
interface ToolActivity {
  id: string;
  label: string;
  /** The tool's latest progress line, replaced as it arrives rather than
   *  stacked: a render reports every shot, and thirty lines of that is a log,
   *  not a status. */
  detail: string;
  running: boolean;
  failed?: boolean;
}

interface ChatMsg {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  streaming: boolean;
  at?: number; // epoch ms — shown as a relative age under the message
  atts?: UiAttachment[];
  sources?: WorkIqSource[];
  /** What Azure did inside the turn that Compass did not do itself — code it
   *  ran, pages it opened. Shown because a sandbox in another country
   *  executing Python should not be the one thing on screen that leaves no
   *  trace. Pages that were opened also become `sources`; this is the rest. */
  serverActivity?: string[];
  /** What Compass itself is doing this turn — a video being rendered, a page
   *  being fetched. Home had no tools when it was written, so a turn that
   *  called one finished its bubble while it was still empty and then spent
   *  minutes rendering with nothing on screen at all. */
  toolActivity?: ToolActivity[];
  /** The model's reasoning, when it reasoned. Arrives before the answer and
   *  is kept apart from it: this is the working, not the reply. A turn the
   *  model answered outright has none, which is normal. */
  thinking?: string;
  /** Tokens the reasoning cost. Billed as output, and shares the cap with
   *  the answer — the number that explains a turn that ran out of room. */
  thinkingTokens?: number;
  /** Still being produced. Open while it streams, collapsed once done, so
   *  the answer is what remains on screen. */
  thinkingLive?: boolean;
  /** The reader opened it back up. */
}

const FOLLOW_SLACK = 120; // px from the bottom that still counts as following

/** Is there anything below the fold to jump down to? A pane that isn't
 *  scrollable — an empty thread, or one shorter than the viewport — has
 *  nothing to jump to, and during first layout clientHeight is briefly 0,
 *  which would otherwise read as "miles from the bottom" and flash the
 *  chevron on a thread the user never scrolled. */
function scrolledUp(el: HTMLElement): boolean {
  if (el.clientHeight === 0) return false;
  return el.scrollHeight - el.scrollTop - el.clientHeight > FOLLOW_SLACK;
}

/** How hard the model is asked to think. These are the four levels Azure's
 *  reasoning models accept — 'minimal' was never one of them, and 'max' is
 *  rejected. Higher means it thinks more often and goes further; at 'low' it
 *  skips thinking on work that does not need it. */
const EFFORTS = ['minimal', 'low', 'medium', 'high'] as const;

/**
 * Home / Chat — a pure-conversation surface. It is a self-contained sibling of
 * the agent console (App), sharing none of its tool/permission machinery. It
 * talks to the isolated `/v1/chat/*` backend, which runs gpt-5 with no tools,
 * so there are never tool cards or permission prompts here — just chat.
 */
@Component({
  selector: 'app-home-chat',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, BlurOnChange, CompassMark, Markdown],
  templateUrl: './home-chat.html',
  styleUrl: './home-chat.css',
  host: {
    // An outside click or Escape dismisses an open picker, the same bargain
    // the Code composer's menus strike.
    '(document:click)': 'pickMenu.set(null)',
    '(document:keydown.escape)': 'pickMenu.set(null)',
    '(window:resize)': 'measureAtts()',
  },
})
export class HomeChat {
  private readonly api = inject(CompassApiService);
  private readonly auth = inject(AuthService);
  private readonly turnNotify = inject(TurnNotifyService);
  readonly lightbox = inject(LightboxService);

  // Inputs from the shell so we don't duplicate health fetching.
  readonly models = input<string[]>([]);
  readonly deployment = input<string>('');
  // The Home conversation to display: an id to resume, or null for a fresh
  // thread. Driven by the sidebar (App owns the Home conversation list).
  readonly activeSession = input<string | null>(null);
  // Work IQ (owned by the shell topbar) — ground replies in Azure AI Search.
  readonly workIq = input<boolean>(false);
  // Voice mode: whether a realtime deployment is configured on the server.
  readonly voiceAvailable = input<boolean>(false);

  // Fired when the user flips the composer toggle to "Agent" — the shell
  // switches to the Code (Agent Console) section.
  readonly switchToAgent = output<void>();
  // A new chat session was created (first message of a fresh thread).
  readonly sessionCreated = output<string>();
  // The thread changed (new session or a completed turn) — refresh the list.
  readonly threadChanged = output<void>();

  readonly efforts = EFFORTS;
  readonly accept = ATTACH_ACCEPT;
  readonly activeModel = linkedSignal(() => this.deployment());
  readonly activeEffort = signal('medium');

  /** "CHAT · GPT-5 · MEDIUM" — the settings the turn ran under, beside the
   *  mark. Home is the chat surface, so the first pill is the surface's own
   *  name rather than a setting: the Agent button switches consoles, it does
   *  not change a mode within this one. */
  readonly turnPills = computed(() =>
    ['chat', this.activeModel() || 'model', this.activeEffort()].join(' · '),
  );

  /** Which of Home's two composer pickers is open. One signal rather than two
   *  flags: opening either must close the other. */
  readonly pickMenu = signal<'model' | 'effort' | null>(null);

  togglePick(kind: 'model' | 'effort'): void {
    this.pickMenu.update((cur) => (cur === kind ? null : kind));
  }

  readonly draft = signal('');
  readonly messages = signal<ChatMsg[]>([]);
  readonly streaming = signal(false);
  readonly attachments = signal<UiAttachment[]>([]);
  readonly dragOver = signal(false);
  readonly attachError = signal('');
  private readonly fileInput = viewChild<ElementRef<HTMLInputElement>>('fileInput');
  // -- Voice mode (Azure OpenAI Realtime, speech-to-speech over WebRTC) -----
  readonly voiceMode = signal(false);
  readonly voiceState = signal<'connecting' | 'listening' | 'speaking'>('connecting');
  readonly voiceHeard = signal(''); // live transcript of what the user said
  readonly voiceReply = signal(''); // live transcript of the assistant's speech
  readonly voiceError = signal('');
  readonly voiceSupported =
    typeof window !== 'undefined' && typeof RTCPeerConnection !== 'undefined';
  private pc: RTCPeerConnection | null = null;
  private dc: RTCDataChannel | null = null;
  private micStream: MediaStream | null = null;
  private voiceAudioEl: HTMLAudioElement | null = null;

  private sessionId: string | null = null;
  private loadedId: string | null = null; // which session the view is showing
  private currentAssistant: ChatMsg | null = null;
  private smoother: SmoothText | null = null; // smooth token reveal
  private pendingSources: WorkIqSource[] | null = null; // Work IQ sources for the reply

  /** The starter prompts, each with the glyph that belongs to it. They were a
   *  bare string list with one shared lightbulb; three identical icons in a
   *  column is decoration rather than a signal, so the icon now says which
   *  kind of thing the row is. */
  readonly builtInIdeas: { text: string; icon: 'bulb' | 'branch' | 'pen' | 'film' }[] = [
    { text: 'Explain a tricky concept in simple terms', icon: 'bulb' },
    { text: 'Brainstorm names for a new project', icon: 'branch' },
    { text: 'Draft a short message or email', icon: 'pen' },
    // Rendering is the one thing here that is not obviously a chat's job, so
    // it is the one that has to be said out loud.
    { text: 'Cut a teaser from photos I attach', icon: 'film' },
  ];

  // ── the prompt library ──────────────────────────────────────────────
  /** Everything saved, newest first. */
  readonly saved = signal<SavedPrompt[]>([]);
  /** How many fit on the Home screen before "Show all" takes over. */
  readonly onScreen = signal(4);
  readonly libraryOpen = signal(false);

  /** The save dialog: null when closed, otherwise what it is editing. */
  readonly saveDialog = signal<{
    id: string;          // '' for a new one
    title: string;
    text: string;
    original: string;    // what was typed, so a sharpen can be undone
    sharpened: boolean;
  } | null>(null);
  readonly sharpening = signal(false);
  readonly savingPrompt = signal(false);
  readonly promptError = signal('');

  /** What the Home screen offers: saved prompts first, then the built-ins to
   *  fill the row. A library with four entries is the person's own; an empty
   *  one should still suggest something rather than show a blank space. */
  readonly ideas = computed<{ text: string; icon: string; id: string; title: string }[]>(() => {
    const mine = this.saved().map((p) => ({
      text: p.text, icon: p.icon, id: p.id, title: p.title,
    }));
    const builtIn = this.builtInIdeas.map((s) => ({
      text: s.text, icon: s.icon, id: '', title: s.text,
    }));
    return [...mine, ...builtIn];
  });

  /** The first few, which is all Home shows. */
  readonly ideasOnScreen = computed(() => this.ideas().slice(0, this.onScreen()));

  /** Whether there is anything behind "Show all". */
  readonly hasMoreIdeas = computed(() => this.ideas().length > this.onScreen());

  async loadPrompts(): Promise<void> {
    try {
      const res = await this.api.savedPrompts();
      this.saved.set(res.prompts ?? []);
      if (res.on_screen) this.onScreen.set(res.on_screen);
    } catch {
      /* the built-in starters stand on their own */
    }
  }

  /** Open the save dialog for one message. */
  savePromptFrom(m: { id: string; text: string }): void {
    const text = (m.text || '').trim();
    if (!text) return;
    this.promptError.set('');
    this.saveDialog.set({ id: '', title: '', text, original: text, sharpened: false });
  }

  editSaved(p: SavedPrompt): void {
    this.promptError.set('');
    this.saveDialog.set({
      id: p.id, title: p.title, text: p.text, original: p.text, sharpened: false,
    });
  }

  closeSaveDialog(): void {
    this.saveDialog.set(null);
    this.sharpening.set(false);
    this.promptError.set('');
  }

  patchDialog(patch: Partial<{ title: string; text: string }>): void {
    const d = this.saveDialog();
    if (d) this.saveDialog.set({ ...d, ...patch });
  }

  /** Ask the model for a version that stands on its own.
   *
   *  The surrounding messages go along as context, because a prompt typed
   *  mid-conversation often refers to what was already on screen — but only
   *  so the rewrite can resolve what it points at, never to fold in other
   *  topics. The result lands in the text box as a suggestion, not a commit. */
  async sharpen(): Promise<void> {
    const d = this.saveDialog();
    if (!d || this.sharpening()) return;
    this.sharpening.set(true);
    this.promptError.set('');
    try {
      const res = await this.api.sharpenPrompt(d.original, this.nearbyContext(d.original));
      this.saveDialog.set({
        ...d,
        title: d.title || res.title || '',
        text: res.text || d.text,
        sharpened: true,
      });
    } catch (err) {
      this.promptError.set(String(err));
    } finally {
      this.sharpening.set(false);
    }
  }

  /** Put back what was typed, if the rewrite went somewhere they did not mean. */
  undoSharpen(): void {
    const d = this.saveDialog();
    if (d) this.saveDialog.set({ ...d, text: d.original, sharpened: false });
  }

  /** The few messages around this prompt — enough to resolve what it refers
   *  to, capped so a long conversation does not become the context. */
  private nearbyContext(text: string): string {
    const all = this.messages();
    const at = all.findIndex((m) => (m.text || '').trim() === text.trim());
    const from = Math.max(0, (at < 0 ? all.length : at) - 4);
    return all
      .slice(from, at < 0 ? all.length : at)
      .map((m) => `${m.role === 'user' ? 'Person' : 'Compass'}: ${(m.text || '').slice(0, 400)}`)
      .join('\n');
  }

  async commitSavePrompt(): Promise<void> {
    const d = this.saveDialog();
    if (!d || this.savingPrompt() || !d.text.trim()) return;
    this.savingPrompt.set(true);
    this.promptError.set('');
    try {
      if (d.id) {
        await this.api.editSavedPrompt(d.id, d.title, d.text);
      } else {
        await this.api.savePrompt(d.title, d.text, this.sessionId ?? '');
      }
      await this.loadPrompts();
      this.closeSaveDialog();
    } catch (err) {
      this.promptError.set(String(err));
    } finally {
      this.savingPrompt.set(false);
    }
  }

  /** The library dialog lists ideas, not raw records, so it acts by id. */
  editSavedById(id: string): void {
    const row = this.saved().find((p) => p.id === id);
    if (row) this.editSaved(row);
  }

  removeSavedById(id: string): void {
    const row = this.saved().find((p) => p.id === id);
    if (row) void this.removeSaved(row);
  }

  async removeSaved(p: SavedPrompt): Promise<void> {
    try {
      await this.api.deleteSavedPrompt(p.id);
      await this.loadPrompts();
    } catch (err) {
      this.promptError.set(String(err));
    }
  }

  readonly canSend = computed(
    () => (this.draft().trim().length > 0 || this.attachments().length > 0) && !this.streaming(),
  );

  /** Show the typing indicator while we wait for the model's first token. */
  readonly awaitingReply = computed(() => {
    const list = this.messages();
    return this.streaming() && (list.length === 0 || list[list.length - 1].role === 'user');
  });

  // Live "still working…" meter, so a long first-token wait never looks stuck.
  readonly elapsedMs = signal(0);
  private turnStartMs = 0;
  private readonly workingLines = [
    'Thinking…',
    'Working on it…',
    'Still working…',
    'Composing a response…',
    'Almost there…',
  ];
  readonly workingMsg = signal(this.workingLines[0]);

  /** Relative age under a message ("just now", "7 hours ago"). */
  readonly nowTick = signal(Date.now());
  formatAge(ms: number | undefined): string {
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

  /** Branch the thread at this message into a new conversation. */
  async forkFromMessage(m: ChatMsg): Promise<void> {
    if (!this.sessionId || this.streaming()) return;
    const idx = this.messages().findIndex((x) => x.id === m.id);
    if (idx < 0) return;
    try {
      const r = await this.api.forkChatSession(this.sessionId, idx);
      this.threadChanged.emit();
      this.sessionCreated.emit(r.session_id);
    } catch {
      /* ignore */
    }
  }

  formatElapsed(ms: number): string {
    const s = Math.floor(ms / 1000);
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  }

  private readonly logEl = viewChild<ElementRef<HTMLElement>>('chatlog');
  // Auto-follow streaming only while the user is parked near the bottom.
  private stickBottom = true;
  /** Auto-follow driven by scroll DIRECTION (see App.onLogScroll): a distance
   *  threshold loses the race against streaming tokens, and a wheel-only
   *  listener misses scrollbar drags, keyboard paging and momentum scrolling. */
  private lastScrollTop = 0;
  private programmaticScroll = false;
  readonly showJumpToBottom = signal(false);

  private scrollToBottom(el: HTMLElement, smooth = false): void {
    this.programmaticScroll = true;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    setTimeout(() => (this.programmaticScroll = false), smooth ? 400 : 60);
  }

  jumpToBottom(): void {
    const el = this.logEl()?.nativeElement;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    this.showJumpToBottom.set(false);
  }

  onScroll(): void {
    const el = this.logEl()?.nativeElement;
    if (!el) return;
    this.showJumpToBottom.set(scrolledUp(el));
  }

  constructor() {
    setInterval(() => this.nowTick.set(Date.now()), 30_000);
    // The library is what Home offers before anything is typed, so it is
    // fetched once on boot rather than when the ideas row happens to render.
    void this.loadPrompts();
    // One more file can be the one that makes the row scrollable.
    effect(() => {
      this.attachments();
      queueMicrotask(() => this.measureAtts());
    });
    effect(() => {
      this.messages();
      queueMicrotask(() => {
        const el = this.logEl()?.nativeElement;
        // Instant follow: with rAF-paced text the content grows every frame, so
        // a competing 'smooth' scroll animation would stutter — 'auto' tracks it.
        // Only while the user is parked at the bottom; if they scrolled up to
        // read, don't yank them back down (claude.ai behaviour).
        if (!el) return;
        // Live geometry, not a cached flag — see App: this microtask beats the
        // browser's `scroll` event, so a flag set there would still be stale.
        if (!scrolledUp(el)) {
          el.scrollTop = el.scrollHeight;
          this.showJumpToBottom.set(false);
        } else {
          this.showJumpToBottom.set(true);
        }
      });
    });
    // React to the sidebar selecting a conversation (or "new" = null). Ignore
    // when it already matches what we're showing (e.g. the id we just created).
    effect(() => {
      const target = this.activeSession();
      if (target === this.loadedId) return;
      if (!target) this.resetThread();
      else void this.loadThread(target);
    });
    // Tick the elapsed meter and rotate the "still working…" line while a turn
    // is in flight, so a long wait shows progress instead of looking stuck.
    effect((onCleanup) => {
      if (!this.streaming()) return;
      let i = 0;
      this.workingMsg.set(this.workingLines[0]);
      const id = setInterval(() => {
        this.elapsedMs.set(Math.round(performance.now() - this.turnStartMs));
        if (this.elapsedMs() > (i + 1) * 2400) {
          i++;
          this.workingMsg.set(this.workingLines[i % this.workingLines.length]);
        }
      }, 250);
      onCleanup(() => clearInterval(id));
    });
  }

  private resetThread(): void {
    if (this.voiceMode()) this.exitVoice();
    this.smoother?.cancel();
    this.smoother = null;
    this.messages.set([]);
    this.attachments.set([]);
    this.draft.set('');
    this.sessionId = null;
    this.loadedId = null;
    this.currentAssistant = null;
  }

  private async loadThread(id: string): Promise<void> {
    this.loadedId = id;
    this.sessionId = id;
    this.currentAssistant = null;
    try {
      const t = await this.api.chatTranscript(id);
      const msgs: ChatMsg[] = [];
      for (const m of t.messages) {
        if (m.role !== 'user' && m.role !== 'assistant') continue;
        const text = typeof m.content === 'string' ? m.content : this.contentText(m.content);
        // Reasoning is part of the record, not just of the moment: a reopened
        // thread shows what the model thought, collapsed, exactly as it was
        // left. The sealed reasoning stays on the server — this is the summary.
        const meta = (m['meta'] ?? {}) as Record<string, unknown>;
        const usage = (meta['usage'] ?? {}) as Record<string, unknown>;
        // A stored assistant message with no text is the model asking for a
        // tool, not an answer. Kept in the transcript because that is the
        // record; left out of the thread because on screen it is an empty
        // bubble with a Copy button under it.
        //
        // Reasoning is no reason to keep one: Home does not draw reasoning at
        // all (see the note in the template above the bubble), so a
        // thinking-only message has nothing to render either.
        if (m.role === 'assistant' && !text.trim()) continue;
        msgs.push({
          id: m.uuid || crypto.randomUUID(),
          role: m.role,
          text,
          streaming: false,
          thinking: (meta['thinking_summary'] as string) || undefined,
          thinkingTokens: (usage['reasoning_tokens'] as number) || undefined,
        });
      }
      // Ensure a chat session object exists on the server for follow-up turns.
      await this.api.createChatSession({ resume: true, sessionId: id });
      this.messages.set(msgs);
    } catch {
      this.messages.set([]);
    }
  }

  /** Plain text from a transcript message's content (string or multimodal). */
  private contentText(content: unknown): string {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content
        .filter((p): p is { type: string; text: string } =>
          !!p && typeof p === 'object' && (p as { type?: string }).type === 'text')
        .map((p) => p.text)
        .join(' ');
    }
    return '';
  }

  /** The last thing the assistant said, for a notification's summary line. */
  private lastAnswerText(): string {
    for (let i = this.messages().length - 1; i >= 0; i--) {
      const m = this.messages()[i];
      if (m.role === 'assistant' && m.text) return m.text;
    }
    return 'Answer ready.';
  }

  /** Time-aware greeting, by name only when a name is actually known.
   *
   *  "Evening, macmanishkr20" greeted somebody by the handle in their email
   *  address. Where no name has been given, the hour alone is the greeting:
   *  warm, correct, and not a login on display. Set one in Customize. */
  readonly greeting = computed(() => {
    const h = new Date().getHours();
    const part = h < 12 ? 'Morning' : h < 18 ? 'Afternoon' : 'Evening';
    const name = this.auth.displayName();
    return name ? `${part}, ${name}` : part;
  });

  onKeydown(ev: KeyboardEvent): void {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      void this.send();
    }
  }

  useIdea(text: string): void {
    this.draft.set(text);
    void this.send();
  }

  goAgent(): void {
    this.switchToAgent.emit();
  }

  // -- attachments: pick / paste / drop ------------------------------------
  openPicker(): void {
    this.fileInput()?.nativeElement.click();
  }

  onFilesPicked(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    if (input.files) void this.addFiles(input.files);
    input.value = ''; // allow re-picking the same file
  }

  onPaste(ev: ClipboardEvent): void {
    const files = ev.clipboardData?.files;
    if (files && files.length) {
      ev.preventDefault(); // pasted an image/file — don't also paste its text
      void this.addFiles(files);
    }
  }

  onDragOver(ev: DragEvent): void {
    if (ev.dataTransfer?.types?.includes('Files')) {
      ev.preventDefault();
      this.dragOver.set(true);
    }
  }
  onDragLeave(): void {
    this.dragOver.set(false);
  }
  onDrop(ev: DragEvent): void {
    ev.preventDefault();
    this.dragOver.set(false);
    if (ev.dataTransfer?.files?.length) void this.addFiles(ev.dataTransfer.files);
  }


  // -- the attachment strip -------------------------------------------------
  // It scrolls sideways instead of wrapping, so the composer keeps its height
  // however many files are attached. The arrows exist because a mouse has no
  // obvious way to scroll horizontally; they appear only when there is
  // something past the edge.
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

  private async addFiles(files: FileList): Promise<void> {
    const { added, errors } = await readFiles(files);
    if (added.length) this.attachments.update((list) => [...list, ...added]);
    if (errors.length) {
      this.attachError.set(errors[0]);
      setTimeout(() => this.attachError.set(''), 4000);
    }
  }

  formatSize = formatSize;

  async send(): Promise<void> {
    const content = this.draft().trim();
    const atts = this.attachments();
    if ((!content && atts.length === 0) || this.streaming()) return;
    this.stickBottom = true; // a fresh prompt re-arms auto-follow
    this.draft.set('');
    this.attachments.set([]);
    this.push({
      id: crypto.randomUUID(),
      role: 'user',
      text: content,
      streaming: false,
      at: Date.now(),
      atts: atts.length ? atts : undefined,
    });

    const payload = toWire(atts);
    await this.runStream(async () => {
      if (!this.sessionId) {
        const res = await this.api.createChatSession({
          model: this.activeModel() || undefined,
          effort: this.activeEffort(),
        });
        this.sessionId = res.session_id;
        this.loadedId = res.session_id; // keep in sync so the input echo is a no-op
        this.sessionCreated.emit(res.session_id);
      }
      await this.api.streamChatMessage(
        this.sessionId,
        content,
        (ev) => this.onEvent(ev),
        payload,
        this.workIq(),
      );
    });
  }

  /** Shared turn runner: set up streaming state, run `fn`, then finalise the
   *  pending assistant bubble. Used by send / regenerate / edit. */
  private async runStream(fn: () => Promise<void>): Promise<void> {
    if (this.streaming()) return;
    this.turnStartMs = performance.now();
    this.elapsedMs.set(0);
    this.streaming.set(true);
    this.currentAssistant = null;
    this.pendingSources = null;
    this.turnNotify.arm();
    try {
      await fn();
    } catch (err) {
      this.push({
        id: crypto.randomUUID(),
        role: 'assistant',
        text: '⚠️ ' + (err instanceof Error ? err.message : 'Chat request failed.'),
        streaming: false,
      });
    } finally {
      this.turnNotify.finished('home', this.lastAnswerText());
      const pending = this.currentAssistant as ChatMsg | null;
      if (pending) {
        this.smoother?.finish();
        this.smoother = null;
        this.patch(pending.id, (m) => ({ ...m, streaming: false }));
        this.currentAssistant = null;
      }
      this.streaming.set(false);
      this.threadChanged.emit(); // refresh the sidebar (title / recency)
    }
  }

  // -- message actions (Copy / Retry / Edit), like claude.ai ----------------
  readonly copiedId = signal<string | null>(null);
  readonly editingId = signal<string | null>(null);
  readonly editDraft = signal('');

  async copyMsg(m: ChatMsg): Promise<void> {
    try {
      await navigator.clipboard.writeText(m.text || '');
      this.copiedId.set(m.id);
      setTimeout(() => this.copiedId() === m.id && this.copiedId.set(null), 1300);
    } catch {
      /* clipboard unavailable */
    }
  }

  /** Retry: drop the last assistant reply and re-answer the same prompt. */
  async regenerate(): Promise<void> {
    if (!this.sessionId || this.streaming()) return;
    this.messages.update((list) => {
      const copy = [...list];
      while (copy.length && copy[copy.length - 1].role === 'assistant') copy.pop();
      return copy;
    });
    await this.runStream(() =>
      this.api.streamChatRegenerate(this.sessionId!, (ev) => this.onEvent(ev), this.workIq()),
    );
  }

  startEdit(m: ChatMsg): void {
    if (this.streaming()) return;
    this.editDraft.set(m.text || '');
    this.editingId.set(m.id);
  }
  cancelEdit(): void {
    this.editingId.set(null);
  }
  /** Edit a user message: truncate the thread there, resend the new text. */
  async commitEdit(m: ChatMsg): Promise<void> {
    const idx = this.messages().findIndex((x) => x.id === m.id);
    const text = this.editDraft().trim();
    this.editingId.set(null);
    if (idx < 0 || !text || !this.sessionId || this.streaming()) return;
    this.messages.update((list) => list.slice(0, idx));
    this.push({ id: crypto.randomUUID(), role: 'user', text, streaming: false });
    await this.runStream(() =>
      this.api.streamChatEdit(this.sessionId!, idx, text, (ev) => this.onEvent(ev), this.workIq()),
    );
  }

  async abort(): Promise<void> {
    this.smoother?.cancel();
    if (this.sessionId) {
      try {
        await this.api.abortChat(this.sessionId);
      } catch {
        /* ignore */
      }
    }
  }

  // -- voice mode (Azure OpenAI Realtime, speech-to-speech over WebRTC) -----
  /** Enter voice mode: mint an ephemeral key, open a WebRTC session with the
   *  realtime model, and stream mic audio in / the model's voice out. Server
   *  voice-activity-detection drives the turns — no push-to-talk. */
  async openVoice(): Promise<void> {
    if (!this.voiceSupported) {
      this.flashVoiceError('Voice mode needs a modern browser with WebRTC.');
      return;
    }
    if (!this.voiceAvailable()) {
      this.flashVoiceError('Voice mode isn’t configured — set AZURE_OPENAI_REALTIME_DEPLOYMENT in .env');
      return;
    }
    this.voiceError.set('');
    this.voiceHeard.set('');
    this.voiceReply.set('');
    this.voiceState.set('connecting');
    this.voiceMode.set(true);
    try {
      await this.connectRealtime();
    } catch (err) {
      this.flashVoiceError(err instanceof Error ? err.message : 'Could not start voice mode.');
      this.exitVoice();
    }
  }

  private async connectRealtime(): Promise<void> {
    const { token, webrtc_url } = await this.api.voiceSession();

    const pc = new RTCPeerConnection();
    this.pc = pc;

    // Play the model's voice output.
    const audioEl = document.createElement('audio');
    audioEl.autoplay = true;
    this.voiceAudioEl = audioEl;
    pc.ontrack = (e) => {
      if (e.streams[0]) audioEl.srcObject = e.streams[0];
    };

    // Stream the microphone in.
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.micStream = mic;
    for (const track of mic.getAudioTracks()) pc.addTrack(track, mic);

    // Events channel (server VAD drives the conversation turns automatically).
    const dc = pc.createDataChannel('realtime-channel');
    this.dc = dc;
    dc.addEventListener('open', () => {
      if (this.voiceMode()) this.voiceState.set('listening');
    });
    dc.addEventListener('message', (e) => this.onRealtimeEvent(e));

    pc.onconnectionstatechange = () => {
      if (
        this.voiceMode() &&
        (pc.connectionState === 'failed' || pc.connectionState === 'disconnected')
      ) {
        this.flashVoiceError('Voice connection lost.');
        this.exitVoice();
      }
    };

    // SDP offer → Azure WebRTC calls endpoint (auth with the ephemeral key).
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const resp = await fetch(webrtc_url, {
      method: 'POST',
      body: offer.sdp,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/sdp' },
    });
    if (!resp.ok) throw new Error('Voice handshake failed (' + resp.status + ').');
    const answer = await resp.text();
    await pc.setRemoteDescription({ type: 'answer', sdp: answer });
  }

  /** Realtime data-channel events (webrtcfilter=on keeps this set small). */
  private onRealtimeEvent(e: MessageEvent): void {
    let ev: any;
    try {
      ev = JSON.parse(e.data);
    } catch {
      return;
    }
    switch (ev.type) {
      case 'input_audio_buffer.speech_started':
        this.voiceState.set('listening');
        this.voiceHeard.set('');
        break;
      case 'conversation.item.input_audio_transcription.completed':
        this.voiceHeard.set((ev.transcript || '').trim());
        break;
      case 'output_audio_buffer.started':
        this.voiceState.set('speaking');
        this.voiceReply.set('');
        break;
      case 'response.output_audio_transcript.delta':
        this.voiceReply.update((t) => t + (ev.delta || ''));
        break;
      case 'response.output_audio_transcript.done':
        if (ev.transcript) this.voiceReply.set(ev.transcript);
        break;
      case 'output_audio_buffer.stopped':
        if (this.voiceMode()) this.voiceState.set('listening');
        break;
    }
  }

  /** Barge-in: tap while the assistant is speaking to cut it off and listen. */
  interruptVoice(): void {
    if (this.voiceState() === 'speaking' && this.dc?.readyState === 'open') {
      try {
        this.dc.send(JSON.stringify({ type: 'response.cancel' }));
        this.dc.send(JSON.stringify({ type: 'output_audio_buffer.clear' }));
      } catch {
        /* ignore */
      }
      this.voiceState.set('listening');
    }
  }

  /** Leave voice mode: tear down the WebRTC session and release the mic. */
  exitVoice(): void {
    this.voiceMode.set(false);
    this.voiceState.set('connecting');
    try {
      this.dc?.close();
    } catch {
      /* ignore */
    }
    try {
      this.pc?.close();
    } catch {
      /* ignore */
    }
    this.micStream?.getTracks().forEach((t) => t.stop());
    if (this.voiceAudioEl) {
      this.voiceAudioEl.srcObject = null;
      this.voiceAudioEl.remove();
    }
    this.dc = null;
    this.pc = null;
    this.micStream = null;
    this.voiceAudioEl = null;
    this.voiceHeard.set('');
  }

  private flashVoiceError(msg: string): void {
    this.voiceError.set(msg);
    setTimeout(() => this.voiceError.set(''), 5000);
  }
  /** Show or hide a finished turn's reasoning. It collapses on its own once
   *  the answer starts, so what stays on screen is the answer. */

  private onEvent(ev: CompassEvent): void {
    switch (ev.type) {
      case 'work_iq_sources':
        // Arrives before the answer streams — hold it for the reply bubble.
        this.pendingSources = (ev['sources'] as WorkIqSource[]) ?? null;
        break;
      case 'server_tool_used': {
        // A page the model opened while searching is a source in exactly the
        // sense the Work IQ strip already means, so it goes in the same place
        // rather than into a second surface that says the same thing.
        const urls = (ev['sources'] as string[]) ?? [];
        const detail = (ev['detail'] as string) ?? '';
        // Anything without a page to cite — code execution, a bare query —
        // still gets said, or it happens invisibly.
        if (!urls.length) {
          if (detail && this.currentAssistant) {
            this.patch(this.currentAssistant.id, (m) => ({
              ...m,
              serverActivity: [...(m.serverActivity ?? []), detail],
            }));
          }
          break;
        }
        // Unlike Work IQ's, these arrive after the answer has begun: the
        // search happens inside the turn, not before it.
        const target = this.currentAssistant;
        const add = (have: WorkIqSource[]) => {
          const merged = [...have];
          for (const url of urls) {
            if (merged.some((s) => s.url === url)) continue;
            let title = url;
            try { title = new URL(url).hostname.replace(/^www\./, ''); } catch {}
            merged.push({ n: merged.length + 1, title, url });
          }
          return merged;
        };
        if (target) {
          this.patch(target.id, (m) => ({ ...m, sources: add(m.sources ?? []) }));
        } else {
          this.pendingSources = add(this.pendingSources ?? []);
        }
        break;
      }
      case 'thinking_delta': {
        // Reasoning opens the bubble, so thinking is visible before the first
        // word of the answer rather than after it.
        if (!this.currentAssistant) {
          this.currentAssistant = {
            id: crypto.randomUUID(),
            role: 'assistant',
            text: '',
            streaming: true,
            sources: this.pendingSources ?? undefined,
          };
          this.push(this.currentAssistant);
          this.pendingSources = null;
        }
        const chunk = (ev['text'] as string) ?? '';
        const gap = (ev['starts_part'] as boolean) ? '\n\n' : '';
        const id = this.currentAssistant.id;
        this.patch(id, (m) => ({
          ...m,
          thinking: ((m.thinking ?? '') + (m.thinking ? gap : '') + chunk),
          thinkingLive: true,
        }));
        break;
      }
      case 'thinking_complete': {
        if (this.currentAssistant) {
          const id = this.currentAssistant.id;
          const tokens = (ev['tokens'] as number) ?? 0;
          this.patch(id, (m) => ({ ...m, thinkingLive: false, thinkingTokens: tokens }));
        }
        break;
      }
      case 'text_delta': {
        if (!this.currentAssistant) {
          this.currentAssistant = {
            id: crypto.randomUUID(),
            role: 'assistant',
            text: '',
            streaming: true,
            sources: this.pendingSources ?? undefined,
          };
          this.push(this.currentAssistant);
          this.pendingSources = null;
        }
        if (!this.smoother) {
          // Reveal tokens smoothly (rAF-paced) instead of per-network-chunk.
          // The bubble may already exist because thinking opened it; either
          // way the answer streams into that same one.
          const id = this.currentAssistant.id;
          this.smoother = new SmoothText((t) => this.patch(id, (m) => ({ ...m, text: t })));
        }
        this.smoother.push((ev['text'] as string) ?? '');
        break;
      }
      case 'assistant_message': {
        // A message carrying tool calls is the model asking for work, not
        // answering. Finalising the bubble here is what left an empty one on
        // screen with a Copy button under it while the tool was still
        // running — the answer arrives in a later message, into this same
        // bubble.
        const calls = (ev['tool_calls'] as unknown[]) ?? [];
        if (calls.length) break;
        if (this.currentAssistant) {
          const id = this.currentAssistant.id;
          this.smoother?.finish();
          this.smoother = null;
          this.patch(id, (m) => ({ ...m, streaming: false, thinkingLive: false }));
          this.currentAssistant = null;
        }
        break;
      }
      case 'tool_call_started': {
        const bubble = this.ensureAssistant();
        const id = (ev['tool_call_id'] as string) ?? crypto.randomUUID();
        const label = TOOL_LABELS[(ev['tool_name'] as string) ?? ''] ??
                      ((ev['tool_name'] as string) ?? 'Working');
        this.patch(bubble.id, (m) => ({
          ...m,
          toolActivity: [...(m.toolActivity ?? []),
                         { id, label, detail: '', running: true }],
        }));
        break;
      }
      case 'tool_progress': {
        if (!this.currentAssistant) break;
        const id = ev['tool_call_id'] as string;
        // The last non-empty line of the chunk: a render sends "Rendering 5
        // shots…\n" then a line per shot, and the newest one is the status.
        const detail = String(ev['data'] ?? '').split('\n')
          .map((l) => l.trim()).filter(Boolean).pop() ?? '';
        if (!detail) break;
        this.patch(this.currentAssistant.id, (m) => ({
          ...m,
          toolActivity: (m.toolActivity ?? []).map((a) =>
            a.id === id ? { ...a, detail } : a),
        }));
        break;
      }
      case 'tool_result': {
        if (!this.currentAssistant) break;
        const id = ev['tool_call_id'] as string;
        const failed = Boolean(ev['is_error']);
        this.patch(this.currentAssistant.id, (m) => ({
          ...m,
          toolActivity: (m.toolActivity ?? []).map((a) =>
            a.id === id ? { ...a, running: false, failed,
                            detail: failed ? 'could not finish' : '' } : a),
        }));
        break;
      }
      case 'refused': {
        // Not an error: the request was fine and the model declined. Shown
        // as its own note so the reader knows the turn was stopped rather
        // than that Compass broke, and — when text was already streamed —
        // that what is above is only the part written first.
        this.smoother?.finish();
        this.smoother = null;
        if (this.currentAssistant) {
          this.patch(this.currentAssistant.id, (m) => ({ ...m, streaming: false }));
          this.currentAssistant = null;
        }
        this.push({
          id: crypto.randomUUID(),
          role: 'assistant',
          text: '⊘ ' + ((ev['message'] as string) ?? 'The response was stopped.'),
          streaming: false,
        });
        break;
      }
      case 'error':
        this.push({
          id: crypto.randomUUID(),
          role: 'assistant',
          text: '⚠️ ' + ((ev['message'] as string) ?? 'unknown error'),
          streaming: false,
        });
        break;
    }
  }

  /** The bubble this turn is writing into, opening one if the turn has not
   *  produced anything yet. A tool call can be the first thing that happens
   *  in a turn, and it needs somewhere to say so. */
  private ensureAssistant(): ChatMsg {
    if (!this.currentAssistant) {
      this.currentAssistant = {
        id: crypto.randomUUID(),
        role: 'assistant',
        text: '',
        streaming: true,
        sources: this.pendingSources ?? undefined,
      };
      this.push(this.currentAssistant);
      this.pendingSources = null;
    }
    return this.currentAssistant;
  }

  private push(m: ChatMsg): void {
    this.messages.update((list) => [...list, m]);
  }
  private patch(id: string, fn: (m: ChatMsg) => ChatMsg): void {
    this.messages.update((list) => list.map((m) => (m.id === id ? fn(m) : m)));
  }
}
