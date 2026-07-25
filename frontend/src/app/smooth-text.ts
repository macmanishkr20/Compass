/**
 * Smooth streaming text — the technique claude.ai uses to make token streams
 * flow instead of jumping. Network deltas arrive in irregular bursts; rendering
 * each one immediately looks chunky. Instead we buffer the received text and
 * reveal it on a requestAnimationFrame loop at a steady, self-tuning pace:
 * roughly a fixed fraction of the remaining backlog per frame, so it eases in
 * (fast when far behind, gentle as it catches up) and never stutters.
 *
 * Robustness: rAF is paused in hidden/unfocused tabs, which would otherwise
 * leave text unrendered. A setInterval watchdog detects a stalled rAF and
 * flushes the full text so the content is ALWAYS correct, even off-screen —
 * smoothness is best-effort, correctness is guaranteed.
 *
 * Usage: one instance per streaming message. push() every delta; finish() when
 * the network stream ends; cancel() to stop immediately (abort).
 */
export class SmoothText {
  private target = '';
  private shown = 0;
  private raf = 0;
  private ended = false;
  private watchdog: ReturnType<typeof setInterval> | 0 = 0;
  private lastTickAt = 0;

  constructor(
    private readonly render: (text: string) => void,
    private readonly minStep = 2,
    private readonly divisor = 6,
  ) {}

  /** Append newly-received text and make sure the reveal loop is running. */
  push(delta: string): void {
    if (!delta) return;
    this.target += delta;
    this.start();
  }

  /** The network stream ended — drain the remaining buffer, then stop. */
  finish(): void {
    this.ended = true;
    this.start();
  }

  /** Stop immediately (e.g. the user aborted). Leaves whatever is shown. */
  cancel(): void {
    this.ended = true;
    this.stop();
  }

  /** Full received text so far (not the animated slice). */
  get fullText(): string {
    return this.target;
  }

  private start(): void {
    if (!this.raf) this.raf = requestAnimationFrame(this.tick);
    if (!this.watchdog) {
      this.lastTickAt = performance.now();
      this.watchdog = setInterval(this.guard, 250);
    }
  }

  private stop(): void {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    if (this.watchdog) clearInterval(this.watchdog);
    this.watchdog = 0;
  }

  private readonly tick = (): void => {
    this.raf = 0;
    this.lastTickAt = performance.now();
    const remaining = this.target.length - this.shown;
    if (remaining > 0) {
      const step = Math.max(this.minStep, Math.ceil(remaining / this.divisor));
      this.shown = Math.min(this.target.length, this.shown + step);
      this.render(this.target.slice(0, this.shown));
    }
    if (this.shown < this.target.length) {
      this.raf = requestAnimationFrame(this.tick); // more to reveal
    } else if (this.ended) {
      this.render(this.target); // guarantee the exact final text
      this.stop();
    }
    // else: caught up, stream still open — idle; a future push() restarts us.
  };

  /** Fires even when rAF is paused. If text is pending but rAF hasn't advanced
   *  recently, the tab is hidden/throttled — flush the full text so it's never
   *  stuck empty off-screen. */
  private readonly guard = (): void => {
    if (this.shown >= this.target.length) {
      if (this.ended) this.stop();
      return;
    }
    if (performance.now() - this.lastTickAt > 240) {
      this.shown = this.target.length;
      this.render(this.target);
      this.lastTickAt = performance.now();
      if (this.ended) this.stop();
    }
  };
}
