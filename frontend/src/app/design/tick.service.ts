import { Injectable, signal } from '@angular/core';

/**
 * The compass rose's detent click.
 *
 * Synthesised rather than played from a file. A tick is two hundred bytes of
 * envelope maths, and generating it buys three things a sample would not: it
 * costs no request, it cannot be caught mid-play by the next one, and its
 * pitch can follow the bearing — the ring rises a little as it turns, which is
 * what makes a row of identical clicks read as a dial rather than a keyboard.
 *
 * Two things this has to be careful about, both of them about not being
 * obnoxious.
 *
 * *It cannot start itself.* Browsers refuse an AudioContext until the page has
 * been interacted with, and rightly — a page that makes noise before you have
 * touched it is a page nobody trusts. The context is created on the first tick
 * and resumed if it was suspended; until a real gesture has happened the
 * ticks are simply dropped, silently, rather than queued up to fire all at
 * once later.
 *
 * *It has to be possible to turn off.* The preference is remembered, because
 * being asked to silence the same thing twice is worse than the sound.
 */
@Injectable({ providedIn: 'root' })
export class TickSound {
  private static readonly KEY = 'compass.design.tick';

  /** Whether the rose ticks. Read by the toggle in the rose header. */
  readonly enabled = signal(this.remembered());

  private context: AudioContext | null = null;
  /** The last tick's time, so a fast sweep across the ring cannot stack forty
   *  overlapping clicks into a buzz. */
  private lastAt = 0;

  /** How close two ticks may be. Below this the ear hears a tone, not ticks. */
  private static readonly GAP_MS = 28;

  toggle(): void {
    const next = !this.enabled();
    this.enabled.set(next);
    try {
      localStorage.setItem(TickSound.KEY, next ? '1' : '0');
    } catch {
      // A browser refusing storage is not a reason to refuse the sound.
    }
    // Tick on the way on, so turning it back on demonstrates itself.
    if (next) this.play(0);
  }

  /**
   * One detent. `step` is the tile's place on the ring, which nudges the
   * pitch so turning the dial reads as motion rather than repetition.
   */
  play(step = 0): void {
    if (!this.enabled()) return;
    const now = Date.now();
    if (now - this.lastAt < TickSound.GAP_MS) return;
    this.lastAt = now;

    const ctx = this.audio();
    if (!ctx || ctx.state !== 'running') return;

    const at = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    // A triangle rather than a sine: a sine at this length is a thud, and a
    // square is a spike. The band-pass takes the edge off what is left.
    osc.type = 'triangle';
    osc.frequency.setValueAtTime(1650 + (step % 16) * 26, at);

    const band = ctx.createBiquadFilter();
    band.type = 'bandpass';
    band.frequency.value = 2100;
    band.Q.value = 1.1;

    // 11ms, and most of that is the decay. Any longer stops being a tick and
    // starts being a note, which is tiring after the fourth one.
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.045, at + 0.001);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.011);

    osc.connect(band).connect(gain).connect(ctx.destination);
    osc.start(at);
    osc.stop(at + 0.014);
  }

  /** A firmer, lower version for the moment a template is actually chosen —
   *  the detent landing rather than passing over. */
  clunk(): void {
    if (!this.enabled()) return;
    this.lastAt = 0; // a selection is never the click to drop
    const ctx = this.audio();
    if (!ctx || ctx.state !== 'running') return;

    const at = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'triangle';
    osc.frequency.setValueAtTime(880, at);
    osc.frequency.exponentialRampToValueAtTime(620, at + 0.03);
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.07, at + 0.002);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.045);
    osc.connect(gain).connect(ctx.destination);
    osc.start(at);
    osc.stop(at + 0.05);
  }

  private audio(): AudioContext | null {
    if (typeof window === 'undefined') return null;
    if (!this.context) {
      const Ctor = window.AudioContext
        ?? (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext;
      if (!Ctor) return null;
      try {
        this.context = new Ctor();
      } catch {
        return null;
      }
    }
    // Suspended until the page has been interacted with. Asking politely each
    // time costs nothing and means the first tick after a click works.
    if (this.context.state === 'suspended') {
      void this.context.resume().catch(() => undefined);
    }
    return this.context;
  }

  private remembered(): boolean {
    try {
      return localStorage.getItem(TickSound.KEY) !== '0';
    } catch {
      return true;
    }
  }
}
