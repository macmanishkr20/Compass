import {
  Directive,
  ElementRef,
  Input,
  OnDestroy,
  inject,
} from '@angular/core';
import { prefersReducedMotion } from './motion';

/**
 * Pointer-tracked 3D tilt. The element rotates toward the cursor in
 * perspective space and a soft light sheen follows the pointer, giving cards
 * a sense of physical depth. Fully disabled under prefers-reduced-motion.
 *
 * Uses rAF-batched CSS custom properties (never triggers Angular CD) so it
 * stays smooth even with many cards on screen.
 */
@Directive({
  selector: '[appTilt]',
  host: {
    '(pointerenter)': 'onEnter()',
    '(pointermove)': 'onMove($event)',
    '(pointerleave)': 'onLeave()',
  },
})
export class TiltDirective implements OnDestroy {
  /** Maximum tilt in degrees at the card edges. */
  @Input() tiltMax = 8;
  /** Lift toward the viewer on hover, in px. */
  @Input() tiltLift = 14;

  private readonly host = inject(ElementRef<HTMLElement>).nativeElement;
  private frame = 0;
  private readonly enabled = !prefersReducedMotion();

  onEnter(): void {
    if (!this.enabled) return;
    this.host.style.setProperty('--tilt-lift', `${this.tiltLift}px`);
  }

  onMove(ev: PointerEvent): void {
    if (!this.enabled) return;
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(() => {
      const r = this.host.getBoundingClientRect();
      const px = (ev.clientX - r.left) / r.width; // 0..1
      const py = (ev.clientY - r.top) / r.height; // 0..1
      const rotY = (px - 0.5) * 2 * this.tiltMax;
      const rotX = -(py - 0.5) * 2 * this.tiltMax;
      this.host.style.setProperty('--tilt-x', `${rotX.toFixed(2)}deg`);
      this.host.style.setProperty('--tilt-y', `${rotY.toFixed(2)}deg`);
      this.host.style.setProperty('--sheen-x', `${(px * 100).toFixed(1)}%`);
      this.host.style.setProperty('--sheen-y', `${(py * 100).toFixed(1)}%`);
    });
  }

  onLeave(): void {
    if (!this.enabled) return;
    cancelAnimationFrame(this.frame);
    this.host.style.setProperty('--tilt-x', '0deg');
    this.host.style.setProperty('--tilt-y', '0deg');
    this.host.style.setProperty('--tilt-lift', '0px');
  }

  ngOnDestroy(): void {
    cancelAnimationFrame(this.frame);
  }
}
