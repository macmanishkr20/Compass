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
import { CompassApiService, TranscriptResponse, describeHttpError } from '../compass-api.service';
import { ImageActionsService } from '../image-actions.service';
import { LoadError } from '../load-error';
import { AuthService } from '../auth.service';
import { CompassMark } from '../compass-mark/compass-mark';
import { Markdown } from '../markdown/markdown';
import { Reorder } from '../reorder/reorder';
import { SavedPrompt } from '../models';
import { CompassEvent } from '../models';
import { ATTACH_ACCEPT, UiAttachment, formatSize, readFiles, toWire } from '../attachments';
import { SmoothText } from '../smooth-text';
import { TurnStatus } from '../turn-status';
import { modelLabel } from '../model-label';
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
  /** Spoken, not typed. Kept as a fact about the turn rather than
   *  rewritten to look the same: a transcript is not a draft somebody
   *  chose their words in, and a surface that wants to say so can. */
  voice?: boolean;
  /** Spoken, but the deployment declined to transcribe the question.
   *  The exchange is kept and the gap is shown, rather than filled in
   *  with words nobody said. */
  transcriptMissing?: boolean;
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

/** The fallback ladder, for before the shell has the real one and for a
 *  deployment the server did not describe. Deliberately the three levels
 *  every family measured so far accepts: the real list comes from the server,
 *  because the deployed families take overlapping but different ladders and
 *  any list hardcoded here offers one of them a level the API refuses.
 *  Higher means it thinks more often and goes further. */
const EFFORTS = ['low', 'medium', 'high'] as const;

/**
 * Home / Chat — a pure-conversation surface. It is a self-contained sibling of
 * the agent console (App), sharing none of its tool/permission machinery. It
 * talks to the isolated `/v1/chat/*` backend, which runs gpt-5 with no tools,
 * so there are never tool cards or permission prompts here — just chat.
 */
