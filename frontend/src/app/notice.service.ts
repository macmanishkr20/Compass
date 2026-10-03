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
  }

  private push(partial: Omit<Notice, 'id'>): void {
    const notice: Notice = { ...partial, id: this.next++ };
    // Newest first, and capped: a burst of failures should not become a
    // column of toasts that covers the thing they are about.
    this.notices.update((list) => [notice, ...list].slice(0, 4));
    const ms = notice.tone === 'error' ? NoticeService.ERROR_MS : NoticeService.OK_MS;
    setTimeout(() => this.dismiss(notice.id), ms);
  }
}
