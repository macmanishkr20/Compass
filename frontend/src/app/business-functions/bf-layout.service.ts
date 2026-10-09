import { Injectable, signal } from '@angular/core';

/**
 * Which of the three columns are showing.
 *
 * A service rather than component state because the buttons that collapse
 * them live in the shell's top bar and the panels live inside the section —
 * two components that never meet. The same reason `MissionActivityService`
 * exists: one small piece of UI state, read from both sides.
 *
 * Both default open. Collapsing is something a person does to get room for
 * the thing in the middle, and a section that opened already folded up would
 * be hiding its own features on first sight.
 */
@Injectable({ providedIn: 'root' })
export class BusinessFunctionsLayout {
  /** The function switcher and its feature list. */
  readonly sideOpen = signal(true);
  /** The assistant. */
  readonly railOpen = signal(true);

  toggleSide(): void {
    this.sideOpen.update((v) => !v);
  }

  toggleRail(): void {
    this.railOpen.update((v) => !v);
  }
}
