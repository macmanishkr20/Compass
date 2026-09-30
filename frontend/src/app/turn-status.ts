import { Signal, computed, signal } from '@angular/core';

/**
 * What the turn is doing, said in the turn's own terms.
 *
 * This replaces a `setInterval` that walked a list of phrases every 2.4s.
 * That line was always moving and never meant anything: it read "Checking the
 * edge cases…" while a shell command ran, and "Composing a response…" while
 * the model sat waiting for a permission nobody had granted. A status that is
 * decorative is worse than none, because it is the one part of the screen a
 * person watches to decide whether to keep waiting.
 *
 * So the phase comes from the stream. Compass already emits everything needed
 * to know it — thinking deltas, tool starts, the first text delta, a
 * permission request — and each of those is a different answer to "should I
 * still be waiting?". Where an event names the thing it is working on, the
 * status says so: "Running tests…" beats "Running tools…" and costs nothing,
 * because the tool call is already on the wire.
 *
 * Rotation survives in exactly one phase. While the model is thinking there
 * is genuinely no finer signal to report — no tool, no token, nothing but
 * elapsed time — so the words there vary to show the turn is alive. That is
 * the one place where a phrase is chosen rather than derived, and it is
 * honest: every one of them means the same thing, which is "still thinking".
 */

/** The phases a turn moves through, in the order they usually happen. */
export type TurnPhase =
  | 'starting'
  | 'thinking'
  | 'settling'
  | 'tools'
  | 'writing'
  | 'waiting'
  | 'compacting';

/** Said while the model is thinking and nothing finer is known. They all mean
 *  "still thinking"; the variety is to show the turn has not died, which is
 *  the only question a person is asking by the time they read it twice. */
const THINKING_LINES = [
  'Thinking…',
  'Working through it…',
  'Weighing the approaches…',
  'Considering the edge cases…',
  'Still thinking…',
];

/** A verb per tool, so the status names the work rather than the category.
 *  A tool missing from here falls back to its own name, which is why this can
 *  stay short and why a new tool needs no change to be reported sensibly. */
const TOOL_VERBS: Record<string, string> = {
  bash: 'Running a command',
  bash_output: 'Reading command output',
  file_read: 'Reading files',
  file_write: 'Writing a file',
  file_edit: 'Editing a file',
  glob: 'Looking for files',
  grep: 'Searching the code',
  web_fetch: 'Reading a page',
  browser: 'Driving the browser',
  screenshot: 'Taking a screenshot',
  memory: 'Checking memory',
  todo_write: 'Updating the plan',
  consult: 'Consulting a second model',
  agent: 'Running a subagent',
  find_tools: 'Looking for a tool',
  ask_user: 'Waiting for your answer',
};

export class TurnStatus {
  private readonly phase = signal<TurnPhase>('starting');
  /** The tool in flight, when one is. */
  private readonly tool = signal('');
  /** Advanced by the host's existing tick while `phase` is `thinking`. */
  private readonly beat = signal(0);

  readonly label: Signal<string> = computed(() => {
    switch (this.phase()) {
      case 'starting':
        return 'Getting started…';
      case 'thinking':
        return THINKING_LINES[this.beat() % THINKING_LINES.length];
      case 'settling':
        // Thinking has ended and no answer has started. Worth its own phase:
        // it is the longest silence in a reasoning turn, and the one most
        // likely to be read as a hang.
        return 'Almost done thinking…';
      case 'tools': {
        const name = this.tool();
        if (!name) return 'Running tools…';
        return `${TOOL_VERBS[name] ?? `Running ${name}`}…`;
      }
      case 'writing':
        return 'Writing the answer…';
      case 'waiting':
        return 'Waiting for you…';
      case 'compacting':
        return 'Compacting the conversation…';
    }
  });

  /** Enter the thinking phase, restarting its vocabulary if we were not
   *  already in it. Each stretch of thinking then reads from the top rather
   *  than resuming wherever the last one was interrupted — the phrases are a
   *  liveness signal, and one that starts mid-list looks like a continuation
   *  of work that in fact just resumed. */
  private toThinking(): void {
    if (this.phase() !== 'thinking') {
      this.beat.set(0);
      this.phase.set('thinking');
    }
  }

  /** A new turn. */
  start(): void {
    this.phase.set('starting');
    this.tool.set('');
    this.beat.set(0);
  }

  /** One tick of the host's existing timer. Only the thinking phase has
   *  anything to say on a clock; every other phase is driven by events, and
   *  moving their words on a timer is exactly the lie this replaces. */
  tick(): void {
    if (this.phase() === 'thinking') this.beat.update((n) => n + 1);
  }

  /**
   * Fold one stream event into the phase.
   *
   * Called from the surfaces' existing event switches, which already see
   * every event; the names are the wire names from
   * `compass/common/models/events.py`.
   */
  note(type: string, event?: { tool_name?: unknown; tool?: unknown }): void {
    switch (type) {
      case 'stream_request_start':
        // A second request inside one turn — the model went back to work
        // after a tool answered it. Not a new turn, so the meters stay.
        this.tool.set('');
        this.toThinking();
        break;
      case 'thinking_delta':
        this.toThinking();
        break;
      case 'thinking_complete':
        this.phase.set('settling');
        break;
      case 'tool_arguments':
      case 'tool_call_started':
      case 'tool_progress': {
        const name = typeof event?.tool_name === 'string' ? event.tool_name : '';
        if (name) this.tool.set(name);
        this.phase.set('tools');
        break;
      }
      case 'tool_result':
        // The tool answered; the model has it and is thinking again. Said as
        // thinking rather than left on the tool, or a slow turn reads as
        // still running something that finished.
        this.tool.set('');
        this.toThinking();
        break;
      // `server_tool_used` is deliberately absent. It reports a web search
      // Azure already finished — it "never comes back to the loop", so it
      // arrives in the past tense, and in a measured turn it landed *after*
      // the answer had started streaming. Driving a live status from it puts
      // "Searching the web…" on screen while the answer is being written,
      // which is the decorative-status problem this class exists to remove.
      // A hosted search is genuinely silent while it runs, so the honest
      // reading during one is the thinking phase: busy, nothing finer known.
      case 'text_delta':
        // The answer has started. A tool call after this legitimately takes
        // the line back — an agentic turn writes, runs something, writes on —
        // but nothing that merely reports finished work does.
        this.phase.set('writing');
        break;
      case 'permission_request':
      case 'question_asked':
        this.phase.set('waiting');
        break;
      case 'permission_resolved':
      case 'question_answered':
        this.toThinking();
        break;
      case 'compaction':
        this.phase.set('compacting');
        break;
      default:
        break;
    }
  }
}
