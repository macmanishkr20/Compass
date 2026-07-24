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

interface ChatMsg {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  streaming: boolean;
  atts?: UiAttachment[];
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

  // Inputs from the shell so we don't duplicate health fetching.
  readonly models = input<string[]>([]);
  readonly deployment = input<string>('');

  // Fired when the user flips the composer toggle to "Agent" — the shell
  // switches to the Code (Agent Console) section.
  readonly switchToAgent = output<void>();

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
  private currentAssistant: ChatMsg | null = null;

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

  private readonly logEl = viewChild<ElementRef<HTMLElement>>('chatlog');

  constructor() {
    effect(() => {
      this.messages();
      queueMicrotask(() => {
        const el = this.logEl()?.nativeElement;
        el?.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
      });
    });
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

    this.streaming.set(true);
    this.currentAssistant = null;
    try {
      if (!this.sessionId) {
        const res = await this.api.createChatSession({
          model: this.activeModel() || undefined,
          effort: this.activeEffort(),
        });
        this.sessionId = res.session_id;
      }
      await this.api.streamChatMessage(
        this.sessionId,
        content,
        (ev) => this.onEvent(ev),
        payload,
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
        this.patch(pending.id, (m) => ({ ...m, streaming: false }));
        this.currentAssistant = null;
      }
      this.streaming.set(false);
    }
  }

  async abort(): Promise<void> {
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
      case 'text_delta': {
        if (!this.currentAssistant) {
          this.currentAssistant = {
            id: crypto.randomUUID(),
            role: 'assistant',
            text: '',
            streaming: true,
          };
          this.push(this.currentAssistant);
        }
        const delta = (ev['text'] as string) ?? '';
        this.patch(this.currentAssistant.id, (m) => ({ ...m, text: m.text + delta }));
        break;
      }
      case 'assistant_message':
        if (this.currentAssistant) {
          this.patch(this.currentAssistant.id, (m) => ({ ...m, streaming: false }));
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
