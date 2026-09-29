import { Injectable, computed, signal } from '@angular/core';

/** One line in the live feed: when, what kind of thing happened, and what it
 *  said. `kind` is what gets coloured, so it is a small closed set rather
 *  than free text. */
export interface ActivityLine {
  at: string;
  kind: ActivityKind;
  text: string;
}

export type ActivityKind =
  | 'session_start'
  | 'mission_verdict'
  | 'feature_accepted'
  | 'budget_tick'
  | 'tool_call'
  | 'error';

/** How many lines to keep. A mission runs for hours and produces more events
 *  than anyone scrolls; the feed is a window on the present, and `PROGRESS.md`
 *  is the record. */
const KEEP = 60;

/**
 * The live activity feed, shared between the nav's pulse button and the
 * Missions screen.
 *
 * It is a service rather than component state because the two ends sit in
 * different components: the button that opens the feed lives in the shell's
 * nav, and the feed itself belongs to Missions. Passing a signal through a
 * service is the smaller of the two couplings — the alternative was the shell
 * reaching into the Missions component through a view query, which makes the
 * nav depend on a screen that may not be mounted.
 *
 * Every line here comes from an event the server actually sent. Nothing is
 * synthesised to make the feed look busy: a quiet mission has a quiet feed,
 * and that is information.
 */
@Injectable({ providedIn: 'root' })
export class MissionActivityService {
  readonly lines = signal<ActivityLine[]>([]);
  readonly open = signal(false);
  /** Whether a mission is running, which is what makes the pulse pulse. */
  readonly live = signal(false);
  /** Lines added since the feed was last looked at. */
  readonly unseen = signal(0);

  readonly hasNews = computed(() => this.live() || this.unseen() > 0);

  push(kind: ActivityKind, text: string): void {
    const clean = (text || '').replace(/\s+/g, ' ').trim();
    if (!clean) return;
    const at = new Date().toLocaleTimeString('en-GB', { hour12: false });
    this.lines.update((l) => [{ at, kind, text: clean }, ...l].slice(0, KEEP));
    if (!this.open()) this.unseen.update((n) => n + 1);
  }

  toggle(): void {
    this.open.update((o) => !o);
    if (this.open()) this.unseen.set(0);
  }

  close(): void {
    this.open.set(false);
  }

  clear(): void {
    this.lines.set([]);
    this.unseen.set(0);
  }
}
