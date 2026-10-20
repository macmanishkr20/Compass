import { Injectable, signal } from '@angular/core';

export interface Notice {
  id: number;
  text: string;
  tone: 'ok' | 'error';
  /** Offered on a failure that can be tried again. */
  retryLabel?: string;
  retry?: () => void;
}

/**
 * Short confirmations of things that have finished.
 *
 * Deleting used to be silent in both directions: the row vanished and
 * nothing said whether the server had agreed, so a delete that failed looked
 * exactly like one that worked until the next refresh brought the row back.
 * This is the other half of that — the row goes at once, and a moment later
 * this says whether it really went.
 *
 * Deliberately not `TurnNotifyService`, which answers a different question:
 * that one fires only when you are *not* looking at the section, because a
 * turn finishing in front of you needs no announcement. This fires precisely
 * when you are looking, because you just asked for something.
 */
@Injectable({ providedIn: 'root' })
export class NoticeService {
  readonly notices = signal<Notice[]>([]);
  private next = 1;

  /** How long a notice stays. A failure stays longer: it carries something
   *  to do about it, and four seconds is not long enough to decide. */
  private static readonly OK_MS = 3200;
  private static readonly ERROR_MS = 7000;

  ok(text: string): void {
    this.push({ text, tone: 'ok' });
  }

  error(text: string, retry?: { label: string; run: () => void }): void {
    this.push({
      text,
      tone: 'error',
      retryLabel: retry?.label,
      retry: retry?.run,
    });
  }

  dismiss(id: number): void {
    this.notices.update((list) => list.filter((n) => n.id !== id));
    const timer = this.timers.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.timers.delete(id);
    }
  }

  /** The timer that will retire each live notice, so a repeat can restart
   *  one rather than leaving the first copy to expire on its own clock. */
  private readonly timers = new Map<number, ReturnType<typeof setTimeout>>();

  private push(partial: Omit<Notice, 'id'>): void {
    // THE SAME COMPLAINT TWICE IS ONE COMPLAINT. Pressing a button that
    // refuses four times produced four identical toasts and filled the
    // cap with copies of one sentence — which reads as four things going
    // wrong, and pushes anything else out. A repeat moves the notice it
    // already is back to the front and restarts its clock instead.
    const same = this.notices().find(
      (n) => n.text === partial.text && n.tone === partial.tone,
    );
    if (same) {
      this.notices.update((list) => [same, ...list.filter((n) => n !== same)]);
      this.hold(same);
      return;
    }
    const notice: Notice = { ...partial, id: this.next++ };
    // Newest first, and capped: a burst of failures should not become a
    // column of toasts that covers the thing they are about.
    this.notices.update((list) => [notice, ...list].slice(0, 4));
    this.hold(notice);
  }

  /** Start, or restart, the clock that retires this notice. */
  private hold(notice: Notice): void {
    const running = this.timers.get(notice.id);
    if (running !== undefined) clearTimeout(running);
    const ms = notice.tone === 'error' ? NoticeService.ERROR_MS : NoticeService.OK_MS;
    this.timers.set(notice.id, setTimeout(() => this.dismiss(notice.id), ms));
  }
}
