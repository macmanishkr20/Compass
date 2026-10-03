import {
  ChangeDetectionStrategy,
  Component,
  effect,
  inject,
  signal,
} from '@angular/core';
import { LightboxService } from '../lightbox.service';

/**
 * Full-screen viewer: click a picture or a film in chat to open it, save it,
 * and close it (✕, backdrop, or Esc). A picture also zooms — buttons, wheel
 * or double-click — and pans when zoomed; a film plays with its own controls
 * and ignores all of that. Rendered once in the root component; driven by
 * LightboxService.
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

  /** What the browser should call the file it saves.
   *
   *  A generated picture is stored under a 32-character hex id and a
   *  screenshot under a short one — fine as keys, poor as filenames, but
   *  still the only name the thing has. The extension is kept when there is
   *  one and assumed to be .png when there is not, which is what every
   *  route here serves. */
  fileName(): string {
    // A film arrives with the name it was rendered under, which is a real
    // one; a picture does not.
    if (this.svc.name()) return this.svc.name();
    const last = (this.svc.src() || '').split('/').pop()?.split('?')[0] || '';
    if (!last) return this.svc.kind() === 'video' ? 'video.mp4' : 'image.png';
    if (/\.[a-z0-9]{3,4}$/i.test(last)) return last;
    return this.svc.kind() === 'video' ? `${last}.mp4` : `${last}.png`;
  }

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
    if (ev.key === 'Escape') { this.svc.close(); return; }
    // The zoom keys would otherwise fight a video's own keyboard handling,
    // and there is nothing for them to do to one.
    if (this.svc.kind() === 'video') return;
    if (ev.key === '+' || ev.key === '=') this.svc.zoomIn();
    else if (ev.key === '-' || ev.key === '_') this.svc.zoomOut();
    else if (ev.key === '0') this.svc.reset();
  }

  onWheel(ev: WheelEvent): void {
    if (this.svc.kind() === 'video') return;
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
