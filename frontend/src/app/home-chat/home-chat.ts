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
  at?: number; // epoch ms — shown as a relative age under the message
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
  onScroll(): void {
    const el = this.logEl()?.nativeElement;
    // Re-attach only at the very bottom; detaching is driven by wheel/touch.
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 24) this.stickBottom = true;
  }
  /** Any upward wheel/touch intent detaches auto-follow at once — a distance
   *  threshold cannot win a race against streaming tokens. */
  onWheel(ev: WheelEvent): void {
    if (ev.deltaY < 0) this.stickBottom = false;
  }
  onTouch(): void {
    this.stickBottom = false;
  }

  constructor() {
    setInterval(() => this.nowTick.set(Date.now()), 30_000);
    effect(() => {
      this.messages();
      queueMicrotask(() => {
        const el = this.logEl()?.nativeElement;
        // Instant follow: with rAF-paced text the content grows every frame, so
        // a competing 'smooth' scroll animation would stutter — 'auto' tracks it.
        // Only while the user is parked at the bottom; if they scrolled up to
        // read, don't yank them back down (claude.ai behaviour).
        if (el && this.stickBottom) el.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
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
