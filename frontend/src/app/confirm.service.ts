import { Injectable, signal } from '@angular/core';

/** What the dialog says, and what the answer means. */
export interface ConfirmRequest {
  /** The question, as a short sentence: "Delete this conversation?" */
  title: string;
  /** What will actually happen. Name what goes with it — somebody agreeing
   *  to delete a pipeline may not know its run history goes too. */
  body?: string;
  /** The thing's own name, shown so there is no doubt which one this is. */
  subject?: string;
  /** The affirmative button. "Delete", not "OK": a button that names the
   *  action is one you cannot press by reflex and then misremember. */
  confirmLabel?: string;
  cancelLabel?: string;
  /** Red for destructive, which is nearly everything that comes through here. */
  danger?: boolean;
}

/**
 * One confirmation dialog for the whole app.
 *
 * Deleting was immediate everywhere and asked nothing: a misplaced click took
 * a conversation, a design or a pipeline with no way back. Design did ask,
 * through `window.confirm` — which blocks the event loop, cannot be styled,
 * says "localhost:4200 says", and on a long body is unreadable.
 *
 * This is a promise: `if (!(await confirm.ask({...}))) return;` reads like
 * the native call it replaces, so a call site is one line and there is no
 * excuse for a delete that skips it.
 */
@Injectable({ providedIn: 'root' })
export class ConfirmService {
  /** The open request, or null. Rendered by the shell. */
  readonly open = signal<ConfirmRequest | null>(null);
  private settle: ((ok: boolean) => void) | null = null;

  ask(request: ConfirmRequest): Promise<boolean> {
    // A second question while one is open would leave the first unanswered
    // for ever, and its caller waiting on a promise nothing will settle.
    this.settle?.(false);
    this.open.set({
      confirmLabel: 'Delete',
      cancelLabel: 'Cancel',
      danger: true,
      ...request,
    });
    return new Promise<boolean>((resolve) => {
      this.settle = resolve;
    });
  }

  answer(ok: boolean): void {
    const settle = this.settle;
    this.settle = null;
    this.open.set(null);
    settle?.(ok);
  }
}
