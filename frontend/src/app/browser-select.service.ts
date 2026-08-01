import { Injectable, signal } from '@angular/core';

/** An element picked from the Compass browser's Select tool. `label` is the
 *  compact chip text (e.g. `<h1 class="home-greeting">`); `detail` is the full
 *  context block (opening tag, selector, size, text, CSS) attached to the
 *  prompt when the message is sent — like claude.ai's element references. */
export interface PickedElement {
  id: string;
  label: string;
  detail: string;
}

@Injectable({ providedIn: 'root' })
export class BrowserSelectService {
  readonly picks = signal<PickedElement[]>([]);

  add(p: PickedElement): void {
    this.picks.update((list) =>
      list.some((x) => x.detail === p.detail) ? list : [...list, p],
    );
  }
  remove(id: string): void {
    this.picks.update((list) => list.filter((x) => x.id !== id));
  }
  clear(): void {
    this.picks.set([]);
  }
}
