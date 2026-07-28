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
import { CompassApiService } from '../compass-api.service';
import { AuthService } from '../auth.service';
import { BlurOnChange } from '../blur-on-change.directive';
import { CompassMark } from '../compass-mark/compass-mark';
import { Markdown } from '../markdown/markdown';
import { CompassEvent } from '../models';
import { ATTACH_ACCEPT, UiAttachment, formatSize, readFiles, toWire } from '../attachments';
import { SmoothText } from '../smooth-text';
import { LightboxService } from '../lightbox.service';

interface WorkIqSource {
  n: number;
  title: string;
  url: string;
}

interface ChatMsg {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  streaming: boolean;
  atts?: UiAttachment[];
  sources?: WorkIqSource[];
}

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
})
export class HomeChat {
  private readonly api = inject(CompassApiService);
  private readonly auth = inject(AuthService);
  readonly lightbox = inject(LightboxService);

  // Inputs from the shell so we don't duplicate health fetching.
  readonly models = input<string[]>([]);
  readonly deployment = input<string>('');
  // The Home conversation to display: an id to resume, or null for a fresh
  // thread. Driven by the sidebar (App owns the Home conversation list).
  readonly activeSession = input<string | null>(null);
  // Work IQ (owned by the shell topbar) — ground replies in Azure AI Search.
  readonly workIq = input<boolean>(false);

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

  readonly draft = signal('');
  readonly messages = signal<ChatMsg[]>([]);
  readonly streaming = signal(false);
  readonly attachments = signal<UiAttachment[]>([]);
  readonly dragOver = signal(false);
  readonly attachError = signal('');
  private readonly fileInput = viewChild<ElementRef<HTMLInputElement>>('fileInput');
  private sessionId: string | null = null;
  private loadedId: string | null = null; // which session the view is showing
  private currentAssistant: ChatMsg | null = null;
  private smoother: SmoothText | null = null; // smooth token reveal
  private pendingSources: WorkIqSource[] | null = null; // Work IQ sources for the reply

  readonly ideas = [
    'Explain a tricky concept in simple terms',
    'Brainstorm names for a new project',
    'Draft a short message or email',
  ];

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

  formatElapsed(ms: number): string {
    const s = Math.floor(ms / 1000);
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  }

  private readonly logEl = viewChild<ElementRef<HTMLElement>>('chatlog');

  constructor() {
    effect(() => {
      this.messages();
      queueMicrotask(() => {
        const el = this.logEl()?.nativeElement;
        // Instant follow: with rAF-paced text the content grows every frame, so
        // a competing 'smooth' scroll animation would stutter — 'auto' tracks it.
        el?.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
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
        msgs.push({ id: m.uuid || crypto.randomUUID(), role: m.role, text, streaming: false });
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

  /** Time-aware greeting using the signed-in user's name. */
  readonly greeting = computed(() => {
    const h = new Date().getHours();
    const part = h < 12 ? 'Morning' : h < 18 ? 'Afternoon' : 'Evening';
    const name = this.auth.user()?.username || 'there';
    return `${part}, ${name}`;
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
    this.draft.set('');
    this.attachments.set([]);
    this.push({
      id: crypto.randomUUID(),
      role: 'user',
      text: content,
      streaming: false,
      atts: atts.length ? atts : undefined,
    });

    const payload = toWire(atts);

    this.turnStartMs = performance.now();
    this.elapsedMs.set(0);
    this.streaming.set(true);
    this.currentAssistant = null;
    try {
      if (!this.sessionId) {
        const res = await this.api.createChatSession({
          model: this.activeModel() || undefined,
          effort: this.activeEffort(),
        });
        this.sessionId = res.session_id;
        this.loadedId = res.session_id; // keep in sync so the input echo is a no-op
        this.sessionCreated.emit(res.session_id);
      }
      this.pendingSources = null;
      await this.api.streamChatMessage(
        this.sessionId,
        content,
        (ev) => this.onEvent(ev),
        payload,
        this.workIq(),
      );
    } catch (err) {
      this.push({
        id: crypto.randomUUID(),
        role: 'assistant',
        text: '⚠️ ' + (err instanceof Error ? err.message : 'Chat request failed.'),
        streaming: false,
      });
    } finally {
      // Read through a cast: the field is mutated inside the onEvent callback,
      // which TS's flow analysis can't see, so it would otherwise narrow to null.
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

  private onEvent(ev: CompassEvent): void {
    switch (ev.type) {
      case 'work_iq_sources':
        // Arrives before the answer streams — hold it for the reply bubble.
        this.pendingSources = (ev['sources'] as WorkIqSource[]) ?? null;
        break;
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
          // Reveal tokens smoothly (rAF-paced) instead of per-network-chunk.
          const id = this.currentAssistant.id;
          this.smoother = new SmoothText((t) => this.patch(id, (m) => ({ ...m, text: t })));
        }
        this.smoother?.push((ev['text'] as string) ?? '');
        break;
      }
      case 'assistant_message':
        if (this.currentAssistant) {
          const id = this.currentAssistant.id;
          this.smoother?.finish();
          this.smoother = null;
          this.patch(id, (m) => ({ ...m, streaming: false }));
          this.currentAssistant = null;
        }
        break;
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

  private push(m: ChatMsg): void {
    this.messages.update((list) => [...list, m]);
  }
  private patch(id: string, fn: (m: ChatMsg) => ChatMsg): void {
    this.messages.update((list) => list.map((m) => (m.id === id ? fn(m) : m)));
  }
}
