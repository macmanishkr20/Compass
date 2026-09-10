import { Injectable, signal } from '@angular/core';

/** The five sections a prompt can be running in. */
export type ModuleKey = 'home' | 'code' | 'design' | 'pipelines' | 'estimate';

export interface TurnToast {
  section: ModuleKey;
  title: string;
  body: string;
  ok: boolean;
}

const LABEL: Record<ModuleKey, string> = {
  home: 'Home',
  code: 'Code',
  design: 'Design',
  pipelines: 'Pipelines',
  estimate: 'Estimate',
};

const SOUND_KEY = 'compass.notifySound';

/**
 * Tells you a turn finished in a section you were not watching.
 *
 * A prompt in any of the five sections can run for minutes, and the reason
 * people leave is that they have something else to do. Coming back to find it
 * finished four minutes ago is the whole cost of not being told — so the rule
 * is narrow: say something only when the person could not already see it.
 *
 * "Could not see it" is two separate conditions and both have to be checked.
 * Being in a different section is the obvious one. The other is that the
 * window itself is not on screen: a person watching Code in a background tab,
 * or with the whole app behind their editor, is no better off than one who
 * switched sections. When they *are* looking at the running section in a
 * visible window, nothing fires — a toast about something already on screen is
 * noise, and noise is what makes people turn notifications off.
 *
 * The delivery is the one Routines already uses: an in-app toast, which always
 * works, and a native OS notification on top when the browser has been given
 * permission. The native one is what carries to Windows and macOS while the
 * app is behind something else, which is exactly the case worth carrying.
 */
@Injectable({ providedIn: 'root' })
export class TurnNotifyService {
  /** Which section is on screen. The shell keeps this in step. */
  readonly activeSection = signal<ModuleKey>('home');

  /** The current toast, or null. Rendered by the shell. */
  readonly toast = signal<TurnToast | null>(null);

  /** Whether the arrival click plays. Remembered across reloads. */
  readonly soundOn = signal(this.readSoundPref());

  private timer: ReturnType<typeof setTimeout> | null = null;
  private audio: AudioContext | null = null;

  private readSoundPref(): boolean {
    try {
      return localStorage.getItem(SOUND_KEY) !== '0';
    } catch {
      return true; // storage blocked (private window, embedded) — default on
    }
  }

  setSound(on: boolean): void {
    this.soundOn.set(on);
    try {
      localStorage.setItem(SOUND_KEY, on ? '1' : '0');
    } catch {
      /* not persisting is survivable; the toggle still holds for this session */
    }
    if (on) this.click(); // so the choice is audible at the moment it is made
  }

  /**
   * Called when a turn starts. Two things have to happen off a user gesture
   * and this is the only one there is: asking for OS notification permission,
   * and opening the audio context. Browsers refuse both from a timer, which is
   * where the notification itself is fired from.
   */
  arm(): void {
    try {
      if ('Notification' in window && Notification.permission === 'default') {
        void Notification.requestPermission();
      }
    } catch {
      /* browser without notification support — the toast still works */
    }
    this.primeAudio();
  }

  private primeAudio(): void {
    try {
      const Ctor =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext;
      if (!Ctor) return;
      this.audio ??= new Ctor();
      if (this.audio.state === 'suspended') void this.audio.resume();
    } catch {
      /* no audio available — everything else still works */
    }
  }

  /**
   * A short click. Synthesised rather than shipped as a file: it is two
   * oscillators and an envelope, which is smaller than any asset would be and
   * saves a network fetch at the moment something is trying to get attention.
   *
   * Deliberately quiet and brief — 60ms, peaking at 0.06 gain. A notification
   * sound is heard dozens of times a day by someone who left the room, and the
   * ones people disable are the ones that announce themselves.
   */
  private click(): void {
    if (!this.soundOn()) return;
    this.primeAudio();
    const ctx = this.audio;
    if (!ctx || ctx.state !== 'running') return;
    try {
      const now = ctx.currentTime;
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.06, now + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.06);
      gain.connect(ctx.destination);
      // Two tones a fifth apart read as a "tick" rather than a beep.
      for (const [freq, when] of [[1320, 0], [1980, 0.012]] as const) {
        const osc = ctx.createOscillator();
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(freq, now + when);
        osc.connect(gain);
        osc.start(now + when);
        osc.stop(now + 0.07);
      }
    } catch {
      /* an audio failure must never stop the notification itself */
    }
  }

  /** True when the person can already see the section that just finished. */
  private watching(section: ModuleKey): boolean {
    const visible =
      typeof document === 'undefined' || document.visibilityState === 'visible';
    return visible && this.activeSection() === section;
  }

  /**
   * A turn finished. `summary` is a line of what happened — the first words of
   * the answer, or what was produced — because "Code finished" tells you only
   * that you have to go and look.
   */
  finished(section: ModuleKey, summary: string, ok = true): void {
    if (this.watching(section)) return;
    const title = `${LABEL[section]} ${ok ? 'finished' : 'stopped'}`;
    const body = (summary || '').replace(/\s+/g, ' ').trim().slice(0, 140);
    this.show({ section, title, body, ok });
  }

  private show(t: TurnToast): void {
    this.toast.set(t);
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.toast.set(null), 8000);
    this.click();

    try {
      if ('Notification' in window && Notification.permission === 'granted') {
        // One tag per section, so a second finish in the same section replaces
        // the first rather than stacking another card on somebody's desktop.
        const n = new Notification(t.title, {
          body: t.body,
          tag: `compass-turn-${t.section}`,
        });
        n.onclick = () => {
          try {
            window.focus();
          } catch {
            /* focus can be refused; the toast is still there */
          }
          n.close();
        };
      }
    } catch {
      /* unsupported, or blocked — the in-app toast has already been set */
    }
  }

  dismiss(): void {
    if (this.timer) clearTimeout(this.timer);
    this.toast.set(null);
  }

  label(section: ModuleKey): string {
    return LABEL[section];
  }
}
