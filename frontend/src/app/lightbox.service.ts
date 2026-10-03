import { Injectable, computed, signal } from '@angular/core';

/** Shared full-screen viewer state. One `<app-lightbox>` lives in the root
 *  component; any surface (agent console, Home chat) opens an image or a
 *  film by calling `open()`. Zoom is an explicit scale controlled by +/- and
 *  wheel, and applies to pictures only — a video has its own controls and a
 *  scaled one would put them off the bottom of the screen. */
@Injectable({ providedIn: 'root' })
export class LightboxService {
  readonly src = signal<string | null>(null);
  readonly alt = signal('Image');
  /** What is being shown. Inferred from the URL when the caller does not
   *  say, so an older call site keeps working. */
  readonly kind = signal<'image' | 'video'>('image');
  /** The name a film was given, which is a real filename worth keeping —
   *  unlike a picture's, which is the id it is stored under. */
  readonly name = signal('');
  readonly scale = signal(1);
  readonly isOpen = computed(() => this.src() !== null);

  static readonly MIN = 0.25;
  static readonly MAX = 6;

  open(src: string, alt = 'Image', kind?: 'image' | 'video', name = ''): void {
    if (!src) return;
    this.src.set(src);
    this.alt.set(alt);
    this.kind.set(kind ?? (LightboxService.looksLikeVideo(src) ? 'video' : 'image'));
    this.name.set(name);
    this.scale.set(1);
  }

  /** The extensions `make_video` and the uploads route actually produce. */
  static looksLikeVideo(src: string): boolean {
    return /\.(mp4|webm|mov|m4v)(\?|$)/i.test(src);
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
