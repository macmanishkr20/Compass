import {
  ChangeDetectionStrategy,
  Component,
  effect,
  inject,
  signal,
} from '@angular/core';
import { LightboxService } from '../lightbox.service';

/**
 * Full-screen image viewer: click an image in chat to open it, zoom in/out
 * (buttons, wheel, or double-click), pan when zoomed, and close (✕, backdrop,
 * or Esc). Rendered once in the root component; driven by LightboxService.
 */
@Component({
  selector: 'app-lightbox',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lightbox.html',
  styleUrl: './lightbox.css',
  host: { '(document:keydown)': 'onKeydown($event)' },
})
export class Lightbox {
  readonly svc = inject(LightboxService);

  // Pan offset (px), reset whenever the image or zoom returns to 1×.
  readonly tx = signal(0);
  readonly ty = signal(0);
  private dragging = false;
  private startX = 0;
  private startY = 0;

  readonly pct = () => Math.round(this.svc.scale() * 100);

  constructor() {
    effect(() => {
      // Recenter on open or when zoomed back to (or below) 1×.
      this.svc.src();
      if (this.svc.scale() <= 1) {
        this.tx.set(0);
        this.ty.set(0);
      }
    });
  }

  onKeydown(ev: KeyboardEvent): void {
    if (!this.svc.isOpen()) return;
    if (ev.key === 'Escape') this.svc.close();
    else if (ev.key === '+' || ev.key === '=') this.svc.zoomIn();
    else if (ev.key === '-' || ev.key === '_') this.svc.zoomOut();
    else if (ev.key === '0') this.svc.reset();
  }

  onWheel(ev: WheelEvent): void {
    ev.preventDefault();
    this.svc.setScale(this.svc.scale() + (ev.deltaY < 0 ? 0.2 : -0.2));
  }

  toggleZoom(): void {
    this.svc.setScale(this.svc.scale() > 1 ? 1 : 2);
  }

  onDown(ev: PointerEvent): void {
    if (this.svc.scale() <= 1) return;
    this.dragging = true;
    this.startX = ev.clientX - this.tx();
    this.startY = ev.clientY - this.ty();
    (ev.target as HTMLElement).setPointerCapture?.(ev.pointerId);
  }
  onMove(ev: PointerEvent): void {
    if (!this.dragging) return;
    this.tx.set(ev.clientX - this.startX);
    this.ty.set(ev.clientY - this.startY);
  }
  onUp(): void {
    this.dragging = false;
  }
}
