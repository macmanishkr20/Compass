import { Directive, ElementRef, inject, input, output, OnDestroy, signal } from '@angular/core';

/**
 * Drag a list into the order you want it in.
 *
 * Put it on the container; mark the rows with `data-drag`. It reports
 * `(reordered)` as a from/to pair and touches nothing else — the list is the
 * caller's, so the caller moves the item and decides whether that outlives
 * the session. Rows are found live rather than through a query, because the
 * lists it serves are `@for` blocks whose rows come and go.
 *
 * Three things it has to get right, all of which are the reason this exists
 * rather than a `draggable` attribute:
 *
 *  - A drag must not swallow a click. Every row here is a button that sends
 *    a prompt, so a press only becomes a drag after the pointer has actually
 *    travelled (`SLOP`), and a press that never travels stays a click.
 *  - A long list has to scroll while you hold an item over its edge, or the
 *    only reachable positions are the ones already on screen.
 *  - All of the measuring happens in the scroll container's own coordinates.
 *    Viewport rectangles go stale the moment the list auto-scrolls underneath
 *    the pointer, and the arithmetic that decides where the row lands would
 *    then drift by exactly the distance scrolled.
 *
 * Touch holds still rather than moving: a finger that travels is scrolling
 * the list, which is why rows keep `touch-action: pan-y` and a drag starts
 * from a press that stayed put (`HOLD_MS`).
 */
@Directive({
  selector: '[appReorder]',
  host: {
    '(pointerdown)': 'onDown($event)',
    '(keydown)': 'onKey($event)',
    '[class.reordering]': 'dragging()',
  },
})
export class Reorder implements OnDestroy {
  /** What counts as a row. */
  readonly rowSelector = input('[data-drag]', { alias: 'appReorder' });

  /** Where it went, once it has landed. Indices are into the rows as
   *  rendered, so they match the array the template drew them from. */
  readonly reordered = output<{ from: number; to: number }>();

  readonly dragging = signal(false);

  private readonly host = inject(ElementRef<HTMLElement>).nativeElement as HTMLElement;