@Component({
  selector: 'app-home-chat',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, CompassMark, Markdown, Reorder, LoadError],
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
  private readonly imageActions = inject(ImageActionsService);
  private readonly auth = inject(AuthService);
  private readonly turnNotify = inject(TurnNotifyService);
  readonly lightbox = inject(LightboxService);

  // Inputs from the shell so we don't duplicate health fetching.
  readonly models = input<string[]>([]);
  /** Which levels each deployment accepts, from the shell's /healthz. Passed
   *  in rather than fetched for the same reason `models` is. */
  readonly effortsByModel = input<Record<string, string[]>>({});
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

  /** "Start a new conversation", from the failed-to-load card. The active
   *  thread is an input, so clearing it is the parent's to do. */
  readonly startNewThread = output<void>();

  /** This thread is not there — deleted, or never this account's. Same reason
   *  as above: the active thread is an input, so the parent has to be the one
   *  to stop pointing at it and to drop the row from its list. */
  readonly gone = output<string>();

  /** Which thread is being fetched, or "" — for the sidebar, which lives in
   *  the parent and otherwise has no way to know that the row just clicked
   *  is still waiting on the network. */
  readonly loadingChanged = output<string>();

  /** What the selected deployment will think at; see the note on EFFORTS. */
  readonly efforts = computed<readonly string[]>(
    () => this.effortsByModel()[this.activeModel()] ?? EFFORTS);
  /** The deployment's name as people say it, shown beside each effort
   *  level so the menu says which model the choice applies to — the
   *  ladders differ per family, so the level alone is ambiguous. */
  readonly modelLabel = modelLabel;
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

  /** The picture Edit was pressed on, or "" — see ImageActionsService.
   *
   *  Held here rather than written into the draft so the instruction stays
   *  the person's own words: the URL is attached to the turn when it is
   *  sent, which keeps a 60-character id out of the box they are typing in. */
  readonly editingPicture = signal('');

  cancelPictureEdit(): void {
    this.editingPicture.set('');
  }
  readonly messages = signal<ChatMsg[]>([]);
  readonly streaming = signal(false);
  readonly attachments = signal<UiAttachment[]>([]);
  readonly dragOver = signal(false);
  readonly attachError = signal('');
  private readonly fileInput = viewChild<ElementRef<HTMLInputElement>>('fileInput');
  private readonly composer = viewChild<ElementRef<HTMLTextAreaElement>>('composer');
  // -- Voice mode (Azure OpenAI Realtime, speech-to-speech over WebRTC) -----
  readonly voiceMode = signal(false);
  readonly voiceState = signal<'connecting' | 'listening' | 'speaking'>('connecting');
  readonly voiceHeard = signal(''); // live transcript of what the user said
  /** Spoken turns waiting to reach the server.
   *
   *  Voice mode runs browser-to-Azure over WebRTC, so the server never sees
   *  the conversation and nothing is written unless this client writes it.
   *  That makes the network between a finished exchange and the transcript a
   *  place where a conversation can simply vanish — so turns queue here,
   *  retry with backoff, and survive leaving voice mode. They are dropped
   *  only when the thread itself goes. */
  private pendingVoiceTurns: { turnId: string; heard: string; reply: string }[] = [];
  private voiceFlushTimer: ReturnType<typeof setTimeout> | null = null;
  private voiceFlushing = false;
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
  /** `key` names the row for as long as it exists, which is what an
   *  arrangement is stored against. Deliberately not the text: rewording a
   *  starter would otherwise move it back to where it began. */
  readonly builtInIdeas:
    { key: string; text: string; icon: 'bulb' | 'branch' | 'pen' | 'film' }[] = [
    { key: 'builtin:explain', text: 'Explain a tricky concept in simple terms', icon: 'bulb' },
    { key: 'builtin:names', text: 'Brainstorm names for a new project', icon: 'branch' },
    { key: 'builtin:draft', text: 'Draft a short message or email', icon: 'pen' },
    // Rendering is the one thing here that is not obviously a chat's job, so
    // it is the one that has to be said out loud.
    { key: 'builtin:teaser', text: 'Cut a teaser from photos I attach', icon: 'film' },
  ];

  // ── the prompt library ──────────────────────────────────────────────
  /** Everything saved, newest first. */
  readonly saved = signal<SavedPrompt[]>([]);
  /** How many fit on the Home screen before "Show all" takes over. */
  readonly onScreen = signal(4);
  readonly libraryOpen = signal(false);
  /** Whether the editor was opened from the library, so closing it goes back
   *  there rather than dumping you on Home. Editing one of several is the
   *  normal reason to be in that list. */
  private returnToLibrary = false;

  /** The save dialog: null when closed, otherwise what it is editing. */
  readonly saveDialog = signal<{
    id: string;          // '' for a new one
    title: string;
    text: string;
    original: string;    // what was typed, so a sharpen can be undone
    sharpened: boolean;
    /** Which of the session's prompts were merged, by their number in the
     *  conversation. Shown rather than kept quiet: a prompt assembled out of
     *  turns you cannot see is one you have to take on trust. */
    used: number[];
  } | null>(null);
  readonly sharpening = signal(false);
  readonly savingPrompt = signal(false);
  readonly promptError = signal('');

  /** The arrangement, as a list of keys, if one was ever dragged into place.
   *  Empty means nobody has said otherwise and the default below stands. */
  readonly order = signal<string[]>([]);

  /** What the Home screen offers: saved prompts first, then the built-ins to
   *  fill the row. A library with four entries is the person's own; an empty
   *  one should still suggest something rather than show a blank space.
   *
   *  Once anything has been dragged, that arrangement wins — but it is read
   *  as a preference about the rows it names, not as the whole truth, since
   *  the library changes underneath it. A row it does not mention is one
   *  saved or shipped since: a new prompt of your own goes to the top, where
   *  a just-saved thing belongs and where Home's four will actually show it,
   *  and a starter added in some later version goes to the bottom rather
   *  than pushing into an arrangement somebody chose. Keys naming rows that
   *  no longer exist simply fail to match, so a deleted prompt leaves no
   *  gap, and the next drag writes the list back without them. */
  readonly ideas = computed<
    { key: string; text: string; icon: string; id: string; title: string }[]
  >(() => {
    const mine = this.saved().map((p) => ({
      key: p.id, text: p.text, icon: p.icon, id: p.id, title: p.title,
    }));
    const builtIn = this.builtInIdeas.map((s) => ({
      key: s.key, text: s.text, icon: s.icon, id: '', title: s.text,
    }));
    const order = this.order();
    if (!order.length) return [...mine, ...builtIn];

    const byKey = new Map([...mine, ...builtIn].map((i) => [i.key, i]));
    const placed = new Set(order);
    return [
      ...mine.filter((i) => !placed.has(i.key)),
      ...order.map((k) => byKey.get(k)).filter((i) => !!i),
      ...builtIn.filter((i) => !placed.has(i.key)),
    ];
  });

  /** The first few, which is all Home shows. */
  readonly ideasOnScreen = computed(() => this.ideas().slice(0, this.onScreen()));

  /** Whether there is anything behind "Show all". */
  readonly hasMoreIdeas = computed(() => this.ideas().length > this.onScreen());

  async loadPrompts(): Promise<void> {
    try {
      const res = await this.api.savedPrompts();
      this.saved.set(res.prompts ?? []);
      this.order.set(res.order ?? []);
      if (res.on_screen) this.onScreen.set(res.on_screen);
    } catch {
      /* the built-in starters stand on their own */
    }
  }

  /** Drop a row at `to`, and remember where everything ended up.
   *
   *  Both lists index into `ideas()` — Home shows the first few of exactly
   *  that array — so one method serves the row on screen and the dialog
   *  behind it.
   *
   *  The whole arrangement is written, not the move: it is what makes the
   *  stored list self-healing, since keys for prompts since deleted are not
   *  in `ideas()` and so do not survive the round trip. The list moves first
   *  and is put back if the write fails, because a row that springs back to
   *  where it was is the only honest way to say the order was not kept. */
  async moveIdea(from: number, to: number): Promise<void> {
    const keys = this.ideas().map((i) => i.key);
    if (from === to || from < 0 || to < 0 || from >= keys.length || to >= keys.length) return;
    const previous = this.order();
    const [moved] = keys.splice(from, 1);
    keys.splice(to, 0, moved);
    this.order.set(keys);
    try {
      await this.api.setPromptOrder(keys);
    } catch {
      this.order.set(previous);
      this.promptError.set('That order could not be saved.');
    }
  }

  /** Open the save dialog for one message. */
  savePromptFrom(m: { id: string; text: string }): void {
    const text = (m.text || '').trim();
    if (!text) return;
    this.promptError.set('');
    this.saveDialog.set({ id: '', title: '', text, original: text,
                          sharpened: false, used: [] });
  }

  editSaved(p: SavedPrompt): void {
    this.promptError.set('');
    this.saveDialog.set({
      id: p.id, title: p.title, text: p.text, original: p.text,
      sharpened: false, used: [],
    });
  }

  closeSaveDialog(): void {
    this.saveDialog.set(null);
    this.sharpening.set(false);
    this.promptError.set('');
    if (this.returnToLibrary) {
      this.returnToLibrary = false;
      this.libraryOpen.set(true);
    }
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
      const res = await this.api.sharpenPrompt(d.original, this.threadForSharpen());
      this.saveDialog.set({
        ...d,
        title: d.title || res.title || '',
        text: res.text || d.text,
        sharpened: true,
        used: res.used ?? [],
      });
    } catch (err) {
      this.promptError.set(String(err));
    } finally {
      this.sharpening.set(false);
    }
  }

  /** Everything this answer cited, for the strip under it.
   *
   *  Derived from the answer's own text, not only from what the search tool
   *  reported. A hosted web search only names a URL when it *opens a page*:
   *  a turn that searched, read the results and cited five sites in prose
   *  reported none at all, so the strip was empty while five chips sat in
   *  the paragraph above it. What the answer cited is written in the answer,
   *  which is also the part that survives a reload — so reading it from
   *  there fixes the live case and the reopened one together, and the strip
   *  can never disagree with the chips.
   *
   *  Explicit sources still come first: a Work IQ document is a source with
   *  no URL in the prose at all. */
  sourcesFor(m: { text?: string; sources?: WorkIqSource[] }): WorkIqSource[] {
    const out: WorkIqSource[] = [...(m.sources ?? [])];
    const seen = new Set(out.map((s) => this.hostOf(s.url) || s.url));
    for (const url of this.citedIn(m.text ?? '')) {
      const key = this.hostOf(url) || url;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ n: out.length + 1, title: key, url });
    }
    return out.map((s, i) => ({ ...s, n: i + 1 }));
  }

  /** The links an answer cited: a markdown link whose label is a bare host,
   *  which is how a citation is written and how the chip is recognised. One
   *  entry per host — six bullets citing the same site are one source. */
  private citedIn(text: string): string[] {
    const out: string[] = [];
    const seen = new Set<string>();
    for (const m of text.matchAll(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g)) {
      const label = m[1].trim().replace(/^www\./, '');
      if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(label)) continue;
      const host = this.hostOf(m[2]);
      if (!host || seen.has(host)) continue;
      seen.add(host);
      out.push(m[2]);
    }
    return out;
  }

  /** The server-side icon for a cited page, or '' when there is no host to
   *  ask about — a Work IQ document is a file in a knowledge base, not a
   *  site, and keeps its numbered form. */
  faviconOf(url: string): string {
    const host = this.hostOf(url);
    return host ? `/v1/chat/favicon?url=${encodeURIComponent(url)}` : '';
  }

  /** The letter shown under the icon, so a site with none still has a mark. */
  initialOf(url: string): string {
    return (this.hostOf(url)[0] || '?').toUpperCase();
  }

  private hostOf(url: string): string {
    try {
      const h = new URL(url, location.origin).hostname.replace(/^www\./, '');
      // Same-origin means this server's own media, not a cited site.
      return h && h !== location.hostname ? h : '';
    } catch {
      return '';
    }
  }

  /** Which prompts went into the build, said plainly.
   *
   *  "this prompt alone" is a real and common answer — most prompts do not
   *  have a thread — and saying so is the difference between the button
   *  having decided nothing developed it and the button having failed. */
  usedLine(used: number[]): string {
    if (!used.length) return '';
    if (used.length === 1) return 'Built from this prompt alone';
    const total = this.promptCount();
    return total > used.length
      ? `Built from ${used.length} of your ${total} prompts in this chat`
      : `Built from all ${used.length} of your prompts in this chat`;
  }

  /** The exact numbers, for the tooltip — the ratio says it was selective,
   *  this says which. Ordinals are not drawn in the transcript, so they go
   *  where somebody can look them up rather than in the line itself. */
  usedDetail(used: number[]): string {
    if (used.length < 2) return '';
    return `Merged your prompts ${used.map((i) => `#${i}`).join(', ')}, `
      + `counting only the ones you typed. The rest of the chat was read and `
      + `left out.`;
  }

  /** How many prompts the person has typed — the same ones, in the same
   *  order, that `threadForSharpen` numbers when it sends them. */
  private promptCount(): number {
    return this.messages().filter((m) => m.role === 'user' && (m.text || '').trim()).length;
  }

  /** Put back what was typed, if the rewrite went somewhere they did not mean. */
  undoSharpen(): void {
    const d = this.saveDialog();
    if (d) this.saveDialog.set({ ...d, text: d.original, sharpened: false, used: [] });
  }

  /** The conversation to reason over, oldest first.
   *
   *  The whole thing, not the few messages before the selected prompt. What
   *  somebody asked *after* picking a prompt is usually where the intent is:
   *  people open with "hi" and only then say what they want, so sending only
   *  what preceded it gave the model the one part with nothing in it. Which
   *  of these turns actually belong to the selected prompt's thread is the
   *  model's judgement, not a rule applied here — a conversation can change
   *  subject, and a cut-off by position cannot tell. */
  private threadForSharpen(): { role: string; text: string }[] {
    return this.messages()
      .filter((m) => (m.text || '').trim())
      .map((m) => ({ role: m.role === 'user' ? 'user' : 'assistant', text: m.text || '' }));
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

  /** The library dialog lists ideas, not raw records, so it acts by id.
   *
   *  It also closes the library on the way. Left open, the two dialogs
   *  stacked — and since the library is written second in the template it
   *  painted over the editor, so the thing you had just asked for was the
   *  thing you could not see. */
  editSavedById(id: string): void {
    const row = this.saved().find((p) => p.id === id);
    if (!row) return;
    this.returnToLibrary = this.libraryOpen();
    this.libraryOpen.set(false);
    this.editSaved(row);
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
  /** What the turn is actually doing, from the stream rather than a timer.
   *  See TurnStatus. */
  private readonly turnStatus = new TurnStatus();
  readonly workingMsg = this.turnStatus.label;

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
    // Edit, pressed on a picture anywhere in the thread. Taken up here and
    // cleared, so pressing it twice on two pictures leaves the second one
    // showing rather than both.
    effect(() => {
      const wanted = this.imageActions.editing();
      if (!wanted) return;
      this.editingPicture.set(wanted);
      this.imageActions.taken();
      queueMicrotask(() => this.composer()?.nativeElement.focus());
    });
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
    // Tick the elapsed meter while a turn is in flight, so a long first-token
    // wait never looks stuck. The status line rides the same timer, but only
    // so its thinking phrase can move: every other phase changes when the
    // stream says so.
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
    // Or a new thread would offer to load the previous one's earlier pages.
    this.earlierSeq.set(null);
    this.loadingEarlier.set(false);
    // And a fetch in flight must stop owning the window the moment somebody
    // starts a new conversation — its own guard will drop the result.
    this.loadingThread.set('');
    this.loadingSlow.set(false);
    this.loadError.set(null);
  }

  /** Which thread is being fetched, or "" when none is.
   *
   *  Drives all of it: the bar across the top of the panel, the spinner on
   *  the sidebar row, the skeleton in the window and the locked composer.
   *  One signal rather than four, so they cannot disagree about whether
   *  something is loading. */
  readonly loadingThread = signal('');

  /** Set when a fetch has been going long enough to be worth explaining. */
  readonly loadingSlow = signal(false);

  /** The thread that failed, and why, or null. */
  readonly loadError = signal<{ id: string; detail: string } | null>(null);

  /** How long a fetch may take before the note says why it is still going.
   *  A fetch under this is simply quick; past it, silence reads as a hang. */
  private static readonly SLOW_AFTER_MS = 900;

  private async loadThread(id: string): Promise<void> {
    this.loadedId = id;
    this.sessionId = id;
    this.currentAssistant = null;
    this.loadError.set(null);
    this.earlierSeq.set(null);
    // Cleared before the skeleton goes up, so the previous conversation is
    // never left on screen underneath a spinner that belongs to another one.
    this.messages.set([]);
    this.loadingThread.set(id);
    this.loadingChanged.emit(id);
    this.loadingSlow.set(false);
    const slow = setTimeout(() => {
      if (this.loadedId === id) this.loadingSlow.set(true);
    }, HomeChat.SLOW_AFTER_MS);
    try {
      // Both at once. The thread is drawn from the transcript; the session
      // object is only needed before the *next* turn is sent, so waiting for
      // it before showing anything made opening a conversation cost two
      // round trips instead of one.
      const [t] = await Promise.all([
        this.api.chatTranscript(id, { limit: HomeChat.THREAD_PAGE }),
        this.api.createChatSession({ resume: true, sessionId: id }),
      ]);
      // A newer click wins. Two conversations opened quickly resolve in
      // whatever order the network decides, and without this the slower of
      // the two paints last — showing a thread the user has already left.
      if (this.loadedId !== id) return;
      this.earlierSeq.set(t.before_seq ?? null);
      const msgs = this.buildMessages(t.messages);
      if (this.loadedId !== id) return;
      this.messages.set(msgs);
    } catch (err) {
      // Said out loud rather than shown as an empty thread. A conversation
      // that failed to load and one with nothing in it looked identical
      // before, and only one of them is worth pressing Try again on.
      if (this.loadedId !== id) return;
      this.messages.set([]);
      // Unless there is no such conversation. The card says the store did not
      // answer and the conversation is still there, and for a 404 both halves
      // are untrue: it is gone, or it was somebody else's and the server is
      // right to refuse it. Trying again cannot help, so the thread simply
      // resets to an empty one — which is what a new conversation looks like,
      // and what somebody signing in to a fresh account should see.
      if ((err as { status?: number })?.status === 404) {
        this.resetThread();
        this.gone.emit(id);
        return;
      }
      this.loadError.set({ id, detail: describeHttpError(err) });
    } finally {
      clearTimeout(slow);
      if (this.loadedId === id) {
        this.loadingThread.set('');
        this.loadingChanged.emit('');
        this.loadingSlow.set(false);
      }
    }
  }

  /** Try the failed thread again. */
  retryThread(): void {
    const failed = this.loadError();
    if (!failed) return;
    this.loadError.set(null);
    void this.loadThread(failed.id);
  }

  /** How much of a thread is fetched when it is opened.
   *
   *  Home threads are short — the longest of the 79 here is 36 messages — so
   *  in practice this fetches all of them and the control below never shows.
   *  It is here because "in practice" is a statement about today's data, and
   *  a thread with a year of history in it should open as quickly as a new
   *  one. The same number Code uses, for the same reason. */
  private static readonly THREAD_PAGE = 150;

  /** Where the page above the one on screen ends, or null when the whole
   *  thread is showing. */
  readonly earlierSeq = signal<number | null>(null);
  readonly loadingEarlier = signal(false);

  /** Fetch the page before the one on screen and put it above. */
  async loadEarlier(): Promise<void> {
    const id = this.loadedId;
    const cursor = this.earlierSeq();
    if (!id || cursor === null || this.loadingEarlier()) return;
    this.loadingEarlier.set(true);
    try {
      const t = await this.api.chatTranscript(id, {
        limit: HomeChat.THREAD_PAGE,
        beforeSeq: cursor,
      });
      // The thread may have been left while this was in flight.
      if (this.loadedId !== id) return;
      const older = this.buildMessages(t.messages);
      this.messages.update((msgs) => [...older, ...msgs]);
      this.earlierSeq.set(t.before_seq ?? null);
    } catch {
      // Left as it was, with the control still offering to try again.
    } finally {
      this.loadingEarlier.set(false);
    }
  }

  /** Stored messages turned into what the thread draws.
   *
   *  Pulled out of `loadThread` so a page fetched later is built exactly the
   *  same way as the first — two builders would drift, and the one used less
   *  often would be the one that drifted.
   */
  private buildMessages(source: TranscriptResponse['messages']): ChatMsg[] {
    const t = { messages: source };
    {
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
        // A spoken question the deployment would not transcribe is stored
        // with no content on purpose. On screen it keeps the placeholder it
        // had when it was said, so the exchange still reads as an exchange.
        const spokenGap = !text.trim()
          && Boolean((meta['transcript_unavailable'] as boolean | undefined));
        // The pages the answer rested on, restored with it. They are on the
        // message for the same reason the reasoning summary is: which sources
        // an answer used is part of the record, not of the moment it arrived.
        const cited = (meta['sources'] as string[] | undefined) ?? [];
        msgs.push({
          id: m.uuid || crypto.randomUUID(),
          role: m.role,
          text: spokenGap ? '…' : text,
          streaming: false,
          thinking: (meta['thinking_summary'] as string) || undefined,
          thinkingTokens: (usage['reasoning_tokens'] as number) || undefined,
          sources: cited.length ? cited.map((url, i) => {
            let title = url;
            try { title = new URL(url).hostname.replace(/^www\./, ''); } catch {}
            return { n: i + 1, title, url };
          }) : undefined,
          serverActivity: (meta['activity'] as string[] | undefined) ?? undefined,
          // Spoken turns are part of the record too. A reopened thread shows
          // the conversation that was had out loud, marked as spoken, rather
          // than silently reading as though it had been typed.
          voice: (meta['voice'] as boolean | undefined) || undefined,
          transcriptMissing:
            (meta['transcript_unavailable'] as boolean | undefined) || undefined,
        });
      }
      return msgs;
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
    const editing = this.editingPicture();
    if ((!content && atts.length === 0) || this.streaming()) return;
    this.stickBottom = true; // a fresh prompt re-arms auto-follow
    this.draft.set('');
    this.attachments.set([]);
    this.editingPicture.set('');
    this.push({
      id: crypto.randomUUID(),
      role: 'user',
      text: content,
      streaming: false,
      at: Date.now(),
      atts: atts.length ? atts : undefined,
    });

    const payload = toWire(atts);
    // When Edit was pressed, the picture rides along with the instruction.
    // Said to the model rather than shown to the person: the bubble above
    // carries what they typed, and this line is the part that tells the
    // model which picture "this" is.
    const sent = editing
      ? `Edit this image: ${editing}\n\n${content}`
      : content;
    await this.runStream(async () => {
      // Same path voice uses, so a thread opened by speaking and one opened
      // by typing are created identically.
      const sessionId = await this.ensureSession();
      await this.api.streamChatMessage(
        sessionId,
        sent,
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
    this.turnStatus.start();
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
      case 'response.output_audio_transcript.done': {
        if (ev.transcript) this.voiceReply.set(ev.transcript);
        // Both halves are known here: the question was transcribed while it
        // was being answered. Keeping it now rather than on exit means a
        // dropped connection costs the last exchange at worst, not all of
        // them — exit is not guaranteed to run.
        this.recordVoiceExchange(this.voiceHeard(), this.voiceReply());
        break;
      }
      case 'output_audio_buffer.stopped':
        if (this.voiceMode()) this.voiceState.set('listening');
        break;
    }
  }

  /** Keep one finished spoken exchange: on screen now, on the server soon.
   *
   *  Called when the assistant's transcript completes, which is the point at
   *  which both halves of the exchange are known — the question was
   *  transcribed while it was being answered.
   *
   *  The thread is updated first and unconditionally. A person who has just
   *  spoken should see it in the conversation whether or not the write lands,
   *  and the write is retried underneath rather than blocking the next thing
   *  they say.
   */
  private recordVoiceExchange(heard: string, reply: string): void {
    const said = (heard || '').trim();
    const answered = (reply || '').trim();
    if (!said && !answered) return;

    const turnId = crypto.randomUUID();
    const at = Date.now();
    this.push({
      id: turnId + '-u', role: 'user', streaming: false, at,
      text: said || '…', voice: true, transcriptMissing: !said,
    });
    if (answered) {
      this.push({
        id: turnId + '-a', role: 'assistant', streaming: false, at,
        text: answered, voice: true,
      });
    }
    this.threadChanged.emit();

    this.pendingVoiceTurns.push({ turnId, heard: said, reply: answered });
    void this.flushVoiceTurns();
  }

  /** Drain the queue, oldest first, retrying on failure.
   *
   *  Serial on purpose: the turns are a conversation and arrive in the order
   *  they were spoken. Sending them at once would let the second overtake the
   *  first on a slow connection and write the exchange backwards.
   */
  private async flushVoiceTurns(): Promise<void> {
    if (this.voiceFlushing || !this.pendingVoiceTurns.length) return;
    this.voiceFlushing = true;
    try {
      while (this.pendingVoiceTurns.length) {
        const sessionId = await this.ensureSession();
        const turn = this.pendingVoiceTurns[0];
        try {
          await this.api.recordVoiceTurn(
            sessionId, turn.turnId, turn.heard, turn.reply);
          this.pendingVoiceTurns.shift();
        } catch {
          // Keep it and come back. The turn is already on screen, so the
          // cost of waiting is invisible; the cost of dropping it is the
          // conversation. Backed off so a server that is down is not hit
          // every few hundred milliseconds for the rest of the session.
          this.scheduleVoiceFlush();
          return;
        }
      }
    } catch {
      this.scheduleVoiceFlush();
    } finally {
      this.voiceFlushing = false;
    }
  }

  private scheduleVoiceFlush(delayMs = 4000): void {
    if (this.voiceFlushTimer) return;
    this.voiceFlushTimer = setTimeout(() => {
      this.voiceFlushTimer = null;
      void this.flushVoiceTurns();
    }, delayMs);
  }

  /** The thread's id, creating the thread if speaking is the first thing that
   *  has happened in it. Voice mode can be the opening move of a
   *  conversation, and a spoken exchange still has to land somewhere. */
  private async ensureSession(): Promise<string> {
    if (this.sessionId) return this.sessionId;
    const res = await this.api.createChatSession({
      model: this.activeModel() || undefined,
      effort: this.activeEffort(),
    });
    this.sessionId = res.session_id;
    this.loadedId = res.session_id;
    this.sessionCreated.emit(res.session_id);
    return res.session_id;
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
    // Every event, before anything branches on it — see the note in App.
    this.turnStatus.note(ev.type, ev as { tool_name?: unknown });
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
