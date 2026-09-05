import { Directive, ElementRef, afterNextRender, inject } from '@angular/core';

/**
 * Keeps a dropdown inside the window.
 *
 * The menus here were capped with `max-height: 68vh`, which measures the
 * window and ignores where the menu starts. The design-system picker opens
 * from the composer's footer at roughly y=400, so two thirds of the viewport
 * put its bottom edge 120px below the fold on a 900px window and 300px below
 * on a short one. A fraction of the viewport is only the right cap for
 * something anchored at the top of it.
 *
 * The room below the trigger is the actual constraint, and it can only be
 * measured, so it is measured — once, as the menu renders. Menus are created
 * by `@if` when they open, so this runs on every open with the position the
 * menu actually has rather than the one it had last time.
 *
 * Selected by class rather than by an attribute on each element: there are
 * several of these menus, and one that was missed would be the one that
 * overruns.
 */
@Directive({
  selector: '.dz-menu, .dz-picker, .dz-attach',
  standalone: true,
})
export class FitMenuDirective {
  /** Never squeeze a menu below this: at some point a scrolling sliver is
   *  worse than one that hangs off the edge, and the caller should have
   *  opened it somewhere else. */
  private static readonly FLOOR = 200;
  /** Breathing room, so the last row does not sit flush on the window edge. */
  private static readonly MARGIN = 16;

  private readonly host = inject(ElementRef<HTMLElement>);

  constructor() {
    afterNextRender(() => this.fit());
  }

  private fit(): void {
    const node = this.host.nativeElement as HTMLElement;
    const top = node.getBoundingClientRect().top;
    const room = window.innerHeight - top - FitMenuDirective.MARGIN;
    node.style.maxHeight = `${Math.max(FitMenuDirective.FLOOR, Math.round(room))}px`;
    // Only meaningful with a scroller; the stylesheet sets one, and saying so
    // here as well means a menu without one still behaves when this runs.
    node.style.overflowY = 'auto';
  }
}