  /** A finished drag owes the row a click it must not deliver.
   *
   *  The row travels with the pointer, so a drag presses and releases over
   *  the same element and the browser calls that a click — which on these
   *  lists means the prompt gets sent. Waiting to see whether the pointer
   *  moved is not enough, because by then the click is already on its way.
   *  So the next one is swallowed outright, in the capture phase, before it
   *  reaches the button that would act on it. Cleared on a timer as well as
   *  by the click itself: a drag released outside the list produces no
   *  click at all, and a flag left standing would eat a real one later. */
  private swallowClick = false;
  private swallowTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.host.addEventListener('click', this.onClick, true);
  }

  /** How far a pointer travels before a press is a drag and not a click. */
  private static readonly SLOP = 5;
  /** How long a finger stays put before a press is a drag and not a scroll. */
  private static readonly HOLD_MS = 320;
  /** How close to the edge starts an auto-scroll, and how fast at the very
   *  edge. Proportional in between, so easing up slows it down. */
  private static readonly EDGE = 56;
  private static readonly MAX_SPEED = 18;

  private drag: {
    pointerId: number;
    rows: HTMLElement[];
    from: number;
    /** Row geometry in scroller coordinates, which auto-scrolling cannot
     *  invalidate. */
    tops: number[];
    heights: number[];
    /** How far each displaced row steps aside: the dragged row's own height
     *  plus whatever space separates two rows. */
    step: number;
    startY: number;      // pointer, in scroller coordinates
    scroller: HTMLElement | null;
    /** How far the list could scroll before anything was picked up.
     *
     *  The lifted row is taken out of the flow only as far as the eye is
     *  concerned: it keeps its place in the box and its transform pushes the
     *  scrollable area out with it, so a list held at the bottom edge will
     *  happily scroll on into the blank space the drag itself created. The
     *  honest limit is the one measured before any of that. */
    maxScroll: number;
    to: number;
    /** Set once the press has earned the drag; until then this is a click
     *  that has not finished happening. */
    armed: boolean;
    startClientY: number;
    hold: ReturnType<typeof setTimeout> | null;
    frame: number;
    pointerClientY: number;
  } | null = null;

  ngOnDestroy(): void {
    this.cancel();
    if (this.swallowTimer) clearTimeout(this.swallowTimer);
    this.host.removeEventListener('click', this.onClick, true);
  }

  // ── starting ────────────────────────────────────────────────────────────
  onDown(ev: PointerEvent): void {
    if (ev.button !== 0 || this.drag) return;
    const rows = this.rows();
    const row = (ev.target as HTMLElement | null)?.closest<HTMLElement>(this.rowSelector());
    const from = row ? rows.indexOf(row) : -1;
    if (from < 0 || rows.length < 2) return;

    const scroller = this.scrollerOf(rows[0]);
    const base = this.baseOf(scroller);
    const tops = rows.map((r) => r.getBoundingClientRect().top - base);
    const heights = rows.map((r) => r.getBoundingClientRect().height);
    // The space between two rows, read off the rendered list rather than
    // assumed: the Home cards are separated by a margin and the library rows
    // sit flush against each other.
    const gap = rows.length > 1 ? Math.max(0, tops[1] - (tops[0] + heights[0])) : 0;

    this.drag = {
      pointerId: ev.pointerId,
      rows, from, tops, heights,
      step: heights[from] + gap,
      startY: ev.clientY - base,
      scroller,
      maxScroll: scroller ? Math.max(0, scroller.scrollHeight - scroller.clientHeight) : 0,
      to: from,
      armed: false,
      startClientY: ev.clientY,
      hold: null,
      frame: 0,
      pointerClientY: ev.clientY,
    };

    // A finger is asking to scroll until it has stayed put long enough to be
    // asking for something else.
    if (ev.pointerType === 'touch') {
      this.drag.hold = setTimeout(() => this.arm(ev.pointerId), Reorder.HOLD_MS);
    }

    window.addEventListener('pointermove', this.onMove, { passive: false });
    window.addEventListener('pointerup', this.onUp);
    window.addEventListener('pointercancel', this.onCancel);
    window.addEventListener('keydown', this.onEscape, true);
    // Stops the list panning under a finger that has committed to a drag.
    // Non-passive, or preventDefault is ignored.
    window.addEventListener('touchmove', this.onTouchMove, { passive: false });
  }

  private arm(pointerId: number): void {
    const d = this.drag;
    if (!d || d.armed || d.pointerId !== pointerId) return;
    d.armed = true;
    this.dragging.set(true);
    const row = d.rows[d.from];
    row.classList.add('dragging');
    row.style.zIndex = '2';
    row.style.position = 'relative';
    for (const r of d.rows) r.style.transition = 'none';
    d.frame = requestAnimationFrame(this.tick);
  }

  // ── moving ──────────────────────────────────────────────────────────────
  private readonly onMove = (ev: PointerEvent): void => {
    const d = this.drag;
    if (!d || ev.pointerId !== d.pointerId) return;
    d.pointerClientY = ev.clientY;

    if (!d.armed) {
      const travelled = Math.abs(ev.clientY - d.startClientY);
      // A finger that travels before the hold fired is scrolling the list.
      if (ev.pointerType === 'touch') {
        if (travelled > Reorder.SLOP) this.cancel();
        return;
      }
      if (travelled <= Reorder.SLOP) return;
      this.arm(d.pointerId);
    }
    ev.preventDefault();
    this.paint();
  };

  private readonly onTouchMove = (ev: TouchEvent): void => {
    if (this.drag?.armed) ev.preventDefault();
  };

  /** Where the row sits now, what that makes its index, and which of its
   *  neighbours have to move aside to show it. */
  private paint(): void {
    const d = this.drag;
    if (!d || !d.armed) return;
    const base = this.baseOf(d.scroller);
    const dy = (d.pointerClientY - base) - d.startY;
    const centre = d.tops[d.from] + dy + d.heights[d.from] / 2;

    // The number of other rows whose middle is above this one is exactly the
    // index it would take if dropped — the same count `splice` would need.
    let to = 0;
    for (let i = 0; i < d.rows.length; i++) {
      if (i === d.from) continue;
      if (d.tops[i] + d.heights[i] / 2 < centre) to++;
    }
    d.to = to;

    d.rows[d.from].style.transform = `translateY(${dy}px)`;
    for (let i = 0; i < d.rows.length; i++) {
      if (i === d.from) continue;
      // Where row i sits once the dragged one is lifted out of the list.
      const pos = i < d.from ? i : i - 1;
      let shift = 0;
      if (i < d.from && pos >= to) shift = d.step;
      else if (i > d.from && pos < to) shift = -d.step;
      d.rows[i].style.transform = shift ? `translateY(${shift}px)` : '';
    }
  }

  /** Scroll while an item is held near an edge, and keep painting as the
   *  list moves under it. */
  private readonly tick = (): void => {
    const d = this.drag;
    if (!d || !d.armed) return;
    const sc = d.scroller;
    if (sc) {
      const box = sc.getBoundingClientRect();
      const above = d.pointerClientY - box.top;
      const below = box.bottom - d.pointerClientY;
      let speed = 0;
      if (above < Reorder.EDGE) speed = -Reorder.MAX_SPEED * (1 - Math.max(0, above) / Reorder.EDGE);
      else if (below < Reorder.EDGE) speed = Reorder.MAX_SPEED * (1 - Math.max(0, below) / Reorder.EDGE);
      if (speed) {
        const before = sc.scrollTop;
        sc.scrollTop = Math.max(0, Math.min(d.maxScroll, before + speed));
        // Only repaint when the scroll actually moved; at either end it does
        // not, and the row should sit still rather than creep.
        if (sc.scrollTop !== before) this.paint();
      }
    }
    d.frame = requestAnimationFrame(this.tick);
  };

  // ── landing ─────────────────────────────────────────────────────────────
  private readonly onUp = (ev: PointerEvent): void => {
    const d = this.drag;
    if (!d || ev.pointerId !== d.pointerId) return;
    const dragged = d.armed;
    const moved = dragged && d.to !== d.from;
    const { from, to } = d;
    this.cancel();
    // Whether or not it landed somewhere new: a row dragged back to where it
    // started was still dragged, and still must not count as a press.
    if (dragged) this.armSwallow();
    if (moved) this.reordered.emit({ from, to });
  };

  private armSwallow(): void {
    this.swallowClick = true;
    if (this.swallowTimer) clearTimeout(this.swallowTimer);
    this.swallowTimer = setTimeout(() => { this.swallowClick = false; }, 400);
  }

  private readonly onClick = (ev: MouseEvent): void => {
    if (!this.swallowClick) return;
    this.swallowClick = false;
    if (this.swallowTimer) clearTimeout(this.swallowTimer);
    ev.preventDefault();
    ev.stopPropagation();
  };

  private readonly onCancel = (): void => this.cancel();

  private readonly onEscape = (ev: KeyboardEvent): void => {
    if (ev.key === 'Escape' && this.drag) {
      ev.stopPropagation();
      this.cancel();
    }
  };

  /** Put every row back the way it was found. Called on a clean drop as well
   *  as on an abandoned one: the list re-renders in the new order, so the
   *  transforms have to be gone before it does or the movement is counted
   *  twice. */
  private cancel(): void {
    const d = this.drag;
    if (!d) return;
    if (d.hold) clearTimeout(d.hold);
    if (d.frame) cancelAnimationFrame(d.frame);
    for (const r of d.rows) {
      r.style.transform = '';
      r.style.transition = '';
      r.style.zIndex = '';
      r.style.position = '';
      r.classList.remove('dragging');
    }
    this.drag = null;
    this.dragging.set(false);
    window.removeEventListener('pointermove', this.onMove);
    window.removeEventListener('pointerup', this.onUp);
    window.removeEventListener('pointercancel', this.onCancel);
    window.removeEventListener('keydown', this.onEscape, true);
    window.removeEventListener('touchmove', this.onTouchMove);
  }

  // ── without a pointer ───────────────────────────────────────────────────
  /** Alt+Up/Down moves the focused row. A list you can only arrange by
   *  dragging is one that cannot be arranged without a mouse. */
  onKey(ev: KeyboardEvent): void {
    if (!ev.altKey || (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown')) return;
    const rows = this.rows();
    const row = (ev.target as HTMLElement | null)?.closest<HTMLElement>(this.rowSelector());
    const from = row ? rows.indexOf(row) : -1;
    if (from < 0) return;
    const to = ev.key === 'ArrowUp' ? from - 1 : from + 1;
    if (to < 0 || to >= rows.length) return;
    ev.preventDefault();
    this.reordered.emit({ from, to });
    // The row moves out from under the focus; follow it there. The row is
    // itself the button on Home and wraps one in the library dialog, so take
    // whichever of the two is actually there.
    queueMicrotask(() => {
      const row = this.rows()[to];
      (row?.querySelector('button') ?? row)?.focus?.();
    });
  }

  // ── geometry ────────────────────────────────────────────────────────────
  private rows(): HTMLElement[] {
    return Array.from(this.host.querySelectorAll<HTMLElement>(this.rowSelector()));
  }

  /** The top of the scroller's content, in viewport terms. Subtracting it
   *  turns a viewport rectangle into a coordinate that survives scrolling. */
  private baseOf(scroller: HTMLElement | null): number {
    return scroller ? scroller.getBoundingClientRect().top - scroller.scrollTop : 0;
  }

  private scrollerOf(el: HTMLElement): HTMLElement | null {
    for (let n = el.parentElement; n; n = n.parentElement) {
      const overflow = getComputedStyle(n).overflowY;
      if ((overflow === 'auto' || overflow === 'scroll') && n.scrollHeight > n.clientHeight) {
        return n;
      }
    }
    return null;
  }
}
