import { Injectable, computed, signal } from '@angular/core';

/** Shared full-screen image viewer state. One `<app-lightbox>` lives in the
 *  root component; any surface (agent console, Home chat) opens an image by
 *  calling `open()`. Zoom is an explicit scale controlled by +/- and wheel. */
@Injectable({ providedIn: 'root' })
export class LightboxService {
  readonly src = signal<string | null>(null);
  readonly alt = signal('Image');
  readonly scale = signal(1);
  readonly isOpen = computed(() => this.src() !== null);

  static readonly MIN = 0.25;
  static readonly MAX = 6;

  open(src: string, alt = 'Image'): void {
    if (!src) return;
    this.src.set(src);
    this.alt.set(alt);
    this.scale.set(1);
  }
  close(): void {
    this.src.set(null);
    this.scale.set(1);
  }
  zoomIn(): void {
    this.setScale(this.scale() + 0.25);
  }
  zoomOut(): void {
    this.setScale(this.scale() - 0.25);
  }
  reset(): void {
    this.scale.set(1);
  }
  setScale(s: number): void {
    const clamped = Math.min(LightboxService.MAX, Math.max(LightboxService.MIN, s));
    this.scale.set(Math.round(clamped * 100) / 100);
  }
}
