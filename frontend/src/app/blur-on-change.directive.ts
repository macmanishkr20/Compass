import { Directive, ElementRef, HostListener, inject } from '@angular/core';

/**
 * Blur a native <select> right after a value is chosen. Chrome keeps a select
 * "focused" after a mouse selection, so the focus highlight lingers until you
 * click elsewhere. Blurring on change drops the highlight immediately while
 * still showing a focus ring during keyboard navigation.
 */
@Directive({ selector: 'select[appBlurOnChange]' })
export class BlurOnChange {
  private readonly el = inject(ElementRef<HTMLSelectElement>);

  @HostListener('change')
  onChange(): void {
    // Defer so ngModel's own change handling runs first.
    queueMicrotask(() => this.el.nativeElement.blur());
  }
}
